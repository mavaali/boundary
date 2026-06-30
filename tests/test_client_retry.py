"""Transient-failure retry for the LLM HTTP clients.

The harness's whole value is completing bounded work unsupervised; the model call
is the least-reliable link and (before this) the only one without a retry. These
tests pin the shared retry helper's behavior with an injected sleep (no real
waiting) and a fake response/exception sequence (no network).
"""
from __future__ import annotations

import httpx
import pytest

from boundary.clients._http import RETRYABLE_STATUS, request_with_retry


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _sequence_sender(items):
    """Return a send() that yields each item in turn; an Exception item is raised."""
    it = iter(items)

    def send():
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return x

    return send


def test_returns_first_success_without_retry():
    calls = []
    sleeps = []
    send = _sequence_sender([_Resp(200)])

    def counting_send():
        calls.append(1)
        return send()

    r = request_with_retry(counting_send, sleep=sleeps.append)
    assert r.status_code == 200
    assert len(calls) == 1
    assert sleeps == []  # no retry, no sleep


def test_retries_on_retryable_status_then_succeeds():
    sleeps = []
    send = _sequence_sender([_Resp(503), _Resp(200)])
    r = request_with_retry(send, base_delay=2.0, sleep=sleeps.append)
    assert r.status_code == 200
    assert len(sleeps) == 1  # backed off once between the two attempts


def test_retries_on_transport_exception_then_succeeds():
    sleeps = []
    send = _sequence_sender([httpx.ConnectError("boom"), _Resp(200)])
    r = request_with_retry(send, sleep=sleeps.append)
    assert r.status_code == 200
    assert len(sleeps) == 1


def test_retries_on_timeout_exception():
    sleeps = []
    send = _sequence_sender([httpx.ReadTimeout("slow"), _Resp(200)])
    r = request_with_retry(send, sleep=sleeps.append)
    assert r.status_code == 200


def test_gives_up_and_returns_last_retryable_response():
    # All attempts return a retryable status -> caller gets the final response and
    # raises on it as before (we do NOT mask a persistent 500 as success).
    sleeps = []
    send = _sequence_sender([_Resp(500), _Resp(500), _Resp(500)])
    r = request_with_retry(send, attempts=3, sleep=sleeps.append)
    assert r.status_code == 500
    assert len(sleeps) == 2  # slept between attempts 1->2 and 2->3, not after the last


def test_reraises_last_exception_when_all_attempts_raise():
    sleeps = []
    send = _sequence_sender(
        [httpx.ConnectError("a"), httpx.ConnectError("b"), httpx.ConnectError("c")]
    )
    with pytest.raises(httpx.ConnectError):
        request_with_retry(send, attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2


def test_non_retryable_4xx_is_returned_immediately():
    # A 400/401/403 is a client error — retrying won't help. Return at once.
    sleeps = []
    send = _sequence_sender([_Resp(403)])
    r = request_with_retry(send, sleep=sleeps.append)
    assert r.status_code == 403
    assert sleeps == []


def test_backoff_is_exponential_and_capped():
    sleeps = []
    send = _sequence_sender([_Resp(503), _Resp(503), _Resp(503), _Resp(503), _Resp(200)])
    request_with_retry(send, attempts=5, base_delay=1.0, max_delay=4.0, sleep=sleeps.append)
    # 1, 2, 4, then capped at 4 (not 8)
    assert sleeps == [1.0, 2.0, 4.0, 4.0]


def test_retryable_status_set_covers_the_usual_suspects():
    for code in (408, 429, 500, 502, 503, 504, 529):
        assert code in RETRYABLE_STATUS
    for code in (200, 400, 401, 403, 404):
        assert code not in RETRYABLE_STATUS


# --- wiring: the clients actually route through the retry helper ---------------

class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_anthropic_client_retries_on_529(monkeypatch):
    import boundary.clients.anthropic as mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # no real sleeping during the test
    monkeypatch.setattr(mod, "request_with_retry", _patch_sleep(mod.request_with_retry))

    calls = []
    ok = _FakeResp(200, {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })

    def fake_post(*a, **k):
        calls.append(1)
        return _FakeResp(529, {"error": "overloaded"}) if len(calls) == 1 else ok

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    client = mod.AnthropicClient(model="claude-sonnet-4.6")
    resp = client.chat([mod.Message(role="user", content="hello")])
    assert resp.message.content == "hi"
    assert len(calls) == 2  # 529 then 200


def _patch_sleep(fn):
    # Wrap request_with_retry so its default sleep is a no-op in tests.
    import functools

    @functools.wraps(fn)
    def wrapper(send, **kw):
        kw.setdefault("sleep", lambda _s: None)
        return fn(send, **kw)

    return wrapper
