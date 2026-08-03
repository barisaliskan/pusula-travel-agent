"""Dokuz uzman ajan — PLAN.md §2.4.

Her ajan **iki biçimde** tanımlıdır ve ikisi aynı `AgentSpec`'ten türer:

* **Agno `Agent` nesnesi** (gerçek mod, yavaş yol) — lider ajanın `members` listesine girer,
  kendi araçlarını kendi çağırır, `output_schema` ile yapılandırılmış çıktı üretir.
* **Deterministik `handler`** (mock mod ve hızlı yol) — aynı araçları çağırır, olguları
  toplar ve şablonla yazar. LLM olmadan da tam çalışır (CLAUDE.md kural 2).

Bu ikilik kod tekrarı değil, **tasarım**: olgular her iki yolda da aynı araçlardan gelir;
değişen yalnızca cümleyi kimin kurduğudur. Gerçek modda LLM olguları yazıya döker, mock
modda şablon. "Model asla olgu uydurmaz" kuralı (CLAUDE.md 3) böylece mimariden gelir,
promptta verilen bir temenniden değil.

**Kapsam kilidi veri seviyesindedir:** `tiers` alanı ajanın erişebileceği kaynak
kademelerini sınırlar (`documents_officer` -> yalnızca T1) ve `tools` alanı hangi aracı
çağırabileceğini belirler. Prompt'a "sakın vize uydurma" yazmakla yetinmiyoruz;
uyduracak veriye erişemiyor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from . import config, models, preferences as pref, tools as T
from .knowledge import kb
from .planner import build_itinerary, validate_itinerary
from .schemas import Itinerary, Source, Tier, TravelerProfile
from .tools.base import tl

# ─────────────────────────────────────────────────────────────────────
# Ajan bağlamı ve yanıtı
# ─────────────────────────────────────────────────────────────────────
_AYLAR = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "mayis": 5,
    "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9,
    "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12,
}
_GUN_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:gün|gunluk|günlük|gece)", re.IGNORECASE)
_KISI_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:kişi|kisi|yolcu)", re.IGNORECASE)
_TARIH_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass
class Ctx:
    """Ajanların ortak girdisi. Yuvalar (gün, ay, kişi) mesajdan bir kez çıkarılır."""

    message: str
    profile: TravelerProfile = field(default_factory=TravelerProfile)
    user_id: str = "anon"
    session_id: str = "demo"
    dest_key: Optional[str] = None
    days: Optional[int] = None
    month: Optional[int] = None
    start_date: Optional[str] = None
    travelers: int = 1
    high_risk: bool = False

    # ── Konuşma bağlamı ────────────────────────────────────────────
    # Bunlar olmadan "peki orada ne yenir" veya "bunlardan başka yok mu" gibi takip
    # soruları cevapsız kalıyordu: her mesaj sıfırdan yorumlanıyor, önceki turda
    # konuşulan destinasyon ve öneriler biliniyormuş gibi davranılmıyordu.
    exclude: list[str] = field(default_factory=list)   # zaten önerilmiş/elenmiş destinasyonlar
    followup: bool = False                             # önceki turun devamı mı
    inherited_dest: bool = False                       # destinasyon bağlamdan mı geldi
    intent: Optional[str] = None                       # alternatif | ucuz | selam | duzeltme | ...
    history: list = field(default_factory=list)        # son turlar (rol, içerik)
    unknown_place: Optional[str] = None                # kapsam dışı yer adı ("Bali")
    notes: list[str] = field(default_factory=list)     # kırpma / geçersiz değer uyarıları
    variant: int = 0                                   # "başka bir plan" isteğinde artar

    @property
    def destination(self) -> str:
        d = kb.destination(self.dest_key) if self.dest_key else None
        return (d or {}).get("name", self.dest_key or "")


@dataclass
class AgentReply:
    """Bir uzmanın çıktısı: metin + **olgu paketi** + atıflar.

    `facts` gerçek modda LLM'e bağlam olarak verilir ve groundedness denetiminin
    referansıdır: yanıtta geçip burada geçmeyen sayı = uydurma.
    """

    agent: str
    text: str = ""
    facts: list[Any] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    itinerary: Optional[Itinerary] = None
    suggestions: list = field(default_factory=list)
    profile: Optional[TravelerProfile] = None
    high_risk: bool = False
    disclaimer: Optional[str] = None


MAX_DAYS = 10
MAX_TRAVELERS = 12

# Anlamlı içerik ölçütü: en az iki harfli, durak olmayan bir sözcük.
# "!!!???" veya "12345678" gibi girdilerde sistem, oturumdan devraldığı destinasyonla
# emin bir cevap üretiyordu — anlamsız girdiye anlamlı cevap, sessiz bir hatadır.
_ANLAMLI_RE = re.compile(r"[a-zçğıöşüA-ZÇĞİÖŞÜ]{2,}")


def anlamli_mi(message: str) -> bool:
    """Mesajda yorumlanabilir bir sözcük var mı?"""
    from .text import fold

    kelimeler = [k for k in _ANLAMLI_RE.findall(message) if len(fold(k)) >= 2]
    return bool(kelimeler)


def parse_slots(message: str, profile: Optional[TravelerProfile] = None) -> dict:
    """Mesajdan gün sayısı, ay, tarih, kişi sayısı ve destinasyonu çıkarır.

    Sınır dışı ve geçersiz değerler **sessizce kırpılmaz**: kullanıcıya ne yaptığımızı
    söyleyebilmek için `_notlar` listesine yazılır. Sessiz kırpma, kullanıcının 99 gün
    isteyip 3 günlük plan almasına ve nedenini bilmemesine yol açıyordu.
    """
    text = message.lower()
    out: dict[str, Any] = {"dest_key": kb.resolve_destination(message)}
    notlar: list[str] = []

    if m := _GUN_RE.search(text):
        istenen = int(m.group(1))
        out["days"] = max(1, min(MAX_DAYS, istenen))
        if istenen > MAX_DAYS:
            notlar.append(f"{istenen} günlük plan istediniz; küratörlü POI havuzumuz "
                          f"en fazla {MAX_DAYS} güne yetiyor, planı {MAX_DAYS} güne göre kurdum.")
    if m := _KISI_RE.search(text):
        istenen = int(m.group(1))
        out["travelers"] = max(1, min(MAX_TRAVELERS, istenen))
        if istenen > MAX_TRAVELERS:
            notlar.append(f"{istenen} kişilik grup için tahminleri {MAX_TRAVELERS} kişiye göre "
                          "hesapladım; daha büyük gruplarda grup tarifeleri devreye girer.")
    for ad, no in _AYLAR.items():
        # Sonda `\b` YOK: Türkçede ay adları çekim eki alır ("eylülde", "ekimin", "martta").
        # `\beylül\b` bunları kaçırır ve tarih bugüne düşer -> yanlış sezon, yanlış hava.
        if re.search(rf"\b{ad}", text):
            out["month"] = no
            break
    if m := _TARIH_RE.search(message):
        # Tarih GERÇEKTEN geçerli mi? "2026-13-45" regex'i geçiyor ama takvimde yok;
        # doğrulamadan kabul edersek ay=13 olur ve plan sessizce tarihsiz kurulur.
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            notlar.append(f"'{m.group(0)}' geçerli bir tarih değil, dikkate almadım.")
            d = None
        if d:
            bugun = date.today()
            if d < bugun:
                notlar.append(f"{d.strftime('%d.%m.%Y')} geçmiş bir tarih; planı yine de "
                              "o tarihe göre kurdum, ileri bir tarih verirseniz sezon ve "
                              "hava tahmini anlamlı olur.")
            out["start_date"] = d.isoformat()
            out["month"] = d.month
    elif out.get("month"):
        # Ay verilmiş ama tarih yok: o ayın 14'ünü varsayarız (hafta ortası, sezon temsili).
        yil = date.today().year + (1 if out["month"] < date.today().month else 0)
        out["start_date"] = f"{yil}-{out['month']:02d}-14"

    if notlar:
        out["_notlar"] = notlar
    return out


def make_ctx(message: str, profile: TravelerProfile, **kw) -> Ctx:
    slots = parse_slots(message, profile)
    notlar = slots.pop("_notlar", [])
    slots.update({k: v for k, v in kw.items() if v is not None})
    ctx = Ctx(message=message, profile=profile,
              **{k: v for k, v in slots.items() if k in Ctx.__dataclass_fields__})
    ctx.notes = notlar
    return ctx


def _src(title: str, tier: str, valid_until: Optional[str] = None) -> Source:
    try:
        t = Tier(tier if tier in ("T0", "T1", "T2", "T3", "T4") else "T0")
    except ValueError:
        t = Tier.T0
    vu = None
    if valid_until:
        try:
            vu = date.fromisoformat(str(valid_until)[:10])
        except ValueError:
            vu = None
    return Source(title=title, tier=t, valid_until=vu)


def _src_of(tool_out: dict) -> Source:
    k = (tool_out or {}).get("_kaynak") or {}
    return _src(k.get("baslik", "Pusula İçerik Editörlüğü"), k.get("kademe", "T0"),
                tool_out.get("gecerlilik_tarihi"))


def _need_destination(agent: str, ne_icin: str) -> AgentReply:
    ornekler = ", ".join(d["name"] for d in kb.destinations[:6])
    return AgentReply(
        agent=agent,
        text=(f"{ne_icin} için hangi destinasyonu merak ettiğinizi yazar mısınız?\n\n"
              f"Küratörlü bilgi tabanımızda şu an şu destinasyonlar var: **{ornekler}** ve "
              f"{len(kb.destinations) - 6} tanesi daha. Kapsam dışındaki bir yer için "
              "doğrulanmış verimiz olmadığından bilgi üretmiyoruz."),
    )


# ─────────────────────────────────────────────────────────────────────
# 1. destination_scout
# ─────────────────────────────────────────────────────────────────────
def h_destination_scout(ctx: Ctx) -> AgentReply:
    p = ctx.profile
    nights = ctx.days or 4

    # Kullanıcı belirli bir destinasyonu andıysa ve yeni liste istemiyorsa, o destinasyonu
    # ANLAT. Eskiden burada koşulsuz yeniden sıralama yapılıyordu: "1. Roma'yı seçiyorum,
    # detaylı anlat" isteğine üç destinasyonluk liste dönüyordu.
    if (ctx.dest_key and ctx.intent not in ("alternatif", "ucuz", "oneri")
            and not ctx.exclude):
        return _h_destination_brief(ctx)

    # "Roma yerine daha ucuz bir yer" -> Roma'yı ELE. Bu eleme olmadan sistem, alternatif
    # istenen destinasyonu listenin başında tekrar öneriyordu.
    exclude = list(dict.fromkeys(list(ctx.exclude) + ([ctx.dest_key] if ctx.dest_key else [])))

    oneriler, elenenler = pref.recommend(
        p, month=ctx.month, nights=nights, limit=3, user_id=ctx.user_id, exclude=exclude,
        travelers=ctx.travelers,
    )
    facts: list[Any] = [{"elenenler": elenenler, "haric_tutulanlar": exclude}]
    if not oneriler:
        if exclude:
            gosterilen = ", ".join((kb.destination(k) or {}).get("name", k) for k in exclude)
            return AgentReply(
                agent="destination_scout",
                text=(f"Küratörlü listemizde ({len(kb.destinations)} destinasyon) daha önce "
                      f"gösterdiklerimin ({gosterilen}) dışında, kriterlerinize uyan başka bir "
                      "yer kalmadı.\n\nBütçe bandını veya ayı değiştirirsek yeniden bakabilirim — "
                      "ya da elediğim destinasyonların gerekçelerini gösterebilirim."),
                facts=facts, tools_used=["search_destinations"])
        # Çıkmaz sokak bırakma: neyin işe yarayacağını da söyle. Sert filtre doğru
        # çalışsa bile "hiçbir şey yok" demek kullanıcıyı ortada bırakır.
        satirlar = ["Belirttiğiniz kısıtlarla eşleşen destinasyon kalmadı — hepsi sert "
                    "filtrelerden elendi:\n"]
        satirlar += [f"- **{e['name']}**: {e['sebep']}" for e in elenenler[:5]]

        cikis_yolu = _butce_cikis_yolu(p, nights, ctx.travelers)
        if cikis_yolu:
            satirlar.append("\n**Ne yapabiliriz:**")
            satirlar += [f"- {c}" for c in cikis_yolu["oneriler"]]
            facts.append(cikis_yolu["olgular"])
        else:
            satirlar.append("\nBütçe üst sınırını veya kısıtları biraz gevşetirsek "
                            "yeniden bakabilirim.")

        return AgentReply(agent="destination_scout", text="\n".join(satirlar),
                          facts=facts, tools_used=["search_destinations", "estimate_trip_cost"])

    if ctx.intent == "ucuz":
        basli = f"Daha ekonomik **{len(oneriler)} alternatif**"
    elif ctx.intent == "alternatif" or exclude:
        basli = f"Bunların dışında **{len(oneriler)} destinasyon** daha"
    else:
        basli = f"Profilinize göre **{len(oneriler)} destinasyon** öne çıkıyor"
    satirlar = [basli + (f" ({ctx.month}. ay için)" if ctx.month else "") + ":\n"]
    kaynaklar: list[Source] = []
    for i, s in enumerate(oneriler, 1):
        maliyet = T.estimate_trip_cost(s.key, nights=nights, budget_band=p.budget_band or "orta",
                                       travelers=ctx.travelers)
        facts.append(maliyet)
        sezon = T.get_seasonality(s.key, ctx.month)
        facts.append(sezon)
        satirlar.append(
            f"**{i}. {s.name}, {s.country}** — skor {s.score.total:.2f}\n"
            f"{s.summary}\n"
            f"- Tahmini toplam: **{tl(maliyet['tahmini_toplam_try'])} TRY** "
            f"({nights} gece, {ctx.travelers} kişi, {p.budget_band or 'orta'} bandı; uçuş dahil)\n"
            f"- İdeal aylar: {', '.join(map(str, s.best_months))}"
            + (f" — {sezon['sorulan_ay_durumu']}" if sezon.get("sorulan_ay_durumu") else "")
            + (("\n- " + "\n- ".join(s.score.notes[:3])) if s.score.notes else "")
        )
        kaynaklar.extend(s.sources)
    if elenenler:
        satirlar.append("\n**Sert filtrelerle elenenler:** " +
                        ", ".join(f"{e['name']} ({e['sebep']})" for e in elenenler[:3]))
    satirlar.append("\n_Sıralamanın nasıl hesaplandığını görmek için **\"neden bu öneri\"** "
                    "diye sorabilirsiniz._")

    return AgentReply(agent="destination_scout", text="\n\n".join(satirlar), facts=facts,
                      sources=kaynaklar[:3], suggestions=oneriler,
                      tools_used=["search_destinations", "estimate_trip_cost", "get_seasonality"])


def _butce_cikis_yolu(p: TravelerProfile, nights: int, travelers: int) -> Optional[dict]:
    """Bütçe hiçbir destinasyona yetmiyorsa: en ucuz seçenek nedir, ne değişirse olur?

    Kullanıcıya "olmaz" demek yetmez; **hangi kısıt gevşerse olur** sorusunun cevabı
    da verilmelidir. Tüm rakamlar küratörlü maliyet bandından hesaplanır.
    """
    if not p.budget_total:
        return None
    band = p.budget_band or "orta"
    adaylar = []
    for d in kb.destinations:
        if not (d.get("daily_cost_try") or {}).get(band):
            continue
        adaylar.append((pref.trip_total(d, band, nights, travelers), d))
    if not adaylar:
        return None
    adaylar.sort(key=lambda x: x[0])
    en_ucuz_tutar, en_ucuz = adaylar[0]

    # Her yazdığımız tutar olgu paketine de girer: türetilmiş sayılar da groundedness
    # denetiminden geçmek zorunda (kendi hesabımız diye muaf değil).
    olgular: dict[str, Any] = {
        "en_ucuz_destinasyon": en_ucuz["name"],
        "en_ucuz_toplam_try": round(en_ucuz_tutar, 2),
        "en_ucuz_gosterim": tl(en_ucuz_tutar),
        "butce_try": p.budget_total, "gece": nights, "kisi": travelers, "bant": band,
    }
    oneriler = [
        f"**{en_ucuz['name']}** listemizin en ekonomik seçeneği: {nights} gece × "
        f"{travelers} kişi için uçuş dahil **{tl(en_ucuz_tutar)} TRY**. Bütçenizi "
        f"{tl(en_ucuz_tutar)} TRY'ye çıkarabilirseniz bu mümkün olur."
    ]
    # Daha az gece ile bütçeye sığar mı?
    for az_gece in range(nights - 1, 0, -1):
        tutar = pref.trip_total(en_ucuz, band, az_gece, travelers)
        if tutar <= p.budget_total:
            olgular["az_gece"] = az_gece
            olgular["az_gece_tutar_try"] = round(tutar, 2)
            olgular["az_gece_gosterim"] = tl(tutar)
            oneriler.append(f"Aynı bütçeyle **{az_gece} gece** yapabilirsiniz: "
                            f"{en_ucuz['name']} için tahmini **{tl(tutar)} TRY**.")
            break
    # Daha ekonomik bant işe yarar mı?
    if band != "ekonomik":
        tutar = pref.trip_total(en_ucuz, "ekonomik", nights, travelers)
        olgular["ekonomik_bant_tutar_try"] = round(tutar, 2)
        olgular["ekonomik_bant_gosterim"] = tl(tutar)
        if tutar <= p.budget_total:
            oneriler.append(f"**Ekonomik bandda** {nights} gece mümkün: "
                            f"{en_ucuz['name']} için tahmini **{tl(tutar)} TRY**.")
        else:
            oneriler.append(f"Ekonomik banda geçmek tutarı {tl(tutar)} TRY'ye indiriyor "
                            "ama yine de bütçenin üzerinde kalıyor.")
    oneriler.append("_Uçuş kalemi simüle envanterden gelir; gerçek tarifelerde sezon ve "
                    "erken rezervasyon farkı belirleyicidir._")

    return {"oneriler": oneriler, "olgular": olgular}


def _h_destination_brief(ctx: Ctx) -> AgentReply:
    """Tek destinasyonu tanıtır: özet, maliyet, sezon, öne çıkanlar, artı/eksi.

    "Roma'yı seçiyorum, detaylı anlat" ya da "Roma'nın kötü yanları neler" gibi
    isteklerin doğru cevabı yeni bir liste değil, o destinasyonun künyesidir.
    """
    d = kb.destination(ctx.dest_key) or {}
    if not d:
        return _need_destination("destination_scout", "Destinasyon bilgisi")
    p = ctx.profile
    nights = ctx.days or 4
    band = p.budget_band or "orta"

    maliyet = T.estimate_trip_cost(ctx.dest_key, nights=nights, budget_band=band,
                                   travelers=ctx.travelers)
    sezon = T.get_seasonality(ctx.dest_key, ctx.month)
    skor = pref.score_destination(d, p, month=ctx.month, nights=nights)
    poi = T.get_pois(ctx.dest_key, limit=4)

    aylar = {1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
             7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"}
    ay_ad = lambda ms: ", ".join(aylar[m] for m in ms if m in aylar)  # noqa: E731

    satirlar = [f"**{d['name']}, {d['country']}**\n", d.get("summary", ""), ""]
    satirlar.append(f"- **Tahmini toplam:** {tl(maliyet['tahmini_toplam_try'])} TRY "
                    f"({nights} gece, {ctx.travelers} kişi, {band} bandı; uçuş dahil)")
    satirlar.append(f"- **Günlük harcama:** ~{tl(maliyet['gunluk_harcama_try'])} TRY · "
                    f"**uçuş:** {tl(maliyet['ucus_try'])} TRY")
    if d.get("best_months"):
        satirlar.append(f"- **İdeal aylar:** {ay_ad(d['best_months'])}")
    if d.get("avoid_months"):
        satirlar.append(f"- **Kaçınılacak aylar:** {ay_ad(d['avoid_months'])}")
    if sezon.get("sorulan_ay_durumu"):
        satirlar.append(f"- **Sorduğunuz ay:** {sezon['sorulan_ay_durumu']}")
    if d.get("seasonality_note"):
        satirlar.append(f"- {d['seasonality_note']}")
    satirlar.append(f"- **Uçuş süresi:** İstanbul'dan ~{d.get('flight_hours_from_ist','?')} saat · "
                    f"**para birimi:** {d.get('currency','')}")

    if poi.get("duraklar"):
        satirlar.append("\n**Öne çıkan duraklar:** " +
                        ", ".join(x["ad"] for x in poi["duraklar"][:4]))

    # Dürüst artı/eksi: kalabalık, yürünebilirlik ve erişilebilirlik küratörlü veriden
    artilar, eksiler = [], []
    if d.get("walkability", 0) >= 4:
        artilar.append("merkez yürünebilir, duraklar birbirine yakın")
    if d.get("family_friendly", 0) >= 4:
        artilar.append("aile seyahatine uygun")
    if d.get("crowd_level", 0) >= 5:
        eksiler.append("yoğun turist kalabalığı; popüler noktalarda kuyruk olabilir")
    elif d.get("crowd_level", 0) <= 2:
        artilar.append("kalabalık düşük")
    if d.get("avoid_months"):
        eksiler.append(f"{ay_ad(d['avoid_months'])} aylarında koşullar zorlaşıyor")
    if d.get("accessibility_note"):
        eksiler.append(d["accessibility_note"])
    tum = [v for v in (d.get("daily_cost_try") or {}).values() if v]
    if tum and (d.get("daily_cost_try") or {}).get(band, 0) >= sorted(tum)[-1] * 0.8:
        eksiler.append(f"{band} bandında günlük maliyet listemizin üst sıralarında")

    if artilar:
        satirlar.append("\n**Artıları:** " + " · ".join(artilar))
    if eksiler:
        satirlar.append("**Dikkat edilecekler:** " + " · ".join(eksiler))

    # Bütçe sert filtresi künyede de uygulanır. Aksi hâlde "bütçem 10 bin TL" diyen
    # kullanıcıya 16.440 TRY'lik bir destinasyon uyarısız sunuluyordu.
    if p.budget_total and maliyet["tahmini_toplam_try"] > p.budget_total:
        asim = maliyet["tahmini_toplam_try"] - p.budget_total
        satirlar.append(f"\n🔴 **Bütçe uyarısı:** bu tahmin, belirttiğiniz "
                        f"{tl(p.budget_total)} TRY bütçeyi **{tl(asim)} TRY aşıyor.** "
                        "Gece sayısını azaltabilir, ekonomik banda geçebilir ya da "
                        "_\"daha ekonomik alternatifler\"_ diyebilirsiniz.")

    satirlar.append(f"\n_Profilinize uygunluk skoru: {skor.total:.2f}. "
                    "İsterseniz **gezi planı çıkarayım**, **konaklama** bakayım ya da "
                    "**daha ekonomik alternatifler** önereyim._")

    src = (d.get("sources") or [{}])[0]
    return AgentReply(
        agent="destination_scout", text="\n".join(satirlar),
        facts=[maliyet, sezon, poi, {"skor": skor.model_dump(mode="json"),
                                     "kalabalik": d.get("crowd_level"),
                                     "yurunebilirlik": d.get("walkability")}],
        sources=[_src(src.get("title", "Pusula İçerik Editörlüğü"), src.get("tier", "T0"),
                      src.get("valid_until"))],
        tools_used=["estimate_trip_cost", "get_seasonality", "get_pois"])


# ─────────────────────────────────────────────────────────────────────
# 2. itinerary_architect
# ─────────────────────────────────────────────────────────────────────
_LISTE_RE = re.compile(r"(görülmesi gereken|gezilecek yer|turistik|mutlaka gör|ne görülür|"
                       r"neler var|hangi yerler)", re.IGNORECASE)


def _h_poi_listesi(ctx: Ctx) -> AgentReply:
    """Durak listesi: küratörlü POI setinden süre, ücret ve önerilen zamanla."""
    veri = T.get_pois(ctx.dest_key or "", accessible_only=bool(ctx.profile.accessibility),
                      limit=8)
    duraklar = veri.get("duraklar", [])
    if not duraklar:
        return _need_destination("itinerary_architect", "Gezilecek yerler")

    satirlar = [f"**{veri['destinasyon']} — öne çıkan duraklar** "
                f"(küratörlü POI setinden {len(duraklar)} kayıt)\n"]
    for d in duraklar:
        ucret = f"{tl(d['ucret_try'])} TRY" if d["ucret_try"] else "ücretsiz"
        eris = " · ♿ erişilebilir" if d["erisilebilir"] else ""
        satirlar.append(f"- **{d['ad']}** — {d['semt']} · {d['kategori']} · {d['sure_dk']} dk · "
                        f"{ucret} · en iyi zaman: {d['onerilen_zaman']}{eris}"
                        + (f"\n  {d['not']}" if d["not"] else ""))
    toplam_sure = sum(d["sure_dk"] for d in duraklar)
    satirlar.append(f"\nToplam gezme süresi yaklaşık **{toplam_sure // 60} saat "
                    f"{toplam_sure % 60} dakika**; bunları günlere bölmemi isterseniz "
                    "_\"3 günlük plan çıkar\"_ diyebilirsiniz.")
    if ctx.profile.accessibility:
        satirlar.append("\n_Erişilebilirlik sert filtresi uygulandı._")

    return AgentReply(
        agent="itinerary_architect", text="\n".join(satirlar),
        facts=[veri, {"toplam_sure_dk": toplam_sure, "saat": toplam_sure // 60,
                      "dakika": toplam_sure % 60}],
        sources=[_src("Pusula İçerik Editörlüğü — POI seti", "T0")],
        tools_used=["get_pois"])


def h_itinerary_architect(ctx: Ctx) -> AgentReply:
    if not ctx.dest_key:
        return _need_destination("itinerary_architect", "Gezi planı")

    # "Prag'da mutlaka görülmesi gereken yerler neler?" bir plan isteği değil, durak
    # listesi isteğidir. Gün sayısı verilmemişse tam plan kurmak soruyu aşar.
    if ctx.days is None and _LISTE_RE.search(ctx.message):
        return _h_poi_listesi(ctx)

    days = ctx.days or 3
    hava = T.get_weather(ctx.dest_key, ctx.start_date, min(7, days))
    yagmurlu = [i + 1 for i, g in enumerate(hava.get("tahmin", [])) if g.get("kapali_mekan_onerilir")]

    itin = build_itinerary(ctx.dest_key, days, ctx.profile, ctx.start_date,
                           rainy_days=yagmurlu, variant=ctx.variant)
    rapor = validate_itinerary(itin, ctx.profile)

    # Doğrulayıcı hata bulduysa planı bir kez daha, kısıtları gevşeterek kurmayı denemeyiz:
    # deterministik kurucu zaten kısıtlara uyar. Hata çıkması LLM planında anlamlıdır.
    itin.sources = [_src("Pusula İçerik Editörlüğü — POI seti", "T0")]

    onek = ""
    if ctx.variant:
        onek = (f"Anladım, farklı bir kurgu hazırladım — bu **{ctx.variant + 1}. plan**. "
                "Duraklar başka bir semt sırasıyla dağıtıldı ve öğünler değiştirildi.\n\n")
    satirlar = [onek + f"**{itin.destination} — {len(itin.days)} günlük plan** "
                f"({ctx.profile.pace or 'dengeli'} tempo, {ctx.profile.budget_band or 'orta'} bütçe)\n"]
    for gun in itin.days:
        tarih = f" · {gun.date.strftime('%d.%m.%Y')}" if gun.date else ""
        satirlar.append(f"**{gun.day}. Gün{tarih} — {gun.theme}**")
        for s in gun.slots:
            zaman = {"sabah": "Sabah", "ogle": "Öğle", "ikindi": "İkindi",
                     "aksam": "Akşam", "gece": "Gece"}[s.time]
            ucret = f" · {tl(s.cost_try)} TRY" if s.cost_try else " · ücretsiz"
            yol = f" · {s.travel_min_from_prev} dk yol" if s.travel_min_from_prev else ""
            satirlar.append(f"- _{zaman}_ **{s.title}** ({s.duration_min} dk{ucret}{yol})"
                            + (f"\n  {s.detail}" if s.detail else ""))
        satirlar.append(f"  → Gün toplamı: {tl(gun.total_cost)} TRY, "
                        f"{gun.total_minutes // 60} sa {gun.total_minutes % 60} dk")

    satirlar.append(f"\n**Plan toplamı (aktivite + öğün): {tl(itin.total_cost_try)} TRY** "
                    f"— konaklama ve ulaşım hariç.")
    if yagmurlu:
        satirlar.append(f"🌧️ {', '.join(str(g) for g in yagmurlu)}. gün(ler) için yağış bekleniyor; "
                        "o günlere kapalı mekânlar önceliklendirildi.")
    uyarilar = [i for i in rapor.issues if i.severity in ("warning", "info")]
    if uyarilar:
        satirlar.append("\n**Plan doğrulayıcı notları:** " +
                        " ".join(f"({i.severity}) {i.message}" for i in uyarilar[:3]))
    if itin.notes:
        satirlar.append("\n" + "\n".join(f"- {n}" for n in itin.notes[:3]))
    satirlar.append("\n_Planı değiştirmek için \"2. günü sakinleştir\" gibi yazabilirsiniz._")

    # Türetilmiş toplamlar olgu paketine AÇIKÇA yazılır. `total_cost`/`total_minutes`
    # birer property olduğu için `model_dump`'a girmez; girmezse groundedness denetimi
    # bizim kendi hesapladığımız gün toplamını "uydurma sayı" sanır.
    ozet = {
        "gun_toplamlari": [
            {"gun": d.day, "toplam_try": d.total_cost, "toplam_dakika": d.total_minutes,
             "saat": d.total_minutes // 60, "dakika": d.total_minutes % 60}
            for d in itin.days
        ],
        "plan_toplami_try": itin.total_cost_try,
        "gun_sayisi": len(itin.days),
    }

    return AgentReply(
        agent="itinerary_architect", text="\n".join(satirlar),
        facts=[itin.model_dump(mode="json"), ozet, hava,
               {"dogrulama": [i.model_dump(mode="json") for i in rapor.issues]}],
        sources=itin.sources, itinerary=itin,
        tools_used=["get_weather", "get_pois", "estimate_travel_time",
                    "build_itinerary", "validate_itinerary"],
    )


# ─────────────────────────────────────────────────────────────────────
# 3. logistics_agent
# ─────────────────────────────────────────────────────────────────────
def h_logistics_agent(ctx: Ctx) -> AgentReply:
    if not ctx.dest_key:
        return _need_destination("logistics_agent", "Konaklama ve ulaşım")
    nights = ctx.days or 4
    band = ctx.profile.budget_band or "orta"
    ucus = T.search_flights(ctx.dest_key, ctx.start_date, passengers=ctx.travelers)
    otel = T.search_hotels(ctx.dest_key, ctx.start_date, nights, band, ctx.travelers,
                           accessible=bool(ctx.profile.accessibility))
    pratik = T.get_practical_facts(ctx.dest_key)

    satirlar = [f"**{ucus['varis']} — ulaşım ve konaklama** ({band} bandı, {nights} gece, "
                f"{ctx.travelers} kişi)\n", "**Uçuş seçenekleri** (IST çıkışlı):"]
    for s in ucus["secenekler"]:
        aktarma = "direkt" if not s["aktarma"] else f"{s['aktarma']} aktarma"
        satirlar.append(f"- **{s['etiket']}** — {s['ucus_suresi_saat']} sa, {aktarma}, "
                        f"**{tl(s['toplam_ucret_try'])} TRY**")
    satirlar.append("\n**Konaklama seçenekleri:**")
    for s in otel["secenekler"]:
        eris = " · erişilebilir oda ✓" if s["erisilebilir_oda"] else ""
        satirlar.append(f"- **{s['tip']}** — {s['semt']} · gecelik {tl(s['gecelik_try'])} TRY · "
                        f"toplam **{tl(s['toplam_try'])} TRY**{eris}\n  {s['semt_notu']}")
    if pratik.get("ulasim_karti"):
        satirlar.append(f"\n**Şehir içi ulaşım:** {pratik['ulasim_karti']}")
    toplam = ucus["en_dusuk_try"] + otel["en_dusuk_toplam_try"]
    satirlar.append(f"\n**En ekonomik kombinasyon: {tl(toplam)} TRY** (uçuş + konaklama).")
    satirlar.append("\n⚠️ Uçuş ve konaklama kayıtları **simüle envanterden** gelir; işletme adı "
                    "üretilmez. Üretimde Amadeus/Hotelbeds entegrasyonu aynı araç imzasıyla bağlanır.")

    if ctx.profile.budget_total and toplam > ctx.profile.budget_total:
        satirlar.append(f"\n🔴 **Bütçe uyarısı:** bu kombinasyon bütçenizi "
                        f"({tl(ctx.profile.budget_total)} TRY) **{tl(toplam - ctx.profile.budget_total)} TRY** aşıyor. "
                        "Daha ekonomik banda geçmek veya gece sayısını azaltmak gerekir.")

    return AgentReply(agent="logistics_agent", text="\n".join(satirlar),
                      facts=[ucus, otel, pratik,
                             {"en_ekonomik_kombinasyon_try": toplam,
                              "butce_asimi_try": (round(toplam - ctx.profile.budget_total)
                                                  if ctx.profile.budget_total and
                                                  toplam > ctx.profile.budget_total else 0)}],
                      sources=[_src_of(ucus), _src_of(otel)],
                      tools_used=["search_flights", "search_hotels", "get_practical_facts"])


# ─────────────────────────────────────────────────────────────────────
# 4. culinary_guide
# ─────────────────────────────────────────────────────────────────────
def h_culinary_guide(ctx: Ctx) -> AgentReply:
    if not ctx.dest_key:
        return _need_destination("culinary_guide", "Yeme-içme önerileri")
    diyet = list(ctx.profile.dietary)
    veri = T.search_restaurants(ctx.dest_key, diyet, ctx.profile.budget_band or "orta")

    satirlar = [f"**{veri['destinasyon']} — yöresel lezzetler**"
                + (f" ({', '.join(diyet)} filtresi uygulandı)" if diyet else "") + "\n"]
    if veri.get("diyet_uyarisi"):
        satirlar.append(f"⚠️ {veri['diyet_uyarisi']}\n")
    for d in veri["yoresel_lezzetler"]:
        etiket = f" _{', '.join(d['diyet'])}_" if d.get("diyet") else ""
        satirlar.append(f"- **{d['ad']}** — {d['aciklama']}{etiket}")
    if veri["mekan_onerileri"]:
        satirlar.append("\n**Nerede yenir:**")
        for m in veri["mekan_onerileri"]:
            satirlar.append(f"- {m['semt']} — **{m['yer_tipi']}** ({m['segment']}, "
                            f"kişi başı ~{tl(m['kisi_basi_try'])} TRY) · {m['aciklama']}")
    if veri.get("yemek_saatleri"):
        satirlar.append(f"\n**Yemek saatleri:** {veri['yemek_saatleri']}")
    for ip in veri.get("ipuclari", [])[:2]:
        satirlar.append(f"- 💡 {ip}")
    if diyet and veri.get("vegan_notu"):
        satirlar.append(f"\n{veri['vegan_notu']}")
    satirlar.append("\n_Yemekler ve mutfak kültürü küratörlü içerikten; mekân önerileri simüle "
                    "envanterden yer tipi + semt olarak gelir (uydurma işletme adı kullanmıyoruz)._")

    return AgentReply(agent="culinary_guide", text="\n".join(satirlar), facts=[veri],
                      sources=[_src_of(veri)],
                      tools_used=["search_restaurants", "get_local_dishes"])


# ─────────────────────────────────────────────────────────────────────
# 5. culture_curator
# ─────────────────────────────────────────────────────────────────────
def h_culture_curator(ctx: Ctx) -> AgentReply:
    if not ctx.dest_key:
        return _need_destination("culture_curator", "Kültürel bilgiler")
    c = T.get_culture_notes(ctx.dest_key)
    if c.get("hata"):
        return AgentReply(agent="culture_curator", text=c["hata"], facts=[c])

    satirlar = [f"**{c['destinasyon']} — kültür ve görgü kuralları**\n"]
    for baslik, alan in [("Selamlaşma", "selamlasma"), ("Kıyafet", "kiyafet"),
                         ("Bahşiş", "bahsis"), ("Pazarlık", "pazarlik"),
                         ("Dini hassasiyet", "dini_hassasiyet"), ("Güvenlik", "guvenlik")]:
        if c.get(alan):
            satirlar.append(f"- **{baslik}:** {c[alan]}")
    if c.get("gorgu_kurallari"):
        satirlar.append("\n**Bilinmesi iyi olanlar:**")
        satirlar += [f"- {g}" for g in c["gorgu_kurallari"]]
    if c.get("kacinilmasi_gerekenler"):
        satirlar.append("\n**Kaçınılması gerekenler:**")
        satirlar += [f"- ⛔ {k}" for k in c["kacinilmasi_gerekenler"]]
    if c.get("dil_ipuclari"):
        satirlar.append("\n**Birkaç kelime:** " +
                        " · ".join(f"_{tr}_ → **{yerel}**" for tr, yerel in
                                   list(c["dil_ipuclari"].items())[:5]))
    satirlar.append("\n_Kültürel bilgiler yalnızca küratörlü içerikten sunulur (T0); "
                    "modelin kendi bilgisi bu alanda kullanılmaz._")

    return AgentReply(agent="culture_curator", text="\n".join(satirlar), facts=[c],
                      sources=[_src("Pusula İçerik Editörlüğü — Kültür Rehberi", "T0")],
                      tools_used=["get_culture_notes"])


# ─────────────────────────────────────────────────────────────────────
# 6. practical_desk
# ─────────────────────────────────────────────────────────────────────
_SAAT_RE = re.compile(r"(saat fark|zaman fark|kaç saat ileri|kaç saat geri|jetlag|jet lag)", re.I)
_DOVIZ_RE = re.compile(r"(döviz|kur|euro|dolar|yen|para birimi|kaç (tl|lira)|çevir)", re.I)
_HAVA_RE = re.compile(r"(hava|sıcaklık|yağmur|yağış|ne giy|kaç derece|iklim)", re.I)
_PRATIK_RE = re.compile(r"(priz|voltaj|adaptör|acil|numara|musluk|su iç|internet|sim|"
                        r"ulaşım kart|elektrik)", re.I)


def h_practical_desk(ctx: Ctx) -> AgentReply:
    if not ctx.dest_key:
        return _need_destination("practical_desk", "Pratik seyahat bilgisi")
    msg = ctx.message
    satirlar: list[str] = []
    facts: list[Any] = []
    kullanilan: list[str] = []
    kaynaklar: list[Source] = []

    istenen = {
        "hava": bool(_HAVA_RE.search(msg)), "saat": bool(_SAAT_RE.search(msg)),
        "doviz": bool(_DOVIZ_RE.search(msg)), "pratik": bool(_PRATIK_RE.search(msg)),
    }
    if not any(istenen.values()):  # niyet net değilse pratik özeti ver
        istenen = {"hava": True, "saat": True, "doviz": True, "pratik": True}

    if istenen["hava"]:
        w = T.get_weather(ctx.dest_key, ctx.start_date, min(7, ctx.days or 3))
        facts.append(w)
        kullanilan.append("get_weather")
        kaynaklar.append(_src_of(w))
        satirlar.append(f"**{w['destinasyon']} — hava durumu**")
        for g in w["tahmin"][:5]:
            satirlar.append(f"- {g['tarih']}: {g['durum']}, {g['en_dusuk_c']}–{g['en_yuksek_c']} °C, "
                            f"yağış olasılığı %{g['yagis_olasiligi_yuzde']}")
        satirlar.append(f"\n**Ne giyilir:** {w['ne_giyilir']}")
        if w["yagmurlu_gun_sayisi"]:
            satirlar.append(f"☔ {w['yagmurlu_gun_sayisi']} gün için yağış bekleniyor; "
                            "kapalı mekân alternatifi planlamakta fayda var.")
        satirlar.append("")

    if istenen["saat"]:
        tzd = T.get_timezone_diff(ctx.dest_key)
        facts.append(tzd)
        kullanilan.append("get_timezone_diff")
        if not tzd.get("hata"):
            satirlar.append(f"**Saat farkı:** {tzd['destinasyon']} Türkiye'ye göre "
                            f"**{tzd['ifade']}** ({tzd['saat_dilimi']}). "
                            f"Şu an orada saat **{tzd['yerel_saat']}**, Türkiye'de {tzd['kalkis_yerel_saat']}.")
            satirlar.append(f"- {tzd['jetlag_notu']}")
            satirlar.append(f"- _{tzd['dst_notu']} Bu hesap IANA tzdata ile yerel yapılır, "
                            "dış API kullanılmaz._\n")

    if istenen["doviz"]:
        kod = T.destination_currency(ctx.dest_key)
        if kod and kod != "TRY":
            fx = T.get_fx(kod, "TRY", 1)
            facts.append(fx)
            kullanilan.append("get_fx")
            kaynaklar.append(_src_of(fx))
            if not fx.get("hata"):
                satirlar.append(f"**Döviz:** 1 {kod} ≈ **{fx['kur_gosterim']} TRY** ({fx['tarih']}). "
                                f"{fx['not']}\n")

    if istenen["pratik"]:
        pr = T.get_practical_facts(ctx.dest_key)
        facts.append(pr)
        kullanilan.append("get_practical_facts")
        if not pr.get("hata"):
            kaynaklar.append(_src_of(pr))
            el = pr.get("elektrik", {})
            satirlar.append("**Pratik bilgiler**")
            satirlar.append(f"- **Elektrik:** {el.get('voltaj','')} {el.get('frekans','')}, "
                            f"priz tipi {', '.join(el.get('priz_tipi', []))}. {el.get('not','')}")
            acil = ", ".join(f"{k}: **{v}**" for k, v in pr.get("acil_numaralar", {}).items()
                             if k != "not")
            satirlar.append(f"- **Acil numaralar:** {acil}")
            satirlar.append(f"- **Musluk suyu:** {pr.get('musluk_suyu','')}")
            satirlar.append(f"- **İnternet:** {pr.get('internet','')}")
            if pr.get("ulasim_karti"):
                satirlar.append(f"- **Ulaşım kartı:** {pr['ulasim_karti']}")

    return AgentReply(agent="practical_desk", text="\n".join(satirlar).strip(), facts=facts,
                      sources=kaynaklar[:3], tools_used=kullanilan)


# ─────────────────────────────────────────────────────────────────────
# 7. documents_officer — YÜKSEK RİSK
# ─────────────────────────────────────────────────────────────────────
def h_documents_officer(ctx: Ctx) -> AgentReply:
    hedef = ctx.dest_key or ctx.message
    v = T.get_visa_requirements(hedef)

    if v.get("kapsam_disi") or v.get("bulunamadi"):
        return AgentReply(agent="documents_officer", text=v["mesaj"], facts=[v],
                          sources=[_src("T.C. Dışişleri Bakanlığı / IATA Timatic", "T1")],
                          tools_used=["get_visa_requirements"], high_risk=True)

    gerekli = v.get("vize_gerekli")
    satirlar = [f"**{v['ulke']} — vize ve giriş koşulları** (T.C. vatandaşı, umuma mahsus pasaport)\n"]
    satirlar.append(f"- **Vize:** {'gerekli' if gerekli else 'gerekmiyor'}"
                    + (f" — {v['vize_turu']}" if v.get("vize_turu") else ""))
    if v.get("kalis_suresi"):
        satirlar.append(f"- **Kalış süresi:** {v['kalis_suresi']}")
    if v.get("pasaport_gecerliligi"):
        satirlar.append(f"- **Pasaport geçerliliği:** {v['pasaport_gecerliligi']}")
    if v.get("basvuru_notu"):
        satirlar.append(f"- **Başvuru:** {v['basvuru_notu']}")
    if v.get("tipik_belgeler"):
        satirlar.append("\n**Tipik olarak istenen belgeler:**")
        satirlar += [f"- {b}" for b in v["tipik_belgeler"]]
    if v.get("ek_kosullar"):
        satirlar.append("\n**Ek koşullar:**")
        satirlar += [f"- {k}" for k in v["ek_kosullar"]]
    if v.get("diger_pasaport_turleri"):
        farkli = [f"{k.replace('_', ' ')}: {'vizeli' if d.get('vize_gerekli') else 'vizesiz'}"
                  for k, d in v["diger_pasaport_turleri"].items()]
        if farkli:
            satirlar.append("\n**Diğer pasaport türleri:** " + " · ".join(farkli))

    satirlar.append(f"\n📌 **Kaynak:** {v['_kaynak']['baslik']} · "
                    f"**Geçerlilik:** {v.get('gecerlilik_tarihi', '—')}")

    return AgentReply(agent="documents_officer", text="\n".join(satirlar), facts=[v],
                      sources=[_src("T.C. Dışişleri Bakanlığı / IATA Timatic", "T1",
                                    v.get("gecerlilik_tarihi"))],
                      tools_used=["get_visa_requirements"], high_risk=True,
                      disclaimer=v.get("feragat"))


# ─────────────────────────────────────────────────────────────────────
# 8. faq_specialist
# ─────────────────────────────────────────────────────────────────────
# SSS alaka eşiği. Ölçüm (30 Tem): alakalı sorular 0,40–1,19 arası skor alıyor;
# anlamsız girdiler ("asdfgh", "merhaba", "ne alaka") 0,05–0,10 arasında kalıyor.
# Eşik olmadan sistem çöp girdiye EMİN bir SSS cevabı veriyordu — kullanıcı bunu
# canlı olarak yaşadı ("bu 3ünden başka yok mu" -> iade politikası).
FAQ_MIN_SCORE = 0.30
FAQ_MIN_SCORE_FALLBACK = 0.38   # SSS yalnızca "başka eşleşme yok" diye seçildiyse daha katı


def h_faq_specialist(ctx: Ctx) -> AgentReply:
    veri = T.search_faq(ctx.message, limit=3)
    sonuclar = veri["sonuclar"]

    esik = FAQ_MIN_SCORE_FALLBACK if ctx.intent == "fallback" else FAQ_MIN_SCORE
    # Hiç sonuç yoksa da ("?", "asdfgh") netleştirmeye düşeriz: SSS'in "bulamadım"
    # metni, kullanıcı zaten bir SSS sormadıysa yanıltıcı olur.
    if ctx.intent == "fallback" and (not sonuclar or sonuclar[0]["skor"] < esik):
        acik = h_concierge(Ctx(message=ctx.message, profile=ctx.profile, user_id=ctx.user_id,
                               session_id=ctx.session_id, dest_key=ctx.dest_key,
                               history=ctx.history, intent="belirsiz"))
        acik.facts.append({"faq_en_yakin": sonuclar[0]["soru"] if sonuclar else None,
                           "skor": sonuclar[0]["skor"] if sonuclar else 0.0,
                           "esik": esik, "karar": "eşik altında — cevap üretilmedi"})
        return acik
    if sonuclar and sonuclar[0]["skor"] < esik:
        # Zayıf eşleşme = eşleşme yok. Anlamadığımızı söyleyip yönlendiriyoruz.
        acik = h_concierge(Ctx(message=ctx.message, profile=ctx.profile, user_id=ctx.user_id,
                               session_id=ctx.session_id, dest_key=ctx.dest_key,
                               history=ctx.history, intent="belirsiz"))
        acik.facts.append({"faq_en_yakin": sonuclar[0]["soru"], "skor": sonuclar[0]["skor"],
                           "esik": esik, "karar": "eşik altında — cevap üretilmedi"})
        return acik

    if not sonuclar:
        return AgentReply(
            agent="faq_specialist",
            text=("Bu soruya bilgi tabanımızda doğrulanmış bir karşılık bulamadım. Uydurma cevap "
                  "vermektense konuyu bir müşteri temsilcisine aktarmayı öneriyorum.\n\n"
                  f"SSS kategorilerimiz: {', '.join(kb.faq_categories[:8])}…"),
            facts=[veri], tools_used=["search_faq"])

    ana = sonuclar[0]
    satirlar = [f"**{ana['soru']}**\n", ana["cevap"]]
    if ana.get("kategori"):
        satirlar.append(f"\n📂 _{ana['kategori']}_ · kaynak: {ana.get('kaynak', '—')}"
                        + (f" · geçerlilik: {ana['gecerlilik']}" if ana.get("gecerlilik") else ""))
    digerleri = [s for s in sonuclar[1:] if s["skor"] > 0.12]
    if digerleri:
        satirlar.append("\n**İlgili olabilir:**")
        satirlar += [f"- {s['soru']}" for s in digerleri]

    return AgentReply(agent="faq_specialist", text="\n".join(satirlar), facts=[veri],
                      sources=[_src(ana.get("kaynak") or "Pusula SSS", ana.get("kademe", "T0"),
                                    ana.get("gecerlilik"))],
                      tools_used=["search_faq"], high_risk=bool(ana.get("yuksek_risk")))


# ─────────────────────────────────────────────────────────────────────
# 9. preference_keeper — Çıktı 7'nin kalbi
# ─────────────────────────────────────────────────────────────────────
_NEDEN_RE = re.compile(r"(neden bu öneri|neden bunu|niye bu|nasıl (hesapla|sırala)|"
                       r"skor|gerekçe)", re.IGNORECASE)


def h_preference_keeper(ctx: Ctx) -> AgentReply:
    from . import kvkk

    if _NEDEN_RE.search(ctx.message):
        oneriler, _ = pref.recommend(ctx.profile, month=ctx.month, nights=ctx.days or 4,
                                     limit=1, user_id=ctx.user_id)
        if oneriler:
            aciklama = pref.explain(oneriler[0])
            f = pref.formula()
            return AgentReply(
                agent="preference_keeper",
                text=aciklama + f"\n\n**Kullanılan formül:**\n`{f['formul']}`",
                facts=[oneriler[0].model_dump(mode="json"), f],
                suggestions=oneriler, tools_used=["search_destinations", "explain_recommendation"])

    cikarim = pref.extract_from_text(ctx.message)
    cikarim.pop("_mentioned_destination", None)
    if not cikarim:
        p = ctx.profile
        if p.is_empty():
            return AgentReply(
                agent="preference_keeper",
                text=("Henüz kayıtlı bir tercihiniz yok. Bütçe bandınızı, seyahat stilinizi ve "
                      "temponuzu yazarsanız (ör. _\"orta bütçe, kültür ağırlıklı, sakin tempo\"_) "
                      "önerileri buna göre kişiselleştiririm.\n\n"
                      "Hazır arketiplerden biriyle de başlayabiliriz: "
                      + ", ".join(v["baslik"] for v in pref.PERSONAS.values())),
                profile=p, tools_used=["load_profile"])
        return AgentReply(agent="preference_keeper", text=_profil_ozeti(p, ctx.user_id),
                          profile=p, facts=[p.model_dump(mode="json")], tools_used=["load_profile"])

    yeni = pref.merge(ctx.profile, cikarim)
    ok, mesaj = pref.save(ctx.user_id, yeni)
    consent = kvkk.get_consent(ctx.user_id)

    ogrenilen = ", ".join(f"**{k}**: {v}" for k, v in cikarim.items() if not k.startswith("_"))
    satirlar = [f"Şunları not ettim → {ogrenilen}\n"]
    if not ok:
        satirlar.append(f"ℹ️ {mesaj} Öneriler yalnızca bu mesaja göre kişiselleştirildi; "
                        "kalıcı saklama için **Verilerim** panelinden kişiselleştirme rızası "
                        "verebilirsiniz.")
    else:
        satirlar.append(f"✅ {mesaj}")
        if cikarim.get("dietary") or cikarim.get("accessibility"):
            if consent.sensitive_data:
                satirlar.append("Diyet/erişilebilirlik bilgisi **KVKK m.6 özel nitelikli veri** "
                                "sayılır; açık rızanız olduğu için ayrı alanda saklandı.")
            else:
                satirlar.append("⚠️ Diyet/erişilebilirlik bilgisi **KVKK m.6 özel nitelikli veri** "
                                "sayılır. Açık rıza vermediğiniz için **saklanmadı**; yalnızca bu "
                                "yanıtta sert filtre olarak kullanılıp unutuluyor.")
    satirlar.append("\n" + _profil_ozeti(yeni, ctx.user_id))

    return AgentReply(agent="preference_keeper", text="\n".join(satirlar), profile=yeni,
                      facts=[{"cikarim": cikarim}, yeni.model_dump(mode="json")],
                      tools_used=["extract_preferences", "save_profile"])


def _profil_ozeti(p: TravelerProfile, user_id: str) -> str:
    alanlar = []
    for etiket, deger in [("Bütçe bandı", p.budget_band), ("Tempo", p.pace),
                          ("İklim", p.climate), ("Grup", p.group),
                          ("Bütçe üst sınırı", f"{tl(p.budget_total)} TRY" if p.budget_total else None)]:
        if deger:
            alanlar.append(f"- {etiket}: **{deger}**")
    if p.styles:
        alanlar.append(f"- Stiller: **{', '.join(p.styles)}**")
    if p.dietary:
        alanlar.append(f"- Diyet: **{', '.join(p.dietary)}** _(özel nitelikli)_")
    if p.accessibility:
        alanlar.append(f"- Erişilebilirlik: **{', '.join(p.accessibility)}** _(özel nitelikli)_")
    if not alanlar:
        return "_Profiliniz şu an boş._"
    return "**Güncel tercih profiliniz:**\n" + "\n".join(alanlar) + \
           "\n\n_Tercih panelinden düzeltebilir, **Verilerim** panelinden silebilirsiniz._"


# ─────────────────────────────────────────────────────────────────────
# 10. concierge — karşılama, kimlik, anlaşılmayan girdi, düzeltme
# ─────────────────────────────────────────────────────────────────────
# Bu ajan sonradan eklendi ve gerçek kullanımda ortaya çıkan en ciddi açığı kapatıyor:
# sistem her mesaja EMİN bir cevap veriyordu. "merhaba", "asdfgh", "ne alaka ondan
# bahsetmedim ki" gibi girdiler alakasız bir SSS kaydına düşüyor ve kullanıcıya sanki
# sorusunun cevabıymış gibi sunuluyordu. Kaynağa dayalı bir asistan için bu, olgu
# uydurmaktan daha kötüdür: sistem yanıldığını bilmiyor.
#
# Kural: **ne sorulduğu anlaşılmadıysa cevap üretilmez, anlaşılmadığı söylenir.**

YETENEKLER = [
    ("🧭 Destinasyon önerisi", "bütçenize, tarihinize ve tercihlerinize göre skorlanmış öneriler"),
    ("🗺️ Günlük gezi planı", "semte göre kümelenmiş, doğrulayıcıdan geçmiş rota"),
    ("🏨 Konaklama ve ulaşım", "uçuş ve konaklama seçenekleri, bütçe filtresiyle"),
    ("🍽️ Yöresel lezzetler", "mutfak kültürü ve diyetinize uygun seçenekler"),
    ("📚 Kültür ve görgü kuralları", "kıyafet, bahşiş, yerel hassasiyetler"),
    ("🌦️ Hava, saat farkı, döviz", "pratik seyahat bilgileri"),
    ("🛂 Vize ve pasaport", "resmî kaynaklı bilgilendirme (feragatle)"),
    ("❓ Sıkça sorulan sorular", "bagaj, iptal, iade, sigorta, ödeme"),
]


def _yetenek_listesi() -> str:
    return "\n".join(f"- **{ad}** — {aciklama}" for ad, aciklama in YETENEKLER)


def _ornek_sorular(ctx: Ctx) -> str:
    dest = ctx.destination or "Roma"
    return (f"- _\"{dest}'da 3 günlük gezi planı çıkar\"_\n"
            f"- _\"Eylülde 40.000 TL bütçeyle nereye gidebilirim?\"_\n"
            f"- _\"{dest}'da ne yenir? Vejetaryenim\"_\n"
            f"- _\"İtalya için vize gerekiyor mu?\"_")


def h_concierge(ctx: Ctx) -> AgentReply:
    """Karşılama ajanı — olgu paketi sarmalayıcısıyla.

    Yanıtlarda destinasyon sayısı ve örnek tutar gibi sayılar geçiyor; bunlar da
    groundedness denetiminden geçmek zorunda. Metinde ne yazıyorsak olgu paketinde
    de bulunmalı — kuralın istisnası yok, "bizim kendi metnimiz" diye muaf tutulmuyor.
    """
    reply = _h_concierge(ctx)
    reply.facts.append({
        "destinasyon_sayisi": len(kb.destinations),
        "destinasyonlar": [d["name"] for d in kb.destinations],
        "yetenekler": [ad for ad, _ in YETENEKLER],
        "ornek_sorular": _ornek_sorular(ctx),
        "niyet": ctx.intent or "belirsiz",
    })
    return reply


def _h_concierge(ctx: Ctx) -> AgentReply:
    niyet = ctx.intent or "belirsiz"
    kb.load()

    if niyet == "selam":
        ad_listesi = ", ".join(d["name"] for d in kb.destinations[:6])
        metin = (
            "Merhaba! Ben **Pusula AI**, seyahat asistanınız. 👋\n\n"
            "Şunlarda yardımcı olabilirim:\n" + _yetenek_listesi() +
            f"\n\nKüratörlü bilgi tabanımda şu an **{len(kb.destinations)} destinasyon** var: "
            f"{ad_listesi} ve {len(kb.destinations) - 6} tanesi daha.\n\n"
            "Nereden başlayalım?\n" + _ornek_sorular(ctx))
        return AgentReply(agent="concierge", text=metin, tools_used=["greet"])

    if niyet == "tesekkur":
        return AgentReply(agent="concierge", tools_used=["closing"], text=(
            "Rica ederim, iyi yolculuklar! ✈️\n\n"
            "Aklınıza sonradan bir şey takılırsa buradayım — planı revize edebilir, "
            "konaklama bakabilir ya da vize koşullarını kontrol edebilirim.\n\n"
            "_Tercihlerinizi ve verilerinizi sağdaki **Verilerim** panelinden istediğiniz an "
            "görebilir veya silebilirsiniz._"))

    if niyet == "kimlik":
        return AgentReply(agent="concierge", tools_used=["identity"], text=(
            "Ben **Pusula AI**'yım — seyahat planlaması için tasarlanmış bir yapay zekâ "
            "asistanı.\n\n"
            "Beni diğer sohbet asistanlarından ayıran şey şu: **olgu uydurmam.** "
            "Söylediğim her fiyat, saat, mesafe ve vize kuralı ya küratörlü bilgi tabanımdan "
            "ya da bağlı olduğum araçlardan gelir; dil modeli yalnızca bu bilgiyi cümleye "
            "döker. Kaynağı olmayan bir şey sorduğunuzda **bilmiyorum** derim.\n\n"
            "Ayrıca verilerinizi rızanız olmadan saklamam; ne sakladığımı **Verilerim** "
            "panelinden görebilir, tek tıkla silebilirsiniz.\n\n"
            "Yapabildiklerim:\n" + _yetenek_listesi()))

    if niyet == "yetenek":
        return AgentReply(agent="concierge", tools_used=["capabilities"], text=(
            "Şunları yapabilirim:\n" + _yetenek_listesi() +
            "\n\nHer yanıtın altında **hangi uzmanın çalıştığını, hangi araçları kullandığını "
            "ve kaynağın güven kademesini** görebilirsiniz.\n\n"
            "Denemek için:\n" + _ornek_sorular(ctx)))

    if niyet == "duzeltme":
        son = ctx.history[-1]["content"][:90] if ctx.history else None
        metin = ["Haklısınız, sorunuzu yanlış anlamışım — özür dilerim. 🙏\n"]
        if son:
            metin.append(f"Son olarak _\"{son}\"_ mesajınıza cevap vermeye çalışmıştım.\n")
        metin.append("Doğru anlayabilmem için biraz daha açar mısınız? Örneğin:\n"
                     + _ornek_sorular(ctx) +
                     "\n\n_Uydurma bir cevap vermektense sormayı tercih ediyorum._")
        return AgentReply(agent="concierge", text="\n".join(metin), tools_used=["repair"])

    if niyet == "kapsam_disi_yer":
        yer = ctx.unknown_place or "Sorduğunuz yer"
        adlar = ", ".join(d["name"] for d in kb.destinations)
        return AgentReply(agent="concierge", tools_used=["scope_check"], text=(
            f"**{yer}** şu an küratörlü bilgi tabanımın dışında. Doğrulanmış verim olmadığı "
            f"için o destinasyon hakkında bilgi üretmiyorum — uydurmaktansa söylemeyi "
            f"tercih ederim.\n\n"
            f"Elimde derinlemesine içerik bulunan **{len(kb.destinations)} destinasyon** var:\n"
            f"{adlar}.\n\n"
            "Bunlardan biriyle ilgilenir misiniz, yoksa tercihlerinizi söyleyip size uygun "
            "olanı birlikte mi bulalım?"))

    # niyet == "belirsiz" — anlaşılmayan girdi
    metin = ["Bu isteği tam olarak anlayamadım. Yanlış bir cevap vermektense sormayı "
             "tercih ediyorum.\n"]
    if ctx.dest_key:
        metin.append(f"**{ctx.destination}** hakkında konuşuyorduk — şunlardan birini mi "
                     "istemiştiniz?\n"
                     f"- _\"{ctx.destination}'da gezi planı çıkar\"_\n"
                     f"- _\"{ctx.destination}'da ne yenir\"_\n"
                     f"- _\"{ctx.destination}'da hava nasıl\"_\n"
                     f"- _\"{ctx.destination} için vize gerekiyor mu\"_")
    else:
        metin.append("Şunlarda yardımcı olabilirim:\n" + _yetenek_listesi() +
                     "\n\nÖrnek:\n" + _ornek_sorular(ctx))
    return AgentReply(agent="concierge", text="\n".join(metin), tools_used=["clarify"])


# ─────────────────────────────────────────────────────────────────────
# Ajan tanımları
# ─────────────────────────────────────────────────────────────────────
@dataclass
class AgentSpec:
    key: str
    name: str
    role: str
    description: str
    instructions: list[str]
    tools: list[str]
    tiers: set[str]
    handler: Callable[[Ctx], AgentReply]
    model_tier: str = "specialist"
    output_schema: Optional[type] = None
    high_risk: bool = False
    keywords: list[str] = field(default_factory=list)


_ORTAK_KURALLAR = [
    "Türkçe, sıcak ama profesyonel bir tonda yanıt ver.",
    "ASLA fiyat, saat, mesafe, vize kuralı veya mekân uydurma. Her olgusal bilgi araç "
    "çıktısından veya bilgi tabanından gelmelidir.",
    "Araçtan gelmeyen bir bilgi soruluyorsa 'doğrulanmış kaydımız yok' de ve uydurma.",
    "Sayıları araç çıktısındaki değerlerle birebir aynı yaz; yuvarlama veya tahmin ekleme.",
    "Yanıtın sonunda kullandığın kaynağı ve varsa geçerlilik tarihini belirt.",
]

SPECS: dict[str, AgentSpec] = {
    "destination_scout": AgentSpec(
        key="destination_scout", name="Destinasyon Kâşifi", role="Destinasyon önerisi uzmanı",
        description="Bütçe, tarih ve tercihlere uygun destinasyonları skorlayıp sıralar.",
        instructions=[
            "Kullanıcının bütçesine, tarihine ve tercihlerine uyan destinasyonları öner.",
            "search_destinations aracını çağır; skor kırılımını ve elenenleri mutlaka aktar.",
            "Her öneri için estimate_trip_cost ile tahmini maliyeti ver.",
            "Sert filtrelerle elenen destinasyonları sebebiyle birlikte söyle — şeffaflık güven verir.",
        ] + _ORTAK_KURALLAR,
        tools=["search_destinations", "estimate_trip_cost", "get_seasonality"],
        tiers={"T0"}, handler=h_destination_scout,
        keywords=["nereye", "destinasyon", "öner", "tavsiye", "gidebilirim", "rota önerisi",
                  "hangi ülke", "hangi şehir", "tatil"],
    ),
    "itinerary_architect": AgentSpec(
        key="itinerary_architect", name="Plan Mimarı", role="Gezi planı mimarı",
        description="Çok günlük rota ve günlük gezi planı üretir; planı doğrulayıcıdan geçirir.",
        instructions=[
            "Çok günlük gezi planını build_itinerary aracıyla kur, sonra validate_itinerary ile denetle.",
            "Doğrulayıcı 'error' döndürürse planı düzelt ve yeniden denetle.",
            "Yalnızca get_pois'ten dönen durakları kullan — POI uydurma, poi_key alanını doldur.",
            "Günleri semte göre kümele; ulaşım süresini estimate_travel_time'dan al.",
            "Hava durumunu dikkate al: yağmurlu güne kapalı mekân yerleştir.",
        ] + _ORTAK_KURALLAR,
        tools=["get_pois", "estimate_travel_time", "build_itinerary", "validate_itinerary",
               "get_weather"],
        tiers={"T0", "T3"}, handler=h_itinerary_architect, model_tier="planner",
        output_schema=Itinerary,
        keywords=["plan", "program", "gezi planı", "günlük", "rota", "itinerary", "gezelim",
                  "ne yapsak", "gezi programı", "görülmesi gereken", "gezilecek", "turistik",
                  "mutlaka gör", "ne görülür"],
    ),
    "logistics_agent": AgentSpec(
        key="logistics_agent", name="Lojistik Uzmanı", role="Konaklama ve ulaşım uzmanı",
        description="Uçuş, konaklama ve şehir içi ulaşım seçeneklerini bütçe filtresiyle sunar.",
        instructions=[
            "search_flights ve search_hotels araçlarını kullan.",
            "Bütçe bandı sert filtredir: üst bandın üstünde seçenek sunma.",
            "Kayıtların simüle olduğunu ve işletme adı üretilmediğini açıkça belirt.",
            "Bütçe üst sınırı aşılıyorsa uyar ve alternatif öner.",
        ] + _ORTAK_KURALLAR,
        tools=["search_flights", "search_hotels", "get_practical_facts", "get_fx"],
        tiers={"T1", "T2"}, handler=h_logistics_agent,
        # Konuşma dili şart: kullanıcı "nerede kalıcaz" yazıyor, "nerede kalayım" değil.
        # "nasıl giderim" de buraya aittir — bu bir ulaşım sorusudur, destinasyon sorusu değil.
        keywords=["otel", "konaklama", "konakla", "uçuş", "bilet", "uçak", "transfer",
                  "havalimanı", "nerede kal", "nereye kal", "kalacak", "kalıcak", "kalıcaz",
                  "kalacağız", "hostel", "pansiyon", "yatacak",
                  "nasıl gider", "nasıl gidil", "nasıl gidebil", "nasıl ulaş", "ulaşım",
                  "kaç saat uçuş", "direkt uçuş", "aktarma"],
    ),
    "culinary_guide": AgentSpec(
        key="culinary_guide", name="Gastronomi Rehberi", role="Yeme-içme uzmanı",
        description="Yöresel lezzetleri ve mekân tiplerini diyet sert filtresiyle önerir.",
        instructions=[
            "get_local_dishes ve search_restaurants araçlarını kullan.",
            "Diyet kısıtı SERT FİLTREDİR: vegan kullanıcıya vegan olmayan yemek önerme.",
            "Uygun yöresel yemek yoksa bunu açıkça söyle, uydurma seçenek üretme.",
            "Mekân önerilerinde işletme adı kullanma; yer tipi + semt biçiminde ver.",
        ] + _ORTAK_KURALLAR,
        tools=["search_restaurants", "get_local_dishes"],
        tiers={"T0", "T2"}, handler=h_culinary_guide,
        keywords=["yemek", "lezzet", "mutfak", "restoran", "ne yenir", "kahvaltı", "tatlı",
                  "gastronomi", "vegan", "vejetaryen"],
    ),
    "culture_curator": AgentSpec(
        key="culture_curator", name="Kültür Küratörü", role="Kültür ve görgü kuralları uzmanı",
        description="Kültürel bilgiler, görgü kuralları, kıyafet ve yerel hassasiyetler.",
        instructions=[
            "YALNIZCA get_culture_notes aracından ve küratörlü içerikten konuş (T0).",
            "Kültürel iddia halüsinasyona en açık alandır: bilgi tabanında yoksa 'kaydımız yok' de.",
            "Genelleme ve klişeden kaçın; kaynaktaki ifadeye sadık kal.",
        ] + _ORTAK_KURALLAR,
        tools=["get_culture_notes"], tiers={"T0"}, handler=h_culture_curator,
        keywords=["kültür", "görgü", "gelenek", "adet", "bahşiş", "kıyafet", "saygısızlık",
                  "nasıl davran", "selamlaş", "dini"],
    ),
    "practical_desk": AgentSpec(
        key="practical_desk", name="Pratik Bilgi Masası", role="Hava, saat farkı, döviz, pratik bilgi",
        description="Hava durumu, saat farkı (zoneinfo), döviz kuru, priz, acil numaralar.",
        instructions=[
            "Soruya göre get_weather, get_timezone_diff, get_fx, get_practical_facts araçlarını çağır.",
            "Saat farkı hesabı zoneinfo ile yapılır; yaz saati notunu ekle.",
            "Döviz kurunun referans kur olduğunu, bankaların makas uyguladığını belirt.",
        ] + _ORTAK_KURALLAR,
        tools=["get_weather", "get_timezone_diff", "get_fx", "get_practical_facts"],
        tiers={"T1", "T3"}, handler=h_practical_desk,
        keywords=["hava", "sıcaklık", "yağmur", "ne giy", "saat fark", "kaç saat", "zaman fark",
                  "jetlag", "döviz kur", "kuru", "kaç tl", "priz", "voltaj", "acil", "musluk", "internet",
                  "sim kart", "elektrik"],
    ),
    "documents_officer": AgentSpec(
        key="documents_officer", name="Belge Sorumlusu", role="Vize, pasaport ve giriş koşulları",
        description="Vize/pasaport bilgilendirmesi — yalnızca T1 resmî kaynak + zorunlu feragat.",
        instructions=[
            "YÜKSEK RİSK ALANI. Yalnızca get_visa_requirements ve check_passport_validity "
            "araçlarının döndürdüğü resmî (T1) veriden konuş.",
            "Kesin hukuki sonuç bildirme; 'vizeniz kesin onaylanır' gibi ifadeler yasak.",
            "Her yanıtta kaynağı, geçerlilik tarihini ve feragat metnini ver.",
            "T.C. dışı vatandaşlık sorulursa veri üretme, resmî kanala yönlendir.",
            "Bilgi tabanında olmayan ülke için tahmin yürütme.",
        ],
        tools=["get_visa_requirements", "check_passport_validity"],
        tiers={"T1"}, handler=h_documents_officer, high_risk=True,
        keywords=["vize", "pasaport", "giriş koşul", "schengen", "oturum izni", "transit",
                  "belge", "konsolosluk"],
    ),
    "faq_specialist": AgentSpec(
        key="faq_specialist", name="SSS Uzmanı", role="Sıkça sorulan sorular uzmanı",
        description="13 kategorili SSS bilgi tabanından yanıt verir.",
        instructions=[
            "search_faq aracıyla bilgi tabanını ara ve dönen cevabı sadakatle aktar.",
            "Eşleşme yoksa uydurma; insana devretmeyi öner.",
            "Yüksek riskli SSS kaydında feragat metnini koru.",
        ] + _ORTAK_KURALLAR,
        tools=["search_faq"], tiers={"T0", "T1"}, handler=h_faq_specialist,
        # "kural" ve "politika" fazla genel: "görgü kuralları" sorusunu SSS'ye çekiyordu.
        # Anahtar kelime seti dar ve ayırt edici tutulur.
        keywords=["bagaj", "iptal", "iade", "değişiklik", "sigorta", "ödeme", "fatura", "check-in",
                  "kampanya", "puan", "sadakat", "sss", "rezervasyon", "geri ödeme"],
    ),
    "preference_keeper": AgentSpec(
        key="preference_keeper", name="Tercih Yöneticisi", role="Tercih çıkarımı ve açıklama",
        description="Tercihleri konuşmadan çıkarır, KVKK kapısından geçirerek saklar, "
                    "'neden bu öneri?' sorusunu skor kırılımıyla yanıtlar.",
        instructions=[
            "Kullanıcının mesajından seyahat tercihlerini çıkar ve TravelerProfile şemasına doldur.",
            "SADECE seyahat tercihi çıkar: ad, kimlik, pasaport, sağlık verisi ASLA yakalanmaz.",
            "Diyet ve erişilebilirlik KVKK m.6 özel nitelikli veridir; açık rıza yoksa saklanmaz.",
            "'Neden bu öneri' sorulursa skor kırılımını kalem kalem açıkla.",
        ],
        tools=["search_destinations"], tiers={"T0"}, handler=h_preference_keeper,
        output_schema=TravelerProfile,
        keywords=["tercih", "profil", "seviyorum", "sevmem", "vejetaryen", "vegan", "bütçem",
                  "neden bu öneri", "beni tanı", "hatırla"],
    ),
    "concierge": AgentSpec(
        key="concierge", name="Karşılama & Yönlendirme", role="Karşılama, kapsam ve netleştirme",
        description="Selamlama, kimlik/yetenek soruları, anlaşılmayan girdi ve kullanıcı "
                    "düzeltmelerini karşılar. Emin olunmayan hiçbir isteğe cevap üretmez.",
        instructions=[
            "Ne sorulduğunu anlamadıysan CEVAP ÜRETME; anlamadığını söyle ve netleştirici "
            "soru sor. Yanlış cevap, cevapsızlıktan kötüdür.",
            "Kullanıcı 'yanlış anladın' derse özür dile, son anladığın isteği söyle ve "
            "yeniden sor.",
            "Kapsam dışı bir destinasyon sorulursa uydurma; elimizdeki destinasyonları listele.",
            "Yeteneklerini sayarken somut örnek cümleler ver.",
        ],
        tools=[], tiers={"T0"}, handler=h_concierge,
        keywords=[],  # anahtar kelimeyle değil, niyetle çağrılır (bkz. team.classify)
    ),
}

MEMBER_ORDER = list(SPECS.keys())


def spec(key: str) -> Optional[AgentSpec]:
    return SPECS.get(key)


def run_agent(key: str, ctx: Ctx) -> AgentReply:
    """Deterministik yol: uzmanın handler'ını çalıştırır (mock mod ve hızlı yol)."""
    s = SPECS.get(key)
    if not s:
        return AgentReply(agent=key, text="Bu konuda uzman bir ajanım yok.")
    reply = s.handler(ctx)
    reply.high_risk = reply.high_risk or s.high_risk
    return reply


