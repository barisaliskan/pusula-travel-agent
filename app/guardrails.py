"""Guardrail katmanı — giriş ve çıkış.

Üç savunma hattı, bilinçli olarak üst üste bindirilmiştir:

  1. **Agno hazır guardrail'leri** — `PromptInjectionGuardrail`, `PIIDetectionGuardrail(mask_pii=True)`,
     `OpenAIModerationGuardrail`. Framework düzeyinde, ajan çalışmadan önce devreye girer.
  2. **Türkçe kalıp denetimi** (bu dosya) — Pegasus projesinden devralınan regex'ler + seyahat
     alanına özgü eklemeler. Agno'nun kalıpları İngilizce ağırlıklıdır; Türkçe saldırıyı kaçırabilir.
  3. **Çıkış denetimi** — groundedness, PII maskeleme, yüksek-risk feragat enjeksiyonu.

Modül framework'ten bağımsız çalışır (saf fonksiyonlar) — böylece mock modda ve testlerde
Agno çalıştırmadan doğrulanabilir. Agno'ya bağlanış `agno_input_guardrails()` ve
`groundedness_post_hook()` adaptörleriyle olur.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import config

# ─────────────────────────────────────────────────────────────────────
# Giriş kalıpları
# ─────────────────────────────────────────────────────────────────────
# Etik ihlali: yasa dışı / güvenlik riski taşıyan istekler
_ETHICS = [
    r"(uyuşturucu|narkotik|eroin|kokain|esrar)",
    r"(silah|bomba|patlayıcı|mermi)\b",
    r"(yasa ?dışı|kaçak|illegal).*(taşı|geçir|sakla|gizle|götür|giriş)",
    r"(sakla|gizle|kaçır|geçir).*(bagaj|valiz|çanta|gümrü[kğ]|uçak)",
    r"(kaçakçılık|smuggl)",
    r"(sahte|düzmece).*(pasaport|vize|bilet|belge|rezervasyon)",
    r"(vize|pasaport).*(sahtesi|nasıl.*kandır|atlat)",
    # Ünsüz yumuşaması: "gümrük" -> "gümrüğü". Sert ünsüzle yazılan kalıp
    # çekimli hâli kaçırır; Türkçe kalıplarda [kğ] gibi varyantlar şart.
    r"gümrü[kğ].*(kandır|atlat|görünmeden|kaçır)",
]

# Prompt-injection / manipülasyon
_INJECTION = [
    r"ignore.*(instruction|previous|rule|prompt)",
    r"disregard.*(instruction|previous|above)",
    r"(talimat|kural|yönerge)lar[ıi]n[ıi].*(yok say|unut|görmezden|boş ?ver|dikkate alma)",
    r"(önceki|yukarıdaki).*(talimat|kural).*(unut|yok say)",
    r"system prompt|sistem prompt|sistem mesaj",
    r"jailbreak|dan mode|developer mode|geliştirici modu",
    r"(sen artık|bundan sonra sen).*(değilsin|farklı bir)",
    r"(rolünü|kimliğini).*(değiştir|unut|bırak)",
    r"(bedava|ücretsiz|free).*(bilet|ticket|rezervasyon).*(ver|give|yap|ayarla)",
    r"(prompt|talimat)(ını|unu|ları).*(göster|yazdır|paylaş|söyle)",
]

# Kapsam dışı: seyahat asistanının uzmanlık alanı değil (kibarca devret)
_OUT_OF_SCOPE = [
    r"\b(hisse|borsa|kripto|bitcoin|yatırım tavsiye)",
    r"(hangi ilac|ilaç öner|teşhis|tedavi öner|hastalığım)",
    r"(dava|boşanma|mahkeme|hukuki tavsiye|avukat gibi)",
    r"(ödev|sınav).*(yap|çöz)",
]

# Yüksek riskli konular -> zorunlu feragat + T1 kaynak şartı (CLAUDE.md kural 4)
_HIGH_RISK = [
    r"\bvize\b", r"\bvizesi\b", r"pasaport", r"giriş koşul", r"schengen",
    r"\başı\b", r"aşılama", r"sağlık şart", r"karantina", r"sigorta zorunlu",
    r"gümrü[kğ].*(limit|kural)", r"ikamet izni", r"oturum izni",
]

# KVKK: özel nitelikli olabilecek veri sinyalleri (m.6) -> açık rıza olmadan profile YAZILMAZ
# DİKKAT: sonda `\b` KULLANMA. Türkçe ek alır: "Veganım", "Vejetaryenim", "helalse".
# `\bvegan\b` kalıbı "Veganım" ifadesini kaçırıyordu ve m.6 özel nitelikli veri sinyali
# hiç tetiklenmiyordu — yani KVKK uyarısı, tam da en çok gerektiği cümlede çalışmıyordu.
_SENSITIVE = [
    r"\b(vegan|vejetaryen|vejeteryan)", r"\bhelal", r"\bkoşer", r"glutensiz|çölyak",
    r"(tekerlekli sandalye|engelli|görme engel|işitme engel|ortopedik)",
    r"(hamile|gebe)", r"(diyabet|şeker hastal|alerji|astım|kalp hastal)",
    r"\b(müslüman|hristiyan|musevi|yahudi|ateist|alevi)",
]

# PII kalıpları — Türkiye'ye özgü olanlar Agno'nun varsayılan setinde yok
_PII_PATTERNS = {
    "e-posta": r"[\w.+-]+@[\w-]+\.[\w.-]{2,}",
    "TC kimlik no": r"\b[1-9]\d{10}\b",
    "IBAN": r"\bTR\d{2}[\s]?(?:\d{4}[\s]?){5}\d{2}\b",
    "kart no": r"\b(?:\d{4}[\s-]?){3}\d{4}\b",
    "telefon": r"(?:\+90|0)?[\s-]?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b",
    "pasaport no": r"\b[UAP]\d{8}\b",
}

_ETHICS_RE = [re.compile(p, re.IGNORECASE) for p in _ETHICS]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION]
_OUT_OF_SCOPE_RE = [re.compile(p, re.IGNORECASE) for p in _OUT_OF_SCOPE]
_HIGH_RISK_RE = [re.compile(p, re.IGNORECASE) for p in _HIGH_RISK]
_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in _SENSITIVE]
_PII_RE = {name: re.compile(p) for name, p in _PII_PATTERNS.items()}

# Hazır yanıtlar — engellenen istekte LLM'e hiç gidilmez (0 çağrı, 0 sızıntı)
ETHICS_REPLY = (
    "Bu konuda yardımcı olamam. Yasa dışı madde taşınması, sahte belge veya gümrük kurallarının "
    "atlatılması konularında yönlendirme yapmam mümkün değil.\n\n"
    "Bunun yerine size ülkelerin **yasal giriş koşullarını**, gümrük limitlerini veya bagaj "
    "kurallarını açıklayabilirim. Hangisini isterdiniz?"
)
INJECTION_REPLY = (
    "Bu isteği yerine getiremem. Ben Pusula'nın seyahat asistanıyım; rolüm ve çalışma kurallarım sabittir.\n\n"
    "Destinasyon önerisi, gezi planı, konaklama ve ulaşım, yöresel lezzetler, kültürel bilgiler, "
    "hava durumu, saat farkı ve vize bilgilendirmesi konularında yardımcı olabilirim. "
    "Nereden başlayalım?"
)
OUT_OF_SCOPE_REPLY = (
    "Bu konu uzmanlık alanımın dışında; yanlış bilgi vermemek için yönlendirme yapmıyorum. "
    "Konuyla ilgili yetkili bir uzmana danışmanızı öneririm.\n\n"
    "Seyahat planlaması, destinasyon önerileri, kültürel bilgiler ve pratik seyahat sorularında "
    "size yardımcı olabilirim."
)

HIGH_RISK_DISCLAIMER = (
    "\n\n---\n"
    "ℹ️ **Önemli:** Bu bilgi genel bilgilendirme amaçlıdır, hukuki sonuç doğurmaz ve bağlayıcı değildir. "
    "Vize ve giriş kuralları önceden haber verilmeksizin değişebilir. Seyahatinizden önce "
    "**T.C. Dışişleri Bakanlığı** ve ilgili ülkenin Türkiye'deki temsilciliğinden mutlaka teyit alınız."
)

UNGROUNDED_NOTICE = (
    "\n\n_Not: Bu yanıttaki bazı sayısal ayrıntılar doğrulanmış kaynaklarımızda teyit edilemedi; "
    "kesin bilgi için ilgili resmî kaynağı kontrol ediniz._"
)


# ─────────────────────────────────────────────────────────────────────
# Sonuç tipleri
# ─────────────────────────────────────────────────────────────────────
@dataclass
class InputVerdict:
    blocked: bool = False
    category: Optional[str] = None      # ethics | injection | out_of_scope
    reply: Optional[str] = None         # engellendiyse kullanıcıya dönecek hazır yanıt
    message: str = ""                   # PII maskelenmiş hali (modele bu gider)
    pii_found: list[str] = field(default_factory=list)
    high_risk: bool = False
    sensitive: bool = False             # KVKK m.6 sinyali
    triggered: list[str] = field(default_factory=list)  # trace için guardrail adları


@dataclass
class OutputVerdict:
    answer: str = ""
    grounded: bool = True
    unsupported: list[str] = field(default_factory=list)  # bağlamda bulunamayan sayısal iddialar
    pii_masked: list[str] = field(default_factory=list)
    disclaimer_added: bool = False
    triggered: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Giriş guardrail'i
# ─────────────────────────────────────────────────────────────────────
def mask_pii(text: str) -> tuple[str, list[str]]:
    """PII'yi maskele ve hangi türlerin bulunduğunu bildir.

    KVKK m.9 gerekçesi: LLM sağlayıcısı yurt dışı veri işleyicisidir. Maskeleme
    **modele gitmeden önce** yapılır, böylece sınır ötesine kişisel veri hiç geçmez.
    """
    found: list[str] = []
    out = text
    for name, rx in _PII_RE.items():
        if rx.search(out):
            found.append(name)
            out = rx.sub(f"[{name} gizlendi]", out)
    return out, found


def is_high_risk(text: str) -> bool:
    """Vize/pasaport/sağlık gibi zorunlu feragat gerektiren konu mu?"""
    return any(rx.search(text) for rx in _HIGH_RISK_RE)


def has_sensitive_signal(text: str) -> bool:
    """KVKK m.6 kapsamına girebilecek veri sinyali (diyet/inanç/sağlık/erişilebilirlik)."""
    return any(rx.search(text) for rx in _SENSITIVE_RE)


def check_input(message: str) -> InputVerdict:
    """Giriş denetimi. Engellenen istekte LLM'e hiç gidilmez."""
    triggered: list[str] = []

    for rx in _ETHICS_RE:
        if rx.search(message):
            return InputVerdict(blocked=True, category="ethics", reply=ETHICS_REPLY,
                                message=message, triggered=["ethics"])
    for rx in _INJECTION_RE:
        if rx.search(message):
            return InputVerdict(blocked=True, category="injection", reply=INJECTION_REPLY,
                                message=message, triggered=["prompt_injection"])
    for rx in _OUT_OF_SCOPE_RE:
        if rx.search(message):
            return InputVerdict(blocked=True, category="out_of_scope", reply=OUT_OF_SCOPE_REPLY,
                                message=message, triggered=["out_of_scope"])

    masked, pii = mask_pii(message)
    if pii:
        triggered.append("pii_mask")
    high_risk = is_high_risk(message)
    if high_risk:
        triggered.append("high_risk_topic")
    sensitive = has_sensitive_signal(message)
    if sensitive:
        triggered.append("sensitive_data")

    return InputVerdict(blocked=False, message=masked, pii_found=pii,
                        high_risk=high_risk, sensitive=sensitive, triggered=triggered)


