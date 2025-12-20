"""Selector parsing and normalisation utilities."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote_plus

from pydantic import BaseModel, ConfigDict

__all__ = [
    "SelectorParseError",
    "CursorSelector",
    "RangeSelector",
    "SymbolSelector",
    "AnchorSelector",
    "AstPathSelector",
    "PositionSpec",
    "parse_selector",
]


class SelectorParseError(ValueError):
    """Raised when a selector string cannot be parsed."""

    def __init__(self, selector: str, message: str) -> None:
        super().__init__(message)
        self.selector = selector
        self.message = message

    def to_dict(self) -> dict[str, str]:
        """Serialise the error for diagnostics."""

        return {"selector": self.selector, "message": self.message}


class CursorSelector(BaseModel):
    """Represents a cursor position within a document."""

    kind: Literal["cursor"] = "cursor"
    uri: str
    line: int
    column: int
    indexing: str
    doc_version: str | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> dict[str, object]:
        """Convert the selector to a serialisable payload."""

        payload: dict[str, object] = {
            "kind": self.kind,
            "uri": self.uri,
            "line": self.line,
            "col": self.column,
            "indexing": self.indexing,
        }
        if self.doc_version is not None:
            payload["docVersion"] = self.doc_version
        return payload


class RangeSelector(BaseModel):
    """Represents a range inside a document."""

    kind: Literal["range"] = "range"
    uri: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    indexing: str
    doc_version: str | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> dict[str, object]:
        """Convert the selector to a serialisable payload."""

        payload: dict[str, object] = {
            "kind": self.kind,
            "uri": self.uri,
            "start": [self.start_line, self.start_column],
            "end": [self.end_line, self.end_column],
            "indexing": self.indexing,
        }
        if self.doc_version is not None:
            payload["docVersion"] = self.doc_version
        return payload


class SymbolSelector(BaseModel):
    """Represents a symbolic reference to a Python object."""

    kind: Literal["symbol"] = "symbol"
    module: str
    symbol: str
    role: Literal["def", "sig", "body", "doc"] | None = None
    overload: int | None = None
    doc_version: str | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> dict[str, object]:
        """Convert the selector to a serialisable payload."""

        payload: dict[str, object] = {
            "kind": self.kind,
            "qualname": f"{self.module}:{self.symbol}",
        }
        if self.role is not None:
            payload["role"] = self.role
        if self.overload is not None:
            payload["overload"] = self.overload
        if self.doc_version is not None:
            payload["docVersion"] = self.doc_version
        return payload


class AnchorSelector(BaseModel):
    """Represents a content anchor selector."""

    kind: Literal["anchor"] = "anchor"
    uri: str
    snippet: str
    context: int = 0
    hash: str | None = None
    doc_version: str | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> dict[str, object]:
        """Convert the selector to a serialisable payload."""

        payload: dict[str, object] = {
            "kind": self.kind,
            "uri": self.uri,
            "snippet": self.snippet,
            "context": self.context,
        }
        if self.hash is not None:
            payload["hash"] = self.hash
        if self.doc_version is not None:
            payload["docVersion"] = self.doc_version
        return payload


class AstPathSegment(BaseModel):
    """Represents a single segment in an AST path selector."""

    axis: str
    value: str | None = None
    index: int | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> list[object]:
        """Serialise the segment into a list payload."""

        payload: list[object] = [self.axis]
        if self.value is not None:
            payload.append(self.value)
        if self.index is not None:
            payload.append(self.index)
        return payload


class AstPathSelector(BaseModel):
    """Represents an AST path selector."""

    kind: Literal["ast"] = "ast"
    path: tuple[AstPathSegment, ...]
    doc_version: str | None = None

    model_config = ConfigDict(frozen=True)

    def to_payload(self) -> dict[str, object]:
        """Convert the selector to a serialisable payload."""

        payload: dict[str, object] = {
            "kind": self.kind,
            "path": [segment.to_payload() for segment in self.path],
        }
        if self.doc_version is not None:
            payload["docVersion"] = self.doc_version
        return payload


PositionSpec = CursorSelector | RangeSelector | SymbolSelector | AnchorSelector | AstPathSelector


_PATH_PATTERN = re.compile(r"^(?P<path>[^@]+)@(?P<body>.+)$")
_CURSOR_PATTERN = re.compile(r"^L(?P<line>\d+):C(?P<col>\d+)$")
_RANGE_PATTERN = re.compile(
    r"^R\((?P<start_line>\d+),(?P<start_col>\d+)->(?P<end_line>\d+),(?P<end_col>\d+)\)$"
)
_SYMBOL_PREFIX = "py://"
_ANCHOR_PREFIX = "anchor://"
_AST_PREFIX = "ast://"
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_MODULE_PATTERN = re.compile(rf"^{_IDENTIFIER}(\.{_IDENTIFIER})*$")
_QUALNAME_PATTERN = re.compile(rf"^{_IDENTIFIER}(\.{_IDENTIFIER}|:{_IDENTIFIER})*$")
_SYMBOL_ROLES = {"def", "sig", "body", "doc"}
_AST_AXIS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_AST_SEGMENT_PATTERN = re.compile(
    r"^"
    r"(?:(?:\[(?P<bracket_axis>[A-Za-z_][A-Za-z0-9_-]*)=(?P<bracket_value>[^\]]+)\])"
    r"|(?P<axis>[A-Za-z_][A-Za-z0-9_-]*)(?:=(?P<value>[^\[]+))?)"
    r"(?:\[(?P<index>\d+)\])?"
    r"$"
)


def _normalise_path(raw_path: str, workspace: Path) -> str:
    """Return a canonical ``file://`` URI for ``raw_path``."""

    if raw_path.startswith("file://"):
        return raw_path

    path = Path(raw_path)
    if not path.is_absolute():
        path = (workspace / path).resolve()
    return path.as_uri()


