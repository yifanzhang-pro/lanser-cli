from __future__ import annotations

import os
from pathlib import Path

import pytest

from lanser.workspace_lock import WorkspaceLock, WorkspaceLockError, WorkspaceLockOwner


def test_workspace_lock_blocks_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "workspace.lock"
    owner = WorkspaceLockOwner.capture(command=("pytest", "lock"))
    primary = WorkspaceLock(lock_path, owner=owner)
    primary.acquire()

    secondary = WorkspaceLock(lock_path)
    with pytest.raises(WorkspaceLockError) as excinfo:
        secondary.acquire()

    error = excinfo.value
    assert error.lock_path == lock_path
    assert error.owner is not None
    assert error.owner.pid == owner.pid
    primary.release()

    secondary.acquire()
    assert secondary.owner.pid == os.getpid()
    secondary.release()


def test_workspace_lock_context_manager(tmp_path: Path) -> None:
    lock_path = tmp_path / "workspace.lock"
    with WorkspaceLock(lock_path) as lock:
        assert lock.path == lock_path
        payload = lock.owner.to_payload()
        assert payload["pid"] == lock.owner.pid
        assert payload["hostname"]
