"""JSON Schema definitions for public Lanser payload contracts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

JSON_SCHEMA_DRAFT_URI = "https://json-schema.org/draft/2020-12/schema"

__all__ = [
    "JSON_SCHEMA_DRAFT_URI",
    "SchemaDescriptor",
    "SchemaKind",
    "analysis_bundle_schema",
    "environment_metadata_schema",
    "repositioning_schema",
    "resolution_schema",
    "schema_for",
    "schema_model_for",
    "schema_descriptors",
    "validate_schema_payload",
    "validate_schema_file",
    "validate_schema_files",
    "SchemaValidationEntry",
    "SchemaValidationSummary",
    "SchemaValidationError",
]


class SchemaKind(str, Enum):
    """Enumerate JSON schemas exposed to CLI consumers."""

    ANALYSIS_BUNDLE = "analysis-bundle"
    ENVIRONMENT_METADATA = "environment-metadata"
    RESOLUTION = "resolution"
    REPOSITIONING = "repositioning"


class SchemaDescriptor(BaseModel):
    """Immutable descriptor advertised by ``lanser schema list``."""

    name: SchemaKind
    description: str

    model_config = ConfigDict(frozen=True)


class _RequestEnvelope(BaseModel):
    """Common request envelope fields shared across bundles."""

    request_id: str = Field(
        alias="requestId",
        description="Deterministic identifier derived from the request payload.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class _GitMetadata(BaseModel):
    """Captured Git state for the active workspace."""

    root: str | None = Field(
        default=None,
        description="Absolute path to the Git repository root when detected.",
    )
    head: str | None = Field(
        default=None,
        description="Current Git HEAD reference or commit hash when available.",
    )
    dirty: bool | None = Field(
        default=None,
        description="Indicates whether uncommitted changes were detected.",
    )

    model_config = ConfigDict(extra="forbid")


class _PythonCompatibilityEntry(BaseModel):
    """Compatibility evaluation for a single interpreter target."""

    target: str
    normalized_version: str | None = Field(default=None, alias="normalizedVersion")
    satisfies: bool | None = Field(
        default=None,
        description="Indicates whether the target satisfies the interpreter requirement.",
    )
    reason: str | None = Field(
        default=None,
        description="Optional explanation when compatibility cannot be determined.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class _EnvironmentMetadata(BaseModel):
    """Snapshot of orchestrator runtime and workspace metadata."""

    schema_version: Literal["env-meta.v1"] = Field(
        alias="schemaVersion",
        description="Version identifier for the environment metadata schema.",
    )
    workspace: str = Field(description="Workspace root path for the orchestrator run.")
    position_encoding: str = Field(
        alias="positionEncoding",
        description="Position encoding negotiated with the language server.",
    )
    frozen_snapshot: bool = Field(
        alias="frozenSnapshot",
        description="Indicates whether frozen snapshot mode is active.",
    )
    workspace_snapshot_id: str = Field(
        alias="workspaceSnapshotId",
        description="Deterministic digest identifying the workspace snapshot.",
    )
    python_version: str = Field(alias="pythonVersion")
    python_executable: str = Field(alias="pythonExecutable")
    python_requirement: str | None = Field(
        default=None,
        alias="pythonRequirement",
        description="Python requirement string extracted from package metadata.",
    )
    platform: str
    cwd: str = Field(description="Working directory used when collecting metadata.")
    pyright_version: str | None = Field(
        default=None,
        alias="pyrightVersion",
        description="Pyright version discovered during environment gathering.",
    )
    pyright_expected_version: str | None = Field(
        default=None,
        alias="pyrightExpectedVersion",
        description="Pyright version the orchestrator expects to negotiate.",
    )
    pyright_supported_versions: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="pyrightSupportedVersions",
        description="Ordered list of Pyright versions accepted by the orchestrator.",
    )
    project_files: tuple[str, ...] = Field(
        alias="projectFiles",
        description="Project configuration files captured in the snapshot.",
    )
    server_version: str | None = Field(
        default=None,
        alias="serverVersion",
        description="Language server version reported by the active session.",
    )
    server_version_mismatch: bool | None = Field(
        default=None,
        alias="serverVersionMismatch",
        description="True when the connected server version differs from the expected version.",
    )
    config_digest: str | None = Field(
        default=None,
        alias="configDigest",
        description="Digest of configuration inputs included in the snapshot.",
    )
    git: _GitMetadata
    python_compatibility: tuple[_PythonCompatibilityEntry, ...] = Field(
        default_factory=tuple,
        alias="pythonCompatibility",
        description="Compatibility evaluation against specific interpreter targets.",
    )
    language_server: dict[str, Any] | None = Field(
        default=None,
        alias="languageServer",
        description="Raw metadata describing the connected language server, when available.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class _ResolutionExplanation(BaseModel):
    """Narrative context describing how selector repositioning succeeded."""

    version: str
    strategy: str
    notes: str | None = None

    model_config = ConfigDict(extra="allow")


class _ResolutionCandidate(BaseModel):
    """Single ranked candidate describing selector anchoring."""

    rank: int
    spec: dict[str, Any]
    score: float
    source: str
    reason: str
    symbol: dict[str, Any] | None = None
    selection_range: dict[str, Any] | None = Field(default=None, alias="selectionRange")
    document_uri: str | None = Field(default=None, alias="documentUri")
    location: dict[str, Any] | None = None
    details: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class _ResolutionPayload(BaseModel):
    """Resolution metadata emitted with each analysis bundle."""

    schema_version: Literal["resolution.v1"] = Field(alias="schemaVersion")
    status: str
    selected: dict[str, Any]
    candidates: tuple[_ResolutionCandidate, ...]
    explanation: _ResolutionExplanation
    repositioning: _RepositioningPayload

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class _RepositioningPayload(BaseModel):
    """Selector repositioning metadata describing relocation strategies."""

    schema_version: Literal["repositioning.v1"] = Field(alias="schemaVersion")
    target: dict[str, Any]
    strategy: str | None = None
    confidence: float | None = None
    notes: str | None = None
    fallbacks: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    anchor: dict[str, Any] | None = None
    symbol: dict[str, Any] | None = None
    path: tuple[dict[str, Any], ...] | None = None
    window: dict[str, Any] | None = None
    cursor: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class _AnalysisBundlePayload(BaseModel):
    """Canonical JSON payload returned for selector-driven operations."""

    schema_version: Literal["analysis-bundle.v1"] = Field(alias="schemaVersion")
    bundle_id: str = Field(alias="bundleId")
    kind: str
    request: _RequestEnvelope
    environment: _EnvironmentMetadata
    resolution: _ResolutionPayload
    result: dict[str, Any]

    model_config = ConfigDict(populate_by_name=True, extra="allow")


_DESCRIPTORS: tuple[SchemaDescriptor, ...] = (
    SchemaDescriptor(
        name=SchemaKind.ANALYSIS_BUNDLE,
        description="Analysis bundle envelope emitted by selector-driven commands.",
    ),
    SchemaDescriptor(
        name=SchemaKind.ENVIRONMENT_METADATA,
        description="Environment metadata captured for orchestrator sessions.",
    ),
    SchemaDescriptor(
        name=SchemaKind.RESOLUTION,
        description="Selector resolution metadata describing anchoring details.",
    ),
    SchemaDescriptor(
        name=SchemaKind.REPOSITIONING,
        description="Selector repositioning strategies and fallbacks.",
    ),
)


class SchemaValidationError(ValueError):
    """Raised when payload validation fails for a published schema."""

    kind: SchemaKind
    errors: tuple[dict[str, object], ...]

    def __init__(
        self,
        *,
        kind: SchemaKind,
        errors: list[dict[str, object]],
    ) -> None:
        message = f"{kind.value} payload failed schema validation."
        super().__init__(message)
        self.kind = kind
        self.errors = tuple(dict(entry) for entry in errors)


def schema_descriptors() -> tuple[SchemaDescriptor, ...]:
    """Return all schema descriptors in deterministic order."""

    return _DESCRIPTORS


def analysis_bundle_schema() -> dict[str, Any]:
    """Return the JSON schema for analysis bundle payloads."""

    schema = _AnalysisBundlePayload.model_json_schema(by_alias=True)
    schema.setdefault("$schema", JSON_SCHEMA_DRAFT_URI)
    return schema


def environment_metadata_schema() -> dict[str, Any]:
    """Return the JSON schema for environment metadata payloads."""

    schema = _EnvironmentMetadata.model_json_schema(by_alias=True)
    schema.setdefault("$schema", JSON_SCHEMA_DRAFT_URI)
    return schema


def resolution_schema() -> dict[str, Any]:
    """Return the JSON schema for resolution metadata payloads."""

    schema = _ResolutionPayload.model_json_schema(by_alias=True)
    schema.setdefault("$schema", JSON_SCHEMA_DRAFT_URI)
    return schema


def repositioning_schema() -> dict[str, Any]:
    """Return the JSON schema for selector repositioning metadata."""

    schema = _RepositioningPayload.model_json_schema(by_alias=True)
    schema.setdefault("$schema", JSON_SCHEMA_DRAFT_URI)
    return schema


_SCHEMA_FACTORY = {
    SchemaKind.ANALYSIS_BUNDLE: analysis_bundle_schema,
    SchemaKind.ENVIRONMENT_METADATA: environment_metadata_schema,
    SchemaKind.RESOLUTION: resolution_schema,
    SchemaKind.REPOSITIONING: repositioning_schema,
}


_SCHEMA_MODELS: dict[SchemaKind, type[BaseModel]] = {
    SchemaKind.ANALYSIS_BUNDLE: _AnalysisBundlePayload,
    SchemaKind.ENVIRONMENT_METADATA: _EnvironmentMetadata,
    SchemaKind.RESOLUTION: _ResolutionPayload,
    SchemaKind.REPOSITIONING: _RepositioningPayload,
}


def schema_for(kind: SchemaKind) -> dict[str, Any]:
    """Return the JSON schema associated with ``kind``."""

    factory = _SCHEMA_FACTORY[kind]
    return factory().copy()


def schema_model_for(kind: SchemaKind) -> type[BaseModel]:
    """Return the Pydantic model that defines ``kind``."""

    model = _SCHEMA_MODELS.get(kind)
    if model is None:  # pragma: no cover - defensive guard
        msg = f"No schema model registered for kind: {kind.value}"
        raise KeyError(msg)
    return model


def validate_schema_payload(
    kind: SchemaKind,
    payload: Mapping[str, Any],
) -> BaseModel:
    """Validate ``payload`` against the schema identified by ``kind``."""

    if not isinstance(payload, Mapping):
        msg = f"{kind.value} schema validation requires a mapping payload."
        raise TypeError(msg)

    model = schema_model_for(kind)
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        details = [dict(entry) for entry in error.errors()]
        raise SchemaValidationError(kind=kind, errors=details) from error


class SchemaValidationEntry(BaseModel):
    """Outcome for validating a single payload file."""

    path: str
    ok: bool
    errors: tuple[dict[str, object], ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True)


class SchemaValidationSummary(BaseModel):
    """Aggregated results for validating multiple payload files."""

    kind: SchemaKind
    total: int
    passed: int
    failed: int
    results: tuple[SchemaValidationEntry, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True, use_enum_values=True)


def _validation_failure(
    payload_path: Path,
    *,
    message: str,
    error_type: str,
) -> SchemaValidationEntry:
    """Return a schema validation entry describing a failure condition."""

    location: tuple[object, ...] = ()
    detail: dict[str, object] = {"msg": message, "type": error_type, "loc": location}
    return SchemaValidationEntry(path=str(payload_path), ok=False, errors=(detail,))


def validate_schema_file(kind: SchemaKind, payload_path: Path) -> SchemaValidationEntry:
    """Validate a JSON payload located at ``payload_path`` against ``kind``."""

    try:
        payload_text = payload_path.read_text(encoding="utf-8")
    except OSError as error:
        message = f"Failed to read payload: {error}"
        return _validation_failure(payload_path, message=message, error_type="io_error")

    try:
        raw_payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        message = f"Failed to parse JSON payload: {error}"
        return _validation_failure(payload_path, message=message, error_type="json_decode")

    if not isinstance(raw_payload, Mapping):
        payload_type = type(raw_payload).__name__
        message = f"{kind.value} schema expects a JSON object payload; received {payload_type}."
        return _validation_failure(payload_path, message=message, error_type="type_error")

    payload = cast("Mapping[str, Any]", raw_payload)

    try:
        validate_schema_payload(kind, payload)
    except SchemaValidationError as error:
        errors = tuple(dict(entry) for entry in error.errors)
        return SchemaValidationEntry(path=str(payload_path), ok=False, errors=errors)

    return SchemaValidationEntry(path=str(payload_path), ok=True)


def validate_schema_files(
    kind: SchemaKind,
    payloads: Sequence[Path] | Iterable[Path],
) -> SchemaValidationSummary:
    """Validate multiple payload files and return an aggregated summary."""

    entries = [validate_schema_file(kind, Path(path)) for path in payloads]
    total = len(entries)
    passed = sum(1 for entry in entries if entry.ok)
    summary = SchemaValidationSummary(
        kind=kind,
        total=total,
        passed=passed,
        failed=total - passed,
        results=tuple(entries),
    )
    return summary
