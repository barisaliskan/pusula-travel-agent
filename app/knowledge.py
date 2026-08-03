"""Bilgi tabanı + retrieval katmanı (RAG).

İki yol, tek arayüz:

  A) **Yerleşik hibrit retriever** (varsayılan, sıfır bağımlılık)
     Anahtar kelime (IDF ağırlıklı) + vektör (cosine) skorlarının birleşimi.
     Anahtarsız modda da çalışır -> video çekiminde canlı hata riski yok (CLAUDE.md kural 2).
     Hashing embedder'la tek başına vektör araması zayıf kalır; anahtar kelime bacağı bunu telafi eder.

  B) **Agno `Knowledge` + `RedisVectorDb`** (üretim yolu)
     `USE_REMOTE_EMBEDDINGS=true` + Redis varsa kurulur ve ajanlara `knowledge=` olarak verilir.
     Sunumda "aynı kod, ölçeklenebilir altyapı" anlatısının somut karşılığı.

Her belge kaynağını (`tier`, `source`, `valid_until`) taşır — groundedness guardrail'i ve
atıf üretimi bu alanlara dayanır (CLAUDE.md kural 3).
"""
from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Optional

from . import config, models
from .cache import cache, keys
from .text import fold as _tr_fold

_TOKEN_RE = re.compile(r"[a-zçğıöşü0-9]{2,}", re.IGNORECASE)

# Destinasyon takma adları: İngilizce yazımlar ve yaygın alternatifler.
# "a 3 day plan for Rome" isteğinde "Rome" çözülemiyordu.
DEST_ALIASES: dict[str, tuple[str, ...]] = {
    "roma": ("rome",),
    "barselona": ("barcelona",),
    "paris": (),
    "prag": ("prague", "praha"),
    "tokyo": ("tokio", "japonya", "japan"),
    "bangkok": ("tayland", "thailand"),
    "tiflis": ("tbilisi", "gurcistan", "georgia"),
    "saraybosna": ("sarajevo", "bosna", "bosnia"),
    "baku": ("bakü", "azerbaycan", "azerbaijan"),
    "dubai": ("bae", "emirlik"),
    "marakes": ("marrakesh", "marrakech", "fas", "morocco"),
    "kapadokya": ("cappadocia", "nevsehir", "goreme", "görme"),
}

# Kapsam dışı yer tespiti: yer adı, seyahat ifadesinin hemen SOLUNDA durur.
# `(\w+)` sonrası `['’]?\w{0,4}` çekim/kesme işaretini yutar: "Bali'ye", "Maldivler'e",
# "trabzonda". Büyük harf aranmaz — kullanıcı küçük harfle yazıyor.
_YER_KALIPLARI = [re.compile(k, re.IGNORECASE) for k in (
    r"(\w{3,})['’]?\w{0,4}\s+hakkında",
    r"(\w{3,})['’]?\w{0,4}\s+(?:gitmek|gideceğim|gitmeyi|gidiyorum|gideyim|gidelim|uçmak)",
    r"(\w{3,})['’]?\w{0,4}\s+(?:gezmek|gezeceğim|gezmeyi|ziyaret|görmek)",
    # DİKKAT: "planı" burada YOK. "gezi planı" / "geiz planı" (yazım hatası) gibi
    # sıradan tamlamalar yer adı sanılıyordu. Yer + gün sayısı kalıbı ise güvenli.
    r"(\w{3,})['’]?\w{0,4}\s+(?:tatili|turu|gezisi|seyahati)",
    r"(\w{3,})['’]?[dt][ae]\s+\d+\s*(?:gün|gece)",
    r"(\w{3,})['’]?\w{0,4}\s+nasıl bir yer",
    r"(\w{3,})['’]?[dt][ae]\s+(?:ne|neler|hangi|nerede|kaç)",
)]

