"""plsem-platform backend.

Flow: upload dataset -> variable dictionary + audit -> submit model spec -> the
job queue runs the engine -> poll status -> results JSON. Long-running work
(estimation, MGA, citation lookups) goes through the in-process queue in jobs.py:
the POST answers 202 with a pollable job, so requests never block on the engine.

Run:  .venv/bin/uvicorn backend.app.main:app --reload  (from the project root)
Docs: http://127.0.0.1:8000/docs
"""
import contextlib
import datetime
import io
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import List, Literal, Optional

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, auth, citations, collab, jobs, publish, workspace
from .assess import assess, assess_mga
from .audit import run_audit, variable_dictionary
from .engine import MGA_SCRIPT, run_engine
from .export import write_xlsx
from .report import build_report
from .storage import ROOT, analysis_dir, dataset_dir, new_id, read_json, write_json


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    jobs.recover_interrupted()  # fail jobs orphaned by a previous server death
    yield


app = FastAPI(title="plsem-platform", version="0.3.0", lifespan=_lifespan)
app.include_router(auth.router)
app.include_router(workspace.router)


# --------------------------------------------------------------------------- #
# Login gate. Everything under /api needs a session, except auth itself and the
# token-based share viewer (its capability URL is the credential). AI-powered
# POSTs are additionally charged against the user's daily quota so a single
# account can't drain the server's Anthropic credits.
# --------------------------------------------------------------------------- #
_PUBLIC_API_PREFIXES = ("/api/auth/", "/api/shared/")
_AI_POST_SUFFIXES = ("/chat", "/interpretation", "/manuscript", "/propose-model")


@app.middleware("http")
async def _require_login(request, call_next):
    path = request.url.path
    if path.startswith("/api") and not path.startswith(_PUBLIC_API_PREFIXES):
        username = auth.session_username(request.cookies.get(auth.COOKIE))
        if not username:
            return JSONResponse({"detail": "login required"}, status_code=401)
        if (request.method == "POST" and path.endswith(_AI_POST_SUFFIXES)
                and ai.is_configured() and not auth.charge_ai_call(username)):
            return JSONResponse(
                {"detail": f"daily AI limit reached ({auth.ai_daily_limit()} "
                           "calls/day) — try again tomorrow"}, status_code=429)
        token = auth.current_username.set(username)
        try:
            return await call_next(request)
        finally:
            auth.current_username.reset(token)
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #

def _read_sav(raw: bytes) -> tuple[pd.DataFrame, dict]:
    """Read an SPSS .sav file; returns the data and variable labels."""
    import pyreadstat
    with tempfile.NamedTemporaryFile(suffix=".sav") as tmp:
        tmp.write(raw)
        tmp.flush()
        df, meta = pyreadstat.read_sav(tmp.name)
    labels = {name: label for name, label in
              zip(meta.column_names, meta.column_labels) if label}
    return df, labels


def _persist_dataset(df: pd.DataFrame, source: str, missing_value: Optional[str],
                     variable_labels: Optional[dict] = None) -> dict:
    """Normalize an ingested frame to a dataset: write raw.csv + meta.json, return meta.

    Shared by every ingestion path (file upload, SQL, Google Sheets) so the variable
    dictionary and audit are identical regardless of where the data came from.
    """
    if df.empty:
        raise HTTPException(422, "the source returned no data rows")

    dataset_id = new_id("ds")
    d = dataset_dir(dataset_id, create=True)
    df.to_csv(d / "raw.csv", index=False)

    meta = {
        "id": dataset_id,
        "owner": auth.current_username.get(),
        "filename": source,
        "uploaded_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "missing_value": missing_value,
        "n_observations": len(df),
        "variables": variable_dictionary(df, missing_value),
        "variable_labels": variable_labels,
        "audit": run_audit(df, missing_value),
    }
    write_json(d / "meta.json", meta)
    return meta


@app.post("/api/datasets")
async def upload_dataset(file: UploadFile, missing_value: Optional[str] = Form(None)):
    """Upload CSV, Excel, or SPSS .sav; returns the variable dictionary and data audit."""
    raw = await file.read()
    name = (file.filename or "upload").lower()
    variable_labels = None
    try:
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw))
        elif name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
        elif name.endswith(".sav"):
            df, variable_labels = _read_sav(raw)
        else:
            raise HTTPException(415, "unsupported file type — upload .csv, .xlsx, .xls, or .sav")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"could not parse file: {exc}")

    return _persist_dataset(df, file.filename or "upload", missing_value, variable_labels)


