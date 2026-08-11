import { useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  describeError,
  type Artifact,
  type QcReview,
  type StatisticalDesign,
  type StatisticalResult,
  type StatisticalResultDetail,
} from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";

type TestType = "one_sample_t" | "independent_two_sample_t" | "paired_t";
type Tail = "two_sided" | "one_sided_positive" | "one_sided_negative";
type Centering = "none" | "grand_mean" | "within_group";
type GrfSmoothnessMode = "dpabi_header_or_estimate" | "provided_dlh";
type Covariate = { name: string; centering: Centering; values: Array<{ subject_id: string; value: number }> };

function lines(value: string): string[][] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => line.split("|").map((part) => part.trim()));
}

function parseCovariates(value: string, subjects: string[], test: TestType): Covariate[] {
  if (test === "paired_t" && value.trim()) {
    throw new Error("DPABI V8.2 配对 t 检验暂不支持受试者协变量，因为它会与受试者回归量共线。");
  }
  return lines(value).map(([name, centering, assignments]) => {
    if (!name || !(["none", "grand_mean", "within_group"] as string[]).includes(centering) || !assignments) {
      throw new Error("协变量每行必须是“名称 | none/grand_mean/within_group | 受试者=数值,…”。");
    }
    if (centering === "within_group" && test !== "independent_two_sample_t") {
      throw new Error("within_group 中心化只适用于独立双样本 t 检验。");
    }
    if (test === "one_sample_t" && centering !== "grand_mean") {
      throw new Error("DPABI V8.2 单样本 t 检验的协变量必须选择 grand_mean 中心化。");
    }
    const bySubject = new Map<string, number>();
    for (const assignment of assignments.split(",").map((item) => item.trim()).filter(Boolean)) {
      const [subjectId, raw, ...rest] = assignment.split("=").map((item) => item.trim());
      const numeric = Number(raw);
      if (!subjectId || !raw || rest.length || !Number.isFinite(numeric)) throw new Error(`协变量 ${name} 含无效的受试者=数值项。`);
      if (bySubject.has(subjectId)) throw new Error(`协变量 ${name} 重复受试者：${subjectId}`);
      bySubject.set(subjectId, numeric);
    }
    if (bySubject.size !== subjects.length || subjects.some((subject) => !bySubject.has(subject))) {
      throw new Error(`协变量 ${name} 必须严格覆盖冻结 QC 中的全部受试者。`);
    }
    return {
      name,
      centering: centering as Centering,
      values: subjects.map((subject_id) => ({ subject_id, value: bySubject.get(subject_id)! })),
    };
  });
}

