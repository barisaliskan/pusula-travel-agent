# Agno 2.8.2 — Doğrulanmış API Referansı

> **Bu dosya 25 Temmuz 2026'da kurulu sürüm üzerinde çalıştırılarak doğrulanmıştır.**
> Agno'nun online dokümantasyonu v1 ve v2 örneklerini karıştırıyor (örn. `agno.memory.v2` **eski**).
> Kod yazarken bu dosya esastır, docs.agno.com değil. Şüphe halinde `inspect.signature` ile tekrar doğrula.

Doğrulama scripti: `.venv/bin/python -c "..."` ile aşağıdaki importlar tek tek çalıştırıldı.

## ✅ Çalışan importlar

```python
from agno.agent import Agent
from agno.team import Team                    # agno.team.team.Team de çalışır
from agno.models.openai import OpenAIChat, OpenAILike, OpenAIResponses
from agno.db.redis import RedisDb             # sadece `redis` paketi yeterli
from agno.db.sqlite import SqliteDb           # `sqlalchemy` gerektirir
from agno.db.in_memory import InMemoryDb      # ⭐ sıfır bağımlılık fallback
from agno.memory import MemoryManager, UserMemory
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.vectordb.redis import RedisVectorDb, RedisDB, SearchType   # `redisvl` gerektirir
from agno.tools import tool                   # @tool dekoratörü
from agno.os import AgentOS
from agno.guardrails import (
    PIIDetectionGuardrail,
    PromptInjectionGuardrail,
    OpenAIModerationGuardrail,
    BaseGuardrail,
)
from agno.exceptions import InputCheckError, OutputCheckError
```

## ⚠️ Kurulmayan (ihtiyaç yok, ama bilinsin)

| Import | Gereken paket | Karar |
|---|---|---|
| `agno.vectordb.lancedb.LanceDb` | `lancedb` | Kurulmadı — Redis vector kullanıyoruz (case Redis istiyor) |
| `agno.vectordb.pgvector.PgVector` | `sqlalchemy` + `pgvector` | Kurulmadı — prod alternatifi olarak slaytta anılır |

## `Team` constructor — doğrulanmış parametreler

Hepsi mevcut: `members` · `model` · `mode` · `name` · `role` · `instructions` · `db` · `knowledge`
· `enable_user_memories` · `memory_manager` · `add_history_to_context` · `num_history_runs`
· `show_members_responses` · `markdown` · `output_schema` · `pre_hooks` · `post_hooks`
· `session_id` · `user_id` · `tools` · `respond_directly` · `stream` · `telemetry` · `debug_mode`

## `Agent` constructor — doğrulanmış parametreler

Hepsi mevcut: `model` · `name` · `role` · `instructions` · `tools` · `db` · `knowledge`
· `search_knowledge` · `enable_user_memories` · `memory_manager` · `output_schema`
· `pre_hooks` · `post_hooks` · `add_history_to_context` · `add_datetime_to_context`
· `markdown` · `session_id` · `user_id`

## `TeamMode` — 4 mod

`TeamMode` bir **str enum**'dır (`from agno.team.team import TeamMode`), değerleri:

| Mod | Davranış | Bizdeki kullanım |
|---|---|---|
| `coordinate` | Lider görevi böler, üyelere delege eder, sonuçları sentezler (varsayılan) | **Yavaş yol** — çok günlük plan, karma istekler |
| `route` | Lider router gibi davranır, isteği TEK uygun üyeye yönlendirir | **Hızlı yol** — tek alanlı sorular |
| `broadcast` | Aynı görev tüm üyelere gider | Kullanmıyoruz |
| `tasks` | Iteratif görev döngüsü (`max_iterations` ile) | Kullanmıyoruz |

`mode="coordinate"` gibi düz string de kabul edilir (str enum olduğu için).

## `MemoryManager` — doğrulanmış metotlar

Tercih yönetimi (Çıktı 7) ve KVKK için kritik olanlar:

```python
add_user_memory(memory=UserMemory(...), user_id=...)
get_user_memories(user_id=...)
get_user_memory(...)
search_user_memories(...)
replace_user_memory(...)
update_memories(...) / create_or_update_memories(...)

# ⭐ KVKK silme hakkı — framework seviyesinde hazır:
delete_user_memory(...)
clear_user_memories(...)
```

Async karşılıkları da var (FastAPI async yolu için): `aget_user_memories`, `acreate_user_memories`,
`aclear_user_memories`, `arun_memory_task`, `aoptimize_memories`.

Ayrıca `memory_capture_instructions` ile **neyin hatırlanacağı** yönlendirilebilir → KVKK veri
minimizasyonu buradan uygulanır (pasaport no vb. asla yakalanmaz).

## `BaseGuardrail` — custom guardrail yazımı

```python
from agno.guardrails import BaseGuardrail
from agno.exceptions import InputCheckError, OutputCheckError

class GroundednessGuardrail(BaseGuardrail):
    def check(self, run_input) -> None:        # senkron
        ...raise OutputCheckError("...")
    async def async_check(self, run_input) -> None:   # async
        ...
```

İki metot: `check` ve `async_check`. Agent/Team'e `pre_hooks=[...]` / `post_hooks=[...]` ile bağlanır.

## Model yapılandırması (GitHub Models)

```python
from agno.models.openai import OpenAILike

model = OpenAILike(
    id="openai/gpt-4o",                            # GitHub Models id biçimi
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://models.github.ai/inference",
)
```

`OpenAILike` → `OpenAIChat`'ten türer, tüm parametreleri destekler. Gerçek OpenAI'ya geçmek için
`OpenAIChat(id="gpt-4o")` yeterli; **başka kod değişmez**.

