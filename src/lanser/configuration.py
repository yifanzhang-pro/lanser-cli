"""Configuration helpers for the CLI and SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["RuntimeConfig", "DEFAULT_CONFIG"]


class RuntimeConfig(BaseModel):
    """Represents runtime configuration resolved at invocation."""

    workspace: Path
    frozen_snapshot: bool = False
    position_encoding: str = "utf-16"
    allow_dirty: bool = False
    allow_paths: tuple[Path, ...] = Field(default_factory=tuple)
    deny_paths: tuple[Path, ...] = Field(default_factory=tuple)
    trace_file: Path | None = None
    workspace_lock: bool = True

    model_config = ConfigDict(frozen=True)

    def to_dict(self) -> dict[str, Any]:
        """Return a serialisable mapping suitable for JSON export."""

        return {
            "workspace": str(self.workspace),
            "frozen_snapshot": self.frozen_snapshot,
            "position_encoding": self.position_encoding,
            "allow_dirty": self.allow_dirty,
            "allow_paths": [str(path) for path in self.allow_paths],
            "deny_paths": [str(path) for path in self.deny_paths],
            "trace_file": str(self.trace_file) if self.trace_file is not None else None,
            "workspace_lock": self.workspace_lock,
        }


DEFAULT_CONFIG = RuntimeConfig(workspace=Path.cwd())
