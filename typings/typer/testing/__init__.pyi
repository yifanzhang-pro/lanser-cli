from __future__ import annotations

from typing import Any

class Result:
    exit_code: int
    stdout: str

class CliRunner:
    def invoke(self, *args: Any, **kwargs: Any) -> Result: ...
