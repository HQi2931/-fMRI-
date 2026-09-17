"""Versioned conversation cards and a closed application-operation catalog."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from neuroagent.application import contracts as c
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.chat.models import RoutedIntent

# name -> (service method, request schema, positional target getter, idempotent)
OPERATIONS: dict[str, tuple[str, type[BaseModel], str | None, bool]] = {
    "createProject": ("create_project", c.ProjectCreate, None, True),
    "createDataset": ("create_dataset", c.DatasetCreate, "get_project", True),
    "inspectDataset": ("inspect_dataset", c.ManifestScanRequest, "get_dataset", True),
    "importDemographics": ("import_demographics", c.DemographicsImportRequest, "get_dataset", True),
    "createSplit": ("create_split", c.DatasetSplitCreate, "get_dataset", True),
    "resolveSkillPlan": ("resolve_skill_plan", c.SkillPlanResolveRequest, None, True),
    "approvePlan": ("approve_plan", c.ApprovalCreate, "get_plan", True),
    "createRun": ("create_run", c.RunCreate, None, True),
    "cancelRun": ("cancel_run", c.RunAction, "get_run", True),
    "retryRun": ("retry_run", c.RunAction, "get_run", True),
    "diagnoseRun": ("diagnose_run", c.RunDiagnosisRequest, "get_run", False),
    "createQcReview": ("create_qc_review", c.QcReviewCreate, None, True),
    "approveQcReview": ("approve_qc_review", c.QcReviewApprove, "get_qc_review", True),
    "createStatisticalDesign": ("create_statistical_design", c.StatisticalDesignCreate, None, True),
    "validateStatisticalDesign": (
        "validate_statistical_design",
        c.StatisticalDesignValidationRequest,
        "get_plan",
        True,
    ),
    "createStatisticsRun": ("create_statistics_run", c.StatisticsRunCreate, None, True),
    "inspectMlTable": ("inspect_ml_table", c.MlTableInspectRequest, None, False),
    "createMlTemplate": ("create_ml_template", c.MlTemplateCreateRequest, None, False),
    "validateRoiTable": ("validate_roi_table", c.RoiTableCreateRequest, None, False),
    "localizeClusters": ("localize_clusters", c.ClusterLocalizationRequest, None, False),
    "answerRsFmriQuestion": ("answer_rsfmri_question", c.RsFmriQuestionRequest, None, False),
    "organizationPreview": ("organization_preview", c.OrganizationPreviewRequest, None, False),
}
CATALOG: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "project": ("项目与工作区", (), ("selectProject", "createProject")),
    "data": (
        "数据登记与清单",
        ("project_id",),
        (
            "createProject",
            "createDataset",
            "inspectDataset",
            "importDemographics",
            "createSplit",
            "organizationPreview",
        ),
    ),
    "plan": (
        "预处理与指标方案",
        ("project_id", "dataset_id", "manifest_id"),
        ("resolveSkillPlan", "approvePlan"),
    ),
    "runs": ("运行与结果", ("project_id",), ("createRun", "cancelRun", "retryRun", "diagnoseRun")),
    "qc": ("质量控制审核", ("project_id", "run_id"), ("createQcReview", "approveQcReview")),
    "statistics": (
        "统计设计与结果",
        ("project_id",),
        (
            "createStatisticalDesign",
            "validateStatisticalDesign",
            "approvePlan",
            "createStatisticsRun",
        ),
    ),
    "analysis": (
        "扩展分析",
        (),
        (
            "inspectMlTable",
            "createMlTemplate",
            "validateRoiTable",
            "localizeClusters",
            "answerRsFmriQuestion",
            "organizationPreview",
            "diagnoseRun",
        ),
    ),
    "settings": ("环境与模型设置", (), ()),
}
WORDS = {
    "settings": ("设置", "环境配置", "模型连接", "配置环境"),
    "data": ("数据登记", "数据集", "清单", "人口学", "数据划分", "导入数据"),
    "plan": ("方案", "预处理", "alff", "falff", "reho"),
    "qc": ("qc", "质控", "质量控制"),
    "statistics": ("统计", "t检验", "t 检验", "显著簇"),
    "runs": ("进度", "日志", "取消", "重试", "产物", "报告", "运行", "启动"),
    "analysis": ("扩展分析", "机器学习", "roi", "脑区定位", "聚类定位"),
    "project": ("项目", "工作区"),
}


def latest_cards(conversation: c.ConversationView) -> dict[str, c.WorkCard]:
    cards: dict[str, c.WorkCard] = {}
    for message in conversation.messages:
        for raw in message.payload.get("work_cards", []):
            card = c.WorkCard.model_validate(raw)
            cards[card.card_id] = card
    return cards


def current_bindings(conversation: c.ConversationView) -> c.WorkCardBindings:
    binding = c.WorkCardBindings(
        project_id=conversation.project_id, run_id=conversation.active_run_id
    )
    for card in latest_cards(conversation).values():
        if card.bindings.project_id == conversation.project_id:
            binding = binding.model_copy(
                update={k: v for k, v in card.bindings.model_dump().items() if v is not None}
            )
    binding.run_id = conversation.active_run_id or binding.run_id
    return binding


class WorkCardsMixin(BaseServiceMixin):
    def work_capabilities(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": kind,
                "title": title,
                "required_context": list(required),
                "allowed_operations": ["saveDraft", *operations],
            }
            for kind, (title, required, operations) in CATALOG.items()
        ]

    async def route_work_card(
        self, conversation: c.ConversationView, request: c.ConversationTurnCreate
    ) -> WorkActionResult:
        text = request.content.strip().casefold()
        explaining = bool(re.search(r"如何|怎么|为什么|解释|介绍|是什么|how |what |why ", text))
        kind = request.card_kind
        route_source = "explicit" if kind else "local"
        matches = [key for key, words in WORDS.items() if any(word in text for word in words)]
        if kind is None and explaining:
            answer = self.answer_rsfmri_question(  # type: ignore[attr-defined]
                c.RsFmriQuestionRequest(question=request.content)
            )
            return WorkActionResult(
                answer.answer.answer,
                payload={
                    "work_cards": [],
                    "capabilities": self.work_capabilities(),
                    "route_intent": "explain",
                },
                workspace_path=conversation.workspace_path,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
        if kind is None and conversation.preferred_profile_id:
            try:
                result = await self._generate_rsfmri_chat(  # type: ignore[attr-defined]
                    question=request.content,
                    evidence=[],
                    preferred_profile_id=conversation.preferred_profile_id,
                    model=request.model,
                    allow_web_search=False,
                    routing=True,
                    work_context={
                        "capabilities": self.work_capabilities(),
                        "instruction": (
                            "For a work request, task must be one of the capability "
                            "kind values; return only a draft. Never execute."
                        ),
                    },
                )
                routed = RoutedIntent.model_validate(json.loads(result.response.content))
                if routed.work_request and routed.work_request.task in CATALOG:
                    kind = cast(c.WorkCardKind, routed.work_request.task)
                    route_source = "model"
            except (ApplicationError, ValidationError, ValueError):
                route_source = "local_fallback"
        if kind is None and matches:
            # Preparatory scientific requests take precedence over generic run words.
            kind = cast(c.WorkCardKind, matches[0])
        if kind is None:
            return WorkActionResult(
                "请选择要准备的操作：项目、数据、方案、运行、质控、统计或扩展分析。",
                payload={
                    "work_cards": [],
                    "capabilities": self.work_capabilities(),
                    "route_intent": "clarify",
                },
                workspace_path=conversation.workspace_path,
                project_id=conversation.project_id,
                active_run_id=conversation.active_run_id,
            )
        title, required, operations = CATALOG[kind]
        binding = current_bindings(conversation)
        card_id = str(uuid4())
        card = c.WorkCard(
            card_id=card_id,
            kind=kind,
            title=title,
            draft_ref=card_id,
            bindings=binding,
            allowed_operations=["saveDraft", *operations],
        )
        missing = [name for name in required if getattr(binding, name) is None]
        message = (
            "环境与模型连接请在右上角设置中管理。"
            if kind == "settings"
            else f"已打开{title}。请在卡片中填写和确认操作。"
        )
        if missing:
            message += " 当前缺少关联信息，请先完成项目或数据选择。"
        return WorkActionResult(
            message,
            payload={
                "work_cards": [card.model_dump(mode="json")],
                "route_intent": "prepare",
                "route_source": route_source,
                "missing_context": missing,
                "remaining_capabilities": [item for item in matches if item != kind],
            },
            workspace_path=conversation.workspace_path,
            project_id=conversation.project_id,
            active_run_id=conversation.active_run_id,
        )

    def act_on_work_card(
        self, conversation_id: str, card_id: str, request: c.WorkCardAction, idempotency_key: str
    ) -> c.WorkCardActionView:
        def action() -> c.WorkCardActionView:
            conversation = self.repository.get_conversation(conversation_id)
            if conversation.mode != c.ConversationMode.WORK:
                raise InputValidationError("work_mode_required", "卡片操作仅适用于 Work 对话。")
            card = latest_cards(conversation).get(card_id)
            if card is None:
                raise InputValidationError("work_card_not_found", "当前对话中找不到这张卡片。")
            if card.version != request.expected_version:
                raise ConflictError("work_card_stale", "卡片已经更新，请刷新后重新确认。")
            if request.operation not in card.allowed_operations:
                raise InputValidationError("work_card_operation_rejected", "该操作不属于当前卡片。")
            if card.bindings.project_id != conversation.project_id and request.operation not in (
                "selectProject",
                "createProject",
            ):
                raise ConflictError("work_card_project_changed", "项目已切换，请重新打开操作卡片。")
            result: Any = None
            workspace_path = conversation.workspace_path
            binding = card.bindings.model_copy()
            if request.operation == "saveDraft":
                if len(request.args) != 1 or not isinstance(request.args[0], dict):
                    raise InputValidationError("invalid_card_draft", "草稿必须是一个字段对象。")
                if len(json.dumps(request.args[0], ensure_ascii=False)) > 100_000:
                    raise InputValidationError("card_draft_too_large", "草稿内容过大。")
                card.draft = request.args[0]
            elif request.operation == "selectProject":
                if len(request.args) != 1 or not isinstance(request.args[0], str):
                    raise InputValidationError("invalid_card_arguments", "请选择项目。")
                project = self.get_project(request.args[0])  # type: ignore[attr-defined]
                binding = c.WorkCardBindings(project_id=project.project_id)
                workspace_path = project.source_roots[0] if project.source_roots else None
                result = project.model_dump(mode="json")
            else:
                method, schema, getter, idempotent = OPERATIONS[request.operation]
                if len(request.args) != (2 if getter else 1):
                    raise InputValidationError("invalid_card_arguments", "卡片操作参数数量不正确。")
                try:
                    body = schema.model_validate(request.args[-1])
                except ValidationError as exc:
                    raise InputValidationError(
                        "invalid_card_arguments",
                        "卡片参数未通过校验。",
                        errors=exc.errors(include_context=False),
                    ) from exc
                target = None
                if getter:
                    if not isinstance(request.args[0], str):
                        raise InputValidationError(
                            "invalid_card_target", "关联对象标识必须是字符串。"
                        )
                    target = getattr(self, getter)(request.args[0])
                    self._check_card_object(binding, target)
                self._check_card_body(binding, body.model_dump(mode="json"))
                call_args = ([request.args[0]] if getter else []) + [body]
                if idempotent:
                    call_args.append(f"{idempotency_key}:operation")
                response = getattr(self, method)(*call_args)
                result = response.model_dump(mode="json")
                if request.operation == "createProject":
                    binding = c.WorkCardBindings(project_id=response.project_id)
                    workspace_path = response.source_roots[0] if response.source_roots else None
                elif request.operation == "createDataset":
                    binding.dataset_id = response.dataset_id
                    binding.manifest_id = None
                    binding.plan_revision_id = None
                    binding.run_id = None
                    workspace_path = response.source_path
                else:
                    for field in (
                        "project_id",
                        "dataset_id",
                        "manifest_id",
                        "plan_revision_id",
                        "run_id",
                    ):
                        value = result.get(field)
                        if value is not None:
                            setattr(binding, field, value)
                    if isinstance(result.get("plan_revision"), dict):
                        binding.plan_revision_id = result["plan_revision"]["plan_revision_id"]
                        if request.operation in (
                            "createStatisticalDesign",
                            "validateStatisticalDesign",
                        ):
                            binding.statistical_design_id = binding.plan_revision_id
                    if isinstance(result.get("review"), dict):
                        binding.qc_review_id = result["review"]["review_revision_id"]
            card.bindings = binding
            card.version += 1
            if request.operation == "saveDraft":
                stored = self.repository.save_work_card_draft(
                    conversation_id, card.model_dump(mode="json")
                )
                return c.WorkCardActionView(card=card, conversation=stored)
            stored, _, _, _ = self.repository.append_conversation_exchange(
                conversation_id,
                user_content=f"{card.title}：{request.operation}",
                assistant_content=f"{card.title}操作已完成。",
                assistant_payload={
                    "work_cards": [card.model_dump(mode="json")],
                    "card_operation": request.operation,
                    "operation_result": result,
                },
                tool=None,
                workspace_path=workspace_path,
                preferred_profile_id=conversation.preferred_profile_id,
                project_id=binding.project_id,
                active_run_id=binding.run_id,
            )
            return c.WorkCardActionView(card=card, conversation=stored, result=result)

        return self._idempotent(
            scope=f"conversations:{conversation_id}:cards:{card_id}",
            key=idempotency_key,
            request=request,
            response_type=c.WorkCardActionView,
            action=action,
        )

    @staticmethod
    def _check_card_object(binding: c.WorkCardBindings, obj: Any) -> None:
        project_id = getattr(obj, "project_id", None)
        if binding.project_id is None or project_id != binding.project_id:
            raise ConflictError("work_card_cross_project", "关联对象不属于当前卡片项目。")

    def _check_card_body(self, binding: c.WorkCardBindings, body: dict[str, Any]) -> None:
        for key, value in body.items():
            if isinstance(value, dict):
                self._check_card_body(binding, value)
            elif key == "project_id" and value != binding.project_id:
                raise ConflictError("work_card_cross_project", "提交内容不属于当前卡片项目。")
            elif value and key in (
                "dataset_ref",
                "dataset_id",
                "plan_revision_id",
                "run_id",
                "qc_review_id",
            ):
                getter = {
                    "dataset_ref": "get_dataset",
                    "dataset_id": "get_dataset",
                    "plan_revision_id": "get_plan",
                    "run_id": "get_run",
                    "qc_review_id": "get_qc_review",
                }[key]
                self._check_card_object(binding, getattr(self, getter)(value))
