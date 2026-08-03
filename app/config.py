"""Yapılandırma + mod tespiti.

Tasarım ilkesi (CLAUDE.md kural 2): Uygulama harici servis olmadan da çalışır.
- OPENAI_API_KEY yoksa  -> LLM 'mock' modda çalışır (kural tabanlı + kaynağa dayalı şablon)
- Redis erişilemezse    -> cache 'memory' + Agno 'InMemoryDb' fallback'ine düşer
Anahtar/Redis geldiğinde otomatik 'gerçek' moda geçilir. Her senaryo iki modda da geçmelidir.

TTL tablosu PLAN.md §5'teki Redis anahtar şemasıyla birebir eşleşir.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / ".data"  # denetim izi / sqlite fallback (git'e girmez)


def _load_dotenv(path: Path) -> None:
    """Bağımlılıksız minimal .env yükleyici (yalnızca zaten set edilmemiş anahtarlar).

    python-dotenv'e bilerek bağımlı değiliz: tek dosyalık davranış, sürpriz yok.
    Satır içi yorumlar (`KEY=value  # açıklama`) temizlenir — .env.example bu biçimde.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Tırnaklı değerde '#' geçebilir -> önce tırnağı soy, yorumu sonra kes.
        if val[:1] in ("'", '"') and val[:1] == val[-1:] and len(val) > 1:
            val = val[1:-1]
        elif "#" in val:
            val = val.split("#", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "evet")


# ─────────────────────────────────────────────────────────────────────
# LLM sağlayıcı — Çıktı 6 (OpenAI kullanım mimarisi)
# ─────────────────────────────────────────────────────────────────────
# base_url doluysa OpenAI-uyumlu sağlayıcı (GitHub Models) -> Agno'da OpenAILike.
# base_url boşsa gerçek OpenAI -> OpenAIChat. Geçiş tek satır, başka kod değişmez.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()

# Görev bazlı model katmanları (PLAN.md §6). Kod hiçbir yerde model id'si hardcode etmez.
MODEL_LEADER = os.getenv("OPENAI_MODEL_LEADER", "openai/gpt-4o").strip()
MODEL_PLANNER = os.getenv("OPENAI_MODEL_PLANNER", "openai/gpt-4o").strip()
MODEL_SPECIALIST = os.getenv("OPENAI_MODEL_SPECIALIST", "openai/gpt-4o-mini").strip()
MODEL_CLASSIFIER = os.getenv("OPENAI_MODEL_CLASSIFIER", "openai/gpt-4o-mini").strip()
EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "openai/text-embedding-3-small").strip()

GEN_TEMPERATURE = _float("GEN_TEMPERATURE", 0.2)  # düşük = deterministik/stabil
# Embedding'ler varsayılan olarak yerel/mock (ücretsiz sağlayıcı kotasını korur).
USE_REMOTE_EMBEDDINGS = _bool("USE_REMOTE_EMBEDDINGS", False)
MOCK_EMBED_DIM = 512  # hashing vectorizer boyutu; büyük boyut = daha az çakışma

LLM_MODE = "openai" if OPENAI_API_KEY else "mock"
PROVIDER_LABEL = (
    "GitHub Models" if (OPENAI_API_KEY and OPENAI_BASE_URL)
    else "OpenAI" if OPENAI_API_KEY
    else "Mock (anahtarsız)"
)

# ─────────────────────────────────────────────────────────────────────
# Redis — tek altyapı, üç iş: oturum + vektör + cache (PLAN.md §2.5)
# ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
USE_REDIS = os.getenv("USE_REDIS", "auto").strip().lower()  # auto | true | false

# ─────────────────────────────────────────────────────────────────────
# Semantic cache — cache HIT = 0 LLM çağrısı
# ─────────────────────────────────────────────────────────────────────
SEMANTIC_THRESHOLD = _float("SEMANTIC_THRESHOLD", 0.92)
SEMANTIC_TTL = _int("SEMANTIC_TTL", 3600)
SEMANTIC_MAX_ENTRIES = 300

# ─────────────────────────────────────────────────────────────────────
# TTL politikası (saniye) — PLAN.md §5 tablosuyla birebir
# ─────────────────────────────────────────────────────────────────────
TTL_WEATHER = _int("TTL_WEATHER", 3600)            # wx:{qid}:{tarih}      1 sa
TTL_FX = _int("TTL_FX", 3600)                      # fx:{parite}          1 sa
TTL_FLIGHT_SEARCH = _int("TTL_FLIGHT_SEARCH", 900)   # fl:{o}:{d}:{tarih} 15 dk
TTL_HOTEL_SEARCH = _int("TTL_HOTEL_SEARCH", 1800)    # htl:{qid}:...      30 dk
TTL_POI = _int("TTL_POI", 86400)                   # poi:{qid}:{kat}     24 sa
TTL_VISA = _int("TTL_VISA", 86400)                 # visa:{from}:{to}    24 sa + olay-tabanlı
TTL_DEST_BRIEF = _int("TTL_DEST_BRIEF", 604800)    # dest:{qid}           7 gün
TTL_ITINERARY = _int("TTL_ITINERARY", 86400)       # itin:{session}:{v}  24 sa
TTL_EMBEDDING = _int("TTL_EMBEDDING", 604800)      # emb:{hash}           7 gün
TTL_IDEMPOTENCY = _int("TTL_IDEMPOTENCY", 86400)   # idem:{key}          24 sa
SESSION_TTL = _int("SESSION_TTL", 1800)            # agno:session:{id}   30 dk sliding
HISTORY_MAXLEN = _int("HISTORY_MAXLEN", 10)

# Dayanıklılık (PLAN.md §5): stampede koruması ve bayat-veri toleransı
SINGLE_FLIGHT_TTL = _int("SINGLE_FLIGHT_TTL", 10)   # kilidin azami ömrü
STALE_GRACE = _int("STALE_GRACE", 600)              # TTL sonrası bayat servis penceresi

# ─────────────────────────────────────────────────────────────────────
# Rate limit
# ─────────────────────────────────────────────────────────────────────
RATE_LIMIT_MAX = _int("RATE_LIMIT_MAX", 30)
RATE_LIMIT_WINDOW = _int("RATE_LIMIT_WINDOW", 60)

# ─────────────────────────────────────────────────────────────────────
# KVKK
# ─────────────────────────────────────────────────────────────────────
# Kişiselleştirme rızası olmadan profil YAZILMAZ (varsayılan kapalı = veri minimizasyonu).
DEFAULT_PERSONALIZATION_CONSENT = _bool("DEFAULT_PERSONALIZATION_CONSENT", False)
AUDIT_LOG_ENABLED = _bool("AUDIT_LOG_ENABLED", True)

# Yüksek riskli alanlar: yalnızca T1 kaynak + atıf + feragat (CLAUDE.md kural 4)
HIGH_RISK_TOPICS = ("vize", "pasaport", "saglik", "asi", "gumruk")


def runtime_summary() -> dict:
    """UI/loglar için tek bakışta mod özeti — 'hangi modda çalışıyoruz' sorusunun tek cevabı."""
    return {
        "llm_mode": LLM_MODE,
        "provider": PROVIDER_LABEL,
        "base_url": OPENAI_BASE_URL or "https://api.openai.com/v1",
        "models": {
            "leader": MODEL_LEADER,
            "planner": MODEL_PLANNER,
            "specialist": MODEL_SPECIALIST,
            "classifier": MODEL_CLASSIFIER,
            "embedder": EMBED_MODEL,
        },
        "remote_embeddings": USE_REMOTE_EMBEDDINGS,
        "redis_pref": USE_REDIS,
    }
