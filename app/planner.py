"""Gezi planı üretici ve **doğrulayıcı**.

İki işlevi var ve ikisi de mimarinin can damarı:

**1. `build_itinerary` — deterministik plan kurucu.**
Küratörlü POI havuzundan günlük planı kurar: semte göre kümeler (ulaşım süresini düşürür),
`best_time` alanına göre gün içinde sıralar, tempoyu profile göre ayarlar, kapalı günleri
atlar, yağmurlu güne kapalı mekân yerleştirir. LLM olmadan da tam çalışır — mock modun
plan üretme yeteneği buradan gelir (CLAUDE.md kural 2).

**2. `validate_itinerary` — planın hakemi.**
Gerçek modda `itinerary_architect` ajanı `output_schema=Itinerary` ile plan üretir.
Yapılandırılmış çıktı **parse** hatasını çözer ama **olgu** hatasını çözmez: model
var olmayan bir müze uydurabilir, bütçeyi aşabilir, bir güne 11 saat sıkıştırabilir.
Doğrulayıcı bunları yakalar; `severity="error"` varsa plan kullanıcıya sunulmadan
deterministik plana düşülür. Halüsinasyona karşı **son kapı** budur.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any, Optional

from .knowledge import kb
from .schemas import Itinerary, ItineraryDay, ItinerarySlot, TravelerProfile, ValidationReport
from .tools.content import estimate_travel_time

# Tempo -> (günlük POI sayısı, günlük azami dolu dakika)
PACE_PROFILE = {
    "sakin":   (2, 380),
    "dengeli": (3, 500),
    "yogun":   (4, 620),
}
DEFAULT_PACE = "dengeli"
# Bu süreyi aşan tek durak "tam gün turu"dur (Ayutthaya, Versay, balon turu…): bölünemez,
# bu yüzden tempo sınırını aşması bir plan hatası değil, o aktivitenin doğasıdır.
FULL_DAY_MIN = 300

# Zaman dilimi sıralaması — gün içi akış bu sıraya göre kurulur
TIME_ORDER = ["sabah", "ogle", "ikindi", "aksam", "gece"]

# POI kategorisi -> seyahat stili (tercih uyumu skorunu besler)
CATEGORY_STYLE = {
    "tarih": "kultur", "müze": "kultur", "mimari": "kultur", "semt": "kultur", "meydan": "kultur",
    "köprü": "kultur", "tapınak": "dini", "dini": "dini",
    "pazar": "gastronomi", "gastronomi": "gastronomi", "yeme-içme": "gastronomi",
    "doğa": "doga", "park": "doga", "bahçe": "doga", "manzara": "doga",
    "plaj": "plaj", "macera": "macera", "deneyim": "macera", "tur": "macera",
    "aile": "doga", "ulaşım": "kultur",
}

_GUNLER = {"pazartesi": 0, "salı": 1, "sali": 1, "çarşamba": 2, "carsamba": 2, "perşembe": 3,
           "persembe": 3, "cuma": 4, "cumartesi": 5, "pazar": 6}
_KAPALI_RE = re.compile(
    r"(pazartesi|salı|sali|çarşamba|carsamba|perşembe|persembe|cuma|cumartesi|pazar)"
    r"\s*(?:günleri|günü|gunleri|gunu)?\s*(?:[^.]{0,25}?)\bkapalı",
    re.IGNORECASE,
)
# "Pazar günleri ARAÇ TRAFİĞİNE kapalı" ziyarete kapalı demek değildir — bu, planı
# bozmayan (hatta iyileştiren) bir bilgidir; kapalı gün sayılmamalı.
_TRAFIK_RE = re.compile(r"(trafi[ğg]|ara[çc]|otomobil)", re.IGNORECASE)


def closed_weekdays(note: str) -> set[int]:
    """POI notundan kapalı günleri çıkarır. 'Salı kapalı.' -> {1}"""
    days: set[int] = set()
    for m in _KAPALI_RE.finditer(note or ""):
        parca = (note or "")[m.start():m.end()]
        if _TRAFIK_RE.search(parca):
            continue  # araç trafiğine kapalı -> ziyarete açık
        idx = _GUNLER.get(m.group(1).lower())
        if idx is not None:
            days.add(idx)
    return days


# ─────────────────────────────────────────────────────────────────────
# Plan kurucu
# ─────────────────────────────────────────────────────────────────────
def _poi_priority(poi: dict, profile: TravelerProfile) -> float:
    """Bu durak bu kullanıcı için ne kadar öncelikli? (sıralama için, skor değil)"""
    score = 1.0
    style = CATEGORY_STYLE.get(poi.get("category", ""), "kultur")
    if profile.styles:
        score += 1.4 if style in profile.styles else -0.25
    if profile.budget_band == "ekonomik" and float(poi.get("cost_try", 0)) == 0:
        score += 0.45
    if profile.group == "aile_cocuklu" and poi.get("category") in ("park", "aile", "doğa", "deneyim"):
        score += 0.5
    if profile.pace == "sakin" and int(poi.get("duration_min", 90)) > 180:
        score -= 0.3
    if "ikonik" in (poi.get("tags") or []):
        score += 0.35
    return score


def _meal_slot(dest_key: str, band: str, when: str, dietary: list[str],
               gun: int = 1, variant: int = 0) -> Optional[ItinerarySlot]:
    """Öğün yuvası — yemek adı küratörlü mutfak verisinden gelir, uydurulmaz.

    Yemek **güne göre döner**: üç günlük planda her gün aynı Svíčková + Guláš ikilisini
    yazmak, küratörlü mutfak verisini boşa harcamak ve planı yapay göstermekti.
    """
    cuisine = kb.cuisine(dest_key) or {}
    dishes = cuisine.get("dishes", [])
    if dietary:
        dishes = [d for d in dishes
                  if all(x.lower() in [t.lower() for t in d.get("diet", [])] for x in dietary)]
    dest = kb.destination(dest_key) or {}
    gunluk = float((dest.get("daily_cost_try") or {}).get(band, 5000))
    oran = 0.11 if when == "ogle" else 0.17
    if dishes:
        # Öğle ve akşam farklı yemeğe düşsün; gün ve varyant ilerledikçe liste dönsün.
        kaydir = (gun - 1) * 2 + (0 if when == "ogle" else 1) + variant
        d = dishes[kaydir % len(dishes)]
        baslik = f"{'Öğle' if when == 'ogle' else 'Akşam'} yemeği — {d['name']}"
        detay = d.get("aciklama", "")
    else:
        baslik = f"{'Öğle' if when == 'ogle' else 'Akşam'} yemeği"
        detay = ("Diyet kısıtınıza uyan küratörlü yöresel yemek kaydımız yok; "
                 "uluslararası mutfak seçenekleri tercih edilebilir.")
    return ItinerarySlot(
        time=when, title=baslik, detail=detay, duration_min=60 if when == "ogle" else 90,
        cost_try=round(gunluk * oran, -1), tags=["yemek"] + list(dietary),
    )


def build_itinerary(
    destination: str,
    days: int = 3,
    profile: Optional[TravelerProfile] = None,
    start_date: Optional[str] = None,
    *,
    rainy_days: Optional[list[int]] = None,
    variant: int = 0,
) -> Itinerary:
    """Küratörlü POI havuzundan çok günlük plan kurar.

    Args:
        destination: Destinasyon adı/anahtarı.
        days: Gün sayısı (1-10).
        profile: Gezgin profili — tempo, stil, bütçe, erişilebilirlik buradan okunur.
        start_date: Başlangıç tarihi (YYYY-AA-GG); kapalı gün denetimi için kullanılır.
        rainy_days: Yağmur beklenen gün numaraları (1'den başlar) — o günlere kapalı mekân öncelenir.
        variant: Kullanıcı planı beğenmediğinde artırılır. Semt sırası ve durak öncelikleri
            döndürülerek **gerçekten farklı** bir kurgu üretilir; aynı havuzdan aynı planı
            tekrar sunmak "başka bir plan yap" isteğine verilebilecek en kötü cevaptır.
    """
    profile = profile or TravelerProfile()
    dest_key = kb.resolve_destination(destination) or (destination or "").lower().strip()
    dest = kb.destination(dest_key) or {}
    days = max(1, min(10, int(days or 3)))
    band = profile.budget_band or "orta"
    pace = profile.pace or DEFAULT_PACE
    per_day, _ = PACE_PROFILE.get(pace, PACE_PROFILE[DEFAULT_PACE])
    rainy = set(rainy_days or [])

    start = None
    if start_date:
        try:
            start = date.fromisoformat(str(start_date)[:10])
        except ValueError:
            start = None

    _, gunluk_limit = PACE_PROFILE.get(pace, PACE_PROFILE[DEFAULT_PACE])
    OGUN_DK = 150  # öğle 60 + akşam 90; tempo bütçesinden önce bunlar düşülür
    poi_butcesi = gunluk_limit - OGUN_DK

    havuz = list(kb.pois(dest_key))
    if profile.accessibility:
        havuz = [p for p in havuz if p.get("accessible")]  # sert filtre
    # Varyant, öncelik sıralamasına deterministik bir eğim ekler: aynı havuzdan farklı
    # ama yine tutarlı bir plan çıkar (rastgelelik yok, tekrar üretilebilir).
    def _oncelik(p: dict) -> float:
        taban = _poi_priority(p, profile)
        if not variant:
            return taban
        h = int(hashlib.md5(f"{p['key']}|{variant}".encode("utf-8")).hexdigest()[:8], 16)
        return taban + (h % 100) / 100.0

    havuz.sort(key=_oncelik, reverse=True)

    # Semte göre kümele: aynı gün aynı semt = ulaşım süresi düşer
    kumeler: dict[str, list[dict]] = {}
    for p in havuz:
        kumeler.setdefault(p.get("district", "Merkez"), []).append(p)
    sirali_semtler = sorted(
        kumeler, key=lambda d: sum(_oncelik(p) for p in kumeler[d]), reverse=True
    )
    if variant and len(sirali_semtler) > 1:
        kaydir = variant % len(sirali_semtler)
        sirali_semtler = sirali_semtler[kaydir:] + sirali_semtler[:kaydir]

    kullanilan: set[str] = set()
    gunler: list[ItineraryDay] = []

    for gun_no in range(1, days + 1):
        gun_tarihi = start + timedelta(days=gun_no - 1) if start else None
        weekday = gun_tarihi.weekday() if gun_tarihi else None
        yagmurlu = gun_no in rainy

        def _uygun(p: dict) -> bool:
            if p["key"] in kullanilan:
                return False
            if weekday is not None and weekday in closed_weekdays(p.get("note", "")):
                return False
            return True

        # Adaylar: önce günün semti (aynı semt = az ulaşım), sonra havuzun kalanı.
        # Kalan günlere eşit dağıtmak için gün başına hedef adet hesaplanır; yoksa ilk
        # günler dolar, son günler tek duraklı kalır.
        kalan_poi = [p for p in havuz if _uygun(p)]
        if not kalan_poi:
            break  # POI havuzu tükendi; uydurma durak eklemektense planı kısa bırakırız
        kalan_gun = days - gun_no + 1
        hedef_adet = max(1, min(per_day, round(len(kalan_poi) / kalan_gun)))

        gun_semti = next((s for s in sirali_semtler if any(_uygun(p) for p in kumeler[s])), None)
        adaylar = [p for p in kumeler.get(gun_semti, []) if _uygun(p)]
        adaylar += [p for p in kalan_poi if p not in adaylar]
        if yagmurlu:  # yağmur planı: kapalı mekânlar öne
            adaylar.sort(key=lambda p: (not p.get("indoor", False), -_poi_priority(p, profile)))

        # Dakika bütçeli doldurma: tempo sınırı POI SAYISINA değil, GEÇEN SÜREYE bakar.
        # 240 dakikalık tek müze, 45 dakikalık üç duraktan daha yorucudur.
        secilenler: list[dict] = []
        dolu_dk = 0
        onceki = None
        for p in adaylar:
            if len(secilenler) >= hedef_adet:
                break
            gecis = 0 if onceki is None else int(
                estimate_travel_time(dest_key, onceki.get("district", ""), p.get("district", ""))["tahmini_dakika"]
            )  # noqa: E501
            maliyet_dk = int(p.get("duration_min", 90)) + gecis
            if secilenler and dolu_dk + maliyet_dk > poi_butcesi:
                continue  # sığmıyor -> sonraki (daha kısa) adaya bak
            secilenler.append(p)
            dolu_dk += maliyet_dk
            onceki = p
        if not secilenler:
            secilenler = [adaylar[0]]  # en az bir durak: gün boş kalmasın

        secilenler.sort(key=lambda p: TIME_ORDER.index(p.get("best_time", "sabah"))
                        if p.get("best_time") in TIME_ORDER else 1)

        slots: list[ItinerarySlot] = []
        onceki_semt = None
        ogle_eklendi = False
        for p in secilenler:
            kullanilan.add(p["key"])
            semt = p.get("district", "")
            gecis = 0
            if onceki_semt is not None:
                gecis = int(estimate_travel_time(dest_key, onceki_semt, semt)["tahmini_dakika"])
            zaman = p.get("best_time") if p.get("best_time") in TIME_ORDER else "sabah"

            if not ogle_eklendi and zaman in ("ikindi", "aksam", "gece"):
                meal = _meal_slot(dest_key, band, "ogle", profile.dietary, gun_no, variant)
                if meal:
                    slots.append(meal)
                ogle_eklendi = True

            slots.append(ItinerarySlot(
                time=zaman, title=p["name"],
                detail=(p.get("note") or "") + (
                    f" ({p.get('duration_note')})" if p.get("duration_note") else ""),
                poi_key=p["key"], duration_min=int(p.get("duration_min", 90)),
                cost_try=float(p.get("cost_try", 0)), travel_min_from_prev=gecis,
                tags=[semt] + list(p.get("tags") or []),
            ))
            onceki_semt = semt

        # Tam gün turu (tek durak, 5+ saat): öğle yemeği turun içinde yenir, ayrı yuva açmayız.
        tam_gun = len(secilenler) == 1 and int(secilenler[0].get("duration_min", 0)) >= FULL_DAY_MIN
        if not ogle_eklendi and not tam_gun:
            meal = _meal_slot(dest_key, band, "ogle", profile.dietary, gun_no, variant)
            if meal:
                slots.append(meal)
        aksam = _meal_slot(dest_key, band, "aksam", profile.dietary, gun_no, variant)
        if aksam:
            slots.append(aksam)
        slots.sort(key=lambda s: TIME_ORDER.index(s.time))

        semtler = [s.tags[0] for s in slots if s.poi_key and s.tags]
        tema = semtler[0] if semtler else dest.get("name", "")
        if yagmurlu:
            tema += " (yağmur planı: kapalı mekân ağırlıklı)"

        gunler.append(ItineraryDay(
            day=gun_no, date=gun_tarihi, theme=tema, slots=slots,
        ))

    itin = Itinerary(
        destination=dest.get("name", destination), destination_key=dest_key or None, days=gunler,
        notes=_plan_notes(dest, profile, len(gunler), days, variant),
        sources=[],
    )
    itin.recompute_cost()
    return itin


def _plan_notes(dest: dict, profile: TravelerProfile, uretilen: int, istenen: int,
                variant: int = 0) -> list[str]:
    notes: list[str] = []
    if variant:
        notes.append(f"Bu {variant + 1}. kurgu: duraklar farklı semt sırasıyla dağıtıldı ve "
                     "öğünler döndürüldü. Havuz aynı küratörlü POI setidir; uydurma durak "
                     "eklenmez.")
    if uretilen < istenen:
        notes.append(f"Küratörlü POI havuzu {uretilen} güne yetti; kalan günler için uydurma "
                     "durak eklemedik. Çevre gezileri veya serbest zaman önerilebilir.")
    if profile.accessibility:
        notes.append("Erişilebilirlik sert filtresi uygulandı: yalnızca tekerlekli sandalyeyle "
                     "makul erişimi olan duraklar planlandı.")
    if profile.dietary:
        notes.append(f"Öğünler {', '.join(profile.dietary)} kısıtına göre seçildi.")
    if dest.get("accessibility_note"):
        notes.append(dest["accessibility_note"])
    notes.append("Giriş ücretleri küratörlü tahminlerdir; müze/ören yeri fiyatları sezona göre "
                 "değişebilir.")
    return notes


# ─────────────────────────────────────────────────────────────────────
# Doğrulayıcı
# ─────────────────────────────────────────────────────────────────────
def validate_itinerary(
    itinerary: Itinerary,
    profile: Optional[TravelerProfile] = None,
    *,
    budget_total: Optional[float] = None,
) -> ValidationReport:
    """Planı bütçe / tempo / mesafe / erişilebilirlik / kapalı gün / gerçeklik açısından denetler.

    `severity="error"` bulunursa plan **sunulmaz**; çağıran taraf deterministik plana döner.
    """
    profile = profile or TravelerProfile()
    report = ValidationReport()
    dest_key = itinerary.destination_key or kb.resolve_destination(itinerary.destination or "")
    pace = profile.pace or DEFAULT_PACE
    _, gunluk_limit = PACE_PROFILE.get(pace, PACE_PROFILE[DEFAULT_PACE])

    if not itinerary.days:
        report.add("error", "EMPTY_ITINERARY", "Plan hiç gün içermiyor.")
        return report

    bilinen = {p["key"]: p for p in (kb.pois(dest_key) if dest_key else [])}
    gorulen: dict[str, int] = {}

    for gun in itinerary.days:
        if not gun.slots:
            report.add("error", "EMPTY_DAY", f"{gun.day}. gün boş.", gun.day)
            continue

        dolu = gun.total_minutes
        poi_slotlari = [s for s in gun.slots if s.poi_key]
        tam_gun = len(poi_slotlari) == 1 and poi_slotlari[0].duration_min >= FULL_DAY_MIN
        if tam_gun and dolu > gunluk_limit:
            report.add("info", "FULL_DAY_TRIP",
                       f"{gun.day}. gün tek bir tam gün turuna ayrılmış "
                       f"({poi_slotlari[0].title}, {poi_slotlari[0].duration_min // 60} saat). "
                       "Bölünemeyen aktivite olduğu için tempo sınırı uygulanmadı.", gun.day)
        elif dolu > gunluk_limit:
            report.add("error" if dolu > gunluk_limit * 1.25 else "warning", "PACE_OVERLOAD",
                       f"{gun.day}. gün {dolu // 60} saat {dolu % 60} dakika dolu; "
                       f"'{pace}' tempo için üst sınır {gunluk_limit // 60} saat. "
                       "Bir durağı başka güne almak gerekir.", gun.day)
        elif dolu < gunluk_limit * 0.35:
            report.add("info", "PACE_UNDERUSE",
                       f"{gun.day}. gün oldukça boş ({dolu} dk); bir durak daha eklenebilir.", gun.day)

        yol = sum(s.travel_min_from_prev for s in gun.slots)
        if dolu and yol > dolu * 0.28:
            report.add("warning", "TRAVEL_HEAVY",
                       f"{gun.day}. günün {yol} dakikası yolda geçiyor. Aynı semtteki durakları "
                       "aynı güne toplamak süreyi kısaltır.", gun.day)

        if not any("yemek" in (s.tags or []) for s in gun.slots):
            report.add("info", "NO_MEAL", f"{gun.day}. günde öğün planlanmamış.", gun.day)

        for s in gun.slots:
            if not s.poi_key:
                continue
            gorulen[s.poi_key] = gorulen.get(s.poi_key, 0) + 1

            # Gerçeklik denetimi: LLM var olmayan bir durak uydurduysa burada yakalanır.
            poi = bilinen.get(s.poi_key)
            if bilinen and poi is None:
                report.add("error", "UNKNOWN_POI",
                           f"'{s.title}' ({s.poi_key}) küratörlü POI setinde yok. Doğrulanmamış "
                           "durak plana konmaz.", gun.day)
                continue
            if poi is None:
                continue

            if profile.accessibility and not poi.get("accessible"):
                report.add("error", "ACCESSIBILITY_VIOLATION",
                           f"'{poi['name']}' erişilebilirlik ihtiyacınıza uygun değil; "
                           "sert filtre ihlal edilemez.", gun.day)

            if gun.date:
                kapali = closed_weekdays(poi.get("note", ""))
                if gun.date.weekday() in kapali:
                    adlar = [g for g, i in _GUNLER.items() if i == gun.date.weekday()]
                    report.add("error", "CLOSED_ON_DAY",
                               f"'{poi['name']}' {adlar[0]} günü kapalı ({gun.date.isoformat()}). "
                               "Başka bir güne alınmalı.", gun.day)

            beklenen = float(poi.get("cost_try", 0))
            if beklenen and abs(s.cost_try - beklenen) > max(50.0, beklenen * 0.25):
                report.add("warning", "COST_MISMATCH",
                           f"'{poi['name']}' için plandaki ücret {s.cost_try:.0f} TRY, küratörlü "
                           f"veri {beklenen:.0f} TRY. Küratörlü değer esastır.", gun.day)

    for key, adet in gorulen.items():
        if adet > 1:
            report.add("error", "DUPLICATE_POI",
                       f"'{bilinen.get(key, {}).get('name', key)}' plana {adet} kez konmuş.")

    limit = budget_total if budget_total is not None else profile.budget_total
    toplam = itinerary.recompute_cost()
    if limit:
        if toplam > limit:
            report.add("error", "BUDGET_EXCEEDED",
                       f"Planın aktivite+yemek maliyeti {toplam:.0f} TRY, bütçe üst sınırınız "
                       f"{limit:.0f} TRY. Ücretli duraklar ücretsizlerle değiştirilmeli.")
        elif toplam > limit * 0.9:
            report.add("warning", "BUDGET_TIGHT",
                       f"Plan bütçenizin %{toplam / limit * 100:.0f}'ini kullanıyor; "
                       "konaklama ve ulaşım bu tutara dahil değil.")
    return report


# ─────────────────────────────────────────────────────────────────────
# Agno araç sarmalayıcıları (JSON döner)
# ─────────────────────────────────────────────────────────────────────
def build_itinerary_tool(
    destination: str, days: int = 3, pace: str = "dengeli", budget_band: str = "orta",
    styles: Optional[list[str]] = None, dietary: Optional[list[str]] = None,
    accessibility: Optional[list[str]] = None, start_date: Optional[str] = None,
) -> dict:
    """Küratörlü POI havuzundan günlük gezi planı taslağı kurar.

    Args:
        destination: Destinasyon adı/anahtarı.
        days: Gün sayısı.
        pace: sakin | dengeli | yogun
        budget_band: ekonomik | orta | konforlu | luks
        styles: Seyahat stili tercihleri.
        dietary: Diyet kısıtları (sert filtre).
        accessibility: Erişilebilirlik ihtiyaçları (sert filtre).
        start_date: Başlangıç tarihi (YYYY-AA-GG) — kapalı gün denetimi için.
    """
    profile = TravelerProfile(
        pace=pace if pace in PACE_PROFILE else DEFAULT_PACE,  # type: ignore[arg-type]
        budget_band=budget_band if budget_band in ("ekonomik", "orta", "konforlu", "luks") else "orta",  # type: ignore[arg-type]
        styles=[s for s in (styles or [])],  # type: ignore[arg-type]
        dietary=list(dietary or []), accessibility=list(accessibility or []),
    )
    itin = build_itinerary(destination, days, profile, start_date)
    return {"plan": itin.model_dump(mode="json"),
            "toplam_maliyet_try": itin.total_cost_try,
            "gun_sayisi": len(itin.days),
            "_kaynak": {"baslik": "Pusula İçerik Editörlüğü — POI seti", "kademe": "T0",
                        "not": "Duraklar küratörlü havuzdan seçildi"},
            "_simule": False}


def validate_itinerary_tool(itinerary: dict, budget_total_try: Optional[float] = None,
                            pace: str = "dengeli", accessibility: Optional[list[str]] = None) -> dict:
    """Bir gezi planını bütçe, tempo, mesafe, erişilebilirlik ve kapalı gün açısından denetler.

    Args:
        itinerary: Denetlenecek plan (Itinerary şeması).
        budget_total_try: Bütçe üst sınırı (TRY).
        pace: sakin | dengeli | yogun
        accessibility: Erişilebilirlik ihtiyaçları.
    """
    try:
        itin = Itinerary(**itinerary) if isinstance(itinerary, dict) else itinerary
    except Exception as exc:
        return {"ok": False, "hata": f"Plan şemaya uymuyor: {exc.__class__.__name__}"}
    profile = TravelerProfile(
        pace=pace if pace in PACE_PROFILE else DEFAULT_PACE,  # type: ignore[arg-type]
        budget_total=budget_total_try, accessibility=list(accessibility or []),
    )
    rapor = validate_itinerary(itin, profile)
    return {"ok": rapor.ok,
            "bulgular": [i.model_dump(mode="json") for i in rapor.issues],
            "hata_sayisi": sum(1 for i in rapor.issues if i.severity == "error"),
            "_kaynak": {"baslik": "Pusula plan doğrulayıcı", "kademe": "T0",
                        "not": "Kural tabanlı denetim; LLM kullanılmaz"},
            "_simule": False}
