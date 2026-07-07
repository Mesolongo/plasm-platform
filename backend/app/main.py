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
import tempfile
from pathlib import Path
from typing import List, Literal, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai
from .assess import assess
from .audit import run_audit, variable_dictionary
from .engine import EngineError, run_engine
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

    def validate_spec(self):
        for c in self.constructs:
            hoc = c.measurement.startswith("higher_order")
            if hoc and (len(c.dimensions) < 2 or c.indicators):
                raise HTTPException(422, f"higher-order construct {c.name} needs >= 2 "
                                         "dimensions and no direct indicators")
            if not hoc and not c.indicators:
                raise HTTPException(422, f"construct {c.name} has no indicators")


@app.post("/api/analyses")
def create_analysis(req: AnalysisRequest):
    ds_dir = dataset_dir(req.dataset_id)
    if not (ds_dir / "meta.json").exists():
        raise HTTPException(404, "dataset not found")
    ds_meta = read_json(ds_dir / "meta.json")

    analysis_id = new_id("an")
    a_dir = analysis_dir(analysis_id, create=True)

    req.validate_spec()
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


@app.get("/api/analyses/{analysis_id}/report.docx")
def get_report(analysis_id: str):
    a_dir, meta, request, results = _load_analysis(analysis_id)
    dataset_meta = read_json(dataset_dir(meta["dataset_id"]) / "meta.json")
    interp_path = a_dir / "interpretation.json"
    interpretation = read_json(interp_path) if interp_path.exists() else None
    doc = build_report(dataset_meta, request, results, assess(results, request),
                       interpretation=interpretation)
    out = a_dir / "report.docx"
    doc.save(out)
    return FileResponse(out, filename=f"plsem_report_{analysis_id}.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


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