# --------------------------------------------------------------------------- #
# Dataset connectors (Phase 3): pull data from a SQL database or a Google Sheet
# instead of a file upload. Both funnel through _persist_dataset, so downstream
# audit / modelling / assessment are unchanged.
# --------------------------------------------------------------------------- #

# Reject statements that write to or mutate the database. Ingestion is read-only:
# the connector runs the user's query verbatim, so this is a guardrail against a
# fat-fingered (or pasted) DDL/DML statement, not a security boundary — pair it
# with a read-only database account.
_SQL_FORBIDDEN = {"insert", "update", "delete", "drop", "alter", "create",
                  "truncate", "grant", "revoke", "merge", "replace", "call", "exec"}


def _reject_non_select(query: str) -> None:
    stripped = query.strip().rstrip(";")
    if not stripped:
        raise HTTPException(422, "query is empty")
    # Block multiple statements and any leading write keyword.
    if ";" in stripped:
        raise HTTPException(422, "only a single SELECT statement is allowed")
    first = stripped.split(None, 1)[0].lower()
    if first in _SQL_FORBIDDEN or (first not in {"select", "with"}):
        raise HTTPException(422, "only read-only SELECT queries are permitted")


class SqlDatasetRequest(BaseModel):
    dsn: str = Field(..., description="SQLAlchemy database URL, e.g. postgresql+psycopg://user:pw@host/db")
    query: str = Field(..., description="A single read-only SELECT statement")
    missing_value: Optional[str] = None


@app.post("/api/datasets/sql")
def connect_sql(req: SqlDatasetRequest):
    """Ingest a dataset from a SQL database via a SQLAlchemy DSN and a SELECT query."""
    _reject_non_select(req.query)
    try:
        from sqlalchemy import create_engine, text
    except ModuleNotFoundError:
        raise HTTPException(501, "SQL connector requires SQLAlchemy on the server")
    try:
        engine = create_engine(req.dsn)
        with engine.connect() as conn:
            df = pd.read_sql_query(text(req.query), conn)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"SQL connection or query failed: {exc}")
    finally:
        try:
            engine.dispose()
        except Exception:
            pass

    label = urllib.parse.urlsplit(req.dsn).path.lstrip("/") or "sql"
    return _persist_dataset(df, f"sql:{label}", req.missing_value)


class SheetDatasetRequest(BaseModel):
    url: str = Field(..., description="A Google Sheets share/edit URL (sheet must be link-viewable)")
    missing_value: Optional[str] = None


