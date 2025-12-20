from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lanser import cli
from lanser.exit_codes import ExitCode
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
def test_cli_definition_with_real_pyright(tmp_path: Path) -> None:
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

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "def", "pkg/greeter.py@L1:C5", "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)

    status = payload.get("status") if isinstance(payload, Mapping) else None
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    data = payload.get("payload") if isinstance(payload, Mapping) else None
    assert isinstance(data, Mapping)
    result_block = data.get("result")
    assert isinstance(result_block, Mapping)
    definitions = result_block.get("definitions")
    assert isinstance(definitions, Sequence)
    assert any(
        isinstance(entry, Mapping) and entry.get("uri") == module.resolve().as_uri()
        for entry in definitions
    )

    resolution = data.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, Sequence)
    primary = candidates[0]
    assert isinstance(primary, Mapping)
    assert primary.get("source") == "pyright"
    location = primary.get("location")
    assert isinstance(location, Mapping)
    assert location.get("uri") == module.resolve().as_uri()

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    _assert_pyright_metadata(pyright_meta)


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_definition_ast_selector_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "class Greeter:",
                "    def greet(self, name: str) -> str:",
                '        """Return a personalised greeting."""',
                '        return f"Hello {name}!"',
                "",
            )
        ),
        encoding="utf-8",
    )

    selector = "ast://[module=pkg.greeter]/[class=Greeter]/[def=greet]"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "def", selector, "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert isinstance(payload, Mapping)

    status = payload.get("status")
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("connected") is True

    bundle = payload.get("payload")
    assert isinstance(bundle, Mapping)
    resolution = bundle.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, Sequence)
    assert candidates, "resolution should include candidates"
    primary = candidates[0]
    assert isinstance(primary, Mapping)
    assert primary.get("source") == "pyright"

    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    definitions = result_block.get("definitions")
    assert isinstance(definitions, Sequence)
    assert any(
        isinstance(entry, Mapping) and entry.get("uri") == module.resolve().as_uri()
        for entry in definitions
    )


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_hover_ast_selector_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "class Greeter:",
                "    def greet(self, name: str) -> str:",
                '        """Return a personalised greeting."""',
                '        return f"Hello {name}!"',
                "",
            )
        ),
        encoding="utf-8",
    )

    selector = "ast://[module=pkg.greeter]/[class=Greeter]/[def=greet]"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "hover", selector, "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert isinstance(payload, Mapping)

    status = payload.get("status")
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("connected") is True

    bundle = payload.get("payload")
    assert isinstance(bundle, Mapping)
    resolution = bundle.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, Sequence)
    assert candidates, "resolution should include candidates"
    primary = candidates[0]
    assert isinstance(primary, Mapping)
    assert primary.get("source") == "pyright"

    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    hover_block = result_block.get("hover")
    assert isinstance(hover_block, Mapping)
    contents = hover_block.get("contents")
    assert isinstance(contents, Sequence)
    assert contents, "hover contents should be populated"


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_references_ast_selector_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "class Greeter:",
                "    def greet(self, name: str) -> str:",
                '        """Return a personalised greeting."""',
                '        return f"Hello {name}!"',
                "",
                "greeter = Greeter()",
                'result = greeter.greet("world")',
                "",
            )
        ),
        encoding="utf-8",
    )

    selector = "ast://[module=pkg.greeter]/[class=Greeter]/[def=greet]"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "references", selector, "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert isinstance(payload, Mapping)

    status = payload.get("status")
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("connected") is True

    bundle = payload.get("payload")
    assert isinstance(bundle, Mapping)
    resolution = bundle.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, Sequence)
    assert candidates, "resolution should include candidates"
    primary = candidates[0]
    assert isinstance(primary, Mapping)
    assert primary.get("source") == "pyright"

    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    references = result_block.get("references")
    assert isinstance(references, Sequence)
    assert any(
        isinstance(entry, Mapping)
        and entry.get("uri") == module.resolve().as_uri()
        and entry.get("role") == "definition"
        for entry in references
    )
    assert any(
        isinstance(entry, Mapping)
        and entry.get("uri") == module.resolve().as_uri()
        and entry.get("role") == "reference"
        for entry in references
    )


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_rename_ast_selector_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    module = workspace / "pkg" / "greeter.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "\n".join(
            (
                "class Greeter:",
                "    def greet(self, name: str) -> str:",
                '        """Return a personalised greeting."""',
                '        return f"Hello {name}!"',
                "",
                "greeter = Greeter()",
                'result = greeter.greet("world")',
                "",
            )
        ),
        encoding="utf-8",
    )

    selector = "ast://[module=pkg.greeter]/[class=Greeter]/[def=greet]"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "rename", selector, "welcome", "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert isinstance(payload, Mapping)

    status = payload.get("status")
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    assert pyright_meta.get("connected") is True

    bundle = payload.get("payload")
    assert isinstance(bundle, Mapping)
    resolution = bundle.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, Sequence)
    assert candidates, "resolution should include candidates"
    primary = candidates[0]
    assert isinstance(primary, Mapping)
    assert primary.get("source") == "pyright"

    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    rename_block = result_block.get("rename")
    assert isinstance(rename_block, Mapping)
    assert rename_block.get("applyMode") == "preview"
    assert rename_block.get("applyStatus") == "preview"
    assert rename_block.get("changeCount")


