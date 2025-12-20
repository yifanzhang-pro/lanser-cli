"""Project version metadata validated via Pydantic."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["VersionInfo", "__version__", "__version_info__"]


_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class VersionInfo(BaseModel):
    """Immutable semantic version descriptor for the package."""

    version: str

    model_config = ConfigDict(frozen=True)

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, value: str) -> str:
        if not _SEMVER_PATTERN.fullmatch(value):
            msg = "Version must follow semantic versioning (major.minor.patch)."
            raise ValueError(msg)
        return value

    def __str__(self) -> str:  # pragma: no cover - trivial proxy
        return self.version


__version_info__ = VersionInfo(version="1.0.2")
__version__ = __version_info__.version
