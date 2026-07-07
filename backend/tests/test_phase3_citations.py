"""Phase 3 literature-citation tests.

Crossref is stubbed so the tests are deterministic and offline. We assert that each
direct hypothesis gets candidate references keyed off its construct names, that a
moderation term is skipped, and that a failed lookup degrades gracefully.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import citations as citations_module
from backend.app.main import app
from backend.app.storage import ROOT

from .helpers import create_analysis, wait_job

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"
client = TestClient(app)


def _crossref_payload(title="Antecedents of customer loyalty in retail banking"):
    return {"message": {"items": [{
        "DOI": "10.1000/abc123",
        "title": [title],
        "author": [{"family": "Sarstedt"}, {"family": "Ringle"}, {"family": "Hair"},
                   {"family": "Becker"}],
        "issued": {"date-parts": [[2019]]},
        "container-title": ["Journal of Business Research"],
        "is-referenced-by-count": 42,
    }]}}


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


class _FakeClient:
    """Stand-in for httpx.Client, capturing the Crossref request params."""
    def __init__(self, payload):
        self._payload = payload
        self.params = None

    def get(self, url, params=None, **kw):
        self.params = params

        class _R:
            def raise_for_status(self_):
                pass

            def json(self_):
                return self._payload
        return _R()


def test_search_crossref_builds_request():
    fake = _FakeClient(_crossref_payload())
    refs = citations_module.search_crossref("brand loyalty", rows=5, client=fake)
    assert fake.params["query.bibliographic"] == "brand loyalty"
    assert fake.params["rows"] == 5
    assert refs[0]["doi"] == "10.1000/abc123"


def test_reference_parsing():
    ref = citations_module._parse_work(_crossref_payload()["message"]["items"][0])
    assert ref["authors"] == "Sarstedt, Ringle, Hair et al."  # 4 authors -> et al.
    assert ref["year"] == 2019
    assert ref["doi"] == "10.1000/abc123"
    assert ref["url"] == "https://doi.org/10.1000/abc123"
    assert ref["cited_by"] == 42


def test_citations_endpoint_grounds_each_direct_hypothesis(analysis_id, monkeypatch):
    # Patch at the search seam, not httpx — the TestClient itself runs on httpx.
    calls = []
    one_ref = citations_module._parse_work(_crossref_payload()["message"]["items"][0])

    def fake_search(query, rows=3, client=None):
        calls.append(query)
        return [one_ref]

    monkeypatch.setattr(citations_module, "search_crossref", fake_search)

    # The lookup is queued; the patch holds while we wait because the worker
    # thread runs in this same process.
    resp = client.post(f"/api/analyses/{analysis_id}/citations")
    assert resp.status_code == 202, resp.text
    job = wait_job(client, resp.json()["id"])
    assert job["status"] == "completed", job["error"]
    body = client.get(f"/api/analyses/{analysis_id}/citations").json()
    assert body["source"] == "Crossref"
    assert body["hypotheses"], "expected at least one hypothesis"

    for h in body["hypotheses"]:
        assert h["references"], f"no references for {h['path']}"
        assert h["references"][0]["doi"] == "10.1000/abc123"
        # The query is built from the two construct names of the path.
        from_c, to_c = [p.strip() for p in h["path"].split("->")]
        assert from_c in h["query"] and to_c in h["query"]

    # No moderation hypotheses in this model, so every hypothesis was searched.
    assert len(calls) == len(body["hypotheses"])

    # Persisted and retrievable.
    got = client.get(f"/api/analyses/{analysis_id}/citations")
    assert got.status_code == 200
    assert got.json()["hypotheses"][0]["path"] == body["hypotheses"][0]["path"]


def test_citations_skip_moderation_terms(monkeypatch):
    calls = []
    monkeypatch.setattr(citations_module, "search_crossref",
                        lambda query, rows=3, client=None: calls.append(query) or [])
    hyps = [
        {"hypothesis": "H1", "path": "A -> B", "type": "direct"},
        {"hypothesis": "H2", "path": "A*M -> B", "type": "moderation"},
    ]
    out = citations_module.suggest_for_hypotheses(hyps)
    assert [h["path"] for h in out] == ["A -> B"]
    assert calls == ["A B structural equation modeling"]


def test_citations_lookup_failure_is_isolated(monkeypatch):
    def boom(query, rows=3, client=None):
        raise RuntimeError("crossref down")

    monkeypatch.setattr(citations_module, "search_crossref", boom)
    out = citations_module.suggest_for_hypotheses(
        [{"hypothesis": "H1", "path": "A -> B", "type": "direct"}])
    assert out[0]["references"] == []
    assert "crossref down" in out[0]["error"]


def test_get_citations_before_generation_404(analysis_id):
    # A fresh analysis with no citations run yet.
    with CSV.open("rb") as f:
        ds = client.post("/api/datasets", files={"file": ("c.csv", f, "text/csv")},
                         data={"missing_value": "-99"}).json()
    assert client.get(f"/api/analyses/an_never/citations").status_code == 404
