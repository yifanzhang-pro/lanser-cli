"""CLI integration tests for Typer commands."""

from __future__ import annotations

import ast
import bisect
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from platform import python_version
from types import MethodType
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse

import pytest
from typer.testing import CliRunner

from lanser import cli
from lanser.environment import gather_environment
from lanser.exit_codes import ExitCode
from lanser.orchestrator import LSPOrchestrator
from lanser.pyright import PyrightHandshake
from lanser.pyright_version import PYRIGHT_VERSION, PYRIGHT_VERSION_SUPPORT
from lanser.trace import JsonRpcTraceRecorder


PYTHON_VERSION = python_version()


def _environment_payload(workspace: str) -> dict[str, object]:
    return {
        "schemaVersion": "env-meta.v1",
        "workspace": workspace,
        "positionEncoding": "utf-16",
        "frozenSnapshot": False,
        "workspaceSnapshotId": "sha256:abc",
        "pythonVersion": PYTHON_VERSION,
        "pythonExecutable": "/usr/bin/python3",
        "platform": "Linux",
        "cwd": workspace,
        "projectFiles": [],
        "git": {"root": workspace, "head": "deadbeef", "dirty": False},
    }


@pytest.fixture(autouse=True)
def fake_cli_pyright(monkeypatch: pytest.MonkeyPatch) -> None:
    handshake = PyrightHandshake(
        result={
            "capabilities": {"positionEncoding": "utf-16"},
            "serverInfo": {"name": "fake-pyright", "version": "0.0"},
        }
    )

    class _FakeSession:
        def __init__(self) -> None:
            self._handshake = handshake
            self.shutdown_called = False

        @property
        def handshake(self) -> PyrightHandshake:
            return self._handshake

        def notify(self, method: str, params: Any) -> None:  # pragma: no cover - noop
            return None

        def request(
            self,
            method: str,
            params: Any,
            timeout: float | None = None,
            *,
            cancellable: bool = False,
        ) -> Any:
            if method == "textDocument/definition":
                text_document = params.get("textDocument", {}) if isinstance(params, dict) else {}
                uri = text_document.get("uri")
                return [
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                    }
                ]
            if method == "textDocument/prepareRename":
                return {
                    "range": {
                        "start": {"line": 0, "character": 4},
                        "end": {"line": 0, "character": 7},
                    },
                    "placeholder": "foo",
                }
            if method == "textDocument/references":
                text_document = params.get("textDocument", {}) if isinstance(params, dict) else {}
                uri = text_document.get("uri")
                return [
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": 0, "character": 4},
                            "end": {"line": 0, "character": 7},
                        },
                    },
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": 1, "character": 4},
                            "end": {"line": 1, "character": 7},
                        },
                    },
                ]
            if method == "textDocument/hover":
                return {
                    "contents": {"kind": "markdown", "value": "**hover**"},
                    "range": {
                        "start": {"line": 0, "character": 4},
                        "end": {"line": 0, "character": 7},
                    },
                }
            if method == "textDocument/documentSymbol":
                text_document = params.get("textDocument", {}) if isinstance(params, dict) else {}
                uri = text_document.get("uri")
                if not isinstance(uri, str):
                    return []
                parsed = urlparse(uri)
                path = Path(unquote(parsed.path))
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    return []

                entries: list[dict[str, Any]] = []
                stack: list[tuple[int, dict[str, Any]]] = []
                lines = source.splitlines()
                for index, line in enumerate(lines):
                    stripped = line.lstrip()
                    if not stripped.startswith("def "):
                        continue
                    name_portion = stripped[4:]
                    name = re.split(r"\W", name_portion, maxsplit=1)[0]
                    indent = len(line) - len(stripped)
                    start_char = indent + 4
                    end_char = start_char + len(name)
                    entry = {
                        "name": name,
                        "kind": 12,
                        "detail": "function",
                        "range": {
                            "start": {"line": index, "character": 0},
                            "end": {"line": index, "character": len(line)},
                        },
                        "selectionRange": {
                            "start": {"line": index, "character": start_char},
                            "end": {"line": index, "character": end_char},
                        },
                        "children": [],
                    }
                    while stack and indent <= stack[-1][0]:
                        stack.pop()
                    if stack:
                        stack[-1][1]["children"].append(entry)
                    else:
                        entries.append(entry)
                    stack.append((indent, entry))
                return entries
            if method == "textDocument/diagnostic":
                text_document = params.get("textDocument", {}) if isinstance(params, dict) else {}
                uri = text_document.get("uri")
                if not isinstance(uri, str):
                    return {"items": []}
                parsed = urlparse(uri)
                path = Path(unquote(parsed.path))
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    return {"items": []}
                try:
                    ast.parse(source)
                except SyntaxError as error:
                    line_index = max((error.lineno or 1) - 1, 0)
                    char_index = max((error.offset or 1) - 1, 0)
                    return {
                        "items": [
                            {
                                "uri": uri,
                                "diagnostics": [
                                    {
                                        "range": {
                                            "start": {"line": line_index, "character": char_index},
                                            "end": {
                                                "line": line_index,
                                                "character": char_index + 1,
                                            },
                                        },
                                        "message": str(error),
                                        "severity": 1,
                                        "code": "syntax-error",
                                    }
                                ],
                            }
                        ]
                    }
                return {"items": []}
            if method == "textDocument/rename":
                if not isinstance(params, dict):
                    return None
                text_document = params.get("textDocument")
                position = params.get("position")
                new_name = params.get("newName")
                if not (
                    isinstance(text_document, dict)
                    and isinstance(position, dict)
                    and isinstance(new_name, str)
                ):
                    return None
                uri = text_document.get("uri")
                if not isinstance(uri, str):
                    return None
                parsed = urlparse(uri)
                path = Path(unquote(parsed.path))
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    return {"documentChanges": []}

                line_value = position.get("line")
                char_value = position.get("character")
                if not (isinstance(line_value, int) and isinstance(char_value, int)):
                    return {"documentChanges": []}

                lines = source.splitlines(keepends=True)
                if not lines:
                    return {"documentChanges": []}
                if line_value >= len(lines):
                    return {"documentChanges": []}

                offsets: list[int] = [0]
                for entry in lines:
                    offsets.append(offsets[-1] + len(entry))

                position_index = offsets[line_value] + char_value
                start_index = position_index
                while start_index > 0 and (
                    source[start_index - 1].isalnum() or source[start_index - 1] == "_"
                ):
                    start_index -= 1
                end_index = position_index
                while end_index < len(source) and (
                    source[end_index].isalnum() or source[end_index] == "_"
                ):
                    end_index += 1
                symbol = source[start_index:end_index] or "foo"

                pattern = re.compile(rf"\b{re.escape(symbol)}\b")
                edits: list[dict[str, Any]] = []
                for match in pattern.finditer(source):
                    start = match.start()
                    end = match.end()
                    line_idx = bisect.bisect_right(offsets, start) - 1
                    column = start - offsets[line_idx]
                    end_line_idx = bisect.bisect_right(offsets, end) - 1
                    end_column = end - offsets[end_line_idx]
                    edits.append(
                        {
                            "range": {
                                "start": {"line": line_idx, "character": column},
                                "end": {"line": end_line_idx, "character": end_column},
                            },
                            "newText": new_name,
                        }
                    )

                return {
                    "documentChanges": [
                        {"textDocument": {"uri": uri}, "edits": edits},
                    ]
                }
            return None

        def drain_notifications(self, *, method: str | None = None) -> list[dict[str, Any]]:
            return []

        def wait_for_notifications(
            self, *, method: str, timeout: float = 5.0
        ) -> list[dict[str, Any]]:
            return []

        def refresh_configuration(self) -> None:  # pragma: no cover - noop
            return None

        def shutdown(self) -> None:
            self.shutdown_called = True

    monkeypatch.setattr(
        "lanser.orchestrator.create_pyright_session",
        lambda workspace, recorder=None: _FakeSession(),
    )

    original_gather = gather_environment

    def _gather(workspace: Path):
        snapshot = original_gather(workspace)
        if snapshot.git_root is None:
            return snapshot.model_copy(update={"git_dirty": None})
        return snapshot

    monkeypatch.setattr("lanser.orchestrator.gather_environment", _gather)


