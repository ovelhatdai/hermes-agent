import asyncio
import json
import time

from hermes_cli import mentee_cache
from hermes_cli.mentee_cache import cache_get, cache_key, cache_set


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}

    async def get(self, key):
        expires_at = self.expiries.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self.values.pop(key, None)
            self.expiries.pop(key, None)
            return None
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value
        self.expiries[key] = time.monotonic() + ttl


def _install_fake_redis(monkeypatch):
    redis_client = FakeRedis()
    monkeypatch.setattr(mentee_cache, "_redis_client", redis_client)
    return redis_client


def test_cache_set_get(monkeypatch):
    _install_fake_redis(monkeypatch)
    asyncio.run(cache_set("test_oab_99999", {"foo": "bar"}, ttl=10))
    result = asyncio.run(cache_get("test_oab_99999"))
    assert result == {"foo": "bar"}


def test_cache_miss(monkeypatch):
    _install_fake_redis(monkeypatch)
    result = asyncio.run(cache_get("nonexistent_99999"))
    assert result is None


def test_cache_ttl_expira(monkeypatch):
    _install_fake_redis(monkeypatch)
    asyncio.run(cache_set("short_ttl", {"x": 1}, ttl=1))
    time.sleep(1.1)
    result = asyncio.run(cache_get("short_ttl"))
    assert result is None


def test_cache_key_inclui_versao_schema():
    assert cache_key("19570") == "mentee:snapshot:v1:19570"


def test_cache_payload_json_parseable(monkeypatch):
    fake_redis = _install_fake_redis(monkeypatch)
    asyncio.run(cache_set("json_payload", {"foo": "bar"}, ttl=10))
    raw = fake_redis.values["mentee:snapshot:v1:json_payload"]
    assert json.loads(raw) == {"foo": "bar"}
