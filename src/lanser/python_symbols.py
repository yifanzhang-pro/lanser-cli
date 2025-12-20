"""Lightweight Python source analysis helpers for stub bundles."""

from __future__ import annotations

import ast
import bisect
import pathlib
import re
from collections import abc
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PythonDiagnostic",
    "PythonModuleAnalysis",
    "SymbolData",
    "SymbolSpan",
    "analyse_python_module",
]


class SymbolSpan(BaseModel):
    """Represents a half-open range inside a source file."""

    start_line: int
    start_col: int
    end_line: int
    end_col: int

    model_config = ConfigDict(frozen=True)

    def contains(self, line: int, column: int) -> bool:
        """Return ``True`` if ``line``/``column`` falls within the span."""

        start = (self.start_line, self.start_col)
        end = (self.end_line, self.end_col)
        point = (line, column)
        if point < start:
            return False
        if point > end:
            return False
        if point == end:
            return False
        return True

    def order_key(self) -> tuple[int, int]:
        """Return a key favouring narrower spans for comparisons."""

        return (self.end_line - self.start_line, self.end_col - self.start_col)

    def to_dict(self) -> dict[str, list[int]]:
        """Serialise the span into an LSP-style range mapping."""

        return {
            "start": [self.start_line, self.start_col],
            "end": [self.end_line, self.end_col],
        }


class SymbolData(BaseModel):
    """Captured metadata for a Python symbol."""

    name: str
    qualname: str
    kind: Literal["function", "class"]
    is_async: bool
    range: SymbolSpan
    selection: SymbolSpan
    docstring: str | None
    signature: str | None

    model_config = ConfigDict(frozen=True)


class PythonDiagnostic(BaseModel):
    """Represents a diagnostic discovered during stub analysis."""

    message: str
    severity: Literal["error", "warning", "information"]
    range: SymbolSpan
    code: str | None = None

    model_config = ConfigDict(frozen=True)


class PythonModuleAnalysis(BaseModel):
    """Aggregates symbols and diagnostics for a Python module."""

    path: pathlib.Path
    uri: str
    source: str
    lines: tuple[str, ...] = Field(default_factory=tuple)
    line_offsets: tuple[int, ...] = Field(default_factory=tuple)
    symbols: tuple[SymbolData, ...] = Field(default_factory=tuple)
    diagnostics: tuple[PythonDiagnostic, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True)

    def find_at(self, line: int, column: int) -> SymbolData | None:
        """Return the most specific symbol containing ``line``/``column``."""

        candidates = [symbol for symbol in self.symbols if symbol.range.contains(line, column)]
        if not candidates:
            return None
        return min(candidates, key=lambda symbol: symbol.range.order_key())

    def find_by_qualname(self, qualname: str) -> SymbolData | None:
        """Return the symbol matching ``qualname`` if available."""

        normalised = qualname.replace(":", ".")
        for symbol in self.symbols:
            if symbol.qualname == normalised:
                return symbol
        return None

    def find_snippet(self, snippet: str) -> tuple[int, int] | None:
        """Return the first position where ``snippet`` occurs in the source."""

        if not snippet:
            return None
        index = self.source.find(snippet)
        if index == -1:
            return None
        return self._index_to_position(index)

    def find_name_occurrences(self, name: str) -> list[tuple[int, int]]:
        """Return all source positions where ``name`` appears as an identifier."""

        if not name:
            return []
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        positions: list[tuple[int, int]] = []
        for match in pattern.finditer(self.source):
            positions.append(self._index_to_position(match.start()))
        return positions

    def position_to_index(self, line: int, column: int) -> int:
        """Translate ``(line, column)`` coordinates into a character index."""

        if line <= 0:
            return max(column, 0)
        line_index = line - 1
        if line_index < len(self.line_offsets):
            base = self.line_offsets[line_index]
        elif self.line_offsets:
            base = self.line_offsets[-1]
        else:
            base = 0
        return max(base + column, 0)

    def _index_to_position(self, index: int) -> tuple[int, int]:
        """Translate a character index into ``(line, column)`` coordinates."""

        if index <= 0:
            return (1, 0)
        line_idx = bisect.bisect_right(self.line_offsets, index) - 1
        if line_idx < 0:
            return (1, index)
        line_no = line_idx + 1
        line_start = self.line_offsets[line_idx]
        column = index - line_start
        return (line_no, column)


