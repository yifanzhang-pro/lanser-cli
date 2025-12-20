"""Core orchestration primitives for interacting with language servers."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import subprocess
import tomllib
import types
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .analysis import AnalysisBundle
from .environment import EnvironmentSnapshot, gather_environment
from .exit_codes import ExitCode
from .pyright import (
    PyrightSession,
    PyrightSessionError,
    PyrightSessionTimeout,
    create_pyright_session,
)
from .pyright_version import PYRIGHT_VERSION, PYRIGHT_VERSION_SUPPORT
from .python_symbols import (
    PythonModuleAnalysis,
    SymbolData,
    analyse_python_module,
)
from .selectors import (
    AnchorSelector,
    AstPathSelector,
    CursorSelector,
    PositionSpec,
    RangeSelector,
    SelectorParseError,
    SymbolSelector,
    parse_selector,
)
from .workspace_lock import WorkspaceLock, WorkspaceLockError, WorkspaceLockOwner

if TYPE_CHECKING:
    from .trace import TraceRecorderProtocol
else:

    class TraceRecorderProtocol(Protocol):  # pragma: no cover - runtime stub
        def record_metadata(self, kind: str, data: Mapping[str, Any]) -> None: ...

        def record_jsonrpc(self, direction: str, message: Mapping[str, Any]) -> None: ...

        def close(self) -> None: ...


BatchCommand = Literal[
    "definition",
    "references",
    "hover",
    "symbols",
    "diagnostics",
    "rename",
]


__all__ = [
    "BatchRequest",
    "BatchResponse",
    "LSPOrchestrator",
    "OrchestratorSettings",
    "OperationOutcome",
]


type JsonMapping = Mapping[str, Any]

_EMPTY_JSON_MAPPING: JsonMapping = MappingProxyType({})


def _ensure_json_mapping(value: object) -> JsonMapping | None:
    """Return ``value`` when it is a mapping with string keys."""

    if not isinstance(value, Mapping):
        return None
    for key in cast("tuple[object, ...]", tuple(value.keys())):
        if not isinstance(key, str):
            return None
    return cast("JsonMapping", value)


def _ensure_json_sequence(value: object) -> Sequence[object] | None:
    """Return ``value`` when it is a non-string sequence."""

    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray | str):
        return None
    return cast("Sequence[object]", value)


_MODULE_SEARCH_SKIP: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".svn",
        ".tox",
        ".venv",
        "node_modules",
    }
)
_MODULE_SEARCH_MAX_DEPTH = 3
_MODULE_SEARCH_MAX_NODES = 256


class PyrightFactoryProtocol(Protocol):
    """Callable protocol for creating Pyright sessions."""

    def __call__(
        self, workspace: Path, *, recorder: TraceRecorderProtocol | None = None
    ) -> PyrightSession: ...


class OperationOutcome(BaseModel):
    """Result container for orchestrator operations."""

    ok: bool
    message: str
    payload: Mapping[str, Any] | None = None
    exit_code: ExitCode = ExitCode.OK
    metadata: Mapping[str, Any] | None = None

    model_config = ConfigDict(frozen=True)


class OrchestratorSettings(BaseModel):
    """Configuration required to establish an orchestrator session."""

    workspace: Path
    frozen_snapshot: bool = False
    position_encoding: str = "utf-16"
    allow_dirty: bool = False
    allow_paths: tuple[Path, ...] | None = Field(default=None)
    deny_paths: tuple[Path, ...] | None = Field(default=None)
    workspace_lock_path: Path | None = Field(default=None)

    model_config = ConfigDict(frozen=True)


class BatchRequest(BaseModel):
    """Describe a single request executed as part of a batch."""

    id: str
    command: BatchCommand
    selector: str | None = None
    scope: Literal["document", "workspace"] | None = None
    new_name: str | None = None
    apply: bool = False

    model_config = ConfigDict(frozen=True)


class BatchResponse(BaseModel):
    """Response envelope for batch execution results."""

    id: str
    ok: bool
    message: str
    exit_code: ExitCode
    payload: Mapping[str, Any] | None
    metadata: Mapping[str, Any] | None

    model_config = ConfigDict(frozen=True)


class GitStatusSummary(BaseModel):
    """Summarise Git status output for guardrail diagnostics."""

    lines: tuple[str, ...] = Field(default_factory=tuple)
    total: int = 0
    truncated: bool = False

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> Mapping[str, Any]:
        """Return a JSON-serialisable payload describing the summary."""

        payload: dict[str, Any] = {"lines": list(self.lines), "total": self.total}
        if self.truncated:
            payload["truncated"] = True
        return payload


class _SelectorContext(BaseModel):
    """Capture analysis context for a selector-driven operation."""

    analysis: PythonModuleAnalysis
    symbol: SymbolData | None
    position: tuple[int, int] | None

    model_config = ConfigDict(frozen=True)


class _ModuleAnalysisFailure(BaseModel):
    """Record why stub analysis could not read a module."""

    kind: Literal["unreadable", "invalid-encoding"]
    message: str

    model_config = ConfigDict(frozen=True)


class _DiagnosticReport(BaseModel):
    """Structured diagnostics extracted from a Pyright response."""

    diagnostics: list[dict[str, Any]] | None
    explicit: bool = False
    entries: bool = False

    model_config = ConfigDict(frozen=True)


class _ResultBuilder(Protocol):
    """Callable that materialises result payloads for selector operations."""

    def __call__(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return a deterministic payload for ``selector``."""

        ...


class _RenameFileEdit(BaseModel):
    """Describe the source transformation for a single file rename edit."""

    path: Path
    original_source: str
    updated_source: str

    model_config = ConfigDict(frozen=True)


class _RenamePlan(BaseModel):
    """Describe the changes required to apply a rename locally."""

    edits: tuple[_RenameFileEdit, ...]

    model_config = ConfigDict(frozen=True)


class _AppliedRenameEdit(BaseModel):
    """Record an applied rename edit so it can be rolled back if needed."""

    path: Path
    original_source: str

    model_config = ConfigDict(frozen=True)


class _RenamePrepareDetails(BaseModel):
    """Normalised summary of a ``textDocument/prepareRename`` response."""

    allowed: bool
    range: dict[str, list[int]] | None = None
    placeholder: str | None = None
    default_behavior: bool = False
    message: str | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> dict[str, Any]:
        """Return a serialisable representation for bundle payloads."""

        payload: dict[str, Any] = {
            "status": "allowed" if self.allowed else "denied",
        }
        if self.default_behavior:
            payload["defaultBehavior"] = True
        if self.range is not None:
            payload["range"] = {
                "start": list(self.range["start"]),
                "end": list(self.range["end"]),
            }
        if self.placeholder is not None:
            payload["placeholder"] = self.placeholder
        if self.message is not None:
            payload["message"] = self.message
        return payload


class _ResolvedTextEdit(BaseModel):
    """Materialised representation of a text edit against a file."""

    uri: str
    path: Path
    range: dict[str, list[int]]
    new_text: str
    original_text: str
    start_index: int
    end_index: int

    model_config = ConfigDict(frozen=True)


class _RenameApplyError(RuntimeError):
    """Raised when applying a rename workspace edit fails."""

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class _PyrightExecutionEnvironment(BaseModel):
    """Subset of execution-environment settings from ``pyrightconfig.json``."""

    root: Path | None = None
    extra_paths: tuple[Path, ...] = Field(default_factory=tuple, alias="extraPaths")

    model_config = ConfigDict(frozen=True, populate_by_name=True)


class _PyrightConfigPaths(BaseModel):
    """Normalised path fields extracted from ``pyrightconfig.json``."""

    include: tuple[Path, ...] = Field(default_factory=tuple)
    extra_paths: tuple[Path, ...] = Field(default_factory=tuple, alias="extraPaths")
    execution_environments: tuple[_PyrightExecutionEnvironment, ...] = Field(
        default_factory=tuple, alias="executionEnvironments"
    )

    model_config = ConfigDict(frozen=True, populate_by_name=True)


