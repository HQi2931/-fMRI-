import { useEffect, useState } from "react";

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
  type WorkspaceCheck,
} from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";

type ChatMessage = { id: string; role: "user" | "assistant"; text: string; citations?: Citation[] };
type TaskType = "plan_explainer" | "log_summarizer" | "report_writer";
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
  text: "这里是 rs-fMRI 工作模式。请先选择工作区，我会检查输入格式，再协助准备预处理、QC 和结果分析。",
};

function messagesFrom(conversation: Conversation): ChatMessage[] {
  return conversation.messages
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => ({ id: item.message_id, role: item.role as "user" | "assistant", text: item.content, citations: (item.payload.chat as { citations?: Citation[] } | undefined)?.citations }));
}

function workspaceReportFrom(conversation: Conversation): WorkspaceCheck | null {
  for (const item of [...conversation.messages].reverse()) {
    const checked = item.payload.workspace_check;
    if (checked && typeof checked === "object") return checked as WorkspaceCheck;
  }
  return null;
}

function kindLabel(kind: WorkspaceCheck["kind"]): string {
  return {
    bids: "BIDS",
    dpabi_ready: "DPABI-ready",
    dicom: "DICOM",
    nifti: "普通 NIfTI",
    mixed: "混合目录",
    unknown: "未识别",
  }[kind];
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

export function AgentPage() {
  const workspace = useWorkspace();
  const [mode, setMode] = useState<ConversationMode>("work");
  const [conversationIds, setConversationIds] = useState<Partial<Record<ConversationMode, string>>>({});
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [modelChoices, setModelChoices] = useState<ModelChoice[]>([]);
  const [selectedModelKey, setSelectedModelKey] = useState("");
  const [allowRemoteSearch, setAllowRemoteSearch] = useState(false);
  const [taskType, setTaskType] = useState<TaskType>("plan_explainer");
  const [workspacePath, setWorkspacePath] = useState(workspace.workspacePath ?? "");
  const [report, setReport] = useState<WorkspaceCheck | null>(null);
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

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([api.profiles(controller.signal), api.conversations(undefined, controller.signal)])
      .then(async ([loadedProfiles, conversations]) => {
        setProfiles(loadedProfiles);
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
          setReport(workspaceReportFrom(work));
          if (work.workspace_path) setWorkspacePath(work.workspace_path);
        }
      })
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(describeError(caught));
        }
      });
    return () => controller.abort();
  }, []);

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
    });
    setConversationIds((items) => ({ ...items, [targetMode]: created.conversation_id }));
    if (targetMode === "chat") setChatMessages(messagesFrom(created));
    else setWorkMessages(messagesFrom(created));
    return created.conversation_id;
  }

  async function refreshContext(conversationId: string): Promise<void> {
    setConversationContext(await api.conversationContext(conversationId));
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
        scope: "conversation",
        pinned: true,
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
    action: "confirm" | "update" | "reject" | "pin" | "unpin" | "forget",
  ): Promise<void> {
    const content = action === "update" ? window.prompt("修改记忆", memory.content)?.trim() : undefined;
    if (action === "update" && !content) return;
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

  async function pickWorkspace(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const picked = await api.pickWorkspace();
      if (picked.path) setWorkspacePath(picked.path);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function checkWorkspace(): Promise<void> {
    const path = workspacePath.trim();
    if (!path) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const conversationId = await ensureConversation("work");
      const turn = await api.sendConversationTurn(conversationId, {
        content: "检查所选工作区是否符合 DPABI 输入格式",
        action: "check_workspace",
        workspace_path: path,
        preferred_profile_id: selectedModel?.profileId ?? null,
      });
      setWorkMessages(messagesFrom(turn.conversation));
      await refreshContext(conversationId);
      const checkedValue = turn.assistant_message.payload.workspace_check;
      if (!checkedValue || typeof checkedValue !== "object") {
        setMessage(turn.assistant_message.content);
        return;
      }
      const checked = checkedValue as WorkspaceCheck;
      setReport(checked);
      updateWorkspace({
        workspacePath: checked.path,
        workspaceKind: checked.kind,
        workspaceCheckedAt: checked.checked_at,
      });
      const outcome = checked.blocking_issues.length
        ? `检查完成，但有 ${checked.blocking_issues.length} 个阻断问题，需要先处理。`
        : `检查完成：${checked.functional_subject_count} 名受试者的功能输入可以进入下一步。`;
      setMessage(outcome);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
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
        plan_revision_id: workspace.planRevisionId ?? null,
        expected_plan_hash: workspace.planHash ?? null,
      });
      setWorkMessages(messagesFrom(turn.conversation));
      const checked = turn.assistant_message.payload.workspace_check;
      if (checked && typeof checked === "object") setReport(checked as WorkspaceCheck);
      await refreshContext(conversationId);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function startPreprocessing(): Promise<void> {
    if (!workspace.projectId || !workspace.planRevisionId || !workspace.planHash) return;
    const existingOutputs = report?.output_directories.length
      ? `\n\n工作区已存在这些结果目录：${report.output_directories.join("、")}。DPABI 可能写入其中的同名产物。`
      : "";
    if (!window.confirm(`确认使用已审批计划启动本次 MATLAB/DPABI 运行，并在所选工作区生成结果目录？${existingOutputs}`)) return;
    setBusy(true);
    setError("");
    try {
      const conversationId = await ensureConversation("work");
      const turn = await api.sendConversationTurn(conversationId, {
        content: "确认启动已审批的 DPABI 预处理",
        action: "start_preprocessing",
        workspace_path: workspacePath.trim() || null,
        preferred_profile_id: selectedModel?.profileId ?? null,
        project_id: workspace.projectId,
        plan_revision_id: workspace.planRevisionId,
        expected_plan_hash: workspace.planHash,
        real_execution_confirmed: true,
      });
      setWorkMessages(messagesFrom(turn.conversation));
      await refreshContext(conversationId);
      if (turn.conversation.active_run_id) {
        updateWorkspace({ runId: turn.conversation.active_run_id, runState: "queued" });
      }
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function submitStructuredTask(): Promise<void> {
    if (!workspace.projectId || !workspace.projectVersion) return;
    setBusy(true);
    setError("");
    try {
      const task = await api.createAgentTask({
        request: {
          task_type: taskType,
          project_id: workspace.projectId,
          summary: {
            purpose:
              taskType === "plan_explainer"
                ? "explain_current_plan"
                : taskType === "log_summarizer"
                  ? "summarize_registered_run"
                  : "draft_method_report",
            metric_kinds: [],
            workflow_state: "not_started",
            issue_count: 0,
            has_blocking_issues: false,
          },
          required_capabilities: ["json_object"],
          preferred_profile_id: selectedModel?.profileId ?? null,
        },
        expected_project_version: workspace.projectVersion,
      });
      setWorkMessages((items) => [
        ...items,
        { id: crypto.randomUUID(), role: "assistant", text: task.result.recommendation.summary },
      ]);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  const isChat = mode === "chat";
  const memories = (conversationContext.memories ?? []).filter(
    (item) => item.status === "pending" || item.pinned,
  );
  const selectedModel = modelChoices.find((item) => item.key === selectedModelKey);
  const canSearchWithSelection = selectedModel
    ? selectedModel.capabilities.includes("web_search")
    : profiles.some((item) => item.profile.capabilities.includes("web_search"));

  return (
    <>
      <PageHeader
        eyebrow="Agent"
        title="fMRI 专项对话与工作"
        description="Chat 结合本地 RAG 与已配置 LLM 回答 fMRI 方法问题，并可显式开启联网搜索；Work 调用受控工具完成工作区检查、预处理和结果分析。"
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
          <small>检查数据、准备预处理并分析结果</small>
        </button>
      </div>
      <Feedback message={error || message} error={Boolean(error)} />
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
            <button className="button button-secondary" type="button" disabled={busy || !memoryDraft.trim()} onClick={() => void addMemory()}>固定</button>
          </div>
          {memories.length === 0 ? (
            <p className="muted">尚无待确认或已固定的记忆。</p>
          ) : (
            <ul className="memory-list">
              {memories.map((item) => (
                <li key={item.memory_id}>
                  <div><StatusPill tone={item.status === "pending" ? "warn" : "good"}>{item.status === "pending" ? "待确认" : item.pinned ? "已固定" : "已确认"}</StatusPill><strong>{item.content}</strong><small>{item.kind === "scientific_parameter" ? "科研参数" : item.kind === "preference" ? "偏好" : "指令"}</small></div>
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
        <>
          <section className="workspace-bar panel">
            <div className="workspace-bar-title">
              <div className="assistant-icon" aria-hidden="true">⌂</div>
              <div><span className="eyebrow">当前工作区</span><strong>{workspace.workspacePath || "尚未选择目录"}</strong><p>选择包含输入文件的目录，DPABI 结果由后续受控工作流生成。</p></div>
            </div>
            <div className="workspace-picker">
              <div className="workspace-path-display" aria-label="已选择的本机目录">
                <span>本机目录</span>
                <strong>{workspacePath || "请通过系统窗口选择工作区"}</strong>
              </div>
              <button className="button button-secondary" type="button" disabled={busy} onClick={() => void pickWorkspace()}>浏览…</button>
              <button className="button button-primary" type="button" disabled={busy || !workspacePath.trim()} onClick={() => void checkWorkspace()}>{busy ? "正在检查…" : "检查工作区"}</button>
            </div>
          </section>
          <div className="conversation-layout">
            <section className="panel conversation-panel">
              <div className="panel-heading"><div><span className="eyebrow">Work</span><h2>告诉 Agent 下一步</h2></div><StatusPill tone={report ? (report.blocking_issues.length ? "warn" : "good") : "neutral"}>{report ? "已检查" : "等待工作区"}</StatusPill></div>
              <MessageHistory messages={workMessages} />
              <div className="chat-composer">
                <textarea
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  placeholder="例如：检查完成后，帮我准备一个 ALFF 预处理方案"
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void sendWorkMessage();
                    }
                  }}
                />
                <div className="chat-actions">
                  <label>任务类型<select aria-label="任务类型" value={taskType} onChange={(event) => setTaskType(event.target.value as TaskType)}><option value="plan_explainer">方案解释</option><option value="log_summarizer">日志总结</option><option value="report_writer">报告草稿</option></select></label>
                  <label className="model-select">模型<select value={selectedModelKey} onChange={(event) => setSelectedModelKey(event.target.value)}><option value="">自动选择</option>{modelChoices.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
                  {report && report.blocking_issues.length === 0 && workspace.planRevisionId && workspace.planHash && (
                    <button className="button button-secondary" type="button" disabled={busy} onClick={() => void startPreprocessing()}>启动 DPABI</button>
                  )}
                  <button className="button button-primary" type="button" aria-label="发送安全结构摘要" disabled={busy || (!prompt.trim() && (!workspace.projectId || !workspace.projectVersion))} onClick={prompt.trim() ? () => void sendWorkMessage() : () => void submitStructuredTask()}>{prompt.trim() ? "发送" : "发送安全结构摘要"}</button>
                </div>
              </div>
            </section>
            <aside className="panel workspace-report-panel">
              <div className="panel-heading"><div><span className="eyebrow">检查结果</span><h2>DPABI 输入体检</h2></div>{report && <StatusPill tone={report.blocking_issues.length ? "danger" : "good"}>{report.blocking_issues.length ? "不可执行" : "可继续"}</StatusPill>}</div>
              {!report ? (
                <EmptyState title="等待检查" detail="选择工作区后，这里会显示目录类型、受试者配对、输入阶段和问题。" />
              ) : (
                <>
                  <div className="workspace-stats"><div><strong>{kindLabel(report.kind)}</strong><span>目录类型</span></div><div><strong>{report.file_count}</strong><span>文件</span></div><div><strong>{report.subject_count}</strong><span>受试者</span></div><div><strong>{report.functional_subject_count}/{report.anatomical_subject_count}</strong><span>功能 / T1</span></div></div>
                  {report.input_stage && <p className="report-highlight">输入阶段：<strong>{report.input_stage}</strong></p>}
                  {report.blocking_issues.length > 0 && <div className="issue-box"><strong>阻断问题</strong><ul className="compact-list">{report.blocking_issues.map((item) => <li key={item}>{item}</li>)}</ul></div>}
                  {report.warnings.length > 0 && <div className="issue-box warning-box"><strong>检查提示</strong><ul className="compact-list">{report.warnings.map((item) => <li key={item}>{item}</li>)}</ul></div>}
                  {report.output_directories.length > 0 && <p className="muted">已发现结果目录：{report.output_directories.join("、")}</p>}
                </>
              )}
            </aside>
          </div>
        </>
      )}
    </>
  );
}
