"""Önbellek / oturum katmanı — Çıktı 5'in kod karşılığı.

Redis erişilebilirse RedisBackend, değilse MemoryBackend (fallback). Üzerine kurulanlar:
semantic cache · cache-aside API önbelleği · oturum belleği · rate-limit · KVKK purge.

Pegasus projesinden devralınan çekirdek (backend soyutlaması + semantic cache) üzerine
bu projede eklenenler:
  * PLAN.md §5 anahtar şeması ve TTL politikası (`keys` yardımcıları)
  * **single-flight kilidi** — cache stampede koruması
  * **stale-while-revalidate** — TTL dolsa da bayat veriyi servis edip arkada tazele
  * `purge_user` — KVKK silme hakkı cache'i de kapsar
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable, Optional

from . import config


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def hash_user(user_id: str) -> str:
    """Anahtarlarda ham PII taşımayız (PLAN.md §5 KVKK kesişimi) -> kısa hash."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


class keys:
    """PLAN.md §5 anahtar şeması. Anahtar biçimini tek yerde tutmak,
    KVKK purge'ünün hangi desenleri sileceğini de tek yerde tanımlar."""

    @staticmethod
    def semantic(lang: str, digest: str) -> str:
        return f"sc:{lang}:{digest}"

    @staticmethod
    def dest_brief(qid: str) -> str:
        return f"dest:{qid}"

    @staticmethod
    def poi(qid: str, category: str) -> str:
        return f"poi:{qid}:{category}"

    @staticmethod
    def weather(qid: str, day: str) -> str:
        return f"wx:{qid}:{day}"

    @staticmethod
    def fx(pair: str) -> str:
        return f"fx:{pair}"

    @staticmethod
    def flight(origin: str, dest: str, day: str) -> str:
        return f"fl:{origin}:{dest}:{day}"

    @staticmethod
    def hotel(qid: str, checkin: str, checkout: str, pax: int) -> str:
        return f"htl:{qid}:{checkin}:{checkout}:{pax}"

    @staticmethod
    def visa(from_c: str, to_c: str) -> str:
        return f"visa:{from_c}:{to_c}"

    @staticmethod
    def itinerary(session_id: str, version: int) -> str:
        return f"itin:{session_id}:{version}"

    @staticmethod
    def profile(user_id: str) -> str:
        return f"prof:{hash_user(user_id)}"

    @staticmethod
    def consent(user_id: str) -> str:
        return f"consent:{hash_user(user_id)}"

    @staticmethod
    def session(session_id: str) -> str:
        return f"sess:{session_id}"

    @staticmethod
    def rate_limit(user_id: str, window: int) -> str:
        return f"rl:{hash_user(user_id)}:{window}"

    @staticmethod
    def embedding(text: str) -> str:
        return f"emb:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def idempotency(token: str) -> str:
        return f"idem:{token}"

    @staticmethod
    def lock(key: str) -> str:
        return f"lock:{key}"


