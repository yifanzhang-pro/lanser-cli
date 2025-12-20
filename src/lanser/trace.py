"""Utilities for recording structured trace events during orchestrator runs."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict, abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "TraceEvent",
    "TraceRecorderProtocol",
    "JsonRpcTraceRecorder",
    "TraceExitCodeCount",
    "TraceOperationSummary",
    "TraceSummary",
    "TraceOperationRecord",
    "load_trace_summary",
    "load_trace_operations",
]


def _utcnow() -> datetime:
    """Return the current UTC time with timezone information."""

    return datetime.now(UTC)


def _normalise_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``payload`` coerced to a JSON-serialisable ``dict``."""

    if not isinstance(payload, Mapping):
        msg = "Trace payload must be a mapping"
        raise TypeError(msg)
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return json.loads(serialised)


class TraceRecorderProtocol(Protocol):
    """Structural typing protocol for trace recorders."""

    def record_metadata(self, kind: str, data: Mapping[str, Any]) -> None: ...

    def record_jsonrpc(self, direction: str, message: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...


class TraceEvent(BaseModel):
    """Immutable descriptor for a recorded trace event."""

    event: str
    timestamp: datetime = Field(default_factory=_utcnow)
    direction: str | None = None
    message: dict[str, Any] | None = None
    kind: str | None = None
    data: dict[str, Any] | None = None

    model_config = ConfigDict(frozen=True)


class JsonRpcTraceRecorder(TraceRecorderProtocol):
    """Write structured trace events to a JSONL destination."""

    def __init__(self, destination: Path | str) -> None:
        self._destination = Path(destination)
        self._lock = threading.Lock()
        self._handle: TextIO | None = None

    def _ensure_handle(self) -> TextIO:
        handle = self._handle
        if handle is None:
            self._destination.parent.mkdir(parents=True, exist_ok=True)
            handle = self._destination.open("a", encoding="utf-8")
            self._handle = handle
        return handle

    def _write_event(self, event: TraceEvent) -> None:
        handle = self._ensure_handle()
        payload = event.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()

    def record_jsonrpc(self, direction: str, message: Mapping[str, Any]) -> None:
        """Record a JSON-RPC message sent by the client or server."""

        event = TraceEvent(
            event="jsonrpc",
            direction=direction,
            message=_normalise_mapping(message),
        )
        self._write_event(event)

    def record_metadata(self, kind: str, data: Mapping[str, Any]) -> None:
        """Record supplemental metadata about the orchestrator run."""

        event = TraceEvent(event="metadata", kind=kind, data=_normalise_mapping(data))
        self._write_event(event)

    def close(self) -> None:
        """Flush and close the underlying file handle."""

        handle = self._handle
        if handle is None:
            return
        handle.flush()
        handle.close()
        self._handle = None


class TraceExitCodeCount(BaseModel):
    """Frequency count for an exit code observed in trace operations."""

    code: str
    count: int

    model_config = ConfigDict(frozen=True)


class TraceOperationSummary(BaseModel):
    """Summary statistics describing recorded operation outcomes."""

    operation: str
    total: int
    ok: int
    failed: int
    exit_codes: tuple[TraceExitCodeCount, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True)


class TraceSummary(BaseModel):
    """Aggregate statistics for a recorded trace file."""

    total_events: int = Field(alias="totalEvents")
    metadata_events: int = Field(alias="metadataEvents")
    jsonrpc_events: int = Field(alias="jsonrpcEvents")
    operations_total: int = Field(alias="operationsTotal")
    operations: tuple[TraceOperationSummary, ...] = Field(default_factory=tuple)
    first_event_at: datetime | None = Field(default=None, alias="firstEventAt")
    last_event_at: datetime | None = Field(default=None, alias="lastEventAt")
    environment: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, frozen=True)


def _new_exit_code_counts() -> OrderedDict[str, int]:
    """Return a typed mapping for exit code frequency tracking."""

    return OrderedDict()


@dataclass(slots=True)
class _MutableOperationStats:
    """Mutable accumulator for per-operation statistics."""

    total: int = 0
    ok: int = 0
    failed: int = 0
    exit_codes: OrderedDict[str, int] = field(default_factory=_new_exit_code_counts)


