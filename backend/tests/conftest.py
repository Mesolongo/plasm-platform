"""Every /api route now requires a login (backend/app/auth.py), and the test
files build module-level TestClients at import time. conftest is imported
before any test module, so patching TestClient.__init__ here gives every
client — including those module-level ones — a session cookie for a shared
test account. test_auth.py clears the cookie jar where it needs to be
unauthenticated."""
import uuid

import fastapi.testclient

from backend.app import auth

TEST_USER = f"pytest-{uuid.uuid4().hex[:8]}"
TEST_PASSWORD = "pytest-password"

auth.create_user(TEST_USER, TEST_PASSWORD)
_TOKEN = auth.create_session(TEST_USER)

_orig_init = fastapi.testclient.TestClient.__init__


def _logged_in_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    self.cookies.set(auth.COOKIE, _TOKEN)


fastapi.testclient.TestClient.__init__ = _logged_in_init
