"""Araç (tool) altyapısı — deterministik mock adapter'ların ortak zemini.

Gerçek seyahat API'lerine (Amadeus, Google Places, Open-Meteo) erişimimiz yok.
Bu yüzden her araç, **gerçek API'ye geçişe uygun imzayla** yazılmış bir adapter'dır:
gövde simüle eder, imza ve dönüş sözleşmesi üretimdekiyle aynıdır. Üretime geçişte
yalnızca `_produce` gövdesi HTTP çağrısıyla değiştirilir; çağıran hiçbir kod değişmez.

Üç değişmez kural (CLAUDE.md kural 2 ve 3):

1. **Determinizm.** Aynı girdi her zaman aynı çıktıyı verir (`seeded` RNG). Video çekimi
   sırasında ekranda gördüğümüz sayı, provada gördüğümüz sayıyla aynı olmalı.
2. **Şeffaflık.** Simüle her kayıt `_simule: True` ve `_kaynak` künyesi taşır; arayüz
   bunu rozetle gösterir, sunumda açıkça söylenir. Uydurma veriyi gerçekmiş gibi sunmuyoruz.
3. **Uydurma işletme kaydı yok.** Otel/restoran adı ÜRETİLMEZ (PROGRESS.md kararı).
   Adapter, gerçek API'nin dolduracağı **yer tipi + semt** yuvasını döner
   ("Trastevere'de geleneksel trattoria"), sahte bir işletme kimliği değil.

Her araç cache'ten geçer (PLAN.md §5 anahtar şeması + TTL tablosu) — Çıktı 5'in
soyut planı değil, çalışan hâli. Dönen sözlükteki `_cache` alanı hit/miss durumunu
taşır ve arayüzdeki gecikme rozetini besler.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from ..cache import cache

# Simüle adapter'ların künyesi: üretimde hangi sağlayıcının geleceği burada yazılı.
# Sunumda "hangi veri nereden gelecek" sorusunun (Çıktı 4) araç seviyesindeki cevabı.
PROVIDERS = {
    "weather": ("Open-Meteo (simüle)", "T3", "Açık veri hava servisi; üretimde canlı API"),
    "timezone": ("IANA tzdata (gerçek)", "T3", "zoneinfo ile yerel hesap — dış API yok"),
    "fx": ("TCMB/ECB kuru (simüle)", "T3", "Üretimde merkez bankası referans kuru"),
    "flights": ("Amadeus Flight Offers (simüle)", "T2", "Lisanslı ticari envanter"),
    "hotels": ("Amadeus / Hotelbeds (simüle)", "T2", "Lisanslı ticari envanter"),
    "restaurants": ("Google Places / Foursquare (simüle)", "T2", "Canlı mekân envanteri"),
    "pois": ("Pusula İçerik Editörlüğü — POI seti", "T0", "Kurum içi küratörlü içerik"),
    "curated": ("Pusula İçerik Editörlüğü", "T0", "Kurum içi küratörlü içerik"),
    "visa": ("T.C. Dışişleri Bakanlığı / IATA Timatic", "T1", "Resmî kaynak — yüksek risk"),
}


def source_of(provider: str) -> dict:
    """Aracın kaynak künyesi — atıf satırı ve groundedness denetimi bunu kullanır."""
    title, tier, note = PROVIDERS.get(provider, PROVIDERS["curated"])
    return {"baslik": title, "kademe": tier, "not": note}


def seeded(*parts: Any) -> random.Random:
    """Girdiye bağlı deterministik RNG. Aynı sorgu -> aynı sonuç, her çalıştırmada."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def parse_date(value: Optional[str], default_offset_days: int = 30) -> date:
    """'2026-09-14' -> date. Boş/bozuksa bugünden `default_offset_days` sonrası.

    Araçlar tarihsiz de çağrılabilmeli: kullanıcı "Roma'da hava nasıl" derken tarih vermez.
    """
    if value:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except ValueError:
                continue
    return date.fromordinal(date.today().toordinal() + default_offset_days)


# Araç çıktı şemasının sürümü. Bir aracın dönüş alanları değiştiğinde bunu artır:
# önbellekteki ESKİ ŞEKİLLİ kayıtlar kod değişikliğinden sağ çıkıp `KeyError` üretiyordu
# (canlı olarak yaşandı: `kur_gosterim` alanı eklendi, Redis'teki eski kayıtta yoktu).
# Sürümü anahtara katmak bu sınıf hatayı tümüyle kapatır.
TOOL_SCHEMA_VERSION = "v2"


def cached_tool(key: str, ttl: int, producer: Callable[[], dict], *, provider: str) -> dict:
    """Aracı cache-aside sarmalayıcıdan geçirir ve künye/durum alanlarını ekler.

    `cache.fetch` single-flight + stale-while-revalidate sağlar: aynı anahtarı aynı anda
    isteyen 50 istek tek üretim yapar, üretim patlarsa bayat veri servis edilir.
    """
    value, status = cache.fetch(f"{key}#{TOOL_SCHEMA_VERSION}", producer, ttl)
    out = dict(value)
    out["_cache"] = status
    out["_kaynak"] = source_of(provider)
    out.setdefault("_simule", provider not in ("timezone", "pois", "curated", "visa"))
    out.setdefault("_alindi", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out


def try_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def tl(amount: float) -> str:
    """Türkçe binlik ayraçlı tutar: 12500 -> '12.500'."""
    return f"{int(round(amount)):,}".replace(",", ".")


def tr_decimal(value: float, digits: int = 2) -> str:
    """Türkçe ondalık gösterim: 47.9307 -> '47,93'.

    Neden önemli: Türkçede nokta BİNLİK ayracıdır. '47.9307 TRY' yazarsak hem kullanıcı
    yanlış okur hem de groundedness denetimi metni '47.930' + '7' diye parçalar ve
    olmayan bir '9307 TRY' iddiası görür. Bu yüzden gösterim dizesi tek yerde üretilir
    ve **olgu paketine de o dize konur** — yazdığımızla doğruladığımız aynı şey olur.
    """
    return f"{value:,.{digits}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
