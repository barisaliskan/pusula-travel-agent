# İLERLEME TAKİBİ — Pusula AI Seyahat Asistanı

> **Bu dosyanın amacı:** Başka bir oturumda kaldığın yerden devam edebilmek. Yeni oturuma başlarken
> **önce bu dosyayı**, sonra `CASE.md` ve `PLAN.md`'yi oku.

**Son güncelleme:** 30 Temmuz 2026
**Teslim:** 30 Temmuz 2026 — Sunum (PDF) + Video
**Durum:** 🟢 **Sistem uçtan uca çalışıyor ve teslim paketi hazır.**
40/40 birim testi + **32/32 uçtan uca senaryo**, hem mock hem **gerçek GitHub Models** modunda.
Kalan tek iş: **slaytları PDF'e çevirmek ve videoyu kaydetmek** (`SUNUM-METNI.md` hazır).

**30 Tem — ikinci tur:** sistem gerçek bir kullanıcıya verildi ve **11 açık** çıktı
(konuşma hafızası yokluğu, Türkçe büyük harf hatası, alakasız SSS cevapları, kota
hatasının kullanıcıya sızması). Hepsi kapatıldı; 8 yeni senaryo (S19–S26) regresyon
setine eklendi.

**30 Tem — üçüncü tur:** ikinci bir kullanıcı oturumu daha incelendi, **7 açık daha**
bulundu (konuşma dili, plan reddi, öneri/künye karışması, bütçe filtresinin uçuşu
saymaması, küçük harfli yer adları). Hepsi kapatıldı; S27–S32 eklendi. Ayrıntı aşağıda.

---

## 🎬 Teslimden önce yapılacaklar (yalnızca bunlar kaldı)

```bash
cd /home/baris/Desktop/projects/pusula-travel-agent
docker compose up -d                                    # Redis
.venv/bin/python tests/test_agent.py                    # 40/40 bekleniyor
.venv/bin/python tests/test_scenarios.py                # 32/32 bekleniyor
.venv/bin/python -m uvicorn app.main:app --port 8000    # http://localhost:8000
```

1. **Slaytları PDF yap:** `localhost:8000/slides` → Ctrl+P → *Hedef: PDF olarak kaydet* →
   **Yatay (landscape)** · Kenar boşlukları **Yok** · **Arka plan grafikleri AÇIK**
   (kritik: kapatılırsa renkler ve kademe rozetleri kaybolur) → **24 sayfa** çıkmalı.
2. **Provayı bir kez yap:** `SUNUM-METNI.md` içindeki demo sırasını baştan sona dene
   (7 demo adımı, ~4 dk). Demo öncesi sağ panel → *Verilerim → Verilerimi sil*.
3. **Videoyu kaydet.** Hedef 15–18 dk. Ekran + ses.

---

## ✅ Tamamlanan (30 Temmuz) — ajan katmanı, API, arayüz, teslim paketi

- [x] `app/tools/` — **19 araç adapter'ı** (base · live · inventory · content · documents).
      Deterministik (`seeded` RNG), cache-aside + TTL'li, her kayıt `_simule` ve `_kaynak`
      künyesi taşıyor. **Uydurma işletme adı üretilmiyor** — yer tipi + semt yuvası dönüyor.
- [x] `app/planner.py` — plan kurucu + **doğrulayıcı** (9 denetim kodu).
      Dakika bütçeli gün doldurma, semt kümeleme, kapalı gün ayrıştırma, yağmur planı.
      **576 kombinasyon** (12 dest × 3 tempo × 4 uzunluk × 4 kurgu) hatasız.
- [x] `app/agents.py` — **10 uzman ajan** (9 alan uzmanı + karşılama). Her biri tek `AgentSpec`'ten hem Agno `Agent`
      nesnesi hem deterministik handler üretiyor. Kapsam kilidi `tiers` + `tools` ile.
- [x] `app/team.py` — lider ajan + **iki yollu akış** + kural tabanlı sınıflandırıcı +
      semantic cache kovalama + KVKK niyetleri + plan revizyonu + `architecture()`.
- [x] `app/scenarios.py` — **32 senaryo tek doğruluk kaynağı** (arayüz + test + slayt).
- [x] `app/main.py` — FastAPI: `/api/chat` · **`/api/chat/stream` (SSE)** · `/api/preferences`
      (+signal, +explain) · `/api/kvkk/me` (GET/DELETE) · `/api/kvkk/consent` · `/api/kvkk/audit`
      · `/api/architecture` · `/api/scenarios` · `/api/knowledge/search` · `/api/health`
