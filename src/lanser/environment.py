"""Environment discovery utilities for Lanser."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from collections import abc, deque
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field

from .pyright_version import PYRIGHT_VERSION_SUPPORT

__all__ = [
    "EnvironmentSnapshot",
    "PythonCompatibilityEntry",
    "DEFAULT_COMPATIBILITY_TARGETS",
    "gather_environment",
    "snapshot_to_json",
]


class PythonCompatibilityEntry(BaseModel):
    """Represents compatibility of a specific Python version target."""

    target: str
    normalized_version: str | None
    satisfies: bool | None
    reason: str | None = None

    model_config = ConfigDict(frozen=True)


class EnvironmentSnapshot(BaseModel):
    """Capture the runtime environment for diagnostics and replay."""

    python_version: str
    python_executable: str
    platform: str
    cwd: str
    pyright_version: str | None
    pyright_expected_version: str = Field(default=PYRIGHT_VERSION_SUPPORT.cli_label)
    pyright_supported_versions: tuple[str, ...] = Field(
        default=PYRIGHT_VERSION_SUPPORT.supported_versions,
    )
    project_files: tuple[str, ...] = Field(default_factory=tuple)
    config_digest: str | None
    git_root: str | None
    git_head: str | None
    git_dirty: bool | None
    workspace_snapshot: str
    python_requirement: str | None = None
    python_compatibility: tuple[PythonCompatibilityEntry, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable mapping."""

        return self.model_dump()


_PROJECT_FILE_CANDIDATES: tuple[str, ...] = (
    "pyproject.toml",
    "pyrightconfig.json",
    "PROJECT.md",
    "CHECKLIST.md",
)
_PROJECT_DISCOVERY_SKIP: frozenset[str] = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".tox", "node_modules"}
)
_PROJECT_MAX_DEPTH = 2
_CONFIG_DIGEST_FILES: frozenset[str] = frozenset({"pyproject.toml", "pyrightconfig.json"})

DEFAULT_COMPATIBILITY_TARGETS: tuple[str, ...] = ("3.12.*", "3.13.*")


def _discover_project_files(workspace: Path, *, max_depth: int = _PROJECT_MAX_DEPTH) -> list[Path]:
    """Return relevant project files discovered within ``workspace``.

    The search walks breadth-first up to ``max_depth`` directories deep, skipping common
    virtual environment and cache folders. Discovered files are returned as resolved paths
    with deterministic ordering.
    """

    root = workspace.resolve()
    discovered: set[Path] = set()
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    visited: set[Path] = set()

    while queue:
        current, depth = queue.popleft()
        if current in visited:
            continue
        visited.add(current)

        try:
            entries = list(current.iterdir())
        except OSError:
            continue

        for entry in entries:
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file() and name in _PROJECT_FILE_CANDIDATES:
                    try:
                        resolved = entry.resolve()
                    except OSError:
                        continue
                    discovered.add(resolved)
                    continue
                if depth >= max_depth:
                    continue
                if entry.is_dir() and name not in _PROJECT_DISCOVERY_SKIP:
                    queue.append((entry, depth + 1))
            except OSError:
                continue

    return sorted(discovered, key=lambda path: str(path))


def _discover_pyright_version() -> str | None:
    """Return the installed Pyright version, if available."""

    spec = importlib.util.find_spec("pyright")
    if spec is not None:
        module = importlib.import_module("pyright")
        version_value = getattr(module, "__version__", "").strip()
        if version_value:
            return f"pyright {version_value}"

    executable = shutil.which("pyright")
    if executable is None:
        return None

    try:
        process = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    version = process.stdout.strip() or process.stderr.strip()
    return version or None


