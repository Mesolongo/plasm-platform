"""Phase 4 publishing-assistant tests.

The reviewer-anticipation checks are deterministic and run offline: we exercise the
trigger logic on hand-built assessments (so each concern's firing condition is pinned
down) and end-to-end against the real corp-rep analysis. The AI manuscript drafter is
only checked for its credential gate — the live Claude call is not made in tests.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import publish
from backend.app.main import app
from backend.app.storage import ROOT

from .helpers import create_analysis

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"
client = TestClient(app)


# --------------------------------------------------------------------------- #
# Unit tests for the deterministic reviewer_checks trigger logic
# --------------------------------------------------------------------------- #

def _req():
    return {"constructs": [{"name": "A", "measurement": "reflective"}],
            "paths": [{"from_construct": "A", "to_construct": "Y"}]}


def _areas(concerns):
    return {c["area"] for c in concerns}


def test_clean_model_still_raises_only_standard_concerns():
    # An assessment with nothing wrong: no evidence-driven concern fires, but the
    # standing concerns (CMB, endogeneity, heterogeneity) always do.
    assessment = {
        "measurement_model": [
            {"family": "discriminant_validity", "construct": "A / B", "metric": "HTMT",
             "value": 0.62, "verdict": "pass"},
            {"family": "convergent_validity", "construct": "A", "metric": "AVE",
             "value": 0.71, "verdict": "pass"},
        ],
        "structural_model": [
            {"family": "collinearity", "construct": "A -> Y", "metric": "inner VIF",
             "value": 1.8, "verdict": "pass"},
            {"family": "explanatory_power", "construct": "Y", "metric": "R^2",
             "value": 0.62, "verdict": "moderate"},
            {"family": "predictive_relevance", "construct": "y1", "metric": "Q^2_predict",
             "value": 0.2, "verdict": "pass"},
        ],
        "hypotheses": [{"hypothesis": "H1", "path": "A -> Y", "type": "direct",
                        "verdict": "supported"}],
    }
    concerns = publish.reviewer_checks(assessment, _req())
    areas = _areas(concerns)
    assert "common_method_bias" in areas
    assert "endogeneity" in areas
    assert "unobserved_heterogeneity" in areas
    # No evidence-driven concern fired.
    assert not ({"discriminant_validity", "convergent_validity", "collinearity",
                 "explanatory_power", "unsupported_hypotheses",
                 "predictive_validity"} & areas)


def test_marginal_discriminant_validity_is_flagged():
    assessment = {
        "measurement_model": [{"family": "discriminant_validity", "construct": "A / B",
                               "metric": "HTMT", "value": 0.88, "verdict": "review"}],
        "structural_model": [{"family": "predictive_relevance", "construct": "y1",
                              "metric": "Q^2_predict", "value": 0.1, "verdict": "pass"}],
        "hypotheses": [],
    }
    dv = [c for c in publish.reviewer_checks(assessment, _req())
          if c["area"] == "discriminant_validity"]
    assert len(dv) == 1
    assert dv[0]["severity"] == "medium"
    assert "0.88" in dv[0]["evidence"]
    assert dv[0]["citation"] == "Henseler et al. (2015)"


def test_failed_ave_is_high_severity():
    assessment = {
        "measurement_model": [{"family": "convergent_validity", "construct": "A",
                               "metric": "AVE", "value": 0.42, "verdict": "fail"}],
        "structural_model": [{"family": "predictive_power", "construct": "x",
                              "metric": "m", "value": "1/1", "verdict": "high"}],
        "hypotheses": [],
    }
    cv = [c for c in publish.reviewer_checks(assessment, _req())
          if c["area"] == "convergent_validity"]
    assert cv and cv[0]["severity"] == "high"


def test_missing_plspredict_is_flagged_and_present_suppresses_it():
    base = {"measurement_model": [], "hypotheses": []}
    without = {**base, "structural_model": [{"family": "explanatory_power",
               "construct": "Y", "metric": "R^2", "value": 0.6, "verdict": "moderate"}]}
    assert "predictive_validity" in _areas(publish.reviewer_checks(without, _req()))

    with_pred = {**base, "structural_model": [{"family": "predictive_relevance",
                 "construct": "y1", "metric": "Q^2_predict", "value": 0.3, "verdict": "pass"}]}
    assert "predictive_validity" not in _areas(publish.reviewer_checks(with_pred, _req()))


def test_majority_unsupported_hypotheses_is_high_severity():
    assessment = {
        "measurement_model": [], "structural_model": [
            {"family": "predictive_power", "construct": "x", "metric": "m",
             "value": "1/1", "verdict": "high"}],
        "hypotheses": [
            {"hypothesis": "H1", "path": "A -> Y", "type": "direct", "verdict": "not supported"},
            {"hypothesis": "H2", "path": "B -> Y", "type": "direct", "verdict": "not supported"},
            {"hypothesis": "H3", "path": "C -> Y", "type": "direct", "verdict": "supported"},
        ],
    }
    uh = [c for c in publish.reviewer_checks(assessment, _req())
          if c["area"] == "unsupported_hypotheses"]
    assert uh and uh[0]["severity"] == "high"
    assert "2 of 3" in uh[0]["evidence"]


def test_overridden_gates_surface_as_high_concern():
    assessment = {"measurement_model": [], "structural_model": [
        {"family": "predictive_power", "construct": "x", "metric": "m",
         "value": "1/1", "verdict": "high"}], "hypotheses": []}
    meta = {"assumption_gates": {"overridden": True, "violations": [
        {"gate": "sample_size_10x", "detail": "n = 40 is below 10 x 8"}]}}
    concerns = publish.reviewer_checks(assessment, _req(), analysis_meta=meta)
    assumptions = [c for c in concerns if c["area"] == "assumptions"]
    assert assumptions and assumptions[0]["severity"] == "high"
    assert "sample_size_10x" in assumptions[0]["evidence"] or "n = 40" in assumptions[0]["evidence"]


def test_mga_present_suppresses_heterogeneity_concern():
    assessment = {"measurement_model": [], "structural_model": [
        {"family": "predictive_power", "construct": "x", "metric": "m",
         "value": "1/1", "verdict": "high"}], "hypotheses": []}
    with_mga = publish.reviewer_checks(assessment, _req(), mga={"summary": {}})
    assert "unobserved_heterogeneity" not in _areas(with_mga)


def test_concerns_sorted_by_severity():
    assessment = {
        "measurement_model": [{"family": "convergent_validity", "construct": "A",
                               "metric": "AVE", "value": 0.4, "verdict": "fail"}],
        "structural_model": [], "hypotheses": [],
    }
    ranks = [publish._SEVERITY_RANK[c["severity"]]
             for c in publish.reviewer_checks(assessment, _req())]
    assert ranks == sorted(ranks)


def test_summarize_counts_by_severity():
    concerns = [{"severity": "high"}, {"severity": "high"}, {"severity": "medium"},
                {"severity": "info"}]
    assert publish.summarize(concerns) == {"high": 2, "medium": 1, "low": 0,
                                           "info": 1, "total": 4}


# --------------------------------------------------------------------------- #
# End-to-end against the real corp-rep analysis
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def analysis_id():
    with CSV.open("rb") as f:
        ds = client.post("/api/datasets", files={"file": ("corp_rep.csv", f, "text/csv")},
                         data={"missing_value": "-99"}).json()
    spec = json.loads((ROOT / "ai" / "fixtures" / "model_spec_reference.json").read_text())
    return create_analysis(client, {
        "dataset_id": ds["id"], "nboot": 100, "prediction": False,
        "constructs": [{"name": c["name"], "indicators": c.get("indicators", []),
                        "measurement": c["measurement"]} for c in spec["constructs"]],
        "paths": [{"from_construct": p["from_construct"], "to_construct": p["to_construct"]}
                  for p in spec["paths"]],
    })["id"]


def test_reviewer_check_endpoint(analysis_id):
    resp = client.get(f"/api/analyses/{analysis_id}/reviewer-check")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["total"] == len(body["concerns"])
    # prediction was disabled, so PLSpredict should be flagged as missing.
    areas = {c["area"] for c in body["concerns"]}
    assert "predictive_validity" in areas
    assert "common_method_bias" in areas
    # every concern is well-formed
    for c in body["concerns"]:
        assert {"area", "severity", "concern", "evidence", "recommendation",
                "citation"} <= c.keys()


def test_reviewer_check_missing_analysis_404():
    assert client.get("/api/analyses/an_nope/reviewer-check").status_code == 404


def test_manuscript_gated_on_credentials(analysis_id, monkeypatch):
    from backend.app import ai
    monkeypatch.setattr(ai, "is_configured", lambda: False)
    resp = client.post(f"/api/analyses/{analysis_id}/manuscript",
                       json={"mode": "journal", "target_journal": "JBR"})
    assert resp.status_code == 503
    assert "credentials" in resp.json()["detail"]


def test_get_manuscript_before_drafting_404(analysis_id):
    assert client.get(f"/api/analyses/{analysis_id}/manuscript").status_code == 404
