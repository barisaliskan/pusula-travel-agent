"""Model registry — Çıktı 6'nın kod karşılığı.

Tek kural: **hiçbir modül model id'si hardcode etmez**, hepsi buradan ister.
Böylece sağlayıcı/sürüm değişimi tek dosyada kalır.

Görev bazlı katmanlar (PLAN.md §6):
    classifier  -> en ucuz/hızlı  (yol seçimi, tek etiket)
    leader      -> orta-güçlü     (delegasyon + sentez)
    planner     -> en güçlü       (çok günlük planlama, akıl yürütme yoğun)
    specialist  -> ucuz-orta      (dar kapsam, tool sonucunu dile çevirme)
    embedder    -> text-embedding-3-small (RAG + semantic cache)

Sağlayıcı seçimi:
    OPENAI_BASE_URL dolu  -> OpenAILike  (GitHub Models; id'ler "openai/" önekli)
    OPENAI_BASE_URL boş   -> OpenAIChat  (gerçek OpenAI)
    OPENAI_API_KEY yok    -> None        -> çağıran taraf mock moda düşer

`None` dönmek bilinçli bir tasarım: ajan katmanı "model yoksa mock şablon" dalını
zaten yürütmek zorunda (CLAUDE.md kural 2), bu yüzden burada sahte bir model
nesnesi üretip hatayı gizlemek yerine yokluğu açıkça bildiriyoruz.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Optional

from . import config

# Katman adı -> config'teki model id'si
_TIERS = {
    "leader": config.MODEL_LEADER,
    "planner": config.MODEL_PLANNER,
    "specialist": config.MODEL_SPECIALIST,
    "classifier": config.MODEL_CLASSIFIER,
}

# Aynı katman için tekrar tekrar istemci kurmayalım (bağlantı havuzu paylaşılsın).
_cache: dict[str, Any] = {}


def available() -> bool:
    """Gerçek LLM kullanılabilir mi? False ise tüm sistem mock modda çalışır."""
    return config.LLM_MODE == "openai"


def model_id(tier: str) -> str:
    """Katmanın model id'si — trace/loglarda göstermek için (anahtar olmasa da döner)."""
    return _TIERS.get(tier, config.MODEL_SPECIALIST)


def get_model(tier: str = "specialist", **overrides) -> Optional[Any]:
    """Katmana karşılık gelen Agno model nesnesi; anahtar yoksa None.

    `overrides` ile tek seferlik parametre geçilebilir (ör. temperature=0 sınıflandırıcıda).
    """
    if not available():
        return None

    key = f"{tier}:{sorted(overrides.items())}"
    if key in _cache:
        return _cache[key]

    from agno.models.openai import OpenAIChat, OpenAILike

    params: dict[str, Any] = {
        "id": model_id(tier),
        "api_key": config.OPENAI_API_KEY,
        "temperature": config.GEN_TEMPERATURE,
        # Stabil/tutarlı yanıt (case: "tutarlı yanıt mekanizması"). seed sabit -> regresyon testi mümkün.
        "seed": 42,
        "timeout": 30.0,
        "max_retries": 2,
    }
    params.update(overrides)

    if config.OPENAI_BASE_URL:
        # OpenAI-uyumlu sağlayıcı (GitHub Models). Gerçek OpenAI'ya geçiş: base_url'i boşalt.
        model = OpenAILike(base_url=config.OPENAI_BASE_URL, **params)
    else:
        model = OpenAIChat(**params)

    _cache[key] = model
    return model


def get_classifier_model() -> Optional[Any]:
    """Karmaşıklık sınıflandırıcı: tek etiket döner, yaratıcılık istemiyoruz."""
    return get_model("classifier", temperature=0.0, max_tokens=10)


# ─────────────────────────────────────────────────────────────────────
# Embedding — RAG + semantic cache
# ─────────────────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[a-zçğıöşü0-9]+", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Türkçe aksan katlaması + doğru küçük harf (bkz. app/text.py).

    Katlamazsak aynı soru cache'te ıskalanır ve gereksiz LLM çağrısı yapılır;
    `str.lower()` kullanırsak büyük harfli girdi hiç eşleşmez.
    """
    from .text import fold

    return fold(text)


def mock_embed(text: str, dim: int = config.MOCK_EMBED_DIM) -> list[float]:
    """Bağımlılıksız hashing vectorizer — anahtarsız modda semantic cache'i çalıştırır.

    Neden torch/sentence-transformers değil: /home %91 dolu (PROGRESS.md kararı).
    Neden yine de işe yarıyor: semantic cache'in ihtiyacı "aynı soru tekrar mı soruldu"
    ayrımı; karakter n-gram + kelime hashing bunu yeterince yakalar. Gerçek anlamsal
    yakınlık gerektiğinde USE_REMOTE_EMBEDDINGS=true ile API embedder devreye girer.
    """
    vec = [0.0] * dim
    words = _TOKEN_RE.findall(_normalize(text))
    for w in words:
        h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
        # Karakter 3-gram'ları: ek/çekim farklarına (Roma/Roma'da) dayanıklılık kazandırır.
        for i in range(len(w) - 2):
            g = int(hashlib.md5(w[i:i + 3].encode("utf-8")).hexdigest(), 16)
            vec[g % dim] += 0.5
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def get_embedder() -> Optional[Any]:
    """Agno `OpenAIEmbedder`; anahtar yoksa veya uzak embedding kapalıysa None (-> mock_embed)."""
    if not available() or not config.USE_REMOTE_EMBEDDINGS:
        return None
    if "embedder" in _cache:
        return _cache["embedder"]

    from agno.knowledge.embedder.openai import OpenAIEmbedder

    params: dict[str, Any] = {"id": config.EMBED_MODEL, "api_key": config.OPENAI_API_KEY}
    if config.OPENAI_BASE_URL:
        params["base_url"] = config.OPENAI_BASE_URL
    embedder = OpenAIEmbedder(**params)
    _cache["embedder"] = embedder
    return embedder


def embed(text: str) -> list[float]:
    """Tek giriş noktası: gerçek embedder varsa onu, yoksa mock'u kullanır.

    Gerçek çağrı hata verirse sessizce mock'a düşer — video çekiminde ağ hatası
    sunumu bitirmesin (CLAUDE.md kural 2).
    """
    embedder = get_embedder()
    if embedder is None:
        return mock_embed(text)
    try:
        result = embedder.get_embedding(text)
        return list(result) if result else mock_embed(text)
    except Exception:
        return mock_embed(text)


def registry_summary() -> dict:
    """Sunum/UI için: hangi görev hangi modele düşüyor."""
    return {
        "mode": config.LLM_MODE,
        "provider": config.PROVIDER_LABEL,
        "tiers": {name: mid for name, mid in _TIERS.items()},
        "embedder": config.EMBED_MODEL if config.USE_REMOTE_EMBEDDINGS else "mock-hashing-512",
    }
