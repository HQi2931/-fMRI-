import { useEffect, useState } from "react";

import { api, describeError, type AgentTask, type ModelProfile } from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { useWorkspace } from "../workspace";

export function AgentPage() {
  const workspace = useWorkspace();
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [preferred, setPreferred] = useState("");
  const [taskType, setTaskType] = useState<"plan_explainer" | "log_summarizer" | "report_writer">("plan_explainer");
  const [result, setResult] = useState<AgentTask | null>(null);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api.profiles(controller.signal).then(setProfiles).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, []);

  async function submit(): Promise<void> {
    if (!workspace.projectId || !workspace.projectVersion) return;
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      const task = await api.createAgentTask({
        request: {
          task_type: taskType,
          project_id: workspace.projectId,
          summary: {
            purpose: taskType === "plan_explainer" ? "explain_current_plan" : taskType === "log_summarizer" ? "summarize_registered_run" : "draft_method_report",
            metric_kinds: [],
            workflow_state: "not_started",
            issue_count: 0,
            has_blocking_issues: false,
          },
          required_capabilities: ["json_object"],
          preferred_profile_id: preferred || null,
        },
        expected_project_version: workspace.projectVersion,
      });
      setResult(task);
      setFeedback("模型输出已完成去标识化与结构校验。它只是建议，不会启动 Workflow。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader eyebrow="智能助手" title="解释方案，不替你做科学决定" description="模型只接收去标识化结构摘要；输出必须通过 Schema，且无权执行 MATLAB、Shell 或审批计划。" />
      <Feedback message={error || feedback} error={Boolean(error)} />
      <div className="two-column wide-left">
        <section className="panel assistant-panel">
          {result ? (
            <div className="assistant-message"><div className="assistant-icon" aria-hidden="true">✦</div><div><strong>结构化建议</strong><p>{result.result.recommendation.summary}</p><ul className="compact-list">{(result.result.recommendation.warnings ?? []).map((warning) => <li key={warning}>{warning}</li>)}</ul></div></div>
          ) : <EmptyState title="等待安全任务" detail="可解释计划、总结已登记运行或起草方法报告；不会外发自由文本、标识符或显著性阈值选择。" />}
          <div className="composer">
            <p className="muted">系统只发送上方选择的任务类型和服务端允许的聚合状态；当前 MVP 不向外部模型发送自由文本。</p>
            <button className="button button-primary" type="button" disabled={busy || !workspace.projectId || profiles.length === 0} onClick={submit}>{busy ? "正在校验…" : "发送安全结构摘要"}</button>
          </div>
        </section>
        <aside className="panel">
          <div className="panel-heading"><div><span className="eyebrow">模型路由</span><h2>能力优先</h2></div><StatusPill tone="good">安全边界</StatusPill></div>
          <div className="parameter-list">
            <label>任务类型<select value={taskType} onChange={(event) => setTaskType(event.target.value as typeof taskType)}><option value="plan_explainer">方案解释</option><option value="log_summarizer">日志总结</option><option value="report_writer">报告草稿</option></select></label>
            <label>模型<select value={preferred} onChange={(event) => setPreferred(event.target.value)}><option value="">自动路由</option>{profiles.map((item) => <option key={item.profile.id} value={item.profile.id}>{item.profile.model} · {item.profile.id}</option>)}</select></label>
          </div>
          <dl className="detail-list">
            <div><dt>必需能力</dt><dd>JSON object</dd></div>
            <div><dt>外发影像</dt><dd>禁止</dd></div>
            <div><dt>绝对路径</dt><dd>移除</dd></div>
            <div><dt>直接执行</dt><dd>禁止</dd></div>
          </dl>
          {result && <p className="muted">路由：{result.result.routing.selected_profile_id} · 上下文 {result.result.context_hash.slice(0, 12)}…</p>}
        </aside>
      </div>
    </>
  );
}
