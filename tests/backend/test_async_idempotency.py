from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from neuroagent.application.errors import ConflictError
from neuroagent.application.services import NeuroAgentService


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_make_dispatched_operation_retryable(
    service: NeuroAgentService,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def prepare() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "durable-response"

    request = _Request(value="same")
    pending = asyncio.create_task(
        service._idempotent_async(
            scope="test:async-cancel",
            key="cancelled-caller-key",
            request=request,
            response_type=_Response,
            prepare=prepare,
            finalize=lambda value: _Response(value=value),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending

    async def must_not_repeat() -> str:
        raise AssertionError("the completed operation must be replayed from storage")

    repeated = await service._idempotent_async(
        scope="test:async-cancel",
        key="cancelled-caller-key",
        request=request,
        response_type=_Response,
        prepare=must_not_repeat,
        finalize=lambda value: _Response(value=value),
    )
    assert repeated == _Response(value="durable-response")
    assert calls == 1


@pytest.mark.asyncio
async def test_final_owner_fence_precedes_business_finalization(
    service: NeuroAgentService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized = False

    def finalize(value: str) -> _Response:
        nonlocal finalized
        finalized = True
        return _Response(value=value)

    monkeypatch.setattr(
        service.repository,
        "renew_idempotent_request",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(ConflictError) as raised:
        await service._idempotent_async(
            scope="test:owner-fence",
            key="lost-owner-key",
            request=_Request(value="same"),
            response_type=_Response,
            prepare=lambda: asyncio.sleep(0, result="prepared"),
            finalize=finalize,
        )

    assert raised.value.code == "idempotency_lease_lost"
    assert finalized is False
