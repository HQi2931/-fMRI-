"""Intent routing uses mocked models, never external providers or execution."""

# ruff: noqa: RUF001

from unittest.mock import AsyncMock

import pytest

from neuroagent.chat.models import ChatAgentRequest, ChatIntent
from neuroagent.chat.services import ModelIntentRouter, RuleBasedIntentRouter
from neuroagent.context.interfaces import ContextMessage


@pytest.mark.parametrize(
    "message",
    [
        "帮我查一下 ALFF 的预处理方案",
        "不要运行，我只是问一下",
        "ALFF预处理方案需要包含QC",
        "帮我计算 ALFF 之前需要准备什么",
        "TR 采用 2 秒",
    ],
)
async def test_every_substantive_turn_reaches_model(message: str) -> None:
    classify = AsyncMock(return_value='{"intent":"knowledge_query","query":"ALFF 方法"}')
    router = ModelIntentRouter(classify, has_profiles=lambda: True)
    request = ChatAgentRequest(session_id="s", message=message)
    result = await router.route(request)
    classify.assert_awaited_once_with(request)
    assert result.intent is ChatIntent.KNOWLEDGE_QUERY
    assert result.work_request is None


async def test_followup_passes_context_and_selected_model_to_classifier() -> None:
    classify = AsyncMock(return_value='{"intent":"knowledge_query","query":"ALFF TR 的作用"}')
    request = ChatAgentRequest(
        session_id="s",
        message="2 秒可以吗",
        recent_messages=(ContextMessage(role="assistant", content="我们正在讨论 ALFF 的 TR"),),
        conversation_summary="用户在了解 ALFF 预处理",
        preferred_profile_id="selected",
        model="selected-model",
    )
    result = await ModelIntentRouter(classify, has_profiles=lambda: False).route(request)
    classify.assert_awaited_once_with(request)
    assert result.query == "ALFF TR 的作用"


@pytest.mark.parametrize("message", ["你好", "谢谢！", "hello"])
async def test_greeting_keeps_lightweight_shortcut(message: str) -> None:
    classify = AsyncMock()
    result = await ModelIntentRouter(classify, has_profiles=lambda: True).route(
        ChatAgentRequest(session_id="s", message=message)
    )
    classify.assert_not_awaited()
    assert result.intent is ChatIntent.CONVERSATION


@pytest.mark.parametrize(
    "message",
    [
        "帮我查一下 ALFF 的预处理方案",
        "不要运行，我只是问一下",
        "运行之前先解释 ALFF",
        "运行 ALFF，但不要真正执行",
        "Please do not run preprocessing",
        "ALFF preprocessing plan",
    ],
)
async def test_offline_fallback_never_turns_lookup_or_negation_into_work(message: str) -> None:
    classify = AsyncMock()
    result = await ModelIntentRouter(classify, has_profiles=lambda: False).route(
        ChatAgentRequest(session_id="s", message=message)
    )
    classify.assert_not_awaited()
    assert result.intent is ChatIntent.KNOWLEDGE_QUERY
    assert result.work_request is None


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"intent":"work_request","query":"run"}',
        '{"intent":"knowledge_query","query":" "}',
        '{"intent":"execute","query":"run"}',
    ],
)
async def test_invalid_classifier_fails_to_nonexecuting_knowledge(raw: str) -> None:
    classify = AsyncMock(return_value=raw)
    result = await ModelIntentRouter(classify, has_profiles=lambda: True).route(
        ChatAgentRequest(session_id="s", message="帮我运行 ALFF")
    )
    assert result.intent is ChatIntent.KNOWLEDGE_QUERY
    assert result.query == "帮我运行 ALFF"
    assert result.work_request is None


async def test_provider_failure_does_not_infer_work_from_keywords() -> None:
    classify = AsyncMock(side_effect=RuntimeError("mock provider unavailable"))
    result = await ModelIntentRouter(classify, has_profiles=lambda: True).route(
        ChatAgentRequest(session_id="s", message="帮我运行 ALFF")
    )
    assert result.intent is ChatIntent.KNOWLEDGE_QUERY
    assert result.work_request is None


async def test_model_work_draft_always_requires_confirmation() -> None:
    classify = AsyncMock(
        return_value='{"intent":"work_request","query":"ALFF",'
        '"work_request":{"task":"calculate_alff","requires_user_confirmation":false}}'
    )
    result = await ModelIntentRouter(classify, has_profiles=lambda: True).route(
        ChatAgentRequest(session_id="s", message="用刚才的参数执行")
    )
    assert result.intent is ChatIntent.WORK_REQUEST
    assert result.work_request is not None
    assert result.work_request.requires_user_confirmation is True
    assert result.work_request.status == "draft"


async def test_nonwork_result_cannot_smuggle_work_payload() -> None:
    classify = AsyncMock(
        return_value='{"intent":"knowledge_query","query":"ALFF",'
        '"work_request":{"task":"calculate_alff"}}'
    )
    result = await ModelIntentRouter(classify, has_profiles=lambda: True).route(
        ChatAgentRequest(session_id="s", message="解释 ALFF")
    )
    assert result.work_request is None


async def test_offline_explicit_work_request_remains_only_a_draft() -> None:
    result = await RuleBasedIntentRouter().route(
        ChatAgentRequest(session_id="s", message="按照这篇文献帮我计算数据的 ALFF，频段 0.01-0.08")
    )
    assert result.intent is ChatIntent.WORK_REQUEST
    assert result.work_request is not None
    assert result.work_request.parameters == {"frequency_band": [0.01, 0.08]}
    assert result.work_request.requires_user_confirmation is True
