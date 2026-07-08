"""Accounts, sessions, and password resets.

Persistence lives in accounts.py: your PostgreSQL when PLSEM_DATABASE_URL is
set, files under <data>/ otherwise. Passwords are scrypt-hashed before they
reach storage, so neither backend ever holds a readable password.

Anyone can register (open signup). All /api routes except /api/auth/* and the
token-based /api/shared/* viewer require a session cookie — enforced by the
middleware in main.py, which also charges AI-powered POSTs against a per-user
daily quota (PLSEM_AI_DAILY_LIMIT, default 25) so one account can't drain the
server's Anthropic credits.
"""
import contextvars
import datetime
import hashlib
import hmac
import os
import re
import secrets

import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import accounts, mailer

COOKIE = "plsem_session"
SESSION_DAYS = 30
RESET_MINUTES = 60
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Set by the auth middleware for the duration of a request, so storage writers
# (dataset/analysis meta) can stamp an owner without threading it through every
# route signature. anyio copies the context into the threadpool that runs sync
# routes, so this is safe for both async and sync handlers.
current_username: contextvars.ContextVar = contextvars.ContextVar(
    "current_username", default=None)


def ai_daily_limit() -> int:
    return int(os.environ.get("PLSEM_AI_DAILY_LIMIT", "25"))


# ------------------------------- passwords -------------------------------- #

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def _load_user(username: str) -> dict | None:
    return accounts.get_user(username)


def find_by_email(email: str) -> dict | None:
    return accounts.user_by_email(email)