def _gsheet_csv_url(url: str) -> str:
    """Turn a Google Sheets URL into its CSV-export URL, preserving the tab (gid)."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise HTTPException(422, "not a Google Sheets URL")
    sheet_id = m.group(1)
    gid_match = re.search(r"[#&?]gid=(\d+)", url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


@app.post("/api/datasets/gsheet")
def connect_gsheet(req: SheetDatasetRequest):
    """Ingest a dataset from a link-viewable Google Sheet (no OAuth; uses CSV export)."""
    csv_url = _gsheet_csv_url(req.url)
    try:
        resp = httpx.get(csv_url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(422, f"could not fetch the sheet: {exc}")
    ctype = resp.headers.get("content-type", "")
    if "text/csv" not in ctype:
        raise HTTPException(422, "the sheet is not link-viewable — set sharing to "
                                 "'Anyone with the link' (Viewer)")
    try:
        df = pd.read_csv(io.BytesIO(resp.content))
    except Exception as exc:
        raise HTTPException(422, f"could not parse the sheet as CSV: {exc}")
    return _persist_dataset(df, "gsheet", req.missing_value)


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str):
    d = dataset_dir(dataset_id)
    if not (d / "meta.json").exists():
        raise HTTPException(404, "dataset not found")
    return read_json(d / "meta.json")


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #

class ConstructSpec(BaseModel):
    name: str
    indicators: List[str] = []
    dimensions: List[str] = []  # higher-order constructs only
    measurement: Literal["reflective", "formative", "single_item",
                         "higher_order_reflective", "higher_order_formative"]


class PathSpec(BaseModel):
    from_construct: str
    to_construct: str


class InteractionSpec(BaseModel):
    """Two-stage interaction term for moderation; the engine names it 'IV*MOD'."""
    iv: str
    moderator: str


class AnalysisRequest(BaseModel):
    dataset_id: str
    constructs: List[ConstructSpec]
    paths: List[PathSpec]
    interactions: List[InteractionSpec] = []
    nboot: int = Field(default=5000, ge=100, le=10000)
    seed: int = 123
    prediction: bool = True  # PLSpredict k-fold out-of-sample metrics
    override_gates: bool = False  # run despite assumption-gate violations

    def validate_spec(self):
        for c in self.constructs:
            hoc = c.measurement.startswith("higher_order")
            if hoc and (len(c.dimensions) < 2 or c.indicators):
                raise HTTPException(422, f"higher-order construct {c.name} needs >= 2 "
                                         "dimensions and no direct indicators")
            if not hoc and not c.indicators:
                raise HTTPException(422, f"construct {c.name} has no indicators")


def assumption_gates(ds_meta: dict, req: AnalysisRequest) -> list[dict]:
    """Pre-estimation gates from the dataset audit + the model's demands.

    Violations block the run unless the user explicitly overrides — the override
    is recorded with the analysis so the report can disclose it.
    """
    violations = []
    n = ds_meta["n_observations"]
    used = {i for c in req.constructs for i in c.indicators}

    incoming: dict[str, int] = {}
    for p in req.paths:
        incoming[p.to_construct] = incoming.get(p.to_construct, 0) + 1
    max_arrows = max([len(c.indicators) for c in req.constructs
                      if c.measurement == "formative"] + list(incoming.values()) + [0])
    if max_arrows and n < 10 * max_arrows:
        violations.append({
            "gate": "sample_size_10x",
            "detail": f"n = {n} is below 10 x {max_arrows} (the largest number of "
                      f"arrows pointing at any construct)",
            "citation": "Hair et al. (2022)",
        })
    for f in (ds_meta.get("audit") or {}).get("findings") or []:
        var = f.get("variable")
        if f["check"] == "missing_values" and f["severity"] == "warning" and var in used:
            violations.append({
                "gate": "excessive_missing",
                "detail": f"indicator {var} has {f['pct']}% missing (> 5% — mean "
                          f"replacement is not defensible)",
                "citation": "Hair et al. (2022)",
            })
        elif f["check"] == "zero_variance" and var in used:
            violations.append({
                "gate": "zero_variance",
                "detail": f"indicator {var} is constant and cannot be estimated",
                "citation": "engine requirement",
            })
        elif f["check"] == "straight_lining":
            violations.append({
                "gate": "straight_lining",
                "detail": f"{f['count']} respondent(s) answered identically across "
                          f"all items — review before trusting the estimates",
                "citation": "Hair et al. (2022)",
            })
    return violations


@app.post("/api/analyses", status_code=202)
def create_analysis(req: AnalysisRequest):
    """Validate the spec and queue the estimation; poll GET /api/analyses/{id}
    until status is completed (results ready) or failed (error in the meta)."""
    ds_dir = dataset_dir(req.dataset_id)
    if not (ds_dir / "meta.json").exists():
        raise HTTPException(404, "dataset not found")
    ds_meta = read_json(ds_dir / "meta.json")

    req.validate_spec()
    violations = assumption_gates(ds_meta, req)
    if violations and not req.override_gates:
        raise HTTPException(422, {
            "stage": "assumption_gates",
            "message": "assumption checks failed: "
                       + " · ".join(v["detail"] for v in violations)
                       + " — review the data, or run again with the override",
            "violations": violations,
        })

    analysis_id = new_id("an")
    a_dir = analysis_dir(analysis_id, create=True)
    engine_request = {
        "schema_version": 2,
        "data_csv": str(ds_dir / "raw.csv"),
        "missing_value": ds_meta.get("missing_value"),
        "options": {"nboot": req.nboot, "seed": req.seed, "prediction": req.prediction},
        "constructs": [c.model_dump() for c in req.constructs],
        "interactions": [i.model_dump() for i in req.interactions],
        "paths": [p.model_dump() for p in req.paths],
    }
    write_json(a_dir / "request.json", engine_request)

    # The job id goes into the meta before the worker can touch it, so the two
    # files never race; the worker mirrors the final status back via on_done.
    job_id = new_id("job")
    meta = {
        "id": analysis_id,
        "owner": auth.current_username.get(),
        "dataset_id": req.dataset_id,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "queued",
        "job_id": job_id,
        "assumption_gates": {"violations": violations,
                             "overridden": bool(violations)},
    }
    write_json(a_dir / "meta.json", meta)

    def estimate():
        record("running", None)  # mirror the job's start so polling shows progress
        run_engine(a_dir / "request.json", a_dir / "results.json")

    def record(status, error):
        m = read_json(a_dir / "meta.json")
        m["status"] = status
        if error:
            m["error"] = error
        write_json(a_dir / "meta.json", m)

    jobs.submit("estimate", analysis_id, estimate, on_done=record, job_id=job_id)
    return {**meta, "results_url": f"/api/analyses/{analysis_id}/results"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Poll a queued job (estimation, MGA, citations); error is set when failed."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    a_dir = analysis_dir(analysis_id)
    if not (a_dir / "meta.json").exists():
        raise HTTPException(404, "analysis not found")
    return read_json(a_dir / "meta.json")


@app.get("/api/analyses/{analysis_id}/spec")
def get_spec(analysis_id: str):
    """The model spec an analysis was run with — lets the frontend reopen a past
    analysis with its builder state intact. Server-side paths are not exposed."""
    a_dir = analysis_dir(analysis_id)
    if not (a_dir / "request.json").exists():
        raise HTTPException(404, "analysis not found")
    request = read_json(a_dir / "request.json")
    return {"constructs": request.get("constructs", []),
            "paths": request.get("paths", []),
            "interactions": request.get("interactions", []),
            "options": request.get("options", {})}


@app.get("/api/analyses/{analysis_id}/results")
def get_results(analysis_id: str):
    a_dir = analysis_dir(analysis_id)
    if not (a_dir / "results.json").exists():
        raise HTTPException(404, "results not available")
    return read_json(a_dir / "results.json")


def _load_analysis(analysis_id: str):
    a_dir = analysis_dir(analysis_id)
    if not (a_dir / "results.json").exists():
        raise HTTPException(404, "results not available")
    request = read_json(a_dir / "request.json")
    results = read_json(a_dir / "results.json")
    meta = read_json(a_dir / "meta.json")
    return a_dir, meta, request, results


@app.get("/api/analyses/{analysis_id}/assessment")
def get_assessment(analysis_id: str):
    """Rule-based threshold verdicts + hypothesis support (no AI involved)."""
    _, _, request, results = _load_analysis(analysis_id)
    return assess(results, request)


class MGARequest(BaseModel):
    """Two-group comparison; MICOM invariance testing always runs first."""
    group_variable: str
    value_a: str
    value_b: str
    npermutations: int = Field(default=1000, ge=100, le=5000)
    seed: int = 123


@app.post("/api/analyses/{analysis_id}/mga", status_code=202)
def create_mga(analysis_id: str, req: MGARequest):
    """Multi-group analysis: MICOM (Henseler et al. 2016) gates the permutation
    test on path differences (Chin & Dibbern 2010) — no invariance, no comparison.
    Queues the permutation run; poll the returned job, then GET .../mga."""
    a_dir, _, request, _ = _load_analysis(analysis_id)
    engine_request = {
        **request,
        "group": {"variable": req.group_variable,
                  "value_a": req.value_a, "value_b": req.value_b},
        "options": {"npermutations": req.npermutations, "seed": req.seed},
    }
    write_json(a_dir / "mga_request.json", engine_request)

    def run():
        run_engine(a_dir / "mga_request.json", a_dir / "mga.json", script=MGA_SCRIPT)

    return jobs.submit("mga", analysis_id, run)


@app.get("/api/analyses/{analysis_id}/mga")
def get_mga(analysis_id: str):
    a_dir = analysis_dir(analysis_id)
    p = a_dir / "mga.json"
    if not p.exists():
        raise HTTPException(404, "no multi-group analysis run yet")
    return assess_mga(read_json(p))


@app.get("/api/analyses/{analysis_id}/report.docx")
def get_report(analysis_id: str):
    a_dir, meta, request, results = _load_analysis(analysis_id)
    dataset_meta = read_json(dataset_dir(meta["dataset_id"]) / "meta.json")
    interp_path = a_dir / "interpretation.json"
    interpretation = read_json(interp_path) if interp_path.exists() else None
    doc = build_report(dataset_meta, request, results, assess(results, request),
                       interpretation=interpretation, mga=_mga_if_any(a_dir))
    out = a_dir / "report.docx"
    doc.save(out)
    return FileResponse(out, filename=f"plsem_report_{analysis_id}.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def _mga_if_any(a_dir):
    p = a_dir / "mga.json"
    return assess_mga(read_json(p)) if p.exists() else None


@app.get("/api/analyses/{analysis_id}/results.xlsx")
def get_xlsx(analysis_id: str):
    """Full results workbook — every engine table on its own sheet."""
    a_dir, _, request, results = _load_analysis(analysis_id)
    out = a_dir / "results.xlsx"
    write_xlsx(out, results, assess(results, request), mga=_mga_if_any(a_dir))
    return FileResponse(out, filename=f"plsem_results_{analysis_id}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _find_soffice() -> str | None:
    return (shutil.which("soffice")
            or next((p for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)
                     if Path(p).exists()), None))


@app.get("/api/analyses/{analysis_id}/report.pdf")
def get_report_pdf(analysis_id: str):
    """Word report converted to PDF via LibreOffice (when installed)."""
    soffice = _find_soffice()
    if not soffice:
        raise HTTPException(501, "PDF export needs LibreOffice (`brew install --cask "
                                 "libreoffice`); the Word report is available meanwhile")
    a_dir, meta, request, results = _load_analysis(analysis_id)
    dataset_meta = read_json(dataset_dir(meta["dataset_id"]) / "meta.json")
    interp_path = a_dir / "interpretation.json"
    interpretation = read_json(interp_path) if interp_path.exists() else None
    doc = build_report(dataset_meta, request, results, assess(results, request),
                       interpretation=interpretation, mga=_mga_if_any(a_dir))
    doc.save(a_dir / "report.docx")
    proc = subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                           "--outdir", str(a_dir), str(a_dir / "report.docx")],
                          capture_output=True, text=True, timeout=120)
    pdf = a_dir / "report.pdf"
    if proc.returncode != 0 or not pdf.exists():
        raise HTTPException(500, f"PDF conversion failed: {proc.stderr[-500:]}")
    return FileResponse(pdf, filename=f"plsem_report_{analysis_id}.pdf",
                        media_type="application/pdf")


class ChatRequest(BaseModel):
    message: str
    history: List[dict] = []  # [{role: user|assistant, content: str}], client-held


@app.post("/api/analyses/{analysis_id}/chat")
def analysis_chat(analysis_id: str, req: ChatRequest):
    """Research-assistant chat, grounded in this analysis's assessment."""
    if not ai.is_configured():
        raise HTTPException(503, ai.NOT_CONFIGURED)
    a_dir, _, request, results = _load_analysis(analysis_id)
    history = [{"role": h["role"], "content": h["content"]} for h in req.history
               if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str)]
    try:
        reply = ai.chat(request, assess(results, request), history, req.message,
                        mga=_mga_if_any(a_dir))
    except Exception as exc:
        raise HTTPException(502, f"AI chat failed: {exc}")
    return {"reply": reply}


