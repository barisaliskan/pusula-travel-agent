"""Tercih yönetimi ve öneri mekanizması — Çıktı 7.

Üç katmanlı tercih toplama (PLAN.md §3):
  1. **Açık**   — tercih panelinden gelen alanlar
  2. **Örtük**  — davranış sinyalleri (kaydetti/reddetti/tekrar sordu), zamanla sönümlenir
  3. **Konuşmadan** — mesaj metninden çıkarım (`extract_from_text`); gerçek modda
     Agno `enable_user_memories` da aynı profili besler

Öneri üretimi iki aşamalıdır ve bu ayrım kasıtlıdır:
  * **Sert filtreler** asla ihlal edilmez (diyet, erişilebilirlik, bütçe üst sınırı, vize uygunluğu).
    Bunlar skora eklenen bir terim DEĞİLDİR — eleme kriteridir. Vegan birine "biraz uygun"
    bir öneri sunmak kabul edilemez.
  * **Yumuşak yeniden sıralama** kalanları skorlar:
        score = w1·tercih_uyumu + w2·popülerlik + w3·sezon + w4·bütçe + w5·yenilik − w6·son_red

Her öneri `ScoreBreakdown` ile döner -> "Neden bu öneri?" hem UX şeffaflığı hem KVKK
düzeltme hakkı sağlar.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

from . import kvkk
from .cache import cache, keys
from .knowledge import kb
from .schemas import (
    DestinationSuggestion,
    PreferenceSignal,
    ScoreBreakdown,
    Source,
    Tier,
    TravelerProfile,
)

# Skor ağırlıkları — sunumda formül olarak gösterilir, toplamları 1.0
WEIGHTS = {
    "preference_fit": 0.35,
    "budget_fit": 0.25,
    "seasonality": 0.20,
    "popularity": 0.10,
    "novelty": 0.10,
}
REJECTION_PENALTY = 0.40  # son reddedilenler için düşülen ceza

# Örtük sinyal ham ağırlıkları
SIGNAL_WEIGHTS = {"saved": 1.0, "booked": 1.5, "clicked": 0.3, "asked_again": 0.5, "rejected": -1.2}
SIGNAL_HALF_LIFE_DAYS = 30.0  # sinyal ağırlığı 30 günde yarılanır

# Cold-start persona arketipleri (PLAN.md §1.3)
PERSONAS: dict[str, dict] = {
    "butce_odakli": {
        "baslik": "Bütçe Odaklı Gezgin",
        "oncelikler": "En uygun fiyat, ekonomik konaklama, ücretsiz aktiviteler",
        "profile": {"budget_band": "ekonomik", "pace": "yogun", "styles": ["kultur", "gastronomi"]},
    },
    "kultur_avcisi": {
        "baslik": "Kültür Avcısı",
        "oncelikler": "Müze, tarih, yerel gelenek, sakin tempo",
        "profile": {"budget_band": "orta", "pace": "sakin", "styles": ["kultur", "dini"]},
    },
    "gastronomi": {
        "baslik": "Gastronomi Meraklısı",
        "oncelikler": "Yöresel lezzet, restoran, pazar",
        "profile": {"budget_band": "orta", "pace": "dengeli", "styles": ["gastronomi", "kultur"]},
    },
    "aile": {
        "baslik": "Aile Seyahati",
        "oncelikler": "Güvenlik, çocuk dostu, kısa mesafe, erişilebilirlik",
        "profile": {"budget_band": "orta", "pace": "sakin", "group": "aile_cocuklu",
                    "styles": ["doga", "plaj"]},
    },
    "konfor": {
        "baslik": "Konfor / Lüks",
        "oncelikler": "Merkezi konum, üst segment otel, özel transfer",
        "profile": {"budget_band": "luks", "pace": "sakin", "styles": ["gastronomi", "alisveris"]},
    },
}

# ─────────────────────────────────────────────────────────────────────
# Konuşmadan çıkarım
# ─────────────────────────────────────────────────────────────────────
_BUDGET_PATTERNS = [
    (r"(ekonomik|ucuz|bütçe dostu|hesaplı|sınırlı bütçe|öğrenci)", "ekonomik"),
    (r"(orta (bütçe|segment)|makul)", "orta"),
    (r"(konforlu|rahat bir|iyi bir otel)", "konforlu"),
    (r"(lüks|luks|beş yıldız|5 yıldız|üst segment|premium)", "luks"),
]
_STYLE_PATTERNS = [
    (r"(müze|tarih|kültür|antik|mimari)", "kultur"),
    (r"(yemek|lezzet|mutfak|gastronomi|restoran|şarap)", "gastronomi"),
    (r"(doğa|yürüyüş|dağ|vadi|trekking|manzara)", "doga"),
    (r"(plaj|deniz|kumsal|sahil)", "plaj"),
    (r"(macera|adrenalin|safari|dalış|balon)", "macera"),
    (r"(alışveriş|çarşı|outlet|mağaza)", "alisveris"),
    (r"(gece hayatı|bar|kulüp|eğlence)", "gece_hayati"),
    (r"(cami|kilise|tapınak|dini|hac|manevi)", "dini"),
]
_PACE_PATTERNS = [
    (r"(sakin|yavaş|dinlenmek|acele etmeden|rahat tempo|kalabalık sevmem|kalabalıktan)", "sakin"),
    (r"(dengeli|normal tempo)", "dengeli"),
    (r"(yoğun|çok yer|her şeyi görmek|maksimum|hızlı tempo)", "yogun"),
]
_CLIMATE_PATTERNS = [
    (r"(serin|soğuk|kar|kış)", "serin"),
    (r"(ılıman|mutedil)", "ilıman"),
    (r"(sıcak|güneş|yaz)", "sicak"),
]
_GROUP_PATTERNS = [
    (r"(yalnız|tek başıma|solo)", "yalniz"),
    (r"(eşimle|sevgilim|balayı|çift olarak|ikimiz)", "cift"),
    (r"(çocuk|ailemle|aile olarak|bebek)", "aile_cocuklu"),
    (r"(arkadaş|arkadaşlarımla|grup olarak)", "arkadas_grubu"),
    (r"(iş (gezisi|seyahati)|toplantı|konferans)", "is"),
]
_DIET_PATTERNS = [
    (r"(vegan)", "vegan"),
    (r"(vejetaryen|vejeteryan|et yemiyorum)", "vejetaryen"),
    (r"(helal)", "helal"),
    (r"(glutensiz|çölyak)", "glutensiz"),
    (r"(koşer)", "koşer"),
]
_ACCESS_PATTERNS = [
    (r"(tekerlekli sandalye|engelli erişim|ortopedik)", "tekerlekli sandalye"),
    (r"(görme engel)", "görme engelli"),
    (r"(işitme engel)", "işitme engelli"),
    (r"(merdiven çıkamıyorum|yürümekte zorlan)", "sınırlı hareket"),
]
_BUDGET_AMOUNT_RE = re.compile(r"(\d+(?:[.\s]\d{3})*)\s*(?:bin\s*)?(?:tl|try|₺|lira)", re.IGNORECASE)


def _match_first(text: str, patterns: list[tuple[str, str]]) -> Optional[str]:
    for pattern, value in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return value
    return None


def _match_all(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    return [v for p, v in patterns if re.search(p, text, re.IGNORECASE)]


def extract_from_text(text: str) -> dict:
    """Serbest metinden tercih çıkarımı (mock modda tek çıkarım yolu).

    Gerçek modda `preference_keeper` ajanı `output_schema=TravelerProfile` ile aynı işi
    daha zengin yapar; bu fonksiyon her iki modda da taban çizgisi olarak çalışır.
    """
    found: dict[str, Any] = {}
    if v := _match_first(text, _BUDGET_PATTERNS):
        found["budget_band"] = v
    if v := _match_first(text, _PACE_PATTERNS):
        found["pace"] = v
    if v := _match_first(text, _CLIMATE_PATTERNS):
        found["climate"] = v
    if v := _match_first(text, _GROUP_PATTERNS):
        found["group"] = v
    if styles := _match_all(text, _STYLE_PATTERNS):
        found["styles"] = styles
    if diets := _match_all(text, _DIET_PATTERNS):
        found["dietary"] = diets
    if acc := _match_all(text, _ACCESS_PATTERNS):
        found["accessibility"] = acc

    if m := _BUDGET_AMOUNT_RE.search(text):
        raw = m.group(1).replace(".", "").replace(" ", "")
        try:
            amount = float(raw)
            if "bin" in text[m.start():m.end() + 5].lower():
                amount *= 1000
            found["budget_total"] = amount
        except ValueError:
            pass

    if city := kb.resolve_destination(text):
        found["_mentioned_destination"] = city
    return found


# ─────────────────────────────────────────────────────────────────────
# Profil yaşam döngüsü
# ─────────────────────────────────────────────────────────────────────
def load(user_id: str) -> TravelerProfile:
    """Profili yükler. Rıza yoksa boş profil döner (kvkk katmanı okumayı da kapatır)."""
    data = kvkk.load_profile(user_id)
    if not data:
        return TravelerProfile(user_id=user_id)
    data.pop("user_id", None)
    try:
        return TravelerProfile(user_id=user_id, **data)
    except Exception:
        return TravelerProfile(user_id=user_id)


def merge(profile: TravelerProfile, updates: dict) -> TravelerProfile:
    """Çıkarılan tercihleri profile işler. Listeler birleşir, skalerler üzerine yazılır."""
    data = profile.model_dump()
    for key, value in updates.items():
        if key.startswith("_") or value in (None, [], ""):
            continue
        if isinstance(value, list):
            data[key] = sorted(set(data.get(key) or []) | set(value))
        else:
            data[key] = value
    data["updated_at"] = datetime.now(timezone.utc)
    return TravelerProfile(**data)


def save(user_id: str, profile: TravelerProfile) -> tuple[bool, str]:
    """Profili KVKK kapısından geçirerek saklar (tek yazma yolu)."""
    payload = profile.model_dump(mode="json", exclude={"user_id"})
    return kvkk.save_profile(user_id, payload)


def apply_persona(profile: TravelerProfile, persona_key: str) -> TravelerProfile:
    """Cold-start: sinyal yokken persona arketipiyle başla, 2-3 etkileşimde bireysele geç."""
    persona = PERSONAS.get(persona_key)
    if not persona:
        return profile
    merged = merge(profile, dict(persona["profile"]))
    merged.persona = persona_key
    return merged


# ─────────────────────────────────────────────────────────────────────
# Örtük sinyaller
# ─────────────────────────────────────────────────────────────────────
def _signal_key(user_id: str) -> str:
    return f"sig:{keys.profile(user_id).split(':', 1)[1]}"


def record_signal(user_id: str, kind: str, target: str) -> None:
    """Davranış sinyalini kaydeder (kaydetti/reddetti/tıkladı/tekrar sordu)."""
    if not kvkk.can_write_profile(user_id):
        return  # rıza yoksa davranış da saklanmaz
    signals = cache.get_json(_signal_key(user_id)) or []
    signals.append(PreferenceSignal(kind=kind, target=target,
                                    weight=SIGNAL_WEIGHTS.get(kind, 0.5)).model_dump(mode="json"))
    cache.set_json(_signal_key(user_id), signals[-100:])
    kvkk.audit(user_id, "signal.record", f"{kind}")


def signal_scores(user_id: str) -> dict[str, float]:
    """Sinyalleri zamanla sönümleyerek destinasyon başına net skora indirger.

    Sönümleme neden önemli: kullanıcı 6 ay önce bir yeri reddetmiş olabilir; o red
    bugünkü öneriyi sonsuza kadar bloklamamalı. Yarılanma ömrü 30 gün.
    """
    raw = cache.get_json(_signal_key(user_id)) or []
    now = datetime.now(timezone.utc)
    scores: dict[str, float] = {}
    for s in raw:
        try:
            ts = datetime.fromisoformat(str(s["ts"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        decay = math.pow(0.5, age_days / SIGNAL_HALF_LIFE_DAYS)
        scores[s["target"]] = scores.get(s["target"], 0.0) + float(s.get("weight", 0)) * decay
    return scores


# ─────────────────────────────────────────────────────────────────────
# Sert filtreler — asla ihlal edilmez
# ─────────────────────────────────────────────────────────────────────
def trip_total(dest: dict, band: str, nights: int, travelers: int = 1) -> float:
    """Kullanıcıya GÖSTERİLEN toplamın aynısı: yerinde harcama + uçuş.

    Sert filtre ile öneri kartındaki tutar aynı formülden gelmelidir. Eskiden filtre
    yalnızca günlük harcamayı sayıyor, kart ise uçuşu da ekliyordu; sonuç, "bütçenize
    uygun" denip 10.000 TRY bütçeye 12.440 TRY'lik destinasyon önerilmesiydi.
    """
    from .tools.inventory import search_flights  # döngüsel import olmasın diye yerel

    daily = float((dest.get("daily_cost_try") or {}).get(band, 0) or 0)
    yerinde = daily * nights * max(1, travelers)
    try:
        ucus = float(search_flights(dest["key"], passengers=max(1, travelers))
                     .get("en_dusuk_try", 0) or 0)
    except Exception:
        ucus = 0.0
    return yerinde + ucus


def hard_filter(dest: dict, profile: TravelerProfile, *, nights: int = 4,
                travelers: int = 1) -> Optional[str]:
    """Destinasyon elenmeli mi? Eleme sebebini döner, uygunsa None.

    Bunlar skora eklenen terim değil, eleme kriteridir — 'biraz uygun' diye bir şey yok.
    """
    # 1) Bütçe üst sınırı — uçuş dahil toplam üzerinden
    if profile.budget_total:
        band = profile.budget_band or "orta"
        daily = dest.get("daily_cost_try", {}).get(band)
        if daily:
            toplam = trip_total(dest, band, nights, travelers)
            if toplam > profile.budget_total:
                return (f"bütçe üst sınırı aşılıyor (uçuş dahil tahmini "
                        f"{int(toplam):,} TRY > {int(profile.budget_total):,} TRY)"
                        .replace(",", "."))

    # 2) Erişilebilirlik
    if profile.accessibility:
        accessible_pois = [p for p in kb.pois(dest["key"]) if p.get("accessible")]
        if len(accessible_pois) < 3:
            return "erişilebilir durak sayısı yetersiz"

    # 3) Diyet — mutfakta hiç uygun yemek yoksa ele
    if profile.dietary:
        cuisine = kb.cuisine(dest["key"]) or {}
        dishes = cuisine.get("dishes", [])
        for diet in profile.dietary:
            if diet in ("vegan", "vejetaryen", "helal", "glutensiz"):
                uygun = [d for d in dishes if diet in d.get("diet", [])]
                if dishes and not uygun:
                    return f"{diet} uyumlu yöresel seçenek bulunmuyor"
    return None


# ─────────────────────────────────────────────────────────────────────
# Skorlama — yumuşak yeniden sıralama
# ─────────────────────────────────────────────────────────────────────
def _preference_fit(dest: dict, profile: TravelerProfile) -> tuple[float, list[str]]:
    notes: list[str] = []
    if not profile.styles:
        return 0.5, []
    overlap = set(profile.styles) & set(dest.get("styles", []))
    fit = len(overlap) / len(profile.styles)
    if overlap:
        notes.append(f"ilgi alanlarınızla örtüşüyor: {', '.join(sorted(overlap))}")

    if profile.pace == "sakin" and dest.get("crowd_level", 3) >= 5:
        fit *= 0.7
        notes.append("sakin tempo tercihinize göre kalabalık bir destinasyon")
    if profile.group == "aile_cocuklu":
        fam = dest.get("family_friendly", 3) / 5
        fit = 0.6 * fit + 0.4 * fam
        if dest.get("family_friendly", 0) >= 4:
            notes.append("aile seyahatine uygun")
    if profile.avoid:
        for a in profile.avoid:
            if a.lower() in " ".join(dest.get("tags", [])).lower():
                fit *= 0.5
                notes.append(f"kaçınmak istediğiniz '{a}' burada öne çıkıyor")
    return min(1.0, fit), notes


def _budget_fit(dest: dict, profile: TravelerProfile, nights: int) -> tuple[float, list[str]]:
    band = profile.budget_band
    if not band and not profile.budget_total:
        return 0.5, []
    costs = dest.get("daily_cost_try", {})
    daily = costs.get(band or "orta")
    if not daily:
        return 0.5, []
    total = daily * nights
    if profile.budget_total:
        ratio = total / profile.budget_total
        tutar = f"~{int(total):,} TRY".replace(",", ".")
        # En iyi uyum bütçenin %60-90'ını kullanmak: ne israf ne kısıtlı.
        # Basamaklı plato yerine sürekli eğri: aynı banttaki destinasyonlar
        # gerçek maliyetlerine göre birbirinden ayrışsın.
        if ratio <= 0.6:
            return round(0.60 + 0.40 * (ratio / 0.6), 3), [f"bütçenizin altında kalıyor ({tutar})"]
        if ratio <= 0.9:
            return 1.0, [f"bütçenize çok uygun ({tutar})"]
        return round(max(0.5, 1.0 - 5.0 * (ratio - 0.9)), 3), [f"bütçenizin üst sınırına yakın ({tutar})"]
    # Sadece bant verilmişse: bandın o destinasyondaki göreli ucuzluğu
    tum = [v for v in costs.values() if v]
    if not tum:
        return 0.5, []
    rel = 1.0 - (daily - min(tum)) / (max(tum) - min(tum) or 1)
    return max(0.2, rel), [f"{band} bandında günlük ~{int(daily):,} TRY".replace(",", ".")]


def _seasonality(dest: dict, month: Optional[int]) -> tuple[float, list[str]]:
    if not month:
        return 0.5, []
    if month in dest.get("avoid_months", []):
        return 0.15, [f"{month}. ay bu destinasyon için uygun değil: {dest.get('seasonality_note','')}"]
    if month in dest.get("best_months", []):
        return 1.0, [f"{month}. ay ideal sezon"]
    return 0.55, ["sezon açısından ortalama"]


def _novelty(dest: dict, profile: TravelerProfile, signals: dict[str, float]) -> tuple[float, list[str]]:
    """Yenilik = kullanıcı için ne kadar YENİ, kalabalık seviyesi değil.

    DİKKAT: Bir zamanlar yenilik `(6-crowd)/5` ile tanımlıydı, yani popülerliğin birebir
    tersiydi. Eşit ağırlıklarla iki terim sabite dönüşüp formülden düşüyordu
    (0.1·c/5 + 0.1·(6−c)/5 = 0.12, her destinasyon için aynı). Yenilik artık kullanıcı
    geçmişine bakar; kalabalık yalnızca hafif bir eğim olarak kalır.
    """
    key = dest["key"]
    if key in profile.liked:
        return 0.2, ["daha önce beğendiğiniz bir yer — yenilik puanı düşük"]
    if signals.get(key):
        return 0.45, ["daha önce ilgilendiğiniz bir destinasyon"]
    if profile.home_city and profile.home_city.lower() in dest.get("country", "").lower():
        return 0.3, ["yaşadığınız ülkede"]
    # Hiç etkileşim yok: az bilinen yerler hafifçe daha yüksek yenilik alır
    crowd = dest.get("crowd_level", 3)
    return round(0.75 + 0.05 * (5 - crowd), 3), []


def score_destination(
    dest: dict,
    profile: TravelerProfile,
    *,
    month: Optional[int] = None,
    nights: int = 4,
    signals: Optional[dict[str, float]] = None,
) -> ScoreBreakdown:
    """Tek destinasyonun skor kırılımı — 'Neden bu öneri?' bunun üzerine kurulur."""
    signals = signals or {}
    pref, n1 = _preference_fit(dest, profile)
    budget, n2 = _budget_fit(dest, profile, nights)
    season, n3 = _seasonality(dest, month)
    novelty, n4 = _novelty(dest, profile, signals)
    popularity = dest.get("crowd_level", 3) / 5

    sig = signals.get(dest["key"], 0.0)
    penalty = 0.0
    notes = n1 + n2 + n3 + n4
    if dest["key"] in profile.disliked or sig < -0.2:
        penalty = REJECTION_PENALTY * min(1.0, abs(sig) or 1.0)
        notes.append("daha önce reddettiğiniz için sıralamada geriye alındı")
    elif sig > 0.2:
        notes.append("önceki ilginize göre öne çıkarıldı")

    total = (
        WEIGHTS["preference_fit"] * pref
        + WEIGHTS["budget_fit"] * budget
        + WEIGHTS["seasonality"] * season
        + WEIGHTS["popularity"] * popularity
        + WEIGHTS["novelty"] * novelty
        - penalty
    )
    if sig > 0.2:
        total += min(0.1, sig * 0.05)

    return ScoreBreakdown(
        preference_fit=round(pref, 3),
        popularity=round(popularity, 3),
        seasonality=round(season, 3),
        budget_fit=round(budget, 3),
        novelty=round(novelty, 3),
        recent_rejection_penalty=round(penalty, 3),
        total=round(max(0.0, total), 4),
        notes=notes,
    )


def recommend(
    profile: TravelerProfile,
    *,
    month: Optional[int] = None,
    nights: int = 4,
    limit: int = 5,
    user_id: Optional[str] = None,
    exclude: Optional[list[str]] = None,
    travelers: int = 1,
) -> tuple[list[DestinationSuggestion], list[dict]]:
    """Öneri üretir. Döner: (öneriler, elenenler).

    Elenenleri de döndürmek bilinçli: UI'da "şu 3 destinasyon şu sebeple elendi" demek
    hem güven verir hem kullanıcının kısıtını gözden geçirmesini sağlar.
    """
    signals = signal_scores(user_id) if user_id else {}
    exclude_set = set(exclude or [])
    suggestions: list[DestinationSuggestion] = []
    rejected: list[dict] = []

    for dest in kb.destinations:
        if dest["key"] in exclude_set:
            continue
        reason = hard_filter(dest, profile, nights=nights, travelers=travelers)
        if reason:
            rejected.append({"key": dest["key"], "name": dest["name"], "sebep": reason})
            continue

        breakdown = score_destination(dest, profile, month=month, nights=nights, signals=signals)
        band = profile.budget_band or "orta"
        daily = dest.get("daily_cost_try", {}).get(band)
        src = (dest.get("sources") or [{}])[0]
        toplam_maliyet = trip_total(dest, band, nights, travelers) if daily else None
        suggestions.append(
            DestinationSuggestion(
                key=dest["key"], name=dest["name"], country=dest["country"],
                summary=dest.get("summary", ""),
                est_cost_try=round(toplam_maliyet, 2) if toplam_maliyet else None,
                best_months=dest.get("best_months", []),
                score=breakdown,
                sources=[Source(title=src.get("title", "Pusula İçerik Editörlüğü"),
                                tier=Tier(src.get("tier", "T0")),
                                valid_until=src.get("valid_until"))],
            )
        )

    suggestions.sort(key=lambda s: s.score.total, reverse=True)
    return suggestions[:limit], rejected


def explain(suggestion: DestinationSuggestion) -> str:
    """'Neden bu öneri?' — skor kırılımının insan-okur açıklaması."""
    b = suggestion.score
    satirlar = [f"**{suggestion.name}** neden önerildi? (toplam skor {b.total:.2f})", ""]
    kalemler = [
        ("Tercih uyumu", b.preference_fit, WEIGHTS["preference_fit"]),
        ("Bütçe uyumu", b.budget_fit, WEIGHTS["budget_fit"]),
        ("Sezon uygunluğu", b.seasonality, WEIGHTS["seasonality"]),
        ("Popülerlik", b.popularity, WEIGHTS["popularity"]),
        ("Yenilik", b.novelty, WEIGHTS["novelty"]),
    ]
    for ad, deger, agirlik in kalemler:
        satirlar.append(f"- {ad}: {deger:.2f} × ağırlık {agirlik:.2f} = **{deger * agirlik:.3f}**")
    if b.recent_rejection_penalty:
        satirlar.append(f"- Son reddedilenler cezası: **−{b.recent_rejection_penalty:.3f}**")
    if b.notes:
        satirlar += ["", "**Gerekçeler:**"] + [f"- {n}" for n in b.notes]
    satirlar += ["", "_Katılmadığınız bir çıkarımı tercih panelinden düzeltebilirsiniz; "
                 "öneriler anında yeniden hesaplanır._"]
    return "\n".join(satirlar)


def formula() -> dict:
    """Sunum slaytı için formül ve ağırlıklar."""
    return {
        "formul": ("score = w1·tercih_uyumu + w2·bütçe_uyumu + w3·sezon_uygunluğu "
                   "+ w4·popülerlik + w5·yenilik − w6·son_reddedilenler"),
        "agirliklar": WEIGHTS,
        "red_cezasi": REJECTION_PENALTY,
        "sert_filtreler": ["diyet", "erişilebilirlik", "bütçe üst sınırı", "vize uygunluğu"],
        "sinyal_yarilanma_gun": SIGNAL_HALF_LIFE_DAYS,
        "personalar": {k: v["baslik"] for k, v in PERSONAS.items()},
    }