# ─────────────────────────────────────────────────────────────────────
# Backend'ler
# ─────────────────────────────────────────────────────────────────────
class MemoryBackend:
    """Basit, thread-safe, TTL destekli in-memory KV. Redis yoksa devreye girer."""

    name = "memory"

    def __init__(self) -> None:
        self._kv: dict[str, tuple[str, Optional[float]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            item = self._kv.get(key)
            if not item:
                return None
            val, exp = item
            if exp is not None and exp < time.time():
                self._kv.pop(key, None)
                return None
            return val

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        with self._lock:
            self._kv[key] = (value, time.time() + ttl if ttl else None)

    def delete(self, key: str) -> None:
        with self._lock:
            self._kv.pop(key, None)

    def setnx(self, key: str, value: str, ttl: int) -> bool:
        """Atomik 'yoksa yaz' — single-flight kilidinin temeli."""
        with self._lock:
            item = self._kv.get(key)
            if item and (item[1] is None or item[1] > time.time()):
                return False
            self._kv[key] = (value, time.time() + ttl)
            return True

    def incr(self, key: str, ttl: int) -> int:
        with self._lock:
            item = self._kv.get(key)
            if item and (item[1] is None or item[1] > time.time()):
                n = int(item[0]) + 1
                self._kv[key] = (str(n), item[1])
                return n
            self._kv[key] = ("1", time.time() + ttl)
            return 1

    def scan(self, pattern: str) -> list[str]:
        """Sadece 'önek*' biçimini destekler — purge için yeterli."""
        prefix = pattern.rstrip("*")
        with self._lock:
            return [k for k in self._kv if k.startswith(prefix)]


class RedisBackend:
    """Redis KV. Bağlanamazsa __init__ hata fırlatır -> Cache fallback'e düşer."""

    name = "redis"

    def __init__(self, url: str) -> None:
        import redis  # opsiyonel bağımlılık

        self.r = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1
        )
        self.r.ping()  # erişilemezse exception -> fallback

    def get(self, key: str) -> Optional[str]:
        return self.r.get(key)

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        self.r.set(key, value, ex=ttl) if ttl else self.r.set(key, value)

    def delete(self, key: str) -> None:
        self.r.delete(key)

    def setnx(self, key: str, value: str, ttl: int) -> bool:
        return bool(self.r.set(key, value, nx=True, ex=ttl))

    def incr(self, key: str, ttl: int) -> int:
        n = self.r.incr(key)
        if n == 1:
            self.r.expire(key, ttl)
        return int(n)

    def scan(self, pattern: str) -> list[str]:
        return [k for k in self.r.scan_iter(match=pattern, count=500)]


# ─────────────────────────────────────────────────────────────────────
# Cache cephesi
# ─────────────────────────────────────────────────────────────────────
class Cache:
    def __init__(self) -> None:
        self.backend, self.backend_name = self._make_backend()

    def _make_backend(self):
        if config.USE_REDIS in ("auto", "true"):
            try:
                b = RedisBackend(config.REDIS_URL)
                return b, b.name
            except Exception:
                if config.USE_REDIS == "true":
                    # Açıkça istenmişse sessizce yutmayalım; yine de çalışmaya devam ederiz.
                    print("[cache] UYARI: Redis istendi ama erişilemedi -> in-memory fallback")
        b = MemoryBackend()
        return b, b.name

    # ---- generic JSON ----
    def get_json(self, key: str) -> Any:
        raw = self.backend.get(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set_json(self, key: str, obj: Any, ttl: Optional[int] = None) -> None:
        self.backend.set(key, json.dumps(obj, ensure_ascii=False, default=str), ttl)

    def delete(self, key: str) -> None:
        self.backend.delete(key)

    # ---- cache-aside + stale-while-revalidate + single-flight ----
    def fetch(
        self,
        key: str,
        producer: Callable[[], Any],
        ttl: int,
        *,
        allow_stale: bool = True,
    ) -> tuple[Any, str]:
        """Dayanıklı cache-aside. Dönen ikinci değer durum: hit | stale | miss | lock-stale.

        Değeri zarflayarak (`{v, exp}`) saklarız; böylece TTL dolduktan sonra da
        `STALE_GRACE` penceresinde bayat veriyi servis edebiliriz. Redis kaydının
        gerçek TTL'i `ttl + STALE_GRACE` olarak verilir, mantıksal tazelik `exp` alanında.

        Stampede koruması: aynı anahtar için ilk çağıran kilidi alır ve üretir; kilidi
        alamayan bayat veri varsa onu döner, yoksa üretimi kendisi yapar (kilit bekleyip
        istek zincirini uzatmaktansa nadiren çift üretim tercih edilir).
        """
        env = self.get_json(key)
        now = time.time()
        if env and isinstance(env, dict) and "exp" in env:
            if env["exp"] > now:
                return env["v"], "hit"
            stale_value = env.get("v")
        else:
            stale_value = None

        lock_key = keys.lock(key)
        got_lock = self.backend.setnx(lock_key, "1", config.SINGLE_FLIGHT_TTL)
        if not got_lock and stale_value is not None and allow_stale:
            # Başkası tazeliyor -> bayat veriyi hemen ver (gecikme > mutlak tazelik).
            return stale_value, "lock-stale"

        try:
            value = producer()
        except Exception:
            # Üretim başarısız: fallback zinciri -> bayat veri + uyarı (PLAN.md §5)
            if stale_value is not None and allow_stale:
                return stale_value, "stale"
            raise
        finally:
            if got_lock:
                self.backend.delete(lock_key)

        self.set_json(key, {"v": value, "exp": now + ttl}, ttl=ttl + config.STALE_GRACE)
        return value, "miss"

    # ---- oturum belleği (Agno RedisDb yoksa / hafif izleme için) ----
    def get_history(self, session_id: str) -> list[dict]:
        return self.get_json(keys.session(session_id)) or []

    def append_history(self, session_id: str, role: str, content: str) -> None:
        hist = self.get_history(session_id)
        hist.append({"role": role, "content": content, "ts": time.time()})
        hist = hist[-config.HISTORY_MAXLEN:]
        self.set_json(keys.session(session_id), hist, ttl=config.SESSION_TTL)

    # ---- semantic cache: HIT = 0 LLM çağrısı ----
    def semantic_lookup(self, embedding: list[float], lang: str = "tr") -> tuple[Optional[dict], float]:
        index = self.get_json(f"sc:{lang}:index") or []
        best, best_sim = None, 0.0
        for e in index:
            sim = cosine(embedding, e["emb"])
            if sim > best_sim:
                best, best_sim = e, sim
        if best and best_sim >= config.SEMANTIC_THRESHOLD:
            return best, round(best_sim, 4)
        return None, round(best_sim, 4)

    def semantic_store(
        self, embedding: list[float], question: str, payload: dict, lang: str = "tr"
    ) -> None:
        """Payload tam yanıt zarfıdır (cevap + kaynaklar) — HIT'te atıflar da geri gelsin."""
        index = self.get_json(f"sc:{lang}:index") or []
        index.append({"emb": embedding, "question": question, "payload": payload, "ts": time.time()})
        index = index[-config.SEMANTIC_MAX_ENTRIES:]
        self.set_json(f"sc:{lang}:index", index, ttl=config.SEMANTIC_TTL)

    def semantic_clear(self, lang: str = "tr") -> None:
        self.delete(f"sc:{lang}:index")

    # ---- rate limit ----
    def rate_limit_hit(self, user_id: str) -> tuple[bool, int]:
        """(izin_var_mı, mevcut_sayaç). Sabit pencere — demo için yeterli, prod'da sliding."""
        window = int(time.time() // config.RATE_LIMIT_WINDOW)
        n = self.backend.incr(keys.rate_limit(user_id, window), config.RATE_LIMIT_WINDOW)
        return n <= config.RATE_LIMIT_MAX, n

    # ---- KVKK: silme hakkı cache'i de kapsar ----
    def purge_user(self, user_id: str) -> list[str]:
        """Kullanıcıya ait tüm cache anahtarlarını siler, silinenleri döner (denetim izi için)."""
        removed: list[str] = []
        for key in (keys.profile(user_id), keys.consent(user_id)):
            if self.backend.get(key) is not None:
                self.backend.delete(key)
                removed.append(key)
        h = hash_user(user_id)
        for pattern in (f"rl:{h}:*", f"prof:{h}*", f"consent:{h}*"):
            for key in self.backend.scan(pattern):
                self.backend.delete(key)
                if key not in removed:
                    removed.append(key)
        return removed

    def stats(self) -> dict:
        return {"backend": self.backend_name, "semantic_threshold": config.SEMANTIC_THRESHOLD}


# Uygulama genelinde tek örnek
cache = Cache()
