"""Role routing and role-scoped Work evidence lookup."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from neuroagent.application.contracts.conversations import ConversationView
from neuroagent.multi_agent.registry import WorkAgentRole

_ANALYSIS_INTENT = re.compile(
    r"统计|数据分析|分析.{0,12}(?:ALFF|fALFF|ReHo|指标|结果)|比较|差异|t\s*检验|"
    r"校正|FDR|GRF|脑区|簇|相关|回归|ANOVA|ANCOVA|显著性|结果解释|ROI|atlas|"
    r"\b(?:statistic(?:s|al)?|compar(?:e|ison)|t[ -]?test|correlat(?:ion|e)|regression|"
    r"locali[sz](?:e|ation)|interpret(?:ation)?|group[ -]difference)\b|"
    r"\banaly[sz](?:e|ing)\b.{0,30}\b(?:results?|metrics?|ALFF|fALFF|ReHo)\b",
    re.IGNORECASE,
)
_PREPROCESSING_INTENT = re.compile(
    r"预处理|ALFF|fALFF|ReHo|计算指标|DPABI|滤波|平滑|配准|标准化|QC|质控|"
    r"头动|数据检查|输入阶段|运行失败|处理数据|"
    r"\b(?:preprocess(?:ing)?|comput(?:e|ation)|calculat(?:e|ion)|motion|"
    r"filter(?:ing)?|smooth(?:ing)?|registration|normali[sz](?:e|ation))\b",
    re.IGNORECASE,
)
_PREPROCESS_THEN_ANALYZE = re.compile(
    r"先.{0,40}(?:预处理|ALFF|fALFF|ReHo|计算指标).{0,24}(?:再|然后|之后).{0,36}(?:统计|比较|分析|检验)|"
    r"\b(?:first\s+)?(?:preprocess(?:ing)?|comput(?:e|ing)|calculat(?:e|ing))\b.{0,70}"
    r"\b(?:then|afterwards)\b.{0,70}"
    r"\b(?:compar(?:e|ing)|analy[sz](?:e|ing)|t[ -]?test|statistics)\b",
    re.IGNORECASE,
)
_DRAFT_EDIT_LANGUAGE = re.compile(
    r"修改|调整|补充|改成|设置|参数|其中|把.{0,12}(?:设为|改为|调整)|新增一个参数"
)
_CONTINUATION = re.compile(
    r"^(继续|接着|下一步|查看进度|查看结果|刷新|再试一次|重试|continue|next(?: step)?|"
    r"show(?: the)? (?:results?|progress)|check(?: the)? (?:results?|progress)|refresh|retry)"
    r"[。！!？?. ]*$",
    re.IGNORECASE,
)
_PREPROCESSING_SUBJECT = re.compile(
    r"预处理|ALFF|fALFF|ReHo|DPABI|滤波|平滑|配准|标准化|头动|"
    r"\b(?:preprocess(?:ing)?|motion|registration|normali[sz](?:e|ation))\b",
    re.IGNORECASE,
)
_PREPROCESSING_DIAGNOSTIC = re.compile(
    r"失败|报错|故障|日志|诊断|\b(?:fail(?:ed|ure)?|error|log(?:s)?|diagnos(?:e|is))\b",
    re.IGNORECASE,
)
_STATISTICAL_OPERATION = re.compile(
    r"统计|比较|差异|检验|FDR|GRF|相关|回归|ANOVA|ANCOVA|"
    r"\b(?:statistics|statistical|compar(?:e|ison)|t[ -]?test|correlat(?:ion|e)|regression)\b",
    re.IGNORECASE,
)
_VAGUE_TASK = re.compile(
    r"^(?:请|帮我)?(?:分析(?:一下)?(?:数据)?|处理(?:一下)?(?:数据)?|看看(?:数据)?|"
    r"analy[sz]e(?: the)? data|process(?: the)? data|help(?: me)?)[。！!？?. ]*$",
    re.IGNORECASE,
)


def work_agent_routing_question(
    content: str, *, requested: WorkAgentRole | None, conversation: ConversationView
) -> str | None:
    """Clarify an unbound continuation instead of inventing its target task."""
    if requested is not None:
        return None
    if not (_CONTINUATION.fullmatch(content.strip()) or _VAGUE_TASK.fullmatch(content.strip())):
        return None
    role = work_agent_role_for_turn(content, requested=None, conversation=conversation)
    if conversation.active_run_id or any(
        task.agent_role is role and (task.run_id or task.plan_revision_id or task.proposal_hash)
        for task in conversation.work_tasks
    ):
        return None
    if (
        latest_agent_payload(
            conversation, "analysis_draft" if role is WorkAgentRole.ANALYSIS else "work_draft", role
        )
        or latest_handoff_for_agent(conversation, role, pending_only=True)
        or latest_analysis_dependency(conversation)
    ):
        return None
    return "目前没有可承接的明确任务。你希望准备预处理，还是使用已有指标进行统计分析？"


def work_agent_role_for_turn(
    content: str,
    *,
    requested: WorkAgentRole | None,
    conversation: ConversationView,
    analysis_ready: bool = False,
) -> WorkAgentRole:
    """Resolve the UI selection or infer the role for a new task."""

    if requested is not None:
        return requested
    prior = conversation.foreground_agent_role
    pending_handoff: WorkAgentRole | None = None
    for message in reversed(conversation.messages):
        if message.role != "assistant":
            continue
        value = message.payload.get("agent_role")
        handoff = message.payload.get("agent_handoff")
        if (
            isinstance(handoff, dict)
            and handoff.get("status") in {"pending", "proposed"}
            and _handoff_project_matches(handoff, conversation)
        ):
            target = handoff.get("to_agent")
            if target in {item.value for item in WorkAgentRole}:
                pending_handoff = WorkAgentRole(target)
        if value in {item.value for item in WorkAgentRole}:
            break
        if isinstance(message.payload.get("work_draft"), dict):
            break
    if _CONTINUATION.fullmatch(content.strip()):
        if pending_handoff is not None:
            return pending_handoff
        dependency = latest_analysis_dependency(conversation)
        if (
            analysis_ready
            and dependency is not None
            and dependency.get("status") in {"waiting_for_preprocessing", "analysis_ready"}
        ):
            return WorkAgentRole.ANALYSIS
        return prior
    if _PREPROCESS_THEN_ANALYZE.search(content):
        return WorkAgentRole.PREPROCESSING
    if (
        _PREPROCESSING_SUBJECT.search(content)
        and _PREPROCESSING_DIAGNOSTIC.search(content)
        and not _STATISTICAL_OPERATION.search(content)
    ):
        return WorkAgentRole.PREPROCESSING
    if _ANALYSIS_INTENT.search(content):
        return WorkAgentRole.ANALYSIS
    if _PREPROCESSING_INTENT.search(content):
        return WorkAgentRole.PREPROCESSING
    return prior


def latest_handoff_for_agent(
    conversation: ConversationView, role: WorkAgentRole, *, pending_only: bool = False
) -> dict[str, object] | None:
    """Return a small handoff contract without exposing the source Agent history."""

    for message in reversed(conversation.messages):
        value = message.payload.get("agent_handoff")
        if not isinstance(value, dict) or value.get("to_agent") != role.value:
            continue
        if not _handoff_project_matches(value, conversation):
            continue
        if pending_only and value.get("status") not in {"pending", "proposed"}:
            return None
        return dict(value)
    return None


def _handoff_project_matches(handoff: dict[str, object], conversation: ConversationView) -> bool:
    references = handoff.get("data_references")
    return isinstance(references, dict) and references.get("project_id") == conversation.project_id


def analysis_dependency_for_request(
    content: str, project_id: str | None, conversation_id: str, idempotency_key: str
) -> dict[str, object] | None:
    """Capture an explicit two-stage request without guessing its parameters."""

    if not _PREPROCESS_THEN_ANALYZE.search(content):
        return None
    task: dict[str, object] = {
        "task_kind": "preprocess_then_analyze",
        "conversation_id": conversation_id,
        "project_id": project_id,
        "analysis_request": content.strip(),
        "status": "waiting_for_preprocessing",
    }
    task["task_id"] = handoff_identity({**task, "idempotency_key": idempotency_key})[:24]
    return task


def starts_new_preprocessing_task(content: str) -> bool:
    """Distinguish a new preprocessing goal from edits to an active draft."""
    normalized = content.strip()
    if not _PREPROCESSING_INTENT.search(normalized) or _PREPROCESS_THEN_ANALYZE.search(normalized):
        return False
    if _DRAFT_EDIT_LANGUAGE.search(normalized):
        return False
    return not re.fullmatch(
        r"(?:确认方案参数 [a-f0-9]{12}|批准并启动 [a-f0-9]{12} .+|"
        r"查看运行进度|重新校验当前方案与启动确认。?)",
        normalized,
    )


def clear_analysis_intent_envelope(
    payload: dict[str, Any], *, conversation_id: str, project_id: str | None
) -> None:
    """Tombstone an old intent even when this turn has not produced a draft yet."""
    payload["analysis_intent_envelope"] = {
        "status": "cleared",
        "conversation_id": conversation_id,
        "project_id": project_id,
    }
    draft = payload.get("work_draft")
    if isinstance(draft, dict):
        draft.pop("analysis_intent_task_id", None)
        draft.pop("analysis_request_hash", None)


def latest_analysis_dependency(
    conversation: ConversationView,
) -> dict[str, object] | None:
    for message in reversed(conversation.messages):
        value = message.payload.get("analysis_dependency")
        if isinstance(value, dict) and value.get("project_id") == conversation.project_id:
            return dict(value)
    return None


def handoff_identity(record: dict[str, object]) -> str:
    """Stable identity makes a repeated handoff proposal idempotent."""

    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def recent_messages_for_agent(
    conversation: ConversationView, role: WorkAgentRole, *, limit: int = 12
) -> list[dict[str, str]]:
    """Return completed user/assistant turns owned by one role."""

    messages: list[dict[str, str]] = []
    pending_user: str | None = None
    for message in conversation.messages:
        if message.role == "user":
            pending_user = message.content
            continue
        if message.role != "assistant":
            continue
        owner = message.payload.get("agent_role")
        if owner is None and isinstance(message.payload.get("work_draft"), dict):
            owner = WorkAgentRole.PREPROCESSING.value
        if owner != role.value:
            pending_user = None
            continue
        if pending_user is not None:
            messages.append({"role": "user", "content": pending_user})
        messages.append({"role": "assistant", "content": message.content})
        pending_user = None
    return messages[-limit:]


def latest_agent_payload(
    conversation: ConversationView, key: str, role: WorkAgentRole
) -> dict[str, object] | None:
    """Read the most recent payload owned by one Work role."""

    for message in reversed(conversation.messages):
        value = message.payload.get(key)
        if not isinstance(value, dict):
            continue
        message_role = message.payload.get("agent_role")
        # Before role metadata was introduced, work_draft belonged to preprocessing.
        if message_role is None and key == "work_draft":
            message_role = WorkAgentRole.PREPROCESSING.value
        if message_role == role.value:
            return value
    return None
