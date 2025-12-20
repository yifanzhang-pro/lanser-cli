"""Command line interface for the Lanser orchestrator."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import click
import typer

from . import __version__
from .configuration import DEFAULT_CONFIG, RuntimeConfig
from .environment import gather_environment
from .exit_codes import ExitCode
from .orchestrator import (
    BatchCommand,
    BatchRequest,
    BatchResponse,
    LSPOrchestrator,
    OperationOutcome,
    OrchestratorSettings,
)
from .sarif import SarifLog, diagnostics_to_sarif
from .schemas import (
    SchemaKind,
    SchemaValidationError,
    SchemaValidationSummary,
    schema_descriptors,
    schema_for,
    validate_schema_files,
    validate_schema_payload,
)
from .trace import (
    JsonRpcTraceRecorder,
    TraceOperationRecord,
    load_trace_operations,
    load_trace_summary,
)

__all__ = ["app", "main"]


app = typer.Typer(
    add_completion=False,
    help="CLI-first orchestration layer for agent-grade language server workflows.",
)
config_app = typer.Typer(help="Inspect and manage runtime configuration.")
app.add_typer(config_app, name="config")
schema_app = typer.Typer(help="Inspect published JSON schema definitions.")
app.add_typer(schema_app, name="schema")
trace_app = typer.Typer(help="Inspect recorded trace logs for orchestrator runs.")
app.add_typer(trace_app, name="trace")


_BATCH_COMMANDS: frozenset[str] = frozenset(
    {
        "definition",
        "references",
        "hover",
        "symbols",
        "diagnostics",
        "rename",
    }
)
_BATCH_SCOPES: frozenset[str] = frozenset({"document", "workspace"})

_EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})


def _normalise_filter_path(workspace: Path, path: Path) -> Path:
    """Return ``path`` resolved relative to ``workspace`` if needed."""

    if path.is_absolute():
        return path.resolve()
    return (workspace / path).resolve()


def _build_orchestrator(
    config: RuntimeConfig,
    *,
    progress_handler: Callable[[Mapping[str, Any]], None] | None = None,
) -> LSPOrchestrator:
    trace_recorder: JsonRpcTraceRecorder | None = None
    if config.trace_file is not None:
        trace_recorder = JsonRpcTraceRecorder(config.trace_file)
    lock_path: Path | None = None
    if config.workspace_lock:
        lock_path = (config.workspace / ".lanser" / "locks" / "workspace.lock").resolve()
    settings = OrchestratorSettings(
        workspace=config.workspace,
        frozen_snapshot=config.frozen_snapshot,
        position_encoding=config.position_encoding,
        allow_dirty=config.allow_dirty,
        allow_paths=tuple(
            _normalise_filter_path(config.workspace, path) for path in config.allow_paths
        )
        if config.allow_paths
        else None,
        deny_paths=tuple(
            _normalise_filter_path(config.workspace, path) for path in config.deny_paths
        )
        if config.deny_paths
        else None,
        workspace_lock_path=lock_path,
    )
    return LSPOrchestrator(
        settings=settings,
        progress_handler=progress_handler,
        trace_recorder=trace_recorder,
    )


def _echo_mapping(title: str, payload: Mapping[str, Any]) -> None:
    """Render ``payload`` under ``title`` with deterministic ordering."""

    typer.echo(title)
    for key in sorted(payload):
        value = payload[key]
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        typer.echo(f"  {key}: {encoded}")


@trace_app.command("inspect")
def trace_inspect(
    trace_file: Path = typer.Argument(..., help="Trace JSONL file produced by --trace-file."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON summary instead of human-readable output.",
    ),
) -> None:
    """Summarise metadata and outcomes captured in a trace log."""

    try:
        summary = load_trace_summary(trace_file)
    except ValueError as error:  # pragma: no cover - handled by Typer
        raise typer.BadParameter(str(error)) from error

    if json_output:
        payload = summary.model_dump(mode="json", by_alias=True)
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    typer.echo(f"Trace file: {trace_file}")
    typer.echo(
        "Events: "
        f"{summary.total_events} "
        f"(metadata {summary.metadata_events}, jsonrpc {summary.jsonrpc_events})"
    )
    typer.echo(f"Operations recorded: {summary.operations_total}")
    if summary.first_event_at is not None:
        typer.echo(f"First event: {summary.first_event_at.isoformat()}")
    if summary.last_event_at is not None:
        typer.echo(f"Last event: {summary.last_event_at.isoformat()}")

    if summary.environment is not None:
        _echo_mapping("Environment metadata:", summary.environment)
    if summary.settings is not None:
        _echo_mapping("Settings:", summary.settings)

    if not summary.operations:
        typer.echo("Operation outcomes: none recorded")
        return

    typer.echo("Operation outcomes:")
    for stats in summary.operations:
        typer.echo(
            f"  {stats.operation}: {stats.total} total, {stats.ok} ok, {stats.failed} failed"
        )
        if stats.exit_codes:
            parts = ", ".join(f"{entry.code}\u00d7{entry.count}" for entry in stats.exit_codes)
            typer.echo(f"    Exit codes: {parts}")


@trace_app.command("list")
def trace_list(
    trace_file: Path = typer.Argument(..., help="Trace JSONL file produced by --trace-file."),
    operation: str = typer.Option(
        "",
        "--operation",
        help="Filter to a specific operation name.",
    ),
    selector: str = typer.Option(
        "",
        "--selector",
        help="Filter to recorded selectors containing the provided text.",
    ),
    exit_code: str = typer.Option(
        "",
        "--exit-code",
        help="Filter to a specific exit code.",
    ),
    status: str = typer.Option(
        "",
        "--status",
        help="Filter to operation status (either 'ok' or 'failed').",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON describing recorded operations.",
    ),
) -> None:
    """List recorded operations captured in a trace log."""

    try:
        operations = load_trace_operations(trace_file)
    except ValueError as error:  # pragma: no cover - surfaced via Typer
        raise typer.BadParameter(str(error)) from error

    if not operations:
        typer.echo(f"Trace file: {trace_file}")
        typer.echo("Recorded operations: none")
        return

    exit_code_value: int | None
    if exit_code:
        try:
            exit_code_value = int(exit_code)
        except ValueError as error:  # pragma: no cover - surfaced via Typer
            raise typer.BadParameter("Exit code must be an integer.") from error
    else:
        exit_code_value = None

    normalised_operation = operation.lower() or None
    selector_filter = selector.lower() or None
    status_filter: bool | None
    if status:
        normalised_status = status.lower()
        if normalised_status not in {"ok", "failed"}:
            raise typer.BadParameter("Status must be 'ok' or 'failed'.")
        status_filter = normalised_status == "ok"
    else:
        status_filter = None
    counters: dict[str, int] = {}
    matches: list[tuple[int, int, TraceOperationRecord]] = []
    for index, record in enumerate(operations):
        if normalised_operation is not None and record.operation.lower() != normalised_operation:
            continue
        if selector_filter is not None:
            selector_value = (record.selector or "").lower()
            if selector_filter not in selector_value:
                continue
        if exit_code_value is not None and record.exit_code != exit_code_value:
            continue
        if status_filter is not None and record.ok != status_filter:
            continue
        key = record.operation.lower()
        match_index = counters.get(key, 0)
        counters[key] = match_index + 1
        matches.append((index, match_index, record))

    if json_output:
        payload: list[dict[str, Any]] = []
        for index, match_index, record in matches:
            entry: dict[str, Any] = record.model_dump(mode="json", by_alias=True)
            entry["index"] = index
            entry["operationIndex"] = match_index
            payload.append(entry)
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    typer.echo(f"Trace file: {trace_file}")
    if not matches:
        typer.echo("Recorded operations: none match the requested filters")
        return

    typer.echo("Recorded operations:")
    for index, match_index, record in matches:
        status = "ok" if record.ok else "failed"
        typer.echo(
            f"  [{index}] {record.operation} #{match_index} - {status} (exit {record.exit_code})"
        )
        typer.echo(f"    Timestamp: {record.timestamp.isoformat()}")
        if record.selector:
            typer.echo(f"    Selector: {record.selector}")
        if record.message:
            typer.echo(f"    Message: {record.message}")


def _select_trace_operation(
    *,
    operations: Sequence[TraceOperationRecord],
    operation: str | None,
    index: int,
    selector: str | None,
    status: Literal["ok", "failed"] | None,
) -> TraceOperationRecord:
    """Return the trace operation matching ``operation`` and ``index``."""

    if not operations:
        msg = "Trace file does not contain any recorded operations."
        raise typer.BadParameter(msg)

    if operation is not None:
        normalised = operation.lower()
        filtered = [record for record in operations if record.operation.lower() == normalised]
        if not filtered:
            available = sorted({record.operation for record in operations})
            if available:
                joined = ", ".join(available)
                msg = (
                    f"No operations named '{operation}' were recorded. "
                    f"Available operations: {joined}."
                )
            else:
                msg = f"No operations named '{operation}' were recorded."
            raise typer.BadParameter(msg)
        matches = filtered
    else:
        matches = list(operations)

    if selector is not None:
        selector_filter = selector.lower()
        selector_matches = [
            record for record in matches if selector_filter in (record.selector or "").lower()
        ]
        if not selector_matches:
            qualifier = f" named '{operation}'" if operation is not None else ""
            msg = f"No operations{qualifier} matching selector '{selector}' were recorded."
            raise typer.BadParameter(msg)
        matches = selector_matches

    if status is not None:
        expect_ok = status == "ok"
        status_matches = [record for record in matches if record.ok == expect_ok]
        if not status_matches:
            qualifier = "ok" if expect_ok else "failed"
            msg = (
                "No operations"
                f"{' named ' + operation if operation is not None else ''}"
                f" with status '{qualifier}' were recorded."
            )
            raise typer.BadParameter(msg)
        matches = status_matches

    if index < 0 or index >= len(matches):
        msg = (
            f"Index {index} is out of range for {len(matches)} recorded "
            f"operation{'s' if len(matches) != 1 else ''}."
        )
        raise typer.BadParameter(msg)

    return matches[index]


@trace_app.command("replay")
def trace_replay(
    trace_file: Path = typer.Argument(..., help="Trace JSONL file produced by --trace-file."),
    operation: str = typer.Option(
        "",
        "--operation",
        help="Name of the recorded operation to replay (defaults to the first entry).",
    ),
    index: int = typer.Option(
        0,
        "--index",
        help="Zero-based index selecting among matching operations.",
        min=0,
    ),
    selector: str = typer.Option(
        "",
        "--selector",
        help="Filter to recorded selectors containing the provided text before selecting by index.",
    ),
    status: str = typer.Option(
        "",
        "--status",
        help="Filter to operation status before selecting the index ('ok' or 'failed').",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON identical to the original command output.",
    ),
) -> None:
    """Reconstruct the outcome of a recorded operation from a trace log."""

    try:
        operations = load_trace_operations(trace_file)
    except ValueError as error:  # pragma: no cover - surfaced via Typer
        raise typer.BadParameter(str(error)) from error

    status_filter: Literal["ok", "failed"] | None
    if status:
        normalised_status = status.lower()
        if normalised_status not in {"ok", "failed"}:
            raise typer.BadParameter("Status must be 'ok' or 'failed'.")
        status_filter = cast("Literal['ok', 'failed']", normalised_status)
    else:
        status_filter = None

    record = _select_trace_operation(
        operations=operations,
        operation=operation or None,
        index=index,
        selector=selector or None,
        status=status_filter,
    )

    try:
        exit_code = ExitCode(record.exit_code)
    except ValueError as error:  # pragma: no cover - invalid trace data
        msg = f"Trace operation uses unsupported exit code {record.exit_code}."
        raise typer.BadParameter(msg) from error

    outcome = OperationOutcome(
        ok=record.ok,
        message=record.message,
        payload=record.payload,
        exit_code=exit_code,
        metadata=record.metadata,
    )

    if not json_output:
        typer.echo(
            f"Replaying operation '{record.operation}' recorded at {record.timestamp.isoformat()}"
        )
        if record.selector:
            typer.echo(f"Selector: {record.selector}")
        if record.selector_payload:
            selector_payload = json.dumps(
                record.selector_payload, ensure_ascii=False, sort_keys=True
            )
            typer.echo(f"Selector payload: {selector_payload}")

    _handle_outcome(outcome, json_output=json_output)


@trace_app.command("show")
def trace_show(
    trace_file: Path = typer.Argument(..., help="Trace JSONL file produced by --trace-file."),
    operation: str = typer.Option(
        "",
        "--operation",
        help="Name of the recorded operation to display (defaults to the first entry).",
    ),
    index: int = typer.Option(
        0,
        "--index",
        help="Zero-based index selecting among matching operations.",
        min=0,
    ),
    selector: str = typer.Option(
        "",
        "--selector",
        help="Filter to recorded selectors containing the provided text before selecting by index.",
    ),
    status: str = typer.Option(
        "",
        "--status",
        help="Filter to operation status before selecting the index ('ok' or 'failed').",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON describing the recorded operation.",
    ),
) -> None:
    """Display metadata for a recorded operation captured in a trace log."""

    try:
        operations = load_trace_operations(trace_file)
    except ValueError as error:  # pragma: no cover - surfaced via Typer
        raise typer.BadParameter(str(error)) from error

    status_filter: Literal["ok", "failed"] | None
    if status:
        normalised_status = status.lower()
        if normalised_status not in {"ok", "failed"}:
            raise typer.BadParameter("Status must be 'ok' or 'failed'.")
        status_filter = cast("Literal['ok', 'failed']", normalised_status)
    else:
        status_filter = None

    record = _select_trace_operation(
        operations=operations,
        operation=operation or None,
        index=index,
        selector=selector or None,
        status=status_filter,
    )

    if json_output:
        payload = record.model_dump(mode="json", by_alias=True)
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    typer.echo(f"Trace file: {trace_file}")
    typer.echo(f"Operation: {record.operation}")
    typer.echo(f"Timestamp: {record.timestamp.isoformat()}")
    status = "ok" if record.ok else "failed"
    typer.echo(f"Status: {status} (exit {record.exit_code})")
    if record.message:
        typer.echo(f"Message: {record.message}")
    if record.selector:
        typer.echo(f"Selector: {record.selector}")
    if record.selector_payload:
        selector_payload = json.dumps(record.selector_payload, ensure_ascii=False, sort_keys=True)
        typer.echo(f"Selector payload: {selector_payload}")
    if record.payload:
        payload = json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
        typer.echo(f"Payload: {payload}")
    if record.metadata:
        metadata = json.dumps(record.metadata, ensure_ascii=False, sort_keys=True)
        typer.echo(f"Metadata: {metadata}")


def _load_batch_requests(source: Path) -> list[BatchRequest]:
    """Return parsed batch requests from ``source``."""

    try:
        content = source.read_text(encoding="utf-8")
    except OSError as error:  # pragma: no cover - surfaced via Typer error handling
        raise typer.BadParameter(f"Failed to read batch input: {error}") from error

    requests: list[BatchRequest] = []
    for line_number, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise typer.BadParameter(
                f"Line {line_number}: invalid JSON payload ({error.msg})."
            ) from error

        if not isinstance(payload, Mapping):
            raise typer.BadParameter(f"Line {line_number}: batch entry must be a JSON object.")

        for key in cast("tuple[object, ...]", tuple(payload.keys())):
            if not isinstance(key, str):
                raise typer.BadParameter(f"Line {line_number}: batch entry keys must be strings.")

        payload_mapping = cast("Mapping[str, object]", payload)

        if "id" not in payload_mapping:
            raise typer.BadParameter(f"Line {line_number}: missing required field 'id'.")
        identifier = str(payload_mapping["id"])

        command_value = payload_mapping.get("command")
        if not isinstance(command_value, str):
            raise typer.BadParameter(f"Line {line_number}: 'command' must be a string.")
        command = command_value.lower()
        if command not in _BATCH_COMMANDS:
            raise typer.BadParameter(f"Line {line_number}: unsupported command '{command_value}'.")

        selector_value = payload_mapping.get("selector")
        selector = None
        if selector_value is not None:
            if not isinstance(selector_value, str):
                raise typer.BadParameter(
                    f"Line {line_number}: 'selector' must be a string when provided."
                )
            selector = selector_value

        scope_value = payload_mapping.get("scope")
        scope = None
        if scope_value is not None:
            if not isinstance(scope_value, str):
                raise typer.BadParameter(
                    f"Line {line_number}: 'scope' must be a string when provided."
                )
            scope_lower = scope_value.lower()
            if scope_lower not in _BATCH_SCOPES:
                raise typer.BadParameter(f"Line {line_number}: unsupported scope '{scope_value}'.")
            scope = cast("Literal['document', 'workspace']", scope_lower)

        new_name_value = payload_mapping.get("newName", payload_mapping.get("new_name"))
        new_name = None
        if new_name_value is not None:
            if not isinstance(new_name_value, str):
                raise typer.BadParameter(
                    f"Line {line_number}: 'newName' must be a string when provided."
                )
            new_name = new_name_value

        apply_value = payload_mapping.get("apply", False)
        if isinstance(apply_value, bool):
            apply = apply_value
        else:
            raise typer.BadParameter(
                f"Line {line_number}: 'apply' must be a boolean when provided."
            )

        requests.append(
            BatchRequest(
                id=identifier,
                command=cast("BatchCommand", command),
                selector=selector,
                scope=scope,
                new_name=new_name,
                apply=apply,
            )
        )

    return requests


def _batch_response_envelope(request: BatchRequest, response: BatchResponse) -> Mapping[str, Any]:
    """Return a deterministic envelope for a batch response."""

    payload = response.payload or {}
    metadata = response.metadata or {}
    request_descriptor: dict[str, Any] = {
        "id": request.id,
        "command": request.command,
    }
    if request.selector is not None:
        request_descriptor["selector"] = request.selector
    if request.scope is not None:
        request_descriptor["scope"] = request.scope
    if request.new_name is not None:
        request_descriptor["newName"] = request.new_name
    if request.apply:
        request_descriptor["apply"] = request.apply

    return {
        "request": request_descriptor,
        "payload": payload,
        "metadata": metadata,
        "status": {
            "ok": response.ok,
            "message": response.message,
            "exitCode": int(response.exit_code.value),
        },
    }


def _serialise_batch_response(request: BatchRequest, response: BatchResponse) -> str:
    """Return a deterministic JSON line for a batch response."""

    return json.dumps(_batch_response_envelope(request, response), sort_keys=True)


def _write_batch_output(
    entries: Sequence[tuple[BatchRequest, BatchResponse]], destination: Path | None
) -> None:
    """Serialise ``entries`` to ``destination`` or stdout when ``None``."""

    lines = [_serialise_batch_response(request, response) for request, response in entries]

    if destination is None:
        for line in lines:
            typer.echo(line)
        return

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    except OSError as error:  # pragma: no cover - surfaced via Typer error handling
        raise typer.BadParameter(
            f"Failed to write batch output to {destination}: {error}"
        ) from error


@contextmanager
def _open_batch_stream(
    destination: Path | None,
) -> Iterator[Callable[[str], None]]:
    """Yield a writer that streams JSONL lines to ``destination`` or stdout."""

    if destination is None:
        yield typer.echo
        return

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as handle:

            def _write(line: str) -> None:
                try:
                    handle.write(line + "\n")
                    handle.flush()
                except OSError as error:  # pragma: no cover - surfaced via Typer error handling
                    raise typer.BadParameter(
                        f"Failed to write batch output to {destination}: {error}"
                    ) from error

            yield _write
    except OSError as error:  # pragma: no cover - surfaced via Typer error handling
        raise typer.BadParameter(
            f"Failed to open batch output at {destination}: {error}"
        ) from error


def _write_sarif_log(log: SarifLog, destination: Path) -> None:
    """Persist ``log`` to ``destination`` with deterministic formatting."""

    payload = json.dumps(log.model_dump(by_alias=True), sort_keys=True, indent=2)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload + "\n", encoding="utf-8")
    except OSError as error:  # pragma: no cover - surfaced via Typer error handling
        raise typer.BadParameter(
            f"Failed to write SARIF output to {destination}: {error}"
        ) from error


def _write_schema_file(
    kind: SchemaKind,
    destination: Path,
    *,
    sort_keys: bool,
    force: bool,
) -> Path:
    """Write the schema identified by ``kind`` to ``destination``."""

    schema_payload = schema_for(kind)
    target = destination
    if destination.exists() and destination.is_dir():
        target = destination / f"{kind.value}.schema.json"

    if target.exists() and not force:
        msg = f"Schema output already exists at {target}; pass --force to overwrite."
        typer.echo(msg)
        raise typer.Exit(1)

    payload = json.dumps(schema_payload, sort_keys=sort_keys, indent=2) + "\n"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as error:  # pragma: no cover - surfaced via Typer error handling
        raise typer.BadParameter(f"Failed to write schema output to {target}: {error}") from error

    return target


def _emit_metadata(metadata: Mapping[str, Any] | None) -> None:
    """Print metadata details for human-readable command output."""

    if not metadata:
        return

    cache_info = metadata.get("cache") if isinstance(metadata, Mapping) else None
    if not cache_info:
        return

    hit = cache_info.get("hit")
    status = "hit" if hit else "miss"
    size = cache_info.get("size")
    key = cache_info.get("key")
    details = [f"entries={size}" if size is not None else None]
    if key:
        details.append(f"key={key}")
    detail_str = f" ({', '.join([d for d in details if d])})" if any(details) else ""
    typer.echo(f"Cache: {status}{detail_str}")


def _format_progress_event(event: Mapping[str, Any]) -> str:
    """Return a human-readable summary of a progress notification."""

    kind = str(event.get("kind", "unknown"))
    details: list[str] = []

    token = event.get("token")
    if isinstance(token, str | int | float):
        if isinstance(token, float):
            token_repr = format(float(token), "g")
        else:
            token_repr = str(token)
        details.append(f"token={token_repr}")

    for key in ("title", "message", "status"):
        value = event.get(key)
        if isinstance(value, str) and value:
            details.append(f"{key}={value}")

    percentage = event.get("percentage")
    if isinstance(percentage, int | float):
        details.append(f"percentage={format(float(percentage), 'g')}")

    cancellable = event.get("cancellable")
    if isinstance(cancellable, bool):
        details.append(f"cancellable={'yes' if cancellable else 'no'}")

    success = event.get("success")
    if isinstance(success, bool):
        details.append(f"success={'yes' if success else 'no'}")

    if not details:
        return f"[progress:{kind}]"

    return f"[progress:{kind}] {', '.join(details)}"


def _progress_handler_factory(
    *, stream: bool, json_output: bool
) -> Callable[[Mapping[str, Any]], None] | None:
    """Return a progress handler respecting output preferences."""

    if not stream:
        return None

    def _handle(event: Mapping[str, Any]) -> None:
        payload = dict(event)
        if json_output:
            frame = {"event": "progress", "progress": payload}
            typer.echo(json.dumps(frame, sort_keys=True))
            return
        typer.echo(_format_progress_event(payload))

    return _handle


def _handle_outcome(outcome: OperationOutcome, json_output: bool, *, stream: bool = False) -> None:
    if json_output:
        envelope = {
            "payload": outcome.payload or {},
            "metadata": outcome.metadata or {},
            "status": {
                "ok": outcome.ok,
                "message": outcome.message,
                "exitCode": int(outcome.exit_code.value),
            },
        }
        if stream:
            frame = {"event": "result", "result": envelope}
            typer.echo(json.dumps(frame, sort_keys=True))
        else:
            typer.echo(json.dumps(envelope, sort_keys=True, indent=2))
    else:
        typer.echo(outcome.message)
        _emit_metadata(outcome.metadata)

    if not outcome.ok:
        raise typer.Exit(outcome.exit_code.value)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    workspace: Path = typer.Option(
        Path.cwd(),
        "--workspace",
        help="Workspace root that the orchestrator should operate on.",
        click_type=click.Path(file_okay=False, dir_okay=True, resolve_path=True),
        is_flag=False,
    ),
    frozen_snapshot: bool = typer.Option(
        False,
        "--frozen-snapshot",
        help="Enable frozen snapshot mode for deterministic reads.",
        is_flag=True,
        flag_value=True,
    ),
    position_encoding: str = typer.Option(
        "utf-16",
        help="Preferred LSP position encoding to negotiate.",
    ),
    allow_dirty: bool = typer.Option(
        False,
        "--allow-dirty",
        help="Bypass dirty workspace guardrails for mutating operations.",
        is_flag=True,
        flag_value=True,
    ),
    workspace_lock: bool = typer.Option(
        True,
        "--workspace-lock/--no-workspace-lock",
        help="Require an exclusive workspace lock before running operations.",
    ),
    allow_path: list[Path] = typer.Option(
        [],
        "--allow-path",
        help="Restrict operations to the provided path (repeatable).",
        dir_okay=True,
        file_okay=True,
    ),
    deny_path: list[Path] = typer.Option(
        [],
        "--deny-path",
        help="Deny operations that touch the provided path (repeatable).",
        dir_okay=True,
        file_okay=True,
    ),
    trace_file: Path | None = typer.Option(
        None,
        "--trace-file",
        help="Write JSONL trace events for LSP traffic to this file.",
        click_type=click.Path(dir_okay=False, file_okay=True, resolve_path=True),
    ),
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the Lanser version and exit.",
        is_flag=True,
        flag_value=True,
        is_eager=True,
    ),
) -> None:
    """Top-level callback storing shared configuration in context."""

    if version:
        typer.echo(__version__)
        raise typer.Exit(0)

    ctx.obj = RuntimeConfig(
        workspace=workspace,
        frozen_snapshot=frozen_snapshot,
        position_encoding=position_encoding,
        allow_dirty=allow_dirty,
        workspace_lock=workspace_lock,
        allow_paths=tuple(allow_path),
        deny_paths=tuple(deny_path),
        trace_file=trace_file,
    )


@app.command()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
) -> None:
    """Print diagnostics about the current environment."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    snapshot = gather_environment(workspace=config.workspace)
    with _build_orchestrator(config) as orchestrator:
        outcome = orchestrator.doctor()
    if json_output:
        payload = {
            "environment": snapshot.to_dict(),
            "orchestrator": outcome.payload or {},
            "status": {
                "exitCode": int(outcome.exit_code.value),
                "message": outcome.message,
                "ok": outcome.ok,
            },
        }
        typer.echo(json.dumps(payload, sort_keys=True, indent=2))
        if not outcome.ok:
            raise typer.Exit(outcome.exit_code.value)
        return

    typer.echo("Environment diagnostics:")
    typer.echo(f"  Python: {snapshot.python_version} ({snapshot.python_executable})")
    if snapshot.python_requirement:
        typer.echo(f"  Python requirement: {snapshot.python_requirement}")
    if snapshot.python_compatibility:
        typer.echo("  Python compatibility:")
        for entry in snapshot.python_compatibility:
            if entry.satisfies is True:
                status = "ok"
            elif entry.satisfies is False:
                status = "not supported"
            else:
                status = "unknown"
            details = (
                f" (normalized {entry.normalized_version})" if entry.normalized_version else ""
            )
            if entry.reason:
                details = f"{details} - {entry.reason}" if details else f" - {entry.reason}"
            typer.echo(f"    - {entry.target}: {status}{details}")
    typer.echo(f"  Platform: {snapshot.platform}")
    if snapshot.pyright_version:
        typer.echo(f"  Pyright: {snapshot.pyright_version}")
    else:
        typer.echo("  Pyright: <not found>")
    typer.echo(f"  Expected Pyright: {snapshot.pyright_expected_version}")
    if snapshot.pyright_supported_versions:
        typer.echo("  Supported Pyright versions:")
        for version in snapshot.pyright_supported_versions:
            typer.echo(f"    - {version}")
    if snapshot.project_files:
        typer.echo("  Project files:")
        for file in snapshot.project_files:
            typer.echo(f"    - {file}")
    typer.echo(f"  Workspace snapshot: {snapshot.workspace_snapshot}")
    if snapshot.config_digest:
        typer.echo(f"  Config digest: {snapshot.config_digest}")
    if snapshot.git_root:
        typer.echo("  Git:")
        typer.echo(f"    Root: {snapshot.git_root}")
        head = snapshot.git_head or "<unknown>"
        typer.echo(f"    Head: {head}")
        if snapshot.git_dirty is None:
            typer.echo("    Dirty: <unknown>")
        else:
            dirty = "yes" if snapshot.git_dirty else "no"
            typer.echo(f"    Dirty: {dirty}")
    payload_mapping = outcome.payload if outcome.payload is not None else _EMPTY_MAPPING
    settings_payload: dict[str, Any] = dict(payload_mapping)
    cache_raw = settings_payload.pop("cache", None)
    pyright_raw = settings_payload.pop("pyright", None)
    cache_selector_entries: str | int | None = None
    if isinstance(cache_raw, Mapping):
        cache_mapping = cast("Mapping[str, Any]", cache_raw)
        selector_entries = cache_mapping.get("selectorEntries")
        if isinstance(selector_entries, str | int):
            cache_selector_entries = selector_entries
    pyright_info: Mapping[str, Any] | None = None
    if isinstance(pyright_raw, Mapping):
        pyright_info = cast("Mapping[str, Any]", pyright_raw)
    typer.echo("Orchestrator settings:")
    typer.echo(f"  Workspace: {settings_payload.get('workspace')}")
    typer.echo(
        "  Frozen snapshot: "
        + ("enabled" if settings_payload.get("frozenSnapshot") else "disabled")
    )
    typer.echo(
        "  Allow dirty workspace: " + ("yes" if settings_payload.get("allowDirty") else "no")
    )
    lock_raw = settings_payload.pop("workspaceLock", None)
    if isinstance(lock_raw, Mapping):
        lock_mapping = cast("Mapping[str, Any]", lock_raw)
        status = str(lock_mapping.get("status") or "unknown")
        path_value = lock_mapping.get("path")
        details = f" ({path_value})" if isinstance(path_value, str) else ""
        typer.echo(f"  Workspace lock: {status}{details}")
    else:
        typer.echo("  Workspace lock: disabled")
    configured_encoding = settings_payload.pop("configuredPositionEncoding", None)
    position_encoding = settings_payload.get("positionEncoding")
    typer.echo(f"  Position encoding: {position_encoding}")
    if configured_encoding and configured_encoding != position_encoding:
        typer.echo(f"  Configured encoding: {configured_encoding}")
    typer.echo(
        "  Workspace jail: "
        + ("enabled" if settings_payload.get("workspaceJail", True) else "disabled")
    )
    allow_filters: tuple[str, ...] = ()
    allow_filters_raw = settings_payload.get("allowPaths")
    if isinstance(allow_filters_raw, Sequence) and not isinstance(allow_filters_raw, str | bytes):
        allow_values: list[str] = []
        for entry in cast("Sequence[object]", allow_filters_raw):
            allow_values.append(str(entry))
        allow_filters = tuple(allow_values)
    deny_filters: tuple[str, ...] = ()
    deny_filters_raw = settings_payload.get("denyPaths")
    if isinstance(deny_filters_raw, Sequence) and not isinstance(deny_filters_raw, str | bytes):
        deny_values: list[str] = []
        for entry in cast("Sequence[object]", deny_filters_raw):
            deny_values.append(str(entry))
        deny_filters = tuple(deny_values)
    if allow_filters:
        typer.echo("  Allow paths:")
        for path in allow_filters:
            typer.echo(f"    - {path}")
    if deny_filters:
        typer.echo("  Deny paths:")
        for path in deny_filters:
            typer.echo(f"    - {path}")
    if cache_selector_entries is not None:
        typer.echo(f"  Cache entries: {cache_selector_entries}")
    if pyright_info is not None:
        connected = bool(pyright_info.get("connected"))
        status = "connected" if connected else "unavailable"
        typer.echo(f"  Pyright connection: {status}")
        expected_version = pyright_info.get("expectedVersion")
        if isinstance(expected_version, str) and expected_version:
            typer.echo(f"    Expected version: {expected_version}")
        server_version = pyright_info.get("serverVersion")
        if isinstance(server_version, str) and server_version:
            typer.echo(f"    Server version: {server_version}")
        mismatch_value = pyright_info.get("versionMismatch")
        if mismatch_value is True:
            typer.echo("    Version match: no")
        elif mismatch_value is False:
            typer.echo("    Version match: yes")
        if connected:
            handshake_raw = pyright_info.get("handshake")
            handshake: Mapping[str, Any] | None = None
            if isinstance(handshake_raw, Mapping):
                handshake = cast("Mapping[str, Any]", handshake_raw)
            if handshake is not None:
                server_info_raw = handshake.get("serverInfo")
                server_info: Mapping[str, Any] | None = None
                if isinstance(server_info_raw, Mapping):
                    server_info = cast("Mapping[str, Any]", server_info_raw)
                if server_info is not None:
                    name_value = server_info.get("name")
                    version_value = server_info.get("version")
                    name = str(name_value) if name_value is not None else "<unknown>"
                    version = str(version_value) if version_value is not None else "<unknown>"
                    typer.echo(f"    Server: {name} {version}")
                position_encoding = handshake.get("positionEncoding")
                if isinstance(position_encoding, str):
                    typer.echo(f"    Position encoding: {position_encoding}")
        else:
            error = pyright_info.get("error")
            if isinstance(error, str) and error:
                typer.echo(f"    Error: {error}")
    _handle_outcome(outcome, json_output=False)


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
) -> None:
    """Display the resolved runtime configuration."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    if json_output:
        typer.echo(json.dumps(config.to_dict(), sort_keys=True, indent=2))
    else:
        typer.echo("Runtime configuration:")
        typer.echo(f"  Workspace: {config.workspace}")
        typer.echo("  Frozen snapshot: " + ("enabled" if config.frozen_snapshot else "disabled"))
        typer.echo(f"  Position encoding: {config.position_encoding}")
        typer.echo("  Allow dirty workspace: " + ("yes" if config.allow_dirty else "no"))
        if config.allow_paths:
            typer.echo("  Allow paths:")
            for path in config.allow_paths:
                typer.echo(f"    - {path}")
        if config.deny_paths:
            typer.echo("  Deny paths:")
            for path in config.deny_paths:
                typer.echo(f"    - {path}")
        if config.trace_file is not None:
            typer.echo(f"  Trace file: {config.trace_file}")
        else:
            typer.echo("  Trace file: <disabled>")


@schema_app.command("list")
def schema_list(
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
) -> None:
    """List all JSON schemas published by the CLI."""

    descriptors = schema_descriptors()
    if json_output:
        payload = [
            {"name": descriptor.name.value, "description": descriptor.description}
            for descriptor in descriptors
        ]
        typer.echo(json.dumps(payload, sort_keys=True, indent=2))
        return

    typer.echo("Available schemas:")
    for descriptor in descriptors:
        typer.echo(f"  - {descriptor.name.value}: {descriptor.description}")


@schema_app.command("show")
def schema_show(
    kind: SchemaKind = typer.Argument(..., help="Name of the schema to display."),
    sort_keys: bool = typer.Option(
        True,
        "--sort-keys/--no-sort-keys",
        help="Control whether JSON object keys are sorted in the output.",
    ),
) -> None:
    """Emit the JSON schema for ``kind`` to stdout."""

    schema = schema_for(kind)
    typer.echo(json.dumps(schema, sort_keys=sort_keys, indent=2))


@schema_app.command("export")
def schema_export(
    kind: SchemaKind = typer.Argument(..., help="Name of the schema to export."),
    destination: Path = typer.Argument(
        ..., help="File or directory path where the schema will be written."
    ),
    sort_keys: bool = typer.Option(
        True,
        "--sort-keys/--no-sort-keys",
        help="Control whether JSON object keys are sorted in the output.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the destination when the file already exists.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Write the JSON schema for ``kind`` to ``destination``."""

    target = _write_schema_file(kind, destination, sort_keys=sort_keys, force=force)
    typer.echo(f"Wrote {kind.value} schema to {target}")


