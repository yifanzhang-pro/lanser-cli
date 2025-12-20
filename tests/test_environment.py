"""Tests for environment discovery utilities."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lanser.environment import DEFAULT_COMPATIBILITY_TARGETS, gather_environment
from lanser.pyright_version import PYRIGHT_VERSION_SUPPORT


def test_gather_environment_reports_python_version() -> None:
    snapshot = gather_environment()
    assert snapshot.python_version.startswith(str(sys.version_info.major))
    assert snapshot.python_executable
    assert snapshot.workspace_snapshot.startswith("sha256:")


def test_gather_environment_reports_expected_pyright_version() -> None:
    snapshot = gather_environment()
    assert snapshot.pyright_expected_version == PYRIGHT_VERSION_SUPPORT.cli_label
    assert snapshot.pyright_supported_versions == PYRIGHT_VERSION_SUPPORT.supported_versions


def test_gather_environment_reports_python_requirement() -> None:
    snapshot = gather_environment()
    assert snapshot.python_requirement is None or snapshot.python_requirement.startswith(">=")


def test_gather_environment_default_targets_cover_latest_series() -> None:
    snapshot = gather_environment()
    targets = tuple(entry.target for entry in snapshot.python_compatibility)
    for target in DEFAULT_COMPATIBILITY_TARGETS:
        assert target in targets


def test_gather_environment_evaluates_python_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lanser.environment._discover_python_requirement", lambda: ">=3.12")

    snapshot = gather_environment(python_targets=("3.11.*", "3.12.*", "3.13"))

    matrix = {entry.target: entry for entry in snapshot.python_compatibility}
    assert matrix["3.11.*"].satisfies is False
    assert matrix["3.12.*"].satisfies is True
    assert matrix["3.12.*"].normalized_version == "3.12.0"
    assert matrix["3.13"].satisfies is True


def test_gather_environment_includes_config_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "pyrightconfig.json"
    config.write_text('{\n  "typeCheckingMode": "strict"\n}\n')
    monkeypatch.chdir(tmp_path)

    snapshot = gather_environment()

    assert snapshot.config_digest is not None
    assert snapshot.config_digest.startswith("sha256:")


def test_gather_environment_reports_git_metadata(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    file_path = tmp_path / "sample.py"
    file_path.write_text("print('hi')\n")

    snapshot = gather_environment(workspace=tmp_path)

    assert snapshot.git_root is not None
    assert snapshot.git_root.endswith(str(tmp_path.name))
    assert snapshot.git_dirty is True
    assert snapshot.git_head is None
    assert snapshot.workspace_snapshot.startswith("sha256:")


def test_gather_environment_discovers_nested_project_files(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    nested_pyright = config_dir / "pyrightconfig.json"
    nested_pyright.write_text('{\n  "typeCheckingMode": "strict"\n}\n')

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    project_doc = docs_dir / "PROJECT.md"
    project_doc.write_text("# Spec\n")

    snapshot = gather_environment(workspace=tmp_path)

    assert any(path.endswith("config/pyrightconfig.json") for path in snapshot.project_files)
    assert any(path.endswith("docs/PROJECT.md") for path in snapshot.project_files)
    assert snapshot.config_digest is not None


def test_workspace_snapshot_changes_with_config_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text("[tool.pyright]\n")
    monkeypatch.chdir(tmp_path)

    first = gather_environment()
    config.write_text("[tool.pyright]\ntypeCheckingMode='strict'\n")
    second = gather_environment()

    assert first.workspace_snapshot != second.workspace_snapshot
