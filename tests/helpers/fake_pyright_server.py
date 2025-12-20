"""Minimal JSON-RPC server emulating Pyright responses for tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any


def _read_message() -> Mapping[str, Any] | None:
    headers: dict[str, str] = {}
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if line in (b"", None):
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii").partition(":")
        headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    payload = stdin.read(length)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_message(message: Mapping[str, Any]) -> None:
    stdout = sys.stdout.buffer
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stdout.write(body)
    stdout.flush()


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            break

        if "method" not in message:
            continue

        method = message["method"]
        if method == "initialize":
            params = message.get("params")
            capabilities: Mapping[str, Any] | None = None
            if isinstance(params, Mapping):
                raw_capabilities = params.get("capabilities")
                if isinstance(raw_capabilities, Mapping):
                    capabilities = raw_capabilities

            error_response: Mapping[str, Any] | None = None
            if capabilities is None:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {
                        "code": -32602,
                        "message": "missing capabilities",
                    },
                }
            else:
                text_document_caps = capabilities.get("textDocument")
                workspace_caps = capabilities.get("workspace")
                text_diag = (
                    text_document_caps.get("diagnostic")
                    if isinstance(text_document_caps, Mapping)
                    else None
                )
                workspace_diag = (
                    workspace_caps.get("diagnostic")
                    if isinstance(workspace_caps, Mapping)
                    else None
                )
                if not (
                    isinstance(text_diag, Mapping) and text_diag.get("dynamicRegistration") is False
                ):
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {
                            "code": -32602,
                            "message": "textDocument.diagnostic not advertised",
                        },
                    }
                elif not (
                    isinstance(workspace_diag, Mapping)
                    and workspace_diag.get("refreshSupport") is False
                ):
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {
                            "code": -32602,
                            "message": "workspace.diagnostic not advertised",
                        },
                    }

            if error_response is not None:
                _write_message(error_response)
                continue

            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "capabilities": {
                        "positionEncoding": "utf-16",
                        "textDocumentSync": 1,
                    },
                    "serverInfo": {
                        "name": "fake-pyright",
                        "version": "0.0-test",
                    },
                },
            }
            _write_message(response)
            continue

        if method == "shutdown":
            _write_message({"jsonrpc": "2.0", "id": message.get("id"), "result": None})
            continue

        if method == "exit":
            break

        if "id" in message:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"echo": method},
                }
            )


if __name__ == "__main__":
    main()