@schema_app.command("validate")
def schema_validate(
    kind: SchemaKind = typer.Argument(..., help="Name of the schema to validate."),
    payload_file: Path = typer.Argument(
        ..., help="Path to a JSON payload to validate against the schema."
    ),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
) -> None:
    """Validate ``payload_file`` contents against the requested schema."""

    try:
        payload_text = payload_file.read_text(encoding="utf-8")
    except OSError as error:  # pragma: no cover - surfaced via Typer error handling
        raise typer.BadParameter(f"Failed to read payload from {payload_file}: {error}") from error

    try:
        raw_payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"Failed to parse JSON payload: {error}") from error

    if not isinstance(raw_payload, Mapping):
        kind_name = kind.value
        payload_type = type(raw_payload).__name__
        msg = f"{kind_name} schema expects a JSON object payload; received {payload_type}."
        raise typer.BadParameter(msg)

    payload = cast("Mapping[str, Any]", raw_payload)

    try:
        validate_schema_payload(kind, payload)
    except SchemaValidationError as error:
        errors: list[dict[str, Any]] = [dict(entry) for entry in error.errors]
        if json_output:
            frame = {"kind": kind.value, "ok": False, "errors": errors}
            typer.echo(json.dumps(frame, sort_keys=True, indent=2))
        else:
            typer.echo("Validation failed:")
            for entry in errors:
                location = entry.get("loc", ())
                location_str = "".join(
                    f".{part}" if index > 0 else str(part) for index, part in enumerate(location)
                )
                message = entry.get("msg", "Validation error")
                typer.echo(f"  - {location_str or '<root>'}: {message}")
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps({"kind": kind.value, "ok": True}, sort_keys=True, indent=2))
    else:
        typer.echo(f"{kind.value} payload is valid.")


