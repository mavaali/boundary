"""Shared transient-failure retry for the LLM HTTP clients.

The model call is the least-reliable link in an unsupervised run: a provider can
return a 5xx/429 or drop the connection and then succeed on the next attempt. A
run that dies on the first blip wastes the whole envelope. This wraps a single
`send()` (a closure that performs one httpx request) with bounded exponential
backoff over the retryable cases, and leaves everything else exactly as it was —
a persistent 500 is returned to the caller to raise on, never masked as success.

`sleep` is injectable so the policy is unit-testable without real waiting.
"""
from __future__ import annotations

import time
from collections.abc import Callable

import httpx

# Statuses worth retrying: request timeout, rate limit, and the 5xx family
# (incl. Anthropic's 529 "overloaded"). A 4xx other than 408/429 is a client
# error — retrying won't help, so it is returned immediately.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 529})

# Transport-level failures that are worth retrying: timeouts and connection
# errors. A malformed-request or protocol error is not in here on purpose.
_RETRYABLE_EXC = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.WriteError)


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    retry_status: frozenset[int] = RETRYABLE_STATUS,
) -> httpx.Response:
    """Call ``send()`` up to ``attempts`` times with exponential backoff.

    Retries on a transport timeout/connection error or a response whose status is
    in ``retry_status``. Returns the final response (the caller still checks the
    status and raises on it). Re-raises the last transport exception if every
    attempt raised.
    """
    last_exc: Exception | None = None
    last_resp: httpx.Response | None = None
    for i in range(attempts):
        try:
            resp = send()
        except _RETRYABLE_EXC as e:
            last_exc = e
            if i == attempts - 1:
                raise
            sleep(min(base_delay * (2**i), max_delay))
            continue
        last_resp = resp
        if resp.status_code in retry_status and i < attempts - 1:
            sleep(min(base_delay * (2**i), max_delay))
            continue
        return resp
    # Exhausted retries on a retryable status: hand back the last response so the
    # caller raises its usual "<api> <status>: <body>" error.
    if last_resp is not None:
        return last_resp
    assert last_exc is not None  # unreachable: attempts >= 1
    raise last_exc
