"""HTTP katmanı — FastAPI + SSE.

Servis **stateless**'tır: tüm durum Redis'te (oturum, profil, cache) tutulur, süreç
belleğinde değil. Bu, case'in "ölçeklenebilir yanıt mekanizması" gereksiniminin
karşılığıdır — istek herhangi bir kopyaya düşebilir.

Uç noktalar KVKK haklarını **çalışır** biçimde sunar: `GET /api/kvkk/me` (erişim +
taşınabilirlik), `DELETE /api/kvkk/me` (silme), `POST /api/kvkk/consent` (rızayı verme
ve geri alma). Slaytta "uyumluyuz" yazmak yerine uç noktayı canlı çağırıyoruz.

`POST /api/chat/stream` akışın her durağını (guardrail → cache → sınıflandırma → ajanlar
→ yanıt) canlı yayınlar. Videoda lider ajanın delegasyonu böyle görünür hâle gelir.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agents as A
from . import config, guardrails as g, kvkk, models
from . import preferences as pref
from . import team, tools as T
from .cache import cache
from .knowledge import kb
from .schemas import ChatRequest, ChatResponse, TravelerProfile

app = FastAPI(
    title="Pusula AI — Seyahat Asistanı",
    description="Agno lider-ajan mimarisi üzerine kurulu, kaynağa dayalı seyahat asistanı",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────
# Sohbet
# ─────────────────────────────────────────────────────────────────────
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Tek seferlik yanıt. Akışın tamamı `team.respond` içinde."""
    return team.respond(req)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Server-Sent Events: hattın her durağı ve ardından yanıt parça parça.

    `team.respond` senkron çalışır (araçlar ve Agno senkron); iş parçacığında çalıştırıp
    olayları thread-safe kuyruktan aktarıyoruz. Böylece FastAPI'nin olay döngüsü bloke
    olmaz ve arayüz akışı gerçek zamanlı görür.
    """
    olaylar: queue.SimpleQueue = queue.SimpleQueue()
    sonuc: dict[str, Any] = {}

    def calis() -> None:
        try:
            sonuc["yanit"] = team.respond(req, on_event=lambda a, v: olaylar.put((a, v)))
        except Exception as exc:  # akış hiçbir koşulda yarıda kalmasın
            sonuc["hata"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            olaylar.put(None)

    threading.Thread(target=calis, daemon=True).start()

    async def uret():
        while True:
            item = await asyncio.to_thread(olaylar.get)
            if item is None:
                break
            asama, veri = item
            yield f"event: {asama}\ndata: {json.dumps(veri, ensure_ascii=False)}\n\n"

        if "hata" in sonuc:
            yield f"event: error\ndata: {json.dumps({'mesaj': sonuc['hata']}, ensure_ascii=False)}\n\n"
            return

        yanit: ChatResponse = sonuc["yanit"]
        # Yanıtı parça parça gönder: yazılıyor etkisi + ilk token algısı
        metin = yanit.answer
        adim = 90
        for i in range(0, len(metin), adim):
            yield f"event: token\ndata: {json.dumps({'t': metin[i:i + adim]}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.012)
        yield ("event: done\ndata: " +
               json.dumps(yanit.model_dump(mode="json"), ensure_ascii=False) + "\n\n")

    return StreamingResponse(uret(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────────────
# Tercihler — Çıktı 7
# ─────────────────────────────────────────────────────────────────────
class PreferenceUpdate(BaseModel):
    user_id: str = "anon"
    updates: dict = Field(default_factory=dict)
    persona: Optional[str] = None


class SignalIn(BaseModel):
    user_id: str = "anon"
    kind: str = Field(description="saved | rejected | clicked | asked_again | booked")
    target: str


@app.get("/api/preferences")
def get_preferences(user_id: str = Query("anon")) -> dict:
    profil = pref.load(user_id)
    oneriler, elenenler = pref.recommend(profil, limit=5, user_id=user_id)
    return {
        "profil": profil.model_dump(mode="json"),
        "riza": kvkk.get_consent(user_id).model_dump(mode="json"),
        "formul": pref.formula(),
        "personalar": pref.PERSONAS,
        "oneriler": [s.model_dump(mode="json") for s in oneriler],
        "elenenler": elenenler,
        "sinyaller": pref.signal_scores(user_id),
    }


@app.post("/api/preferences")
def set_preferences(body: PreferenceUpdate) -> dict:
    profil = pref.load(body.user_id)
    if body.persona:
        profil = pref.apply_persona(profil, body.persona)
    if body.updates:
        profil = pref.merge(profil, body.updates)
    ok, mesaj = pref.save(body.user_id, profil)
    oneriler, elenenler = pref.recommend(profil, limit=5, user_id=body.user_id)
    return {
        "kaydedildi": ok, "mesaj": mesaj,
        "profil": profil.model_dump(mode="json"),
        "oneriler": [s.model_dump(mode="json") for s in oneriler],
        "elenenler": elenenler,
    }


@app.post("/api/preferences/signal")
def record_signal(body: SignalIn) -> dict:
    """Örtük sinyal: beğendi/reddetti. Rıza yoksa sessizce yok sayılır (veri minimizasyonu)."""
    pref.record_signal(body.user_id, body.kind, body.target)
    return {"kaydedildi": kvkk.can_write_profile(body.user_id),
            "sinyaller": pref.signal_scores(body.user_id)}


@app.get("/api/preferences/explain")
def explain(user_id: str = Query("anon"), key: Optional[str] = None) -> dict:
    """'Neden bu öneri?' — skor kırılımı."""
    profil = pref.load(user_id)
    oneriler, _ = pref.recommend(profil, limit=5, user_id=user_id)
    secim = next((s for s in oneriler if s.key == key), oneriler[0] if oneriler else None)
    if not secim:
        raise HTTPException(404, "Açıklanacak öneri bulunamadı.")
    return {"aciklama": pref.explain(secim), "skor": secim.score.model_dump(mode="json"),
            "formul": pref.formula()}


# ─────────────────────────────────────────────────────────────────────
# KVKK
# ─────────────────────────────────────────────────────────────────────
class ConsentUpdate(BaseModel):
    user_id: str = "anon"
    personalization: Optional[bool] = None
    sensitive_data: Optional[bool] = None
    marketing: Optional[bool] = None


@app.get("/api/kvkk/me")
def kvkk_me(user_id: str = Query("anon")) -> dict:
    """Erişim + taşınabilirlik hakkı: kullanıcının tüm verisi tek JSON'da."""
    return {"veri": kvkk.export_user_data(user_id),
            "uyum": kvkk.compliance_summary(user_id),
            "denetim_izi": kvkk.read_audit(user_id, limit=20)}


