import { useEffect, useState } from "react";

import {
  api,
  describeError,
  type Conversation,
  type ConversationMode,
  type ModelProfile,
  type RsFmriAnswer,
  type WorkspaceCheck,
} from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";

type ChatMessage = { id: string; role: "user" | "assistant"; text: string };
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
    .map((item) => ({ id: item.message_id, role: item.role as "user" | "assistant", text: item.content }));
}

function workspaceReportFrom(conversation: Conversation): WorkspaceCheck | null {
  for (const item of [...conversation.messages].reverse()) {
    const checked = item.payload.workspace_check;
    if (checked && typeof checked === "object") return checked as WorkspaceCheck;
  }
  return null;
}

function ragAnswerFrom(conversation: Conversation): RsFmriAnswer | null {
  for (const item of [...conversation.messages].reverse()) {
    const rag = item.payload.rag;
    if (rag && typeof rag === "object") return rag as RsFmriAnswer;
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
  return (
    <div className="chat-history" aria-live="polite">
      {messages.map((item) => (
        <div className={`chat-bubble chat-${item.role}`} key={item.id}>
          <span>{item.role === "assistant" ? "✦ Agent" : "你"}</span>
          <p>{item.text}</p>
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
  const [ragAnswer, setRagAnswer] = useState<RsFmriAnswer | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([CHAT_WELCOME]);
  const [workMessages, setWorkMessages] = useState<ChatMessage[]>([WORK_WELCOME]);
  const [prompt, setPrompt] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

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
          setRagAnswer(ragAnswerFrom(chat));
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
      });
      const answer = turn.assistant_message.payload.rag as RsFmriAnswer;
      setRagAnswer(answer);
      setChatMessages(messagesFrom(turn.conversation));
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
                  if (event.key === "Enter" && !event.shiftKey) {
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
            <div className="panel-heading"><div><span className="eyebrow">RAG</span><h2>回答依据</h2></div></div>
            {!ragAnswer ? (
              <EmptyState title="等待问题" detail="回答后，这里会列出本地 RAG 与联网搜索返回的引用来源。" />
            ) : ragAnswer.answer.evidence.length === 0 ? (
              <EmptyState title="未找到项目证据" detail="可以换一种问法，或将相关方法文档加入项目知识库。" />
            ) : (
              <ul className="evidence-list">
                {ragAnswer.answer.evidence.map((item) => (
                  <li key={`${item.source}-${item.title}`}><strong>{item.source.startsWith("http") ? <a href={item.source} target="_blank" rel="noreferrer">{item.title}</a> : item.title}</strong><p>{item.excerpt}</p><span>{item.source.startsWith("http") ? "联网来源" : `本地相关度 ${item.score}`}</span></li>
                ))}
              </ul>
            )}
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