# `unknown_place` yanlış alarm vermesin diye: cümle başında büyük yazılan sıradan kelimeler.
# Eşleşme `startswith` ile yapılır; böylece çekim ekli hâller de yakalanır
# ("Eylülde" -> "eylul", "Nereye" -> "nere"). Buradakiler kapalı sınıf işlev sözcükleri
# ve seyahat sözlüğünün yaygın adlarıdır — hiçbiri bir yer adı olamaz.
_COMMON_WORDS = {
    # soru ve işaret sözcükleri (kapalı sınıf)
    "nere", "hangi", "nasil", "neden", "niye", "nicin", "kim", "kac", "hani", "ne",
    "bu", "su", "bunlar", "sunlar", "onlar", "ora", "bura", "sura", "sira",
    # zamir ve bağlaçlar
    "ben", "sen", "biz", "siz", "bana", "sana", "bize", "size", "benim", "senin", "bizim",
    "sizin", "ayrica", "ancak", "fakat", "cunku", "sonra", "once", "yine", "hem", "veya",
    "merhaba", "selam", "peki", "tamam", "evet", "hayir", "lutfen", "acaba", "tesekkur",
    # zaman
    "bugun", "dun", "yarin", "simdi", "hafta", "hafta sonu", "gun", "gece", "ay", "yil",
    "ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul",
    "ekim", "kasim", "aralik", "pazartesi", "sali", "carsamba", "persembe", "cuma",
    "cumartesi", "pazar",
    # seyahat sözlüğü
    "gunluk", "plan", "program", "gezi", "rota", "otel", "ucus", "ucak", "vize", "pasaport",
    "yemek", "hava", "butce", "tatil", "seyahat", "sehir", "ulke", "yer", "kisi", "muze",
    "restoran", "mekan", "bilet", "bagaj", "konaklama", "transfer",
    # bizim kapsamımızdaki ülke/şehirler zaten çözülüyor; TR şehirleri yanlış alarm vermesin
    "turkiye", "istanbul", "ankara", "izmir", "antalya", "pusula", "avrupa", "asya",
}

# Türkçe durak kelimeleri — IDF'yi bunlar üzerinden harcamayalım.
_STOPWORDS = {
    "ve", "ile", "bir", "bu", "da", "de", "için", "icin", "mi", "mu", "ne", "ki", "ya",
    "olan", "olarak", "gibi", "daha", "cok", "çok", "en", "her", "ama", "veya", "ise",
    "var", "yok", "the", "and", "for",
}


def _tokens(text: str) -> list[str]:
    # Türkçe doğru küçük harf: "YENİR".lower() 'yeni̇r' üretir ve eşleşmeyi kırar (app/text.py).
    return [t for t in _TOKEN_RE.findall(_tr_fold(text)) if t not in _STOPWORDS]


@dataclass
class Document:
    """Retrieval biriminin tamamı: metin + kaynak künyesi."""

    id: str
    title: str
    text: str
    collection: str  # destination | poi | cuisine | culture | practical | visa | faq
    dest_key: Optional[str] = None
    category: Optional[str] = None
    tier: str = "T0"
    high_risk: bool = False
    source: str = "Pusula İçerik Editörlüğü"
    valid_until: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def searchable(self) -> str:
        return f"{self.title} {self.text}"


@dataclass
class Hit:
    doc: Document
    score: float
    keyword_score: float
    vector_score: float