class InterpretRequest(BaseModel):
    study_description: str = ""


@app.post("/api/analyses/{analysis_id}/interpretation")
def create_interpretation(analysis_id: str, req: InterpretRequest):
    """AI report writer: narrative sections grounded in the rule-based assessment."""
    if not ai.is_configured():
        raise HTTPException(503, ai.NOT_CONFIGURED)
    a_dir, _, request, results = _load_analysis(analysis_id)
    try:
        interpretation = ai.interpret(request, assess(results, request), req.study_description)
    except Exception as exc:
        raise HTTPException(502, f"AI interpretation failed: {exc}")
    write_json(a_dir / "interpretation.json", interpretation)
    return interpretation


@app.get("/api/analyses/{analysis_id}/interpretation")
def get_interpretation(analysis_id: str):
    a_dir = analysis_dir(analysis_id)
    p = a_dir / "interpretation.json"
    if not p.exists():
        raise HTTPException(404, "no interpretation generated yet")
    return read_json(p)


# --------------------------------------------------------------------------- #
# Collaboration (Phase 3): token-based share links + comment threads. Sharing is
# read-only; a link may additionally grant commenting. No accounts — see collab.py.
# --------------------------------------------------------------------------- #

class ShareRequest(BaseModel):
    scope: Literal["view", "comment"] = "view"
    label: str = ""