# ─────────────────────────────────────────────────────────────────────
# Çıkış guardrail'i — groundedness
# ─────────────────────────────────────────────────────────────────────
# Olgusal iddia taşıyan örüntüler: para, süre, saat, yüzde, yıl, mesafe.
# Model "güzel bir şehirdir" diyebilir (dil); "girişi 900 TL" diyemez (olgu) —
# ikincisi bağlamda geçmiyorsa uydurulmuştur (CLAUDE.md kural 3).
# Sayı örüntüsü tek yerde: hem düz ("2500") hem binlik gruplu ("12.500", "1 500")
# hem ondalıklı ("12,5") biçimleri yakalar. `\d{1,3}` kullanmak 4 haneli düz sayıları
# kaçırırdı; `(?<!\d)` de sayının ortasından eşleşmeyi engeller.
# Binlik ayracı olarak BOŞLUK kabul edilmez. Edilirse tarayıcı, bağlamda yan yana duran
# iki ayrı sayıyı tek sayıya yapıştırır: "24800.0 30330.0" -> "0 303" olarak okunur ve
# gerçek değerler bağlam kümesinden düşer; sonuç, doğru sayıların "uydurma" sayılmasıdır.
# Türkçede binlik ayracı zaten noktadır ("30.330"), boşluk kullanılmaz.
_NUM = r"(?<!\d)\d+(?:\.\d{3})*(?:,\d+)?"

