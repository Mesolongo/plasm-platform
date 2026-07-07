"""Async job-queue tests: estimation runs through the queue (202 + poll), engine
failures land on both the job record and the analysis meta, and jobs orphaned by
a server death are failed by the startup recovery sweep."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import jobs
from backend.app.main import app
from backend.app.storage import analysis_dir, read_json, write_json

from .helpers import wait_job

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


def _dataset_id():
    with CSV.open("rb") as f:
        resp = client.post("/api/datasets", files={"file": ("corp_rep.csv", f, "text/csv")},
                           data={"missing_value": "-99"})
    return resp.json()["id"]


def test_estimation_runs_through_the_queue():
    resp = client.post("/api/analyses", json={"dataset_id": _dataset_id(), **SPEC})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["job_id"]

    job = wait_job(client, body["job_id"])
    assert job["status"] == "completed", job["error"]
    assert job["kind"] == "estimate" and job["analysis_id"] == body["id"]
    assert job["started_at"] and job["finished_at"]

    meta = client.get(f"/api/analyses/{body['id']}").json()
    assert meta["status"] == "completed"
    assert client.get(body["results_url"]).status_code == 200


def test_engine_failure_lands_on_job_and_meta():
    resp = client.post("/api/analyses", json={
        "dataset_id": _dataset_id(), "nboot": 100,
        "constructs": [{"name": "X", "indicators": ["no_such_col"],
                        "measurement": "reflective"}],
        "paths": [],
    })
    assert resp.status_code == 202, resp.text
    job = wait_job(client, resp.json()["job_id"])
    assert job["status"] == "failed"
    assert "no_such_col" in json.dumps(job["error"])

    meta = client.get(f"/api/analyses/{resp.json()['id']}").json()
    assert meta["status"] == "failed"
    assert meta["error"] == job["error"]


def test_unknown_job_404():
    assert client.get("/api/jobs/job_nope").status_code == 404


def test_recovery_fails_orphaned_jobs_and_their_analyses():
    # Simulate a job (and its analysis) left in flight by a dead server.
    a_dir = analysis_dir("an_orphaned_by_restart", create=True)
    write_json(a_dir / "meta.json", {"id": "an_orphaned_by_restart", "status": "running"})
    jobs.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(jobs.JOBS_DIR / "job_orphaned_by_restart.json",
               {"id": "job_orphaned_by_restart", "kind": "estimate",
                "analysis_id": "an_orphaned_by_restart", "status": "running",
                "created_at": "2026-01-01T00:00:00"})

    recovered = jobs.recover_interrupted()

    assert "job_orphaned_by_restart" in {j["id"] for j in recovered}
    job = read_json(jobs.JOBS_DIR / "job_orphaned_by_restart.json")
    assert job["status"] == "failed" and job["error"]["stage"] == "interrupted"
    meta = read_json(a_dir / "meta.json")
    assert meta["status"] == "failed" and meta["error"]["stage"] == "interrupted"
