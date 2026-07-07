"""End-to-end slice test: upload the corp-rep CSV, run the AI-architect fixture spec
through the real R engine, and verify key statistics against the published SmartPLS
values (Phase 0 parity references). Uses 1000 bootstraps for speed — point estimates
don't depend on bootstrap count, so parity tolerances still hold; only t-values widen.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

ROOT = Path(__file__).resolve().parents[2]
CSV = Path(__file__).parent / "fixtures_corp_rep.csv"
SPEC = ROOT / "ai" / "fixtures" / "model_spec_reference.json"

client = TestClient(app)


@pytest.fixture(scope="module")
def dataset():
    with CSV.open("rb") as f:
        resp = client.post(
            "/api/datasets",
            files={"file": ("corp_rep.csv", f, "text/csv")},
            data={"missing_value": "-99"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_upload_builds_dictionary_and_audit(dataset):
    assert dataset["n_observations"] == 344
    names = {v["name"] for v in dataset["variables"]}
    assert {"comp_1", "like_3", "cusa", "qual_8"} <= names
    # corp_rep_data has -99 missing codes; the audit must surface them
    checks = {f["check"] for f in dataset["audit"]["findings"]}
    assert "missing_values" in checks


def test_rejects_bad_spec(dataset):
    resp = client.post("/api/analyses", json={
        "dataset_id": dataset["id"],
        "constructs": [{"name": "X", "indicators": ["no_such_col"], "measurement": "reflective"}],
        "paths": [],
        "nboot": 100,
    })
    assert resp.status_code == 422
    assert "no_such_col" in json.dumps(resp.json())


def test_full_analysis_matches_published_values(dataset):
    spec = json.loads(SPEC.read_text())
    resp = client.post("/api/analyses", json={
        "dataset_id": dataset["id"],
        "constructs": [
            {"name": c["name"], "indicators": c["indicators"], "measurement": c["measurement"]}
            for c in spec["constructs"]
        ],
        "paths": [
            {"from_construct": p["from_construct"], "to_construct": p["to_construct"]}
            for p in spec["paths"]
        ],
        "nboot": 1000,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"

    results = client.get(resp.json()["results_url"]).json()

    def cell(records, row, col):
        for rec in records:
            if rec["row"] == row:
                return rec[col]
        raise KeyError(row)

    # Published SmartPLS 4 values (Hair et al. primer, ch. 6) — rounding tolerance
    assert abs(cell(results["paths_and_r2"], "CUSA", "CUSL") - 0.505) < 0.005
    assert abs(cell(results["paths_and_r2"], "LIKE", "CUSL") - 0.344) < 0.005
    assert abs(cell(results["paths_and_r2"], "R^2", "CUSL") - 0.562) < 0.005
    assert abs(cell(results["reliability"], "COMP", "alpha") - 0.776) < 0.005
    assert abs(cell(results["total_indirect"], "LIKE", "CUSL") - 0.220) < 0.005
    assert results["meta"]["n"] == 344
