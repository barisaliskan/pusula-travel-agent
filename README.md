# 🧭 Pusula AI — Seyahat Asistanı

Yapay zekâ destekli, uçtan uca seyahat planlama asistanı: **lider ajan + 9 uzman ajan**
mimarisi, kademeli güven hiyerarşisine dayalı veri stratejisi, Redis cache/oturum katmanı,
KVKK panelı ve çalışan bir web demosu.

> **AI Designer case study.** Bu depo, bir seyahat teknolojileri şirketi için hazırlanan
> AI Travel Assistant tasarımının hem **dokümantasyonunu** hem de tasarımı doğrulayan
> **çalışan referans uygulamasını** içerir. Orijinal brief: [`CASE.md`](CASE.md).

---

## Öne çıkanlar

| | |
|---|---|
| 🧠 **Lider ajan mimarisi** | [Agno](https://github.com/agno-agi/agno) `Team` üzerinde 1 lider + 9 alan uzmanı, kural tabanlı sınıflandırıcıyla **iki yollu akış** (hızlı yol / tam delegasyon) |
| 🔒 **Sıfır halüsinasyon politikası** | Fiyat, saat, vize kuralı ve mekân **asla** modelden gelmez — küratörlü bilgi tabanı veya araç çıktısıdır; çıkış guardrail'i groundedness denetler |
| 🛂 **Yüksek riskli alan ayrımı** | Vize/pasaport/sağlık yanıtları yalnızca T1 (resmî) kaynak + atıf + geçerlilik tarihi + zorunlu feragat ile üretilir |
| 🔌 **Harici servis olmadan çalışır** | LLM anahtarı yoksa kural tabanlı mock, Redis yoksa in-memory fallback. Anahtar/Redis gelince otomatik gerçek moda geçer |
| ✅ **Regresyonla doğrulanmış** | 40 birim testi + 32 uçtan uca senaryo, **hem mock hem gerçek LLM** modunda geçiyor |

---

## Mimari

```
Kullanıcı ─► FastAPI ─► Guardrail (giriş)
                            │
                            ▼
                   Karmaşıklık sınıflandırıcı
                       │             │
              hızlı yol │             │ tam delegasyon
                       ▼             ▼
              tek uzman        Lider Ajan (Team)
                       │             │  delege eder ▼
                       │      ┌──────┴───────────────────────┐
                       │      │ Destinasyon Kâşifi           │
                       │      │ Plan Mimarı                  │
                       │      │ Lojistik Uzmanı              │
                       │      │ Gastronomi Rehberi           │
                       │      │ Kültür Küratörü              │
                       │      │ Pratik Bilgi Masası          │
                       │      │ Belge Sorumlusu   (T1 kilit) │
                       │      │ SSS Uzmanı                   │
                       │      │ Tercih Yöneticisi            │
                       │      └──────┬───────────────────────┘
                       │             │  19 araç adapter'ı
                       └──────┬──────┘  (canlı · envanter · içerik · belge)
                              ▼
                    Guardrail (çıkış) + atıf/feragat
                              ▼
                     Redis: oturum · semantic cache · TTL
```

Ayrıntılı tasarım kararları, kademeli veri stratejisi, Redis anahtar şeması ve
KVKK katmanı için: **[`PLAN.md`](PLAN.md)**.
Aynı mimarinin n8n görsel karşılığı (ekran görüntüleri + düğüm açıklamaları):
**[`n8n/README.md`](n8n/README.md)**.

---

## Hızlı başlangıç

```bash
git clone https://github.com/barisaliskan/pusula-travel-agent.git
cd pusula-travel-agent

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env      # anahtar boş bırakılabilir → mock mod
docker compose up -d      # Redis (opsiyonel, yoksa in-memory'ye düşer)

.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

- Demo arayüzü → <http://localhost:8000>
- Sunum slaytları → <http://localhost:8000/slides>

### Testler

```bash
.venv/bin/python tests/test_agent.py       # 40 birim testi
.venv/bin/python tests/test_scenarios.py   # 32 uçtan uca senaryo
```

pytest gerekmez; her iki dosya da kendi koşucusunu barındırır.

### Çalışma modları

| | Anahtar yok | Anahtar var |
|---|---|---|
| **Redis yok** | kural tabanlı yanıt + in-memory cache | LLM + in-memory cache |
| **Redis var** | kural tabanlı yanıt + Redis cache/oturum | tam mod (vektör arama dahil) |

Varsayılan LLM sağlayıcısı **GitHub Models** (ücretsiz, OpenAI-uyumlu).
Gerçek OpenAI'ya geçiş için `.env` içindeki `OPENAI_BASE_URL` satırını boşaltmak yeterli.

---

## API

| Uç nokta | Açıklama |
|---|---|
| `POST /api/chat` | Sohbet (tek yanıt) |
| `POST /api/chat/stream` | SSE ile akış + canlı ajan izi |
| `GET·POST /api/preferences` | Tercih yönetimi |
| `POST /api/preferences/signal` | Örtük tercih sinyali kaydı |
| `GET /api/preferences/explain` | Önerinin gerekçesi ("neden bu?") |
| `GET·DELETE /api/kvkk/me` | Kişisel veriyi görüntüle / sil |
| `POST /api/kvkk/consent` | Kişiselleştirme rızası |
| `GET /api/kvkk/audit` | Denetim izi |
| `GET /api/architecture` | Mimari grafiği (arayüzün mimari sekmesi bunu okur) |
| `GET /api/scenarios` | 32 senaryonun tek doğruluk kaynağı |
| `GET /api/knowledge/search` | Bilgi tabanı araması |
| `GET /api/session/{id}` | Oturum durumu |
| `GET /api/health` | Mod tespiti (LLM / Redis durumu) |

---

## Depo yapısı

```
app/            FastAPI uygulaması, ajanlar, planlayıcı, guardrail, KVKK
  tools/        19 araç adapter'ı — canlı · envanter · içerik · belge
knowledge/      küratörlü bilgi tabanı (destinasyon, vize, POI, mutfak, kültür, SSS)
web/            sıfır bağımlılıklı demo arayüzü (CDN yok)
slides/         sunum slaytları (tarayıcıdan PDF'e basılır)
n8n/            görsel iş akışları — ana akış + veri hattı (ekran görüntüleriyle)
tests/          birim testleri + senaryo regresyon seti
docs/           kurulu Agno sürümünde doğrulanmış API notları
PLAN.md         mimari ve tasarım kararları
CASE.md         orijinal brief
SUNUM-METNI.md  sunum konuşma metni
```

---

## Notlar

- **Simülasyon şeffaflığı:** canlı API'si olmayan alanlarda (uçuş/otel envanteri) veriler
  deterministik olarak üretilir ve her kayıt `_simule` + `_kaynak` künyesi taşır.
  Arayüzde de bu şekilde işaretlenir — gerçekmiş gibi sunulmaz.
- **Uydurma işletme adı üretilmez.** Mekân önerileri yer tipi + semt yuvası döner.
- Dokümanlar ve arayüz Türkçe; kod ve değişken adları İngilizce.

## İlgili çalışma

[barisaliskan/pegasus-ai-agent](https://github.com/barisaliskan/pegasus-ai-agent) — aynı serinin
ilk case study'si (havayolu müşteri destek agent'ı). Cache ve guardrail katmanları oradan devralındı.
