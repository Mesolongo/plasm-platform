"""Assessment, report, and gated-AI tests. Reuses one analysis run for speed."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

from .helpers import create_analysis

ROOT = Path(__file__).resolve().parents[2]
CSV = Path(__file__).parent / "fixtures_corp_rep.csv"
SPEC = ROOT / "ai" / "fixtures" / "model_spec_reference.json"

client = TestClient(app)


@pytest.fixture(scope="module")
def analysis():
    with CSV.open("rb") as f:
        ds = client.post("/api/datasets",
                         files={"file": ("corp_rep.csv", f, "text/csv")},
                         data={"missing_value": "-99"}).json()
    spec = json.loads(SPEC.read_text())
    return create_analysis(client, {
        "dataset_id": ds["id"],
        "constructs": [{"name": c["name"], "indicators": c["indicators"],
                        "measurement": c["measurement"]} for c in spec["constructs"]],
        "paths": [{"from_construct": p["from_construct"], "to_construct": p["to_construct"]}
                  for p in spec["paths"]],
        "nboot": 1000,
    })


def test_assessment_verdicts(analysis):
    a = client.get(f"/api/analyses/{analysis['id']}/assessment").json()

    # Reliability: all reflective constructs pass (known from parity values)
    rel = [m for m in a["measurement_model"] if m["family"] == "internal_consistency"]
    assert rel and all(m["verdict"] == "pass" for m in rel)
    assert {m["construct"] for m in rel} == {"COMP", "LIKE", "CUSL"}  # not CUSA (single item)

    # HTMT all below 0.85 in this dataset
    htmt = [m for m in a["measurement_model"] if m["family"] == "discriminant_validity"]
    assert htmt and all(m["verdict"] == "pass" for m in htmt)

    # Hypotheses: book result — 9 of 13 supported (PERF->LIKE, ATTR->COMP,
    # CSOR->COMP, COMP->CUSL are not significant)
    verdicts = {h["path"]: h["verdict"] for h in a["hypotheses"]}
    assert verdicts["CUSA -> CUSL"] == "supported"
    assert verdicts["COMP -> CUSL"] == "not supported"
    assert a["summary"]["hypotheses_total"] == 13
    assert a["summary"]["hypotheses_supported"] == 9

    # R2 labels
    r2 = {s["construct"]: s["verdict"] for s in a["structural_model"]
          if s["family"] == "explanatory_power"}
    assert r2["CUSL"] == "moderate" and r2["CUSA"] == "weak"


def test_report_downloads(analysis):
    resp = client.get(f"/api/analyses/{analysis['id']}/report.docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml")
    assert len(resp.content) > 10_000  # a real document, not an empty shell

    # And it opens as a valid docx with the expected sections
    import io
    from docx import Document
    doc = Document(io.BytesIO(resp.content))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert any("Measurement Model" in h for h in headings)
    assert any("Structural Model" in h for h in headings)


def test_ai_gate(analysis, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("backend.app.ai.is_configured", lambda: False)
    resp = client.post(f"/api/datasets/{analysis['dataset_id']}/propose-model",
                       json={"study_description": "x"})
    assert resp.status_code == 503
    assert "API" in resp.json()["detail"]


def test_frontend_served():
    resp = client.get("/app/")
    assert resp.status_code == 200
    assert "plsem-platform" in resp.text
