/* plsem-platform frontend — vanilla JS, no build step. */
"use strict";

const state = { dataset: null, constructs: [], paths: [], interactions: [], analysis: null };
const $ = (sel) => document.querySelector(sel);

/* ------------------------------ helpers ------------------------------ */
async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = body.detail;
    const msg = typeof detail === "string" ? detail
      : detail && detail.message ? `[${detail.stage}] ${detail.message}`
      : JSON.stringify(detail || body);
    throw new Error(msg);
  }
  return body;
}
function setStatus(sel, msg, isError = false) {
  const el = $(sel);
  el.textContent = msg;
  el.classList.toggle("error", isError);
}
function badge(text, cls) {
  return `<span class="badge ${cls}">${text}</span>`;
}
function verdictBadge(v) {
  const cls = { pass: "pass", fail: "fail", review: "review", supported: "supported" }[v]
    || (v === "not supported" ? "not-supported" : "neutral");
  return badge(v, cls);
}
function fmt(v) { return typeof v === "number" ? v.toFixed(3) : (v ?? ""); }
function goStep(n) {
  document.querySelectorAll(".panel").forEach((p, i) => p.classList.toggle("hidden", i !== n - 1));
  document.querySelectorAll(".step").forEach((s) => {
    const num = +s.dataset.step;
    s.classList.toggle("active", num === n);
    if (num <= n) s.disabled = false;
  });
}
document.querySelectorAll(".step").forEach((s) =>
  s.addEventListener("click", () => !s.disabled && goStep(+s.dataset.step)));

/* ------------------------------ AI badge ------------------------------ */
let aiConfigured = false;
api("/api/ai/status").then(({ configured }) => {
  aiConfigured = configured;
  const el = $("#ai-badge");
  el.textContent = configured ? "AI: connected" : "AI: no API key";
  el.classList.toggle("off", !configured);
  el.title = configured ? "" : "Set ANTHROPIC_API_KEY and restart the server to enable AI features";
});

/* ------------------------------ Step 1: data ------------------------------ */
/* Source tabs: file upload, SQL database, or Google Sheet. All three land the same
   dataset shape, so the rest of the app is source-agnostic. */
document.querySelectorAll(".source-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const src = tab.dataset.source;
    document.querySelectorAll(".source-tab").forEach((t) =>
      t.classList.toggle("active", t === tab));
    document.querySelectorAll(".source-panel").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.source !== src));
    setStatus("#upload-status", "");
  });
});

async function ingest(promise, busyMsg) {
  setStatus("#upload-status", busyMsg);
  try {
    state.dataset = await promise;
    renderDataset();
    setStatus("#upload-status", "");
  } catch (err) {
    setStatus("#upload-status", err.message, true);
  }
}

$("#upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const file = $("#file-input").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const code = $("#missing-code").value.trim();
  if (code) fd.append("missing_value", code);
  ingest(api("/api/datasets", { method: "POST", body: fd }), "Uploading and auditing…");
});

$("#sql-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = {
    dsn: $("#sql-dsn").value.trim(),
    query: $("#sql-query").value.trim(),
    missing_value: $("#sql-missing").value.trim() || null,
  };
  ingest(api("/api/datasets/sql", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }), "Querying database and auditing…");
});

$("#gsheet-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const body = {
    url: $("#gsheet-url").value.trim(),
    missing_value: $("#gsheet-missing").value.trim() || null,
  };
  ingest(api("/api/datasets/gsheet", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }), "Importing sheet and auditing…");
});

function renderDataset() {
  const d = state.dataset;
  $("#data-summary").classList.remove("hidden");
  const warnings = d.audit.findings.filter((f) => f.severity === "warning").length;
  $("#data-cards").innerHTML = `
    <div class="stat"><div class="v">${d.n_observations}</div><div class="l">respondents</div></div>
    <div class="stat"><div class="v">${d.variables.length}</div><div class="l">variables</div></div>
    <div class="stat ${warnings ? "warn" : "good"}"><div class="v">${d.audit.n_findings}</div><div class="l">audit findings</div></div>`;
  $("#audit-list").innerHTML = d.audit.findings.length
    ? d.audit.findings.map((f) => `
        <div class="audit-item ${f.severity}">
          ${badge(f.check.replaceAll("_", " "), f.severity)}
          ${f.variable ? `<b>${f.variable}</b>` : ""}
          ${f.count != null ? `— ${f.count}${f.pct != null ? ` (${f.pct}%)` : ""}` : ""}
          · ${f.suggestion}
        </div>`).join("")
    : `<div class="audit-item">No issues found.</div>`;
  const rows = d.variables.map((v) => `
    <tr><td><b>${v.name}</b></td><td>${v.dtype}</td><td>${v.n_missing}</td>
    <td>${v.n_unique}</td><td>${v.min ?? ""}</td><td>${v.max ?? ""}</td></tr>`).join("");
  $("#var-table").innerHTML =
    `<tr><th>Variable</th><th>Type</th><th>Missing</th><th>Unique</th><th>Min</th><th>Max</th></tr>${rows}`;
}
$("#to-model").addEventListener("click", () => { goStep(2); renderModel(); });

/* ------------------------------ Step 2: model ------------------------------ */
function varNames() { return state.dataset ? state.dataset.variables.map((v) => v.name) : []; }

