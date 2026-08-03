"""Ticari envanter araçları: uçuş · konaklama · restoran (T2, simüle).

**Neden sahte işletme adı üretmiyoruz** (PROGRESS.md kararı, CLAUDE.md kural 3):
"Hotel Bella Roma, 4.6 puan, gece 3.200 TL" cümlesi uydurma bir işletme kaydıdır —
kullanıcı onu arar, bulamaz, güven biter. Bunun yerine adapter, gerçek API'nin
dolduracağı **yuvayı** döner: yer tipi + semt + segment + fiyat aralığı
("Trastevere'de geleneksel trattoria, orta segment"). Bu bilgi doğrudur, kullanışlıdır
ve üretimde Google Places kaydıyla değiştirilecek yeri açıkça işaret eder.

Fiyatlar küratörlü `daily_cost_try` bandından türetilir; yani "uydurulmuş" değil,
editoryal maliyet verisinden **hesaplanmıştır** ve groundedness denetimini geçer.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .. import config
from ..cache import keys
from ..knowledge import kb
from .base import cached_tool, parse_date, seeded, tl

ORIGIN_DEFAULT = "IST"

# Konaklama segmentleri: bütçe bandı -> (yer tipi, gecelik oranın günlük bütçeye payı)
_STAY_TYPES = {
    "ekonomik": [("Hostel — özel oda", 0.32), ("2★ pansiyon", 0.38), ("Kiralık stüdyo daire", 0.42)],
    "orta": [("3★ otel", 0.42), ("Butik pansiyon", 0.46), ("Kiralık 1+1 daire", 0.40)],
    "konforlu": [("4★ otel", 0.45), ("Butik tasarım otel", 0.50), ("Aparthotel", 0.43)],
    "luks": [("5★ otel", 0.48), ("Tarihi konak / butik lüks", 0.55), ("Süit apart", 0.46)],
}

# Restoran arketipleri: mutfak verisindeki yemekleri "nerede yenir" yuvasına bağlar.
_VENUE_TYPES = [
    ("geleneksel esnaf lokantası", "orta", "Yerel halkın öğle yemeği yediği, menüsü kısa yer"),
    ("çarşı/pazar içi tezgâh", "ekonomik", "Ayaküstü, en taze ve en ekonomik seçenek"),
    ("mahalle restoranı", "orta", "Akşam yemeği için rezervasyon önerilir"),
    ("üst segment yerel mutfak", "konforlu", "Klasik tarifleri modern sunumla veren adres"),
    ("kahvaltı/fırın", "ekonomik", "Sabah erken açılır, yerel kahvaltı kültürü burada"),
]


def _districts(dest_key: str) -> list[str]:
    """POI setinden semt listesi — konaklama/restoran yuvalarını gerçek semtlere bağlar."""
    seen: list[str] = []
    for p in kb.pois(dest_key):
        d = p.get("district")
        if d and d not in seen:
            seen.append(d)
    return seen or ["Merkez"]


# ─────────────────────────────────────────────────────────────────────
# Uçuş
# ─────────────────────────────────────────────────────────────────────
def search_flights(
    destination: str,
    depart_date: Optional[str] = None,
    return_date: Optional[str] = None,
    origin: str = ORIGIN_DEFAULT,
    passengers: int = 1,
) -> dict:
    """Uçuş seçeneklerini arar (simüle Amadeus adapter'ı; imza gerçek API'ye uyumlu).

    Args:
        destination: Varış destinasyonu adı/anahtarı.
        depart_date: Gidiş tarihi (YYYY-AA-GG).
        return_date: Dönüş tarihi; boşsa tek yön.
        origin: Kalkış havalimanı IATA kodu (varsayılan IST).
        passengers: Yolcu sayısı.
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key) or {}
    dep = parse_date(depart_date)
    ret = parse_date(return_date, default_offset_days=34) if return_date else None
    pax = max(1, int(passengers or 1))
    hours = float(dest.get("flight_hours_from_ist") or 3.0)

    def _produce() -> dict:
        rng = seeded("fl", origin, dest_key, dep.isoformat())
        # Temel fiyat: uçuş süresi + sezon (yoğun ay = zam) + rastgele ama deterministik sapma
        taban = 1450 + hours * 1180
        if dep.month in dest.get("best_months", []):
            taban *= 1.18
        if dep.month in (7, 8):
            taban *= 1.12
        gidis_donus = ret is not None

        secenekler = []
        for i, (etiket, carpan, aktarma) in enumerate([
            ("En ekonomik", 0.82, 1),
            ("En hızlı — direkt", 1.15, 0),
            ("Dengeli", 0.97, 0 if hours < 5 else 1),
        ]):
            fiyat = taban * carpan * (1.85 if gidis_donus else 1.0) * pax
            fiyat *= 1 + rng.uniform(-0.05, 0.05)
            sure = hours + (1.8 if aktarma else 0.0)
            secenekler.append({
                "etiket": etiket,
                "tasiyici": "Tarifeli taşıyıcı (simüle kayıt)",
                "aktarma": aktarma,
                "ucus_suresi_saat": round(sure, 1),
                "toplam_ucret_try": round(fiyat, -1),
                "kisi_basi_try": round(fiyat / pax, -1),
                "bagaj": "Kabin 8 kg dahil; kayıtlı bagaj seçeneğe göre değişir.",
            })
        secenekler.sort(key=lambda s: s["toplam_ucret_try"])
        return {
            "kalkis": origin,
            "varis": dest.get("name", destination),
            "gidis_tarihi": dep.isoformat(),
            "donus_tarihi": ret.isoformat() if ret else None,
            "yolcu": pax,
            "yon": "gidiş-dönüş" if ret else "tek yön",
            "secenekler": secenekler,
            "en_dusuk_try": secenekler[0]["toplam_ucret_try"],
            "not": "Fiyatlar simüle envanterden üretilmiştir; gerçek entegrasyonda anlık "
                   "Amadeus teklifleri gelir ve 15 dakika TTL ile önbelleklenir.",
        }

    return cached_tool(keys.flight(origin, dest_key, dep.isoformat()),
                       config.TTL_FLIGHT_SEARCH, _produce, provider="flights")