def _require_analysis(analysis_id: str):
    if not (analysis_dir(analysis_id) / "meta.json").exists():
        raise HTTPException(404, "analysis not found")


@app.post("/api/analyses/{analysis_id}/shares")
def create_share(analysis_id: str, req: ShareRequest):
    """Mint a read-only (optionally commentable) share link for an analysis."""
    _require_analysis(analysis_id)
    share = collab.create_share(analysis_id, req.scope, req.label)
    return {**share, "url": f"/app/shared.html?token={share['token']}"}


@app.get("/api/analyses/{analysis_id}/shares")
def list_shares(analysis_id: str):
    _require_analysis(analysis_id)
    return {"shares": [{**s, "url": f"/app/shared.html?token={s['token']}"}
                       for s in collab.list_shares(analysis_id)]}


@app.delete("/api/analyses/{analysis_id}/shares/{token}")
def revoke_share(analysis_id: str, token: str):
    _require_analysis(analysis_id)
    if not collab.revoke_share(analysis_id, token):
        raise HTTPException(404, "share link not found")
    return {"revoked": token}


@app.get("/api/analyses/{analysis_id}/comments")
def owner_comments(analysis_id: str):
    """Owner-side view of the comment thread (no token needed)."""
    _require_analysis(analysis_id)
    return {"comments": collab.list_comments(analysis_id)}