def _discover_git_metadata(workspace: Path) -> tuple[str | None, str | None, bool | None]:
    """Return Git repository metadata for ``workspace`` if available."""

    try:
        process = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return (None, None, None)

    git_root_raw = process.stdout.strip() or process.stderr.strip()
    if not git_root_raw:
        return (None, None, None)

    git_root = Path(git_root_raw)

    try:
        head_process = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        git_head = head_process.stdout.strip() or head_process.stderr.strip() or None
    except (OSError, subprocess.SubprocessError):
        git_head = None

    try:
        status_process = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        git_dirty = bool(status_process.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        git_dirty = None

    return (str(git_root), git_head, git_dirty)


def _compute_config_digest(project_files: abc.Sequence[Path]) -> str | None:
    """Return a deterministic digest for configuration files."""

    relevant: list[Path] = [path for path in project_files if path.name in _CONFIG_DIGEST_FILES]

    if not relevant:
        return None

    hasher = hashlib.sha256()
    for path in sorted(relevant):
        hasher.update(path.name.encode("utf-8"))
        try:
            hasher.update(path.read_bytes())
        except OSError:
            continue
    return f"sha256:{hasher.hexdigest()}"


def _compute_workspace_snapshot(
    *,
    workspace: Path,
    project_files: abc.Sequence[str],
    config_digest: str | None,
    git_head: str | None,
    git_dirty: bool | None,
) -> str:
    """Return a deterministic digest describing the workspace snapshot.

    The snapshot intentionally focuses on reproducibility signals that agents care
    about for frozen workspaces: the resolved workspace root, configuration
    digests, and Git state. When Git metadata is unavailable the digest still
    incorporates the discovered project files so callers can differentiate
    between worktrees.
    """

    hasher = hashlib.sha256()
    hasher.update(str(workspace).encode("utf-8"))

    if config_digest:
        hasher.update(config_digest.encode("utf-8"))

    if git_head is not None:
        hasher.update(f"git-head:{git_head}".encode())
    else:
        hasher.update(b"git-head:<none>")

    if git_dirty is None:
        hasher.update(b"git-dirty:<unknown>")
    else:
        hasher.update(f"git-dirty:{git_dirty}".encode())

    for path in sorted(project_files):
        hasher.update(path.encode())

    return f"sha256:{hasher.hexdigest()}"


def _discover_python_requirement() -> str | None:
    """Return the ``Requires-Python`` metadata for the installed package."""

    try:
        metadata = importlib.metadata.metadata("lanser")
    except importlib.metadata.PackageNotFoundError:
        return None

    requirement = metadata.get("Requires-Python")
    if requirement:
        requirement = requirement.strip()
    return requirement or None


def _normalise_version_target(target: str) -> tuple[str | None, str | None]:
    """Return the normalised version used for compatibility checks."""

    text = target.strip()
    if not text:
        return (None, None)

    candidate = text
    if candidate.endswith(".*"):
        candidate = candidate[:-2]

    if not candidate:
        return (text, None)

    if candidate.count(".") == 1:
        candidate = f"{candidate}.0"

    return (text, candidate)


def _evaluate_python_compatibility(
    requirement: str | None,
    targets: abc.Sequence[str],
) -> tuple[PythonCompatibilityEntry, ...]:
    """Return compatibility entries evaluating ``targets`` against ``requirement``."""

    if not targets:
        return tuple()

    spec_set: SpecifierSet | None = None
    spec_error: str | None = None
    if requirement:
        try:
            spec_set = SpecifierSet(requirement)
        except InvalidSpecifier as error:
            spec_error = f"invalid requirement: {error}".strip()

    entries: list[PythonCompatibilityEntry] = []
    for target in targets:
        original, candidate = _normalise_version_target(target)
        reason: str | None = None
        satisfies: bool | None

        if original is None:
            continue

        if spec_set is None:
            satisfies = None
            reason = spec_error
        else:
            if candidate is None:
                satisfies = None
                reason = "empty version target"
            else:
                try:
                    version = Version(candidate)
                except InvalidVersion as error:
                    satisfies = None
                    reason = f"invalid version: {error}".strip()
                else:
                    satisfies = version in spec_set

        entries.append(
            PythonCompatibilityEntry(
                target=original,
                normalized_version=candidate,
                satisfies=satisfies,
                reason=reason,
            )
        )

    return tuple(entries)


def gather_environment(
    workspace: Path | None = None,
    *,
    python_targets: abc.Sequence[str] | None = None,
) -> EnvironmentSnapshot:
    """Assemble a snapshot of the current runtime characteristics."""

    root = (workspace or Path.cwd()).resolve()

    discovered_paths = _discover_project_files(root)
    project_files = [str(path) for path in discovered_paths]

    git_root, git_head, git_dirty = _discover_git_metadata(root)

    config_digest = _compute_config_digest(discovered_paths)

    python_requirement = _discover_python_requirement()
    targets = tuple(python_targets) if python_targets is not None else DEFAULT_COMPATIBILITY_TARGETS
    python_compatibility = _evaluate_python_compatibility(python_requirement, targets)

    workspace_snapshot = _compute_workspace_snapshot(
        workspace=root,
        project_files=project_files,
        config_digest=config_digest,
        git_head=git_head,
        git_dirty=git_dirty,
    )

    snapshot = EnvironmentSnapshot(
        python_version=sys.version.split(" ")[0],
        python_executable=sys.executable,
        platform=platform.platform(),
        cwd=str(root),
        pyright_version=_discover_pyright_version(),
        project_files=tuple(project_files),
        config_digest=config_digest,
        git_root=git_root,
        git_head=git_head,
        git_dirty=git_dirty,
        workspace_snapshot=workspace_snapshot,
        python_requirement=python_requirement,
        python_compatibility=python_compatibility,
    )
    return snapshot


def snapshot_to_json(snapshot: EnvironmentSnapshot) -> str:
    """Serialise ``snapshot`` to deterministic JSON."""

    return json.dumps(snapshot.to_dict(), sort_keys=True, indent=2)
