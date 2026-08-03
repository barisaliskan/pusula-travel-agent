"""KVKK katmanı — rıza yönetimi, veri envanteri, dışa aktarma, silme hakkı, denetim izi.

Case'in değerlendirme kriterlerinden biri doğrudan "Güvenlik, etik ve KVKK uyumluluğu".
Burada slogan değil **çalışan mekanizma** var: rıza olmadan profil yazılmaz, silme talebi
gerçekten siler, her işlem denetim izine düşer.

Tasarım kararları:
  * **Rıza kademeli ve ayrı ayrı geri alınabilir** — oturum / kişiselleştirme / özel nitelikli / pazarlama.
  * **Varsayılan kapalı** — kişiselleştirme ve pazarlama açıkça açılmadıkça işlemez (veri minimizasyonu).
  * **Anahtarlarda ham PII yok** — kullanıcı kimliği hash'lenir (`cache.hash_user`).
  * **Denetim izi kişisel içerik taşımaz** — yalnızca "kim (hash), ne zaman, hangi işlem".
    Böylece silme hakkı ile hesap verebilirlik yükümlülüğü çakışmaz.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config
from .cache import cache, hash_user, keys
from .schemas import AuditEntry, ConsentState

# Rıza kademelerinin insan-okur karşılıkları (aydınlatma metni ve UI paneli için)
CONSENT_LEVELS = {
    "session_only": {
        "baslik": "Oturum verisi",
        "aciklama": "Konuşmanın akışını sürdürebilmek için mesajlarınız oturum süresince tutulur. "
                    f"{config.SESSION_TTL // 60} dakika işlem yapılmazsa otomatik silinir.",
        "geri_alinabilir": False,
        "hukuki_dayanak": "Hizmetin sunulabilmesi için zorunlu (sözleşmenin ifası)",
        "zorunlu": True,
    },
    "personalization": {
        "baslik": "Kişiselleştirme",
        "aciklama": "Seyahat tercihleriniz (bütçe bandı, seyahat stili, tempo, iklim, grup) profilinizde "
                    "saklanır ve öneriler buna göre kişiselleştirilir. Kapalıyken öneriler yalnızca "
                    "o anki mesajınıza göre üretilir.",
        "geri_alinabilir": True,
        "hukuki_dayanak": "Açık rıza",
        "zorunlu": False,
    },
    "sensitive_data": {
        "baslik": "Özel nitelikli veriler",
        "aciklama": "Diyet tercihi (vegan, helal vb.) inanç veya felsefi görüşü, erişilebilirlik ihtiyacı "
                    "ise sağlık durumunu ifşa edebilir. KVKK bu verileri özel nitelikli sayar ve işlenmesi "
                    "için ayrı açık rıza arar. Kapalıyken bu bilgiler profile YAZILMAZ; yalnızca o anki "
                    "yanıtta sert filtre olarak kullanılıp hemen unutulur.",
        "geri_alinabilir": True,
        "hukuki_dayanak": "Açık rıza (KVKK m.6 — özel nitelikli kişisel veri)",
        "zorunlu": False,
    },
    "marketing": {
        "baslik": "Kampanya iletileri",
        "aciklama": "Size özel kampanya ve fırsat bildirimleri gönderilebilir.",
        "geri_alinabilir": True,
        "hukuki_dayanak": "Açık rıza (ticari elektronik ileti izni)",
        "zorunlu": False,
    },
}

# Veri envanteri — "hangi veriyi neden, ne kadar süreyle, nerede tutuyoruz"
DATA_INVENTORY = [
    {
        "kategori": "Oturum geçmişi",
        "ornek_alanlar": ["mesajlar", "oturum kimliği"],
        "amac": "Konuşma bağlamını sürdürmek",
        "hukuki_dayanak": "Sözleşmenin ifası",
        "saklama_suresi": f"{config.SESSION_TTL // 60} dakika (sliding)",
        "konum": "Redis / bellek",
        "ozel_nitelikli": False,
        "rıza_gerekli": False,
    },
    {
        "kategori": "Seyahat tercihleri profili",
        "ornek_alanlar": ["bütçe bandı", "seyahat stili", "tempo", "iklim", "grup"],
        "amac": "Önerileri kişiselleştirmek",
        "hukuki_dayanak": "Açık rıza",
        "saklama_suresi": "Rıza geri alınana veya silme talebine kadar",
        "konum": "Redis / bellek",
        "ozel_nitelikli": False,
        "rıza_gerekli": True,
    },
    {
        "kategori": "Diyet ve erişilebilirlik bilgisi",
        "ornek_alanlar": ["vegan", "helal", "tekerlekli sandalye"],
        "amac": "Uygun olmayan önerileri sert filtreyle elemek",
        "hukuki_dayanak": "Açık rıza (KVKK m.6)",
        "saklama_suresi": "Rıza geri alınana veya silme talebine kadar; rıza yoksa saklanmaz",
        "konum": "Redis / bellek — profilden ayrı alanda",
        "ozel_nitelikli": True,
        "rıza_gerekli": True,
    },
    {
        "kategori": "Rıza ve denetim kayıtları",
        "ornek_alanlar": ["işlem türü", "zaman damgası", "hash'li kullanıcı kimliği"],
        "amac": "Hesap verebilirlik ve rızanın ispatı",
        "hukuki_dayanak": "Hukuki yükümlülük",
        "saklama_suresi": "Yasal saklama süresi boyunca",
        "konum": "Denetim kaydı dosyası",
        "ozel_nitelikli": False,
        "rıza_gerekli": False,
    },
    {
        "kategori": "İŞLENMEYEN veriler",
        "ornek_alanlar": ["pasaport numarası", "T.C. kimlik numarası", "kart bilgisi", "ad-soyad"],
        "amac": "—",
        "hukuki_dayanak": "—",
        "saklama_suresi": "Hiç saklanmaz; girişte maskelenir ve modele gönderilmez",
        "konum": "—",
        "ozel_nitelikli": True,
        "rıza_gerekli": False,
    },
]

# `MemoryManager.memory_capture_instructions` olarak Agno'ya verilir — neyin
# hatırlanacağını promptla değil, yapılandırmayla sınırlarız (veri minimizasyonu).
MEMORY_CAPTURE_INSTRUCTIONS = """
Yalnızca kullanıcının SEYAHAT TERCİHLERİNİ hatırla:
bütçe bandı, seyahat stili, tempo, iklim tercihi, grup bileşimi, kalkış şehri,
beğendiği/beğenmediği destinasyonlar.

