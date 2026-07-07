"""Phase 3 connector tests: SQL and Google Sheets ingestion.

Both connectors must funnel through the same _persist_dataset path as file upload,
so a dataset produced by a connector is indistinguishable downstream — it carries a
variable dictionary and an audit and can be modelled like any uploaded dataset.
"""
import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app.main import app

CSV = Path(__file__).parent / "fixtures_corp_rep.csv"

client = TestClient(app)


@pytest.fixture(scope="module")
def sqlite_dsn(tmp_path_factory):
    """A file-backed SQLite database holding the corp-rep fixture as a table."""
    db = tmp_path_factory.mktemp("db") / "corp.sqlite"
    from sqlalchemy import create_engine
    dsn = f"sqlite:///{db}"
    engine = create_engine(dsn)
    pd.read_csv(CSV).to_sql("corp_rep", engine, index=False, if_exists="replace")
    engine.dispose()
    return dsn


# ------------------------------- SQL connector ------------------------------- #

def test_sql_connector_ingests_dataset(sqlite_dsn):
    resp = client.post("/api/datasets/sql", json={
        "dsn": sqlite_dsn,
        "query": "SELECT * FROM corp_rep",
        "missing_value": "-99",
    })
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["id"].startswith("ds_")
    assert meta["filename"].startswith("sql:")  # source labelled by its origin
    assert meta["n_observations"] > 0
    assert meta["variables"], "connector dataset must carry a variable dictionary"
    assert "audit" in meta
    # The dataset is fully usable downstream: it can be fetched like any other.
    got = client.get(f"/api/datasets/{meta['id']}")
    assert got.status_code == 200
    assert got.json()["n_observations"] == meta["n_observations"]


def test_sql_connector_projects_columns(sqlite_dsn):
    resp = client.post("/api/datasets/sql", json={
        "dsn": sqlite_dsn,
        "query": "SELECT cusa, cusl_1 FROM corp_rep",
    })
    assert resp.status_code == 200, resp.text
    names = {v["name"] for v in resp.json()["variables"]}
    assert names == {"cusa", "cusl_1"}


@pytest.mark.parametrize("query", [
    "DELETE FROM corp_rep",
    "DROP TABLE corp_rep",
    "UPDATE corp_rep SET cusa = 1",
    "SELECT * FROM corp_rep; DROP TABLE corp_rep",
    "   ",
])
def test_sql_connector_rejects_non_select(sqlite_dsn, query):
    resp = client.post("/api/datasets/sql", json={"dsn": sqlite_dsn, "query": query})
    assert resp.status_code == 422, resp.text


def test_sql_connector_reports_bad_dsn():
    resp = client.post("/api/datasets/sql", json={
        "dsn": "sqlite:////nonexistent/nope.sqlite",
        "query": "SELECT * FROM missing_table",
    })
    assert resp.status_code == 422
    assert "failed" in resp.json()["detail"].lower()


# --------------------------- Google Sheets connector -------------------------- #

class _FakeResponse:
    def __init__(self, content: bytes, content_type: str, status: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


def test_gsheet_connector_ingests_csv(monkeypatch):
    csv_bytes = CSV.read_bytes()
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResponse(csv_bytes, "text/csv; charset=utf-8")

    monkeypatch.setattr(main_module.httpx, "get", fake_get)
    resp = client.post("/api/datasets/gsheet", json={
        "url": "https://docs.google.com/spreadsheets/d/ABC123_x-y/edit#gid=42",
        "missing_value": "-99",
    })
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["filename"] == "gsheet"
    assert meta["n_observations"] > 0
    assert meta["variables"]
    # The export URL must target the right spreadsheet id and tab (gid).
    assert "ABC123_x-y" in captured["url"]
    assert "format=csv" in captured["url"]
    assert "gid=42" in captured["url"]


def test_gsheet_connector_defaults_gid_zero(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResponse(CSV.read_bytes(), "text/csv")

    monkeypatch.setattr(main_module.httpx, "get", fake_get)
    resp = client.post("/api/datasets/gsheet", json={
        "url": "https://docs.google.com/spreadsheets/d/ABC123/edit",
    })
    assert resp.status_code == 200, resp.text
    assert "gid=0" in captured["url"]


def test_gsheet_connector_rejects_non_sheet_url():
    resp = client.post("/api/datasets/gsheet", json={"url": "https://example.com/data"})
    assert resp.status_code == 422
    assert "Google Sheets" in resp.json()["detail"]


def test_gsheet_connector_detects_private_sheet(monkeypatch):
    # A private sheet redirects to an HTML sign-in page instead of CSV.
    monkeypatch.setattr(main_module.httpx, "get",
                        lambda url, **kw: _FakeResponse(b"<html>login</html>", "text/html"))
    resp = client.post("/api/datasets/gsheet", json={
        "url": "https://docs.google.com/spreadsheets/d/PRIV123/edit",
    })
    assert resp.status_code == 422
    assert "link-viewable" in resp.json()["detail"]