class LSPOrchestrator:
    """Thin wrapper that will coordinate LSP interactions."""

    def __init__(
        self,
        settings: OrchestratorSettings,
        *,
        pyright_factory: PyrightFactoryProtocol | None = None,
        progress_handler: Callable[[Mapping[str, Any]], None] | None = None,
        trace_recorder: TraceRecorderProtocol | None = None,
    ) -> None:
        self._settings = settings
        self._environment_snapshot = gather_environment(self._settings.workspace)
        self._selector_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._selector_cache_fingerprints: dict[tuple[str, str], str] = {}
        self._module_analysis_cache: dict[Path, PythonModuleAnalysis] = {}
        self._module_analysis_failures: dict[Path, _ModuleAnalysisFailure] = {}
        self._workspace_root = self._resolve_path(self._settings.workspace)
        self._allow_paths = tuple(
            self._resolve_path(path) for path in (self._settings.allow_paths or ())
        )
        self._deny_paths = tuple(
            self._resolve_path(path) for path in (self._settings.deny_paths or ())
        )
        self._trace_recorder = trace_recorder
        self._pyright_factory = pyright_factory or create_pyright_session
        self._pyright_session: PyrightSession | None = None
        self._pyright_error: str | None = None
        self._pyright_result_source: Literal["pyright", "stub"] | None = None
        self._pyright_progress_events: list[dict[str, Any]] = []
        self._progress_handler = progress_handler
        self._open_documents: set[Path] = set()
        self._document_versions: dict[Path, int] = {}
        self._workspace_diagnostic_cache: dict[str, list[dict[str, Any]]] = {}
        self._workspace_diagnostic_result_ids: dict[str, str] = {}
        self._module_roots_cache: tuple[Path, ...] | None = None
        self._module_roots_stamp: tuple[float | None, float | None] | None = None
        self._module_roots_configured: bool = False
        self._module_path_cache: dict[str, Path | None] = {}
        self._module_path_cache_stamp: tuple[float | None, float | None] | None = None
        self._workspace_lock_path: Path | None = None
        self._workspace_lock: WorkspaceLock | None = None
        self._workspace_lock_owner: WorkspaceLockOwner | None = None
        self._workspace_lock_error: WorkspaceLockError | None = None

        if self._trace_recorder is not None:
            self._trace_recorder.record_metadata(
                "environment", self._environment_snapshot.to_dict()
            )
            self._trace_recorder.record_metadata("settings", self._trace_settings_payload())

        lock_path_setting = self._settings.workspace_lock_path
        if lock_path_setting is not None:
            self._workspace_lock_path = self._resolve_path(lock_path_setting)
            self._acquire_workspace_lock()

    _SEMVER_PATTERN = re.compile(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
    )

    @property
    def settings(self) -> OrchestratorSettings:
        """Return the immutable settings for this orchestrator."""

        return self._settings

    def __enter__(self) -> LSPOrchestrator:
        """Enter the orchestrator context."""

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        """Ensure the Pyright session shuts down when leaving the context."""

        self.close()

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self.close()
        except Exception:
            # Avoid leaking exceptions during interpreter shutdown.
            pass

    def close(self) -> None:
        """Release open documents and shut down the Pyright session."""

        session = self._pyright_session
        if session is None:
            return

        documents = tuple(self._open_documents)
        for path in documents:
            uri = self._resolve_path(path).as_uri()
            try:
                session.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            except PyrightSessionError as error:
                if self._pyright_error is None:
                    self._pyright_error = str(error)
            finally:
                self._open_documents.discard(path)
                self._document_versions.pop(path, None)

        try:
            session.shutdown()
        except PyrightSessionError as error:
            if self._pyright_error is None:
                self._pyright_error = str(error)
        finally:
            self._pyright_session = None

        self._pyright_result_source = None
        self._open_documents.clear()
        self._document_versions.clear()
        if self._trace_recorder is not None:
            self._trace_recorder.close()

        lock = self._workspace_lock
        if lock is not None:
            try:
                lock.release()
            finally:
                self._workspace_lock = None
        self._workspace_lock_owner = None

    def _trace_settings_payload(self) -> Mapping[str, Any]:
        """Return the orchestrator settings payload recorded in traces."""

        lock_payload = self._workspace_lock_payload()
        payload: dict[str, Any] = {
            "workspace": str(self._workspace_root),
            "frozenSnapshot": self._settings.frozen_snapshot,
            "allowDirty": self._settings.allow_dirty,
            "positionEncoding": self._settings.position_encoding,
            "allowPaths": [str(path) for path in self._allow_paths],
            "denyPaths": [str(path) for path in self._deny_paths],
        }
        if lock_payload is not None:
            payload["workspaceLock"] = lock_payload
        return payload

    def _refresh_environment_snapshot(self) -> EnvironmentSnapshot:
        """Update and return the current environment snapshot."""

        snapshot = gather_environment(self._settings.workspace)
        self._environment_snapshot = snapshot
        return snapshot

    def _workspace_lock_payload(self) -> Mapping[str, Any] | None:
        """Return metadata describing the current workspace lock state."""

        path = self._workspace_lock_path
        if path is None:
            return None

        payload: dict[str, Any] = {"path": str(path)}
        if self._workspace_lock is not None and self._workspace_lock_owner is not None:
            payload["status"] = "acquired"
            payload["owner"] = self._workspace_lock_owner.to_payload()
            return payload

        if self._workspace_lock_error is not None:
            payload["status"] = "blocked"
            owner = self._workspace_lock_error.owner
            if owner is not None:
                payload["owner"] = owner.to_payload()
            return payload

        payload["status"] = "released"
        return payload

    def _acquire_workspace_lock(self) -> bool:
        """Attempt to acquire the workspace lock if configured."""

        path = self._workspace_lock_path
        if path is None:
            return True

        owner = WorkspaceLockOwner.capture()
        try:
            lock = WorkspaceLock(path, owner=owner)
            lock.acquire()
        except WorkspaceLockError as error:
            self._workspace_lock = None
            self._workspace_lock_owner = None
            self._workspace_lock_error = error
            return False

        self._workspace_lock = lock
        self._workspace_lock_owner = owner
        self._workspace_lock_error = None
        return True

    def _ensure_workspace_lock(self) -> bool:
        """Ensure an exclusive workspace lock is held when configured."""

        if self._workspace_lock_path is None:
            return True
        if self._workspace_lock is not None:
            return True
        return self._acquire_workspace_lock()

    def _trace_outcome(
        self,
        *,
        operation: str,
        outcome: OperationOutcome,
        selector: str | None = None,
        spec: PositionSpec | None = None,
    ) -> OperationOutcome:
        """Record ``outcome`` in the trace recorder when configured."""

        if self._trace_recorder is None:
            return outcome

        payload: dict[str, Any] = {
            "operation": operation,
            "ok": outcome.ok,
            "message": outcome.message,
            "exitCode": int(outcome.exit_code.value),
        }
        if selector is not None:
            payload["selector"] = selector
        if spec is not None:
            payload["selectorPayload"] = spec.to_payload()
        if outcome.payload is not None:
            payload["payload"] = json.loads(
                json.dumps(outcome.payload, ensure_ascii=False, sort_keys=True)
            )
        if outcome.metadata is not None:
            payload["metadata"] = json.loads(
                json.dumps(outcome.metadata, ensure_ascii=False, sort_keys=True)
            )

        self._trace_recorder.record_metadata("operation", payload)
        return outcome

    def _ensure_pyright_session(self) -> None:
        """Initialise a Pyright session if a factory is configured."""

        if self._pyright_session is not None or self._pyright_error is not None:
            return

        if self._pyright_factory is None:
            self._pyright_error = "Pyright factory disabled"
            return

        try:
            session = self._pyright_factory(self._workspace_root, recorder=self._trace_recorder)
        except PyrightSessionError as exc:
            self._pyright_error = str(exc)
            return

        self._pyright_session = session

    def _pyright_metadata(self) -> dict[str, Any] | None:
        """Return metadata describing the Pyright session state."""

        expected_version = PYRIGHT_VERSION.version
        supported_versions = PYRIGHT_VERSION_SUPPORT.supported_versions
        supported_list = list(supported_versions)

        if self._pyright_session is not None:
            handshake = self._pyright_session.handshake
            payload: dict[str, Any] = {
                "connected": True,
                "expectedVersion": expected_version,
                "supportedVersions": supported_list,
            }
            resolved_version: str | None = None
            if handshake is not None:
                handshake_metadata: Mapping[str, Any] = handshake.to_metadata()
                payload["handshake"] = handshake_metadata
                server_info = handshake_metadata.get("serverInfo")
                if isinstance(server_info, Mapping):
                    server_info_mapping = cast("Mapping[str, Any]", server_info)
                    version_value = server_info_mapping.get("version")
                    resolved_version = self._extract_pyright_version(version_value)
            if resolved_version is None:
                resolved_version = self._extract_pyright_version(
                    self._environment_snapshot.pyright_version
                )
            if resolved_version is not None:
                payload["serverVersion"] = resolved_version
                payload["versionMismatch"] = resolved_version not in supported_versions
            else:
                payload["versionMismatch"] = None
            if self._pyright_error is not None:
                payload["error"] = self._pyright_error
            if self._pyright_progress_events:
                payload["progress"] = copy.deepcopy(self._pyright_progress_events)
            return payload

        if self._pyright_error is not None:
            payload: dict[str, Any] = {
                "connected": False,
                "error": self._pyright_error,
                "expectedVersion": expected_version,
                "supportedVersions": supported_list,
            }
            resolved_version = self._extract_pyright_version(
                self._environment_snapshot.pyright_version
            )
            if resolved_version is not None:
                payload["serverVersion"] = resolved_version
                payload["versionMismatch"] = resolved_version not in supported_versions
            else:
                payload["versionMismatch"] = None
            return payload

        return None

    @classmethod
    def _extract_pyright_version(cls, value: object) -> str | None:
        """Return a normalised Pyright version string from ``value`` when possible."""

        if not isinstance(value, str):
            return None

        stripped = value.strip()
        if not stripped:
            return None

        match = cls._SEMVER_PATTERN.search(stripped)
        if match is not None:
            return match.group(0)

        return stripped

    def _active_position_encoding(self) -> str:
        """Return the negotiated position encoding when available."""

        session = self._pyright_session
        if session is not None and session.handshake is not None:
            negotiated = session.handshake.position_encoding
            if negotiated:
                return negotiated
        return self._settings.position_encoding

    def _record_pyright_source(self, source: Literal["pyright", "stub"]) -> None:
        """Remember whether the current result used Pyright or a stub fallback."""

        self._pyright_result_source = source

    def _pyright_failure_outcome(
        self,
        *,
        kind: str,
        selector_text: str,
        spec: PositionSpec | None = None,
    ) -> OperationOutcome:
        """Return an error outcome describing a Pyright failure for ``kind``."""

        error_text = self._pyright_error.strip() if self._pyright_error else None
        if error_text:
            normalized = error_text.rstrip(". ")
            message = f"{kind.title()} operation failed due to Pyright error: {normalized}."
            error_kind = "pyright-error"
        else:
            message = f"{kind.title()} operation failed because Pyright is unavailable."
            error_kind = "pyright-unavailable"

        error_payload: dict[str, Any] = {"kind": error_kind, "message": message}
        if error_text:
            error_payload["pyrightError"] = error_text

        payload: dict[str, Any] = {"error": error_payload, "selector": selector_text}
        if spec is not None:
            payload["selectorPayload"] = spec.to_payload()

        metadata = self._pyright_metadata()

        return OperationOutcome(
            ok=False,
            message=message,
            payload=payload,
            exit_code=ExitCode.LS_CRASH,
            metadata={"pyright": metadata} if metadata is not None else None,
        )

    def _stub_fallback_failure(
        self,
        *,
        kind: str,
        selector_text: str,
        spec: PositionSpec | None,
    ) -> OperationOutcome:
        """Return an error outcome when stub analysis is attempted unexpectedly."""

        message = (
            f"{kind.title()} operation refused stub analysis fallback because "
            "Pyright reported a healthy session."
        )
        error_payload: dict[str, Any] = {
            "kind": "stub-fallback-denied",
            "message": message,
        }
        payload: dict[str, Any] = {"error": error_payload, "selector": selector_text}
        if spec is not None:
            payload["selectorPayload"] = spec.to_payload()

        metadata = self._pyright_metadata()

        return OperationOutcome(
            ok=False,
            message=message,
            payload=payload,
            exit_code=ExitCode.LS_CRASH,
            metadata={"pyright": metadata} if metadata is not None else None,
        )

    def _reset_pyright_progress(self) -> None:
        """Clear previously recorded Pyright progress notifications."""

        self._pyright_progress_events = []

    @staticmethod
    def _normalise_progress_token(token: object) -> str | int | float | None:
        """Return a JSON-serialisable token representation."""

        if isinstance(token, str | int | float):
            return token
        if token is None:
            return None
        try:
            return json.dumps(token, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr(token)

    @classmethod
    def _normalise_progress_notification(cls, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return a deterministic payload describing a progress notification."""

        params = _ensure_json_mapping(message.get("params"))
        if params is None:
            return None

        token_payload = cls._normalise_progress_token(params.get("token"))

        value_mapping = _ensure_json_mapping(params.get("value"))
        if value_mapping is None:
            return None

        kind_value = value_mapping.get("kind")
        if not isinstance(kind_value, str):
            return None

        entry: dict[str, Any] = {"kind": kind_value}
        if token_payload is not None:
            entry["token"] = token_payload

        for key in ("title", "message", "status"):
            text_value = value_mapping.get(key)
            if isinstance(text_value, str) and text_value:
                entry[key] = text_value

        percentage = value_mapping.get("percentage")
        if isinstance(percentage, int | float):
            entry["percentage"] = float(percentage)

        cancellable = value_mapping.get("cancellable")
        if isinstance(cancellable, bool):
            entry["cancellable"] = cancellable

        success = value_mapping.get("success")
        if isinstance(success, bool):
            entry["success"] = success

        return entry

    def _collect_pyright_progress(self) -> None:
        """Drain and record Pyright progress notifications."""

        session = self._pyright_session
        if session is None:
            return

        notifications = session.drain_notifications(method="$/progress")
        for message in notifications:
            message_mapping = _ensure_json_mapping(message)
            if message_mapping is None:
                continue
            entry = self._normalise_progress_notification(message_mapping)
            if entry is None:
                continue
            self._pyright_progress_events.append(entry)
            handler = self._progress_handler
            if handler is not None:
                handler(MappingProxyType(copy.deepcopy(entry)))

    @staticmethod
    def _resolve_path(path: Path) -> Path:
        """Return a best-effort resolved version of ``path``."""

        try:
            return path.resolve()
        except OSError:
            return path

    def _resolve_workspace_path(self, path: Path) -> Path:
        """Return ``path`` resolved relative to the workspace root."""

        if path.is_absolute():
            return self._resolve_path(path)
        return self._resolve_path(self._settings.workspace / path)

    @staticmethod
    def _uri_to_path(uri: str) -> Path | None:
        """Convert a ``file://`` URI to a ``Path`` if possible."""

        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        path_str = parsed.path
        if parsed.netloc:
            path_str = f"//{parsed.netloc}{parsed.path}"
        return Path(unquote(path_str))

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return ``True`` if ``path`` is located within ``root``."""

        resolved_path = LSPOrchestrator._resolve_path(path)
        resolved_root = LSPOrchestrator._resolve_path(root)
        return resolved_path == resolved_root or resolved_root in resolved_path.parents

    def _environment_payload(self) -> Mapping[str, Any]:
        """Return environment metadata for bundle responses."""

        snapshot: EnvironmentSnapshot = self._environment_snapshot
        position_encoding = self._active_position_encoding()
        server_version: str | None = snapshot.pyright_version
        language_server: Mapping[str, Any] | None = None
        expected_label = snapshot.pyright_expected_version
        supported_versions = snapshot.pyright_supported_versions
        expected_version = None
        if supported_versions:
            expected_version = supported_versions[0]
        else:
            expected_version = self._extract_pyright_version(expected_label) or expected_label

        session = self._pyright_session
        if session is not None and session.handshake is not None:
            handshake_metadata: dict[str, Any] = dict(session.handshake.to_metadata())

            server_info_raw = handshake_metadata.get("serverInfo")
            if isinstance(server_info_raw, Mapping):
                server_info = cast("Mapping[str, Any]", server_info_raw)
                handshake_version = server_info.get("version")
                if isinstance(handshake_version, str) and handshake_version:
                    server_version = handshake_version

            language_server = handshake_metadata

        server_version_norm = (
            self._extract_pyright_version(server_version)
            if isinstance(server_version, str)
            else None
        )
        version_mismatch: bool | None = None
        if server_version_norm is not None:
            if supported_versions:
                version_mismatch = server_version_norm not in supported_versions
            elif expected_version is not None:
                version_mismatch = server_version_norm != expected_version

        payload: dict[str, Any] = {
            "schemaVersion": "env-meta.v1",
            "workspace": str(self._workspace_root),
            "positionEncoding": position_encoding,
            "frozenSnapshot": self._settings.frozen_snapshot,
            "workspaceSnapshotId": snapshot.workspace_snapshot,
            "pythonVersion": snapshot.python_version,
            "pythonExecutable": snapshot.python_executable,
            "pythonRequirement": snapshot.python_requirement,
            "pythonCompatibility": [
                entry.model_dump(by_alias=True) for entry in snapshot.python_compatibility
            ],
            "platform": snapshot.platform,
            "cwd": snapshot.cwd,
            "pyrightVersion": snapshot.pyright_version,
            "pyrightExpectedVersion": expected_label,
            "pyrightSupportedVersions": list(supported_versions),
            "projectFiles": list(snapshot.project_files),
            "serverVersion": server_version,
            "serverVersionMismatch": version_mismatch,
            "configDigest": snapshot.config_digest,
            "git": {
                "root": snapshot.git_root,
                "head": snapshot.git_head,
                "dirty": snapshot.git_dirty,
            },
        }

        if language_server is not None:
            if supported_versions:
                language_server.setdefault("supportedVersions", list(supported_versions))
                language_server.setdefault("expectedVersion", supported_versions[0])
            elif expected_version is not None:
                language_server.setdefault("expectedVersion", expected_version)
            if server_version_norm is not None:
                language_server.setdefault("serverVersion", server_version_norm)
            elif isinstance(server_version, str) and server_version:
                language_server.setdefault("serverVersion", server_version)
            if version_mismatch is not None:
                language_server["versionMismatch"] = version_mismatch
            payload["languageServer"] = language_server

        lock_payload = self._workspace_lock_payload()
        if lock_payload is not None:
            payload["workspaceLock"] = lock_payload

        return payload

    def _git_status_summary(self, *, limit: int = 10) -> GitStatusSummary | None:
        """Return a summary of ``git status`` output for the workspace."""

        snapshot = self._environment_snapshot
        root = snapshot.git_root
        workspace_root = Path(root) if isinstance(root, str) else self._settings.workspace

        try:
            process = subprocess.run(
                ["git", "-C", str(workspace_root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        output = process.stdout if process.stdout.strip() else process.stderr
        if not output:
            return GitStatusSummary()

        raw_lines = [line.rstrip() for line in output.splitlines() if line.strip()]
        if not raw_lines:
            return GitStatusSummary()

        limited = tuple(raw_lines[:limit]) if limit >= 0 else tuple(raw_lines)
        truncated = len(limited) < len(raw_lines)
        return GitStatusSummary(lines=limited, total=len(raw_lines), truncated=truncated)

    def _dirty_guardrail(self, *, operation: str) -> OperationOutcome | None:
        """Return an error outcome if the workspace is dirty and guardrail is enforced."""

        snapshot = self._environment_snapshot
        if snapshot.git_dirty is not True:
            snapshot = self._refresh_environment_snapshot()

        git_dirty = snapshot.git_dirty
        if git_dirty is not True:
            return None

        if self._settings.allow_dirty:
            return None

        status_summary = self._git_status_summary(limit=8)
        preview_note = ""
        if status_summary is not None and status_summary.lines:
            preview_limit = min(3, len(status_summary.lines))
            preview_entries = [line.strip() for line in status_summary.lines[:preview_limit]]
            joined = ", ".join(entry for entry in preview_entries if entry)
            remaining = status_summary.total - preview_limit
            if remaining > 0:
                joined = f"{joined} (+{remaining} more)" if joined else f"{remaining} more paths"
            if joined:
                preview_note = f" Dirty paths: {joined}."

        guidance = (
            "Workspace has uncommitted changes; refusing to run "
            f"{operation} without --allow-dirty. Another user or tool may have "
            "modified files, so review the Git state before continuing."
            f"{preview_note}"
        )
        git_payload: dict[str, Any] = {
            "root": snapshot.git_root,
            "head": snapshot.git_head,
        }
        error_payload: dict[str, Any] = {
            "kind": "workspace-dirty",
            "message": guidance,
            "git": git_payload,
            "workspaceSnapshotId": snapshot.workspace_snapshot,
        }
        payload: dict[str, Any] = {"error": error_payload}

        if status_summary is not None and status_summary.lines:
            git_payload["statusSample"] = status_summary.to_payload()

        message = (
            "Workspace has uncommitted changes; stabilise the Git state or rerun "
            "with --allow-dirty if concurrent edits are expected."
            f"{preview_note}"
        )

        return OperationOutcome(
            ok=False,
            message=message,
            payload=payload,
            exit_code=ExitCode.VERSION_SKEW,
        )

    def _workspace_lock_guardrail(self, *, operation: str) -> OperationOutcome | None:
        """Return an error when the workspace lock is held by another session."""

        if self._ensure_workspace_lock():
            return None

        path = self._workspace_lock_path
        lock_payload: dict[str, Any] = {}
        if path is not None:
            lock_payload["path"] = str(path)

        owner_payload: Mapping[str, Any] | None = None
        error = self._workspace_lock_error
        if error is not None and error.owner is not None:
            owner_payload = error.owner.to_payload()

        message = (
            "Workspace is locked by another lanser session; refusing to run "
            f"{operation}. Coordinate with the other user or rerun with --no-workspace-lock."
        )
        error_payload: dict[str, Any] = {
            "kind": "workspace-locked",
            "message": message,
            "lock": lock_payload,
        }
        if owner_payload is not None:
            error_payload["owner"] = owner_payload

        payload: dict[str, Any] = {"error": error_payload}

        return OperationOutcome(
            ok=False,
            message=message,
            payload=payload,
            exit_code=ExitCode.VERSION_SKEW,
        )

    def _selector_repositioning(self, selector: PositionSpec) -> Mapping[str, Any]:
        """Return deterministic repositioning metadata for ``selector``."""

        payload: dict[str, Any] = {
            "schemaVersion": "repositioning.v1",
            "target": selector.to_payload(),
            "fallbacks": [],
        }

        if isinstance(selector, AnchorSelector):
            snippet_hash = selector.hash
            if snippet_hash is None:
                digest = hashlib.sha256(selector.snippet.encode("utf-8")).hexdigest()
                snippet_hash = f"sha256:{digest}"
            payload.update(
                {
                    "strategy": "anchor-snippet",
                    "confidence": 0.95 if selector.hash else 0.9,
                    "anchor": {
                        "uri": selector.uri,
                        "snippetHash": snippet_hash,
                        "context": selector.context,
                    },
                    "notes": "Content anchors relocate via snippet hashing and context windows.",
                }
            )
        elif isinstance(selector, SymbolSelector):
            payload.update(
                {
                    "strategy": "symbol-qualname",
                    "confidence": 0.9,
                    "symbol": {
                        "module": selector.module,
                        "qualname": selector.symbol,
                        "role": selector.role,
                        "overload": selector.overload,
                    },
                    "notes": "Symbol selectors fall back to fully-qualified resolution.",
                }
            )
        elif isinstance(selector, AstPathSelector):
            payload.update(
                {
                    "strategy": "ast-path",
                    "confidence": 0.88,
                    "path": [segment.to_payload() for segment in selector.path],
                    "notes": "AST paths are replayed to navigate structural matches.",
                }
            )
        elif isinstance(selector, RangeSelector):
            window_payload: dict[str, Any] = {
                "start": [selector.start_line, selector.start_column],
                "end": [selector.end_line, selector.end_column],
            }
            if selector.doc_version is not None:
                window_payload["docVersion"] = selector.doc_version

            payload.update(
                {
                    "strategy": "range-window",
                    "confidence": 0.72,
                    "window": window_payload,
                    "notes": "Range selectors retain offsets while drift correction matures.",
                }
            )
            payload["fallbacks"].append(
                {
                    "strategy": "cursor-centre",
                    "selector": {
                        "kind": "cursor",
                        "uri": selector.uri,
                        "line": selector.start_line,
                        "col": selector.start_column,
                    },
                    "confidence": 0.6,
                }
            )
            if selector.doc_version is not None:
                payload["fallbacks"][-1]["selector"]["docVersion"] = selector.doc_version
        elif isinstance(selector, CursorSelector):
            payload.update(
                {
                    "strategy": "cursor-line-col",
                    "confidence": 0.68,
                    "cursor": {
                        "line": selector.line,
                        "col": selector.column,
                    },
                    "notes": "Cursor selectors reposition using line/column anchors.",
                }
            )
            if selector.doc_version is not None:
                payload["cursor"]["docVersion"] = selector.doc_version
        else:
            payload.update(
                {
                    "strategy": "passthrough",
                    "confidence": 0.5,
                    "notes": "Selector type lacks dedicated repositioning heuristics yet.",
                }
            )

        return payload

    def _selector_paths(self, selector: PositionSpec) -> list[Path]:
        """Return filesystem paths touched by ``selector``."""

        paths: list[Path] = []
        if isinstance(selector, CursorSelector | RangeSelector | AnchorSelector):
            path = self._uri_to_path(selector.uri)
            if path is not None:
                paths.append(path)
        if isinstance(selector, SymbolSelector):
            path = self._module_path_from_symbol_selector(selector)
            if path is not None:
                paths.append(path)
        if isinstance(selector, AstPathSelector):
            module_name = self._module_name_from_ast_selector(selector)
            if module_name is not None:
                path = self._module_path_from_module_name(module_name)
                if path is not None:
                    paths.append(path)
        return paths

    def _path_for_selector(self, selector: PositionSpec) -> Path | None:
        """Return a representative filesystem path for ``selector``."""

        paths = self._selector_paths(selector)
        if paths:
            return self._resolve_path(paths[0])
        return None

    def _ensure_document_open(self, path: Path) -> str | None:
        """Open ``path`` in the Pyright session if required and return its URI."""

        session = self._pyright_session
        if session is None:
            return None

        resolved = self._resolve_path(path)
        uri = resolved.as_uri()
        if resolved in self._open_documents:
            return uri

        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            reason = error.reason or "invalid UTF-8 sequence"
            message = f"Failed to decode '{resolved}' as UTF-8: {reason}."
            self._record_module_analysis_failure(resolved, kind="invalid-encoding", message=message)
            return None
        except OSError as error:
            description = error.strerror or str(error)
            message = f"Failed to read '{resolved}': {description}."
            self._record_module_analysis_failure(resolved, kind="unreadable", message=message)
            return None

        self._clear_module_analysis_failure(resolved)

        version = self._document_versions.get(resolved, 0) + 1
        self._document_versions[resolved] = version

        try:
            session.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "python",
                        "text": text,
                        "version": version,
                    }
                },
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            return None

        self._open_documents.add(resolved)
        return uri

    def _synchronise_document(self, path: Path, text: str) -> None:
        """Publish ``text`` for ``path`` to Pyright via didOpen/didChange."""

        session = self._pyright_session
        if session is None:
            return

        resolved = self._resolve_path(path)
        uri = resolved.as_uri()
        version = self._document_versions.get(resolved, 0) + 1
        self._document_versions[resolved] = version

        if resolved not in self._open_documents:
            try:
                session.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "python",
                            "text": text,
                            "version": version,
                        }
                    },
                )
            except PyrightSessionError as error:
                self._pyright_error = str(error)
                return

            self._open_documents.add(resolved)
            return

        try:
            session.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            self._open_documents.discard(resolved)

    @staticmethod
    def _lsp_position(line: int, column: int) -> dict[str, int]:
        """Return a zero-based LSP position for ``line``/``column`` inputs."""

        line_index = line - 1 if line > 0 else 0
        column_index = column - 1 if column > 0 else 0
        return {"line": line_index, "character": column_index}

    def _selector_position_params(self, selector: PositionSpec) -> Mapping[str, Any] | None:
        """Return ``textDocument`` + ``position`` params for ``selector`` if resolvable."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        path = self._path_for_selector(selector)
        if path is None:
            return None

        uri = self._ensure_document_open(path)
        if uri is None:
            return None

        analysis: PythonModuleAnalysis | None = None

        def _analysis() -> PythonModuleAnalysis | None:
            nonlocal analysis
            if analysis is None:
                analysis = self._analysis_for_path(path)
            return analysis

        def _symbol_start(symbol: SymbolData) -> dict[str, int]:
            return self._lsp_position(
                symbol.selection.start_line,
                symbol.selection.start_col + 1,
            )

        if isinstance(selector, CursorSelector):
            position = self._lsp_position(selector.line, selector.column)
            analysis_candidate = _analysis()
            if analysis_candidate is not None:
                symbol = analysis_candidate.find_at(selector.line, selector.column)
                if symbol is not None:
                    position = _symbol_start(symbol)
        elif isinstance(selector, RangeSelector):
            position = self._lsp_position(selector.start_line, selector.start_column)
            analysis_candidate = _analysis()
            if analysis_candidate is not None:
                symbol = analysis_candidate.find_at(selector.start_line, selector.start_column)
                if symbol is not None:
                    position = _symbol_start(symbol)
        elif isinstance(selector, AnchorSelector):
            analysis_candidate = _analysis()
            if analysis_candidate is None:
                return None
            anchor_position = analysis_candidate.find_snippet(selector.snippet)
            if anchor_position is None:
                return None
            position = self._lsp_position(*anchor_position)
        elif isinstance(selector, SymbolSelector):
            analysis_candidate = _analysis()
            if analysis_candidate is None:
                return None
            symbol = analysis_candidate.find_by_qualname(selector.symbol)
            if symbol is None:
                return None
            position = _symbol_start(symbol)
        elif isinstance(selector, AstPathSelector):
            analysis_candidate = _analysis()
            if analysis_candidate is None:
                return None
            symbol = self._symbol_from_ast_selector(analysis_candidate, selector)
            if symbol is None:
                return None
            position = _symbol_start(symbol)
        else:
            return None

        return {"textDocument": {"uri": uri}, "position": position}

    def _context_position_params(
        self, selector: PositionSpec, context: _SelectorContext
    ) -> Mapping[str, Any] | None:
        """Return Pyright params derived from ``context`` when possible."""

        analysis = context.analysis
        path = self._path_for_selector(selector) or analysis.path
        uri = self._ensure_document_open(path)
        if uri is None:
            return None

        if context.symbol is not None:
            position = self._lsp_position(
                context.symbol.selection.start_line,
                context.symbol.selection.start_col + 1,
            )
        elif context.position is not None:
            position = self._lsp_position(*context.position)
        else:
            return None

        return {"textDocument": {"uri": uri}, "position": position}

    @staticmethod
    def _normalise_range(range_payload: JsonMapping) -> dict[str, list[int]] | None:
        """Convert an LSP range payload into 1-based line metadata."""

        start = range_payload.get("start")
        end = range_payload.get("end")
        start_mapping = _ensure_json_mapping(start)
        end_mapping = _ensure_json_mapping(end)
        if start_mapping is None or end_mapping is None:
            return None

        start_line = start_mapping.get("line")
        start_char = start_mapping.get("character")
        end_line = end_mapping.get("line")
        end_char = end_mapping.get("character")

        if not isinstance(start_line, int) or not isinstance(start_char, int):
            return None
        if not isinstance(end_line, int) or not isinstance(end_char, int):
            return None

        return {
            "start": [max(start_line + 1, 1), max(start_char, 0)],
            "end": [max(end_line + 1, 1), max(end_char, 0)],
        }

    @staticmethod
    def _range_contains_point(
        range_dict: Mapping[str, Sequence[int]], point: Sequence[int]
    ) -> bool:
        """Return ``True`` if ``point`` falls within ``range_dict``."""

        if (
            not isinstance(range_dict, Mapping)
            or not isinstance(point, Sequence)
            or len(point) != 2
        ):
            return False

        start = range_dict.get("start")
        end = range_dict.get("end")
        if (
            not isinstance(start, Sequence)
            or not isinstance(end, Sequence)
            or len(start) != 2
            or len(end) != 2
        ):
            return False

        start_line, start_col = start
        end_line, end_col = end
        point_line, point_col = point

        if not all(
            isinstance(value, int)
            for value in (start_line, start_col, end_line, end_col, point_line, point_col)
        ):
            return False

        if point_line < start_line or point_line > end_line:
            return False
        if point_line == start_line and point_col < start_col:
            return False
        if point_line == end_line and point_col > end_col:
            return False
        return True

    def _normalise_definition_locations(self, result: object) -> list[dict[str, Any]]:
        """Return deterministic definition entries from an LSP response."""

        entries: list[dict[str, Any]] = []

        mapping_result = _ensure_json_mapping(result)
        sequence_items: list[JsonMapping] = []

        if mapping_result is not None:
            if "targetUri" in mapping_result or "targetRange" in mapping_result:
                sequence_items.append(mapping_result)
            else:
                range_payload = mapping_result.get("range")
                uri = mapping_result.get("uri")
                range_mapping = _ensure_json_mapping(range_payload)
                if isinstance(uri, str) and range_mapping is not None:
                    range_dict = self._normalise_range(range_mapping)
                    if range_dict is not None:
                        entries.append({"uri": uri, "range": range_dict})
                return entries
        else:
            raw_sequence = _ensure_json_sequence(result)
            if raw_sequence is None:
                return entries
            for item in raw_sequence:
                mapping_item = _ensure_json_mapping(item)
                if mapping_item is not None:
                    sequence_items.append(mapping_item)

        for item in sequence_items:
            if "targetUri" in item:
                uri_value = item.get("targetUri")
                range_payload = item.get("targetRange")
                selection_payload = item.get("targetSelectionRange")
                origin_payload = item.get("originSelectionRange")
                range_mapping = _ensure_json_mapping(range_payload)
                if isinstance(uri_value, str) and range_mapping is not None:
                    range_dict = self._normalise_range(range_mapping)
                    if range_dict is None:
                        continue
                    entry: dict[str, Any] = {
                        "uri": uri_value,
                        "range": range_dict,
                        "type": "locationLink",
                    }
                    selection_mapping = _ensure_json_mapping(selection_payload)
                    if selection_mapping is not None:
                        selection_range = self._normalise_range(selection_mapping)
                        if selection_range is not None:
                            entry["selectionRange"] = selection_range
                    origin_mapping = _ensure_json_mapping(origin_payload)
                    if origin_mapping is not None:
                        origin_range = self._normalise_range(origin_mapping)
                        if origin_range is not None:
                            entry["originSelectionRange"] = origin_range
                    entries.append(entry)
            else:
                uri_value = item.get("uri")
                range_payload = item.get("range")
                range_mapping = _ensure_json_mapping(range_payload)
                if isinstance(uri_value, str) and range_mapping is not None:
                    range_dict = self._normalise_range(range_mapping)
                    if range_dict is None:
                        continue
                    entries.append({"uri": uri_value, "range": range_dict, "type": "location"})

        entries.sort(
            key=lambda entry: (entry.get("uri", ""), entry.get("range", {}).get("start", [0, 0]))
        )
        return entries

    @staticmethod
    def _normalise_hover_contents(contents: object) -> list[dict[str, str]]:
        """Return deterministic hover content entries from ``contents``."""

        items: list[dict[str, str]] = []

        def _append(kind: str, value: str) -> None:
            text = value.strip()
            if not text:
                return
            if kind not in {"markdown", "plaintext"}:
                kind = "plaintext"
            items.append({"kind": kind, "value": text})

        if contents is None:
            return items

        mapping_contents = _ensure_json_mapping(contents)
        if mapping_contents is not None:
            value = mapping_contents.get("value")
            if isinstance(value, str):
                kind = mapping_contents.get("kind")
                if isinstance(kind, str):
                    _append(kind, value)
                else:
                    _append("plaintext", value)
            return items

        if isinstance(contents, str):
            _append("plaintext", contents)
            return items

        sequence_contents = _ensure_json_sequence(contents)
        if sequence_contents is not None:
            for entry in sequence_contents:
                if isinstance(entry, str):
                    _append("plaintext", entry)
                else:
                    entry_mapping = _ensure_json_mapping(entry)
                    if entry_mapping is None:
                        continue
                    language = entry_mapping.get("language")
                    value = entry_mapping.get("value")
                    if isinstance(language, str) and isinstance(value, str):
                        _append("markdown", f"```{language}\n{value}\n```")
                    elif isinstance(value, str):
                        _append("plaintext", value)
            return items

        return items

    def _pyright_definition(
        self, selector: PositionSpec, context: _SelectorContext | None
    ) -> Mapping[str, Any] | None:
        """Return Pyright-backed definition data for ``selector`` when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        params = self._selector_position_params(selector)
        if params is None and context is not None:
            params = self._context_position_params(selector, context)
        if params is None:
            return None

        try:
            result = session.request(
                "textDocument/definition",
                params,
                timeout=10.0,
                cancellable=True,
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            return None
        finally:
            self._collect_pyright_progress()

        self._pyright_error = None

        if result is None:
            return {"definitions": []}

        return {"definitions": self._normalise_definition_locations(result)}

    def _pyright_references(
        self,
        selector: PositionSpec,
        *,
        context: _SelectorContext | None,
    ) -> Mapping[str, Any] | None:
        """Return Pyright-backed reference data for ``selector`` when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        params = self._selector_position_params(selector)
        if params is None:
            return None

        params = dict(params)
        params["context"] = {"includeDeclaration": True}

        try:
            result = session.request(
                "textDocument/references",
                params,
                timeout=10.0,
                cancellable=True,
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            return None
        finally:
            self._collect_pyright_progress()

        self._pyright_error = None

        locations = self._normalise_definition_locations(result)
        if not locations:
            return {"references": []}

        symbol_descriptor: dict[str, Any] | None = None
        selection_range: Mapping[str, Sequence[int]] | None = None
        if context is not None and context.symbol is not None:
            symbol_descriptor = self._symbol_descriptor(context.symbol)
            selection_range = context.symbol.selection.to_dict()

        entries: list[dict[str, Any]] = []
        for entry in locations:
            payload = dict(entry)
            role: str | None = None
            if selection_range is not None:
                candidate = (
                    payload.get("selectionRange")
                    if isinstance(payload.get("selectionRange"), Mapping)
                    else payload.get("range")
                )
                if isinstance(candidate, Mapping):
                    if candidate == selection_range:
                        role = "definition"
                    elif self._range_contains_point(
                        candidate, selection_range.get("start", (1, 0))
                    ):
                        role = "definition"
            if role is None:
                if symbol_descriptor is not None:
                    role = "reference"
                else:
                    role = "match"

            if symbol_descriptor is not None:
                payload["symbol"] = dict(symbol_descriptor)
            payload["role"] = role
            entries.append(payload)

        return {"references": entries}

    def _pyright_hover(
        self,
        selector: PositionSpec,
        *,
        context: _SelectorContext | None,
    ) -> Mapping[str, Any] | None:
        """Return Pyright-backed hover data for ``selector`` when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        params = self._selector_position_params(selector)
        if params is None:
            return None

        try:
            result = session.request(
                "textDocument/hover",
                params,
                timeout=5.0,
                cancellable=True,
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            return None
        finally:
            self._collect_pyright_progress()

        self._pyright_error = None

        if result is None:
            return {"hover": {"contents": []}}

        result_mapping = _ensure_json_mapping(result)
        if result_mapping is None:
            return {"hover": {"contents": []}}

        hover_payload: dict[str, Any] = {
            "contents": self._normalise_hover_contents(result_mapping.get("contents"))
        }

        range_payload = result_mapping.get("range")
        range_mapping = _ensure_json_mapping(range_payload)
        if range_mapping is not None:
            normalised = self._normalise_range(range_mapping)
            if normalised is not None:
                hover_payload["range"] = normalised

        if context is not None and context.symbol is not None:
            hover_payload["symbol"] = self._symbol_descriptor(context.symbol)

        return {"hover": hover_payload}

    @staticmethod
    def _symbol_kind_name(value: object) -> str:
        """Return a readable name for an LSP ``SymbolKind`` value."""

        if isinstance(value, str):
            lowered = value.strip().lower()
            return lowered or "unknown"

        if isinstance(value, int):
            mapping = {
                1: "file",
                2: "module",
                3: "namespace",
                4: "package",
                5: "class",
                6: "method",
                7: "property",
                8: "field",
                9: "constructor",
                10: "enum",
                11: "interface",
                12: "function",
                13: "variable",
                14: "constant",
                15: "string",
                16: "number",
                17: "boolean",
                18: "array",
                19: "object",
                20: "key",
                21: "null",
                22: "enummember",
                23: "struct",
                24: "event",
                25: "operator",
                26: "typeparameter",
            }
            return mapping.get(value, "unknown")

        return "unknown"

    def _match_symbol_from_analysis(
        self,
        *,
        analysis: PythonModuleAnalysis | None,
        selection_range: Mapping[str, Sequence[int]] | None,
    ) -> SymbolData | None:
        """Return the stub analysis symbol matching ``selection_range`` if any."""

        if analysis is None or selection_range is None:
            return None

        start = selection_range.get("start")
        if not isinstance(start, Sequence) or len(start) != 2:
            return None

        line, column = start
        if not isinstance(line, int) or not isinstance(column, int):
            return None

        return analysis.find_at(line, column)

    def _overlay_analysis_symbol(
        self,
        *,
        entry: dict[str, Any],
        analysis: PythonModuleAnalysis,
        symbol: SymbolData,
        lsp_kind: str,
        container_name: str | None,
    ) -> None:
        """Augment ``entry`` with deterministic metadata from stub analysis."""

        analysis_entry = self._symbol_payload(analysis=analysis, symbol=symbol)

        existing_range = entry.get("range")
        existing_selection = entry.get("selectionRange")
        if existing_range is not None:
            entry["lspRange"] = existing_range
        if existing_selection is not None:
            entry["lspSelectionRange"] = existing_selection

        entry["uri"] = analysis_entry["uri"]
        entry["range"] = analysis_entry["range"]
        entry["selectionRange"] = analysis_entry["selectionRange"]

        descriptor = dict(analysis_entry["symbol"])
        descriptor["lspKind"] = lsp_kind
        if container_name is not None:
            descriptor.setdefault("containerName", container_name)
        entry["symbol"] = descriptor

        signature = analysis_entry.get("signature")
        if isinstance(signature, str):
            entry["signature"] = signature

        docstring = analysis_entry.get("docstring")
        if isinstance(docstring, str):
            entry["docstring"] = docstring

    def _document_symbol_entry(
        self,
        payload: JsonMapping,
        *,
        uri: str,
        analysis: PythonModuleAnalysis | None,
        container_name: str | None,
    ) -> dict[str, Any] | None:
        """Return a normalised symbol entry from a Pyright response mapping."""

        if "range" in payload and "selectionRange" in payload:
            return self._document_symbol_from_document_symbol(
                payload,
                uri=uri,
                analysis=analysis,
                container_name=container_name,
            )

        location = payload.get("location")
        location_mapping = _ensure_json_mapping(location)
        if location_mapping is not None:
            return self._document_symbol_from_symbol_information(
                payload,
                location=location_mapping,
                default_uri=uri,
                analysis=analysis,
                container_name=container_name,
            )

        return None

    def _document_symbol_from_document_symbol(
        self,
        payload: JsonMapping,
        *,
        uri: str,
        analysis: PythonModuleAnalysis | None,
        container_name: str | None,
    ) -> dict[str, Any] | None:
        """Return a normalised entry for an LSP ``DocumentSymbol`` payload."""

        name = payload.get("name")
        if not isinstance(name, str):
            return None

        kind = self._symbol_kind_name(payload.get("kind"))
        range_mapping = _ensure_json_mapping(payload.get("range"))
        selection_mapping = _ensure_json_mapping(payload.get("selectionRange"))
        range_dict = self._normalise_range(range_mapping) if range_mapping is not None else None
        selection_dict = (
            self._normalise_range(selection_mapping) if selection_mapping is not None else None
        )

        entry: dict[str, Any] = {
            "uri": uri,
            "symbol": {"name": name, "kind": kind},
        }
        if range_dict is not None:
            entry["range"] = range_dict
        if selection_dict is not None:
            entry["selectionRange"] = selection_dict

        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            entry["detail"] = detail

        if payload.get("deprecated") is True:
            entry["deprecated"] = True

        tags_value = payload.get("tags")
        tags_sequence = _ensure_json_sequence(tags_value)
        if tags_sequence is not None:
            tags = [tag for tag in tags_sequence if isinstance(tag, int)]
            if tags:
                entry["tags"] = tags

        descriptor = entry["symbol"]
        descriptor["lspKind"] = kind
        if container_name is not None:
            descriptor.setdefault("containerName", container_name)

        matched_symbol = self._match_symbol_from_analysis(
            analysis=analysis,
            selection_range=selection_dict or range_dict,
        )
        if matched_symbol is not None and analysis is not None:
            self._overlay_analysis_symbol(
                entry=entry,
                analysis=analysis,
                symbol=matched_symbol,
                lsp_kind=kind,
                container_name=container_name,
            )

        children_value = payload.get("children")
        children_sequence = _ensure_json_sequence(children_value)
        if children_sequence is not None:
            children: list[dict[str, Any]] = []
            for child in children_sequence:
                child_mapping = _ensure_json_mapping(child)
                if child_mapping is None:
                    continue
                child_entry = self._document_symbol_entry(
                    child_mapping,
                    uri=uri,
                    analysis=analysis,
                    container_name=name,
                )
                if child_entry is not None:
                    children.append(child_entry)
            if children:
                entry["children"] = children

        return entry

    def _document_symbol_from_symbol_information(
        self,
        payload: JsonMapping,
        *,
        location: JsonMapping,
        default_uri: str,
        analysis: PythonModuleAnalysis | None,
        container_name: str | None,
    ) -> dict[str, Any] | None:
        """Return a normalised entry for an LSP ``SymbolInformation`` payload."""

        name = payload.get("name")
        if not isinstance(name, str):
            return None

        kind = self._symbol_kind_name(payload.get("kind"))
        uri_value = location.get("uri")
        uri = uri_value if isinstance(uri_value, str) else default_uri
        range_mapping = _ensure_json_mapping(location.get("range"))
        range_dict = self._normalise_range(range_mapping) if range_mapping is not None else None

        entry: dict[str, Any] = {
            "uri": uri,
            "symbol": {"name": name, "kind": kind},
        }
        if range_dict is not None:
            entry["range"] = range_dict
            entry["selectionRange"] = range_dict

        descriptor = entry["symbol"]
        descriptor["lspKind"] = kind

        container = payload.get("containerName")
        if isinstance(container, str) and container:
            descriptor["containerName"] = container
        elif container_name is not None:
            descriptor.setdefault("containerName", container_name)

        analysis_candidate = analysis
        analysis_path = self._uri_to_path(uri)
        if analysis_candidate is None and analysis_path is not None:
            analysis_candidate = self._analysis_for_path(analysis_path)
        elif analysis_candidate is not None and analysis_candidate.uri != uri:
            if analysis_path is not None:
                analysis_candidate = self._analysis_for_path(analysis_path)

        matched_symbol = self._match_symbol_from_analysis(
            analysis=analysis_candidate,
            selection_range=range_dict,
        )
        if matched_symbol is not None and analysis_candidate is not None:
            self._overlay_analysis_symbol(
                entry=entry,
                analysis=analysis_candidate,
                symbol=matched_symbol,
                lsp_kind=kind,
                container_name=descriptor.get("containerName"),
            )

        return entry

    def _normalise_document_symbols(
        self,
        result: object,
        *,
        uri: str,
        analysis: PythonModuleAnalysis | None,
    ) -> list[dict[str, Any]] | None:
        """Return deterministic document symbol entries from an LSP response."""

        if result is None:
            return None

        sequence = _ensure_json_sequence(result)
        if sequence is None:
            mapping = _ensure_json_mapping(result)
            if mapping is None:
                return None
            sequence = (mapping,)

        entries: list[dict[str, Any]] = []
        for item in sequence:
            mapping = _ensure_json_mapping(item)
            if mapping is None:
                continue
            entry = self._document_symbol_entry(
                mapping,
                uri=uri,
                analysis=analysis,
                container_name=None,
            )
            if entry is not None:
                entries.append(entry)

        return entries

    def _pyright_document_symbols(
        self,
        selector: PositionSpec,
        *,
        context: _SelectorContext | None,
    ) -> Mapping[str, Any] | None:
        """Return Pyright-backed document symbols for ``selector`` when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        path = self._path_for_selector(selector)
        if path is None:
            return None

        uri = self._ensure_document_open(path)
        if uri is None:
            return None

        analysis = context.analysis if context is not None else self._analysis_for_path(path)

        try:
            result = session.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                timeout=10.0,
                cancellable=True,
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            return None
        finally:
            self._collect_pyright_progress()

        self._pyright_error = None

        entries = self._normalise_document_symbols(result, uri=uri, analysis=analysis)
        if entries is None:
            return None

        return {"symbols": entries}

    @staticmethod
    def _diagnostic_severity_name(value: object) -> str:
        """Return a human-readable severity string for ``value``."""

        if isinstance(value, int):
            severity_map = {
                1: "error",
                2: "warning",
                3: "information",
                4: "hint",
            }
            return severity_map.get(value, "information")
        if isinstance(value, str):
            lowered = value.lower()
            if lowered in {"error", "warning", "information", "hint"}:
                return lowered
        return "information"

    def _normalise_diagnostic_entries(
        self, entries: Sequence[object], *, uri: str
    ) -> list[dict[str, Any]]:
        """Return deterministic diagnostic payloads from ``entries``."""

        diagnostics: list[dict[str, Any]] = []
        for entry in entries:
            mapping = _ensure_json_mapping(entry)
            if mapping is None:
                continue

            if not self._is_diagnostic_mapping(mapping):
                continue

            range_payload = mapping.get("range")
            range_mapping = _ensure_json_mapping(range_payload)
            if range_mapping is None:
                continue

            range_dict = self._normalise_range(range_mapping)
            if range_dict is None:
                continue

            message = mapping.get("message")
            if not isinstance(message, str):
                continue

            severity = self._diagnostic_severity_name(mapping.get("severity"))
            diagnostic: dict[str, Any] = {
                "uri": uri,
                "range": range_dict,
                "message": message,
                "severity": severity,
            }

            code_value = mapping.get("code")
            if isinstance(code_value, str | int):
                diagnostic["code"] = str(code_value)
            else:
                code_mapping = _ensure_json_mapping(code_value)
                if code_mapping is not None:
                    code_literal = code_mapping.get("value")
                    if isinstance(code_literal, str | int):
                        diagnostic["code"] = str(code_literal)

            source_value = mapping.get("source")
            if isinstance(source_value, str):
                diagnostic["source"] = source_value

            tags_value = mapping.get("tags")
            tags_sequence = _ensure_json_sequence(tags_value)
            if tags_sequence is not None:
                tags = [tag for tag in tags_sequence if isinstance(tag, int)]
                if tags:
                    diagnostic["tags"] = tags

            related_value = mapping.get("relatedInformation")
            related_sequence = _ensure_json_sequence(related_value)
            if related_sequence is not None:
                related_entries: list[dict[str, Any]] = []
                for related in related_sequence:
                    related_mapping = _ensure_json_mapping(related)
                    if related_mapping is None:
                        continue
                    location_mapping = _ensure_json_mapping(related_mapping.get("location"))
                    if location_mapping is None:
                        continue
                    related_uri = location_mapping.get("uri")
                    related_range_mapping = _ensure_json_mapping(location_mapping.get("range"))
                    if not isinstance(related_uri, str) or related_range_mapping is None:
                        continue
                    related_range = self._normalise_range(related_range_mapping)
                    if related_range is None:
                        continue
                    related_message = related_mapping.get("message")
                    if not isinstance(related_message, str):
                        continue
                    related_entries.append(
                        {
                            "uri": related_uri,
                            "range": related_range,
                            "message": related_message,
                        }
                    )
                if related_entries:
                    diagnostic["relatedInformation"] = related_entries

            diagnostics.append(diagnostic)

        return diagnostics

    @staticmethod
    def _is_diagnostic_mapping(mapping: JsonMapping) -> bool:
        """Return ``True`` when ``mapping`` resembles an LSP diagnostic entry."""

        range_payload = mapping.get("range")
        range_mapping = _ensure_json_mapping(range_payload)
        if range_mapping is None:
            return False

        if not isinstance(mapping.get("message"), str):
            return False

        return True

    def _diagnostics_from_result(
        self, result: object, *, fallback_uri: str
    ) -> _DiagnosticReport | None:
        """Return diagnostics extracted from a Pyright diagnostic response."""

        if result is None:
            return None

        mapping_result = _ensure_json_mapping(result)
        if mapping_result is None:
            return None

        diagnostics: list[dict[str, Any]] = []
        explicit_report = False
        emitted_entries = False

        def _mark_observed() -> None:
            nonlocal explicit_report
            explicit_report = True

        def _extend_from_sequence(entries: Sequence[object] | None, *, uri: str) -> None:
            nonlocal emitted_entries
            if entries is None:
                return
            _mark_observed()
            normalised = self._normalise_diagnostic_entries(entries, uri=uri)
            if normalised:
                emitted_entries = True
            diagnostics.extend(normalised)

        items_value = mapping_result.get("items")
        items_sequence = _ensure_json_sequence(items_value)
        if items_sequence is not None:
            _mark_observed()
            for item in items_sequence:
                item_mapping = _ensure_json_mapping(item)
                if item_mapping is None:
                    continue
                uri_value = item_mapping.get("uri")
                item_uri = uri_value if isinstance(uri_value, str) else fallback_uri
                diagnostics_payload = item_mapping.get("diagnostics")
                diagnostics_sequence = _ensure_json_sequence(diagnostics_payload)
                if diagnostics_sequence is None:
                    diagnostics_sequence = _ensure_json_sequence(item_mapping.get("items"))
                if diagnostics_sequence is None and self._is_diagnostic_mapping(item_mapping):
                    diagnostics_sequence = (item_mapping,)
                _extend_from_sequence(diagnostics_sequence, uri=item_uri)

                related_documents = item_mapping.get("relatedDocuments")
                related_mapping = _ensure_json_mapping(related_documents)
                if related_mapping is not None:
                    for related_uri, related_payload in related_mapping.items():
                        if not isinstance(related_uri, str):
                            continue
                        related_mapping_payload = _ensure_json_mapping(related_payload)
                        if related_mapping_payload is None:
                            continue
                        related_sequence = _ensure_json_sequence(
                            related_mapping_payload.get("diagnostics")
                        )
                        if related_sequence is None:
                            related_sequence = _ensure_json_sequence(
                                related_mapping_payload.get("items")
                            )
                        _extend_from_sequence(related_sequence, uri=related_uri)

            if emitted_entries:
                return _DiagnosticReport(
                    diagnostics=diagnostics,
                    explicit=explicit_report,
                    entries=True,
                )

        diagnostics_payload = mapping_result.get("diagnostics")
        diagnostics_sequence = _ensure_json_sequence(diagnostics_payload)
        if diagnostics_sequence is not None:
            _extend_from_sequence(diagnostics_sequence, uri=fallback_uri)

        related_documents = mapping_result.get("relatedDocuments")
        related_mapping = _ensure_json_mapping(related_documents)
        if related_mapping is not None:
            _mark_observed()
            for related_uri, related_payload in related_mapping.items():
                if not isinstance(related_uri, str):
                    continue
                related_mapping_payload = _ensure_json_mapping(related_payload)
                if related_mapping_payload is None:
                    continue
                related_sequence = _ensure_json_sequence(related_mapping_payload.get("diagnostics"))
                if related_sequence is None:
                    related_sequence = _ensure_json_sequence(related_mapping_payload.get("items"))
                _extend_from_sequence(related_sequence, uri=related_uri)

        if not explicit_report:
            result_id_value = mapping_result.get("resultId")
            kind_value = mapping_result.get("kind")
            if isinstance(result_id_value, str) or isinstance(kind_value, str):
                _mark_observed()

        if not explicit_report:
            return None

        return _DiagnosticReport(
            diagnostics=diagnostics,
            explicit=explicit_report,
            entries=emitted_entries,
        )

    def _diagnostics_from_notifications(
        self,
        notifications: Sequence[Mapping[str, Any]],
        *,
        uri: str,
    ) -> list[dict[str, Any]] | None:
        """Return diagnostics extracted from ``publishDiagnostics`` notifications."""

        selected_version: int | None = None
        selected: list[dict[str, Any]] | None = None

        for message in notifications:
            params = _ensure_json_mapping(message.get("params"))
            if params is None:
                continue

            notif_uri = params.get("uri")
            if not isinstance(notif_uri, str):
                continue

            if not self._uris_equal(notif_uri, uri):
                continue

            version_value = params.get("version")
            version = version_value if isinstance(version_value, int) else None
            diagnostics_value = params.get("diagnostics")
            diagnostics_sequence = _ensure_json_sequence(diagnostics_value)
            if diagnostics_sequence is None:
                diagnostics_list: list[dict[str, Any]] = []
            else:
                diagnostics_list = self._normalise_diagnostic_entries(
                    diagnostics_sequence, uri=notif_uri
                )

            if selected is None:
                selected_version = version
                selected = diagnostics_list
                continue

            if version is None:
                continue

            if selected_version is None or version >= selected_version:
                selected_version = version
                selected = diagnostics_list

        return selected

    @staticmethod
    def _uris_equal(first: str, second: str) -> bool:
        """Return ``True`` if ``first`` and ``second`` identify the same file URI."""

        if first == second:
            return True

        first_path = LSPOrchestrator._uri_to_path(first)
        second_path = LSPOrchestrator._uri_to_path(second)

        if first_path is None or second_path is None:
            return False

        return LSPOrchestrator._resolve_path(first_path) == LSPOrchestrator._resolve_path(
            second_path
        )

    def _pyright_document_diagnostics(self, selector: PositionSpec) -> Mapping[str, Any] | None:
        """Return Pyright-backed diagnostics for ``selector`` when available."""

        session = self._pyright_session
        if session is None:
            return None

        path = self._path_for_selector(selector)
        if path is None:
            return None

        uri = self._ensure_document_open(path)
        if uri is None:
            return None

        initial_notifications = session.drain_notifications(
            method="textDocument/publishDiagnostics"
        )
        self._collect_pyright_progress()

        report: _DiagnosticReport | None
        try:
            result = session.request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": uri}, "previousResultId": None},
                timeout=10.0,
                cancellable=True,
            )
        except PyrightSessionError:
            report = None
        else:
            self._pyright_error = None
            report = self._diagnostics_from_result(result, fallback_uri=uri)
        finally:
            self._collect_pyright_progress()

        diagnostics: list[dict[str, Any]] | None = None
        explicit_report = False
        emitted_entries = False

        if report is not None:
            diagnostics = list(report.diagnostics or [])
            explicit_report = report.explicit
            emitted_entries = report.entries

        if diagnostics is None or not emitted_entries:
            initial_diagnostics = self._diagnostics_from_notifications(
                initial_notifications, uri=uri
            )
            if initial_diagnostics is not None:
                diagnostics = initial_diagnostics
                emitted_entries = bool(initial_diagnostics)
                explicit_report = True
        else:
            initial_notifications = []

        if diagnostics is None or not emitted_entries:
            notifications = session.drain_notifications(method="textDocument/publishDiagnostics")
            self._collect_pyright_progress()
            notification_diagnostics = self._diagnostics_from_notifications(notifications, uri=uri)
            if notification_diagnostics is not None:
                diagnostics = notification_diagnostics
                emitted_entries = bool(notification_diagnostics)
                explicit_report = True

        if diagnostics is None or not emitted_entries:
            notifications = session.wait_for_notifications(
                method="textDocument/publishDiagnostics", timeout=2.0
            )
            self._collect_pyright_progress()
            notification_diagnostics = self._diagnostics_from_notifications(notifications, uri=uri)
            if notification_diagnostics is not None:
                diagnostics = notification_diagnostics
                emitted_entries = bool(notification_diagnostics)
                explicit_report = True

        if diagnostics is None:
            if explicit_report:
                return {"diagnostics": []}
            return None

        return {"diagnostics": diagnostics}

    def _process_workspace_diagnostic_report(
        self, report: Mapping[str, Any], *, uri: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Normalise ``report`` into diagnostics and metadata descriptors."""

        diagnostics: list[dict[str, Any]] = []
        descriptors: list[dict[str, str]] = []

        result_id_value = report.get("resultId")
        kind_value = report.get("kind")
        descriptor: dict[str, str] = {"uri": uri}
        if isinstance(result_id_value, str) and result_id_value:
            self._workspace_diagnostic_result_ids[uri] = result_id_value
            descriptor["resultId"] = result_id_value
        elif kind_value != "unchanged":
            self._workspace_diagnostic_result_ids.pop(uri, None)
        if isinstance(kind_value, str) and kind_value:
            descriptor["kind"] = kind_value
        if len(descriptor) > 1:
            descriptors.append(descriptor)

        diagnostics_payload = report.get("diagnostics")
        diagnostics_sequence = _ensure_json_sequence(diagnostics_payload)
        if diagnostics_sequence is None:
            diagnostics_sequence = _ensure_json_sequence(report.get("items"))

        if diagnostics_sequence is None:
            cached = self._workspace_diagnostic_cache.get(uri)
            if cached is not None:
                diagnostics.extend(copy.deepcopy(cached))
        else:
            entries = self._normalise_diagnostic_entries(diagnostics_sequence, uri=uri)
            cached_entries = copy.deepcopy(entries)
            self._workspace_diagnostic_cache[uri] = cached_entries
            diagnostics.extend(copy.deepcopy(entries))

        related_documents = _ensure_json_mapping(report.get("relatedDocuments"))
        if related_documents is not None:
            for related_uri, related_report in related_documents.items():
                if not isinstance(related_uri, str):
                    continue
                related_mapping = _ensure_json_mapping(related_report)
                if related_mapping is None:
                    continue
                nested_diagnostics, nested_descriptors = self._process_workspace_diagnostic_report(
                    related_mapping, uri=related_uri
                )
                diagnostics.extend(nested_diagnostics)
                descriptors.extend(nested_descriptors)

        return diagnostics, descriptors

    def _normalise_workspace_diagnostic_response(
        self, result: object, *, fallback_uri: str
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, str]]]:
        """Return diagnostics and descriptors derived from ``result``."""

        mapping_result = _ensure_json_mapping(result)
        if mapping_result is None:
            return (None, [])

        diagnostics: list[dict[str, Any]] = []
        descriptors: list[dict[str, str]] = []

        items_value = mapping_result.get("items")
        items_sequence = _ensure_json_sequence(items_value)
        if items_sequence is not None:
            for item in items_sequence:
                item_mapping = _ensure_json_mapping(item)
                if item_mapping is None:
                    continue
                uri_value = item_mapping.get("uri")
                item_uri = uri_value if isinstance(uri_value, str) else fallback_uri
                nested_diagnostics, nested_descriptors = self._process_workspace_diagnostic_report(
                    item_mapping, uri=item_uri
                )
                diagnostics.extend(nested_diagnostics)
                descriptors.extend(nested_descriptors)
        else:
            nested_diagnostics, nested_descriptors = self._process_workspace_diagnostic_report(
                mapping_result, uri=fallback_uri
            )
            diagnostics.extend(nested_diagnostics)
            descriptors.extend(nested_descriptors)

        return diagnostics, descriptors

    def _normalise_prepare_rename_result(self, result: object) -> _RenamePrepareDetails:
        """Return a deterministic description of a prepare-rename payload."""

        if result is None:
            return _RenamePrepareDetails(
                allowed=False,
                message="Language server refused to prepare rename for the requested selector.",
            )

        mapping = _ensure_json_mapping(result)
        if mapping is None:
            return _RenamePrepareDetails(
                allowed=False,
                message="Language server returned an unsupported prepareRename payload.",
            )

        mapping_obj = cast("dict[str, object | None]", dict(mapping))

        default_behavior = bool(mapping_obj.get("defaultBehavior"))

        placeholder: str | None = None
        placeholder_value = cast("object | None", mapping_obj.get("placeholder"))
        if isinstance(placeholder_value, str):
            placeholder = placeholder_value
        else:
            text_candidate = cast("object | None", mapping_obj.get("text"))
            if isinstance(text_candidate, str):
                placeholder = text_candidate

        range_payload: Mapping[str, Any] | None = None
        range_value = cast("object | None", mapping_obj.get("range"))
        if range_value is not None:
            candidate = _ensure_json_mapping(range_value)
            if candidate is not None:
                range_payload = candidate
        if range_payload is None:
            start_mapping = _ensure_json_mapping(cast("object | None", mapping_obj.get("start")))
            end_mapping = _ensure_json_mapping(cast("object | None", mapping_obj.get("end")))
            if start_mapping is not None and end_mapping is not None:
                range_payload = {"start": start_mapping, "end": end_mapping}

        range_dict = self._normalise_range(range_payload) if range_payload is not None else None

        message: str | None = None
        if "message" in mapping_obj:
            message_candidate = mapping_obj["message"]
            if isinstance(message_candidate, str):
                message = message_candidate

        allowed = default_behavior or range_dict is not None
        if not allowed and message is None:
            message = "Language server refused to prepare rename for the requested selector."

        return _RenamePrepareDetails(
            allowed=allowed,
            range=range_dict,
            placeholder=placeholder,
            default_behavior=default_behavior,
            message=message,
        )

    def _pyright_prepare_rename(self, selector: PositionSpec) -> _RenamePrepareDetails | None:
        """Return Pyright-backed ``prepareRename`` details when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        params = self._selector_position_params(selector)
        if params is None:
            return None

        try:
            result = session.request(
                "textDocument/prepareRename",
                params,
                timeout=10.0,
                cancellable=True,
            )
        except PyrightSessionError as error:
            raw = str(error)
            message = raw
            if raw.startswith("{"):
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, Mapping):
                    decoded_mapping = cast("Mapping[str, object]", decoded)
                    message_candidate = decoded_mapping.get("message")
                    if isinstance(message_candidate, str):
                        message = message_candidate
            return _RenamePrepareDetails(allowed=False, message=message)
        finally:
            self._collect_pyright_progress()

        self._pyright_error = None

        return self._normalise_prepare_rename_result(result)

    def _pyright_workspace_diagnostics(self) -> Mapping[str, Any] | None:
        """Return Pyright-backed workspace diagnostics when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        handshake = session.handshake
        if handshake is not None:
            capabilities = handshake.result.get("capabilities")
            if isinstance(capabilities, Mapping):
                capabilities_mapping = cast("Mapping[str, Any]", capabilities)
                provider = capabilities_mapping.get("workspaceDiagnosticProvider")
                if not provider:
                    return None

        previous_result_ids = [
            {"uri": uri, "value": result_id}
            for uri, result_id in self._workspace_diagnostic_result_ids.items()
            if result_id
        ]

        try:
            result = session.request(
                "workspace/diagnostic",
                {"identifier": "lanser", "previousResultIds": previous_result_ids},
                timeout=15.0,
                cancellable=True,
            )
        except (PyrightSessionError, PyrightSessionTimeout) as error:
            self._pyright_error = str(error)
            return None
        finally:
            self._collect_pyright_progress()

        self._pyright_error = None

        diagnostics, descriptors = self._normalise_workspace_diagnostic_response(
            result, fallback_uri=self._workspace_root.as_uri()
        )

        if diagnostics is None:
            return None

        payload: dict[str, Any] = {"diagnostics": diagnostics}
        if descriptors:
            payload["resultIds"] = descriptors
        return payload

    def _workspace_diagnostics_result(self) -> Mapping[str, Any]:
        """Return diagnostics payload for the workspace."""

        lsp_payload = self._pyright_workspace_diagnostics()
        if lsp_payload is not None:
            self._record_pyright_source("pyright")
            return lsp_payload
        self._record_pyright_source("pyright")
        return {"diagnostics": []}

    @staticmethod
    def _line_offsets_for_text(source: str) -> list[int]:
        """Return byte offsets for the start of each line in ``source``."""

        offsets: list[int] = [0]
        for index, character in enumerate(source):
            if character == "\n":
                offsets.append(index + 1)
        return offsets

    @staticmethod
    def _offset_for_position(offsets: Sequence[int], source: str, line: int, column: int) -> int:
        """Return the byte offset into ``source`` for ``line``/``column``."""

        if line < 0:
            line = 0
        if column < 0:
            column = 0

        if line >= len(offsets):
            return len(source)

        line_start = offsets[line]
        next_index = offsets[line + 1] if line + 1 < len(offsets) else len(source)
        line_length = max(next_index - line_start, 0)
        clamped_column = min(column, line_length)
        return line_start + clamped_column

    def _classify_rename_role(
        self,
        *,
        context: _SelectorContext | None,
        path: Path,
        range_dict: Mapping[str, Sequence[int]],
    ) -> str:
        """Return a best-effort occurrence role for a rename change."""

        if context is None or context.symbol is None:
            return "match"

        symbol = context.symbol
        symbol_path = self._resolve_path(context.analysis.path)
        if self._resolve_path(path) != symbol_path:
            return "match"

        selection = symbol.selection.to_dict()
        if selection == range_dict:
            return "definition"

        return "reference"

    def _pyright_rename(
        self,
        selector: PositionSpec,
        *,
        new_name: str,
        apply: bool,
        context: _SelectorContext | None,
    ) -> tuple[Mapping[str, Any], _RenamePlan | None] | None:
        """Return Pyright-backed rename payload for ``selector`` when available."""

        session = self._pyright_session
        if session is None:
            return None

        session.refresh_configuration()

        prepare_details = self._pyright_prepare_rename(selector)
        prepare_payload: dict[str, Any] | None = None
        if prepare_details is not None:
            prepare_payload = prepare_details.to_payload()
            if not prepare_details.allowed:
                rename_payload: dict[str, Any] = {
                    "rename": {
                        "requestedName": new_name,
                        "applyMode": "apply" if apply else "preview",
                        "changes": [],
                        "changeCount": 0,
                        "applied": False,
                        "applyStatus": "prepare-denied",
                    },
                    "workspaceEdit": {"documentChanges": [], "changeCount": 0},
                    "diff": None,
                }
                if prepare_details.message is not None:
                    rename_payload["rename"]["message"] = prepare_details.message
                if prepare_payload is not None:
                    rename_payload["rename"]["prepare"] = prepare_payload
                return rename_payload, None

        params = self._selector_position_params(selector)
        if params is None:
            return None

        rename_params = dict(params)
        rename_params["newName"] = new_name

        try:
            result = session.request(
                "textDocument/rename",
                rename_params,
                timeout=15.0,
                cancellable=True,
            )
        except PyrightSessionError as error:
            self._pyright_error = str(error)
            return None
        finally:
            self._collect_pyright_progress()

        workspace_edit_raw = _ensure_json_mapping(result)
        if workspace_edit_raw is None:
            return None

        aggregated: dict[str, list[dict[str, Any]]] = {}
        change_count = 0

        changes_mapping = _ensure_json_mapping(workspace_edit_raw.get("changes"))
        if changes_mapping is not None:
            for uri, edits_value in changes_mapping.items():
                if not isinstance(uri, str):
                    continue
                edits_sequence = _ensure_json_sequence(edits_value)
                if edits_sequence is None:
                    continue
                for edit_entry in edits_sequence:
                    edit_mapping = _ensure_json_mapping(edit_entry)
                    if edit_mapping is None:
                        continue
                    range_mapping = _ensure_json_mapping(edit_mapping.get("range"))
                    new_text = edit_mapping.get("newText")
                    if range_mapping is None or not isinstance(new_text, str):
                        continue
                    range_dict = self._normalise_range(range_mapping)
                    if range_dict is None:
                        continue
                    aggregated.setdefault(uri, []).append(
                        {
                            "range": {
                                "start": list(range_dict["start"]),
                                "end": list(range_dict["end"]),
                            },
                            "newText": new_text,
                        }
                    )
                    change_count += 1

        document_changes_sequence = _ensure_json_sequence(workspace_edit_raw.get("documentChanges"))
        if document_changes_sequence is not None:
            for entry in document_changes_sequence:
                change_mapping = _ensure_json_mapping(entry)
                if change_mapping is None:
                    continue
                text_document = _ensure_json_mapping(change_mapping.get("textDocument"))
                edits_sequence = _ensure_json_sequence(change_mapping.get("edits"))
                if text_document is None or edits_sequence is None:
                    continue
                uri = text_document.get("uri")
                if not isinstance(uri, str):
                    continue
                for edit_entry in edits_sequence:
                    edit_mapping = _ensure_json_mapping(edit_entry)
                    if edit_mapping is None:
                        continue
                    range_mapping = _ensure_json_mapping(edit_mapping.get("range"))
                    new_text = edit_mapping.get("newText")
                    if range_mapping is None or not isinstance(new_text, str):
                        continue
                    range_dict = self._normalise_range(range_mapping)
                    if range_dict is None:
                        continue
                    aggregated.setdefault(uri, []).append(
                        {
                            "range": {
                                "start": list(range_dict["start"]),
                                "end": list(range_dict["end"]),
                            },
                            "newText": new_text,
                        }
                    )
                    change_count += 1

        if not aggregated:
            payload: dict[str, Any] = {
                "rename": {
                    "requestedName": new_name,
                    "applyMode": "apply" if apply else "preview",
                    "changes": [],
                    "changeCount": 0,
                    "applied": False,
                    "applyStatus": "planned" if apply else "preview",
                },
                "workspaceEdit": {"documentChanges": [], "changeCount": 0},
                "diff": None,
            }
            if context is not None and context.symbol is not None:
                rename_block = cast("dict[str, Any]", payload["rename"])
                rename_block["originalName"] = context.symbol.name
            return payload, None

        for edits in aggregated.values():
            edits.sort(
                key=lambda entry: (
                    entry["range"]["start"][0],
                    entry["range"]["start"][1],
                    entry["range"]["end"][0],
                    entry["range"]["end"][1],
                )
            )

        document_changes: list[dict[str, Any]] = []
        rename_changes: list[dict[str, Any]] = []
        diff_hunks: list[str] = []
        plan_edits: list[_RenameFileEdit] = []
        occurrence_index = 0
        original_name: str | None = None

        for uri, edits in sorted(aggregated.items()):
            doc_edits: list[dict[str, Any]] = []
            for edit in edits:
                doc_edits.append(
                    {
                        "range": {
                            "start": list(edit["range"]["start"]),
                            "end": list(edit["range"]["end"]),
                        },
                        "newText": edit["newText"],
                    }
                )
            document_changes.append({"textDocument": {"uri": uri}, "edits": doc_edits})

            path = self._uri_to_path(uri)
            if path is None:
                return None

            resolved_path = self._resolve_path(path)
            if not self._is_within(resolved_path, self._workspace_root):
                return None

            try:
                original_source = resolved_path.read_text(encoding="utf-8")
            except OSError:
                return None

            offsets = self._line_offsets_for_text(original_source)
            resolved_edits: list[_ResolvedTextEdit] = []
            for edit in doc_edits:
                range_payload = edit["range"]
                start_line = max(range_payload["start"][0] - 1, 0)
                start_col = max(range_payload["start"][1], 0)
                end_line = max(range_payload["end"][0] - 1, 0)
                end_col = max(range_payload["end"][1], 0)
                start_index = self._offset_for_position(
                    offsets, original_source, start_line, start_col
                )
                end_index = self._offset_for_position(offsets, original_source, end_line, end_col)
                original_text = original_source[start_index:end_index]
                resolved_edits.append(
                    _ResolvedTextEdit(
                        uri=uri,
                        path=resolved_path,
                        range={
                            "start": list(range_payload["start"]),
                            "end": list(range_payload["end"]),
                        },
                        new_text=edit["newText"],
                        original_text=original_text,
                        start_index=start_index,
                        end_index=end_index,
                    )
                )

            sorted_edits = sorted(
                resolved_edits, key=lambda item: (item.start_index, item.end_index)
            )
            updated_source = original_source
            for resolved_edit in reversed(sorted_edits):
                updated_source = (
                    updated_source[: resolved_edit.start_index]
                    + resolved_edit.new_text
                    + updated_source[resolved_edit.end_index :]
                )

            plan_edits.append(
                _RenameFileEdit(
                    path=resolved_path,
                    original_source=original_source,
                    updated_source=updated_source,
                )
            )

            diff_hunks.extend(
                difflib.unified_diff(
                    original_source.splitlines(keepends=True),
                    updated_source.splitlines(keepends=True),
                    fromfile=str(resolved_path),
                    tofile=str(resolved_path),
                    lineterm="",
                )
            )

            for resolved_edit in sorted_edits:
                range_payload = {
                    "start": list(resolved_edit.range["start"]),
                    "end": list(resolved_edit.range["end"]),
                }
                role = self._classify_rename_role(
                    context=context, path=resolved_edit.path, range_dict=range_payload
                )
                rename_changes.append(
                    {
                        "uri": resolved_edit.uri,
                        "range": range_payload,
                        "newText": resolved_edit.new_text,
                        "originalText": resolved_edit.original_text,
                        "occurrence": {
                            "index": occurrence_index,
                            "line": range_payload["start"][0],
                            "column": range_payload["start"][1],
                            "role": role,
                        },
                    }
                )
                occurrence_index += 1
                if original_name is None and resolved_edit.original_text:
                    original_name = resolved_edit.original_text

        if original_name is None and context is not None and context.symbol is not None:
            original_name = context.symbol.name

        diff_payload: Mapping[str, Any] | None = None
        if diff_hunks:
            diff_payload = {"format": "unified", "hunks": diff_hunks}

        workspace_edit_payload = {
            "documentChanges": document_changes,
            "changeCount": change_count,
        }

        rename_payload: dict[str, Any] = {
            "rename": {
                "requestedName": new_name,
                "applyMode": "apply" if apply else "preview",
                "changes": rename_changes,
                "changeCount": change_count,
                "applied": False,
                "applyStatus": "planned" if apply else "preview",
            },
            "workspaceEdit": workspace_edit_payload,
            "diff": diff_payload,
        }

        if prepare_payload is not None:
            rename_payload["rename"]["prepare"] = prepare_payload

        if original_name is not None:
            rename_block = cast("dict[str, Any]", rename_payload["rename"])
            rename_block["originalName"] = original_name

        plan = _RenamePlan(edits=tuple(plan_edits)) if plan_edits else None
        return rename_payload, plan

    def _module_path_from_module_name(self, module: str) -> Path | None:
        """Return the filesystem path referenced by ``module`` when available."""

        module_parts = [part for part in module.split(".") if part]
        if not module_parts:
            return None

        relative = Path(*module_parts)
        module_filename = relative.with_suffix(".py")
        package_init = relative / "__init__.py"

        search_roots = self._module_search_roots()
        if not search_roots:
            return None

        stamp = self._module_roots_stamp
        if stamp != self._module_path_cache_stamp:
            self._module_path_cache.clear()
            self._module_path_cache_stamp = stamp

        if module in self._module_path_cache:
            return self._module_path_cache[module]

        fallback = self._resolve_path(search_roots[0] / module_filename)

        resolved_path: Path | None = None
        for root in search_roots:
            candidate_file = self._resolve_path(root / module_filename)
            try:
                if candidate_file.exists():
                    resolved_path = candidate_file
                    break
            except OSError:
                pass
            candidate_init = self._resolve_path(root / package_init)
            try:
                if candidate_init.exists():
                    resolved_path = candidate_init
                    break
            except OSError:
                pass

        if resolved_path is None and not self._module_roots_configured:
            resolved_path = self._search_module_path(module_filename, package_init)

        if resolved_path is None:
            resolved_path = fallback

        self._module_path_cache[module] = resolved_path
        return resolved_path

    def _module_search_roots(self) -> tuple[Path, ...]:
        """Return candidate directories for resolving Python module paths."""

        workspace = self._workspace_root
        config_path = workspace / "pyrightconfig.json"
        pyproject_path = workspace / "pyproject.toml"

        config_mtime = self._safe_mtime(config_path)
        pyproject_mtime = self._safe_mtime(pyproject_path)

        cache_stamp = (config_mtime, pyproject_mtime)
        if self._module_roots_cache is not None and self._module_roots_stamp == cache_stamp:
            return self._module_roots_cache

        roots: list[Path] = [workspace]
        seen: set[Path] = {workspace}
        config_sourced = False

        config_payload: Mapping[str, Any] | None = None
        if config_mtime is not None:
            try:
                raw_text = config_path.read_text(encoding="utf-8")
            except OSError:
                raw_text = ""
            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    parsed = None
                else:
                    if isinstance(parsed, Mapping):
                        config_payload = cast("Mapping[str, Any]", parsed)

        if config_payload is not None:
            try:
                config_paths = _PyrightConfigPaths.model_validate(config_payload)
            except ValidationError:
                config_paths = None
            if config_paths is not None:
                self._append_module_roots_from_config(roots, seen, config_paths, workspace)
                config_sourced = True

        if config_payload is None and pyproject_mtime is not None:
            pyproject_payload = self._pyproject_pyright_settings(pyproject_path)
            if pyproject_payload is not None:
                try:
                    config_paths = _PyrightConfigPaths.model_validate(pyproject_payload)
                except ValidationError:
                    config_paths = None
                else:
                    self._append_module_roots_from_config(roots, seen, config_paths, workspace)
                    config_sourced = True

        if not config_sourced:
            heuristic_roots = ("src", "source", "python", "lib")
            for name in heuristic_roots:
                candidate = self._resolve_path(workspace / name)
                if not candidate.exists() or not candidate.is_dir():
                    continue
                if candidate not in seen:
                    roots.append(candidate)
                    seen.add(candidate)

        self._module_roots_cache = tuple(roots)
        self._module_roots_stamp = cache_stamp
        self._module_roots_configured = config_sourced
        return self._module_roots_cache

    def _append_module_roots_from_config(
        self,
        roots: list[Path],
        seen: set[Path],
        config_paths: _PyrightConfigPaths,
        workspace: Path,
    ) -> None:
        def _append_candidate(path: Path, *, base: Path | None = None) -> None:
            anchor = base or workspace
            if path.is_absolute():
                resolved = self._resolve_path(path)
            else:
                resolved = self._resolve_path(anchor / path)
            if resolved.is_file():
                resolved = resolved.parent
            if not resolved.exists() or not resolved.is_dir():
                return
            if resolved not in seen:
                roots.append(resolved)
                seen.add(resolved)

        for include_path in config_paths.include:
            _append_candidate(include_path)
        for extra_path in config_paths.extra_paths:
            _append_candidate(extra_path)
        for environment in config_paths.execution_environments:
            environment_root: Path | None = None
            if environment.root is not None:
                environment_root = self._resolve_workspace_path(environment.root)
                if environment_root.is_file():
                    environment_root = environment_root.parent
                if environment_root.exists() and environment_root.is_dir():
                    if environment_root not in seen:
                        roots.append(environment_root)
                        seen.add(environment_root)
                else:
                    environment_root = None
            for extra_path in environment.extra_paths:
                _append_candidate(extra_path, base=environment_root)

    def _search_module_path(self, module_filename: Path, package_init: Path) -> Path | None:
        """Return a resolved module path discovered via workspace search."""

        queue: deque[tuple[Path, int]] = deque([(self._workspace_root, 0)])
        visited: set[Path] = set()
        examined = 0

        while queue and examined < _MODULE_SEARCH_MAX_NODES:
            current, depth = queue.popleft()
            resolved_current = self._resolve_path(current)
            if resolved_current in visited:
                continue
            visited.add(resolved_current)
            examined += 1

            candidate_file = resolved_current / module_filename
            try:
                if candidate_file.exists():
                    return self._resolve_path(candidate_file)
            except OSError:
                pass

            candidate_init = resolved_current / package_init
            try:
                if candidate_init.exists():
                    return self._resolve_path(candidate_init)
            except OSError:
                pass

            if depth >= _MODULE_SEARCH_MAX_DEPTH:
                continue

            try:
                children = sorted(resolved_current.iterdir(), key=lambda entry: entry.name)
            except OSError:
                continue

            for child in children:
                try:
                    if child.is_symlink() or not child.is_dir():
                        continue
                except OSError:
                    continue

                name = child.name
                if name in _MODULE_SEARCH_SKIP:
                    continue

                resolved_child = self._resolve_path(child)
                if resolved_child in visited:
                    continue
                if not self._is_within(resolved_child, self._workspace_root):
                    continue

                queue.append((resolved_child, depth + 1))

        return None

    def _pyproject_pyright_settings(self, pyproject_path: Path) -> Mapping[str, Any] | None:
        try:
            raw_text = pyproject_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw_text.strip():
            return None
        try:
            parsed = tomllib.loads(raw_text)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            return None
        root_mapping = _ensure_json_mapping(parsed)
        if root_mapping is None:
            return None
        tool_block = _ensure_json_mapping(root_mapping.get("tool"))
        if tool_block is None:
            return None
        pyright_block = tool_block.get("pyright")
        return _ensure_json_mapping(pyright_block)

    @staticmethod
    def _safe_mtime(path: Path) -> float | None:
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return stat_result.st_mtime

    def _module_path_from_symbol_selector(self, selector: SymbolSelector) -> Path | None:
        """Return the filesystem path referenced by ``selector`` if resolvable."""

        return self._module_path_from_module_name(selector.module)

    @staticmethod
    def _module_name_from_ast_selector(selector: AstPathSelector) -> str | None:
        """Return the module name referenced by ``selector`` when available."""

        for segment in selector.path:
            value = segment.value
            if segment.axis == "module" and isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _symbol_from_ast_selector(
        analysis: PythonModuleAnalysis, selector: AstPathSelector
    ) -> SymbolData | None:
        """Return the symbol referenced by ``selector`` if resolvable."""

        symbol_axes = {"class", "def", "function", "asyncFunction", "async-def"}
        parts: list[str] = []
        for segment in selector.path:
            if segment.axis in symbol_axes:
                value = segment.value
                if isinstance(value, str) and value:
                    parts.append(value)

        if not parts:
            return None

        qualname = ".".join(parts)
        return analysis.find_by_qualname(qualname)

    def _record_module_analysis_failure(
        self,
        path: Path,
        *,
        kind: Literal["unreadable", "invalid-encoding"],
        message: str,
    ) -> None:
        """Remember why static analysis failed for ``path``."""

        resolved = self._resolve_path(path)
        self._module_analysis_failures[resolved] = _ModuleAnalysisFailure(
            kind=kind, message=message
        )
        self._module_analysis_cache.pop(resolved, None)

    def _clear_module_analysis_failure(self, path: Path) -> None:
        """Remove cached failure metadata for ``path`` if present."""

        resolved = self._resolve_path(path)
        self._module_analysis_failures.pop(resolved, None)

    def _analysis_for_path(self, path: Path) -> PythonModuleAnalysis | None:
        """Return cached :class:`PythonModuleAnalysis` for ``path`` if available."""

        resolved = self._resolve_path(path)
        if resolved.suffix not in {".py", ".pyi"}:
            return None
        if resolved in self._module_analysis_cache:
            return self._module_analysis_cache[resolved]

        try:
            source = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            reason = error.reason or "invalid UTF-8 sequence"
            message = f"Failed to decode '{resolved}' as UTF-8: {reason}."
            self._record_module_analysis_failure(resolved, kind="invalid-encoding", message=message)
            return None
        except OSError as error:
            description = error.strerror or str(error)
            message = f"Failed to read '{resolved}': {description}."
            self._record_module_analysis_failure(resolved, kind="unreadable", message=message)
            return None

        analysis = analyse_python_module(resolved, source=source)
        if analysis is not None:
            self._module_analysis_cache[resolved] = analysis
            self._clear_module_analysis_failure(resolved)
        else:
            self._record_module_analysis_failure(
                resolved,
                kind="unreadable",
                message=f"Failed to analyse '{resolved}' using stub parser.",
            )
        return analysis

    def _selector_context(self, selector: PositionSpec) -> _SelectorContext | None:
        """Return the analysis context associated with ``selector``."""

        if isinstance(selector, SymbolSelector):
            path = self._module_path_from_symbol_selector(selector)
            if path is None:
                return None
            analysis = self._analysis_for_path(path)
            if analysis is None:
                return None
            symbol = analysis.find_by_qualname(selector.symbol)
            return _SelectorContext(analysis=analysis, symbol=symbol, position=None)

        if isinstance(selector, AstPathSelector):
            module_name = self._module_name_from_ast_selector(selector)
            if module_name is None:
                return None
            path = self._module_path_from_module_name(module_name)
            if path is None:
                return None
            analysis = self._analysis_for_path(path)
            if analysis is None:
                return None
            symbol = self._symbol_from_ast_selector(analysis, selector)
            position: tuple[int, int] | None = None
            if symbol is not None:
                position = (
                    symbol.selection.start_line,
                    symbol.selection.start_col + 1,
                )
            return _SelectorContext(analysis=analysis, symbol=symbol, position=position)

        paths = self._selector_paths(selector)
        if not paths:
            return None

        analysis = self._analysis_for_path(paths[0])
        if analysis is None:
            return None

        position = self._position_for_selector(selector, analysis)
        symbol = analysis.find_at(*position) if position is not None else None
        return _SelectorContext(analysis=analysis, symbol=symbol, position=position)

    def _position_for_selector(
        self, selector: PositionSpec, analysis: PythonModuleAnalysis
    ) -> tuple[int, int] | None:
        """Return a best-effort ``(line, column)`` for ``selector``."""

        if isinstance(selector, CursorSelector):
            return (selector.line, selector.column)
        if isinstance(selector, RangeSelector):
            return (selector.start_line, selector.start_column)
        if isinstance(selector, AnchorSelector):
            return analysis.find_snippet(selector.snippet)
        return None

    def _symbol_payload(
        self, *, analysis: PythonModuleAnalysis, symbol: SymbolData
    ) -> dict[str, Any]:
        """Return a deterministic payload describing ``symbol``."""

        payload: dict[str, Any] = {
            "uri": analysis.uri,
            "range": symbol.range.to_dict(),
            "selectionRange": symbol.selection.to_dict(),
            "symbol": self._symbol_descriptor(symbol),
        }
        if symbol.signature:
            payload["signature"] = symbol.signature
        if symbol.docstring:
            payload["docstring"] = symbol.docstring
        return payload

    @staticmethod
    def _range_dict(
        start_line: int, start_col: int, end_line: int, end_col: int
    ) -> dict[str, list[int]]:
        """Return a dict describing a range with ``start`` and ``end`` keys."""

        return {
            "start": [start_line, start_col],
            "end": [end_line, end_col],
        }

    def _symbol_descriptor(self, symbol: SymbolData) -> dict[str, Any]:
        """Return a compact descriptor for ``symbol``."""

        descriptor: dict[str, Any] = {
            "name": symbol.name,
            "qualname": symbol.qualname,
            "kind": symbol.kind,
        }
        if symbol.is_async:
            descriptor["async"] = True
        return descriptor

    def _selector_failure_outcome(
        self,
        *,
        kind: str,
        selector: PositionSpec,
        error_kind: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> OperationOutcome:
        """Return a not-found outcome for selector resolution failures."""

        error_payload: dict[str, Any] = {"kind": error_kind, "message": message}
        if details is not None:
            error_payload.update(dict(details))
        payload = {
            "error": error_payload,
            "selector": selector.to_payload(),
        }
        return OperationOutcome(
            ok=False,
            message=message,
            payload=payload,
            exit_code=ExitCode.NOT_FOUND,
        )

    def _selector_resolution_failure(
        self,
        *,
        kind: str,
        selector: PositionSpec,
        context: _SelectorContext | None,
    ) -> OperationOutcome | None:
        """Return an error outcome when ``selector`` lacks a Pyright target."""

        if context is not None:
            if isinstance(selector, AnchorSelector) and context.position is None:
                path = self._path_for_selector(selector)
                location = str(path) if path is not None else selector.uri
                details: dict[str, Any] = {
                    "path": location,
                    "snippet": selector.snippet,
                    "context": selector.context,
                }
                message = (
                    f"{kind.title()} operation failed because the anchor snippet "
                    f"could not be located in '{location}'."
                )
                return self._selector_failure_outcome(
                    kind=kind,
                    selector=selector,
                    error_kind="anchor-snippet-missing",
                    message=message,
                    details=details,
                )

            if isinstance(selector, SymbolSelector) and context.symbol is None:
                path = self._path_for_selector(selector)
                details = {
                    "module": selector.module,
                    "symbol": selector.symbol,
                }
                if path is not None:
                    details["path"] = str(path)
                message = (
                    f"{kind.title()} operation failed because symbol "
                    f"'{selector.symbol}' was not found in module "
                    f"'{selector.module}'."
                )
                return self._selector_failure_outcome(
                    kind=kind,
                    selector=selector,
                    error_kind="symbol-not-found",
                    message=message,
                    details=details,
                )

            if isinstance(selector, AstPathSelector) and context.symbol is None:
                path = self._path_for_selector(selector)
                module_name = self._module_name_from_ast_selector(selector)
                details = {"path": str(path)} if path is not None else {}
                if module_name is not None:
                    details["module"] = module_name
                message = (
                    f"{kind.title()} operation failed because the AST selector did "
                    "not resolve to a symbol."
                )
                return self._selector_failure_outcome(
                    kind=kind,
                    selector=selector,
                    error_kind="ast-path-unresolved",
                    message=message,
                    details=details,
                )

        if context is None:
            path = self._path_for_selector(selector)
            resolved_path = self._resolve_path(path) if path is not None else None
            analysis_failure = (
                self._module_analysis_failures.get(resolved_path)
                if resolved_path is not None
                else None
            )
            exists = resolved_path.exists() if resolved_path is not None else False
            location = str(resolved_path) if resolved_path is not None else None

            if (
                analysis_failure is not None
                and analysis_failure.kind == "invalid-encoding"
                and location is not None
            ):
                message = (
                    f"{kind.title()} operation failed because '{location}' contains "
                    "bytes that are not valid UTF-8."
                )
                error_payload: dict[str, Any] = {
                    "kind": "analysis-target-invalid-encoding",
                    "message": message,
                    "path": location,
                    "encoding": "utf-8",
                    "reason": analysis_failure.message,
                }
                payload = {
                    "error": error_payload,
                    "selector": selector.to_payload(),
                }
                metadata = self._pyright_metadata()
                return OperationOutcome(
                    ok=False,
                    message=message,
                    payload=payload,
                    exit_code=ExitCode.LS_CRASH,
                    metadata={"pyright": metadata} if metadata is not None else None,
                )

            if isinstance(selector, SymbolSelector):
                details = {
                    "module": selector.module,
                    "symbol": selector.symbol,
                }
                if location is not None:
                    details["path"] = location
                    details["exists"] = exists
                if analysis_failure is not None:
                    details["analysisFailure"] = analysis_failure.model_dump()
                if exists:
                    message = (
                        f"{kind.title()} operation failed because module "
                        f"'{selector.module}' could not be read for analysis."
                    )
                else:
                    message = (
                        f"{kind.title()} operation failed because module "
                        f"'{selector.module}' does not exist on disk."
                    )
                return self._selector_failure_outcome(
                    kind=kind,
                    selector=selector,
                    error_kind="module-unresolved",
                    message=message,
                    details=details,
                )

            if isinstance(selector, AstPathSelector):
                module_name = self._module_name_from_ast_selector(selector)
                details = {}
                if module_name is not None:
                    details["module"] = module_name
                if location is not None:
                    details["path"] = location
                    details["exists"] = exists
                if analysis_failure is not None:
                    details["analysisFailure"] = analysis_failure.model_dump()
                if exists:
                    message = (
                        f"{kind.title()} operation failed because the module "
                        "referenced by the AST selector could not be read."
                    )
                else:
                    message = (
                        f"{kind.title()} operation failed because the module "
                        "referenced by the AST selector does not exist on disk."
                    )
                return self._selector_failure_outcome(
                    kind=kind,
                    selector=selector,
                    error_kind="ast-module-unresolved",
                    message=message,
                    details=details,
                )

            if isinstance(selector, AnchorSelector | CursorSelector | RangeSelector):
                details = {"uri": selector.uri}
                if location is not None:
                    details["path"] = location
                    details["exists"] = exists
                if analysis_failure is not None:
                    details["analysisFailure"] = analysis_failure.model_dump()
                if exists:
                    message = (
                        f"{kind.title()} operation failed because '{location}' "
                        "could not be read for analysis."
                    )
                else:
                    target = location or selector.uri
                    message = (
                        f"{kind.title()} operation failed because '{target}' "
                        "does not exist on disk."
                    )
                return self._selector_failure_outcome(
                    kind=kind,
                    selector=selector,
                    error_kind="analysis-target-unavailable",
                    message=message,
                    details=details,
                )

        return None

    def _definition_result(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return definition payload for ``selector`` if available."""

        lsp_payload = self._pyright_definition(selector, context)
        if lsp_payload is not None:
            self._record_pyright_source("pyright")
            return lsp_payload
        self._record_pyright_source("pyright")
        return {"definitions": []}

    def _definition_result_stub(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return stub definition payload for ``selector`` if available."""

        context = context or self._selector_context(selector)
        if context is None or context.symbol is None:
            return {"definitions": []}

        entry = self._symbol_payload(analysis=context.analysis, symbol=context.symbol)
        return {"definitions": [entry]}

    def _references_result(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return reference payload for ``selector`` if available."""

        context = context or self._selector_context(selector)
        lsp_payload = self._pyright_references(selector, context=context)
        if lsp_payload is not None:
            self._record_pyright_source("pyright")
            return lsp_payload
        self._record_pyright_source("pyright")
        return {"references": []}

    def _hover_result(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return hover payload for ``selector`` if available."""

        context = context or self._selector_context(selector)
        lsp_payload = self._pyright_hover(selector, context=context)
        if lsp_payload is not None:
            self._record_pyright_source("pyright")
            return lsp_payload
        self._record_pyright_source("pyright")
        return {"hover": {"contents": []}}

    def _symbols_result(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return symbol listing for the module referenced by ``selector``."""

        context = context or self._selector_context(selector)
        pyright_payload = self._pyright_document_symbols(selector, context=context)
        if pyright_payload is not None:
            self._record_pyright_source("pyright")
            return pyright_payload
        self._record_pyright_source("pyright")
        return {"symbols": []}

    def _diagnostics_result(
        self, selector: PositionSpec, *, context: _SelectorContext | None
    ) -> Mapping[str, Any]:
        """Return diagnostics payload for ``selector`` if available."""

        lsp_payload = self._pyright_document_diagnostics(selector)
        if lsp_payload is not None:
            self._record_pyright_source("pyright")
            return lsp_payload

        self._record_pyright_source("pyright")
        return {"diagnostics": []}

    def _rename_result(
        self,
        selector: PositionSpec,
        *,
        new_name: str,
        apply: bool,
        context: _SelectorContext | None,
    ) -> tuple[Mapping[str, Any], _RenamePlan | None]:
        """Return rename payload and apply plan for ``selector``."""

        context = context or self._selector_context(selector)
        pyright_payload = self._pyright_rename(
            selector, new_name=new_name, apply=apply, context=context
        )
        if pyright_payload is not None:
            self._record_pyright_source("pyright")
            return pyright_payload

        if self._pyright_error is None:
            self._pyright_error = (
                "Pyright did not produce a workspace edit for the requested rename"
            )
        self._record_pyright_source("pyright")
        mode = "apply" if apply else "preview"
        payload: dict[str, Any] = {
            "rename": {
                "requestedName": new_name,
                "applyMode": mode,
                "changes": [],
                "changeCount": 0,
                "applied": False,
                "applyStatus": "unavailable",
            },
            "workspaceEdit": {"documentChanges": [], "changeCount": 0},
            "diff": None,
        }
        if context is not None and context.symbol is not None:
            payload["rename"]["originalName"] = context.symbol.name
        return payload, None

    def _enforce_workspace_jail(
        self, *, kind: str, selector: PositionSpec
    ) -> OperationOutcome | None:
        """Return an error outcome if ``selector`` violates workspace guardrails."""

        paths = self._selector_paths(selector)
        if not paths:
            return None

        for path in paths:
            if not self._is_within(path, self._workspace_root):
                payload = {
                    "error": {
                        "kind": "workspace-jail",
                        "message": (
                            f"{kind.title()} operation targets path outside the workspace jail."
                        ),
                        "path": str(self._resolve_path(path)),
                        "workspace": str(self._workspace_root),
                    },
                    "selector": selector.to_payload(),
                }
                return OperationOutcome(
                    ok=False,
                    message=(
                        "Operation denied because the selector resolves outside the workspace jail."
                    ),
                    payload=payload,
                    exit_code=ExitCode.FS_PERMISSIONS,
                )

            if self._deny_paths and any(
                self._is_within(path, denied) for denied in self._deny_paths
            ):
                payload = {
                    "error": {
                        "kind": "path-denied",
                        "message": (
                            f"{kind.title()} operation targets a path denied by workspace filters."
                        ),
                        "path": str(self._resolve_path(path)),
                        "deny": [str(root) for root in self._deny_paths],
                    },
                    "selector": selector.to_payload(),
                }
                return OperationOutcome(
                    ok=False,
                    message="Operation denied by workspace path filters.",
                    payload=payload,
                    exit_code=ExitCode.FS_PERMISSIONS,
                )

            if self._allow_paths and not any(
                self._is_within(path, allowed) for allowed in self._allow_paths
            ):
                payload = {
                    "error": {
                        "kind": "path-not-allowed",
                        "message": (
                            f"{kind.title()} operation targets a path outside the allowed filters."
                        ),
                        "path": str(self._resolve_path(path)),
                        "allow": [str(root) for root in self._allow_paths],
                    },
                    "selector": selector.to_payload(),
                }
                return OperationOutcome(
                    ok=False,
                    message="Operation denied because the path is not allowed by filters.",
                    payload=payload,
                    exit_code=ExitCode.FS_PERMISSIONS,
                )

        return None

    def _selector_resolution(
        self,
        *,
        kind: str,
        selector: PositionSpec,
        context: _SelectorContext | None,
        result: Mapping[str, Any] | None,
        source: Literal["pyright", "stub"],
    ) -> Mapping[str, Any]:
        """Return resolution metadata summarising selector anchoring."""

        selected_payload = selector.to_payload()
        location = self._resolution_location(
            kind=kind, selector=selector, result=result, context=context
        )
        details = self._resolution_details(kind=kind, result=result)

        candidate: dict[str, Any] = {
            "rank": 0,
            "spec": selected_payload,
            "score": 1.0,
            "source": source,
        }

        if source == "pyright":
            candidate["reason"] = "language-server"
            strategy = "language-server"
            if location is not None:
                notes = f"{kind.title()} anchored using Pyright language server metadata."
            else:
                notes = "Pyright responded without concrete locations; preserving selector context."
        else:
            candidate["reason"] = "analysis-fallback"
            strategy = "analysis-fallback"
            if self._pyright_error is not None:
                notes = "Pyright unavailable; using static analysis fallback metadata."
            else:
                notes = "Language server fallback active; using static analysis selector metadata."

        if context is not None and context.symbol is not None:
            candidate["symbol"] = self._symbol_descriptor(context.symbol)
            candidate["selectionRange"] = context.symbol.selection.to_dict()
            candidate["documentUri"] = context.analysis.uri

        if location is not None:
            candidate["location"] = location

        if details:
            candidate["details"] = details

        payload = {
            "schemaVersion": "resolution.v1",
            "status": "resolved",
            "selected": selected_payload,
            "candidates": [candidate],
            "explanation": {
                "version": "rpos-v0",
                "strategy": strategy,
                "notes": notes,
            },
            "repositioning": self._selector_repositioning(selector),
        }
        return payload

    def _resolution_location(
        self,
        *,
        kind: str,
        selector: PositionSpec,
        result: Mapping[str, Any] | None,
        context: _SelectorContext | None,
    ) -> Mapping[str, Any] | None:
        """Return a primary location derived from ``result`` when available."""

        if not isinstance(result, Mapping):
            return None

        if kind == "definition":
            entries_value = result.get("definitions")
            entries_sequence = _ensure_json_sequence(entries_value)
            if entries_sequence is None:
                return None
            for entry in entries_sequence:
                entry_mapping = _ensure_json_mapping(entry)
                if entry_mapping is None:
                    continue
                uri_value = entry_mapping.get("uri")
                if not isinstance(uri_value, str):
                    continue
                range_mapping = _ensure_json_mapping(entry_mapping.get("selectionRange"))
                if range_mapping is None:
                    range_mapping = _ensure_json_mapping(entry_mapping.get("range"))
                if range_mapping is None:
                    continue
                location: dict[str, Any] = {
                    "uri": uri_value,
                    "range": copy.deepcopy(range_mapping),
                }
                entry_type = entry_mapping.get("type")
                if isinstance(entry_type, str):
                    location["type"] = entry_type
                return location
            return None

        if kind == "references":
            entries_value = result.get("references")
            entries_sequence = _ensure_json_sequence(entries_value)
            if entries_sequence is None:
                return None
            preferred: JsonMapping | None = None
            fallback: JsonMapping | None = None
            for entry in entries_sequence:
                entry_mapping = _ensure_json_mapping(entry)
                if entry_mapping is None:
                    continue
                if fallback is None:
                    fallback = entry_mapping
                if entry_mapping.get("role") == "definition":
                    preferred = entry_mapping
                    break
            target = preferred or fallback
            if target is None:
                return None
            uri_value = target.get("uri")
            if not isinstance(uri_value, str):
                return None
            range_mapping = _ensure_json_mapping(target.get("selectionRange"))
            if range_mapping is None:
                range_mapping = _ensure_json_mapping(target.get("range"))
            if range_mapping is None:
                return None
            location = {
                "uri": uri_value,
                "range": copy.deepcopy(range_mapping),
            }
            role_value = target.get("role")
            if isinstance(role_value, str):
                location["role"] = role_value
            return location

        if kind == "hover":
            hover_block = _ensure_json_mapping(result.get("hover"))
            if hover_block is None:
                return None
            range_mapping = _ensure_json_mapping(hover_block.get("range"))
            if range_mapping is None:
                return None
            uri: str | None = None
            if context is not None:
                uri = context.analysis.uri
            if uri is None:
                path = self._path_for_selector(selector)
                if path is not None:
                    uri = self._resolve_path(path).as_uri()
            if uri is None:
                return None
            return {"uri": uri, "range": copy.deepcopy(range_mapping)}

        if kind == "diagnostics":
            entries_value = result.get("diagnostics")
            entries_sequence = _ensure_json_sequence(entries_value)
            if entries_sequence is None:
                return None
            for entry in entries_sequence:
                entry_mapping = _ensure_json_mapping(entry)
                if entry_mapping is None:
                    continue
                uri_value = entry_mapping.get("uri")
                range_mapping = _ensure_json_mapping(entry_mapping.get("range"))
                if not isinstance(uri_value, str) or range_mapping is None:
                    continue
                location = {
                    "uri": uri_value,
                    "range": copy.deepcopy(range_mapping),
                }
                severity = entry_mapping.get("severity")
                if isinstance(severity, str):
                    location["severity"] = severity
                return location
            return None

        if kind == "rename":
            rename_block = _ensure_json_mapping(result.get("rename"))
            if rename_block is None:
                return None
            changes_value = rename_block.get("changes")
            changes_sequence = _ensure_json_sequence(changes_value)
            if changes_sequence is None:
                return None
            for change in changes_sequence:
                change_mapping = _ensure_json_mapping(change)
                if change_mapping is None:
                    continue
                uri_value = change_mapping.get("uri")
                range_mapping = _ensure_json_mapping(change_mapping.get("range"))
                if not isinstance(uri_value, str) or range_mapping is None:
                    continue
                location = {
                    "uri": uri_value,
                    "range": copy.deepcopy(range_mapping),
                }
                occurrence_mapping = _ensure_json_mapping(change_mapping.get("occurrence"))
                if occurrence_mapping is not None:
                    location["occurrence"] = dict(occurrence_mapping)
                role = change_mapping.get("role")
                if isinstance(role, str):
                    location["role"] = role
                return location
            return None

        return None

    def _resolution_details(
        self,
        *,
        kind: str,
        result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Return summary statistics derived from ``result``."""

        if not isinstance(result, Mapping):
            return {}

        details: dict[str, Any] = {}

        if kind == "definition":
            entries_value = result.get("definitions")
            entries_sequence = _ensure_json_sequence(entries_value)
            if entries_sequence is not None:
                count = sum(
                    1 for entry in entries_sequence if _ensure_json_mapping(entry) is not None
                )
                if count:
                    details["definitionCount"] = count
            return details

        if kind == "references":
            entries_value = result.get("references")
            entries_sequence = _ensure_json_sequence(entries_value)
            if entries_sequence is not None:
                mappings = [
                    entry_mapping
                    for entry in entries_sequence
                    if (entry_mapping := _ensure_json_mapping(entry)) is not None
                ]
                if mappings:
                    details["totalReferences"] = len(mappings)
                    definition_matches = sum(
                        1 for entry in mappings if entry.get("role") == "definition"
                    )
                    reference_matches = sum(
                        1 for entry in mappings if entry.get("role") == "reference"
                    )
                    if definition_matches:
                        details["definitionMatches"] = definition_matches
                    if reference_matches:
                        details["referenceMatches"] = reference_matches
            return details

        if kind == "hover":
            hover_block = _ensure_json_mapping(result.get("hover"))
            if hover_block is not None:
                contents_sequence = _ensure_json_sequence(hover_block.get("contents"))
                if contents_sequence is not None:
                    count = len(list(contents_sequence))
                    if count:
                        details["contentEntries"] = count
            return details

        if kind == "diagnostics":
            entries_value = result.get("diagnostics")
            entries_sequence = _ensure_json_sequence(entries_value)
            if entries_sequence is not None:
                count = sum(
                    1 for entry in entries_sequence if _ensure_json_mapping(entry) is not None
                )
                if count:
                    details["diagnosticCount"] = count
            return details

        if kind == "rename":
            rename_block = _ensure_json_mapping(result.get("rename"))
            if rename_block is not None:
                change_count = rename_block.get("changeCount")
                if isinstance(change_count, int):
                    details["changeCount"] = change_count
                apply_status = rename_block.get("applyStatus")
                if isinstance(apply_status, str):
                    details["applyStatus"] = apply_status
            return details

        return details

    def _selector_operation(
        self,
        *,
        kind: str,
        selector: str,
        result_builder: _ResultBuilder | None = None,
        cache: bool = True,
    ) -> OperationOutcome:
        """Execute a selector-driven operation returning an analysis bundle."""

        lock_guard = self._workspace_lock_guardrail(operation=kind)
        if lock_guard is not None:
            return self._trace_outcome(
                operation=kind,
                outcome=lock_guard,
                selector=selector,
            )

        self._reset_pyright_progress()
        self._pyright_result_source = None
        configured_indexing = self._settings.position_encoding
        try:
            spec = parse_selector(
                selector,
                workspace=self._settings.workspace,
                indexing=configured_indexing,
            )
        except SelectorParseError as error:
            message = "Failed to parse selector."
            payload = {
                "error": error.to_dict(),
                "selector": selector,
            }
            outcome = OperationOutcome(
                ok=False,
                message=message,
                payload=payload,
                exit_code=ExitCode.BAD_SELECTOR_SYNTAX,
            )
            return self._trace_outcome(operation=kind, outcome=outcome, selector=selector)

        guardrail = self._enforce_workspace_jail(kind=kind, selector=spec)
        if guardrail is not None:
            return self._trace_outcome(
                operation=kind, outcome=guardrail, selector=selector, spec=spec
            )

        dirty = self._dirty_guardrail(operation=kind)
        if dirty is not None:
            return self._trace_outcome(operation=kind, outcome=dirty, selector=selector, spec=spec)

        self._ensure_pyright_session()

        if self._pyright_session is None:
            failure = self._pyright_failure_outcome(
                kind=kind,
                selector_text=selector,
                spec=spec,
            )
            return self._trace_outcome(
                operation=kind,
                outcome=failure,
                selector=selector,
                spec=spec,
            )

        negotiated_indexing = self._active_position_encoding()
        if negotiated_indexing != configured_indexing and isinstance(
            spec, CursorSelector | RangeSelector
        ):
            spec = cast(
                "PositionSpec",
                spec.model_copy(update={"indexing": negotiated_indexing}),
            )

        selector_payload = spec.to_payload()
        context = self._selector_context(spec)
        failure = self._selector_resolution_failure(kind=kind, selector=spec, context=context)
        if failure is not None:
            return self._trace_outcome(
                operation=kind,
                outcome=failure,
                selector=selector,
                spec=spec,
            )
        result_payload = (
            result_builder(spec, context=context)
            if result_builder is not None
            else _EMPTY_JSON_MAPPING
        )

        if self._pyright_error is not None:
            failure = self._pyright_failure_outcome(
                kind=kind,
                selector_text=selector,
                spec=spec,
            )
            return self._trace_outcome(
                operation=kind,
                outcome=failure,
                selector=selector,
                spec=spec,
            )

        if (
            self._pyright_result_source == "stub"
            and self._pyright_session is not None
            and self._pyright_error is None
        ):
            failure = self._stub_fallback_failure(
                kind=kind,
                selector_text=selector,
                spec=spec,
            )
            self._pyright_result_source = None
            return self._trace_outcome(
                operation=kind,
                outcome=failure,
                selector=selector,
                spec=spec,
            )

        request_fingerprint = json.dumps(
            {"selector": selector_payload, "result": result_payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = (kind, request_fingerprint)
        cache_fingerprint = hashlib.sha256(f"{kind}:{request_fingerprint}".encode()).hexdigest()
        cache_identifier = f"sha256:{cache_fingerprint}"

        if cache and cache_key in self._selector_cache:
            cached = self._selector_cache[cache_key]
            metadata = {
                "cache": {
                    "hit": True,
                    "key": cache_identifier,
                    "size": len(self._selector_cache),
                    "enabled": True,
                }
            }
            pyright_meta = self._pyright_metadata()
            if pyright_meta is not None:
                metadata["pyright"] = pyright_meta
            outcome = OperationOutcome(
                ok=True,
                message=f"{kind.title()} bundle served from cache.",
                payload=copy.deepcopy(cached),
                exit_code=ExitCode.OK,
                metadata=metadata,
            )
            return self._trace_outcome(
                operation=kind,
                outcome=outcome,
                selector=selector,
                spec=spec,
            )

        source = self._pyright_result_source or "pyright"

        bundle = AnalysisBundle.for_selector(
            kind=kind,
            selector=spec,
            environment=self._environment_payload(),
            resolution=self._selector_resolution(
                kind=kind,
                selector=spec,
                context=context,
                result=result_payload,
                source=source,
            ),
            result=result_payload,
        )

        payload = bundle.to_dict()
        if cache:
            self._selector_cache[cache_key] = copy.deepcopy(payload)
            self._selector_cache_fingerprints[cache_key] = cache_identifier

        metadata = {
            "cache": {
                "hit": False,
                "key": cache_identifier,
                "size": len(self._selector_cache),
                "enabled": cache,
            }
        }
        pyright_meta = self._pyright_metadata()
        if pyright_meta is not None:
            metadata["pyright"] = pyright_meta

        self._pyright_result_source = None

        message = f"{kind.title()} bundle generated via Pyright."

        outcome = OperationOutcome(
            ok=True,
            message=message,
            payload=payload,
            exit_code=ExitCode.OK,
            metadata=metadata,
        )
        return self._trace_outcome(
            operation=kind,
            outcome=outcome,
            selector=selector,
            spec=spec,
        )

    def _dispatch_batch_request(self, request: BatchRequest) -> OperationOutcome:
        """Return the :class:`OperationOutcome` for ``request``."""

        command = request.command

        if command == "definition":
            if request.selector is None:
                raise ValueError("Definition batch requests require a selector")
            return self.definition(request.selector)

        if command == "references":
            if request.selector is None:
                raise ValueError("References batch requests require a selector")
            return self.references(request.selector)

        if command == "hover":
            if request.selector is None:
                raise ValueError("Hover batch requests require a selector")
            return self.hover(request.selector)

        if command == "symbols":
            if request.selector is None:
                raise ValueError("Symbols batch requests require a selector")
            return self.symbols(request.selector)

        if command == "diagnostics":
            scope = request.scope or "document"
            if scope == "workspace":
                return self.diagnostics(scope="workspace", selector=None)
            if request.selector is None:
                raise ValueError("Document diagnostics batch requests require a selector")
            return self.diagnostics(scope="document", selector=request.selector)

        if command == "rename":
            if request.selector is None or request.new_name is None:
                raise ValueError("Rename batch requests require a selector and new name")
            return self.rename(
                selector=request.selector,
                new_name=request.new_name,
                apply=request.apply,
            )

        raise ValueError(f"Unsupported batch command: {command}")

    def batch(self, requests: Sequence[BatchRequest]) -> list[BatchResponse]:
        """Execute ``requests`` sequentially and return their responses."""

        responses: list[BatchResponse] = []
        for request in requests:
            outcome = self._dispatch_batch_request(request)
            responses.append(
                BatchResponse(
                    id=request.id,
                    ok=outcome.ok,
                    message=outcome.message,
                    exit_code=outcome.exit_code,
                    payload=outcome.payload,
                    metadata=outcome.metadata,
                )
            )
        return responses

    def definition(self, selector: str) -> OperationOutcome:
        """Resolve the definition for ``selector`` using Pyright when possible."""

        return self._selector_operation(
            kind="definition",
            selector=selector,
            result_builder=self._definition_result,
        )

    def references(self, selector: str) -> OperationOutcome:
        """Resolve references for ``selector`` using Pyright when possible."""

        return self._selector_operation(
            kind="references",
            selector=selector,
            result_builder=self._references_result,
        )

    def hover(self, selector: str) -> OperationOutcome:
        """Resolve hover information for ``selector`` using Pyright when possible."""

        return self._selector_operation(
            kind="hover",
            selector=selector,
            result_builder=self._hover_result,
        )

    def symbols(self, selector: str) -> OperationOutcome:
        """Resolve document symbols for ``selector`` using Pyright when possible."""

        return self._selector_operation(
            kind="symbols",
            selector=selector,
            result_builder=self._symbols_result,
        )

    def diagnostics(
        self,
        *,
        scope: Literal["document", "workspace"],
        selector: str | None,
    ) -> OperationOutcome:
        """Resolve diagnostics for ``selector`` or the workspace using Pyright when possible."""

        lock_guard = self._workspace_lock_guardrail(operation="diagnostics")
        if lock_guard is not None:
            return self._trace_outcome(
                operation="diagnostics",
                outcome=lock_guard,
                selector=selector,
            )

        self._pyright_result_source = None
        self._reset_pyright_progress()
        if scope == "workspace":
            self._ensure_pyright_session()
            if self._pyright_session is None:
                failure = self._pyright_failure_outcome(
                    kind="diagnostics",
                    selector_text="workspace",
                    spec=None,
                )
                return self._trace_outcome(
                    operation="diagnostics",
                    outcome=failure,
                    selector=selector,
                )

            result_payload = self._workspace_diagnostics_result()

            if self._pyright_error is not None:
                failure = self._pyright_failure_outcome(
                    kind="diagnostics",
                    selector_text="workspace",
                    spec=None,
                )
                return self._trace_outcome(
                    operation="diagnostics",
                    outcome=failure,
                    selector=selector,
                )

            if (
                self._pyright_result_source == "stub"
                and self._pyright_session is not None
                and self._pyright_error is None
            ):
                failure = self._stub_fallback_failure(
                    kind="diagnostics",
                    selector_text="workspace",
                    spec=None,
                )
                self._pyright_result_source = None
                return self._trace_outcome(
                    operation="diagnostics",
                    outcome=failure,
                    selector=selector,
                )

            explanation_notes = "Workspace diagnostics resolved via Pyright workspace/diagnostic."
            bundle = AnalysisBundle(
                kind="diagnostics",
                request={"scope": "workspace"},
                environment=self._environment_payload(),
                resolution={
                    "schemaVersion": "resolution.v1",
                    "status": "workspace-scan",
                    "selected": {"scope": "workspace"},
                    "candidates": [],
                    "explanation": {
                        "version": "rpos-v0",
                        "strategy": "workspace-scan",
                        "notes": explanation_notes,
                    },
                },
                result=result_payload,
            )

            metadata: dict[str, Any] = {}
            pyright_meta = self._pyright_metadata()
            if pyright_meta is not None:
                metadata["pyright"] = pyright_meta

            message = "Workspace diagnostics bundle generated via Pyright."

            self._pyright_result_source = None

            outcome = OperationOutcome(
                ok=True,
                message=message,
                payload=bundle.to_dict(),
                exit_code=ExitCode.OK,
                metadata=metadata or None,
            )
            return self._trace_outcome(operation="diagnostics", outcome=outcome, selector=selector)

        if selector is None:
            raise ValueError("Document diagnostics require a selector")

        return self._selector_operation(
            kind="diagnostics",
            selector=selector,
            result_builder=self._diagnostics_result,
        )

    def rename(self, selector: str, new_name: str, apply: bool) -> OperationOutcome:
        """Preview or apply a rename for ``selector``."""

        guardrail = self._dirty_guardrail(operation="rename")
        if guardrail is not None:
            return self._trace_outcome(operation="rename", outcome=guardrail, selector=selector)

        plan_holder: dict[str, _RenamePlan | None] = {"plan": None}

        def _build_result(
            selector: PositionSpec, *, context: _SelectorContext | None
        ) -> Mapping[str, Any]:
            payload, plan = self._rename_result(
                selector, new_name=new_name, apply=apply, context=context
            )
            plan_holder["plan"] = plan
            return payload

        outcome = self._selector_operation(
            kind="rename",
            selector=selector,
            result_builder=_build_result,
            cache=not apply,
        )

        payload_source = outcome.payload if outcome.payload is not None else _EMPTY_JSON_MAPPING
        payload: dict[str, Any] = dict(payload_source)
        rename_payload: dict[str, Any] | None = None
        result_block_raw = payload.get("result")
        result_block_mapping = _ensure_json_mapping(result_block_raw)
        if result_block_mapping is not None:
            result_block: dict[str, Any] = dict(result_block_mapping)
            payload["result"] = result_block
            rename_raw = result_block_mapping.get("rename")
            rename_mapping = _ensure_json_mapping(rename_raw)
            if rename_mapping is not None:
                rename_payload = dict(rename_mapping)
                result_block["rename"] = rename_payload
                if "applied" not in rename_payload:
                    rename_payload["applied"] = False

        if rename_payload is not None:
            status_value = rename_payload.get("applyStatus")
            if status_value == "prepare-denied":
                message_text = rename_payload.get("message")
                if not isinstance(message_text, str):
                    message_text = "Language server refused to rename the requested selector."
                denied = OperationOutcome(
                    ok=False,
                    message=message_text,
                    payload=payload,
                    exit_code=ExitCode.NOT_FOUND,
                    metadata=outcome.metadata,
                )
                return self._trace_outcome(operation="rename", outcome=denied, selector=selector)

        if not apply or not outcome.ok:
            return outcome

        plan = plan_holder.get("plan")

        if rename_payload is not None and "applyStatus" not in rename_payload:
            rename_payload["applyStatus"] = "planned"

        if rename_payload is not None and rename_payload.get("changeCount", 0) == 0:
            rename_payload["applyStatus"] = "no-changes"
            no_change = OperationOutcome(
                ok=True,
                message="Rename completed with no changes to apply.",
                payload=payload,
                exit_code=ExitCode.OK,
                metadata=outcome.metadata,
            )
            return self._trace_outcome(operation="rename", outcome=no_change, selector=selector)

        if plan is None:
            if rename_payload is not None:
                rename_payload["applyStatus"] = "no-plan"
            missing_plan = OperationOutcome(
                ok=False,
                message="Unable to compute rename apply plan for the requested selector.",
                payload=payload,
                exit_code=ExitCode.APPLY_CONFLICT,
                metadata=outcome.metadata,
            )
            return self._trace_outcome(operation="rename", outcome=missing_plan, selector=selector)

        try:
            changed_files = self._apply_rename_plan(plan)
        except _RenameApplyError as error:
            if rename_payload is not None:
                rename_payload["applied"] = False
                rename_payload["applyStatus"] = error.status
            metadata = self._merge_metadata(
                outcome.metadata,
                {"apply": {"status": error.status, "changedFiles": []}},
            )
            failure = OperationOutcome(
                ok=False,
                message=error.message,
                payload=payload,
                exit_code=ExitCode.APPLY_CONFLICT,
                metadata=metadata,
            )
            return self._trace_outcome(operation="rename", outcome=failure, selector=selector)

        if rename_payload is not None:
            rename_payload["applied"] = True
            rename_payload["applyStatus"] = "applied"

        metadata = self._merge_metadata(
            outcome.metadata,
            {
                "apply": {
                    "status": "applied",
                    "changedFiles": [str(path) for path in changed_files],
                }
            },
        )
        message = "Rename applied successfully." if changed_files else "Rename already up to date."
        final_outcome = OperationOutcome(
            ok=True,
            message=message,
            payload=payload,
            exit_code=ExitCode.OK,
            metadata=metadata,
        )
        return self._trace_outcome(operation="rename", outcome=final_outcome, selector=selector)

    def doctor(self) -> OperationOutcome:
        """Perform health checks against the orchestrator environment."""

        lock_guard = self._workspace_lock_guardrail(operation="doctor")
        if lock_guard is not None:
            return self._trace_outcome(operation="doctor", outcome=lock_guard)

        self._reset_pyright_progress()
        self._ensure_pyright_session()
        pyright_meta = self._pyright_metadata()
        cache_fingerprints = sorted(self._selector_cache_fingerprints.values())
        payload: dict[str, Any] = {
            "workspace": str(self._workspace_root),
            "positionEncoding": self._active_position_encoding(),
            "configuredPositionEncoding": self._settings.position_encoding,
            "frozenSnapshot": self._settings.frozen_snapshot,
            "allowDirty": self._settings.allow_dirty,
            "workspaceJail": True,
            "allowPaths": [str(path) for path in self._allow_paths],
            "denyPaths": [str(path) for path in self._deny_paths],
            "cache": {
                "selectorEntries": len(self._selector_cache),
                "fingerprints": cache_fingerprints,
            },
        }
        lock_payload = self._workspace_lock_payload()
        if lock_payload is not None:
            payload["workspaceLock"] = lock_payload
        if pyright_meta is not None:
            payload["pyright"] = pyright_meta
        outcome = OperationOutcome(
            ok=True,
            message="Environment report generated.",
            payload=payload,
        )
        return self._trace_outcome(operation="doctor", outcome=outcome)

    def _rollback_rename_edits(self, edits: Sequence[_AppliedRenameEdit]) -> list[str]:
        """Best-effort rollback for rename edits that were partially applied."""

        if not edits:
            return []

        rollback_errors: list[str] = []
        for edit in reversed(edits):
            path = self._resolve_path(edit.path)
            try:
                path.write_text(edit.original_source, encoding="utf-8")
            except OSError as error:
                rollback_errors.append(f"{path}: {error}")
                continue
            self._synchronise_document(path, edit.original_source)
            self._module_analysis_cache.pop(path, None)

        self._selector_cache.clear()
        self._selector_cache_fingerprints.clear()
        return rollback_errors

    def _apply_rename_plan(self, plan: _RenamePlan) -> list[Path]:
        """Write the updated source for ``plan`` to disk."""

        changed_paths: list[Path] = []
        applied_edits: list[_AppliedRenameEdit] = []

        try:
            for file_edit in plan.edits:
                resolved_path = self._resolve_path(file_edit.path)
                try:
                    current_source = resolved_path.read_text(encoding="utf-8")
                except (
                    OSError
                ) as error:  # pragma: no cover - surfaced via orchestrator rename tests
                    raise _RenameApplyError(
                        f"Failed to read file '{resolved_path}' before applying rename: {error}",
                        status="io-read",
                    ) from error

                if current_source != file_edit.original_source:
                    raise _RenameApplyError(
                        "File contents changed since analysis; refusing to apply rename.",
                        status="stale",
                    )

                if current_source == file_edit.updated_source:
                    continue

                try:
                    resolved_path.write_text(file_edit.updated_source, encoding="utf-8")
                except (
                    OSError
                ) as error:  # pragma: no cover - surfaced via orchestrator rename tests
                    raise _RenameApplyError(
                        f"Failed to write updated source for '{resolved_path}': {error}",
                        status="io-write",
                    ) from error

                self._synchronise_document(resolved_path, file_edit.updated_source)
                changed_paths.append(resolved_path)
                applied_edits.append(
                    _AppliedRenameEdit(path=resolved_path, original_source=current_source)
                )
                self._module_analysis_cache.pop(resolved_path, None)
        except _RenameApplyError as error:
            rollback_errors = self._rollback_rename_edits(applied_edits)
            if rollback_errors:
                failure_summary = "; ".join(rollback_errors)
                message = (
                    f"{error.message} Additionally, failed to roll back updates for: "
                    f"{failure_summary}. Manual intervention required."
                )
                raise _RenameApplyError(message, status="rollback-failed") from error
            raise

        if changed_paths:
            self._selector_cache.clear()
            self._selector_cache_fingerprints.clear()

        return changed_paths

    @staticmethod
    def _merge_metadata(
        base: Mapping[str, Any] | None, extra: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        """Return ``base`` merged with ``extra`` (shallow merge)."""

        if not base and not extra:
            return None

        merged: dict[str, Any] = dict(base or {})
        if extra:
            for key, value in extra.items():
                if (
                    key in merged
                    and isinstance(merged[key], Mapping)
                    and isinstance(value, Mapping)
                ):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
        return merged
