"""Pydantic sözleşmeleri — sistemin ortak dili.

İki işi var:
1. HTTP katmanının istek/yanıt doğrulaması.
2. Agno `output_schema=` ile **yapılandırılmış çıktı** (PLAN.md §6): `Itinerary` ve
   `TravelerProfile` doğrudan modele şema olarak verilir -> parse hatası yok.

Türkçe alan açıklamaları (`description=`) modele gider ve çıktı kalitesini yükseltir;
bu yüzden bilinçli olarak Türkçe yazılmıştır (arayüz dili Türkçe, CLAUDE.md kural 5).
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `ItineraryDay`'in alan adı `date` olduğu için sınıf gövdesinde `date` adı gölgelenir ve
# `Optional[date]` anotasyonu `Optional[None]`a çözülür (alan hiç tarih tutamaz hâle gelir).
# Takma ad bu tuzağı kapatır.
DateT = date


# ─────────────────────────────────────────────────────────────────────
# Kaynak & güven kademesi — CLAUDE.md kural 3: model asla olgu uydurmaz
# ─────────────────────────────────────────────────────────────────────
class Tier(str, Enum):
    """Veri kaynağı güven hiyerarşisi (PLAN.md §4)."""

    T0 = "T0"  # kurum içi küratörlü — en yüksek güven
    T1 = "T1"  # resmi/kamusal — vize/pasaport/sağlık için ZORUNLU
    T2 = "T2"  # lisanslı ticari API — canlı envanter, kısa TTL
    T3 = "T3"  # açık veri — lisans + atıf zorunlu
    T4 = "T4"  # LLM parametrik bilgi — olgu için ASLA


class Source(BaseModel):
    """Her olgusal iddianın arkasındaki atıf. Groundodness guardrail'i bunu denetler."""

    title: str = Field(description="Kaynağın adı, ör. 'T.C. Dışişleri Bakanlığı'")
    tier: Tier = Field(description="Güven kademesi T0-T4")
    url: Optional[str] = Field(default=None, description="Varsa doğrulanabilir bağlantı")
    valid_until: Optional[date] = Field(
        default=None, description="Bilginin geçerlilik tarihi; yüksek riskli konularda zorunlu"
    )
    retrieved_at: Optional[datetime] = Field(default=None, description="Verinin çekildiği an")

    def label(self) -> str:
        """Yanıt sonuna basılacak insan-okur atıf satırı."""
        parts = [f"{self.title} ({self.tier.value})"]
        if self.valid_until:
            parts.append(f"geçerlilik: {self.valid_until.isoformat()}")
        return " · ".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Gezgin profili — Çıktı 7'nin kalbi
# ─────────────────────────────────────────────────────────────────────
BudgetBand = Literal["ekonomik", "orta", "konforlu", "luks"]
Pace = Literal["sakin", "dengeli", "yogun"]
TravelStyle = Literal[
    "kultur", "gastronomi", "doga", "plaj", "macera", "alisveris", "gece_hayati", "dini"
]
ClimatePref = Literal["serin", "ilıman", "sicak", "farketmez"]
GroupType = Literal["yalniz", "cift", "aile_cocuklu", "arkadas_grubu", "is"]


