/* OpenAmika UI — shared chrome (nav, toast, fetch helpers). */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  const SAKURA = `<svg class="oa-sakura" viewBox="0 0 100 100" aria-hidden="true">
    <g fill="#f4a8c1">
      <ellipse cx="50" cy="26" rx="11" ry="17" transform="rotate(0 50 50)"/>
      <ellipse cx="50" cy="26" rx="11" ry="17" transform="rotate(72 50 50)"/>
      <ellipse cx="50" cy="26" rx="11" ry="17" transform="rotate(144 50 50)"/>
      <ellipse cx="50" cy="26" rx="11" ry="17" transform="rotate(216 50 50)"/>
      <ellipse cx="50" cy="26" rx="11" ry="17" transform="rotate(288 50 50)"/>
      <circle cx="50" cy="50" r="7" fill="#c8385a"/>
    </g></svg>`;

  // Simple / Professional UI mode — pure CSS gating via a class on <html>.
  function uiMode() {
    let mode = "simple";
    try { mode = localStorage.getItem("openamika-ui-mode") || "simple"; } catch (_) {}
    return mode === "pro" ? "pro" : "simple";
  }
  function applyMode(mode) {
    document.documentElement.classList.remove("mode-simple", "mode-pro");
    document.documentElement.classList.add("mode-" + mode);
    try { localStorage.setItem("openamika-ui-mode", mode); } catch (_) {}
    document.querySelectorAll(".oa-mode button").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === mode));
  }
  applyMode(uiMode());

  function nav(active) {
    const el = document.createElement("nav");
    el.className = "oa-nav";
    el.innerHTML = `
      <a class="oa-wordmark" href="/use" aria-label="OpenAmika home">
        ${SAKURA}
        <span class="mark-name">Open<em>Amika</em></span>
        <span class="mark-jp">オープン・アミカ</span>
      </a>
      <div class="oa-nav-links">
        <a href="/setup" data-page="setup">Setup</a>
        <a href="/tools" data-page="tools">Tools</a>
        <a href="/use" data-page="use">Use</a>
        <a href="/about" data-page="about">About</a>
      </div>
      <div style="display:flex;align-items:center;">
        <div class="oa-mode" title="Simple hides the expert controls; Professional shows everything">
          <button type="button" data-mode="simple">Simple</button>
          <button type="button" data-mode="pro">Professional</button>
        </div>
        <button type="button" class="oa-health" id="navHealth" aria-haspopup="true" aria-expanded="false" title="System health">
          <span class="light" id="navHealthLight"></span><span class="label" id="navHealthLabel">Checking…</span>
        </button>
      </div>`;
    el.querySelectorAll("a[data-page]").forEach((a) => {
      if (a.dataset.page === active) a.classList.add("active");
    });
    el.querySelectorAll(".oa-mode button").forEach((b) =>
      b.addEventListener("click", () => applyMode(b.dataset.mode)));
    document.body.prepend(el);
    applyMode(uiMode());
    startHealth();
  }

  function footer() {
    const el = document.createElement("footer");
    el.className = "oa-footer";
    el.innerHTML = `
      <div class="links">
        <a href="/setup">Setup</a>
        <a href="/tools">Tools</a>
        <a href="/use">Use</a>
        <a href="/flow">System flow</a>
        <a href="/about">About &amp; support</a>
        <a href="https://github.com/zeekk0/OpenAmika" rel="noopener" target="_blank">GitHub</a>
      </div>
      <p class="fine">OpenAmika · an open companion · locally yours</p>`;
    document.body.append(el);
  }

  let toastEl = null, toastTimer = null;
  function toast(message, isError) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "oa-toast";
      document.body.append(toastEl);
    }
    toastEl.textContent = message;
    toastEl.classList.toggle("err", !!isError);
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), isError ? 6000 : 3200);
  }

  // a refusal's detail is a sentence, or {code, message} when the page should react to it (e.g. the IoT safety
  // statement: code "iot_disclaimer_required"); the Error carries .status and .code either way
  function refusal(payload, status, fallback) {
    const detail = payload && payload.detail;
    const structured = detail && typeof detail === "object";
    const error = new Error((structured ? detail.message : detail) || (payload && payload.error) || fallback);
    error.status = status;
    error.code = (structured && detail.code) || (payload && payload.code) || null;
    return error;
  }

  async function getJSON(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch (_) {}
      throw refusal(payload, response.status, `${url} → HTTP ${response.status}`);
    }
    return response.json();
  }

  async function postAction(url, action, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Build-F-Action": action },
      body: JSON.stringify(body || {}),
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) throw refusal(payload, response.status, `HTTP ${response.status}`);
    return payload;
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // ── system health: the one light at the top right (green / yellow / breathing red), like the launcher's
  // console lights. Click it for the list of what is wrong. Every page shares this poll.
  const LEVEL_RANK = { ok: 0, warn: 1, err: 2 };
  const LEVEL_LABEL = { ok: "System Ready", warn: "Warnings", err: "System Failures" };
  const health = { level: null, items: [], listeners: new Set(), timer: null, pop: null };

  async function healthCheck() {
    const items = [];
    const add = (level, name, detail) => items.push({ level, name, detail });
    // brain (via the gateway's proxy): templates and memory
    try {
      const s = await getJSON("/api/build_f/status");
      const checks = s.template_check || {};
      const problems = Object.values(checks).reduce((a, list) => a + (Array.isArray(list) ? list.length : 0), 0);
      add(problems ? "warn" : "ok", "Brain", problems ? problems + " template warning(s) — Setup → Personality" : (s.app || "online"));
      const memory = s.memory || {};
      if (memory.degraded) add("warn", "Memory", "running degraded — see Setup → System health");
      else add("ok", "Memory", (memory.events != null ? memory.events + " conversations" : "ready"));
    } catch (error) { add("err", "Brain", "not answering — is the brain running? (launch.py)"); }
    // her brain's provider per role: a cloud role needs the OpenRouter key; a local role needs Ollama up
    let usesCloud = true, usesOllama = false, usesCustom = false, localModels = [];
    try {
      const m = await getJSON("/api/build_f/models");
      const roles = ["surface", "deep", "reflection", "memory_write"];
      const kinds = roles.map((role) => { const item = ((m.options || {})[role] || []).find((o) => o.key === (m.selections || {})[role]); return item ? item.transport : "openrouter"; });
      usesCloud = kinds.includes("openrouter");
      usesOllama = kinds.includes("ollama");
      usesCustom = kinds.includes("custom");
      localModels = roles.filter((_, i) => kinds[i] === "ollama").map((role) => { const item = (m.options[role] || []).find((o) => o.key === m.selections[role]); return item ? item.model : ""; });
    } catch (_) {}
    if (usesCloud) {
      try {
        const k = await getJSON("/api/build_f/openrouter_key");
        add(k.present ? "ok" : "err", "OpenRouter key", k.present ? "saved " + (k.hint || "") : "no key saved — Setup → Brain (or switch to Ollama)");
      } catch (_) { /* the brain row already says it is down */ }
    }
    if (usesOllama) {
      try {
        const o = await getJSON("/api/build_f/ollama");
        const have = new Set((o.models || []).map((x) => x.name));
        const missing = Array.from(new Set(localModels)).filter((name) => name && !have.has(name));
        if (!o.reachable) add("err", "Ollama (local brain)", "not answering at " + o.base_url + " — start Ollama");
        else if (missing.length) add("warn", "Ollama (local brain)", "model(s) no longer detected: " + missing.join(", ") + " — Setup → Brain → Detect");
        else add("ok", "Ollama (local brain)", (o.live_version ? "v" + o.live_version + " · " : "") + Array.from(new Set(localModels)).join(", "));
      } catch (_) { add("err", "Ollama (local brain)", "status unavailable"); }
    }
    if (usesCustom) {
      // A custom endpoint is ready only when it has both a catalog and a key; the transport raises on
      // the first reply otherwise, which reads to the person as "she is broken".
      try {
        const c = await getJSON("/api/build_f/custom");
        if (c.blocked) add("err", "Custom endpoint", "unusable until a save succeeds: " + c.blocked);
        else if (!c.configured) add("err", "Custom endpoint", "no endpoint saved — Setup → Brain → Custom endpoint");
        else if (!c.key_present) add("err", "Custom endpoint", c.base_url + " has no key saved — Setup → Brain → Custom endpoint");
        else if (!c.preset_ready) add("warn", "Custom endpoint", c.base_url + " — tick JSON schema on one model in Setup, or the memory writer has nowhere to go");
        else add("ok", "Custom endpoint", c.base_url);
      } catch (_) { add("err", "Custom endpoint", "status unavailable"); }
    }
    // speech / STT
    try {
      const h = await getJSON("/health");
      add(h.cuda_available ? "ok" : "warn", "Speech / STT", h.cuda_available ? (h.gpu_name || "cuda") : "running on CPU (slow)");
    } catch (_) { add("err", "Speech / STT", "gateway not answering"); }
    // TTS
    try {
      const p = await getJSON("/api/tts_probe");
      add(p.ok ? "ok" : "err", "TTS (her voice)", p.ok ? p.url : "unreachable at " + p.url +
        " — not installed (envs\\brain\\Scripts\\python.exe scripts\\setup_tts.py) or not running yet (~20 s after a start); see logs/tts.log");
    } catch (_) { add("err", "TTS (her voice)", "probe failed"); }
    // her Live2D model: the Use page's stage needs one; scripts/setup_tts.py installs hers together with her voice
    try {
      const a = await getJSON("/api/avatar/models");
      const mine = (a.models || []).filter((m) => m.role !== "agent");
      add(mine.length ? "ok" : "err", "Live2D model", mine.length ? mine.map((m) => m.name).join(", ")
        : "none in avatar/models/ — run envs\\brain\\Scripts\\python.exe scripts\\setup_tts.py (her voice and Live2D model), then reload");
    } catch (_) { add("err", "Live2D model", "model list unavailable"); }
    // the launcher's pre-flight: whatever was not green when the stack started
    try {
      const p = await getJSON("/api/preflight");
      for (const c of (p.checks || [])) {
        if (c.level && c.level !== "ok") add(c.level === "err" ? "err" : "warn", "Startup: " + c.name, c.detail || "");
      }
    } catch (_) {}
    // messengers (optional): only a configured bot that cannot connect is a warning
    try {
      const m = await getJSON("/api/build_f/messengers");
      for (const p of ["discord", "telegram"]) {
        const cfg = m[p] || {}, live = (m.live || {})[p] || {};
        if (cfg.enabled && cfg.token_present && live.error) add("warn", p[0].toUpperCase() + p.slice(1) + " bot", live.error);
      }
    } catch (_) {}
    let level = "ok";
    for (const it of items) if (LEVEL_RANK[it.level] > LEVEL_RANK[level]) level = it.level;
    health.level = level; health.items = items;
    renderHealth();
    for (const fn of health.listeners) { try { fn(level, items); } catch (_) {} }
    return { level, items };
  }

  function renderHealth() {
    const light = document.getElementById("navHealthLight"), label = document.getElementById("navHealthLabel");
    const button = document.getElementById("navHealth");
    if (!light || !label || !button) return;
    const level = health.level || "";
    light.className = "light " + level;
    label.textContent = level ? LEVEL_LABEL[level] : "Checking…";
    button.title = level === "ok" ? "System Ready" : "Click for details";
    if (health.pop) renderHealthPop();
  }

  function renderHealthPop() {
    const pop = health.pop;
    if (!pop) return;
    pop.replaceChildren();
    const level = health.level || "ok";
    pop.append(el("div", { class: "head " + level }, el("span", { class: "light " + level }), el("b", { text: LEVEL_LABEL[level] })));
    const bad = health.items.filter((it) => it.level !== "ok");
    if (!bad.length) {
      pop.append(el("p", { class: "fine", text: "Brain, speech, voice, Live2D model and the startup checks are all green." }));
    } else {
      pop.append(el("p", { class: "fine", text: level === "err" ? "Something she needs is down:" : "She works, but check these:" }));
      for (const it of bad) {
        pop.append(el("div", { class: "row" }, el("span", { class: "dot " + it.level }),
          el("span", { class: "name", text: it.name }), el("span", { class: "meta", text: it.detail || "" })));
      }
    }
    pop.append(el("div", { class: "foot" },
      el("a", { href: "/setup", text: "Open Setup →" }),
      el("button", { type: "button", class: "editorial-link", text: "Re-check", onclick: () => { healthCheck(); } })));
  }

  function toggleHealthPop(force) {
    const button = document.getElementById("navHealth");
    if (!button) return;
    const open = force !== undefined ? force : !health.pop;
    if (!open) {
      if (health.pop) health.pop.remove();
      health.pop = null;
      button.setAttribute("aria-expanded", "false");
      return;
    }
    health.pop = el("div", { class: "oa-health-pop", role: "dialog", "aria-label": "System health" });
    document.body.append(health.pop);
    button.setAttribute("aria-expanded", "true");
    renderHealthPop();
  }

  function startHealth() {
    const button = document.getElementById("navHealth");
    if (!button) return;
    button.addEventListener("click", (event) => { event.stopPropagation(); toggleHealthPop(); });
    document.addEventListener("click", (event) => {
      if (health.pop && !health.pop.contains(event.target)) toggleHealthPop(false);
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") toggleHealthPop(false); });
    const tick = () => {
      clearTimeout(health.timer);
      const run = document.hidden ? Promise.resolve() : healthCheck().catch(() => {});
      run.finally(() => { health.timer = setTimeout(tick, 15000); });
    };
    tick();
  }
  function onHealth(fn) { health.listeners.add(fn); if (health.level) fn(health.level, health.items); }

  // kept for older page scripts: the three dots became the one health light above
  function setNavDot() {}

  const fmt = {
    ms: (v) => (Number.isFinite(v) ? Math.round(v).toLocaleString() + " ms" : "—"),
    s1: (v) => (Number.isFinite(v) ? (v / 1000).toFixed(v >= 10000 ? 1 : 2) + " s" : "—"),
    usd: (v) => (Number.isFinite(v) ? "$" + (v >= 0.01 ? v.toFixed(3) : v.toFixed(4)) : "—"),
    int: (v) => (Number.isFinite(v) ? Math.round(v).toLocaleString() : "—"),
    pct: (v) => (Number.isFinite(v) ? (Math.round(v * 10) / 10) + "%" : "—"),
  };

  window.OA = { nav, footer, toast, getJSON, postAction, el, setNavDot, fmt, healthCheck, onHealth };
})();
