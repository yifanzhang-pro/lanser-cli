from __future__ import annotations

import json
from pathlib import Path
from platform import python_version

import pytest

from lanser.schemas import (
    SchemaKind,
    SchemaValidationError,
    SchemaValidationSummary,
    validate_schema_files,
    validate_schema_payload,
)


PYTHON_VERSION = python_version()


def _valid_environment_payload(workspace: str) -> dict[str, object]:
    return {
        "schemaVersion": "env-meta.v1",
        "workspace": workspace,
        "positionEncoding": "utf-16",
        "frozenSnapshot": False,
        "workspaceSnapshotId": "sha256:abc",
        "pythonVersion": PYTHON_VERSION,
        "pythonExecutable": "/usr/bin/python3",
        "pythonRequirement": ">=3.12",
        "pythonCompatibility": [
            {
                "target": "3.12.*",
                "normalizedVersion": "3.12.0",
                "satisfies": True,
                "reason": None,
            }
        ],
        "platform": "Linux",
        "cwd": workspace,
        "projectFiles": [],
        "git": {"root": workspace, "head": "deadbeef", "dirty": False},
    }


def test_validate_schema_payload_returns_model(tmp_path: Path) -> None:
    payload = _valid_environment_payload(str(tmp_path))
    model = validate_schema_payload(SchemaKind.ENVIRONMENT_METADATA, payload)
    assert model.schema_version == "env-meta.v1"
    assert model.git.root == str(tmp_path)


def test_validate_schema_payload_raises_error(tmp_path: Path) -> None:
    payload = _valid_environment_payload(str(tmp_path))
    payload.pop("workspaceSnapshotId")
    with pytest.raises(SchemaValidationError) as error_info:
        validate_schema_payload(SchemaKind.ENVIRONMENT_METADATA, payload)
    assert error_info.value.kind is SchemaKind.ENVIRONMENT_METADATA
    assert error_info.value.errors


def test_validate_schema_files_returns_summary(tmp_path: Path) -> None:
    payload = _valid_environment_payload(str(tmp_path))
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    summary = validate_schema_files(SchemaKind.ENVIRONMENT_METADATA, [payload_file])
    assert isinstance(summary, SchemaValidationSummary)
    assert summary.total == 1
    assert summary.passed == 1
    assert summary.failed == 0
    assert summary.results[0].ok is True


def test_validate_schema_files_reports_errors(tmp_path: Path) -> None:
    payload = _valid_environment_payload(str(tmp_path))
    payload.pop("workspaceSnapshotId")
    payload_file = tmp_path / "invalid.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    summary = validate_schema_files(SchemaKind.ENVIRONMENT_METADATA, [payload_file])
    assert summary.total == 1
    assert summary.failed == 1
    assert not summary.results[0].ok
    assert summary.results[0].errors
