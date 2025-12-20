from __future__ import annotations

import shutil
from pathlib import Path
from collections.abc import Mapping, Sequence

import pytest

from lanser.exit_codes import ExitCode
from lanser.orchestrator import LSPOrchestrator, OrchestratorSettings
from lanser.pyright_version import PYRIGHT_VERSION, PYRIGHT_VERSION_SUPPORT

PYRIGHT_BINARY = shutil.which("pyright-langserver")


def _assert_pyright_metadata(meta: Mapping[str, object]) -> None:
    assert meta.get("connected") is True
    assert meta.get("expectedVersion") == PYRIGHT_VERSION.version
    supported_versions = meta.get("supportedVersions")
    assert isinstance(supported_versions, Sequence)
    assert tuple(supported_versions) == PYRIGHT_VERSION_SUPPORT.supported_versions
    server_version = meta.get("serverVersion")
    assert isinstance(server_version, str)
    assert server_version in PYRIGHT_VERSION_SUPPORT.supported_versions
    assert meta.get("versionMismatch") is False


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_orchestrator_definition_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "def greet(name: str) -> str:",
                '    """Return a personalised greeting."""',
                '    return f"Hello {name}!"',
                "",
                'result = greet("world")',
                "",
            )
        ),
        encoding="utf-8",
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=workspace))
    try:
        outcome = orchestrator.definition("pkg/greeter.py@L1:C5")
        assert outcome.exit_code is ExitCode.OK
        assert outcome.ok is True
        assert "Pyright" in outcome.message

        payload = outcome.payload
        assert payload is not None
        result_block = payload.get("result") if isinstance(payload, Mapping) else None
        assert isinstance(result_block, Mapping)
        definitions = result_block.get("definitions")
        assert isinstance(definitions, Sequence)
        assert any(
            isinstance(entry, Mapping)
            and entry.get("uri") == module.resolve().as_uri()
            and isinstance(entry.get("range"), Mapping)
            for entry in definitions
        )

        resolution = payload.get("resolution") if isinstance(payload, Mapping) else None
        assert isinstance(resolution, Mapping)
        candidates = resolution.get("candidates")
        assert isinstance(candidates, Sequence)
        assert candidates, "resolution should include at least one candidate"
        primary = candidates[0]
        assert isinstance(primary, Mapping)
        assert primary.get("source") == "pyright"
        location = primary.get("location")
        assert isinstance(location, Mapping)
        assert location.get("uri") == module.resolve().as_uri()

        metadata = outcome.metadata
        assert metadata is not None
        pyright_meta = metadata.get("pyright") if isinstance(metadata, Mapping) else None
        assert isinstance(pyright_meta, Mapping)
        _assert_pyright_metadata(pyright_meta)
        handshake = pyright_meta.get("handshake")
        assert isinstance(handshake, Mapping)
        server_info = handshake.get("serverInfo")
        if isinstance(server_info, Mapping):
            assert server_info.get("name")
    finally:
        orchestrator.close()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_orchestrator_document_diagnostics_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "example.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
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

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=workspace))
    try:
        warmup = orchestrator.definition("pkg/example.py@L1:C5")
        assert warmup.exit_code is ExitCode.OK
        assert warmup.ok is True
        outcome = orchestrator.diagnostics(scope="document", selector="pkg/example.py@L1:C1")
        assert outcome.exit_code is ExitCode.OK
        assert outcome.ok is True
        assert "Pyright" in outcome.message

        payload = outcome.payload
        assert payload is not None
        result_block = payload.get("result") if isinstance(payload, Mapping) else None
        assert isinstance(result_block, Mapping)
        diagnostics = result_block.get("diagnostics")
        assert isinstance(diagnostics, Sequence)
        assert any(
            isinstance(entry, Mapping)
            and entry.get("uri") == module.resolve().as_uri()
            and isinstance(entry.get("message"), str)
            and "Literal[123]" in entry.get("message", "")
            for entry in diagnostics
        )

        resolution = payload.get("resolution") if isinstance(payload, Mapping) else None
        assert isinstance(resolution, Mapping)
        candidates = resolution.get("candidates")
        assert isinstance(candidates, Sequence)
        primary = candidates[0]
        assert isinstance(primary, Mapping)
        assert primary.get("source") == "pyright"
        location = primary.get("location")
        assert isinstance(location, Mapping)
        assert location.get("uri") == module.resolve().as_uri()
        assert location.get("severity") in {"error", "warning", "information", "hint"}

        metadata = outcome.metadata
        assert metadata is not None
        pyright_meta = metadata.get("pyright") if isinstance(metadata, Mapping) else None
        assert isinstance(pyright_meta, Mapping)
        _assert_pyright_metadata(pyright_meta)
    finally:
        orchestrator.close()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_orchestrator_references_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "def greet(name: str) -> str:",
                '    """Return a personalised greeting."""',
                '    return f"Hello {name}!"',
                "",
                'result = greet("world")',
                "",
            )
        ),
        encoding="utf-8",
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=workspace))
    try:
        outcome = orchestrator.references("pkg/greeter.py@L1:C5")
        assert outcome.exit_code is ExitCode.OK
        assert outcome.ok is True
        assert "Pyright" in outcome.message

        payload = outcome.payload
        assert isinstance(payload, Mapping)
        result_block = payload.get("result")
        assert isinstance(result_block, Mapping)
        references = result_block.get("references")
        assert isinstance(references, Sequence)

        uri = module.resolve().as_uri()
        assert any(
            isinstance(entry, Mapping)
            and entry.get("uri") == uri
            and entry.get("role") == "definition"
            for entry in references
        )
        assert any(
            isinstance(entry, Mapping)
            and entry.get("uri") == uri
            and entry.get("role") == "reference"
            for entry in references
        )

        resolution = payload.get("resolution")
        assert isinstance(resolution, Mapping)
        candidates = resolution.get("candidates")
        assert isinstance(candidates, Sequence)
        primary = candidates[0]
        assert isinstance(primary, Mapping)
        assert primary.get("source") == "pyright"
        location = primary.get("location")
        assert isinstance(location, Mapping)
        assert location.get("uri") == uri
        symbol = primary.get("symbol")
        assert isinstance(symbol, Mapping)
        assert symbol.get("name") == "greet"

        metadata = outcome.metadata
        assert isinstance(metadata, Mapping)
        pyright_meta = metadata.get("pyright")
        assert isinstance(pyright_meta, Mapping)
        _assert_pyright_metadata(pyright_meta)
    finally:
        orchestrator.close()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_orchestrator_rename_preview_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "def greet(name: str) -> str:",
                '    """Return a personalised greeting."""',
                '    return f"Hello {name}!"',
                "",
                'result = greet("world")',
                "",
            )
        ),
        encoding="utf-8",
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=workspace))
    try:
        outcome = orchestrator.rename("pkg/greeter.py@L1:C5", new_name="welcome", apply=False)
        assert outcome.exit_code is ExitCode.OK
        assert outcome.ok is True

        payload = outcome.payload
        assert isinstance(payload, Mapping)
        result_block = payload.get("result")
        assert isinstance(result_block, Mapping)
        rename_block = result_block.get("rename")
        assert isinstance(rename_block, Mapping)
        assert rename_block.get("applyMode") == "preview"
        assert rename_block.get("applyStatus") == "preview"
        assert rename_block.get("changeCount")

        changes = rename_block.get("changes")
        assert isinstance(changes, Sequence)
        assert changes
        assert any(
            isinstance(change, Mapping) and change.get("occurrence", {}).get("role") == "definition"
            for change in changes
        )
        assert all(
            isinstance(change, Mapping) and change.get("newText") == "welcome"
            for change in changes
            if isinstance(change, Mapping)
        )

        prepare_block = rename_block.get("prepare")
        if isinstance(prepare_block, Mapping):
            assert prepare_block.get("status") == "allowed"

        metadata = outcome.metadata
        assert isinstance(metadata, Mapping)
        pyright_meta = metadata.get("pyright")
        assert isinstance(pyright_meta, Mapping)
        _assert_pyright_metadata(pyright_meta)
    finally:
        orchestrator.close()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_orchestrator_rename_apply_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "def greet(name: str) -> str:",
                '    """Return a personalised greeting."""',
                '    return f"Hello {name}!"',
                "",
                'result = greet("world")',
                "",
            )
        ),
        encoding="utf-8",
    )

    orchestrator = LSPOrchestrator(OrchestratorSettings(workspace=workspace))
    try:
        outcome = orchestrator.rename("pkg/greeter.py@L1:C5", new_name="welcome", apply=True)
        assert outcome.exit_code is ExitCode.OK
        assert outcome.ok is True

        payload = outcome.payload
        assert isinstance(payload, Mapping)
        result_block = payload.get("result")
        assert isinstance(result_block, Mapping)
        rename_block = result_block.get("rename")
        assert isinstance(rename_block, Mapping)
        assert rename_block.get("applied") is True
        assert rename_block.get("applyStatus") == "applied"
        assert rename_block.get("changeCount")

        metadata = outcome.metadata
        assert isinstance(metadata, Mapping)
        pyright_meta = metadata.get("pyright")
        assert isinstance(pyright_meta, Mapping)
        _assert_pyright_metadata(pyright_meta)
        apply_meta = metadata.get("apply")
        assert isinstance(apply_meta, Mapping)
        assert apply_meta.get("status") == "applied"

        updated_source = module.read_text(encoding="utf-8")
        assert "def welcome(" in updated_source
        assert "result = welcome(" in updated_source
    finally:
        orchestrator.close()
