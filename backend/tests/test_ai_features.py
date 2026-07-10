"""AI features without live Claude calls: the response unwrapper (_structured —
the site of a past NoneType crash), the endpoint wiring for propose-model /
interpretation / chat with the ai functions stubbed, persistence of the
interpretation into the Word report, AI failures surfacing as 502, and the
per-user daily quota middleware answering 429."""
import io
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import ai
from backend.app.main import app

from .helpers import create_analysis

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"
client = TestClient(app)

SPEC = {
    "constructs": [
        {"name": "CUSA", "indicators": ["cusa"], "measurement": "single_item"},
        {"name": "CUSL", "indicators": ["cusl_1", "cusl_2", "cusl_3"],
         "measurement": "reflective"},
    ],
    "paths": [{"from_construct": "CUSA", "to_construct": "CUSL"}],
    "nboot": 100, "prediction": False,
}

INTERPRETATION = {
    "results_narrative": "Customer satisfaction strongly predicts loyalty.",
    "discussion": "The effect is robust across resamples.",
    "conclusion": "Satisfaction is the primary lever for loyalty.",
    "managerial_implications": "Invest in satisfaction programmes.",
    "limitations": "Cross-sectional data limit causal claims.",
}


@pytest.fixture(scope="module")
def analysis():
    with CSV.open("rb") as f:
        ds = client.post("/api/datasets",
                         files={"file": ("corp_rep.csv", f, "text/csv")},
                         data={"missing_value": "-99"}).json()
    return {"dataset": ds, **create_analysis(client, {"dataset_id": ds["id"], **SPEC})}


# ------------------------- _structured unwrapping --------------------------- #

class _FakeParsed:
    def __init__(self, obj):
        self._obj = obj

    def model_dump_json(self):
        return json.dumps(self._obj)


class _FakeResponse:
    def __init__(self, parsed=None, stop_reason="end_turn"):
        self.parsed_output = _FakeParsed(parsed) if parsed is not None else None
        self.stop_reason = stop_reason


def test_structured_unwraps_a_parsed_reply():
    assert ai._structured(_FakeResponse({"a": 1, "b": ["x"]})) == {"a": 1, "b": ["x"]}


def test_structured_explains_a_max_tokens_cutoff():
    # regression: parsed_output=None used to crash with AttributeError
    with pytest.raises(RuntimeError, match="cut off"):
        ai._structured(_FakeResponse(stop_reason="max_tokens"))


def test_structured_explains_a_refusal():
    with pytest.raises(RuntimeError, match="declined"):
        ai._structured(_FakeResponse(stop_reason="refusal"))


def test_structured_reports_unknown_stop_reasons():
    with pytest.raises(RuntimeError, match="end_turn"):
        ai._structured(_FakeResponse(stop_reason="end_turn"))


# ------------------------- endpoint wiring (stubbed) ------------------------ #

def _configured(monkeypatch):
    monkeypatch.setattr(ai, "is_configured", lambda: True)


def test_propose_model_endpoint(analysis, monkeypatch):
    _configured(monkeypatch)
    proposal = {"summary": "one-path model", "constructs": SPEC["constructs"],
                "paths": SPEC["paths"], "interactions": []}
    seen = {}

    def fake_propose(variables, study_description):
        seen["variables"] = variables
        seen["study_description"] = study_description
        return proposal

    monkeypatch.setattr(ai, "propose_model", fake_propose)
    resp = client.post(f"/api/datasets/{analysis['dataset']['id']}/propose-model",
                       json={"study_description": "loyalty study"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == proposal
    # the architect saw this dataset's variable dictionary and the description
    assert {v["name"] for v in seen["variables"]} \
        == {v["name"] for v in analysis["dataset"]["variables"]}
    assert seen["study_description"] == "loyalty study"


def test_interpretation_persists_and_lands_in_the_report(analysis, monkeypatch):
    _configured(monkeypatch)
    # nothing generated yet -> 404
    assert client.get(f"/api/analyses/{analysis['id']}/interpretation").status_code == 404

    monkeypatch.setattr(ai, "interpret", lambda req, assessment, desc: INTERPRETATION)
    resp = client.post(f"/api/analyses/{analysis['id']}/interpretation",
                       json={"study_description": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json() == INTERPRETATION

    # persisted: a plain GET (and a fresh session) sees the same sections
    assert client.get(f"/api/analyses/{analysis['id']}/interpretation").json() \
        == INTERPRETATION

    # and the Word report picks the narrative up
    report = client.get(f"/api/analyses/{analysis['id']}/report.docx")
    assert report.status_code == 200
    from docx import Document
    text = "\n".join(p.text for p in Document(io.BytesIO(report.content)).paragraphs)
    assert INTERPRETATION["results_narrative"] in text


def test_chat_endpoint_passes_history_and_grounding(analysis, monkeypatch):
    _configured(monkeypatch)

    def fake_chat(request, assessment, history, message, mga=None):
        assert {c["name"] for c in request["constructs"]} == {"CUSA", "CUSL"}
        assert assessment["hypotheses"]  # grounded in the computed assessment
        return f"echo:{message} (history={len(history)})"

    monkeypatch.setattr(ai, "chat", fake_chat)
    resp = client.post(f"/api/analyses/{analysis['id']}/chat", json={
        "message": "Is H1 supported?",
        "history": [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "tool", "content": "dropped"}],  # non-chat roles filtered
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["reply"] == "echo:Is H1 supported? (history=2)"


def test_ai_failure_surfaces_as_502(analysis, monkeypatch):
    _configured(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("the model declined to generate this content")

    monkeypatch.setattr(ai, "interpret", boom)
    resp = client.post(f"/api/analyses/{analysis['id']}/interpretation",
                       json={"study_description": ""})
    assert resp.status_code == 502
    assert "declined" in resp.json()["detail"]


def test_daily_quota_middleware_answers_429(analysis, monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("PLSEM_AI_DAILY_LIMIT", "1")
    monkeypatch.setattr(ai, "chat", lambda *a, **kw: "ok")

    # a fresh account, so this test's quota can't leak into other tests
    fresh = TestClient(app)
    fresh.cookies.clear()
    slug = uuid.uuid4().hex[:8]
    fresh.post("/api/auth/register", json={
        "username": f"quota-{slug}", "password": "s3cret-pw",
        "email": f"quota-{slug}@example.org"})

    body = {"message": "hello", "history": []}
    first = fresh.post(f"/api/analyses/{analysis['id']}/chat", json=body)
    assert first.status_code == 200, first.text
    second = fresh.post(f"/api/analyses/{analysis['id']}/chat", json=body)
    assert second.status_code == 429
    assert "daily AI limit" in second.json()["detail"]

    # non-AI routes keep working after the quota is exhausted
    assert fresh.get(f"/api/analyses/{analysis['id']}/assessment").status_code == 200
