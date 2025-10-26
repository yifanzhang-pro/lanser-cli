"""Exit code definitions for the Lanser CLI."""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["ExitCode", "ExitCodeDescriptor", "exit_code_descriptors"]


class ExitCode(IntEnum):
    """Enumerates the canonical exit codes used by the CLI."""

    OK = 0
    BAD_SELECTOR_SYNTAX = 2
    NOT_FOUND = 3
    AMBIGUOUS = 4
    VERSION_SKEW = 10
    LS_TIMEOUT = 64
    LS_CRASH = 65
    APPLY_CONFLICT = 70
    FS_PERMISSIONS = 71
    UNSUPPORTED_CAP = 72
    REQUEST_CANCELLED = 73
    CONTENT_MODIFIED = 74
    INDEXING_UNSUPPORTED = 75
    REPLAY_MISMATCH = 76

    def describe(self) -> str:
        """Return a short human readable description."""

        return exit_code_descriptors[self].description


class ExitCodeDescriptor(BaseModel):
    """Pydantic descriptor associating metadata with an exit code."""

    code: ExitCode
    description: str

    model_config = ConfigDict(frozen=True)


exit_code_descriptors: dict[ExitCode, ExitCodeDescriptor] = {
    ExitCode.OK: ExitCodeDescriptor(code=ExitCode.OK, description="Success"),
    ExitCode.BAD_SELECTOR_SYNTAX: ExitCodeDescriptor(
        code=ExitCode.BAD_SELECTOR_SYNTAX,
        description="Selector parse error",
    ),
    ExitCode.NOT_FOUND: ExitCodeDescriptor(
        code=ExitCode.NOT_FOUND,
        description="No resolvable target",
    ),
    ExitCode.AMBIGUOUS: ExitCodeDescriptor(
        code=ExitCode.AMBIGUOUS,
        description="Multiple candidates",
    ),
    ExitCode.VERSION_SKEW: ExitCodeDescriptor(
        code=ExitCode.VERSION_SKEW,
        description="Snapshot mismatch",
    ),
    ExitCode.LS_TIMEOUT: ExitCodeDescriptor(
        code=ExitCode.LS_TIMEOUT,
        description="Language server timeout",
    ),
    ExitCode.LS_CRASH: ExitCodeDescriptor(
        code=ExitCode.LS_CRASH,
        description="Language server crashed",
    ),
    ExitCode.APPLY_CONFLICT: ExitCodeDescriptor(
        code=ExitCode.APPLY_CONFLICT,
        description="Patch could not be applied",
    ),
    ExitCode.FS_PERMISSIONS: ExitCodeDescriptor(
        code=ExitCode.FS_PERMISSIONS,
        description="Write denied",
    ),
    ExitCode.UNSUPPORTED_CAP: ExitCodeDescriptor(
        code=ExitCode.UNSUPPORTED_CAP,
        description="Capability unsupported",
    ),
    ExitCode.REQUEST_CANCELLED: ExitCodeDescriptor(
        code=ExitCode.REQUEST_CANCELLED,
        description="Request was cancelled",
    ),
    ExitCode.CONTENT_MODIFIED: ExitCodeDescriptor(
        code=ExitCode.CONTENT_MODIFIED,
        description="Content changed mid-request",
    ),
    ExitCode.INDEXING_UNSUPPORTED: ExitCodeDescriptor(
        code=ExitCode.INDEXING_UNSUPPORTED,
        description="IO indexing unsupported",
    ),
    ExitCode.REPLAY_MISMATCH: ExitCodeDescriptor(
        code=ExitCode.REPLAY_MISMATCH,
        description="Trace/workspace digest mismatch",
    ),
}