_CLAIM_RE = re.compile(
    rf"(?P<num>{_NUM})\s*"
    r"(?P<unit>TRY|TL|₺|EUR|€|USD|\$|dakika|dk|saat|gün|km|%|derece|°C)",
    re.IGNORECASE,
)
# Saat YALNIZCA iki nokta ile yazılır. Nokta da kabul edilirse ("\d{1,2}[:.]\d{2}")
# her ondalık sayı saat sanılır: skor "0.84", kur "47.93", sıcaklık "25.60" hepsi
# "uydurma saat" olarak işaretlenirdi. Bu, yanlış pozitif üreten bir kalıptı.
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")


def _normalize_number(s: str) -> str:
    return s.replace(" ", "").replace(".", "").replace(",", "")


def _serialize_context(context: Iterable[Any]) -> str:
    """Bağlamı tek metne indirger. Belge nesnesi, sözlük veya düz metin kabul eder."""
    parts: list[str] = []
    for c in context or []:
        if c is None:
            continue
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict):
            parts.append(" ".join(str(v) for v in c.values()))
        elif hasattr(c, "searchable"):
            parts.append(c.searchable())          # knowledge.Document
        elif hasattr(c, "doc"):
            parts.append(c.doc.searchable())      # knowledge.Hit
        else:
            parts.append(str(c))
    return " ".join(parts)