def test_config_show_json_respects_workspace(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--workspace", str(tmp_path), "config", "show", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["workspace"] == str(tmp_path)
    assert data["allow_paths"] == []
    assert data["deny_paths"] == []
    assert data["trace_file"] is None


def test_definition_traces_are_written(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "--trace-file",
            str(trace_path),
            "def",
            "py://pkg.mod#symbol",
            "--json",
        ],
    )
    assert result.exit_code == ExitCode.NOT_FOUND.value
    assert trace_path.exists(), "Expected trace file to be created"
    envelope = json.loads(result.stdout)
    status_block = envelope["status"]
    assert status_block["exitCode"] == ExitCode.NOT_FOUND.value
    assert status_block["ok"] is False
    lines = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "Expected trace file to contain events"
    events = [json.loads(line) for line in lines]
    kinds = {event.get("kind") for event in events if event.get("event") == "metadata"}
    assert "environment" in kinds
    assert "operation" in kinds
    operation_events = [event for event in events if event.get("kind") == "operation"]
    assert any(
        entry.get("data", {}).get("operation") == "definition" for entry in operation_events
    ), "Expected trace to include definition operation metadata"
    assert any(
        entry.get("data", {}).get("exitCode") == ExitCode.NOT_FOUND.value
        for entry in operation_events
    ), "Expected trace to include the not-found exit code"


def test_definition_cli_refuses_stub_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n", encoding="utf-8")

    original_builder = cli._build_orchestrator

    def _build_with_stub(
        config: cli.RuntimeConfig,
        *,
        progress_handler: Any | None = None,
    ) -> LSPOrchestrator:
        orchestrator = original_builder(config, progress_handler=progress_handler)

        def _force_stub(
            self: LSPOrchestrator,
            selector: Any,
            *,
            context: Any = None,
        ) -> Mapping[str, Any]:
            self._record_pyright_source("stub")
            return self._definition_result_stub(selector, context=context)

        setattr(orchestrator, "_definition_result", MethodType(_force_stub, orchestrator))
        return orchestrator

    monkeypatch.setattr(cli, "_build_orchestrator", _build_with_stub)

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "def",
            "py://pkg.mod#foo:def",
            "--json",
        ],
    )

    assert result.exit_code == ExitCode.LS_CRASH.value

    envelope = json.loads(result.stdout)
    status = envelope["status"]
    assert status["ok"] is False
    assert status["exitCode"] == ExitCode.LS_CRASH.value
    assert "stub analysis fallback" in status["message"]

    payload = envelope["payload"]
    assert payload["error"]["kind"] == "stub-fallback-denied"

    metadata = envelope["metadata"]
    assert metadata["pyright"]["connected"] is True


