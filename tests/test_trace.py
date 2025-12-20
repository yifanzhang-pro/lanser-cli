"""Tests for trace recording helpers."""

from __future__ import annotations

import json
from pathlib import Path

from lanser.trace import (
    JsonRpcTraceRecorder,
    load_trace_operations,
    load_trace_summary,
)


def test_trace_recorder_writes_events(tmp_path: Path) -> None:
    destination = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(destination)
    recorder.record_metadata("environment", {"workspace": "demo"})
    recorder.record_jsonrpc("client", {"jsonrpc": "2.0", "method": "initialize"})
    recorder.record_metadata("operation", {"operation": "definition", "ok": True})
    recorder.close()

    contents = destination.read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 3
    events = [json.loads(line) for line in contents]
    assert events[0]["event"] == "metadata"
    assert events[0]["kind"] == "environment"
    assert events[1]["event"] == "jsonrpc"
    assert events[1]["direction"] == "client"
    assert events[2]["data"]["operation"] == "definition"


def test_trace_summary_counts_operations(tmp_path: Path) -> None:
    destination = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(destination)
    recorder.record_metadata("environment", {"workspace": "demo"})
    recorder.record_metadata("settings", {"allowDirty": False})
    recorder.record_jsonrpc("client", {"jsonrpc": "2.0", "method": "initialize"})
    recorder.record_metadata(
        "operation",
        {"operation": "definition", "ok": True, "exitCode": 0},
    )
    recorder.record_metadata(
        "operation",
        {"operation": "definition", "ok": False, "exitCode": 64},
    )
    recorder.close()

    summary = load_trace_summary(destination)
    assert summary.total_events == 5
    assert summary.metadata_events == 4
    assert summary.jsonrpc_events == 1
    assert summary.operations_total == 2
    assert summary.environment == {"workspace": "demo"}
    assert summary.settings == {"allowDirty": False}
    assert len(summary.operations) == 1
    stats = summary.operations[0]
    assert stats.operation == "definition"
    assert stats.total == 2
    assert stats.ok == 1
    assert stats.failed == 1
    exit_counts = {entry.code: entry.count for entry in stats.exit_codes}
    assert exit_counts == {"0": 1, "64": 1}


def test_trace_operations_roundtrip(tmp_path: Path) -> None:
    destination = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(destination)
    recorder.record_metadata(
        "operation",
        {
            "operation": "definition",
            "ok": True,
            "message": "Definition served from cache.",
            "exitCode": 0,
            "selector": "py://pkg.mod#symbol",
            "selectorPayload": {"kind": "symbol", "value": "pkg.mod#symbol"},
            "payload": {"bundleId": "sha256:demo"},
            "metadata": {"cache": {"hit": True, "size": 1}},
        },
    )
    recorder.close()

    operations = load_trace_operations(destination)
    assert len(operations) == 1
    record = operations[0]
    assert record.operation == "definition"
    assert record.ok is True
    assert record.message == "Definition served from cache."
    assert record.exit_code == 0
    assert record.selector == "py://pkg.mod#symbol"
    assert record.selector_payload == {
        "kind": "symbol",
        "value": "pkg.mod#symbol",
    }
    assert record.payload == {"bundleId": "sha256:demo"}
    assert record.metadata == {"cache": {"hit": True, "size": 1}}
    assert record.timestamp.tzinfo is not None