const MEASUREMENTS = ["reflective", "formative", "single_item",
  "higher_order_reflective", "higher_order_formative"];
const isHOC = (c) => c.measurement.startsWith("higher_order");

function renderModel() {
  const cList = $("#construct-list");
  cList.innerHTML = "";
  state.constructs.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "construct-row";
    // Higher-order constructs pick lower-order constructs as dimensions;
    // everything else picks data columns as indicators.
    const itemOpts = isHOC(c)
      ? state.constructs.filter((o) => o !== c && o.name && !isHOC(o)).map((o) => o.name)
      : varNames();
    const selected = isHOC(c) ? (c.dimensions || []) : c.indicators;
    row.innerHTML = `
      <input type="text" value="${c.name}" placeholder="NAME">
      <select class="mode">
        ${MEASUREMENTS.map((m) =>
          `<option ${m === c.measurement ? "selected" : ""}>${m}</option>`).join("")}
      </select>
      <select class="items" multiple title="${isHOC(c) ? "dimensions (lower-order constructs)" : "indicators"}">
        ${itemOpts.map((v) =>
          `<option ${selected.includes(v) ? "selected" : ""}>${v}</option>`).join("")}
      </select>
      <button class="rm" title="remove">✕</button>`;
    row.querySelector("input").addEventListener("input", (e) => { c.name = e.target.value.trim(); renderPathsAndDiagram(); });
    row.querySelector(".mode").addEventListener("change", (e) => { c.measurement = e.target.value; renderModel(); });
    row.querySelector(".items").addEventListener("change", (e) => {
      const values = [...e.target.selectedOptions].map((o) => o.value);
      if (isHOC(c)) { c.dimensions = values; } else { c.indicators = values; }
    });
    row.querySelector(".rm").addEventListener("click", () => {
      state.constructs.splice(i, 1);
      state.paths = state.paths.filter((p) => p.from_construct !== c.name && p.to_construct !== c.name);
      state.interactions = state.interactions.filter((x) => x.iv !== c.name && x.moderator !== c.name);
      renderModel();
    });
    cList.appendChild(row);
  });
  renderPathsAndDiagram();
}

function interactionName(x) { return `${x.iv}*${x.moderator}`; }

function renderPathsAndDiagram() {
  const cNames = state.constructs.map((c) => c.name).filter(Boolean);
  const names = [...cNames, ...state.interactions.map(interactionName)];
  const pList = $("#path-list");
  pList.innerHTML = "";
  state.paths.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "path-row";
    const opts = (sel) => names.map((n) => `<option ${n === sel ? "selected" : ""}>${n}</option>`).join("");
    row.innerHTML = `<select class="from">${opts(p.from_construct)}</select> →
      <select class="to">${opts(p.to_construct)}</select>
      <button class="rm" title="remove">✕</button>`;
    row.querySelector(".from").addEventListener("change", (e) => { p.from_construct = e.target.value; drawDiagram(); });
    row.querySelector(".to").addEventListener("change", (e) => { p.to_construct = e.target.value; drawDiagram(); });
    row.querySelector(".rm").addEventListener("click", () => { state.paths.splice(i, 1); renderPathsAndDiagram(); });
    pList.appendChild(row);
  });

  const xList = $("#interaction-list");
  xList.innerHTML = "";
  state.interactions.forEach((x, i) => {
    const row = document.createElement("div");
    row.className = "path-row";
    const lower = state.constructs.filter((c) => c.name && !isHOC(c)).map((c) => c.name);
    const opts = (sel) => lower.map((n) => `<option ${n === sel ? "selected" : ""}>${n}</option>`).join("");
    row.innerHTML = `<select class="iv">${opts(x.iv)}</select> ×
      <select class="moderator">${opts(x.moderator)}</select>
      <button class="rm" title="remove">✕</button>`;
    const rename = (fn) => (e) => {
      const old = interactionName(x);
      fn(e.target.value);
      state.paths.forEach((p) => {
        if (p.from_construct === old) p.from_construct = interactionName(x);
        if (p.to_construct === old) p.to_construct = interactionName(x);
      });
      renderPathsAndDiagram();
    };
    row.querySelector(".iv").addEventListener("change", rename((v) => { x.iv = v; }));
    row.querySelector(".moderator").addEventListener("change", rename((v) => { x.moderator = v; }));
    row.querySelector(".rm").addEventListener("click", () => {
      const name = interactionName(x);
      state.interactions.splice(i, 1);
      state.paths = state.paths.filter((p) => p.from_construct !== name && p.to_construct !== name);
      renderPathsAndDiagram();
    });
    xList.appendChild(row);
  });
  drawDiagram();
}

$("#add-construct").addEventListener("click", () => {
  state.constructs.push({ name: "", indicators: [], dimensions: [], measurement: "reflective" });
  renderModel();
});
$("#add-path").addEventListener("click", () => {
  const names = state.constructs.map((c) => c.name).filter(Boolean);
  if (names.length < 2) return;
  state.paths.push({ from_construct: names[0], to_construct: names[1] });
  renderPathsAndDiagram();
});
$("#add-interaction").addEventListener("click", () => {
  const lower = state.constructs.filter((c) => c.name && !isHOC(c)).map((c) => c.name);
  if (lower.length < 2) return;
  const x = { iv: lower[0], moderator: lower[1] };
  state.interactions.push(x);
  // A moderator only acts through a path from the interaction term.
  const endo = state.paths.find((p) => p.from_construct === x.iv)?.to_construct;
  if (endo) state.paths.push({ from_construct: interactionName(x), to_construct: endo });
  renderPathsAndDiagram();
});
$("#btn-clear").addEventListener("click", () => {
  state.constructs = []; state.paths = []; state.interactions = [];
  renderModel();
});

