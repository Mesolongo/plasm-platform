"""Per-user workspace: pick up where you left off after logging out.

Two pieces behind GET /api/workspace:

- listing — datasets and analyses are stamped with an owner when they are
  created (main.py), so a fresh login can list and reopen everything the user
  ever uploaded or ran. Nothing extra is written; this scans the file store.
- draft — the in-progress model builder state (which dataset was open, the
  constructs/paths/interactions being edited, which analysis was showing). The
  frontend autosaves it on every edit; it lives under
  <data>/workspaces/<username>.json and survives logout, so closing the tab
  mid-model costs nothing.

Usernames are validated at registration (auth.USERNAME_RE: [a-z0-9._-]) and so
are safe to use as file names.
"""
import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import auth
from .storage import DATA_DIR, read_json, write_json

WORKSPACES_DIR = DATA_DIR / "workspaces"

# Extra artifacts an analysis may have accumulated; surfaced as flags so the
# frontend can say "report ready", "has MGA" etc. without extra requests.
_ANALYSIS_ARTIFACTS = {
    "has_results": "results.json",
    "has_mga": "mga.json",
    "has_interpretation": "interpretation.json",
    "has_citations": "citations.json",
    "has_manuscript": "manuscript.json",
}

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def _require_user() -> str:
    username = auth.current_username.get()
    if not username:  # unreachable behind the login middleware; belt and braces
        raise HTTPException(401, "login required")
    return username


def _owned_metas(kind: str, username: str) -> list[dict]:
    """All meta.json records under <data>/<kind>/*/ owned by this user."""
    base = DATA_DIR / kind
    metas = []
    if base.is_dir():
        for meta_path in base.glob("*/meta.json"):
            try:
                meta = read_json(meta_path)
            except Exception:
                continue  # half-written or corrupt entry; skip, don't 500
            if meta.get("owner") == username:
                metas.append(meta)
    return metas


def list_datasets(username: str) -> list[dict]:
    items = [{
        "id": m["id"],
        "filename": m.get("filename"),
        "uploaded_at": m.get("uploaded_at"),
        "n_observations": m.get("n_observations"),
        "n_variables": len(m.get("variables") or []),
    } for m in _owned_metas("datasets", username)]
    return sorted(items, key=lambda d: d.get("uploaded_at") or "", reverse=True)


def list_analyses(username: str) -> list[dict]:
    dataset_names = {d["id"]: d["filename"] for d in list_datasets(username)}
    items = []
    for m in _owned_metas("analyses", username):
        a_dir = DATA_DIR / "analyses" / m["id"]
        items.append({
            "id": m["id"],
            "dataset_id": m.get("dataset_id"),
            "dataset_filename": dataset_names.get(m.get("dataset_id")),
            "created_at": m.get("created_at"),
            "status": m.get("status"),
            "error": m.get("error"),
            **{flag: (a_dir / fname).exists()
               for flag, fname in _ANALYSIS_ARTIFACTS.items()},
        })
    return sorted(items, key=lambda a: a.get("created_at") or "", reverse=True)


# --------------------------------- draft ----------------------------------- #

class Draft(BaseModel):
    """Model-builder state as the frontend holds it. The construct/path dicts
    are stored as-is (the frontend is the only consumer); caps keep a bad
    client from turning the workspace file into a dumping ground."""
    dataset_id: Optional[str] = Field(default=None, max_length=64)
    analysis_id: Optional[str] = Field(default=None, max_length=64)
    step: int = Field(default=1, ge=1, le=3)
    constructs: List[dict] = Field(default=[], max_length=100)
    paths: List[dict] = Field(default=[], max_length=200)
    interactions: List[dict] = Field(default=[], max_length=50)


def _draft_path(username: str):
    return WORKSPACES_DIR / f"{username}.json"


def load_draft(username: str) -> dict | None:
    p = _draft_path(username)
    return read_json(p) if p.is_file() else None


# -------------------------------- endpoints -------------------------------- #

@router.get("")
def get_workspace():
    """Everything needed to resume: the user's datasets, analyses, and draft."""
    username = _require_user()
    return {
        "username": username,
        "datasets": list_datasets(username),
        "analyses": list_analyses(username),
        "draft": load_draft(username),
    }


@router.put("/draft")
def put_draft(draft: Draft):
    """Autosave the model builder; overwrites the previous draft."""
    username = _require_user()
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        **draft.model_dump(),
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(_draft_path(username), payload)
    return payload


@router.delete("/draft")
def delete_draft():
    username = _require_user()
    _draft_path(username).unlink(missing_ok=True)
    return {"ok": True}
