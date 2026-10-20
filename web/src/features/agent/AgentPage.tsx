import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  describeError,
  ApiError,
} from "../../api/transport";
import type {
  Conversation,
  CompositeGoalProgress,
  ConversationMode,
  Run,
  WorkspaceConsent,
  WorkspaceCheck,
  WorkspaceProposal,
} from "../../api/types";
import { agentApi } from "../../api/agent";
import { environmentApi } from "../../api/environment";
import { runsApi } from "../../api/runs";
import { workspacesApi } from "../../api/workspaces";
import { Feedback } from "../../components/Ui";
import { ConfirmationDialog } from "../../components/ConfirmationDialog";
import { WorkReviewRegion } from "../../components/WorkReviewRegion";
import { currentConfirmation } from "../../components/confirmation";
import { WorkspacePanel } from "../../components/WorkspacePanel";
import { WorkDraftPanel } from "../../components/WorkDraftPanel";
import { WorkRunPanel } from "../../components/WorkRunPanel";
import { ExternalAnalysisImportPanel } from "../../components/ExternalAnalysisImportPanel";
import { updateWorkspace, useWorkspace } from "../../workspace";
import "./agent-work.css";

import { ConversationHistory } from "./ConversationHistory";
export { AnalysisProposalActions } from "./AnalysisProposalActions";

const RUN_TERMINAL_STATES = new Set(["succeeded", "failed_retryable", "failed_terminal", "timed_out", "cancelled", "qc_review"]);
const OUTBOX_REFRESH_MAX_INTERVAL_MS = 60_000;
const MAX_OUTBOX_CONVERSATION_REFRESHES = 16;
const COMPOSITE_GOAL_REFRESH_INTERVAL_MS = 10_000;
const COMPOSITE_GOAL_REFRESH_MAX_INTERVAL_MS = 30_000;

type CompositeGoalTone = "good" | "warn" | "danger" | "info" | "neutral";

function compositeGoalStatusView(status: string): { label: string; tone: CompositeGoalTone; detail: string } {
  switch (status) {
    case "waiting_preprocessing": return { label: "等待预处理", tone: "info", detail: "预处理尚未完成，后续分析会在条件满足后继续。" };
    case "waiting_review": return { label: "等待审查", tone: "warn", detail: "当前方案或质量控制记录等待你的审查。" };
    case "waiting_run": return { label: "等待运行完成", tone: "info", detail: "目标已绑定到现有运行，系统会等待并重新核验该运行。" };
    case "waiting_analysis": return { label: "等待分析", tone: "info", detail: "统计分析方案或计算正在推进。" };
    case "waiting_result_interpretation": return { label: "等待结果解释", tone: "info", detail: "统计结果已生成，正在整理结果说明。" };
    case "waiting_user": return { label: "需要你处理", tone: "warn", detail: "流程需要你的确认或补充后才能继续。" };
    case "blocked": return { label: "已阻断", tone: "danger", detail: "当前流程无法继续，请查看阻断原因并处理。" };
    case "completed": return { label: "已完成", tone: "good", detail: "结果解释已送达当前会话。" };
    case "indeterminate": return { label: "状态待核验", tone: "warn", detail: "流程关联信息需要核验，页面不会推断当前阶段。" };
    default: return { label: "状态待核验", tone: "neutral", detail: "收到尚未识别的进度状态，请刷新会话后核对。" };
  }
}

function compositeGoalStageView(status: string | null | undefined): string {
  if (!status) return "尚未进入";
  switch (status) {
    case "queued": return "排队中";
    case "running": return "进行中";
    case "waiting_user": return "需要你处理";
    case "waiting_run": return "等待计算";
    case "waiting_dependency": return "等待前置环节";
    case "pending": return "等待开始";
    case "ready": return "已就绪";
    case "proposal_ready": return "方案已准备";
    case "completed": return "已完成";
    case "blocked": return "已阻断";
    case "cancelled": return "已取消";
    case "failed": return "未通过";
    default: return "状态待核验";
  }
}

function compositeGoalReasonView(reasonCode: string | null | undefined): string | null {
  if (!reasonCode) return null;
  const labels: Record<string, string> = {
    preprocessing_not_started: "预处理尚未开始。",
    preprocessing_in_progress: "预处理仍在进行。",
    preprocessing_user_decision: "预处理需要你的决定。",
    preprocessing_plan_review: "预处理方案等待你的审查。",
    preprocessing_run_pending: "预处理目标正在等待已绑定的运行。",
    preprocessing_run_authority_mismatch: "预处理运行关联需要核验。",
    preprocessing_run_state_unknown: "预处理运行状态不受支持，需要核验。",
    preprocessing_plan_review_authority_mismatch: "预处理方案审查关联需要核验。",
    preprocessing_plan_state_unknown: "预处理方案状态不受支持，需要核验。",
    preprocessing_plan_review_state_mismatch: "预处理方案状态与等待审查记录不一致。",
    preprocessing_run_start_user: "预处理方案已批准，等待你明确启动运行。",
    preprocessing_qc_review: "预处理质量控制等待你的审查。",
    preprocessing_qc_review_authority_mismatch: "预处理质量控制审查关联需要核验。",
    preprocessing_qc_review_state_mismatch: "预处理质量控制状态与审查记录不一致。",
    preprocessing_terminal_block: "预处理已停止，需检查后再继续。",
    preprocessing_task_missing: "找不到对应的预处理任务。",
    preprocessing_binding_mismatch: "预处理任务与当前目标的关联需要核验。",
    proposal_outbox_missing: "找不到对应的分析准备任务。",
    proposal_outbox_binding_missing: "分析准备任务的关联信息需要核验。",
    proposal_outbox_authority_mismatch: "分析准备任务的来源信息不一致。",
    analysis_proposal_blocked: "准备分析方案时遇到阻断。",
    analysis_proposal_pending: "正在准备分析方案。",
    proposal_analysis_task_missing: "找不到对应的分析方案任务。",
    proposal_analysis_task_mismatch: "分析方案任务的来源需要核验。",
    proposal_analysis_attempt_missing: "分析方案的记录不完整。",
    proposal_analysis_attempt_mismatch: "当前分析方案与任务记录不一致。",
    analysis_decision_required: "分析方案需要你的确认。",
    analysis_task_link_missing: "找不到对应的统计分析任务。",
    analysis_task_mismatch: "统计分析任务与当前目标的关联需要核验。",
    analysis_task_reference_mismatch: "统计分析任务的版本关联需要核验。",
    analysis_terminal_block: "统计分析已停止，需检查后再继续。",
    analysis_user_decision: "统计分析需要你的决定。",
    analysis_plan_review: "统计方案等待你的审查。",
    analysis_run_pending: "统计目标正在等待已绑定的运行。",
    analysis_run_authority_mismatch: "统计运行关联需要核验。",
    analysis_run_state_unknown: "统计运行状态不受支持，需要核验。",
    analysis_plan_review_authority_mismatch: "统计方案审查关联需要核验。",
    analysis_plan_state_unknown: "统计方案状态不受支持，需要核验。",
    analysis_plan_review_state_mismatch: "统计方案状态与等待审查记录不一致。",
    analysis_run_start_user: "统计方案已批准，等待你明确启动运行。",
    analysis_qc_review: "统计质量控制等待你的审查。",
    analysis_qc_review_authority_mismatch: "统计质量控制审查关联需要核验。",
    analysis_qc_review_state_mismatch: "统计质量控制状态与审查记录不一致。",
    analysis_in_progress: "统计分析仍在进行。",
    analysis_only_child_binding_mismatch: "外部指标分析的任务关联需要核验。",
    analysis_only_conversation_mismatch: "外部指标分析与当前会话的关联需要核验。",
    analysis_only_source_changed: "外部指标的纳入修订已变化，请重新核实分析来源。",
    analysis_task_not_created: "正在准备统计分析草稿。",
    analysis_draft_user_decision: "统计分析草稿需要你补充或确认。",
    analysis_task_missing: "找不到对应的统计分析任务。",
    result_interpretation_binding_missing: "结果解释任务的关联信息需要核验。",
    result_interpretation_blocked: "生成结果解释时遇到阻断。",
    result_interpretation_user_decision: "结果解释需要你的决定。",
    result_interpretation_pending: "正在整理统计结果说明。",
    result_continuation_mismatch: "结果说明与当前任务的关联需要核验。",
    result_interpretation_incomplete: "结果说明状态尚未完整，需要核验。",
    result_interpretation_delivered: "结果说明已送达当前会话。",
  };
  return labels[reasonCode] ?? "详细状态原因需要核验。";
}

