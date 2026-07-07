/* Read-only viewer for a shared analysis (opened via ?token=…). No build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const token = new URLSearchParams(location.search).get("token");

function setStatus(sel, msg, isError = false) {
  const el = $(sel);
  el.textContent = msg;
  el.classList.toggle("error", isError);
}
function badge(text, cls) { return `<span class="badge ${cls}">${text}</span>`; }
function verdictBadge(v) {
  const cls = { pass: "pass", fail: "fail", review: "review", supported: "supported" }[v]
    || (v === "not supported" ? "not-supported" : "neutral");
  return badge(v, cls);
}
function fmt(v) { return typeof v === "number" ? v.toFixed(3) : (v ?? ""); }
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : "request failed");
  return body;
}

let scope = "view";

async function load() {
  if (!token) { setStatus("#shared-status", "No share token in the link.", true); return; }
  let data;
  try {
    data = await api(`/api/shared/${encodeURIComponent(token)}`);
  } catch (err) {
    setStatus("#shared-status", err.message, true);
    return;
  }
  scope = data.scope;
  setStatus("#shared-status", "");
  $("#shared-body").classList.remove("hidden");
  render(data);
}

function render(d) {
  const s = d.assessment.summary;
  $("#shared-meta").textContent =
    `${d.dataset.name || "dataset"} · ${d.dataset.n_observations} respondents · `
    + `${d.model.constructs.length} constructs · shared ${d.created_at || ""}`;

  const fit = d.assessment.structural_model.find((m) => m.family === "model_fit");
  const r2s = d.assessment.structural_model
    .filter((m) => m.family === "explanatory_power" && typeof m.value === "number")
    .map((m) => m.value);
  const maxR2 = r2s.length ? Math.max(...r2s) : null;
  $("#shared-cards").innerHTML = `
    <div class="stat ${s.hypotheses_supported === s.hypotheses_total ? "good" : ""}">
      <div class="v">${s.hypotheses_supported}/${s.hypotheses_total}</div><div class="l">hypotheses supported</div></div>
    <div class="stat good"><div class="v">${s.pass}</div><div class="l">checks passed</div></div>
    <div class="stat ${s.review ? "warn" : ""}"><div class="v">${s.review}</div><div class="l">to review</div></div>
    ${maxR2 != null ? `<div class="stat"><div class="v">${fmt(maxR2)}</div><div class="l">largest R²</div></div>` : ""}
    ${fit ? `<div class="stat ${{ pass: "good", review: "warn", fail: "bad" }[fit.verdict] || ""}">
      <div class="v">${fmt(fit.value)}</div><div class="l">SRMR</div></div>` : ""}`;

  $("#shared-hyp").innerHTML =
    `<tr><th>#</th><th>Path</th><th>β</th><th>95% CI</th><th>Verdict</th></tr>` +
    d.assessment.hypotheses.map((h) => `
      <tr><td>${h.hypothesis}</td><td>${esc(h.path)}</td><td>${fmt(h.estimate)}</td>
      <td>${h.ci_95 ? `[${fmt(h.ci_95[0])}, ${fmt(h.ci_95[1])}]` : ""}</td>
      <td>${verdictBadge(h.verdict)}</td></tr>`).join("");

  const med = d.assessment.mediation || [];
  $("#shared-med-block").classList.toggle("hidden", !med.length);
  if (med.length) {
    $("#shared-med").innerHTML =
      `<tr><th>Indirect path</th><th>Effect</th><th>95% CI</th><th>Classification</th></tr>` +
      med.map((m) => `<tr><td>${esc(m.path)}</td><td>${fmt(m.indirect_effect)}</td>
        <td>${m.ci_95 ? `[${fmt(m.ci_95[0])}, ${fmt(m.ci_95[1])}]` : ""}</td>
        <td>${esc(m.classification || "")}</td></tr>`).join("");
  }

  $("#shared-struct").innerHTML =
    `<tr><th>Metric</th><th>Value</th><th>Threshold</th><th>Verdict</th></tr>` +
    d.assessment.structural_model.map((m) => `
      <tr><td>${esc(m.metric || m.construct || m.family)}</td><td>${fmt(m.value)}</td>
      <td>${esc(m.threshold || "")}</td><td>${verdictBadge(m.verdict)}</td></tr>`).join("");

  renderComments(d.comments || []);
  if (scope === "comment") {
    $("#comment-form").classList.remove("hidden");
  } else {
    $("#viewonly-note").classList.remove("hidden");
  }
}

function renderComments(comments) {
  const log = $("#comment-log");
  if (!comments.length) {
    log.innerHTML = `<div class="chat-msg assistant">No comments yet.</div>`;
    return;
  }
  log.innerHTML = comments.map((c) => `
    <div class="chat-msg assistant">
      <b>${esc(c.author)}</b> <span class="hint">${esc(c.created_at)}</span><br>
      ${esc(c.body).replaceAll("\n", "<br>")}
    </div>`).join("");
  log.scrollTop = log.scrollHeight;
}

$("#comment-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = $("#comment-body").value.trim();
  if (!body) return;
  setStatus("#comment-status", "Posting…");
  try {
    await api(`/api/shared/${encodeURIComponent(token)}/comments`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ author: $("#comment-author").value.trim(), body }),
    });
    $("#comment-body").value = "";
    setStatus("#comment-status", "");
    const refreshed = await api(`/api/shared/${encodeURIComponent(token)}`);
    renderComments(refreshed.comments || []);
  } catch (err) {
    setStatus("#comment-status", err.message, true);
  }
});

load();
