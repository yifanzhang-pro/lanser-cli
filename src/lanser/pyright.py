"""Pyright language server session management."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import types
from collections import abc, deque
from typing import IO, TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, computed_field

from ._version import __version__

if TYPE_CHECKING:
    from pathlib import Path

    from .trace import TraceRecorderProtocol
else:
    TraceRecorderProtocol = Any

__all__ = [
    "PyrightHandshake",
    "PyrightSession",
    "PyrightSessionError",
    "PyrightSessionTimeout",
    "create_pyright_session",
]


def _default_pyright_command() -> tuple[str, ...]:
    """Return the default command used to launch Pyright."""

    executable = sys.executable or "python"
    return (executable, "-m", "pyright.langserver", "--stdio")


def _default_client_capabilities() -> dict[str, Any]:
    """Return the baseline LSP client capabilities advertised to Pyright."""

    return {
        "general": {"positionEncodings": ["utf-16", "utf-8"]},
        "textDocument": {
            "publishDiagnostics": {"relatedInformation": True},
            "synchronization": {
                "dynamicRegistration": False,
                "willSave": False,
                "willSaveWaitUntil": False,
                "didSave": False,
            },
            "hover": {"contentFormat": ["markdown", "plaintext"]},
            "definition": {"dynamicRegistration": False},
            "references": {"dynamicRegistration": False},
            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
            "rename": {"prepareSupport": True},
            "diagnostic": {
                "dynamicRegistration": False,
                "relatedDocumentSupport": False,
            },
        },
        "workspace": {
            "configuration": True,
            "diagnostic": {"refreshSupport": False},
            "workspaceFolders": {"supported": True},
        },
    }


def _ensure_json_mapping(value: object) -> abc.Mapping[str, Any] | None:
    """Return ``value`` when it is a mapping with string keys."""

    if not isinstance(value, abc.Mapping):
        return None
    for key in cast("tuple[object, ...]", tuple(value.keys())):
        if not isinstance(key, str):
            return None
    return cast("abc.Mapping[str, Any]", value)


class PyrightSessionError(RuntimeError):
    """Raised when the Pyright session cannot be established."""


class PyrightSessionTimeout(PyrightSessionError):
    """Raised when the language server does not respond in time."""


class PyrightHandshake(BaseModel):
    """Capture metadata returned from the Pyright ``initialize`` response."""

    result: abc.Mapping[str, Any]
    model_config = ConfigDict(frozen=True)

    @computed_field
    @property
    def capabilities_digest(self) -> str:
        """Return a deterministic digest of the negotiated capabilities."""

        capabilities = self.result.get("capabilities", {})
        if isinstance(capabilities, abc.Mapping):
            canonical = json.dumps(
                dict(capabilities),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            canonical = b"{}"
        digest = hashlib.sha256(canonical).hexdigest()
        return f"sha256:{digest}"

    @computed_field
    @property
    def server_info(self) -> abc.Mapping[str, Any] | None:
        """Return details about the language server, when provided."""

        server_info_raw = self.result.get("serverInfo")
        return dict(server_info_raw) if isinstance(server_info_raw, abc.Mapping) else None

    @computed_field
    @property
    def position_encoding(self) -> str | None:
        """Return the negotiated position encoding if present."""

        capabilities = self.result.get("capabilities", {})
        if isinstance(capabilities, abc.Mapping):
            capabilities_mapping = cast("abc.Mapping[str, Any]", capabilities)
            raw_value = capabilities_mapping.get("positionEncoding")
            if isinstance(raw_value, str):
                return raw_value
        return None

    def to_metadata(self) -> abc.Mapping[str, Any]:
        """Return a JSON-serialisable summary of the handshake."""

        payload: dict[str, Any] = {
            "capabilitiesDigest": self.capabilities_digest,
        }
        if self.server_info is not None:
            payload["serverInfo"] = self.server_info
        if self.position_encoding is not None:
            payload["positionEncoding"] = self.position_encoding
        return payload


class _JsonRpcStreamReader:
    """Parse JSON-RPC 2.0 messages from a byte stream."""

    def __init__(self, stream: IO[bytes], *, recorder: TraceRecorderProtocol | None = None) -> None:
        self._stream = stream
        self._recorder = recorder

    def read_message(self) -> abc.MutableMapping[str, Any] | None:
        """Return the next JSON-RPC message from the stream."""

        headers: dict[str, str] = {}
        while True:
            line = self._stream.readline()
            if line in (b"", None):
                return None
            if line in (b"\r\n", b"\n"):
                break
            try:
                decoded = line.decode("ascii")
            except UnicodeDecodeError as exc:  # pragma: no cover - defensive guard
                raise PyrightSessionError("Failed to decode JSON-RPC header") from exc
            if not decoded.strip():
                break
            if ":" not in decoded:
                continue
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length_str = headers.get("content-length")
        if length_str is None:
            raise PyrightSessionError("JSON-RPC message missing Content-Length header")

        try:
            length = int(length_str)
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise PyrightSessionError("Invalid Content-Length header") from exc

        try:
            payload = self._stream.read(length)
        except ValueError:  # pragma: no cover - stream torn down concurrently
            return None

        if payload is None or len(payload) < length:
            raise PyrightSessionError("Incomplete JSON-RPC payload received")

        try:
            message = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive guard
            raise PyrightSessionError("Failed to decode JSON-RPC payload") from exc

        if self._recorder is not None:
            self._recorder.record_jsonrpc("server", message)

        return message


class _JsonRpcStreamWriter:
    """Serialise JSON-RPC messages to a byte stream."""

    def __init__(self, stream: IO[bytes], *, recorder: TraceRecorderProtocol | None = None) -> None:
        self._stream = stream
        self._write_lock = threading.Lock()
        self._recorder = recorder

    def write_message(self, message: abc.Mapping[str, Any]) -> None:
        """Serialise ``message`` to the stream."""

        body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            self._stream.write(header)
        self._stream.write(body)
        self._stream.flush()
        if self._recorder is not None:
            self._recorder.record_jsonrpc("client", message)


class PyrightSession:
    """Manage a Pyright language server process over JSON-RPC."""

    def __init__(
        self,
        workspace: Path,
        *,
        command: abc.Sequence[str] | None = None,
        initialization_options: abc.Mapping[str, Any] | None = None,
        recorder: TraceRecorderProtocol | None = None,
    ) -> None:
        self._workspace = workspace
        if command is None:
            command_tuple = _default_pyright_command()
        else:
            command_tuple = tuple(command)
            if not all(isinstance(entry, str) for entry in command_tuple):
                raise TypeError("Pyright command entries must be strings")

        self._command: tuple[str, ...] = command_tuple
        self._initialization_options = dict(initialization_options or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: IO[bytes] | None = None
        self._stdout: IO[bytes] | None = None
        self._stderr: IO[bytes] | None = None
        self._reader: _JsonRpcStreamReader | None = None
        self._writer: _JsonRpcStreamWriter | None = None
        self._incoming: queue.Queue[abc.MutableMapping[str, Any]] = queue.Queue()
        self._responses: dict[int, abc.MutableMapping[str, Any]] = {}
        self._notifications: deque[abc.MutableMapping[str, Any]] = deque()
        self._notification_lock = threading.Lock()
        self._notification_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._reader_closed = threading.Event()
        self._next_id = 1
        self._handshake: PyrightHandshake | None = None
        self._closed = False
        self._workspace_config_cache: abc.Mapping[str, Any] | None = None
        self._workspace_config_mtime: float | None = None
        self._workspace_config_sent_digest: str | None = None
        self._pending_requests: dict[int, bool] = {}
        self._pending_lock = threading.Lock()
        self._recorder = recorder

    @property
    def handshake(self) -> PyrightHandshake | None:
        """Return the cached handshake metadata, if available."""

        return self._handshake

    def __enter__(self) -> PyrightSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        self.shutdown()

    def start(self) -> None:
        """Spawn the Pyright language server if it is not already running."""

        if self._process is not None:
            return

        if not self._command:
            raise PyrightSessionError("Pyright command is empty")

        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._workspace),
            )
        except OSError as exc:
            raise PyrightSessionError(
                f"Failed to start Pyright language server: {exc.strerror or exc}"
            ) from exc

        if process.stdin is None or process.stdout is None:
            process.kill()
            raise PyrightSessionError("Pyright process did not expose stdio pipes")

        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = process.stderr
        self._reader = _JsonRpcStreamReader(self._stdout, recorder=self._recorder)
        self._writer = _JsonRpcStreamWriter(self._stdin, recorder=self._recorder)
        self._closed = False

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="pyright-jsonrpc-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                message = self._reader.read_message()
                if message is None:
                    break
                self._incoming.put(message)
        except PyrightSessionError:
            # The queue consumer will surface the error via timeout/closed handling.
            pass
        finally:
            self._reader_closed.set()

    def _respond_to_server_request(
        self,
        *,
        request_id: int,
        method: str,
        params: object | None,
    ) -> bool:
        """Return ``True`` if a server-initiated request was handled."""

        if self._writer is None:
            return False

        if method == "window/workDoneProgress/create":
            self._writer.write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
            return True

        if method == "workspace/configuration":
            self._writer.write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": self._build_configuration_items(params),
                }
            )
            return True

        # Default acknowledgement for requests where no specific handling is required.
        self._writer.write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
        return True

    def _load_workspace_configuration(self) -> abc.Mapping[str, Any] | None:
        """Return parsed ``pyrightconfig.json`` content when available."""

        config_path = self._workspace / "pyrightconfig.json"
        try:
            stat_result = config_path.stat()
        except FileNotFoundError:
            self._workspace_config_cache = None
            self._workspace_config_mtime = None
            return None
        except OSError:
            return None

        mtime = stat_result.st_mtime
        if (
            self._workspace_config_cache is not None
            and self._workspace_config_mtime is not None
            and self._workspace_config_mtime == mtime
        ):
            return self._workspace_config_cache

        try:
            raw_text = config_path.read_text(encoding="utf-8")
        except OSError:
            return None

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            self._workspace_config_cache = None
            self._workspace_config_mtime = mtime
            return None

        if not isinstance(parsed, abc.Mapping):
            self._workspace_config_cache = None
            self._workspace_config_mtime = mtime
            return None

        self._workspace_config_cache = cast("abc.Mapping[str, Any]", parsed)
        self._workspace_config_mtime = mtime
        return self._workspace_config_cache

    @staticmethod
    def _config_section_value(config: abc.Mapping[str, Any], section: str | None) -> object | None:
        """Return the configuration value matching ``section``."""

        if section is None or not section.strip():
            return dict(config)

        normalised = section.strip()
        if normalised in {"pyright", "python"}:
            return dict(config)

        cursor: object = config
        for part in normalised.split("."):
            if not part:
                return None
            if isinstance(cursor, abc.Mapping):
                mapping_cursor = cast("abc.Mapping[str, Any]", cursor)
                cursor = mapping_cursor.get(part)
            else:
                return None

        if isinstance(cursor, abc.Mapping):
            return dict(cursor)
        if isinstance(cursor, list | tuple):
            sequence_cursor = cast("abc.Sequence[Any]", cursor)
            return list(sequence_cursor)
        return cursor

    def _build_configuration_items(self, params: object | None) -> list[object | None]:
        """Return configuration payloads for ``workspace/configuration`` requests."""

        config = self._load_workspace_configuration()
        results: list[object | None] = []

        if not isinstance(params, abc.Mapping):
            return results

        mapping_params = cast("abc.Mapping[str, object]", params)
        raw_items = mapping_params.get("items")
        if not isinstance(raw_items, abc.Sequence):
            return results

        items = cast("abc.Sequence[object]", raw_items)
        for item in items:
            section: str | None = None
            mapping_item = _ensure_json_mapping(item)
            if mapping_item is not None:
                raw_section = mapping_item.get("section")
                section = raw_section if isinstance(raw_section, str) else None
            if config is None:
                results.append(None)
            else:
                results.append(self._config_section_value(config, section))
        return results

    def request(
        self,
        method: str,
        params: abc.Mapping[str, Any] | abc.Sequence[Any] | None,
        *,
        timeout: float = 10.0,
        cancellable: bool = False,
    ) -> abc.Mapping[str, Any] | None:
        """Send a JSON-RPC request to the server and wait for its response."""

        if self._process is None or self._writer is None:
            raise PyrightSessionError("Pyright session has not been started")

        request_id = self._next_id
        self._next_id += 1

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        self._writer.write_message(message)

        with self._pending_lock:
            self._pending_requests[request_id] = cancellable

        try:
            return self._await_response(request_id=request_id, timeout=timeout)
        except PyrightSessionTimeout:
            if cancellable:
                self.cancel_request(request_id)
            raise
        finally:
            with self._pending_lock:
                self._pending_requests.pop(request_id, None)

    def notify(
        self,
        method: str,
        params: abc.Mapping[str, Any] | abc.Sequence[Any] | None,
    ) -> None:
        """Send a JSON-RPC notification to the language server."""

        if self._writer is None:
            raise PyrightSessionError("Pyright session has not been started")

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._writer.write_message(message)

    def cancel_request(self, request_id: int) -> None:
        """Request cancellation for the in-flight request ``request_id``."""

        writer = self._writer
        if writer is None:
            return

        with self._pending_lock:
            cancellable = self._pending_requests.get(request_id, False)

        if not cancellable:
            return

        try:
            writer.write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": request_id},
                }
            )
        except PyrightSessionError:
            # Cancellation is best-effort; ignore transport errors.
            pass

    def drain_notifications(
        self, *, method: str | None = None
    ) -> list[abc.MutableMapping[str, Any]]:
        """Return queued notifications optionally filtered by ``method``."""

        matches: list[abc.MutableMapping[str, Any]] = []
        remainder: deque[abc.MutableMapping[str, Any]] = deque()

        with self._notification_lock:
            while self._notifications:
                message = self._notifications.popleft()
                message_method = message.get("method")
                if method is None or message_method == method:
                    matches.append(message)
                else:
                    remainder.append(message)

            self._notifications = remainder
            if self._notifications:
                self._notification_event.set()
            else:
                self._notification_event.clear()

        return matches

    def wait_for_notifications(
        self, *, method: str, timeout: float = 5.0
    ) -> list[abc.MutableMapping[str, Any]]:
        """Return notifications for ``method`` within ``timeout`` seconds."""

        deadline = time.monotonic() + timeout

        while True:
            matches = self.drain_notifications(method=method)
            if matches:
                return matches

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []

            self._notification_event.wait(timeout=remaining)

    def _await_response(self, *, request_id: int, timeout: float) -> abc.Mapping[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            cached = self._responses.pop(request_id, None)
            if cached is not None:
                if "error" in cached:
                    raise PyrightSessionError(json.dumps(cached["error"]))
                return cached.get("result")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PyrightSessionTimeout(f"Timed out waiting for response to {request_id}")

            try:
                message = self._incoming.get(timeout=remaining)
            except queue.Empty:
                if self._reader_closed.is_set():
                    raise PyrightSessionError("Pyright server closed the connection")
                continue

            message_id = message.get("id")
            if isinstance(message_id, int):
                method = message.get("method")
                if isinstance(method, str) and self._respond_to_server_request(
                    request_id=message_id,
                    method=method,
                    params=message.get("params"),
                ):
                    continue

                self._responses[message_id] = message
                continue

            with self._notification_lock:
                self._notifications.append(message)
                self._notification_event.set()

    def initialize(
        self,
        *,
        client_info: abc.Mapping[str, Any] | None = None,
        initialization_options: abc.Mapping[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> PyrightHandshake:
        """Perform the LSP ``initialize`` handshake."""

        if self._process is None:
            self.start()

        workspace_path = self._workspace
        try:
            resolved_workspace = workspace_path.resolve()
        except OSError:
            resolved_workspace = workspace_path
        workspace_uri = resolved_workspace.as_uri()

        params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootPath": str(resolved_workspace),
            "rootUri": workspace_uri,
            "capabilities": _default_client_capabilities(),
            "clientInfo": dict(client_info or {"name": "lanser", "version": __version__}),
            "workspaceFolders": [
                {
                    "name": resolved_workspace.name or str(resolved_workspace),
                    "uri": workspace_uri,
                }
            ],
        }

        initial_options = dict(self._initialization_options)
        if initialization_options is not None:
            initial_options.update(initialization_options)
        if initial_options:
            params["initializationOptions"] = initial_options

        result = self.request("initialize", params, timeout=timeout)
        handshake = PyrightHandshake(result=result or {})
        self._handshake = handshake
        self.notify("initialized", {"workspaceFolders": params["workspaceFolders"]})
        self._send_initial_configuration()
        return handshake

    def shutdown(self) -> None:
        """Attempt a graceful shutdown of the Pyright process."""

        if self._closed:
            return

        try:
            self.request("shutdown", None, timeout=5.0)
        except PyrightSessionError:
            pass
        else:
            try:
                self.notify("exit", None)
            except PyrightSessionError:
                pass

        self._closed = True
        self._teardown_process()

    def close(self) -> None:
        """Alias for :meth:`shutdown` for compatibility."""

        self.shutdown()

    def _teardown_process(self) -> None:
        process = self._process
        if process is None:
            return

        if self._stdin is not None:
            try:
                self._stdin.close()
            except OSError:
                pass
        if self._stdout is not None:
            try:
                self._stdout.close()
            except OSError:
                pass
        if self._stderr is not None:
            try:
                self._stderr.close()
            except OSError:
                pass

        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive guard
                pass

        self._process = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None
        with self._pending_lock:
            self._pending_requests.clear()

    def _configuration_settings_payload(
        self, config: abc.Mapping[str, Any] | None
    ) -> tuple[abc.Mapping[str, Any], str]:
        """Return configuration settings payload and digest for ``config``."""

        if config is None:
            settings_payload: abc.Mapping[str, Any] = {}
        else:
            config_mapping = copy.deepcopy(dict(config))
            settings_payload = {
                "python": copy.deepcopy(config_mapping),
                "pyright": config_mapping,
            }

        canonical = json.dumps(
            settings_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return settings_payload, f"sha256:{digest}"

    def refresh_configuration(self) -> None:
        """Notify Pyright when the workspace configuration changes."""

        config = self._load_workspace_configuration()
        settings_payload, digest = self._configuration_settings_payload(config)

        if self._workspace_config_sent_digest == digest:
            return

        try:
            self.notify("workspace/didChangeConfiguration", {"settings": settings_payload})
        except PyrightSessionError:
            return

        self._workspace_config_sent_digest = digest

    def _send_initial_configuration(self) -> None:
        """Notify Pyright about the current workspace configuration."""

        self.refresh_configuration()


def create_pyright_session(
    workspace: Path, *, recorder: TraceRecorderProtocol | None = None
) -> PyrightSession:
    """Return a ready-to-use Pyright session for ``workspace``."""

    session = PyrightSession(workspace, recorder=recorder)
    session.start()
    session.initialize()
    return session