@schema_app.command("validate-batch")
def schema_validate_batch(
    kind: SchemaKind = typer.Argument(..., help="Name of the schema to validate."),
    payload_path: Path = typer.Argument(
        ..., help="Path to a JSON file or directory of JSON payloads."
    ),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
) -> None:
    """Validate one or more payload files against the requested schema."""

    if not payload_path.exists():
        msg = f"Validation path does not exist: {payload_path}"
        raise typer.BadParameter(msg)

    payload_files: list[Path]
    if payload_path.is_dir():
        payload_files = [
            candidate for candidate in sorted(payload_path.rglob("*.json")) if candidate.is_file()
        ]
        if not payload_files:
            msg = f"No JSON payloads found under {payload_path}"
            raise typer.BadParameter(msg)
    else:
        payload_files = [payload_path]

    summary: SchemaValidationSummary = validate_schema_files(kind, payload_files)

    if json_output:
        payload = summary.model_dump(mode="json")
        typer.echo(json.dumps(payload, sort_keys=True, indent=2))
    else:
        typer.echo(f"Validated {summary.total} payload(s) for {kind.value}.")
        typer.echo(f"  Passed: {summary.passed}")
        typer.echo(f"  Failed: {summary.failed}")
        if summary.failed:
            typer.echo("Failures:")
            for entry in summary.results:
                if entry.ok:
                    continue
                typer.echo(f"  - {entry.path}")
                for error in entry.errors:
                    raw_location = error.get("loc", ())
                    location_parts: list[str]
                    if isinstance(raw_location, tuple) or isinstance(raw_location, list):
                        iterable_location = cast("Iterable[object]", raw_location)
                        location_parts = []
                        for item in iterable_location:
                            location_parts.append(str(item))
                    elif raw_location is None:
                        location_parts = []
                    else:
                        location_parts = [str(raw_location)]
                    location_str = ""
                    for index, part in enumerate(location_parts):
                        prefix = "." if index > 0 else ""
                        location_str += f"{prefix}{part}"
                    message = error.get("msg", "Validation error")
                    typer.echo(f"      {location_str or '<root>'}: {message}")

    if summary.failed:
        raise typer.Exit(1)