Doğrulanmış constructor parametreleri (kurulu sürümde `inspect.signature` ile):
`id` · `api_key` · `base_url` · `temperature` · `seed` · `max_tokens` · `timeout` · `max_retries`
· `default_headers` · `client_params` · `request_params` · `strict_output` · `top_p` · `stop`

### 🔑 Token: `models:read` izni ŞART

GitHub Models fine-grained PAT ister ve **Account permissions → Models → Read-only** verilmelidir.
Bu izin yoksa çağrı 401/403 döner. Repo erişimi gerekmez (`Public Repositories (read-only)` yeterli).

## Canlı doğrulama — 27 Temmuz 2026 (gerçek token ile çalıştırıldı)

| Ne | Sonuç |
|---|---|
| `openai/gpt-4o-mini` (classifier/specialist) | ✅ ~3 sn, 19 giriş / 5 çıkış token |
| `openai/gpt-4o` (leader) | ✅ ~2.6 sn |
| `openai/gpt-4o` (planner) | ✅ ~1.7 sn |
| **`Agent(output_schema=TravelerProfile)`** | ✅ **GitHub Models üzerinde çalışıyor** |
| `tests/test_agent.py` gerçek modda | ✅ 40/40 (mock moddakiyle aynı) |

**`output_schema` neden kritik:** `preference_keeper` (→ `TravelerProfile`) ve `itinerary_architect`
(→ `Itinerary`) buna dayanıyor. GitHub Models'ın structured output desteklememesi ihtimali
en büyük mimari riskti; doğrulandı ve **kapandı**.

Türkçe enum değerleri de doğru geldi — "Vejetaryenim, sakin tempo severim, orta bütçe, eşimle geziyorum"
girdisi `{budget_band: 'orta', pace: 'sakin', group: 'cift', dietary: ['vejetaryen']}` üretti.

## Canlı doğrulama — 30 Temmuz 2026 (ajan katmanı yazılırken)

Aşağıdakiler `inspect.signature` ile kurulu sürümde doğrulandı ve **ikisi gerçek hataya
yol açtığı için** buraya yazıldı.

### ⚠️ `RedisDb` parametresi `db_url` — `redis_url` DEĞİL

```python
from agno.db.redis import RedisDb
db = RedisDb(db_url=config.REDIS_URL, db_prefix="pusula", expire=config.SESSION_TTL)
```

Doğrulanmış parametreler: `id` · `redis_client` · **`db_url`** · `db_prefix` · `expire`
· `session_table` · `memory_table` · `metrics_table` · `eval_table` · `knowledge_table`
· `culture_table` · `traces_table` · `spans_table`

`InMemoryDb()` **hiç parametre almaz** (fallback yolu).

### ⚠️ `PIIDetectionGuardrail(custom_patterns=...)` SÖZLÜK ister

```python
PIIDetectionGuardrail(
    mask_pii=True,
    custom_patterns={"TC kimlik no": r"\b[1-9]\d{10}\b", "IBAN": r"..."},   # dict!
)
```

Liste verilirse Agno içeride `.items()` çağırır → `AttributeError` → **tüm guardrail
katmanı sessizce devre dışı kalır** (kurulum `try/except` içindeyse fark edilmez).
Bu hata canlı olarak yaşandı; artık `tests/` bu kurulumu doğruluyor.

Doğrulanmış parametreler: `mask_pii` · `enable_ssn_check` · `enable_credit_card_check`
· `enable_email_check` · `enable_phone_check` · `custom_patterns`
`PromptInjectionGuardrail` yalnızca `injection_patterns` alır.

### `Agent.run()` / `Team.run()` imzaları

İkisi de aynı ilk parametreleri alır:
`input` · `stream` · `stream_events` · `user_id` · `session_id` · `session_state`
· `run_context` · `run_id` · `audio` · `images` · `videos` · `files`
· `knowledge_filters` · `add_history_to_context` · `add_dependencies_to_context`

Dönen `RunOutput` nesnesinde işimize yarayanlar: `content` · `content_type` · `messages`
· `metrics` · `model` · `citations` · `events` · `reasoning_content`.

### `MemoryManager` constructor

`model` · `system_message` · **`memory_capture_instructions`** · `additional_instructions`
· `db` · `delete_memories` · `update_memories` · `add_memories` · `clear_memories`
· `debug_mode` · `id` · `name` · `owner_id` · `owner_type`

### Çalıştığı doğrulanan `Team` kurulumu

```python
Team(name=..., mode="coordinate", model=..., members=[...], instructions=[...],
     db=RedisDb(...), add_history_to_context=True, num_history_runs=3,
     enable_user_memories=False, memory_manager=MemoryManager(...),
     show_members_responses=True, markdown=True,
     pre_hooks=[PromptInjectionGuardrail(), PIIDetectionGuardrail(...)],
     telemetry=False)
```

18 senaryonun tamamı bu kurulumla gerçek GitHub Models üzerinde geçti (30 Tem).
Yavaş yol (`coordinate`) 3 LLM çağrısı / ~8,1 sn ölçüldü.

## Kurulu sürümler (doğrulandı)

```
agno==2.8.2   openai==2.48.0   fastapi==0.140.0   uvicorn==0.51.0
redis==7.4.1  redisvl==0.23.0  SQLAlchemy==2.0.51  pydantic==2.13.4  numpy==2.5.1
```

> Not: `redisvl` kurulumu `redis`'i 8.0.1 → 7.4.1'e düşürdü. Sorun yaşanmadı; `RedisDb` çalışıyor.
> Torch/sentence-transformers **kurulmadı** — disk kısıtı nedeniyle kaçınıldı (`/home` %91 dolu).
