"""Pyright version metadata validated via Pydantic."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["PyrightVersionInfo", "PYRIGHT_VERSION"]


_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class PyrightVersionInfo(BaseModel):
    """Immutable semantic version descriptor for the Pyright dependency."""

    version: str

    model_config = ConfigDict(frozen=True)

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, value: str) -> str:
        if not _SEMVER_PATTERN.fullmatch(value):
            msg = "Pyright version must follow semantic versioning (major.minor.patch)."
            raise ValueError(msg)
        return value

    @property
    def cli_label(self) -> str:
        """Return the CLI ``--version`` label for the pinned Pyright version."""

        return f"pyright {self.version}"

    def __str__(self) -> str:  # pragma: no cover - trivial proxy
        return self.version


PYRIGHT_VERSION = PyrightVersionInfo(version="1.1.406")