function loadSpec(spec) {
  state.constructs = spec.constructs.map((c) => ({
    name: c.name, indicators: c.indicators || [], dimensions: c.dimensions || [],
    measurement: c.measurement,
  }));
  state.paths = spec.paths.map((p) => ({ from_construct: p.from_construct, to_construct: p.to_construct }));
  state.interactions = (spec.interactions || []).map((x) => ({ iv: x.iv, moderator: x.moderator }));
  renderModel();
}

$("#btn-example").addEventListener("click", async () => {
  loadSpec(await api("/api/fixtures/model-spec"));
});

$("#btn-propose").addEventListener("click", async () => {
  if (!state.dataset) return;
  setStatus("#propose-status", "Asking the AI architect…");
  try {
    const spec = await api(`/api/datasets/${state.dataset.id}/propose-model`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ study_description: "" }),
    });
    loadSpec(spec);
    setStatus("#propose-status", `AI proposal loaded: ${spec.summary || ""}`);
  } catch (err) {
    setStatus("#propose-status", err.message, true);
  }
});

/* Layered path diagram (exogenous left → outcomes right). */
function drawDiagram() {
  const svg = $("#diagram");
  const nodes = [
    ...state.constructs,
    ...state.interactions.map((x) => ({ name: interactionName(x), measurement: "interaction" })),
  ];
  const names = nodes.map((c) => c.name).filter(Boolean);
  svg.innerHTML = `<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M 0 0 L 10 5 L 0 10 z" fill="#55617a"/></marker></defs>`;
  if (!names.length) return;

  const layer = {};
  const incoming = (n) => state.paths.filter((p) => p.to_construct === n && p.from_construct !== n);
  const compute = (n, seen = new Set()) => {
    if (layer[n] != null) return layer[n];
    if (seen.has(n)) return 0;
    seen.add(n);
    const inc = incoming(n);
    layer[n] = inc.length ? 1 + Math.max(...inc.map((p) => compute(p.from_construct, seen))) : 0;
    return layer[n];
  };
  names.forEach((n) => compute(n));

  const W = svg.clientWidth || 560, H = 420;
  const maxLayer = Math.max(...names.map((n) => layer[n]));
  const cols = {};
  names.forEach((n) => (cols[layer[n]] = cols[layer[n]] || []).push(n));
  const pos = {};
  Object.entries(cols).forEach(([l, ns]) => {
    ns.forEach((n, i) => {
      pos[n] = {
        x: 90 + (maxLayer ? (l * (W - 180)) / maxLayer : 0),
        y: ((i + 1) * H) / (ns.length + 1),
      };
    });
  });

  const NS = "http://www.w3.org/2000/svg";
  state.paths.forEach((p) => {
    const a = pos[p.from_construct], b = pos[p.to_construct];
    if (!a || !b) return;
    const line = document.createElementNS(NS, "path");
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const sx = a.x + (dx / len) * 62, sy = a.y + (dy / len) * 30;
    const ex = b.x - (dx / len) * 62, ey = b.y - (dy / len) * 30;
    line.setAttribute("d", `M ${sx} ${sy} L ${ex} ${ey}`);
    line.setAttribute("class", "edge");
    line.setAttribute("stroke-width", "1.4");
    svg.appendChild(line);
  });
  nodes.forEach((c) => {
    if (!c.name || !pos[c.name]) return;
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", `node ${c.measurement}`);
    const { x, y } = pos[c.name];
    const el = document.createElementNS(NS, "ellipse");
    el.setAttribute("cx", x); el.setAttribute("cy", y);
    el.setAttribute("rx", 58); el.setAttribute("ry", 26);
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y + 4);
    t.setAttribute("text-anchor", "middle");
    t.textContent = c.name;
    g.appendChild(el); g.appendChild(t);
    svg.appendChild(g);
  });
}

/* ------------------------------ Run ------------------------------ */
$("#btn-run").addEventListener("click", async () => {
  if (!state.dataset) return;
  const payload = {
    dataset_id: state.dataset.id,
    constructs: state.constructs.filter((c) =>
      c.name && (isHOC(c) ? (c.dimensions || []).length >= 2 : c.indicators.length)),
    paths: state.paths,
    interactions: state.interactions,
    nboot: +$("#nboot").value,
    override_gates: $("#override-gates").checked,
  };
  if (!payload.constructs.length || !payload.paths.length) {
    setStatus("#run-status", "Define at least two constructs and one path.", true);
    return;
  }
  setStatus("#run-status", `Estimating (${payload.nboot.toLocaleString()} bootstrap resamples)…`);
  try {
    const analysis = await api("/api/analyses", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const assessment = await api(`/api/analyses/${analysis.id}/assessment`);
    const results = await api(`/api/analyses/${analysis.id}/results`);
    state.analysis = { ...analysis, assessment, results };
    setStatus("#run-status", "");
    renderResults();
    goStep(3);
  } catch (err) {
    setStatus("#run-status", err.message, true);
  }
});

/* ------------------------------ Interpretation ------------------------------ */
$("#btn-interpret").addEventListener("click", async () => {
  if (!state.analysis) return;
  setStatus("#interpret-status", "The AI writer is drafting the narrative (30–90 s)…");
  try {
    const interp = await api(`/api/analyses/${state.analysis.id}/interpretation`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ study_description: "" }),
    });
    renderInterpretation(interp);
    setStatus("#interpret-status", "Done — the Word report now includes these sections.");
  } catch (err) {
    setStatus("#interpret-status", err.message, true);
  }
});

