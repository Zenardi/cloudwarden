"""Observability contract (issue #54, M13.4).

Executable spec for the operability layer: Prometheus metrics, OpenTelemetry
tracing, structured JSON logs with correlation ids, and the ``/metrics`` +
``/ready`` probes.

Deterministic and offline: metrics/logging/tracing are exercised directly, the
``/ready`` DB probe is dependency-overridden (or monkeypatched), and no test
needs a live Postgres, Docker, or the network.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from cloudwarden import observability
from cloudwarden.api import main
from cloudwarden.api.main import app

# --------------------------------------------------------------------------- #
# Metrics — /metrics endpoint + execution/remediation counters
# --------------------------------------------------------------------------- #


def test_metrics_endpoint_exposes_execution_and_remediation_counters() -> None:
    # Arrange — observe one of each so the labelled samples materialise.
    observability.record_policy_execution("succeeded")
    observability.record_remediation_action("stop", "executed")
    # Act
    resp = TestClient(app).get("/metrics")
    # Assert — both counters are exposed for scraping.
    assert resp.status_code == 200
    assert "cloudwarden_policy_executions_total" in resp.text
    assert "cloudwarden_remediation_actions_total" in resp.text


def test_metrics_endpoint_uses_prometheus_content_type() -> None:
    # Arrange / Act
    resp = TestClient(app).get("/metrics")
    # Assert — Prometheus text exposition format.
    assert resp.headers["content-type"].startswith("text/plain")


def test_record_policy_execution_increments_counter() -> None:
    # Arrange
    name, labels = "cloudwarden_policy_executions_total", {"status": "failed"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    # Act
    observability.record_policy_execution("failed")
    # Assert
    assert REGISTRY.get_sample_value(name, labels) == before + 1


def test_record_remediation_action_increments_counter() -> None:
    # Arrange
    name = "cloudwarden_remediation_actions_total"
    labels = {"action": "delete", "status": "executed"}
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    # Act
    observability.record_remediation_action("delete", "executed")
    # Assert
    assert REGISTRY.get_sample_value(name, labels) == before + 1


def test_time_execution_records_a_duration_sample() -> None:
    # Arrange
    name = "cloudwarden_policy_execution_duration_seconds_count"
    before = REGISTRY.get_sample_value(name) or 0.0
    # Act
    with observability.time_execution():
        pass
    # Assert — one more timed observation was recorded.
    assert REGISTRY.get_sample_value(name) == before + 1


# --------------------------------------------------------------------------- #
# Readiness — /ready reflects DB reachability (200 healthy / 503 down)
# --------------------------------------------------------------------------- #


def test_ready_returns_200_when_db_healthy() -> None:
    # Arrange — a probe that succeeds (DB reachable).
    app.dependency_overrides[main.get_readiness_checker] = lambda: lambda: None
    try:
        # Act
        resp = TestClient(app).get("/ready")
        # Assert
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
        assert resp.json()["checks"]["database"] == "ok"
    finally:
        app.dependency_overrides.clear()


def test_ready_returns_503_when_db_unreachable() -> None:
    # Arrange — a probe that raises (DB down).
    def checker():
        def _probe() -> None:
            raise RuntimeError("connection refused")

        return _probe

    app.dependency_overrides[main.get_readiness_checker] = checker
    try:
        # Act
        resp = TestClient(app).get("/ready")
        # Assert
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"
        assert resp.json()["checks"]["database"] == "unavailable"
    finally:
        app.dependency_overrides.clear()


def test_get_readiness_checker_returns_db_probe() -> None:
    # Arrange / Act / Assert — the default probe is the real DB check.
    assert main.get_readiness_checker() is main.check_database_ready


def test_check_database_ready_executes_select_1(monkeypatch) -> None:
    # Arrange — capture the SQL run against a fake session.
    executed: list[str] = []

    class _Session:
        def execute(self, stmt) -> None:
            executed.append(str(stmt))

    @contextlib.contextmanager
    def _scope():
        yield _Session()

    monkeypatch.setattr(main, "session_scope", _scope)
    # Act
    main.check_database_ready()
    # Assert — a trivial liveness query was issued.
    assert executed == ["SELECT 1"]


def test_check_database_ready_raises_when_db_down(monkeypatch) -> None:
    # Arrange — the session scope itself fails to open (DB unreachable).
    @contextlib.contextmanager
    def _scope():
        raise RuntimeError("db down")
        yield  # pragma: no cover - unreachable, satisfies the generator contract

    monkeypatch.setattr(main, "session_scope", _scope)
    # Act / Assert — the failure propagates so /ready can report 503.
    with pytest.raises(RuntimeError):
        main.check_database_ready()


# --------------------------------------------------------------------------- #
# Structured JSON logs with correlation ids
# --------------------------------------------------------------------------- #


def test_json_log_formatter_emits_valid_json() -> None:
    # Arrange
    formatter = observability.JsonLogFormatter()
    record = logging.LogRecord("cloudwarden.test", logging.INFO, __file__, 10, "hello", None, None)
    # Act
    payload = json.loads(formatter.format(record))
    # Assert
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cloudwarden.test"
    assert "timestamp" in payload


def test_json_log_includes_correlation_id() -> None:
    # Arrange — a correlation id is bound to the context.
    token = observability.set_correlation_id("corr-123")
    try:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        # Act
        payload = json.loads(observability.JsonLogFormatter().format(record))
        # Assert
        assert payload["correlation_id"] == "corr-123"
    finally:
        observability.reset_correlation_id(token)


def test_json_log_includes_exception_when_present() -> None:
    # Arrange — a record carrying exception info.
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    # Act
    payload = json.loads(observability.JsonLogFormatter().format(record))
    # Assert — the traceback is embedded as structured data.
    assert "boom" in payload["exception"]


def test_configure_logging_installs_json_formatter() -> None:
    # Arrange — preserve the root logger so the suite's logging is untouched.
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        # Act
        observability.configure_logging()
        # Assert
        assert any(isinstance(h.formatter, observability.JsonLogFormatter) for h in root.handlers)
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# --------------------------------------------------------------------------- #
# Correlation-id propagation (middleware + context helpers)
# --------------------------------------------------------------------------- #


def test_correlation_id_added_to_response_header() -> None:
    # Arrange / Act
    resp = TestClient(app).get("/health")
    # Assert — a generated 32-char hex correlation id is returned to the caller.
    cid = resp.headers.get("x-correlation-id")
    assert cid is not None and len(cid) == 32


def test_correlation_id_echoed_from_request_header() -> None:
    # Arrange / Act — the caller supplies its own id (distributed tracing).
    resp = TestClient(app).get("/health", headers={"X-Correlation-ID": "trace-abc"})
    # Assert — it is preserved end-to-end.
    assert resp.headers.get("x-correlation-id") == "trace-abc"


def test_new_correlation_id_binds_to_context() -> None:
    # Arrange / Act
    cid = observability.new_correlation_id()
    # Assert
    assert observability.get_correlation_id() == cid


def test_correlation_middleware_passes_through_non_http_scope() -> None:
    # Arrange — a non-HTTP (e.g. lifespan) scope must be forwarded untouched.
    seen: dict[str, str] = {}

    async def _inner(scope, receive, send) -> None:
        seen["type"] = scope["type"]

    middleware = observability.CorrelationIdMiddleware(_inner)

    # Act
    import asyncio

    asyncio.run(middleware({"type": "lifespan"}, None, None))
    # Assert
    assert seen["type"] == "lifespan"


# --------------------------------------------------------------------------- #
# OpenTelemetry tracing — execution runs create spans
# --------------------------------------------------------------------------- #


def test_span_is_exported_with_its_name() -> None:
    # Arrange — capture spans in memory (no exporter reaches the network).
    exporter = InMemorySpanExporter()
    observability.configure_tracing(exporter=exporter)
    # Act
    with observability.span("policy.execute"):
        pass
    # Assert
    assert "policy.execute" in [s.name for s in exporter.get_finished_spans()]


def test_span_records_attributes() -> None:
    # Arrange
    exporter = InMemorySpanExporter()
    observability.configure_tracing(exporter=exporter)
    # Act
    with observability.span("run.subscription", subscription_id="sub-1"):
        pass
    # Assert
    target = next(s for s in exporter.get_finished_spans() if s.name == "run.subscription")
    assert target.attributes["subscription_id"] == "sub-1"


def test_get_tracer_lazily_configures_a_default_provider() -> None:
    # Arrange — no provider configured yet.
    observability._provider = None
    # Act
    tracer = observability.get_tracer()
    # Assert — a usable tracer is returned without an explicit configure call.
    assert hasattr(tracer, "start_as_current_span")