def _shared_bundle(token: str) -> tuple[dict, dict]:
    """Resolve a share token to (share, read-only payload). 404 if the link is dead."""
    share = collab.resolve_token(token)
    if not share:
        raise HTTPException(404, "share link is invalid or has been revoked")
    a_dir, meta, request, results = _load_analysis(share["analysis_id"])
    ds_meta = read_json(dataset_dir(meta["dataset_id"]) / "meta.json")
    payload = {
        "scope": share["scope"],
        "label": share["label"],
        "created_at": meta.get("created_at"),
        "dataset": {"name": ds_meta.get("filename"),
                    "n_observations": ds_meta.get("n_observations")},
        "model": {
            "constructs": [{"name": c["name"], "measurement": c["measurement"]}
                           for c in request["constructs"]],
            "paths": [f"{p['from_construct']} -> {p['to_construct']}"
                      for p in request["paths"]],
        },
        "assessment": assess(results, request),
        "mga": _mga_if_any(a_dir),
        "comments": collab.list_comments(share["analysis_id"]),
    }
    return share, payload


@app.get("/api/shared/{token}")
def shared_view(token: str):
    """Read-only bundle behind a share link: findings, assessment, comments."""
    _, payload = _shared_bundle(token)
    return payload


class CommentRequest(BaseModel):
    author: str = ""
    body: str


@app.post("/api/shared/{token}/comments")
def shared_comment(token: str, req: CommentRequest):
    """Leave a comment via a share link (only if the link grants commenting)."""
    share = collab.resolve_token(token)
    if not share:
        raise HTTPException(404, "share link is invalid or has been revoked")
    if share["scope"] != "comment":
        raise HTTPException(403, "this share link is view-only")
    try:
        return collab.add_comment(share["analysis_id"], req.author, req.body)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# --------------------------------------------------------------------------- #
# Literature citations (Phase 3): candidate references grounding each hypothesis,
# from Crossref. Suggestions for a literature review, not automatic support claims.
# --------------------------------------------------------------------------- #

