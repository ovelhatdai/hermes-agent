from gateway.platforms._custom import feedback_router as router


class DummyRequest:
    def __init__(self, authorization=None):
        self.headers = {}
        if authorization is not None:
            self.headers["Authorization"] = authorization


def test_validate_payload_accepts_valid_feedback():
    valid, error = router._validate_payload({
        "hash": "a3f2",
        "rating": "positive",
        "user_jid": "5551991987972@s.whatsapp.net",
        "context": {"model": "smoke"},
    })
    assert valid is True
    assert error == ""


def test_validate_payload_rejects_long_hash():
    valid, error = router._validate_payload({
        "hash": "muito-longo",
        "rating": "positive",
        "user_jid": "5551991987972@s.whatsapp.net",
    })
    assert valid is False
    assert error == "invalid_hash"


def test_validate_payload_rejects_bad_rating():
    valid, error = router._validate_payload({
        "hash": "a3f2",
        "rating": "bad",
        "user_jid": "5551991987972@s.whatsapp.net",
    })
    assert valid is False
    assert error == "invalid_rating"


def test_authorized_requires_bearer_token(monkeypatch):
    monkeypatch.setenv("HERMES_FEEDBACK_TOKEN", "secret")
    assert router._authorized(DummyRequest("Bearer secret")) is True
    assert router._authorized(DummyRequest("Bearer wrong")) is False


def test_authorized_fails_when_token_missing(monkeypatch):
    monkeypatch.delenv("HERMES_FEEDBACK_TOKEN", raising=False)
    assert router._authorized(DummyRequest("Bearer secret")) is False