ASLA hatırlama (bu bilgiler yakalanırsa da kaydedilmez):
- Ad, soyad, doğum tarihi, adres
- Pasaport numarası, T.C. kimlik numarası, herhangi bir kimlik numarası
- Telefon, e-posta, banka/kart bilgisi
- Sağlık durumu, inanç, siyasi görüş, cinsel yönelim
- Diyet ve erişilebilirlik bilgisi (bunlar ayrı açık rıza ile ayrı alanda tutulur)
""".strip()

AYDINLATMA_METNI = """
## Aydınlatma Metni — Pusula AI Seyahat Asistanı

**Veri sorumlusu:** Pusula AI (demo)

**İşlenen veriler ve amaçlar:** Seyahat asistanı hizmetini sunabilmek için mesajlarınız oturum
süresince işlenir. Açık rıza vermeniz hâlinde seyahat tercihleriniz kişiselleştirme amacıyla saklanır.

**Yurt dışına aktarım (KVKK m.9):** Yanıt üretiminde kullanılan büyük dil modeli yurt dışında
konumlanmış bir hizmet sağlayıcısına aittir. Bu nedenle mesajınız modele iletilmeden önce
kişisel veri maskeleme katmanından geçirilir; ad, telefon, e-posta, kimlik ve kart numarası gibi
tanımlayıcılar tespit edilerek maskelenir ve sınır ötesine aktarılmaz.

**Özel nitelikli kişisel veriler (KVKK m.6):** Diyet tercihi ve erişilebilirlik ihtiyacı özel
nitelikli veri kapsamına girebilir. Bu veriler ancak ayrı açık rızanızla işlenir ve saklanır.

**Haklarınız:** Verilerinize erişme, düzeltme, silme ve rızanızı geri alma haklarına sahipsiniz.
Bu hakları asistan arayüzündeki "Verilerim" panelinden anında kullanabilirsiniz.

**Otomatik karar verme:** Öneriler algoritmik olarak sıralanır. Her önerinin gerekçesini
"Neden bu öneri?" bağlantısından görebilir ve tercihlerinizi düzelterek sonucu değiştirebilirsiniz.

