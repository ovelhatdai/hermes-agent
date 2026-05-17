import base64

import bcrypt
import pytest
from starlette.testclient import TestClient

from hermes_cli.web_server import app


TEST_TOKEN = "test-token-spec144"
BASIC_USER = "vini"
BASIC_PASSWORD = "dashboard-pass-spec144"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(
        "MENTORHUB_HERMES_TOKEN_HASH",
        bcrypt.hashpw(TEST_TOKEN.encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("MENTORHUB_CORS_ORIGINS", "https://app.base44.com")
    monkeypatch.setenv("HERMES_DASHBOARD_USER", BASIC_USER)
    dashboard_hash = bcrypt.hashpw(BASIC_PASSWORD.encode(), bcrypt.gensalt()).decode()
    monkeypatch.setenv(
        "HERMES_DASHBOARD_HASH",
        BASIC_USER + ":" + dashboard_hash.replace("$", "$$"),
    )

    async def fake_resolve_mentee(identifier, pg_pool=None):
        return {
            "id": "68cdc38e57d3f6bbc721dec7",
            "oab": "19570",
            "nome": "Karoline Catananti",
            "condutor": "Karoline Catananti",
            "status": "active",
        }

    async def fake_pg_pool():
        return object()

    async def fake_aggregate_snapshot(mentee, pg_pool):
        return {
            "mentee": mentee,
            "trafego_cards": [],
            "sla_alerts": [],
            "kanban_tasks": [],
            "latest_briefing": None,
            "meta": {
                "cache_hit": False,
                "latency_ms": 1,
                "fetched_at": "2026-05-10T00:00:00+00:00",
            },
        }

    monkeypatch.setattr("hermes_cli.web_server.resolve_mentee", fake_resolve_mentee)
    monkeypatch.setattr("hermes_cli.web_server._get_mentee_pg_pool", fake_pg_pool)
    monkeypatch.setattr("hermes_cli.web_server.aggregate_snapshot", fake_aggregate_snapshot)
    return TestClient(app)


def _basic_header(user: str = BASIC_USER, password: str = BASIC_PASSWORD) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def test_bearer_ok(client):
    r = client.get(
        "/api/mentee/19570/snapshot",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.json()["meta"]["auth_method"] == "bearer"
    assert r.json()["mentee"]["oab"] == "19570"


def test_bearer_wrong(client):
    r = client.get(
        "/api/mentee/19570/snapshot",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_no_auth(client):
    r = client.get("/api/mentee/19570/snapshot")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_basic_ok(client):
    r = client.get(
        "/api/mentee/19570/snapshot",
        headers={"Authorization": _basic_header()},
    )
    assert r.status_code == 200
    assert r.json()["meta"]["auth_method"] == "basic"


def test_other_routes_unaffected(client):
    r = client.get("/api/status")
    assert r.status_code != 401


def test_cors_allowed_origin(client):
    r = client.get(
        "/api/mentee/19570/snapshot",
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "Origin": "https://app.base44.com",
        },
    )
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://app.base44.com"


def test_cors_rejects_unknown_origin(client):
    r = client.get(
        "/api/mentee/19570/snapshot",
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "Origin": "https://evil.example",
        },
    )
    assert r.status_code == 403
    assert "Access-Control-Allow-Origin" not in r.headers


def test_mask_auth_header():
    from hermes_cli.web_server import _mask_auth_header

    assert _mask_auth_header("Bearer abcdefghijkl") == "Bearer a***"
    assert _mask_auth_header("") == "(empty)"
