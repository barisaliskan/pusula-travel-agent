# İLERLEME TAKİBİ — Pusula AI Seyahat Asistanı

> **Bu dosyanın amacı:** Başka bir oturumda kaldığın yerden devam edebilmek. Yeni oturuma başlarken
> **önce bu dosyayı**, sonra `CASE.md` ve `PLAN.md`'yi oku.

**Son güncelleme:** 25 Temmuz 2026
**Teslim:** 30 Temmuz 2026 — Sunum (PDF) + Video
**Durum:** 🟡 İskelet kuruldu, Agno API doğrulandı. **Kod yazımı henüz başlamadı.**

---

## ✅ Tamamlanan (25 Temmuz)

- [x] Proje klasörü + `.venv` (Python 3.12.3)
- [x] Bağımlılıklar kuruldu ve **Agno 2.8.2 API'si kurulu sürümde tek tek doğrulandı** → `docs/agno-api-dogrulama.md`
  - `Team` / `Agent` / `TeamMode` (coordinate·route·broadcast·tasks) ✓
  - `MemoryManager` + `UserMemory` — **`delete_user_memory` / `clear_user_memories` var** (KVKK silme hakkı hazır) ✓
  - `RedisDb` (oturum) · `RedisVectorDb` (vektör) · `InMemoryDb` + `SqliteDb` (fallback) ✓
  - `PIIDetectionGuardrail` · `PromptInjectionGuardrail` · `OpenAIModerationGuardrail` · `BaseGuardrail` ✓
  - `OpenAILike` (GitHub Models `base_url` için) · `OpenAIEmbedder` · `AgentOS` · `@tool` ✓
- [x] `CASE.md` — orijinal brief birebir kaydedildi
- [x] `PLAN.md` — mimari + 7 çıktının tasarımı + yol haritası + risk tablosu
- [x] `CLAUDE.md` — proje kuralları (yeni oturum için)
- [x] `requirements.txt` · `.env.example` · `docker-compose.yml` · `.gitignore`

---

## 🔜 SIRADAKİ ADIM — buradan başla

**26 Temmuz: Çekirdek modüller.** Sırayla:

1. `app/config.py` — `../pegasuschatbot/app/config.py`'yi uyarla (bağımsız `.env` yükleyici + mod tespiti;
   `base_url` desteği zaten var). Yeni TTL tablosu (`PLAN.md` §5) eklenecek.
2. `app/models.py` — model registry: `leader` / `planner` / `specialist` / `classifier` / `embedder`.
   Anahtar yoksa mock moduna düşer.
3. `app/schemas.py` — Pydantic: `TravelerProfile`, `Itinerary` (+`ItineraryDay`, `ItinerarySlot`),
   `ChatRequest/Response`, `ConsentState`, `Source`.
4. `app/cache.py` — `../pegasuschatbot/app/cache.py`'yi devral (Redis/memory fallback + `cosine` +
   semantic cache). **Ekle:** single-flight kilidi, stale-while-revalidate.
5. `app/knowledge.py` — `Knowledge` + `RedisVectorDb` + embedder; Redis yoksa in-memory vektör fallback.
6. `app/guardrails.py` — Agno hazır guardrail'leri + custom `GroundednessGuardrail` (post-hook) +
   yüksek-risk feragat enjeksiyonu. Pegasus regex'leri ikinci savunma hattı olarak korunur.
7. `app/kvkk.py` — rıza defteri, veri envanteri, dışa aktarma, `clear_user_memories` ile silme.
8. `app/preferences.py` — profil şeması, skorlama formülü (`PLAN.md` §3), sert filtreler, geri bildirim.
9. `knowledge/*.json` — küratörlü örnek veri: ~12 destinasyon (kültür rehberi + POI + mutfak),
   SSS (13 kategori), vize matrisi, pratik bilgiler.
10. `docker-compose.yml` doğrula — `redis/redis-stack` (vektör arama için RediSearch gerekli).

Sonra **27 Temmuz:** 9 uzman ajan + `app/team.py` lider ajan (iki yol) + `app/tools/` + `app/planner.py`.

---

## 🧭 Alınan Kararlar (değiştirmeden önce gerekçesini oku)

| Karar | Gerekçe |
|---|---|
| **Agno `Team` ile leader-agent mimarisi** | Kullanıcı isteği. Brief "n8n/Langflow/Dify'dan ilham alınabilir" diyor; üretim-sınıfı framework bir kademe üstü. |
| **İki yollu akış** (`route` hızlı / `coordinate` yavaş) | Coordinate mode her istekte çok LLM çağrısı = case'in "düşük gecikme" gereksinimiyle çelişir. Karmaşıklığa göre yol seçimi çözüyor. |
| **Redis üç iş yapıyor** (oturum + vektör + cache) | Case açıkça "cache (Redis vb.) ve veri yönetimi planı" istiyor. Tek altyapı, üç görev = güçlü anlatı. |
| **GitHub Models** (`base_url=https://models.github.ai/inference`, id'ler `openai/gpt-4o`) | Kullanıcı yeni token alacak. `OpenAILike` ile; gerçek OpenAI'ya geçiş tek satır. |
| **Mock/in-memory fallback zorunlu** | Teslim video kaydı; canlı hata riski sıfırlanmalı. Ayrıca iki modun da yeşil olması regresyon yakalıyor. |
| **Ürün adı: Pusula AI** | Brief şirketi anonimleştirmiş ("bir seyahat teknolojileri şirketi") → kendi markamızı koyabiliyoruz. |
| **Slaytlar offline tek-dosya HTML → Ctrl+P → PDF** | Pegasus'ta çalıştı: tam tasarım kontrolü, video için pürüzsüz gezinme. |
| **Torch/sentence-transformers YOK** | `/home` %91 dolu (8.6 GB boş). Embedding API'den veya mock hashing ile. |

---

## 📋 Teslim Paketi Durumu

| Çıktı | Dosya | Durum |
|---|---|---|
| Tasarım dokümanı | `PLAN.md` | 🟡 iskelet hazır, sunum-hazır proza genişletilecek |
| Çalışan demo | `app/` + `web/` | ⬜ başlamadı |
| n8n görsel akış | `n8n/*.json` | ⬜ başlamadı |
| **Sunum (asıl teslim)** | `slides/index.html` → PDF | ⬜ başlamadı |
| Video konuşma metni | `SUNUM-METNI.md` | ⬜ başlamadı |
| Öğrenme rehberi | `docs/proje-rehberi.html` | ⬜ başlamadı |
| Agno API referansı | `docs/agno-api-dogrulama.md` | ✅ bitti |

---

## 📝 Notlar / Açık Sorular

- **GitHub Models token'ı `.env`'de henüz yok.** `.env.example`'ı `.env` olarak kopyalayıp token yapıştırılacak.
  Token olmadan da her şey mock modda çalışır (kasıtlı).
- `redisvl` kurulumu `redis` paketini 8.0.1 → 7.4.1'e düşürdü. Sorun yaşanmadı.
- Gerçek seyahat API'leri (Amadeus, Google Places vb.) yok → deterministik mock adapter'lar yazılacak;
  tool imzaları gerçek API'ye geçişe uygun soyutlanacak. Sunumda açıkça belirtilecek.
- KVKK madde numaraları (m.6 özel nitelikli veri, m.9 yurt dışına aktarım) slayt yazımında bir kez daha teyit edilecek.
- Vektör arama için Docker'da `redis/redis-stack` gerekiyor (düz `redis` imajında RediSearch yok).
