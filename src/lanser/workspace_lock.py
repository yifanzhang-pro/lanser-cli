"""Workspace locking helpers to coordinate concurrent orchestrator sessions."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = ["WorkspaceLock", "WorkspaceLockError", "WorkspaceLockOwner"]


class WorkspaceLockOwner(BaseModel):
    """Metadata describing the process that currently owns the workspace lock."""

    pid: int
    hostname: str
    command: tuple[str, ...] = Field(default_factory=tuple)
    acquired_at: str = Field(alias="acquiredAt")

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    @classmethod
    def capture(cls, command: tuple[str, ...] | list[str] | None = None) -> WorkspaceLockOwner:
        """Return owner metadata captured from the current process."""

        if command is None:
            command_tuple = tuple(str(arg) for arg in sys.argv)
        else:
            command_tuple = tuple(str(arg) for arg in command)
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        return cls(
            pid=os.getpid(),
            hostname=os.uname().nodename,
            command=command_tuple,
            acquiredAt=timestamp,
        )

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-serialisable payload representing the owner."""

        return self.model_dump(by_alias=True)


class WorkspaceLockError(RuntimeError):
    """Raised when an exclusive workspace lock cannot be acquired."""

    def __init__(
        self,
        message: str,
        *,
        lock_path: Path,
        owner: WorkspaceLockOwner | None,
    ) -> None:
        super().__init__(message)
        self.lock_path = lock_path
        self.owner = owner


class WorkspaceLock:
    """Coordinate exclusive workspace access through an advisory file lock."""

    def __init__(self, path: Path, *, owner: WorkspaceLockOwner | None = None) -> None:
        resolved_path = Path(path)
        self._path = resolved_path
        self._owner = owner or WorkspaceLockOwner.capture()
        self._file: IO[str] | None = None

    @property
    def path(self) -> Path:
        """Return the path backing the lock file."""

        return self._path

    @property
    def owner(self) -> WorkspaceLockOwner:
        """Return metadata describing the lock owner."""

        return self._owner

    def acquire(self) -> None:
        """Acquire the exclusive lock or raise :class:`WorkspaceLockError`."""

        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        file = open(path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                owner = self._read_owner(file)
                raise WorkspaceLockError(
                    "Workspace lock is already held by another process.",
                    lock_path=path,
                    owner=owner,
                ) from error

            self._write_owner(file)
            self._file = file
        except Exception:
            file.close()
            raise

    def release(self) -> None:
        """Release the lock if it is currently held."""

        file = self._file
        if file is None:
            return
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
            self._file = None

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self.release()
        except Exception:
            pass

    def _write_owner(self, file: IO[str]) -> None:
        payload = json.dumps(self._owner.to_payload(), ensure_ascii=False, sort_keys=True)
        file.seek(0)
        file.truncate()
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())

    @staticmethod
    def _read_owner(file: IO[str]) -> WorkspaceLockOwner | None:
        file.seek(0)
        data = file.read().strip()
        if not data:
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return WorkspaceLockOwner.model_validate(payload)
        except ValidationError:
            return None
