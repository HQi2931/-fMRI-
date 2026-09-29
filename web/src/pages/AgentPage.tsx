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
import { WorkspacePanel } from "../components/WorkspacePanel";
import { updateWorkspace, useWorkspace } from "../workspace";

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
};
type ModelChoice = {
  key: string;
  profileId: string;
  model: string;
  label: string;
  capabilities: ModelProfile["profile"]["capabilities"];
};

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
          text: item.content,
        };
      }
      const message: Message = {
        id: item.message_id,
        role: "assistant",
        text: item.content,
        citations: (item.payload.chat as { citations?: Citation[] } | undefined)?.citations,
        steps: pending,
      };
      const proposal = item.payload.workspace_change_proposal as WorkspaceProposal | undefined;
      const workspaceCitations = item.payload.workspace_citations as Array<{ path: string; locator: string }> | undefined;
      if (proposal) message.proposal = proposal;
      if (workspaceCitations) message.workspaceCitations = workspaceCitations;
      pending = undefined;
      return message;
    });
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

function ConversationHistory({ messages, busy, onApply, onUndo }: { messages: Message[]; busy: boolean; onApply: (proposal: WorkspaceProposal) => void; onUndo: (proposal: WorkspaceProposal) => void }) {
  return <div className="chat-history" aria-live="polite">
    {messages.map((item) => <div className={`chat-bubble chat-${item.role}`} key={item.id}>
      <span>{item.role === "assistant" ? "Agent" : "你"}</span>
      {item.steps && item.steps.length > 0 && <ol className="chat-tool-steps" aria-label="Agent 工具步骤">
        {item.steps.map((step) => <li key={step.key} data-tier={step.tier ?? "unknown"} data-status={step.status}>
          <code>{step.name}</code>
          {step.tier && <span className="tool-tier">{step.tier}</span>}
          <span className="tool-status">{step.status}</span>
          {step.durationMs !== null && <span className="tool-duration">{step.durationMs} ms</span>}
        </li>)}
      </ol>}
      <p>{item.text}</p>
      {item.streamingStatus && <small role="status">{item.streamingStatus}</small>}
      {item.workspaceCitations && <ul className="workspace-citations">{item.workspaceCitations.map((citation) => <li key={`${citation.path}:${citation.locator}`}><code>{citation.path}</code> · {citation.locator}</li>)}</ul>}
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

function runStageLabel(run: Run): string {
  if (run.state === "qc_review") return "等待 QC 审核";
  if (run.state === "succeeded" && run.qc_status === "skipped") return "已完成 · 已跳过 QC";
  if (run.state === "succeeded") return "已完成";
  if (["failed_terminal", "timed_out", "cancelled"].includes(run.state)) return "已结束";
  return run.stage;
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
  const [selectedModelKey, setSelectedModelKey] = useState("");
  const [allowRemoteSearch, setAllowRemoteSearch] = useState(false);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
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
  const visibleMessages = messagesFrom(activeConversation);
  if (pendingTurn?.mode === mode) visibleMessages.push(
    { id: "pending-user", role: "user", text: pendingTurn.user },
    { id: "pending-assistant", role: "assistant", text: pendingTurn.text, streamingStatus: pendingTurn.status },
  );
  const selectedModel = choices.find((item) => item.key === selectedModelKey);
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
    const runId = activeConversation?.active_run_id ?? (mode === "work" ? workspace.runId : undefined);
    if (!runId) { setActiveRun(null); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const terminal = new Set(["succeeded", "failed_terminal", "timed_out", "cancelled", "qc_review"]);
    const poll = async () => {
      try {
        const run = await api.run(runId, controller.signal);
        setActiveRun(run);
        updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state });
        if (!terminal.has(run.state)) timer = setTimeout(() => void poll(), 2000);
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
      }
    };
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [activeConversation?.active_run_id, mode, workspace.runId]);

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
        setSelectedInputStage(result.selected_input_stage ?? "");
      })
      .catch((caught) => { if (!controller.signal.aborted) setWorkspaceCheckError(describeError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setWorkspaceChecking(false); });
    return () => controller.abort();
  }, [mode, workspacePath]);

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
    setConversations((items) => [conversation, ...items.filter((item) => item.conversation_id !== conversation.conversation_id)]);
    setActiveIds((items) => ({ ...items, [conversation.mode]: conversation.conversation_id }));
    if (conversation.mode === "work") {
      applyConversationContext(conversation);
      if (conversation.workspace_path) setWorkspacePath(conversation.workspace_path);
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

  async function send(): Promise<void> {
    const content = prompt.trim();
    if (!content || busy) return;
    const controller = new AbortController();
    turnController.current = controller;
    setPendingTurn({ mode, user: content, text: "", status: "正在生成回答…" });
    setPrompt(""); setBusy(true); setError("");
    try {
      const conversationId = await ensureConversation(mode);
      if (controller.signal.aborted) return;
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
        setError(describeError(caught)); setPrompt(content);
        setPendingTurn((pending) => pending && { ...pending, status: "回答未完成，临时内容可能不完整。请重试。" });
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      if (turnController.current === controller) turnController.current = null;
    }
  }

  return <main className="agent-shell">
    <div className="agent-mode-switch" role="tablist" aria-label="对话模式">
      {(["chat", "work"] as ConversationMode[]).map((item) => <button
        key={item}
        className={mode === item ? "agent-mode active" : "agent-mode"}
        type="button"
        role="tab"
        disabled={busy}
        aria-selected={mode === item}
        onClick={() => { setMode(item); setPrompt(""); setError(""); setPendingTurn(null); }}
      ><strong>{item === "chat" ? "Chat" : "Work"}</strong><small>{item === "chat" ? "方法问答" : "执行任务"}</small></button>)}
    </div>
    <Feedback message={error} error={Boolean(error)} />
    <section className="conversation-panel">
      <div className="conversation-toolbar">
        <select disabled={busy} aria-label="当前会话" value={activeIds[mode] ?? ""} onChange={(event) => {
          const selected = conversations.find((item) => item.conversation_id === event.target.value);
          if (selected) { cache(selected); setPendingTurn(null); }
        }}><option value="">新会话</option>{modeConversations.map((item) => <option key={item.conversation_id} value={item.conversation_id}>{item.title}</option>)}</select>
        <button className="button button-secondary" type="button" disabled={busy} onClick={() => void newConversation()}>新会话</button>
      </div>
      {mode === "work" && <WorkspacePanel
        key={workspacePath}
        path={workspacePath}
        check={workspaceCheck}
        checking={workspaceChecking}
        checkError={workspaceCheckError}
        selectedStage={selectedInputStage}
        busy={busy}
        consentBusy={consentBusy}
        consentGranted={Boolean(workspaceConsent)}
        canAuthorize={canAuthorizeWorkspaceFiles}
        authorizationUnavailableReason={authorizationUnavailableReason}
        onChoose={() => void chooseWorkspace()}
        onStageChange={setSelectedInputStage}
        onConsent={() => void (workspaceConsent ? revokeWorkspaceConsent() : grantWorkspaceConsent())}
      >
        {workspaceEntries.length > 0 && <details className="workspace-scope"><summary>文件范围（{selectedScope.length ? `已选 ${selectedScope.length} 项` : "默认按需读取"}）</summary><p>仅勾选的文件会暴露给本轮文件工具；不选择则使用整个已授权工作区。</p>{workspaceEntries.map((entry) => <label key={entry.path}><input type="checkbox" checked={selectedScope.includes(entry.path)} onChange={(event) => setSelectedScope((items) => event.target.checked ? [...items, entry.path] : items.filter((item) => item !== entry.path))} /> <code>{entry.path}</code></label>)}</details>}
      </WorkspacePanel>}
      <ConversationHistory busy={busy} messages={visibleMessages.length ? visibleMessages : [WELCOME[mode]]} onApply={(proposal) => void applyWorkspaceProposal(proposal)} onUndo={(proposal) => void undoWorkspaceProposal(proposal)} />
      {mode === "work" && activeRun && <div className="run-inline" role="status">
        <strong>任务状态：{runStageLabel(activeRun)}</strong>
        {typeof activeRun.stage_progress === "number" && activeRun.state === "running" && <span>当前进度：{Math.round(activeRun.stage_progress * 100)}%</span>}
        {activeRun.error && <details><summary>失败原因</summary><pre>{activeRun.error}</pre></details>}
      </div>}
      <div className="chat-composer">
        <textarea disabled={busy} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={mode === "chat" ? "问一个 rs-fMRI 方法问题" : "例如：用当前 DemoData 准备 ALFF 预处理方案"} onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !busy) { event.preventDefault(); void send(); }
        }} />
        <div className="chat-actions">
          {mode === "chat" && <label><input type="checkbox" checked={allowRemoteSearch} disabled={busy || !canSearch} onChange={(event) => setAllowRemoteSearch(event.target.checked)} />联网搜索</label>}
          <label>模型<select disabled={busy} value={selectedModelKey} onChange={(event) => setSelectedModelKey(event.target.value)}><option value="">自动选择</option>{choices.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
          <button className="button button-primary" type="button" disabled={busy || !prompt.trim()} onClick={() => void send()}>{busy ? "处理中…" : "发送"}</button>
        </div>
      </div>
    </section>
  </main>;
}
