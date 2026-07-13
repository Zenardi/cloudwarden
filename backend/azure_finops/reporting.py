"""Governance reporting & export (M9.4).

Serialize the per-execution governance evidence (policy, subscription, status,
matches, timing) as **CSV** or **JSON**, streamed from ``repo.iter_governance_export``
so an arbitrarily large history is never materialized in memory. The same serializer
backs the HTTP export endpoint and the optional scheduled report.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .config import get_settings
from .storage import repository as repo

EXPORT_FORMATS = ("csv", "json")


def stream_export(session, fmt: str = "csv", batch_size: int = 500) -> Iterator[str]:
    """Return a streaming iterator of ``fmt`` text chunks for the export.

    ``csv`` yields the header then one line per row; ``json`` yields a single JSON
    array, one object at a time. Raises ``ValueError`` for an unsupported format.
    """
    if fmt == "csv":
        return _csv_stream(session, batch_size)
    if fmt == "json":
        return _json_stream(session, batch_size)
    raise ValueError(f"unsupported export format: {fmt!r}")


def _csv_stream(session, batch_size: int) -> Iterator[str]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(repo.GOVERNANCE_EXPORT_COLUMNS)
    yield _drain(buf)
    for row in repo.iter_governance_export(session, batch_size=batch_size):
        writer.writerow([_cell(row[col]) for col in repo.GOVERNANCE_EXPORT_COLUMNS])
        yield _drain(buf)


def _json_stream(session, batch_size: int) -> Iterator[str]:
    yield "["
    first = True
    for row in repo.iter_governance_export(session, batch_size=batch_size):
        yield ("" if first else ",") + json.dumps(row, default=str)
        first = False
    yield "]"


def _drain(buf: io.StringIO) -> str:
    value = buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    return value


def _cell(value: Any) -> Any:
    return "" if value is None else value


def stream_export_owning_session(fmt: str = "csv", batch_size: int = 500) -> Iterator[str]:
    """Stream the export from a freshly-opened session that lives for the whole
    stream — the session must outlive lazy consumption (e.g. a ``StreamingResponse``
    drained after the request handler returns)."""
    from .storage.db import session_scope

    with session_scope() as session:
        yield from stream_export(session, fmt, batch_size)


def generate_report(fmt: str = "csv", batch_size: int = 500) -> str:
    """Materialize the whole export as a single string (for the scheduled report)."""
    return "".join(stream_export_owning_session(fmt, batch_size))


def write_report(fmt: str = "csv") -> str:
    """Write a timestamped governance report under ``APP_DATA_DIR``; return its path."""
    settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(settings.app_data_dir, f"governance-report-{stamp}.{fmt}")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write(generate_report(fmt))
    return path