def test_cli_hover_with_real_pyright(tmp_path: Path) -> None:
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

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "hover", "pkg/greeter.py@L1:C5", "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert isinstance(payload, Mapping)

    status = payload.get("status")
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    _assert_pyright_metadata(pyright_meta)
    handshake = pyright_meta.get("handshake")
    assert isinstance(handshake, Mapping)
    assert handshake.get("capabilitiesDigest")

    bundle = payload.get("payload")
    assert isinstance(bundle, Mapping)
    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    hover_block = result_block.get("hover")
    assert isinstance(hover_block, Mapping)

    contents = hover_block.get("contents")
    assert isinstance(contents, Sequence)
    assert any(
        isinstance(entry, Mapping)
        and isinstance(entry.get("value"), str)
        and "def greet" in entry.get("value", "")
        for entry in contents
    )
    symbol = hover_block.get("symbol")
    assert isinstance(symbol, Mapping)
    assert symbol.get("name") == "greet"

    resolution = bundle.get("resolution")
    assert isinstance(resolution, Mapping)
    candidates = resolution.get("candidates")
    assert isinstance(candidates, Sequence)
    primary = candidates[0]
    assert isinstance(primary, Mapping)
    assert primary.get("source") == "pyright"
    location = primary.get("location")
    assert isinstance(location, Mapping)
    assert location.get("uri") == module.resolve().as_uri()


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_batch_diagnostics_with_real_pyright(tmp_path: Path) -> None:
    workspace = tmp_path
    source = workspace / "pkg.py"
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

    request_file = workspace / "requests.jsonl"
    request_file.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "id": "def",
                        "command": "definition",
                        "selector": "pkg.py@L1:C5",
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "id": "diag",
                        "command": "diagnostics",
                        "selector": "pkg.py@L1:C1",
                    },
                    sort_keys=True,
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "batch", "--in", str(request_file)],
    )

    assert result.exit_code == 0, result.stdout

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])

    first_status = first.get("status") if isinstance(first, Mapping) else None
    assert isinstance(first_status, Mapping)
    assert first_status.get("ok") is True
    assert first_status.get("exitCode") == int(ExitCode.OK)

    first_payload = first.get("payload") if isinstance(first, Mapping) else None
    assert isinstance(first_payload, Mapping)
    first_resolution = first_payload.get("resolution")
    assert isinstance(first_resolution, Mapping)
    first_candidates = first_resolution.get("candidates")
    assert isinstance(first_candidates, Sequence)
    first_primary = first_candidates[0]
    assert isinstance(first_primary, Mapping)
    assert first_primary.get("source") == "pyright"

    second_status = second.get("status") if isinstance(second, Mapping) else None
    assert isinstance(second_status, Mapping)
    assert second_status.get("ok") is True
    assert second_status.get("exitCode") == int(ExitCode.OK)

    second_payload = second.get("payload") if isinstance(second, Mapping) else None
    assert isinstance(second_payload, Mapping)
    second_resolution = second_payload.get("resolution")
    assert isinstance(second_resolution, Mapping)
    second_candidates = second_resolution.get("candidates")
    assert isinstance(second_candidates, Sequence)
    second_primary = second_candidates[0]
    assert isinstance(second_primary, Mapping)
    assert second_primary.get("source") == "pyright"
    second_location = second_primary.get("location")
    assert isinstance(second_location, Mapping)
    assert second_location.get("uri") == source.resolve().as_uri()

    second_result = second_payload.get("result")
    assert isinstance(second_result, Mapping)
    diagnostics = second_result.get("diagnostics")
    assert isinstance(diagnostics, Sequence)
    assert any(
        isinstance(entry, Mapping)
        and entry.get("uri") == source.resolve().as_uri()
        and "Literal[123]" in str(entry.get("message"))
        for entry in diagnostics
    )

    metadata = second.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    _assert_pyright_metadata(pyright_meta)


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_symbols_with_real_pyright(tmp_path: Path) -> None:
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
                "class Greeter:",
                "    def welcome(self, target: str) -> str:",
                "        return greet(target)",
                "",
            )
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--workspace", str(workspace), "symbols", "pkg/greeter.py@L1:C1", "--json"],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert isinstance(payload, Mapping)

    status = payload.get("status")
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    metadata = payload.get("metadata")
    assert isinstance(metadata, Mapping)
    pyright_meta = metadata.get("pyright")
    assert isinstance(pyright_meta, Mapping)
    _assert_pyright_metadata(pyright_meta)

    bundle = payload.get("payload")
    assert isinstance(bundle, Mapping)
    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    symbols = result_block.get("symbols")
    assert isinstance(symbols, Sequence)
    assert any(
        isinstance(entry, Mapping)
        and isinstance(entry.get("symbol"), Mapping)
        and entry["symbol"].get("name") == "greet"
        for entry in symbols
    )
    assert any(
        isinstance(entry, Mapping)
        and isinstance(entry.get("symbol"), Mapping)
        and entry["symbol"].get("name") == "Greeter"
        for entry in symbols
    )


@pytest.mark.skipif(PYRIGHT_BINARY is None, reason="pyright-langserver is not available")
def test_cli_rename_apply_with_real_pyright(tmp_path: Path) -> None:
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

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "--workspace",
            str(workspace),
            "rename",
            "pkg/greeter.py@L1:C5",
            "welcome",
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    status = payload.get("status") if isinstance(payload, Mapping) else None
    assert isinstance(status, Mapping)
    assert status.get("ok") is True
    assert status.get("exitCode") == int(ExitCode.OK)

    bundle = payload.get("payload") if isinstance(payload, Mapping) else None
    assert isinstance(bundle, Mapping)
    result_block = bundle.get("result")
    assert isinstance(result_block, Mapping)
    rename_block = result_block.get("rename")
    assert isinstance(rename_block, Mapping)
    assert rename_block.get("applied") is True
    assert rename_block.get("applyStatus") == "applied"
    assert rename_block.get("changeCount")

    metadata = payload.get("metadata")
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