def _parse_symbol_selector(selector: str) -> SymbolSelector:
    """Parse a symbolic selector of the form ``py://module#qualname[:role]``."""

    raw = selector[len(_SYMBOL_PREFIX) :]
    module_part, hash_sep, remainder = raw.partition("#")
    if not hash_sep or not module_part:
        raise SelectorParseError(
            selector, "Symbol selector must include module path and '#' separator"
        )
    if not _MODULE_PATTERN.fullmatch(module_part):
        raise SelectorParseError(selector, "Invalid module path for symbol selector")

    path_part, _, query_part = remainder.partition("?")
    if not path_part:
        raise SelectorParseError(selector, "Symbol selector missing qualified name")

    role: Literal["def", "sig", "body", "doc"] | None = None
    symbol_part = path_part
    if ":" in path_part:
        symbol_part, role_part = path_part.rsplit(":", 1)
        if not role_part:
            raise SelectorParseError(selector, "Symbol selector role cannot be empty")
        if role_part not in _SYMBOL_ROLES:
            raise SelectorParseError(selector, "Unsupported symbol selector role")
        role = cast("Literal['def', 'sig', 'body', 'doc']", role_part)

    if not _QUALNAME_PATTERN.fullmatch(symbol_part):
        raise SelectorParseError(selector, "Invalid qualified name for symbol selector")

    overload: int | None = None
    doc_version: str | None = None
    if query_part:
        for chunk in query_part.split("&"):
            if not chunk:
                continue
            key, eq, value = chunk.partition("=")
            if key == "overload":
                if not eq:
                    raise SelectorParseError(selector, "Symbol selector overload must have a value")
                try:
                    overload = int(value)
                except ValueError as exc:
                    raise SelectorParseError(
                        selector, "Symbol selector overload must be an integer"
                    ) from exc
                if overload < 0:
                    raise SelectorParseError(
                        selector, "Symbol selector overload must be non-negative"
                    )
            elif key in {"doc", "docVersion"}:
                if not eq:
                    raise SelectorParseError(
                        selector, "Symbol selector docVersion must have a value"
                    )
                doc_version = value
            else:
                raise SelectorParseError(
                    selector, f"Unsupported symbol selector query parameter '{key}'"
                )

    return SymbolSelector(
        kind="symbol",
        module=module_part,
        symbol=symbol_part,
        role=role,
        overload=overload,
        doc_version=doc_version,
    )


def _parse_anchor_selector(selector: str, workspace: Path) -> AnchorSelector:
    """Parse a content anchor selector of the form ``anchor://path#snippet``."""

    raw = selector[len(_ANCHOR_PREFIX) :]
    path_part, hash_sep, remainder = raw.partition("#")
    if not hash_sep or not path_part:
        raise SelectorParseError(
            selector, "Anchor selector must include a file path and '#' snippet separator"
        )

    uri = _normalise_path(path_part, workspace=workspace)
    snippet_part, _, query_part = remainder.partition("?")
    if not snippet_part:
        raise SelectorParseError(selector, "Anchor selector requires a snippet segment")

    snippet = snippet_part
    if snippet.startswith('"') and snippet.endswith('"') and len(snippet) >= 2:
        snippet = snippet[1:-1]
    snippet = unquote_plus(snippet)
    if not snippet:
        raise SelectorParseError(selector, "Anchor selector snippet cannot be empty")

    context = 0
    anchor_hash: str | None = None
    doc_version: str | None = None
    if query_part:
        for chunk in query_part.split("&"):
            if not chunk:
                continue
            key, eq, value = chunk.partition("=")
            if not eq:
                raise SelectorParseError(selector, "Anchor selector query parameters must use '='")
            if key == "ctx":
                try:
                    context = int(value)
                except ValueError as exc:
                    raise SelectorParseError(
                        selector, "Anchor selector context must be an integer"
                    ) from exc
                if context < 0:
                    raise SelectorParseError(
                        selector, "Anchor selector context must be non-negative"
                    )
            elif key == "hash":
                if not value:
                    raise SelectorParseError(selector, "Anchor selector hash must have a value")
                anchor_hash = value
            elif key in {"doc", "docVersion"}:
                if not value:
                    raise SelectorParseError(
                        selector, "Anchor selector docVersion must have a value"
                    )
                doc_version = value
            else:
                raise SelectorParseError(
                    selector, f"Unsupported anchor selector query parameter '{key}'"
                )

    return AnchorSelector(
        kind="anchor",
        uri=uri,
        snippet=snippet,
        context=context,
        hash=anchor_hash,
        doc_version=doc_version,
    )