- [x] `web/` — sohbet + **canlı akış izi** + trace rozetleri + tercih paneli + KVKK paneli
      + mimari sekmesi. Sıfır dış bağımlılık (CDN yok).
- [x] `tests/test_scenarios.py` — 32 senaryonun **yol / uzman / kademe / guardrail / feragat**
      sözleşmesini doğrular + cache, kişiselleştirme ve KVKK kanıtları.
- [x] `n8n/pusula-ana-akis.json` (19 node) + `n8n/pusula-veri-hatti.json` (11 node) —
      bağlantı bütünlüğü doğrulandı.
- [x] `slides/index.html` — **24 slayt**, offline tek dosya
      (son iki slayt n8n akışlarının ekran görüntüsü, PNG'ler HTML'e gömülü), Ctrl+P → A4 yatay PDF.
- [x] `SUNUM-METNI.md` — dakika dakika konuşma metni + 7 adımlı demo senaryosu +
      olası soruların cevapları.
- [x] `docs/agno-api-dogrulama.md` — 30 Tem doğrulamaları eklendi (`RedisDb.db_url`,
      `custom_patterns` sözlük şartı, `run()` imzaları).

### 🔴 Gerçek kullanıcı testinde çıkan açıklar ve kapatılışları (30 Tem, ikinci tur)

Sistem 18/18 geçerken **gerçek bir kullanıcıya** verildi. Altı mesajda dört kusur çıktı;
sonda genişletilince toplam **11 açık** bulundu. Ortak kök neden: her mesaj sıfırdan
yorumlanıyordu ve **eşleşme bulunamayınca emin bir tonda alakasız cevap** veriliyordu.

| # | Belirti | Kök neden | Düzeltme |
|---|---|---|---|
| 1 | "roma yerine **daha ucuza**" → 1. sırada yine Roma | Alternatif isteğinde eleme yok | `ctx.exclude` + `intent="ucuz"`; kâşif mevcut destinasyonu eler |
| 2 | "bu 3ünden **başka yok mu**" → iade politikası SSS'i | Takip sorusu kavramı yok | Oturum hafızası (`last_suggestions`) + `_ALTERNATIF_RE` |
| 3 | "**ne alaka** ondan bahsetmedim" → bahşiş SSS'i | Kullanıcı düzeltmesi tanınmıyor | `_DUZELTME_RE` → concierge özür + yeniden sorma |
| 4 | "merhaba", "naber" → acil durum numaraları | `_SELAM_RE` yazılmış ama **hiç kullanılmıyordu** | Sohbet niyetleri sınıflandırıcıya bağlandı |
| 5 | `ROMA'DA NE YENİR` → yanlış ajan | **`"YENİR".lower()` = `"yeni̇r"`** (i + birleşen nokta) | `app/text.py` · `tr_lower`/`fold` her yerde |
| 6 | "asdfgh" → "Rezervasyonunuz onaylandı mı?" | SSS'te alaka eşiği yok | `FAQ_MIN_SCORE=0.30` (ölçümle: alakalı 0,40–1,19 · çöp 0,05–0,10) |
| 7 | "peki orada ne yenir" → "hangi destinasyon?" | Bağlam devralınmıyor | `_apply_session_context`: dest/gün/ay/öneri devri |
| 8 | "Bali'ye gitmek istiyorum" → sezon SSS'i | Kapsam dışı yer söylenmiyor | `kb.unknown_place()` + concierge `kapsam_disi_yer` |
| 9 | "I want a 3 day plan for Rome" → destinasyon çözülmüyor | İngilizce ad yok | `DEST_ALIASES` (rome, barcelona, prague…) |
| 10 | "!!!???" / "12345678" → bağlamdan destinasyon anlatımı | Anlamsız girdi kontrolü yok | `anlamli_mi()` → concierge netleştirme |
| 11 | 🔴 **Kota hatası kullanıcıya yanıt olarak sunuluyordu** | Agno sağlayıcı hatasında **exception fırlatmaz**, hata metnini `content` döndürür | `_gecerli_uretim()` hata izi denetimi + **devre kesici** (3 hata → 120 sn şablon modu) |

**11. madde en kritiğiydi:** video çekiminde kota dolsaydı ekrana
_"Too many requests… GitHub Terms of Service"_ düşecekti ve kimse fark etmeyecekti.