function renderInterpretation(interp) {
  const titles = {
    results_narrative: "Results", discussion: "Discussion",
    managerial_implications: "Managerial implications",
    limitations: "Limitations", conclusion: "Conclusion",
  };
  $("#interpretation").classList.remove("hidden");
  $("#interp-sections").innerHTML = Object.entries(titles)
    .filter(([k]) => interp[k])
    .map(([k, title]) => `<div class="card interp-card"><h3>${title}</h3>
      ${interp[k].split("\n\n").map((p) => `<p>${p}</p>`).join("")}</div>`)
    .join("");
}

/* ------------------------------ Step 3: results ------------------------------ */
function renderResults() {
  $("#interpretation").classList.add("hidden");
  $("#interp-sections").innerHTML = "";
  setStatus("#interpret-status", "");
  const a = state.analysis.assessment;
  const s = a.summary;
  $("#btn-report").href = `/api/analyses/${state.analysis.id}/report.docx`;
  $("#btn-xlsx").href = `/api/analyses/${state.analysis.id}/results.xlsx`;
  $("#btn-pptx").href = `/api/analyses/${state.analysis.id}/summary.pptx`;
  $("#btn-json").href = `/api/analyses/${state.analysis.id}/results`;
  state.chatHistory = [];
  $("#chat-log").innerHTML = "";
  $("#chat-block").classList.toggle("hidden", !aiConfigured);
  $("#citations-list").innerHTML = "";
  setStatus("#citations-status", "");
  refreshShares();

  const fit = a.structural_model.find((m) => m.family === "model_fit");
  $("#result-cards").innerHTML = `
    <div class="stat ${s.hypotheses_supported === s.hypotheses_total ? "good" : ""}">
      <div class="v">${s.hypotheses_supported}/${s.hypotheses_total}</div><div class="l">hypotheses supported</div></div>
    <div class="stat good"><div class="v">${s.pass}</div><div class="l">checks passed</div></div>
    <div class="stat ${s.review ? "warn" : ""}"><div class="v">${s.review}</div><div class="l">to review</div></div>
    <div class="stat ${s.fail ? "bad" : ""}"><div class="v">${s.fail}</div><div class="l">failed</div></div>
    ${fit ? `<div class="stat ${{ pass: "good", review: "warn", fail: "bad" }[fit.verdict] || ""}">
      <div class="v">${fmt(fit.value)}</div><div class="l">SRMR</div></div>` : ""}`;

  $("#hyp-table").innerHTML =
    `<tr><th>#</th><th>Path</th><th>β</th><th>t</th><th>95% CI</th><th>Verdict</th></tr>` +
    a.hypotheses.map((h) => `<tr><td>${h.hypothesis}</td><td>${h.path}</td>
      <td>${fmt(h.estimate)}</td><td>${fmt(h.t_value)}</td>
      <td>${h.ci_95 ? `[${fmt(h.ci_95[0])}; ${fmt(h.ci_95[1])}]` : ""}</td>
      <td>${verdictBadge(h.verdict)}</td></tr>`).join("");

  const rel = a.measurement_model.filter((m) =>
    ["internal_consistency", "convergent_validity"].includes(m.family));
  $("#rel-table").innerHTML =
    `<tr><th>Construct</th><th>Metric</th><th>Value</th><th>Criterion</th><th>Verdict</th></tr>` +
    rel.map((m) => `<tr><td>${m.construct}</td><td>${m.metric}</td><td>${fmt(m.value)}</td>
      <td>${m.threshold}</td><td>${verdictBadge(m.verdict)}</td></tr>`).join("");

  const htmt = a.measurement_model.filter((m) => m.family === "discriminant_validity");
  $("#htmt-table").innerHTML =
    `<tr><th>Pair</th><th>HTMT</th><th>Criterion</th><th>Verdict</th></tr>` +
    htmt.map((m) => `<tr><td>${m.construct}</td><td>${fmt(m.value)}</td>
      <td>${m.threshold}</td><td>${verdictBadge(m.verdict)}</td></tr>`).join("");

  $("#struct-table").innerHTML =
    `<tr><th>Family</th><th>Target</th><th>Metric</th><th>Value</th><th>Criterion</th><th>Verdict</th></tr>` +
    a.structural_model.map((m) => `<tr><td>${m.family.replaceAll("_", " ")}</td>
      <td>${m.construct}</td><td>${m.metric}</td><td>${fmt(m.value)}</td>
      <td>${m.threshold}</td><td>${verdictBadge(m.verdict)}</td></tr>`).join("");

  renderMediation(a.mediation || []);
  renderSlopes(a);
  renderIpma(state.analysis.results);
  setupMga();
}