# ─────────────────────────────────────────────────────────────────────
# Agno üye ajanları (gerçek mod, yavaş yol)
# ─────────────────────────────────────────────────────────────────────
def build_members(db: Any = None, knowledge: Any = None) -> list[Any]:
    """`SPECS`'ten Agno `Agent` nesneleri üretir. Anahtar yoksa boş liste döner.

    Dokuz ajan tek şablondan üretilir — PLAN.md §13'teki "9 ajan aynı şablondan
    çoğaltılır" riskini azaltma kararının kod karşılığı.
    """
    if not models.available():
        return []
    from agno.agent import Agent

    members: list[Any] = []
    for s in SPECS.values():
        try:
            members.append(Agent(
                name=s.name,
                role=s.role,
                model=models.get_model(s.model_tier),
                instructions=s.instructions,
                tools=T.tools_for(s.tools),
                db=db,
                knowledge=knowledge if s.key in ("culture_curator", "faq_specialist") else None,
                search_knowledge=bool(knowledge) and s.key in ("culture_curator", "faq_specialist"),
                output_schema=s.output_schema,
                markdown=True,
                add_datetime_to_context=True,
            ))
        except Exception as exc:
            print(f"[agents] '{s.key}' kurulamadı ({exc.__class__.__name__}: {exc}) -> atlandı")
    return members


def roster() -> list[dict]:
    """Sunum/arayüz/n8n için ajan kadrosu tablosu."""
    return [
        {"anahtar": s.key, "ad": s.name, "rol": s.role, "aciklama": s.description,
         "araclar": s.tools, "kaynak_kademeleri": sorted(s.tiers),
         "model_katmani": s.model_tier, "yuksek_risk": s.high_risk,
         "yapilandirilmis_cikti": s.output_schema.__name__ if s.output_schema else None}
        for s in SPECS.values()
    ]
