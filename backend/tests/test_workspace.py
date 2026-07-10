"""Workspace: after logging out and back in, a user finds their datasets,
analyses, and in-progress model builder draft exactly where they left them.
Listings are owner-scoped; the draft is per-user and survives sessions."""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import app

from .helpers import create_analysis

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"

SPEC = {
    "constructs": [
        {"name": "CUSA", "indicators": ["cusa"], "measurement": "single_item"},
        {"name": "CUSL", "indicators": ["cusl_1", "cusl_2", "cusl_3"],
         "measurement": "reflective"},
    ],
    "paths": [{"from_construct": "CUSA", "to_construct": "CUSL"}],
    "nboot": 100, "prediction": False,
}

DRAFT = {
    "dataset_id": "ds_abc123", "analysis_id": None, "step": 2,
    "constructs": SPEC["constructs"], "paths": SPEC["paths"], "interactions": [],
}


def fresh_user():
    """A logged-in client for a brand-new account (conftest's shared cookie is
    replaced by this user's own session), plus the credentials."""
    client = TestClient(app)
    client.cookies.clear()
    slug = uuid.uuid4().hex[:8]
    creds = {"username": f"ws-{slug}", "password": "s3cret-pw",
             "email": f"ws-{slug}@example.org"}
    assert client.post("/api/auth/register", json=creds).status_code == 201
    return client, creds


def upload_dataset(client):
    with CSV.open("rb") as f:
        resp = client.post("/api/datasets",
                           files={"file": ("corp_rep.csv", f, "text/csv")},
                           data={"missing_value": "-99"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_workspace_requires_login():
    anon = TestClient(app)
    anon.cookies.clear()
    assert anon.get("/api/workspace").status_code == 401
    assert anon.put("/api/workspace/draft", json=DRAFT).status_code == 401
    assert anon.delete("/api/workspace/draft").status_code == 401


def test_fresh_account_has_an_empty_workspace():
    client, _ = fresh_user()
    ws = client.get("/api/workspace").json()
    assert ws["datasets"] == [] and ws["analyses"] == [] and ws["draft"] is None


def test_datasets_are_listed_for_their_owner_only():
    owner, _ = fresh_user()
    other, _ = fresh_user()
    ds = upload_dataset(owner)

    listed = owner.get("/api/workspace").json()["datasets"]
    assert [d["id"] for d in listed] == [ds["id"]]
    assert listed[0]["filename"] == "corp_rep.csv"
    assert listed[0]["n_observations"] == 344
    assert listed[0]["n_variables"] == len(ds["variables"])

    assert other.get("/api/workspace").json()["datasets"] == []


def test_completed_analysis_appears_with_status_and_artifacts():
    client, _ = fresh_user()
    ds = upload_dataset(client)
    analysis = create_analysis(client, {"dataset_id": ds["id"], **SPEC})

    listed = client.get("/api/workspace").json()["analyses"]
    assert [a["id"] for a in listed] == [analysis["id"]]
    a = listed[0]
    assert a["status"] == "completed"
    assert a["dataset_id"] == ds["id"]
    assert a["dataset_filename"] == "corp_rep.csv"
    assert a["has_results"] is True
    assert a["has_mga"] is False and a["has_interpretation"] is False

    # the spec endpoint lets the frontend rebuild the model builder ...
    spec = client.get(f"/api/analyses/{analysis['id']}/spec").json()
    assert {c["name"] for c in spec["constructs"]} == {"CUSA", "CUSL"}
    assert spec["paths"] == SPEC["paths"]
    # ... without leaking server-side paths from the raw engine request
    assert "data_csv" not in spec

    assert client.get("/api/analyses/an_nope/spec").status_code == 404


def test_failed_analysis_is_listed_too():
    client, _ = fresh_user()
    ds = upload_dataset(client)
    create_analysis(client, {
        "dataset_id": ds["id"], "nboot": 100,
        "constructs": [{"name": "X", "indicators": ["no_such_col"],
                        "measurement": "reflective"}],
        "paths": [],
    }, expect="failed")
    listed = client.get("/api/workspace").json()["analyses"]
    assert listed[0]["status"] == "failed"
    assert listed[0]["error"]
    assert listed[0]["has_results"] is False


def test_draft_survives_logout_and_login():
    client, creds = fresh_user()
    saved = client.put("/api/workspace/draft", json=DRAFT).json()
    assert saved["saved_at"]

    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/workspace").status_code == 401

    again = TestClient(app)
    again.cookies.clear()
    assert again.post("/api/auth/login", json=creds).status_code == 200
    draft = again.get("/api/workspace").json()["draft"]
    assert draft["dataset_id"] == DRAFT["dataset_id"]
    assert draft["step"] == 2
    assert draft["constructs"] == DRAFT["constructs"]
    assert draft["paths"] == DRAFT["paths"]

    # a discarded draft stays gone
    assert again.delete("/api/workspace/draft").status_code == 200
    assert again.get("/api/workspace").json()["draft"] is None


def test_draft_is_per_user():
    alice, _ = fresh_user()
    bob, _ = fresh_user()
    alice.put("/api/workspace/draft", json=DRAFT)
    assert bob.get("/api/workspace").json()["draft"] is None
    bob.put("/api/workspace/draft", json={**DRAFT, "step": 1, "constructs": []})
    assert alice.get("/api/workspace").json()["draft"]["step"] == 2


def test_draft_rejects_out_of_range_payloads():
    client, _ = fresh_user()
    assert client.put("/api/workspace/draft",
                      json={**DRAFT, "step": 9}).status_code == 422
    assert client.put("/api/workspace/draft",
                      json={**DRAFT, "constructs": [{}] * 101}).status_code == 422
    # a second save overwrites the first — the draft is a single slot
    client.put("/api/workspace/draft", json=DRAFT)
    client.put("/api/workspace/draft", json={**DRAFT, "step": 3})
    assert client.get("/api/workspace").json()["draft"]["step"] == 3