class TravelerProfile(BaseModel):
    """Kullanıcının seyahat tercihleri. `preference_keeper` ajanının `output_schema`'sı.

    KVKK notu: `dietary` ve `accessibility` **özel nitelikli veri** sayılabilir
    (m.6 — inanç/sağlık ifşası). Açık rıza olmadan yazılmaz, ayrı saklanır, log'lanmaz.
    Pasaport no / kimlik gibi alanlar bilinçli olarak **yoktur** (veri minimizasyonu).
    """

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(default="anon", description="Hash'lenmiş kullanıcı kimliği; ham PII değil")
    home_city: Optional[str] = Field(default=None, description="Kalkış şehri, ör. 'İstanbul'")
    budget_band: Optional[BudgetBand] = Field(default=None, description="Bütçe bandı")
    budget_total: Optional[float] = Field(
        default=None, ge=0, description="Toplam bütçe (TRY); sert filtre üst sınırı"
    )
    styles: list[TravelStyle] = Field(default_factory=list, description="Seyahat stili tercihleri")
    pace: Optional[Pace] = Field(default=None, description="Günlük tempo")
    climate: Optional[ClimatePref] = Field(default=None, description="İklim tercihi")
    group: Optional[GroupType] = Field(default=None, description="Grup bileşimi")
    dietary: list[str] = Field(
        default_factory=list, description="Diyet kısıtları (vegan, vejetaryen, helal, glutensiz) — ÖZEL NİTELİKLİ"
    )
    accessibility: list[str] = Field(
        default_factory=list, description="Erişilebilirlik ihtiyaçları — ÖZEL NİTELİKLİ"
    )
    languages: list[str] = Field(default_factory=lambda: ["tr"], description="Tercih edilen diller")
    avoid: list[str] = Field(default_factory=list, description="İstenmeyenler, ör. 'kalabalık'")
    liked: list[str] = Field(default_factory=list, description="Beğenilen destinasyon anahtarları")
    disliked: list[str] = Field(default_factory=list, description="Reddedilen destinasyon anahtarları")
    persona: Optional[str] = Field(
        default=None, description="Cold-start arketipi; 2-3 etkileşimde bireysel profile devreder"
    )
    updated_at: Optional[datetime] = None

    def hard_filters(self) -> dict:
        """Asla ihlal edilmeyen kısıtlar (PLAN.md §3). Skorlamadan ÖNCE uygulanır."""
        return {
            "dietary": list(self.dietary),
            "accessibility": list(self.accessibility),
            "budget_total": self.budget_total,
        }

    def is_empty(self) -> bool:
        """Cold-start tespiti: hiç sinyal yoksa persona arketipine düşülür."""
        return not any([self.budget_band, self.styles, self.pace, self.climate, self.group,
                        self.dietary, self.accessibility, self.liked])


class PreferenceSignal(BaseModel):
    """Örtük sinyal: davranıştan öğrenme. Ağırlığı zamanla sönümlenir."""

    kind: Literal["saved", "rejected", "asked_again", "clicked", "booked"]
    target: str = Field(description="Destinasyon/POI anahtarı")
    weight: float = Field(default=1.0, description="Sinyalin ham ağırlığı")
    ts: datetime = Field(default_factory=datetime.utcnow)


class ScoreBreakdown(BaseModel):
    """'Neden bu öneri?' — skor kırılımı. Aynı anda UX şeffaflığı ve KVKK düzeltme hakkı."""

    preference_fit: float = 0.0
    popularity: float = 0.0
    seasonality: float = 0.0
    budget_fit: float = 0.0
    novelty: float = 0.0
    recent_rejection_penalty: float = 0.0
    total: float = 0.0
    notes: list[str] = Field(default_factory=list, description="İnsan-okur gerekçe cümleleri")


class DestinationSuggestion(BaseModel):
    """Skorlanmış destinasyon önerisi."""

    key: str = Field(description="Kanonik destinasyon anahtarı (Wikidata QID ile eşlenir)")
    name: str
    country: str
    summary: str = ""
    est_cost_try: Optional[float] = Field(default=None, description="Tahmini toplam maliyet (TRY)")
    best_months: list[int] = Field(default_factory=list)
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    sources: list[Source] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Gezi planı — `itinerary_architect` ajanının `output_schema`'sı
# ─────────────────────────────────────────────────────────────────────
class ItinerarySlot(BaseModel):
    """Bir günün tek zaman dilimi."""

    time: Literal["sabah", "ogle", "ikindi", "aksam", "gece"] = Field(description="Zaman dilimi")
    title: str = Field(description="Etkinliğin kısa adı")
    detail: str = Field(default="", description="Ne yapılacak, neden önerildi")
    poi_key: Optional[str] = Field(default=None, description="Bilgi tabanındaki POI anahtarı")
    duration_min: int = Field(default=90, ge=0, description="Tahmini süre (dakika)")
    cost_try: float = Field(default=0.0, ge=0, description="Tahmini kişi başı maliyet (TRY)")
    travel_min_from_prev: int = Field(
        default=0, ge=0, description="Bir önceki duraktan tahmini ulaşım süresi (dakika)"
    )
    tags: list[str] = Field(default_factory=list)


class ItineraryDay(BaseModel):
    day: int = Field(ge=1, description="Kaçıncı gün")
    date: Optional[DateT] = None
    theme: str = Field(default="", description="Günün teması, ör. 'Antik Roma'")
    slots: list[ItinerarySlot] = Field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(s.cost_try for s in self.slots)

    @property
    def total_minutes(self) -> int:
        return sum(s.duration_min + s.travel_min_from_prev for s in self.slots)


