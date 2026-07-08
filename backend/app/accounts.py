"""Persistence for accounts, sessions, and password-reset tokens.

Two interchangeable backends behind one function set:

- files under <data>/users|sessions|resets — the default, zero setup, same
  pattern as the rest of the MVP storage
- any SQLAlchemy-supported database when PLSEM_DATABASE_URL is set, e.g.
  postgresql+psycopg://user:pw@host:5432/dbname (tables are created on first
  use: plsem_users, plsem_sessions, plsem_resets)

Records have the same shape either way; auth.py never knows which backend is
live. Passwords arrive here already scrypt-hashed — neither backend ever sees
a plaintext password.
"""
import os

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, delete, insert, select, update

from .storage import DATA_DIR, read_json, write_json

USERS_DIR = DATA_DIR / "users"
SESSIONS_DIR = DATA_DIR / "sessions"
RESETS_DIR = DATA_DIR / "resets"

_META = MetaData()
_USERS = Table("plsem_users", _META,
               Column("username", String(32), primary_key=True),
               Column("email", String(254), index=True),
               Column("password_salt", Text, nullable=False),
               Column("password_hash", Text, nullable=False),
               Column("created_at", String(32)),
               Column("ai_date", String(10)),
               Column("ai_used", Integer, nullable=False, default=0))
_SESSIONS = Table("plsem_sessions", _META,
                  Column("token", String(64), primary_key=True),
                  Column("username", String(32), index=True),
                  Column("expires", String(32)))
_RESETS = Table("plsem_resets", _META,
                Column("token", String(64), primary_key=True),
                Column("username", String(32)),
                Column("expires", String(32)))

_engines: dict = {}


def _db_url() -> str | None:
    return os.environ.get("PLSEM_DATABASE_URL") or None


def _engine():
    url = _db_url()
    if url not in _engines:
        from sqlalchemy import create_engine
        engine = create_engine(url, pool_pre_ping=True)
        _META.create_all(engine)
        _engines[url] = engine
    return _engines[url]


def _row_to_user(row) -> dict:
    return {"username": row.username, "email": row.email,
            "password": {"salt": row.password_salt, "hash": row.password_hash},
            "created_at": row.created_at,
            "ai_usage": {row.ai_date: row.ai_used} if row.ai_date else {}}


def _user_to_row(user: dict) -> dict:
    # charge_ai_call keeps at most one day in ai_usage, so a single pair suffices
    ai_date, ai_used = next(iter(user.get("ai_usage", {}).items()), (None, 0))
    return {"username": user["username"], "email": user.get("email"),
            "password_salt": user["password"]["salt"],
            "password_hash": user["password"]["hash"],
            "created_at": user.get("created_at"),
            "ai_date": ai_date, "ai_used": ai_used}


# --------------------------------- users ----------------------------------- #

def get_user(username: str) -> dict | None:
    if _db_url():
        with _engine().connect() as conn:
            row = conn.execute(select(_USERS).where(_USERS.c.username == username)).first()
        return _row_to_user(row) if row else None
    p = USERS_DIR / f"{username}.json"
    return read_json(p) if p.is_file() else None


def put_user(user: dict) -> None:
    if _db_url():
        row = _user_to_row(user)
        with _engine().begin() as conn:
            done = conn.execute(update(_USERS)
                                .where(_USERS.c.username == row["username"])
                                .values(**row)).rowcount
            if not done:
                conn.execute(insert(_USERS).values(**row))
        return
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(USERS_DIR / f"{user['username']}.json", user)


def user_by_email(email: str) -> dict | None:
    email = email.strip().lower()
    if _db_url():
        with _engine().connect() as conn:
            row = conn.execute(select(_USERS).where(_USERS.c.email == email)).first()
        return _row_to_user(row) if row else None
    if not USERS_DIR.is_dir():
        return None
    for p in USERS_DIR.glob("*.json"):
        user = read_json(p)
        if (user.get("email") or "").lower() == email:
            return user
    return None


# -------------------------------- sessions --------------------------------- #

def add_session(token: str, username: str, expires: str) -> None:
    if _db_url():
        with _engine().begin() as conn:
            conn.execute(insert(_SESSIONS).values(token=token, username=username,
                                                  expires=expires))
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(SESSIONS_DIR / f"{token}.json", {"username": username, "expires": expires})


def get_session(token: str) -> dict | None:
    if _db_url():
        with _engine().connect() as conn:
            row = conn.execute(select(_SESSIONS).where(_SESSIONS.c.token == token)).first()
        return {"username": row.username, "expires": row.expires} if row else None
    p = SESSIONS_DIR / f"{token}.json"
    return read_json(p) if p.is_file() else None


def delete_session(token: str) -> None:
    if _db_url():
        with _engine().begin() as conn:
            conn.execute(delete(_SESSIONS).where(_SESSIONS.c.token == token))
        return
    (SESSIONS_DIR / f"{token}.json").unlink(missing_ok=True)


def delete_user_sessions(username: str) -> None:
    if _db_url():
        with _engine().begin() as conn:
            conn.execute(delete(_SESSIONS).where(_SESSIONS.c.username == username))
        return
    if SESSIONS_DIR.is_dir():
        for p in SESSIONS_DIR.glob("*.json"):
            if read_json(p).get("username") == username:
                p.unlink(missing_ok=True)


# ------------------------------ reset tokens ------------------------------- #

def add_reset(token: str, username: str, expires: str) -> None:
    if _db_url():
        with _engine().begin() as conn:
            conn.execute(insert(_RESETS).values(token=token, username=username,
                                                expires=expires))
        return
    RESETS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(RESETS_DIR / f"{token}.json", {"username": username, "expires": expires})


def pop_reset(token: str) -> dict | None:
    """Return and delete the reset record — tokens are single-use."""
    if _db_url():
        with _engine().begin() as conn:
            row = conn.execute(select(_RESETS).where(_RESETS.c.token == token)).first()
            if row:
                conn.execute(delete(_RESETS).where(_RESETS.c.token == token))
        return {"username": row.username, "expires": row.expires} if row else None
    p = RESETS_DIR / f"{token}.json"
    if not p.is_file():
        return None
    reset = read_json(p)
    p.unlink(missing_ok=True)
    return reset
