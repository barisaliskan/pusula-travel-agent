# Pusula AI — Proje Talimatları

## Bu proje ne?

Barış'ın staj yerindeki **ikinci** AI Designer case study'si: yapay zekâ destekli **Seyahat Asistanı**.
Teslim **30 Temmuz 2026** — Sunum (PDF) + Video kaydı.

## Yeni bir oturuma başlarken bu sırayla oku

1. **`PROGRESS.md`** — nerede kaldık, sıradaki adım ne (her zaman önce bu)
2. **`CASE.md`** — staj yerinden gelen orijinal brief (7 çıktı + değerlendirme kriterleri). Kapsam tartışmasında bu metin esastır.
3. **`PLAN.md`** — mimari ve 7 çıktının tasarımı
4. **`docs/agno-api-dogrulama.md`** — kurulu Agno sürümünde **doğrulanmış** import yolları

## Değişmez kurallar

**1. Agno API'si için docs.agno.com'a değil `docs/agno-api-dogrulama.md`'ye güven.**
Online dokümantasyon v1 ve v2 örneklerini karıştırıyor (`agno.memory.v2` **eski**). Kurulu sürüm otoritedir.
Yeni bir Agno sınıfı/parametresi kullanacaksan önce `inspect.signature` ile doğrula, sonra o dosyaya ekle.

**2. Sistem harici servis olmadan da çalışmalı.**
LLM anahtarı yoksa → mock (kural tabanlı + kaynağa dayalı şablon). Redis yoksa → `InMemoryDb` + in-memory cache.
Anahtar/Redis gelince otomatik gerçek moda geçer. **Sebep:** teslim bir video kaydı; çekim sırasında kota/ağ
hatası sunumu bitirir. Her senaryo **iki modda da** geçmeli.

**3. Model asla olgu uydurmaz.**
Fiyat, saat, vize kuralı, mekân → ya küratörlü bilgi tabanından ya canlı API'den. LLM yalnızca dile çevirir.
Çıkış guardrail'i groundedness'i denetler.

**4. Vize/pasaport/sağlık = yüksek risk alanı.**
Yalnızca T1 (resmi/kamusal) kaynak + atıf + geçerlilik tarihi + zorunlu feragat. Kesin hukuki sonuç bildirilmez.

**5. Dokümanlar ve arayüz Türkçe.** Kod ve değişken adları İngilizce, yorumlar Türkçe (Pegasus projesindeki gibi).

## Çalıştırma

```bash
cd /home/baris/Desktop/projects/pusula-travel-agent
.venv/bin/python -m uvicorn app.main:app --reload --port 8000    # http://localhost:8000
.venv/bin/python tests/test_agent.py                              # pytest gerekmez
docker compose up -d                                              # Redis (opsiyonel)
```

Sunucuyu yeniden başlatmak için: `pkill -f "[u]vicorn app.main"` (regex hilesi kendi kabuğunu öldürmez).

## Referans proje

`../pegasuschatbot/` — birinci case study (Pegasus AI destek agent'ı). Şunlar buradan devralınıyor:
`cache.py` (Redis/memory fallback + semantic cache), `config.py` (bağımsız `.env` yükleyici + mod tespiti),
`guardrails.py` regex'leri, `web/` + `slides/index.html` yapısı, `tests/` deseni.
`orchestrator.py`, `llm.py`, `rag.py` ise **Agno ile değiştiriliyor** (Team / models / Knowledge).

## Kapsam disiplini

Teslim 5 günde. Öncelik sırası: **slaytlar + sunum metni > demo > n8n > UI cilası.**
Asıl teslim sunum ve video; demo farklılaştırıcı ama sıkışırsa kesilecek ilk kalem UI cilası ve n8n'dir.