function compositeGoalIsActive(goal: CompositeGoalProgress): boolean {
  return ["waiting_preprocessing", "waiting_run", "waiting_analysis", "waiting_result_interpretation"].includes(goal.status);
}

function CompositeGoalProgressPanel({
  view,
  refreshingAutomatically,
  onRefresh,
  disabled,
  onSelect,
}: {
  view: { conversationId: string; goals: CompositeGoalProgress[]; loading: boolean; error: string };
  refreshingAutomatically: boolean;
  onRefresh: () => void;
  disabled: boolean;
  onSelect: (intentTaskId: string) => void;
}) {
  return <section className="panel composite-goal-progress" aria-label="自动化流程进度">
    <div className="panel-heading">
      <div>
        <span className="eyebrow">Work 自动化</span>
        <h2>流程进度</h2>
        <p>这里只显示当前会话记录的处理阶段，不推断科研方案或统计结论。</p>
      </div>
      <div>
        <button className="button button-secondary" type="button" disabled={view.loading} onClick={onRefresh}>刷新进度</button>
      </div>
    </div>
    {refreshingAutomatically && <p className="chat-technical" role="status">进行中的任务每 10 秒自动更新。</p>}
    {view.error && <p className="composite-goal-error" role="status">{view.error}</p>}
    {view.loading && view.goals.length === 0 && <p role="status">正在读取本会话的自动化进度…</p>}
    {!view.loading && view.goals.length === 0 && !view.error && <p>当前会话还没有需要跟踪的完整自动化流程。</p>}
    {view.goals.length > 0 && <ol className="composite-goal-list">
      {view.goals.map((goal, index) => {
        const status = compositeGoalStatusView(goal.status);
        const reason = compositeGoalReasonView(goal.reason_code);
        const analysisOnly = goal.goal_kind === "analysis_only_external_import";
        return <li key={goal.intent_task_id}>
          <article className="composite-goal-card">
            <h3>自动化流程 {index + 1} <span className={`status-pill status-${status.tone}`}>{status.label}</span></h3>
            <p><strong>流程类型：</strong>{analysisOnly ? "外部指标分析" : "预处理与分析"}</p>
            {goal.analysis_summary && <p className="composite-goal-summary"><strong>分析目标：</strong>{goal.analysis_summary}</p>}
            <p>{reason ?? status.detail}</p>
            <ol aria-label={`自动化流程 ${index + 1} 的处理阶段`}>
              {!analysisOnly && <li><strong>预处理：</strong>{compositeGoalStageView(goal.preprocessing_status)}</li>}
              {!analysisOnly && <li><strong>分析方案：</strong>{compositeGoalStageView(goal.analysis_proposal_status)}</li>}
              <li><strong>统计分析：</strong>{compositeGoalStageView(goal.analysis_status)}</li>
              <li><strong>结果解释：</strong>{compositeGoalStageView(goal.result_interpretation_status)}</li>
            </ol>
            {(analysisOnly && ["waiting_user", "waiting_analysis", "waiting_result_interpretation", "completed"].includes(goal.status)
              || !analysisOnly && ["waiting_user", "waiting_analysis", "waiting_result_interpretation", "completed"].includes(goal.status)
              && ["waiting_user", "proposal_ready"].includes(goal.analysis_proposal_status ?? "")
              && (goal.analysis_work_task_id || (!goal.analysis_status && goal.analysis_proposal_status === "waiting_user"))
              && !["blocked", "cancelled"].includes(goal.analysis_status ?? ""))
              && <button className="button button-secondary" type="button" disabled={disabled} onClick={() => onSelect(goal.intent_task_id)}>{!goal.analysis_status || goal.analysis_status === "waiting_user" ? "继续此分析" : "查看此分析"}</button>}
          </article>
        </li>;
      })}
    </ol>}
  </section>;
}

function pendingAnalysisOutboxIntentId(conversation?: Conversation): string | null {
  if (!conversation?.project_id) return null;
  const activeMessage = [...conversation.messages].reverse().find((item) => {
    const envelope = item.payload.analysis_intent_envelope;
    return Boolean(
      envelope && typeof envelope === "object" && "status" in envelope && envelope.status === "active"
      && "task_id" in envelope && typeof envelope.task_id === "string"
      && "project_id" in envelope && envelope.project_id === conversation.project_id,
    );
  });
  const envelope = activeMessage?.payload.analysis_intent_envelope;
  if (!envelope || typeof envelope !== "object" || !("task_id" in envelope) || typeof envelope.task_id !== "string") return null;
  const retryGeneration = conversation.messages.reduce((latest, item) => {
    const retry = item.payload.analysis_outbox_retry;
    return retry && typeof retry === "object" && "intent_task_id" in retry
      && retry.intent_task_id === envelope.task_id && "retry_generation" in retry
      && typeof retry.retry_generation === "number"
      ? Math.max(latest, retry.retry_generation)
      : latest;
  }, 0);
  const continuationExistsForGeneration = conversation.messages.some((item) => {
    const continuation = item.payload.analysis_outbox_continuation;
    return Boolean(
      continuation && typeof continuation === "object" && "intent_task_id" in continuation
      && continuation.intent_task_id === envelope.task_id
      && (!("retry_generation" in continuation) || continuation.retry_generation === retryGeneration),
    );
  });
  return continuationExistsForGeneration ? null : envelope.task_id;
}

function pendingResultInterpretationTaskId(conversation: Conversation | undefined, runId: string): string | null {
  const task = conversation?.work_tasks?.find((item) => item.agent_role === "analysis" && item.run_id === runId);
  if (!task?.work_task_id) return null;
  let latestGeneration = -1;
  let latestStatus: unknown;
  for (const message of conversation?.messages ?? []) {
    for (const key of ["statistical_result_interpretation", "statistical_result_interpretation_retry"]) {
      const state = message.payload[key] as { task_id?: unknown; retry_generation?: unknown; status?: unknown } | undefined;
      if (state?.task_id !== task.work_task_id || typeof state.retry_generation !== "number") continue;
      if (state.retry_generation >= latestGeneration) {
        latestGeneration = state.retry_generation;
        latestStatus = state.status;
      }
    }
  }
  return latestStatus === "completed" || latestStatus === "blocked" || latestStatus === "waiting_user"
    ? null : task.work_task_id;
}

import {
  WELCOME,
  confirmedInputStage,
  conversationWorkspacePatch,
  messagesFrom,
  MODEL_SELECTION_KEY,
  modelChoice,
  rememberedModel,
  type ModelChoice,
  type WorkAgentRole,
} from "./conversation-model";

