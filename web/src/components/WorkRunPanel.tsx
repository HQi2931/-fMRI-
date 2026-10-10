import { useEffect, useMemo, useState } from "react";
import { api, describeError, type Artifact, type PlanRevision, type QcReview, type Run, type RunDiagnosis, type RuntimeEvent, type StatisticalAtlas, type StatisticalAtlasSelection, type StatisticalResultDetail, type StatisticalResultLocalization, type StatisticalResultSliceInfo } from "../api/client";
import "./WorkRunPanel.css";
import { StatisticalClusterRoiPanel } from "./StatisticalClusterRoiPanel";
import { StatisticalMethodPanel } from "./StatisticalMethodPanel";

const RUN_LABELS: Record<string, string> = {
  queued: "等待计算", running: "计算中", cancelling: "正在取消计算", cancelled: "计算已取消",
  failed_retryable: "计算失败 · 可重试", failed_terminal: "计算失败", timed_out: "计算超时",
  qc_review: "计算完成 · 待 QC 审查", succeeded: "计算完成",
};
const PHASE_LABELS: Record<string, string> = { queued: "等待排队", staging: "准备输入", running: "MATLAB 计算中", slice_timing: "层时间校正", realignment: "头动校正", brain_extraction: "BET 去颅骨", coregistration: "T1 配准", segmentation: "T1 组织分割", normalization: "空间标准化", smoothing: "空间平滑", nuisance: "协变量回归", metric_calculation: "指标计算" };
const activeStates = new Set(["queued", "running", "cancelling"]);
const failedStates = new Set(["failed_retryable", "failed_terminal", "timed_out"]);
const contentUrl = (artifact: Artifact) => `/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}/content`;
const ARTIFACT_LABELS: Record<string, string> = {
  "metric.alff": "ALFF 指标图", "metric.alff.raw": "原始 ALFF", "metric.alff.global_mean": "均值归一化 ALFF", "metric.alff.z_score": "z 标准化 ALFF",
  "metric.falff.raw": "原始 fALFF", "metric.reho.raw": "原始 ReHo",
  "preprocessing.functional_timeseries": "预处理功能时序", "preprocessing.brain_mask": "脑掩膜", "preprocessing.tissue_probability": "组织概率图",
  "preprocessing.t1_prepared": "已准备的 T1", "preprocessing.t1_inspection_image": "T1 检查图", "preprocessing.t1_preparation_evidence": "T1 处理记录",
  "preprocessing.metadata_evidence": "处理谱系记录", "preprocessing.execution_log": "计算记录", "execution.matlab_log": "MATLAB 日志",
  "qc.head_motion_report": "头动 QC 报告", "qc.head_motion_failure": "头动 QC 诊断",
  "statistics.execution_script": "统计运行脚本", "statistics.execution_bootstrap": "统计环境脚本",
  "statistics.job_configuration": "统计运行配置", "statistics.input_manifest": "统计输入清单",
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

function delimitedTable(rows: unknown[][], separator: "," | "\t") {
  return rows.map((row) => row.map((value) => {
    const text = value == null ? "" : String(value);
    return separator === "," ? `"${text.replace(/"/g, '""')}"` : text.replace(/[\t\r\n]/g, " ");
  }).join(separator)).join("\n");
}

function ResultAtlasLocalization({ result }: { result: StatisticalResultDetail }) {
  const frozenAtlas = useMemo(() => {
    try {
      const value = JSON.parse(result.report_json).localization_plan as StatisticalAtlasSelection | undefined;
      return value && ["aal", "aal3", "brodmann"].includes(value.atlas_id) ? value : null;
    } catch { return null; }
  }, [result.report_json]);
  const [atlases, setAtlases] = useState<StatisticalAtlas[]>([]);
  const [atlasId, setAtlasId] = useState<string>(frozenAtlas?.atlas_id ?? "aal");
  const [localization, setLocalization] = useState<StatisticalResultLocalization | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void api.statisticalAtlases(controller.signal).then((items) => {
      setAtlases(items);
      if (!frozenAtlas && !items.some((item) => item.atlas_id === "aal" && item.available)) {
        const first = items.find((item) => item.available);
        if (first) setAtlasId(first.atlas_id);
      }
    }).catch((caught) => {
      if (!controller.signal.aborted) setError(`DPABI 图谱列表暂时不可用：${describeError(caught)}`);
    });
    return () => controller.abort();
  }, [frozenAtlas]);
  const usableAtlases = atlases.filter((item) => item.available);
  const correction = result.manifest.correction;
  const fdrSummary = Boolean(correction && typeof correction === "object" && "method" in correction && correction.method === "fdr");
  const coverageTable = localization ? delimitedTable([
    ["cluster_id", "atlas_id", "label_value", "label", "voxel_count", "proportion"],
    ...localization.clusters.flatMap((cluster) => cluster.coverage.map((region) =>
      [cluster.cluster_id, localization.atlas_id, region.label_value, region.label, region.voxel_count, region.proportion])),
  ], "\t") : "";
  async function localize() {
    if (busy || !atlasId) return;
    setBusy(true); setError("");
    try {
      const value = await api.localizeStatisticalResult(result.result_id, atlasId);
      setLocalization(value);
    } catch (caught) { setError(`脑区定位未完成：${describeError(caught)}`); }
    finally { setBusy(false); }
  }
  return <details className="result-atlas-localization"><summary>DPABI 图谱峰值定位</summary>
    <p>按统计图和图谱的仿射定位峰值，并计算整个簇在各脑区标签中的体素覆盖比例。</p>
    {usableAtlases.length > 0 ? <div className="work-run-actions">
      <label>图谱 <select disabled={Boolean(frozenAtlas)} value={atlasId} onChange={(event) => { setAtlasId(event.target.value); setLocalization(null); }}>
        {frozenAtlas ? <option value={frozenAtlas.atlas_id}>{frozenAtlas.atlas_name}（方案图谱）</option> : usableAtlases.map((atlas) => <option key={atlas.atlas_id} value={atlas.atlas_id}>{atlas.name}</option>)}
      </select></label>
      {frozenAtlas && <p>图谱已写入正式方案，按该图谱版本定位；变更图谱需另建方案。</p>}
      <button type="button" disabled={busy || result.non_scientific} onClick={() => void localize()}>{busy ? "定位中…" : "定位峰值"}</button>
    </div> : <p>当前 DPABI 配置下没有可用的 AAL、AAL3 或 Brodmann 图谱。</p>}
    {error && <p role="alert">{error}</p>}
    {fdrSummary && <p>FDR 簇按空间汇总展示；簇定位不构成独立的二次显著性检验。</p>}
    {localization && localization.result_bundle_hash === result.bundle_hash && <>
      <p>{localization.atlas_name ?? ""} · {localization.coordinate_space ?? ""} · 图谱指纹 {localization.atlas_content_hash?.slice(0, 12) ?? "—"}</p>
      <a download={`localization-${result.result_id}-${atlasId}.json`} href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(localization, null, 2))}`}>下载定位及图谱来源记录</a>
      <a download={`coverage-${result.result_id}-${atlasId}.tsv`} href={`data:text/tab-separated-values;charset=utf-8,${encodeURIComponent(coverageTable)}`}>下载脑区覆盖表</a>
      {localization.clusters.length > 0 ? <div className="table-scroll"><table><thead><tr><th>簇</th><th>峰值坐标（mm）</th><th>脑区</th><th>标签值</th><th>图谱体素中心距离（mm）</th><th>整簇脑区覆盖</th></tr></thead><tbody>
        {localization.clusters.map((cluster) => <tr key={cluster.cluster_id}><td>{cluster.cluster_id}</td><td>{cluster.peak_coordinate_mm.map((value) => value.toFixed(1)).join(", ")}</td><td>{cluster.label ?? "图谱范围外或背景区"}</td><td>{cluster.label_value ?? "—"}</td><td>{cluster.distance_to_voxel_center_mm?.toFixed(2) ?? "—"}</td><td><details><summary>{cluster.coverage.length} 个标签</summary><table><thead><tr><th>脑区标签</th><th>体素</th><th>比例</th></tr></thead><tbody>{cluster.coverage.map((region) => <tr key={region.label_value}><td>{region.label}</td><td>{region.voxel_count.toLocaleString()}</td><td>{(region.proportion * 100).toFixed(1)}%</td></tr>)}</tbody></table></details></td></tr>)}
      </tbody></table></div> : <p>本次结果没有可定位的簇。</p>}
    </>}
  </details>;
}

function StatisticalResultClusters({ result, clusters }: { result: StatisticalResultDetail; clusters: Record<string, unknown>[] }) {
  const [sliceInfo, setSliceInfo] = useState<StatisticalResultSliceInfo | null>(null);
  const [sliceIndex, setSliceIndex] = useState(0);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [sliceError, setSliceError] = useState("");
  const [imageFailed, setImageFailed] = useState(false);
  const clusterTable = [
    ["cluster_id", "extent_voxels", "peak_statistic", "x_mm", "y_mm", "z_mm", "coordinate_space", "inference", "direction", "source_map_artifact_id"],
    ...clusters.map((cluster) => [cluster.cluster_id, cluster.extent_voxels, cluster.peak_statistic,
      ...(Array.isArray(cluster.peak_coordinate_mm) ? cluster.peak_coordinate_mm : [null, null, null]),
      cluster.coordinate_space, cluster.inference, cluster.direction, cluster.source_map_artifact_id]),
  ];
  useEffect(() => {
    const controller = new AbortController();
    setSliceInfo(null); setSliceError(""); setImageFailed(false); setSelectedCluster(null);
    void api.statisticalResultSliceInfo(result.result_id, controller.signal).then((info) => {
      setSliceInfo(info); setSliceIndex(info.initial_slice_index);
    }).catch((caught) => {
      if (!controller.signal.aborted) setSliceError(`统计图切片信息不可用：${describeError(caught)}`);
    });
    return () => controller.abort();
  }, [result.result_id]);
  function selectCluster(clusterId: string) {
    setSelectedCluster(clusterId);
    const linkedSlice = sliceInfo?.cluster_slices.find((item) => item.cluster_id === clusterId);
    if (linkedSlice) { setSliceIndex(linkedSlice.slice_index); setImageFailed(false); }
  }
  return <>
    <h5>统计簇表</h5>
    <div className="work-run-actions">
      <a download={`clusters-${result.result_id}.csv`} href={`data:text/csv;charset=utf-8,${encodeURIComponent(delimitedTable(clusterTable, ","))}`}>下载簇表 CSV</a>
      <a download={`clusters-${result.result_id}.tsv`} href={`data:text/tab-separated-values;charset=utf-8,${encodeURIComponent(delimitedTable(clusterTable, "\t"))}`}>下载簇表 TSV</a>
    </div>
    {clusters.length > 0 ? <div className="table-scroll"><table><thead><tr><th>簇</th><th>体素数</th><th>峰值统计量</th><th>峰值坐标（mm）</th><th>空间</th><th>推断来源</th><th>方向</th></tr></thead><tbody>
      {clusters.map((cluster, index) => {
        const clusterId = String(cluster.cluster_id ?? `cluster-${index + 1}`);
        const coordinate = Array.isArray(cluster.peak_coordinate_mm)
          ? cluster.peak_coordinate_mm.map((value) => typeof value === "number" ? value.toFixed(1) : "?").join(", ")
          : "—";
        return <tr key={clusterId} className={selectedCluster === clusterId ? "selected-result-cluster" : undefined}>
          <td><button type="button" className="link-button" onClick={() => selectCluster(clusterId)}>簇 {clusterId}</button></td>
          <td>{String(cluster.extent_voxels ?? "—")}</td>
          <td>{typeof cluster.peak_statistic === "number" ? cluster.peak_statistic.toFixed(3) : "—"}</td>
          <td>{coordinate}</td><td>{String(cluster.coordinate_space ?? "—")}</td>
          <td>{String(cluster.inference ?? "—")}</td><td>{cluster.direction === 1 ? "正向" : cluster.direction === -1 ? "负向" : "—"}</td>
        </tr>;
      })}
    </tbody></table></div> : <p>本次结果没有可定位的簇。</p>}
    <div className="statistical-slice-viewer">
      <h5>统计图轴向切片</h5>
      {sliceError && <p role="alert">{sliceError}</p>}
      {sliceInfo && <>
        <label>切片 {sliceIndex + 1} / {sliceInfo.slice_count}
          <input aria-label="统计图轴向切片" type="range" min={0} max={sliceInfo.slice_count - 1} value={sliceIndex}
            onChange={(event) => { setSliceIndex(Number(event.target.value)); setImageFailed(false); }} />
        </label>
        {selectedCluster && <p>已联动到簇 {selectedCluster} 的峰值切片；拖动滑块可浏览相邻层。</p>}
        {imageFailed ? <p>此统计图切片无法预览，请下载 NIfTI 文件检查。</p> : <figure>
          <img loading="lazy" alt={`统计图第 ${sliceIndex + 1} 层轴向切片`}
            src={api.statisticalResultSliceUrl(result.result_id, sliceIndex)}
            onError={() => setImageFailed(true)} />
          <figcaption>基于统计图原始网格显示，不做空间重采样或方向推断。</figcaption>
        </figure>}
        <a download={`statistics-${result.result_id}-slice-${sliceIndex + 1}.png`} href={api.statisticalResultSliceUrl(result.result_id, sliceIndex)}>下载当前切片 PNG</a>
      </>}
    </div>
    {selectedCluster && <StatisticalClusterRoiPanel key={`${result.result_id}-${selectedCluster}`} resultId={result.result_id} bundleHash={result.bundle_hash} clusterId={selectedCluster} />}
  </>;
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
  const [data, setData] = useState<{ runId: string; attempt: number; artifacts: Artifact[]; events: RuntimeEvent[]; plan: PlanRevision | null; review: QcReview | null; reviewLoaded: boolean; qcSubjects: Array<Record<string, unknown>>; statisticalResults: StatisticalResultDetail[] } | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState<{ runId: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [diagnosis, setDiagnosis] = useState<{ runId: string; attempt: number; error: string; value: RunDiagnosis["diagnosis"] } | null>(null);
  useEffect(() => {
    if (!failedStates.has(run.state) || !run.error?.trim()) return;
    const controller = new AbortController();
    const rawError = run.error;
    void (async () => {
      try {
        const result = await api.diagnoseRun(run.run_id, { log_text: rawError.slice(-200_000) }, controller.signal);
        if (!controller.signal.aborted) setDiagnosis({ runId: run.run_id, attempt: run.attempt, error: rawError, value: result.diagnosis });
      } catch { /* Keep the neutral explanation and original technical details available. */ }
    })();
    return () => controller.abort();
  }, [run.run_id, run.attempt, run.state, run.error]);
  const currentDiagnosis = diagnosis?.runId === run.run_id && diagnosis.attempt === run.attempt && diagnosis.error === run.error ? diagnosis.value : null;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const [artifactResult, eventResult, planResult, statisticsResult] = await Promise.allSettled([
          api.artifacts(run.run_id, controller.signal),
          api.runEvents(run.run_id, 0, controller.signal),
          api.plan(run.plan_revision_id, controller.signal),
          run.state === "succeeded" ? api.statisticalResults(run.project_id, controller.signal) : Promise.resolve([]),
        ]);
        const artifacts = artifactResult.status === "fulfilled" ? artifactResult.value : [];
        const events = eventResult.status === "fulfilled" ? eventResult.value : [];
        const plan = planResult.status === "fulfilled" ? planResult.value : null;
        const resultSummaries = statisticsResult.status === "fulfilled" ? statisticsResult.value.filter((item) => item.run_id === run.run_id) : [];
        const statisticsDetailResponses = await Promise.allSettled(resultSummaries.map((item) => api.statisticalResult(item.result_id, controller.signal)));
        const statisticalResults = statisticsDetailResponses.flatMap((item) => item.status === "fulfilled" ? [item.value] : []);
        const detailErrors = [artifactResult.status === "rejected" ? "产物列表暂时不可用。" : "", eventResult.status === "rejected" ? "阶段记录暂时不可用。" : "", planResult.status === "rejected" ? "对应方案暂时不可用。" : "", statisticsResult.status === "rejected" ? "统计结果登记表暂时不可用。" : ""];
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
              review, reviewLoaded: !reviewError, qcSubjects,
              statisticalResults: statisticsResult.status === "fulfilled" ? statisticalResults : sameAttempt?.statisticalResults ?? [] };
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
  }, [run.project_id, run.run_id, run.plan_revision_id, run.version, run.state, run.attempt, reload]);
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
  const statisticalResults = current?.statisticalResults ?? [];
  const elapsed = elapsedLabel(events, now);
  const phaseEvent = [...events].reverse().find((event) => ["MatlabPhaseStarted", "MatlabPhaseFinished"].includes(event.event_type) && event.payload.attempt === run.attempt);
  const observedPhase = phaseEvent?.event_type === "MatlabPhaseStarted" ? String(phaseEvent.payload.phase) : run.state === "running" && run.stage === "queued" ? "running" : run.stage;
  const completedPhases = [...new Set(events.filter((event) => event.event_type === "MatlabPhaseFinished" && event.payload.attempt === run.attempt).map((event) => String(event.payload.phase)))];
  const finished = [...events].reverse().find((event) => event.event_type === "RunStageFinished" && event.payload.stage === "running");
  const mock = artifacts.some((artifact) => artifact.provenance.non_scientific === true || String(artifact.provenance.executor ?? "").includes("mock")) || events.some((event) => event.payload.execution_backend === "mock") || statisticalResults.some((result) => result.non_scientific);
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
    {run.state === "succeeded" && <p>{statisticalResults.length > 0 ? "统计计算及结果登记已完成，请结合本次检验和校正阈值查看结果。" : run.qc_status === "skipped" ? "QC 已明确跳过，请自行评估使用限制。" : run.qc_status === "approved" ? "QC 审查已通过。" : "请查看 QC 记录确认审查结论。"}</p>}
    {failedStates.has(run.state) && <div className="work-run-failure">
      <p role="alert">{currentDiagnosis?.summary ?? "本次计算未能完成。具体原因尚未确定，请查看本次日志。"}</p>
      {currentDiagnosis && currentDiagnosis.suggestions.length > 0 && <ul>{currentDiagnosis.suggestions.map((suggestion, index) => <li key={index}>{suggestion}</li>)}</ul>}
      <p>如需调整输入或参数，请在对话中准备新方案并重新审查。</p>
      {run.error && <details><summary>错误技术详情</summary><pre>{run.error}</pre></details>}
    </div>}
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
    {statisticalResults.map((result) => {
      const clustersValue = (result.manifest as { clusters?: unknown } | undefined)?.clusters;
      const clusters = Array.isArray(clustersValue)
        ? clustersValue.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
        : [];
      const correctionValue = result.manifest.correction;
      const hasCorrection = Boolean(correctionValue && typeof correctionValue === "object");
      const inferenceStatus = typeof result.manifest.inference_status === "string"
        ? result.manifest.inference_status
        : result.non_scientific
          ? "not_assessed_synthetic"
          : hasCorrection
            ? clusters.length > 0 ? "significant_voxels" : "no_significant_voxels"
            : "uncorrected_descriptive";
      const significantVoxelCount = typeof result.manifest.significant_voxel_count === "number"
        ? result.manifest.significant_voxel_count
        : inferenceStatus === "no_significant_voxels"
          ? 0
          : clusters.reduce((total, cluster) => total + (typeof cluster.extent_voxels === "number" ? cluster.extent_voxels : 0), 0);
      return <section className="work-run-results" key={result.result_id} aria-label="统计分析结果">
        <h4>统计分析报告</h4>
        <p>{result.non_scientific ? "非科学演示结果" : "已登记统计结果"} · {inferenceStatus === "no_significant_voxels" ? "校正后 0 个显著体素" : `${clusters.length} 个簇`} · 结果版本 {result.version}</p>
        {inferenceStatus === "no_significant_voxels" && <p role="status">本次检验未发现通过多重比较校正的显著差异体素（0 个）。没有显著簇或可定位脑区；空间定位不适用于这个空结果。</p>}
        {inferenceStatus === "significant_voxels" && significantVoxelCount !== null && <p>校正后保留 {significantVoxelCount.toLocaleString()} 个显著体素，分布于 {clusters.length} 个簇。</p>}
        {result.non_scientific
          ? <p>非科学占位结果不提供统计图切片预览。</p>
          : inferenceStatus === "no_significant_voxels"
            ? null
            : <StatisticalResultClusters result={result} clusters={clusters} />}
        <StatisticalMethodPanel summary={result.method_summary} />
        {!result.non_scientific && clusters.length > 0 && <ResultAtlasLocalization result={result} />}
        <details><summary>可复现结果报告</summary><div className="work-run-actions">
          <a download={`statistics-${result.result_id}.md`} href={`data:text/markdown;charset=utf-8,${encodeURIComponent(result.report_markdown)}`}>下载 Markdown 报告</a>
          <a download={`statistics-${result.result_id}.json`} href={`data:application/json;charset=utf-8,${encodeURIComponent(result.report_json)}`}>下载 JSON 报告</a>
        </div><pre>{result.report_markdown}</pre></details>
      </section>;
    })}
    {current && current.qcSubjects.length > 0 && <div><h4>自动头动 QC</h4><p>自动测量不代表人工科学审查已通过。</p><table><thead><tr><th>受试者</th><th>最大平移（mm）</th><th>最大旋转（°）</th><th>平均 FD（mm）</th><th>自动检查</th></tr></thead><tbody>{current.qcSubjects.map((subject, index) => <tr key={index}><td>{String(subject.subject_id ?? "—")}</td>{["max_translation_mm", "max_rotation_deg", "mean_fd_power"].map((key) => <td key={key}>{typeof subject[key] === "number" ? subject[key].toFixed(3) : "—"}</td>)}<td>{subject.passed === true ? "通过" : "需检查"}</td></tr>)}</tbody></table></div>}
    {run.state === "qc_review" && current?.reviewLoaded && !review && <details><summary>生成头动 QC 审查</summary><p>阈值：平移 2 mm，旋转 2°，平均 FD 0.2 mm，FD &gt; 0.5 mm 比例 20%。请核对本项目要求后选择。</p><button type="button" disabled={busy} onClick={() => void action("qc")}>采用这些阈值生成审查</button></details>}
    {review && <div><h4>QC 检查</h4><ul>{review.review.checks.map((check) => <li key={check.code}>{check.passed ? "通过" : "需处理"}：{QC_CHECK_LABELS[check.code]?.[check.passed ? 0 : 1] ?? check.message}</li>)}</ul><p>纳入 {review.review.included_subject_ids.length} 人，排除 {review.review.excluded_subject_ids.length} 人。</p>
      {review.state === "draft" && run.state === "qc_review" && <button type="button" disabled={busy || review.review.checks.some((check) => !check.passed && check.severity === "blocking")} onClick={() => void action("approve")}>确认 QC 结论并批准</button>}
    </div>}
    {current && <details><summary>方案与技术记录</summary>{current.plan && <a download="approved-plan.json" href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(current.plan, null, 2))}`}>下载对应方案</a>}<ol>{events.filter((event) => event.event_type === "RunStageStarted" || event.event_type === "RunStageFinished").map((event) => <li key={event.event_id}>{new Date(event.created_at).toLocaleString("zh-CN")} · {event.event_type === "RunStageStarted" ? "开始" : "结束"} {PHASE_LABELS[String(event.payload.stage)] ?? String(event.payload.stage)}</li>)}</ol></details>}
  </section>;
}
