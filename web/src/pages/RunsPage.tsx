import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  describeError,
  type Artifact,
  type Run,
  type RunDiagnosis,
  type RuntimeEvent,
} from "../api/client";
import { EmptyState, Feedback, PageHeader, ProgressBar } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";

function stateTone(state: string): "neutral" | "good" | "warn" | "danger" | "info" {
  if (state === "succeeded") return "good";
  if (state.startsWith("failed") || state === "timed_out") return "danger";
  if (state === "running" || state === "queued") return "info";
  if (state === "qc_review" || state === "cancelling") return "warn";
  return "neutral";
}

function progressFor(state: string): number {
  return ({ queued: 10, running: 55, cancelling: 70, qc_review: 85, succeeded: 100, failed_retryable: 70, failed_terminal: 70, timed_out: 70, cancelled: 70 } as Record<string, number>)[state] ?? 0;
}

function shouldPoll(state: string): boolean {
  return ["queued", "running", "cancelling"].includes(state);
}

export function RunsPage() {
  const workspace = useWorkspace();
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState(workspace.runId ?? "");
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [outcome, setOutcome] = useState<"succeed" | "fail_retryable" | "fail_terminal" | "timeout">("succeed");
  const [operationReason, setOperationReason] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [logExcerpt, setLogExcerpt] = useState("");
  const [diagnosis, setDiagnosis] = useState<RunDiagnosis | null>(null);
  const [busy, setBusy] = useState(false);
  const [pollGeneration, setPollGeneration] = useState(0);

  const selected = useMemo(() => runs.find((run) => run.run_id === selectedId) ?? null, [runs, selectedId]);
  const latestStage = useMemo(() => {
    const stageEvent = [...events]
      .reverse()
      .find((event) => event.event_type === "RunStageStarted" || event.event_type === "RunStageFinished");
    const payload = stageEvent?.payload ?? {};
    return {
      name: typeof payload.stage === "string" ? payload.stage : selected?.stage ?? selected?.state ?? "queued",
      progress:
        typeof payload.stage_progress === "number"
          ? Math.round(payload.stage_progress * 100)
          : selected?.stage_progress !== null && selected?.stage_progress !== undefined
            ? Math.round(selected.stage_progress * 100)
            : progressFor(selected?.state ?? "queued"),
    };
  }, [events, selected]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const items = await api.runs(workspace.projectId, signal);
    setRuns(items);
    if (!selectedId && items.length) setSelectedId(items[0].run_id);
  }, [selectedId, workspace.projectId]);

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setEvents([]);
      setArtifacts([]);
      return;
    }
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let afterEventId = 0;
    let controller: AbortController | undefined;
    setEvents([]);
    setArtifacts([]);

    async function poll(): Promise<void> {
      controller = new AbortController();
      try {
        const [currentRuns, nextEvents, currentArtifacts] = await Promise.all([
          api.runs(workspace.projectId, controller.signal),
          api.runEvents(selectedId, afterEventId, controller.signal),
          api.artifacts(selectedId, controller.signal),
        ]);
        if (!active) return;
        setRuns(currentRuns);
        const currentRun = currentRuns.find((item) => item.run_id === selectedId);
        if (nextEvents.length) {
          afterEventId = Math.max(afterEventId, ...nextEvents.map((event) => event.event_id));
          setEvents((current) => {
            const merged = new Map(current.map((event) => [event.event_id, event]));
            nextEvents.forEach((event) => merged.set(event.event_id, event));
            return Array.from(merged.values()).sort((left, right) => left.event_id - right.event_id);
          });
        }
        setArtifacts(currentArtifacts);
        if (currentRun) {
          updateWorkspace({ runId: currentRun.run_id, runVersion: currentRun.version, runState: currentRun.state });
          if (shouldPoll(currentRun.state)) timer = setTimeout(poll, 1000);
        }
      } catch (caught) {
        if (!active || (caught instanceof DOMException && caught.name === "AbortError")) return;
        setError(describeError(caught));
        timer = setTimeout(poll, 2000);
      }
    }

    void poll();
    return () => {
      active = false;
      controller?.abort();
      if (timer) clearTimeout(timer);
    };
  }, [pollGeneration, selectedId, workspace.projectId]);

  async function createRun(): Promise<void> {
    if (!workspace.projectId || !workspace.planRevisionId || !workspace.planHash) return;
    setBusy(true);
    setError("");
    try {
      const created = await api.createRun({
        project_id: workspace.projectId,
        plan_revision_id: workspace.planRevisionId,
        expected_plan_hash: workspace.planHash,
        max_attempts: 2,
        mock_outcome: outcome,
        mock_delay_ms: 50,
      });
      updateWorkspace({ runId: created.run_id, runVersion: created.version, runState: created.state });
      setSelectedId(created.run_id);
      await refresh();
      setMessage("任务已进入本机 SQLite 队列；独立 Worker 会原子领取。当前按钮只创建 Mock 安全闭环。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function act(kind: "cancel" | "retry"): Promise<void> {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      if (!operationReason.trim()) throw new Error("取消或重试前必须填写运行操作理由。");
      const body = { expected_version: selected.version, reason: operationReason.trim() };
      const updated = kind === "cancel" ? await api.cancelRun(selected.run_id, body) : await api.retryRun(selected.run_id, body);
      updateWorkspace({ runId: updated.run_id, runVersion: updated.version, runState: updated.state });
      await refresh();
      setPollGeneration((current) => current + 1);
      setOperationReason("");
      setMessage(kind === "cancel" ? "取消请求已记录。" : "重试已排队。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function diagnose(): Promise<void> {
    if (!selected || !logExcerpt.trim()) return;
    setBusy(true);
    setError("");
    try {
      setDiagnosis(await api.diagnoseRun(selected.run_id, { log_text: logExcerpt.trim() }));
      setMessage("已基于本地确定性规则生成诊断建议，不会自动修改方案或重跑任务。");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="运行"
        title="任务进度与恢复"
        description="API 与 Worker 分离；状态、事件和产物均来自 SQLite。部分产物不会被误报为完整成功。"
        action={<button className="button button-secondary" type="button" onClick={() => refresh().catch((caught) => setError(describeError(caught)))}>刷新</button>}
      />
      <Feedback message={error || message} error={Boolean(error)} />
      <section className="panel form-panel">
        <label className="field-grow">Mock 验证结果
          <select value={outcome} onChange={(event) => setOutcome(event.target.value as typeof outcome)}>
            <option value="succeed">成功后进入人工 QC</option>
            <option value="fail_retryable">可重试失败</option>
            <option value="fail_terminal">终止失败</option>
            <option value="timeout">超时</option>
          </select>
        </label>
        <button className="button button-primary" type="button" disabled={busy || workspace.planState !== "approved"} onClick={createRun}>创建已审批计划的 Mock 运行</button>
      </section>
      <div className="two-column wide-left">
        <section className="panel run-card">
          {!selected ? <EmptyState title="尚无运行" detail="只有状态为 approved 且哈希未失效的计划可以创建任务。" /> : (
            <>
              <div className="panel-heading"><div><span className="eyebrow">{selected.run_id}</span><h2>Workflow run</h2></div><StatusPill tone={stateTone(selected.state)}>{selected.state}</StatusPill></div>
              <ProgressBar value={latestStage.progress} />
              <div className="run-meta"><span>阶段: {latestStage.name}</span><span>尝试次数: {selected.attempt}</span><span>{new Date(selected.updated_at).toLocaleString()}</span></div>
              <div className="event-log" aria-label="运行事件" aria-live="polite">
                {events.length ? events.map((event) => <p key={event.event_id}><time>{new Date(event.created_at).toLocaleTimeString()}</time><span>{event.event_type} · {JSON.stringify(event.payload)}</span></p>) : <p><time>—</time><span>暂无事件</span></p>}
              </div>
              <label>运行操作理由<textarea value={operationReason} onChange={(event) => setOperationReason(event.target.value)} disabled={busy || !["queued", "running", "cancelling", "failed_retryable"].includes(selected.state)} placeholder="说明取消或重试的实际依据；该内容将进入审计事件" /></label>
              <div className="button-row">
                <button className="button button-secondary" type="button" disabled={busy || selected.state !== "failed_retryable" || !operationReason.trim()} onClick={() => act("retry")}>重试</button>
                <button className="button button-danger" type="button" disabled={busy || !["queued", "running", "cancelling"].includes(selected.state) || !operationReason.trim()} onClick={() => act("cancel")}>请求取消</button>
              </div>
              {["failed_retryable", "failed_terminal", "timed_out", "cancelled"].includes(selected.state) && <div className="composer"><label>错误日志片段<textarea value={logExcerpt} onChange={(event) => setLogExcerpt(event.target.value)} placeholder="粘贴不含身份信息的本次错误日志片段" /></label><button className="button button-secondary" type="button" disabled={busy || !logExcerpt.trim()} onClick={diagnose}>检查失败原因</button>{diagnosis && <div className="assistant-message"><div><strong>{diagnosis.diagnosis.code}</strong><p>{diagnosis.diagnosis.summary}</p><ul className="compact-list">{diagnosis.diagnosis.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}</ul></div></div>}</div>}
            </>
          )}
        </section>
        <aside className="panel">
          <span className="eyebrow">运行记录</span><h2>{runs.length} 项</h2>
          <div className="selection-list">
            {runs.map((run) => <button type="button" className={run.run_id === selectedId ? "selected" : ""} key={run.run_id} onClick={() => { setSelectedId(run.run_id); updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state }); }}><span>{run.run_id.slice(0, 12)}…</span><StatusPill tone={stateTone(run.state)}>{run.state}</StatusPill></button>)}
          </div>
          <span className="eyebrow">已登记产物</span><h2>{artifacts.length} 项</h2>
          <ul className="compact-list">{artifacts.map((artifact) => <li key={artifact.artifact_id}>{artifact.artifact_type}<small>{artifact.relative_path}</small></li>)}</ul>
        </aside>
      </div>
    </>
  );
}
