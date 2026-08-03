"""Küratörlü içerik araçları (T0): POI · kültür · pratik bilgi · mutfak · SSS · destinasyon skorlama.

Bu araçlar veri **üretmez**, küratörlü bilgi tabanını (`knowledge/*.json`) sorgular.
Groundedness açısından en güvenli kaynak budur: dönen her sayı editoryal veriden gelir.

`estimate_travel_time` kasıtlı olarak harita API'si kullanmaz. Elimizde koordinat yok;
uydurma "2,4 km" demek yerine **semt tabanlı** ve gerekçesi açıklanabilir bir tahmin
üretiyoruz: aynı semt = yürüme, farklı semt = toplu taşıma, şehrin yürünebilirlik
puanı çarpan. Plan doğrulayıcısının tempo denetimi bu tahmine dayanır.
"""
from __future__ import annotations

from typing import Optional

from .. import config
from ..cache import keys
from ..knowledge import kb
from .base import cached_tool, source_of, tl

# Yürünebilirlik puanı (1-5) -> semtler arası tipik ulaşım süresi (dakika)
_TRANSIT_BY_WALKABILITY = {5: 18, 4: 22, 3: 28, 2: 35, 1: 45}
_SAME_DISTRICT_MIN = 10


def get_pois(
    destination: str,
    category: Optional[str] = None,
    accessible_only: bool = False,
    indoor_only: bool = False,
    max_cost_try: Optional[float] = None,
    limit: int = 10,
) -> dict:
    """Destinasyonun gezilecek yerlerini küratörlü POI setinden döner.

    Args:
        destination: Destinasyon adı/anahtarı.
        category: tarih | müze | manzara | pazar | doğa | semt | tapınak vb.
        accessible_only: Yalnızca tekerlekli sandalyeyle erişilebilir duraklar (sert filtre).
        indoor_only: Yalnızca kapalı mekânlar — yağmurlu gün planı için.
        max_cost_try: Kişi başı üst ücret sınırı.
        limit: Azami kayıt sayısı.
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key) or {}
    qid = dest.get("qid", dest_key)

    def _produce() -> dict:
        rows = []
        for p in kb.pois(dest_key):
            if category and p.get("category") != category:
                continue
            if accessible_only and not p.get("accessible"):
                continue
            if indoor_only and not p.get("indoor"):
                continue
            if max_cost_try is not None and float(p.get("cost_try", 0)) > float(max_cost_try):
                continue
            rows.append({
                "anahtar": p["key"], "ad": p["name"], "semt": p.get("district", ""),
                "kategori": p.get("category", ""), "sure_dk": p.get("duration_min", 90),
                "ucret_try": p.get("cost_try", 0), "onerilen_zaman": p.get("best_time", ""),
                "kapali_mekan": p.get("indoor", False), "erisilebilir": p.get("accessible", False),
                "not": p.get("note", ""),
            })
        return {
            "destinasyon": dest.get("name", destination),
            "destinasyon_anahtari": dest_key,
            "toplam": len(rows),
            "duraklar": rows[: max(1, int(limit or 10))],
            "filtreler": {"kategori": category, "erisilebilir": accessible_only,
                          "kapali_mekan": indoor_only, "ust_ucret": max_cost_try},
        }

    return cached_tool(keys.poi(qid, category or "hepsi"), config.TTL_POI, _produce, provider="pois")


def estimate_travel_time(destination: str, from_district: str, to_district: str) -> dict:
    """İki semt arası tahmini ulaşım süresi (harita API'si yok — semt tabanlı model).

    Args:
        destination: Destinasyon adı/anahtarı.
        from_district: Kalkış semti.
        to_district: Varış semti.
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key) or {}
    walk = int(dest.get("walkability", 3) or 3)

    if not from_district or from_district == to_district:
        dakika, mod = _SAME_DISTRICT_MIN, "yürüyerek"
    else:
        dakika = _TRANSIT_BY_WALKABILITY.get(walk, 28)
        mod = "yürüyerek/kısa toplu taşıma" if walk >= 4 else "toplu taşıma"

    return {
        "destinasyon": dest.get("name", destination),
        "nereden": from_district or "—",
        "nereye": to_district,
        "tahmini_dakika": dakika,
        "ulasim_modu": mod,
        "yontem": (f"Semt tabanlı tahmin: {dest.get('name', destination)} yürünebilirlik puanı "
                   f"{walk}/5. Aynı semtte {_SAME_DISTRICT_MIN} dk, semtler arası {dakika} dk."),
        "not": "Koordinat verisi tutulmadığı için mesafe uydurulmaz; üretimde Directions API bağlanır.",
        "_kaynak": source_of("curated"), "_simule": False, "_cache": "yok (yerel hesap)",
    }