def test_trace_inspect_reports_summary(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(trace_path)
    recorder.record_metadata("environment", {"workspace": "demo"})
    recorder.record_metadata("settings", {"allowDirty": True})
    recorder.record_metadata(
        "operation",
        {"operation": "definition", "ok": True, "exitCode": 0},
    )
    recorder.record_metadata(
        "operation",
        {"operation": "diagnostics", "ok": False, "exitCode": 3},
    )
    recorder.close()

    runner = CliRunner()
    result = runner.invoke(cli.app, ["trace", "inspect", str(trace_path)])
    assert result.exit_code == 0
    assert "Operations recorded: 2" in result.stdout
    assert "definition" in result.stdout
    assert "diagnostics" in result.stdout

    json_result = runner.invoke(
        cli.app,
        ["trace", "inspect", str(trace_path), "--json"],
    )
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["totalEvents"] == 4


def _write_sample_trace(trace_path: Path) -> None:
    recorder = JsonRpcTraceRecorder(trace_path)
    recorder.record_metadata(
        "operation",
        {
            "operation": "definition",
            "ok": True,
            "exitCode": ExitCode.OK.value,
            "message": "ok",
            "selector": "py://pkg.alpha#symbol",
        },
    )
    recorder.record_metadata(
        "operation",
        {
            "operation": "definition",
            "ok": False,
            "exitCode": ExitCode.LS_TIMEOUT.value,
            "message": "error",
            "selector": "py://pkg.beta#symbol",
        },
    )
    recorder.close()


def test_trace_list_filters_by_status(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "list", str(trace_path), "--operation", "definition", "--status", "failed"],
    )
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert any("failed" in line for line in lines)
    assert all("ok" not in line for line in lines if line.startswith("  ["))


def test_trace_list_rejects_invalid_status(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "list", str(trace_path), "--status", "pending"],
    )
    assert result.exit_code != 0
    assert "Status must be 'ok' or 'failed'." in result.stdout


def test_trace_list_filters_by_selector(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "list", str(trace_path), "--selector", "beta"],
    )
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert any("py://pkg.beta#symbol" in line for line in lines)
    assert all("py://pkg.alpha#symbol" not in line for line in lines if line.startswith("  ["))


def test_trace_show_supports_status_filter(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "show", str(trace_path), "--status", "failed"],
    )
    assert result.exit_code == 0
    assert "Status: failed" in result.stdout
    assert "Message: error" in result.stdout


def test_trace_show_supports_selector_filter(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "show", str(trace_path), "--selector", "beta"],
    )
    assert result.exit_code == 0
    assert "Selector: py://pkg.beta#symbol" in result.stdout
    assert "Status: failed" in result.stdout


def test_trace_replay_supports_status_filter(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "replay", str(trace_path), "--status", "failed", "--json"],
    )
    assert result.exit_code == ExitCode.LS_TIMEOUT.value
    payload = json.loads(result.stdout)
    assert payload["status"]["ok"] is False
    assert payload["status"]["exitCode"] == ExitCode.LS_TIMEOUT.value


def test_trace_replay_supports_selector_filter(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    _write_sample_trace(trace_path)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["trace", "replay", str(trace_path), "--selector", "beta", "--json"],
    )
    assert result.exit_code == ExitCode.LS_TIMEOUT.value
    payload = json.loads(result.stdout)
    assert payload["status"]["ok"] is False
    assert payload["status"]["exitCode"] == ExitCode.LS_TIMEOUT.value


