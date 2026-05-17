import asyncio

from starlette.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.web_server import MONGO_ID_RE, OAB_RE, app, resolve_mentee


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, value):
        self.calls.append((sql, value))
        return self.row


class FakePool:
    def __init__(self, row=None):
        self.conn = FakeConn(row)

    def acquire(self):
        return FakeAcquire(self.conn)


def test_oab_regex():
    assert OAB_RE.match("19570")
    assert OAB_RE.match("277712")
    assert not OAB_RE.match("ABC")
    assert not OAB_RE.match("0277.712")


def test_mongo_id_regex():
    assert MONGO_ID_RE.match("68cdc3873b12c6541a493ab5")
    assert not MONGO_ID_RE.match("19570")
    assert not MONGO_ID_RE.match("notvalid")


def test_resolve_oab_found_and_normalized():
    pool = FakePool(
        {
            "id": "68cdc38e57d3f6bbc721dec7",
            "oab": "19570",
            "nome": "Karoline Catananti",
            "condutor": "Karoline Catananti",
            "status": "active",
        }
    )

    result = asyncio.run(resolve_mentee("019570", pool))

    assert result is not None
    assert result["oab"] == "19570"
    sql, value = pool.conn.calls[0]
    assert "WHERE oab = $1" in sql
    assert "internal_test" in sql
    assert value == "19570"


def test_resolve_mongo_id_found():
    mentee_id = "68cdc3873b12c6541a493ab5"
    pool = FakePool(
        {
            "id": mentee_id,
            "oab": "277712",
            "nome": "Rafael e Keyla",
            "condutor": "Rafael e Keyla",
            "status": "active",
        }
    )

    result = asyncio.run(resolve_mentee(mentee_id, pool))

    assert result is not None
    assert result["id"] == mentee_id
    sql, value = pool.conn.calls[0]
    assert "WHERE mentee_id = $1" in sql
    assert value == mentee_id


def test_resolve_invalid():
    result = asyncio.run(resolve_mentee("xxx-yyy-zzz", FakePool()))

    assert result is None


def test_snapshot_returns_404_for_unknown_mentee(monkeypatch):
    async def fake_resolve(identifier, pg_pool=None):
        return None

    token = "test-token-spec144"
    monkeypatch.setenv(
        "MENTORHUB_HERMES_TOKEN_HASH",
        web_server.bcrypt.hashpw(token.encode(), web_server.bcrypt.gensalt()).decode(),
    )
    monkeypatch.setattr("hermes_cli.web_server.resolve_mentee", fake_resolve)

    client = TestClient(app)
    response = client.get(
        "/api/mentee/999999/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "mentee_not_found",
        "identifier": "999999",
    }
