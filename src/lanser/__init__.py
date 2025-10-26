"""Lanser package providing CLI access to language server orchestration."""

from pydantic import BaseModel, ConfigDict

from ._version import VersionInfo, __version__, __version_info__
from .sdk import AsyncLanserClient, ClientConfig, SyncLanserClient

__all__ = [
    "__version__",
    "package_metadata",
    "ClientConfig",
    "SyncLanserClient",
    "AsyncLanserClient",
]


class PackageMetadata(BaseModel):
    """Expose immutable package metadata via Pydantic."""

    version: str

    model_config = ConfigDict(frozen=True)

    @classmethod
    def from_version_info(cls, info: VersionInfo) -> "PackageMetadata":
        """Construct metadata from a ``VersionInfo`` instance."""

        return cls(version=info.version)


package_metadata = PackageMetadata.from_version_info(__version_info__)