type PreprocessingAnalysisSource = { key: string; taskId: string; runId: string };

function preprocessingAnalysisSources(conversation: Conversation | undefined): PreprocessingAnalysisSource[] {
  const sources = new Map<string, PreprocessingAnalysisSource>();
  for (const task of conversation?.work_tasks ?? []) {
    if (task.agent_role !== "preprocessing" || !task.work_task_id || !task.run_id) continue;
    const key = JSON.stringify([task.work_task_id, task.run_id]);
    sources.set(key, { key, taskId: task.work_task_id, runId: task.run_id });
  }
  return [...sources.values()];
}

export function AgentPage() {
  const workspace = useWorkspace();
  const initialWorkspace = useRef(workspace.workspacePath ?? "").current;
  const [mode, setMode] = useState<ConversationMode>("work");
  const [agentRole, setAgentRole] = useState<WorkAgentRole>("auto");
  const [externalSource, setExternalSource] = useState<{ projectId: string; conversationId: string | null; importId: string | null } | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [compositeGoalView, setCompositeGoalView] = useState<{
    conversationId: string;
    goals: CompositeGoalProgress[];
    loading: boolean;
    error: string;
  } | null>(null);
  const [compositeGoalRefreshNonce, setCompositeGoalRefreshNonce] = useState(0);
  const [activeIds, setActiveIds] = useState<Partial<Record<ConversationMode, string>>>({});
  const [workspacePath, setWorkspacePath] = useState(initialWorkspace);
  const [choices, setChoices] = useState<ModelChoice[]>([]);
  const [selectedModelKey, setSelectedModelKey] = useState(rememberedModel);
  const [allowRemoteSearch, setAllowRemoteSearch] = useState(false);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [draftDirty, setDraftDirty] = useState(false);
  const [error, setError] = useState("");
  const [openedConfirmation, setOpenedConfirmation] = useState<string | null>(null);
  const [planOpen, setPlanOpen] = useState(() => window.innerWidth > 1180);
  const [pollNonce, setPollNonce] = useState(0);
  const [runConnectionMessage, setRunConnectionMessage] = useState("");
  const composer = useRef<HTMLTextAreaElement>(null);
  const turnController = useRef<AbortController | null>(null);
  const [pendingTurn, setPendingTurn] = useState<{ mode: ConversationMode; user: string; text: string; status: string; confirmation: boolean } | null>(null);
  useEffect(() => () => turnController.current?.abort(), []);
  const [consentBusy, setConsentBusy] = useState(false);
  const [workspaceConsent, setWorkspaceConsent] = useState<WorkspaceConsent | null>(null);
  const [workspaceEntries, setWorkspaceEntries] = useState<Array<{ path: string; kind: string; readable?: boolean }>>([]);
  const [selectedScope, setSelectedScope] = useState<string[]>([]);
  const [workspaceCheck, setWorkspaceCheck] = useState<WorkspaceCheck | null>(null);
  const [workspaceChecking, setWorkspaceChecking] = useState(false);
  const [workspaceCheckError, setWorkspaceCheckError] = useState("");
  const [selectedInputStage, setSelectedInputStage] = useState<string>("");
  const [analysisSourceSelection, setAnalysisSourceSelection] = useState<{
    context: string;
    enabled: boolean;
    sourceKey: string;
  } | null>(null);
  const [analysisGoalCreationSelection, setAnalysisGoalCreationSelection] = useState<{
    context: string;
    enabled: boolean;
  } | null>(null);

  const modeConversations = useMemo(() => conversations.filter((item) => item.mode === mode), [conversations, mode]);
  const activeConversation = conversations.find((item) => item.conversation_id === activeIds[mode]);
  const currentConversationId = useRef(activeConversation?.conversation_id);
  currentConversationId.current = activeConversation?.conversation_id;
  const activeCompositeGoalView = mode === "work" && activeConversation
    && compositeGoalView?.conversationId === activeConversation.conversation_id ? compositeGoalView : null;
  const hasActiveCompositeGoal = activeCompositeGoalView?.goals.some(compositeGoalIsActive) ?? false;
  const boundToSelectedWorkspace = activeConversation?.workspace_path === workspacePath;
  const boundProjectId = boundToSelectedWorkspace ? activeConversation?.project_id ?? null : null;
  const preprocessingSources = preprocessingAnalysisSources(activeConversation);
  const preprocessingSourceContext = JSON.stringify([
    mode,
    activeConversation?.conversation_id ?? null,
    workspacePath,
    workspace.projectId ?? null,
    boundProjectId,
    preprocessingSources.map((source) => source.key),
  ]);
  const canRegisterAnalysisFromPreprocessing = mode === "work" && boundToSelectedWorkspace
    && Boolean(boundProjectId) && workspace.projectId === boundProjectId && preprocessingSources.length > 0;
  const registerAnalysisFromPreprocessing = canRegisterAnalysisFromPreprocessing
    && analysisSourceSelection?.context === preprocessingSourceContext && analysisSourceSelection.enabled;
  const selectedPreprocessingSource = registerAnalysisFromPreprocessing
    ? preprocessingSources.find((source) => source.key === analysisSourceSelection.sourceKey) ?? null
    : null;
  useEffect(() => {
    setAnalysisSourceSelection((current) => current?.context === preprocessingSourceContext ? current : null);
  }, [preprocessingSourceContext]);
  const analysisRoleTask = activeConversation?.work_tasks?.find((task) => task.agent_role === "analysis");
  const selectedAnalysisIntentId = analysisRoleTask?.analysis_intent_task_id ?? null;
  const latestAnalysisPayload = [...(activeConversation?.messages ?? [])].reverse()
    .find((message) => {
      if (message.role !== "assistant" || message.payload.agent_role !== "analysis") return false;
      const draft = message.payload.analysis_draft;
      return draft && typeof draft === "object" && (!selectedAnalysisIntentId
        || ("analysis_intent_task_id" in draft && draft.analysis_intent_task_id === selectedAnalysisIntentId));
    })?.payload.analysis_draft;
  const savedAnalysisDraft = latestAnalysisPayload && typeof latestAnalysisPayload === "object" && "draft" in latestAnalysisPayload ? latestAnalysisPayload.draft : null;
  const savedExternalImportId = savedAnalysisDraft && typeof savedAnalysisDraft === "object" && "external_import_id" in savedAnalysisDraft && typeof savedAnalysisDraft.external_import_id === "string" ? savedAnalysisDraft.external_import_id : null;
  const selectedExternalImportId = agentRole === "preprocessing" || !boundProjectId || selectedPreprocessingSource ? null
    : externalSource?.projectId === boundProjectId && externalSource?.conversationId === (activeConversation?.conversation_id ?? null) ? externalSource.importId : savedExternalImportId;
  const routedRole = [...(activeConversation?.messages ?? [])].reverse()
    .find((message) => message.role === "assistant" && (message.payload.agent_role === "analysis" || message.payload.agent_role === "preprocessing"))
    ?.payload.agent_role;
  const effectiveAgentRole = selectedPreprocessingSource ? "analysis" : agentRole === "auto" ? (activeConversation?.foreground_agent_role ?? (routedRole === "analysis" ? "analysis" : "preprocessing")) : agentRole;
  const analysisGoalCreationContext = JSON.stringify([
    mode,
    activeConversation?.conversation_id ?? null,
    boundProjectId,
    selectedExternalImportId,
  ]);
  const createIndependentAnalysisGoal = mode === "work" && effectiveAgentRole === "analysis"
    && !selectedPreprocessingSource && Boolean(selectedExternalImportId)
    && analysisGoalCreationSelection?.context === analysisGoalCreationContext
    && analysisGoalCreationSelection.enabled;
  useEffect(() => {
    setAnalysisGoalCreationSelection((current) => current?.context === analysisGoalCreationContext ? current : null);
  }, [analysisGoalCreationContext]);
  const offeredConfirmation = mode === "work" && effectiveAgentRole === "preprocessing" && boundToSelectedWorkspace
    ? currentConfirmation(activeConversation?.messages ?? [], workspacePath) : null;
  const confirmation = offeredConfirmation?.request ?? null;
  const confirmationKey = offeredConfirmation ? `${activeConversation?.conversation_id}:${offeredConfirmation.messageId}` : null;
  const visibleMessages = messagesFrom(activeConversation);
  const latestDraftId = mode === "work" && effectiveAgentRole === "preprocessing" && boundToSelectedWorkspace ? [...visibleMessages].reverse().find((item) => item.workDraft)?.id : undefined;
  const currentDraftMessage = visibleMessages.find((item) => item.id === latestDraftId);
  if (pendingTurn?.mode === mode) visibleMessages.push(
    { id: "pending-user", role: "user", text: pendingTurn.user },
    { id: "pending-assistant", role: "assistant", text: pendingTurn.text, streamingStatus: pendingTurn.status },
  );
  const selectedModel = choices.find((item) => item.key === selectedModelKey);
  const receiveStatisticsRun = useCallback((run: Run) => {
    const conversationId = activeConversation?.conversation_id;
    if (!conversationId || currentConversationId.current !== conversationId) return;
    setAgentRole("analysis");
    setActiveRun(run);
    updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state });
  }, [activeConversation?.conversation_id]);
  const refreshWorkTask = useCallback(async () => {
    if (!activeConversation?.conversation_id) return;
    const latest = await agentApi.conversation(activeConversation.conversation_id);
    setConversations((items) => items.map((item) => item.conversation_id === latest.conversation_id ? latest : item));
  }, [activeConversation?.conversation_id]);
  const retryAnalysisOutbox = useCallback(async (intentTaskId: string) => {
    if (!activeConversation?.conversation_id || busy || draftDirty) return;
    setBusy(true);
    setError("");
    try {
      const latest = await agentApi.retryAnalysisOutbox(activeConversation.conversation_id, intentTaskId);
      cache(latest);
      setAgentRole("analysis");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [activeConversation?.conversation_id, busy, draftDirty]);
  const retryResultInterpretation = useCallback(async (taskId: string) => {
    if (!activeConversation?.conversation_id || busy || draftDirty) return;
    setBusy(true);
    setError("");
    try {
      const latest = await agentApi.retryStatisticalResultInterpretation(activeConversation.conversation_id, taskId);
      cache(latest);
      setAgentRole("analysis");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [activeConversation?.conversation_id, busy, draftDirty]);
  const confirmedWorkspaceStage = confirmedInputStage(activeConversation, workspacePath);
  const canAuthorizeWorkspaceFiles = Boolean(selectedModel?.capabilities.includes("tool_use"));
  const authorizationUnavailableReason = !selectedModel
    ? "先选择一个具体模型，自动选择不能授予文件授权"
    : "模型配置未声明工具调用；请在设置中确认并启用 tool_use";
  const canSearch = selectedModel?.capabilities.includes("web_search") ?? choices.some((item) => item.capabilities.includes("web_search"));

  useEffect(() => {
    const conversationId = mode === "work" ? activeConversation?.conversation_id : undefined;
    if (!conversationId) {
      setCompositeGoalView(null);
      return;
    }

    const controller = new AbortController();
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    setCompositeGoalView((existing) => existing?.conversationId === conversationId
      ? existing
      : { conversationId, goals: [], loading: true, error: "" });

    const refresh = async () => {
      try {
        const goals = await agentApi.compositeGoals(conversationId, controller.signal);
        if (!current || controller.signal.aborted || currentConversationId.current !== conversationId) return;
        failures = 0;
        setCompositeGoalView({ conversationId, goals, loading: false, error: "" });
        if (goals.some(compositeGoalIsActive)) {
          timer = setTimeout(() => void refresh(), COMPOSITE_GOAL_REFRESH_INTERVAL_MS);
        }
      } catch {
        if (!current || controller.signal.aborted || currentConversationId.current !== conversationId) return;
        failures += 1;
        setCompositeGoalView((existing) => ({
          conversationId,
          goals: existing?.conversationId === conversationId ? existing.goals : [],
          loading: false,
          error: "自动化进度暂时无法更新。对话仍可继续使用，请稍后重试。",
        }));
        timer = setTimeout(
          () => void refresh(),
          Math.min(COMPOSITE_GOAL_REFRESH_INTERVAL_MS * 2 ** Math.min(failures - 1, 2), COMPOSITE_GOAL_REFRESH_MAX_INTERVAL_MS),
        );
      }
    };

    void refresh();
    return () => {
      current = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [activeConversation?.conversation_id, activeConversation?.version, compositeGoalRefreshNonce, mode]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([agentApi.profiles(controller.signal), agentApi.conversations(undefined, controller.signal)])
      .then(async ([profiles, loaded]) => {
        const lists = await Promise.allSettled(profiles.map((profile) => agentApi.listProviderModels({
          base_url: profile.profile.base_url,
          api_key: null,
          api_key_env: profile.profile.api_key_env,
        }, controller.signal)));
        setChoices(profiles.flatMap((profile, index) => {
          const result = lists[index];
          const models = result?.status === "fulfilled" && result.value.models.length ? result.value.models : [profile.profile.model];
          return models.map((model) => modelChoice(profile, model));
        }));
        setConversations(loaded);
        const ids: Partial<Record<ConversationMode, string>> = {};
        for (const conversation of loaded) if (!ids[conversation.mode]) ids[conversation.mode] = conversation.conversation_id;
        setActiveIds(ids);
        const work = loaded.find((item) => item.mode === "work");
        if (work) {
          updateWorkspace(conversationWorkspacePatch(work));
          if (work.workspace_path && !initialWorkspace) setWorkspacePath(work.workspace_path);
        }
      })
      .catch((caught) => { if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught)); });
    return () => controller.abort();
  }, [initialWorkspace]);

  const taskRunId = activeConversation?.work_tasks?.find((item) => item.agent_role === effectiveAgentRole)?.run_id;
  const monitoredRunId = mode === "work" && boundToSelectedWorkspace ? taskRunId ?? activeConversation?.active_run_id : null;
  const pendingOutboxIntentId = pendingAnalysisOutboxIntentId(activeConversation);
  const observedRunId = activeRun?.run_id;
  const observedRunState = activeRun?.state;
  const pendingResultInterpretationId = observedRunId && observedRunState === "succeeded"
    ? pendingResultInterpretationTaskId(activeConversation, observedRunId) : null;

  useEffect(() => {
    const runId = monitoredRunId;
    setActiveRun(null);
    if (!runId) { setActiveRun(null); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const poll = async () => {
      try {
        const run = await runsApi.run(runId, controller.signal);
        setActiveRun(run);
        failures = 0;
        setRunConnectionMessage("");
        updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state });
        if (!RUN_TERMINAL_STATES.has(run.state)) timer = setTimeout(() => void poll(), 2000);
      } catch {
        if (!controller.signal.aborted) {
          failures += 1;
          setRunConnectionMessage(failures < 6 ? "运行状态连接中断，正在重新连接…" : "暂时无法获取运行状态，请重新连接。计算可能仍在继续。");
          if (failures < 6) timer = setTimeout(() => void poll(), Math.min(1000 * 2 ** failures, 15_000));
        }
      }
    };
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [activeConversation?.conversation_id, activeConversation?.active_run_id, taskRunId, boundToSelectedWorkspace, effectiveAgentRole, mode, monitoredRunId, pollNonce]);

  useEffect(() => {
    const conversationId = activeConversation?.conversation_id;
    if (
      mode !== "work" || !boundToSelectedWorkspace || !conversationId || !observedRunId
      || observedRunId !== monitoredRunId || !observedRunState
      || !RUN_TERMINAL_STATES.has(observedRunState)
    ) return;

    const controller = new AbortController();
    const intentTaskId = pendingOutboxIntentId;
    const resultTaskId = pendingResultInterpretationId;
    const hasAnalysisTaskForRun = activeConversation?.work_tasks?.some(
      (item) => item.agent_role === "analysis" && item.run_id === observedRunId,
    ) ?? false;
    if (observedRunState === "succeeded" && hasAnalysisTaskForRun && !resultTaskId) return;
    const maxRefreshes = ((intentTaskId || resultTaskId) && observedRunState === "succeeded")
      ? MAX_OUTBOX_CONVERSATION_REFRESHES : 1;
    let refreshes = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const scheduleRefresh = () => {
      if (refreshes >= maxRefreshes) return;
      const delayMs = Math.min(1000 * 2 ** Math.max(0, refreshes - 1), OUTBOX_REFRESH_MAX_INTERVAL_MS);
      timer = setTimeout(() => void refresh(), delayMs);
    };
    const refresh = async () => {
      refreshes += 1;
      try {
        const latest = await agentApi.conversation(conversationId, controller.signal);
        if (controller.signal.aborted || currentConversationId.current !== conversationId) return;
        setConversations((items) => items.map((item) => item.conversation_id === conversationId ? latest : item));
        if (refreshes < maxRefreshes && (
          (intentTaskId && pendingAnalysisOutboxIntentId(latest) === intentTaskId)
          || (resultTaskId && pendingResultInterpretationTaskId(latest, observedRunId) === resultTaskId)
        )) {
          scheduleRefresh();
        }
      } catch {
        if (!controller.signal.aborted) scheduleRefresh();
      }
    };
    void refresh();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [activeConversation?.conversation_id, activeConversation?.version, activeConversation?.work_tasks, boundToSelectedWorkspace, mode, monitoredRunId, observedRunId, observedRunState, pendingOutboxIntentId, pendingResultInterpretationId, taskRunId]);

  useEffect(() => {
    if (mode !== "work" || !activeConversation || !workspacePath) { setWorkspaceEntries([]); return; }
    const controller = new AbortController();
    workspacesApi.workspaceFiles(activeConversation.conversation_id, "", controller.signal)
      .then((listing) => setWorkspaceEntries(listing.entries.filter((item) => item.kind === "file" && item.readable !== false)))
      .catch(() => setWorkspaceEntries([]));
    return () => controller.abort();
  }, [activeConversation, mode, workspacePath]);

  useEffect(() => {
    setWorkspaceCheck(null);
    setSelectedInputStage("");
    setWorkspaceCheckError("");
    if (mode !== "work" || !workspacePath) { setWorkspaceChecking(false); return; }
    const controller = new AbortController();
    setWorkspaceChecking(true);
    environmentApi.checkWorkspace({ path: workspacePath }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setWorkspaceCheck(result);
        setSelectedInputStage(result.selected_input_stage ?? confirmedWorkspaceStage ?? "");
      })
      .catch((caught) => { if (!controller.signal.aborted) setWorkspaceCheckError(describeError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setWorkspaceChecking(false); });
    return () => controller.abort();
  }, [mode, workspacePath, activeConversation?.conversation_id, confirmedWorkspaceStage]);

  useEffect(() => {
    if (mode !== "work" || !activeConversation || activeConversation.workspace_path !== workspacePath || !selectedModel) { setWorkspaceConsent(null); return; }
    setWorkspaceConsent(null);
    const controller = new AbortController();
    workspacesApi.workspaceConsents(activeConversation.conversation_id, controller.signal)
      .then((items) => setWorkspaceConsent(items.find((item) => item.profile_id === selectedModel.profileId && item.model === selectedModel.model && item.valid_for_current_context && !item.revoked_at) ?? null))
      .catch(() => setWorkspaceConsent(null));
    return () => controller.abort();
  }, [activeConversation, mode, selectedModel, workspacePath]);

  function cache(conversation: Conversation): void {
    setDraftDirty(false);
    setConversations((items) => [conversation, ...items.filter((item) => item.conversation_id !== conversation.conversation_id)]);
    setActiveIds((items) => ({ ...items, [conversation.mode]: conversation.conversation_id }));
    if (conversation.mode === "work") {
      updateWorkspace(conversationWorkspacePatch(conversation));
      if (conversation.workspace_path) setWorkspacePath(conversation.workspace_path);
      const stage = conversation.messages.at(-1)?.payload.selected_input_stage;
      if (typeof stage === "string") setSelectedInputStage(stage);
    }
  }

  async function selectAgent(role: WorkAgentRole): Promise<void> {
    if (role === "auto" || !activeConversation) { setAgentRole(role); return; }
    setBusy(true); setError("");
    try {
      cache(await agentApi.selectWorkAgent(activeConversation.conversation_id, { agent_role: role, expected_version: activeConversation.version }));
      setAgentRole(role);
    } catch (caught) { setError(describeError(caught)); await refreshWorkTask().catch(() => {}); }
    finally { setBusy(false); }
  }

  async function selectCompositeGoal(intentTaskId: string): Promise<void> {
    if (!activeConversation || busy || draftDirty) return;
    setBusy(true); setError("");
    try {
      const selected = await agentApi.selectCompositeGoal(activeConversation.conversation_id, intentTaskId, activeConversation.version);
      cache(selected);
      setAgentRole("analysis");
      setExternalSource(null);
      setAnalysisSourceSelection(null);
      setAnalysisGoalCreationSelection(null);
      setOpenedConfirmation(null);
      setCompositeGoalRefreshNonce((value) => value + 1);
    } catch (caught) {
      setError(describeError(caught));
      await refreshWorkTask().catch(() => {});
      setCompositeGoalRefreshNonce((value) => value + 1);
    } finally { setBusy(false); }
  }

  async function ensureConversation(target: ConversationMode): Promise<string> {
    const existing = activeIds[target];
    if (existing) return existing;
    const created = await agentApi.createConversation({
      mode: target,
      project_id: target === "work" ? boundProjectId : null,
      workspace_path: target === "work" ? workspacePath.trim() || null : null,
      preferred_profile_id: selectedModel?.profileId ?? null,
    });
    cache(created);
    return created.conversation_id;
  }

  async function newConversation(): Promise<void> {
    setBusy(true); setError("");
    try {
      const created = await agentApi.createConversation({
        mode,
        project_id: mode === "work" ? boundProjectId : null,
        workspace_path: mode === "work" ? workspacePath.trim() || null : null,
        preferred_profile_id: selectedModel?.profileId ?? null,
      });
      cache(created); setPendingTurn(null); setAgentRole("auto");
    } catch (caught) { setError(describeError(caught)); } finally { setBusy(false); }
  }

  async function chooseWorkspace(): Promise<void> {
    setBusy(true); setError("");
    try {
      const picked = await environmentApi.pickWorkspace();
      if (picked.path) {
        if (activeConversation?.workspace_path && activeConversation.workspace_path !== picked.path) {
          setActiveIds((items) => ({ ...items, work: undefined }));
        }
        setSelectedScope([]); setWorkspacePath(picked.path); updateWorkspace({ workspacePath: picked.path });
      }
    } catch (caught) { setError(describeError(caught)); } finally { setBusy(false); }
  }

  async function grantWorkspaceConsent(): Promise<void> {
    if (!selectedModel) { setError("请先选择一个具体模型，再授权文件内容读取。"); return; }
    if (!selectedModel.capabilities.includes("tool_use")) {
      setError("该模型配置未声明工具调用能力。请在设置中确认并启用 tool_use，再授权文件分析。");
      return;
    }
    const provider = selectedModel.label.split(" · ")[0];
    if (!window.confirm(`授权工作区文件分析？\n\n模型服务：${provider}\n发送范围：当前工作区内、用户选定的文件\n默认排除：.git、node_modules、虚拟环境、缓存和构建产物\n规则：凭据、邮箱、电话和绝对路径会脱敏；无法可靠脱敏的临床身份整段省略。\n\n现在会先向所选模型发送一次不含工作区内容的工具调用测试；测试通过后才创建授权。`)) return;
    setConsentBusy(true); setError("");
    try {
      const conversationId = await ensureConversation("work");
      const consent = await workspacesApi.grantWorkspaceConsent(conversationId, {
        profile_id: selectedModel.profileId,
        model: selectedModel.model,
        acknowledge_remote_processing: true,
      });
      setWorkspaceConsent(consent);
    } catch (caught) { setError(describeError(caught)); } finally { setConsentBusy(false); }
  }

  async function revokeWorkspaceConsent(): Promise<void> {
    if (!activeConversation || !workspaceConsent) return;
    setConsentBusy(true); setError("");
    try { await workspacesApi.revokeWorkspaceConsent(activeConversation.conversation_id, workspaceConsent.consent_id); setWorkspaceConsent(null); }
    catch (caught) { setError(describeError(caught)); }
    finally { setConsentBusy(false); }
  }

  async function applyWorkspaceProposal(proposal: WorkspaceProposal): Promise<void> {
    if (!activeConversation) return;
    setBusy(true); setError("");
    try {
      const applied = await workspacesApi.applyWorkspaceProposal(activeConversation.conversation_id, proposal.proposal_id, proposal.proposal_hash);
      setConversations((items) => items.map((conversation) => conversation.conversation_id !== activeConversation.conversation_id ? conversation : {
        ...conversation,
        messages: conversation.messages.map((message) => {
          const current = message.payload.workspace_change_proposal as WorkspaceProposal | undefined;
          return current?.proposal_id === proposal.proposal_id ? { ...message, payload: { ...message.payload, workspace_change_proposal: { ...current, status: applied.status, applied_at: applied.applied_at } } } : message;
        }),
      }));
    }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  async function undoWorkspaceProposal(proposal: WorkspaceProposal): Promise<void> {
    if (!activeConversation) return;
    setBusy(true); setError("");
    try {
      const undone = await workspacesApi.undoWorkspaceProposal(activeConversation.conversation_id, proposal.proposal_id);
      setConversations((items) => items.map((conversation) => conversation.conversation_id !== activeConversation.conversation_id ? conversation : {
        ...conversation,
        messages: conversation.messages.map((message) => {
          const current = message.payload.workspace_change_proposal as WorkspaceProposal | undefined;
          return current?.proposal_id === proposal.proposal_id ? { ...message, payload: { ...message.payload, workspace_change_proposal: { ...current, status: undone.status, undone_at: undone.undone_at } } } : message;
        }),
      }));
    }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  async function send(confirmationCommand?: string): Promise<void> {
    const content = (confirmationCommand ?? prompt).trim();
    if (!content || busy) return;
    if (registerAnalysisFromPreprocessing && !selectedPreprocessingSource) {
      setError("请先明确选择要用于分析的预处理任务和运行。");
      return;
    }
    const controller = new AbortController();
    turnController.current = controller;
    const deadline = window.setTimeout(() => {
      if (turnController.current !== controller) return;
      controller.abort();
      setBusy(false);
      setPrompt(confirmationCommand ? "" : content);
      if (confirmationCommand) setOpenedConfirmation(null);
      setPendingTurn(null);
      setError(confirmationCommand ? "确认操作等待已超过 5 分钟，尚不能确定是否提交成功。请刷新会话核对结果；停止等待不会取消计算。" : "等待回答已超过 5 分钟，请刷新会话查看已保存结果后重试。");
    }, 300_000);
    const confirmationLabel = confirmation?.options.find((option) => option.command === content)?.label;
    setPendingTurn({ mode, user: confirmationLabel ?? content, text: "", status: confirmationCommand ? "正在核对方案并提交操作…" : "正在生成回答…", confirmation: !!confirmationCommand });
    setPrompt(""); setBusy(true); setError("");
    try {
      const conversationId = await ensureConversation(mode);
      if (controller.signal.aborted) return;
      if (confirmationCommand) {
        const turn = await agentApi.confirmWorkAction(conversationId, confirmationCommand, currentDraftMessage?.workDraft?.hash ?? null, controller.signal);
        if (!controller.signal.aborted) { cache(turn.conversation); setPendingTurn(null); setOpenedConfirmation(null); }
        return;
      }
      const analysisGoalTurnFields = mode === "work" && effectiveAgentRole === "analysis" && !selectedPreprocessingSource
        ? createIndependentAnalysisGoal
          ? { start_new_analysis_goal: true }
          : selectedAnalysisIntentId ? { analysis_intent_task_id: selectedAnalysisIntentId } : {}
        : {};
      const turn = await agentApi.streamConversationTurn(conversationId, {
        content,
        workspace_path: mode === "work" ? workspacePath.trim() || null : null,
        preferred_profile_id: selectedModel?.profileId ?? null,
        model: selectedModel?.model ?? null,
        allow_remote_search: mode === "chat" && allowRemoteSearch,
        project_id: mode === "work" ? boundProjectId : null,
        agent_role: selectedPreprocessingSource ? "analysis" : mode === "work" && agentRole !== "auto" ? agentRole : null,
        preprocessing_task_id: selectedPreprocessingSource?.taskId,
        external_analysis_import_id: mode === "work" && !selectedPreprocessingSource ? selectedExternalImportId : null,
        target_run_id: selectedPreprocessingSource?.runId ?? (mode === "work" && boundToSelectedWorkspace && !selectedExternalImportId ? workspace.runId ?? null : null),
        plan_revision_id: mode === "work" && boundToSelectedWorkspace && !selectedPreprocessingSource ? workspace.planRevisionId ?? null : null,
        expected_plan_hash: mode === "work" && boundToSelectedWorkspace && !selectedPreprocessingSource ? workspace.planHash ?? null : null,
        workspace_file_scope: mode === "work" && selectedScope.length ? { paths: selectedScope } : null,
        selected_input_stage: mode === "work" && selectedInputStage ? selectedInputStage : null,
        real_execution_confirmed: false,
        ...analysisGoalTurnFields,
      }, (event) => {
        if (controller.signal.aborted) return;
        if (event.type === "reset") setPendingTurn((pending) => pending && { ...pending, text: "" });
        if (event.type === "delta" && mode === "chat") setPendingTurn((pending) => pending && { ...pending, text: pending.text + event.text, status: "正在生成回答…" });
        if (event.type === "status") setPendingTurn((pending) => pending && { ...pending, status: event.message });
      }, controller.signal);
      if (!controller.signal.aborted) { cache(turn.conversation); setPendingTurn(null); }
    } catch (caught) {
      if (!controller.signal.aborted) {
        if (confirmationCommand && activeConversation) {
          if (caught instanceof ApiError && caught.code === "work_input_changed" && currentDraftMessage?.workDraft) {
            try {
              const refreshed = await agentApi.prepareWorkReview(activeConversation.conversation_id, currentDraftMessage.workDraft.hash, controller.signal);
              cache(refreshed.conversation); setPlanOpen(true); setPendingTurn(null); setOpenedConfirmation(null);
              setError("输入已变化，旧确认已撤回。请重新核实输入，再审查方案。");
              return;
            } catch { /* Preserve the original rejection if recovery is unavailable. */ }
          }
          setPendingTurn((pending) => pending && { ...pending, status: "正在确认操作是否已保存…" });
          try {
            // One read-only recovery. Never repeat an execution request here.
            const recovered = await agentApi.conversation(activeConversation.conversation_id, controller.signal);
            if (caught instanceof ApiError && caught.status === 409 && ["work_draft_changed", "work_confirmation_changed", "work_review_already_started"].includes(caught.code)) {
              cache(recovered); setPendingTurn(null); setOpenedConfirmation(null);
              setError(`${describeError(caught)}已刷新当前方案，请重新审查后继续。`);
              return;
            }
            if (recovered.messages.some((item) => item.role === "user" && item.content === confirmationCommand)) {
              cache(recovered); setPendingTurn(null); setOpenedConfirmation(null); setError("");
              return;
            }
          } catch { /* Keep the original idempotency key for an explicit retry. */ }
          setPendingTurn(null);
          setError(`未能确认操作是否完成，请刷新会话或重试相同操作。${describeError(caught)}`);
          return;
        }
        setError(describeError(caught)); if (!confirmationCommand) setPrompt(content);
        setPendingTurn((pending) => pending && { ...pending, status: mode === "work" ? "本轮结果尚未确认，请重试恢复。" : "回答未完成，临时内容可能不完整。请重试。" });
      }
    } finally {
      window.clearTimeout(deadline);
      if (turnController.current === controller) {
        setBusy(false);
        turnController.current = null;
      }
    }
  }

  async function selectWorkspacePath(path: string): Promise<void> {
    setBusy(true); setError("");
    try {
      await environmentApi.checkWorkspace({ path });
      if (activeConversation?.workspace_path && activeConversation.workspace_path !== path) {
        setActiveIds((items) => ({ ...items, work: undefined }));
      }
      setSelectedScope([]); setWorkspacePath(path); updateWorkspace({ workspacePath: path });
    } catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  function stopWaiting(): void {
    const controller = turnController.current;
    if (!controller) return;
    controller.abort();
    turnController.current = null;
    setPrompt(pendingTurn?.confirmation ? "" : pendingTurn?.user ?? "");
    if (pendingTurn?.confirmation) setOpenedConfirmation(null);
    setPendingTurn(null);
    setBusy(false);
    setError(pendingTurn?.confirmation ? "已停止等待确认结果，尚不能确定是否提交成功。请刷新会话核对结果；停止等待不会取消计算。" : "已停止等待。服务端可能继续保存本轮结果；请刷新会话查看后再发送。");
  }

  async function saveDraft(hash: string, patch: Record<string, unknown>): Promise<void> {
    if (!activeConversation || busy) throw new Error("当前会话正在处理，请稍后重试");
    setBusy(true);
    try { const turn = await agentApi.patchWorkDraft(activeConversation.conversation_id, hash, patch); cache(turn.conversation); }
    finally { setBusy(false); }
  }

  async function prepareReview(): Promise<void> {
    if (!activeConversation || !currentDraftMessage?.workDraft || currentDraftMessage.workDraft.consumed || busy || draftDirty) return;
    setBusy(true); setError("");
    try { cache((await agentApi.prepareWorkReview(activeConversation.conversation_id, currentDraftMessage.workDraft.hash)).conversation); }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  return <main className={`agent-shell${mode === "work" && currentDraftMessage && planOpen ? " is-reviewing" : ""}`}>
    {confirmation && !draftDirty && confirmationKey === openedConfirmation && <ConfirmationDialog
      key={confirmationKey} request={confirmation} busy={busy} error={error}
      onConfirm={(command) => void send(command)}
      onStopWaiting={pendingTurn?.confirmation ? stopWaiting : undefined}
      onDismiss={() => { setOpenedConfirmation(null); }}
      onEdit={() => { setOpenedConfirmation(null); setPlanOpen(true); requestAnimationFrame(() => composer.current?.focus()); }}
    />}
    <div className="agent-mode-switch" role="tablist" aria-label="对话模式">
      {(["chat", "work"] as ConversationMode[]).map((item) => <button
        key={item}
        className={mode === item ? "agent-mode active" : "agent-mode"}
        type="button"
        role="tab"
        disabled={busy || draftDirty}
        aria-selected={mode === item}
        onClick={() => { setMode(item); setPrompt(""); setError(""); setPendingTurn(null); }}
      ><strong>{item === "chat" ? "Chat" : "Work"}</strong><small>{item === "chat" ? "方法问答" : "执行任务"}</small></button>)}
    </div>
    <Feedback message={error} error={Boolean(error)} />
    <section className="conversation-panel">
      <div className="conversation-toolbar">
        <select disabled={busy || draftDirty} aria-label="当前会话" value={activeIds[mode] ?? ""} onChange={(event) => {
          const selected = conversations.find((item) => item.conversation_id === event.target.value);
          if (selected) { cache(selected); setPendingTurn(null); setAgentRole("auto"); }
        }}><option value="">新会话</option>{modeConversations.map((item) => <option key={item.conversation_id} value={item.conversation_id}>{item.title}</option>)}</select>
        {mode === "work" && <select aria-label="Work 负责 Agent" value={agentRole} disabled={busy || draftDirty} onChange={(event) => void selectAgent(event.target.value as WorkAgentRole)}>
          <option value="auto">自动分派</option><option value="preprocessing">预处理 Agent</option><option value="analysis">数据分析 Agent</option>
        </select>}
        {mode === "work" && <span className="work-agent-status" aria-live="polite">当前负责：{effectiveAgentRole === "analysis" ? "数据分析 Agent" : "预处理 Agent"}</span>}
        <button className="button button-secondary" type="button" disabled={busy || draftDirty} onClick={() => void newConversation()}>新会话</button>
        {activeConversation && <button className="button button-secondary" type="button" disabled={busy || draftDirty} onClick={() => {
          setError(""); void agentApi.conversation(activeConversation.conversation_id).then(cache).catch((caught) => setError(describeError(caught)));
        }}>刷新会话</button>}
      </div>
      {mode === "work" && <WorkspacePanel
        key={workspacePath}
        path={workspacePath}
        check={workspaceCheck}
        checking={workspaceChecking}
        checkError={workspaceCheckError}
        selectedStage={selectedInputStage}
        stageConfirmed={Boolean(selectedInputStage && selectedInputStage === confirmedWorkspaceStage)}
        busy={busy || draftDirty}
        consentBusy={consentBusy}
        consentGranted={Boolean(workspaceConsent)}
        canAuthorize={canAuthorizeWorkspaceFiles}
        authorizationUnavailableReason={authorizationUnavailableReason}
        onChoose={() => void chooseWorkspace()}
        onPathSubmit={(path) => void selectWorkspacePath(path)}
        onStageChange={setSelectedInputStage}
        onConsent={() => void (workspaceConsent ? revokeWorkspaceConsent() : grantWorkspaceConsent())}
      >
        {workspaceEntries.length > 0 && <details className="workspace-scope"><summary>文件范围（{selectedScope.length ? `已选 ${selectedScope.length} 项` : "默认按需读取"}）</summary><p>仅勾选的文件会暴露给本轮文件工具；不选择则使用整个已授权工作区。</p>{workspaceEntries.map((entry) => <label key={entry.path}><input type="checkbox" checked={selectedScope.includes(entry.path)} onChange={(event) => setSelectedScope((items) => event.target.checked ? [...items, entry.path] : items.filter((item) => item !== entry.path))} /> <code>{entry.path}</code></label>)}</details>}
      </WorkspacePanel>}
      {activeCompositeGoalView && (activeCompositeGoalView.loading || activeCompositeGoalView.error || activeCompositeGoalView.goals.length > 0) && <CompositeGoalProgressPanel
        view={activeCompositeGoalView}
        refreshingAutomatically={hasActiveCompositeGoal}
        onRefresh={() => setCompositeGoalRefreshNonce((value) => value + 1)}
        disabled={busy || draftDirty}
        onSelect={(intentTaskId) => void selectCompositeGoal(intentTaskId)}
      />}
      <div className={`work-review-layout${mode === "work" && currentDraftMessage && planOpen ? " has-plan" : ""}`}>
      <div className="work-dialogue-region">
      {mode === "work" && !registerAnalysisFromPreprocessing && effectiveAgentRole === "analysis" && boundProjectId && <ExternalAnalysisImportPanel projectId={boundProjectId} disabled={busy || draftDirty} selectedImportId={selectedExternalImportId} onSelect={(importId) => { setAnalysisGoalCreationSelection(null); setExternalSource({ projectId: boundProjectId, conversationId: activeConversation?.conversation_id ?? null, importId }); setAgentRole("analysis"); }} />}
      <ConversationHistory busy={busy} messages={visibleMessages.length ? visibleMessages : [WELCOME[mode]]} onApply={(proposal) => void applyWorkspaceProposal(proposal)} onUndo={(proposal) => void undoWorkspaceProposal(proposal)} onRetryAnalysisOutbox={(intentTaskId) => void retryAnalysisOutbox(intentTaskId)} onRetryResultInterpretation={(taskId) => void retryResultInterpretation(taskId)} latestDraftId={latestDraftId} onOpenPlan={() => setPlanOpen(true)} onStatisticsRun={receiveStatisticsRun} conversation={activeConversation} agentRole={effectiveAgentRole} onTaskChange={refreshWorkTask} />
      {mode === "work" && activeRun && <WorkRunPanel run={activeRun} onRunChange={(run) => { setActiveRun(run); updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state }); }} />}
      {runConnectionMessage && <div role="status">{runConnectionMessage} <button type="button" onClick={() => setPollNonce((value) => value + 1)}>重新连接</button></div>}
      <div className="chat-composer">
        {confirmation && openedConfirmation !== confirmationKey && <button className="button button-secondary confirmation-reopen" type="button" disabled={busy || draftDirty} onClick={() => { setError(""); setOpenedConfirmation(confirmationKey); }}>待确认 · {confirmation.title}</button>}
        {draftDirty && <small role="status">请先保存或放弃参数修改，再继续对话或切换会话。</small>}
        {canRegisterAnalysisFromPreprocessing && <div className="preprocessing-analysis-source">
          <label><input
            type="checkbox"
            checked={Boolean(registerAnalysisFromPreprocessing)}
            disabled={busy || draftDirty}
            onChange={(event) => setAnalysisSourceSelection(event.target.checked
              ? { context: preprocessingSourceContext, enabled: true, sourceKey: "" }
              : null)}
          />以指定的预处理结果新建分析目标</label>
          {registerAnalysisFromPreprocessing && <label>预处理来源<select
            aria-label="预处理来源"
            value={selectedPreprocessingSource?.key ?? ""}
            disabled={busy || draftDirty}
            onChange={(event) => setAnalysisSourceSelection({ context: preprocessingSourceContext, enabled: true, sourceKey: event.target.value })}
          >
            <option value="">请明确选择任务和运行</option>
            {preprocessingSources.map((source) => <option key={source.key} value={source.key}>任务 {source.taskId} · 运行 {source.runId}</option>)}
          </select></label>}
        </div>}
        {mode === "work" && effectiveAgentRole === "analysis" && !selectedPreprocessingSource && selectedExternalImportId && <div className="analysis-goal-source">
          <label><input
            type="checkbox"
            checked={createIndependentAnalysisGoal}
            disabled={busy || draftDirty}
            onChange={(event) => setAnalysisGoalCreationSelection(event.target.checked
              ? { context: analysisGoalCreationContext, enabled: true }
              : null)}
          />新建独立分析目标</label>
          <small>仅使用当前已纳入的外部指标；勾选后，本轮将创建独立目标，不会继续当前已选分析目标。</small>
        </div>}
        <textarea ref={composer} disabled={busy || draftDirty} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={mode === "chat" ? "问一个 rs-fMRI 方法问题" : "例如：用当前 DemoData 准备 ALFF 预处理方案"} onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !busy) { event.preventDefault(); void send(); }
        }} />
        <div className="chat-actions">
          {mode === "chat" && <label><input type="checkbox" checked={allowRemoteSearch} disabled={busy || !canSearch} onChange={(event) => setAllowRemoteSearch(event.target.checked)} />联网搜索</label>}
          <label>模型<select disabled={busy} value={selectedModelKey} onChange={(event) => { setSelectedModelKey(event.target.value); try { window.sessionStorage.setItem(MODEL_SELECTION_KEY, event.target.value); } catch { /* Selection still works when browser storage is unavailable. */ } }}><option value="">自动选择</option>{choices.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
          <button className="button button-primary" type="button" disabled={busy || draftDirty || !prompt.trim() || (registerAnalysisFromPreprocessing && !selectedPreprocessingSource)} onClick={() => void send()}>{busy ? "处理中…" : "发送"}</button>
          {busy && pendingTurn && <button className="button button-secondary" type="button" onClick={stopWaiting}>停止等待</button>}
        </div>
      </div>
      </div>
      {mode === "work" && currentDraftMessage?.workDraft && <WorkReviewRegion open={planOpen} onClose={() => setPlanOpen(false)}>
        <div className="work-plan-heading"><div><h2>当前方案</h2><p>核对参数、保存修改，然后确认下一步</p></div><button className="button button-secondary" type="button" onClick={() => setPlanOpen(false)}>收起方案</button></div>
        <WorkDraftPanel key={`${currentDraftMessage.id}:${currentDraftMessage.workDraft.hash}`} snapshot={currentDraftMessage.workDraft} issues={currentDraftMessage.draftIssues} preview={currentDraftMessage.planPreview} busy={busy} onSave={saveDraft} onDirty={setDraftDirty} confirmation={confirmation} confirmLabel={confirmation?.options.length === 1 ? confirmation.options[0].label : "审查并继续"} onConfirm={() => { setError(""); setOpenedConfirmation(confirmationKey); }} />
        <div className="work-plan-secondary"><button className="button button-secondary" type="button" disabled={busy || draftDirty || currentDraftMessage.workDraft.consumed || Boolean(currentDraftMessage.draftIssues?.length)} onClick={() => void prepareReview()}>重新校验方案与启动确认</button></div>
      </WorkReviewRegion>}
      </div>
    </section>
  </main>;
}
