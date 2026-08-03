/* Pusula AI — arayüz mantığı.
   Bağımlılık yok: markdown çevirici, SSE istemcisi ve durum yönetimi elde yazıldı.
   Gerekçe CLAUDE.md kural 2 ile aynı: video çekiminde CDN'e gidilmesin. */

const API = "";
const USER_ID = localStorage.getItem("pusula_user") ||
  (() => { const v = "demo-" + Math.random().toString(36).slice(2, 8);
           localStorage.setItem("pusula_user", v); return v; })();
const SESSION_ID = "sess-" + USER_ID;

const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => { const n = document.createElement(tag);
  if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };

const STYLES = { kultur: "Kültür", gastronomi: "Gastronomi", doga: "Doğa", plaj: "Plaj",
  macera: "Macera", alisveris: "Alışveriş", gece_hayati: "Gece hayatı", dini: "Dini" };
const DIETS = ["vegan", "vejetaryen", "helal", "glutensiz", "koşer"];
const ACCESS = ["tekerlekli sandalye", "görme engelli", "işitme engelli", "sınırlı hareket"];

let profil = {};
let secili = { styles: new Set(), dietary: new Set(), accessibility: new Set() };

/* ─────────── Minimal markdown ─────────── */
function md(t) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  let s = esc(t);
  s = s.replace(/^### (.+)$/gm, "<h3>$1</h3>").replace(/^## (.+)$/gm, "<h2>$1</h2>");
  s = s.replace(/^---$/gm, "<hr>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>");
  const satirlar = s.split("\n");
  let out = "", liste = false;
  for (const ham of satirlar) {
    const l = ham.trimEnd();
    if (/^\s*[-•]\s+/.test(l)) {
      if (!liste) { out += "<ul>"; liste = true; }
      out += "<li>" + l.replace(/^\s*[-•]\s+/, "") + "</li>";
    } else {
      if (liste) { out += "</ul>"; liste = false; }
      if (/^<(h2|h3|hr)/.test(l)) out += l;
      else if (l.trim() === "") out += "";
      else out += "<p>" + l + "</p>";
    }
  }
  if (liste) out += "</ul>";
  return out;
}

/* ─────────── Akış izi ─────────── */
const ADIMLAR = ["guardrail", "profile", "cache", "classify", "agent", "llm", "done"];
function pipelineSifirla() {
  ADIMLAR.forEach((a) => {
    const n = $(`.pipeline li[data-step="${a}"]`);
    if (n) n.className = "";
  });
  $("#pipeline-detail").innerHTML = "";
}
function pipelineIsaretle(adim, durum = "active") {
  const n = $(`.pipeline li[data-step="${adim}"]`);
  if (!n) return;
  const i = ADIMLAR.indexOf(adim);
  ADIMLAR.slice(0, i).forEach((a) => {
    const p = $(`.pipeline li[data-step="${a}"]`);
    if (p && p.className !== "done") p.className = "done";
  });
  n.className = durum;
}
function pipelineNot(html) {
  $("#pipeline-detail").appendChild(el("div", null, html));
}

/* ─────────── Sohbet ─────────── */
function mesajEkle(rol, html) {
  const m = el("div", "msg " + rol);
  const b = el("div", "bubble", html);
  m.appendChild(b);
  $("#messages").appendChild(m);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return b;
}

function izRozetleri(trace, sources, disclaimer) {
  const t = el("div", "trace");
  const yolAd = { cache: "⚡ cache HIT", fast: "→ hızlı yol", slow: "⇉ yavaş yol (lider ajan)",
                  blocked: "⛔ engellendi" }[trace.route] || trace.route;
  t.appendChild(el("span", "tag route-" + trace.route, yolAd));
  t.appendChild(el("span", "tag", trace.latency_ms + " ms"));
  t.appendChild(el("span", "tag", "LLM çağrısı: " + trace.llm_calls));
  (trace.agents || []).forEach((a) => t.appendChild(el("span", "tag", "🤖 " + a)));
  (trace.tools || []).forEach((a) => t.appendChild(el("span", "tag", "🔧 " + a)));
  (trace.guardrails || []).forEach((gd) => t.appendChild(el("span", "tag guard", "🛡 " + gd)));
  (sources || []).forEach((s) => t.appendChild(
    el("span", "tag tier-" + s.tier, `${s.tier} · ${s.title}` +
       (s.valid_until ? ` · geçerlilik ${s.valid_until}` : ""))));
  if (trace.cache_hit) t.appendChild(el("span", "tag route-cache",
    "benzerlik " + trace.cache_similarity));
  if (disclaimer) t.appendChild(el("span", "tag guard", "⚠️ feragat eklendi"));
  return t;
}

