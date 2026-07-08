"""Auth: open signup, session cookies, the /api login gate, and the AI quota."""
import re
import uuid

from fastapi.testclient import TestClient

from backend.app import accounts, auth, mailer
from backend.app.main import app


def anon_client() -> TestClient:
    client = TestClient(app)
    client.cookies.clear()  # conftest logs every TestClient in; undo that
    return client


def creds():
    slug = uuid.uuid4().hex[:8]
    return {"username": f"user-{slug}", "password": "s3cret-pw",
            "email": f"user-{slug}@example.org"}


def test_api_requires_login():
    client = anon_client()
    assert client.get("/api/fixtures/model-spec").status_code == 401
    assert client.post("/api/analyses", json={}).status_code == 401


def test_shared_viewer_and_static_stay_public():
    client = anon_client()
    # wrong token is a 404, not a login redirect — capability URLs keep working
    assert client.get("/api/shared/not-a-real-token").status_code == 404
    assert client.get("/app/").status_code == 200


def test_register_login_logout_roundtrip():
    client = anon_client()
    c = creds()

    r = client.post("/api/auth/register", json=c)
    assert r.status_code == 201, r.text
    assert r.json()["username"] == c["username"]
    assert auth.COOKIE in client.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == c["username"]
    assert client.get("/api/fixtures/model-spec").status_code == 200

    # duplicate username is rejected
    assert anon_client().post("/api/auth/register", json=c).status_code == 409

    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401

    # fresh login with the same credentials
    client2 = anon_client()
    assert client2.post("/api/auth/login", json=c).status_code == 200
    assert client2.get("/api/auth/me").status_code == 200


def test_bad_credentials_and_weak_signup():
    client = anon_client()
    c = creds()
    client.post("/api/auth/register", json=c)

    bad = anon_client()
    assert bad.post("/api/auth/login",
                    json={**c, "password": "wrong-password"}).status_code == 401
    assert bad.post("/api/auth/register",
                    json={**creds(), "password": "short"}).status_code == 422
    assert bad.post("/api/auth/register",
                    json={**creds(), "username": "no spaces!"}).status_code == 422
    assert bad.post("/api/auth/register",
                    json={**creds(), "email": "not-an-email"}).status_code == 422
    # a second account on the same email is rejected (it would break resets)
    assert bad.post("/api/auth/register",
                    json={**creds(), "email": c["email"]}).status_code == 409


def test_ai_daily_quota(monkeypatch):
    monkeypatch.setenv("PLSEM_AI_DAILY_LIMIT", "2")
    c = creds()
    auth.create_user(c["username"], c["password"])
    assert auth.charge_ai_call(c["username"]) is True
    assert auth.charge_ai_call(c["username"]) is True
    assert auth.charge_ai_call(c["username"]) is False  # limit hit


def test_forgot_password_reset_roundtrip(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append(body))
    client = anon_client()
    c = creds()
    client.post("/api/auth/register", json=c)

    # forgot never reveals whether the address exists (and mails nobody)
    fresh = anon_client()
    r = fresh.post("/api/auth/forgot", json={"email": "nobody@example.org"})
    assert r.status_code == 200
    assert len(sent) == 1  # just the welcome mail so far
    assert fresh.post("/api/auth/forgot", json={"email": c["email"]}).status_code == 200

    # pull the single-use token out of the reset mail's link
    token = re.search(r"\?reset=([A-Za-z0-9_-]+)", sent[-1]).group(1)

    r = fresh.post("/api/auth/reset", json={"token": token, "password": "brand-new-pw"})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == c["username"]
    assert fresh.get("/api/auth/me").status_code == 200  # reset logs you in

    # the token is single-use, old sessions are dead, and only the new password works
    assert fresh.post("/api/auth/reset",
                      json={"token": token, "password": "another-pw"}).status_code == 410
    assert client.get("/api/auth/me").status_code == 401
    assert anon_client().post("/api/auth/login", json=c).status_code == 401
    assert anon_client().post("/api/auth/login",
                              json={**c, "password": "brand-new-pw"}).status_code == 200


def test_expired_session_is_rejected():
    c = creds()
    auth.create_user(c["username"], c["password"])
    token = "expired-token-" + uuid.uuid4().hex[:8]
    accounts.add_session(token, c["username"], "2000-01-01T00:00:00")
    assert auth.session_username(token) is None
    assert accounts.get_session(token) is None  # expired sessions are reaped on read


def test_google_signin_disabled_by_default():
    client = anon_client()
    assert client.get("/api/auth/methods").json()["google"] is False
    assert client.get("/api/auth/google").status_code == 404


def test_google_signin_flow(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    client = anon_client()
    assert client.get("/api/auth/methods").json()["google"] is True

    # start: redirect to Google carrying our client id; state parked in a cookie
    r = client.get("/api/auth/google", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(auth.GOOGLE_AUTH_URL)
    assert "test-client-id" in location
    state = re.search(r"state=([A-Za-z0-9_-]+)", location).group(1)

    # a forged state bounces back to the login screen with an error
    bad = client.get("/api/auth/google/callback?code=x&state=forged",
                     follow_redirects=False)
    assert "auth_error" in bad.headers["location"]

    # stub Google's side; the callback then creates the account and the session
    email = f"g-{uuid.uuid4().hex[:8]}@gmail.com"
    monkeypatch.setattr(auth, "_google_identity",
                        lambda code, uri: {"email": email, "email_verified": True,
                                           "name": "G User"})
    ok = client.get(f"/api/auth/google/callback?code=x&state={state}",
                    follow_redirects=False)
    assert ok.status_code == 302 and ok.headers["location"] == "/app/"
    me = client.get("/api/auth/me").json()
    assert me["email"] == email

    # the same Google identity signs into the same account — no duplicates
    assert auth.google_signin(email, "G User")["username"] == me["username"]
    # and the placeholder password can't be used to log in
    assert anon_client().post("/api/auth/login",
                              json={"username": me["username"],
                                    "password": "anything-at-all"}).status_code == 401


def test_postgres_style_sql_backend(monkeypatch, tmp_path):
    """Same auth flows with PLSEM_DATABASE_URL set (SQLite here; the SQLAlchemy
    layer is identical for postgresql+psycopg://)."""
    monkeypatch.setenv("PLSEM_DATABASE_URL", f"sqlite:///{tmp_path}/accounts.db")
    client = anon_client()
    c = creds()

    assert client.post("/api/auth/register", json=c).status_code == 201
    assert client.get("/api/auth/me").json()["username"] == c["username"]
    assert client.get("/api/fixtures/model-spec").status_code == 200

    # the account lives in the database, not in the file store
    user = accounts.get_user(c["username"])
    assert user["email"] == c["email"]
    assert not (accounts.USERS_DIR / f"{c['username']}.json").exists()
    # and only the scrypt hash is stored — never the password itself
    assert c["password"] not in str(user)

    assert anon_client().post("/api/auth/login", json=c).status_code == 200
    assert anon_client().post("/api/auth/login",
                              json={**c, "password": "wrong"}).status_code == 401