def test_trace_list_outputs_operations(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(trace_path)
    recorder.record_metadata(
        "operation",
        {
            "operation": "definition",
            "ok": True,
            "exitCode": ExitCode.OK,
            "message": "Definition completed.",
            "selector": "py://pkg.mod#symbol",
        },
    )
    recorder.record_metadata(
        "operation",
        {
            "operation": "diagnostics",
            "ok": False,
            "exitCode": ExitCode.NOT_FOUND,
            "message": "Diagnostics failed.",
        },
    )
    recorder.record_metadata(
        "operation",
        {
            "operation": "definition",
            "ok": True,
            "exitCode": ExitCode.OK,
            "message": "Definition cached.",
        },
    )
    recorder.close()

    runner = CliRunner()
    result = runner.invoke(cli.app, ["trace", "list", str(trace_path)])
    assert result.exit_code == 0
    output = result.stdout.splitlines()
    assert any("[0] definition #0" in line for line in output)
    assert any("[1] diagnostics #0" in line for line in output)
    assert any("[2] definition #1" in line for line in output)

    json_result = runner.invoke(
        cli.app,
        [
            "trace",
            "list",
            str(trace_path),
            "--operation",
            "definition",
            "--json",
        ],
    )
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert [entry["index"] for entry in payload] == [0, 2]
    assert [entry["operationIndex"] for entry in payload] == [0, 1]

    exit_filtered = runner.invoke(
        cli.app,
        [
            "trace",
            "list",
            str(trace_path),
            "--exit-code",
            str(ExitCode.NOT_FOUND.value),
        ],
    )
    assert exit_filtered.exit_code == 0
    assert "diagnostics #0" in exit_filtered.stdout
    assert "definition" not in exit_filtered.stdout


def test_trace_show_prints_operation_details(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(trace_path)
    recorder.record_metadata(
        "operation",
        {
            "operation": "definition",
            "ok": True,
            "exitCode": ExitCode.OK,
            "message": "Definition completed.",
            "selector": "py://pkg.mod#symbol",
            "selectorPayload": {"kind": "symbol", "value": "pkg.mod#symbol"},
            "payload": {"bundleId": "sha256:demo"},
            "metadata": {"cache": {"hit": True}},
        },
    )
    recorder.close()

    runner = CliRunner()
    result = runner.invoke(cli.app, ["trace", "show", str(trace_path)])
    assert result.exit_code == 0
    output = result.stdout
    assert "Operation: definition" in output
    assert "Status: ok (exit 0)" in output
    assert "Message: Definition completed." in output
    assert "Selector: py://pkg.mod#symbol" in output
    assert '"bundleId": "sha256:demo"' in output
    assert '"cache": {"hit": true}' in output


def test_trace_show_json_outputs_record(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(trace_path)
    recorder.record_metadata(
        "operation",
        {
            "operation": "diagnostics",
            "ok": False,
            "exitCode": ExitCode.NOT_FOUND,
            "message": "Diagnostics failed.",
        },
    )
    recorder.close()

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "trace",
            "show",
            str(trace_path),
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["operation"] == "diagnostics"
    assert payload["exitCode"] == ExitCode.NOT_FOUND
    assert payload["ok"] is False
    assert payload["message"] == "Diagnostics failed."


def test_trace_replay_reconstructs_json_output(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    runner = CliRunner()
    definition_result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "--trace-file",
            str(trace_path),
            "def",
            "py://pkg.mod#symbol",
            "--json",
        ],
    )
    assert definition_result.exit_code == ExitCode.NOT_FOUND.value
    recorded_payload = json.loads(definition_result.stdout)

    replay_result = runner.invoke(
        cli.app,
        [
            "trace",
            "replay",
            str(trace_path),
            "--operation",
            "definition",
            "--json",
        ],
    )
    assert replay_result.exit_code == ExitCode.NOT_FOUND.value
    replay_payload = json.loads(replay_result.stdout)
    assert replay_payload == recorded_payload


def test_schema_list_json_reports_available_schemas() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["schema", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {entry["name"] for entry in payload}
    assert "analysis-bundle" in names
    assert "environment-metadata" in names


def test_schema_show_outputs_valid_json_schema() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["schema", "show", "analysis-bundle"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["$schema"].endswith("2020-12/schema")
    properties = schema.get("properties", {})
    assert "bundleId" in properties
    assert "environment" in properties


def test_schema_export_writes_schema_file(tmp_path: Path) -> None:
    destination = tmp_path / "analysis.schema.json"
    runner = CliRunner()

    result = runner.invoke(
        cli.app,
        ["schema", "export", "analysis-bundle", str(destination)],
    )

    assert result.exit_code == 0
    assert "Wrote analysis-bundle schema" in result.stdout
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["$schema"].endswith("2020-12/schema")
    assert isinstance(payload.get("title"), str)


def test_schema_export_requires_force_for_existing_target(tmp_path: Path) -> None:
    destination = tmp_path / "analysis.schema.json"
    destination.write_text("{}", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        cli.app,
        ["schema", "export", "analysis-bundle", str(destination)],
    )

    assert result.exit_code != 0
    assert "pass --force" in result.stdout


def test_schema_validate_reports_success(tmp_path: Path) -> None:
    payload = {
        "schemaVersion": "env-meta.v1",
        "workspace": str(tmp_path),
        "positionEncoding": "utf-16",
        "frozenSnapshot": False,
        "workspaceSnapshotId": "sha256:123",
        "pythonVersion": PYTHON_VERSION,
        "pythonExecutable": "/usr/bin/python3",
        "platform": "Linux",
        "cwd": str(tmp_path),
        "projectFiles": [],
        "git": {"root": None, "head": None, "dirty": False},
    }
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["schema", "validate", "environment-metadata", str(payload_file)]
    )
    assert result.exit_code == 0
    assert "environment-metadata payload is valid" in result.stdout


def test_schema_validate_reports_errors(tmp_path: Path) -> None:
    invalid_payload = {
        "schemaVersion": "env-meta.v1",
        "workspace": str(tmp_path),
        "positionEncoding": "utf-16",
    }
    payload_file = tmp_path / "invalid.json"
    payload_file.write_text(json.dumps(invalid_payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["schema", "validate", "environment-metadata", str(payload_file)]
    )
    assert result.exit_code == 1
    assert "Validation failed" in result.stdout


def test_schema_validate_batch_reports_success(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    payload_a = payload_dir / "env-a.json"
    payload_b = payload_dir / "env-b.json"
    payload_a.write_text(json.dumps(_environment_payload(str(tmp_path))), encoding="utf-8")
    payload_b.write_text(json.dumps(_environment_payload(str(tmp_path))), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["schema", "validate-batch", "environment-metadata", str(payload_dir)]
    )

    assert result.exit_code == 0
    assert "Validated 2 payload(s)" in result.stdout
    assert "Passed: 2" in result.stdout
    assert "Failed: 0" in result.stdout


def test_schema_validate_batch_reports_failures(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    valid_payload = payload_dir / "env-valid.json"
    invalid_payload = payload_dir / "env-invalid.json"
    valid_payload.write_text(json.dumps(_environment_payload(str(tmp_path))), encoding="utf-8")
    invalid_data = _environment_payload(str(tmp_path))
    invalid_data.pop("workspaceSnapshotId")
    invalid_payload.write_text(json.dumps(invalid_data), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["schema", "validate-batch", "environment-metadata", str(payload_dir)]
    )

    assert result.exit_code == 1
    assert "Failed: 1" in result.stdout
    assert "workspaceSnapshotId" in result.stdout


def test_schema_validate_batch_json_output(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    payload_dir.joinpath("env.json").write_text(
        json.dumps(_environment_payload(str(tmp_path))),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "schema",
            "validate-batch",
            "environment-metadata",
            str(payload_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "environment-metadata"
    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["failed"] == 0


def test_doctor_json_includes_environment_and_status() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "environment" in data
    assert "status" in data
    assert data["status"]["ok"] is True
    assert data["environment"]["workspace_snapshot"].startswith("sha256:")
    assert "python_requirement" in data["environment"]
    assert isinstance(data["environment"].get("python_compatibility"), list)
    orchestrator_payload = data.get("orchestrator", {})
    cache_info = orchestrator_payload.get("cache", {})
    assert cache_info.get("selectorEntries") == 0
    assert cache_info.get("fingerprints") == []
    pyright_info = orchestrator_payload.get("pyright", {})
    assert pyright_info.get("expectedVersion") == PYRIGHT_VERSION.version
    supported_versions = pyright_info.get("supportedVersions")
    assert isinstance(supported_versions, list)
    assert tuple(supported_versions) == PYRIGHT_VERSION_SUPPORT.supported_versions


def test_doctor_json_reports_config_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "pyrightconfig.json"
    config.write_text('{\n  "venvPath": ".venv"\n}\n')
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    digest = data["environment"].get("config_digest")
    assert isinstance(digest, str)
    assert digest.startswith("sha256:")


def test_doctor_json_reports_git_metadata(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("print('tracked')\n")

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "doctor", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["environment"]["git_root"].endswith(str(tmp_path.name))
    assert data["environment"]["git_dirty"] is True
    assert data["environment"]["workspace_snapshot"].startswith("sha256:")


def test_doctor_text_output_surfaces_snapshot_and_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--workspace", str(tmp_path), "doctor"])

    assert result.exit_code == 0
    stdout = result.stdout
    assert "Workspace snapshot:" in stdout
    assert "Git:" in stdout
    assert "Dirty:" in stdout
    assert "Python requirement:" in stdout
    assert "Python compatibility:" in stdout
    assert "Cache entries:" in stdout
    assert "Workspace jail:" in stdout
    assert "Workspace lock:" in stdout


def test_definition_json_bundle_contains_selector(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "src" / "main.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def greet():\n    return 'hello'\n")
    selector = "src/main.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "def", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    metadata = data["metadata"]
    assert payload["kind"] == "definition"
    assert payload["request"]["selector"]["uri"].startswith("file://")
    assert payload["request"]["requestId"].startswith("sha256:")
    assert payload["resolution"]["status"] == "resolved"
    repositioning = payload["resolution"]["repositioning"]
    assert repositioning["strategy"] == "cursor-line-col"
    assert repositioning["target"]["kind"] == "cursor"
    definitions = payload["result"]["definitions"]
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition["uri"].endswith("src/main.py")
    assert definition["range"] == {"start": [1, 0], "end": [1, 5]}
    cache_info = metadata.get("cache", {})
    assert cache_info.get("hit") is False
    assert cache_info.get("size") == 1


def test_definition_stream_json_emits_progress_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    module = tmp_path / "src" / "main.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def greet():\n    return 'hello'\n")

    handshake = PyrightHandshake(
        result={
            "capabilities": {"positionEncoding": "utf-16"},
            "serverInfo": {"name": "fake-pyright", "version": "0.0"},
        }
    )

    class _StreamingSession:
        def __init__(self) -> None:
            self._handshake = handshake
            self.shutdown_called = False
            self.progress_notifications: list[dict[str, Any]] = [
                {
                    "jsonrpc": "2.0",
                    "method": "$/progress",
                    "params": {
                        "token": "index-1",
                        "value": {"kind": "begin", "title": "Indexing"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "$/progress",
                    "params": {
                        "token": "index-1",
                        "value": {
                            "kind": "end",
                            "message": "Completed",
                            "percentage": 100,
                        },
                    },
                },
            ]

        @property
        def handshake(self) -> PyrightHandshake:
            return self._handshake

        def notify(self, method: str, params: Any) -> None:  # pragma: no cover - noop
            return None

        def request(
            self,
            method: str,
            params: Any,
            timeout: float | None = None,
            *,
            cancellable: bool = False,
        ) -> Any:
            if method == "textDocument/definition":
                text_document = params.get("textDocument", {}) if isinstance(params, dict) else {}
                uri = text_document.get("uri")
                return [
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                    }
                ]
            return None

        def drain_notifications(self, *, method: str | None = None) -> list[dict[str, Any]]:
            if method is None or method == "$/progress":
                notifications = list(self.progress_notifications)
                self.progress_notifications.clear()
                return notifications
            return []

        def wait_for_notifications(
            self, *, method: str, timeout: float = 5.0
        ) -> list[dict[str, Any]]:  # pragma: no cover - unused in tests
            return []

        def refresh_configuration(self) -> None:  # pragma: no cover - noop
            return None

        def shutdown(self) -> None:
            self.shutdown_called = True

    monkeypatch.setattr(
        "lanser.orchestrator.create_pyright_session",
        lambda workspace, recorder=None: _StreamingSession(),
    )

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "def",
            "src/main.py@L1:C1",
            "--json",
            "--stream",
        ],
    )

    assert result.exit_code == 0
    frames = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert len(frames) == 3
    assert frames[0]["event"] == "progress"
    assert frames[0]["progress"] == {
        "kind": "begin",
        "token": "index-1",
        "title": "Indexing",
    }
    assert frames[1]["event"] == "progress"
    assert frames[1]["progress"] == {
        "kind": "end",
        "token": "index-1",
        "message": "Completed",
        "percentage": 100.0,
    }
    assert frames[2]["event"] == "result"
    assert frames[2]["result"]["status"]["ok"] is True


def test_definition_stream_text_emits_human_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    module = tmp_path / "src" / "main.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def greet():\n    return 'hello'\n")

    handshake = PyrightHandshake(
        result={
            "capabilities": {"positionEncoding": "utf-16"},
            "serverInfo": {"name": "fake-pyright", "version": "0.0"},
        }
    )

    class _StreamingSession:
        def __init__(self) -> None:
            self._handshake = handshake
            self.progress_notifications: list[dict[str, Any]] = [
                {
                    "jsonrpc": "2.0",
                    "method": "$/progress",
                    "params": {
                        "token": "index-1",
                        "value": {"kind": "begin", "title": "Indexing"},
                    },
                }
            ]

        @property
        def handshake(self) -> PyrightHandshake:
            return self._handshake

        def notify(self, method: str, params: Any) -> None:  # pragma: no cover - noop
            return None

        def request(
            self,
            method: str,
            params: Any,
            timeout: float | None = None,
            *,
            cancellable: bool = False,
        ) -> Any:
            if method == "textDocument/definition":
                text_document = params.get("textDocument", {}) if isinstance(params, dict) else {}
                uri = text_document.get("uri")
                return [
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                    }
                ]
            return None

        def drain_notifications(self, *, method: str | None = None) -> list[dict[str, Any]]:
            if method is None or method == "$/progress":
                notifications = list(self.progress_notifications)
                self.progress_notifications.clear()
                return notifications
            return []

        def wait_for_notifications(
            self, *, method: str, timeout: float = 5.0
        ) -> list[dict[str, Any]]:  # pragma: no cover - unused
            return []

        def refresh_configuration(self) -> None:  # pragma: no cover - noop
            return None

        def shutdown(self) -> None:  # pragma: no cover - noop
            return None

    monkeypatch.setattr(
        "lanser.orchestrator.create_pyright_session",
        lambda workspace, recorder=None: _StreamingSession(),
    )

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "def", "src/main.py@L1:C1", "--stream"],
    )

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line]
    assert lines[0].startswith("[progress:begin]")
    assert "title=Indexing" in lines[0]
    assert any("Definition bundle generated" in line for line in lines)


def test_definition_text_output_includes_cache_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "src" / "main.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def greet():\n    return 'hello'\n")
    selector = "src/main.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "def", selector],
    )

    assert result.exit_code == 0
    stdout = result.stdout
    assert "Cache: miss" in stdout
    assert "entries=1" in stdout


def test_definition_anchor_selector_repositioning(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    snippet = quote_plus("def foo")
    selector = f"anchor://{module.relative_to(tmp_path)}#{snippet}?ctx=2"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "def", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    repositioning = payload["resolution"]["repositioning"]
    assert repositioning["strategy"] == "anchor-snippet"
    assert repositioning["anchor"]["context"] == 2
    assert repositioning["anchor"]["snippetHash"].startswith("sha256:")


def test_definition_invalid_selector_reports_error() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["def", "invalid", "--json"])

    assert result.exit_code == ExitCode.BAD_SELECTOR_SYNTAX
    data = json.loads(result.stdout)
    payload = data["payload"]
    status = data["status"]
    assert status["ok"] is False
    assert payload["error"]["selector"] == "invalid"


def test_references_json_bundle_contains_selector(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@R(1,1->1,3)"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "references", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    metadata = data["metadata"]
    assert payload["kind"] == "references"
    assert payload["resolution"]["selected"]["uri"].startswith("file://")
    references = payload["result"]["references"]
    assert references
    roles = {entry["role"] for entry in references}
    assert "definition" in roles
    repositioning = payload["resolution"]["repositioning"]
    assert repositioning["strategy"] == "range-window"
    assert repositioning["fallbacks"][0]["strategy"] == "cursor-centre"
    assert metadata.get("cache", {}).get("hit") is False


def test_selector_bundles_emit_stable_request_ids(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    first = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "def", selector, "--json"],
    )
    second = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "def", selector, "--json"],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0

    first_id = json.loads(first.stdout)["payload"]["request"]["requestId"]
    second_id = json.loads(second.stdout)["payload"]["request"]["requestId"]

    assert first_id == second_id