async function gonder(mesaj) {
  if (!mesaj.trim()) return;
  $("#input").value = "";
  $("#send").disabled = true;
  mesajEkle("user", md(mesaj));
  pipelineSifirla();

  const balon = mesajEkle("bot", '<div class="typing"><span></span><span></span><span></span></div>');
  let metin = "";

  try {
    const res = await fetch(API + "/api/chat/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: mesaj, session_id: SESSION_ID, user_id: USER_ID, lang: "tr" }),
    });
    const okuyucu = res.body.getReader();
    const cozucu = new TextDecoder();
    let tampon = "";

    while (true) {
      const { done, value } = await okuyucu.read();
      if (done) break;
      tampon += cozucu.decode(value, { stream: true });
      const parcalar = tampon.split("\n\n");
      tampon = parcalar.pop();

      for (const p of parcalar) {
        const olay = (p.match(/^event: (.+)$/m) || [])[1];
        const ham = (p.match(/^data: (.+)$/m) || [])[1];
        if (!olay || !ham) continue;
        const veri = JSON.parse(ham);

        if (olay === "token") {
          if (!metin) balon.innerHTML = "";
          metin += veri.t;
          balon.innerHTML = md(metin);
          $("#messages").scrollTop = $("#messages").scrollHeight;
        } else if (olay === "done") {
          balon.innerHTML = md(veri.answer);
          balon.appendChild(izRozetleri(veri.trace, veri.sources, veri.disclaimer));
          pipelineIsaretle("done", "done");
          $("#pipeline-detail").appendChild(el("div", null,
            `<b>${veri.trace.latency_ms} ms</b> · ${veri.trace.llm_calls} LLM çağrısı`));
          if (veri.profile) tercihleriYukle();
          if (veri.trace.agents && veri.trace.agents.includes("preference_keeper")) tercihleriYukle();
          if (veri.trace.agents && veri.trace.agents.includes("kvkk_desk")) { tercihleriYukle(); kvkkYukle(); }
        } else if (olay === "error") {
          balon.innerHTML = md("⚠️ Bir hata oluştu: " + veri.mesaj);
        } else {
          pipelineIsaretle(olay);
          if (olay === "classify") {
            pipelineNot(`<b>${veri.yol === "slow" ? "yavaş yol" : "hızlı yol"}</b> — ${veri.gerekce}`);
            pipelineNot("Uzman: " + (veri.ajanlar || []).join(", "));
          } else if (olay === "cache") {
            pipelineNot(veri.hit ? `<b>HIT</b> — benzerlik ${veri.benzerlik}, 0 LLM çağrısı`
                                 : `MISS (en yakın ${veri.benzerlik})`);
            if (veri.hit) { pipelineIsaretle("done", "done"); }
          } else if (olay === "agent") {
            pipelineNot(`🤖 <b>${veri.ad}</b> → ${(veri.araclar || []).join(", ")}`);
          } else if (olay === "llm") {
            pipelineNot(`🧠 ${veri.mod} · ${veri.model}`);
          } else if (olay === "blocked") {
            pipelineNot(`⛔ guardrail: <b>${veri.kategori}</b> — LLM'e gidilmedi`);
          }
        }
      }
    }
  } catch (e) {
    balon.innerHTML = md("⚠️ Sunucuya ulaşılamadı: " + e.message);
  } finally {
    $("#send").disabled = false;
    $("#input").focus();
  }
}

/* ─────────── Tercihler ─────────── */
function cipOlustur(kap, degerler, kume, etiketler) {
  kap.innerHTML = "";
  degerler.forEach((v) => {
    const c = el("span", "chip" + (kume.has(v) ? " on" : ""), etiketler ? etiketler[v] || v : v);
    c.onclick = () => { kume.has(v) ? kume.delete(v) : kume.add(v); c.classList.toggle("on"); };
    kap.appendChild(c);
  });
}

async function tercihleriYukle() {
  const r = await fetch(`${API}/api/preferences?user_id=${USER_ID}`).then((x) => x.json());
  profil = r.profil;
  $("#pref-budget").value = profil.budget_band || "";
  $("#pref-pace").value = profil.pace || "";
  $("#pref-group").value = profil.group || "";
  $("#pref-budget-total").value = profil.budget_total || "";
  secili.styles = new Set(profil.styles || []);
  secili.dietary = new Set(profil.dietary || []);
  secili.accessibility = new Set(profil.accessibility || []);
  cipOlustur($("#pref-styles"), Object.keys(STYLES), secili.styles, STYLES);
  cipOlustur($("#pref-diet"), DIETS, secili.dietary);
  cipOlustur($("#pref-access"), ACCESS, secili.accessibility);

  $("#consent-personalization").checked = !!r.riza.personalization;
  $("#consent-sensitive").checked = !!r.riza.sensitive_data;
  $("#consent-marketing").checked = !!r.riza.marketing;

  const p = $("#personas");
  p.innerHTML = "";
  Object.entries(r.formul.personalar).forEach(([k, ad]) => {
    const c = el("span", "chip" + (profil.persona === k ? " on" : ""), ad);
    c.onclick = () => personaUygula(k);
    p.appendChild(c);
  });
  onerileriCiz(r.oneriler, r.elenenler);
}

