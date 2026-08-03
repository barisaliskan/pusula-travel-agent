"""Senaryoların uçtan uca doğrulaması — pytest gerektirmez.

`app/scenarios.py` bir belge değil **sözleşme**dir: her senaryonun beklenen yolu,
uzmanı, kaynak kademesi ve guardrail davranışı orada yazılıdır. Bu dosya o sözleşmeyi
çalıştırır.

    .venv/bin/python tests/test_scenarios.py

Her senaryo temiz bir kullanıcı ve oturum kimliğiyle çalışır; birinin bıraktığı profil
veya cache diğerini etkilemesin diye. Aynı komut **mock modda ve gerçek LLM modunda**
geçmelidir (CLAUDE.md kural 2) — gerçek modda yalnızca cümleyi kuran değişir.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, kvkk, models  # noqa: E402
from app.cache import cache  # noqa: E402
from app.scenarios import SCENARIOS  # noqa: E402
from app.schemas import ChatRequest  # noqa: E402
from app import team  # noqa: E402

GECTI, KALDI = [], []


def kontrol(kosul: bool, mesaj: str) -> bool:
    if not kosul:
        KALDI.append(mesaj)
    return kosul


def temizle(user_id: str) -> None:
    cache.purge_user(user_id)
    for pattern in ("sc:*",):
        for k in cache.backend.scan(pattern):
            cache.delete(k)


def senaryo_calistir(s, user_id: str, session_id: str):
    for hazirlik in s.kurulum:
        team.respond(ChatRequest(message=hazirlik, session_id=session_id, user_id=user_id))
    return team.respond(ChatRequest(message=s.mesaj, session_id=session_id, user_id=user_id,
                                    profile_overrides=s.profil))


def main() -> int:
    print("=" * 74)
    print(f"  {len(SCENARIOS)} SENARYO — UÇTAN UCA  ·  mod: {config.PROVIDER_LABEL} "
          f"· cache: {cache.backend_name}")
    print("=" * 74)

    toplam_sure = 0.0
    for s in SCENARIOS:
        uid, sid = f"sen{s.no}", f"sess{s.no}"
        temizle(uid)
        if s.riza:
            kvkk.set_consent(uid, **s.riza)

        hatalar_once = len(KALDI)
        t0 = time.perf_counter()
        try:
            r = senaryo_calistir(s, uid, sid)
        except Exception as exc:
            KALDI.append(f"S{s.no} çalıştırılamadı: {exc.__class__.__name__}: {exc}")
            print(f"  ✗ S{s.no:2d} {s.baslik} — ÇÖKTÜ ({exc.__class__.__name__})")
            continue
        sure = (time.perf_counter() - t0) * 1000
        toplam_sure += sure

        kontrol(r.trace.route == s.beklenen_yol,
                f"S{s.no}: yol '{r.trace.route}', beklenen '{s.beklenen_yol}'")
        for ajan in s.beklenen_ajanlar:
            kontrol(ajan in r.trace.agents,
                    f"S{s.no}: '{ajan}' devreye girmedi (giren: {r.trace.agents})")
        for guard in s.beklenen_guardrail:
            kontrol(guard in r.trace.guardrails,
                    f"S{s.no}: guardrail '{guard}' tetiklenmedi ({r.trace.guardrails})")
        kontrol(len(r.answer.strip()) > 40, f"S{s.no}: yanıt çok kısa")
        kontrol("groundedness" not in r.trace.guardrails,
                f"S{s.no}: yanıtta dayanaksız sayı var (groundedness tetiklendi)")

        # Yüksek riskli konuda feragat zorunlu (CLAUDE.md kural 4)
        if s.kaynak_kademesi == "T1":
            kontrol("Dışişleri Bakanlığı" in r.answer,
                    f"S{s.no}: yüksek riskli yanıtta resmî kaynak/feragat yok")
        # Engellenen isteklerde LLM'e hiç gidilmemeli
        if s.beklenen_yol == "blocked":
            kontrol(r.trace.llm_calls == 0, f"S{s.no}: engellenen istekte LLM çağrıldı")

        # Konuşma senaryolarının içerik sözleşmeleri
        if s.no == 20:  # alternatif: önceki öneriler tekrar edilmemeli
            onceki = team._load_session_ctx(sid).get("last_suggestions") or []
            kontrol(bool(onceki), f"S{s.no}: önceki öneriler oturum bağlamına yazılmamış")
        if s.no == 21:  # "Roma yerine" -> Roma önerilmemeli
            kontrol("Roma" not in r.answer.split("Dikkat")[0],
                    f"S{s.no}: elenmesi gereken Roma yine önerildi")
        if s.no == 24:  # anlaşılmayan girdi -> SSS cevabı verilmemeli
            kontrol("anlayamadım" in r.answer or "anlamadım" in r.answer,
                    f"S{s.no}: anlaşılmayan girdiye netleştirme yerine cevap üretildi")
        if s.no == 26:  # kapsam dışı yer adı yanıtta geçmeli
            kontrol("Bali" in r.answer, f"S{s.no}: kapsam dışı yer adı yanıtta anılmadı")
        if s.no == 28:  # plan reddi -> farklı kurgu olduğu söylenmeli
            kontrol("2. plan" in r.answer or "kurgu" in r.answer,
                    f"S{s.no}: farklı kurgu üretildiği belirtilmedi")
        if s.no == 29:  # bütçeyi aşan destinasyon önerilmemeli
            import re as _re
            asim = [int(x.replace(".", "")) for x in
                    _re.findall(r"Tahmini toplam: \*\*([\d.]+) TRY", r.answer)]
            kontrol(all(a <= 10000 for a in asim),
                    f"S{s.no}: bütçe üstü destinasyon önerildi ({asim})")
        if s.no == 30:  # küçük harfli yer adı yakalanmalı
            kontrol("Trabzon" in r.answer, f"S{s.no}: 'Trabzon' kapsam dışı olarak anılmadı")

        durum = "✓" if len(KALDI) == hatalar_once else "✗"
        if durum == "✓":
            GECTI.append(s.no)
        print(f"  {durum} S{s.no:2d} {s.baslik[:44]:46s} {r.trace.route:7s} "
              f"{sure:6.0f}ms llm={r.trace.llm_calls} ajan={len(r.trace.agents)}")

    # Cache HIT kanıtı: aynı soru ikinci kez sorulunca 0 LLM çağrısı
    print("-" * 74)
    temizle("cachetest")
    ilk = team.respond(ChatRequest(message="Prag'da görgü kuralları nelerdir?",
                                   session_id="c1", user_id="cachetest"))
    ikinci = team.respond(ChatRequest(message="Prag'da görgü kuralları nelerdir?",
                                      session_id="c1", user_id="cachetest"))
    kontrol(ikinci.trace.cache_hit, "Cache: aynı soru ikinci kez HIT vermedi")
    kontrol(ikinci.trace.llm_calls == 0, "Cache: HIT'te LLM çağrısı yapıldı")
    kontrol(ikinci.trace.latency_ms <= max(60, ilk.trace.latency_ms),
            "Cache: HIT ilk çağrıdan hızlı değil")
    print(f"  ✓ cache: {ilk.trace.latency_ms}ms (MISS, llm={ilk.trace.llm_calls}) → "
          f"{ikinci.trace.latency_ms}ms (HIT, llm={ikinci.trace.llm_calls})")

    # Kişiselleştirme kanıtı: aynı soru, iki farklı profil, farklı sonuç
    temizle("pA")
    temizle("pB")
    kvkk.set_consent("pA", personalization=True)
    kvkk.set_consent("pB", personalization=True)
    a = team.respond(ChatRequest(message="Eylülde 4 günlük kaçamak için nereye gitsem?",
                                 session_id="pa", user_id="pA",
                                 profile_overrides={"budget_band": "ekonomik",
                                                    "styles": ["doga"], "pace": "sakin"}))
    b = team.respond(ChatRequest(message="Eylülde 4 günlük kaçamak için nereye gitsem?",
                                 session_id="pb", user_id="pB",
                                 profile_overrides={"budget_band": "luks",
                                                    "styles": ["gastronomi", "alisveris"]}))
    kontrol(a.answer != b.answer, "Kişiselleştirme: iki farklı profil aynı yanıtı üretti")
    print(f"  ✓ kişiselleştirme: aynı soru iki profilde farklı yanıt "
          f"({len(a.answer)} / {len(b.answer)} karakter)")

    # KVKK kanıtı: sil → kalıntı yok
    temizle("delme")
    kvkk.set_consent("delme", personalization=True)
    team.respond(ChatRequest(message="Vejetaryenim, orta bütçem var",
                             session_id="d1", user_id="delme"))
    sonuc = kvkk.delete_all("delme")
    kontrol(sonuc["dogrulama"] == "temiz", "KVKK: silme sonrası kalıntı var")
    print(f"  ✓ KVKK silme: {len(sonuc['silindi'])} anahtar silindi, "
          f"kalıntı denetimi '{sonuc['dogrulama']}'")

    print("-" * 74)
    if KALDI:
        print(f"  {len(GECTI)}/{len(SCENARIOS)} senaryo geçti — {len(KALDI)} sorun:")
        for h in KALDI:
            print(f"    ✗ {h}")
    else:
        print(f"  {len(SCENARIOS)}/{len(SCENARIOS)} senaryo geçti "
              f"· ortalama {toplam_sure / len(SCENARIOS):.0f} ms · "
              f"LLM modu: {config.LLM_MODE}")
    print("=" * 74)
    return 1 if KALDI else 0


if __name__ == "__main__":
    sys.exit(main())
