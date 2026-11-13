"""The Agent tool loop is operator-configurable, and disabled means disabled.

The loop is a new outbound surface, so the settings that bound it must be real:
a configured budget has to reach the loop, and turning the loop off has to leave
the single-shot path exactly as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.agent.models import ModelCapability, ModelProfile, ProviderResponse, ToolCall
from neuroagent.agent.providers import MockProvider
from neuroagent.api.app import create_app
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service


def _settings(tmp_path: Path, source_root: Path, **overrides: Any) -> Settings:
    return Settings(
        rag_db_dir=None,
        database_url=f"sqlite:///{(tmp_path / 'tool-budget.sqlite').as_posix()}",
        allowed_source_roots=[source_root],
        allowed_work_root=tmp_path / "work",
        redaction_salt="stable-tool-budget-redaction-salt",
        secrets_file=tmp_path / ".env",
        **overrides,
    )


def _tool_profile() -> ModelProfile:
    return ModelProfile(
        id="tool-profile",
        provider="openai-compatible",
        base_url="https://example.org/v1",
        model="tool-model",
        api_key_env="BUDGET_API_KEY",
        capabilities=frozenset({ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE}),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_max_tool_steps", 0),
        ("agent_max_tool_steps", 33),
        ("agent_max_tool_seconds", 0),
        ("agent_max_tool_seconds", 601),
        ("agent_max_tool_tokens", 999),
        ("agent_max_repeat_tool_failures", 0),
        ("agent_max_repeat_tool_failures", 11),
        ("agent_tool_result_max_bytes", 512),
        ("agent_tool_result_max_bytes", 2_097_152),
    ],
)
def test_loop_budget_settings_are_bounded(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, tmp_path / "source", **{field: value})


def test_loop_budget_defaults_are_conservative(tmp_path: Path) -> None:
    settings = _settings(tmp_path, tmp_path / "source")

    assert settings.agent_tool_loop_enabled is True
    assert settings.agent_max_tool_steps == 8
    assert settings.agent_max_tool_seconds == 90.0
    assert settings.agent_max_repeat_tool_failures == 2


def _loop_budget_from(settings: Settings) -> AgentLoopBudget:
    """Mirror the composition root's mapping so drift is caught here."""

    return AgentLoopBudget(
        max_steps=settings.agent_max_tool_steps,
        max_seconds=settings.agent_max_tool_seconds,
        max_total_tokens=settings.agent_max_tool_tokens,
        max_repeat_failures=settings.agent_max_repeat_tool_failures,
    )


def test_configured_budget_is_mapped_onto_the_loop(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        tmp_path / "source",
        agent_max_tool_steps=3,
        agent_max_tool_seconds=12.5,
        agent_max_tool_tokens=5_000,
        agent_max_repeat_tool_failures=4,
    )

    budget = _loop_budget_from(settings)

    assert budget.max_steps == 3
    assert budget.max_seconds == 12.5
    assert budget.max_total_tokens == 5_000
    assert budget.max_repeat_failures == 4


def test_a_configured_step_budget_actually_stops_the_loop(tmp_path: Path) -> None:
    """A budget that does not stop the loop would be a lie in the settings UI."""

    import asyncio

    from neuroagent.agent.loop import AgentLoop
    from neuroagent.agent.models import AgentLoopStopReason
    from neuroagent.agent.tool_result import ToolCallOutcome, ToolCallStatus
    from neuroagent.agent.tool_spec import ToolTier

    class _Dispatcher:
        catalog: tuple[Any, ...] = ()

        async def dispatch(self, call: ToolCall) -> ToolCallOutcome:
            return ToolCallOutcome(
                call_id=call.call_id,
                name=call.name,
                tier=ToolTier.READ,
                status=ToolCallStatus.SUCCEEDED,
                result={"ok": True},
            )

    settings = _settings(tmp_path, tmp_path / "source", agent_max_tool_steps=2)

    async def call_model(messages: list[dict[str, Any]]) -> ProviderResponse:
        del messages
        return ProviderResponse(
            content="",
            model="tool-model",
            tool_calls=(ToolCall(call_id="c1", name="search_evidence", arguments={"query": "a"}),),
        )

    result = asyncio.run(
        AgentLoop(_Dispatcher(), budget=_loop_budget_from(settings)).run(
            messages=[{"role": "user", "content": "hi"}],
            call_model=call_model,
        )
    )

    assert result.stop_reason is AgentLoopStopReason.BUDGET_STEPS
    assert result.steps == 2


def test_disabling_the_loop_removes_the_runner_and_the_tools(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the loop disabled the profile must not be offered any tool."""

    provider = MockProvider(
        [
            ProviderResponse(
                content='{"intent":"knowledge_query","query":"ALFF 生理含义"}',
                model="tool-model",
            ),
            ProviderResponse(content="单次回答。", model="tool-model"),
        ]
    )
    monkeypatch.setenv("BUDGET_API_KEY", "test-secret")
    settings = _settings(
        tmp_path,
        source_root,
        agent_tool_loop_enabled=False,
    )
    service = build_service(settings, providers={"openai-compatible": provider})
    try:
        assert service.chat_agent._tool_runner is None
        service.repository.create_model_profile(_tool_profile())
        with TestClient(create_app(service=service)) as client:
            conversation = client.post(
                "/api/v1/conversations",
                json={"mode": "chat", "preferred_profile_id": "tool-profile"},
                headers={"Idempotency-Key": "budget-create"},
            ).json()
            response = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={"content": "ALFF 的生理含义是什么?", "preferred_profile_id": "tool-profile"},
                headers={"Idempotency-Key": "budget-turn"},
            )
    finally:
        service.close()

    assert response.status_code == 201, response.text
    metadata = response.json()["assistant_message"]["payload"]["chat"]["metadata"]
    assert "tool_loop" not in metadata
    # Even a tool-capable profile is offered nothing when the loop is off.
    assert all(not entry for entry in provider.tool_requests)