def check_groundedness(answer: str, context: Iterable[Any]) -> tuple[bool, list[str]]:
    """Yanıttaki sayısal iddiaların bağlamda karşılığı var mı?

    Bağlam olarak retrieval sonuçları **ve kullanıcı mesajı** verilmelidir: '4 günlük plan'
    isteğindeki '4 gün' kullanıcıdan gelir, uydurma değildir.
    """
    ctx = _serialize_context(context)
    ctx_numbers = {_normalize_number(m) for m in re.findall(_NUM, ctx)}
    ctx_times = set(_TIME_RE.findall(ctx))

    unsupported: list[str] = []
    for m in _CLAIM_RE.finditer(answer):
        num = _normalize_number(m.group("num"))
        # Küçük sayılar (gün sayısı, kişi sayısı, sıra) çoğunlukla kullanıcıdan/plandan gelir;
        # bunları uydurma saymak yanlış pozitif üretir.
        if len(num) <= 2 and int(num or 0) <= 30:
            continue
        if num not in ctx_numbers:
            unsupported.append(m.group(0).strip())
    for t in _TIME_RE.findall(answer):
        if t not in ctx_times:
            unsupported.append(t)

    return (not unsupported), unsupported


def check_output(
    answer: str,
    context: Iterable[Any] = (),
    *,
    high_risk: bool = False,
    enforce_groundedness: bool = True,
) -> OutputVerdict:
    """Çıkış denetimi: PII maskeleme + groundedness + yüksek-risk feragat enjeksiyonu."""
    triggered: list[str] = []

    masked, pii = mask_pii(answer)
    if pii:
        triggered.append("pii_mask_output")

    grounded, unsupported = (True, [])
    if enforce_groundedness:
        grounded, unsupported = check_groundedness(masked, context)
        if not grounded:
            triggered.append("groundedness")
            masked += UNGROUNDED_NOTICE

    disclaimer_added = False
    if high_risk and "Dışişleri Bakanlığı" not in masked:
        masked += HIGH_RISK_DISCLAIMER
        disclaimer_added = True
        triggered.append("high_risk_disclaimer")

    return OutputVerdict(answer=masked, grounded=grounded, unsupported=unsupported,
                         pii_masked=pii, disclaimer_added=disclaimer_added, triggered=triggered)


