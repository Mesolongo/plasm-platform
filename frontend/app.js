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
api("/api/ai/status").then(({ configured }) => {
  const el = $("#ai-badge");
  el.textContent = configured ? "AI: connected" : "AI: no API key";
  el.classList.toggle("off", !configured);
  el.title = configured ? "" : "Set ANTHROPIC_API_KEY and restart the server to enable AI features";
});

/* ------------------------------ Step 1: data ------------------------------ */
$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("#file-input").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const code = $("#missing-code").value.trim();
  if (code) fd.append("missing_value", code);
  setStatus("#upload-status", "Uploading and auditing…");
  try {
    state.dataset = await api("/api/datasets", { method: "POST", body: fd });
    renderDataset();
    setStatus("#upload-status", "");
  } catch (err) {
    setStatus("#upload-status", err.message, true);
  }
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
  $("#btn-json").href = `/api/analyses/${state.analysis.id}/results`;

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
  renderIpma(state.analysis.results);
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
}