/* Simple-slopes plot: relationship IV -> Y at moderator = -1 SD, mean, +1 SD
   (standardized construct scores; two-stage interaction estimates). */
const SLOPE_COLORS = { "+1 SD": "#0c7fa8", "mean": "#b45309", "−1 SD": "#7c5cd6" };
function renderSlopes(a) {
  const x = state.interactions[0];
  const est = {};
  a.hypotheses.forEach((h) => { est[h.path] = h.estimate; });
  const target = x && state.paths.find((p) => p.from_construct === interactionName(x));
  const show = x && target && est[`${interactionName(x)} -> ${target.to_construct}`] != null;
  $("#slopes-block").classList.toggle("hidden", !show);
  if (!show) return;
  const Y = target.to_construct;
  const b1 = est[`${x.iv} -> ${Y}`] || 0;
  const b2 = est[`${x.moderator} -> ${Y}`] || 0;
  const b3 = est[`${interactionName(x)} -> ${Y}`] || 0;

  const svg = $("#slopes-plot");
  svg.innerHTML = "";
  const NS = "http://www.w3.org/2000/svg";
  const W = svg.clientWidth || 640, H = 330;
  const M = { top: 16, right: 120, bottom: 42, left: 52 };
  const lines = [["+1 SD", 1], ["mean", 0], ["−1 SD", -1]]
    .map(([label, m]) => ({ label, m, y: (xx) => (b1 + b3 * m) * xx + b2 * m }));
  const ys = lines.flatMap((l) => [l.y(-2), l.y(2)]);
  const [y0, y1] = [Math.min(...ys) - 0.2, Math.max(...ys) + 0.2];
  const sx = (v) => M.left + ((v + 2) / 4) * (W - M.left - M.right);
  const sy = (v) => H - M.bottom - ((v - y0) / (y1 - y0)) * (H - M.top - M.bottom);
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
    if (text != null) e.textContent = text;
    svg.appendChild(e);
    return e;
  };
  [-2, -1, 0, 1, 2].forEach((v) => {
    el("line", { x1: sx(v), y1: H - M.bottom, x2: sx(v), y2: H - M.bottom + 4, class: "axis" });
    el("text", { x: sx(v), y: H - M.bottom + 16, "text-anchor": "middle", class: "tick" }, v);
  });
  el("line", { x1: M.left, y1: H - M.bottom, x2: W - M.right, y2: H - M.bottom, class: "axis" });
  el("line", { x1: M.left, y1: M.top, x2: M.left, y2: H - M.bottom, class: "axis" });
  el("text", { x: (M.left + W - M.right) / 2, y: H - 6, "text-anchor": "middle", class: "axis-title" },
     `${x.iv} (standardized)`);
  const yt = el("text", { x: 0, y: 0, "text-anchor": "middle", class: "axis-title" }, `${Y} (standardized)`);
  yt.setAttribute("transform", `translate(14, ${(M.top + H - M.bottom) / 2}) rotate(-90)`);
  lines.forEach((l) => {
    el("line", { x1: sx(-2), y1: sy(l.y(-2)), x2: sx(2), y2: sy(l.y(2)),
                 stroke: SLOPE_COLORS[l.label], "stroke-width": 2, fill: "none" });
  });
  // Direct labels at the line ends, nudged apart when the lines converge
  let lastLabelY = -1e9;
  lines
    .map((l) => ({ l, ly: sy(l.y(2)) + 4 }))
    .sort((a, b) => a.ly - b.ly)
    .forEach((entry) => {
      if (entry.ly - lastLabelY < 14) entry.ly = lastLabelY + 14;
      lastLabelY = entry.ly;
      el("text", { x: sx(2) + 8, y: entry.ly, class: "slope-label",
                   fill: SLOPE_COLORS[entry.l.label] }, `${x.moderator} ${entry.l.label}`);
    });
  el("text", { x: M.left + 6, y: M.top + 8, class: "quadrant-caption" },
     `slope = ${(b1 + b3).toFixed(3)} at +1 SD · ${b1.toFixed(3)} at mean · ${(b1 - b3).toFixed(3)} at −1 SD`);
}