# ─────────────────────────────────────────────────────────────────────
# Konaklama
# ─────────────────────────────────────────────────────────────────────
def search_hotels(
    destination: str,
    checkin: Optional[str] = None,
    nights: int = 4,
    budget_band: str = "orta",
    guests: int = 2,
    accessible: bool = False,
) -> dict:
    """Konaklama seçeneklerini arar (simüle Amadeus/Hotelbeds adapter'ı).

    Args:
        destination: Destinasyon adı/anahtarı.
        checkin: Giriş tarihi (YYYY-AA-GG).
        nights: Gece sayısı.
        budget_band: ekonomik | orta | konforlu | luks — bütçe sert filtresi buradan uygulanır.
        guests: Misafir sayısı.
        accessible: Erişilebilir oda zorunlu mu (sert filtre).
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key) or {}
    band = budget_band if budget_band in _STAY_TYPES else "orta"
    ci = parse_date(checkin)
    nights = max(1, min(30, int(nights or 4)))
    co = ci + timedelta(days=nights)
    qid = dest.get("qid", dest_key)
    gunluk = (dest.get("daily_cost_try") or {}).get(band, 5000)

    def _produce() -> dict:
        rng = seeded("htl", dest_key, band, ci.isoformat(), int(accessible))
        semtler = _districts(dest_key)
        secenekler = []
        for i, (tip, oran) in enumerate(_STAY_TYPES[band]):
            semt = semtler[i % len(semtler)]
            gece = gunluk * oran * (1 + rng.uniform(-0.08, 0.08))
            if dest.get("walkability", 3) >= 4 and i == 0:
                semt_notu = "Merkezde; başlıca duraklara yürüme mesafesi."
            else:
                semt_notu = "Toplu taşımayla merkeze 15-25 dakika."
            secenekler.append({
                "tip": tip,
                "semt": semt,
                "segment": band,
                "gecelik_try": round(gece, -1),
                "toplam_try": round(gece * nights, -1),
                "erisilebilir_oda": True if accessible else bool(rng.random() > 0.35),
                "semt_notu": semt_notu,
                "kayit_turu": "yuva — gerçek entegrasyonda tedarikçi kaydıyla doldurulur",
            })
        if accessible:
            for s in secenekler:
                s["erisilebilir_oda"] = True
                s["not"] = "Erişilebilir oda sert filtresi uygulandı."
        secenekler.sort(key=lambda s: s["toplam_try"])
        return {
            "destinasyon": dest.get("name", destination),
            "giris": ci.isoformat(),
            "cikis": co.isoformat(),
            "gece": nights,
            "misafir": max(1, int(guests or 2)),
            "butce_bandi": band,
            "secenekler": secenekler,
            "en_dusuk_toplam_try": secenekler[0]["toplam_try"],
            "not": f"{band} bandında {nights} gece için tahmini konaklama aralığı "
                   f"{tl(secenekler[0]['toplam_try'])}–{tl(secenekler[-1]['toplam_try'])} TRY. "
                   "İşletme adları üretilmez; simüle envanter yer tipi ve semt döndürür.",
        }

    return cached_tool(keys.hotel(qid, ci.isoformat(), co.isoformat(), max(1, int(guests or 2))),
                       config.TTL_HOTEL_SEARCH, _produce, provider="hotels")


# ─────────────────────────────────────────────────────────────────────
# Restoran
# ─────────────────────────────────────────────────────────────────────
def search_restaurants(
    destination: str,
    dietary: Optional[list[str]] = None,
    budget_band: str = "orta",
    meal: str = "aksam",
) -> dict:
    """Yeme-içme önerileri (simüle Google Places adapter'ı) + küratörlü yöresel lezzetler.

    Diyet kısıtı **sert filtredir**: vegan bir kullanıcıya vegan olmayan yemek önerilmez;
    uygun yemek yoksa bunu açıkça söyleriz (uydurma seçenek üretmeyiz).

    Args:
        destination: Destinasyon adı/anahtarı.
        dietary: Diyet kısıtları listesi (vegan, vejetaryen, helal, glutensiz).
        budget_band: Bütçe bandı.
        meal: sabah | ogle | aksam
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key) or {}
    cuisine = kb.cuisine(dest_key) or {}
    diets = [d.lower() for d in (dietary or [])]
    band = budget_band if budget_band in _STAY_TYPES else "orta"

    tum_yemekler = cuisine.get("dishes", [])
    if diets:
        uygun = [d for d in tum_yemekler if all(x in [t.lower() for t in d.get("diet", [])] for x in diets)]
    else:
        uygun = list(tum_yemekler)

    rng = seeded("rest", dest_key, band, meal, ",".join(sorted(diets)))
    semtler = _districts(dest_key)
    mekanlar = []
    for i, (tip, seg, aciklama) in enumerate(_VENUE_TYPES):
        if band == "ekonomik" and seg == "konforlu":
            continue
        if band == "luks" and seg == "ekonomik" and i != 1:
            continue
        mekanlar.append({
            "yer_tipi": tip,
            "semt": semtler[(i + 1) % len(semtler)],
            "segment": seg,
            "kisi_basi_try": round((dest.get("daily_cost_try", {}).get(band, 5000)) *
                                   (0.08 if seg == "ekonomik" else 0.14 if seg == "orta" else 0.22)
                                   * (1 + rng.uniform(-0.1, 0.1)), -1),
            "aciklama": aciklama,
            "kayit_turu": "yuva — gerçek entegrasyonda Places/Foursquare kaydı gelir",
        })

    return {
        "destinasyon": dest.get("name", destination),
        "ogun": meal,
        "diyet_filtresi": diets,
        "yoresel_lezzetler": [
            {"ad": d["name"], "aciklama": d.get("aciklama", ""),
             "diyet": d.get("diet", []), "fiyat_bandi": d.get("fiyat_bandi", "orta")}
            for d in uygun[:6]
        ],
        "elenen_lezzet_sayisi": len(tum_yemekler) - len(uygun),
        "diyet_uyarisi": (
            f"Küratörlü mutfak verisinde {', '.join(diets)} uyumlu yöresel yemek bulunamadı; "
            "uydurma seçenek üretmiyoruz. Uluslararası mutfak seçenekleri önerilebilir."
            if diets and not uygun else None
        ),
        "mekan_onerileri": mekanlar[:4],
        "yemek_saatleri": cuisine.get("yemek_saatleri", ""),
        "ipuclari": cuisine.get("ipuclari", [])[:3],
        "vegan_notu": cuisine.get("vegan_notu", ""),
        "_kaynak": {"baslik": "Pusula Mutfak Rehberi (T0) + Places envanteri (T2, simüle)",
                    "kademe": "T0/T2",
                    "not": "Yemek ve mutfak kültürü küratörlü; mekân yuvaları simüle"},
        "_simule": True,
        "_cache": "yok (küratörlü veriden türetildi)",
    }