class KnowledgeBase:
    """Küratörlü JSON dosyalarını yükler, belge dizinini ve retriever'ı kurar.

    Tek örnek olarak kullanılır (`kb`); yükleme tembeldir (ilk aramada) ve thread-safe'tir.
    """

    FILES = ("destinations", "pois", "cuisine", "culture", "practical", "visa", "faq")

    def __init__(self, directory=None) -> None:
        self.dir = directory or config.KNOWLEDGE_DIR
        self.raw: dict[str, dict] = {}
        self.docs: list[Document] = []
        self._by_id: dict[str, Document] = {}
        self._idf: dict[str, float] = {}
        self._doc_tokens: list[set[str]] = []
        self._title_tokens: list[set[str]] = []
        self._vectors: list[list[float]] = []
        self._loaded = False
        self._lock = threading.Lock()

    # ---- yükleme ----
    def load(self) -> "KnowledgeBase":
        with self._lock:
            if self._loaded:
                return self
            for name in self.FILES:
                path = self.dir / f"{name}.json"
                self.raw[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            self.docs = list(self._build_documents())
            self._by_id = {d.id: d for d in self.docs}
            self._index()
            self._loaded = True
        return self

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    # ---- tipli erişimciler (ajanlar/araçlar bunları kullanır) ----
    @property
    def destinations(self) -> list[dict]:
        self._ensure()
        return self.raw["destinations"].get("destinations", [])

    def destination(self, key: str) -> Optional[dict]:
        return next((d for d in self.destinations if d["key"] == key), None)

    def pois(self, dest_key: str) -> list[dict]:
        self._ensure()
        return self.raw["pois"].get("pois", {}).get(dest_key, [])

    def cuisine(self, dest_key: str) -> Optional[dict]:
        self._ensure()
        return self.raw["cuisine"].get("cuisines", {}).get(dest_key)

    def culture(self, dest_key: str) -> Optional[dict]:
        self._ensure()
        return self.raw["culture"].get("cultures", {}).get(dest_key)

    def practical(self, dest_key: str) -> Optional[dict]:
        self._ensure()
        return self.raw["practical"].get("practical", {}).get(dest_key)

    def visa(self, from_country: str, to_country: str) -> Optional[dict]:
        self._ensure()
        return self.raw["visa"].get("matrix", {}).get(f"{from_country}->{to_country}")

    def visa_for_destination(self, dest_key: str) -> Optional[dict]:
        self._ensure()
        for row in self.raw["visa"].get("matrix", {}).values():
            if dest_key in row.get("destination_keys", []):
                return row
        return None

    @property
    def visa_meta(self) -> dict:
        self._ensure()
        return self.raw["visa"].get("_meta", {})

    @property
    def faqs(self) -> list[dict]:
        self._ensure()
        return self.raw["faq"].get("faqs", [])

    @property
    def faq_categories(self) -> list[str]:
        self._ensure()
        return self.raw["faq"].get("_meta", {}).get("kategoriler", [])

    def resolve_destination(self, text: str) -> Optional[str]:
        """Serbest metinde geçen destinasyonu anahtarına çözer ('Roma'da' -> 'roma').

        Ajanların ve araçların ortak giriş noktası; her modül kendi eşleştirmesini yazmasın.
        """
        self._ensure()
        return self._resolve_unlocked(text)

    def _resolve_unlocked(self, text: str) -> Optional[str]:
        """`_ensure()` çağırmayan çekirdek — zaten yüklü olduğu bilinen yerlerden kullanılır."""
        folded = _tr_fold(text)
        for d in self.raw.get("destinations", {}).get("destinations", []):
            adaylar = [d["key"], _tr_fold(d["name"]), _tr_fold(d["country"])]
            adaylar += [_tr_fold(a) for a in DEST_ALIASES.get(d["key"], ())]
            for c in adaylar:
                if c and c in folded:
                    return d["key"]
        return None

    def unknown_place(self, text: str) -> Optional[str]:
        """Metinde destinasyon gibi duran ama kapsamımızda OLMAYAN bir yer adı var mı?

        "Bali'ye gitmek istiyorum" isteğine sessizce alakasız bir SSS dönmek yerine
        "Bali kapsamımızda yok" diyebilmek için. Büyük harfle başlayan, bilinen bir
        Türkçe kelime olmayan ve bilgi tabanında bulunmayan sözcükleri arar.
        """
        self._ensure()
        if self._resolve_unlocked(text):
            return None

        # Büyük harf varsayımına GÜVENİLMEZ: kullanıcı "trabzon hakkında bilgin varmı"
        # diye yazıyor. Bunun yerine cümle yapısına bakıyoruz — yer adı, seyahat
        # ifadesinin hemen SOLUNDA durur. Bu, hem küçük harfe hem de yanlış alarma karşı
        # büyük harf taramasından çok daha güvenilir.
        for rx in _YER_KALIPLARI:
            for m in rx.finditer(text):
                aday = m.group(1)
                katlanmis = _tr_fold(aday)
                if len(katlanmis) < 3:
                    continue
                if any(katlanmis.startswith(w) for w in _COMMON_WORDS):
                    continue
                if katlanmis in _STOPWORDS:
                    continue
                if self._resolve_unlocked(aday):   # bildiğimiz bir destinasyon
                    return None
                # Görüntüde ilk harf büyük olsun ("trabzon" -> "Trabzon")
                return aday[0].upper() + aday[1:] if aday[:1].islower() else aday
        return None

    # ---- belge üretimi ----
    def _build_documents(self) -> Iterable[Document]:
        # DİKKAT: `load()` kilidi tutarken çağrılır -> burada tipli erişimcileri (self.destinations
        # gibi) KULLANMA; onlar `_ensure()` üzerinden `load()`'a geri döner ve kilitlenme yaratır.
        # Bu metot yalnızca `self.raw`'ı okur.

        # 1) Destinasyon özetleri
        for d in self.raw.get("destinations", {}).get("destinations", []):
            src = (d.get("sources") or [{}])[0]
            body = (
                f"{d['summary']} Ülke: {d['country']}. Bölge: {d.get('region','')}. "
                f"En uygun aylar: {', '.join(map(str, d.get('best_months', [])))}. "
                f"{d.get('seasonality_note','')} Para birimi: {d.get('currency','')}. "
                f"Öne çıkanlar: {', '.join(d.get('tags', []))}. "
                f"Erişilebilirlik: {d.get('accessibility_note','')}"
            )
            yield Document(
                id=f"dest:{d['key']}", title=f"{d['name']} — destinasyon özeti", text=body,
                collection="destination", dest_key=d["key"], category="destinasyon",
                tier=src.get("tier", "T0"), source=src.get("title", "Pusula İçerik Editörlüğü"),
                valid_until=src.get("valid_until"), meta={"qid": d.get("qid"), "styles": d.get("styles", [])},
            )

        # 2) POI'ler
        for dest_key, items in self.raw.get("pois", {}).get("pois", {}).items():
            for p in items:
                body = (
                    f"{p['name']} — {p.get('category','')} ({p.get('district','')}). "
                    f"Ortalama süre {p.get('duration_min',0)} dakika, tahmini ücret {p.get('cost_try',0)} TRY. "
                    f"Önerilen zaman: {p.get('best_time','')}. {p.get('note','')}"
                )
                yield Document(
                    id=f"poi:{p['key']}", title=p["name"], text=body, collection="poi",
                    dest_key=dest_key, category=p.get("category"), tier="T0",
                    source="Pusula İçerik Editörlüğü — POI seti",
                    meta={"duration_min": p.get("duration_min"), "cost_try": p.get("cost_try"),
                          "indoor": p.get("indoor"), "accessible": p.get("accessible")},
                )

        # 3) Mutfak
        for dest_key, c in self.raw.get("cuisine", {}).get("cuisines", {}).items():
            dishes = "; ".join(f"{d['name']}: {d['aciklama']}" for d in c.get("dishes", []))
            body = (
                f"{c.get('ozet','')} Yemek saatleri: {c.get('yemek_saatleri','')}. "
                f"Yöresel lezzetler — {dishes}. İpuçları: {' '.join(c.get('ipuclari', []))} "
                f"Vegan notu: {c.get('vegan_notu','')}"
            )
            yield Document(
                id=f"cuisine:{dest_key}", title=f"{dest_key} mutfağı ve yöresel lezzetler", text=body,
                collection="cuisine", dest_key=dest_key, category="mutfak", tier="T0",
                source="Pusula İçerik Editörlüğü — Mutfak Rehberi",
            )

        # 4) Kültür ve görgü kuralları
        for dest_key, c in self.raw.get("culture", {}).get("cultures", {}).items():
            body = (
                f"Selamlaşma: {c.get('selamlasma','')} Kıyafet: {c.get('kiyafet','')} "
                f"Görgü kuralları: {' '.join(c.get('gorgu', []))} Bahşiş: {c.get('bahsis','')} "
                f"Pazarlık: {c.get('pazarlik','')} Dini hassasiyet: {c.get('dini_hassasiyet','')} "
                f"Güvenlik: {c.get('guvenlik','')} Kaçınılması gerekenler: {' '.join(c.get('kacinilmasi_gerekenler', []))}"
            )
            yield Document(
                id=f"culture:{dest_key}", title=f"{dest_key} kültürü ve görgü kuralları", text=body,
                collection="culture", dest_key=dest_key, category="kültür", tier="T0",
                source="Pusula İçerik Editörlüğü — Kültür Rehberi",
                meta={"dil_ipuclari": c.get("dil_ipuclari", {})},
            )

        # 5) Pratik bilgiler
        for dest_key, p in self.raw.get("practical", {}).get("practical", {}).items():
            el = p.get("elektrik", {})
            cur = p.get("currency", {})
            acil = ", ".join(f"{k}: {v}" for k, v in p.get("acil_numaralar", {}).items() if k != "not")
            body = (
                f"Saat dilimi {p.get('timezone','')}. Para birimi {cur.get('code','')} ({cur.get('symbol','')}). "
                f"{cur.get('nakit_kullanimi','')} Elektrik {el.get('voltaj','')} {el.get('frekans','')}, "
                f"priz tipi {', '.join(el.get('priz_tipi', []))}. {el.get('not','')} "
                f"Acil numaralar — {acil}. Musluk suyu: {p.get('musluk_suyu','')} "
                f"İnternet: {p.get('internet','')} Ulaşım kartı: {p.get('ulasim_karti','')}"
            )
            yield Document(
                id=f"practical:{dest_key}", title=f"{dest_key} pratik bilgiler", text=body,
                collection="practical", dest_key=dest_key, category="pratik", tier="T1",
                source="Resmî acil hizmet bilgileri ve IEC priz standartları",
                meta={"timezone": p.get("timezone"), "currency": cur.get("code")},
            )

        # 6) Vize — YÜKSEK RİSK
        vmeta = self.raw.get("visa", {}).get("_meta", {})
        for pair, row in self.raw.get("visa", {}).get("matrix", {}).items():
            um = row.get("umuma_mahsus", {})
            body = (
                f"{row['country']} için T.C. vatandaşları: "
                f"{'vize gereklidir' if um.get('vize_gerekli') else 'vize gerekmez'}. "
                f"{um.get('tur','')} — {um.get('sure','')}. "
                f"Pasaport geçerliliği: {row.get('pasaport_gecerliligi','')}. "
                f"{row.get('basvuru_notu','')} "
                f"Tipik belgeler: {', '.join(row.get('tipik_belgeler', []))}. "
                f"{' '.join(row.get('ek_kosullar', []))}"
            )
            yield Document(
                id=f"visa:{pair}", title=f"{row['country']} vize ve giriş koşulları", text=body,
                collection="visa", dest_key=(row.get("destination_keys") or [None])[0],
                category="vize", tier="T1", high_risk=True,
                source="T.C. Dışişleri Bakanlığı / IATA Timatic",
                valid_until=vmeta.get("gecerlilik_sonu"),
                meta={"pair": pair, "destination_keys": row.get("destination_keys", [])},
            )

        # 7) SSS
        for f in self.raw.get("faq", {}).get("faqs", []):
            yield Document(
                id=f["id"], title=f["soru"], text=f["cevap"], collection="faq",
                category=f["kategori"], tier=f.get("guven_kademesi", "T0"),
                high_risk=f.get("yuksek_risk", False), source=f.get("kaynak", ""),
                valid_until=f.get("gecerlilik_tarihi"),
                meta={"alt_konu": f.get("alt_konu"), "etiketler": f.get("etiketler", []),
                      "ilgili": f.get("ilgili", [])},
            )

    # ---- dizin ----
    def _index(self) -> None:
        """IDF sözlüğü + belge vektörleri. Vektörler embedding cache'inden okunur."""
        n = len(self.docs)
        df: dict[str, int] = {}
        self._doc_tokens = []
        self._title_tokens = []
        for d in self.docs:
            toks = set(_tokens(d.searchable()))
            self._doc_tokens.append(toks)
            self._title_tokens.append(set(_tokens(d.title)))
            for t in toks:
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}
        self._vectors = [self._embed_cached(d.id, d.searchable()) for d in self.docs]

    def _embed_cached(self, doc_id: str, text: str) -> list[float]:
        """Belge embedding'ini önbellekten oku; yoksa üret ve yaz (emb:{hash}, 7 gün).

        Uzak embedder kullanılıyorsa bu, her açılışta yeniden ücret ödemeyi engeller.
        """
        if not config.USE_REMOTE_EMBEDDINGS:
            return models.mock_embed(text)  # ucuz, cache'e gerek yok
        key = keys.embedding(f"{doc_id}|{config.EMBED_MODEL}")
        cached = cache.get_json(key)
        if cached:
            return cached
        vec = models.embed(text)
        cache.set_json(key, vec, ttl=config.TTL_EMBEDDING)
        return vec

    # ---- arama ----
    def search(
        self,
        query: str,
        k: int = 5,
        *,
        collections: Optional[Iterable[str]] = None,
        dest_key: Optional[str] = None,
        category: Optional[str] = None,
        tiers: Optional[Iterable[str]] = None,
        min_score: float = 0.05,
        auto_dest: bool = True,
    ) -> list[Hit]:
        """Hibrit arama: IDF ağırlıklı anahtar kelime + cosine vektör.

        `tiers` ile ajan-kaynak yetkilendirmesi uygulanır: `documents_officer` yalnızca
        `tiers={'T1'}` ile çağırır ve T1 dışına fiilen erişemez (PLAN.md §4) — kapsam kilidi
        promptta değil, veri seviyesinde.
        """
        self._ensure()
        q_tokens = set(_tokens(query))
        q_vec = models.embed(query) if query.strip() else []
        coll_filter = set(collections) if collections else None
        tier_filter = set(tiers) if tiers else None

        # Varlık farkındalığı: sorguda geçen destinasyon, tesadüfen eşleşen sıradan bir
        # kelimeden daha belirleyicidir ("Roma'da ne yenir" -> Tiflis mutfağı dönmesin).
        # Sert filtre yerine ağırlık kullanıyoruz; böylece destinasyondan bağımsız SSS
        # dokümanları (bagaj, iade, KVKK) yine yüzeye çıkabilir.
        focus = dest_key or (self._resolve_unlocked(query) if auto_dest else None)

        hits: list[Hit] = []
        for i, doc in enumerate(self.docs):
            if coll_filter and doc.collection not in coll_filter:
                continue
            if dest_key and doc.dest_key and doc.dest_key != dest_key:
                continue
            if dest_key and doc.collection in ("destination", "poi", "cuisine", "culture", "practical") and not doc.dest_key:
                continue
            if category and doc.category != category:
                continue
            if tier_filter and doc.tier not in tier_filter:
                continue

            # Başlıkta geçen eşleşme gövdedekinden daha güçlü sinyaldir.
            overlap = q_tokens & self._doc_tokens[i]
            title_toks = self._title_tokens[i]
            kw_raw = sum(self._idf.get(t, 0.0) * (1.5 if t in title_toks else 1.0) for t in overlap)
            kw_norm = sum(self._idf.get(t, 0.0) for t in q_tokens) or 1.0
            kw = kw_raw / kw_norm

            vs = 0.0
            if q_vec:
                v = self._vectors[i]
                vs = sum(a * b for a, b in zip(q_vec, v))  # ikisi de birim vektör

            score = 0.65 * kw + 0.35 * vs
            if focus and doc.dest_key:
                score *= 1.6 if doc.dest_key == focus else 0.45
            if score >= min_score:
                hits.append(Hit(doc=doc, score=round(score, 4),
                                keyword_score=round(kw, 4), vector_score=round(vs, 4)))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def get(self, doc_id: str) -> Optional[Document]:
        self._ensure()
        return self._by_id.get(doc_id)

    def stats(self) -> dict:
        self._ensure()
        by_coll: dict[str, int] = {}
        for d in self.docs:
            by_coll[d.collection] = by_coll.get(d.collection, 0) + 1
        return {
            "documents": len(self.docs),
            "by_collection": by_coll,
            "high_risk_docs": sum(1 for d in self.docs if d.high_risk),
            "vocabulary": len(self._idf),
            "retriever": "hybrid(keyword+vector)",
            "embedder": config.EMBED_MODEL if config.USE_REMOTE_EMBEDDINGS else "mock-hashing-512",
        }


