"""Phase 2 feature tests: moderation (two-stage interaction), higher-order
constructs, SRMR, PLSpredict, and SPSS .sav import.

The moderation model is the Hair et al. (2022) primer ch. 7 example
(CUSA x SC -> CUSL on the corporate reputation data), so the interaction
estimate can be checked against the published two-stage value (-0.071).
Point estimates don't depend on bootstrap count; small nboot keeps tests fast.
"""
import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

from .helpers import create_analysis

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"

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


MODERATION_SPEC = {
    "constructs": [
        {"name": "COMP", "indicators": ["comp_1", "comp_2", "comp_3"], "measurement": "reflective"},
        {"name": "LIKE", "indicators": ["like_1", "like_2", "like_3"], "measurement": "reflective"},
        {"name": "CUSA", "indicators": ["cusa"], "measurement": "single_item"},
        {"name": "SC", "indicators": ["switch_1", "switch_2", "switch_3", "switch_4"],
         "measurement": "reflective"},
        {"name": "CUSL", "indicators": ["cusl_1", "cusl_2", "cusl_3"], "measurement": "reflective"},
    ],
    "interactions": [{"iv": "CUSA", "moderator": "SC"}],
    "paths": [
        {"from_construct": "COMP", "to_construct": "CUSA"},
        {"from_construct": "LIKE", "to_construct": "CUSA"},
        {"from_construct": "COMP", "to_construct": "CUSL"},
        {"from_construct": "LIKE", "to_construct": "CUSL"},
        {"from_construct": "CUSA", "to_construct": "CUSL"},
        {"from_construct": "SC", "to_construct": "CUSL"},
        {"from_construct": "CUSA*SC", "to_construct": "CUSL"},
    ],
}


def _cell(records, row, col):
    for rec in records:
        if rec["row"] == row:
            return rec[col]
    raise KeyError(row)


def test_moderation_matches_published_values(dataset):
    analysis = create_analysis(client, {
        "dataset_id": dataset["id"], "nboot": 200, **MODERATION_SPEC,
    })
    results = client.get(analysis["results_url"]).json()

    # Two-stage interaction effect and R2 from the primer's moderation example
    assert abs(_cell(results["paths_and_r2"], "CUSA*SC", "CUSL") - (-0.071)) < 0.005
    assert abs(_cell(results["paths_and_r2"], "R^2", "CUSL") - 0.571) < 0.005
    assert results["meta"]["interactions"] == "CUSA*SC"

    # SRMR (saturated model) is reported and plausible
    assert 0.0 < results["srmr"] < 0.15

    # PLSpredict ran: every endogenous indicator has out-of-sample metrics
    pred = {r["row"]: r for r in results["prediction"]}
    assert {"cusa", "cusl_1", "cusl_2", "cusl_3"} <= set(pred)
    assert all(r["q2_predict"] is not None for r in pred.values())

    a = client.get(f"/api/analyses/{analysis['id']}/assessment").json()
    mod = {h["path"]: h for h in a["hypotheses"] if h["type"] == "moderation"}
    assert set(mod) == {"CUSA*SC -> CUSL"}

    fam = {}
    for item in a["structural_model"]:
        fam.setdefault(item["family"], []).append(item)
    # Interaction f2 uses Kenny's benchmarks (published value ~0.014 -> medium)
    (mod_f2,) = fam["moderation_effect_size"]
    assert abs(mod_f2["value"] - 0.014) < 0.005
    assert mod_f2["verdict"] == "medium"
    assert fam["model_fit"][0]["metric"].startswith("SRMR")
    assert all(q["verdict"] == "pass" for q in fam["predictive_relevance"])
    assert fam["predictive_power"][0]["verdict"] in {"high", "medium", "low", "none"}


def test_higher_order_construct(dataset):
    analysis = create_analysis(client, {
        "dataset_id": dataset["id"], "nboot": 200, "prediction": False,
        "constructs": [
            {"name": "QUAL", "indicators": [f"qual_{i}" for i in range(1, 9)],
             "measurement": "formative"},
            {"name": "PERF", "indicators": [f"perf_{i}" for i in range(1, 6)],
             "measurement": "formative"},
            {"name": "DRIVERS", "dimensions": ["QUAL", "PERF"],
             "measurement": "higher_order_formative"},
            {"name": "CUSA", "indicators": ["cusa"], "measurement": "single_item"},
            {"name": "CUSL", "indicators": ["cusl_1", "cusl_2", "cusl_3"],
             "measurement": "reflective"},
        ],
        "paths": [
            {"from_construct": "DRIVERS", "to_construct": "CUSA"},
            {"from_construct": "DRIVERS", "to_construct": "CUSL"},
            {"from_construct": "CUSA", "to_construct": "CUSL"},
        ],
    })
    results = client.get(analysis["results_url"]).json()
    assert results["meta"]["higher_order"] == "DRIVERS"
    assert not results.get("prediction")  # explicitly disabled
    assert 0.0 < results["srmr"] < 0.15
    assert _cell(results["paths_and_r2"], "DRIVERS", "CUSA") is not None

    a = client.get(f"/api/analyses/{analysis['id']}/assessment").json()
    # Dimension weights on the HOC are assessed like formative indicators
    fiv = [m for m in a["measurement_model"] if m["family"] == "formative_indicator_validity"]
    assert {"QUAL", "PERF"} <= {m["item"] for m in fiv if m["construct"] == "DRIVERS"}
    hyp = {h["path"]: h["verdict"] for h in a["hypotheses"]}
    assert hyp["DRIVERS -> CUSA"] == "supported"

    # The Word report renders with the new sections
    report = client.get(f"/api/analyses/{analysis['id']}/report.docx")
    assert report.status_code == 200
    from docx import Document
    doc = Document(io.BytesIO(report.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "two-stage" in text and "SRMR" in text


def test_spss_sav_upload(tmp_path):
    import pyreadstat
    df = pd.read_csv(CSV).iloc[:50, :5]
    sav = tmp_path / "survey.sav"
    pyreadstat.write_sav(df, str(sav), column_labels=[f"label {c}" for c in df.columns])
    with sav.open("rb") as f:
        resp = client.post("/api/datasets", files={"file": ("survey.sav", f, "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["n_observations"] == 50
    assert {v["name"] for v in meta["variables"]} == set(df.columns)
    assert meta["variable_labels"]["cusa"] == "label cusa"


def test_invalid_phase2_specs(dataset):
    base = {"dataset_id": dataset["id"], "nboot": 100}
    # Higher-order construct with a single dimension
    resp = client.post("/api/analyses", json={
        **base,
        "constructs": [
            {"name": "A", "indicators": ["comp_1", "comp_2"], "measurement": "reflective"},
            {"name": "H", "dimensions": ["A"], "measurement": "higher_order_reflective"},
        ],
        "paths": [{"from_construct": "A", "to_construct": "H"}],
    })
    assert resp.status_code == 422

    # Interaction referencing an unknown construct (caught by the engine, so the
    # queued job fails and the error lands on the analysis meta)
    meta = create_analysis(client, {
        **base,
        "constructs": [
            {"name": "A", "indicators": ["comp_1", "comp_2"], "measurement": "reflective"},
            {"name": "B", "indicators": ["like_1", "like_2"], "measurement": "reflective"},
        ],
        "interactions": [{"iv": "A", "moderator": "NOPE"}],
        "paths": [{"from_construct": "A", "to_construct": "B"}],
    }, expect="failed")
    assert "NOPE" in str(meta["error"])
