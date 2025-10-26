"""Pyright version metadata validated via Pydantic."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "PyrightVersionInfo",
    "PyrightVersionSupport",
    "PYRIGHT_VERSION",
    "PYRIGHT_VERSION_SUPPORT",
]


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


class PyrightVersionSupport(BaseModel):
    """Describe the supported Pyright versions for the orchestrator."""

    primary: PyrightVersionInfo
    additional: tuple[PyrightVersionInfo, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True)

    @property
    def supported_versions(self) -> tuple[str, ...]:
        """Return the ordered tuple of supported Pyright versions."""

        return (self.primary.version, *[entry.version for entry in self.additional])

    @property
    def cli_label(self) -> str:
        """Return the CLI label describing supported Pyright versions."""

        if not self.additional:
            return self.primary.cli_label
        extras = ", ".join(entry.version for entry in self.additional)
        return f"{self.primary.cli_label} (also supports: {extras})"

    def contains(self, version: str | None) -> bool:
        """Return ``True`` when ``version`` is within the supported set."""

        if version is None:
            return False
        normalized = version.strip()
        if not normalized:
            return False
        return normalized in self.supported_versions


PYRIGHT_VERSION = PyrightVersionInfo(version="1.1.407")
PYRIGHT_VERSION_SUPPORT = PyrightVersionSupport(
    primary=PYRIGHT_VERSION,
    additional=(PyrightVersionInfo(version="1.1.406"),),
)
