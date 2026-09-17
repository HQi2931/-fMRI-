import { useEffect, useRef, useState } from "react";

import {
  api,
  describeError,
  type Conversation,
  type ConversationContext,
  type ConversationMemory,
  type ConversationMode,
  type ModelProfile,
  type Citation,
  type PaperIngestResult,
  type Run,
  type WorkCardKind,
  type WorkCardResult,
} from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";
import { capabilities, cardsFrom } from "../work/catalog";
import { WorkCardView } from "../work/WorkCards";

type ChatMessage = { id: string; role: "user" | "assistant"; text: string; citations?: Citation[]; payload?: Record<string, unknown> };
type ModelChoice = {
  key: string;
  profileId: string;
  model: string;
  label: string;
  capabilities: ModelProfile["profile"]["capabilities"];
};
const CHAT_WELCOME: ChatMessage = {
  id: "chat-welcome",
  role: "assistant",
  text: "这里是 fMRI 专项问答。我会检索项目内的 rs-fMRI、DPABI 和统计方法文档，并根据找到的证据回答。",
};

const WORK_WELCOME: ChatMessage = {
  id: "work-welcome",
  role: "assistant",
  text: "这里是 rs-fMRI 工作模式。请选择由你维护文件内容的工作区，我会协助准备预处理、QC 和结果分析。",
};

function messagesFrom(conversation: Conversation): ChatMessage[] {
  return conversation.messages
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => ({ id: item.message_id, role: item.role as "user" | "assistant", text: item.content, payload: item.payload, citations: (item.payload.chat as { citations?: Citation[] } | undefined)?.citations }));
}