function onerileriCiz(oneriler, elenenler) {
  const kap = $("#recommendations");
  kap.innerHTML = "";
  (oneriler || []).forEach((s) => {
    const d = el("div", "rec");
    d.innerHTML = `<span class="score">${s.score.total.toFixed(2)}</span>
      <h4>${s.name}, ${s.country}</h4>
      <p>${s.est_cost_try ? Math.round(s.est_cost_try).toLocaleString("tr-TR") + " TRY tahmini" : ""}</p>
      <div class="bar"><span style="width:${Math.min(100, s.score.total * 100)}%"></span></div>`;
    const acts = el("div", "acts");
    const nedenBtn = el("button", null, "Neden bu öneri?");
    nedenBtn.onclick = () => gonder(`${s.name} için neden bu öneri?`);
    const begen = el("button", null, "👍 Beğendim");
    begen.onclick = () => sinyal("saved", s.key);
    const red = el("button", null, "👎 İlgilenmiyorum");
    red.onclick = () => sinyal("rejected", s.key);
    acts.append(nedenBtn, begen, red);
    d.appendChild(acts);
    kap.appendChild(d);
  });
  if (elenenler && elenenler.length) {
    kap.appendChild(el("div", "rejected", "<b>Sert filtreyle elenenler:</b> " +
      elenenler.map((e) => `${e.name} — ${e.sebep}`).join(" · ")));
  }
}

async function sinyal(kind, target) {
  const r = await fetch(`${API}/api/preferences/signal`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: USER_ID, kind, target }),
  }).then((x) => x.json());
  durum("#pref-status", r.kaydedildi
    ? `Sinyal kaydedildi (${kind}). Öneriler yeniden sıralandı.`
    : "Sinyal kaydedilmedi: kişiselleştirme rızası kapalı (veri minimizasyonu).", !r.kaydedildi);
  tercihleriYukle();
}

async function personaUygula(persona) {
  const r = await fetch(`${API}/api/preferences`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: USER_ID, persona }),
  }).then((x) => x.json());
  durum("#pref-status", r.mesaj, !r.kaydedildi);
  tercihleriYukle();
}

async function tercihleriKaydet() {
  const updates = {
    budget_band: $("#pref-budget").value || null,
    pace: $("#pref-pace").value || null,
    group: $("#pref-group").value || null,
    budget_total: parseFloat($("#pref-budget-total").value) || null,
    styles: [...secili.styles], dietary: [...secili.dietary],
    accessibility: [...secili.accessibility],
  };
  Object.keys(updates).forEach((k) => updates[k] == null && delete updates[k]);
  const r = await fetch(`${API}/api/preferences`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: USER_ID, updates }),
  }).then((x) => x.json());
  durum("#pref-status", r.mesaj, !r.kaydedildi);
  onerileriCiz(r.oneriler, r.elenenler);
}

function durum(sel, mesaj, hata) {
  const n = $(sel);
  n.textContent = mesaj;
  n.className = "status" + (hata ? " err" : "");
  setTimeout(() => { if (n.textContent === mesaj) n.textContent = ""; }, 7000);
}

/* ─────────── KVKK ─────────── */
async function rizaGuncelle() {
  const body = { user_id: USER_ID,
    personalization: $("#consent-personalization").checked,
    sensitive_data: $("#consent-sensitive").checked,
    marketing: $("#consent-marketing").checked };
  const r = await fetch(`${API}/api/kvkk/consent`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }).then((x) => x.json());
  durum("#consent-status", "Rıza güncellendi. Kişiselleştirme: " +
    (r.riza.personalization ? "açık" : "kapalı") + " · m.6: " +
    (r.riza.sensitive_data ? "açık" : "kapalı"));
  tercihleriYukle();
  kvkkYukle();
}