def analyse_python_module(
    path: pathlib.Path, *, source: str | None = None
) -> PythonModuleAnalysis | None:
    """Return :class:`PythonModuleAnalysis` for ``path`` when readable."""

    if source is None:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    lines = tuple(source.splitlines())
    line_offsets = _compute_line_offsets(source)

    diagnostics: list[PythonDiagnostic] = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        start_line = error.lineno or 1
        start_col = (error.offset or 1) - 1
        end_line = error.end_lineno or start_line
        end_col = (error.end_offset or error.offset or (start_col + 1)) - 1
        if end_col <= start_col:
            end_col = start_col + 1
        diagnostics.append(
            PythonDiagnostic(
                message=error.msg or "Syntax error",
                severity="error",
                range=SymbolSpan(
                    start_line=start_line,
                    start_col=max(start_col, 0),
                    end_line=end_line,
                    end_col=max(end_col, start_col + 1),
                ),
            )
        )
        symbols: tuple[SymbolData, ...] = ()
    else:
        collector = _SymbolCollector(path=path, source=source, lines=lines)
        collector.visit(tree)
        symbols = tuple(collector.symbols)

    return PythonModuleAnalysis(
        path=path,
        uri=path.resolve().as_uri(),
        source=source,
        lines=lines,
        line_offsets=line_offsets,
        symbols=symbols,
        diagnostics=tuple(diagnostics),
    )


def _compute_line_offsets(source: str) -> tuple[int, ...]:
    """Return the starting offset for each line in ``source``."""

    offsets: list[int] = [0]
    for index, char in enumerate(source):
        if char == "\n":
            offsets.append(index + 1)
    return tuple(offsets)


class _SymbolCollector(ast.NodeVisitor):
    """Collect function and class symbols for stub analysis."""

    def __init__(self, *, path: pathlib.Path, source: str, lines: abc.Sequence[str]) -> None:
        super().__init__()
        self._path = path
        self._source = source
        self._lines = lines
        self.symbols: list[SymbolData] = []
        self._stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_named_block(node=node, kind="class", is_async=False)
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_named_block(node=node, kind="function", is_async=False)
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_named_block(node=node, kind="function", is_async=True)
        return None

    def _visit_named_block(
        self,
        *,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        kind: Literal["function", "class"],
        is_async: bool,
    ) -> None:
        self._record_symbol(node=node, name=node.name, kind=kind, is_async=is_async)
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def _record_symbol(
        self,
        *,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        name: str,
        kind: Literal["function", "class"],
        is_async: bool,
    ) -> None:
        start_line = getattr(node, "lineno", 1)
        start_col = getattr(node, "col_offset", 0)
        end_line = getattr(node, "end_lineno", start_line) or start_line
        end_col = getattr(node, "end_col_offset", start_col) or start_col
        if (end_line, end_col) <= (start_line, start_col):
            end_col = start_col + max(len(name), 1)

        range_span = SymbolSpan(
            start_line=start_line,
            start_col=start_col,
            end_line=end_line,
            end_col=end_col,
        )
        selection = self._selection_span(name=name, line=start_line, hint_col=start_col)
        docstring = ast.get_docstring(node, clean=False)
        signature = self._extract_signature(node=node, name=name, kind=kind, is_async=is_async)
        qualname = ".".join([*self._stack, name]) if self._stack else name

        self.symbols.append(
            SymbolData(
                name=name,
                qualname=qualname,
                kind=kind,
                is_async=is_async,
                range=range_span,
                selection=selection,
                docstring=docstring,
                signature=signature,
            )
        )

    def _selection_span(self, *, name: str, line: int, hint_col: int) -> SymbolSpan:
        line_index = line - 1
        if 0 <= line_index < len(self._lines):
            text = self._lines[line_index]
            search_start = max(hint_col, 0)
            position = text.find(name, search_start)
            if position == -1:
                position = text.find(name)
            if position == -1:
                position = search_start
        else:
            position = hint_col
        end_col = position + len(name)
        return SymbolSpan(
            start_line=line,
            start_col=position,
            end_line=line,
            end_col=end_col,
        )

    def _extract_signature(
        self,
        *,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        name: str,
        kind: Literal["function", "class"],
        is_async: bool,
    ) -> str | None:
        segment = ast.get_source_segment(self._source, node)
        if segment:
            cleaned: list[str] = []
            for line in segment.splitlines():
                cleaned.append(line.rstrip())
                if ":" in line:
                    break
            signature = "\n".join(part for part in cleaned if part)
            if signature:
                return signature
        if kind == "function":
            prefix = "async def" if is_async else "def"
            return f"{prefix} {name}(...)"
        return f"class {name}:"