# ─────────────────────────────────────────────────────────────────────
# B) Üretim yolu: Agno Knowledge + RedisVectorDb
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def build_agno_knowledge() -> Optional[Any]:
    """Agno `Knowledge` nesnesi; koşullar sağlanmazsa None (ajanlar yerleşik retriever'a düşer).

    Koşullar: gerçek embedder (uzak embedding açık) + erişilebilir Redis.
    Vektör araması RediSearch modülü ister; `redis/redis-stack` imajı bunu sağlar.
    """
    embedder = models.get_embedder()
    if embedder is None or cache.backend_name != "redis":
        return None
    try:
        from agno.knowledge.knowledge import Knowledge
        from agno.vectordb.redis import RedisVectorDb

        vector_db = RedisVectorDb(
            index_name="pusula_knowledge",
            redis_url=config.REDIS_URL,
            embedder=embedder,
        )
        return Knowledge(
            name="Pusula Seyahat Bilgi Tabanı",
            description="Küratörlü destinasyon, POI, mutfak, kültür, pratik bilgi, vize ve SSS içeriği",
            vector_db=vector_db,
            max_results=5,
        )
    except Exception as exc:  # RediSearch yoksa veya bağlantı düşerse sessizce yerleşik yola dön
        print(f"[knowledge] Agno Knowledge kurulamadı ({exc.__class__.__name__}) -> yerleşik retriever")
        return None


# Uygulama genelinde tek örnek
kb = KnowledgeBase()
