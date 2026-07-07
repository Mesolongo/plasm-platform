"""In-process async job queue for long-running work (estimation, MGA, citations).

Jobs are file-backed under data/jobs/<job_id>.json so status survives restarts and
is pollable via GET /api/jobs/{job_id}. Work runs on a small thread pool — every
job body is a subprocess (the R engine) or a network call, so threads suffice and
the app stays a single process with zero extra infrastructure (same reasoning as
file storage over Postgres and share tokens over accounts). If deployment ever
needs multiple web workers, swap the executor for a Celery/Redis task with the
same (fn, on_done) shape — the polling API doesn't change.
"""
import datetime
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .engine import EngineError
from .storage import DATA_DIR, analysis_dir, new_id, read_json, write_json

JOBS_DIR = DATA_DIR / "jobs"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="plsem-job")
_lock = threading.Lock()

_INTERRUPTED = {"stage": "interrupted",
                "message": "the server restarted while this job was in flight — run it again"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _path(job_id: str):
    return JOBS_DIR / f"{job_id}.json"


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = read_json(_path(job_id))
        job.update(fields)
        write_json(_path(job_id), job)


def get(job_id: str) -> Optional[dict]:
    p = _path(job_id)
    return read_json(p) if p.exists() else None


def submit(kind: str, analysis_id: str, fn: Callable[[], None],
           on_done: Optional[Callable[[str, Optional[dict]], None]] = None,
           job_id: Optional[str] = None) -> dict:
    """Queue fn on the worker pool; returns the persisted job record immediately.

    fn persists its own results (results.json, mga.json, ...); the job record only
    tracks lifecycle. on_done(status, error) runs after the record is final, e.g.
    to mirror the outcome into an analysis meta.json. Pass job_id when the caller
    must reference the job in something it writes before the worker can finish.
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = {"id": job_id or new_id("job"), "kind": kind, "analysis_id": analysis_id,
           "status": "queued", "created_at": _now(), "error": None}
    write_json(_path(job["id"]), job)
    _executor.submit(_run, job["id"], fn, on_done)
    return job


def _run(job_id: str, fn, on_done) -> None:
    _update(job_id, status="running", started_at=_now())
    error = None
    try:
        fn()
        status = "completed"
    except EngineError as exc:
        status, error = "failed", {"stage": exc.stage, "message": exc.message}
    except Exception as exc:
        status, error = "failed", {"stage": "internal", "message": str(exc)}
    _update(job_id, status=status, error=error, finished_at=_now())
    if on_done:
        try:
            on_done(status, error)
        except Exception as exc:  # the job record is already final — don't lose it
            print(f"job {job_id}: on_done callback failed: {exc}", file=sys.stderr)


def recover_interrupted() -> list[dict]:
    """Startup sweep: fail jobs (and their analyses) left in flight by a dead server."""
    stale = []
    if not JOBS_DIR.exists():
        return stale
    for p in sorted(JOBS_DIR.glob("job_*.json")):
        job = read_json(p)
        if job.get("status") not in ("queued", "running"):
            continue
        job.update(status="failed", finished_at=_now(), error=_INTERRUPTED)
        write_json(p, job)
        stale.append(job)
        if job.get("kind") == "estimate" and job.get("analysis_id"):
            meta_path = analysis_dir(job["analysis_id"]) / "meta.json"
            if meta_path.exists():
                meta = read_json(meta_path)
                if meta.get("status") in ("queued", "running"):
                    meta.update(status="failed", error=_INTERRUPTED)
                    write_json(meta_path, meta)
    return stale