**Eklenenler:** `app/text.py` (Türkçe normalizasyon) · 10. ajan `concierge`
(karşılama/kimlik/netleştirme/düzeltme/kapsam) · oturum hafızası · SSS alaka eşiği ·
destinasyon künyesi modu · yuva doğrulama (geçersiz tarih, kırpılan gün/kişi sayısı
artık kullanıcıya bildiriliyor).

### 🔴 İkinci kullanıcı oturumundan çıkan açıklar (30 Tem, üçüncü tur)

Konuşma yeterliliği eklendikten **sonra** yapılan oturum. Ortak tema: sistem soruyu
"duyuyor" ama **ilişkiyi** kuramıyordu.

| # | Belirti | Kök neden | Düzeltme |
|---|---|---|---|
| 1 | "peki **nerede kalıcaz**" → "anlayamadım" | Sözlük kitap diline göre yazılmış (`nerede kalayım`) | Konuşma dili anahtarları: `nerede kal`, `kalıcaz`, `kalacağız`, `yatacak` |
| 2 | "**başka bir plan** yap beğenmedim" → neredeyse aynı plan | Plan reddi kavramı yok | `_PLAN_ALT_RE` + `build_itinerary(variant=n)`: semt sırası ve öğünler döner |
| 3 | Her gün aynı yemek (Svíčková + Guláš) | `_meal_slot` hep `dishes[0]`/`[1]` seçiyordu | Öğün gün ve varyanta göre rotasyonlu |
| 4 | "bütçem 10 bin tl **destinasyon öner**" → Prag künyesi, 16.440 TRY | `_ONERI_RE` çıplak "öner"i kaçırıyor → oturumdan devralınan destinasyonun künyesi | Kalıp düzeltildi + `intent="oneri"` künye modunu kapatıyor |
| 5 | 🔴 Sert filtre **uçuşu saymıyordu** | Filtre `günlük × gece`, kart ise uçuş dahil toplam gösteriyordu → "bütçenize uygun" denip bütçe üstü rakam sunuluyordu | `trip_total()` tek formül; filtre ve kart aynı sayıyı kullanıyor |
| 6 | "**trabzon** hakkında bilgin varmı" → güvenlik SSS'i | Yer tespiti **büyük harf** varsayıyordu | Cümle yapısına dayalı tespit (`X hakkında`, `X'e gitmek`, `X'de N gün`) |
| 7 | "istanbuldan **nasıl giderim**" → alakasız | `gidebilirim` öneri kalıbındaydı; ulaşım anahtarı yoktu | `_ONERI_RE`'den çıkarıldı, lojistiğe `nasıl gider/ulaş` eklendi |

**5. madde en sinsisiydi:** sistem tutarlıydı ama **kendi içinde çelişiyordu** —
eleme bir formülle, gösterim başka bir formülle yapılıyordu.

### İlk turda yakalanan ve düzeltilen gerçek hatalar

| Hata | Belirti | Kök neden | Düzeltme |
|---|---|---|---|
| `ItineraryDay.date` **hiç tarih tutamıyordu** | Kapalı gün denetimi sessizce çalışmıyordu | Alan adı `date`, tipi de `date` → sınıf gövdesinde ad tipi gölgeledi, pydantic `Optional[None]` çözdü | `DateT = date` takma adı |
| Groundedness doğru sayıları "uydurma" sayıyordu | Her plan yanıtına uyarı ekleniyor, hiçbiri cache'lenmiyordu | Sayı örüntüsü **boşluğu binlik ayracı** kabul ediyor, `"24800.0 30330.0"` metnini `"0 303"` diye tek sayıya yapıştırıyordu | Ayraç yalnızca nokta (`\.\d{3}`) |
| Ondalık sayılar "saat" sanılıyordu | Skor `0.84`, kur `47.93` dayanaksız işaretleniyordu | `_TIME_RE` nokta ayraçlı saati de kabul ediyordu | Saat yalnızca iki nokta: `([01]?\d\|2[0-3]):[0-5]\d` |
| Agno guardrail'leri **sessizce devre dışıydı** | Log'da `AttributeError`, katman hiç kurulmuyordu | `PIIDetectionGuardrail(custom_patterns=...)` **liste değil sözlük** ister | Sözlüğe çevrildi + doküman |
| KVKK m.6 uyarısı en gerekli cümlede çalışmıyordu | "Veganım" / "Vejetaryenim" hassas veri sinyali vermiyordu | `\bvegan\b` — sondaki `\b` Türkçe çekim ekini kesiyor | Sonda sınır kaldırıldı |
| Yanlış sezon ve hava tahmini | "eylülde" ay olarak tanınmıyor, tarih bugüne düşüyordu | Aynı sondaki-`\b` sorunu | `\b{ay}` (önek eşleşmesi) |
| Kültür sorusuna pratik masası ortak oluyordu | Tek alanlı soru yavaş yola gidiyordu | Anahtar kelime **alt dize** taraması: `kur` (döviz) → "**kur**allar" | Kelime başı eşleşmesi + `kur` → `döviz kur`/`kuru` |
| Destinasyon sorusu plan sanılıyordu | "4 günlük kaçamak için nereye gitsem" plan mimarına gidiyordu | `\d+ günlük` kalıbı fazla hevesli | Öneri sorusu kalıbı (`nereye`/`gitsem`) plan isteğini iptal ediyor |
| Cache yanlış yanıt döndürüyordu | Destinasyon sorusuna plan yanıtı geldi (benzerlik 0,954) | Hashing embedder farklı niyetleri karıştırıyor | Cache kovası: `dil \| profil \| niyet \| destinasyon` |
| Araç şeması değişince `KeyError` | Redis'teki eski şekilli kayıt hayatta kaldı | Anahtar şema sürümü taşımıyordu | Anahtara `#v2` soneki |
| Plan kurucu kendi doğrulayıcısını geçemiyordu | 17/144 kombinasyonda `PACE_OVERLOAD` | Gün doldurma POI **sayısına** bakıyordu, süreye değil | Dakika bütçeli doldurma + `FULL_DAY_TRIP` istisnası |

