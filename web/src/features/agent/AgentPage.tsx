import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  describeError,
  ApiError,
} from "../../api/transport";
import type {
  Conversation,
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

export function AgentPage() {
  const workspace = useWorkspace();
  const initialWorkspace = useRef(workspace.workspacePath ?? "").current;
  const [mode, setMode] = useState<ConversationMode>("work");
  const [agentRole, setAgentRole] = useState<WorkAgentRole>("auto");
  const [externalSource, setExternalSource] = useState<{ projectId: string; conversationId: string | null; importId: string | null } | null>(null);
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

  const modeConversations = useMemo(() => conversations.filter((item) => item.mode === mode), [conversations, mode]);
  const activeConversation = conversations.find((item) => item.conversation_id === activeIds[mode]);
  const currentConversationId = useRef(activeConversation?.conversation_id);
  currentConversationId.current = activeConversation?.conversation_id;
  const boundToSelectedWorkspace = activeConversation?.workspace_path === workspacePath;
  const boundProjectId = boundToSelectedWorkspace ? activeConversation?.project_id ?? null : null;
  const latestAnalysisPayload = [...(activeConversation?.messages ?? [])].reverse()
    .find((message) => message.role === "assistant" && message.payload.agent_role === "analysis" && message.payload.analysis_draft)?.payload.analysis_draft;
  const savedAnalysisDraft = latestAnalysisPayload && typeof latestAnalysisPayload === "object" && "draft" in latestAnalysisPayload ? latestAnalysisPayload.draft : null;
  const savedExternalImportId = savedAnalysisDraft && typeof savedAnalysisDraft === "object" && "external_import_id" in savedAnalysisDraft && typeof savedAnalysisDraft.external_import_id === "string" ? savedAnalysisDraft.external_import_id : null;
  const selectedExternalImportId = agentRole === "preprocessing" || !boundProjectId ? null
    : externalSource?.projectId === boundProjectId && externalSource?.conversationId === (activeConversation?.conversation_id ?? null) ? externalSource.importId : savedExternalImportId;
  const routedRole = [...(activeConversation?.messages ?? [])].reverse()
    .find((message) => message.role === "assistant" && (message.payload.agent_role === "analysis" || message.payload.agent_role === "preprocessing"))
    ?.payload.agent_role;
  const effectiveAgentRole = agentRole === "auto" ? (activeConversation?.foreground_agent_role ?? (routedRole === "analysis" ? "analysis" : "preprocessing")) : agentRole;
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
  const confirmedWorkspaceStage = confirmedInputStage(activeConversation, workspacePath);
  const canAuthorizeWorkspaceFiles = Boolean(selectedModel?.capabilities.includes("tool_use"));
  const authorizationUnavailableReason = !selectedModel
    ? "先选择一个具体模型，自动选择不能授予文件授权"
    : "模型配置未声明工具调用；请在设置中确认并启用 tool_use";
  const canSearch = selectedModel?.capabilities.includes("web_search") ?? choices.some((item) => item.capabilities.includes("web_search"));

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

  useEffect(() => {
    const taskRunId = activeConversation?.work_tasks?.find((item) => item.agent_role === effectiveAgentRole)?.run_id;
    const runId = mode === "work" && boundToSelectedWorkspace ? taskRunId ?? activeConversation?.active_run_id : null;
    setActiveRun(null);
    if (!runId) { setActiveRun(null); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const terminal = new Set(["succeeded", "failed_retryable", "failed_terminal", "timed_out", "cancelled", "qc_review"]);
    let failures = 0;
    const poll = async () => {
      try {
        const run = await runsApi.run(runId, controller.signal);
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
  }, [activeConversation?.conversation_id, activeConversation?.active_run_id, activeConversation?.work_tasks, boundToSelectedWorkspace, effectiveAgentRole, mode, pollNonce]);

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
      const turn = await agentApi.streamConversationTurn(conversationId, {
        content,
        workspace_path: mode === "work" ? workspacePath.trim() || null : null,
        preferred_profile_id: selectedModel?.profileId ?? null,
        model: selectedModel?.model ?? null,
        allow_remote_search: mode === "chat" && allowRemoteSearch,
        project_id: mode === "work" ? boundProjectId : null,
        agent_role: mode === "work" && agentRole !== "auto" ? agentRole : null,
        external_analysis_import_id: mode === "work" ? selectedExternalImportId : null,
        target_run_id: mode === "work" && boundToSelectedWorkspace && !selectedExternalImportId ? workspace.runId ?? null : null,
        plan_revision_id: mode === "work" && boundToSelectedWorkspace ? workspace.planRevisionId ?? null : null,
        expected_plan_hash: mode === "work" && boundToSelectedWorkspace ? workspace.planHash ?? null : null,
        workspace_file_scope: mode === "work" && selectedScope.length ? { paths: selectedScope } : null,
        selected_input_stage: mode === "work" && selectedInputStage ? selectedInputStage : null,
        real_execution_confirmed: false,
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
      <div className={`work-review-layout${mode === "work" && currentDraftMessage && planOpen ? " has-plan" : ""}`}>
      <div className="work-dialogue-region">
      {mode === "work" && effectiveAgentRole === "analysis" && boundProjectId && <ExternalAnalysisImportPanel projectId={boundProjectId} disabled={busy || draftDirty} selectedImportId={selectedExternalImportId} onSelect={(importId) => { setExternalSource({ projectId: boundProjectId, conversationId: activeConversation?.conversation_id ?? null, importId }); setAgentRole("analysis"); }} />}
      <ConversationHistory busy={busy} messages={visibleMessages.length ? visibleMessages : [WELCOME[mode]]} onApply={(proposal) => void applyWorkspaceProposal(proposal)} onUndo={(proposal) => void undoWorkspaceProposal(proposal)} latestDraftId={latestDraftId} onOpenPlan={() => setPlanOpen(true)} onStatisticsRun={receiveStatisticsRun} conversation={activeConversation} agentRole={effectiveAgentRole} onTaskChange={refreshWorkTask} />
      {mode === "work" && activeRun && <WorkRunPanel run={activeRun} onRunChange={(run) => { setActiveRun(run); updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state }); }} />}
      {runConnectionMessage && <div role="status">{runConnectionMessage} <button type="button" onClick={() => setPollNonce((value) => value + 1)}>重新连接</button></div>}
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
