import { useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  describeError,
  type Citation,
  type Conversation,
  type ConversationMode,
  type ModelProfile,
  type Run,
  type WorkspaceConsent,
  type WorkspaceCheck,
  type WorkspaceProposal,
} from "../api/client";
import { Feedback } from "../components/Ui";
import { ConfirmationDialog } from "../components/ConfirmationDialog";
import { currentConfirmation } from "../components/confirmation";
import { ConversationText } from "../components/ConversationText";
import { WorkTimingDetails } from "../components/WorkTimingDetails";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { WorkDraftPanel } from "../components/WorkDraftPanel";
import { WorkRunPanel } from "../components/WorkRunPanel";
import { readWorkDraft, type WorkDraftSnapshot } from "../components/workDraft";
import { updateWorkspace, useWorkspace } from "../workspace";
import "./agent-work.css";

type ToolStep = {
  key: string;
  name: string;
  status: string;
  tier: string | null;
  durationMs: number | null;
};
type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  steps?: ToolStep[];
  workspaceCitations?: Array<{ path: string; locator: string }>;
  proposal?: WorkspaceProposal;
  streamingStatus?: string;
  workDraft?: WorkDraftSnapshot;
  draftIssues?: string[];
  planPreview?: unknown;
  workTimings?: unknown;
};
type ModelChoice = {
  key: string;
  profileId: string;
  model: string;
  label: string;
  capabilities: ModelProfile["profile"]["capabilities"];
};

const MODEL_SELECTION_KEY = "rsfmri-agent-model-v1";
function rememberedModel(): string {
  try { return window.sessionStorage.getItem(MODEL_SELECTION_KEY) ?? ""; }
  catch { return ""; }
}

const WELCOME: Record<ConversationMode, Message> = {
  chat: { id: "chat-welcome", role: "assistant", text: "可以问我 rs-fMRI、DPABI 与统计方法问题。" },
  work: { id: "work-welcome", role: "assistant", text: "告诉我你的目标。我会在对话中准备方案、列出待确认参数，并在明确确认后启动任务。" },
};

function messagesFrom(conversation?: Conversation): Message[] {
  if (!conversation) return [];
  // A tool-loop turn writes one row per step, keyed to the user message that
  // triggered it, so the steps are re-attached to its reply on every reload.
  const stepsByUser = new Map<string, ToolStep[]>();
  for (const call of conversation.tool_calls ?? []) {
    if (call.step_index === null || call.step_index === undefined) continue;
    const steps = stepsByUser.get(call.user_message_id) ?? [];
    steps.push({
      key: `${call.tool_call_id}`,
      name: call.tool_name,
      status: call.status,
      tier: call.risk_tier ?? null,
      durationMs: call.duration_ms ?? null,
    });
    stepsByUser.set(call.user_message_id, steps);
  }
  let pending: ToolStep[] | undefined;
  return conversation.messages
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => {
      if (item.role === "user") {
        pending = stepsByUser.get(item.message_id);
        return {
          id: item.message_id,
          role: "user" as const,
          text: item.content.startsWith("批准并启动 ") ? "已确认本次方案并启动计算。"
            : item.content.startsWith("确认起始阶段 ") ? "已确认预处理起始阶段。"
            : item.content.startsWith("确认方案参数 ") ? "已审查方案参数。" : item.content,
        };
      }
      const message: Message = {
        id: item.message_id,
        role: "assistant",
        text: item.content,
        citations: (item.payload.chat as { citations?: Citation[] } | undefined)?.citations,
        steps: pending,
        workDraft: readWorkDraft(item.payload.work_draft),
        draftIssues: Array.isArray(item.payload.draft_issues) ? item.payload.draft_issues.filter((value): value is string => typeof value === "string") : readWorkDraft(item.payload.work_draft)?.review_issues ?? [],
        planPreview: item.payload.plan_preview,
        workTimings: item.payload.work_timings,
      };
      const proposal = item.payload.workspace_change_proposal as WorkspaceProposal | undefined;
      const workspaceCitations = item.payload.workspace_citations as Array<{ path: string; locator: string }> | undefined;
      if (proposal) message.proposal = proposal;
      if (workspaceCitations) message.workspaceCitations = workspaceCitations;
      pending = undefined;
      return message;
    });
}