def test_hover_json_bundle_contains_stub(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "hover", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "hover"
    hover_payload = payload["result"]["hover"]
    assert hover_payload["contents"]
    assert hover_payload["symbol"]["name"] == "foo"
    assert payload["resolution"]["repositioning"]["strategy"] == "cursor-line-col"


def test_symbols_json_bundle_contains_stub(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "symbols", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "symbols"
    symbols = payload["result"]["symbols"]
    assert symbols
    assert symbols[0]["symbol"]["name"] == "foo"


def test_document_diagnostics_json_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "diagnostics", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "diagnostics"
    assert payload["result"]["diagnostics"] == []


def test_diag_alias_invokes_document_diagnostics(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "diag", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "diagnostics"
    assert payload["result"]["diagnostics"] == []


def test_document_diagnostics_reports_syntax_error(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "broken.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def broken(:\n    pass\n")
    selector = "pkg/broken.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "diagnostics", selector, "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    diagnostics = data["payload"]["result"]["diagnostics"]
    assert diagnostics
    assert diagnostics[0]["severity"] == "error"
    assert "syntax" in diagnostics[0]["message"].lower()


def test_document_diagnostics_sarif_export(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "broken.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def broken(:\n    pass\n")
    selector = "pkg/broken.py@L1:C1"
    report_path = tmp_path / "reports" / "diagnostics.sarif"

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "diagnostics",
            selector,
            "--sarif",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(report_path.read_text())
    run = payload["runs"][0]
    assert run["results"], result.stdout
    sarif_result = run["results"][0]
    assert sarif_result["level"] == "error"
    assert "syntax" in sarif_result["message"]["text"].lower()


def test_workspace_diagnostics_json_bundle(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "diagnostics", "--workspace", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "diagnostics"
    assert payload["request"]["scope"] == "workspace"
    assert payload["request"]["requestId"].startswith("sha256:")


def test_rename_preview_json_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "rename", selector, "new_name", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "rename"
    rename_result = payload["result"]["rename"]
    assert rename_result["requestedName"] == "new_name"
    assert rename_result["applyMode"] == "preview"
    assert rename_result["changes"]
    assert rename_result["originalName"] == "foo"
    assert rename_result["changeCount"] == len(rename_result["changes"])
    assert rename_result["applied"] is False
    assert rename_result["applyStatus"] == "preview"
    prepare_block = rename_result.get("prepare")
    if isinstance(prepare_block, dict):
        assert prepare_block["status"] == "allowed"
    first_change = rename_result["changes"][0]
    assert first_change["newText"] == "new_name"
    assert first_change["occurrence"]["role"] == "definition"
    workspace_edit = payload["result"]["workspaceEdit"]
    assert workspace_edit is not None
    assert workspace_edit["changeCount"] == rename_result["changeCount"]
    diff_preview = payload["result"]["diff"]
    assert diff_preview is not None
    assert diff_preview["format"] == "unified"
    assert any("new_name" in line for line in diff_preview["hunks"])


def test_rename_apply_json_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "rename",
            selector,
            "new_name",
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "rename"
    rename_result = payload["result"]["rename"]
    assert rename_result["applyMode"] == "apply"
    assert rename_result["changes"]
    assert rename_result["changeCount"] == len(rename_result["changes"])
    assert rename_result["applied"] is True
    assert rename_result["applyStatus"] == "applied"
    prepare_block = rename_result.get("prepare")
    if isinstance(prepare_block, dict):
        assert prepare_block["status"] == "allowed"
    workspace_edit = payload["result"]["workspaceEdit"]
    assert workspace_edit is not None
    assert workspace_edit["changeCount"] == rename_result["changeCount"]
    diff_preview = payload["result"]["diff"]
    assert diff_preview is not None
    assert any("new_name" in line for line in diff_preview["hunks"])
    assert "new_name" in module.read_text()


def test_rename_blocks_when_workspace_dirty(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "rename", selector, "new_name", "--json"],
    )

    assert result.exit_code == ExitCode.VERSION_SKEW
    data = json.loads(result.stdout)
    payload = data["payload"]
    status = data["status"]
    assert status["ok"] is False
    assert payload["error"]["kind"] == "workspace-dirty"
    git_info = payload["error"]["git"]
    status_sample = git_info["statusSample"]
    assert isinstance(status_sample, dict)
    assert status_sample["lines"]
    assert status_sample["total"] >= len(status_sample["lines"])
    assert "Dirty paths:" in status["message"]


def test_rename_allows_dirty_with_flag(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")
    selector = "pkg/mod.py@L1:C1"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "--allow-dirty",
            "rename",
            selector,
            "new_name",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    payload = data["payload"]
    assert payload["kind"] == "rename"


def test_rename_blocks_outside_workspace(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    external_root = tmp_path_factory.mktemp("external")
    module = external_root / "mod.py"
    module.write_text("def foo():\n    return 42\n")
    selector = f"{module}@L1:C1"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(tmp_path), "rename", selector, "new_name", "--json"],
    )

    assert result.exit_code == ExitCode.FS_PERMISSIONS
    data = json.loads(result.stdout)
    assert data["status"]["ok"] is False
    assert data["payload"]["error"]["kind"] == "workspace-jail"


def test_rename_denied_by_filter(tmp_path: Path) -> None:
    denied_dir = tmp_path / "blocked"
    denied_dir.mkdir()
    module = denied_dir / "mod.py"
    module.write_text("def foo():\n    return 42\n")
    selector = "blocked/mod.py@L1:C1"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "--deny-path",
            "blocked",
            "rename",
            selector,
            "new_name",
            "--json",
        ],
    )

    assert result.exit_code == ExitCode.FS_PERMISSIONS
    data = json.loads(result.stdout)
    assert data["status"]["ok"] is False
    assert data["payload"]["error"]["kind"] == "path-denied"


def test_allow_path_filter_restricts_to_selected_directory(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    allowed_file = allowed_dir / "one.py"
    allowed_file.write_text("def foo():\n    return 42\n")
    other_file = other_dir / "two.py"
    other_file.write_text("def bar():\n    return 24\n")

    runner = CliRunner()
    allowed_selector = "allowed/one.py@L1:C1"
    permitted = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "--allow-path",
            "allowed",
            "rename",
            allowed_selector,
            "new_name",
            "--json",
        ],
    )
    assert permitted.exit_code == 0

    blocked_selector = "other/two.py@L1:C1"
    denied = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "--allow-path",
            "allowed",
            "rename",
            blocked_selector,
            "new_name",
            "--json",
        ],
    )
    assert denied.exit_code == ExitCode.FS_PERMISSIONS
    denied_payload = json.loads(denied.stdout)
    assert denied_payload["payload"]["error"]["kind"] == "path-not-allowed"


