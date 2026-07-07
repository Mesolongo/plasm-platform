"""Phase 3 collaboration tests: share links (view / comment scopes) and comments.

Sharing is token-based and read-only; a 'comment'-scoped link additionally lets a
recipient post to the thread. A 'view' link must not. Revoking a link kills it.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.storage import ROOT

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"
client = TestClient(app)


@pytest.fixture(scope="module")
def analysis_id():
    with CSV.open("rb") as f:
        ds = client.post("/api/datasets", files={"file": ("corp_rep.csv", f, "text/csv")},
                         data={"missing_value": "-99"}).json()
    spec = json.loads((ROOT / "ai" / "fixtures" / "model_spec_reference.json").read_text())
    resp = client.post("/api/analyses", json={
        "dataset_id": ds["id"], "nboot": 100, "prediction": False,
        "constructs": [{"name": c["name"], "indicators": c.get("indicators", []),
                        "measurement": c["measurement"]} for c in spec["constructs"]],
        "paths": [{"from_construct": p["from_construct"], "to_construct": p["to_construct"]}
                  for p in spec["paths"]],
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_view_share_resolves_readonly_bundle(analysis_id):
    resp = client.post(f"/api/analyses/{analysis_id}/shares",
                       json={"scope": "view", "label": "committee"})
    assert resp.status_code == 200, resp.text
    share = resp.json()
    assert share["scope"] == "view"
    assert share["url"].endswith(share["token"])

    view = client.get(f"/api/shared/{share['token']}")
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["scope"] == "view"
    assert body["label"] == "committee"
    assert body["assessment"]["summary"]["hypotheses_total"] > 0
    assert body["dataset"]["n_observations"] > 0
    assert body["model"]["paths"]


def test_view_link_cannot_comment(analysis_id):
    share = client.post(f"/api/analyses/{analysis_id}/shares",
                        json={"scope": "view"}).json()
    resp = client.post(f"/api/shared/{share['token']}/comments",
                       json={"author": "Reviewer 2", "body": "Nice R²."})
    assert resp.status_code == 403
    assert "view-only" in resp.json()["detail"]


def test_comment_link_roundtrip(analysis_id):
    share = client.post(f"/api/analyses/{analysis_id}/shares",
                        json={"scope": "comment"}).json()
    post = client.post(f"/api/shared/{share['token']}/comments",
                       json={"author": "Dr. Nguyen", "body": "Check HTMT for BRAND."})
    assert post.status_code == 200, post.text
    assert post.json()["author"] == "Dr. Nguyen"

    # The comment shows up both through the share link and to the owner.
    via_link = client.get(f"/api/shared/{share['token']}").json()["comments"]
    via_owner = client.get(f"/api/analyses/{analysis_id}/comments").json()["comments"]
    assert any(c["body"] == "Check HTMT for BRAND." for c in via_link)
    assert any(c["author"] == "Dr. Nguyen" for c in via_owner)


def test_empty_comment_rejected(analysis_id):
    share = client.post(f"/api/analyses/{analysis_id}/shares",
                        json={"scope": "comment"}).json()
    resp = client.post(f"/api/shared/{share['token']}/comments",
                       json={"author": "x", "body": "   "})
    assert resp.status_code == 422


def test_revoke_kills_the_link(analysis_id):
    share = client.post(f"/api/analyses/{analysis_id}/shares",
                        json={"scope": "view"}).json()
    token = share["token"]
    assert client.get(f"/api/shared/{token}").status_code == 200

    revoke = client.delete(f"/api/analyses/{analysis_id}/shares/{token}")
    assert revoke.status_code == 200
    assert client.get(f"/api/shared/{token}").status_code == 404
    # It is gone from the owner's list, too.
    tokens = [s["token"] for s in
              client.get(f"/api/analyses/{analysis_id}/shares").json()["shares"]]
    assert token not in tokens


def test_unknown_token_404():
    assert client.get("/api/shared/does-not-exist").status_code == 404
    assert client.post("/api/shared/does-not-exist/comments",
                       json={"body": "hi"}).status_code == 404


def test_share_requires_existing_analysis():
    assert client.post("/api/analyses/an_missing/shares",
                       json={"scope": "view"}).status_code == 404
