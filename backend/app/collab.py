"""Collaboration: share links + comment threads for an analysis.

Sharing is token-based, not account-based — the right weight for this file-based
MVP. An owner mints a share link (a random token); anyone with the link gets a
read-only view of the results, and, if the link grants it, can leave comments
signed with a free-text name. No login, no user table.

Per-analysis state lives in analyses/<id>/collab.json:
    {"shares": [{token, scope, label, created_at}],
     "comments": [{id, author, body, created_at}]}

A single reverse index (data/share_index.json) maps token -> analysis_id so a
share link resolves in one lookup instead of scanning every analysis.
"""
import datetime
import secrets

from .storage import DATA_DIR, analysis_dir, read_json, write_json

SCOPES = ("view", "comment")
_INDEX = DATA_DIR / "share_index.json"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _collab_path(analysis_id: str):
    return analysis_dir(analysis_id) / "collab.json"


def _load(analysis_id: str) -> dict:
    p = _collab_path(analysis_id)
    return read_json(p) if p.exists() else {"shares": [], "comments": []}


def _save(analysis_id: str, state: dict) -> None:
    write_json(_collab_path(analysis_id), state)


def _load_index() -> dict:
    return read_json(_INDEX) if _INDEX.exists() else {}


def _save_index(index: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json(_INDEX, index)


def create_share(analysis_id: str, scope: str = "view", label: str = "") -> dict:
    """Mint a new share token for an analysis and record it in the reverse index."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    token = secrets.token_urlsafe(12)
    share = {"token": token, "scope": scope, "label": label.strip(),
             "created_at": _now()}
    state = _load(analysis_id)
    state["shares"].append(share)
    _save(analysis_id, state)

    index = _load_index()
    index[token] = analysis_id
    _save_index(index)
    return share


def list_shares(analysis_id: str) -> list[dict]:
    return _load(analysis_id)["shares"]


def revoke_share(analysis_id: str, token: str) -> bool:
    """Remove a share token. Returns True if a token was removed."""
    state = _load(analysis_id)
    kept = [s for s in state["shares"] if s["token"] != token]
    removed = len(kept) != len(state["shares"])
    if removed:
        state["shares"] = kept
        _save(analysis_id, state)
        index = _load_index()
        if index.pop(token, None) is not None:
            _save_index(index)
    return removed


def resolve_token(token: str) -> dict | None:
    """Look up a share by token. Returns {analysis_id, scope, label, ...} or None."""
    analysis_id = _load_index().get(token)
    if not analysis_id:
        return None
    for share in _load(analysis_id)["shares"]:
        if share["token"] == token:
            return {**share, "analysis_id": analysis_id}
    # Index and per-analysis state disagree (e.g. a hand-deleted analysis dir):
    # treat the link as dead rather than trusting a stale index entry.
    return None


def list_comments(analysis_id: str) -> list[dict]:
    return _load(analysis_id)["comments"]


def add_comment(analysis_id: str, author: str, body: str) -> dict:
    author = (author or "Anonymous").strip() or "Anonymous"
    body = body.strip()
    if not body:
        raise ValueError("comment body is empty")
    comment = {"id": secrets.token_urlsafe(6), "author": author[:80],
               "body": body[:4000], "created_at": _now()}
    state = _load(analysis_id)
    state["comments"].append(comment)
    _save(analysis_id, state)
    return comment