def test_batch_cli_processes_requests(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    batch_file = tmp_path / "batch.jsonl"
    responses_file = tmp_path / "responses.jsonl"
    entries = [
        {"id": "one", "command": "definition", "selector": "pkg/mod.py@L1:C1"},
        {"id": "two", "command": "hover", "selector": "pkg/mod.py@L1:C1"},
    ]
    batch_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "batch",
            "--in",
            str(batch_file),
            "--out",
            str(responses_file),
        ],
    )

    assert result.exit_code == 0
    lines = responses_file.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["request"]["command"] == "definition"
    assert first["status"]["ok"] is True
    assert first["payload"]["kind"] == "definition"
    assert second["request"]["command"] == "hover"
    assert second["status"]["ok"] is True
    assert second["payload"]["kind"] == "hover"


def test_batch_cli_stops_on_first_failure_without_continue(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    batch_file = tmp_path / "batch.jsonl"
    responses_file = tmp_path / "responses.jsonl"
    entries = [
        {"id": "ok", "command": "definition", "selector": "pkg/mod.py@L1:C1"},
        {"id": "bad", "command": "definition", "selector": "not-a-selector"},
        {"id": "skip", "command": "hover", "selector": "pkg/mod.py@L1:C1"},
    ]
    batch_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "batch",
            "--in",
            str(batch_file),
            "--out",
            str(responses_file),
        ],
    )

    assert result.exit_code == ExitCode.BAD_SELECTOR_SYNTAX
    lines = responses_file.read_text().splitlines()
    assert len(lines) == 2
    ok_entry = json.loads(lines[0])
    bad_entry = json.loads(lines[1])
    assert ok_entry["status"]["ok"] is True
    assert bad_entry["status"]["ok"] is False
    assert bad_entry["status"]["exitCode"] == int(ExitCode.BAD_SELECTOR_SYNTAX)


