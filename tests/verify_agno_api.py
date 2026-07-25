import importlib, inspect
def t(label, fn):
    try:
        r = fn()
        print(f"OK   {label}" + (f"  -> {r}" if r else ""))
    except Exception as e:
        print(f"FAIL {label}  :: {type(e).__name__}: {e}")

import agno
print("agno", getattr(agno, "__version__", "?"))
print("-" * 60)

t("agno.agent.Agent",            lambda: __import__("agno.agent", fromlist=["Agent"]).Agent and "")
t("agno.team.Team",              lambda: __import__("agno.team", fromlist=["Team"]).Team and "")
t("agno.team.team.Team",         lambda: __import__("agno.team.team", fromlist=["Team"]).Team and "")
t("agno.models.openai.OpenAIChat", lambda: __import__("agno.models.openai", fromlist=["OpenAIChat"]).OpenAIChat and "")
t("agno.models.openai.OpenAILike", lambda: __import__("agno.models.openai", fromlist=["OpenAILike"]).OpenAILike and "")
t("agno.models.openai.OpenAIResponses", lambda: __import__("agno.models.openai", fromlist=["OpenAIResponses"]).OpenAIResponses and "")
t("agno.db.sqlite.SqliteDb",     lambda: __import__("agno.db.sqlite", fromlist=["SqliteDb"]).SqliteDb and "")
t("agno.db.redis.RedisDb",       lambda: __import__("agno.db.redis", fromlist=["RedisDb"]).RedisDb and "")
t("agno.db.in_memory",           lambda: str(__import__("agno.db.in_memory", fromlist=["x"]).__all__ if hasattr(__import__("agno.db.in_memory", fromlist=["x"]),"__all__") else "module ok"))
t("agno.memory.MemoryManager",   lambda: __import__("agno.memory", fromlist=["MemoryManager"]).MemoryManager and "")
t("agno.memory.UserMemory",      lambda: __import__("agno.memory", fromlist=["UserMemory"]).UserMemory and "")
t("agno.knowledge.knowledge.Knowledge", lambda: __import__("agno.knowledge.knowledge", fromlist=["Knowledge"]).Knowledge and "")
t("agno.knowledge.embedder.openai.OpenAIEmbedder", lambda: __import__("agno.knowledge.embedder.openai", fromlist=["OpenAIEmbedder"]).OpenAIEmbedder and "")
t("agno.vectordb.lancedb",       lambda: __import__("agno.vectordb.lancedb", fromlist=["LanceDb"]).LanceDb and "")
t("agno.vectordb.redis",         lambda: str(dir(__import__("agno.vectordb.redis", fromlist=["x"])))[:80])
t("agno.vectordb.pgvector",      lambda: __import__("agno.vectordb.pgvector", fromlist=["PgVector"]).PgVector and "")
t("agno.tools.tool decorator",   lambda: __import__("agno.tools", fromlist=["tool"]).tool and "")
t("agno.os.AgentOS",             lambda: __import__("agno.os", fromlist=["AgentOS"]).AgentOS and "")
print("-" * 60)
# guardrails
for mod, names in [("agno.guardrails", ["PIIDetectionGuardrail","PromptInjectionGuardrail","OpenAIModerationGuardrail","BaseGuardrail"]),
                   ("agno.exceptions", ["InputCheckError","OutputCheckError"])]:
    try:
        m = importlib.import_module(mod)
        have = [n for n in names if hasattr(m, n)]
        miss = [n for n in names if not hasattr(m, n)]
        print(f"OK   {mod}: {have}" + (f"   MISSING {miss}" if miss else ""))
    except Exception as e:
        print(f"FAIL {mod} :: {e}")
