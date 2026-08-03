"""Oynak veri araçları: hava durumu · saat farkı · döviz.

Bu üç veri tipi "istek anında çekilir" sınıfındadır (PLAN.md §4 alım hattı) ve
TTL'leri kısadır (hava 1 sa, döviz 1 sa). Saat farkı ise **hiç API kullanmaz**:
IANA tzdata Python'un içinde gelir, `zoneinfo` ile yerel hesaplanır — dış servise
bağımlılık eklemek yerine doğru cevabı ücretsiz ve çevrimdışı üretmek tercih edildi.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .. import config
from ..cache import keys
from ..knowledge import kb
from .base import cached_tool, parse_date, seeded, tr_decimal

HOME_TZ = "Europe/Istanbul"

# ── İklim normalleri (simüle hava servisinin arka planı) ──────────────
# Gerçek entegrasyonda bu tablo yerine Open-Meteo çağrısı gelir. Sinüzoidal model:
#   T(ay) = ortalama − genlik · cos(2π(ay − 1)/12)   -> Ocak en soğuk, Temmuz en sıcak.
# Kuzey yarımküredeki 12 destinasyon için yeterli; tropik Bangkok'ta genlik zaten küçük.
_CLIMATE = {
    "roma":       {"mean": 16.0, "amp": 8.5,  "wet": [10, 11, 12, 1, 2]},
    "barselona":  {"mean": 17.0, "amp": 7.0,  "wet": [9, 10, 11]},
    "paris":      {"mean": 12.0, "amp": 8.0,  "wet": [10, 11, 12, 1]},
    "prag":       {"mean": 9.5,  "amp": 10.0, "wet": [5, 6, 7]},
    "tokyo":      {"mean": 16.5, "amp": 10.0, "wet": [6, 7, 9]},
    "bangkok":    {"mean": 29.0, "amp": 2.5,  "wet": [5, 6, 7, 8, 9, 10]},
    "tiflis":     {"mean": 13.5, "amp": 11.0, "wet": [4, 5, 6]},
    "saraybosna": {"mean": 10.0, "amp": 10.5, "wet": [10, 11, 12, 1]},
    "baku":       {"mean": 15.5, "amp": 9.5,  "wet": [10, 11, 12]},
    "dubai":      {"mean": 28.5, "amp": 8.0,  "wet": [1, 2]},
    "marakes":    {"mean": 20.0, "amp": 9.0,  "wet": [11, 12, 1, 2, 3]},
    "kapadokya":  {"mean": 11.0, "amp": 11.0, "wet": [4, 5, 11, 12]},
}

# Simüle referans kuru: 1 birim yabancı para kaç TRY (PLAN.md §5, TTL 1 sa).
_FX_TRY = {
    "EUR": 47.80, "USD": 43.20, "GBP": 55.40, "JPY": 0.288, "THB": 1.225,
    "GEL": 16.05, "BAM": 24.45, "AZN": 25.40, "AED": 11.76, "MAD": 4.42, "CZK": 1.93,
}

_GIYIM = [
    (5,  "Kalın mont, atkı ve bere; katmanlı giyinin."),
    (12, "Mont veya kalın hırka; akşamları belirgin serinliyor."),
    (18, "İnce ceket veya sweatshirt; gündüz güneşte rahat eder."),
    (26, "Yazlık kıyafet; şapka ve güneş kremi işinize yarar."),
    (99, "Hafif ve nefes alan kumaşlar; öğle saatlerinde gölge arayın."),
]


def _temp_for(dest_key: str, day: date) -> tuple[float, float]:
    """Ayın ortalama yüksek/düşük sıcaklığı (°C)."""
    c = _CLIMATE.get(dest_key, {"mean": 15.0, "amp": 9.0, "wet": []})
    seasonal = c["mean"] - c["amp"] * math.cos(2 * math.pi * (day.month - 1) / 12)
    rng = seeded("wx", dest_key, day.isoformat())
    jitter = rng.uniform(-2.0, 2.0)
    high = round(seasonal + 4.5 + jitter, 1)
    low = round(seasonal - 4.5 + jitter * 0.5, 1)
    return high, low


def _condition(dest_key: str, day: date, rng) -> tuple[str, int]:
    wet = _CLIMATE.get(dest_key, {}).get("wet", [])
    base = 55 if day.month in wet else 18
    prob = max(0, min(95, int(rng.gauss(base, 15))))
    if prob >= 60:
        return "yağmurlu", prob
    if prob >= 35:
        return "parçalı bulutlu, sağanak ihtimali", prob
    if prob >= 18:
        return "parçalı bulutlu", prob
    return "açık", prob


def _clothing(high: float) -> str:
    for limit, advice in _GIYIM:
        if high <= limit:
            return advice
    return _GIYIM[-1][1]


def get_weather(destination: str, start_date: Optional[str] = None, days: int = 3) -> dict:
    """Destinasyonun günlük hava tahmini (simüle Open-Meteo adapter'ı).

    Args:
        destination: Destinasyon adı veya anahtarı ("Roma" / "roma").
        start_date: Başlangıç tarihi (YYYY-AA-GG). Boşsa bugünden 30 gün sonrası.
        days: Kaç günlük tahmin (1-7).
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key)
    start = parse_date(start_date, default_offset_days=0)
    days = max(1, min(7, int(days or 3)))
    qid = (dest or {}).get("qid", dest_key)

    def _produce() -> dict:
        forecast = []
        for i in range(days):
            d = start + timedelta(days=i)
            rng = seeded("cond", dest_key, d.isoformat())
            high, low = _temp_for(dest_key, d)
            cond, prob = _condition(dest_key, d, rng)
            forecast.append({
                "tarih": d.isoformat(),
                "durum": cond,
                "en_yuksek_c": high,
                "en_dusuk_c": low,
                "yagis_olasiligi_yuzde": prob,
                "kapali_mekan_onerilir": prob >= 60,
            })
        ortalama = round(sum(f["en_yuksek_c"] for f in forecast) / len(forecast), 1)
        return {
            "destinasyon": (dest or {}).get("name", destination),
            "destinasyon_anahtari": dest_key,
            "tahmin": forecast,
            "ortalama_en_yuksek_c": ortalama,
            "ne_giyilir": _clothing(ortalama),
            "yagmurlu_gun_sayisi": sum(1 for f in forecast if f["kapali_mekan_onerilir"]),
        }

    return cached_tool(keys.weather(qid, start.isoformat()), config.TTL_WEATHER,
                       _produce, provider="weather")