def test_batch_cli_continue_on_error_processes_all(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    batch_file = tmp_path / "batch.jsonl"
    responses_file = tmp_path / "responses.jsonl"
    entries = [
        {"id": "ok", "command": "definition", "selector": "pkg/mod.py@L1:C1"},
        {"id": "bad", "command": "definition", "selector": "not-a-selector"},
        {"id": "later", "command": "hover", "selector": "pkg/mod.py@L1:C1"},
    ]
    batch_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "batch",
            "--in",
            str(batch_file),
            "--out",
            str(responses_file),
            "--continue-on-error",
        ],
    )

    assert result.exit_code == ExitCode.BAD_SELECTOR_SYNTAX
    lines = responses_file.read_text().splitlines()
    assert len(lines) == 3
    later_entry = json.loads(lines[2])
    assert later_entry["request"]["id"] == "later"
    assert later_entry["status"]["ok"] is True


def test_batch_cli_streams_to_stdout(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    batch_file = tmp_path / "batch.jsonl"
    entries = [
        {"id": "one", "command": "definition", "selector": "pkg/mod.py@L1:C1"},
        {"id": "two", "command": "hover", "selector": "pkg/mod.py@L1:C1"},
    ]
    batch_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "batch",
            "--in",
            str(batch_file),
            "--stream",
        ],
    )

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["request"]["id"] == "one"
    assert second["request"]["id"] == "two"


def test_batch_cli_streams_to_file(tmp_path: Path) -> None:
    runner = CliRunner()
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    batch_file = tmp_path / "batch.jsonl"
    out_file = tmp_path / "logs" / "responses.jsonl"
    entries = [
        {"id": "alpha", "command": "definition", "selector": "pkg/mod.py@L1:C1"},
        {"id": "beta", "command": "hover", "selector": "pkg/mod.py@L1:C1"},
    ]
    batch_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(tmp_path),
            "batch",
            "--in",
            str(batch_file),
            "--out",
            str(out_file),
            "--stream",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == ""
    lines = out_file.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["request"]["id"] == "alpha"
    assert json.loads(lines[1])["request"]["id"] == "beta"
