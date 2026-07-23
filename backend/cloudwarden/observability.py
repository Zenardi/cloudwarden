"""Observability: Prometheus metrics, OpenTelemetry tracing, structured JSON logs.

Issue #54 (M13.4) — the operability layer behind the governance platform. This
module is deliberately free of any ``cloudwarden`` internal imports so it can be
imported from storage, remediation and the API without creating an import cycle.

  * **Metrics** (prometheus-client): a *policy executions* counter, a *remediation
    actions* counter and an execution-duration histogram, exposed at ``/metrics``.
  * **Tracing** (opentelemetry-sdk): :func:`span` wraps execution runs in OTel
    spans. No exporter is configured by default, so spans are recorded but never
    leave the process (no network egress) until an operator wires one up.
  * **Logging**: :class:`JsonLogFormatter` renders each record as a single JSON
    line carrying the request's correlation id; :class:`CorrelationIdMiddleware`
    binds one per request and echoes it back on the response.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.datastructures import Headers

SERVICE_NAME = "cloudwarden"
INSTRUMENTATION_NAME = "cloudwarden.observability"
CORRELATION_ID_HEADER = "X-Correlation-ID"

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
# Counters are named WITHOUT the ``_total`` suffix — prometheus-client appends it
# to the exposed sample (``cloudwarden_policy_executions_total``).
POLICY_EXECUTIONS = Counter(
    "cloudwarden_policy_executions",
    "Policy executions completed, labelled by terminal status.",
    ["status"],
)
REMEDIATION_ACTIONS = Counter(
    "cloudwarden_remediation_actions",
    "Remediation actions taken, labelled by action type and terminal status.",
    ["action", "status"],
)
EXECUTION_DURATION = Histogram(
    "cloudwarden_policy_execution_duration_seconds",
    "Wall-clock duration of a policy execution run, in seconds.",
)


def record_policy_execution(status: str) -> None:
    """Count one finished policy execution, keyed by its terminal ``status``."""
    POLICY_EXECUTIONS.labels(status=status).inc()


def record_remediation_action(action: str, status: str) -> None:
    """Count one remediation action, keyed by ``action`` type and terminal ``status``."""
    REMEDIATION_ACTIONS.labels(action=action, status=status).inc()


@contextmanager
def time_execution() -> Iterator[None]:
    """Observe the wall-clock duration of the wrapped execution run (seconds)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        EXECUTION_DURATION.observe(time.perf_counter() - start)


def metrics_payload() -> tuple[bytes, str]:
    """Return the Prometheus exposition body + its content type for ``/metrics``."""
    return generate_latest(), CONTENT_TYPE_LATEST


# --------------------------------------------------------------------------- #
# Correlation id — one per request, threaded through logs
# --------------------------------------------------------------------------- #
_correlation_id: ContextVar[str | None] = ContextVar("cloudwarden_correlation_id", default=None)


def get_correlation_id() -> str | None:
    """The correlation id bound to the current context, or ``None``."""
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token:
    """Bind ``value`` as the current correlation id; returns a reset token."""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    """Restore the correlation id to its value before :func:`set_correlation_id`."""
    _correlation_id.reset(token)


def new_correlation_id() -> str:
    """Generate, bind and return a fresh correlation id."""
    cid = uuid.uuid4().hex
    _correlation_id.set(cid)
    return cid


class CorrelationIdMiddleware:
    """Pure-ASGI middleware: bind a correlation id per request + echo it back.

    Reads an inbound ``X-Correlation-ID`` (so a caller's id survives across
    services) or mints one, binds it for the request's logs, and stamps it on the
    response. Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so it never
    buffers the response body — streaming endpoints keep streaming.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cid = Headers(scope=scope).get(CORRELATION_ID_HEADER) or uuid.uuid4().hex
        token = set_correlation_id(cid)

        async def send_with_correlation_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                raw = message.setdefault("headers", [])
                raw.append((CORRELATION_ID_HEADER.encode("latin-1"), cid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation_id)
        finally:
            reset_correlation_id(token)


# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #
class JsonLogFormatter(logging.Formatter):
    """Render a log record as a single structured JSON line.

    Includes the timestamp, level, logger name, message and the active correlation
    id (so every line a request emits can be traced back to it), plus a formatted
    traceback whenever the record carries exception info.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (structured logs everywhere)."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #
_provider: TracerProvider | None = None


def configure_tracing(exporter: SpanExporter | None = None) -> TracerProvider:
    """(Re)build the tracer provider, optionally exporting spans via ``exporter``.

    With no exporter, spans are recorded in-process but never emitted anywhere — no
    network egress. Tests inject an in-memory exporter to assert on spans.
    """
    global _provider
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    _provider = provider
    return provider


def get_tracer() -> trace.Tracer:
    """Return the CloudWarden tracer, lazily configuring a default provider."""
    if _provider is None:
        configure_tracing()
    assert _provider is not None
    return _provider.get_tracer(INSTRUMENTATION_NAME)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Open an OTel span around an execution run, tagged with ``attributes``."""
    with get_tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current