def _clone_payload(data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a defensive copy of ``data`` suitable for summary storage."""

    if data is None:
        return None
    encoded = json.dumps(data, ensure_ascii=False)
    return json.loads(encoded)


class TraceOperationRecord(BaseModel):
    """Recorded operation outcome captured in a trace log."""

    timestamp: datetime
    operation: str
    ok: bool
    message: str
    exit_code: int = Field(alias="exitCode")
    selector: str | None = None
    selector_payload: dict[str, Any] | None = Field(default=None, alias="selectorPayload")
    payload: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    def status_payload(self) -> dict[str, Any]:
        """Return a serialisable status payload matching CLI envelopes."""

        return {
            "ok": self.ok,
            "message": self.message,
            "exitCode": self.exit_code,
        }


def _load_trace_events(source: Path | str) -> tuple[TraceEvent, ...]:
    """Return all trace events stored at ``source``."""

    path = Path(source)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        msg = f"Failed to read trace file: {error}"
        raise ValueError(msg) from error

    events: list[TraceEvent] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = TraceEvent.model_validate_json(line)
        except ValidationError as error:
            details = ", ".join(item["msg"] for item in error.errors())
            msg = f"Line {line_number}: invalid trace event ({details})."
            raise ValueError(msg) from error
        events.append(event)
    return tuple(events)


def load_trace_summary(source: Path | str) -> TraceSummary:
    """Return aggregated statistics for the JSONL trace at ``source``."""

    events = _load_trace_events(source)

    total_events = 0
    metadata_events = 0
    jsonrpc_events = 0
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
    environment: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    operation_stats: OrderedDict[str, _MutableOperationStats] = OrderedDict()

    for event in events:
        total_events += 1
        if first_event_at is None or event.timestamp < first_event_at:
            first_event_at = event.timestamp
        if last_event_at is None or event.timestamp > last_event_at:
            last_event_at = event.timestamp

        if event.event == "metadata":
            metadata_events += 1
            if event.kind == "environment" and environment is None:
                environment = _clone_payload(event.data)
            elif event.kind == "settings" and settings is None:
                settings = _clone_payload(event.data)
            elif event.kind == "operation" and event.data is not None:
                operation_name = event.data.get("operation")
                if isinstance(operation_name, str):
                    stats = operation_stats.setdefault(
                        operation_name,
                        _MutableOperationStats(),
                    )
                    stats.total += 1
                    ok_value = bool(event.data.get("ok"))
                    if ok_value:
                        stats.ok += 1
                    else:
                        stats.failed += 1
                    exit_code_value = event.data.get("exitCode")
                    if isinstance(exit_code_value, int):
                        code_key = str(exit_code_value)
                        stats.exit_codes[code_key] = stats.exit_codes.get(code_key, 0) + 1
        elif event.event == "jsonrpc":
            jsonrpc_events += 1

    operation_summaries: list[TraceOperationSummary] = []
    operations_total = 0
    for name, stats in operation_stats.items():
        exit_codes = tuple(
            TraceExitCodeCount(code=code, count=count) for code, count in stats.exit_codes.items()
        )
        operations_total += stats.total
        operation_summaries.append(
            TraceOperationSummary(
                operation=name,
                total=stats.total,
                ok=stats.ok,
                failed=stats.failed,
                exit_codes=exit_codes,
            )
        )

    payload = {
        "totalEvents": total_events,
        "metadataEvents": metadata_events,
        "jsonrpcEvents": jsonrpc_events,
        "operationsTotal": operations_total,
        "operations": [summary.model_dump(mode="python") for summary in operation_summaries],
        "firstEventAt": first_event_at,
        "lastEventAt": last_event_at,
        "environment": environment,
        "settings": settings,
    }
    return TraceSummary.model_validate(payload)


def load_trace_operations(source: Path | str) -> tuple[TraceOperationRecord, ...]:
    """Return recorded operation outcomes from the trace at ``source``."""

    events = _load_trace_events(source)

    operations: list[TraceOperationRecord] = []
    for event in events:
        if event.event != "metadata" or event.kind != "operation":
            continue
        data = event.data
        if data is None:
            msg = "Operation metadata event is missing payload data."
            raise ValueError(msg)
        if not isinstance(data, abc.Mapping):
            msg = "Operation metadata event payload must be a JSON object."
            raise ValueError(msg)
        payload = json.loads(json.dumps(data, ensure_ascii=False))
        payload["timestamp"] = event.timestamp
        try:
            record = TraceOperationRecord.model_validate(payload)
        except ValidationError as error:
            details = ", ".join(item["msg"] for item in error.errors())
            msg = f"Operation metadata event is malformed: {details}"
            raise ValueError(msg) from error
        operations.append(record)

    return tuple(operations)
