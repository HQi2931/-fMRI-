"""Redacted, schema-checked access to configured model providers."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from neuroagent.agent.models import (
    AgentTaskRequest,
    GatewayResult,
    ModelCapability,
    ModelProfile,
    ProviderResponse,
    StructuredRecommendation,
)
from neuroagent.agent.providers import ModelProvider, ProviderError, RetryableProviderError
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.router import ModelRouter
from neuroagent.agent.secrets import SecretResolver
from neuroagent.agent.streaming import current_answer_stream
from neuroagent.agent.tool_spec import LlmTool
from neuroagent.observability.work_timings import measure_work_stage


class ModelGatewayError(RuntimeError):
    pass


class ModelGatewayTimeoutError(ModelGatewayError):
    timeout = True


@dataclass(frozen=True)
class ToolTurnResult:
    """One provider turn taken inside the bounded tool loop."""

    response: ProviderResponse
    selected_profile_id: str
    attempted_profile_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChatMessages:
    """Redacted chat payload rendered into provider-ready messages."""

    messages: list[dict[str, str]]
    payload: dict[str, object]
    context_hash: str
    redaction_count: int


_CHAT_SYSTEM_PROMPT = (
    "你是 rs-fMRI 科研文字分析助手。只回答 rs-fMRI、DPABI、SPM、"
    "影像统计设计和相关参数问题。优先使用给出的本地证据; "
    "证据不足时明确说明不确定性, 不得编造科研结论、参数默认值或引用。"
    "回答使用中文, 并区分通用方法信息与用户项目事实。"
    "面向用户只输出自然语言, 不要输出内部 ID、UUID、哈希、trace 或工具字段; "
    "只有用户明确要求时才解释这些内部信息的用途。"
)

_CHAT_CITATION_PROMPT = (
    "允许简短回应寒暄。历史消息用于理解追问, 不视为文献证据。"
    "引用本地证据时必须使用其 citation_id, 例如 [C1]; 只引用实际支持结论的片段。"
    "无本地证据时说明未找到文献依据, 一般解释不得冒充文献结论。"
)

_ROUTING_SYSTEM_PROMPT = (
    "根据当前问题与历史判断意图, 并将科研追问改写为独立检索问题。只返回 JSON: "
    '{"intent":"knowledge_query|conversation|work_request","query":"独立检索问题",'
    '"work_request":null}。执行需求的 work_request 为 {"task":"任务名",'
    '"parameters":{}}。只生成草案; 方法咨询不是执行请求。'
    "不要回答问题, 不要编造历史未提供的信息。"
    "查找方案、检索文献、解释步骤及否定执行都是知识咨询；"
    "提到预处理或运行不等于请求执行。只有明确要求实际处理数据才是 work_request。"
)

# The Work model may only name these actions.  This tuple is duplicated from
# ``neuroagent.application.work_actions`` deliberately: the gateway must not
# import the application layer, so a test asserts the two stay equal.
_WORK_ACTIONS = (
    "conversation",
    "get_progress",
    "get_qc_status",
    "get_statistical_results",
    "prepare_preprocessing_plan",
    "propose_workspace_changes",
    "propose_run_confirmation",
)

_WORK_SYSTEM_PROMPT = (
    "你是 rs-fMRI Work 对话助手，采用 Plan-and-Execute 工作方式："
    "先理解目标、核实输入并提出可审查方案，用户确认后由服务端编译和执行。"
    "理解用户当前意图和历史追问，使用中文自然回应。"
    "常规 answer 简短说明本轮完成、本轮变化或少量必要问题，约150至300汉字。"
    "完整参数与执行流程由服务端方案区域展示，不在 answer 重复参数清单、字段路径、"
    "空配置、哈希或技术口令；方法解释按用户需求展开。"
    "只返回 JSON 对象：answer（非空中文回答），action（"
    + "、".join(_WORK_ACTIONS)
    + " 之一），draft_patch（对象，默认空），workspace_citations（数组，默认空），"
    "workspace_edits（数组，默认空），recommended_input_stage（字符串或 null），"
    "stage_intent（字符串或 null），stage_reasoning（字符串或 null），"
    "stage_questions（字符串数组，默认空），questions（最多五个待决定问题，默认空；"
    "推荐使用对象{text:简短中文问题,parameter_paths:相关草稿参数路径数组}），"
    "recommendation_reason（简短中文推荐理由或 null）。"
    "规划时将需要用户回答的问题放入 questions，不只藏在 answer。"
    "已经明确的指标与参数不要重复询问，数据事实由文件证据核实，不询问是否接受真实数值。"
    "方法查询、寒暄、帮助、单纯取消或否定操作应选 conversation，不要按词语触发操作。"
    "用户要求计算指标、制定方案或修改参数时，选 prepare_preprocessing_plan；"
    "“先给方案，不要执行”仍是规划请求，不是运行授权。"
    "你只提出草稿，不直接编写或执行自由 MATLAB/Shell 命令，不宣称已批准、登记或执行。"
    "只有本轮工具结果或服务端上下文提供证据时，才能描述已核实的数据和状态。"
    "规划分为数据事实、流程建议、参数建议、待确认项四部分。"
    "先结合用户目标、work_context.parameter_draft、工作区阶段候选和可用工具结果，"
    "识别原始输入或已有预处理检查点；未读取的数据保持未知。"
    "work_context.planning_decisions 是服务端生成的草稿决策摘要；"
    "missing 表示缺项，conflict 表示草稿校验冲突，proposed 是尚未核实的候选值，"
    "user_draft 只表示用户编辑过草稿，不等于采集元数据已验证。"
    "可使用 work_context.workspace_data_profile 中的匿名计数、数据类型、问题和警告，"
    "但不能从匿名计数反推出 TR、时间点数、层数或采集顺序。"
    "TR、时间点数、层数、采集层顺序和已有处理记录只能来自明确用户输入或可追溯数据证据，"
    "不能根据文件名、目录后缀或常见采集习惯猜测。目录阶段候选不等于完整处理谱系。"
    "不要只返回一长串待填字段；先给出当前证据支持的流程草案和参数建议，"
    "再集中询问最影响方案的少量缺项。缺项存在时仍可规划，但不得声称方案可执行。"
    "参数建议优先依据用户研究协议、已审核 Skill 约束和可追溯方法证据。"
    "有明确依据的候选值可写入增量 draft_patch，并在 answer 中逐项说明值、理由和依据，"
    "明确标记为“建议，待用户审查”，不能称为用户已选择或已经核验的科研默认值。"
    "没有可靠依据时给出可选方案和取舍并追问，不捏造引用、研究协议、扫描参数或验证结论。"
    "完整字段结构见 work_context.draft_schema，只输出该 schema 允许的增量字段，"
    "流程和推荐理由写入 answer，不新增 steps、plan 或 recommendation 等 JSON 字段。"
    "保留历史明确选择，建议不能静默覆盖已有参数；用户修改时说明本轮改动及影响。"
    "只为用户明确要求的指标建草稿：要求 ALFF 不等于要求 fALFF；"
    "用户指定 Segment 时必须用 segmentation=segment、dartel=false、normalization.mode=t1_segment，"
    "不得改成 New Segment 或 DARTEL。"
    "使用语义选择：normalization.mode 为 disabled/epi_template/t1_segment/dartel，"
    "nuisance.head_motion_model 可为 friston_24，服务端确定性映射到 DPABI。"
    "不要把助手建议当成用户确认；只有服务端校验的用户确认才能生成参数来源记录。"
    "流程按依赖和指标消费的中间检查点规划，不能让所有指标都读取同一个最终预处理文件。"
    "当前项目 ALFF/fALFF Skill 消费未带通滤波、未空间平滑的合格检查点；"
    "ReHo 消费未空间平滑的时序，可按确认协议使用后续时间滤波检查点，"
    "需要平滑时对 ReHo 结果图执行，避免 ReHo 专用平滑与全局结果平滑重复。"
    "组合 ALFF/fALFF 与 ReHo 时，说明共享步骤与各自分支，保留 ALFF/fALFF 的滤波前输入；"
    "这属于当前 Skill 合同，不能宣称是所有研究的唯一方法，也不能自行重排受控执行步骤。"
    "当前指标运行要求公共预处理 smoothing 停用，结果平滑通过指标参数声明；"
    "公共 temporal_filter 与指标 filter_timing/temporal_filter_band 必须保持一致，"
    "具体兼容性以服务端校验和编译结果为准。"
    "T1 分割和 DARTEL 需要显式 t1 参数及冻结结构像输入。"
    "分割与标准化分别选择，DARTEL 必须使用 New Segment；"
    "T1 重定向裁剪、配准、分割和 DARTEL 属于同一次预处理，程序检查输入及依赖，"
    "不要求中途人工批准。真实 MATLAB 验证状态以服务端能力证据为准。"
    "结构像输入可使用服务端符号绑定 workspace:anatomical-source；它在确认后绑定当前冻结清单，"
    "不得捏造实际 Artifact ID。"
    "不能为使方案可运行而擅自改成另一种标准化方法。"
    "answer 简洁展示输入事实与未知项、拟用步骤和分支、关键参数及来源、"
    "预期产物与 QC 选择、限制和下一步审查事项；不虚构输出路径、运行编号或预计耗时。"
    "需要分析本地文件时先使用可用的工作区只读工具；文件结论必须给出相对路径和定位。"
    "当用户描述预处理已经完成的步骤时，先调用 list_workspace_stages，结合服务端候选阶段理解意图，"
    "只能从候选中提出 recommended_input_stage；不能把建议当作阶段确认。"
    "需要修改文件时选 propose_workspace_changes，只生成可审查补丁，绝不自行写入文件。"
    "workspace_citations 使用 {path, locator, quote?}；locator 必须来自读取结果"
    "（如 line 12、page 2、json_pointer /x、sheet/row）。"
    "path 必须逐字复制本轮读取结果的 path，匿名句柄不得恢复原名或补扩展名；"
    "NIfTI 头信息使用读取结果的 section 定位值。运行状态与产物列表不是文件引用，"
    "不得把 run_id、artifact_id 或状态工具名填入 workspace_citations。"
    "workspace_edits 使用 {path, expected_sha256, start_line, end_line, replacement}；"
    "新文件省略 expected_sha256/start_line/end_line 并提供完整 replacement。"
    "draft_patch 不得包含 provenance、ID、路径、授权、哈希或版本号。"
    "关闭某个步骤时按 schema 明确 enabled=false 及其不适用参数 null。"
    "节省输出：只返回 answer、action 和本轮实际修改的 draft_patch；"
    "其余字段没有内容时省略，不复述 schema、不输出整份草稿或默认空字段。"
    "answer 尽量不超过 800 个汉字；完整参数、步骤和产物由服务端审查面板展示。"
    "系统会单独展示并要求确认草稿，随后才校验编译；参数确认不是运行授权。"
    "用户明确要求真正开始任务时选 propose_run_confirmation，"
    "它只会让服务端展示需要用户本人回复的确认指令；"
    "不能用于询问、否定或条件讨论，也不能自行授予授权。"
    "实际授权由服务端检查。只依赖给定服务端状态描述任务进度。"
)

_SUMMARY_SYSTEM_PROMPT = (
    "将较早的对话压缩为简洁中文摘要, 保留用户目标、已明确偏好、已作决定和"
    '未解决问题。不要添加新事实。只返回 JSON: {"summary":"..."}。'
)

_MEMORY_SYSTEM_PROMPT = (
    "Extract only durable user preferences, instructions, project facts, decisions, "
    "and explicitly chosen scientific parameters from the current user message. "
    "Return JSON with candidates, an array of objects containing kind "
    "(preference|instruction|scientific_parameter|project_fact|decision), key "
    "(stable lowercase identifier), "
    "content, scope (conversation|project), confidence (0..1), importance (0..1). "
    "Do not infer facts, copy questions, store secrets or identifiers, or treat "
    "assistant text as user memory. Use project scope only when the user explicitly "
    "asks for project-wide "
    "reuse. Return an empty array when nothing should persist."
)


@dataclass(frozen=True)
class ChatGatewayResult:
    response: ProviderResponse
    selected_profile_id: str
    context_hash: str
    attempted_profile_ids: tuple[str, ...]
    remote_search_used: bool
    redaction_count: int


class ModelGateway:
    _system_prompt = (
        "You assist a research workflow. Return one JSON object with these fields: "
        "summary (string), proposed_skill_request (object or null), warnings (string array), "
        "unresolved_questions (string array), requires_user_confirmation (boolean), and for "
        "workspace format advice optionally expected_layout (string array), adjustment_steps "
        "(string array), recommended_target_stage (a single safe DPABI stage name such as "
        "FunRaw, FunImgAR, FunImgARW, FunImgARWS, or null). For Work preprocessing-stage intent, "
        "return recommended_input_stage (only from the server-provided workspace stage "
        "candidates), stage_intent, stage_reasoning, and stage_questions. You may only "
        "recommend a stage; the user must confirm it with the server-issued command. "
        "Never emit commands, absolute paths, raw image content, tool calls, workflow "
        "transitions, or invented scientific defaults."
    )

    def __init__(
        self,
        router: ModelRouter,
        providers: Mapping[str, ModelProvider],
        outbound_policy: OutboundContextPolicy,
        secret_resolver: SecretResolver,
    ) -> None:
        self._router = router
        self._providers = providers
        self._outbound_policy = outbound_policy
        self._secret_resolver = secret_resolver

    async def generate(self, request: AgentTaskRequest) -> GatewayResult:
        context = self._outbound_policy.redact(request.summary.model_dump(mode="json"))
        candidates = self._router.candidates(request)
        attempted: list[str] = []
        last_retryable: Exception | None = None
        for profile in candidates:
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            api_key = self._secret_resolver.resolve(profile.api_key_env)
            if not api_key:
                continue
            attempted.append(profile.id)
            try:
                recommendation, response = await self._request_structured(
                    provider, profile, api_key, context.payload
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            decision = self._router.decision(request, candidates, profile)
            return GatewayResult(
                recommendation=recommendation,
                routing=decision,
                context_hash=context.context_hash,
                attempted_profile_ids=tuple(attempted),
                model=response.model,
                redaction_count=context.redaction_count,
            )
        if last_retryable:
            if getattr(last_retryable, "timeout", False):
                raise ModelGatewayTimeoutError(
                    "all providers exceeded their request deadlines"
                ) from last_retryable
            raise ModelGatewayError(
                "all available providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError("no routed provider has both an adapter and a configured API key")

    async def generate_chat(
        self,
        *,
        question: str,
        evidence: list[dict[str, object]],
        preferred_profile_id: str | None,
        model: str | None,
        allow_web_search: bool,
        recent_messages: list[dict[str, object]] | None = None,
        pinned_context: list[dict[str, object]] | None = None,
        conversation_summary: str | None = None,
        work_context: dict[str, object] | None = None,
        routing: bool = False,
        summary_mode: bool = False,
        memory_mode: bool = False,
        work_mode: bool = False,
    ) -> ChatGatewayResult:
        rendered = self.build_chat_messages(
            question=question,
            evidence=evidence,
            recent_messages=recent_messages,
            pinned_context=pinned_context,
            conversation_summary=conversation_summary,
            work_context=work_context,
            routing=routing,
            summary_mode=summary_mode,
            memory_mode=memory_mode,
            work_mode=work_mode,
            allow_web_search=allow_web_search,
        )
        messages = rendered.messages
        profiles = list(self._router.profiles.values())
        if preferred_profile_id is not None:
            profiles = [profile for profile in profiles if profile.id == preferred_profile_id]
        else:
            profiles.sort(key=lambda item: item.priority)
        if allow_web_search:
            from neuroagent.agent.models import ModelCapability

            profiles = [
                profile
                for profile in profiles
                if ModelCapability.WEB_SEARCH in profile.capabilities
            ]
        if model is not None:
            profiles = [profile.model_copy(update={"model": model}) for profile in profiles]
        if not profiles:
            requirement = "支持联网搜索的" if allow_web_search else "可用的"
            raise ModelGatewayError(f"no {requirement} model profile is configured")

        system_prompt = _CHAT_SYSTEM_PROMPT + _CHAT_CITATION_PROMPT
        if routing:
            system_prompt = _ROUTING_SYSTEM_PROMPT
        elif work_mode:
            system_prompt = _WORK_SYSTEM_PROMPT
        elif summary_mode:
            system_prompt = _SUMMARY_SYSTEM_PROMPT
        elif memory_mode:
            system_prompt = _MEMORY_SYSTEM_PROMPT
        if allow_web_search:
            system_prompt += (
                "本次允许使用联网搜索。仅引用与问题直接相关的公开来源, 并在结论旁保留来源引用。"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(rendered.payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        attempted: list[str] = []
        last_retryable: Exception | None = None
        for profile in profiles:
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            api_key = self._secret_resolver.resolve(profile.api_key_env)
            if not api_key:
                continue
            attempted.append(profile.id)
            try:
                response = await self._generate_visible(
                    provider,
                    profile,
                    api_key,
                    messages,
                    web_search=allow_web_search,
                    json_object=routing or summary_mode or memory_mode or work_mode,
                    visible=not (routing or summary_mode or memory_mode),
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            return ChatGatewayResult(
                response=response,
                selected_profile_id=profile.id,
                context_hash=rendered.context_hash,
                attempted_profile_ids=tuple(attempted),
                remote_search_used=allow_web_search,
                redaction_count=rendered.redaction_count,
            )
        if last_retryable:
            raise ModelGatewayError(
                "all available chat providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError(
            "no routed chat provider has both an adapter and a configured API key"
        )

    def build_chat_messages(
        self,
        *,
        question: str,
        evidence: list[dict[str, object]],
        preferred_profile_id: str | None = None,
        model: str | None = None,
        allow_web_search: bool = False,
        recent_messages: list[dict[str, object]] | None = None,
        pinned_context: list[dict[str, object]] | None = None,
        conversation_summary: str | None = None,
        work_context: dict[str, object] | None = None,
        routing: bool = False,
        summary_mode: bool = False,
        memory_mode: bool = False,
        work_mode: bool = False,
    ) -> ChatMessages:
        """Redact the chat payload and render it into provider messages.

        Both the single-shot path and the bounded tool loop use this so a tool
        turn can never be sent with a weaker prompt or weaker redaction.
        """

        del preferred_profile_id, model
        context = self._outbound_policy.redact(
            {
                "question": question,
                "local_evidence": evidence,
                "recent_messages": recent_messages or [],
                "pinned_context": pinned_context or [],
                "conversation_summary": conversation_summary,
                "work_context": work_context or {},
            }
        )
        system_prompt = _CHAT_SYSTEM_PROMPT + _CHAT_CITATION_PROMPT
        if routing:
            system_prompt = _ROUTING_SYSTEM_PROMPT
        elif work_mode:
            system_prompt = _WORK_SYSTEM_PROMPT
        elif summary_mode:
            system_prompt = _SUMMARY_SYSTEM_PROMPT
        elif memory_mode:
            system_prompt = _MEMORY_SYSTEM_PROMPT
        if allow_web_search:
            system_prompt += (
                "本次允许使用联网搜索。仅引用与问题直接相关的公开来源, 并在结论旁保留来源引用。"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(context.payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        return ChatMessages(
            messages=messages,
            payload=dict(context.payload),
            context_hash=context.context_hash,
            redaction_count=context.redaction_count,
        )

    async def tool_capable(self, preferred_profile_id: str | None) -> bool:
        """Whether any usable profile declares tool-calling support."""

        return bool(self._tool_profiles(preferred_profile_id, None))

    async def generate_tool_turn(
        self,
        *,
        messages: Sequence[dict[str, object]],
        tools: Sequence[LlmTool],
        preferred_profile_id: str | None,
        model: str | None,
        remaining_seconds: float | None = None,
    ) -> ToolTurnResult:
        """One raw tool-calling turn, used by the bounded Agent loop.

        The caller owns the message transcript and the catalog, so both are
        already redacted and already restricted to model-visible tools.
        """

        if not tools:
            raise ModelGatewayError("a tool turn requires at least one model-visible tool")
        profiles = self._tool_profiles(preferred_profile_id, model)
        attempted: list[str] = []
        last_retryable: Exception | None = None
        for profile in profiles:
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            api_key = self._secret_resolver.resolve(profile.api_key_env)
            if not api_key:
                continue
            attempted.append(profile.id)
            try:
                response = await self._generate_visible(
                    provider,
                    profile,
                    api_key,
                    messages,
                    tools=tools,
                    tool_choice="auto",
                    # A tool-calling turn must not also demand JSON mode: the model
                    # has to be free to answer with tool_calls instead of a JSON
                    # object, and several OpenAI-compatible gateways reject the
                    # combination outright.
                    json_object=False,
                    timeout_seconds=(
                        min(profile.timeout_seconds, remaining_seconds)
                        if remaining_seconds is not None
                        else None
                    ),
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            return ToolTurnResult(
                response=response,
                selected_profile_id=profile.id,
                attempted_profile_ids=tuple(attempted),
            )
        if last_retryable:
            if getattr(last_retryable, "timeout", False):
                raise ModelGatewayTimeoutError(
                    "all tool-use providers exceeded their request deadlines"
                ) from last_retryable
            raise ModelGatewayError(
                "all available tool-use providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError("no tool-use provider has both an adapter and a configured API key")

    async def _generate_visible(
        self,
        provider: ModelProvider,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, object]],
        *,
        visible: bool = True,
        **options: object,
    ) -> ProviderResponse:
        observer = current_answer_stream() if visible else None
        if observer is not None:
            await observer.reset()
            stream = getattr(provider, "generate_stream", None)
            if stream is not None and ModelCapability.STREAMING in profile.capabilities:
                with measure_work_stage("model_response"):
                    response: ProviderResponse = await stream(
                        profile, api_key, messages, on_text=observer.text, **options
                    )
                if response.tool_calls:
                    await observer.reset()
                return response
        with measure_work_stage("model_response"):
            response = await provider.generate(profile, api_key, messages, **options)  # type: ignore[arg-type]
        if observer is not None and not response.tool_calls:
            await observer.text(response.content)
        return response

    def _tool_profiles(
        self, preferred_profile_id: str | None, model: str | None
    ) -> list[ModelProfile]:
        profiles = [
            profile
            for profile in self._router.profiles.values()
            if ModelCapability.TOOL_USE in profile.capabilities
        ]
        if preferred_profile_id is not None:
            profiles = [profile for profile in profiles if profile.id == preferred_profile_id]
        else:
            profiles.sort(key=lambda item: item.priority)
        if model is not None:
            profiles = [profile.model_copy(update={"model": model}) for profile in profiles]
        return profiles

    async def _request_structured(
        self,
        provider: ModelProvider,
        profile: ModelProfile,
        api_key: str,
        payload: Mapping[str, object],
    ) -> tuple[StructuredRecommendation, ProviderResponse]:
        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        response = await provider.generate(profile, api_key, messages)
        try:
            return StructuredRecommendation.model_validate_json(response.content), response
        except ValidationError:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": response.content[:12_000]},
                {
                    "role": "user",
                    "content": (
                        "Repair the previous response. Return only one valid JSON object "
                        "matching the required schema."
                    ),
                },
            ]
            repaired = await provider.generate(profile, api_key, repair_messages)
            try:
                return StructuredRecommendation.model_validate_json(repaired.content), repaired
            except ValidationError as exc:
                raise ProviderError(
                    "provider output failed schema validation after one repair"
                ) from exc
