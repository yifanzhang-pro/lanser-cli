from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections import abc
from pathlib import Path
from typing import Any, cast

import pytest

from lanser.pyright import PyrightSession, PyrightSessionTimeout, create_pyright_session
from lanser.trace import JsonRpcTraceRecorder


def _fake_server() -> Path:
    return Path(__file__).parent / "helpers" / "fake_pyright_server.py"


def test_pyright_session_workspace_configuration(tmp_path: Path) -> None:
    workspace = tmp_path
    config_payload = {
        "typeCheckingMode": "strict",
        "executionEnvironments": [{"root": ".", "venvPath": ".venv"}],
        "reportMissingImports": "warning",
    }
    (workspace / "pyrightconfig.json").write_text(
        json.dumps(config_payload, sort_keys=True),
        encoding="utf-8",
    )

    session = PyrightSession(workspace, command=(sys.executable, str(_fake_server())))

    params: abc.Mapping[str, object] = {
        "items": [
            {"section": "pyright"},
            {"section": "executionEnvironments"},
            {"section": "typeCheckingMode"},
            {"section": "unknown.section"},
            {},
        ]
    }

    try:
        results = session._build_configuration_items(params)
    finally:
        session.shutdown()

    assert results[0] == config_payload
    assert results[1] == config_payload["executionEnvironments"]
    assert results[2] == config_payload["typeCheckingMode"]
    assert results[3] is None
    assert results[4] == config_payload


def test_pyright_session_initial_configuration_payload(tmp_path: Path) -> None:
    workspace = tmp_path
    config_payload = {
        "typeCheckingMode": "strict",
        "executionEnvironments": [{"root": ".", "venvPath": ".venv"}],
    }
    (workspace / "pyrightconfig.json").write_text(
        json.dumps(config_payload, sort_keys=True),
        encoding="utf-8",
    )

    session = PyrightSession(workspace, command=(sys.executable, str(_fake_server())))

    captured: list[tuple[str, abc.Mapping[str, Any] | None]] = []

    def _capture_notify(method: str, params: abc.Mapping[str, Any] | None) -> None:
        captured.append((method, params))

    session.notify = _capture_notify  # type: ignore[assignment]

    session._send_initial_configuration()

    assert captured, "Expected configuration notification to be sent"
    method, params = captured[-1]
    assert method == "workspace/didChangeConfiguration"
    assert isinstance(params, abc.Mapping)
    settings = params.get("settings") if isinstance(params, abc.Mapping) else None
    assert isinstance(settings, abc.Mapping)
    expected = {
        "python": config_payload,
        "pyright": config_payload,
    }
    assert settings == expected


def test_pyright_session_initial_configuration_without_config(tmp_path: Path) -> None:
    session = PyrightSession(tmp_path, command=(sys.executable, str(_fake_server())))

    captured: list[tuple[str, abc.Mapping[str, Any] | None]] = []

    def _capture_notify(method: str, params: abc.Mapping[str, Any] | None) -> None:
        captured.append((method, params))

    session.notify = _capture_notify  # type: ignore[assignment]

    session._send_initial_configuration()

    assert captured, "Expected configuration notification to be sent"
    method, params = captured[-1]
    assert method == "workspace/didChangeConfiguration"
    assert isinstance(params, abc.Mapping)
    settings = params.get("settings") if isinstance(params, abc.Mapping) else None
    assert settings == {}