def get_timezone_diff(destination: str, home_timezone: str = HOME_TZ) -> dict:
    """Destinasyon ile kalkış şehri arasındaki saat farkı — **gerçek hesap, API yok**.

    `zoneinfo` IANA tzdata'yı kullanır; yaz saati uygulaması (DST) o anki tarihe göre
    doğru şekilde hesaba katılır. Bu yüzden fark mevsime göre değişebilir ve bunu
    açıkça belirtiriz.
    """
    dest_key = kb.resolve_destination(destination) or destination.lower().strip()
    dest = kb.destination(dest_key) or {}
    practical = kb.practical(dest_key) or {}
    tz_name = dest.get("timezone") or practical.get("timezone")
    if not tz_name:
        return {"hata": f"'{destination}' için saat dilimi bilgisi bulunamadı.",
                "_kaynak": {"baslik": "IANA tzdata", "kademe": "T3", "not": "yerel hesap"},
                "_simule": False}

    now = datetime.now(ZoneInfo(home_timezone))
    there = now.astimezone(ZoneInfo(tz_name))
    diff_hours = (there.utcoffset() - now.utcoffset()).total_seconds() / 3600  # type: ignore[operator]
    yon = "ileride" if diff_hours > 0 else ("geride" if diff_hours < 0 else "aynı")
    mutlak = abs(diff_hours)
    ifade = ("saat farkı yok" if diff_hours == 0
             else f"{mutlak:g} saat {yon}".replace(".0", ""))

    return {
        "destinasyon": dest.get("name", destination),
        "saat_dilimi": tz_name,
        "kalkis_saat_dilimi": home_timezone,
        "fark_saat": diff_hours,
        "ifade": ifade,
        "yerel_saat": there.strftime("%H:%M"),
        "kalkis_yerel_saat": now.strftime("%H:%M"),
        "jetlag_notu": (
            "3 saatin altındaki farklarda uyum genellikle bir günde tamamlanır."
            if mutlak < 3 else
            "Uçuş öncesi birkaç gün yatış saatini hedef saate 30-60 dakika kaydırmak uyumu kolaylaştırır."
        ),
        "dst_notu": "Fark, yaz saati uygulaması nedeniyle yılın dönemine göre 1 saat değişebilir.",
        "_kaynak": {"baslik": "IANA tzdata (gerçek)", "kademe": "T3",
                    "not": "zoneinfo ile yerel hesap — dış API çağrısı yok"},
        "_simule": False,
        "_cache": "yok (yerel hesap)",
    }


def get_fx(base_currency: str, quote_currency: str = "TRY", amount: float = 1.0) -> dict:
    """Döviz kuru ve çevrim (simüle merkez bankası referans kuru).

    Args:
        base_currency: Kaynak para birimi (ör. "EUR").
        quote_currency: Hedef para birimi; varsayılan TRY.
        amount: Çevrilecek tutar.
    """
    base = (base_currency or "").strip().upper()
    quote = (quote_currency or "TRY").strip().upper()
    pair = f"{base}{quote}"

    def _produce() -> dict:
        if base == quote:
            rate = 1.0
        elif quote == "TRY" and base in _FX_TRY:
            rate = _FX_TRY[base]
        elif base == "TRY" and quote in _FX_TRY:
            rate = round(1 / _FX_TRY[quote], 6)
        elif base in _FX_TRY and quote in _FX_TRY:
            rate = round(_FX_TRY[base] / _FX_TRY[quote], 6)
        else:
            return {"hata": f"{pair} paritesi desteklenmiyor.", "parite": pair}
        # Günlük deterministik dalgalanma (±%0,4) — kurun canlı olduğunu gösterir,
        # ama aynı gün içinde aynı sonucu verir (video çekimi için şart).
        rng = seeded("fx", pair, date.today().isoformat())
        rate = round(rate * (1 + rng.uniform(-0.004, 0.004)), 4)
        basamak = 2 if rate >= 1 else 4  # JPY/THB gibi küçük kurlarda 2 basamak yetmez
        cevrilen = round(rate * float(amount or 1), 2)
        return {
            "parite": f"{base}/{quote}",
            "kur": rate,
            # Yazdırılacak dize olgu paketinde de bulunsun (groundedness denetimi için)
            "kur_gosterim": tr_decimal(rate, basamak),
            "tutar": amount,
            "cevrilen": cevrilen,
            "cevrilen_gosterim": tr_decimal(cevrilen, 2),
            "tarih": date.today().isoformat(),
            "not": "Referans kurdur; bankalar ve döviz büroları alış-satış makası uygular.",
        }

    return cached_tool(keys.fx(pair), config.TTL_FX, _produce, provider="fx")


def destination_currency(destination: str) -> Optional[str]:
    """Destinasyonun para birimi kodu — döviz aracını otomatik beslemek için."""
    dest_key = kb.resolve_destination(destination) or destination
    dest = kb.destination(dest_key)
    return (dest or {}).get("currency")
