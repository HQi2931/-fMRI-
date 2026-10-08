import { useEffect, useState } from "react";
import { api, describeError, type Artifact, type PlanRevision, type QcReview, type Run, type RuntimeEvent } from "../api/client";
import "./WorkRunPanel.css";

const RUN_LABELS: Record<string, string> = {
  queued: "等待计算", running: "计算中", cancelling: "正在取消计算", cancelled: "计算已取消",
  failed_retryable: "计算失败 · 可重试", failed_terminal: "计算失败", timed_out: "计算超时",
  qc_review: "计算完成 · 待 QC 审查", succeeded: "计算完成",
};
const PHASE_LABELS: Record<string, string> = { queued: "等待排队", staging: "准备输入", running: "MATLAB 计算中", slice_timing: "层时间校正", realignment: "头动校正", brain_extraction: "BET 去颅骨", coregistration: "T1 配准", segmentation: "T1 组织分割", normalization: "空间标准化", smoothing: "空间平滑", nuisance: "协变量回归", metric_calculation: "指标计算" };
const activeStates = new Set(["queued", "running", "cancelling"]);
const contentUrl = (artifact: Artifact) => `/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`;
const ARTIFACT_LABELS: Record<string, string> = {
  "metric.alff": "ALFF 指标图", "metric.alff.raw": "原始 ALFF", "metric.alff.global_mean": "均值归一化 ALFF", "metric.alff.z_score": "z 标准化 ALFF",
  "metric.falff.raw": "原始 fALFF", "metric.reho.raw": "原始 ReHo",
  "preprocessing.functional_timeseries": "预处理功能时序", "preprocessing.brain_mask": "脑掩膜", "preprocessing.tissue_probability": "组织概率图",
  "preprocessing.t1_prepared": "已准备的 T1", "preprocessing.t1_inspection_image": "T1 检查图", "preprocessing.t1_preparation_evidence": "T1 处理记录",
  "preprocessing.metadata_evidence": "处理谱系记录", "preprocessing.execution_log": "计算记录", "execution.matlab_log": "MATLAB 日志",
  "qc.head_motion_report": "头动 QC 报告", "qc.head_motion_failure": "头动 QC 诊断",
};
const artifactLabel = (artifact: Artifact) => ARTIFACT_LABELS[artifact.artifact_type] ?? "计算产物";
const QC_CHECK_LABELS: Record<string, [string, string]> = {
  HEAD_MOTION_EVIDENCE_COMPLETE: ["头动记录完整且与输入清单对应", "头动记录缺失或与输入清单不一致"],
  HEAD_MOTION_INCLUDED_SUBJECTS_PASS: ["纳入受试者满足本次头动阈值", "纳入受试者未满足本次头动阈值"],
  HEAD_MOTION_EXCLUSIONS_RECORDED: ["头动排除记录已保存", "头动排除记录不完整"],
  HEAD_MOTION_INCLUDED_SUBJECTS_NONEMPTY: ["至少保留一名受试者", "没有可纳入的受试者"],
  METRIC_ARTIFACTS_AVAILABLE: ["指标产物可用于后续 QC 审查", "缺少后续 QC 审查所需的指标产物"],
};

function ImagePreview({ artifact }: { artifact: Artifact }) {
  const [failed, setFailed] = useState(false);
  const nifti = /\.nii(\.gz)?$/i.test(artifact.relative_path);
  if (failed) return <p>此图像无法预览，请下载文件检查。</p>;
  return <figure><img loading="lazy" alt={`${artifactLabel(artifact)} 结果预览`} src={nifti ? `/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}/preview.png` : contentUrl(artifact)} onError={() => setFailed(true)} />
    <figcaption>{nifti ? "首个体积的中间轴向切片 · 原始体素坐标 · 自动灰度显示，仅供检查" : "注册图像预览"}</figcaption></figure>;
}

