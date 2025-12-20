from __future__ import annotations

import ast
import bisect
import json
import re
import subprocess
from collections import deque
from collections import abc
from collections.abc import Mapping, Sequence
from pathlib import Path
from platform import python_version
from types import MethodType
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse

import pytest

from lanser.environment import EnvironmentSnapshot
from lanser.exit_codes import ExitCode
from lanser.orchestrator import (
    BatchRequest,
    LSPOrchestrator,
    OrchestratorSettings,
    _RenameFileEdit,
    _RenamePlan,
)
from lanser.pyright import PyrightHandshake, PyrightSessionError
from lanser.pyright_version import PYRIGHT_VERSION, PYRIGHT_VERSION_SUPPORT
from lanser.workspace_lock import WorkspaceLock


PYTHON_VERSION = python_version()


class _FakeSession:
    def __init__(self, handshake: PyrightHandshake) -> None:
        self._handshake = handshake
        self.notifications: list[tuple[str, object]] = []
        self.requests: list[tuple[str, object]] = []
        self.shutdown_called = False
        self.progress_notifications: list[dict[str, object]] = []
        self.workspace_diag_call_count = 0
        self.refresh_call_count = 0

    @property
    def handshake(self) -> PyrightHandshake:
        return self._handshake

    def notify(self, method: str, params: object) -> None:
        self.notifications.append((method, params))

    def request(
        self,
        method: str,
        params: object,
        timeout: float | None = None,
        *,
        cancellable: bool = False,
    ) -> object:
        self.requests.append((method, params))
        if method == "textDocument/prepareRename":
            return {
                "range": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 7},
                },
                "placeholder": "foo",
            }
        if method == "textDocument/definition":
            text_document = params.get("textDocument") if isinstance(params, dict) else None
            uri = text_document.get("uri") if isinstance(text_document, dict) else None
            return [
                {
                    "uri": uri or "file:///missing.py",
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 3},
                    },
                }
            ]
        if method == "textDocument/references":
            text_document = params.get("textDocument") if isinstance(params, dict) else None
            uri = text_document.get("uri") if isinstance(text_document, dict) else None
            return [
                {
                    "uri": uri or "file:///missing.py",
                    "range": {
                        "start": {"line": 0, "character": 4},
                        "end": {"line": 0, "character": 7},
                    },
                },
                {
                    "uri": uri or "file:///missing.py",
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
            text_document = params.get("textDocument") if isinstance(params, dict) else None
            uri = text_document.get("uri") if isinstance(text_document, dict) else None
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
            text_document = params.get("textDocument") if isinstance(params, dict) else None
            uri = text_document.get("uri") if isinstance(text_document, dict) else None
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
        if method == "workspace/diagnostic":
            self.workspace_diag_call_count += 1
            params_mapping = params if isinstance(params, dict) else {}
            previous_ids = params_mapping.get("previousResultIds")
            has_previous = False
            if isinstance(previous_ids, list):
                for entry in previous_ids:
                    if (
                        isinstance(entry, dict)
                        and entry.get("uri") == "file:///workspace/pkg/mod.py"
                        and entry.get("value") == "diag-1"
                    ):
                        has_previous = True
                        break
            if self.workspace_diag_call_count == 1 or not has_previous:
                return {
                    "items": [
                        {
                            "uri": "file:///workspace/pkg/mod.py",
                            "kind": "full",
                            "resultId": "diag-1",
                            "items": [
                                {
                                    "range": {
                                        "start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 3},
                                    },
                                    "message": "diagnostic from pyright",
                                    "severity": 1,
                                    "code": "reportGeneralTypeIssues",
                                }
                            ],
                        }
                    ]
                }
            return {
                "items": [
                    {
                        "uri": "file:///workspace/pkg/mod.py",
                        "kind": "unchanged",
                        "resultId": "diag-1",
                    }
                ]
            }
        return None

    def drain_notifications(self, *, method: str | None = None) -> list[dict[str, object]]:
        if method == "$/progress":
            notifications = list(self.progress_notifications)
            self.progress_notifications.clear()
            return notifications
        return []

    def refresh_configuration(self) -> None:
        self.refresh_call_count += 1

    def wait_for_notifications(
        self, *, method: str, timeout: float = 5.0
    ) -> list[dict[str, object]]:
        return []

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def fake_pyright_session(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeSession]:
    handshake = PyrightHandshake(
        result={
            "capabilities": {
                "positionEncoding": "utf-16",
                "workspaceDiagnosticProvider": True,
            },
            "serverInfo": {"name": "fake-pyright", "version": "0.0"},
        }
    )

    session_holder: dict[str, _FakeSession] = {}

    def _factory(workspace: Path, recorder: object | None = None) -> _FakeSession:
        session = _FakeSession(handshake)
        session_holder["session"] = session
        return session

    monkeypatch.setattr("lanser.orchestrator.create_pyright_session", _factory)
    session_holder["handshake"] = handshake
    return session_holder


def _fake_snapshot(
    workspace: Path,
    *,
    pyright_version: str | None = None,
    git_root: str | None = None,
    git_head: str | None = None,
    git_dirty: bool | None = None,
    workspace_snapshot: str = "sha256:test",
) -> EnvironmentSnapshot:
    version = pyright_version or PYRIGHT_VERSION.cli_label
    return EnvironmentSnapshot(
        python_version=PYTHON_VERSION,
        python_executable="/usr/bin/python",
        platform="Linux",
        cwd=str(workspace),
        pyright_version=version,
        project_files=(),
        config_digest=None,
        git_root=git_root,
        git_head=git_head,
        git_dirty=git_dirty,
        workspace_snapshot=workspace_snapshot,
    )


def _assert_pyright_metadata(meta: Mapping[str, object]) -> None:
    assert meta.get("connected") is True
    supported_versions = meta.get("supportedVersions")
    assert isinstance(supported_versions, Sequence)
    assert tuple(supported_versions) == PYRIGHT_VERSION_SUPPORT.supported_versions
    server_version = meta.get("serverVersion")
    assert isinstance(server_version, str)
    assert server_version in PYRIGHT_VERSION_SUPPORT.supported_versions
    assert meta.get("expectedVersion") == PYRIGHT_VERSION.version
    assert meta.get("versionMismatch") is False


def _pyright_supports_workspace_diagnostics(orchestrator: LSPOrchestrator) -> bool:
    session = getattr(orchestrator, "_pyright_session", None)
    if session is None or session.handshake is None:
        return False
    capabilities = session.handshake.result.get("capabilities")
    if isinstance(capabilities, abc.Mapping):
        return bool(capabilities.get("workspaceDiagnosticProvider"))
    return False


def test_module_path_uses_pyright_include(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_payload = {"include": ["src"], "extraPaths": ["src"]}
    (tmp_path / "pyrightconfig.json").write_text(json.dumps(config_payload), encoding="utf-8")

    module_path = tmp_path / "src" / "pkg" / "mod.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("def marker() -> None:\n    return None\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    resolved = orchestrator._module_path_from_module_name("pkg.mod")
    assert resolved == module_path.resolve()


def test_module_path_uses_src_heuristic_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = tmp_path / "src" / "pkg" / "mod.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("def marker() -> None:\n    return None\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("py://pkg.mod#marker:def")

    assert outcome.ok
    assert outcome.message == "Definition bundle generated via Pyright."

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    result_block = payload.get("result")
    assert isinstance(result_block, Mapping)
    definitions = result_block.get("definitions")
    assert isinstance(definitions, list)
    assert definitions
    location = definitions[0].get("uri")
    assert location == module_path.as_uri()


def test_module_path_scans_workspace_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = tmp_path / "services" / "pkg" / "mod.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("def marker() -> None:\n    return None\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("py://pkg.mod#marker:def")

    assert outcome.ok
    assert outcome.message == "Definition bundle generated via Pyright."

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    result_block = payload.get("result")
    assert isinstance(result_block, Mapping)
    definitions = result_block.get("definitions")
    assert isinstance(definitions, list)
    assert definitions
    location = definitions[0].get("uri")
    assert location == module_path.as_uri()


def test_module_path_uses_pyproject_tool_pyright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[tool.pyright]
include = ["app"]
extraPaths = ["app"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    module_path = tmp_path / "app" / "pkg" / "module.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text('def greet() -> str:\n    return "hi"\n', encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("py://pkg.module#greet:def")

    assert outcome.ok
    assert outcome.message == "Definition bundle generated via Pyright."

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    result_block = payload.get("result")
    assert isinstance(result_block, Mapping)
    definitions = result_block.get("definitions")
    assert isinstance(definitions, list)
    assert definitions
    location = definitions[0].get("uri")
    assert location == module_path.as_uri()


def test_environment_payload_includes_server_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")
    assert outcome.ok
    payload = outcome.payload
    assert payload is not None
    environment = payload["environment"]
    assert environment["serverVersion"] == "0.0"
    assert environment["pyrightVersion"] == PYRIGHT_VERSION.cli_label
    assert environment["pyrightExpectedVersion"] == PYRIGHT_VERSION_SUPPORT.cli_label
    assert environment["pyrightSupportedVersions"] == list(
        PYRIGHT_VERSION_SUPPORT.supported_versions
    )
    assert environment["serverVersionMismatch"] is True
    assert environment["positionEncoding"] == "utf-16"

    language_server = environment.get("languageServer")
    assert isinstance(language_server, dict)
    assert language_server.get("capabilitiesDigest", "").startswith("sha256:")
    server_info = language_server.get("serverInfo")
    assert isinstance(server_info, dict)
    assert server_info.get("name") == "fake-pyright"
    assert language_server.get("expectedVersion") == PYRIGHT_VERSION.version
    assert language_server.get("supportedVersions") == list(
        PYRIGHT_VERSION_SUPPORT.supported_versions
    )
    assert language_server.get("serverVersion") == "0.0"
    assert language_server.get("versionMismatch") is True

    session = fake_pyright_session["session"]
    method, params = session.requests[0]
    assert method == "textDocument/definition"
    assert isinstance(params, dict)
    position = params.get("position")
    assert isinstance(position, dict)
    assert position.get("line") == 0
    # The cursor selector started at column 1; the orchestrator should
    # reposition to the function name so Pyright receives a precise
    # location inside the symbol span.
    assert position.get("character") == 4
    assert session.notifications[0][0] == "textDocument/didOpen"


def test_definition_metadata_includes_pyright_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    orchestrator._ensure_pyright_session()
    session = fake_pyright_session["session"]
    session.progress_notifications.extend(
        [
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
    )

    outcome = orchestrator.definition("pkg/mod.py@L1:C1")
    assert outcome.ok is True
    metadata = outcome.metadata
    assert metadata is not None
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, dict)
    progress = pyright_meta.get("progress")
    assert isinstance(progress, list)
    assert progress == [
        {"kind": "begin", "token": "index-1", "title": "Indexing"},
        {
            "kind": "end",
            "token": "index-1",
            "message": "Completed",
            "percentage": 100.0,
        },
    ]


def test_progress_handler_receives_normalised_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    captured: list[dict[str, object]] = []

    orchestrator = LSPOrchestrator(
        OrchestratorSettings(workspace=tmp_path),
        progress_handler=lambda event: captured.append(dict(event)),
    )
    orchestrator._ensure_pyright_session()
    session = fake_pyright_session["session"]
    session.progress_notifications.extend(
        [
            {
                "jsonrpc": "2.0",
                "method": "$/progress",
                "params": {
                    "token": 1,
                    "value": {"kind": "begin", "title": "Indexing"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "$/progress",
                "params": {
                    "token": 1,
                    "value": {
                        "kind": "report",
                        "percentage": 50,
                        "cancellable": True,
                    },
                },
            },
        ]
    )

    try:
        outcome = orchestrator.definition("pkg/mod.py@L1:C1")
    finally:
        orchestrator.close()

    assert outcome.ok is True
    assert captured == [
        {"kind": "begin", "token": 1, "title": "Indexing"},
        {
            "kind": "report",
            "token": 1,
            "percentage": 50.0,
            "cancellable": True,
        },
    ]


def test_selector_uses_negotiated_position_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    handshake = PyrightHandshake(
        result={
            "capabilities": {"positionEncoding": "utf-8"},
            "serverInfo": {"name": "fake-pyright", "version": "0.0"},
        }
    )

    def _factory(_: Path, recorder: object | None = None) -> _FakeSession:
        return _FakeSession(handshake)

    monkeypatch.setattr("lanser.orchestrator.create_pyright_session", _factory)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert outcome.ok
    payload = outcome.payload
    assert isinstance(payload, dict)
    selector_payload = payload.get("request", {}).get("selector", {})
    assert selector_payload.get("indexing") == "utf-8"
    environment = payload.get("environment", {})
    assert environment.get("positionEncoding") == "utf-8"
    metadata = outcome.metadata or {}
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, dict)
    handshake_meta = pyright_meta.get("handshake")
    assert isinstance(handshake_meta, dict)
    assert handshake_meta.get("positionEncoding") == "utf-8"


def test_definition_returns_pyright_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")
    assert outcome.ok

    payload = outcome.payload
    assert payload is not None
    definitions = payload["result"]["definitions"]
    assert definitions == [
        {
            "uri": (tmp_path / "pkg" / "mod.py").resolve().as_uri(),
            "range": {"start": [1, 0], "end": [1, 3]},
            "type": "location",
        }
    ]

    session = fake_pyright_session["session"]
    assert session.requests[0][0] == "textDocument/definition"


def test_definition_refreshes_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    orchestrator.definition("pkg/mod.py@L1:C1")

    session = fake_pyright_session["session"]
    assert session.refresh_call_count >= 1


def test_close_sends_did_close_and_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    orchestrator.definition("pkg/mod.py@L1:C1")

    session = fake_pyright_session["session"]
    orchestrator.close()

    did_close = [
        params for method, params in session.notifications if method == "textDocument/didClose"
    ]
    assert did_close
    assert session.shutdown_called is True

    # ``close`` is idempotent so repeated calls do not raise errors.
    orchestrator.close()
    assert session.shutdown_called is True


def test_references_return_pyright_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return foo()\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.references("pkg/mod.py@L1:C1")

    assert outcome.ok
    payload = outcome.payload
    assert payload is not None
    references = payload["result"]["references"]
    assert len(references) == 2
    first, second = references
    assert first["role"] == "definition"
    assert first["range"]["start"] == [1, 4]
    assert first["symbol"]["name"] == "foo"
    assert second["role"] in {"reference", "match"}

    session = fake_pyright_session["session"]
    assert any(method == "textDocument/references" for method, _ in session.requests)


def test_hover_returns_pyright_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.hover("pkg/mod.py@L1:C1")

    assert outcome.ok
    payload = outcome.payload
    assert payload is not None
    hover_payload = payload["result"]["hover"]
    assert hover_payload["contents"]
    assert hover_payload["contents"][0]["value"] == "**hover**"
    assert hover_payload["range"]["start"] == [1, 4]
    assert hover_payload["symbol"]["name"] == "foo"

    session = fake_pyright_session["session"]
    assert any(method == "textDocument/hover" for method, _ in session.requests)


def test_hover_stub_fallback_clears_after_pyright_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    handshake = fake_pyright_session["handshake"]
    assert isinstance(handshake, PyrightHandshake)

    class _FlakySession(_FakeSession):
        def __init__(self, handshake_obj: PyrightHandshake) -> None:
            super().__init__(handshake_obj)
            self._fail_next = True

        def request(
            self,
            method: str,
            params: object,
            timeout: float | None = None,
            *,
            cancellable: bool = False,
        ) -> object:
            if method == "textDocument/hover" and self._fail_next:
                self._fail_next = False
                raise PyrightSessionError("temporary hover failure")
            return super().request(method, params, timeout, cancellable=cancellable)

    monkeypatch.setattr(
        "lanser.orchestrator.create_pyright_session",
        lambda workspace, recorder=None: _FlakySession(handshake),
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    selector = "pkg/mod.py@L1:C1"

    first = orchestrator.hover(selector)
    assert not first.ok
    assert first.exit_code == ExitCode.LS_CRASH
    assert "Pyright" in first.message
    assert "temporary hover failure" in first.message
    assert first.metadata is not None
    pyright_meta = first.metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("error") == "temporary hover failure"

    second = orchestrator.hover(selector)
    assert second.ok
    assert "via Pyright" in second.message


def test_symbols_return_pyright_document_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            [
                "def foo():",
                '    """Docstring."""',
                "    def inner(value: int) -> int:",
                "        return value + 1",
                "    return inner(0)",
            ]
        )
        + "\n"
    )

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.symbols("pkg/mod.py@L1:C1")

    assert outcome.ok
    payload = outcome.payload
    assert payload is not None
    symbols = payload["result"]["symbols"]
    assert symbols

    root = symbols[0]
    assert root["symbol"]["name"] == "foo"
    assert root["symbol"]["qualname"] == "foo"
    assert root["symbol"]["lspKind"] == "function"
    assert root.get("signature", "").startswith("def foo")

    children = root.get("children", [])
    assert children
    first_child = children[0]
    assert first_child["symbol"]["name"] == "inner"
    assert first_child["symbol"]["qualname"] == "foo.inner"
    assert first_child["symbol"]["lspKind"] == "function"

    session = fake_pyright_session["session"]
    assert any(method == "textDocument/documentSymbol" for method, _ in session.requests)


def test_definition_message_reports_pyright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert outcome.ok
    assert outcome.message.endswith("via Pyright.")


def test_definition_message_remains_pyright_when_response_is_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    def _return_none(
        self: LSPOrchestrator,
        selector: object,
        context: object,
    ) -> Mapping[str, Any] | None:
        return None

    monkeypatch.setattr(LSPOrchestrator, "_pyright_definition", _return_none)

    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert outcome.ok
    assert outcome.message.endswith("via Pyright.")

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    result = payload.get("result")
    assert isinstance(result, Mapping)
    definitions = result.get("definitions")
    assert isinstance(definitions, list)
    assert definitions == []


def test_definition_message_when_pyright_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    def _raise(_: Path, recorder: object | None = None) -> None:
        raise PyrightSessionError("pyright missing")

    monkeypatch.setattr("lanser.orchestrator.create_pyright_session", _raise)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.LS_CRASH
    assert "Pyright" in outcome.message
    assert outcome.metadata is not None
    pyright_meta = outcome.metadata.get("pyright")
    assert isinstance(pyright_meta, dict)
    assert pyright_meta.get("connected") is False
    assert "pyright missing" in pyright_meta.get("error", "")
    assert pyright_meta.get("expectedVersion") == PYRIGHT_VERSION.version
    supported_versions = pyright_meta.get("supportedVersions")
    assert isinstance(supported_versions, list)
    assert tuple(supported_versions) == PYRIGHT_VERSION_SUPPORT.supported_versions
    server_version = pyright_meta.get("serverVersion")
    assert isinstance(server_version, str)
    assert server_version in PYRIGHT_VERSION_SUPPORT.supported_versions
    assert pyright_meta.get("versionMismatch") is False


def test_definition_refuses_stub_fallback_when_pyright_connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    def _force_stub(
        self: LSPOrchestrator,
        selector: Any,
        *,
        context: Any = None,
    ) -> Mapping[str, Any]:
        self._record_pyright_source("stub")
        return self._definition_result_stub(selector, context=context)

    monkeypatch.setattr(
        orchestrator,
        "_definition_result",
        MethodType(_force_stub, orchestrator),
    )

    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert outcome.ok is False
    assert outcome.exit_code == ExitCode.LS_CRASH
    assert "stub analysis fallback" in outcome.message

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    error = payload.get("error")
    assert isinstance(error, Mapping)
    assert error.get("kind") == "stub-fallback-denied"

    metadata = outcome.metadata
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("connected") is True


def test_definition_handles_pyi_without_stub_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    stub_path = tmp_path / "typings" / "typer" / "__init__.pyi"
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text("def placeholder() -> None: ...\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("typings/typer/__init__.pyi@L1:C1")

    assert outcome.ok
    assert outcome.message == "Definition bundle generated via Pyright."

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    resolution = payload.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, list)
    assert candidates
    selected = candidates[0]
    assert isinstance(selected, Mapping)
    assert selected.get("source") == "pyright"

    session = fake_pyright_session["session"]
    assert any(
        method == "textDocument/didOpen" and stub_path.as_uri() == params["textDocument"]["uri"]
        for method, params in session.notifications
    )


def test_definition_reports_invalid_utf8_without_stub_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_bytes(b"def broken():\n\xff\xfe\xfd\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.LS_CRASH
    assert "utf-8" in outcome.message.lower()

    payload = outcome.payload
    assert isinstance(payload, Mapping)
    error = payload.get("error")
    assert isinstance(error, Mapping)
    assert error.get("kind") == "analysis-target-invalid-encoding"
    assert error.get("encoding") == "utf-8"
    assert error.get("path") == str(module.resolve())
    assert isinstance(error.get("reason"), str)

    metadata = outcome.metadata
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("connected") is True


def test_definition_anchor_snippet_missing_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    snippet = quote_plus("def missing")
    selector = f"anchor://{module.relative_to(tmp_path)}#{snippet}?ctx=2"

    outcome = orchestrator.definition(selector)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.NOT_FOUND
    payload = outcome.payload
    assert isinstance(payload, Mapping)
    error = payload.get("error")
    assert isinstance(error, Mapping)
    assert error.get("kind") == "anchor-snippet-missing"


def test_definition_missing_file_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    outcome = orchestrator.definition("missing.py@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.NOT_FOUND
    payload = outcome.payload
    assert isinstance(payload, Mapping)
    error = payload.get("error")
    assert isinstance(error, Mapping)
    assert error.get("kind") == "analysis-target-unavailable"


def test_definition_symbol_missing_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    outcome = orchestrator.definition("py://pkg.mod#missing_symbol")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.NOT_FOUND
    payload = outcome.payload
    assert isinstance(payload, Mapping)
    error = payload.get("error")
    assert isinstance(error, Mapping)
    assert error.get("kind") == "symbol-not-found"


def test_definition_ast_selector_missing_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("class Present:\n    pass\n", encoding="utf-8")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    selector = "ast://[module=pkg.mod]/[class=Missing]"
    outcome = orchestrator.definition(selector)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.NOT_FOUND
    payload = outcome.payload
    assert isinstance(payload, Mapping)
    error = payload.get("error")
    assert isinstance(error, Mapping)
    assert error.get("kind") == "ast-path-unresolved"


def test_doctor_reports_pyright_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    report = orchestrator.doctor()

    assert report.ok
    payload = report.payload
    assert isinstance(payload, dict)
    pyright_meta = payload.get("pyright")
    assert isinstance(pyright_meta, dict)
    assert pyright_meta.get("connected") is True
    assert pyright_meta.get("expectedVersion") == PYRIGHT_VERSION.version
    supported_versions = pyright_meta.get("supportedVersions")
    assert isinstance(supported_versions, list)
    assert tuple(supported_versions) == PYRIGHT_VERSION_SUPPORT.supported_versions
    assert pyright_meta.get("serverVersion") == "0.0"
    assert pyright_meta.get("versionMismatch") is True
    handshake = pyright_meta.get("handshake")
    assert isinstance(handshake, dict)
    server_info = handshake.get("serverInfo")
    assert isinstance(server_info, dict)
    assert server_info.get("name") == "fake-pyright"


def test_selector_operations_report_cache_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    first = orchestrator.definition("pkg/mod.py@L1:C1")
    second = orchestrator.definition("pkg/mod.py@L1:C1")

    assert first.ok
    assert second.ok
    assert first.metadata is not None
    assert second.metadata is not None
    assert first.metadata["cache"]["hit"] is False
    assert second.metadata["cache"]["hit"] is True
    assert first.metadata["cache"]["key"] == second.metadata["cache"]["key"]
    assert first.payload == second.payload
    assert "pyright" in first.metadata
    assert first.metadata["pyright"]["connected"] is True


def test_cache_keys_include_result_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    first = orchestrator.rename("pkg/mod.py@L1:C1", new_name="one", apply=False)
    repeat = orchestrator.rename("pkg/mod.py@L1:C1", new_name="one", apply=False)
    second = orchestrator.rename("pkg/mod.py@L1:C1", new_name="two", apply=False)

    assert first.metadata is not None
    assert repeat.metadata is not None
    assert second.metadata is not None

    assert first.metadata["cache"]["hit"] is False
    assert repeat.metadata["cache"]["hit"] is True
    assert second.metadata["cache"]["hit"] is False
    assert first.metadata["cache"]["key"] != second.metadata["cache"]["key"]
    assert first.metadata["pyright"]["connected"] is True


def test_batch_executes_requests_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    requests = [
        BatchRequest(id="one", command="definition", selector="pkg/mod.py@L1:C1"),
        BatchRequest(id="two", command="definition", selector="pkg/mod.py@L1:C1"),
    ]

    responses = orchestrator.batch(requests)

    assert [response.id for response in responses] == ["one", "two"]
    first, second = responses
    assert first.payload is not None
    assert second.payload is not None
    assert first.payload["kind"] == "definition"
    assert second.metadata is not None
    assert second.metadata["cache"]["hit"] is True


def test_batch_supports_workspace_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    requests = [BatchRequest(id="diag", command="diagnostics", scope="workspace")]

    responses = orchestrator.batch(requests)
    assert len(responses) == 1
    if not _pyright_supports_workspace_diagnostics(orchestrator):
        pytest.skip("Pyright server does not advertise workspace diagnostics.")
    payload = responses[0].payload
    assert payload is not None
    assert payload["request"]["scope"] == "workspace"
    assert payload["result"]["diagnostics"]


def test_workspace_diagnostics_include_pyright_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.diagnostics(scope="workspace", selector=None)

    assert outcome.ok
    assert outcome.message.endswith("via Pyright.")

    payload = outcome.payload
    assert payload is not None
    diagnostics = payload["result"]["diagnostics"]
    if not diagnostics and not _pyright_supports_workspace_diagnostics(orchestrator):
        pytest.skip("Pyright server does not advertise workspace diagnostics.")
    assert diagnostics
    first = diagnostics[0]
    assert first["uri"].startswith("file://")
    assert first["message"] == "diagnostic from pyright"

    metadata = outcome.metadata
    assert metadata is not None
    assert metadata["pyright"]["connected"] is True


def test_workspace_diagnostics_reuse_cached_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    first = orchestrator.diagnostics(scope="workspace", selector=None)
    assert first.ok
    first_payload = first.payload
    assert first_payload is not None
    first_diag = first_payload["result"]["diagnostics"]
    if not first_diag and not _pyright_supports_workspace_diagnostics(orchestrator):
        pytest.skip("Pyright server does not advertise workspace diagnostics.")
    assert first_diag
    assert first_diag[0]["message"] == "diagnostic from pyright"

    second = orchestrator.diagnostics(scope="workspace", selector=None)
    assert second.ok
    second_payload = second.payload
    assert second_payload is not None
    second_diag = second_payload["result"]["diagnostics"]
    assert second_diag
    assert second_diag[0]["message"] == "diagnostic from pyright"

    session = fake_pyright_session["session"]
    workspace_requests = [
        params for method, params in session.requests if method == "workspace/diagnostic"
    ]
    assert len(workspace_requests) == 2

    second_params = workspace_requests[1]
    assert isinstance(second_params, dict)
    previous_ids = second_params.get("previousResultIds")
    assert isinstance(previous_ids, list)
    assert previous_ids
    descriptor = previous_ids[0]
    assert isinstance(descriptor, dict)
    assert descriptor.get("value") == "diag-1"


def test_batch_rename_requires_parameters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))

    with pytest.raises(ValueError):
        orchestrator.batch([BatchRequest(id="r1", command="rename", selector="pkg/mod.py@L1:C1")])

    responses = orchestrator.batch(
        [
            BatchRequest(
                id="r2",
                command="rename",
                selector="pkg/mod.py@L1:C1",
                new_name="alt",
                apply=True,
            )
        ]
    )

    payload = responses[0].payload
    assert payload is not None
    result_block = payload["result"]
    rename_result = result_block["rename"]
    assert rename_result["applyMode"] == "apply"
    assert rename_result["changeCount"] >= 1
    assert rename_result["changes"]
    assert rename_result["applied"] is True
    assert rename_result["applyStatus"] == "applied"
    prepare_block = rename_result.get("prepare")
    if isinstance(prepare_block, dict):
        assert prepare_block["status"] == "allowed"
    first_change = rename_result["changes"][0]
    assert first_change["newText"] == "alt"
    assert first_change["occurrence"]["role"] == "definition"
    workspace_edit = result_block["workspaceEdit"]
    assert workspace_edit is not None
    assert workspace_edit["changeCount"] == rename_result["changeCount"]
    diff_preview = result_block["diff"]
    assert diff_preview is not None
    assert diff_preview["format"] == "unified"
    assert any("alt" in line for line in diff_preview["hunks"])
    assert "alt" in module.read_text()


def test_rename_apply_synchronises_open_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.rename("pkg/mod.py@L1:C1", new_name="bar", apply=True)

    assert outcome.ok
    session = fake_pyright_session["session"]
    notifications = session.notifications
    did_open = [params for method, params in notifications if method == "textDocument/didOpen"]
    did_change = [params for method, params in notifications if method == "textDocument/didChange"]

    assert did_open
    assert did_change

    open_payload = did_open[-1]
    change_payload = did_change[-1]
    assert isinstance(open_payload, dict)
    assert isinstance(change_payload, dict)

    open_doc = open_payload.get("textDocument", {})
    change_doc = change_payload.get("textDocument", {})
    assert isinstance(open_doc, dict)
    assert isinstance(change_doc, dict)
    assert change_doc.get("uri") == open_doc.get("uri")

    open_version = open_doc.get("version")
    change_version = change_doc.get("version")
    assert isinstance(open_version, int)
    assert isinstance(change_version, int)
    assert change_version == open_version + 1

    changes = change_payload.get("contentChanges")
    assert isinstance(changes, list)
    assert changes
    last_change = changes[-1]
    assert isinstance(last_change, dict)
    assert last_change.get("text") == module.read_text()


def test_rename_apply_detects_concurrent_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    original_apply = orchestrator._apply_rename_plan

    def _patched_apply(plan):
        module.write_text("def foo():\n    return 99\n")
        return original_apply(plan)

    monkeypatch.setattr(orchestrator, "_apply_rename_plan", _patched_apply)

    outcome = orchestrator.rename("pkg/mod.py@L1:C1", new_name="bar", apply=True)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.APPLY_CONFLICT
    payload = outcome.payload
    assert payload is not None
    rename_block = payload["result"]["rename"]
    assert rename_block["applied"] is False
    assert rename_block["applyStatus"] == "stale"
    assert "return 99" in module.read_text()


def test_rename_apply_rolls_back_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "pkg"
    first = workspace / "first.py"
    second = workspace / "second.py"
    workspace.mkdir(parents=True, exist_ok=True)
    first_source = "def one():\n    return 1\n"
    second_source = "def two():\n    return 2\n"
    first.write_text(first_source)
    second.write_text(second_source)

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace_path: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    selector = "pkg/first.py@L1:C5"
    updated_texts: dict[str, str] = {}

    def _fake_rename_result(
        self: LSPOrchestrator,
        selector_spec: object,
        *,
        new_name: str,
        apply: bool,
        context: object,
    ) -> tuple[Mapping[str, Any], _RenamePlan | None]:
        del selector_spec, context
        self._record_pyright_source("pyright")
        updated_first = f"def one():\n    return {new_name}\n"
        updated_second = f"def two():\n    return {new_name}\n"
        updated_texts["first"] = updated_first
        updated_texts["second"] = updated_second
        plan = _RenamePlan(
            edits=(
                _RenameFileEdit(
                    path=first_resolved,
                    original_source=first_source,
                    updated_source=updated_first,
                ),
                _RenameFileEdit(
                    path=second_resolved,
                    original_source=second_source,
                    updated_source=updated_second,
                ),
            )
        )
        payload: Mapping[str, Any] = {
            "rename": {
                "requestedName": new_name,
                "applyMode": "apply" if apply else "preview",
                "changes": [
                    {
                        "uri": first_resolved.as_uri(),
                        "range": {"start": [1, 4], "end": [1, 7]},
                        "newText": new_name,
                        "originalText": "one",
                        "occurrence": {
                            "index": 0,
                            "line": 1,
                            "column": 4,
                            "role": "definition",
                        },
                    },
                    {
                        "uri": second_resolved.as_uri(),
                        "range": {"start": [1, 4], "end": [1, 7]},
                        "newText": new_name,
                        "originalText": "two",
                        "occurrence": {
                            "index": 1,
                            "line": 1,
                            "column": 4,
                            "role": "reference",
                        },
                    },
                ],
                "changeCount": 2,
                "applied": False,
                "applyStatus": "planned" if apply else "preview",
            },
            "workspaceEdit": {
                "documentChanges": [
                    {
                        "textDocument": {"uri": first_resolved.as_uri()},
                        "edits": [{"range": {"start": [1, 4], "end": [1, 7]}, "newText": new_name}],
                    },
                    {
                        "textDocument": {"uri": second_resolved.as_uri()},
                        "edits": [{"range": {"start": [1, 4], "end": [1, 7]}, "newText": new_name}],
                    },
                ],
                "changeCount": 2,
            },
            "diff": None,
        }
        return payload, plan

    monkeypatch.setattr(LSPOrchestrator, "_rename_result", _fake_rename_result)

    original_write_text = Path.write_text

    def _faulty_write_text(
        path_obj: Path,
        text: str,
        encoding: str | None = "utf-8",
        errors: str | None = None,
    ) -> int:
        if path_obj.resolve() == second_resolved and text == updated_texts.get("second"):
            raise OSError("simulated write failure")
        return original_write_text(path_obj, text, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "write_text", _faulty_write_text)

    outcome = orchestrator.rename(selector, new_name="replacement", apply=True)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.APPLY_CONFLICT
    payload = outcome.payload
    assert payload is not None
    rename_payload = payload["result"]["rename"]
    assert rename_payload["applyStatus"] == "io-write"
    assert first.read_text() == first_source
    assert second.read_text() == second_source


def test_rename_apply_reports_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "pkg"
    first = workspace / "first.py"
    second = workspace / "second.py"
    workspace.mkdir(parents=True, exist_ok=True)
    first_source = "def one():\n    return 1\n"
    second_source = "def two():\n    return 2\n"
    first.write_text(first_source)
    second.write_text(second_source)

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace_path: snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    selector = "pkg/first.py@L1:C5"
    updated_texts: dict[str, str] = {}

    def _fake_rename_result(
        self: LSPOrchestrator,
        selector_spec: object,
        *,
        new_name: str,
        apply: bool,
        context: object,
    ) -> tuple[Mapping[str, Any], _RenamePlan | None]:
        del selector_spec, context
        self._record_pyright_source("pyright")
        updated_first = f"def one():\n    return {new_name}\n"
        updated_second = f"def two():\n    return {new_name}\n"
        updated_texts["first"] = updated_first
        updated_texts["second"] = updated_second
        plan = _RenamePlan(
            edits=(
                _RenameFileEdit(
                    path=first_resolved,
                    original_source=first_source,
                    updated_source=updated_first,
                ),
                _RenameFileEdit(
                    path=second_resolved,
                    original_source=second_source,
                    updated_source=updated_second,
                ),
            )
        )
        payload: Mapping[str, Any] = {
            "rename": {
                "requestedName": new_name,
                "applyMode": "apply" if apply else "preview",
                "changes": [
                    {
                        "uri": first_resolved.as_uri(),
                        "range": {"start": [1, 4], "end": [1, 7]},
                        "newText": new_name,
                        "originalText": "one",
                        "occurrence": {
                            "index": 0,
                            "line": 1,
                            "column": 4,
                            "role": "definition",
                        },
                    },
                    {
                        "uri": second_resolved.as_uri(),
                        "range": {"start": [1, 4], "end": [1, 7]},
                        "newText": new_name,
                        "originalText": "two",
                        "occurrence": {
                            "index": 1,
                            "line": 1,
                            "column": 4,
                            "role": "reference",
                        },
                    },
                ],
                "changeCount": 2,
                "applied": False,
                "applyStatus": "planned" if apply else "preview",
            },
            "workspaceEdit": {
                "documentChanges": [
                    {
                        "textDocument": {"uri": first_resolved.as_uri()},
                        "edits": [{"range": {"start": [1, 4], "end": [1, 7]}, "newText": new_name}],
                    },
                    {
                        "textDocument": {"uri": second_resolved.as_uri()},
                        "edits": [{"range": {"start": [1, 4], "end": [1, 7]}, "newText": new_name}],
                    },
                ],
                "changeCount": 2,
            },
            "diff": None,
        }
        return payload, plan

    monkeypatch.setattr(LSPOrchestrator, "_rename_result", _fake_rename_result)

    original_write_text = Path.write_text

    def _faulty_write_text(
        path_obj: Path,
        text: str,
        encoding: str | None = "utf-8",
        errors: str | None = None,
    ) -> int:
        resolved = path_obj.resolve()
        if resolved == second_resolved and text == updated_texts.get("second"):
            raise OSError("simulated write failure")
        if resolved == first_resolved and text == first_source:
            raise OSError("rollback failure")
        return original_write_text(path_obj, text, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "write_text", _faulty_write_text)

    outcome = orchestrator.rename(selector, new_name="replacement", apply=True)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.APPLY_CONFLICT
    payload = outcome.payload
    assert payload is not None
    rename_payload = payload["result"]["rename"]
    assert rename_payload["applyStatus"] == "rollback-failed"
    assert first.read_text() == updated_texts["first"]
    assert second.read_text() == second_source


def test_rename_respects_prepare_rename_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pyright_session: dict[str, _FakeSession],
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("value = 1\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    handshake_obj = fake_pyright_session["handshake"]
    assert isinstance(handshake_obj, PyrightHandshake)

    class _DenySession(_FakeSession):
        def request(
            self,
            method: str,
            params: object,
            timeout: float | None = None,
            *,
            cancellable: bool = False,
        ) -> object:
            if method == "textDocument/prepareRename":
                raise PyrightSessionError('{"message": "Cannot rename here"}')
            return super().request(method, params, timeout, cancellable=cancellable)

    monkeypatch.setattr(
        "lanser.orchestrator.create_pyright_session",
        lambda workspace, recorder=None: _DenySession(handshake_obj),
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.rename("pkg/mod.py@L1:C1", new_name="other", apply=False)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.NOT_FOUND
    payload = outcome.payload
    assert payload is not None
    rename_block = payload["result"]["rename"]
    assert rename_block["applyStatus"] == "prepare-denied"
    assert rename_block["changeCount"] == 0
    assert rename_block["changes"] == []
    message_text = rename_block.get("message")
    assert isinstance(message_text, str)
    assert "Cannot rename" in message_text
    prepare_block = rename_block.get("prepare")
    assert isinstance(prepare_block, dict)
    assert prepare_block["status"] == "denied"


def test_doctor_reports_cache_fingerprints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    orchestrator.definition("pkg/mod.py@L1:C1")
    orchestrator.definition("pkg/mod.py@L1:C1")

    report = orchestrator.doctor()
    assert report.ok
    report_payload = report.payload
    assert report_payload is not None
    cache_info = report_payload["cache"]
    assert cache_info["selectorEntries"] == 1
    assert cache_info["fingerprints"]
    assert cache_info["fingerprints"][0].startswith("sha256:")


def test_workspace_jail_blocks_paths_outside_workspace(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_root = tmp_path_factory.mktemp("external")
    module = external_root / "mod.py"
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition(f"{module}@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.FS_PERMISSIONS
    payload = outcome.payload
    assert payload is not None
    assert payload["error"]["kind"] == "workspace-jail"


def test_path_filters_allow_and_deny(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed_dir = tmp_path / "allowed"
    denied_dir = tmp_path / "denied"
    allowed_dir.mkdir()
    denied_dir.mkdir()

    (allowed_dir / "one.py").write_text("print('ok')\n")
    (denied_dir / "two.py").write_text("print('no')\n")
    (tmp_path / "other.py").write_text("print('other')\n")

    snapshot = _fake_snapshot(tmp_path)
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(
        OrchestratorSettings(
            workspace=tmp_path,
            allow_paths=(allowed_dir,),
            deny_paths=(denied_dir,),
        )
    )

    allowed = orchestrator.definition("allowed/one.py@L1:C1")
    assert allowed.ok

    denied = orchestrator.definition("denied/two.py@L1:C1")
    assert not denied.ok
    assert denied.exit_code == ExitCode.FS_PERMISSIONS
    denied_payload = denied.payload
    assert denied_payload is not None
    assert denied_payload["error"]["kind"] == "path-denied"

    other = orchestrator.definition("other.py@L1:C1")
    assert not other.ok
    assert other.exit_code == ExitCode.FS_PERMISSIONS
    other_payload = other.payload
    assert other_payload is not None
    assert other_payload["error"]["kind"] == "path-not-allowed"


def test_dirty_workspace_guardrail_blocks_selector_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(
        tmp_path,
        git_root=str(tmp_path),
        git_head="deadbeef",
        git_dirty=True,
    )
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.VERSION_SKEW
    payload = outcome.payload
    assert payload is not None
    assert payload["error"]["kind"] == "workspace-dirty"


def test_dirty_workspace_guardrail_refreshes_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshots: deque[EnvironmentSnapshot] = deque(
        [
            _fake_snapshot(
                tmp_path,
                git_root=str(tmp_path),
                git_head="cafebabe",
                git_dirty=False,
                workspace_snapshot="sha256:clean",
            ),
            _fake_snapshot(
                tmp_path,
                git_root=str(tmp_path),
                git_head="cafebabe",
                git_dirty=True,
                workspace_snapshot="sha256:dirty",
            ),
        ]
    )

    def _next_snapshot(workspace: Path) -> EnvironmentSnapshot:
        if len(snapshots) > 1:
            return snapshots.popleft()
        return snapshots[0]

    monkeypatch.setattr("lanser.orchestrator.gather_environment", _next_snapshot)

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("pkg/mod.py@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.VERSION_SKEW
    payload = outcome.payload
    assert payload is not None
    error = payload["error"]
    assert error["kind"] == "workspace-dirty"
    assert error["workspaceSnapshotId"] == "sha256:dirty"
    git_info = error["git"]
    assert git_info["root"] == str(tmp_path)


def test_dirty_workspace_guardrail_reports_git_status(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    for index in range(4):
        path = tmp_path / f"file{index}.py"
        path.write_text("def foo():\n    return 42\n")

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.definition("file0.py@L1:C1")

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.VERSION_SKEW
    payload = outcome.payload
    assert payload is not None
    error = payload["error"]
    assert error["kind"] == "workspace-dirty"
    git_info = error["git"]
    status_sample = git_info.get("statusSample")
    assert isinstance(status_sample, dict)
    lines = status_sample.get("lines")
    assert isinstance(lines, list)
    assert lines
    assert any(line.strip().startswith("?? file") for line in lines)
    assert status_sample.get("total") >= len(lines)
    assert "Dirty paths:" in outcome.message
    assert "(+1 more)" in outcome.message


def test_dirty_workspace_guardrail_blocks_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(
        tmp_path,
        git_root=str(tmp_path),
        git_head="deadbeef",
        git_dirty=True,
    )
    monkeypatch.setattr(
        "lanser.orchestrator.gather_environment",
        lambda workspace: snapshot,
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=tmp_path))
    outcome = orchestrator.rename("pkg/mod.py@L1:C1", new_name="bar", apply=False)

    assert not outcome.ok
    assert outcome.exit_code == ExitCode.VERSION_SKEW
    payload = outcome.payload
    assert payload is not None
    assert payload["error"]["kind"] == "workspace-dirty"


def test_workspace_lock_guardrail_blocks_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def foo():\n    return 42\n")

    snapshot = _fake_snapshot(
        tmp_path,
        git_root=str(tmp_path),
        git_head="cafebabe",
        git_dirty=False,
    )
    monkeypatch.setattr("lanser.orchestrator.gather_environment", lambda workspace: snapshot)

    lock_path = tmp_path / "state" / "workspace.lock"
    orchestrator: LSPOrchestrator | None = None
    with WorkspaceLock(lock_path):
        orchestrator = LSPOrchestrator(
            OrchestratorSettings(
                workspace=tmp_path,
                workspace_lock_path=lock_path,
            )
        )
        blocked = orchestrator.definition("pkg/mod.py@L1:C1")

        assert not blocked.ok
        assert blocked.exit_code == ExitCode.VERSION_SKEW
        blocked_payload = blocked.payload
        assert blocked_payload is not None
        blocked_error = blocked_payload["error"]
        assert blocked_error["kind"] == "workspace-locked"
        lock_details = blocked_error["lock"]
        assert lock_details["path"] == str(lock_path)

    assert orchestrator is not None
    try:
        allowed = orchestrator.definition("pkg/mod.py@L1:C1")
        assert allowed.ok
        assert allowed.exit_code == ExitCode.OK
    finally:
        orchestrator.close()