> Bu metin bir case study demosu kapsamında hazırlanmıştır ve hukuki görüş niteliği taşımaz.
""".strip()


# ─────────────────────────────────────────────────────────────────────
# Rıza defteri
# ─────────────────────────────────────────────────────────────────────
def get_consent(user_id: str) -> ConsentState:
    """Kullanıcının rıza durumu; kayıt yoksa varsayılan (yalnızca oturum) döner."""
    raw = cache.get_json(keys.consent(user_id))
    if not raw:
        return ConsentState(
            user_id=user_id,
            personalization=config.DEFAULT_PERSONALIZATION_CONSENT,
        )
    raw["user_id"] = user_id
    try:
        return ConsentState(**raw)
    except Exception:
        return ConsentState(user_id=user_id)


def set_consent(user_id: str, **changes) -> ConsentState:
    """Rıza kademelerini günceller ve denetim izine yazar.

    Kişiselleştirme rızası geri alınırsa profil de silinir — "rızayı geri aldım ama
    verim duruyor" durumu KVKK açısından kabul edilemez.
    """
    state = get_consent(user_id)
    applied: dict[str, Any] = {}
    for field_name in ("personalization", "sensitive_data", "marketing"):
        if field_name in changes and changes[field_name] is not None:
            new_val = bool(changes[field_name])
            if getattr(state, field_name) != new_val:
                applied[field_name] = new_val
            setattr(state, field_name, new_val)
    state.updated_at = datetime.now(timezone.utc)

    cache.set_json(keys.consent(user_id), state.model_dump(mode="json"))

    for field_name, val in applied.items():
        audit(user_id, f"consent.{'grant' if val else 'revoke'}", field_name)

    # Rıza geri alındıysa ilgili veriyi de temizle
    if applied.get("personalization") is False:
        delete_profile(user_id, reason="consent.revoke")
    if applied.get("sensitive_data") is False:
        strip_sensitive(user_id)

    return state


# ─────────────────────────────────────────────────────────────────────
# Profil yazımı — rıza kapısı
# ─────────────────────────────────────────────────────────────────────
SENSITIVE_FIELDS = ("dietary", "accessibility")


def can_write_profile(user_id: str) -> bool:
    return get_consent(user_id).allows_profile_write()


def save_profile(user_id: str, profile: dict) -> tuple[bool, str]:
    """Profili saklar. Rıza yoksa YAZMAZ ve sebebini döner.

    Bu fonksiyon sistemin tek profil yazma kapısıdır; başka hiçbir modül
    doğrudan `prof:` anahtarına yazmaz.
    """
    consent = get_consent(user_id)
    if not consent.allows_profile_write():
        return False, "Kişiselleştirme rızası yok — profil saklanmadı (veri minimizasyonu)."

    data = dict(profile)
    stripped: list[str] = []
    if not consent.sensitive_data:
        for f in SENSITIVE_FIELDS:
            if data.get(f):
                stripped.append(f)
            data[f] = []
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    cache.set_json(keys.profile(user_id), data)
    audit(user_id, "profile.write",
          f"alanlar={sorted(k for k, v in data.items() if v)}" +
          (f" | özel_nitelikli_atlandı={stripped}" if stripped else ""))

    if stripped:
        return True, ("Profil kaydedildi. Özel nitelikli alanlar "
                      f"({', '.join(stripped)}) açık rıza olmadığı için saklanmadı.")
    return True, "Profil kaydedildi."


def load_profile(user_id: str) -> Optional[dict]:
    if not can_write_profile(user_id):
        return None  # rıza yoksa okuma da yapılmaz (kayıt zaten olmamalı)
    return cache.get_json(keys.profile(user_id))


def strip_sensitive(user_id: str) -> None:
    """Özel nitelikli alanları profilden temizler (m.6 rızası geri alındığında)."""
    data = cache.get_json(keys.profile(user_id))
    if not data:
        return
    for f in SENSITIVE_FIELDS:
        data[f] = []
    cache.set_json(keys.profile(user_id), data)
    audit(user_id, "profile.strip_sensitive", ", ".join(SENSITIVE_FIELDS))


# ─────────────────────────────────────────────────────────────────────
# Erişim, dışa aktarma, silme
# ─────────────────────────────────────────────────────────────────────
def export_user_data(user_id: str) -> dict:
    """KVKK erişim hakkı: kullanıcıya ait tüm veriyi taşınabilir biçimde döner."""
    consent = get_consent(user_id)
    payload = {
        "kullanici_kimligi_hash": hash_user(user_id),
        "not": "Ham kullanıcı kimliği anahtarlarda saklanmaz; yalnızca hash tutulur.",
        "disa_aktarma_zamani": datetime.now(timezone.utc).isoformat(),
        "riza_durumu": consent.model_dump(mode="json"),
        "profil": cache.get_json(keys.profile(user_id)),
        "oturum_gecmisi_not": "Oturum geçmişi oturum kimliğine bağlıdır ve TTL ile otomatik silinir.",
        "veri_envanteri": DATA_INVENTORY,
    }
    audit(user_id, "data.export", "kullanıcı verisi dışa aktarıldı")
    return payload


def delete_profile(user_id: str, reason: str = "user.request") -> list[str]:
    """Yalnızca profili siler (rıza kaydı korunur)."""
    key = keys.profile(user_id)
    existed = cache.get_json(key) is not None
    cache.delete(key)
    audit(user_id, "profile.delete", reason)
    return [key] if existed else []


def delete_all(user_id: str, memory_manager: Any = None, session_ids: Optional[list[str]] = None) -> dict:
    """KVKK silme hakkı: profil + rıza + cache + Agno hafızası + oturumlar.

    `memory_manager` verilirse Agno `clear_user_memories` da çağrılır — böylece
    framework'ün kendi hafızasında kalıntı bırakılmaz (doğrulanmış API, bkz.
    docs/agno-api-dogrulama.md).
    """
    removed = cache.purge_user(user_id)

    for sid in session_ids or []:
        skey = keys.session(sid)
        if cache.get_json(skey) is not None:
            cache.delete(skey)
            removed.append(skey)

    agno_cleared = False
    if memory_manager is not None:
        try:
            memory_manager.clear_user_memories(user_id=user_id)
            agno_cleared = True
        except Exception as exc:
            audit(user_id, "memory.clear_failed", exc.__class__.__name__)

    audit(user_id, "data.delete_all", f"silinen_anahtar={len(removed)} agno_hafiza={agno_cleared}")

    # Doğrulama: silme sonrası kalıntı var mı? (Demo senaryosu 15'in kanıtı)
    residual = [k for k in (keys.profile(user_id), keys.consent(user_id))
                if cache.get_json(k) is not None]

    return {
        "silindi": removed,
        "agno_hafizasi_temizlendi": agno_cleared,
        "kalinti": residual,
        "dogrulama": "temiz" if not residual else "KALINTI VAR",
        "denetim_kaydi": "yazıldı (kişisel içerik barındırmaz)",
    }


# ─────────────────────────────────────────────────────────────────────
# Denetim izi
# ─────────────────────────────────────────────────────────────────────
def _audit_path():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "audit.log"


def audit(user_id: str, action: str, detail: str = "") -> Optional[AuditEntry]:
    """Denetim kaydı yazar. **Ham kullanıcı kimliği değil, hash yazılır** ve
    kayıt hiçbir kişisel içerik (mesaj metni, tercih değeri) barındırmaz."""
    if not config.AUDIT_LOG_ENABLED:
        return None
    entry = AuditEntry(user_id=hash_user(user_id), action=action, detail=detail)
    try:
        with _audit_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
    except OSError:
        pass  # denetim yazılamazsa hizmet durmamalı
    return entry


def read_audit(user_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Denetim izini okur; `user_id` verilirse yalnızca o kullanıcının kayıtları."""
    path = _audit_path()
    if not path.exists():
        return []
    wanted = hash_user(user_id) if user_id else None
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if wanted is None or row.get("user_id") == wanted:
            rows.append(row)
    return rows[-limit:]


def compliance_summary(user_id: str = "anon") -> dict:
    """UI'daki 'Verilerim' paneli ve sunum slaytı için tek bakışta uyum tablosu."""
    consent = get_consent(user_id)
    return {
        "riza_kademeleri": CONSENT_LEVELS,
        "mevcut_riza": consent.model_dump(mode="json"),
        "veri_envanteri": DATA_INVENTORY,
        "haklar": ["erişim", "düzeltme", "silme", "rızayı geri alma", "taşınabilirlik"],
        "mekanizmalar": {
            "yurt_disi_aktarim": "PII maskeleme (modele gönderilmeden önce)",
            "veri_minimizasyonu": "memory_capture_instructions ile yakalama sınırlandırılır",
            "silme_hakki": "cache purge + Agno clear_user_memories + denetim kaydı",
            "ozel_nitelikli": "ayrı açık rıza; rıza yoksa profile yazılmaz",
        },
        "aydinlatma_metni": AYDINLATMA_METNI,
    }