async function kvkkYukle() {
  const r = await fetch(`${API}/api/kvkk/me?user_id=${USER_ID}`).then((x) => x.json());
  const a = $("#audit");
  a.innerHTML = "";
  (r.denetim_izi || []).slice(-12).reverse().forEach((k) => {
    a.appendChild(el("div", null,
      `${(k.ts || "").slice(11, 19)} · <b>${k.action}</b> · ${k.detail || ""}`));
  });
  if (!r.denetim_izi.length) a.innerHTML = '<div class="muted">Henüz kayıt yok.</div>';
}

async function veriGoster() {
  const r = await fetch(`${API}/api/kvkk/me?user_id=${USER_ID}`).then((x) => x.json());
  $("#kvkk-output").textContent = JSON.stringify(r.veri, null, 2);
  kvkkYukle();
}

async function veriSil() {
  if (!confirm("Tüm verileriniz silinecek. Onaylıyor musunuz?")) return;
  const r = await fetch(`${API}/api/kvkk/me?user_id=${USER_ID}&session_id=${SESSION_ID}`,
    { method: "DELETE" }).then((x) => x.json());
  $("#kvkk-output").textContent = JSON.stringify(r, null, 2);
  durum("#consent-status", `Silindi: ${r.silindi.length} anahtar · kalıntı denetimi: ${r.dogrulama}`);
  tercihleriYukle();
  kvkkYukle();
}

/* ─────────── Mimari sekmesi ─────────── */
async function mimariYukle() {
  const r = await fetch(`${API}/api/architecture`).then((x) => x.json());
  const ros = $("#roster");
  ros.innerHTML = "";
  r.uzmanlar.forEach((u) => {
    ros.appendChild(el("div", "agent-row",
      `<b>${u.ad}${u.yuksek_risk ? " ⚠️" : ""}</b>
       <div class="meta">${u.aciklama}</div>
       <div class="meta">kademe: ${u.kaynak_kademeleri.join(", ")} · model: ${u.model_katmani}
       ${u.yapilandirilmis_cikti ? " · output_schema: " + u.yapilandirilmis_cikti : ""}</div>`));
  });
  const tc = $("#toolcat");
  tc.innerHTML = "";
  r.araclar.forEach((t) => {
    tc.appendChild(el("div", "agent-row",
      `<b>${t.ad}</b><div class="meta">${t.aciklama} · ${t.kaynak_kademesi}
       ${t.simule ? '<span class="badge m6">simüle</span>' : ""}</div>`));
  });
  $("#runtime").textContent = JSON.stringify(
    { calisma_modu: r.calisma_modu, modeller: r.modeller, cache: r.cache,
      bilgi_tabani: r.bilgi_tabani, yollar: r.yollar }, null, 2);
}

/* ─────────── Açılış ─────────── */
async function saglik() {
  const r = await fetch(`${API}/api/health`).then((x) => x.json());
  const b = $("#mode-badges");
  b.innerHTML = "";
  b.appendChild(el("span", "pill " + (r.llm.gercek_llm ? "on" : "off"),
    r.llm.gercek_llm ? "LLM: " + r.llm.saglayici : "LLM: mock (anahtarsız)"));
  b.appendChild(el("span", "pill " + (r.cache.backend === "redis" ? "on" : "off"),
    "Cache: " + r.cache.backend));
  b.appendChild(el("span", "pill", r.ajan_sayisi + " ajan · " + r.arac_sayisi + " araç"));
  b.appendChild(el("span", "pill", r.bilgi_tabani.belge + " belge"));
}

async function senaryolariYukle() {
  const r = await fetch(`${API}/api/scenarios`).then((x) => x.json());
  const kap = $("#scenarios");
  kap.innerHTML = "";
  r.senaryolar.forEach((s) => {
    const b = el("button", "scn" + (s.one_cikan ? " star" : ""),
      `<span class="no">${s.no}.</span> <b>${s.baslik}</b>`);
    b.title = s.mesaj + "\n\nBeklenen: " + s.davranis;
    b.onclick = async () => {
      for (const hazirlik of s.kurulum || []) await gonder(hazirlik);
      gonder(s.mesaj);
    };
    kap.appendChild(b);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  $("#composer").onsubmit = (e) => { e.preventDefault(); gonder($("#input").value); };
  $("#save-prefs").onclick = tercihleriKaydet;
  $("#btn-export").onclick = veriGoster;
  $("#btn-delete").onclick = veriSil;
  ["#consent-personalization", "#consent-sensitive", "#consent-marketing"]
    .forEach((s) => { $(s).onchange = rizaGuncelle; });
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".tabpane").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $("#tab-" + t.dataset.tab).classList.add("active");
      if (t.dataset.tab === "arch") mimariYukle();
      if (t.dataset.tab === "kvkk") kvkkYukle();
    };
  });
  saglik(); senaryolariYukle(); tercihleriYukle();
});