@app.command("def")
def definition(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Selector identifying the symbol."),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream language-server progress notifications as they arrive.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Resolve a definition for ``selector`` using Pyright when available."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    progress_handler = _progress_handler_factory(stream=stream, json_output=json_output)
    with _build_orchestrator(config, progress_handler=progress_handler) as orchestrator:
        outcome = orchestrator.definition(selector)
    _handle_outcome(outcome, json_output, stream=stream)


@app.command("references")
def references(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Selector identifying the symbol."),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream language-server progress notifications as they arrive.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Resolve references for ``selector`` using Pyright when available."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    progress_handler = _progress_handler_factory(stream=stream, json_output=json_output)
    with _build_orchestrator(config, progress_handler=progress_handler) as orchestrator:
        outcome = orchestrator.references(selector)
    _handle_outcome(outcome, json_output, stream=stream)


@app.command("hover")
def hover(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Selector identifying the symbol."),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream language-server progress notifications as they arrive.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Resolve hover information for ``selector`` using Pyright when available."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    progress_handler = _progress_handler_factory(stream=stream, json_output=json_output)
    with _build_orchestrator(config, progress_handler=progress_handler) as orchestrator:
        outcome = orchestrator.hover(selector)
    _handle_outcome(outcome, json_output, stream=stream)


@app.command("symbols")
def symbols(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Selector identifying the document scope."),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream language-server progress notifications as they arrive.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Resolve symbols for ``selector`` using the configured language server."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    progress_handler = _progress_handler_factory(stream=stream, json_output=json_output)
    with _build_orchestrator(config, progress_handler=progress_handler) as orchestrator:
        outcome = orchestrator.symbols(selector)
    _handle_outcome(outcome, json_output, stream=stream)


@app.command("diagnostics")
def diagnostics(
    ctx: typer.Context,
    selector: str = typer.Argument(
        None,
        metavar="SELECTOR",
        show_default=False,
        help="Selector identifying the document to analyse (omit with --workspace).",
    ),
    workspace: bool = typer.Option(
        False,
        "--workspace",
        help="Run a workspace-wide diagnostics scan instead of a document query.",
        is_flag=True,
        flag_value=True,
    ),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream language-server progress notifications as they arrive.",
        is_flag=True,
        flag_value=True,
    ),
    sarif: Path | None = typer.Option(
        None,
        "--sarif",
        help="Write diagnostics results to the specified SARIF file.",
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
    ),
) -> None:
    """Resolve diagnostics for a document or the workspace via the language server."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG

    selector_value = cast("str | None", selector)

    progress_handler = _progress_handler_factory(stream=stream, json_output=json_output)
    outcome: OperationOutcome

    with _build_orchestrator(config, progress_handler=progress_handler) as orchestrator:
        if workspace:
            if selector is not None:
                raise typer.BadParameter(
                    "Selector argument is not allowed when running workspace diagnostics."
                )
            outcome = cast(
                "OperationOutcome",
                orchestrator.diagnostics(scope="workspace", selector=None),
            )
        else:
            if selector_value is None:
                raise typer.BadParameter(
                    "Document diagnostics require a selector unless --workspace is set."
                )
            outcome = cast(
                "OperationOutcome",
                orchestrator.diagnostics(scope="document", selector=selector_value),
            )

    outcome_ok = cast("bool", outcome.ok)

    if sarif is not None and outcome_ok:
        payload_obj = cast("Mapping[str, Any] | None", outcome.payload)
        if not isinstance(payload_obj, Mapping):
            raise typer.BadParameter(
                "Diagnostics response missing bundle payload; cannot emit SARIF."
            )
        payload_mapping = cast("Mapping[str, Any]", payload_obj)
        metadata_obj = cast("Mapping[str, Any] | None", outcome.metadata)
        metadata_mapping = (
            cast("Mapping[str, Any]", metadata_obj)
            if isinstance(metadata_obj, Mapping)
            else _EMPTY_MAPPING
        )
        sarif_log = diagnostics_to_sarif(payload_mapping, metadata=metadata_mapping)
        destination = sarif if isinstance(sarif, Path) else Path(sarif)
        _write_sarif_log(sarif_log, destination)

    _handle_outcome(outcome, json_output, stream=stream)


app.command("diag")(diagnostics)


@app.command("rename")
def rename(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Selector identifying the symbol."),
    new_name: str = typer.Argument(..., help="New name to preview or apply."),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply the rename instead of providing a preview bundle.",
        is_flag=True,
        flag_value=True,
    ),
    json_output: bool = typer.Option(False, "--json", is_flag=True, flag_value=True),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream language-server progress notifications as they arrive.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Preview or apply a rename operation via the language server."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    progress_handler = _progress_handler_factory(stream=stream, json_output=json_output)
    with _build_orchestrator(config, progress_handler=progress_handler) as orchestrator:
        outcome = orchestrator.rename(selector=selector, new_name=new_name, apply=apply)
    _handle_outcome(outcome, json_output, stream=stream)


@app.command("batch")
def batch(
    ctx: typer.Context,
    in_path: Path = typer.Option(
        ...,
        "--in",
        help="Path to a JSONL file describing batch requests.",
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        exists=True,
    ),
    out_path: Path | None = typer.Option(
        None,
        "--out",
        help="Write JSONL batch responses to this path (defaults to stdout).",
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
    ),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help="Continue executing remaining requests when a command fails.",
        is_flag=True,
        flag_value=True,
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream JSONL responses as each batch request completes.",
        is_flag=True,
        flag_value=True,
    ),
) -> None:
    """Execute batch requests described in ``--in`` JSONL file."""

    config: RuntimeConfig = ctx.obj or DEFAULT_CONFIG
    source_path = Path(in_path)
    requests = _load_batch_requests(source_path)

    exit_code = ExitCode.OK
    destination = Path(out_path) if out_path is not None else None
    processed: list[tuple[BatchRequest, BatchResponse]] | None = None

    with _build_orchestrator(config) as orchestrator:
        if stream:
            with _open_batch_stream(destination) as write_line:
                for request in requests:
                    response = orchestrator.batch([request])[0]
                    line = _serialise_batch_response(request, response)
                    write_line(line)
                    if not response.ok and exit_code == ExitCode.OK:
                        exit_code = response.exit_code
                    if not response.ok and not continue_on_error:
                        break
        else:
            processed = []
            for request in requests:
                response = orchestrator.batch([request])[0]
                processed.append((request, response))
                if not response.ok and exit_code == ExitCode.OK:
                    exit_code = response.exit_code
                if not response.ok and not continue_on_error:
                    break

    if not stream and processed is not None:
        _write_batch_output(processed, destination)

    if exit_code != ExitCode.OK:
        raise typer.Exit(int(exit_code.value))


def main() -> None:
    """Entry point for the console script."""

    app()


if __name__ == "__main__":
    main()
