"""Mimari inşa edilebiliyor mu? (API çağrısı yapmadan — sadece kurulum)"""
from agno.agent import Agent
from agno.team import Team
from agno.team.team import TeamMode
from agno.models.openai import OpenAILike
from agno.db.in_memory import InMemoryDb
from agno.memory import MemoryManager, UserMemory
from agno.guardrails import PIIDetectionGuardrail, PromptInjectionGuardrail
from pydantic import BaseModel, Field

# --- yapılandırılmış çıktı şeması (itinerary_architect için) ---
class ItinerarySlot(BaseModel):
    time: str
    title: str
    note: str | None = None

class ItineraryDay(BaseModel):
    day: int
    theme: str
    slots: list[ItinerarySlot]

class Itinerary(BaseModel):
    city: str
    days: list[ItineraryDay]
    total_cost_estimate: float = Field(description="TRY")

db = InMemoryDb()
mm = MemoryManager(db=db)
model = OpenAILike(id="openai/gpt-4o-mini", api_key="dummy",
                   base_url="https://models.github.ai/inference")

# --- uzman ajanlar ---
culture = Agent(name="culture_curator", role="Kültürel bilgi ve görgü kuralları uzmanı",
                model=model, db=db, instructions="Yalnızca küratörlü rehberden yanıtla.")
architect = Agent(name="itinerary_architect", role="Günlük gezi planı üretir",
                  model=model, db=db, output_schema=Itinerary)
practical = Agent(name="practical_desk", role="Hava durumu, saat farkı, döviz",
                  model=model, db=db)
members = [culture, architect, practical]

# --- iki yol, aynı member listesi ---
fast = Team(name="Pusula — Hızlı Yol", members=members, model=model, db=db,
            mode=TeamMode.route, instructions="Tek uzmana yönlendir.")
slow = Team(name="Pusula — Yavaş Yol", members=members, model=model, db=db,
            mode=TeamMode.coordinate,
            instructions="İsteği böl, uzmanlara delege et, sentezle.",
            memory_manager=mm, enable_user_memories=True,
            add_history_to_context=True, num_history_runs=5,
            show_members_responses=True, markdown=True,
            pre_hooks=[PromptInjectionGuardrail(), PIIDetectionGuardrail(mask_pii=True)])

print(f"OK  hızlı yol: mode={fast.mode!r}  üye={len(fast.members)}")
print(f"OK  yavaş yol: mode={slow.mode!r}  üye={len(slow.members)}  memories={slow.enable_user_memories}")
print(f"OK  pre_hooks: {[type(h).__name__ for h in slow.pre_hooks]}")
print(f"OK  output_schema: {architect.output_schema.__name__}")

# --- KVKK: tercih yaz / oku / sil ---
uid = "gezgin_7f3a"
mm.add_user_memory(memory=UserMemory(memory="Vejetaryen, kalabalık sevmiyor",
                                     topics=["diyet", "tempo"]), user_id=uid)
got = mm.get_user_memories(user_id=uid)
print(f"OK  tercih yazıldı/okundu: {len(got)} kayıt -> {got[0].memory!r}")
mm.clear_user_memories(user_id=uid)
print(f"OK  KVKK silme: kalan {len(mm.get_user_memories(user_id=uid))} kayıt")
print("\n*** MİMARİ İNŞA EDİLEBİLİYOR — engel yok ***")