def test_pyright_session_refresh_configuration_tracks_changes(tmp_path: Path) -> None:
    workspace = tmp_path
    config_path = workspace / "pyrightconfig.json"
    config_path.write_text(
        json.dumps({"typeCheckingMode": "basic"}, sort_keys=True),
        encoding="utf-8",
    )

    session = PyrightSession(workspace, command=(sys.executable, str(_fake_server())))

    captured: list[tuple[str, abc.Mapping[str, Any] | None]] = []

    def _capture_notify(method: str, params: abc.Mapping[str, Any] | None) -> None:
        captured.append((method, params))

    session.notify = _capture_notify  # type: ignore[assignment]

    session.refresh_configuration()
    assert len(captured) == 1
    method, params = captured[-1]
    assert method == "workspace/didChangeConfiguration"
    assert isinstance(params, abc.Mapping)
    settings = params.get("settings") if isinstance(params, abc.Mapping) else None
    assert isinstance(settings, abc.Mapping)
    assert settings.get("pyright", {}).get("typeCheckingMode") == "basic"

    session.refresh_configuration()
    assert len(captured) == 1, "Duplicate configuration should not trigger notify"

    time.sleep(0.01)
    config_path.write_text(
        json.dumps({"typeCheckingMode": "strict"}, sort_keys=True),
        encoding="utf-8",
    )

    session.refresh_configuration()
    assert len(captured) == 2
    _, params = captured[-1]
    assert isinstance(params, abc.Mapping)
    settings = params.get("settings") if isinstance(params, abc.Mapping) else None
    assert isinstance(settings, abc.Mapping)
    assert settings.get("pyright", {}).get("typeCheckingMode") == "strict"

    config_path.unlink()
    time.sleep(0.01)
    session.refresh_configuration()
    assert len(captured) == 3
    _, params = captured[-1]
    assert isinstance(params, abc.Mapping)
    settings = params.get("settings") if isinstance(params, abc.Mapping) else None
    assert settings == {}

    session.shutdown()


def test_pyright_session_cancellable_request_sends_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = PyrightSession(tmp_path, command=(sys.executable, str(_fake_server())))

    class _Recorder:
        def __init__(self) -> None:
            self.messages: list[abc.Mapping[str, Any]] = []

        def write_message(self, message: abc.Mapping[str, Any]) -> None:
            self.messages.append(message)

    writer = _Recorder()
    session._writer = writer  # type: ignore[assignment]
    session._process = cast("subprocess.Popen[bytes]", object())

    def _raise_timeout(**_: Any) -> abc.Mapping[str, Any] | None:
        raise PyrightSessionTimeout("timeout")

    monkeypatch.setattr(session, "_await_response", _raise_timeout)

    with pytest.raises(PyrightSessionTimeout):
        session.request("workspace/slow", None, timeout=0.01, cancellable=True)

    cancel_messages = [
        message for message in writer.messages if message.get("method") == "$/cancelRequest"
    ]
    assert cancel_messages, "Expected cancellation notification to be sent"
    params = cancel_messages[-1].get("params")
    assert isinstance(params, abc.Mapping)
    assert params.get("id") == 1

    session._writer = None
    session._process = None


def test_pyright_session_initialize_and_request(tmp_path: Path) -> None:
    session = PyrightSession(
        tmp_path,
        command=(sys.executable, str(_fake_server())),
    )

    try:
        handshake = session.initialize()
        metadata = handshake.to_metadata()

        assert metadata["serverInfo"]["name"] == "fake-pyright"
        assert metadata["positionEncoding"] == "utf-16"

        result = session.request("workspace/echo", {"value": 42})
        assert result == {"echo": "workspace/echo"}
    finally:
        session.shutdown()


PYRIGHT_BINARY = shutil.which("pyright-langserver")


def _collect_document_diagnostic_messages(result: object) -> list[str]:
    messages: list[str] = []
    if isinstance(result, abc.Mapping):
        mapping_result = result
        items = mapping_result.get("items")
        if isinstance(items, abc.Sequence):
            for entry in items:
                if isinstance(entry, abc.Mapping):
                    message = entry.get("message")
                    if isinstance(message, str):
                        messages.append(message)
    return messages


