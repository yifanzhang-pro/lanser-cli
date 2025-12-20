"""Analysis bundle primitives for orchestrator responses."""

from __future__ import annotations

import hashlib
import json
from collections import abc
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

if TYPE_CHECKING:
    from .selectors import PositionSpec

__all__ = ["AnalysisBundle"]


class AnalysisBundle(BaseModel):
    """Represents a deterministic analysis bundle payload."""

    kind: str
    request: abc.Mapping[str, Any]
    environment: abc.Mapping[str, Any]
    resolution: abc.Mapping[str, Any]
    result: abc.Mapping[str, Any] = Field(default_factory=dict)
    schema_version: ClassVar[str] = "analysis-bundle.v1"

    model_config = ConfigDict(frozen=True)

    @computed_field
    @property
    def request_id(self) -> str:
        """Return a deterministic identifier for the originating request."""

        canonical = json.dumps(
            {"kind": self.kind, "request": dict(self.request)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return f"sha256:{digest}"

    def _base_payload(self) -> dict[str, Any]:
        """Return the payload excluding ``bundleId`` for hashing."""

        request_payload = dict(self.request)
        request_payload.setdefault("requestId", self.request_id)
        return {
            "schemaVersion": self.schema_version,
            "kind": self.kind,
            "request": request_payload,
            "environment": dict(self.environment),
            "resolution": dict(self.resolution),
            "result": dict(self.result),
        }

    def compute_bundle_id(self) -> str:
        """Compute a deterministic hash for the bundle."""

        canonical = json.dumps(
            self._base_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return f"sha256:{digest}"

    def to_dict(self) -> dict[str, Any]:
        """Serialise the bundle including ``bundleId``."""

        payload = self._base_payload()
        payload["bundleId"] = self.compute_bundle_id()
        return payload

    @classmethod
    def for_selector(
        cls,
        kind: str,
        selector: PositionSpec,
        environment: abc.Mapping[str, Any],
        resolution: abc.Mapping[str, Any],
        result: abc.Mapping[str, Any] | None = None,
    ) -> AnalysisBundle:
        """Convenience constructor for selector-driven bundles."""

        request = {
            "selector": selector.to_payload(),
        }
        return cls(
            kind=kind,
            request=request,
            environment=environment,
            resolution=resolution,
            result=result or {},
        )
