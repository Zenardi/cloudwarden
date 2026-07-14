"""Retry/backoff, status extraction, and cache helpers (offline)."""

from __future__ import annotations

import pytest

from cloudwarden import resilience as R


class _Resp:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class _HttpErr(Exception):
    def __init__(self, status: int, headers: dict | None = None) -> None:
        super().__init__(f"http {status}")
        self.response = _Resp(status, headers)


class ConnectionError(Exception):  # noqa: A001 - name intentionally matches the retryable set
    pass


def test_status_code_from_attr() -> None:
    exc = Exception()
    exc.status_code = 429  # type: ignore[attr-defined]
    assert R._status_code(exc) == 429


def test_status_code_from_response() -> None:
    assert R._status_code(_HttpErr(503)) == 503


def test_status_code_none() -> None:
    assert R._status_code(Exception("x")) is None


def test_is_conn_error() -> None:
    assert R._is_conn_error(ConnectionError())
    assert not R._is_conn_error(ValueError())


def test_retry_after_seconds() -> None:
    assert R._retry_after_seconds(_HttpErr(429, {"Retry-After": "2"})) == 2.0
    assert R._retry_after_seconds(Exception("x")) is None
    assert R._retry_after_seconds(_HttpErr(429, {"Retry-After": "bad"})) is None


def test_with_retry_succeeds_after_failures() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    @R.with_retry(max_attempts=4, base_delay=0.0, sleep=sleeps.append)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HttpErr(503)
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_with_retry_non_retryable_raises_immediately() -> None:
    calls = {"n": 0}

    @R.with_retry(max_attempts=3, sleep=lambda s: None)
    def bad() -> None:
        calls["n"] += 1
        raise _HttpErr(400)

    with pytest.raises(_HttpErr):
        bad()
    assert calls["n"] == 1


def test_with_retry_exhausts_and_raises_last() -> None:
    @R.with_retry(max_attempts=2, base_delay=0.0, sleep=lambda s: None)
    def always() -> None:
        raise _HttpErr(429, {"Retry-After": "0"})

    with pytest.raises(_HttpErr):
        always()


def test_with_retry_connection_error_retried() -> None:
    calls = {"n": 0}

    @R.with_retry(max_attempts=3, base_delay=0.0, sleep=lambda s: None)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError()
        return "ok"

    assert flaky() == "ok"


def test_cache_roundtrip(tmp_path) -> None:
    R.write_cache(str(tmp_path), "src", {"a": 1})
    assert R.read_cache(str(tmp_path), "src") == {"a": 1}
    assert R.read_cache(str(tmp_path), "missing") is None


def test_cache_read_corrupt(tmp_path) -> None:
    path = R.cache_path(str(tmp_path), "bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert R.read_cache(str(tmp_path), "bad") is None


def test_registry_snapshot() -> None:
    reg = R.StatusRegistry()
    reg.set("s", ok=True)
    reg.set("s2", ok=False, error="e", served_from_cache=True)
    snap = reg.snapshot()
    assert len(snap) == 2
    assert {s["name"] for s in snap} == {"s", "s2"}
