"""File-based storage for the MVP. Layout under <project>/data/:

datasets/<id>/raw.csv       normalized data (whatever was uploaded, as CSV)
datasets/<id>/meta.json     variable dictionary, audit, upload metadata
analyses/<id>/request.json  engine request (spec + options)
analyses/<id>/results.json  engine output
analyses/<id>/meta.json     status, timestamps, error info

Swapped for Postgres + object storage in a later phase; the backend only touches
this module, so the swap is contained.
"""
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def dataset_dir(dataset_id: str, create: bool = False) -> Path:
    d = DATA_DIR / "datasets" / dataset_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def analysis_dir(analysis_id: str, create: bool = False) -> Path:
    d = DATA_DIR / "analyses" / analysis_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


def read_json(path: Path):
    return json.loads(path.read_text())