function elapsedLabel(events: RuntimeEvent[], now: number) {
  const starts = events.filter((event) => event.event_type === "RunStageStarted" && event.payload.stage === "running");
  const started = starts.at(-1);
  if (!started) return null;
  const ended = events.find((event) => event.event_id > started.event_id && event.event_type === "RunStageFinished" && event.payload.stage === "running");
  const duration = Math.max(0, Math.floor(((ended ? Date.parse(ended.created_at) : now) - Date.parse(started.created_at)) / 1000));
  if (!Number.isFinite(duration)) return null;
  return `${Math.floor(duration / 60)} 分 ${duration % 60} 秒`;
}

export function WorkRunPanel({ run, onRunChange }: { run: Run; onRunChange?: (updated: Run) => void }) {
  const [data, setData] = useState<{ runId: string; attempt: number; artifacts: Artifact[]; events: RuntimeEvent[]; plan: PlanRevision | null; review: QcReview | null; reviewLoaded: boolean; qcSubjects: Array<Record<string, unknown>> } | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState<{ runId: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const [artifactResult, eventResult, planResult] = await Promise.allSettled([api.artifacts(run.run_id, controller.signal), api.runEvents(run.run_id, 0, controller.signal), api.plan(run.plan_revision_id, controller.signal)]);
        const artifacts = artifactResult.status === "fulfilled" ? artifactResult.value : [];
        const events = eventResult.status === "fulfilled" ? eventResult.value : [];
        const plan = planResult.status === "fulfilled" ? planResult.value : null;
        const detailErrors = [artifactResult.status === "rejected" ? "产物列表暂时不可用。" : "", eventResult.status === "rejected" ? "阶段记录暂时不可用。" : "", planResult.status === "rejected" ? "对应方案暂时不可用。" : ""];
        let review: QcReview | null = null;
        let reviewError = "";
        try { review = await api.runQcReview(run.run_id, controller.signal); }
        catch { reviewError = "QC 审查记录暂时无法读取。计算产物仍可查看和下载，请重新加载详情。"; }
        const report = artifacts.find((artifact) => artifact.artifact_type === "qc.head_motion_report" && artifact.size_bytes <= 5 * 1024 * 1024);
        let qcSubjects: Array<Record<string, unknown>> = [];
        let reportError = "";
        if (report) {
          try {
            const response = await fetch(contentUrl(report), { signal: controller.signal });
            if (!response.ok) throw new Error("QC 报告读取失败");
            const qc: unknown = await response.json();
            if (qc && typeof qc === "object" && "subjects" in qc && Array.isArray(qc.subjects)) qcSubjects = qc.subjects.filter((subject): subject is Record<string, unknown> => Boolean(subject) && typeof subject === "object");
          } catch { reportError = "自动 QC 报告暂时无法读取。计算产物仍可查看和下载，请重新加载详情或检查报告文件。"; }
        }
        if (!controller.signal.aborted) {
          setData((previous) => {
            const sameAttempt = previous?.runId === run.run_id && previous.attempt === run.attempt ? previous : null;
            return { runId: run.run_id, attempt: run.attempt,
              artifacts: artifactResult.status === "fulfilled" ? artifacts : sameAttempt?.artifacts ?? [],
              events: eventResult.status === "fulfilled" ? events : sameAttempt?.events ?? [],
              plan: planResult.status === "fulfilled" ? plan : sameAttempt?.plan ?? null,
              review, reviewLoaded: !reviewError, qcSubjects };
          });
          setError([...detailErrors, reviewError, reportError].filter(Boolean).join(" "));
        }
      } catch (caught) {
        if (!controller.signal.aborted) setError(`运行详情暂时不可用：${describeError(caught)}`);
      }
      if (!controller.signal.aborted && activeStates.has(run.state)) timer = setTimeout(() => void load(), 4000);
    }
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [run.run_id, run.plan_revision_id, run.version, run.state, run.attempt, reload]);
  useEffect(() => {
    if (!activeStates.has(run.state)) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [run.state]);
  const current = data?.runId === run.run_id && data.attempt === run.attempt ? data : null;
  const events = current?.events ?? [];
  const artifacts = current?.artifacts ?? [];
  const gallery = artifacts.filter((artifact) => artifact.artifact_type.startsWith("metric.") && /\.nii(\.gz)?$|\.png$/i.test(artifact.relative_path)).slice(0, 3);
  const review = current?.review;
  const elapsed = elapsedLabel(events, now);
  const phaseEvent = [...events].reverse().find((event) => ["MatlabPhaseStarted", "MatlabPhaseFinished"].includes(event.event_type) && event.payload.attempt === run.attempt);
  const observedPhase = phaseEvent?.event_type === "MatlabPhaseStarted" ? String(phaseEvent.payload.phase) : run.state === "running" && run.stage === "queued" ? "running" : run.stage;
  const completedPhases = [...new Set(events.filter((event) => event.event_type === "MatlabPhaseFinished" && event.payload.attempt === run.attempt).map((event) => String(event.payload.phase)))];
  const finished = [...events].reverse().find((event) => event.event_type === "RunStageFinished" && event.payload.stage === "running");
  const mock = artifacts.some((artifact) => artifact.provenance.non_scientific === true || String(artifact.provenance.executor ?? "").includes("mock")) || events.some((event) => event.payload.execution_backend === "mock");
  async function action(kind: "cancel" | "retry" | "qc" | "approve") {
    if (busy) return;
    setBusy(true); setActionError(null);
    try {
      if (kind === "cancel" || kind === "retry") {
        const updated = await (kind === "cancel" ? api.cancelRun : api.retryRun)(run.run_id, { expected_version: run.version, reason: kind === "cancel" ? "用户在运行卡片明确取消计算" : "用户在运行卡片明确重试计算" });
        onRunChange?.(updated);
      } else if (kind === "qc") {
        await api.createHeadMotionQc(run.run_id, { expected_run_version: run.version, criteria: { max_translation_mm: 2, max_rotation_deg: 2, mean_fd_power: 0.2, fd_power_gt_0_5_fraction: 0.2 } });
      } else if (review) {
        await api.approveQcReview(review.review.review_revision_id, { expected_review_version: review.version, expected_run_version: run.version, review_hash: review.review.content_hash, actor: "web-user", approved: true, reason: "用户检查 QC 结论后在运行卡片明确批准" });
        onRunChange?.(await api.run(run.run_id));
      }
      setReload((value) => value + 1);
    } catch (caught) { setActionError({ runId: run.run_id, message: describeError(caught) }); }
    finally { setBusy(false); }
  }
  return <section className="work-run-panel" aria-label="运行与结果">
    <header><h3>{RUN_LABELS[run.state] ?? run.state}</h3><span>第 {run.attempt} 次执行</span></header>
    {mock && <p className="work-run-warning">Mock 演示结果 · 非科学产物，不能用于科研结论。</p>}
    {activeStates.has(run.state) && <p role="status">当前阶段：{PHASE_LABELS[observedPhase] ?? "MATLAB 计算中"}{elapsed && ` · 已运行 ${elapsed}`}</p>}
    {completedPhases.length > 0 && <p>已观察到完成：{completedPhases.map((phase) => PHASE_LABELS[phase] ?? "处理步骤").join("、")}</p>}
    {!activeStates.has(run.state) && elapsed && <p>计算耗时：{elapsed}</p>}
    {finished && <p>计算结束：{new Date(finished.created_at).toLocaleString("zh-CN")}</p>}
    {run.state === "qc_review" && <p>指标计算已完成，科学质量审查尚未通过。</p>}
    {run.state === "succeeded" && <p>{run.qc_status === "skipped" ? "QC 已明确跳过，请自行评估使用限制。" : run.qc_status === "approved" ? "QC 审查已通过。" : "请查看 QC 记录确认审查结论。"}</p>}
    {run.error && <p role="alert">运行失败：{run.error}</p>}
    {actionError?.runId === run.run_id && <p role="alert">{actionError.message}</p>}
    {error && <div role="alert">{error}<p>已显示的详情可能不是最新，请重新加载核对。</p><button type="button" onClick={() => setReload((value) => value + 1)}>重新加载详情</button></div>}
    <div className="work-run-actions">
      {["queued", "running"].includes(run.state) && <button type="button" disabled={busy} onClick={() => void action("cancel")}>取消计算</button>}
      {["failed_retryable", "timed_out"].includes(run.state) && <button type="button" disabled={busy} onClick={() => void action("retry")}>确认重试计算</button>}
      {run.state === "cancelling" && <span>正在等待执行器确认取消。</span>}
    </div>
    {artifacts.length > 0 && <div className="work-run-results"><h4>结果与记录（{artifacts.length}）</h4>
      {["failed_retryable", "failed_terminal", "timed_out", "cancelled"].includes(run.state) && <p className="work-run-warning">本次执行未成功完成。以下为已登记文件，请核对处理是否完整及科研适用性。</p>}
      {gallery.length > 0 && <div className="work-run-gallery">{gallery.map((artifact) => <div key={artifact.artifact_id}><strong>{artifactLabel(artifact)}</strong><ImagePreview artifact={artifact} /></div>)}</div>}
      {artifacts.map((artifact) => <details key={artifact.artifact_id}><summary>{artifactLabel(artifact)} · {artifact.relative_path.split("/").at(-1)}</summary>
        {!gallery.includes(artifact) && /\.nii(\.gz)?$|\.png$/i.test(artifact.relative_path) && <ImagePreview artifact={artifact} />}
        <a href={contentUrl(artifact)} download>下载产物</a><p>文件大小：{artifact.size_bytes.toLocaleString()} 字节 · 下载前自动验证完整性</p>
      </details>)}
    </div>}
    {current && current.qcSubjects.length > 0 && <div><h4>自动头动 QC</h4><p>自动测量不代表人工科学审查已通过。</p><table><thead><tr><th>受试者</th><th>最大平移（mm）</th><th>最大旋转（°）</th><th>平均 FD（mm）</th><th>自动检查</th></tr></thead><tbody>{current.qcSubjects.map((subject, index) => <tr key={index}><td>{String(subject.subject_id ?? "—")}</td>{["max_translation_mm", "max_rotation_deg", "mean_fd_power"].map((key) => <td key={key}>{typeof subject[key] === "number" ? subject[key].toFixed(3) : "—"}</td>)}<td>{subject.passed === true ? "通过" : "需检查"}</td></tr>)}</tbody></table></div>}
    {run.state === "qc_review" && current?.reviewLoaded && !review && <details><summary>生成头动 QC 审查</summary><p>阈值：平移 2 mm，旋转 2°，平均 FD 0.2 mm，FD &gt; 0.5 mm 比例 20%。请核对本项目要求后选择。</p><button type="button" disabled={busy} onClick={() => void action("qc")}>采用这些阈值生成审查</button></details>}
    {review && <div><h4>QC 检查</h4><ul>{review.review.checks.map((check) => <li key={check.code}>{check.passed ? "通过" : "需处理"}：{QC_CHECK_LABELS[check.code]?.[check.passed ? 0 : 1] ?? check.message}</li>)}</ul><p>纳入 {review.review.included_subject_ids.length} 人，排除 {review.review.excluded_subject_ids.length} 人。</p>
      {review.state === "draft" && run.state === "qc_review" && <button type="button" disabled={busy || review.review.checks.some((check) => !check.passed && check.severity === "blocking")} onClick={() => void action("approve")}>确认 QC 结论并批准</button>}
    </div>}
    {current && <details><summary>方案与技术记录</summary>{current.plan && <a download="approved-plan.json" href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(current.plan, null, 2))}`}>下载对应方案</a>}<ol>{events.filter((event) => event.event_type === "RunStageStarted" || event.event_type === "RunStageFinished").map((event) => <li key={event.event_id}>{new Date(event.created_at).toLocaleString("zh-CN")} · {event.event_type === "RunStageStarted" ? "开始" : "结束"} {PHASE_LABELS[String(event.payload.stage)] ?? String(event.payload.stage)}</li>)}</ol></details>}
  </section>;
}