class Itinerary(BaseModel):
    """Çok günlük gezi planı. `validate_itinerary` aracı bunu mesafe/bütçe/tempo açısından denetler."""

    destination: str = Field(description="Destinasyon adı")
    destination_key: Optional[str] = None
    days: list[ItineraryDay] = Field(default_factory=list)
    currency: str = "TRY"
    total_cost_try: float = Field(default=0.0, ge=0)
    notes: list[str] = Field(default_factory=list, description="Uyarılar ve ipuçları")
    sources: list[Source] = Field(default_factory=list)
    version: int = Field(default=1, description="Revizyonlar için sürüm; itin:{session}:{v}")

    @field_validator("days")
    @classmethod
    def _days_sorted(cls, v: list[ItineraryDay]) -> list[ItineraryDay]:
        return sorted(v, key=lambda d: d.day)

    def recompute_cost(self) -> float:
        self.total_cost_try = round(sum(d.total_cost for d in self.days), 2)
        return self.total_cost_try


class ValidationIssue(BaseModel):
    """Plan doğrulayıcısının bulgusu. `severity='error'` -> plan düzeltilmeden sunulmaz."""

    severity: Literal["error", "warning", "info"]
    code: str = Field(description="Makine-okur kod, ör. 'BUDGET_EXCEEDED'")
    message: str
    day: Optional[int] = None


class ValidationReport(BaseModel):
    ok: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)

    def add(self, severity: str, code: str, message: str, day: Optional[int] = None) -> None:
        self.issues.append(
            ValidationIssue(severity=severity, code=code, message=message, day=day)  # type: ignore[arg-type]
        )
        if severity == "error":
            self.ok = False


# ─────────────────────────────────────────────────────────────────────
# KVKK — rıza kademeleri (PLAN.md §9)
# ─────────────────────────────────────────────────────────────────────
class ConsentState(BaseModel):
    """Üç kademeli, ayrı ayrı ve geri alınabilir rıza.

    `session_only` her zaman açıktır (hizmetin verilebilmesi için gerekli, saklama yok).
    `personalization` kapalıyken profil **yazılmaz** — cold-start persona ile devam edilir.
    """

    user_id: str = "anon"
    session_only: bool = True
    personalization: bool = False
    sensitive_data: bool = Field(
        default=False, description="KVKK m.6 özel nitelikli veri (diyet/erişilebilirlik) için açık rıza"
    )
    marketing: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def allows_profile_write(self) -> bool:
        return self.personalization


class AuditEntry(BaseModel):
    """Denetim izi kaydı — KVKK hesap verebilirlik ilkesi."""

    ts: datetime = Field(default_factory=datetime.utcnow)
    user_id: str = "anon"
    action: str = Field(description="ör. 'consent.grant', 'profile.delete', 'export'")
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────
# HTTP sözleşmeleri
# ─────────────────────────────────────────────────────────────────────
Route = Literal["cache", "fast", "slow", "blocked"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="demo", description="Oturum kimliği")
    user_id: str = Field(default="anon", description="Hash'lenecek kullanıcı kimliği")
    lang: str = Field(default="tr")
    profile_overrides: Optional[dict] = Field(
        default=None, description="Tercih panelinden gelen anlık değişiklikler"
    )


class Trace(BaseModel):
    """Gözlemlenebilirlik: her yanıtın nasıl üretildiği. Demo'nun en etkili anı bu."""

    route: Route = "fast"
    complexity: Optional[str] = None
    agents: list[str] = Field(default_factory=list, description="Devreye giren uzman ajanlar")
    tools: list[str] = Field(default_factory=list)
    cache_hit: bool = False
    cache_similarity: Optional[float] = None
    latency_ms: int = 0
    llm_calls: int = 0
    llm_mode: str = "mock"
    guardrails: list[str] = Field(default_factory=list, description="Tetiklenen guardrail'ler")


class ChatResponse(BaseModel):
    answer: str
    trace: Trace = Field(default_factory=Trace)
    sources: list[Source] = Field(default_factory=list)
    itinerary: Optional[Itinerary] = None
    suggestions: list[DestinationSuggestion] = Field(default_factory=list)
    profile: Optional[TravelerProfile] = None
    disclaimer: Optional[str] = Field(default=None, description="Yüksek riskli konularda zorunlu feragat")