def create_user(username: str, password: str, email: str | None = None) -> dict:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(422, "username must be 3-32 characters: letters, "
                                 "digits, dots, dashes, underscores")
    if len(password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    if email is not None:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise HTTPException(422, "that doesn't look like an email address")
        if find_by_email(email):
            raise HTTPException(409, "that email already has an account — "
                                     "use “Forgot password?” to get back in")
    if _load_user(username):
        raise HTTPException(409, "that username is already taken")
    salt = os.urandom(16)
    user = {
        "username": username,
        "email": email,
        "password": {"salt": salt.hex(), "hash": _hash_password(password, salt)},
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ai_usage": {},
    }
    accounts.put_user(user)
    return user


def set_password(username: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    user = _load_user(username)
    if not user:
        raise HTTPException(404, "no such account")
    salt = os.urandom(16)
    user["password"] = {"salt": salt.hex(), "hash": _hash_password(new_password, salt)}
    accounts.put_user(user)


def verify_user(username: str, password: str) -> dict | None:
    user = _load_user(username.strip().lower())
    if not user:
        return None
    pw = user["password"]
    computed = _hash_password(password, bytes.fromhex(pw["salt"]))
    return user if hmac.compare_digest(computed, pw["hash"]) else None


# ------------------------------- sessions --------------------------------- #

def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.now() + datetime.timedelta(days=SESSION_DAYS)
    accounts.add_session(token, username, expires.isoformat(timespec="seconds"))
    return token


def session_username(token: str | None) -> str | None:
    """Resolve a session cookie to a username; expired sessions are deleted."""
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    session = accounts.get_session(token)
    if not session:
        return None
    if datetime.datetime.fromisoformat(session["expires"]) < datetime.datetime.now():
        accounts.delete_session(token)
        return None
    return session["username"]


def destroy_session(token: str | None) -> None:
    if token and re.fullmatch(r"[A-Za-z0-9_-]+", token):
        accounts.delete_session(token)


def destroy_user_sessions(username: str) -> None:
    """Log the user out everywhere — run after a password reset."""
    accounts.delete_user_sessions(username)


# ---------------------------- password resets ------------------------------ #

def create_reset_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.now() + datetime.timedelta(minutes=RESET_MINUTES)
    accounts.add_reset(token, username, expires.isoformat(timespec="seconds"))
    return token


def consume_reset_token(token: str) -> str | None:
    """Single use: resolve token -> username and delete it; None if bad/expired."""
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    reset = accounts.pop_reset(token)
    if not reset:
        return None
    if datetime.datetime.fromisoformat(reset["expires"]) < datetime.datetime.now():
        return None
    return reset["username"]


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", path="/")


# ------------------------------ AI daily quota ----------------------------- #

def charge_ai_call(username: str) -> bool:
    """Count one AI call against today's quota; False when the limit is hit.

    Read-modify-write without a lock: concurrent AI calls from one user could
    under-count by a request or two, which is fine for a soft cost cap.
    """
    user = _load_user(username)
    if not user:
        return False
    today = datetime.date.today().isoformat()
    used = user.get("ai_usage", {}).get(today, 0)
    if used >= ai_daily_limit():
        return False
    user["ai_usage"] = {today: used + 1}  # keep only today; old days are noise
    accounts.put_user(user)
    return True


def ai_used_today(user: dict) -> int:
    return user.get("ai_usage", {}).get(datetime.date.today().isoformat(), 0)


# -------------------------------- endpoints -------------------------------- #

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Registration(Credentials):
    email: str = Field(min_length=3, max_length=254)


class ForgotRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def _me_payload(user: dict) -> dict:
    return {"username": user["username"], "email": user.get("email"),
            "ai_used_today": ai_used_today(user),
            "ai_daily_limit": ai_daily_limit()}


@router.post("/register", status_code=201)
def register(reg: Registration, response: Response):
    """Open signup: create the account, log it in, send a welcome mail."""
    user = create_user(reg.username, reg.password, reg.email)
    _set_cookie(response, create_session(user["username"]))
    mailer.send(user["email"], "Welcome to plsem-platform",
                f"Hi {user['username']},\n\n"
                "Your plsem-platform account was created with this email "
                "address. You can now upload survey data, specify a structural "
                "model, and run a full PLS-SEM analysis.\n\n"
                "If you didn't create this account, ignore this mail — no "
                "analysis data is attached to your address.\n")
    return _me_payload(user)


@router.post("/login")
def login(creds: Credentials, response: Response):
    user = verify_user(creds.username, creds.password)
    if not user:
        raise HTTPException(401, "wrong username or password")
    _set_cookie(response, create_session(user["username"]))
    return _me_payload(user)


@router.post("/logout")
def logout(request: Request, response: Response):
    destroy_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/forgot")
def forgot_password(req: ForgotRequest, request: Request):
    """Mail a single-use reset link. Always answers ok — the response must not
    reveal whether an address has an account (no user enumeration)."""
    user = find_by_email(req.email)
    if user and user.get("email"):
        token = create_reset_token(user["username"])
        base = (os.environ.get("PLSEM_BASE_URL") or str(request.base_url)).rstrip("/")
        mailer.send(user["email"], "Reset your plsem-platform password",
                    f"Hi {user['username']},\n\n"
                    "Someone asked to reset the password for this account. "
                    f"Use this link within {RESET_MINUTES} minutes:\n\n"
                    f"  {base}/app/?reset={token}\n\n"
                    "If this wasn't you, ignore this mail — your password "
                    "is unchanged.\n")
    return {"ok": True, "detail": "if that address has an account, "
                                  "a reset link is on its way"}


@router.post("/reset")
def reset_password(req: ResetRequest, response: Response):
    """Set a new password from a reset-link token; logs out every session."""
    username = consume_reset_token(req.token)
    if not username:
        raise HTTPException(410, "this reset link is invalid or has expired — "
                                 "request a new one")
    set_password(username, req.password)
    destroy_user_sessions(username)
    _set_cookie(response, create_session(username))
    return _me_payload(_load_user(username))


@router.get("/me")
def me(request: Request):
    username = session_username(request.cookies.get(COOKIE))
    user = _load_user(username) if username else None
    if not user:
        raise HTTPException(401, "not logged in")
    return _me_payload(user)


# ---------------------------- Sign in with Google --------------------------- #
# Authorization-code flow, enabled by GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET.
# Only basic scopes (openid email profile), so no Google verification review is
# needed. Accounts are matched by verified email: an existing password account
# with the same address is simply signed in; otherwise one is created with an
# unguessable placeholder password ("Forgot password?" can set a real one).

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
STATE_COOKIE = "plsem_oauth_state"


def google_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID")
                and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _redirect_uri(request: Request) -> str:
    base = (os.environ.get("PLSEM_BASE_URL") or str(request.base_url)).rstrip("/")
    return f"{base}/api/auth/google/callback"


def _google_identity(code: str, redirect_uri: str) -> dict:
    """Exchange the auth code, then fetch the userinfo claims."""
    token = httpx.post(GOOGLE_TOKEN_URL, timeout=20, data={
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
    }).raise_for_status().json()
    return httpx.get(GOOGLE_USERINFO_URL, timeout=20, headers={
        "Authorization": f"Bearer {token['access_token']}",
    }).raise_for_status().json()


def _username_from_email(email: str) -> str:
    stem = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower()).lstrip("._-")
    stem = (stem or "user")[:28]
    if len(stem) < 3:
        stem = f"user-{stem}" if stem else "user"
    candidate, n = stem, 1
    while _load_user(candidate):
        n += 1
        candidate = f"{stem}-{n}"
    return candidate


def google_signin(email: str, name: str | None) -> dict:
    """Find the account for a Google-verified email, creating one if needed."""
    email = email.strip().lower()
    user = find_by_email(email)
    if user:
        return user
    salt = os.urandom(16)
    user = {
        "username": _username_from_email(email),
        "email": email,
        # placeholder credential nobody knows — password login stays impossible
        # until the user sets one through the reset flow
        "password": {"salt": salt.hex(),
                     "hash": _hash_password(secrets.token_urlsafe(32), salt)},
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ai_usage": {},
        "google": True,
    }
    accounts.put_user(user)
    mailer.send(email, "Welcome to plsem-platform",
                f"Hi {name or user['username']},\n\n"
                "Your plsem-platform account was created by signing in with "
                "Google using this address. You can now upload survey data, "
                "specify a structural model, and run a full PLS-SEM analysis.\n")
    return user


@router.get("/methods")
def auth_methods():
    """Which sign-in methods the frontend should offer."""
    return {"password": True, "google": google_enabled()}


@router.get("/google")
def google_start(request: Request):
    if not google_enabled():
        raise HTTPException(404, "Google sign-in is not configured on this server")
    state = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=302)
    response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                        samesite="lax", path="/api/auth/google")
    return response


@router.get("/google/callback")
def google_callback(request: Request, code: str = "", state: str = "",
                    error: str = ""):
    def fail(msg: str) -> RedirectResponse:
        return RedirectResponse("/app/?auth_error=" + urllib.parse.quote(msg),
                                status_code=302)

    if not google_enabled():
        raise HTTPException(404, "Google sign-in is not configured on this server")
    if error:
        return fail(f"Google sign-in was cancelled ({error})")
    if not state or state != request.cookies.get(STATE_COOKIE):
        return fail("sign-in session mismatch — try again")
    try:
        claims = _google_identity(code, _redirect_uri(request))
    except httpx.HTTPError:
        return fail("could not reach Google to finish the sign-in — try again")
    if not claims.get("email") or not claims.get("email_verified"):
        return fail("Google did not confirm a verified email for this account")

    user = google_signin(claims["email"], claims.get("name"))
    response = RedirectResponse("/app/", status_code=302)
    response.delete_cookie(STATE_COOKIE, path="/api/auth/google")
    _set_cookie(response, create_session(user["username"]))
    return response