def get_local_dishes(destination: str, dietary: Optional[list[str]] = None) -> dict:
    """Yöresel lezzetler ve mutfak kültürü (küratörlü T0). Diyet kısıtı sert filtredir."""
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    cuisine = kb.cuisine(dest_key) or {}
    diets = [d.lower() for d in (dietary or [])]
    dishes = cuisine.get("dishes", [])
    if diets:
        dishes = [d for d in dishes if all(x in [t.lower() for t in d.get("diet", [])] for x in diets)]
    return {
        "destinasyon": (kb.destination(dest_key) or {}).get("name", destination),
        "ozet": cuisine.get("ozet", ""),
        "yemek_saatleri": cuisine.get("yemek_saatleri", ""),
        "lezzetler": dishes,
        "ipuclari": cuisine.get("ipuclari", []),
        "vegan_notu": cuisine.get("vegan_notu", ""),
        "diyet_filtresi": diets,
        "_kaynak": source_of("curated"), "_simule": False,
    }


def get_culture_notes(destination: str) -> dict:
    """Kültür, görgü kuralları, kıyafet, bahşiş, dini hassasiyet (küratörlü T0).

    Halüsinasyona en açık alan burasıdır: kültürel iddia doğrulanması zor ama yanlışı
    incitici olabilir. Bu yüzden **yalnızca** küratörlü içerikten konuşulur.
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    c = kb.culture(dest_key) or {}
    if not c:
        return {"hata": f"'{destination}' için küratörlü kültür rehberi bulunmuyor.",
                "_kaynak": source_of("curated"), "_simule": False}
    return {
        "destinasyon": (kb.destination(dest_key) or {}).get("name", destination),
        "selamlasma": c.get("selamlasma", ""), "kiyafet": c.get("kiyafet", ""),
        "gorgu_kurallari": c.get("gorgu", []), "bahsis": c.get("bahsis", ""),
        "pazarlik": c.get("pazarlik", ""), "dini_hassasiyet": c.get("dini_hassasiyet", ""),
        "guvenlik": c.get("guvenlik", ""), "kacinilmasi_gerekenler": c.get("kacinilmasi_gerekenler", []),
        "dil_ipuclari": c.get("dil_ipuclari", {}),
        "_kaynak": source_of("curated"), "_simule": False,
    }


def get_practical_facts(destination: str) -> dict:
    """Priz, voltaj, acil numaralar, musluk suyu, internet, ulaşım kartı, para birimi (T1/T0)."""
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    p = kb.practical(dest_key) or {}
    if not p:
        return {"hata": f"'{destination}' için pratik bilgi kaydı bulunmuyor.",
                "_kaynak": source_of("curated"), "_simule": False}
    return {
        "destinasyon": (kb.destination(dest_key) or {}).get("name", destination),
        "saat_dilimi": p.get("timezone", ""), "para_birimi": p.get("currency", {}),
        "elektrik": p.get("elektrik", {}), "acil_numaralar": p.get("acil_numaralar", {}),
        "musluk_suyu": p.get("musluk_suyu", ""), "internet": p.get("internet", ""),
        "ulasim_karti": p.get("ulasim_karti", ""),
        "_kaynak": {"baslik": "Resmî acil hizmet bilgileri ve IEC priz standartları",
                    "kademe": "T1", "not": "Kamusal kaynak"},
        "_simule": False,
    }


def get_seasonality(destination: str, month: Optional[int] = None) -> dict:
    """Destinasyonun sezon uygunluğu: ideal aylar, kaçınılacak aylar, gerekçe."""
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    d = kb.destination(dest_key) or {}
    if not d:
        return {"hata": f"'{destination}' bilgi tabanında yok.", "_kaynak": source_of("curated")}
    best, avoid = d.get("best_months", []), d.get("avoid_months", [])
    durum = None
    if month:
        durum = ("ideal" if month in best else "uygun değil" if month in avoid else "ortalama")
    return {
        "destinasyon": d.get("name", destination), "ideal_aylar": best, "kacinilacak_aylar": avoid,
        "sorulan_ay": month, "sorulan_ay_durumu": durum, "sezon_notu": d.get("seasonality_note", ""),
        "_kaynak": source_of("curated"), "_simule": False,
    }


def estimate_trip_cost(
    destination: str, nights: int = 4, budget_band: str = "orta", travelers: int = 1
) -> dict:
    """Toplam seyahat maliyeti tahmini: uçuş + konaklama + günlük harcama.

    Konaklama ve günlük harcama küratörlü `daily_cost_try` bandından, uçuş ise
    uçuş adapter'ından gelir — hiçbir kalem uydurulmaz.
    """
    from .inventory import search_flights  # döngüsel import olmasın diye yerel

    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    d = kb.destination(dest_key) or {}
    band = budget_band if budget_band in ("ekonomik", "orta", "konforlu", "luks") else "orta"
    nights = max(1, int(nights or 4))
    pax = max(1, int(travelers or 1))
    gunluk = float((d.get("daily_cost_try") or {}).get(band, 5000))

    ucus = search_flights(dest_key, passengers=pax)
    ucus_try = float(ucus.get("en_dusuk_try", 0) or 0)
    yer_try = gunluk * nights * pax
    toplam = ucus_try + yer_try

    return {
        "destinasyon": d.get("name", destination), "gece": nights, "kisi": pax, "butce_bandi": band,
        "ucus_try": round(ucus_try, -1),
        "gunluk_harcama_try": round(gunluk, -1),
        "yerinde_toplam_try": round(yer_try, -1),
        "tahmini_toplam_try": round(toplam, -1),
        "kisi_basi_try": round(toplam / pax, -1),
        "aciklama": (f"{band} bandında günlük {tl(gunluk)} TRY (konaklama + yeme-içme + ulaşım + "
                     f"giriş ücretleri) × {nights} gece × {pax} kişi + uçuş {tl(ucus_try)} TRY."),
        "_kaynak": {"baslik": "Küratörlü maliyet bandı (T0) + uçuş envanteri (T2, simüle)",
                    "kademe": "T0/T2", "not": "Uçuş kalemi simüle"},
        "_simule": True,
    }


def search_destinations(
    styles: Optional[list[str]] = None,
    budget_total_try: Optional[float] = None,
    budget_band: Optional[str] = None,
    month: Optional[int] = None,
    nights: int = 4,
    limit: int = 4,
) -> dict:
    """Tercihlere uygun destinasyonları skorlayarak sıralar (Çıktı 7 formülü).

    Sert filtreler önce uygulanır (bütçe üst sınırı, erişilebilirlik, diyet), sonra
    skor formülüyle yumuşak sıralama yapılır. Elenenler de sebebiyle döner.
    """
    from .. import preferences as pref  # döngüsel import olmasın diye yerel
    from ..schemas import TravelerProfile

    profile = TravelerProfile(
        styles=[s for s in (styles or []) if s in
                ("kultur", "gastronomi", "doga", "plaj", "macera", "alisveris", "gece_hayati", "dini")],
        budget_total=budget_total_try, budget_band=budget_band,  # type: ignore[arg-type]
    )
    oneriler, elenenler = pref.recommend(profile, month=month, nights=nights, limit=limit)
    return {
        "oneriler": [
            {"anahtar": s.key, "ad": s.name, "ulke": s.country, "ozet": s.summary,
             "tahmini_maliyet_try": s.est_cost_try, "ideal_aylar": s.best_months,
             "skor": s.score.total, "gerekceler": s.score.notes[:4]}
            for s in oneriler
        ],
        "elenenler": elenenler,
        "kriterler": {"stiller": styles, "butce_try": budget_total_try, "bant": budget_band,
                      "ay": month, "gece": nights},
        "_kaynak": source_of("curated"), "_simule": False,
    }


def search_faq(query: str, category: Optional[str] = None, limit: int = 3) -> dict:
    """SSS bilgi tabanında arama (13 kategorili taksonomi, hibrit retrieval)."""
    hits = kb.search(query, k=max(1, int(limit or 3)), collections=["faq"],
                     category=category, auto_dest=False)
    return {
        "sorgu": query, "kategori_filtresi": category,
        "sonuclar": [
            {"id": h.doc.id, "soru": h.doc.title, "cevap": h.doc.text,
             "kategori": h.doc.category, "kademe": h.doc.tier,
             "yuksek_risk": h.doc.high_risk, "kaynak": h.doc.source,
             "gecerlilik": h.doc.valid_until, "skor": h.score}
            for h in hits
        ],
        "kategoriler": kb.faq_categories,
        "_kaynak": source_of("curated"), "_simule": False,
    }
