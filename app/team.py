"""Lider ajan ve iki yollu akış — sistemin orkestra şefi.

Bir isteğin geçtiği hat (PLAN.md §2.1):

    rate-limit → giriş guardrail → rıza/profil → semantic cache → karmaşıklık sınıflandırıcı
       → [HIZLI YOL | YAVAŞ YOL] → çıkış guardrail → semantic cache yazımı → trace

**İki yol neden var?** Coordinate mode her istekte lider + üye zinciri çalıştırır; bu,
case'in "düşük gecikme" gereksinimiyle çelişir. Karmaşıklığa göre yol seçmek çözer:

| Yol | Ne zaman | LLM çağrısı |
|---|---|---|
| Cache HIT | Tekrar eden soru | **0** |
| Hızlı yol | Tek alanlı soru ("Roma'da hava nasıl?") | **1** |
| Yavaş yol | Çok alanlı / plan isteği | 3–6 (Agno `Team(mode="coordinate")`) |

**Hızlı yolda `Team(mode="route")` neden kullanılmıyor?** Route modunda lider, yönlendirme
kararı için de bir LLM çağrısı harcar → 2 çağrı. Sınıflandırmayı kural tabanlı yapınca
hızlı yol tek çağrıya iner ve gecikme yarıya düşer. Route ekibi yine kuruluyor ve
`/api/architecture` üzerinden görünür; ölçüm sonucu bilinçli olarak devre dışı
(bkz. PROGRESS.md "Alınan Kararlar").

**Mock mod:** LLM yoksa aynı hat aynı sırayla çalışır; tek fark, cümleyi LLM yerine
şablonun kurmasıdır. Olgular her iki modda da aynı araçlardan gelir (CLAUDE.md kural 2).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import agents as A
from . import config, guardrails as g, kvkk, models
from . import preferences as pref
from .cache import cache, keys
from .knowledge import build_agno_knowledge, kb
from .schemas import ChatRequest, ChatResponse, Source, Trace, TravelerProfile
from .text import fold as _fold   # Türkçe doğru küçük harf + aksan katlama (app/text.py)

# ─────────────────────────────────────────────────────────────────────
# Niyet tespiti — kural tabanlı sınıflandırıcı (0 LLM çağrısı)
# ─────────────────────────────────────────────────────────────────────
_PLAN_RE = re.compile(r"(gezi plan|günlük plan|gunluk plan|program(ı|ını)? (yap|çıkar|oluştur)|"
                      r"itinerary|rota (çıkar|oluştur|yap)|plan(ı|ımı)? (yap|çıkar|oluştur)|"
                      r"ne gezilir|nereleri? gez)", re.I)
# "4 günlük kaçamak için NEREYE gitsem" bir plan isteği değil, destinasyon sorusudur.
# Bu ayrım olmadan "\d+ günlük" kalıbı her destinasyon sorusunu yavaş yola sokuyordu.
# "öner" tek başına da öneri isteğidir ("destinasyon öner"). Eski kalıp bunu kaçırıyor,
# oturumdan devralınan destinasyonun künyesini sunuyordu.
# "gidebilirim" ÇIKARILDI: "İstanbul'dan nasıl gidebilirim" bir ULAŞIM sorusudur.
_ONERI_RE = re.compile(r"(nereye|nereyi|hangi (şehir|ülke|destinasyon|yer)|"
                       r"öner|tavsiye|gitsem|nere(si|ye) gid)", re.I)
# Kullanıcı planı beğenmedi ve farklısını istiyor
_PLAN_ALT_RE = re.compile(r"((başka|farklı|yeni|değişik|alternatif)\s+(bir\s+)?(plan|program|rota))|"
                          r"(beğenmedim|begenmedim|hoşuma gitmedi|olmadı bu|bunu istemedim|"
                          r"tekrar (yap|kur)|yeniden (yap|kur))", re.I)
_REVIZYON_RE = re.compile(r"((\d+)\.?\s*gün[üu]?n?[üu]?)\s*(sakinleştir|hafiflet|değiştir|azalt|"
                          r"yoğunlaştır|doldur)|planı (değiştir|güncelle|revize)", re.I)
_KVKK_SIL_RE = re.compile(r"(verilerimi|bilgilerimi|profilimi|hesabımı).{0,20}(sil|kaldır|unut)|"
                          r"(unut beni|beni unut|kvkk.{0,15}sil)", re.I)
_KVKK_GOR_RE = re.compile(r"(hangi verileri(m|mi)|verilerimi göster|neleri sakl|hangi bilgileri(m|mi)|"
                          r"veri(mi|lerim).{0,15}(gör|indir|dışa aktar)|aydınlatma metni)", re.I)
# ── Sohbet niyetleri ────────────────────────────────────────────────
# Bunlar olmadan sistem "merhaba", "teşekkürler", "sen kimsin", "ne alaka" gibi girdilere
# alakasız bir SSS kaydıyla cevap veriyordu. Gerçek kullanımda yaşandı; en görünür kusurdu.
_SELAM_RE = re.compile(r"^\s*(merhaba|selam(lar)?|günaydın|iyi (günler|akşamlar|sabahlar)|"
                       r"hey|naber|nasılsın|hello|hi|selamünaleyküm|s\.?a)\b[\s!.,?]*$", re.I)
_TESEKKUR_RE = re.compile(r"(teşekkür|tesekkur|sağ ?ol|sagol|eyvallah|thanks|thank you|"
                          r"harikasın|süpersin|çok iyisin|görüşürüz|hoşça ?kal)", re.I)
_KIMLIK_RE = re.compile(r"(sen kimsin|kimsin sen|nesin sen|sen nesin|adın ne|ismin ne|"
                        r"kendini tanıt|who are you|seni kim yaptı|bot musun|yapay zeka mısın)", re.I)
_YETENEK_RE = re.compile(r"(ne(ler)? yapabil|neler biliyorsun|yeteneklerin|ne işe yarıyorsun|"
                         r"nasıl kullan|yardım (edebilir|et)|what can you|ne yapıyorsun)", re.I)
# Kullanıcı düzeltmesi: sistem yanlış anladığında bunu FARK ETMESİ gerekir.
_DUZELTME_RE = re.compile(r"(ne alaka|alakası yok|alakasız|onu sormadım|bunu sormadım|"
                          r"ondan bahsetmedim|bundan bahsetmedim|yanlış anladın|"
                          r"öyle demedim|demek istediğim|saçmalama|anlamadın)", re.I)
# Öneri listesinin devamı: "bunlardan başka", "daha ucuz bir yer"
_ALTERNATIF_RE = re.compile(r"(başka (yok mu|var mı|bir yer|öneri)|bunlar(dan|ın) (başka|dışında)|"
                            r"diğer(leri| seçenek)|farklı (bir yer|öneri|seçenek)|alternatif|"
                            r"3ünden başka|üçünden başka|bunlar olmasın)", re.I)
_UCUZ_RE = re.compile(r"(daha (ucuz|uygun|hesaplı|ekonomik)|ucuza|bütçe dostu|daha az bütçe)", re.I)
_YERINE_RE = re.compile(r"(\w+)['’]?\w*\s+yerine", re.I)
# Bağlam devralmayı tetikleyen göndergesel ifadeler
_GONDERGE_RE = re.compile(r"\b(orada|orda|oranın|orası|oraya|orayı|burada|bu şehir|"
                          r"peki|ya |aynı yer|o şehir)\b", re.I)



_KW_CACHE: dict[str, Any] = {}


def _kw_hit(keyword: str, folded_message: str) -> bool:
    """Anahtar kelime eşleşmesi — **kelime başından**, düz alt dize taramasıyla değil.

    Düz `in` taraması sessiz yanlış eşleşmeler üretiyordu: "döviz **kur**u" için konan
    `kur` anahtarı "görgü **kur**alları" cümlesinde de eşleşiyor ve pratik bilgi masasını
    kültür sorusuna ortak ediyordu. Sonda sınır YOK, çünkü Türkçe ek alır
    ("vize" → "vizesi", "plan" → "planı").
    """
    rx = _KW_CACHE.get(keyword)
    if rx is None:
        rx = _KW_CACHE[keyword] = re.compile(rf"(?<![a-z0-9]){re.escape(_fold(keyword))}")
    return bool(rx.search(folded_message))


@dataclass
class Classification:
    route: str                      # fast | slow | blocked | cache
    complexity: str                 # basit | karmasik
    agents: list[str] = field(default_factory=list)
    reason: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    intent: Optional[str] = None    # selam | tesekkur | kimlik | duzeltme | alternatif | ...


def classify(message: str, ctx: A.Ctx) -> Classification:
    """Karmaşıklık sınıflandırıcı: hangi uzman(lar), hangi yol.

    Kural tabanlıdır ve **0 LLM çağrısı** harcar; PLAN.md'de "ucuz model" olarak
    tasarlanmıştı, ölçünce kuralların hem daha hızlı hem daha kararlı olduğu görüldü.
    Belirsiz kaldığında (hiçbir alan eşleşmiyorsa) ucuz modele danışılır.
    """
    # ── 0) Yorumlanabilir bir sözcük yok mu? ("!!!???", "12345678")
    #    Bu kontrol bağlam devralmadan ÖNCE gelir: aksi hâlde sistem önceki turdan
    #    devraldığı destinasyonla anlamsız girdiye emin bir cevap üretiyordu.
    if not A.anlamli_mi(message):
        return Classification(route="fast", complexity="belirsiz", agents=["concierge"],
                              reason="mesajda yorumlanabilir sözcük yok", intent="belirsiz")

    folded = _fold(message)

    # ── 1) Sohbet niyetleri: bunlar bir bilgi sorusu DEĞİL, karşılama ajanına gider.
    #    Sıra önemli: düzeltme en başta, çünkü kullanıcı yanlış anlaşıldığını söylüyorsa
    #    başka hiçbir yorum yapmadan özür dileyip yeniden sormalıyız.
    for rx, niyet, gerekce in (
        (_DUZELTME_RE, "duzeltme", "kullanıcı yanlış anlaşıldığını bildirdi"),
        (_SELAM_RE, "selam", "selamlama"),
        (_KIMLIK_RE, "kimlik", "kimlik sorusu"),
        (_YETENEK_RE, "yetenek", "yetenek sorusu"),
        (_TESEKKUR_RE, "tesekkur", "teşekkür / kapanış"),
    ):
        if rx.search(message):
            return Classification(route="fast", complexity="sohbet", agents=["concierge"],
                                  reason=gerekce, intent=niyet)

    # ── 2) Kapsam dışı destinasyon: "Bali'ye gitmek istiyorum"
    if ctx.unknown_place and not ctx.dest_key:
        return Classification(route="fast", complexity="kapsam", agents=["concierge"],
                              reason=f"'{ctx.unknown_place}' bilgi tabanında yok",
                              intent="kapsam_disi_yer")

    # Niyet önceliği: "neden bu öneri?" skor kırılımı sorusudur, yeni öneri talebi değil.
    if A._NEDEN_RE.search(message):
        return Classification(route="fast", complexity="basit", agents=["preference_keeper"],
                              reason="skor kırılımı açıklaması isteniyor")

    # ── 2b) Plan reddi: "başka bir plan yap, bunu beğenmedim"
    #    Aynı planı tekrar sunmak bu isteğe verilebilecek en kötü cevaptır.
    if _PLAN_ALT_RE.search(message) and ctx.dest_key:
        return Classification(route="slow", complexity="plan_varyant",
                              agents=["itinerary_architect"],
                              reason="kullanıcı planı beğenmedi, farklı kurgu isteniyor",
                              intent="plan_varyant")

    # ── 3) Öneri listesinin devamı: "bunlardan başka yok mu", "daha ucuz bir yer"
    #    Bu ancak ÖNCEKİ turda öneri verilmişse anlamlıdır; yoksa netleştirmeye düşeriz.
    ucuz = bool(_UCUZ_RE.search(message))
    alternatif = bool(_ALTERNATIF_RE.search(message)) or bool(_YERINE_RE.search(message))
    if (ucuz or alternatif) and (ctx.exclude or ctx.dest_key):
        return Classification(route="fast", complexity="takip", agents=["destination_scout"],
                              reason="önceki önerilerin devamı isteniyor",
                              intent="ucuz" if ucuz else "alternatif")

    skorlar: dict[str, float] = {}
    for key, s in A.SPECS.items():
        puan = sum(1.0 for kw in s.keywords if _kw_hit(kw, folded))
        if puan:
            skorlar[key] = puan

    oneri_istegi = bool(_ONERI_RE.search(message))
    oneri_sorusu = oneri_istegi and not ctx.dest_key
    # Plan isteği her zaman plan mimarına gider ve karmaşıktır
    plan_istegi = (bool(_PLAN_RE.search(message))
                   or bool(ctx.days and ctx.dest_key and "plan" in folded)) and not oneri_sorusu
    if plan_istegi:
        skorlar["itinerary_architect"] = skorlar.get("itinerary_architect", 0) + 3
    elif oneri_sorusu:
        # Destinasyon henüz belli değil: önce nereye gidileceğine karar verilir
        skorlar["destination_scout"] = skorlar.get("destination_scout", 0) + 3
        skorlar.pop("itinerary_architect", None)

    if not skorlar:
        # Hiçbir alan eşleşmedi. Eskiden burada koşulsuz SSS'e düşülüyordu ve SSS,
        # skoru ne olursa olsun en yakın kaydı EMİN bir tonda sunuyordu — "asdfgh"
        # girdisine "Rezervasyonunuz onaylandı mı?" cevabı bu yüzden çıkıyordu.
        # Artık SSS'e `intent="fallback"` ile gidiyoruz: alaka eşiğini geçemezse
        # karşılama ajanı devreye girip anlaşılmadığını söylüyor.
        # DİKKAT: destinasyon bu mesajda AÇIKÇA geçmiyorsa (yalnızca önceki turdan
        # devralındıysa) destinasyon künyesine düşmeyiz. Aksi hâlde Roma konuşulan bir
        # oturumda "asdfgh" yazan kullanıcıya emin bir Roma künyesi sunuluyordu.
        acik_destinasyon = bool(ctx.dest_key) and not ctx.inherited_dest
        varsayilan = "destination_scout" if acik_destinasyon else "faq_specialist"
        return Classification(route="fast", complexity="basit", agents=[varsayilan],
                              reason="anahtar kelime eşleşmesi yok; alaka eşiğiyle denenecek",
                              intent=None if acik_destinasyon else "fallback", scores={})

    # Tercih yöneticisi yalnızca TEK BAŞINA anlamlıdır. "Roma'da otel arıyorum, bütçem
    # 20.000 TL" cümlesinde "bütçem" onu tetikleyip isteği gereksiz yere yavaş yola
    # sokuyordu. Tercih çıkarımı zaten her istekte `respond` içinde yapılıyor; ayrı bir
    # uzman çağırmaya gerek yok.
    # ...ama yalnızca BASKIN değilse. "Vejetaryenim, kalabalık sevmem, orta bütçem var"
    # cümlesinde tercih yöneticisi açık ara önde ve doğru uzman odur.
    if len(skorlar) > 1 and "preference_keeper" in skorlar:
        digerleri = max(v for k, v in skorlar.items() if k != "preference_keeper")
        if skorlar["preference_keeper"] <= digerleri:  # berabere kalırsa alan uzmanı kazanır
            skorlar.pop("preference_keeper")

    sirali = sorted(skorlar.items(), key=lambda kv: kv[1], reverse=True)
    en_yuksek = sirali[0][1]
    secilen = [k for k, v in sirali if v >= max(1.0, en_yuksek * 0.6)]

    karmasik = plan_istegi or len(secilen) >= 2 or len(message) > 160
    if karmasik:
        # Plan isteğinde lojistik ve pratik masası da devreye girer (gerçek çok-ajanlı iş)
        if plan_istegi:
            for ek in ("practical_desk", "culinary_guide"):
                if ek not in secilen:
                    secilen.append(ek)
        return Classification(
            route="slow", complexity="karmasik", agents=secilen[:4],
            reason=("plan isteği: çok adımlı sentez gerekiyor" if plan_istegi
                    else f"{len(secilen)} farklı uzmanlık alanı eşleşti"),
            scores=skorlar, intent="oneri" if (oneri_istegi and not plan_istegi) else None)

    return Classification(route="fast", complexity="basit", agents=secilen[:1],
                          reason=f"tek alanlı soru → {secilen[0]}", scores=skorlar,
                          # "destinasyon öner" isteğinde kâşif LİSTE üretir, künye değil
                          intent="oneri" if oneri_istegi else None)


# ─────────────────────────────────────────────────────────────────────
# Agno altyapısı (gerçek mod)
# ─────────────────────────────────────────────────────────────────────
_agno: dict[str, Any] = {}


def _db() -> Any:
    """Oturum kalıcılığı: Redis varsa `RedisDb`, yoksa `InMemoryDb` (CLAUDE.md kural 2)."""
    if "db" in _agno:
        return _agno["db"]
    db = None
    try:
        if cache.backend_name == "redis":
            from agno.db.redis import RedisDb
            # DİKKAT: parametre adı `db_url` (redis_url DEĞİL) — kurulu sürümde doğrulandı.
            db = RedisDb(db_url=config.REDIS_URL, db_prefix="pusula", expire=config.SESSION_TTL)
        else:
            from agno.db.in_memory import InMemoryDb
            db = InMemoryDb()
    except Exception as exc:
        print(f"[team] Agno db kurulamadı ({exc.__class__.__name__}) -> db'siz devam")
    _agno["db"] = db
    return db


def _memory_manager() -> Any:
    """Agno hafıza yöneticisi — KVKK veri minimizasyonu `memory_capture_instructions` ile."""
    if "mm" in _agno:
        return _agno["mm"]
    mm = None
    if models.available():
        try:
            from agno.memory import MemoryManager
            mm = MemoryManager(
                model=models.get_model("specialist"),
                db=_db(),
                memory_capture_instructions=kvkk.MEMORY_CAPTURE_INSTRUCTIONS,
            )
        except Exception as exc:
            print(f"[team] MemoryManager kurulamadı ({exc.__class__.__name__})")
    _agno["mm"] = mm
    return mm


LEADER_INSTRUCTIONS = [
    "Sen Pusula AI'sın: Türkçe konuşan, kaynağa dayalı bir seyahat asistanı.",
    "Uzman ajanlarına iş dağıt, dönen sonuçları TEK tutarlı sese sentezle. "
    "Uzmanların çıktısını olduğu gibi yapıştırma; kullanıcının sorusuna göre birleştir.",
    "ASLA olgu uydurma: fiyat, saat, mesafe, vize kuralı, mekân adı yalnızca araç "
    "çıktılarından veya OLGU PAKETİ'nden gelir. Emin değilsen 'doğrulanmış kaydımız yok' de.",
    "Sayıları verildiği gibi yaz; yuvarlama, tahmin etme, aralık uydurma.",
    "Uzmanlar çelişirse kaynak kademesi yüksek olan kazanır (T0/T1 > T2/T3).",
    "Vize, pasaport ve sağlık konularında yalnızca Belge Sorumlusu'nun verdiği bilgiyi aktar "
    "ve feragat metnini koru. Kesin hukuki sonuç bildirme.",
    "Kişisel veri isteme; kullanıcı kendiliğinden verirse yanıtta tekrarlama.",
    "Markdown kullan, başlıklarla böl, gereksiz uzatma. Yanıtın sonunda kaynakları belirt.",
]


def build_team(mode: str = "coordinate") -> Any:
    """Agno `Team` — lider ajan + 9 uzman üye. Anahtar yoksa None."""
    if not models.available():
        return None
    anahtar = f"team:{mode}"
    if anahtar in _agno:
        return _agno[anahtar]
    try:
        from agno.team import Team

        knowledge = build_agno_knowledge()
        team = Team(
            name="Pusula Seyahat Ekibi",
            mode=mode,
            model=models.get_model("leader"),
            members=A.build_members(db=_db(), knowledge=knowledge),
            instructions=LEADER_INSTRUCTIONS,
            db=_db(),
            add_history_to_context=True,
            num_history_runs=3,
            enable_user_memories=False,  # profil yazımı KVKK kapısından geçer (kvkk.save_profile)
            memory_manager=_memory_manager(),
            show_members_responses=True,  # delegasyon videoda görünür
            markdown=True,
            pre_hooks=g.agno_input_guardrails(),
            telemetry=False,
        )
    except Exception as exc:
        print(f"[team] Team({mode}) kurulamadı ({exc.__class__.__name__}: {exc})")
        team = None
    _agno[anahtar] = team
    return team


# ─────────────────────────────────────────────────────────────────────
# LLM ile dile çevirme — olgular sabit, cümle modelin
# ─────────────────────────────────────────────────────────────────────
_RENDER_INSTRUCTIONS = [
    "Sen Pusula AI'sın: Türkçe konuşan, kaynağa dayalı bir seyahat asistanı.",
    "Sana bir OLGU PAKETİ (JSON) ve bir TASLAK yanıt verilir.",
    "Görevin taslağı kullanıcının sorusuna göre akıcı, sıcak ve düzenli Türkçeye çevirmektir.",
    "OLGU PAKETİ dışında HİÇBİR bilgi ekleme. Yeni fiyat, saat, mekân, tarih veya kural üretme.",
    "Sayıları ve özel adları taslaktaki gibi birebir koru; yuvarlama veya dönüştürme yapma.",
    "Taslaktaki tüm önemli kalemleri (liste, madde, uyarı, feragat) yanıtta koru.",
    "Markdown kullan. Gereksiz uzatma, girişte kendini tanıtma.",
]


def _render_agent() -> Any:
    if "renderer" in _agno:
        return _agno["renderer"]
    agent = None
    if models.available():
        try:
            from agno.agent import Agent
            agent = Agent(name="Pusula Yazar", model=models.get_model("leader"),
                          instructions=_RENDER_INSTRUCTIONS, markdown=True, telemetry=False)
        except Exception as exc:
            print(f"[team] Yazar ajanı kurulamadı ({exc.__class__.__name__})")
    _agno["renderer"] = agent
    return agent


# ── LLM çıktısı doğrulama + devre kesici ────────────────────────────
# Agno, sağlayıcı hatasında (kota, yetki, zaman aşımı) **exception fırlatmaz**:
# hata metnini `RunOutput.content` olarak döndürür. Doğrulamazsak bu metin doğrudan
# kullanıcıya yanıt olarak gider. Canlı olarak yaşandı: kota dolduğunda ekrana
# "Too many requests ... GitHub Terms of Service" düştü. Bir seyahat asistanının
# verebileceği en kötü yanıt bu.
_LLM_HATA_IZLERI = (
    "too many requests", "rate limit", "quota", "error in agent run", "terms of service",
    "unauthorized", "forbidden", "invalid api key", "authentication", "bad request",
    "internal server error", "service unavailable", "model not found", "context length",
)

# Devre kesici: üst üste başarısızlıkta LLM'i geçici kapatıp şablona düşeriz.
# Her istekte yeniden denemek, kota dolduğunda demoyu saniyelerce bekletirdi.
_BREAKER = {"ardisik_hata": 0, "acik_kalma": 0.0}
BREAKER_ESIGI = 3
BREAKER_SURESI = 120.0  # saniye


def _llm_kullanilabilir() -> bool:
    return models.available() and time.time() >= _BREAKER["acik_kalma"]


def _breaker_bildir(basarili: bool) -> None:
    if basarili:
        _BREAKER["ardisik_hata"] = 0
        return
    _BREAKER["ardisik_hata"] += 1
    if _BREAKER["ardisik_hata"] >= BREAKER_ESIGI:
        _BREAKER["acik_kalma"] = time.time() + BREAKER_SURESI
        _BREAKER["ardisik_hata"] = 0
        print(f"[team] LLM devre kesici açıldı: {BREAKER_SURESI:.0f} sn şablon moduna "
              "geçiliyor (sağlayıcı hataları)")


def _gecerli_uretim(taslak: str, uretilen: Optional[str]) -> bool:
    """Modelin döndürdüğü metin gerçekten bir yanıt mı, yoksa hata/çöp mü?"""
    if not uretilen or len(uretilen.strip()) < 40:
        return False
    kucuk = uretilen.lower()
    if any(iz in kucuk for iz in _LLM_HATA_IZLERI):
        return False
    # Taslakta çok sayıda olgu varsa, yeniden yazımda hiçbirinin kalmaması şüphelidir
    # (kesilmiş yanıt, konu dışı üretim). Tek sayının korunması yeterli sayılır.
    sayilar = set(re.findall(r"\d[\d.,]{2,}", taslak))
    if len(sayilar) >= 3 and not any(s in uretilen for s in sayilar):
        return False
    return True


# Sağlayıcı bağlam sınırı (GitHub Models gpt-4o: 8000 token). Uzun bir plan + üç uzman
# taslağı + olgu paketi bu sınırı aşıyordu ve çağrı hata döndürüyordu. Karakter bütçesi
# token sınırının altında güvenli bir tavan sağlar (~4 karakter ≈ 1 token).
PROMPT_KARAKTER_BUTCESI = 9000
TASLAK_UST_SINIR = 1800       # yavaş yolda uzman başına
RENDER_UST_SINIR = 5000       # hızlı yolda taslak bundan uzunsa LLM'e hiç gitme


def _kirp(metin: str, n: int) -> str:
    return metin if len(metin) <= n else metin[:n] + "\n…(kısaltıldı)"


def _facts_json(facts: Iterable[Any], limit: int = 6000) -> str:
    try:
        blob = json.dumps(list(facts)[:6], ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(list(facts))[:limit]
    return blob[:limit]


def _llm_render(ctx: A.Ctx, reply: A.AgentReply) -> tuple[Optional[str], int]:
    """Olgu paketini akıcı Türkçeye çevirir. Döner: (metin | None, llm_çağrı_sayısı)."""
    # Karşılama ajanının metinleri olgu çevirisi DEĞİL, hassas ifadelerdir
    # ("anlayamadım", "Bali kapsam dışı"). LLM bunları yeniden yazınca anlam kayboluyordu;
    # gerçek modda iki senaryo bu yüzden düşüyordu. Bu metinler olduğu gibi gider.
    if reply.agent == "concierge":
        return None, 0
    # Çok uzun taslakları yeniden yazdırmıyoruz: hem bağlam sınırını zorluyor hem de
    # şablon zaten düzgün biçimlendirilmiş. Kısaltarak göndermek içerik kaybettirirdi.
    if len(reply.text) > RENDER_UST_SINIR:
        return None, 0
    agent = _render_agent()
    if agent is None or not reply.text.strip():
        return None, 0
    # Son turlar prompta girer: "peki orada ne yenir" gibi takip sorularında model
    # neyin devamı olduğunu bilsin ve cümleyi kopuk kurmasın. Bağlam yalnızca ÜSLUP
    # içindir; olgular yine tek kaynaktan (olgu paketi) gelir.
    gecmis = ""
    if ctx.history:
        satirlar = [f"{'Kullanıcı' if h.get('role') == 'user' else 'Asistan'}: "
                    f"{str(h.get('content', ''))[:200]}" for h in ctx.history[-4:]]
        gecmis = "ÖNCEKİ TURLAR (yalnızca bağlam için):\n" + "\n".join(satirlar) + "\n\n"

    devralma = ""
    if ctx.inherited_dest and ctx.destination:
        devralma = (f"NOT: Kullanıcı destinasyon adını yazmadı; konuşmanın akışından "
                    f"**{ctx.destination}** kastedildiği anlaşıldı. Yanıtta bunu doğal biçimde "
                    "belli et (ör. 'Roma için...').\n\n")

    prompt = (
        f"{gecmis}KULLANICI SORUSU:\n{ctx.message}\n\n{devralma}"
        f"UZMAN: {A.SPECS[reply.agent].name if reply.agent in A.SPECS else reply.agent}\n\n"
        f"OLGU PAKETİ (JSON — tek doğru kaynak):\n{_facts_json(reply.facts)}\n\n"
        f"TASLAK YANIT:\n{reply.text}\n\n"
        "Taslağı yukarıdaki kurallara uyarak yeniden yaz."
    )
    try:
        out = agent.run(prompt)
        metin = getattr(out, "content", None)
        metin = metin.strip() if isinstance(metin, str) else None
        if _gecerli_uretim(reply.text, metin):
            _breaker_bildir(True)
            return metin, 1
        print("[team] LLM çıktısı doğrulamadan geçmedi (sağlayıcı hatası olabilir) "
              "-> şablon yanıt")
    except Exception as exc:
        print(f"[team] LLM yazımı başarısız ({exc.__class__.__name__}) -> şablon yanıt")
    _breaker_bildir(False)
    return None, 1


def _team_synthesize(ctx: A.Ctx, replies: list[A.AgentReply], session_id: str,
                     user_id: str) -> tuple[Optional[str], int]:
    """Yavaş yol: Agno `Team(mode="coordinate")` lider ajanı sentezi yapar."""
    team = build_team("coordinate")
    if team is None:
        return None, 0
    olgular = {r.agent: r.facts for r in replies}
    taslaklar = "\n\n".join(
        f"### {A.SPECS[r.agent].name}\n{_kirp(r.text, TASLAK_UST_SINIR)}"
        for r in replies if r.text)
    prompt = (
        f"KULLANICI İSTEĞİ:\n{ctx.message}\n\n"
        f"Uzman ajanlar araçlarını çalıştırdı ve şu OLGU PAKETİ'ni üretti (tek doğru kaynak, "
        f"dışına çıkma):\n{_facts_json([olgular], limit=3000)}\n\n"
        f"UZMAN TASLAKLARI:\n{taslaklar}\n\n"
        "Bu girdileri kullanıcının isteğine göre TEK tutarlı yanıta sentezle. Eksik bir alan "
        "görürsen ilgili uzmana danış. Olgu ekleme, sayı değiştirme."
    )
    if len(prompt) > PROMPT_KARAKTER_BUTCESI:
        print(f"[team] Sentez promptu bağlam bütçesini aşıyor ({len(prompt)} karakter) "
              "-> deterministik birleştirme")
        return None, 0
    try:
        out = team.run(prompt, session_id=session_id, user_id=user_id)
        metin = getattr(out, "content", None)
        metin = metin.strip() if isinstance(metin, str) else None
        if _gecerli_uretim(taslaklar, metin) and len(metin) > 60:
            _breaker_bildir(True)
            return metin, max(2, len(replies))
        print("[team] Coordinate çıktısı doğrulamadan geçmedi -> deterministik birleştirme")
    except Exception as exc:
        print(f"[team] Coordinate sentezi başarısız ({exc.__class__.__name__}: {exc}) "
              "-> deterministik birleştirme")
    _breaker_bildir(False)
    return None, 1


# ─────────────────────────────────────────────────────────────────────
# Oturum bağlamı (revizyon için)
# ─────────────────────────────────────────────────────────────────────
def _ctx_key(session_id: str) -> str:
    return f"sessctx:{session_id}"


def _load_session_ctx(session_id: str) -> dict:
    return cache.get_json(_ctx_key(session_id)) or {}


def _save_session_ctx(session_id: str, data: dict) -> None:
    cache.set_json(_ctx_key(session_id), data, ttl=config.SESSION_TTL)


def _cache_bucket(lang: str, profile: TravelerProfile, agents: list[str],
                  dest_key: Optional[str]) -> str:
    """Semantic cache kovası: dil | profil | niyet | destinasyon.

    Kovalama olmadan hashing embedder farklı niyetleri birbirine karıştırıyordu.
    Kova, benzerlik eşiğinin yükseltilmesinden daha iyi bir çözüm: eşiği yükseltmek
    gerçek tekrarları da kaçırırdı, kovalama yalnızca yanlış eşleşmeyi keser.
    """
    return f"{lang}|{_profile_fingerprint(profile)}|{agents[0] if agents else 'genel'}|{dest_key or '-'}"


# Destinasyon gerektiren uzmanlar: bunlara giden bir takip sorusunda, destinasyon
# mesajda geçmiyorsa oturum bağlamından devralınır.
_DEST_GEREKTIREN = {"itinerary_architect", "culinary_guide", "culture_curator",
                    "practical_desk", "logistics_agent", "documents_officer"}


def _apply_session_context(ctx: A.Ctx, session_id: str, mesaj: str) -> None:
    """Önceki turların bağlamını isteğe taşır — konuşmayı 'konuşma' yapan parça.

    Bu katman olmadan her mesaj sıfırdan yorumlanıyordu ve şunlar cevapsız kalıyordu:
      "peki orada ne yenir"      -> hangi destinasyon?
      "bunlardan başka yok mu"   -> nelerden başka?
      "roma yerine daha ucuz"    -> Roma yine listenin başında öneriliyordu

    Devralınan tek şey **yuvalardır** (destinasyon, gün, ay, önceki öneriler); yanıt
    içeriği değil. Yani bağlam, olgu üretimini değil yalnızca isteğin yorumunu etkiler.
    """
    sctx = _load_session_ctx(session_id)
    if not sctx:
        return

    ctx.history = cache.get_history(session_id)[-4:]

    # Önceki turda önerilen destinasyonlar -> "başka/alternatif" isteğinde elenecekler
    onceki_oneriler = list(sctx.get("last_suggestions") or [])
    if _ALTERNATIF_RE.search(mesaj) or _UCUZ_RE.search(mesaj):
        ctx.exclude = onceki_oneriler
        ctx.followup = True

    # "Roma yerine ..." -> Roma elenmeli
    if m := _YERINE_RE.search(mesaj):
        yerine = kb.resolve_destination(m.group(1))
        if yerine and yerine not in ctx.exclude:
            ctx.exclude.append(yerine)
            ctx.followup = True

    # Destinasyon devralma: mesajda yeni bir destinasyon geçmiyorsa ve konu devam ediyorsa
    if not ctx.dest_key and sctx.get("last_dest") and not ctx.unknown_place:
        yeni_konu = bool(_ONERI_RE.search(mesaj)) and not _GONDERGE_RE.search(mesaj)
        if not yeni_konu:
            ctx.dest_key = sctx["last_dest"]
            ctx.inherited_dest = True
            ctx.followup = True

    if ctx.days is None and sctx.get("last_days"):
        ctx.days = sctx["last_days"]
    if ctx.month is None and sctx.get("last_month"):
        ctx.month = sctx["last_month"]
    if not ctx.start_date and sctx.get("last_start"):
        ctx.start_date = sctx["last_start"]

    # Plan reddedildiyse bir sonraki kurguyu iste
    if _PLAN_ALT_RE.search(mesaj):
        ctx.variant = int(sctx.get("last_variant", 0)) + 1
        ctx.followup = True


def _profile_fingerprint(p: TravelerProfile) -> str:
    """Semantic cache kovası: farklı profil = farklı yanıt, aynı soruyu paylaşmasınlar."""
    import hashlib
    imza = json.dumps({
        "b": p.budget_band, "t": p.pace, "s": sorted(p.styles), "d": sorted(p.dietary),
        "a": sorted(p.accessibility), "g": p.group, "c": p.climate, "bt": p.budget_total,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(imza.encode("utf-8")).hexdigest()[:10]


# ─────────────────────────────────────────────────────────────────────
# KVKK niyetleri — senaryo 15'in çalışan hâli
# ─────────────────────────────────────────────────────────────────────
def _kvkk_reply(message: str, user_id: str, session_id: str) -> Optional[A.AgentReply]:
    if _KVKK_SIL_RE.search(message):
        sonuc = kvkk.delete_all(user_id, memory_manager=_agno.get("mm"),
                                session_ids=[session_id])
        cache.delete(_ctx_key(session_id))
        satirlar = [
            "**Verileriniz silindi.** KVKK silme hakkı talebiniz anında uygulandı.\n",
            f"- Silinen kayıt anahtarı: **{len(sonuc['silindi'])}**",
            f"- Agno hafızası temizlendi: **{'evet' if sonuc['agno_hafizasi_temizlendi'] else 'ilgili kayıt yoktu'}**",
            f"- Silme sonrası kalıntı denetimi: **{sonuc['dogrulama']}**",
            f"- Denetim izi: {sonuc['denetim_kaydi']} — kayıt ham kimliğinizi değil, "
            "hash'lenmiş kimliği içerir.\n",
            "Profiliniz, rıza kaydınız ve oturum bağlamınız kaldırıldı. Bundan sonraki öneriler "
            "sıfırdan, yalnızca o anki mesajınıza göre üretilecek.",
        ]
        return A.AgentReply(agent="kvkk_desk", text="\n".join(satirlar),
                            facts=[sonuc], tools_used=["kvkk.delete_all"])

    if _KVKK_GOR_RE.search(message):
        veri = kvkk.export_user_data(user_id)
        ozet = kvkk.compliance_summary(user_id)
        riza = veri["riza_durumu"]
        satirlar = [
            "**Verileriniz** (KVKK erişim hakkı)\n",
            f"- Kimlik: `{veri['kullanici_kimligi_hash']}` (hash — ham kimliğiniz saklanmaz)",
            f"- Kişiselleştirme rızası: **{'açık' if riza['personalization'] else 'kapalı'}**",
            f"- Özel nitelikli veri rızası (m.6): **{'açık' if riza['sensitive_data'] else 'kapalı'}**",
            f"- Saklanan profil: {'var' if veri['profil'] else '**yok**'}",
            "\n**Ne tutuyoruz, neden, ne kadar süre:**",
        ]
        for k in ozet["veri_envanteri"]:
            satirlar.append(f"- **{k['kategori']}** — {k['amac']} · dayanak: {k['hukuki_dayanak']} · "
                            f"süre: {k['saklama_suresi']}")
        satirlar.append("\n**Haklarınız:** " + ", ".join(ozet["haklar"]) +
                        ". Silmek için _\"verilerimi sil\"_ yazmanız yeterli.")
        return A.AgentReply(agent="kvkk_desk", text="\n".join(satirlar), facts=[veri],
                            tools_used=["kvkk.export_user_data"])
    return None


# ─────────────────────────────────────────────────────────────────────
# Plan revizyonu — senaryo 3
# ─────────────────────────────────────────────────────────────────────
def _revision(message: str, ctx: A.Ctx, session_id: str) -> Optional[A.AgentReply]:
    m = _REVIZYON_RE.search(message)
    if not m:
        return None
    sctx = _load_session_ctx(session_id)
    if not sctx.get("last_dest"):
        return None

    hedef_gun = int(m.group(2)) if m.group(2) else None
    profil = ctx.profile.model_copy(deep=True)
    folded = _fold(message)
    if any(w in folded for w in ("sakinlestir", "hafiflet", "azalt")):
        profil.pace = "sakin"
        degisiklik = "tempo sakinleştirildi"
    elif any(w in folded for w in ("yogunlastir", "doldur")):
        profil.pace = "yogun"
        degisiklik = "tempo yoğunlaştırıldı"
    else:
        degisiklik = "plan yeniden kuruldu"

    yeni_ctx = A.Ctx(message=message, profile=profil, user_id=ctx.user_id,
                     session_id=session_id, dest_key=sctx["last_dest"],
                     days=sctx.get("last_days") or 3, start_date=sctx.get("last_start"))
    reply = A.run_agent("itinerary_architect", yeni_ctx)
    if reply.itinerary:
        reply.itinerary.version = int(sctx.get("last_version", 1)) + 1
        onek = (f"Planı güncelledim — **{degisiklik}**"
                + (f" ({hedef_gun}. gün talebiniz tüm plana yansıtıldı; duraklar yeniden "
                   "dengelendiğinde diğer günler de değişir)" if hedef_gun else "")
                + f". Bu **{reply.itinerary.version}. sürüm**.\n\n")
        reply.text = onek + reply.text
    return reply


# ─────────────────────────────────────────────────────────────────────
# Ana akış
# ─────────────────────────────────────────────────────────────────────
def _collect_sources(replies: list[A.AgentReply]) -> list[Source]:
    gorulen: set[str] = set()
    out: list[Source] = []
    for r in replies:
        for s in r.sources:
            imza = f"{s.title}|{s.tier.value}"
            if imza not in gorulen:
                gorulen.add(imza)
                out.append(s)
    return out


def _compose(replies: list[A.AgentReply]) -> str:
    """Deterministik çok-ajan birleştirme (mock mod / LLM düşünce yedeği)."""
    if len(replies) == 1:
        return replies[0].text
    parcalar = []
    for r in replies:
        if not r.text.strip():
            continue
        ad = A.SPECS[r.agent].name if r.agent in A.SPECS else r.agent
        parcalar.append(f"### {ad}\n{r.text}")
    return "\n\n---\n\n".join(parcalar)


def respond(req: ChatRequest, on_event: Optional[Any] = None) -> ChatResponse:
    """Bir kullanıcı mesajını uçtan uca yanıtlar.

    `on_event(asama, veri)` verilirse hattın her durağı canlı bildirilir; SSE uç noktası
    bunu kullanarak "guardrail → cache → sınıflandırma → ajanlar → yanıt" akışını
    arayüzde gerçek zamanlı gösterir. Demonun en etkili anı bu.
    """
    def emit(asama: str, veri: dict) -> None:
        if on_event:
            try:
                on_event(asama, veri)
            except Exception:
                pass  # yayın hatası yanıtı durdurmasın

    t0 = time.perf_counter()
    trace = Trace(llm_mode=config.LLM_MODE)
    user_id, session_id = req.user_id, req.session_id

    # 1) Rate limit
    izin, sayac = cache.rate_limit_hit(user_id)
    if not izin:
        trace.route = "blocked"
        trace.guardrails.append("rate_limit")
        trace.latency_ms = int((time.perf_counter() - t0) * 1000)
        return ChatResponse(
            answer=(f"Çok sayıda istek aldım ({sayac}/{config.RATE_LIMIT_MAX} · "
                    f"{config.RATE_LIMIT_WINDOW} sn). Bir dakika sonra tekrar dener misiniz?"),
            trace=trace)

    # 2) Giriş guardrail'i — engellenen istekte LLM'e HİÇ gidilmez
    emit("guardrail", {"asama": "giriş denetimi"})
    verdict = g.check_input(req.message)
    trace.guardrails.extend(verdict.triggered)
    if verdict.blocked:
        emit("blocked", {"kategori": verdict.category})
        trace.route = "blocked"
        trace.latency_ms = int((time.perf_counter() - t0) * 1000)
        kvkk.audit(user_id, f"guardrail.{verdict.category}", "istek engellendi")
        return ChatResponse(answer=verdict.reply or "", trace=trace)

    mesaj = verdict.message  # PII maskelenmiş hâli; modele bu gider (KVKK m.9)

    # 3) Profil: kayıtlı + panel geçersiz kılmaları + bu mesajdan çıkarım
    emit("profile", {"asama": "rıza ve profil"})
    profil = pref.load(user_id)
    if req.profile_overrides:
        profil = pref.merge(profil, req.profile_overrides)
    oturum_cikarimi = pref.extract_from_text(mesaj)
    oturum_cikarimi.pop("_mentioned_destination", None)
    if oturum_cikarimi:
        profil = pref.merge(profil, oturum_cikarimi)
    if profil.is_empty() and not req.profile_overrides:
        profil = pref.apply_persona(profil, "kultur_avcisi")  # cold-start arketipi
        trace.guardrails.append("cold_start_persona")

    ctx = A.make_ctx(mesaj, profil, user_id=user_id, session_id=session_id)
    ctx.high_risk = verdict.high_risk
    # Kapsam dışı yer tespiti bağlam devralmadan ÖNCE yapılır: aksi hâlde "Bali'ye gitmek
    # istiyorum" isteği, önceki turdan devralınan Roma bağlamıyla cevaplanırdı.
    if not ctx.dest_key:
        ctx.unknown_place = kb.unknown_place(mesaj)
    _apply_session_context(ctx, session_id, mesaj)

    # 4) KVKK niyetleri (cache'lenmez — durum değiştiren işlemler)
    kvkk_reply = _kvkk_reply(mesaj, user_id, session_id)
    if kvkk_reply is not None:
        trace.route = "fast"
        trace.agents = ["kvkk_desk"]
        trace.tools = kvkk_reply.tools_used
        trace.complexity = "kvkk"
        return _finish(kvkk_reply, ctx, trace, t0, req, cacheable=False)

    # 5) Sınıflandırma önce yapılır: 0 LLM çağrısı, ~1 ms — ama cache kovasını belirler.
    plan = classify(mesaj, ctx)
    emit("classify", {"yol": plan.route, "karmasiklik": plan.complexity,
                      "ajanlar": plan.agents, "gerekce": plan.reason})

    # 6) Semantic cache — kova = dil | profil parmak izi | niyet
    #    Niyeti kovaya katmak şart: hashing embedder'la "4 günlük nereye gitsem" ile
    #    "Roma'da 3 günlük plan" 0.95 benzerlik veriyordu ve destinasyon sorusuna plan
    #    yanıtı dönüyordu. Aynı niyet + aynı profil olmadan HIT verilmez.
    kova = _cache_bucket(req.lang, profil, plan.agents, ctx.dest_key)
    embedding = models.embed(mesaj)
    hit, benzerlik = cache.semantic_lookup(embedding, kova)
    trace.cache_similarity = benzerlik
    emit("cache", {"hit": bool(hit), "benzerlik": benzerlik})
    if hit:
        payload = hit["payload"]
        trace.route = "cache"
        trace.cache_hit = True
        trace.agents = payload.get("agents", [])
        trace.tools = payload.get("tools", [])
        trace.complexity = "cache"
        trace.latency_ms = int((time.perf_counter() - t0) * 1000)
        cache.append_history(session_id, "user", mesaj)
        cache.append_history(session_id, "assistant", payload["answer"])
        return ChatResponse(
            answer=payload["answer"], trace=trace,
            sources=[Source(**s) for s in payload.get("sources", [])],
            disclaimer=payload.get("disclaimer"))

    # 7) Plan revizyonu (oturum bağlamı gerektirir)
    revizyon = _revision(mesaj, ctx, session_id)
    if revizyon is not None:
        trace.route = "slow"
        trace.complexity = "revizyon"
        trace.agents = ["itinerary_architect"]
        trace.tools = revizyon.tools_used
        return _finish(revizyon, ctx, trace, t0, req, cacheable=False)

    ctx.intent = plan.intent
    trace.route = plan.route
    trace.complexity = plan.complexity
    trace.agents = list(plan.agents)

    # 8) Uzmanları çalıştır (deterministik olgu toplama — her iki modda da aynı)
    replies = []
    for k in plan.agents:
        emit("agent", {"anahtar": k, "ad": A.SPECS[k].name if k in A.SPECS else k,
                       "araclar": A.SPECS[k].tools if k in A.SPECS else []})
        replies.append(A.run_agent(k, ctx))
    replies = [r for r in replies if r.text.strip()]
    if not replies:
        replies = [A.run_agent("faq_specialist", ctx)]
    for r in replies:
        for t in r.tools_used:
            if t not in trace.tools:
                trace.tools.append(t)

    # Trace, planlanan değil GERÇEKTE çalışan ajanları göstermeli: SSS uzmanı alaka
    # eşiğini geçemeyip karşılama ajanına devrettiyse arayüzde "concierge" görünmeli.
    gercek = [r.agent for r in replies if r.agent]
    if gercek and gercek != trace.agents:
        trace.agents = list(dict.fromkeys(gercek))

    ana = replies[0]
    birlesik = _compose(replies)
    llm_calls = 0

    # 9) Dile çevirme — hızlı yolda tek çağrı, yavaş yolda lider ajan sentezi
    if _llm_kullanilabilir():
        emit("llm", {"mod": "coordinate lider sentezi" if plan.route == "slow" and len(replies) > 1
                     else "tek çağrı ile dile çevirme",
                     "model": models.model_id("leader")})
        if plan.route == "slow" and len(replies) > 1:
            metin, n = _team_synthesize(ctx, replies, session_id, user_id)
            llm_calls += n
            if metin:
                birlesik = metin
        else:
            tek = A.AgentReply(agent=ana.agent, text=birlesik,
                               facts=[f for r in replies for f in r.facts])
            metin, n = _llm_render(ctx, tek)
            llm_calls += n
            if metin:
                birlesik = metin
    trace.llm_calls = llm_calls
    if models.available():
        if not _llm_kullanilabilir():
            trace.llm_mode = "şablon (LLM devre kesici açık)"
        elif llm_calls and birlesik == _compose(replies):
            trace.llm_mode = "şablon (LLM çıktısı doğrulamayı geçemedi)"

    sonuc = A.AgentReply(
        agent=ana.agent, text=birlesik,
        facts=[f for r in replies for f in r.facts],
        sources=_collect_sources(replies),
        tools_used=trace.tools,
        itinerary=next((r.itinerary for r in replies if r.itinerary), None),
        suggestions=next((r.suggestions for r in replies if r.suggestions), []),
        profile=next((r.profile for r in replies if r.profile), None),
        high_risk=any(r.high_risk for r in replies) or verdict.high_risk,
        disclaimer=next((r.disclaimer for r in replies if r.disclaimer), None),
    )
    return _finish(sonuc, ctx, trace, t0, req, cacheable=True, bucket=kova)


def _finish(reply: A.AgentReply, ctx: A.Ctx, trace: Trace, t0: float,
            req: ChatRequest, *, cacheable: bool, bucket: Optional[str] = None) -> ChatResponse:
    """Çıkış guardrail'i + oturum yazımı + cache + trace tamamlama."""
    # Groundedness bağlamı: araç olguları + kullanıcı mesajı (kullanıcının verdiği
    # '4 gün' uydurma değildir) + küratörlü retrieval sonuçları
    baglam: list[Any] = list(reply.facts) + [ctx.message]
    if ctx.dest_key:
        baglam += [h.doc for h in kb.search(ctx.message, k=3, dest_key=ctx.dest_key)]

    # Yuva uyarıları (kırpılan gün/kişi sayısı, geçersiz tarih) yanıtın başına eklenir:
    # sessizce düzeltmek yerine ne yaptığımızı söylüyoruz.
    if ctx.notes:
        reply.text = "".join(f"ℹ️ {n}\n\n" for n in ctx.notes) + reply.text
        reply.facts.append({"yuva_uyarilari": ctx.notes})

    verdict = g.check_output(reply.text, baglam, high_risk=reply.high_risk)
    trace.guardrails.extend(verdict.triggered)
    metin = verdict.answer
    if reply.disclaimer and not verdict.disclaimer_added and reply.disclaimer not in metin:
        metin += "\n\n---\nℹ️ " + reply.disclaimer

    trace.latency_ms = int((time.perf_counter() - t0) * 1000)

    cache.append_history(req.session_id, "user", ctx.message)
    cache.append_history(req.session_id, "assistant", metin)

    # Oturum bağlamı HER turda güncellenir — takip sorularının dayandığı hafıza budur.
    # (Eskiden yalnızca plan üretildiğinde yazılıyordu; bu yüzden "peki orada ne yenir"
    #  gibi sorular bağlamsız kalıyordu.)
    sctx = _load_session_ctx(req.session_id)
    if ctx.dest_key:
        sctx["last_dest"] = ctx.dest_key
    if ctx.days:
        sctx["last_days"] = ctx.days
    if ctx.month:
        sctx["last_month"] = ctx.month
    if ctx.start_date:
        sctx["last_start"] = ctx.start_date
    if reply.suggestions:
        sctx["last_suggestions"] = [s.key for s in reply.suggestions]
    elif ctx.dest_key and trace.agents == ["destination_scout"]:
        sctx["last_suggestions"] = list(dict.fromkeys(
            (sctx.get("last_suggestions") or []) + [ctx.dest_key]))
    sctx["last_agents"] = list(trace.agents)
    sctx["turn"] = int(sctx.get("turn", 0)) + 1

    if ctx.variant:
        sctx["last_variant"] = ctx.variant
    if reply.itinerary:
        sctx.update({"last_dest": reply.itinerary.destination_key or ctx.dest_key,
                     "last_days": len(reply.itinerary.days),
                     "last_version": reply.itinerary.version})
        cache.set_json(keys.itinerary(req.session_id, reply.itinerary.version),
                       reply.itinerary.model_dump(mode="json"), ttl=config.TTL_ITINERARY)
    _save_session_ctx(req.session_id, sctx)

    kaynaklar = [s.model_dump(mode="json") for s in reply.sources]
    if cacheable and bucket and verdict.grounded and trace.route != "blocked":
        cache.semantic_store(models.embed(ctx.message), ctx.message, {
            "answer": metin, "sources": kaynaklar, "agents": trace.agents,
            "tools": trace.tools, "disclaimer": reply.disclaimer,
        }, bucket)

    return ChatResponse(
        answer=metin, trace=trace, sources=reply.sources,
        itinerary=reply.itinerary, suggestions=reply.suggestions,
        profile=reply.profile,
        disclaimer=reply.disclaimer if reply.high_risk else None)