/* ------------------------------ Research chat ------------------------------ */
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message || !state.analysis) return;
  const log = $("#chat-log");
  log.innerHTML += `<div class="chat-msg user">${message}</div>`;
  input.value = "";
  setStatus("#chat-status", "Thinking…");
  try {
    const { reply } = await api(`/api/analyses/${state.analysis.id}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: state.chatHistory }),
    });
    state.chatHistory.push({ role: "user", content: message },
                           { role: "assistant", content: reply });
    log.innerHTML += `<div class="chat-msg assistant">${reply.replaceAll("\n", "<br>")}</div>`;
    log.scrollTop = log.scrollHeight;
    setStatus("#chat-status", "");
  } catch (err) {
    setStatus("#chat-status", err.message, true);
  }
});

/* PDF export needs LibreOffice on the server; surface the message if missing. */
$("#btn-pdf").addEventListener("click", async () => {
  if (!state.analysis) return;
  setStatus("#interpret-status", "Converting to PDF…");
  const resp = await fetch(`/api/analyses/${state.analysis.id}/report.pdf`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    setStatus("#interpret-status", body.detail || "PDF export failed", true);
    return;
  }
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `plsem_report_${state.analysis.id}.pdf`;
  a.click();
  URL.revokeObjectURL(a.href);
  setStatus("#interpret-status", "");
});

/* ------------------------------ Literature citations ------------------------------ */
function refShort(r) {
  const who = [r.authors, r.year].filter(Boolean).join(", ");
  const cited = r.cited_by != null ? ` · cited by ${r.cited_by}` : "";
  const link = r.url ? `<a href="${r.url}" target="_blank" rel="noopener">${r.doi}</a>` : "";
  return `<div class="ref">
    <div class="ref-title">${escapeHtml(r.title || "(untitled)")}</div>
    <div class="hint">${escapeHtml(who)}${r.venue ? ` — ${escapeHtml(r.venue)}` : ""}${cited} ${link}</div>
  </div>`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function renderCitations(payload) {
  $("#citations-list").innerHTML = payload.hypotheses.map((h) => `
    <div class="citation-group">
      <h4>${escapeHtml(h.hypothesis || "")} · ${escapeHtml(h.path)}</h4>
      ${h.error ? `<div class="status error">lookup failed: ${escapeHtml(h.error)}</div>`
        : h.references.length ? h.references.map(refShort).join("")
        : `<div class="hint">No candidate references found.</div>`}
    </div>`).join("")
    + `<p class="hint">Suggestions for a literature review — not automatic support claims. Verify relevance before citing.</p>`;
}
$("#btn-citations").addEventListener("click", async () => {
  if (!state.analysis) return;
  setStatus("#citations-status", "Searching Crossref…");
  try {
    const payload = await api(`/api/analyses/${state.analysis.id}/citations`, { method: "POST" });
    renderCitations(payload);
    setStatus("#citations-status", "");
  } catch (err) {
    setStatus("#citations-status", err.message, true);
  }
});

/* ------------------------------ Share & collaborate ------------------------------ */
async function refreshShares() {
  $("#share-list").innerHTML = "";
  if (!state.analysis) return;
  try {
    const { shares } = await api(`/api/analyses/${state.analysis.id}/shares`);
    renderShares(shares);
  } catch { /* no shares yet is fine */ }
}
function renderShares(shares) {
  if (!shares.length) { $("#share-list").innerHTML = ""; return; }
  $("#share-list").innerHTML = `<table class="share-table">
    <tr><th>Link</th><th>Access</th><th>Label</th><th></th></tr>` +
    shares.map((s) => {
      const full = `${location.origin}${s.url}`;
      return `<tr>
        <td><a href="${s.url}" target="_blank" rel="noopener">${s.token}</a></td>
        <td>${s.scope === "comment" ? "view + comment" : "view only"}</td>
        <td>${escapeHtml(s.label || "")}</td>
        <td>
          <button class="copy-share" data-url="${full}">Copy</button>
          <button class="revoke-share" data-token="${s.token}">Revoke</button>
        </td></tr>`;
    }).join("") + `</table>`;
  $("#share-list").querySelectorAll(".copy-share").forEach((b) =>
    b.addEventListener("click", () => {
      navigator.clipboard?.writeText(b.dataset.url);
      b.textContent = "Copied!";
      setTimeout(() => (b.textContent = "Copy"), 1200);
    }));
  $("#share-list").querySelectorAll(".revoke-share").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/analyses/${state.analysis.id}/shares/${b.dataset.token}`, { method: "DELETE" });
      refreshShares();
    }));
}
$("#btn-share").addEventListener("click", async () => {
  if (!state.analysis) return;
  setStatus("#share-status", "Creating link…");
  try {
    await api(`/api/analyses/${state.analysis.id}/shares`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: $("#share-scope").value, label: $("#share-label").value.trim() }),
    });
    $("#share-label").value = "";
    setStatus("#share-status", "");
    refreshShares();
  } catch (err) {
    setStatus("#share-status", err.message, true);
  }
});

/* ------------------------------ MGA (MICOM-gated) ------------------------------ */
function setupMga() {
  $("#mga-results").classList.add("hidden");
  setStatus("#mga-status", "");
  // The engine compares lower-order models only; hide the panel when the model
  // has interactions or higher-order constructs, or no candidate grouping variable.
  const unsupported = state.interactions.length || state.constructs.some(isHOC);
  const indicators = new Set(state.constructs.flatMap((c) => c.indicators));
  const candidates = (state.dataset.variables || [])
    .filter((v) => v.values && !indicators.has(v.name));
  $("#mga-panel").classList.toggle("hidden", !!unsupported || !candidates.length);
  if (unsupported || !candidates.length) return;

  const varSel = $("#mga-var");
  varSel.innerHTML = candidates.map((v) => `<option>${v.name}</option>`).join("");
  const fillValues = () => {
    const v = candidates.find((c) => c.name === varSel.value);
    const opts = Object.entries(v.values)
      .map(([val, n]) => `<option value="${val}">${val} (n=${n})</option>`).join("");
    $("#mga-a").innerHTML = opts;
    $("#mga-b").innerHTML = opts;
    if ($("#mga-b").options.length > 1) $("#mga-b").selectedIndex = 1;
  };
  varSel.onchange = fillValues;
  fillValues();
}