export function StatisticsPage() {
  const workspace = useWorkspace();
  const [qc, setQc] = useState<QcReview | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [test, setTest] = useState<TestType | "">("");
  const [tail, setTail] = useState<Tail | "">("");
  const [imageRows, setImageRows] = useState("");
  const [groupOrder, setGroupOrder] = useState("");
  const [conditionOrder, setConditionOrder] = useState("");
  const [oneSampleBaseline, setOneSampleBaseline] = useState("");
  const [covariateRows, setCovariateRows] = useState("");
  const [missingPolicy, setMissingPolicy] = useState<"error" | "exclude_explicitly" | "">("");
  const [maskArtifactId, setMaskArtifactId] = useState("");
  const [correction, setCorrection] = useState<"fdr" | "grf" | "">("");
  const [qThreshold, setQThreshold] = useState("");
  const [voxelThreshold, setVoxelThreshold] = useState("");
  const [clusterThreshold, setClusterThreshold] = useState("");
  const [grfSmoothnessMode, setGrfSmoothnessMode] = useState<GrfSmoothnessMode | "">("");
  const [grfSmoothnessDlh, setGrfSmoothnessDlh] = useState("");
  const [design, setDesign] = useState<StatisticalDesign | null>(null);
  const designIdRef = useRef<string | null>(null);
  const [approvalActor, setApprovalActor] = useState("");
  const [approvalReason, setApprovalReason] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<StatisticalResult[]>([]);
  const [selectedResultId, setSelectedResultId] = useState("");
  const [resultDetail, setResultDetail] = useState<StatisticalResultDetail | null>(null);
  const [resultRefresh, setResultRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const tasks: Promise<unknown>[] = [];
    if (workspace.qcReviewId) tasks.push(api.qcReview(workspace.qcReviewId, controller.signal).then((review) => {
      setQc(review);
      setImageRows((current) => current || review.review.included_subject_ids.map((subject) => `${subject} | `).join("\n"));
    }));
    if (workspace.runId) tasks.push(api.artifacts(workspace.runId, controller.signal).then(setArtifacts));
    if (workspace.statisticalDesignId && designIdRef.current !== workspace.statisticalDesignId) tasks.push(api.statisticalDesign(workspace.statisticalDesignId, controller.signal).then((stored) => {
      designIdRef.current = stored.plan_revision.plan_revision_id;
      setDesign(stored);
      if (["one_sample_t", "independent_two_sample_t", "paired_t"].includes(stored.design.test)) {
        const storedTest = stored.design.test as TestType;
        setTest(storedTest);
        setTail(stored.design.tail);
        setGroupOrder(stored.design.group_order.join(" | "));
        setConditionOrder(stored.design.condition_order.join(" | "));
        setOneSampleBaseline(stored.design.one_sample_baseline === null ? "" : String(stored.design.one_sample_baseline));
        setMissingPolicy(stored.design.missing_value_policy);
        setMaskArtifactId(stored.design.mask_artifact_id);
        if (storedTest === "paired_t") {
          setImageRows(stored.design.subject_order.map((subjectId) => {
            const artifactIds = stored.design.condition_order.map((condition) => stored.design.images.find((image) => image.subject_id === subjectId && image.condition === condition)?.artifact_id ?? "");
            return [subjectId, ...artifactIds].join(" | ");
          }).join("\n"));
        } else {
          setImageRows(stored.design.subject_order.map((subjectId) => {
            const image = stored.design.images.find((candidate) => candidate.subject_id === subjectId);
            return storedTest === "independent_two_sample_t"
              ? `${subjectId} | ${image?.artifact_id ?? ""} | ${image?.group ?? ""}`
              : `${subjectId} | ${image?.artifact_id ?? ""}`;
          }).join("\n"));
        }
        setCovariateRows(stored.design.covariates.map((covariate) => `${covariate.name} | ${covariate.centering} | ${covariate.values.map((value) => `${value.subject_id}=${value.value}`).join(",")}`).join("\n"));
      }
      if (stored.correction && "q_threshold" in stored.correction) {
        setCorrection("fdr");
        setQThreshold(String(stored.correction.q_threshold));
      } else if (stored.correction && "voxel_p_threshold" in stored.correction) {
        setCorrection("grf");
        setVoxelThreshold(String(stored.correction.voxel_p_threshold));
        setClusterThreshold(String(stored.correction.cluster_p_threshold));
        setGrfSmoothnessMode(stored.correction.smoothness_mode);
        setGrfSmoothnessDlh(stored.correction.smoothness_dlh === null ? "" : String(stored.correction.smoothness_dlh));
      }
      updateWorkspace({
        statisticalDesignVersion: stored.plan_revision.version,
        statisticalDesignHash: stored.plan_revision.plan_hash,
      });
    }));
    Promise.all(tasks).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [workspace.qcReviewId, workspace.runId, workspace.statisticalDesignId]);

  useEffect(() => {
    if (!workspace.projectId) return;
    const controller = new AbortController();
    api
      .statisticalResults(workspace.projectId, controller.signal)
      .then(setResults)
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(describeError(caught));
        }
      });
    return () => controller.abort();
  }, [resultRefresh, workspace.projectId]);

  useEffect(() => {
    if (!selectedResultId) {
      setResultDetail(null);
      return;
    }
    const controller = new AbortController();
    api
      .statisticalResult(selectedResultId, controller.signal)
      .then(setResultDetail)
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(describeError(caught));
        }
      });
    return () => controller.abort();
  }, [selectedResultId]);

  const parsedRows = useMemo(() => lines(imageRows), [imageRows]);
  const subjects = qc?.review.included_subject_ids ?? [];

  function buildImages(): Array<{ subject_id: string; artifact_id: string; group: string | null; condition: string | null }> {
    if (!test) throw new Error("必须明确选择检验类型。");
    if (test === "paired_t") {
      const conditions = lines(conditionOrder)[0] ?? [];
      if (conditions.length !== 2 || parsedRows.some((row) => row.length < 3)) throw new Error("配对检验每行必须为“受试者 | 条件1 Artifact | 条件2 Artifact”。");
      return conditions.flatMap((condition, conditionIndex) => parsedRows.map(([subjectId, first, second]) => ({ subject_id: subjectId, artifact_id: conditionIndex === 0 ? first : second, group: null, condition })));
    }
    if (parsedRows.some((row) => row.length < (test === "independent_two_sample_t" ? 3 : 2))) throw new Error("影像映射缺少 Artifact ID 或组别。 ");
    return parsedRows.map(([subjectId, artifactId, group]) => ({ subject_id: subjectId, artifact_id: artifactId, group: test === "independent_two_sample_t" ? group : null, condition: null }));
  }

  async function createDesign(): Promise<void> {
    if (!workspace.projectId || !workspace.projectVersion || !workspace.manifestHash || !qc) return;
    setBusy(true);
    setError("");
    try {
      if (!test || !tail || !missingPolicy || !correction) throw new Error("必须明确选择检验、尾部、缺失策略和校正方法。");
      if (correction === "fdr" && tail !== "two_sided") {
        throw new Error("DPABI V8.2 的 FDR T 图校正只支持双尾 p 值；单尾设计请选择 GRF。");
      }
      if (test === "one_sample_t" && (oneSampleBaseline.trim() === "" || !Number.isFinite(Number(oneSampleBaseline)))) {
        throw new Error("单样本 t 检验必须明确填写有限数值基线。");
      }
      const images = buildImages();
      const groups = test === "independent_two_sample_t" ? (lines(groupOrder)[0] ?? []) : [];
      const conditions = test === "paired_t" ? (lines(conditionOrder)[0] ?? []) : [];
      const covariates = parseCovariates(covariateRows, subjects, test);
      const columns = (test === "paired_t" ? 1 + subjects.length : test === "independent_two_sample_t" ? 2 : 1) + covariates.length;
      const df1 = subjects.length - (test === "independent_two_sample_t" ? 2 : 1) - covariates.length;
      if (df1 <= 0) throw new Error("当前样本量与协变量数量不能保留正的残差自由度。");
      const q = Number(qThreshold);
      const voxelP = Number(voxelThreshold);
      const clusterP = Number(clusterThreshold);
      const dlh = Number(grfSmoothnessDlh);
      if (correction === "fdr" && !(q > 0 && q < 1)) throw new Error("FDR q 阈值必须在 0 与 1 之间。");
      if (correction === "grf" && (!(voxelP > 0 && voxelP < 1) || !(clusterP > 0 && clusterP < 1))) {
        throw new Error("GRF voxel p 与 cluster p 阈值都必须在 0 与 1 之间。");
      }
      if (correction === "grf" && !grfSmoothnessMode) throw new Error("必须明确选择 GRF 平滑度来源。");
      if (grfSmoothnessMode === "provided_dlh" && !(dlh > 0)) throw new Error("显式 DLH 必须是正数。");
      const correctionBody = correction === "fdr" ? {
        method: "fdr" as const,
        q_threshold: q,
        mask_artifact_id: maskArtifactId,
        statistic_type: "T" as const,
        df1,
        df2: null,
      } : {
        method: "grf" as const,
        voxel_p_threshold: voxelP,
        cluster_p_threshold: clusterP,
        two_tailed: tail === "two_sided",
        mask_artifact_id: maskArtifactId,
        statistic_type: "T" as const,
        df1,
        df2: null,
        smoothness_mode: grfSmoothnessMode as GrfSmoothnessMode,
        smoothness_dlh: grfSmoothnessMode === "provided_dlh" ? dlh : null,
      };
      const created = await api.createStatisticalDesign({
        project_id: workspace.projectId,
        expected_project_version: workspace.projectVersion,
        input_manifest_hash: workspace.manifestHash,
        design: {
          revision_id: crypto.randomUUID(),
          test,
          subject_order: subjects,
          images,
          group_order: groups,
          condition_order: conditions,
          covariates,
          contrast: [1, ...Array.from({ length: columns - 1 }, () => 0)],
          one_sample_baseline: test === "one_sample_t" ? Number(oneSampleBaseline) : null,
          mask_artifact_id: maskArtifactId,
          tail,
          missing_value_policy: missingPolicy,
          qc_review_revision_id: qc.review.review_revision_id,
          qc_review_hash: qc.review.content_hash,
        },
        correction: correctionBody,
        supersedes_plan_revision_id: workspace.statisticalDesignId ?? null,
      });
      designIdRef.current = created.plan_revision.plan_revision_id;
      setDesign(created);
      updateWorkspace({
        statisticalDesignId: created.plan_revision.plan_revision_id,
        statisticalDesignVersion: created.plan_revision.version,
        statisticalDesignHash: created.plan_revision.plan_hash,
      });
      setMessage("统计设计已创建。矩阵由冻结的 QC 顺序确定；下一步先验证，再单独批准。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function validateDesign(): Promise<void> {
    if (!design) return;
    setBusy(true);
    setError("");
    try {
      const validated = await api.validateStatisticalDesign(design.plan_revision.plan_revision_id, { expected_version: design.plan_revision.version });
      setDesign(validated);
      updateWorkspace({ statisticalDesignVersion: validated.plan_revision.version });
      setMessage("统计设计已通过确定性校验，正在等待明确批准。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function approveDesign(): Promise<void> {
    if (!design) return;
    setBusy(true);
    setError("");
    try {
      if (!approvalActor.trim() || !approvalReason.trim()) throw new Error("必须填写统计设计审批人和审批理由。");
      await api.approvePlan(design.plan_revision.plan_revision_id, {
        expected_version: design.plan_revision.version,
        plan_hash: design.plan_revision.plan_hash,
        actor: approvalActor.trim(),
        decision: "approved",
        reason: approvalReason.trim(),
      });
      const approved = await api.plan(design.plan_revision.plan_revision_id);
      setDesign({ ...design, plan_revision: approved });
      updateWorkspace({ statisticalDesignVersion: approved.version, statisticalDesignHash: approved.plan_hash });
      setMessage("统计设计已批准；运行仍需单独提交。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function submitStatistics(): Promise<void> {
    if (!workspace.projectId || !design) return;
    setBusy(true);
    setError("");
    try {
      const run = await api.createStatisticsRun({
        project_id: workspace.projectId,
        statistical_design_revision_id: design.plan_revision.plan_revision_id,
        expected_plan_hash: design.plan_revision.plan_hash,
        max_attempts: 1,
      });
      updateWorkspace({ runId: run.run_id, runVersion: run.version, runState: run.state });
      setMessage("统计任务已进入本机队列。当前 MVP 使用受控执行契约；真实 MATLAB 仍需单独授权。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader eyebrow="统计" title="版本化统计设计" description="影像、分组和协变量严格按 QC 冻结顺序对齐；校正方法与统计检验分开建模。" />
      <Feedback message={error || message} error={Boolean(error)} />
      {!qc || qc.state !== "approved" ? <section className="panel"><EmptyState title="统计入口已锁定" detail="必须先创建并批准类型化 QC revision，不能用文件系统顺序或临时排除清单绕过。" /></section> : (
        <div className="two-column wide-left">
          <section className="panel">
            <span className="eyebrow">设计设置</span><h2>t 检验与校正</h2>
            <div className="form-grid">
              <label>检验类型<select value={test} onChange={(event) => setTest(event.target.value as TestType | "")} disabled={Boolean(design)}><option value="">明确选择</option><option value="one_sample_t">单样本 t 检验</option><option value="independent_two_sample_t">独立双样本 t 检验</option><option value="paired_t">配对 t 检验</option></select></label>
              <label>尾部<select value={tail} onChange={(event) => { const next = event.target.value as Tail | ""; setTail(next); if (next !== "two_sided" && correction === "fdr") setCorrection(""); }} disabled={Boolean(design)}><option value="">明确选择</option><option value="two_sided">双尾</option><option value="one_sided_positive">正向单尾</option><option value="one_sided_negative">负向单尾</option></select></label>
              {test === "one_sample_t" && <label>单样本基线<input inputMode="decimal" value={oneSampleBaseline} onChange={(event) => setOneSampleBaseline(event.target.value)} disabled={Boolean(design)} /></label>}
              {test === "independent_two_sample_t" && <label>组顺序（组A | 组B）<input value={groupOrder} onChange={(event) => setGroupOrder(event.target.value)} disabled={Boolean(design)} /></label>}
              {test === "paired_t" && <label>条件顺序（条件A | 条件B）<input value={conditionOrder} onChange={(event) => setConditionOrder(event.target.value)} disabled={Boolean(design)} /></label>}
              <label>缺失值策略<select value={missingPolicy} onChange={(event) => setMissingPolicy(event.target.value as typeof missingPolicy)} disabled={Boolean(design)}><option value="">明确选择</option><option value="error">发现缺失即阻断</option><option value="exclude_explicitly">按显式排除 revision</option></select></label>
              <label>脑掩膜 Artifact<select value={maskArtifactId} onChange={(event) => setMaskArtifactId(event.target.value)} disabled={Boolean(design)}><option value="">必须明确选择</option>{artifacts.map((artifact) => <option key={artifact.artifact_id} value={artifact.artifact_id}>{artifact.artifact_type} · {artifact.artifact_id.slice(0, 8)}</option>)}</select></label>
              <label>多重比较<select value={correction} onChange={(event) => setCorrection(event.target.value as typeof correction)} disabled={Boolean(design)}><option value="">明确选择</option><option value="fdr" disabled={tail !== "two_sided"}>FDR（仅双尾）</option><option value="grf">GRF</option></select></label>
              {correction === "fdr" ? <label>q 阈值<input inputMode="decimal" value={qThreshold} onChange={(event) => setQThreshold(event.target.value)} placeholder="由课题方案明确填写" disabled={Boolean(design)} /></label> : correction === "grf" ? <><label>voxel p 阈值<input inputMode="decimal" value={voxelThreshold} onChange={(event) => setVoxelThreshold(event.target.value)} disabled={Boolean(design)} /></label><label>cluster p 阈值<input inputMode="decimal" value={clusterThreshold} onChange={(event) => setClusterThreshold(event.target.value)} disabled={Boolean(design)} /></label><label>GRF 平滑度来源<select value={grfSmoothnessMode} onChange={(event) => setGrfSmoothnessMode(event.target.value as GrfSmoothnessMode | "")} disabled={Boolean(design)}><option value="">明确选择</option><option value="dpabi_header_or_estimate">读取 NIfTI 头或由 DPABI 估计</option><option value="provided_dlh">使用方案提供的 DLH</option></select></label>{grfSmoothnessMode === "provided_dlh" && <label>DLH<input inputMode="decimal" value={grfSmoothnessDlh} onChange={(event) => setGrfSmoothnessDlh(event.target.value)} disabled={Boolean(design)} /></label>}</> : null}
            </div>
            <label className="mapping-field">影像映射（{test === "paired_t" ? "受试者 | 条件A Artifact | 条件B Artifact" : test === "independent_two_sample_t" ? "受试者 | Artifact | 组别" : "受试者 | Artifact"}）<textarea value={imageRows} onChange={(event) => setImageRows(event.target.value)} disabled={Boolean(design)} /></label>
            <label className="mapping-field">协变量（{test === "paired_t" ? "DPABI V8.2 配对检验暂不支持" : "可留空；每行“名称 | none/grand_mean/within_group | 受试者=数值,…”，顺序会按 QC 冻结清单重排"}）<textarea value={covariateRows} onChange={(event) => setCovariateRows(event.target.value)} disabled={Boolean(design) || test === "paired_t"} /></label>
            <p className="muted">t 检验使用 DPABI 适配器支持的首列 canonical contrast；方向由组/条件顺序与尾部共同明确。</p>
            <div className="form-grid">
              <label>统计设计审批人<input value={approvalActor} onChange={(event) => setApprovalActor(event.target.value)} disabled={!design || design.plan_revision.state !== "awaiting_approval"} /></label>
              <label>统计设计审批理由<textarea value={approvalReason} onChange={(event) => setApprovalReason(event.target.value)} disabled={!design || design.plan_revision.state !== "awaiting_approval"} placeholder="说明已核对的受试者顺序、设计方向、尾部、mask 与校正阈值" /></label>
            </div>
            <div className="button-row">
              <button className="button button-secondary" type="button" disabled={busy || Boolean(design) || !test || !tail || !missingPolicy || !correction || !maskArtifactId || !imageRows.trim() || (test === "one_sample_t" && !oneSampleBaseline.trim()) || !(correction === "fdr" ? qThreshold && tail === "two_sided" : voxelThreshold && clusterThreshold && grfSmoothnessMode && (grfSmoothnessMode !== "provided_dlh" || grfSmoothnessDlh))} onClick={createDesign}>生成设计矩阵</button>
              <button className="button button-secondary" type="button" disabled={busy || design?.plan_revision.state !== "draft"} onClick={validateDesign}>验证设计</button>
              <button className="button button-primary" type="button" disabled={busy || design?.plan_revision.state !== "awaiting_approval" || !approvalActor.trim() || !approvalReason.trim()} onClick={approveDesign}>批准统计设计</button>
              <button className="button button-primary" type="button" disabled={busy || design?.plan_revision.state !== "approved"} onClick={submitStatistics}>提交统计运行</button>
            </div>
          </section>
          <aside className="panel matrix-panel">
            <span className="eyebrow">设计矩阵</span><h2>{design ? `${design.design_matrix.length} × ${design.design_matrix[0]?.length ?? 0}` : "等待生成"}</h2>
            {design ? <div className="matrix-table" role="img" aria-label="设计矩阵数值预览">{design.design_matrix.map((row, rowIndex) => <div key={rowIndex}>{row.map((value, columnIndex) => <span key={columnIndex} title={String(value)} style={{ opacity: Math.max(0.15, Math.min(1, Math.abs(value))) }} />)}</div>)}</div> : <EmptyState title="无示意数据" detail="只在后端完成对齐并返回真实矩阵后显示。" />}
            {design && <><StatusPill tone={design.plan_revision.state === "approved" ? "good" : "warn"}>{design.plan_revision.state}</StatusPill><dl className="detail-list design-audit"><div><dt>设计哈希</dt><dd className="hash-value">{design.plan_revision.plan_hash}</dd></div><div><dt>受试者顺序</dt><dd>{design.design.subject_order.join(" → ")}</dd></div><div><dt>检验 / 尾部</dt><dd>{design.design.test} / {design.design.tail}</dd></div><div><dt>组顺序</dt><dd>{design.design.group_order.join(" → ") || "不适用"}</dd></div><div><dt>条件顺序</dt><dd>{design.design.condition_order.join(" → ") || "不适用"}</dd></div><div><dt>Mask</dt><dd>{design.design.mask_artifact_id}</dd></div><div><dt>Contrast</dt><dd>{design.design.contrast.join(", ")}</dd></div><div><dt>校正</dt><dd>{design.correction?.method ?? "未设置"}</dd></div></dl><div className="table-wrap sr-design-table"><table><caption>设计矩阵完整数值</caption><tbody>{design.design_matrix.map((row, rowIndex) => <tr key={rowIndex}>{row.map((value, columnIndex) => <td key={columnIndex}>{value}</td>)}</tr>)}</tbody></table></div></>}
          </aside>
        </div>
      )}
      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">已登记统计结果</span>
            <h2>{results.length} 项</h2>
          </div>
          <button className="button button-secondary" type="button" onClick={() => setResultRefresh((current) => current + 1)}>
            刷新
          </button>
        </div>
        {results.length === 0 ? (
          <EmptyState
            title="尚无统计结果"
            detail="统计运行完成并登记确定性复现报告后，会在这里出现可查询的冻结报告。"
          />
        ) : (
          <div className="two-column wide-left">
            <aside className="panel">
              <span className="eyebrow">结果列表</span>
              <div className="selection-list">
                {results.map((result) => (
                  <button
                    type="button"
                    key={result.result_id}
                    className={result.result_id === selectedResultId ? "selected" : ""}
                    onClick={() => setSelectedResultId(result.result_id)}
                  >
                    <span>{result.result_id.slice(0, 24)}…</span>
                    <small>{result.mode}</small>
                  </button>
                ))}
              </div>
            </aside>
            <div className="panel">
              {!resultDetail ? (
                <EmptyState title="选择一项结果" detail="点击左侧结果查看冻结的复现报告与已登记产物。" />
              ) : (
                <>
                  {resultDetail.non_scientific && (
                    <div className="assistant-message">
                      <strong>合成 / 非科研结果</strong>
                      <p>{resultDetail.non_scientific_reason}</p>
                    </div>
                  )}
                  <dl className="detail-list">
                    <div><dt>结果 ID</dt><dd>{resultDetail.result_id}</dd></div>
                    <div><dt>运行</dt><dd>{resultDetail.run_id}</dd></div>
                    <div><dt>设计 revision</dt><dd>{resultDetail.design_revision_id}</dd></div>
                    <div><dt>产物 / 簇</dt><dd>{resultDetail.artifact_count} / {resultDetail.cluster_count}</dd></div>
                    <div><dt>报告哈希</dt><dd className="hash-value">{resultDetail.bundle_hash}</dd></div>
                  </dl>
                  <pre className="report-pre">{resultDetail.report_markdown}</pre>
                </>
              )}
            </div>
          </div>
        )}
      </section>
    </>
  );
}
