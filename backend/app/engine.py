"""Wrapper around the R PLS-SEM engine (engine/R/estimate.R).

Synchronous subprocess call for the MVP; the interface (request dict in, results
dict out, EngineError on failure) is what a Celery task will wrap later, so callers
won't change.
"""
import json
import subprocess
from pathlib import Path

from .storage import ROOT

ESTIMATE_SCRIPT = ROOT / "engine" / "R" / "estimate.R"
MGA_SCRIPT = ROOT / "engine" / "R" / "mga.R"
TIMEOUT_SECONDS = 600


class EngineError(Exception):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"[{stage}] {message}")


def run_engine(request_path: Path, results_path: Path, script: Path = ESTIMATE_SCRIPT) -> dict:
    """Run an engine script on a written request.json; returns parsed results."""
    proc = subprocess.run(
        ["Rscript", str(script), str(request_path), str(results_path)],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    if proc.returncode == 2:
        # Structured engine error on stdout
        try:
            err = json.loads(proc.stdout.strip().splitlines()[-1])["error"]
            raise EngineError(err.get("stage", "unknown"), err.get("message", ""))
        except (json.JSONDecodeError, KeyError, IndexError):
            raise EngineError("unknown", proc.stdout or proc.stderr)
    if proc.returncode != 0:
        raise EngineError("crash", (proc.stderr or proc.stdout)[-2000:])
    return json.loads(results_path.read_text())