$("#btn-mga").addEventListener("click", async () => {
  if (!state.analysis) return;
  const nperm = +$("#mga-nperm").value;
  setStatus("#mga-status", `Testing invariance and comparing groups (${nperm.toLocaleString()} permutations)…`);
  try {
    const mga = await api(`/api/analyses/${state.analysis.id}/mga`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group_variable: $("#mga-var").value,
        value_a: $("#mga-a").value,
        value_b: $("#mga-b").value,
        npermutations: nperm,
      }),
    });
    renderMga(mga);
    setStatus("#mga-status", "Done — the Word report now includes the MGA section.");
  } catch (err) {
    setStatus("#mga-status", err.message, true);
  }
});

function renderMga(mga) {
  const m = mga.meta, micom = mga.micom;
  $("#mga-results").classList.remove("hidden");
  const inv = micom.invariance;
  $("#mga-verdict").innerHTML =
    `${m.group_variable}: <b>${m.value_a}</b> (n=${m.n_a}) vs <b>${m.value_b}</b> (n=${m.n_b}) · ` +
    `${m.effective_permutations.toLocaleString()} permutations · MICOM: ` +
    badge(`${inv} invariance`, inv === "none" ? "fail" : "pass") +
    (micom.comparison_permissible ? "" : `<br>${micom.note}`);
  $("#micom2-table").innerHTML =
    `<tr><th>Construct</th><th>c</th><th>Criterion</th><th>Verdict</th></tr>` +
    micom.step2.map((s) => `<tr><td>${s.construct}</td><td>${s.value.toFixed(4)}</td>
      <td>${s.threshold}</td><td>${verdictBadge(s.verdict)}</td></tr>`).join("");
  $("#micom3-table").innerHTML =
    `<tr><th>Construct</th><th>Δ mean</th><th>95% CI</th><th>Equal</th>
      <th>Δ log var</th><th>95% CI</th><th>Equal</th></tr>` +
    micom.step3.map((s) => `<tr><td>${s.construct}</td>
      <td>${fmt(s.mean_diff)}</td><td>[${fmt(s.mean_ci_95[0])}; ${fmt(s.mean_ci_95[1])}]</td>
      <td>${verdictBadge(s.mean_equal ? "pass" : "review")}</td>
      <td>${fmt(s.logvar_diff)}</td><td>[${fmt(s.var_ci_95[0])}; ${fmt(s.var_ci_95[1])}]</td>
      <td>${verdictBadge(s.var_equal ? "pass" : "review")}</td></tr>`).join("");
  $("#mga-path-table").innerHTML =
    `<tr><th>Path</th><th>β (A)</th><th>β (B)</th><th>Δ</th><th>p (permutation)</th><th>Verdict</th></tr>` +
    mga.paths.map((p) => `<tr><td>${p.path}</td><td>${fmt(p.estimate_a)}</td>
      <td>${fmt(p.estimate_b)}</td><td>${fmt(p.difference)}</td><td>${fmt(p.p_value)}</td>
      <td>${badge(p.verdict, { different: "review", withheld: "fail" }[p.verdict] || "neutral")}</td></tr>`).join("");
}

function renderMediation(mediation) {
  $("#mediation-block").classList.toggle("hidden", !mediation.length);
  if (!mediation.length) return;
  $("#med-table").innerHTML =
    `<tr><th>Indirect path</th><th>β (indirect)</th><th>95% CI</th>
      <th>β (direct)</th><th>Classification</th></tr>` +
    mediation.map((m) => `<tr><td>${m.path.replaceAll("->", "→")}</td>
      <td>${fmt(m.indirect_effect)}</td>
      <td>${m.ci_95 ? `[${fmt(m.ci_95[0])}; ${fmt(m.ci_95[1])}]` : ""}</td>
      <td>${fmt(m.direct_effect)}</td>
      <td>${badge(m.classification, m.significant ? "supported" : "neutral")}</td></tr>`).join("");
}

/* IPMA table for the final outcome construct(s): importance = unstandardized
   total effect on the target, performance = construct score rescaled 0-100. */
function renderIpma(results) {
  const ipma = results && results.ipma;
  const perf = {};
  (ipma && ipma.performance || []).forEach((r) => { perf[r.row] = r.performance; });
  const te = {};
  (ipma && ipma.total_effects_unstd || []).forEach((r) => { te[r.row] = r; });
  const outgoing = new Set(state.paths.map((p) => p.from_construct));
  const endogenous = new Set(state.paths.map((p) => p.to_construct));
  const targets = Object.keys(perf).filter((c) => endogenous.has(c) && !outgoing.has(c));
  const rows = [];
  targets.forEach((target) => {
    Object.keys(perf).forEach((pred) => {
      const imp = te[pred] && te[pred][target];
      if (pred === target || typeof imp !== "number" || Math.abs(imp) < 1e-9) return;
      rows.push([target, pred, imp, perf[pred]]);
    });
  });
  $("#ipma-block").classList.toggle("hidden", !rows.length);
  if (!rows.length) return;
  rows.sort((x, y) => x[0].localeCompare(y[0]) || y[2] - x[2]);
  $("#ipma-note").textContent =
    `Target${targets.length > 1 ? "s" : ""}: ` +
    targets.map((t) => `${t} (performance ${perf[t].toFixed(1)})`).join(", ") +
    ". High importance + low performance = priority for action.";
  $("#ipma-table").innerHTML =
    `<tr><th>Target</th><th>Construct</th><th>Importance (total effect)</th>
      <th>Performance (0–100)</th></tr>` +
    rows.map(([t, p, imp, pf]) => `<tr><td>${t}</td><td><b>${p}</b></td>
      <td>${fmt(imp)}</td><td>${pf.toFixed(1)}</td></tr>`).join("");
  drawIpmaMap(rows.filter(([t]) => t === targets[0]), targets[0]);
}