def _collect_workspace_diagnostic_messages(result: object) -> list[str]:
    messages: list[str] = []
    if isinstance(result, abc.Mapping):
        mapping_result = result
        items = mapping_result.get("items")
        if isinstance(items, abc.Sequence):
            for entry in items:
                if isinstance(entry, abc.Mapping):
                    diagnostics = entry.get("items")
                    if isinstance(diagnostics, abc.Sequence):
                        for diagnostic in diagnostics:
                            if isinstance(diagnostic, abc.Mapping):
                                message = diagnostic.get("message")
                                if isinstance(message, str):
                                    messages.append(message)
    return messages


def _collect_notification_messages(notifications: abc.Sequence[abc.Mapping[str, Any]]) -> list[str]:
    messages: list[str] = []
    for notification in notifications:
        params = notification.get("params")
        if not isinstance(params, abc.Mapping):
            continue
        diagnostics = params.get("diagnostics")
        if not isinstance(diagnostics, abc.Sequence):
            continue
        for diagnostic in diagnostics:
            if isinstance(diagnostic, abc.Mapping):
                message = diagnostic.get("message")
                if isinstance(message, str):
                    messages.append(message)
    return messages


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_pyright_session_real_hover_and_symbols(tmp_path: Path) -> None:
    workspace = tmp_path
    source = workspace / "example.py"
    code = (
        """def greet(name: str) -> str:\n    return f"Hello {name}!"\n\nresult = greet("world")\n"""
    )
    source.write_text(code, encoding="utf-8")

    session = PyrightSession(workspace)

    try:
        handshake = session.initialize()
        metadata = handshake.to_metadata()
        assert "capabilitiesDigest" in metadata

        uri = source.resolve().as_uri()
        session.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": code,
                }
            },
        )

        diagnostics = session.wait_for_notifications(
            method="textDocument/publishDiagnostics", timeout=10.0
        )
        if diagnostics:
            assert diagnostics[-1]["params"]["diagnostics"] == []

        hover = session.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 0, "character": 4},
            },
            timeout=30.0,
        )
        assert hover is not None
        contents = hover.get("contents") if isinstance(hover, dict) else None
        assert contents is not None

        symbols = session.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
            timeout=30.0,
        )
        assert isinstance(symbols, list)
        assert any(entry.get("name") == "greet" for entry in symbols)
    finally:
        session.shutdown()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_pyright_session_real_definition_and_references(tmp_path: Path) -> None:
    workspace = tmp_path
    source = workspace / "example.py"
    code = "\n".join(
        (
            "def greet(name: str) -> str:",
            '    return f"Hello {name}!"',
            "",
            'result = greet("world")',
            "",
        )
    )
    source.write_text(code, encoding="utf-8")

    session = PyrightSession(workspace)

    try:
        session.initialize()
        uri = source.resolve().as_uri()
        session.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": code,
                }
            },
        )

        session.wait_for_notifications(method="textDocument/publishDiagnostics", timeout=10.0)

        definition = session.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 3, "character": 10},
            },
            timeout=30.0,
        )

        assert isinstance(definition, list)
        definition_entries = [entry for entry in definition if isinstance(entry, dict)]
        assert definition_entries
        definition_location = definition_entries[0]
        assert definition_location.get("uri") == uri
        range_info = definition_location.get("range")
        assert isinstance(range_info, dict)
        start = range_info.get("start") if isinstance(range_info, dict) else None
        assert isinstance(start, dict)
        assert start.get("line") == 0

        references = session.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 3, "character": 10},
                "context": {"includeDeclaration": False},
            },
            timeout=30.0,
        )

        assert isinstance(references, list)
        reference_entries = [entry for entry in references if isinstance(entry, dict)]
        assert reference_entries
        assert any(entry.get("uri") == uri for entry in reference_entries)
    finally:
        session.shutdown()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_pyright_session_real_workspace_diagnostics(tmp_path: Path) -> None:
    workspace = tmp_path
    source = workspace / "example.py"
    code = "\n".join(
        (
            "def greet(name: str) -> str:",
            "    return name",
            "",
            "result = greet(123)",
            "",
        )
    )
    source.write_text(code, encoding="utf-8")

    session = PyrightSession(workspace)

    try:
        session.initialize()
        uri = source.resolve().as_uri()
        session.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": code,
                }
            },
        )

        messages: list[str] = []

        document_diagnostic = session.request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": uri}, "previousResultId": None},
            timeout=30.0,
        )
        messages.extend(_collect_document_diagnostic_messages(document_diagnostic))

        notifications = session.drain_notifications(method="textDocument/publishDiagnostics")
        if not notifications:
            notifications = session.wait_for_notifications(
                method="textDocument/publishDiagnostics", timeout=15.0
            )
        messages.extend(_collect_notification_messages(notifications))

        try:
            workspace_diagnostics = session.request(
                "workspace/diagnostic",
                {"identifier": "pytest", "previousResultIds": []},
                timeout=30.0,
            )
        except PyrightSessionTimeout as exc:
            pytest.skip(f"Pyright workspace diagnostics unavailable: {exc}")

        assert isinstance(workspace_diagnostics, dict)
        items = workspace_diagnostics.get("items")
        assert isinstance(items, list)
        assert any(isinstance(entry, dict) and entry.get("uri") == uri for entry in items)
        messages.extend(_collect_workspace_diagnostic_messages(workspace_diagnostics))

        assert any("Literal[123]" in message for message in messages)
    finally:
        session.shutdown()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_create_pyright_session_real_diagnostics(tmp_path: Path) -> None:
    workspace = tmp_path
    source = workspace / "example.py"
    source.write_text(
        "\n".join(
            (
                "def greet(name: str) -> str:",
                "    return name",
                "",
                "result = greet(123)",
                "",
            )
        ),
        encoding="utf-8",
    )

    session = create_pyright_session(workspace)

    try:
        handshake = session.handshake
        assert handshake is not None
        metadata = handshake.to_metadata()
        assert metadata["capabilitiesDigest"].startswith("sha256:")
        server_info = metadata.get("serverInfo")
        if isinstance(server_info, abc.Mapping):
            assert server_info.get("name")
        if "positionEncoding" in metadata:
            assert metadata["positionEncoding"] == "utf-16"

        uri = source.resolve().as_uri()
        code = source.read_text(encoding="utf-8")
        session.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": code,
                }
            },
        )

        messages: list[str] = []

        document_diagnostic = session.request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": uri}, "previousResultId": None},
            timeout=30.0,
        )
        messages.extend(_collect_document_diagnostic_messages(document_diagnostic))

        notifications = session.drain_notifications(method="textDocument/publishDiagnostics")
        if not notifications:
            notifications = session.wait_for_notifications(
                method="textDocument/publishDiagnostics", timeout=15.0
            )
        messages.extend(_collect_notification_messages(notifications))

        try:
            workspace_diagnostics = session.request(
                "workspace/diagnostic",
                {"identifier": "pytest", "previousResultIds": []},
                timeout=30.0,
            )
        except PyrightSessionTimeout as exc:
            pytest.skip(f"Pyright workspace diagnostics unavailable: {exc}")
        else:
            messages.extend(_collect_workspace_diagnostic_messages(workspace_diagnostics))

        assert any("Literal[123]" in message for message in messages)
    finally:
        session.shutdown()


def test_pyright_session_records_jsonrpc_trace(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    recorder = JsonRpcTraceRecorder(trace_path)
    session = PyrightSession(
        tmp_path,
        command=(sys.executable, str(_fake_server())),
        recorder=recorder,
    )

    try:
        session.start()
        session.initialize()
        session.request("workspace/echo", None)
    finally:
        session.shutdown()
        recorder.close()

    lines = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "Expected JSON-RPC messages to be recorded"
    events = [json.loads(line) for line in lines]
    assert any(event.get("event") == "jsonrpc" for event in events)
