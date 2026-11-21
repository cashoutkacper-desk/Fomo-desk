/* STEP 8 · the iPhone app. Plain JS, no framework, no build step.
   Plan B: opens once as {URL}#d={DATA_PATH}, keeps it in localStorage, polls the JSON
   files under d/{DATA_PATH}/ every 15s. Plan A: {URL}#t={APP_TOKEN}, /api + SSE.
   Neither value is ever written into this file, a log, or the page.
   Every FOMO link shown here is the exact string the desk sent (links.py); this file
   never builds, rewrites or shortens one. */
(function () {
  "use strict";
  const POLL_S = 15;
  const STALE_S = { B: 20 * 60, A: 90 };      // plan B data is minutes behind by design
  const AGENTS = ["scanner", "auditor", "analyst", "executor", "risk"];
  const RANGES = ["LIVE", "1H", "4H", "1D", "1W", "ALL"];
  const MINT_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
  const reduced = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (s, el) => (el || document).querySelector(s);
  const app = () => $("#app");

  /* ------------------------------------------------------------ credentials */
  function readFragment(hash) {
    const p = new URLSearchParams((hash || "").replace(/^#/, ""));
    const d = p.get("d"), t = p.get("t");
    if (d && /^[A-Za-z0-9_-]{32,}$/.test(d)) return { kind: "B", value: d };
    if (t && t.length >= 32) return { kind: "A", value: t };
    return null;
  }
  function loadCred() {
    const f = readFragment(location.hash);
    if (f) {
      localStorage.setItem(f.kind === "B" ? "desk.d" : "desk.t", f.value);
      localStorage.removeItem(f.kind === "B" ? "desk.t" : "desk.d");
    }
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);  // never left in the address bar
    const d = localStorage.getItem("desk.d"), t = localStorage.getItem("desk.t");
    if (d) return { kind: "B", value: d };
    if (t) return { kind: "A", value: t };
    return null;
  }
  const S = {
    cred: null, view: { name: "home" }, range: "LIVE",
    state: null, chart: null, activity: null, account: null, settings: null,
    decisions: {}, coin: {}, audit: {}, actRows: null, actNext: null,
    lastOk: 0, failing: false, retryAt: 0, unreadSeen: +(localStorage.getItem("desk.unreadSeen") || 0),
  };

  /* ------------------------------------------------------------ data */
  function url(kind, arg) {
    if (S.cred.kind === "B") {
      const base = "d/" + encodeURIComponent(S.cred.value) + "/";
      const files = { state: "state.json", chart: "chart.json", account: "account.json", settings: "settings.json" };
      if (files[kind]) return base + files[kind];
      if (kind === "activity") return base + (arg ? "activity/" + (+arg) + ".json" : "activity.json");
      if (kind === "coin") return base + "coin/" + arg + ".json";
      if (kind === "audit") return base + "audit/" + arg + ".json";
      if (kind === "decisions") return base + "agents/" + arg + ".json";
    } else {
      const m = { state: "/api/state", account: "/api/account", settings: "/api/settings" };
      if (m[kind]) return m[kind];
      if (kind === "chart") return "/api/chart?range=" + encodeURIComponent(arg || "LIVE");
      if (kind === "activity") return "/api/activity" + (arg ? "?before=" + (+arg) : "");
      if (kind === "coin") return "/api/coin/" + arg;
      if (kind === "audit") return "/api/audit/" + arg;
      if (kind === "decisions") return "/api/agents/" + arg + "/decisions";
    }
    throw new Error("unknown " + kind);
  }
  async function get(kind, arg) {
    if ((kind === "coin" || kind === "audit") && !MINT_RE.test(arg)) throw new Error("bad mint");
    if (kind === "decisions" && !AGENTS.includes(arg)) throw new Error("bad agent");
    let u = url(kind, arg);
    const opt = { cache: "no-store", headers: {} };
    if (S.cred.kind === "B") u += (u.includes("?") ? "&" : "?") + "_=" + Date.now();   // past the CDN cache
    else opt.headers.Authorization = "Bearer " + S.cred.value;
    const r = await fetch(u, opt);
    if (r.status === 404 && (kind === "coin" || kind === "audit")) return { missing: true };
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  async function post(path, body) {
    if (S.cred.kind !== "A") throw new Error("read-only on this hosting");
    const r = await fetch(path, { method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + S.cred.value },
      body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }
  async function getChart() {
    if (S.cred.kind === "B") return get("chart");
    const out = Object.assign({}, S.chart || {});
    out[S.range] = await get("chart", S.range);
    return out;
  }

  /* ------------------------------------------------------------ format */
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeLink = (u) => (typeof u === "string" && u.startsWith("https://") && (u.includes("r=savipww") || u.endsWith("/r/savipww"))) ? u : null;
  const money = (v) => v == null || !isFinite(+v) ? "n/a" : "$" + (+v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const big = (v) => {
    if (v == null || v === "n/a" || !isFinite(+v)) return "n/a";
    v = +v; const a = Math.abs(v);
    if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
    return "$" + v.toFixed(0);
  };
  const SUB = "₀₁₂₃₄₅₆₇₈₉";
  const priceFmt = (p) => {                       // small prices: $0.0₅4078 = 0.000004078 (5 zeros)
    if (p == null || !isFinite(+p)) return "n/a";
    p = +p; if (p >= 1) return "$" + p.toLocaleString("en-US", { maximumFractionDigits: 4 });
    if (p >= 0.001 || p <= 0) return "$" + p.toFixed(p >= 0.01 ? 4 : 6);
    const zeros = Math.floor(-Math.log10(p));              // zeros after the decimal point
    const sig = Math.round(p * Math.pow(10, zeros + 4)).toString().slice(0, 4);
    return "$0.0" + String(zeros).split("").map(d => SUB[+d]).join("") + sig;
  };
  const pct = (v) => v == null || !isFinite(+v) ? "" : (v >= 0 ? "+" : "") + (+v).toFixed(2) + "%";
  const hhmm = (iso) => { const t = Date.parse(iso); return isFinite(t) ? new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : ""; };
  const valClass = (v) => typeof v !== "string" ? "" : v.startsWith("+") ? "pos" : v.startsWith("-") ? "neg" : v === "Skipped" ? "faint" : "";

  const ICON = {
    back: '<svg class="icon" viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>',
    search: '<svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg>',
    bell: '<svg class="icon" viewBox="0 0 24 24"><path d="M6 16V11a6 6 0 0112 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 004 0"/></svg>',
    home: '<svg viewBox="0 0 24 24"><path d="M4 11l8-7 8 7v9h-5v-6h-6v6H4z"/></svg>',
    agents: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/></svg>',
    activity: '<svg viewBox="0 0 24 24"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>',
    account: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="4"/><path d="M4 20c1.5-4 4.5-5.5 8-5.5s6.5 1.5 8 5.5"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    chev: '<svg class="icon" viewBox="0 0 24 24" style="width:16px;height:16px;stroke:rgba(235,235,245,0.3)"><path d="M9 5l7 7-7 7"/></svg>',
  };
  const KIND_IC = {
    bought: ['#0a84ff', '<path d="M12 5v14M5 12h14"/>'],
    sold: ['#00d35a', '<path d="M5 12.5l4.5 4.5L19 7.5"/>'],
    stopped: ['#ff453a', '<path d="M12 5v14M6 13l6 6 6-6"/>'],
    rejected: ['rgba(235,235,245,0.6)', '<path d="M6 6l12 12M18 6L6 18"/>'],
  };

  /* ------------------------------------------------------------ odometer */
  class Odo {
    constructor(el) { this.el = el; this.text = null; }
    set(text) {
      if (text === this.text) return;
      const prev = this.text; this.text = text;
      const animate = prev !== null && prev.length === text.length && !reduced();
      if (!animate) {
        this.el.classList.add("odo"); this.el.classList.remove("animate");
        this.el.innerHTML = [...text].map(ch => /\d/.test(ch)
          ? `<span class="dig"><span class="col" style="transform:translateY(${-ch * 1.05}em)">${[...Array(10).keys()].map(i => `<span>${i}</span>`).join("")}</span></span>`
          : `<span class="ch">${esc(ch)}</span>`).join("");
        return;
      }
      this.el.classList.add("odo", "animate");
      const kids = [...this.el.children];
      let fromRight = 0;
      for (let i = text.length - 1; i >= 0; i--) {
        const ch = text[i], node = kids[i];
        if (/\d/.test(ch) && node && node.classList.contains("dig")) {
          const col = node.firstChild;
          if (prev[i] !== ch) {                          // each changed digit rolls 600ms, 30ms stagger right to left
            col.style.transitionDelay = (fromRight * 30) + "ms"; fromRight++;
            col.style.transform = `translateY(${-ch * 1.05}em)`;
          }
        } else if (node) { node.textContent = ch; }
      }
    }
  }

  /* ------------------------------------------------------------ shell */
  function navbar(inner) { return `<div class="navbar">${inner}</div><div class="errrow hidden"></div>`; }
  function backBar(extra) {
    return navbar(`<button class="tap back" data-act="back">${ICON.back}<span>Back</span></button><span class="grow"></span>${extra || ""}`);
  }
  function paperBadge() { return S.state && S.state.mode === "PAPER" ? '<span class="badge-paper">PAPER</span>' : ""; }
  function tabbar() {
    const cur = S.view.name;
    const tab = (n, label) => `<button data-tab="${n}" class="${cur === n ? "on" : ""}" aria-label="${label}">${ICON[n]}<span>${label}</span></button>`;
    return `<nav class="tabbar">${tab("home", "Home")}${tab("agents", "Agents")}${tab("activity", "Activity")}${tab("account", "Account")}</nav>`;
  }
  function go(view, push) {
    if (push) history.pushState({ view }, "", location.pathname + location.search);
    else history.replaceState({ view }, "", location.pathname + location.search);
    S.view = view; mount(); refresh();
  }
  window.addEventListener("popstate", (e) => { S.view = (e.state && e.state.view) || { name: "home" }; mount(); refresh(); });

  /* ------------------------------------------------------------ error row */
  function staleness() {
    if (!S.state) return false;
    const age = (Date.now() - Date.parse(S.state.ts)) / 1000;
    return isFinite(age) && age > STALE_S[S.cred.kind];
  }
  function tickError() {
    const el = $(".errrow"); if (!el) return;
    const bad = S.failing || staleness();
    if (!bad) { el.classList.add("hidden"); return; }
    const n = Math.max(0, Math.ceil((S.retryAt - Date.now()) / 1000));
    const last = S.state ? hhmm(S.state.ts) : "never";
    el.textContent = `Desk offline · retrying in ${n}s · last update ${last}`;
    el.classList.remove("hidden");
  }

  /* ------------------------------------------------------------ views */
  const V = {};
  let odo = null, line = null;

  V.home = {
    mount() {
      const handle = (S.account && S.account.fomoHandle) || "";
      app().innerHTML = `<div class="screen">${navbar(`
        <button class="tap" data-tab="account" aria-label="Account"><span class="avatar">${esc((handle[0] || "G").toUpperCase())}</span></button>
        <button class="title" data-tab="agents">Grok Bot <span class="muted">›</span></button>
        <button class="tap" data-act="search" aria-label="Search">${ICON.search}</button>
        <button class="tap bell" data-act="bell" aria-label="Notifications">${ICON.bell}<span class="dot hidden"></span></button>`)}
        <div id="h-bal"></div>
        <div class="chart-wrap"><canvas id="h-chart"></canvas><div class="chart-empty hidden">Waiting for the first trade</div></div>
        <div class="ranges">${RANGES.map(r => `<button data-range="${r}" class="${S.range === r ? "on" : ""}">${r}</button>`).join("")}</div>
        <h2 class="section"><span id="h-agents-t">Agents</span></h2>
        <div class="carousel" id="h-cards"></div>
        <h2 class="section">Activity <button class="more" data-tab="activity">See all</button></h2>
        <div id="h-act"></div></div>${tabbar()}`;
      odo = null; line = new DeskChart.LineChart($("#h-chart"));
      this.update();
    },
    update() {
      const st = S.state;
      const bal = $("#h-bal");
      if (!st) {
        bal.innerHTML = `<div class="sk" style="height:70px;width:70%;margin-top:8px"></div><div class="sk" style="height:22px;width:45%;margin-top:10px"></div>`;
        $("#h-cards").innerHTML = AGENTS.map(() => `<div class="acard sk"></div>`).join("");
        $("#h-act").innerHTML = `<div class="sk" style="height:300px"></div>`;
        return;
      }
      if (!odo) {
        bal.innerHTML = `<div class="balance-row"><span class="bal" id="h-odo"></span>${paperBadge()}</div>
          <div class="subrow"><span id="h-today"></span><span class="pill-mult" id="h-mult"></span></div>`;
        odo = new Odo($("#h-odo"));
      }
      odo.set(money(st.balance));
      const h = (S.account && S.account.fomoHandle) || ""; const av = $(".avatar"); if (av && h) av.textContent = h[0].toUpperCase();
      const today = $("#h-today"); today.textContent = pct(st.todayPct) + " today"; today.className = st.todayPct >= 0 ? "pos" : "neg";
      $("#h-mult").textContent = (+st.multiplier).toFixed(2) + "×";
      const dot = $(".bell .dot"); if (dot) dot.classList.toggle("hidden", !(st.unread > S.unreadSeen));
      const running = st.agents.filter(a => a.status === "running").length;
      $("#h-agents-t").textContent = `Agents · ${running} running`;
      const car = $("#h-cards"), sl = car.scrollLeft;
      car.innerHTML = st.agents.map(agentCard).join("");
      car.scrollLeft = sl;
      car.querySelectorAll("canvas.ring").forEach(c => DeskChart.ring(c, c.dataset.score === "" ? null : +c.dataset.score, +c.dataset.green));
      this.chart();
      const rows = S.activity ? S.activity.rows.slice(0, 5) : null;
      $("#h-act").innerHTML = rows === null ? `<div class="sk" style="height:300px"></div>` : activityList(rows);
    },
    chart() {
      if (!S.chart || !S.chart[S.range]) return;
      const c = S.chart[S.range];
      $(".chart-empty").classList.toggle("hidden", (c.points || []).length >= 2);
      line.set(c, S.range);
    },
  };

  function agentCard(a) {
    const k = a.name.toLowerCase();
    const who = (color) => `<div class="who"><span class="sdot ${esc(a.status)}"></span><span style="color:${color || "#fff"}">${esc(a.name)}</span></div>`;
    let body = "";
    if (k === "scanner") {
      const feed = (a.feed || []).slice(0, 4).map(f => f.verdict === "kill"
        ? `<div class="row killed"><span class="nm">${esc(f.name)}</span><span class="why">${esc(f.reason || "")}</span></div>`
        : `<div class="row"><span>${esc(f.name)}</span><span class="ok">→ auditor</span></div>`).join("");
      body = `${who("#64d2ff")}<div class="head">${esc(a.headline)}</div>
        <div class="feed">${feed || '<div class="muted">Listening for graduations</div>'}</div>`;
    } else if (k === "auditor") {
      const L = a.layers || [];
      const strips = [...Array(8).keys()].map(i => { const l = L[i]; return `<div class="${l && l.pass === true ? "g" : l && l.pass === false ? "r" : ""}"></div>`; }).join("");
      const done = L.length === 8 && L.every(l => l.pass !== null);
      const status = !L.length ? '<span class="muted">No coin taken apart yet</span>'
        : !done && !L.some(l => l.pass === false) ? "taking apart..."
        : a.result === "8/8 → Executor" ? `<span class="pos">8/8 → Executor</span>` : `<span class="neg">${esc(a.result || "")}</span>`;
      body = `${who()}<div class="head">${esc(a.headline)}</div><div class="strips">${strips}</div>
        <div style="margin-top:12px;font-size:15px">${status}</div><div class="foot">${esc(a.rejectedText || "")}</div>`;
    } else if (k === "analyst") {
      body = `${who("#ffd60a")}<div class="head">${esc(a.headline)}</div>
        <canvas class="ring" data-score="${a.score == null ? "" : +a.score}" data-green="${+a.greenFrom || 70}"></canvas>
        <div class="foot">green from ${+a.greenFrom || 70}</div>`;
    } else if (k === "executor") {
      let bar = '<div class="muted" style="margin-top:12px;font-size:15px">No signal yet tonight</div>';
      if (a.lastStatus === "sent") bar = `<div class="bar indet"><i></i></div><div style="margin-top:8px;font-size:15px">Executing...</div>`;
      else if (a.lastStatus === "bought") bar = `<div class="bar"><i style="width:100%"></i></div><div class="pos" style="margin-top:8px;font-size:15px">✓ Filled</div>`;
      else if (a.lastStatus) bar = `<div class="muted" style="margin-top:12px;font-size:15px">Last signal ${esc(a.lastStatus)}</div>`;
      body = `${who("#00d35a")}<div class="head">${esc(a.headline)}</div>
        <div style="margin-top:6px;font-size:17px">${esc(a.lastText || "")}</div>${bar}`;
    } else if (k === "risk") {
      const pills = (a.pills || []).map(p => `<span class="tpill ${p.color === "red" ? "red" : "green"}">${esc(p.text)}</span>`).join(" ");
      body = `${who("#ff6b9a")}<div class="head">${esc(a.headline)}</div>
        <div style="margin-top:8px;font-size:17px">${esc(a.lastExit || (a.open ? a.open + " open" : "No position open"))}</div>
        <div class="foot">${pills}</div>`;
    }
    const tgt = k === "auditor" ? `data-audit="${esc(a.lastMint || "")}"` : `data-tab="agents"`;
    return `<div class="acard" role="button" ${tgt}>${body}</div>`;
  }

  function activityList(rows) {
    if (!rows.length) return `<div class="list"><div class="empty">Nothing yet tonight</div></div>`;
    return `<div class="list">${rows.map(r => {
      const [col, path] = KIND_IC[r.kind] || KIND_IC.rejected;
      const target = r.mint && MINT_RE.test(r.mint) ? (r.open === "audit" ? `data-audit="${esc(r.mint)}"` : `data-coin="${esc(r.mint)}"`) : "";
      return `<button class="lrow" ${target}><span class="ic"><svg viewBox="0 0 24 24" style="stroke:${col}">${path}</svg></span>
        <span class="tx"><div class="t1">${esc(r.title)}</div><div class="t2">${esc(r.subtitle)}</div></span>
        <span class="rv ${valClass(r.value)}">${esc(r.value)}</span></button>`;
    }).join("")}</div>`;
  }

  V.activity = {
    mount() {
      app().innerHTML = `<div class="screen">${navbar('<span class="title">Activity</span>')}<h1 class="large">Activity</h1>
        <div id="a-list"></div><div id="a-more"></div></div>${tabbar()}`;
      this.update();
    },
    update() {
      if (S.actRows === null) { $("#a-list").innerHTML = `<div class="sk" style="height:480px"></div>`; return; }
      $("#a-list").innerHTML = activityList(S.actRows);
      $("#a-more").innerHTML = S.actNext ? `<button class="linkbtn" data-act="more" style="width:100%;justify-content:center">Load more</button>` : "";
    },
  };

  V.agents = {
    mount() {
      app().innerHTML = `<div class="screen">${navbar('<span class="title">Agents</span>')}<h1 class="large">Agents</h1><div id="g-list"></div></div>${tabbar()}`;
      this.update();
    },
    update() {
      const st = S.state;
      if (!st) { $("#g-list").innerHTML = AGENTS.map(() => `<div class="fcard sk" style="height:220px"></div>`).join(""); return; }
      const ro = S.cred.kind === "B";
      $("#g-list").innerHTML = st.agents.map(a => {
        const k = a.name.toLowerCase();
        const d = S.decisions[k];
        const locked = k === "risk" && a.locked;
        const list = !d ? `<div class="sk" style="height:120px;margin-top:12px"></div>`
          : !d.decisions.length ? `<div class="muted" style="padding:12px 0 0;font-size:15px">${k === "scanner" ? "Listening for graduations" : "No decisions yet"}</div>`
          : `<div class="dec">${d.decisions.map(x => {
              const l = safeLink(x.link);
              const coin = l ? `<a class="cn" href="${esc(l)}" target="_blank" rel="noopener">${esc(x.coin)}</a>` : esc(x.coin);
              return `<div class="d"><span class="tm">${hhmm(x.ts)}</span><span>${coin}</span><span class="v-${esc(x.verdict)}">${esc(x.verdict)}</span>
                ${x.reason ? `<span class="rs">${esc(x.reason)}</span>` : ""}</div>`;
            }).join("")}</div>`;
        const group = k === "analyst" ? "analyst" : k;
        return `<div class="fcard"><div class="top"><span class="nm"><span class="sdot ${esc(a.status)}"></span>${esc(a.name)}</span>
            <label class="swtap" aria-label="Pause or resume ${esc(a.name)}"><span class="sw"><input type="checkbox" data-pause="${k}" ${a.status !== "paused" ? "checked" : ""} ${ro || locked ? "disabled" : ""}><span></span></span></label></div>
          <div class="cnt">${esc(a.status)} · ${esc(a.headline || "")}${locked ? " · locked while a position is open" : ""}</div>
          ${list}
          <button class="linkbtn" data-settings="${group}">Settings</button></div>`;
      }).join("") + (ro ? `<div class="note" style="padding:0 4px">Pause and resume are read-only on this hosting. They go through Telegram commands.</div>` : "");
    },
  };

  V.coin = {
    mount() {
      app().innerHTML = `<div class="screen">${backBar(paperBadge())}<div id="c-body"></div></div>${tabbar()}`;
      this.update();
    },
    update() {
      const m = S.view.mint, c = S.coin[m], body = $("#c-body");
      if (!c) {
        body.innerHTML = `<div class="sk" style="height:34px;width:60%;margin-top:8px"></div><div class="sk" style="height:70px;width:70%;margin-top:12px"></div>
          <div class="sk" style="height:260px;margin-top:12px"></div><div class="sk" style="height:130px;margin-top:16px"></div>`;
        return;
      }
      if (c.missing) { body.innerHTML = `<div class="empty">No data for this coin yet. The desk publishes coins it saw tonight.</div>`; return; }
      const p = c.position, link = safeLink(c.coin_url);
      const since = p && p.sinceEntryPct != null ? `<div class="${p.sinceEntryPct >= 0 ? "pos" : "neg"}" style="font-size:17px">${p.sinceEntryPct >= 0 ? "▲" : "▼"} ${Math.abs(p.sinceEntryPct).toFixed(2)}% since entry</div>` : "";
      const shield = c.shield ? `<button class="chip ${c.shield === "8/8" ? "" : "r"}" data-audit="${esc(m)}">${c.shield}</button>` : "";
      const sub = [c.symbol, c.chain, c.ageText].filter(Boolean).map(esc).join(" · ");
      const exit = p ? `<div class="exitcard">
          <div class="vrow"><span>Your position</span><span class="v">${esc(p.text)}</span></div>
          <div class="vrow"><span>Stop line · -${+p.stopPct}%</span><span class="v r">${priceFmt(p.stopLine)}</span></div>
          <div class="vrow"><span>Trail line</span><span class="v ${p.trailActive ? "g" : "n"}">${p.trailActive ? priceFmt(p.trailLine) : "not active yet"}</span></div>
          <div class="vrow"><span>Time left to timeout</span><span class="v">${Math.floor(p.timeoutLeftMin / 60)}h ${p.timeoutLeftMin % 60}m</span></div></div>` : "";
      const s = c.stats || {};
      body.innerHTML = `<div class="coinname">${esc(c.name || c.symbol || m.slice(0, 4) + "..." + m.slice(-3))} ${shield}</div>
        <div class="muted" style="font-size:15px;margin-top:2px">${sub}</div>
        <div class="price" style="font-size:${Math.min(66, Math.floor((innerWidth - 32) / (priceFmt(c.price).length * 0.62)))}px">${priceFmt(c.price)}</div>${since}
        <div class="cwrap"><canvas id="c-candles"></canvas></div>
        <div class="stats"><div><div class="k">Market cap</div><div class="v">${big(s.marketCap)}</div></div>
          <div><div class="k">Liquidity</div><div class="v">${big(s.liquidity)}</div></div>
          <div><div class="k">24h volume</div><div class="v">${big(s.volume24h)}</div></div>
          <div><div class="k">Holders</div><div class="v">${s.holders == null || s.holders === "n/a" ? "n/a" : (+s.holders).toLocaleString("en-US")}</div></div></div>
        ${exit}
        ${link ? `<a class="bigbtn ${p ? "sell" : ""}" href="${esc(link)}" target="_blank" rel="noopener">${p ? "Sell on FOMO" : "Buy on FOMO"}</a>` : ""}`;
      DeskChart.candles($("#c-candles"), c.candles || [], { exitLine: p ? p.exitLine : null, fmt: priceFmt });
    },
  };

  const beam = { mint: null, i: 0, timer: 0 };
  V.audit = {
    mount() {
      clearTimeout(beam.timer); beam.mint = null;
      app().innerHTML = `<div class="screen">${backBar()}<h1 class="large" id="u-title">Auditor</h1><div id="u-body"></div></div>${tabbar()}`;
      this.update();
    },
    update() {
      const m = S.view.mint, a = m ? S.audit[m] : null, body = $("#u-body");
      if (m && !a) { body.innerHTML = `<div class="sk" style="height:250px;width:220px;margin:18px auto"></div><div class="sk" style="height:380px"></div>`; return; }
      if (!m || a.missing || !a.audited) {
        const link = a && safeLink(a.coin_url);
        if (a && a.name) $("#u-title").textContent = a.name;
        body.innerHTML = `<div class="empty">No coin taken apart yet</div>${link ? `<a class="bigbtn" href="${esc(link)}" target="_blank" rel="noopener">Open on FOMO</a>` : ""}`;
        return;
      }
      $("#u-title").textContent = a.name;
      if (!$("#u-discs")) {
        const ro = S.cred.kind === "B";
        body.innerHTML = `<div class="discs" id="u-discs">${[...Array(8).keys()].map(i => `<div class="disc" style="top:${i * 26}px;z-index:${8 - i}"><svg viewBox="0 0 24 24"></svg></div>`).join("")}<div class="beam"></div></div>
          <div class="verdict"><div class="h" id="u-h">&nbsp;</div><div class="s" id="u-s">&nbsp;</div></div>
          <div class="list plain" id="u-rows"></div>
          <div class="list plain" style="margin-top:16px"><div class="vrow"><span>Take apart before buying</span>
            <label class="swtap"><span class="sw"><input type="checkbox" id="u-enabled" ${a.enabled ? "checked" : ""} ${ro ? "disabled" : ""}><span></span></span></label></div></div>
          ${ro ? `<div class="note">Read-only on this hosting. Changes go through Telegram commands.</div>` : ""}
          <a class="bigbtn" id="u-fomo" target="_blank" rel="noopener">Open on FOMO</a>`;
      }
      const link = safeLink(a.coin_url); const fb = $("#u-fomo");
      if (link) fb.href = link; else fb.remove();
      $("#u-enabled").checked = !!a.enabled;
      this.run(a);
    },
    run(a) {
      if (beam.mint !== a.mint) { beam.mint = a.mint; beam.i = 0; }
      const discs = [...document.querySelectorAll(".disc")], b = $(".beam"), rows = $("#u-rows");
      const paint = (i) => { const l = a.layers[i]; discs[i].classList.add(l.pass ? "g" : "r");
        discs[i].firstChild.innerHTML = l.pass ? '<path d="M5 12.5l4.5 4.5L19 7.5"/>' : '<path d="M6 6l12 12M18 6L6 18"/>'; };
      const renderRows = (upto) => {
        rows.innerHTML = a.layers.map((l, i) => `<div class="vrow"><span>${esc(l.name)}</span><span class="v ${i < upto ? (l.pass ? "g" : "r") : "n"}">${i < upto ? esc(l.value) : "…"}</span></div>`).join("");
      };
      const finish = () => {
        b.classList.remove("on");
        $("#u-h").textContent = a.headline; $("#u-h").className = "h " + (a.passed ? "pos" : "neg");
        $("#u-s").textContent = a.subline || "";
      };
      if (reduced()) {                                   // values swap instantly
        a.layers.forEach((l, i) => { if (l.pass !== null) paint(i); });
        const all = a.layers.every(l => l.pass !== null);
        renderRows(all ? 8 : a.layers.findIndex(l => l.pass === null));
        if (all || a.layers.some(l => l.pass === false)) finish();
        return;
      }
      clearTimeout(beam.timer);
      for (let j = 0; j < beam.i; j++) paint(j);
      renderRows(beam.i);
      const step = () => {
        if (beam.i >= 8) { finish(); return; }
        const i = beam.i;
        b.classList.add("on"); b.style.top = (i * 26 + 21) + "px";      // 350ms per layer, top to bottom
        beam.timer = setTimeout(() => {
          const l = a.layers[i];
          if (l.pass === null) return;                    // the server hasn't sent this layer: the beam waits
          paint(i); beam.i = i + 1; renderRows(beam.i);  // 150ms to green or red (CSS)
          beam.timer = setTimeout(step, 150);
        }, 350);
      };
      if (beam.i >= 8) finish(); else step();
    },
  };

  V.account = {
    mount() {
      app().innerHTML = `<div class="screen">${navbar('<span class="title">Account</span>')}<h1 class="large">Account</h1><div id="x-body"></div></div>${tabbar()}`;
      this.update();
    },
    update() {
      const ac = S.account, st = S.settings, body = $("#x-body");
      if (!ac || !st) { body.innerHTML = `<div class="sk" style="height:110px"></div><div class="sk" style="height:180px;margin-top:16px"></div><div class="sk" style="height:400px;margin-top:16px"></div>`; return; }
      if (body.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // don't clobber typing
      const ro = S.cred.kind === "B" || st.readOnly;
      const stackKey = ac.mode === "LIVE" ? "live_stack" : "paper_start_stack";
      const desk = (st.groups.desk || []);
      const stackSpec = desk.find(s => s.key === stackKey);
      const prof = safeLink(ac.profile_url), sign = safeLink(ac.signup_url);
      const standalone = window.navigator.standalone === true || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
      const pushRow = S.cred.kind === "B"
        ? `<div class="vrow"><span>Notifications</span><span class="v n">Telegram only</span></div><div class="note" style="padding-bottom:10px">This hosting has no endpoint for web push, so pushes come by Telegram.</div>`
        : standalone ? `<button class="lrow" data-act="enablepush"><span class="tx"><div class="t1">Enable notifications</div></span>${ICON.chev}</button>` : "";
      const GROUPS = { scanner: "Scanner", auditor: "Auditor", analyst: "Analyst", analyst_rules: "Analyst · rules mode", executor: "Executor", risk: "Risk", desk: "Desk" };
      const groups = Object.keys(st.groups).map(g => `<div class="setgroup" id="set-${esc(g)}"><h3>${esc(GROUPS[g] || g)}</h3><div class="list plain">${
        st.groups[g].filter(s => s.key !== "mode").map(s => settingRow(s, ro)).join("")}</div></div>`).join("");
      body.innerHTML = `
        <div class="list plain">
          <div class="vrow"><span>Mode</span><div class="seg" style="width:180px">
            <button data-mode="PAPER" class="${ac.mode === "PAPER" ? "on" : ""}" ${ro ? "disabled" : ""}>PAPER</button>
            <button data-mode="LIVE" class="${ac.mode === "LIVE" ? "on" : ""}" ${ro ? "disabled" : ""}>LIVE</button></div></div>
          <div class="vrow"><span>Stack</span><input class="field" type="number" inputmode="decimal" data-key="${stackKey}" value="${stackSpec ? +stackSpec.value : ""}" ${ro ? "disabled" : ""}></div>
          <div class="vrow"><span>Balance</span><span class="v">${money(ac.stack)}</span></div>
        </div>
        ${ro ? `<div class="note">Read-only on this hosting. Mode, stack and settings change through Telegram commands.</div>` : ""}
        <div class="list" style="margin-top:20px">
          ${prof ? `<a class="lrow" href="${esc(prof)}" target="_blank" rel="noopener"><span class="tx"><div class="t1">Your FOMO profile</div><div class="t2">@${esc(ac.fomoHandle)}</div></span>${ICON.chev}</a>` : ""}
          ${sign ? `<button class="lrow" data-act="invite"><span class="tx"><div class="t1">Invite a friend</div><div class="t2">Share your FOMO invite link</div></span>${ICON.chev}</button>` : ""}
        </div>
        <div class="list plain" style="margin-top:20px">
          <div class="vrow"><span>Telegram</span><span class="v ${ac.telegram && ac.telegram.connected ? "g" : "r"}">${ac.telegram && ac.telegram.connected ? "Connected" : "Not connected"}</span></div>
          <div class="vrow"><span>Send test push</span><button class="linkbtn" disabled style="color:rgba(235,235,245,0.3)">Send</button></div>
          ${pushRow}
        </div>
        <div class="note">Send test push has no endpoint on this hosting.</div>
        ${groups}`;
    },
  };

  function settingRow(s, ro) {
    const dis = ro ? "disabled" : "";
    const label = esc(s.key.split(".").pop().replace(/_/g, " "));
    if (s.kind === "bool") {
      return `<div class="setrow"><div class="k"><span>${label}</span><label class="swtap"><span class="sw"><input type="checkbox" data-key="${esc(s.key)}" ${s.value ? "checked" : ""} ${dis}><span></span></span></label></div></div>`;
    }
    if (s.kind === "choice") {
      return `<div class="setrow"><div class="k"><span>${label}</span><div class="seg" style="min-width:150px">${(s.choices || []).map(c =>
        `<button data-key="${esc(s.key)}" data-choice="${esc(c)}" class="${c === s.value ? "on" : ""}" ${dis}>${esc(c)}</button>`).join("")}</div></div></div>`;
    }
    if (s.kind === "int" || s.kind === "float") {
      const step = s.kind === "int" ? 1 : ((s.max - s.min) <= 1 ? 0.01 : (s.max - s.min) <= 100 ? 0.5 : 1);
      return `<div class="setrow"><div class="k"><span>${label}</span><span class="faint" style="font-size:12px;font-family:var(--mono)">${s.min}–${s.max}</span></div>
        <div class="ctl"><input type="range" min="${s.min}" max="${s.max}" step="${step}" value="${+s.value}" data-key="${esc(s.key)}" data-pair="1" ${dis}>
        <input type="number" inputmode="decimal" min="${s.min}" max="${s.max}" step="any" value="${+s.value}" data-key="${esc(s.key)}" ${dis}></div></div>`;
    }
    return `<div class="setrow"><div class="k"><span>${label}</span><input type="text" style="width:190px" data-key="${esc(s.key)}" value="${esc(s.value)}" ${dis}></div></div>`;
  }

  /* ------------------------------------------------------------ search */
  function openSearch() {
    const idx = new Map();
    const add = (mint, name) => { if (mint && MINT_RE.test(mint) && !idx.has(mint)) idx.set(mint, name || ""); };
    (S.state ? S.state.agents : []).forEach(a => (a.feed || []).forEach(f => add(f.mint, f.name)));
    (S.actRows || (S.activity && S.activity.rows) || []).forEach(r => add(r.mint, r.title.replace(/^(Bought|Sold|Stopped|Rejected) /, "")));
    Object.values(S.decisions).forEach(d => d.decisions.forEach(x => add(x.mint, x.coin)));
    Object.values(S.coin).forEach(c => c && c.mint && add(c.mint, [c.name, c.symbol].filter(Boolean).join(" ")));
    const ov = document.createElement("div"); ov.className = "overlay";
    ov.innerHTML = `<div class="searchbar"><input id="q" placeholder="Name, symbol or mint" autocomplete="off" autocapitalize="off" spellcheck="false"><button class="tap" style="color:#0a84ff" data-close>Cancel</button></div><div class="list" style="margin-top:12px" id="qres"></div>`;
    document.body.appendChild(ov);
    const res = $("#qres", ov), q = $("#q", ov);
    const draw = () => {
      const s = q.value.trim().toLowerCase();
      const hits = [...idx.entries()].filter(([m, n]) => !s || m.toLowerCase().includes(s) || n.toLowerCase().includes(s)).slice(0, 30);
      if (MINT_RE.test(q.value.trim()) && !idx.has(q.value.trim())) hits.unshift([q.value.trim(), "Open this mint"]);
      res.innerHTML = hits.length ? hits.map(([m, n]) => `<button class="lrow" data-coin="${esc(m)}"><span class="tx"><div class="t1">${esc(n || m.slice(0, 4) + "..." + m.slice(-3))}</div><div class="t2">${esc(m)}</div></span>${ICON.chev}</button>`).join("")
        : `<div class="empty">No coin matches</div>`;
    };
    q.addEventListener("input", draw); draw(); setTimeout(() => q.focus(), 50);
    ov.addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.closest("[data-close]")) ov.remove();
      const c = e.target.closest("[data-coin]"); if (c) { ov.remove(); go({ name: "coin", mint: c.dataset.coin }, true); }
    });
    AGENTS.forEach(a => { if (!S.decisions[a]) get("decisions", a).then(d => { S.decisions[a] = d; d.decisions.forEach(x => add(x.mint, x.coin)); draw(); }).catch(() => {}); });
  }

  /* ------------------------------------------------------------ modals, toast */
  function toast(text) {
    const t = document.createElement("div"); t.className = "toast"; t.textContent = text;
    document.body.appendChild(t); setTimeout(() => t.remove(), 1800);
  }
  function modal(title, text, opts) {
    return new Promise((resolve) => {
      const m = document.createElement("div"); m.className = "modal";
      m.innerHTML = `<div class="box"><h4>${esc(title)}</h4><p>${esc(text)}</p>${opts.input ? '<input id="mi" autocomplete="off" autocapitalize="characters">' : ""}
        <div class="acts"><button data-r="0">Cancel</button><button data-r="1" class="${opts.danger ? "danger" : "primary"}">${esc(opts.ok || "OK")}</button></div></div>`;
      document.body.appendChild(m);
      m.addEventListener("click", (e) => { const b = e.target.closest("[data-r]"); if (!b) return;
        const v = opts.input ? ($("#mi", m).value || "") : true; m.remove(); resolve(b.dataset.r === "1" ? v : null); });
    });
  }

  /* ------------------------------------------------------------ actions (plan A writes) */
  async function setSetting(key, value) {
    try { const r = await post("/api/settings", { key, value });
      toast((r.message || "Saved") + (r.ticket && r.ticket.message ? " · " + r.ticket.message : "")); }
    catch (e) { toast(e.message); }
    refresh();
  }
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-tab],[data-act],[data-coin],[data-audit],[data-range],[data-settings],[data-mode],[data-choice]");
    if (!t) return;
    if (t.dataset.tab) return go({ name: t.dataset.tab }, false);
    if (t.dataset.coin) return go({ name: "coin", mint: t.dataset.coin }, true);
    if (t.hasAttribute("data-audit")) return go({ name: "audit", mint: t.dataset.audit || null }, true);
    if (t.dataset.range) { S.range = t.dataset.range; document.querySelectorAll("[data-range]").forEach(b => b.classList.toggle("on", b === t));
      if (S.cred.kind === "A") S.chart = await getChart().catch(() => S.chart); V.home.chart(); return; }
    if (t.dataset.settings) { go({ name: "account" }, false); setTimeout(() => { const g = $("#set-" + t.dataset.settings); if (g) g.scrollIntoView(); }, 300); return; }
    if (t.dataset.choice && !t.disabled) return setSetting(t.dataset.key, t.dataset.choice);
    if (t.dataset.mode && !t.disabled) {
      if (t.dataset.mode === "LIVE") {
        const v = await modal("Switch to LIVE", "Type LIVE to confirm. The desk needs one full PAPER night first.", { input: true, ok: "Switch", danger: true });
        if (v === null) return;
        try { await post("/api/mode", { mode: "LIVE", confirm: v.trim() }); toast("LIVE"); } catch (err) { toast(err.message); }
      } else { try { await post("/api/mode", { mode: "PAPER" }); toast("PAPER"); } catch (err) { toast(err.message); } }
      return refresh();
    }
    const act = t.dataset.act;
    if (act === "back") { if (history.state && history.length > 1) history.back(); else go({ name: "home" }, false); }
    else if (act === "search") openSearch();
    else if (act === "bell") { if (S.state) { S.unreadSeen = S.state.unread; localStorage.setItem("desk.unreadSeen", S.unreadSeen); } go({ name: "activity" }, false); }
    else if (act === "more") loadMore();
    else if (act === "invite") invite();
    else if (act === "enablepush") enablePush();
  });
  document.addEventListener("change", async (e) => {
    const el = e.target;
    if (el.dataset.pause && !el.disabled) {
      try { await post(`/api/agents/${el.dataset.pause}/${el.checked ? "resume" : "pause"}`); } catch (err) { toast(err.message); }
      return refresh();
    }
    if (el.id === "u-enabled" && !el.disabled) {
      if (!el.checked) {                                  // turning it off asks twice
        const a = await modal("Stop taking coins apart?", "The desk would buy without the 8-layer audit.", { ok: "Turn off", danger: true });
        const b = a && await modal("Are you sure?", "Every coin would skip the Auditor until you turn this back on.", { ok: "Yes, turn off", danger: true });
        if (!b) { el.checked = true; return; }
      }
      return setSetting("auditor.enabled", el.checked);
    }
    if (el.dataset.key && !el.disabled) {
      const pair = el.closest(".ctl"); if (pair) pair.querySelectorAll("input").forEach(i => { if (i !== el) i.value = el.value; });
      const v = el.type === "checkbox" ? el.checked : (el.type === "range" || el.type === "number") ? +el.value : el.value;
      return setSetting(el.dataset.key, v);
    }
  });
  document.addEventListener("input", (e) => {
    const el = e.target; if (el.dataset.pair) { const n = el.closest(".ctl").querySelector('input[type=number]'); if (n) n.value = el.value; }
  });

  async function invite() {
    const u = safeLink(S.account && S.account.signup_url); if (!u) return;
    if (navigator.share) { try { await navigator.share({ url: u }); return; } catch (e) { if (e && e.name === "AbortError") return; } }
    try { await navigator.clipboard.writeText(u); toast("Link copied"); }
    catch (e) { const ta = document.createElement("textarea"); ta.value = u; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast("Link copied"); }
  }
  async function enablePush() {
    try {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) throw new Error("This browser has no web push");
      const perm = await Notification.requestPermission(); if (perm !== "granted") throw new Error("Notifications not allowed");
      const reg = await navigator.serviceWorker.register("sw.js");
      const key = (await (await fetch("vapid-public.txt", { cache: "no-store" })).text()).trim();
      const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((key.length + 3) % 4));
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, c => c.charCodeAt(0)) });
      await post("/api/push", sub.toJSON()); toast("Notifications on");
    } catch (e) { toast(e.message); }
  }
  async function loadMore() {
    if (!S.actNext) return;
    try { const p = await get("activity", S.actNext); S.actRows = S.actRows.concat(p.rows); S.actNext = p.nextBefore; V.activity.update(); }
    catch (e) { toast("Couldn't load more"); }
  }

  /* ------------------------------------------------------------ refresh loop */
  let timer = 0, busy = false;
  async function refresh() {
    if (busy) return; busy = true;
    clearTimeout(timer);
    const v = S.view.name;
    try {
      const jobs = [get("state").then(x => { S.state = x; })];
      if (v === "home") jobs.push(getChart().then(x => { S.chart = x; }));
      if (v === "home" || (v === "activity" && S.actRows === null)) jobs.push(get("activity").then(x => {
        S.activity = x; if (v === "activity" || S.actRows === null) { S.actRows = x.rows; S.actNext = x.nextBefore; } }));
      if (v === "agents") AGENTS.forEach(a => jobs.push(get("decisions", a).then(x => { S.decisions[a] = x; })));
      if (v === "coin") jobs.push(get("coin", S.view.mint).then(x => { S.coin[S.view.mint] = x; }));
      if (v === "audit" && S.view.mint) jobs.push(get("audit", S.view.mint).then(x => { S.audit[S.view.mint] = x; }));
      if (v === "account" || !S.account) jobs.push(get("account").then(x => { S.account = x; }));
      if (v === "account") jobs.push(get("settings").then(x => { S.settings = x; }));
      const res = await Promise.allSettled(jobs);
      S.failing = res.some(r => r.status === "rejected");
      if (!S.failing) S.lastOk = Date.now();
    } catch (e) { S.failing = true; }
    busy = false;
    if (S.view.name === v && V[v]) V[v].update();
    S.retryAt = Date.now() + POLL_S * 1000;
    tickError();
    timer = setTimeout(refresh, POLL_S * 1000);
  }
  function startStream() {                                // plan A only: SSE nudges a refresh
    if (S.cred.kind !== "A" || !window.EventSource) return;
    const es = new EventSource("/api/stream?t=" + encodeURIComponent(S.cred.value));
    let pending = 0;
    ["scan", "audit_layer", "verdict", "signal", "fill", "exit", "balance"].forEach(ev =>
      es.addEventListener(ev, () => { clearTimeout(pending); pending = setTimeout(refresh, 300); }));
  }

  function mount() {
    if (V[S.view.name]) V[S.view.name].mount(); else { S.view = { name: "home" }; V.home.mount(); }
    tickError(); window.scrollTo(0, 0);
  }

  function connectScreen() {
    app().innerHTML = `<div class="connect"><h1>Grok Bot desk</h1>
      <p>Open your private desk link once on this phone. If you added the app to the Home Screen, paste that link here.</p>
      <input id="cl" placeholder="Paste your desk link" autocomplete="off" autocapitalize="off" spellcheck="false">
      <button id="cb">Open</button><p id="ce" class="neg" style="min-height:20px"></p></div>`;
    $("#cb").addEventListener("click", () => {
      const v = $("#cl").value.trim(); const i = v.indexOf("#");
      const f = i >= 0 ? readFragment(v.slice(i)) : null;
      if (!f) { $("#ce").textContent = "That link has no desk key in it."; return; }
      localStorage.setItem(f.kind === "B" ? "desk.d" : "desk.t", f.value);
      $("#cl").value = ""; location.reload();
    });
  }

  function boot() {
    S.cred = loadCred();
    if (!S.cred) return connectScreen();
    history.replaceState({ view: S.view }, "", location.pathname + location.search);
    mount(); refresh(); startStream();
    setInterval(tickError, 1000);
    window.addEventListener("resize", () => { if (S.view.name === "home") V.home.chart(); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }
  boot();
})();