---

## ✅ Tamamlanan (27 Temmuz) — çekirdek katman

- [x] `app/config.py` · `app/schemas.py` · `app/models.py` · `app/cache.py` · `app/knowledge.py`
      · `app/guardrails.py` · `app/kvkk.py` · `app/preferences.py`
- [x] `knowledge/*.json` — 128 KB küratörlü veri: 12 destinasyon · 76 POI · 12 mutfak
      · 12 kültür · 12 pratik · 12 vize satırı (T1) · 36 SSS / 13 kategori
- [x] `tests/test_agent.py` — 40 test, pytest'siz
- [x] Redis Stack + GitHub Models token'ı canlı doğrulandı
      (`output_schema` GitHub Models'ta çalışıyor — en büyük risk kapandı)

## ✅ Tamamlanan (25 Temmuz)

- [x] Proje iskeleti, `.venv`, bağımlılıklar, **Agno 2.8.2 API doğrulaması**
- [x] `CASE.md` · `PLAN.md` · `CLAUDE.md` · `requirements.txt` · `docker-compose.yml`

---

## 🧭 Alınan Kararlar (değiştirmeden önce gerekçesini oku)

| Karar | Gerekçe |
|---|---|
| **Agno `Team` ile leader-agent mimarisi** | Kullanıcı isteği. Brief "n8n/Langflow/Dify'dan ilham alınabilir" diyor; üretim-sınıfı framework bir kademe üstü. |
| **İki yollu akış** (hızlı / `coordinate` yavaş) | Coordinate mode her istekte çok LLM çağrısı = "düşük gecikme" gereksinimiyle çelişir. |
| ⭐ **Hızlı yolda `Team(mode="route")` KULLANILMIYOR** | Route modunda lider, yönlendirme kararı için de bir LLM çağrısı harcıyor → 2 çağrı. Kural tabanlı sınıflandırıcı hızlı yolu **1 çağrıya** indirdi (~1 ms maliyet). Route ekibi kurulu kalıyor, `/api/architecture`'da görünüyor. **PLAN.md §2.2'den bilinçli sapma.** |
| ⭐ **Sınıflandırıcı LLM kullanmıyor** | PLAN'da "ucuz model" öngörülmüştü; kurallar hem daha hızlı hem daha kararlı çıktı. |
| ⭐ **Olgu paketi deseni** | Uzman önce araçları çalıştırıp JSON olgu paketi üretir; LLM yalnızca onu dile çevirir. "Model olgu uydurmaz" böylece mimariden gelir, promptdan değil. Mock ve gerçek mod aynı olguları kullanır. |
| ⭐ **Semantic cache kovalama** | Kova = `dil \| profil parmak izi \| niyet \| destinasyon`. Eşiği yükseltmek gerçek tekrarları kaçırırdı; kovalama yalnızca yanlış eşleşmeyi keser. |
| ⭐ **Araç çıktı şeması sürümlü** (`#v2`) | Şema değişince önbellekteki eski kayıtlar otomatik geçersizleşir. Canlı olarak `KeyError` üretmişti. |
| **Redis üç iş yapıyor** (oturum + vektör + cache) | Case açıkça "cache (Redis vb.) ve veri yönetimi planı" istiyor. |
| **GitHub Models** (`base_url`, `openai/gpt-4o`) | `OpenAILike` ile; gerçek OpenAI'ya geçiş tek satır. Sunumda açıkça belirtiliyor. |
| **Restoran/otel adları üretilmiyor** | Uydurma işletme kaydı = CLAUDE.md kural 3 ihlali. Adapter, gerçek API'nin dolduracağı **yer tipi + semt** yuvasını döner. |
| **Retrieval kendi hibrit motorumuz** | Uzak embedding varsayılan kapalı → Agno vektör araması mock modda çalışamaz. Hibrit her iki modda aynı sonucu verir. |
| **Mock/in-memory fallback zorunlu** | Teslim video kaydı; canlı hata riski sıfırlanmalı. Ayrıca iki modun yeşil olması regresyon yakalıyor. |
| **Ürün adı: Pusula AI** | Brief şirketi anonimleştirmiş → kendi markamızı koyabiliyoruz. |
| **Slaytlar offline tek-dosya HTML → Ctrl+P → PDF** | Tam tasarım kontrolü, video için pürüzsüz gezinme. |
| **Torch/sentence-transformers YOK** | Disk kısıtı. Embedding API'den veya mock hashing ile. |

---

## 📋 Teslim Paketi Durumu

| Çıktı | Dosya | Durum |
|---|---|---|
| **Sunum (asıl teslim)** | `slides/index.html` → PDF | ✅ **24 slayt** · **PDF'e çevrilecek** |
| Video konuşma metni | `SUNUM-METNI.md` | ✅ bitti · **kayıt yapılacak** |
| Çalışan demo | `app/` + `web/` | ✅ uçtan uca çalışıyor |
| Tasarım dokümanı | `PLAN.md` | ✅ mimari + 7 çıktı (slaytlar bunu genişletiyor) |
| n8n görsel akış | `n8n/*.json` | ✅ 2 akış, 30 node |
| Agno API referansı | `docs/agno-api-dogrulama.md` | ✅ bitti |
| Öğrenme rehberi | `docs/proje-rehberi.html` | ⬜ **opsiyonel** — SUNUM-METNI.md bu ihtiyacı büyük ölçüde karşılıyor |

---

## 📊 Ölçülen sayılar (sunumda kullanılıyor — değişirse slaytları güncelle)

| Ölçüm | Değer |
|---|---|
| Guardrail engeli | 1 ms · 0 LLM çağrısı |
| Semantic cache HIT | **3 ms** · 0 LLM çağrısı (MISS 5.035 ms → **1.678×**) |
| Hızlı yol (gerçek LLM) | 1.390–4.175 ms · 1 çağrı |
| Yavaş yol / plan (gerçek LLM) | 8.086 ms · 3 çağrı |
| Mock mod, 32 senaryo ortalaması | **15 ms** |
| Test toplamı | 40 birim + 32 senaryo = **72** |
| Plan kombinasyonu | **576 / 576** geçerli (4 varyant dahil) |
| Bilgi tabanı | 172 belge · 12 destinasyon · 76 POI · 36 SSS |
| Ajan / araç | **10** / 19 |

---

## 📝 Notlar / Açık Sorular

- **GitHub Models token'ı `.env`'de**, son kullanma ~26 Ağustos 2026.
  ⚠️ Teslimden sonra iptal et: `github.com/settings/personal-access-tokens` → `pusula-ai-demo` → Delete.
  Token düşerse sistem sessizce mock moda geçer, çökmez (kasıtlı).
- **Plan revizyonu gerçek modda da şablon yanıt üretiyor** (`_revision` LLM'e uğramadan
  `_finish`'e gidiyor). Bilinçli: revizyon deterministik olsun. İstenirse `_llm_render`
  çağrısı eklenebilir — 3 satır.
- KVKK madde numaraları slaytlarda ihtiyatlı formüle edildi ("sayılabilir", "ifşa edebilir");
  hukuki görüş iddiası yok, ve slayt 16'da bu açıkça yazıyor.
- `redisvl` kurulumu `redis` paketini 8.0.1 → 7.4.1'e düşürdü. Sorun yaşanmadı.
- Vektör arama için Docker'da `redis/redis-stack` gerekiyor (düz `redis` imajında RediSearch yok).