def _parse_ast_selector(selector: str) -> AstPathSelector:
    """Parse an AST path selector of the form ``ast://…``."""

    raw = selector[len(_AST_PREFIX) :]
    path_part, _, query_part = raw.partition("?")
    parts = [segment for segment in path_part.split("/") if segment]
    if not parts:
        raise SelectorParseError(selector, "AST selector must contain at least one segment")

    doc_version: str | None = None
    if query_part:
        for chunk in query_part.split("&"):
            if not chunk:
                continue
            key, eq, value = chunk.partition("=")
            if key in {"doc", "docVersion"}:
                if not eq:
                    raise SelectorParseError(selector, "AST selector docVersion must have a value")
                doc_version = value
            else:
                raise SelectorParseError(
                    selector, f"Unsupported AST selector query parameter '{key}'"
                )

    segments: list[AstPathSegment] = []
    for part in parts:
        match = _AST_SEGMENT_PATTERN.fullmatch(part)
        if not match:
            raise SelectorParseError(selector, f"Invalid AST selector segment '{part}'")

        axis = match.group("bracket_axis") or match.group("axis")
        if axis is None or not _AST_AXIS_PATTERN.fullmatch(axis):
            raise SelectorParseError(selector, f"Invalid AST selector axis '{axis}'")

        raw_value = match.group("bracket_value") or match.group("value")
        value = unquote_plus(raw_value) if raw_value is not None else None

        index_str = match.group("index")
        index: int | None = None
        if index_str is not None:
            try:
                index = int(index_str)
            except ValueError as exc:
                raise SelectorParseError(selector, "AST selector index must be an integer") from exc
            if index < 0:
                raise SelectorParseError(selector, "AST selector index must be non-negative")

        segments.append(AstPathSegment(axis=axis, value=value, index=index))

    return AstPathSelector(kind="ast", path=tuple(segments), doc_version=doc_version)


def parse_selector(selector: str, workspace: Path, indexing: str) -> PositionSpec:
    """Parse ``selector`` into a strongly typed ``PositionSpec``."""

    if selector.startswith(_SYMBOL_PREFIX):
        return _parse_symbol_selector(selector)
    if selector.startswith(_ANCHOR_PREFIX):
        return _parse_anchor_selector(selector, workspace=workspace)
    if selector.startswith(_AST_PREFIX):
        return _parse_ast_selector(selector)

    match = _PATH_PATTERN.match(selector)
    if not match:
        raise SelectorParseError(selector, "Selector must include '@' separating path and location")

    body = match.group("body")
    uri = _normalise_path(match.group("path"), workspace=workspace)

    location_body, _, query_part = body.partition("?")
    doc_version: str | None = None
    if query_part:
        for chunk in query_part.split("&"):
            if not chunk:
                continue
            key, eq, value = chunk.partition("=")
            if key in {"doc", "docVersion"}:
                if not eq:
                    raise SelectorParseError(
                        selector,
                        "Selector docVersion must have a value",
                    )
                doc_version = value
            else:
                raise SelectorParseError(selector, f"Unsupported selector query parameter '{key}'")

    cursor = _CURSOR_PATTERN.match(location_body)
    if cursor:
        line = int(cursor.group("line"))
        column = int(cursor.group("col"))
        return CursorSelector(
            kind="cursor",
            uri=uri,
            line=line,
            column=column,
            indexing=indexing,
            doc_version=doc_version,
        )

    range_match = _RANGE_PATTERN.match(location_body)
    if range_match:
        return RangeSelector(
            kind="range",
            uri=uri,
            start_line=int(range_match.group("start_line")),
            start_column=int(range_match.group("start_col")),
            end_line=int(range_match.group("end_line")),
            end_column=int(range_match.group("end_col")),
            indexing=indexing,
            doc_version=doc_version,
        )

    raise SelectorParseError(
        selector, "Unsupported selector body. Expected 'L#:#' or 'R(...)' patterns"
    )
