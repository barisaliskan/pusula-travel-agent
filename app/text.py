"""Türkçe metin normalizasyonu — tek yerden.

**Neden ayrı bir modül:** Python'un `str.lower()` metodu Türkçe için yanlıştır ve
hatası *sessizdir*.

    "YENİR".lower()  ->  "yeni̇r"   (i + U+0307 birleşen nokta — iki karakter!)
    "IŞIK".lower()   ->  "işik"     (I -> i olmalıydı, ı bekleniyordu)

Sonuç: kullanıcı büyük harfle yazdığında (`ROMA'DA NE YENİR`) anahtar kelime eşleşmesi
kırılır, istek yanlış ajana gider ve **hiçbir hata mesajı görünmez**. Bu hata canlı
kullanımda yakalandı.

`tr_lower` önce Türkçeye özgü iki harfi elle eşler, sonra normal `lower()` uygular.
`fold` ayrıca aksanları katlar — böylece aksansız yazan kullanıcı ("nasil", "gorgu")
aksanlı bilgi tabanıyla eşleşir.
"""
from __future__ import annotations

# Sıra önemli: önce İ/I elle çevrilir, sonra lower() kalanı halleder.
_TR_LOWER = str.maketrans({"İ": "i", "I": "ı", "Î": "î", "Â": "â", "Û": "û"})
_TR_UPPER = str.maketrans({"i": "İ", "ı": "I"})
_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def tr_lower(text: str) -> str:
    """Türkçe doğru küçük harfe çevirme. `İ -> i`, `I -> ı`."""
    return text.translate(_TR_LOWER).lower()


def tr_upper(text: str) -> str:
    """Türkçe doğru büyük harfe çevirme. `i -> İ`, `ı -> I`."""
    return text.translate(_TR_UPPER).upper()


def fold(text: str) -> str:
    """Küçük harf + aksan katlama: karşılaştırma ve arama için kanonik biçim.

    "ROMA'DA NE YENİR" -> "roma'da ne yenir"
    "Görgü Kuralları"  -> "gorgu kurallari"
    """
    return tr_lower(text).translate(_FOLD)