function confirmedInputStage(conversation: Conversation | undefined, path: string): string | undefined {
  if (!conversation || conversation.workspace_path !== path) return undefined;
  for (const message of [...conversation.messages].reverse()) {
    const stage = message.payload.selected_input_stage;
    if (typeof stage === "string") return stage;
  }
  return undefined;
}

function applyConversationContext(conversation: Conversation): void {
  const patch: Record<string, unknown> = {
    projectId: conversation.project_id ?? undefined,
    runId: conversation.active_run_id ?? undefined,
    workspacePath: conversation.workspace_path ?? undefined,
    planRevisionId: undefined,
    planVersion: undefined,
    planHash: undefined,
    planState: undefined,
  };
  for (const message of conversation.messages) {
    const payload = message.payload as Record<string, unknown>;
    const project = payload.project as Record<string, unknown> | undefined;
    const dataset = payload.dataset as Record<string, unknown> | undefined;
    const manifest = payload.manifest as Record<string, unknown> | undefined;
    const plan = payload.plan_revision as Record<string, unknown> | undefined;
    const run = payload.run as Record<string, unknown> | undefined;
    if (project) { patch.projectId = project.project_id; patch.projectVersion = project.version; }
    if (dataset) { patch.datasetId = dataset.dataset_id; patch.datasetVersion = dataset.version; }
    if (manifest) { patch.manifestId = manifest.manifest_id; patch.manifestHash = manifest.content_hash; }
    if (plan) {
      patch.planRevisionId = plan.plan_revision_id;
      patch.planVersion = plan.version;
      patch.planHash = plan.plan_hash;
      patch.planState = plan.state;
    }
    if (run) { patch.runId = run.run_id; patch.runVersion = run.version; patch.runState = run.state; }
    const draft = payload.work_draft as { compiled?: boolean } | undefined;
    if (draft && !draft.compiled) {
      patch.planRevisionId = undefined;
      patch.planVersion = undefined;
      patch.planHash = undefined;
      patch.planState = undefined;
    }
  }
  updateWorkspace(patch);
}

function ConversationHistory({ messages, busy, onApply, onUndo, latestDraftId, onOpenPlan }: { messages: Message[]; busy: boolean; onApply: (proposal: WorkspaceProposal) => void; onUndo: (proposal: WorkspaceProposal) => void; latestDraftId?: string; onOpenPlan: () => void }) {
  return <div className="chat-history" aria-live="polite">
    {messages.map((item) => <div className={`chat-bubble chat-${item.role}`} key={item.id}>
      <span>{item.role === "assistant" ? "Agent" : "你"}</span>
      {item.steps && item.steps.length > 0 && <details className="chat-technical"><summary>查看工具检查详情（{item.steps.length} 项）</summary><ol className="chat-tool-steps" aria-label="Agent 工具步骤">
        {item.steps.map((step) => <li key={step.key} data-tier={step.tier ?? "unknown"} data-status={step.status}>
          <code>{step.name}</code>
          {step.tier && <span className="tool-tier">{step.tier}</span>}
          <span className="tool-status">{step.status}</span>
          {step.durationMs !== null && <span className="tool-duration">{step.durationMs} ms</span>}
        </li>)}
      </ol></details>}
      <ConversationText text={item.text} />
      <WorkTimingDetails value={item.workTimings} />
      {item.streamingStatus && <small role="status">{item.streamingStatus}</small>}
      {item.workDraft && (item.id === latestDraftId
        ? <button className="button button-secondary" type="button" onClick={onOpenPlan}>查看当前方案{item.workDraft.version ? ` · 第 ${item.workDraft.version} 版` : ""}</button>
        : <details><summary>历史方案{item.workDraft.version ? ` · 第 ${item.workDraft.version} 版` : ""}</summary><WorkDraftPanel snapshot={item.workDraft} issues={item.draftIssues} preview={item.planPreview} readOnly onSave={async () => {}} /></details>)}
      {item.workspaceCitations && item.workspaceCitations.length > 0 && <details className="chat-technical"><summary>查看文件证据（{item.workspaceCitations.length} 项）</summary><ul className="workspace-citations">{item.workspaceCitations.map((citation) => <li key={`${citation.path}:${citation.locator}`}><code>{citation.path}</code> · {citation.locator}</li>)}</ul></details>}
      {item.proposal && <details className="workspace-proposal"><summary>查看文件修改 diff（{item.proposal.proposal_hash.slice(0, 12)}）</summary><p>不可变补丁哈希：<code>{item.proposal.proposal_hash}</code></p><p>文件数：{item.proposal.edits.length} · 增加 {patchStats(item.proposal.diff).added} 行 · 删除 {patchStats(item.proposal.diff).removed} 行</p><pre>{item.proposal.diff}</pre>{item.proposal.status === "proposed" && <button className="button button-primary" type="button" disabled={busy} onClick={() => onApply(item.proposal!)}>确认应用补丁</button>}{item.proposal.status === "applied" && <button className="button button-secondary" type="button" disabled={busy} onClick={() => onUndo(item.proposal!)}>撤销本次补丁</button>}</details>}
      {item.citations?.map((citation) => <a key={citation.citation_id} href={citation.source} target="_blank" rel="noreferrer">[{citation.citation_id}] {citation.title}</a>)}
    </div>)}
  </div>;
}

