"""Unit tests for the Python SDK facade."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

from lanser import AsyncLanserClient, ClientConfig, SyncLanserClient
from lanser.orchestrator import (
    BatchRequest,
    BatchResponse,
    ExitCode,
    OperationOutcome,
    OrchestratorSettings,
)


if TYPE_CHECKING:
    from types import TracebackType
else:  # pragma: no cover - runtime placeholder for type checking
    TracebackType = type(None)


class _DummyOrchestrator:
    """Test double replicating the orchestrator surface."""

    def __init__(
        self,
        *,
        settings: OrchestratorSettings,
        progress_handler: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.progress_handler = progress_handler
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def __enter__(self) -> _DummyOrchestrator:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def _ok(self, name: str) -> OperationOutcome:
        return OperationOutcome(
            ok=True,
            message=f"{name} called",
            payload={},
            exit_code=ExitCode.OK,
            metadata={},
        )

    def doctor(self) -> OperationOutcome:
        self.calls.append(("doctor", tuple()))
        return self._ok("doctor")

    def definition(self, selector: str) -> OperationOutcome:
        self.calls.append(("definition", (selector,)))
        return self._ok("definition")

    def references(self, selector: str) -> OperationOutcome:
        self.calls.append(("references", (selector,)))
        return self._ok("references")

    def hover(self, selector: str) -> OperationOutcome:
        self.calls.append(("hover", (selector,)))
        return self._ok("hover")

    def symbols(self, selector: str) -> OperationOutcome:
        self.calls.append(("symbols", (selector,)))
        return self._ok("symbols")

    def diagnostics(
        self,
        *,
        scope: Literal["document", "workspace"],
        selector: str | None,
    ) -> OperationOutcome:
        self.calls.append(("diagnostics", (scope, selector)))
        return self._ok("diagnostics")

    def rename(self, *, selector: str, new_name: str, apply: bool = False) -> OperationOutcome:
        self.calls.append(("rename", (selector, new_name, apply)))
        return self._ok("rename")

    def batch(self, requests: Sequence[BatchRequest]) -> list[BatchResponse]:
        self.calls.append(("batch", tuple(requests)))
        responses: list[BatchResponse] = []
        for request in requests:
            responses.append(
                BatchResponse(
                    id=request.id,
                    ok=True,
                    message="batch called",
                    exit_code=ExitCode.OK,
                    payload={},
                    metadata={},
                )
            )
        return responses


@pytest.fixture(autouse=True)
def _monkeypatch_orchestrator(monkeypatch):
    """Route SDK orchestrator usage through the dummy test double."""

    from lanser import sdk

    monkeypatch.setattr(sdk, "LSPOrchestrator", _DummyOrchestrator)


def test_sync_client_invokes_orchestrator(tmp_path: Path) -> None:
    config = ClientConfig(workspace=tmp_path)
    client = SyncLanserClient(config=config)

    with client as active:
        result = active.definition("py://module#symbol:def")
        assert result.ok
        assert result.message == "definition called"
        doctor = active.doctor()
        assert doctor.ok

    dummy = getattr(client, "_orchestrator", None)
    assert dummy is None


def test_sync_client_passes_configuration(tmp_path: Path) -> None:
    workspace = tmp_path
    allow = workspace / "allowed.txt"
    deny = Path("relative.txt")
    config = ClientConfig(
        workspace=workspace,
        allow_paths=(allow,),
        deny_paths=(deny,),
        position_encoding="utf-8",
        frozen_snapshot=True,
        allow_dirty=True,
    )
    client = SyncLanserClient(config=config)
    with client:
        dummy = getattr(client, "_orchestrator", None)
        assert isinstance(dummy, _DummyOrchestrator)
        assert dummy.settings.workspace == workspace.resolve()
        assert dummy.settings.allow_paths == (allow.resolve(),)
        assert dummy.settings.deny_paths == ((workspace / deny).resolve(),)
        assert dummy.settings.position_encoding == "utf-8"
        assert dummy.settings.frozen_snapshot is True
        assert dummy.settings.allow_dirty is True


def test_async_client_executes_operations(tmp_path: Path) -> None:
    async def _exercise() -> None:
        config = ClientConfig(workspace=tmp_path)
        async with AsyncLanserClient(config=config) as client:
            definition = await client.definition("py://module#symbol:def")
            assert definition.ok
            rename_result = await client.rename(
                selector="py://module#symbol:def",
                new_name="renamed",
                apply=True,
            )
            assert rename_result.ok
            batch_requests = [
                BatchRequest(
                    id="1",
                    command="definition",
                    selector="py://module#symbol:def",
                ),
            ]
            responses = await client.batch(batch_requests)
            assert responses[0].ok

    asyncio.run(_exercise())