function MessageHistory({ messages }: { messages: ChatMessage[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return (
    <div className="chat-history" aria-live="polite">
      {messages.map((item) => (
        <div className={`chat-bubble chat-${item.role}`} key={item.id}>
          <span>{item.role === "assistant" ? "✦ Agent" : "你"}</span>
          <p>{item.text.split(/(\[C\d+\])/g).map((part, index) => {
            const citation = item.citations?.find((entry) => `[${entry.citation_id}]` === part);
            return citation ? <button key={index} className="citation-link" type="button" aria-expanded={expanded === `${item.id}-${citation.citation_id}`} onClick={() => setExpanded(expanded === `${item.id}-${citation.citation_id}` ? null : `${item.id}-${citation.citation_id}`)}>{part}</button> : part;
          })}</p>
          <div className="citation-sources">{item.citations?.filter((citation) => !item.text.includes(`[${citation.citation_id}]`)).map((citation) => (
            <button key={citation.citation_id} className="citation-link" type="button" aria-expanded={expanded === `${item.id}-${citation.citation_id}`} onClick={() => setExpanded(expanded === `${item.id}-${citation.citation_id}` ? null : `${item.id}-${citation.citation_id}`)}>[{citation.citation_id}] {citation.title}</button>
          ))}</div>
          {item.citations?.map((citation) => expanded === `${item.id}-${citation.citation_id}` && (
            <div className="citation-detail" key={citation.citation_id}>
              <strong>[{citation.citation_id}] {citation.title}</strong>
              <div>{[citation.section, citation.subsection].filter(Boolean).join(" / ") || "章节未知"} · {citation.page_start ? `物理页 ${citation.page_start}${citation.page_end && citation.page_end !== citation.page_start ? `–${citation.page_end}` : ""}` : "页码未知"}</div>
              <p>{citation.excerpt}</p>
              {citation.paper_id ? <a href={`${api.paperSource(citation.paper_id)}${citation.page_start ? `#page=${citation.page_start}` : ""}`} target="_blank" rel="noreferrer">打开原 PDF</a> : /^https?:\/\//.test(citation.source) ? <a href={citation.source} target="_blank" rel="noreferrer">打开来源</a> : <small>{citation.source}</small>}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function modelChoice(profile: ModelProfile, model: string): ModelChoice {
  let providerLabel = profile.profile.provider;
  try {
    providerLabel = new URL(profile.profile.base_url).hostname;
  } catch {
    // The backend has already validated the URL; retain the provider label as a fallback.
  }
  return {
    key: `${profile.profile.id}:${model}`,
    profileId: profile.profile.id,
    model,
    label: `${providerLabel} · ${model}`,
    capabilities: profile.profile.capabilities,
  };
}

export function AgentPage({ initialCapability }: { initialCapability?: string }) {
  const workspace = useWorkspace();
  const initialWorkspacePath = useRef(workspace.workspacePath).current;
  const [mode, setMode] = useState<ConversationMode>("work");
  const [conversationIds, setConversationIds] = useState<Partial<Record<ConversationMode, string>>>({});
  const [conversationList, setConversationList] = useState<Conversation[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [modelChoices, setModelChoices] = useState<ModelChoice[]>([]);
  const [selectedModelKey, setSelectedModelKey] = useState("");
  const [allowRemoteSearch, setAllowRemoteSearch] = useState(false);
  const [workspacePath, setWorkspacePath] = useState(workspace.workspacePath ?? "");
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [papers, setPapers] = useState<PaperIngestResult[]>([]);
  const [selectedPaperIds, setSelectedPaperIds] = useState<string[]>([]);
  const [indexingIds, setIndexingIds] = useState<string[]>([]);
  const [uploading, setUploading] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([CHAT_WELCOME]);
  const [workMessages, setWorkMessages] = useState<ChatMessage[]>([WORK_WELCOME]);
  const [prompt, setPrompt] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [conversationContext, setConversationContext] = useState<ConversationContext>({ memories: [], summary: null });
  const [memoryDraft, setMemoryDraft] = useState("");
  const [memoryScope, setMemoryScope] = useState<"conversation" | "project">("conversation");
  const [memoryImportance, setMemoryImportance] = useState("0.5");
  const [memoryExpiry, setMemoryExpiry] = useState("");
  const [memorySearch, setMemorySearch] = useState("");
  const [memoryIndexing, setMemoryIndexing] = useState(false);
  const [legacyHint, setLegacyHint] = useState(initialCapability ?? "");

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([api.profiles(controller.signal), api.conversations(undefined, controller.signal)])
      .then(async ([loadedProfiles, conversations]) => {
        setProfiles(loadedProfiles);
        setConversationList(conversations);
        const modelResults = await Promise.allSettled(
          loadedProfiles.map((item) =>
            api.listProviderModels({
              base_url: item.profile.base_url,
              api_key: null,
              api_key_env: item.profile.api_key_env,
            }, controller.signal),
          ),
        );
        if (!controller.signal.aborted) {
          setModelChoices(loadedProfiles.flatMap((item, index) => {
            const result = modelResults[index];
            const models = result?.status === "fulfilled" && result.value.models.length > 0
              ? result.value.models
              : [item.profile.model];
            return models.map((model) => modelChoice(item, model));
          }));
        }
        const latest: Partial<Record<ConversationMode, string>> = {};
        for (const conversation of conversations) {
          if (!latest[conversation.mode]) latest[conversation.mode] = conversation.conversation_id;
          if (conversation.mode === "chat" && !latest.chat) latest.chat = conversation.conversation_id;
        }
        setConversationIds(latest);
        const chat = conversations.find((item) => item.mode === "chat");
        const work = conversations.find((item) => item.mode === "work");
        if (chat) {
          setChatMessages(messagesFrom(chat));
        }
        if (work) {
          setWorkMessages(messagesFrom(work));
          // The project-bound workspace persisted by the Data/Plan flow is
          // authoritative. A previously selected conversation may carry a
          // stale workspace path from another project and must not override it
          // when Work is reopened.
          if (work.workspace_path && !initialWorkspacePath) setWorkspacePath(work.workspace_path);
        }
      })
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(describeError(caught));
        }
      });
    return () => controller.abort();
  }, [initialWorkspacePath]);

  useEffect(() => {
    if (initialCapability) setLegacyHint(initialCapability);
  }, [initialCapability]);

  useEffect(() => {
    const onCapability = (event: Event) => {
      const kind = (event as CustomEvent<string>).detail;
      if (kind) void openCapability(kind as WorkCardKind);
    };
    window.addEventListener("work-capability", onCapability);
    return () => window.removeEventListener("work-capability", onCapability);
  });

  const workConversation = conversationList.find((item) => item.conversation_id === conversationIds.work);
  useEffect(() => {
    const runId = workConversation?.active_run_id ?? workspace.runId;
    if (!runId) {
      setActiveRun(null);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const terminal = new Set(["succeeded", "failed_terminal", "timed_out", "cancelled"]);
    const poll = async () => {
      try {
        const run = await api.run(runId, controller.signal);
        if (controller.signal.aborted) return;
        setActiveRun(run);
        updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state });
        if (!terminal.has(run.state)) timer = setTimeout(() => void poll(), 2_000);
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) setActiveRun(null);
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [workConversation?.active_run_id, workspace.runId]);

  useEffect(() => {
    const conversationId = conversationIds[mode];
    if (!conversationId) {
      setConversationContext({ memories: [], summary: null });
      return;
    }
    const controller = new AbortController();
    api.conversationContext(conversationId, controller.signal)
      .then(setConversationContext)
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
      });
    return () => controller.abort();
  }, [conversationIds, mode]);

  useEffect(() => {
    const controller = new AbortController();
    api.papers(controller.signal).then(setPapers).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, []);

  async function uploadPaper(file: File): Promise<void> {
    setUploading(true);
    setError("");
    try {
      const result = await api.uploadPaper(file);
      setPapers((items) => [result, ...items.filter((item) => item.paper.paper_id !== result.paper.paper_id)]);
      setMessage("论文已解析保存，点击“加入知识库”后可用于问答。");
    } catch (caught) { setError(describeError(caught)); }
    finally { setUploading(false); }
  }

  async function indexPaper(paperId: string): Promise<void> {
    setIndexingIds((items) => [...items, paperId]);
    setError("");
    try {
      const result = await api.indexPaper(paperId);
      setPapers((items) => items.map((item) => item.paper.paper_id === paperId ? result : item));
    } catch (caught) {
      const detail = describeError(caught);
      setError(detail);
      setPapers((items) => items.map((item) => item.paper.paper_id === paperId ? { ...item, paper: { ...item.paper, index_status: "failed", index_error: detail } } : item));
    } finally { setIndexingIds((items) => items.filter((id) => id !== paperId)); }
  }

  function switchMode(nextMode: ConversationMode): void {
    setMode(nextMode);
    setPrompt("");
    setMessage("");
    setError("");
  }

  async function ensureConversation(targetMode: ConversationMode): Promise<string> {
    const existing = conversationIds[targetMode];
    if (existing) return existing;
    const created = await api.createConversation({
      mode: targetMode,
      workspace_path: targetMode === "work" ? workspacePath.trim() || null : null,
      preferred_profile_id: selectedModel?.profileId ?? null,
      project_id: workspace.projectId ?? null,
    });
    setConversationIds((items) => ({ ...items, [targetMode]: created.conversation_id }));
    setConversationList((items) => [created, ...items]);
    if (targetMode === "chat") setChatMessages(messagesFrom(created));
    else setWorkMessages(messagesFrom(created));
    return created.conversation_id;
  }

  async function refreshContext(conversationId: string): Promise<void> {
    setConversationContext(await api.conversationContext(conversationId));
  }

  function cacheConversation(conversation: Conversation): void {
    setConversationList((items) => [conversation, ...items.filter((item) => item.conversation_id !== conversation.conversation_id)]);
  }

  async function startNewConversation(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const created = await api.createConversation({
        mode,
        project_id: workspace.projectId ?? null,
        workspace_path: mode === "work" ? workspacePath.trim() || null : null,
        preferred_profile_id: selectedModel?.profileId ?? null,
      });
      setConversationIds((items) => ({ ...items, [mode]: created.conversation_id }));
      setConversationList((items) => [created, ...items]);
      setConversationContext({ memories: [], summary: null });
      if (mode === "chat") setChatMessages(messagesFrom(created));
      else {
        setWorkMessages(messagesFrom(created));
      }
    } catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  function selectConversation(conversationId: string): void {
    const selected = conversationList.find((item) => item.conversation_id === conversationId);
    if (!selected) return;
    setConversationIds((items) => ({ ...items, [mode]: conversationId }));
    if (mode === "chat") setChatMessages(messagesFrom(selected));
    else {
      setWorkMessages(messagesFrom(selected));
      if (selected.workspace_path) setWorkspacePath(selected.workspace_path);
    }
  }

  async function addMemory(): Promise<void> {
    const content = memoryDraft.trim();
    if (!content) return;
    setBusy(true);
    try {
      const conversationId = await ensureConversation(mode);
      await api.createConversationMemory(conversationId, {
        kind: "instruction",
        key: `manual-${Date.now()}`,
        content,
        scope: memoryScope,
        pinned: true,
        importance: Number(memoryImportance),
        expires_at: memoryExpiry ? new Date(`${memoryExpiry}T23:59:59`).toISOString() : null,
        source_message_id: null,
      });
      setMemoryDraft("");
      await refreshContext(conversationId);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function changeMemory(
    memory: ConversationMemory,
    action: "confirm" | "update" | "reject" | "pin" | "unpin" | "forget" | "accept_proposal" | "merge_proposal" | "reject_proposal",
  ): Promise<void> {
    const content = action === "update"
      ? window.prompt("修改记忆", memory.content)?.trim()
      : action === "merge_proposal"
        ? window.prompt("合并当前记忆与新建议", `${memory.content}；${memory.proposed_content ?? ""}`)?.trim()
        : undefined;
    if ((action === "update" || action === "merge_proposal") && !content) return;
    setBusy(true);
    try {
      await api.updateConversationMemory(memory.conversation_id, memory.memory_id, {
        action,
        expected_version: memory.version,
        content,
      });
      await refreshContext(memory.conversation_id);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function indexMemories(): Promise<void> {
    const conversationId = conversationIds[mode];
    if (!conversationId) return;
    setMemoryIndexing(true);
    setError("");
    try {
      const result = await api.indexConversationMemories(conversationId);
      setMessage(result.warning || `语义索引已更新：${result.indexed ?? "0"} 条。`);
      await refreshContext(conversationId);
    } catch (caught) { setError(describeError(caught)); }
    finally { setMemoryIndexing(false); }
  }


  async function sendChatMessage(): Promise<void> {
    const text = prompt.trim();
    if (!text) return;
    setPrompt("");
    setBusy(true);
    setError("");
    setChatMessages((items) => [
      ...items,
      { id: crypto.randomUUID(), role: "user", text },
    ]);
    try {
      const conversationId = await ensureConversation("chat");
      const turn = await api.sendConversationTurn(conversationId, {
        content: text,
        preferred_profile_id: selectedModel?.profileId ?? null,
        model: selectedModel?.model ?? null,
        allow_remote_search: allowRemoteSearch,
        paper_ids: selectedPaperIds.length ? selectedPaperIds : undefined,
      });
      cacheConversation(turn.conversation);
      setChatMessages(messagesFrom(turn.conversation));
      await refreshContext(conversationId);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function sendWorkMessage(): Promise<void> {
    const text = prompt.trim();
    if (!text) return;
    setPrompt("");
    setBusy(true);
    setError("");
    try {
      const conversationId = await ensureConversation("work");
      const turn = await api.sendConversationTurn(conversationId, {
        content: text,
        workspace_path: workspacePath.trim() || null,
        preferred_profile_id: selectedModel?.profileId ?? null,
        project_id: workspace.projectId ?? null,
        target_run_id: workspace.runId ?? null,
        qc_review_id: workspace.qcReviewId ?? null,
        plan_revision_id: workspace.planRevisionId ?? null,
        expected_plan_hash: workspace.planHash ?? null,
      });
      cacheConversation(turn.conversation);
      setWorkMessages(messagesFrom(turn.conversation));
      await refreshContext(conversationId);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function openCapability(kind: WorkCardKind, content?: string): Promise<void> {
    setMode("work");
    setBusy(true);
    setError("");
    setLegacyHint("");
    try {
      const conversationId = await ensureConversation("work");
      const capability = capabilities.find((item) => item.kind === kind);
      const turn = await api.sendConversationTurn(conversationId, {
        content: content ?? capability?.prompt ?? "打开工作卡片",
        card_kind: kind,
        workspace_path: workspacePath.trim() || null,
        preferred_profile_id: selectedModel?.profileId ?? null,
        project_id: workspace.projectId ?? null,
        target_run_id: workspace.runId ?? null,
        qc_review_id: workspace.qcReviewId ?? null,
        plan_revision_id: workspace.planRevisionId ?? null,
        expected_plan_hash: workspace.planHash ?? null,
      } as Parameters<typeof api.sendConversationTurn>[1]);
      cacheConversation(turn.conversation);
      setWorkMessages(messagesFrom(turn.conversation));
      await refreshContext(conversationId);
    } catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  function updateFromCard(result: WorkCardResult): void {
    cacheConversation(result.conversation);
    setWorkMessages(messagesFrom(result.conversation));
    const binding = result.card.bindings;
    const raw = result.result as Record<string, unknown> | null;
    const nested = (key: string): Record<string, unknown> | null => {
      const value = raw?.[key];
      return value && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown>
        : null;
    };
    const planRevision = nested("plan_revision");
    const review = nested("review");
    const patch: Record<string, unknown> = {};
    const bindingKeys = {
      project_id: "projectId", dataset_id: "datasetId", manifest_id: "manifestId",
      plan_revision_id: "planRevisionId", run_id: "runId", qc_review_id: "qcReviewId",
      statistical_design_id: "statisticalDesignId",
    } as const;
    for (const [source, target] of Object.entries(bindingKeys)) {
      const value = binding[source as keyof typeof bindingKeys];
      if (value != null) patch[target] = value;
    }
    if (raw) {
      if (raw.project_id && Array.isArray(raw.source_roots)) {
        Object.assign(patch, {
          datasetId: undefined, manifestId: undefined, planRevisionId: undefined,
          runId: undefined, qcReviewId: undefined, statisticalDesignId: undefined,
        });
      } else if (raw.dataset_id && typeof raw.source_path === "string") {
        Object.assign(patch, {
          manifestId: undefined, planRevisionId: undefined, runId: undefined,
          qcReviewId: undefined, statisticalDesignId: undefined,
        });
      }
      if (typeof raw.version === "number") {
        if (raw.run_id) patch.runVersion = raw.version;
        else if (raw.dataset_id) patch.datasetVersion = raw.version;
        else if (raw.project_id) patch.projectVersion = raw.version;
      }
      if (typeof raw.content_hash === "string" && raw.manifest_id) patch.manifestHash = raw.content_hash;
      if (typeof raw.plan_hash === "string") patch.planHash = raw.plan_hash;
      if (typeof raw.state === "string" && raw.run_id) patch.runState = raw.state;
      if (planRevision) {
        if (result.card.kind === "statistics") {
          if (typeof planRevision.plan_revision_id === "string") patch.statisticalDesignId = planRevision.plan_revision_id;
          if (typeof planRevision.version === "number") patch.statisticalDesignVersion = planRevision.version;
          if (typeof planRevision.plan_hash === "string") patch.statisticalDesignHash = planRevision.plan_hash;
        } else {
          if (typeof planRevision.plan_revision_id === "string") patch.planRevisionId = planRevision.plan_revision_id;
          if (typeof planRevision.version === "number") patch.planVersion = planRevision.version;
          if (typeof planRevision.plan_hash === "string") patch.planHash = planRevision.plan_hash;
          if (typeof planRevision.state === "string") patch.planState = planRevision.state;
        }
      }
      if (review) {
        if (typeof review.review_revision_id === "string") patch.qcReviewId = review.review_revision_id;
        if (typeof review.version === "number") patch.qcReviewVersion = review.version;
        if (typeof review.content_hash === "string") patch.qcReviewHash = review.content_hash;
      }
      const sourceRoots = raw.source_roots;
      const sourcePath = typeof raw.source_path === "string"
        ? raw.source_path
        : Array.isArray(sourceRoots) && typeof sourceRoots[0] === "string" ? sourceRoots[0] : null;
      if (sourcePath) {
        patch.workspacePath = sourcePath;
        setWorkspacePath(sourcePath);
      }
    }
    updateWorkspace(patch);
  }

  const isChat = mode === "chat";
  const memoryQuery = memorySearch.trim().toLocaleLowerCase();
  const memories = (conversationContext.memories ?? []).filter((item) =>
    (item.status === "pending" || item.status === "confirmed")
    && (!memoryQuery || `${item.key} ${item.content} ${item.proposed_content ?? ""}`.toLocaleLowerCase().includes(memoryQuery)),
  );
  const modeConversations = conversationList.filter((item) => item.mode === mode);
  const activeConversation = modeConversations.find((item) => item.conversation_id === conversationIds[mode]);
  const selectedModel = modelChoices.find((item) => item.key === selectedModelKey);
  const canSearchWithSelection = selectedModel
    ? selectedModel.capabilities.includes("web_search")
    : profiles.some((item) => item.profile.capabilities.includes("web_search"));

  return (
    <>
      <PageHeader
        eyebrow="Agent"
        title="fMRI 专项对话与工作"
        description="Chat 结合本地 RAG 与已配置 LLM 回答 fMRI 方法问题，并可显式开启联网搜索；Work 使用你准备的工作区完成预处理和结果分析。"
      />
      <div className="agent-mode-switch" role="tablist" aria-label="Agent 模式">
        <button
          className={isChat ? "agent-mode active" : "agent-mode"}
          type="button"
          role="tab"
          aria-selected={isChat}
          onClick={() => switchMode("chat")}
        >
          <span>Chat</span>
          <strong>fMRI 专项问答</strong>
          <small>本地 RAG + LLM，可选联网检索</small>
        </button>
        <button
          className={!isChat ? "agent-mode active" : "agent-mode"}
          type="button"
          role="tab"
          aria-selected={!isChat}
          onClick={() => switchMode("work")}
        >
          <span>Work</span>
          <strong>rs-fMRI 工作流</strong>
          <small>选择工作区、准备预处理并分析结果</small>
        </button>
      </div>
      <Feedback message={error || message} error={Boolean(error)} />
      <div className="conversation-switcher panel">
        <label>当前会话<select aria-label="当前会话" value={conversationIds[mode] ?? ""} onChange={(event) => selectConversation(event.target.value)}><option value="">尚未创建</option>{modeConversations.map((item) => <option key={item.conversation_id} value={item.conversation_id}>{item.title} · {new Date(item.updated_at).toLocaleString()}</option>)}</select></label>
        <button className="button button-secondary" type="button" disabled={busy} onClick={() => void startNewConversation()}>新建会话</button>
      </div>
      <details className="memory-drawer">
        <summary>对话记忆 <span>{memories.length}</span></summary>
        <div className="memory-drawer-body">
          <div className="memory-create">
            <input
              aria-label="新增固定记忆"
              value={memoryDraft}
              onChange={(event) => setMemoryDraft(event.target.value)}
              placeholder="例如：回答保持简洁"
            />
            <select aria-label="记忆范围" value={memoryScope} onChange={(event) => setMemoryScope(event.target.value as "conversation" | "project")}><option value="conversation">当前会话</option><option value="project" disabled={!(activeConversation?.project_id ?? workspace.projectId)}>当前项目</option></select>
            <select aria-label="记忆重要性" value={memoryImportance} onChange={(event) => setMemoryImportance(event.target.value)}><option value="0.3">一般</option><option value="0.5">重要</option><option value="0.8">很重要</option><option value="1">最高</option></select>
            <input aria-label="记忆到期日" type="date" value={memoryExpiry} onChange={(event) => setMemoryExpiry(event.target.value)} />
            <button className="button button-secondary" type="button" disabled={busy || !memoryDraft.trim()} onClick={() => void addMemory()}>保存记忆</button>
          </div>
          <div className="memory-tools"><input aria-label="搜索记忆" placeholder="搜索记忆" value={memorySearch} onChange={(event) => setMemorySearch(event.target.value)} /><button type="button" className="button button-secondary" disabled={memoryIndexing || !conversationIds[mode]} onClick={() => void indexMemories()}>{memoryIndexing ? "索引中…" : "更新语义索引"}</button></div>
          {memories.length === 0 ? (
            <p className="muted">尚无待确认或已固定的记忆。</p>
          ) : (
            <ul className="memory-list">
              {memories.map((item) => (
                <li key={item.memory_id}>
                  <div><StatusPill tone={item.status === "pending" ? "warn" : "good"}>{item.status === "pending" ? "待确认" : item.pinned ? "已固定" : "已确认"}</StatusPill><strong>{item.content}</strong><small>{{ scientific_parameter: "科研参数", preference: "偏好", instruction: "指令", project_fact: "项目事实", decision: "已作决定" }[item.kind]} · {item.semantic_indexed ? "语义索引就绪" : "关键词可召回"}</small></div>
                  {item.proposed_content && <div className="memory-proposal"><small>发现冲突建议</small><strong>{item.proposed_content}</strong><div className="memory-actions"><button type="button" onClick={() => void changeMemory(item, "accept_proposal")}>采用新值</button><button type="button" onClick={() => void changeMemory(item, "merge_proposal")}>合并</button><button type="button" onClick={() => void changeMemory(item, "reject_proposal")}>保留原值</button></div></div>}
                  <div className="memory-actions">
                    {item.status === "pending" && <button type="button" onClick={() => void changeMemory(item, "confirm")}>确认</button>}
                    <button type="button" onClick={() => void changeMemory(item, "update")}>修改</button>
                    {item.status === "pending" && <button type="button" onClick={() => void changeMemory(item, "reject")}>拒绝</button>}
                    <button type="button" onClick={() => void changeMemory(item, item.pinned ? "unpin" : "pin")}>{item.pinned ? "取消固定" : "固定"}</button>
                    <button type="button" onClick={() => void changeMemory(item, "forget")}>忘记</button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </details>

      {isChat ? (
        <div className="conversation-layout">
          <section className="panel conversation-panel">
            <div className="panel-heading">
              <div><span className="eyebrow">Chat</span><h2>fMRI 专项文字分析</h2></div>
              <StatusPill tone="info">{allowRemoteSearch ? "LLM + 联网" : "RAG + LLM"}</StatusPill>
            </div>
            <MessageHistory messages={chatMessages} />
            <div className="chat-composer">
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="例如：ALFF 和 fALFF 的输入阶段有什么区别？"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !busy) {
                    event.preventDefault();
                    void sendChatMessage();
                  }
                }}
              />
              <div className="chat-actions">
                <label className="search-toggle" title={canSearchWithSelection ? "允许模型检索公开网络来源" : "请先在设置中配置具备联网搜索能力的模型"}>
                  <input
                    type="checkbox"
                    aria-label="联网搜索"
                    checked={allowRemoteSearch}
                    disabled={!canSearchWithSelection}
                    onChange={(event) => setAllowRemoteSearch(event.target.checked)}
                  />
                  联网搜索
                </label>
                <label className="model-select">模型<select value={selectedModelKey} onChange={(event) => { setSelectedModelKey(event.target.value); const next = modelChoices.find((item) => item.key === event.target.value); if (next && !next.capabilities.includes("web_search")) setAllowRemoteSearch(false); }}><option value="">自动选择</option>{modelChoices.map((item) => <option key={item.key} value={item.key}>{item.label}{item.capabilities.includes("web_search") ? " · 可联网" : ""}</option>)}</select></label>
                <button className="button button-primary" type="button" disabled={busy || !prompt.trim()} onClick={() => void sendChatMessage()}>{busy ? (allowRemoteSearch ? "正在联网分析…" : "正在分析…") : "发送"}</button>
              </div>
            </div>
          </section>
          <aside className="panel workspace-report-panel">
            <div className="panel-heading"><div><span className="eyebrow">论文</span><h2>问答知识库</h2></div></div>
            <label className="paper-upload">上传 PDF<input type="file" accept="application/pdf,.pdf" disabled={uploading} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadPaper(file); event.target.value = ""; }} /></label>
            {uploading && <p role="status">正在解析论文…</p>}
            <p className="muted">上传仅解析与保存。点击“加入知识库”会将论文文本片段发送至 DashScope，建立检索索引。</p>
            <p className="muted">{selectedPaperIds.length ? `仅检索已选择的 ${selectedPaperIds.length} 篇论文。` : "未选择论文时检索全部可用文献。"} 点击回答中的 [C1] 等编号查看来源。</p>
            {papers.length === 0 ? <EmptyState title="尚未上传论文" detail="上传 PDF 后可手动加入知识库。" /> : <ul className="paper-list">{papers.map(({ paper }) => {
              const indexing = indexingIds.includes(paper.paper_id);
              const status = indexing ? "indexing" : paper.index_status;
              return <li key={paper.paper_id}>
                <label><input type="checkbox" aria-label={`检索 ${paper.title || paper.paper_id}`} disabled={status !== "ready"} checked={selectedPaperIds.includes(paper.paper_id)} onChange={(event) => setSelectedPaperIds((items) => event.target.checked ? [...items, paper.paper_id] : items.filter((id) => id !== paper.paper_id))} /><strong>{paper.title || "未命名论文"}</strong></label>
                <p>{paper.page_count} 页 · {{ not_indexed: "未索引", indexing: "索引中或上次中断，可重试", ready: "可检索", failed: "索引失败" }[status]}</p>
                {paper.index_error && <p className="paper-error">{paper.index_error}</p>}
                <div className="paper-actions"><a href={api.paperSource(paper.paper_id)} target="_blank" rel="noreferrer">原 PDF</a>{status !== "ready" && <button type="button" className="button button-secondary" disabled={indexing} onClick={() => void indexPaper(paper.paper_id)}>{indexing ? "正在加入…" : status === "failed" || status === "indexing" ? "重试加入知识库" : "加入知识库"}</button>}</div>
              </li>;
            })}</ul>}
          </aside>
        </div>
      ) : (
        <section className="panel conversation-panel work-conversation-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">Work</span><h2>告诉 Agent 下一步</h2></div>
            <StatusPill tone={workspace.projectId ? "good" : "neutral"}>{workspace.projectId ? "已关联项目" : "等待项目"}</StatusPill>
          </div>
          {legacyHint && <div className="issue-box legacy-route-hint"><strong>这个功能已经移入 Work 对话</strong><p>在这里打开对应卡片，不会自动执行任何操作。</p><button className="button button-primary" type="button" onClick={() => void openCapability(legacyHint as WorkCardKind)}>打开功能卡片</button></div>}
          <MessageHistory messages={workMessages} />
          {cardsFrom(workConversation).map((card) => <article className="work-message-card" key={card.card_id}>
            <header><span className="eyebrow">操作卡片</span><h3>{card.title}</h3></header>
            <WorkCardView card={card} conversationId={workConversation!.conversation_id} onUpdate={updateFromCard} open={(kind) => void openCapability(kind)} />
          </article>)}
          {activeRun && <div className="issue-box run-summary" role="status"><strong>运行 {activeRun.run_id.slice(0, 8)} · {activeRun.state}</strong><p className="muted">阶段：{activeRun.stage}{typeof activeRun.stage_progress === "number" ? ` · ${Math.round(activeRun.stage_progress * 100)}%` : ""} · 第 {activeRun.attempt} 次尝试</p>{activeRun.error && <details><summary>查看失败原因</summary><pre>{activeRun.error}</pre></details>}</div>}
          <div className="work-capability-list" aria-label="可用功能">{capabilities.map((item) => <button className="button button-light" type="button" disabled={busy} key={item.kind} onClick={() => void openCapability(item.kind)}>{item.label}</button>)}</div>
          <div className="chat-composer">
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如：登记数据、准备 ALFF 方案、查看运行日志" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendWorkMessage(); } }} />
            <div className="chat-actions"><label className="model-select">模型<select value={selectedModelKey} onChange={(event) => setSelectedModelKey(event.target.value)}><option value="">自动选择</option>{modelChoices.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label><button className="button button-primary" type="button" disabled={busy || !prompt.trim()} onClick={() => void sendWorkMessage()}>{busy ? "处理中…" : "发送"}</button></div>
          </div>
          <div className="work-context-summary"><span>当前工作区</span><strong>{workspacePath || "尚未选择"}</strong><span>项目</span><strong>{workspace.projectId ? `${workspace.projectId.slice(0, 8)}…` : "尚未关联"}</strong></div>
        </section>
      )}
    </>
  );
}
