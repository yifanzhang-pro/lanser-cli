"""Python SDK wrappers for the Lanser orchestrator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from .orchestrator import (
    BatchRequest,
    BatchResponse,
    LSPOrchestrator,
    OperationOutcome,
    OrchestratorSettings,
)

__all__ = [
    "ClientConfig",
    "SyncLanserClient",
    "AsyncLanserClient",
]


ProgressHandler = Callable[[Mapping[str, Any]], None]

_ResultT = TypeVar("_ResultT")

if TYPE_CHECKING:
    from types import TracebackType
else:  # pragma: no cover - runtime placeholder for type checking
    TracebackType = type(None)


class ClientConfig(BaseModel):
    """Configuration options shared by SDK clients."""

    workspace: Path = Field(default_factory=lambda: Path.cwd())
    frozen_snapshot: bool = False
    position_encoding: str = "utf-16"
    allow_dirty: bool = False
    allow_paths: tuple[Path, ...] = ()
    deny_paths: tuple[Path, ...] = ()

    model_config = ConfigDict(frozen=True)

    def _normalise_filter_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path.resolve()
        return (self.workspace / path).resolve()

    def to_settings(self) -> OrchestratorSettings:
        """Convert the configuration to orchestrator settings."""

        allow_paths = (
            tuple(self._normalise_filter_path(path) for path in self.allow_paths)
            if self.allow_paths
            else None
        )
        deny_paths = (
            tuple(self._normalise_filter_path(path) for path in self.deny_paths)
            if self.deny_paths
            else None
        )
        return OrchestratorSettings(
            workspace=self.workspace.resolve(),
            frozen_snapshot=self.frozen_snapshot,
            position_encoding=self.position_encoding,
            allow_dirty=self.allow_dirty,
            allow_paths=allow_paths,
            deny_paths=deny_paths,
        )


class SyncLanserClient(BaseModel):
    """Synchronous SDK facade around :class:`LSPOrchestrator`."""

    config: ClientConfig = Field(default_factory=ClientConfig)
    progress_handler: ProgressHandler | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _orchestrator: LSPOrchestrator | None = PrivateAttr(default=None)

    def __enter__(self) -> SyncLanserClient:
        if self._orchestrator is not None:
            msg = "Client context already active."
            raise RuntimeError(msg)
        orchestrator = LSPOrchestrator(
            settings=self.config.to_settings(),
            progress_handler=self.progress_handler,
        )
        orchestrator.__enter__()
        self._orchestrator = orchestrator
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying orchestrator when active."""

        orchestrator = self._orchestrator
        if orchestrator is None:
            return
        try:
            orchestrator.__exit__(None, None, None)
        finally:
            self._orchestrator = None

    def _require_orchestrator(self) -> LSPOrchestrator:
        orchestrator = self._orchestrator
        if orchestrator is None:
            msg = "Client must be used as a context manager before invoking operations."
            raise RuntimeError(msg)
        return orchestrator

    def doctor(self) -> OperationOutcome:
        return self._require_orchestrator().doctor()

    def definition(self, selector: str) -> OperationOutcome:
        return self._require_orchestrator().definition(selector)

    def references(self, selector: str) -> OperationOutcome:
        return self._require_orchestrator().references(selector)

    def hover(self, selector: str) -> OperationOutcome:
        return self._require_orchestrator().hover(selector)

    def symbols(self, selector: str) -> OperationOutcome:
        return self._require_orchestrator().symbols(selector)

    def diagnostics(
        self,
        *,
        scope: Literal["document", "workspace"],
        selector: str | None,
    ) -> OperationOutcome:
        return self._require_orchestrator().diagnostics(scope=scope, selector=selector)

    def rename(self, *, selector: str, new_name: str, apply: bool = False) -> OperationOutcome:
        return self._require_orchestrator().rename(
            selector=selector,
            new_name=new_name,
            apply=apply,
        )

    def batch(self, requests: Sequence[BatchRequest]) -> list[BatchResponse]:
        return self._require_orchestrator().batch(tuple(requests))


class AsyncLanserClient(BaseModel):
    """Async wrapper around :class:`SyncLanserClient` using ``asyncio`` executors."""

    config: ClientConfig = Field(default_factory=ClientConfig)
    progress_handler: ProgressHandler | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _sync_client: SyncLanserClient | None = PrivateAttr(default=None)
    _lock: asyncio.Lock | None = PrivateAttr(default=None)

    async def __aenter__(self) -> AsyncLanserClient:
        if self._sync_client is not None:
            msg = "Async client context already active."
            raise RuntimeError(msg)
        sync_client = SyncLanserClient(
            config=self.config,
            progress_handler=self.progress_handler,
        )
        sync_client.__enter__()
        self._sync_client = sync_client
        self._lock = asyncio.Lock()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying synchronous client when active."""

        sync_client = self._sync_client
        if sync_client is None:
            return
        try:
            sync_client.close()
        finally:
            self._sync_client = None
            self._lock = None

    def _require_sync_client(self) -> SyncLanserClient:
        sync_client = self._sync_client
        if sync_client is None:
            msg = (
                "Async client must be used as an async context manager before invoking operations."
            )
            raise RuntimeError(msg)
        return sync_client

    async def _run(
        self,
        func: Callable[..., _ResultT],
        *args: object,
        **kwargs: object,
    ) -> _ResultT:
        lock = self._lock
        if lock is None:
            msg = "Async client is not active."
            raise RuntimeError(msg)
        async with lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def doctor(self) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.doctor)

    async def definition(self, selector: str) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.definition, selector)

    async def references(self, selector: str) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.references, selector)

    async def hover(self, selector: str) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.hover, selector)

    async def symbols(self, selector: str) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.symbols, selector)

    async def diagnostics(
        self,
        *,
        scope: Literal["document", "workspace"],
        selector: str | None,
    ) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.diagnostics, scope=scope, selector=selector)

    async def rename(
        self,
        *,
        selector: str,
        new_name: str,
        apply: bool = False,
    ) -> OperationOutcome:
        client = self._require_sync_client()
        return await self._run(client.rename, selector=selector, new_name=new_name, apply=apply)

    async def batch(self, requests: Sequence[BatchRequest]) -> list[BatchResponse]:
        client = self._require_sync_client()
        return await self._run(client.batch, requests)