# ─────────────────────────────────────────────────────────────────────
# Agno adaptörleri
# ─────────────────────────────────────────────────────────────────────
def agno_input_guardrails(include_moderation: bool = False) -> list[Any]:
    """Agno `pre_hooks` listesi. Anahtar yoksa boş döner (mock modda kendi katmanımız çalışır).

    Bizim `check_input`'umuz HTTP katmanında zaten çalışır; buradakiler ajan seviyesindeki
    ikinci hattır — ajan bir araç çıktısıyla dolaylı olarak zehirlenirse de devreye girer.
    """
    guards: list[Any] = []
    try:
        from agno.guardrails import PIIDetectionGuardrail, PromptInjectionGuardrail

        guards.append(PromptInjectionGuardrail())
        guards.append(
            PIIDetectionGuardrail(
                mask_pii=True,  # KVKK m.9: PII modele gitmeden maskelenir
                enable_email_check=True,
                enable_phone_check=True,
                enable_credit_card_check=True,
                enable_ssn_check=True,
                # DİKKAT: `custom_patterns` SÖZLÜK ister ({ad: kalıp}); liste verilirse
                # Agno `.items()` çağırıp AttributeError atar ve tüm guardrail katmanı
                # sessizce devre dışı kalır. Kurulu sürümde doğrulandı.
                custom_patterns={ad: _PII_PATTERNS[ad] for ad in
                                 ("TC kimlik no", "IBAN", "pasaport no")},
            )
        )
        if include_moderation and config.LLM_MODE == "openai" and not config.OPENAI_BASE_URL:
            # Moderation uç noktası yalnızca gerçek OpenAI'da var; GitHub Models'ta yok.
            from agno.guardrails import OpenAIModerationGuardrail

            guards.append(OpenAIModerationGuardrail(api_key=config.OPENAI_API_KEY))
    except Exception as exc:
        print(f"[guardrails] Agno guardrail'leri kurulamadı ({exc.__class__.__name__}) -> yalnızca yerel katman")
    return guards


def make_groundedness_post_hook(context_provider):
    """Agno `post_hooks` için çıkış denetimi üretir.

    `context_provider()` o an kullanılan bağlamı (retrieval sonuçları + kullanıcı mesajı)
    döndürmelidir. Agno hook'a imzasına göre argüman geçirir; biz yalnızca `run_output` istiyoruz.
    """

    def groundedness_post_hook(run_output) -> None:
        content = getattr(run_output, "content", None)
        if not isinstance(content, str) or not content.strip():
            return  # yapılandırılmış çıktı (output_schema) — sayısal denetim şemada yapılır
        ctx, high_risk = context_provider()
        verdict = check_output(content, ctx, high_risk=high_risk)
        run_output.content = verdict.answer

    return groundedness_post_hook


def summary() -> dict:
    """Sunum/UI için: hangi guardrail'ler aktif."""
    return {
        "giris": ["prompt_injection (Agno + TR kalıpları)", "pii_mask (KVKK m.9)",
                  "ethics", "out_of_scope", "high_risk_topic", "sensitive_data (KVKK m.6)"],
        "cikis": ["groundedness", "pii_mask_output", "high_risk_disclaimer"],
        "kalip_sayisi": {
            "ethics": len(_ETHICS), "injection": len(_INJECTION), "out_of_scope": len(_OUT_OF_SCOPE),
            "high_risk": len(_HIGH_RISK), "sensitive": len(_SENSITIVE), "pii": len(_PII_PATTERNS),
        },
        "yuksek_risk_konular": list(config.HIGH_RISK_TOPICS),
    }
