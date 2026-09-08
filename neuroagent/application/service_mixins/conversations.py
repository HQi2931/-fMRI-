"""Persistent Chat/Work conversations with deterministic tool orchestration."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from neuroagent.analysis.models import EvidenceChunk, RsFmriAnswer
from neuroagent.application.contracts import (
    ConversationAction,
    ConversationCreate,
    ConversationMode,
    ConversationToolStatus,
    ConversationTurnCreate,
    ConversationTurnView,
    ConversationView,
    ExecutionBackend,
    ExecutionWorkspaceMode,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunCreate,
    RunView,
    WorkspaceCheckRequest,
    WorkspaceCheckView,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.interfaces import ChatAgentError
from neuroagent.chat.models import ChatAgentRequest
from neuroagent.context.interfaces import ContextMessage


class ConversationMixin(BaseServiceMixin):
    answer_rsfmri_question: Callable[[RsFmriQuestionRequest], RsFmriAnswerView]
    check_workspace: Callable[[WorkspaceCheckRequest], WorkspaceCheckView]
    get_run: Callable[[str], RunView]
    create_run: Callable[[RunCreate, str], RunView]
    chat_agent: ChatAgent

    def create_conversation(
        self, request: ConversationCreate, idempotency_key: str
    ) -> ConversationView:
        title = request.title or (
            "fMRI 专项问答" if request.mode is ConversationMode.CHAT else "rs-fMRI 工作流"
        )
        welcome = (
            "这里是 fMRI 专项问答。我会检索项目内的 rs-fMRI、DPABI 和统计方法文档，"
            "并根据找到的证据回答。"
            if request.mode is ConversationMode.CHAT
            else "这里是 rs-fMRI 工作模式。请选择工作区，我会调用检查工具，"
            "再协助启动受控预处理和查看进度。"
        )
        return self._idempotent(
            scope=f"conversations:{request.mode.value}:create",
            key=idempotency_key,
            request=request,
            response_type=ConversationView,
            action=lambda: self.repository.create_conversation(
                mode=request.mode,
                title=title,
                welcome=welcome,
                workspace_path=request.workspace_path,
                preferred_profile_id=request.preferred_profile_id,
            ),
        )

    def list_conversations(self, mode: ConversationMode | None = None) -> list[ConversationView]:
        return self.repository.list_conversations(mode=mode)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        return self.repository.get_conversation(conversation_id)

    async def send_conversation_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        conversation = self.repository.get_conversation(conversation_id)
        if conversation.mode is ConversationMode.CHAT:
            return await self._send_chat_turn(
                conversation_id,
                request,
                idempotency_key,
            )

        def act() -> ConversationTurnView:
            conversation = self.repository.get_conversation(conversation_id)
            workspace_path = request.workspace_path or conversation.workspace_path
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            project_id = request.project_id or conversation.project_id
            active_run_id = conversation.active_run_id
            tool: dict[str, Any] | None = None
            payload: dict[str, Any] = {}

            if conversation.mode is ConversationMode.CHAT:
                answer = self.answer_rsfmri_question(
                    RsFmriQuestionRequest(question=request.content, allow_remote_search=False)
                )
                payload = {"rag": answer.model_dump(mode="json")}
                tool = self._tool_result(
                    "rag_rsfmri_question",
                    {"question": request.content, "allow_remote_search": False},
                    payload,
                )
                assistant_content = answer.answer.answer
            else:
                action = self._resolve_work_action(request, workspace_path, active_run_id)
                try:
                    if action is ConversationAction.CHECK_WORKSPACE:
                        if workspace_path is None:
                            assistant_content = "请先点击“浏览”并选择工作区。"
                        else:
                            checked = self.check_workspace(
                                WorkspaceCheckRequest(path=workspace_path)
                            )
                            workspace_path = checked.path
                            payload = {"workspace_check": checked.model_dump(mode="json")}
                            tool = self._tool_result(
                                "check_workspace", {"path": workspace_path}, payload
                            )
                            if checked.blocking_issues:
                                assistant_content = (
                                    "工作区检查完成，发现 "
                                    f"{len(checked.blocking_issues)} 个阻断问题。"
                                    "请先按右侧列表修正，再准备预处理。"
                                )
                            else:
                                assistant_content = (
                                    "工作区检查通过：识别到 "
                                    f"{checked.functional_subject_count} 名受试者的功能输入。"
                                    "你可以继续准备已审核的预处理方案。"
                                )
                    elif action is ConversationAction.GET_PROGRESS:
                        if active_run_id is None:
                            assistant_content = "当前对话尚未启动运行。"
                        else:
                            run = self.get_run(active_run_id)
                            payload = {"run": run.model_dump(mode="json")}
                            tool = self._tool_result(
                                "get_run_progress", {"run_id": active_run_id}, payload
                            )
                            assistant_content = (
                                f"运行 {run.run_id[:8]} 当前状态为 {run.state.value}，"
                                f"已执行 {run.attempt} 次。"
                            )
                    elif action is ConversationAction.START_PREPROCESSING:
                        missing = [
                            name
                            for name, value in (
                                ("project_id", project_id),
                                ("plan_revision_id", request.plan_revision_id),
                                ("expected_plan_hash", request.expected_plan_hash),
                            )
                            if value is None
                        ]
                        if missing or not request.real_execution_confirmed:
                            tool = {
                                "tool_name": "start_dpabi_preprocessing",
                                "status": ConversationToolStatus.AWAITING_CONFIRMATION.value,
                                "input": {
                                    "workspace_path": workspace_path,
                                    "missing": missing,
                                },
                                "output": {},
                            }
                            assistant_content = (
                                "启动前需要已验证并审批的计划，以及本次真实 MATLAB/DPABI 运行确认。"
                                "确认后，任务会进入现有 Workflow/Worker 队列，"
                                "并在所选工作区原位生成 DPABI 结果目录。"
                            )
                        else:
                            assert project_id is not None
                            assert request.plan_revision_id is not None
                            assert request.expected_plan_hash is not None
                            project = self.repository.get_project(project_id)
                            plan = self.repository.get_plan(request.plan_revision_id)
                            if plan.project_id != project_id:
                                raise ConflictError(
                                    "cross_project_plan",
                                    "审批计划不属于当前项目。",
                                )
                            dataset_ref = plan.plan.get("skill_plan", {}).get("dataset_ref")
                            if not isinstance(dataset_ref, str):
                                raise InputValidationError(
                                    "preprocessing_skill_plan_required",
                                    "原位 DPABI 运行只接受已编译的预处理 SkillPlan。",
                                )
                            dataset = self.repository.get_dataset(dataset_ref)
                            bound_workspace = self.path_policy.validate_read_path(
                                dataset.source_path,
                                project_roots=project.source_roots,
                                expect_directory=True,
                            )
                            if workspace_path is not None:
                                selected_workspace = self.path_policy.validate_read_path(
                                    workspace_path,
                                    project_roots=project.source_roots,
                                    expect_directory=True,
                                )
                                if selected_workspace != bound_workspace:
                                    raise ConflictError(
                                        "conversation_workspace_plan_mismatch",
                                        "当前对话选择的工作区与审批计划绑定的数据集不一致。",
                                    )
                            workspace_path = str(bound_workspace)
                            run = self.create_run(
                                RunCreate(
                                    project_id=project_id,
                                    plan_revision_id=request.plan_revision_id,
                                    expected_plan_hash=request.expected_plan_hash,
                                    execution_backend=ExecutionBackend.MATLAB,
                                    workspace_mode=ExecutionWorkspaceMode.IN_PLACE,
                                    real_execution_confirmed=True,
                                ),
                                f"{idempotency_key}:run",
                            )
                            active_run_id = run.run_id
                            payload = {"run": run.model_dump(mode="json")}
                            tool = self._tool_result(
                                "start_dpabi_preprocessing",
                                {
                                    "project_id": project_id,
                                    "plan_revision_id": request.plan_revision_id,
                                    "workspace_mode": "in_place",
                                },
                                payload,
                            )
                            assistant_content = (
                                f"DPABI 任务已进入 Workflow/Worker 队列，运行 ID 为 {run.run_id}。"
                                "结果会写入该计划绑定的数据集工作区；可以继续询问运行进度。"
                            )
                    else:
                        assistant_content = (
                            "我已记录这条工作要求。你可以让我检查工作区、查看运行进度，"
                            "或在已有审核计划后明确启动 DPABI 预处理。"
                        )
                except ApplicationError as exc:
                    tool = {
                        "tool_name": self._tool_name(action),
                        "status": ConversationToolStatus.FAILED.value,
                        "input": {"workspace_path": workspace_path},
                        "output": {},
                        "error": exc.message,
                    }
                    assistant_content = exc.message

            stored, user_message, assistant_message, stored_tool = (
                self.repository.append_conversation_exchange(
                    conversation_id,
                    user_content=request.content,
                    assistant_content=assistant_content,
                    assistant_payload=payload,
                    tool=tool,
                    workspace_path=workspace_path,
                    preferred_profile_id=preferred_profile_id,
                    project_id=project_id,
                    active_run_id=active_run_id,
                )
            )
            return ConversationTurnView(
                conversation=stored,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_call=stored_tool,
            )

        return self._idempotent(
            scope=f"conversations:{conversation_id}:turns",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            action=act,
        )

    async def _send_chat_turn(
        self,
        conversation_id: str,
        request: ConversationTurnCreate,
        idempotency_key: str,
    ) -> ConversationTurnView:
        async def prepare() -> tuple[
            str,
            dict[str, Any],
            dict[str, Any],
            str | None,
        ]:
            conversation = self.repository.get_conversation(conversation_id)
            preferred_profile_id = (
                request.preferred_profile_id
                if request.preferred_profile_id is not None
                else conversation.preferred_profile_id
            )
            recent_messages = tuple(
                ContextMessage(role=item.role.value, content=item.content)
                for item in conversation.messages[-12:]
                if item.role.value in {"user", "assistant"}
            )
            try:
                result = await self.chat_agent.respond(
                    ChatAgentRequest(
                        session_id=conversation_id,
                        message=request.content,
                        stream=request.stream,
                        recent_messages=recent_messages,
                        preferred_profile_id=preferred_profile_id,
                        model=request.model,
                        allow_remote_search=request.allow_remote_search,
                    )
                )
            except ChatAgentError as exc:
                raise InputValidationError(exc.code, exc.message) from exc
            evidence = tuple(
                EvidenceChunk(
                    source=citation.source,
                    title=citation.title,
                    excerpt=citation.excerpt,
                    score=1.0,
                )
                for citation in result.citations
            )
            answer = RsFmriAnswer(
                in_scope=bool(result.metadata.get("in_scope", True)),
                answer=result.answer,
                evidence=evidence,
            )
            view = RsFmriAnswerView(
                answer=answer,
                remote_search_used=bool(result.metadata.get("remote_search_used", False)),
            )
            payload = {
                "rag": view.model_dump(mode="json"),
                "chat": result.model_dump(mode="json"),
            }
            model_metadata = result.metadata.get("model")
            if isinstance(model_metadata, dict):
                payload["model"] = model_metadata
                preferred_profile_id = str(model_metadata["profile_id"])
            tool_name = (
                "rsfmri_chat_llm"
                if result.metadata.get("llm_used")
                else "work_request_draft"
                if result.work_request is not None
                else "rag_rsfmri_question"
            )
            tool = self._tool_result(
                tool_name,
                {
                    "question": request.content,
                    "allow_remote_search": request.allow_remote_search,
                    "model_profile_id": preferred_profile_id,
                    "model": request.model,
                },
                payload,
            )
            return result.answer, payload, tool, preferred_profile_id

        def finalize(
            prepared: tuple[str, dict[str, Any], dict[str, Any], str | None]
        ) -> ConversationTurnView:
            assistant_content, payload, tool, preferred_profile_id = prepared
            conversation = self.repository.get_conversation(conversation_id)
            stored, user_message, assistant_message, stored_tool = (
                self.repository.append_conversation_exchange(
                    conversation_id,
                    user_content=request.content,
                    assistant_content=assistant_content,
                    assistant_payload=payload,
                    tool=tool,
                    workspace_path=conversation.workspace_path,
                    preferred_profile_id=preferred_profile_id,
                    project_id=conversation.project_id,
                    active_run_id=conversation.active_run_id,
                )
            )
            return ConversationTurnView(
                conversation=stored,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_call=stored_tool,
            )

        return await self._idempotent_async(
            scope=f"conversations:{conversation_id}:turns",
            key=idempotency_key,
            request=request,
            response_type=ConversationTurnView,
            prepare=prepare,
            finalize=finalize,
        )

    @staticmethod
    def _resolve_work_action(
        request: ConversationTurnCreate,
        workspace_path: str | None,
        active_run_id: str | None,
    ) -> ConversationAction:
        if request.action is not ConversationAction.AUTO:
            return request.action
        text = request.content.casefold()
        if any(word in text for word in ("进度", "状态", "运行到", "完成了吗")):
            return ConversationAction.GET_PROGRESS
        if any(word in text for word in ("启动", "运行", "执行", "开始预处理")):
            return ConversationAction.START_PREPROCESSING
        if workspace_path and any(word in text for word in ("检查", "扫描", "格式", "dpabi")):
            return ConversationAction.CHECK_WORKSPACE
        if workspace_path and active_run_id is None:
            return ConversationAction.CHECK_WORKSPACE
        return ConversationAction.AUTO

    @staticmethod
    def _tool_result(
        name: str, input_data: dict[str, Any], output: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "tool_name": name,
            "status": ConversationToolStatus.SUCCEEDED.value,
            "input": input_data,
            "output": output,
        }

    @staticmethod
    def _tool_name(action: ConversationAction) -> str:
        return {
            ConversationAction.CHECK_WORKSPACE: "check_workspace",
            ConversationAction.START_PREPROCESSING: "start_dpabi_preprocessing",
            ConversationAction.GET_PROGRESS: "get_run_progress",
            ConversationAction.AUTO: "work_router",
        }[action]
