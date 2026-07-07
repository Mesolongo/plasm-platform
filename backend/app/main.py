"""plsem-platform backend — Phase 1 vertical slice.

Flow: upload dataset -> variable dictionary + audit -> submit model spec -> engine
estimates -> results JSON. Estimation runs synchronously in the MVP (a corp-rep-sized
job with 1k bootstraps takes seconds; 10k takes ~1 min). Celery + Redis replace the
sync call in a later increment without changing the API.

Run:  .venv/bin/uvicorn backend.app.main:app --reload  (from the project root)
Docs: http://127.0.0.1:8000/docs
"""
import datetime
import io
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Literal, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai
from .assess import assess, assess_mga
from .audit import run_audit, variable_dictionary
from .engine import MGA_SCRIPT, EngineError, run_engine
from .export import write_pptx, write_xlsx
from .report import build_report
from .storage import ROOT, analysis_dir, dataset_dir, new_id, read_json, write_json

app = FastAPI(title="plsem-platform", version="0.2.0")


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

    if df.empty:
        raise HTTPException(422, "the file contains no data rows")

    dataset_id = new_id("ds")
    d = dataset_dir(dataset_id, create=True)
    df.to_csv(d / "raw.csv", index=False)

    meta = {
        "id": dataset_id,
        "filename": file.filename,
        "uploaded_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "missing_value": missing_value,
        "n_observations": len(df),
        "variables": variable_dictionary(df, missing_value),
        "variable_labels": variable_labels,
        "audit": run_audit(df, missing_value),
    }
    write_json(d / "meta.json", meta)
    return meta


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


@app.post("/api/analyses")
def create_analysis(req: AnalysisRequest):
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

    meta = {
        "id": analysis_id,
        "dataset_id": req.dataset_id,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "running",
        "assumption_gates": {"violations": violations,
                             "overridden": bool(violations)},
    }
    write_json(a_dir / "meta.json", meta)

    try:
        run_engine(a_dir / "request.json", a_dir / "results.json")
        meta["status"] = "completed"
    except EngineError as exc:
        meta["status"] = "failed"
        meta["error"] = {"stage": exc.stage, "message": exc.message}
        write_json(a_dir / "meta.json", meta)
        raise HTTPException(422, meta["error"])
    except Exception as exc:  # crash path — keep the record, surface a 500
        meta["status"] = "failed"
        meta["error"] = {"stage": "internal", "message": str(exc)}
        write_json(a_dir / "meta.json", meta)
        raise HTTPException(500, meta["error"])

    write_json(a_dir / "meta.json", meta)
    return {**meta, "results_url": f"/api/analyses/{analysis_id}/results"}


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    a_dir = analysis_dir(analysis_id)
    if not (a_dir / "meta.json").exists():
        raise HTTPException(404, "analysis not found")
    return read_json(a_dir / "meta.json")


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


@app.post("/api/analyses/{analysis_id}/mga")
def create_mga(analysis_id: str, req: MGARequest):
    """Multi-group analysis: MICOM (Henseler et al. 2016) gates the permutation
    test on path differences (Chin & Dibbern 2010) — no invariance, no comparison."""
    a_dir, _, request, _ = _load_analysis(analysis_id)
    engine_request = {
        **request,
        "group": {"variable": req.group_variable,
                  "value_a": req.value_a, "value_b": req.value_b},
        "options": {"npermutations": req.npermutations, "seed": req.seed},
    }
    write_json(a_dir / "mga_request.json", engine_request)
    try:
        mga = run_engine(a_dir / "mga_request.json", a_dir / "mga.json", script=MGA_SCRIPT)
    except EngineError as exc:
        raise HTTPException(422, {"stage": exc.stage, "message": exc.message})
    return assess_mga(mga)


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


@app.get("/api/analyses/{analysis_id}/summary.pptx")
def get_pptx(analysis_id: str):
    """Compact findings deck (hypotheses, mediation, IPMA, MGA)."""
    a_dir, meta, request, results = _load_analysis(analysis_id)
    dataset_meta = read_json(dataset_dir(meta["dataset_id"]) / "meta.json")
    out = a_dir / "summary.pptx"
    write_pptx(out, dataset_meta, request, results, assess(results, request),
               mga=_mga_if_any(a_dir))
    return FileResponse(out, filename=f"plsem_summary_{analysis_id}.pptx",
                        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


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