@app.delete("/api/kvkk/me")
def kvkk_delete(user_id: str = Query("anon"), session_id: Optional[str] = None) -> dict:
    """Silme hakkı: profil + rıza + cache + Agno hafızası + oturum, kalıntı doğrulamalı."""
    sonuc = kvkk.delete_all(user_id, memory_manager=team._agno.get("mm"),
                            session_ids=[session_id] if session_id else None)
    if session_id:
        cache.delete(team._ctx_key(session_id))
    return sonuc


@app.post("/api/kvkk/consent")
def kvkk_consent(body: ConsentUpdate) -> dict:
    state = kvkk.set_consent(body.user_id, personalization=body.personalization,
                             sensitive_data=body.sensitive_data, marketing=body.marketing)
    return {"riza": state.model_dump(mode="json"),
            "profil": pref.load(body.user_id).model_dump(mode="json")}


@app.get("/api/kvkk/audit")
def kvkk_audit(user_id: Optional[str] = None, limit: int = 50) -> dict:
    return {"kayitlar": kvkk.read_audit(user_id, limit),
            "not": "Kayıtlar ham kimlik değil hash taşır ve kişisel içerik barındırmaz."}


# ─────────────────────────────────────────────────────────────────────
# Mimari / sağlık / demo yardımcıları
# ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    return {
        "durum": "ayakta",
        "llm": {"mod": config.LLM_MODE, "saglayici": config.PROVIDER_LABEL,
                "gercek_llm": models.available()},
        "cache": cache.stats(),
        "bilgi_tabani": {"belge": kb.stats()["documents"], "destinasyon": len(kb.destinations)},
        "ajan_sayisi": len(A.SPECS),
        "arac_sayisi": len(T.all_tools()),
    }


@app.get("/api/architecture")
def architecture() -> dict:
    """Mimarinin tek kaynağı: arayüz, sunum ve n8n akışı hep buradan beslenir."""
    return team.architecture()


@app.get("/api/scenarios")
def scenarios() -> dict:
    """Arayüzdeki hızlı deneme düğmeleri ve uçtan uca doğrulama seti (Çıktı 2)."""
    from .scenarios import SCENARIOS

    return {"senaryolar": [s.as_dict() for s in SCENARIOS]}


@app.get("/api/knowledge/search")
def knowledge_search(q: str, k: int = 5, tier: Optional[str] = None) -> dict:
    hits = kb.search(q, k=k, tiers=[tier] if tier else None)
    return {"sorgu": q, "sonuclar": [
        {"id": h.doc.id, "baslik": h.doc.title, "koleksiyon": h.doc.collection,
         "kademe": h.doc.tier, "skor": h.score, "anahtar_kelime": h.keyword_score,
         "vektor": h.vector_score, "metin": h.doc.text[:300]}
        for h in hits]}


@app.get("/api/session/{session_id}")
def session_history(session_id: str) -> dict:
    return {"gecmis": cache.get_history(session_id),
            "baglam": team._load_session_ctx(session_id)}


# ─────────────────────────────────────────────────────────────────────
# Statik arayüz
# ─────────────────────────────────────────────────────────────────────
if config.WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(config.WEB_DIR)), name="web")


@app.get("/")
def index() -> Any:
    dosya = config.WEB_DIR / "index.html"
    if dosya.exists():
        return FileResponse(str(dosya))
    return JSONResponse({"mesaj": "Pusula AI çalışıyor. Arayüz için web/index.html gerekli.",
                         "saglik": "/api/health", "mimari": "/api/architecture"})


@app.get("/slides")
def slides() -> Any:
    dosya = config.BASE_DIR / "slides" / "index.html"
    if dosya.exists():
        return FileResponse(str(dosya))
    raise HTTPException(404, "Sunum dosyası henüz yok.")
