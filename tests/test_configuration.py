"""Tests for runtime configuration helpers."""

from __future__ import annotations

from pathlib import Path

from lanser.configuration import RuntimeConfig


def test_default_config_points_to_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    refreshed = RuntimeConfig(workspace=Path.cwd())
    assert refreshed.workspace == tmp_path


def test_runtime_config_serialisation(tmp_path: Path) -> None:
    allow_dir = tmp_path / "allowed"
    deny_dir = tmp_path / "denied"
    config = RuntimeConfig(
        workspace=tmp_path,
        frozen_snapshot=True,
        allow_dirty=True,
        allow_paths=(allow_dir,),
        deny_paths=(deny_dir,),
        trace_file=tmp_path / "trace.jsonl",
    )
    data = config.to_dict()
    assert data["workspace"] == str(tmp_path)
    assert data["frozen_snapshot"] is True
    assert data["allow_dirty"] is True
    assert data["allow_paths"] == [str(allow_dir)]
    assert data["deny_paths"] == [str(deny_dir)]
    assert data["trace_file"] == str(tmp_path / "trace.jsonl")
