"""Phase 3 tests: multi-group analysis (permutation MGA) gated on MICOM invariance.

servicetype on the corp-rep data is the published MICOM example (Hair et al.,
Advanced Issues; Henseler et al. 2016): both customer groups reach compositional
invariance, so the group comparison is permissible. Small permutation counts keep
tests fast; observed statistics (c values, path estimates) don't depend on them.
"""
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.assess import assess_mga
from backend.app.main import app

from .helpers import create_analysis, wait_job

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"

client = TestClient(app)

SIMPLE_SPEC = {
    "constructs": [
        {"name": "COMP", "indicators": ["comp_1", "comp_2", "comp_3"], "measurement": "reflective"},
        {"name": "LIKE", "indicators": ["like_1", "like_2", "like_3"], "measurement": "reflective"},
        {"name": "CUSA", "indicators": ["cusa"], "measurement": "single_item"},
        {"name": "CUSL", "indicators": ["cusl_1", "cusl_2", "cusl_3"], "measurement": "reflective"},
    ],
    "paths": [
        {"from_construct": "COMP", "to_construct": "CUSA"},
        {"from_construct": "LIKE", "to_construct": "CUSA"},
        {"from_construct": "COMP", "to_construct": "CUSL"},
        {"from_construct": "LIKE", "to_construct": "CUSL"},
        {"from_construct": "CUSA", "to_construct": "CUSL"},
    ],
}


@pytest.fixture(scope="module")
def analysis():
    with CSV.open("rb") as f:
        resp = client.post(
            "/api/datasets",
            files={"file": ("corp_rep.csv", f, "text/csv")},
            data={"missing_value": "-99"},
        )
    assert resp.status_code == 200, resp.text
    dataset = resp.json()
    # Candidate grouping variables ship value counts in the dictionary
    servicetype = next(v for v in dataset["variables"] if v["name"] == "servicetype")
    assert servicetype["values"] == {"1": 125, "2": 219}

    return create_analysis(client, {
        "dataset_id": dataset["id"], "nboot": 100, "prediction": False, **SIMPLE_SPEC,
    })["id"]


def test_mga_micom_and_paths(analysis):
    resp = client.post(f"/api/analyses/{analysis}/mga", json={
        "group_variable": "servicetype", "value_a": "1", "value_b": "2",
        "npermutations": 100,
    })
    assert resp.status_code == 202, resp.text
    job = wait_job(client, resp.json()["id"])
    assert job["status"] == "completed", job["error"]
    mga = client.get(f"/api/analyses/{analysis}/mga").json()

    assert mga["meta"]["n_a"] == 125 and mga["meta"]["n_b"] == 219
    # Published result: compositional invariance holds for every construct
    assert all(s["verdict"] == "pass" for s in mga["micom"]["step2"])
    assert mga["micom"]["invariance"] in ("full", "partial")
    assert mga["micom"]["comparison_permissible"] is True

    paths = {p["path"]: p for p in mga["paths"]}
    assert set(paths) == {f"{p['from_construct']} -> {p['to_construct']}"
                          for p in SIMPLE_SPEC["paths"]}
    for p in paths.values():
        assert 0 < p["p_value"] <= 1
        assert p["verdict"] in ("different", "not different")
        # values are rounded to 3 dp independently of their difference
        assert p["difference"] == pytest.approx(p["estimate_a"] - p["estimate_b"], abs=0.002)

    # GET returns the stored run; the report gains the MGA section
    assert client.get(f"/api/analyses/{analysis}/mga").json()["meta"]["n_a"] == 125
    report = client.get(f"/api/analyses/{analysis}/report.docx")
    from docx import Document
    text = "\n".join(p.text for p in Document(io.BytesIO(report.content)).paragraphs)
    assert "Multi-Group Analysis" in text and "MICOM" in text


def test_mga_gating_withholds_paths_without_invariance():
    """The invariance gate is pure assessment logic — exercise it directly."""
    mga = {
        "meta": {"n_a": 50, "n_b": 60},
        "micom_step1": "configural",
        "micom_step2": [
            {"row": "A", "c_value": 0.99, "c_quantile_5": 0.95, "invariant": True},
            {"row": "B", "c_value": 0.80, "c_quantile_5": 0.95, "invariant": False},
        ],
        "micom_step3": [],
        "paths": [{"row": "A -> B", "est_a": 0.5, "est_b": 0.1, "diff": 0.4, "p_value": 0.01}],
    }
    out = assess_mga(mga)
    assert out["micom"]["invariance"] == "none"
    assert out["micom"]["comparison_permissible"] is False
    assert "B" in out["micom"]["note"]
    assert out["paths"][0]["verdict"] == "withheld"


def test_mga_invalid_requests(analysis):
    def failed_job(body):
        resp = client.post(f"/api/analyses/{analysis}/mga", json=body)
        assert resp.status_code == 202, resp.text
        job = wait_job(client, resp.json()["id"])
        assert job["status"] == "failed", job
        return job

    # Unknown grouping variable
    job = failed_job({"group_variable": "nope", "value_a": "1", "value_b": "2",
                      "npermutations": 100})
    assert "nope" in str(job["error"])
    # Group value with no observations
    failed_job({"group_variable": "servicetype", "value_a": "1", "value_b": "9",
                "npermutations": 100})
    # Same value twice
    failed_job({"group_variable": "servicetype", "value_a": "1", "value_b": "1",
                "npermutations": 100})