/* IPMA priority map: importance (x) vs performance (y), quadrants split at the
   means. High importance + low performance (bottom right) = act first. */
function drawIpmaMap(rows, target) {
  const svg = $("#ipma-map");
  svg.innerHTML = "";
  if (rows.length < 2) return;
  const NS = "http://www.w3.org/2000/svg";
  const W = svg.clientWidth || 640, H = 380;
  const M = { top: 18, right: 96, bottom: 42, left: 52 };
  const pts = rows.map(([, name, imp, perf]) => ({ name, imp, perf }));
  const pad = (lo, hi) => { const d = (hi - lo) || 1; return [lo - 0.12 * d, hi + 0.12 * d]; };
  const [x0, x1] = pad(Math.min(0, ...pts.map((p) => p.imp)), Math.max(...pts.map((p) => p.imp)));
  const [y0, y1] = pad(Math.min(...pts.map((p) => p.perf)), Math.max(...pts.map((p) => p.perf)));
  const sx = (v) => M.left + ((v - x0) / (x1 - x0)) * (W - M.left - M.right);
  const sy = (v) => H - M.bottom - ((v - y0) / (y1 - y0)) * (H - M.top - M.bottom);
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
    if (text != null) e.textContent = text;
    svg.appendChild(e);
    return e;
  };

  // Recessive axes with ~4 ticks each
  const ticks = (lo, hi, n) => {
    const step = (hi - lo) / n;
    const mag = 10 ** Math.floor(Math.log10(step));
    const s = Math.ceil(step / mag) * mag;
    const out = [];
    for (let v = Math.ceil(lo / s) * s; v <= hi + 1e-9; v += s) out.push(v);
    return out;
  };
  ticks(x0, x1, 4).forEach((v) => {
    el("line", { x1: sx(v), y1: H - M.bottom, x2: sx(v), y2: H - M.bottom + 4, class: "axis" });
    el("text", { x: sx(v), y: H - M.bottom + 16, "text-anchor": "middle", class: "tick" },
       Math.abs(v) < 1 ? v.toFixed(2) : v.toFixed(1));
  });
  ticks(y0, y1, 4).forEach((v) => {
    el("line", { x1: M.left - 4, y1: sy(v), x2: M.left, y2: sy(v), class: "axis" });
    el("text", { x: M.left - 7, y: sy(v) + 3.5, "text-anchor": "end", class: "tick" }, v.toFixed(0));
  });
  el("line", { x1: M.left, y1: H - M.bottom, x2: W - M.right, y2: H - M.bottom, class: "axis" });
  el("line", { x1: M.left, y1: M.top, x2: M.left, y2: H - M.bottom, class: "axis" });
  el("text", { x: (M.left + W - M.right) / 2, y: H - 6, "text-anchor": "middle", class: "axis-title" },
     `Importance — unstandardized total effect on ${target}`);
  const yt = el("text", { x: 0, y: 0, "text-anchor": "middle", class: "axis-title" }, "Performance (0–100)");
  yt.setAttribute("transform", `translate(14, ${(M.top + H - M.bottom) / 2}) rotate(-90)`);

  // Quadrant split at the means; the action quadrant gets a muted caption
  const mx = pts.reduce((s, p) => s + p.imp, 0) / pts.length;
  const my = pts.reduce((s, p) => s + p.perf, 0) / pts.length;
  el("line", { x1: sx(mx), y1: M.top, x2: sx(mx), y2: H - M.bottom, class: "quadrant" });
  el("line", { x1: M.left, y1: sy(my), x2: W - M.right, y2: sy(my), class: "quadrant" });
  el("text", { x: W - M.right - 6, y: H - M.bottom - 8, "text-anchor": "end", class: "quadrant-caption" },
     "high importance · low performance → act first");

  // Marks: 10px dots with a surface ring, direct label per construct
  const tip = $("#ipma-tip");
  pts.sort((a, b) => a.perf - b.perf);
  let lastY = -1e9;
  pts.forEach((p) => {
    const cx = sx(p.imp), cy = sy(p.perf);
    const dot = el("circle", { cx, cy, r: 5, class: "ipma-dot" });
    let ly = cy + 4;
    if (Math.abs(ly - lastY) < 13) ly = lastY - 13;  // nudge colliding labels apart
    lastY = ly;
    const right = cx < W - M.right - 60;
    el("text", { x: right ? cx + 9 : cx - 9, y: ly,
                 "text-anchor": right ? "start" : "end", class: "ipma-label" }, p.name);
    dot.addEventListener("mouseenter", () => {
      tip.innerHTML = `<b>${p.name}</b><br>importance ${p.imp.toFixed(3)} · performance ${p.perf.toFixed(1)}`;
      tip.style.left = `${cx + 12}px`;
      tip.style.top = `${cy - 10}px`;
      tip.classList.remove("hidden");
    });
    dot.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
}
