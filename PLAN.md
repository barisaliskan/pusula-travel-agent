# Pusula AI — Seyahat Asistanı · Proje Planı ve Mimari

> **Pozisyon:** AI Designer (Staj Case Study #2) · **Hazırlayan:** Barış Alışkan
> **Teslim:** 30 Temmuz 2026 — Sunum (PDF) + Video Kaydı
> **Orijinal brief:** [`CASE.md`](CASE.md) · **Doğrulanmış Agno API:** [`docs/agno-api-dogrulama.md`](docs/agno-api-dogrulama.md)
> **Durum takibi:** [`PROGRESS.md`](PROGRESS.md) — yeni bir oturuma başlarken **önce onu oku**

---

## 0. Yönetici Özeti

Bir seyahat teknolojileri şirketi için, kullanıcının seyahat planlamasını uçtan uca destekleyen bir yapay zekâ asistanı tasarlıyorum. Çözümün çekirdeği **Agno framework'ü üzerinde kurulu bir lider-ajan (leader agent) mimarisidir**: bir lider ajan kullanıcının isteğini çözümler, işi 9 uzman ajana delege eder ve dönen sonuçları tek tutarlı yanıta sentezler.

**Ana tasarım ilkesi:** Model asla fiyat, saat, vize kuralı veya mekân uydurmaz. Her olgusal yanıt ya küratörlü bilgi tabanından ya canlı API'den gelir; LLM yalnızca bu veriyi kişiselleştirilmiş, akıcı dile çevirir.

**Üç teknik gereksinimin karşılanması:**

| Gereksinim | Nasıl karşılanıyor |
|---|---|
| ⚡ **Düşük gecikme** | Semantic cache (0 LLM çağrısı) + **karmaşıklığa göre iki yollu ajan akışı** + paralel tool çağrısı + streaming + prompt caching |
| 🔁 **Stabil / tutarlı / ölçeklenebilir** | Düşük temperature + `output_schema` (yapılandırılmış çıktı) + RAG grounding + çıkış guardrail + eval seti + stateless servis |
| 📡 **Güncel veri** | 5 kademeli kaynak hiyerarşisi + her veri tipine özel TTL + olay-tabanlı invalidation |
| 🔒 **Gizlilik / KVKK** | PII modele gitmeden maskelenir · rıza kademeleri · çalışan silme hakkı · denetim izi |

---

## 1. İş Problemi

### 1.1. Problem
Seyahat planlaması parçalı bir deneyimdir: kullanıcı destinasyon seçimi için bir siteye, uçuş için ikincisine, otel için üçüncüsüne, vize için resmi kuruma, "orada ne yenir" için bloglara gider. Bilgi dağınık, güvenilirliği belirsiz ve hiçbiri kullanıcıyı **tanımaz** — vejetaryen olduğunu, kalabalık sevmediğini, bütçesini her seferinde yeniden anlatır.

Aynı zamanda seyahat, hata maliyeti yüksek bir alandır: yanlış vize bilgisi uçağa binememek, yanlış fiyat güven kaybı demektir. Bu yüzden asistanın "akıcı konuşması" yetmez; **kaynağa dayalı** konuşması gerekir.

### 1.2. Başarı Metrikleri (KPI)
- **Containment:** İnsana devretmeden çözülen oturum oranı
- **İlk yanıt süresi:** cache HIT < 300 ms · tek alanlı soru < 1.5 sn · plan üretimi ilk token < 2 sn
- **Groundedness:** Olgusal ifadelerin kaynağa dayanma oranı (halüsinasyon ≈ 0)
- **Kişiselleştirme etkisi:** Profil sonrası öneri kabul oranındaki artış
- **KVKK:** Rıza dışı veri işleme = 0 · silme talebi karşılama süresi

### 1.3. Kullanıcı Personaları (cold-start arketipleri)
| Persona | Öncelikleri |
|---|---|
| **Bütçe Odaklı Gezgin** | En uygun fiyat, hostel, ücretsiz aktivite |
| **Kültür Avcısı** | Müze, tarih, yerel gelenek, sakin tempo |
| **Gastronomi Meraklısı** | Yöresel lezzet, restoran, pazar |
| **Aile Seyahati** | Güvenlik, çocuk dostu, kısa mesafe, erişilebilirlik |
| **Konfor/Lüks** | Merkezi konum, üst segment otel, özel transfer |

---

## 2. Mimari — Lider Ajan + 9 Uzman (Çıktı 1)

### 2.1. Topoloji

```
                    ┌─────────────────────────────────────────┐
   Kullanıcı ──────▶│  GİRİŞ KATMANI (FastAPI)                │
                    │  rate-limit · oturum · RIZA durumu      │
                    └──────────────────┬──────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │  GUARDRAIL — GİRİŞ (Agno pre-hooks)     │
                    │  PromptInjection · PII(mask) · Moderation│
                    └──────────────────┬──────────────────────┘
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │  SEMANTIC CACHE (Redis) ── HIT ──▶ yanıt│  0 LLM çağrısı
                    └──────────────────┬──────────────────────┘ MISS
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │  KARMAŞIKLIK SINIFLANDIRICI (ucuz model)│
                    └────────┬────────────────────┬───────────┘
                   basit ────┘                    └──── karmaşık
                        ▼                                ▼
        ┌───────────────────────────┐   ┌──────────────────────────────────┐
        │ HIZLI YOL                 │   │ YAVAŞ YOL                        │
        │ Team(mode="route")        │   │ Team(mode="coordinate")           │
        │ Lider → TEK uzmana        │   │ LİDER AJAN:                      │
        │ yönlendirir               │   │  1. isteği alt görevlere böl      │
        │ (hava, SSS, vize, döviz)  │   │  2. uzmanlara delege et           │
        │                           │   │  3. sonuçları sentezle            │
        └───────────────────────────┘   └──────────────────────────────────┘
                        └────────────────┬───────────────┘
                                         ▼
   ┌────────────────────────── 9 UZMAN AJAN (members) ──────────────────────────┐
   │ 1 destination_scout    2 itinerary_architect   3 logistics_agent           │
   │ 4 culinary_guide       5 culture_curator       6 practical_desk            │
   │ 7 documents_officer    8 faq_specialist        9 preference_keeper         │
   └───────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
   ┌─────────────── ORTAK KAYNAKLAR (tüm ajanlar erişir) ──────────────────────┐
   │ Knowledge (RedisVectorDb: küratörlü içerik) │ Tools (canlı API adapter'ları)│
   │ MemoryManager (gezgin profili) ⭐            │ RedisDb (oturum) + cache      │
   └───────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
        GUARDRAIL — ÇIKIŞ (post-hook): groundedness/atıf · PII maskeleme
                    · yüksek-risk feragat enjeksiyonu · marka tonu
                                   ▼
        KVKK KATMANI ⭐ (rıza · minimizasyon · TTL · silme hakkı · denetim izi)
                                   ▼
        GÖZLEMLENEBİLİRLİK (trace · gecikme/maliyet · groundedness eval)
```

### 2.2. Neden iki yol? (sunumun kilit argümanı)

Coordinate mode güçlüdür ama lider + üye zinciri her istekte birden fazla LLM çağrısı demektir → case'in "⚡ düşük gecikme" gereksinimiyle çelişir. Çözüm: **isteğin karmaşıklığına göre yol seçimi.**

| Yol | Ne zaman | LLM çağrısı | Hedef gecikme |
|---|---|---|---|
| Cache HIT | Tekrar eden soru | **0** | < 300 ms |
| Hızlı yol (`route`) | Tek alanlı ("Roma'da hava nasıl?") | 1 + 1 | < 1.5 sn |
| Yavaş yol (`coordinate`) | Karma/çok adımlı ("4 günlük Roma planı, vejetaryenim") | 3–6 | ilk token < 2 sn (streaming) |

İki `Team` nesnesi **aynı member listesini paylaşır** → kod tekrarı yok.

### 2.3. Lider ajan sorumlulukları
- İsteği çözümle → alt görevlere böl → doğru uzmanlara delege et → **tek tutarlı sese** sentezle
- Marka personası + etik kurallar + kapsam sınırları + KVKK kuralları lider `instructions`'ında
- `db=RedisDb(...)` → oturum kalıcılığı · `add_history_to_context=True`
- `enable_user_memories=True` → konuşmadan tercih çıkarımı otomatik
- Uzmanlar çelişirse hakem: **kaynak kademesi yüksek olan kazanır** (T0/T1 > T2/T3)
- `show_members_responses=True` → delegasyon videoda görünür (en etkili demo anı)

### 2.4. Dokuz uzman ajan

| # | Ajan | Rol | Tools / Knowledge | Not |
|---|---|---|---|---|
| 1 | `destination_scout` | Bütçe+tarih+tercihe uygun destinasyon önerir | `search_destinations`, `get_seasonality`, `estimate_trip_cost` | Skorlama formülünü uygular |
| 2 | `itinerary_architect` | Rota + günlük gezi planı üretir | `get_pois`, `estimate_travel_time`, `validate_itinerary` · **`output_schema=Itinerary`** | En güçlü model; planı doğrulayıcıdan geçirir |
| 3 | `logistics_agent` | Konaklama + ulaşım + transfer | `search_hotels`, `search_flights`, `get_transfer_options` | Bütçe sert filtresi |
| 4 | `culinary_guide` | Yöresel lezzet + restoran | `search_restaurants`, `get_local_dishes` | Diyet sert filtresi |
| 5 | `culture_curator` | Kültür, görgü kuralları, turistik nokta, ipuçları | `knowledge=` küratörlü rehberler (RAG) | Halüsinasyona en açık alan → yalnızca T0 |
| 6 | `practical_desk` | Hava, **saat farkı**, döviz, priz, güvenlik | `get_weather`, `get_timezone_diff`, `get_fx`, `get_practical_facts` | Saat farkı: `zoneinfo` + IANA tzdata, **API yok** |
| 7 | `documents_officer` | Vize, pasaport, giriş koşulları | `get_visa_requirements`, `check_passport_validity` | **Yüksek risk**: yalnızca T1 resmi kaynak + zorunlu feragat |
| 8 | `faq_specialist` | SSS bilgi tabanı | `knowledge=`, `search_knowledge=True` | 13 kategorili taksonomi |
| 9 | `preference_keeper` | Tercihleri çıkarır, saklar, açıklar | `MemoryManager` · **`output_schema=TravelerProfile`** · `explain_recommendation` | ⭐ Çıktı 7'nin kalbi |

### 2.5. Agno primitifleri → case gereksinimleri eşlemesi

> Bu tablo sunumun en güçlü slaytlarından biri: her case gereksinimi framework'te somut bir karşılığa oturuyor. Tüm importlar kurulu sürümde **doğrulandı** (`docs/agno-api-dogrulama.md`).

| Case gereksinimi | Agno karşılığı |
|---|---|
| Agent mimarisi & modüller | `Team` (lider) + `Agent(role=...)` (üyeler) |
| **Tercih yönetimi (Çıktı 7)** | `MemoryManager` + `UserMemory` + `enable_user_memories` |
| RAG / bilgi tabanı | `Knowledge` + `RedisVectorDb` + `OpenAIEmbedder` |
| **Cache / Redis (Çıktı 5)** | `RedisDb` (oturum) + `RedisVectorDb` (vektör) + kendi semantic cache katmanımız |
| OpenAI kullanımı (Çıktı 6) | `OpenAILike` / `OpenAIChat` + `OpenAIEmbedder` |
| Güvenlik / etik | `PromptInjectionGuardrail`, `OpenAIModerationGuardrail` |
| **KVKK** | `PIIDetectionGuardrail(mask_pii=True)` + `MemoryManager.delete_user_memory()` / `clear_user_memories()` |
| Yapılandırılmış çıktı | `output_schema=` (Pydantic) |
| Ölçeklenebilir servis | `AgentOS` (stateless FastAPI) — prod yolu olarak anlatılır |

**Redis tek altyapıda üç iş yapıyor:** oturum kalıcılığı + vektör arama + önbellek. Case "cache (Redis vb.) ve veri yönetimi planı" istediği için tam isabet.

---

## 3. Çıktı 7 — Tercih yönetimi & öneri mekanizması

**Üç katmanlı tercih toplama:**
1. **Açık** — tercih paneli: bütçe bandı, seyahat stili, tempo, diyet, erişilebilirlik, grup bileşimi, iklim
2. **Örtük** — davranış sinyalleri (kaydetti / reddetti / tekrar sordu) → ağırlıklı, zamanla sönümlenen
3. **Konuşmadan** — Agno `enable_user_memories` otomatik yakalar + `preference_keeper` `output_schema=TravelerProfile` ile yapılandırır

**Öneri skoru (slaytta formül olarak):**
```
score = w₁·tercih_uyumu + w₂·popülerlik + w₃·sezon_uygunluğu
      + w₄·bütçe_uyumu + w₅·yenilik − w₆·son_reddedilenler
```
- **Sert filtreler** (asla ihlal edilmez): diyet, erişilebilirlik, bütçe üst sınırı, vize uygunluğu
- **Yumuşak yeniden sıralama:** yukarıdaki skor
- **Cold start:** 5 persona arketipi (§1.3) → 2-3 etkileşimde bireysel profile geçiş
- **Şeffaflık:** her öneride **"Neden bu öneri?"** (skor kırılımı) + düzenlenebilir tercih paneli → aynı anda UX ve KVKK "düzeltme hakkı" kazanımı
- **Veri minimizasyonu:** `MemoryManager.memory_capture_instructions` ile neyin hatırlanacağı sınırlanır (pasaport no vb. asla yakalanmaz)
- **Demo kanıtı:** tercih panelinden `ekonomik → lüks` + `vegan` açılınca **aynı soru farklı plan üretiyor**

---

## 4. Çıktı 4 — Veri kaynağı stratejisi: 5 kademeli güven hiyerarşisi

| Kademe | Kaynaklar | Kullanım kuralı |
|---|---|---|
| **T0 — Kurum içi küratörlü** | Editoryal kültür rehberleri, POI seti, insan onaylı içerik | En yüksek güven; kültürel bilgi buradan |
| **T1 — Resmi / kamusal** | T.C. Dışişleri Bakanlığı & konsolosluklar, IATA Timatic, WHO/CDC, Sağlık Bakanlığı, seyahat uyarıları | **Vize/pasaport/sağlık için ZORUNLU** — atıf + geçerlilik tarihi + feragat |
| **T2 — Lisanslı ticari API** | Amadeus (uçuş/otel), Booking/Hotelbeds, Google Places / Foursquare, Ticketmaster, Numbeo | Canlı envanter & fiyat; kısa TTL |
| **T3 — Açık veri** | OpenStreetMap/Overpass, Wikidata, Wikivoyage (CC BY-SA), GeoNames, Open-Meteo, IANA tzdata, ECB döviz | Lisans + atıf zorunlu; gecelik batch ETL |
| **T4 — LLM parametrik bilgi** | Modelin kendi bilgisi | **Olgu için ASLA.** Yalnızca dil, ton, özetleme |

**Alım hattı:** yavaş veri → gecelik batch ETL (normalize → **Wikidata QID ile varlık eşleştirme = kanonik destinasyon anahtarı** → chunk → embed → vector DB + metadata) · oynak veri → istek anında tool çağrısı. Her kaynak için tazelik SLA'sı ve lisans kaydı.

**Ajan-kaynak yetkilendirmesi:** her uzman ajan yalnızca kendi kademelerine erişir (`documents_officer` T1 dışına çıkamaz) → kapsam kilidi **veri seviyesinde**, promptla değil.

---

## 5. Çıktı 5 — Redis anahtar şeması & TTL politikası

| Katman | Anahtar | TTL | Not |
|---|---|---|---|
| Semantic cache | `sc:{lang}:{hash}` | 1–24 sa | 0 LLM çağrısı, <300 ms |
| Destinasyon brief (ön-hesaplı) | `dest:{qid}` | 7 gün | Plan üretimini hızlandırır |
| POI / restoran | `poi:{qid}:{kat}` | 24 sa | |
| Hava durumu | `wx:{qid}:{tarih}` | 1 sa | |
| Döviz | `fx:{parite}` | 1 sa | |
| Uçuş arama | `fl:{o}:{d}:{tarih}` | 15 dk | |
| Otel arama | `htl:{qid}:{in}:{out}:{pax}` | 30 dk | |
| Vize matrisi | `visa:{from}:{to}` | 24 sa + **olay-tabanlı invalidation** | Mevzuat değişince anında temizlenir |
| Plan taslağı | `itin:{session}:{v}` | 24 sa | Revizyon için versiyonlu |
| **Gezgin profili** (Agno memory) | `prof:{hash(user)}` | **TTL yok — rızaya bağlı** | Silme talebinde purge |
| Oturum (Agno `RedisDb`) | `agno:session:{id}` | 30 dk sliding | |
| Rate-limit | `rl:{user}:{pencere}` | pencere | |
| Embedding cache | `emb:{hash}` | 7 gün | Maliyet tasarrufu |
| Idempotency | `idem:{key}` | 24 sa | Rezervasyon çift-tetik koruması |

**Dayanıklılık:** cache-aside · **single-flight kilidi** (stampede) · stale-while-revalidate · timeout + exponential backoff + circuit breaker · fallback zinciri `Redis → canlı API → eski cache + "gecikmeli olabilir" uyarısı → resmi kanala yönlendirme`.

**KVKK kesişimi:** profil/oturum anahtarları kişisel veri taşır → anahtarlarda **ham PII yok** (kullanıcı kimliği hash'li), at-rest şifreleme, TR/AB bölgesi, silme talebinde cache purge zorunlu.

---

## 6. Çıktı 6 — OpenAI kullanım mimarisi

**Görev bazlı model katmanları** (`app/models.py` registry'sinden okunur → sürümden bağımsız):

| Görev | Katman | Neden |
|---|---|---|
| Karmaşıklık sınıflandırıcı | en ucuz/hızlı | Yol seçimi; tek etiket döner |
| Lider ajan (delegasyon + sentez) | orta-güçlü | Çözümleme ve tutarlı sentez |
| `itinerary_architect` | **en güçlü** | Akıl yürütme yoğun çok günlük planlama |
| Diğer uzmanlar | ucuz-orta | Dar kapsam, tool sonucunu dile çevirme |
| Embedding (RAG + semantic cache) | `text-embedding-3-small` | Çok dilli retrieval, ekonomik |
| Giriş güvenliği | Moderation | Guardrail-Giriş pre-hook |

**Demo runtime'ı:** GitHub Models — `OpenAILike(id="openai/gpt-4o", base_url="https://models.github.ai/inference")`. Gerçek OpenAI'ya geçiş `OpenAIChat(id=...)` ile tek satır; **başka kod değişmez.** Slaytta üretim tablosu güncel OpenAI kademelerini gösterir, demo bölümünde "aynı kod, farklı `base_url`" dürüstçe anlatılır.

**Teknikler:** Structured Outputs (`output_schema`) → plan/profil parse hatası yok · paralel tool call (`asyncio.gather`: hava + POI + saat farkı) · streaming (SSE) · `temperature 0.2` + `seed` · **prompt caching** (sistem promptu + destinasyon brief'i sabit önek → cache okumada büyük indirim + gecikme kazancı) · embedding cache · fallback zinciri `birincil model → küçük model → mock şablon`.

**Eval:** altın senaryo seti + LLM-as-judge groundedness + regresyon testleri.

---

## 7. Çıktı 2 — Kullanıcı senaryoları (18 senaryo; brief 10 istiyor)

Her senaryo: mesaj → hangi yol (cache/hızlı/yavaş) → **hangi uzman ajan(lar)** → kaynak kademesi → guardrail → beklenen davranış.

1. Destinasyon önerisi (bütçe+tarih+tercih) — `destination_scout`
2. **4 günlük gezi planı** — yavaş yol, çoklu ajan, structured output
3. **Plan revizyonu** ("2. günü sakinleştir") — oturum bağlamı + planner
4. Konaklama önerisi — `logistics_agent`, bütçe sert filtresi
5. Ulaşım / havalimanı transferi
6. Yöresel lezzet + restoran (**vegan sert filtresi**) — `culinary_guide`
7. Kültür & görgü kuralları — `culture_curator`, yalnızca T0
8. Turistik noktalar + gezi ipuçları
9. Hava durumu + ne giyilir — `practical_desk`
10. **Saat farkı** (`zoneinfo`, API yok)
11. **Vize / pasaport / giriş koşulları** — `documents_officer`, T1 + zorunlu feragat
12. SSS (bagaj / iptal / seyahat sigortası) — `faq_specialist`
13. **Tercih öğrenme** ("vejetaryenim, kalabalık sevmem") → profil güncellenir, sonraki öneriler değişir
14. **"Neden bu öneri?"** — skor kırılımı açıklaması
15. **KVKK: "verilerimi sil"** — çalışan silme + doğrulama
16. Etik ihlali reddi (yasa dışı / riskli istek)
17. Prompt-injection denemesi — Agno guardrail anında yakalar
18. Bilinmeyen soru → uydurmadan devretme · + çok dilli (EN/AR) · + **bütçe aşımı uyarısı**

En az 4 tanesi **detaylı adım-adım akış** olarak yazılacak: plan üretimi (lider delegasyonu görünür), vize (yüksek risk), tercih öğrenme, cache HIT.

---

## 8. Çıktı 3 — SSS bilgi mimarisi

13 ana kategori: Rezervasyon & Ödeme · Uçuş & Ulaşım · Konaklama · **Vize & Belgeler** · Sağlık & Sigorta · Bagaj · Değişiklik/İptal/İade · Destinasyon & Plan · Yerel Yaşam (para, priz, ulaşım kartı, bahşiş) · Erişilebilirlik & Özel Yolcu · Güvenlik & Acil Durum · **Hesap & Gizlilik (KVKK)** · Sadakat & Kampanya.

Doküman metadata'sı: `kategori > alt_konu > etiketler` + `dil` · `ülke/bölge kapsamı` · `geçerlilik_tarihi` · `kaynak` + `güven_kademesi` · `yüksek_risk` bayrağı · `son_güncelleme` · `sahip`. Ayrıca **içerik yönetişimi:** kim günceller, inceleme sıklığı, sürümleme.

---

## 9. KVKK & Etik Katmanı

- **Rıza kademeleri:** (1) yalnızca oturum, saklama yok → (2) kişiselleştirme rızası (profil saklanır) → (3) pazarlama. Ayrı ayrı ve geri alınabilir.
- **Özel nitelikli kişisel veri (KVKK m.6):** diyet tercihi inanç/felsefi görüşü (helal, vegan), erişilebilirlik ihtiyacı sağlık verisini ifşa edebilir → **açık rıza** olmadan işlenmez, ayrı saklanır, log'lanmaz.
- **Yurt dışına aktarım (KVKK m.9):** OpenAI bir yurt dışı veri işleyicisi. **Mimari çözüm:** `PIIDetectionGuardrail(mask_pii=True)` pre-hook'u ile PII modele gitmeden maskelenir → sınır ötesine kişisel veri hiç geçmez. Ayrıca hukuki dayanak (açık rıza / standart sözleşme) ve "kullanıcı verisiyle eğitim yok" taahhüdü.
- **Veri minimizasyonu:** pasaport numarası vb. asla saklanmaz (`memory_capture_instructions` ile sınırlanır).
- **Silme hakkı:** çalışan `DELETE /api/kvkk/me` → `MemoryManager.clear_user_memories()` + Redis purge + denetim kaydı.
- **Aydınlatma metni**, veri envanteri, denetim izi, DPIA notu.
- **Etik:** riskli bölge/yasa dışı istek reddi, sağlık/hukuk tavsiyesi vermeme, belirsizlikte insana devretme.

> ⚠️ KVKK madde numaraları yazım aşamasında bir kez daha teyit edilecek; iddialar ihtiyatlı formüle edilecek.

---

## 10. Teslim Paketi

| Çıktı | Dosya | Amaç |
|---|---|---|
| Tasarım dokümanı | `PLAN.md` (bu dosya) | 7 çıktının tamamı |
| **Çalışan demo** | `app/` + `web/` | "Sadece tasarlamadım, çalıştırdım" |
| n8n görsel akış | `n8n/*.json` | Brief'in andığı platform ilhamı |
| **Sunum** | `slides/index.html` → Ctrl+P → PDF | Asıl teslim |
| Video konuşma metni | `SUNUM-METNI.md` | Kayıt için hazır senaryo |
| Öğrenme rehberi | `docs/proje-rehberi.html` | Sunumda kendinden emin konuşmak için |
| Agno API referansı | `docs/agno-api-dogrulama.md` | Doğrulanmış import yolları |

---

## 11. Yol Haritası (25 → 30 Temmuz)

| Gün | Hedef |
|---|---|
| ~~25 Tem~~ | ✅ Agno 2.8.2 API doğrulaması · klasör/venv kurulumu · `CASE.md` · `PLAN.md` · `PROGRESS.md` |
| **26 Tem** | Çekirdek: `config` · `models` · `schemas` · `cache` · `knowledge` · `guardrails` · `kvkk` · `preferences` + `knowledge/` küratörlü veri + docker-compose |
| **27 Tem** | **9 uzman ajan + `team.py` lider ajan (iki yol)** + `tools/` adapter'ları + `planner.py` doğrulayıcı + testler |
| **28 Tem** | `main.py` (SSE) + web UI (zaman çizelgesi · tercih paneli · KVKK paneli) + 18 senaryonun canlı doğrulaması + n8n JSON |
| **29 Tem** | Slaytlar (~22, offline HTML) + `SUNUM-METNI.md` + öğrenme rehberi + PDF export + prova |
| **30 Tem (sabah)** | Video kaydı + teslim |

**Tampon:** UI cilası ve n8n en esnek kalemler. Sıkışırsa slayt/metin önceliklidir — asıl teslim onlar.

---

## 12. Doğrulama Stratejisi

1. **Birim/entegrasyon:** `tests/test_agent.py` — guardrail (etik/injection/PII), tercih skorlama, plan doğrulayıcı (mesafe/bütçe/tempo), KVKK silme, cache HIT/MISS, karmaşıklık sınıflandırıcı yol seçimi. pytest olmadan da çalışabilen desen.
2. **Uçtan uca:** sunucuyu başlat, **18 senaryonun hepsini** HTTP üzerinden gönder, beklenen yol + ajan + kaynak + guardrail davranışını doğrula.
3. **İki modda da çalıştır:** (a) GitHub Models token + Docker Redis, (b) anahtarsız mock + `InMemoryDb`. **İkisinde de tüm senaryolar geçmeli** → video çekiminde canlı hata riski sıfır.
4. **Lider ajan kanıtı:** `show_members_responses=True` ile delegasyonu göster — hangi uzmana gitti, ne döndü, lider nasıl sentezledi.
5. **Kişiselleştirme kanıtı:** aynı soru iki farklı profille → çıktılar gerçekten farklılaşıyor mu?
6. **KVKK kanıtı:** profil oluştur → `GET /api/kvkk/me` → `DELETE /api/kvkk/me` → kalıntı yok mu?
7. **Gecikme kanıtı:** üç yolu ölç, UI'da rozetlerle göster — "düşük gecikme" iddiasını sayıyla destekle.
8. **Slaytlar:** tarayıcıda gez, **Ctrl+P → PDF** bozulmuyor mu (teslim formatı bu).
9. **n8n:** JSON'u import et, node bağlantıları geçerli mi (ekran görüntüsü slayta girecek).

---

## 13. Riskler ve Önlemler

| Risk | Önlem | Durum |
|---|---|---|
| Agno API'si dokümanlardan farklı | Kurulu sürümde tek tek doğrulandı → `docs/agno-api-dogrulama.md` | ✅ **kapandı** |
| Coordinate mode gecikmesi | İki yollu tasarım + semantic cache + paralel tool call; ölçülüp UI'da gösterilecek | planlandı |
| Disk boşluğu (`/home` %91) | Torch/sentence-transformers kurulmadı; bağımlılıklar hafif tutuldu | ✅ kontrol altında |
| Gerçek seyahat API'leri yok | Deterministik mock adapter'lar; prod'da gövde değişir, tool imzası aynı kalır. Sunumda açıkça belirtilir. | planlandı |
| 5 gün kısıtı, mimari büyük | 9 ajan aynı şablondan üretilir (hızlı çoğaltma); slayt/metin öncelikli | planlandı |
| GitHub Models kotası düşük (RPM) | Semantic + embedding cache kotayı korur; mock mod her zaman yedek | planlandı |
| KVKK maddeleri hassas | Madde numaraları yazım sırasında teyit edilecek | açık |
