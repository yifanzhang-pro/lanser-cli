"""Utilities for emitting SARIF logs from diagnostics bundles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SarifLog", "diagnostics_to_sarif"]


def _ensure_mapping(value: object) -> Mapping[str, Any] | None:
    """Return ``value`` when it is a mapping with string keys."""

    if not isinstance(value, Mapping):
        return None
    keys = cast("tuple[object, ...]", tuple(value.keys()))
    for key in keys:
        if not isinstance(key, str):
            return None
    return cast("Mapping[str, Any]", value)


def _ensure_sequence(value: object) -> Sequence[object] | None:
    """Return ``value`` when it is a non-string sequence."""

    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray | str):
        return None
    return cast("Sequence[object]", value)


class SarifMessage(BaseModel):
    """Human-readable text describing a SARIF entity."""

    text: str

    model_config = ConfigDict(frozen=True)


class SarifArtifactLocation(BaseModel):
    """Identify an artifact (file) referenced by a diagnostic."""

    uri: str

    model_config = ConfigDict(frozen=True)


class SarifRegion(BaseModel):
    """Describe the source range associated with a diagnostic."""

    startLine: int | None = None
    startColumn: int | None = None
    endLine: int | None = None
    endColumn: int | None = None

    model_config = ConfigDict(frozen=True)


class SarifPhysicalLocation(BaseModel):
    """Physical location information for a diagnostic."""

    artifactLocation: SarifArtifactLocation
    region: SarifRegion | None = None

    model_config = ConfigDict(frozen=True)


class SarifLocation(BaseModel):
    """Capture a location referenced by a diagnostic result."""

    physicalLocation: SarifPhysicalLocation
    message: SarifMessage | None = None

    model_config = ConfigDict(frozen=True)


class SarifReportingDescriptor(BaseModel):
    """Describe an analysis rule surfaced in SARIF output."""

    id: str
    name: str | None = None
    shortDescription: SarifMessage | None = None

    model_config = ConfigDict(frozen=True)


class SarifToolComponent(BaseModel):
    """Metadata describing the tool that produced the SARIF log."""

    name: str
    version: str | None = None
    rules: tuple[SarifReportingDescriptor, ...] | None = None

    model_config = ConfigDict(frozen=True)


class SarifTool(BaseModel):
    """Capture tool metadata for a SARIF run."""

    driver: SarifToolComponent

    model_config = ConfigDict(frozen=True)


class SarifResult(BaseModel):
    """Represent a single diagnostic surfaced in SARIF output."""

    ruleId: str | None = None
    level: str
    message: SarifMessage
    locations: tuple[SarifLocation, ...]
    kind: str = "fail"
    relatedLocations: tuple[SarifLocation, ...] | None = None
    properties: Mapping[str, Any] | None = None

    model_config = ConfigDict(frozen=True)


class SarifRun(BaseModel):
    """Describe a SARIF run containing diagnostic results."""

    tool: SarifTool
    results: tuple[SarifResult, ...]
    columnKind: str | None = None
    properties: Mapping[str, Any] | None = None

    model_config = ConfigDict(frozen=True)


class SarifLog(BaseModel):
    """Top-level SARIF log structure."""

    version: Literal["2.1.0"] = "2.1.0"
    schema_uri: str = Field(
        default="https://json.schemastore.org/sarif-2.1.0.json",
        alias="$schema",
    )
    runs: tuple[SarifRun, ...]

    model_config = ConfigDict(frozen=True, populate_by_name=True)


def _severity_to_level(severity: str) -> str:
    """Map diagnostics severities to SARIF result levels."""

    severity_map = {
        "error": "error",
        "warning": "warning",
        "information": "note",
        "hint": "note",
    }
    return severity_map.get(severity.lower(), "note")


def _encoding_to_column_kind(value: str | None) -> str | None:
    """Translate LSP position encoding to SARIF column kinds."""

    if value is None:
        return None
    lowered = value.lower()
    if lowered == "utf-16":
        return "utf16CodeUnits"
    if lowered == "utf-8":
        return "utf8CodeUnits"
    if lowered == "utf-32":
        return "unicodeCodePoints"
    return None


def _region_from_range(range_mapping: Mapping[str, Any]) -> SarifRegion:
    """Convert an LSP range mapping into a SARIF region."""

    start = _ensure_mapping(range_mapping.get("start"))
    end = _ensure_mapping(range_mapping.get("end"))

    start_line: int | None = None
    start_column: int | None = None
    end_line: int | None = None
    end_column: int | None = None

    if start is not None:
        start_line_value = start.get("line")
        if isinstance(start_line_value, int):
            start_line = start_line_value + 1
        start_column_value = start.get("character")
        if isinstance(start_column_value, int):
            start_column = start_column_value + 1

    if end is not None:
        end_line_value = end.get("line")
        if isinstance(end_line_value, int):
            end_line = end_line_value + 1
        end_column_value = end.get("character")
        if isinstance(end_column_value, int):
            end_column = end_column_value + 1

    return SarifRegion(
        startLine=start_line,
        startColumn=start_column,
        endLine=end_line,
        endColumn=end_column,
    )


def _location_from_entry(entry: Mapping[str, Any]) -> SarifLocation | None:
    """Create a SARIF location from a diagnostic entry."""

    uri_value = entry.get("uri")
    if not isinstance(uri_value, str) or not uri_value:
        return None

    range_mapping = _ensure_mapping(entry.get("range"))
    region = _region_from_range(range_mapping) if range_mapping is not None else None

    message_value = entry.get("message")
    location_message = SarifMessage(text=message_value) if isinstance(message_value, str) else None

    return SarifLocation(
        physicalLocation=SarifPhysicalLocation(
            artifactLocation=SarifArtifactLocation(uri=uri_value),
            region=region,
        ),
        message=location_message,
    )


def diagnostics_to_sarif(
    bundle: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> SarifLog:
    """Convert a diagnostics analysis bundle into a SARIF log."""

    if bundle.get("kind") != "diagnostics":
        msg = "Expected a diagnostics analysis bundle"
        raise ValueError(msg)

    environment = _ensure_mapping(bundle.get("environment")) or {}
    result_mapping = _ensure_mapping(bundle.get("result")) or {}
    diagnostics_sequence = _ensure_sequence(result_mapping.get("diagnostics")) or []

    tool_metadata = _ensure_mapping(environment.get("languageServer")) or {}
    server_info = _ensure_mapping(tool_metadata.get("serverInfo")) or {}
    tool_name = server_info.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        tool_name = "Pyright"
    tool_version = server_info.get("version")
    if not isinstance(tool_version, str):
        pyright_meta = _ensure_mapping((metadata or {}).get("pyright"))
        version_candidate = None
        if pyright_meta is not None:
            version_candidate = pyright_meta.get("serverVersion")
            if not isinstance(version_candidate, str):
                version_candidate = pyright_meta.get("expectedVersion")
        else:
            version_candidate = environment.get("pyrightVersion")
        if isinstance(version_candidate, str):
            tool_version = version_candidate

    rules: dict[str, SarifReportingDescriptor] = {}
    results: list[SarifResult] = []

    for entry_obj in diagnostics_sequence:
        entry = _ensure_mapping(entry_obj)
        if entry is None:
            continue

        message_value = entry.get("message")
        if not isinstance(message_value, str):
            continue

        location = _location_from_entry(entry)
        if location is None:
            continue

        severity_value = entry.get("severity")
        severity = severity_value if isinstance(severity_value, str) else "information"
        level = _severity_to_level(severity)

        code_value = entry.get("code")
        code = code_value if isinstance(code_value, str) else None
        if code:
            rules.setdefault(code, SarifReportingDescriptor(id=code, name=code))

        related_locations_payload = _ensure_sequence(entry.get("relatedInformation"))
        related_locations: list[SarifLocation] = []
        if related_locations_payload is not None:
            for related_obj in related_locations_payload:
                related_entry = _ensure_mapping(related_obj)
                if related_entry is None:
                    continue
                related_location = _location_from_entry(related_entry)
                if related_location is None:
                    continue
                related_locations.append(related_location)

        properties: dict[str, Any] = {}
        source_value = entry.get("source")
        if isinstance(source_value, str):
            properties["source"] = source_value
        tags_value = _ensure_sequence(entry.get("tags"))
        if tags_value is not None:
            tags = [tag for tag in tags_value if isinstance(tag, int)]
            if tags:
                properties["tags"] = tags
        if severity:
            properties.setdefault("severity", severity)

        result = SarifResult(
            ruleId=code,
            level=level,
            message=SarifMessage(text=message_value),
            locations=(location,),
            relatedLocations=tuple(related_locations) if related_locations else None,
            properties=properties or None,
        )
        results.append(result)

    column_kind = _encoding_to_column_kind(
        environment.get("positionEncoding")
        if isinstance(environment.get("positionEncoding"), str)
        else None
    )

    run_properties: dict[str, Any] = {}
    workspace_value = environment.get("workspace")
    if isinstance(workspace_value, str):
        run_properties["workspace"] = workspace_value
    snapshot_value = environment.get("workspaceSnapshotId")
    if isinstance(snapshot_value, str):
        run_properties["workspaceSnapshotId"] = snapshot_value

    tool_component = SarifToolComponent(
        name=tool_name,
        version=tool_version,
        rules=tuple(rules.values()) if rules else None,
    )

    run = SarifRun(
        tool=SarifTool(driver=tool_component),
        results=tuple(results),
        columnKind=column_kind,
        properties=run_properties or None,
    )

    return SarifLog(runs=(run,))
