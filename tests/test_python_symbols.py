import textwrap
from pathlib import Path

from lanser.python_symbols import analyse_python_module


def test_analyse_python_module_collects_symbols(tmp_path: Path) -> None:
    module = tmp_path / "sample.py"
    module.write_text(
        textwrap.dedent(
            """
            async def outer():
                def inner():
                    '''Docstring'''
                    return 1
                return inner()
            """
        )
    )

    analysis = analyse_python_module(module)
    assert analysis is not None
    assert analysis.symbols

    # The outer async function should be captured with signature metadata.
    outer = analysis.symbols[0]
    assert outer.name == "outer"
    assert outer.is_async is True
    assert "async def outer" in (outer.signature or "")

    inner = analysis.find_by_qualname("outer.inner")
    assert inner is not None
    assert analysis.find_at(inner.range.start_line, inner.range.start_col) == inner
    assert analysis.find_name_occurrences("inner")


def test_analyse_python_module_reports_syntax_errors(tmp_path: Path) -> None:
    module = tmp_path / "broken.py"
    module.write_text("def broken(:\n    pass\n")

    analysis = analyse_python_module(module)
    assert analysis is not None
    assert analysis.diagnostics
    diagnostic = analysis.diagnostics[0]
    assert diagnostic.severity == "error"
    assert "syntax" in diagnostic.message.lower()
