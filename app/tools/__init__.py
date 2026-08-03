"""Araç kayıt defteri — hangi ajan hangi araca erişir, tek yerden tanımlı.

Ajanlar araç listesini isimle ister (`tools_for("practical_desk")`); böylece
**araç yetkilendirmesi** de kaynak yetkilendirmesi gibi veri seviyesinde olur:
`culture_curator` uçuş arayamaz, `documents_officer` restoran öneremez.

`REGISTRY` aynı zamanda arayüzdeki araç rozetlerini, sunumdaki araç tablosunu ve
n8n akışındaki node listesini besler — üç yerde ayrı liste tutmamak için.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .content import (
    estimate_travel_time,
    estimate_trip_cost,
    get_culture_notes,
    get_local_dishes,
    get_pois,
    get_practical_facts,
    get_seasonality,
    search_destinations,
    search_faq,
)
from .documents import check_passport_validity, get_visa_requirements
from .inventory import search_flights, search_hotels, search_restaurants
from .live import destination_currency, get_fx, get_timezone_diff, get_weather

# araç adı -> (fonksiyon, kısa açıklama, kaynak kademesi, simüle mi)
REGISTRY: dict[str, tuple[Callable[..., dict], str, str, bool]] = {
    "get_weather": (get_weather, "Günlük hava tahmini ve ne giyilir", "T3", True),
    "get_timezone_diff": (get_timezone_diff, "Saat farkı — zoneinfo ile gerçek hesap", "T3", False),
    "get_fx": (get_fx, "Döviz kuru ve çevrim", "T3", True),
    "search_flights": (search_flights, "Uçuş seçenekleri", "T2", True),
    "search_hotels": (search_hotels, "Konaklama seçenekleri", "T2", True),
    "search_restaurants": (search_restaurants, "Yeme-içme önerileri + yöresel lezzetler", "T0/T2", True),
    "get_pois": (get_pois, "Gezilecek yerler (küratörlü POI seti)", "T0", False),
    "estimate_travel_time": (estimate_travel_time, "Semtler arası ulaşım süresi tahmini", "T0", False),
    "get_local_dishes": (get_local_dishes, "Yöresel lezzetler ve mutfak kültürü", "T0", False),
    "get_culture_notes": (get_culture_notes, "Kültür, görgü kuralları, kıyafet, bahşiş", "T0", False),
    "get_practical_facts": (get_practical_facts, "Priz, acil numara, su, internet, ulaşım kartı", "T1", False),
    "get_seasonality": (get_seasonality, "Sezon uygunluğu", "T0", False),
    "estimate_trip_cost": (estimate_trip_cost, "Toplam maliyet tahmini", "T0/T2", True),
    "search_destinations": (search_destinations, "Tercihe göre destinasyon skorlama", "T0", False),
    "search_faq": (search_faq, "SSS bilgi tabanı araması", "T0", False),
    "get_visa_requirements": (get_visa_requirements, "Vize ve giriş koşulları (resmî)", "T1", False),
    "check_passport_validity": (check_passport_validity, "Pasaport geçerlilik denetimi", "T1", False),
}


def _planner_tools() -> dict[str, tuple[Callable[..., dict], str, str, bool]]:
    """Plan araçları geç yüklenir: `planner` bu paketi kullandığı için döngü olmasın."""
    from ..planner import build_itinerary_tool, validate_itinerary_tool

    return {
        "build_itinerary": (build_itinerary_tool, "POI havuzundan günlük plan taslağı kurar", "T0", False),
        "validate_itinerary": (validate_itinerary_tool, "Planı bütçe/tempo/mesafe/açılış günü açısından denetler", "T0", False),
    }


def all_tools() -> dict[str, tuple[Callable[..., dict], str, str, bool]]:
    return {**REGISTRY, **_planner_tools()}


def get_tool(name: str) -> Optional[Callable[..., dict]]:
    entry = all_tools().get(name)
    return entry[0] if entry else None


def tools_for(names: list[str]) -> list[Any]:
    """Ajan tanımındaki araç adlarını çağrılabilir fonksiyonlara çevirir (Agno `tools=`)."""
    registry = all_tools()
    return [registry[n][0] for n in names if n in registry]


def catalog() -> list[dict]:
    """Sunum/arayüz/n8n için araç kataloğu."""
    return [
        {"ad": name, "aciklama": desc, "kaynak_kademesi": tier, "simule": sim}
        for name, (_fn, desc, tier, sim) in all_tools().items()
    ]


__all__ = [
    "REGISTRY", "all_tools", "get_tool", "tools_for", "catalog",
    "get_weather", "get_timezone_diff", "get_fx", "destination_currency",
    "search_flights", "search_hotels", "search_restaurants",
    "get_pois", "estimate_travel_time", "get_local_dishes", "get_culture_notes",
    "get_practical_facts", "get_seasonality", "estimate_trip_cost",
    "search_destinations", "search_faq",
    "get_visa_requirements", "check_passport_validity",
]
