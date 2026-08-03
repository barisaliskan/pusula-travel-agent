"""Belge araçları: vize · pasaport geçerliliği — **YÜKSEK RİSK ALANI**.

CLAUDE.md kural 4: yalnızca T1 (resmî/kamusal) kaynak + atıf + geçerlilik tarihi +
zorunlu feragat. Kesin hukuki sonuç bildirilmez.

Bu modülün üç sert kuralı vardır ve hepsi kod seviyesinde uygulanır, promptta değil:

1. **Kapsam kilidi:** yalnızca T.C. vatandaşları (`from_country="TR"`) için veri vardır.
   Başka vatandaşlık sorulursa **cevap üretilmez**, resmî kanala yönlendirilir.
2. **Bilinmeyene cevap yok:** matriste olmayan ülke için tahmin yürütülmez.
3. **Her çıktı feragat taşır** ve geçerlilik tarihini açıkça yazar.

Vize matrisi olay-tabanlı invalidation'a tabidir (PLAN.md §5): mevzuat değişince
`visa:*` anahtarları ve ilgili semantic cache kayıtları anında temizlenir.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .. import config
from ..cache import cache, keys
from ..knowledge import kb
from .base import parse_date, source_of

RESMI_KANAL = ("T.C. Dışişleri Bakanlığı (mfa.gov.tr) ve ilgili ülkenin Türkiye'deki "
               "temsilciliği")

FERAGAT = ("Bu bilgi genel bilgilendirme amaçlıdır, hukuki sonuç doğurmaz ve bağlayıcı değildir. "
           "Vize ve giriş kuralları önceden haber verilmeksizin değişebilir; seyahatten önce "
           f"{RESMI_KANAL} üzerinden mutlaka teyit ediniz.")

_AY_RE = re.compile(r"en az\s*(\d+)\s*ay", re.IGNORECASE)


def get_visa_requirements(
    destination: str,
    citizenship: str = "TR",
    passport_type: str = "umuma_mahsus",
) -> dict:
    """T.C. vatandaşları için vize / giriş koşulları (T1 resmî kaynak).

    Args:
        destination: Destinasyon adı, anahtarı veya ülke adı.
        citizenship: Vatandaşlık ülke kodu. TR dışındaki değerlerde veri üretilmez.
        passport_type: umuma_mahsus (bordo) | hususi (yeşil) | hizmet | diplomatik
    """
    if (citizenship or "TR").strip().upper() != "TR":
        return {
            "kapsam_disi": True,
            "mesaj": (f"Vize matrisimiz yalnızca T.C. vatandaşları içindir. "
                      f"'{citizenship}' vatandaşlığı için bilgi üretmiyoruz — yanlış bilgi "
                      f"uçağa binememek demektir. Lütfen {RESMI_KANAL} veya ilgili ülkenin "
                      "resmî kaynaklarına başvurun."),
            "feragat": FERAGAT,
            "_kaynak": source_of("visa"), "_yuksek_risk": True, "_simule": False,
        }

    dest_key = kb.resolve_destination(destination)
    row = kb.visa_for_destination(dest_key) if dest_key else None
    if not row:
        # Ülke adıyla da dene (destinasyon anahtarı çözülemeyen "Japonya" gibi girdiler)
        hedef = (destination or "").strip().lower()
        for r in (kb.raw.get("visa", {}).get("matrix", {}) or {}).values():
            if hedef and hedef in r.get("country", "").lower():
                row = r
                break
    if not row:
        return {
            "bulunamadi": True,
            "mesaj": (f"'{destination}' için doğrulanmış vize kaydımız yok. Bu konuda tahmin "
                      f"yürütmüyoruz; güncel ve bağlayıcı bilgi için {RESMI_KANAL}."),
            "feragat": FERAGAT,
            "_kaynak": source_of("visa"), "_yuksek_risk": True, "_simule": False,
        }

    ptype = passport_type if passport_type in ("umuma_mahsus", "hususi_pasaport",
                                               "hizmet_pasaportu", "diplomatik_pasaport") else "umuma_mahsus"
    meta = kb.visa_meta
    pair = row.get("_pair") or next(
        (k for k, v in kb.raw["visa"]["matrix"].items() if v is row), f"TR->{row.get('country')}"
    )

    def _produce() -> dict:
        bilgi = row.get(ptype) or row.get("umuma_mahsus", {})
        return {
            "ulke": row.get("country"),
            "pasaport_turu": ptype,
            "vize_gerekli": bilgi.get("vize_gerekli"),
            "vize_turu": bilgi.get("tur", ""),
            "kalis_suresi": bilgi.get("sure", ""),
            "ozel_not": bilgi.get("not", ""),
            "schengen": row.get("schengen", False),
            "pasaport_gecerliligi": row.get("pasaport_gecerliligi", ""),
            "basvuru_notu": row.get("basvuru_notu", ""),
            "tipik_belgeler": row.get("tipik_belgeler", []),
            "ek_kosullar": row.get("ek_kosullar", []),
            "diger_pasaport_turleri": {
                k: {"vize_gerekli": v.get("vize_gerekli"), "sure": v.get("sure", "")}
                for k, v in row.items()
                if k.endswith("pasaport") or k == "umuma_mahsus"
                if isinstance(v, dict) and k != ptype
            },
        }

    value, status = cache.fetch(keys.visa("TR", row.get("country", "?")), _produce, config.TTL_VISA)
    out = dict(value)
    out.update({
        "gecerlilik_tarihi": meta.get("gecerlilik_sonu"),
        "son_dogrulama": meta.get("son_guncelleme") or meta.get("son_dogrulama"),
        "resmi_kanal": RESMI_KANAL,
        "feragat": FERAGAT,
        "_cache": status,
        "_kaynak": source_of("visa"),
        "_yuksek_risk": True,
        "_simule": False,
        "_invalidation": "Mevzuat değişikliğinde visa:* ve ilgili sc:* anahtarları olay tabanlı temizlenir.",
    })
    return out


def check_passport_validity(
    destination: str,
    passport_expiry: str,
    travel_date: Optional[str] = None,
    passport_issued: Optional[str] = None,
) -> dict:
    """Pasaportun hedef ülke kuralına göre yeterli olup olmadığını hesaplar (T1 kural + yerel hesap).

    Args:
        destination: Destinasyon/ülke.
        passport_expiry: Pasaport son geçerlilik tarihi (YYYY-AA-GG).
        travel_date: Seyahat (dönüş) tarihi; boşsa bugünden 30 gün sonrası.
        passport_issued: Düzenlenme tarihi — "son 10 yıl içinde düzenlenmiş" kuralı için.
    """
    kural = get_visa_requirements(destination)
    if kural.get("bulunamadi") or kural.get("kapsam_disi"):
        return kural

    expiry = parse_date(passport_expiry, default_offset_days=0)
    seyahat = parse_date(travel_date, default_offset_days=30)
    metin = kural.get("pasaport_gecerliligi", "") or ""
    m = _AY_RE.search(metin)
    gereken_ay = int(m.group(1)) if m else 6  # kural okunamazsa en sıkı yaygın kural

    # Gereken asgari geçerlilik: seyahat/çıkış tarihinden itibaren N ay
    y, mo = seyahat.year, seyahat.month + gereken_ay
    y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
    try:
        asgari = date(y, mo, seyahat.day)
    except ValueError:  # 31 -> kısa ay
        asgari = date(y, mo, 28)

    yeterli = expiry >= asgari
    kalan_gun = (expiry - asgari).days

    on_yil_uyari = None
    if passport_issued and "10 yıl" in metin:
        issued = parse_date(passport_issued, default_offset_days=0)
        if (seyahat - issued).days > 3652:
            on_yil_uyari = ("Pasaportunuz seyahat tarihinden 10 yıldan daha önce düzenlenmiş "
                            "görünüyor; bu ülke son 10 yıl içinde düzenlenmiş pasaport şartı arıyor.")

    return {
        "ulke": kural.get("ulke"),
        "kural_metni": metin,
        "gereken_asgari_ay": gereken_ay,
        "seyahat_tarihi": seyahat.isoformat(),
        "pasaport_son_gecerlilik": expiry.isoformat(),
        "gereken_asgari_gecerlilik_tarihi": asgari.isoformat(),
        "yeterli_mi": yeterli and not on_yil_uyari,
        "durum": ("Pasaport süresi bu seyahat için yeterli görünüyor."
                  if yeterli else
                  f"Pasaport süresi yetersiz görünüyor: kural gereği en az {asgari.isoformat()} "
                  f"tarihine kadar geçerli olmalı ({abs(kalan_gun)} gün eksik)."),
        "on_yil_uyarisi": on_yil_uyari,
        "gecerlilik_tarihi": kural.get("gecerlilik_tarihi"),
        "feragat": FERAGAT,
        "_kaynak": source_of("visa"),
        "_yuksek_risk": True,
        "_simule": False,
        "_cache": "yok (yerel hesap)",
    }