@app.post("/api/analyses/{analysis_id}/citations", status_code=202)
def create_citations(analysis_id: str):
    """Find candidate grounding references for each hypothesized direct path.
    Crossref lookups take seconds per hypothesis, so the search is queued;
    poll the returned job, then GET .../citations."""
    a_dir, _, request, results = _load_analysis(analysis_id)
    hypotheses = assess(results, request)["hypotheses"]

    def run():
        suggestions = citations.suggest_for_hypotheses(hypotheses)
        write_json(a_dir / "citations.json", {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source": "Crossref", "hypotheses": suggestions})

    return jobs.submit("citations", analysis_id, run)


@app.get("/api/analyses/{analysis_id}/citations")
def get_citations(analysis_id: str):
    p = analysis_dir(analysis_id) / "citations.json"
    if not p.exists():
        raise HTTPException(404, "no citations generated yet")
    return read_json(p)


# --------------------------------------------------------------------------- #
# Publishing assistant (Phase 4): reviewer-anticipation checks (deterministic)
# and AI-drafted submission front matter in a journal or thesis register.
# --------------------------------------------------------------------------- #

def _reviewer_concerns(analysis_id: str) -> tuple[Path, dict, list[dict]]:
    """Compute the deterministic reviewer concerns for an analysis.

    Returns (analysis_dir, assessment, concerns) so the manuscript drafter can
    reuse the assessment without recomputing it.
    """
    a_dir, meta, request, results = _load_analysis(analysis_id)
    assessment = assess(results, request)
    ds_meta = read_json(dataset_dir(meta["dataset_id"]) / "meta.json")
    concerns = publish.reviewer_checks(assessment, request, dataset_meta=ds_meta,
                                       analysis_meta=meta, mga=_mga_if_any(a_dir))
    return a_dir, assessment, concerns


@app.get("/api/analyses/{analysis_id}/reviewer-check")
def reviewer_check(analysis_id: str):
    """Anticipate peer-reviewer concerns from the rule-based assessment (no AI)."""
    _, _, concerns = _reviewer_concerns(analysis_id)
    return {"concerns": concerns, "summary": publish.summarize(concerns)}


class ManuscriptRequest(BaseModel):
    study_description: str = ""
    target_journal: str = ""
    mode: Literal["journal", "thesis"] = "journal"


@app.post("/api/analyses/{analysis_id}/manuscript")
def create_manuscript(analysis_id: str, req: ManuscriptRequest):
    """AI publishing assistant: draft submission front matter + reviewer responses,
    grounded in the assessment and the deterministic reviewer concerns."""
    if not ai.is_configured():
        raise HTTPException(503, ai.NOT_CONFIGURED)
    a_dir, assessment, concerns = _reviewer_concerns(analysis_id)
    _, _, request, _ = _load_analysis(analysis_id)
    try:
        draft = ai.draft_manuscript(request, assessment, concerns, req.study_description,
                                    req.target_journal, req.mode)
    except Exception as exc:
        raise HTTPException(502, f"AI manuscript drafting failed: {exc}")
    payload = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
               "mode": req.mode, "target_journal": req.target_journal,
               "reviewer_concerns": concerns, "draft": draft}
    write_json(a_dir / "manuscript.json", payload)
    return payload


@app.get("/api/analyses/{analysis_id}/manuscript")
def get_manuscript(analysis_id: str):
    p = analysis_dir(analysis_id) / "manuscript.json"
    if not p.exists():
        raise HTTPException(404, "no manuscript drafted yet")
    return read_json(p)


# --------------------------------------------------------------------------- #
# AI (gated on credentials)
# --------------------------------------------------------------------------- #

class ProposeRequest(BaseModel):
    study_description: str = ""


@app.get("/api/ai/status")
def ai_status():
    return {"configured": ai.is_configured()}


@app.post("/api/datasets/{dataset_id}/propose-model")
def propose_model(dataset_id: str, req: ProposeRequest):
    """AI model architect: proposes a model spec from the variable dictionary."""
    d = dataset_dir(dataset_id)
    if not (d / "meta.json").exists():
        raise HTTPException(404, "dataset not found")
    if not ai.is_configured():
        raise HTTPException(503, ai.NOT_CONFIGURED)
    meta = read_json(d / "meta.json")
    try:
        return ai.propose_model(meta["variables"], req.study_description)
    except Exception as exc:
        raise HTTPException(502, f"AI proposal failed: {exc}")


@app.get("/api/fixtures/model-spec")
def example_spec():
    """Reference spec (corp-rep) for demos and for the frontend's 'load example'."""
    spec = read_json(ROOT / "ai" / "fixtures" / "model_spec_reference.json")
    spec.pop("_provenance", None)
    return spec


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #

@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/app/")


app.mount("/app", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
