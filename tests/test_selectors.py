"""Unit tests for selector parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from lanser.selectors import (
    AnchorSelector,
    AstPathSelector,
    CursorSelector,
    RangeSelector,
    SelectorParseError,
    SymbolSelector,
    parse_selector,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


def test_parse_cursor_selector(workspace: Path) -> None:
    spec = parse_selector("src/main.py@L10:C2", workspace=workspace, indexing="utf-16")
    assert isinstance(spec, CursorSelector)
    assert spec.line == 10
    assert spec.column == 2
    assert spec.uri.startswith("file://")


def test_parse_cursor_selector_with_doc_version(workspace: Path) -> None:
    spec = parse_selector(
        "src/main.py@L10:C2?doc=ws-42",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, CursorSelector)
    assert spec.doc_version == "ws-42"


def test_parse_cursor_selector_rejects_unknown_query(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector(
            "src/main.py@L1:C1?foo=bar",
            workspace=workspace,
            indexing="utf-16",
        )


def test_parse_range_selector(workspace: Path) -> None:
    spec = parse_selector(
        "src/main.py@R(1,0->2,5)",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, RangeSelector)
    assert spec.start_line == 1
    assert spec.end_column == 5


def test_parse_range_selector_with_doc_version(workspace: Path) -> None:
    spec = parse_selector(
        "src/main.py@R(1,0->2,5)?docVersion=ws-5",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, RangeSelector)
    assert spec.doc_version == "ws-5"


def test_parse_selector_requires_location(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector("src/main.py", workspace=workspace, indexing="utf-16")


def test_parse_symbol_selector(workspace: Path) -> None:
    spec = parse_selector(
        "py://pkg.mod#Class.method:body",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, SymbolSelector)
    assert spec.module == "pkg.mod"
    assert spec.symbol == "Class.method"
    assert spec.role == "body"
    payload = spec.to_payload()
    assert payload["qualname"] == "pkg.mod:Class.method"
    assert payload["role"] == "body"


def test_parse_symbol_selector_query_parameters(workspace: Path) -> None:
    spec = parse_selector(
        "py://pkg.mod#function_name?overload=1&doc=ws-123",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, SymbolSelector)
    assert spec.overload == 1
    assert spec.doc_version == "ws-123"


def test_parse_symbol_selector_invalid_module(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector("py://1invalid#foo", workspace=workspace, indexing="utf-16")


def test_parse_symbol_selector_invalid_overload_value(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector(
            "py://pkg.mod#func?overload=abc",
            workspace=workspace,
            indexing="utf-16",
        )


def test_parse_anchor_selector(workspace: Path) -> None:
    spec = parse_selector(
        'anchor://src/main.py#"def load_data"?ctx=24&hash=sha1:abc123&doc=ws-5',
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, AnchorSelector)
    assert spec.uri.startswith("file://")
    assert spec.snippet == "def load_data"
    assert spec.context == 24
    assert spec.hash == "sha1:abc123"
    assert spec.doc_version == "ws-5"


def test_parse_anchor_selector_rejects_missing_snippet(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector(
            "anchor://src/main.py#?ctx=4",
            workspace=workspace,
            indexing="utf-16",
        )


def test_parse_anchor_selector_rejects_invalid_context(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector(
            'anchor://src/main.py#"snippet"?ctx=-1',
            workspace=workspace,
            indexing="utf-16",
        )


def test_parse_ast_selector(workspace: Path) -> None:
    spec = parse_selector(
        "ast://[module=pkg.mod]/[class=Class]/[def=method]/name[1]",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, AstPathSelector)
    assert [segment.axis for segment in spec.path] == ["module", "class", "def", "name"]
    payload = spec.to_payload()
    path_payload = payload.get("path")
    assert isinstance(path_payload, list)
    assert path_payload[0] == ["module", "pkg.mod"]
    assert path_payload[-1] == ["name", 1]


def test_parse_ast_selector_with_doc_version(workspace: Path) -> None:
    spec = parse_selector(
        "ast://[module=pkg.mod]/expr=value?doc=ws-7",
        workspace=workspace,
        indexing="utf-16",
    )
    assert isinstance(spec, AstPathSelector)
    assert spec.doc_version == "ws-7"


def test_parse_ast_selector_rejects_invalid_segment(workspace: Path) -> None:
    with pytest.raises(SelectorParseError):
        parse_selector(
            "ast://[module=]/invalid",
            workspace=workspace,
            indexing="utf-16",
        )
