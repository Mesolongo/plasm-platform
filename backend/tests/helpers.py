"""Polling helpers for the async job queue — POSTs answer 202 immediately, so
tests wait for the queued work to reach a terminal status before asserting."""
import time


def wait_analysis(client, analysis_id, timeout=600):
    """Poll an analysis until its queued estimation finishes; returns the meta."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = client.get(f"/api/analyses/{analysis_id}").json()
        if meta["status"] in ("completed", "failed"):
            return meta
        time.sleep(0.15)
    raise AssertionError(f"analysis {analysis_id} did not finish within {timeout}s")


def create_analysis(client, payload, expect="completed"):
    """POST /api/analyses and wait for the estimation job; returns the POST body
    (id, job_id, results_url, ...) merged with the final meta."""
    resp = client.post("/api/analyses", json=payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    meta = wait_analysis(client, body["id"])
    assert meta["status"] == expect, meta.get("error")
    return {**body, **meta}


def wait_job(client, job_id, timeout=600):
    """Poll GET /api/jobs/{job_id} until the job is completed or failed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        job = resp.json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.15)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")