function modelChoice(profile: ModelProfile, model: string): ModelChoice {
  let provider = profile.profile.provider;
  try { provider = new URL(profile.profile.base_url).hostname; } catch { /* validated server-side */ }
  return {
    key: `${profile.profile.id}:${model}`,
    profileId: profile.profile.id,
    model,
    label: `${provider} · ${model}`,
    capabilities: profile.profile.capabilities,
  };
}

function patchStats(diff: string): { added: number; removed: number } {
  let added = 0; let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
    if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
  }
  return { added, removed };
}

export function AgentPage() {
  const workspace = useWorkspace();
  const initialWorkspace = useRef(workspace.workspacePath ?? "").current;
  const [mode, setMode] = useState<ConversationMode>("work");
  const [conversations, setConversations] = useState<Conversation[]>([]);
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
  const [planOpen, setPlanOpen] = useState(() => window.innerWidth > 980);
  const [pollNonce, setPollNonce] = useState(0);
  const [runConnectionMessage, setRunConnectionMessage] = useState("");
  const composer = useRef<HTMLTextAreaElement>(null);
  const turnController = useRef<AbortController | null>(null);
  const [pendingTurn, setPendingTurn] = useState<{ mode: ConversationMode; user: string; text: string; status: string } | null>(null);
  useEffect(() => () => turnController.current?.abort(), []);
  const [consentBusy, setConsentBusy] = useState(false);
  const [workspaceConsent, setWorkspaceConsent] = useState<WorkspaceConsent | null>(null);
  const [workspaceEntries, setWorkspaceEntries] = useState<Array<{ path: string; kind: string; readable?: boolean }>>([]);
  const [selectedScope, setSelectedScope] = useState<string[]>([]);
  const [workspaceCheck, setWorkspaceCheck] = useState<WorkspaceCheck | null>(null);
  const [workspaceChecking, setWorkspaceChecking] = useState(false);
  const [workspaceCheckError, setWorkspaceCheckError] = useState("");
  const [selectedInputStage, setSelectedInputStage] = useState<string>("");

  const modeConversations = useMemo(() => conversations.filter((item) => item.mode === mode), [conversations, mode]);
  const activeConversation = conversations.find((item) => item.conversation_id === activeIds[mode]);
  const boundToSelectedWorkspace = activeConversation?.workspace_path === workspacePath;
  const boundProjectId = boundToSelectedWorkspace ? activeConversation?.project_id ?? null : null;
  const offeredConfirmation = mode === "work" && boundToSelectedWorkspace
    ? currentConfirmation(activeConversation?.messages ?? [], workspacePath) : null;
  const confirmation = offeredConfirmation?.request ?? null;
  const confirmationKey = offeredConfirmation ? `${activeConversation?.conversation_id}:${offeredConfirmation.messageId}` : null;
  const visibleMessages = messagesFrom(activeConversation);
  const latestDraftId = mode === "work" && boundToSelectedWorkspace ? [...visibleMessages].reverse().find((item) => item.workDraft)?.id : undefined;
  const currentDraftMessage = visibleMessages.find((item) => item.id === latestDraftId);
  if (pendingTurn?.mode === mode) visibleMessages.push(
    { id: "pending-user", role: "user", text: pendingTurn.user },
    { id: "pending-assistant", role: "assistant", text: pendingTurn.text, streamingStatus: pendingTurn.status },
  );
  const selectedModel = choices.find((item) => item.key === selectedModelKey);
  const confirmedWorkspaceStage = confirmedInputStage(activeConversation, workspacePath);
  const canAuthorizeWorkspaceFiles = Boolean(selectedModel?.capabilities.includes("tool_use"));
  const authorizationUnavailableReason = !selectedModel
    ? "先选择一个具体模型，自动选择不能授予文件授权"
    : "模型配置未声明工具调用；请在设置中确认并启用 tool_use";
  const canSearch = selectedModel?.capabilities.includes("web_search") ?? choices.some((item) => item.capabilities.includes("web_search"));

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([api.profiles(controller.signal), api.conversations(undefined, controller.signal)])
      .then(async ([profiles, loaded]) => {
        const lists = await Promise.allSettled(profiles.map((profile) => api.listProviderModels({
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
          applyConversationContext(work);
          if (work.workspace_path && !initialWorkspace) setWorkspacePath(work.workspace_path);
        }
      })
      .catch((caught) => { if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught)); });
    return () => controller.abort();
  }, [initialWorkspace]);

  useEffect(() => {
    const runId = mode === "work" && boundToSelectedWorkspace ? activeConversation?.active_run_id : null;
    setActiveRun(null);
    if (!runId) { setActiveRun(null); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const terminal = new Set(["succeeded", "failed_retryable", "failed_terminal", "timed_out", "cancelled", "qc_review"]);
    let failures = 0;
    const poll = async () => {
      try {
        const run = await api.run(runId, controller.signal);
        setActiveRun(run);
        failures = 0;
        setRunConnectionMessage("");
        updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state });
        if (!terminal.has(run.state)) timer = setTimeout(() => void poll(), 2000);
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
  }, [activeConversation?.conversation_id, activeConversation?.active_run_id, boundToSelectedWorkspace, mode, pollNonce]);

  useEffect(() => {
    if (mode !== "work" || !activeConversation || !workspacePath) { setWorkspaceEntries([]); return; }
    const controller = new AbortController();
    api.workspaceFiles(activeConversation.conversation_id, "", controller.signal)
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
    api.checkWorkspace({ path: workspacePath }, controller.signal)
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
    api.workspaceConsents(activeConversation.conversation_id, controller.signal)
      .then((items) => setWorkspaceConsent(items.find((item) => item.profile_id === selectedModel.profileId && item.model === selectedModel.model && item.valid_for_current_context && !item.revoked_at) ?? null))
      .catch(() => setWorkspaceConsent(null));
    return () => controller.abort();
  }, [activeConversation, mode, selectedModel, workspacePath]);

  function cache(conversation: Conversation): void {
    setDraftDirty(false);
    setConversations((items) => [conversation, ...items.filter((item) => item.conversation_id !== conversation.conversation_id)]);
    setActiveIds((items) => ({ ...items, [conversation.mode]: conversation.conversation_id }));
    if (conversation.mode === "work") {
      applyConversationContext(conversation);
      if (conversation.workspace_path) setWorkspacePath(conversation.workspace_path);
      const stage = conversation.messages.at(-1)?.payload.selected_input_stage;
      if (typeof stage === "string") setSelectedInputStage(stage);
    }
  }

  async function ensureConversation(target: ConversationMode): Promise<string> {
    const existing = activeIds[target];
    if (existing) return existing;
    const created = await api.createConversation({
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
      const created = await api.createConversation({
        mode,
        project_id: mode === "work" ? boundProjectId : null,
        workspace_path: mode === "work" ? workspacePath.trim() || null : null,
        preferred_profile_id: selectedModel?.profileId ?? null,
      });
      cache(created); setPendingTurn(null);
    } catch (caught) { setError(describeError(caught)); } finally { setBusy(false); }
  }

  async function chooseWorkspace(): Promise<void> {
    setBusy(true); setError("");
    try {
      const picked = await api.pickWorkspace();
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
      const consent = await api.grantWorkspaceConsent(conversationId, {
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
    try { await api.revokeWorkspaceConsent(activeConversation.conversation_id, workspaceConsent.consent_id); setWorkspaceConsent(null); }
    catch (caught) { setError(describeError(caught)); }
    finally { setConsentBusy(false); }
  }

  async function applyWorkspaceProposal(proposal: WorkspaceProposal): Promise<void> {
    if (!activeConversation) return;
    setBusy(true); setError("");
    try {
      const applied = await api.applyWorkspaceProposal(activeConversation.conversation_id, proposal.proposal_id, proposal.proposal_hash);
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
      const undone = await api.undoWorkspaceProposal(activeConversation.conversation_id, proposal.proposal_id);
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
    const controller = new AbortController();
    turnController.current = controller;
    const deadline = window.setTimeout(() => {
      if (turnController.current !== controller) return;
      controller.abort();
      setBusy(false);
      setPrompt(content);
      setPendingTurn(null);
      setError("等待回答已超过 5 分钟，请刷新会话查看已保存结果后重试。");
    }, 300_000);
    const confirmationLabel = confirmation?.options.find((option) => option.command === content)?.label;
    setPendingTurn({ mode, user: confirmationLabel ?? content, text: "", status: confirmationCommand ? "正在核对方案并提交操作…" : "正在生成回答…" });
    setPrompt(""); setBusy(true); setError("");
    try {
      const conversationId = await ensureConversation(mode);
      if (controller.signal.aborted) return;
      if (confirmationCommand) {
        const turn = await api.confirmWorkAction(conversationId, confirmationCommand, currentDraftMessage?.workDraft?.hash ?? null, controller.signal);
        if (!controller.signal.aborted) { cache(turn.conversation); setPendingTurn(null); setOpenedConfirmation(null); }
        return;
      }
      const turn = await api.streamConversationTurn(conversationId, {
        content,
        workspace_path: mode === "work" ? workspacePath.trim() || null : null,
        preferred_profile_id: selectedModel?.profileId ?? null,
        model: selectedModel?.model ?? null,
        allow_remote_search: mode === "chat" && allowRemoteSearch,
        project_id: mode === "work" ? boundProjectId : null,
        target_run_id: mode === "work" && boundToSelectedWorkspace ? workspace.runId ?? null : null,
        plan_revision_id: mode === "work" && boundToSelectedWorkspace ? workspace.planRevisionId ?? null : null,
        expected_plan_hash: mode === "work" && boundToSelectedWorkspace ? workspace.planHash ?? null : null,
        workspace_file_scope: mode === "work" && selectedScope.length ? { paths: selectedScope } : null,
        selected_input_stage: mode === "work" && selectedInputStage ? selectedInputStage : null,
        real_execution_confirmed: false,
      }, (event) => {
        if (controller.signal.aborted) return;
        if (event.type === "reset") setPendingTurn((pending) => pending && { ...pending, text: "" });
        if (event.type === "delta") setPendingTurn((pending) => pending && { ...pending, text: pending.text + event.text, status: "正在生成回答…" });
        if (event.type === "status") setPendingTurn((pending) => pending && { ...pending, status: event.message });
      }, controller.signal);
      if (!controller.signal.aborted) { cache(turn.conversation); setPendingTurn(null); }
    } catch (caught) {
      if (!controller.signal.aborted) {
        if (confirmationCommand && activeConversation) {
          setPendingTurn((pending) => pending && { ...pending, status: "正在确认操作是否已保存…" });
          try {
            // One read-only recovery. Never repeat an execution request here.
            const recovered = await api.conversation(activeConversation.conversation_id, controller.signal);
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
        setPendingTurn((pending) => pending && { ...pending, status: "回答未完成，临时内容可能不完整。请重试。" });
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
      await api.checkWorkspace({ path });
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
    setPrompt(pendingTurn?.user ?? "");
    setPendingTurn(null);
    setBusy(false);
    setError("已停止等待。服务端可能继续保存本轮结果；请刷新会话查看后再发送。");
  }

  async function saveDraft(hash: string, patch: Record<string, unknown>): Promise<void> {
    if (!activeConversation || busy) throw new Error("当前会话正在处理，请稍后重试");
    setBusy(true);
    try { const turn = await api.patchWorkDraft(activeConversation.conversation_id, hash, patch); cache(turn.conversation); }
    finally { setBusy(false); }
  }

  async function prepareReview(): Promise<void> {
    if (!activeConversation || !currentDraftMessage?.workDraft || busy || draftDirty) return;
    setBusy(true); setError("");
    try { cache((await api.prepareWorkReview(activeConversation.conversation_id, currentDraftMessage.workDraft.hash)).conversation); }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  return <main className="agent-shell">
    {confirmation && !draftDirty && confirmationKey === openedConfirmation && <ConfirmationDialog
      key={confirmationKey} request={confirmation} busy={busy} error={error}
      onConfirm={(command) => void send(command)}
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
          if (selected) { cache(selected); setPendingTurn(null); }
        }}><option value="">新会话</option>{modeConversations.map((item) => <option key={item.conversation_id} value={item.conversation_id}>{item.title}</option>)}</select>
        <button className="button button-secondary" type="button" disabled={busy || draftDirty} onClick={() => void newConversation()}>新会话</button>
        {activeConversation && <button className="button button-secondary" type="button" disabled={busy || draftDirty} onClick={() => {
          setError(""); void api.conversation(activeConversation.conversation_id).then(cache).catch((caught) => setError(describeError(caught)));
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
      <div className={`work-review-layout${mode === "work" && currentDraftMessage && planOpen ? " has-plan" : ""}`}>
      <div className="work-dialogue-region"><ConversationHistory busy={busy} messages={visibleMessages.length ? visibleMessages : [WELCOME[mode]]} onApply={(proposal) => void applyWorkspaceProposal(proposal)} onUndo={(proposal) => void undoWorkspaceProposal(proposal)} latestDraftId={latestDraftId} onOpenPlan={() => setPlanOpen(true)} />
      {mode === "work" && activeRun && <WorkRunPanel run={activeRun} onRunChange={(run) => { setActiveRun(run); updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state }); }} />}
      {runConnectionMessage && <div role="status">{runConnectionMessage} <button type="button" onClick={() => setPollNonce((value) => value + 1)}>重新连接</button></div>}
      </div>
      {mode === "work" && currentDraftMessage?.workDraft && <aside className="work-plan-region" aria-label="当前方案审查" hidden={!planOpen}>
        <div className="work-plan-heading"><h2>当前方案</h2><button type="button" onClick={() => setPlanOpen(false)}>收起方案</button></div>
        <WorkDraftPanel key={`${currentDraftMessage.id}:${currentDraftMessage.workDraft.hash}`} snapshot={currentDraftMessage.workDraft} issues={currentDraftMessage.draftIssues} preview={currentDraftMessage.planPreview} busy={busy} onSave={saveDraft} onDirty={setDraftDirty} confirmation={confirmation} confirmLabel={confirmation?.options.length === 1 ? confirmation.options[0].label : "审查并继续"} onConfirm={() => { setError(""); setOpenedConfirmation(confirmationKey); }} />
        <button className="button button-secondary" type="button" disabled={busy || draftDirty || Boolean(currentDraftMessage.draftIssues?.length)} onClick={() => void prepareReview()}>重新校验方案与启动确认</button>
      </aside>}
      </div>
      <div className="chat-composer">
        {confirmation && openedConfirmation !== confirmationKey && <button className="button button-secondary confirmation-reopen" type="button" disabled={busy || draftDirty} onClick={() => { setError(""); setOpenedConfirmation(confirmationKey); }}>待确认 · {confirmation.title}</button>}
        {draftDirty && <small role="status">请先保存或放弃参数修改，再继续对话或切换会话。</small>}
        <textarea ref={composer} disabled={busy || draftDirty} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={mode === "chat" ? "问一个 rs-fMRI 方法问题" : "例如：用当前 DemoData 准备 ALFF 预处理方案"} onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !busy) { event.preventDefault(); void send(); }
        }} />
        <div className="chat-actions">
          {mode === "chat" && <label><input type="checkbox" checked={allowRemoteSearch} disabled={busy || !canSearch} onChange={(event) => setAllowRemoteSearch(event.target.checked)} />联网搜索</label>}
          <label>模型<select disabled={busy} value={selectedModelKey} onChange={(event) => { setSelectedModelKey(event.target.value); try { window.sessionStorage.setItem(MODEL_SELECTION_KEY, event.target.value); } catch { /* Selection still works when browser storage is unavailable. */ } }}><option value="">自动选择</option>{choices.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
          <button className="button button-primary" type="button" disabled={busy || draftDirty || !prompt.trim()} onClick={() => void send()}>{busy ? "处理中…" : "发送"}</button>
          {busy && pendingTurn && <button className="button button-secondary" type="button" onClick={stopWaiting}>停止等待</button>}
        </div>
      </div>
    </section>
  </main>;
}