# ─────────────────────────────────────────────────────────────────────
# Mimari özeti (arayüz + sunum + n8n aynı kaynaktan beslensin)
# ─────────────────────────────────────────────────────────────────────
def architecture() -> dict:
    from . import tools as T

    return {
        "lider": {
            "ad": "Pusula Seyahat Ekibi",
            "framework": "Agno 2.8.2 Team",
            "modlar": {
                "coordinate": "yavaş yol — lider böler, delege eder, sentezler",
                "route": "kurulu ama devre dışı: yönlendirme için ekstra LLM çağrısı "
                         "harcıyordu; kural tabanlı sınıflandırıcı 1 çağrı tasarruf ediyor",
            },
            "talimatlar": LEADER_INSTRUCTIONS,
        },
        "uzmanlar": A.roster(),
        "araclar": T.catalog(),
        "yollar": [
            {"yol": "cache", "kosul": "semantic cache HIT", "llm_cagrisi": 0, "hedef_ms": 300},
            {"yol": "fast", "kosul": "tek alanlı soru", "llm_cagrisi": 1, "hedef_ms": 1500},
            {"yol": "slow", "kosul": "plan / çok alanlı istek", "llm_cagrisi": "3-6", "hedef_ms": 2000},
            {"yol": "blocked", "kosul": "guardrail engeli", "llm_cagrisi": 0, "hedef_ms": 50},
        ],
        "guardrails": g.summary(),
        "modeller": models.registry_summary(),
        "cache": cache.stats(),
        "bilgi_tabani": kb.stats(),
        "calisma_modu": config.runtime_summary(),
    }
