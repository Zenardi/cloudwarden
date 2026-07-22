"""Retry/backoff and cache-fallback helpers for flaky Azure APIs.

Ported in spirit from the sibling `invest-advisor/resilience.py`: retry on
429/5xx and connection errors (honouring `Retry-After` / `x-ms-ratelimit-*`),
plus an optional last-good disk cache so a transient failure degrades to stale
data instead of a hard error. A `StatusRegistry` records per-source health for a
Grafana/health panel.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger("cloudwarden.resilience")

T = TypeVar("T")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_CONN_ERROR_NAMES = {
    "ConnectionError",
    "ConnectTimeout",
    "ConnectError",
    "ReadTimeout",
    "Timeout",
    "TimeoutError",
    "TransportError",
    "ServiceRequestError",
    "ServiceResponseError",
}


@dataclass
class SourceStatus:
    name: str
    ok: bool = True
    last_error: str | None = None
    served_from_cache: bool = False
    updated_at: float = field(default_factory=time.time)


class StatusRegistry:
    def __init__(self) -> None:
        self._statuses: dict[str, SourceStatus] = {}

    def set(
        self, name: str, *, ok: bool, error: str | None = None, served_from_cache: bool = False
    ) -> None:
        self._statuses[name] = SourceStatus(name, ok, error, served_from_cache, time.time())

    def snapshot(self) -> list[dict[str, Any]]:
        return [vars(s) for s in self._statuses.values()]


REGISTRY = StatusRegistry()


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _is_conn_error(exc: Exception) -> bool:
    return type(exc).__name__ in _CONN_ERROR_NAMES


def _retry_after_seconds(exc: Exception) -> float | None:
    """Largest back-off hint (in seconds) the response advertises.

    Reads the standard ``Retry-After`` *and* Azure's service-specific
    ``x-ms-ratelimit-*-retry-after`` headers (e.g.
    ``x-ms-ratelimit-microsoft.costmanagement-entity-retry-after``). Cost Management
    429s frequently signal the wait *only* via the latter, so honouring just
    ``Retry-After`` made the retry fall back to a too-short exponential delay and give
    up long before the throttle window cleared. Take the max so we wait long enough.
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        items = list(headers.items())
    except AttributeError:
        return None
    hints: list[float] = []
    for name, value in items:
        if "retry-after" not in str(name).lower():
            continue
        try:
            hints.append(float(value))
        except (TypeError, ValueError):
            continue  # HTTP-date form or junk — ignore, fall back to backoff
    return max(hints) if hints else None


def with_retry(
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable:
    """Retry on 429/5xx and connection errors; honour Retry-After when present."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - reraised below
                    last = exc
                    code = _status_code(exc)
                    retryable = code in RETRYABLE_STATUS or (code is None and _is_conn_error(exc))
                    if not retryable or attempt == max_attempts:
                        raise
                    delay = _retry_after_seconds(exc)
                    if delay is None:
                        delay = min(base_delay * 2 ** (attempt - 1), max_delay)
                    delay += random.uniform(0, base_delay)
                    logger.warning(
                        "retry %d/%d in %.1fs (status=%s): %s",
                        attempt,
                        max_attempts,
                        delay,
                        code,
                        exc,
                    )
                    sleep(delay)
            assert last is not None
            raise last

        return wrapper

    return decorator


def cache_path(cache_dir: str, source: str) -> Path:
    return Path(cache_dir) / "cache" / f"{source}.json"


def write_cache(cache_dir: str, source: str, data: Any) -> None:
    path = cache_path(cache_dir, source)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time(), "data": data}, default=str))
    except OSError as exc:
        logger.debug("could not write cache for %s: %s", source, exc)


def read_cache(cache_dir: str, source: str) -> Any | None:
    path = cache_path(cache_dir, source)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["data"]
    except (OSError, ValueError, KeyError):
        return None
