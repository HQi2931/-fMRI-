import { useEffect, useMemo, useRef, useState } from "react";

import { api, describeError, type CompiledSkillPlan, type PlanRevision, type PreprocessingInput, type Skill, type SkillPlanResolveBody } from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";

function numbers(value: string): number[] {
  const tokens = value.trim().split(/[\s,]+/).filter(Boolean);
  const parsed = tokens.map(Number);
  if (parsed.some((item) => !Number.isFinite(item))) throw new Error("数值列表中包含无法识别的内容。");
  return parsed;
}

function triple(value: string, label: string): [number, number, number] {
  const parsed = numbers(value);
  if (parsed.length !== 3 || parsed.some((item) => item <= 0)) throw new Error(`${label} 必须是三个正数。`);
  return parsed as [number, number, number];
}

function bool(value: string, label: string): boolean {
  if (value !== "yes" && value !== "no") throw new Error(`必须明确选择${label}。`);
  return value === "yes";
}

type StepView = { step_id: string; capability?: string; tool?: { capability?: string; tool_id?: string }; needs?: string[] };
type FilterTiming = "disabled" | "before_normalize" | "after_normalize";
type SmoothingTiming = "disabled" | "on_functional_data" | "on_results";
type MetricName = "alff" | "falff" | "reho";
type BooleanChoice = "" | "yes" | "no";
type MetricScaling = "raw" | "global_mean" | "z_score";
type ParameterSource = "user" | "study_protocol" | "dataset_metadata" | "reviewed_preset";
type CensoringInput = NonNullable<PreprocessingInput["scrubbing"]["censoring"]>;
type TissueInput = NonNullable<PreprocessingInput["nuisance"]["white_matter"]>;

function nonNegativeInteger(value: string, label: string): number {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) throw new Error(`${label} 必须是非负整数。`);
  return parsed;
}

function positiveNumber(value: string, label: string): number {
  const parsed = Number(value);
  if (!(parsed > 0) || !Number.isFinite(parsed)) throw new Error(`${label} 必须是正数。`);
  return parsed;
}

function toggleScaling(current: MetricScaling[], value: MetricScaling, checked: boolean): MetricScaling[] {
  return checked ? [...current, value] : current.filter((item) => item !== value);
}

function readableValue(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? String(value);
}

export function PlanPage() {
  const workspace = useWorkspace();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plan, setPlan] = useState<PlanRevision | null>(null);
  const planIdRef = useRef<string | null>(null);
  const [skillPlan, setSkillPlan] = useState<Partial<CompiledSkillPlan> | null>(null);
  const [steps, setSteps] = useState<StepView[]>([]);
  const [protocol, setProtocol] = useState("");
  const [parameterSource, setParameterSource] = useState<ParameterSource | "">("");
  const [parameterEvidence, setParameterEvidence] = useState("");
  const [tr, setTr] = useState("");
  const [timePoints, setTimePoints] = useState("");
  const [dummyScans, setDummyScans] = useState("");
  const [sliceTiming, setSliceTiming] = useState<BooleanChoice>("");
  const [sliceOrder, setSliceOrder] = useState("");
  const [referenceSlice, setReferenceSlice] = useState("");
  const [realignment, setRealignment] = useState<BooleanChoice>("");
  const [nuisance, setNuisance] = useState<BooleanChoice>("");
  const [nuisanceTiming, setNuisanceTiming] = useState("");
  const [polynomialTrend, setPolynomialTrend] = useState("");
  const [headMotionModel, setHeadMotionModel] = useState("");
  const [nuisanceCensoring, setNuisanceCensoring] = useState<BooleanChoice>("");
  const [nuisanceFdType, setNuisanceFdType] = useState("");
  const [nuisanceFdThreshold, setNuisanceFdThreshold] = useState("");
  const [nuisancePreviousPoints, setNuisancePreviousPoints] = useState("");
  const [nuisanceLaterPoints, setNuisanceLaterPoints] = useState("");
  const [whiteMatter, setWhiteMatter] = useState<BooleanChoice>("");
  const [whiteMatterMask, setWhiteMatterMask] = useState("");
  const [whiteMatterThreshold, setWhiteMatterThreshold] = useState("");
  const [whiteMatterMethod, setWhiteMatterMethod] = useState("");
  const [whiteMatterComponents, setWhiteMatterComponents] = useState("");
  const [csf, setCsf] = useState<BooleanChoice>("");
  const [csfMask, setCsfMask] = useState("");
  const [csfThreshold, setCsfThreshold] = useState("");
  const [csfMethod, setCsfMethod] = useState("");
  const [csfComponents, setCsfComponents] = useState("");
  const [globalSignal, setGlobalSignal] = useState<BooleanChoice>("");
  const [globalSignalMask, setGlobalSignalMask] = useState("");
  const [globalSignalMethod, setGlobalSignalMethod] = useState("");
  const [warpMasks, setWarpMasks] = useState<BooleanChoice>("");
  const [nuisanceAddMean, setNuisanceAddMean] = useState<BooleanChoice>("");
  const [normalizationMode, setNormalizationMode] = useState("");
  const [normalizationTiming, setNormalizationTiming] = useState("");
  const [voxelSize, setVoxelSize] = useState("");
  const [boundingBox, setBoundingBox] = useState("");
  const [structuralArtifact, setStructuralArtifact] = useState("");
  const [affineRegularization, setAffineRegularization] = useState("");
  const [detrend, setDetrend] = useState("");
  const [filterTiming, setFilterTiming] = useState<FilterTiming | "">("");
  const [filterLow, setFilterLow] = useState("");
  const [filterHigh, setFilterHigh] = useState("");
  const [filterAddMean, setFilterAddMean] = useState<BooleanChoice>("");
  const [scrubbing, setScrubbing] = useState<BooleanChoice>("");
  const [scrubbingTiming, setScrubbingTiming] = useState("");
  const [fdType, setFdType] = useState("");
  const [fdThreshold, setFdThreshold] = useState("");
  const [previousPoints, setPreviousPoints] = useState("");
  const [laterPoints, setLaterPoints] = useState("");
  const [scrubbingMethod, setScrubbingMethod] = useState("");
  const [smoothingTiming, setSmoothingTiming] = useState<SmoothingTiming | "">("");
  const [smoothingMethod, setSmoothingMethod] = useState("");
  const [smoothingFwhm, setSmoothingFwhm] = useState("");
  const [alff, setAlff] = useState(false);
  const [falff, setFalff] = useState(false);
  const [reho, setReho] = useState(false);
  const [metricLow, setMetricLow] = useState("");
  const [metricHigh, setMetricHigh] = useState("");
  const [rehoNeighbors, setRehoNeighbors] = useState("");
  const [alffScalings, setAlffScalings] = useState<MetricScaling[]>([]);
  const [rehoScalings, setRehoScalings] = useState<MetricScaling[]>([]);
  const [metricMaskArtifact, setMetricMaskArtifact] = useState("");
  const [resultSmoothing, setResultSmoothing] = useState<BooleanChoice>("");
  const [resultSmoothingFwhm, setResultSmoothingFwhm] = useState("");
  const [smoothReho, setSmoothReho] = useState<BooleanChoice>("");
  const [smoothRehoFwhm, setSmoothRehoFwhm] = useState("");
  const [approvalActor, setApprovalActor] = useState("");
  const [approvalReason, setApprovalReason] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api.skills(controller.signal).then(setSkills).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    if (workspace.planRevisionId && planIdRef.current !== workspace.planRevisionId) {
      api.plan(workspace.planRevisionId, controller.signal).then((stored) => {
        planIdRef.current = stored.plan_revision_id;
        setPlan(stored);
        const payload = stored.plan as { skill_plan?: Partial<CompiledSkillPlan> };
        setSkillPlan(payload.skill_plan ?? null);
        setSteps(payload.skill_plan?.steps ?? []);
        updateWorkspace({
          planVersion: stored.version,
          planHash: stored.plan_hash,
          planState: stored.state,
        });
      }).catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
      });
    }
    return () => controller.abort();
  }, [workspace.planRevisionId]);

  const selectedMetrics = useMemo(() => [alff && "alff", falff && "falff", reho && "reho"].filter(Boolean) as MetricName[], [alff, falff, reho]);

  function provenance(names: string[]): Array<{ name: string; source: ParameterSource; evidence_ref: string }> {
    if (!parameterSource || !parameterEvidence.trim()) throw new Error("必须明确选择科学参数来源并填写可核验的来源证据。");
    return names.map((name) => ({ name, source: parameterSource, evidence_ref: parameterEvidence.trim() }));
  }

  function censoring(
    choice: BooleanChoice,
    fdKind: string,
    threshold: string,
    previous: string,
    later: string,
    label: string,
  ): CensoringInput | null {
    if (!bool(choice, `是否${label}`)) return null;
    if (fdKind !== "fd_power" && fdKind !== "fd_jenkinson") throw new Error(`必须明确选择${label}的 FD 类型。`);
    return {
      fd_type: fdKind,
      fd_threshold_mm: positiveNumber(threshold, `${label} FD 阈值`),
      previous_points: nonNegativeInteger(previous, `${label}前向时间点`),
      later_points: nonNegativeInteger(later, `${label}后向时间点`),
    };
  }

  function tissueRegressor(
    choice: BooleanChoice,
    mask: string,
    threshold: string,
    method: string,
    components: string,
    label: string,
  ): TissueInput {
    if (!bool(choice, `是否回归${label}`)) {
      return { enabled: false, mask_source: null, mask_threshold: null, method: null, compcor_components: null };
    }
    if (mask !== "spm" && mask !== "segment") throw new Error(`必须明确选择${label}掩膜来源。`);
    const parsedThreshold = positiveNumber(threshold, `${label}掩膜阈值`);
    if (parsedThreshold > 1) throw new Error(`${label}掩膜阈值不能大于 1。`);
    if (method !== "mean" && method !== "compcor") throw new Error(`必须明确选择${label}回归方法。`);
    return {
      enabled: true,
      mask_source: mask,
      mask_threshold: parsedThreshold,
      method,
      compcor_components: method === "compcor" ? positiveNumber(components, `${label} CompCor 成分数`) : null,
    };
  }

  function preprocessingBody(): PreprocessingInput {
    const trSeconds = positiveNumber(tr, "TR");
    const expectedTimePoints = timePoints.trim() ? positiveNumber(timePoints, "期望时间点") : null;
    if (expectedTimePoints !== null && !Number.isInteger(expectedTimePoints)) throw new Error("期望时间点必须是正整数。");
    const parsedDummyScans = nonNegativeInteger(dummyScans, "删除初始时间点数量");
    if (expectedTimePoints !== null && parsedDummyScans >= expectedTimePoints) throw new Error("删除初始时间点数量必须小于期望时间点。");
    const useSliceTiming = bool(sliceTiming, "是否进行时间层校正");
    const useRealignment = bool(realignment, "是否进行头动校正");
    const useNuisance = bool(nuisance, "是否进行协变量回归");
    const useScrubbing = bool(scrubbing, "是否进行 Scrubbing");
    const useDetrend = bool(detrend, "是否单独去趋势");
    if (!normalizationMode) throw new Error("必须明确选择标准化方法。 ");
    if (!filterTiming) throw new Error("必须明确选择滤波时点。 ");
    if (!smoothingTiming) throw new Error("必须明确选择平滑时点。 ");
    const band = filterTiming === "disabled" ? null : {
      low_hz: Number(filterLow),
      high_hz: Number(filterHigh),
    };
    if (band && !(band.low_hz >= 0 && band.high_hz > band.low_hz && Number.isFinite(band.low_hz))) {
      throw new Error("滤波频段必须满足 0 ≤ low < high。");
    }
    if (band && band.high_hz > 1 / (2 * trSeconds)) throw new Error("滤波 high Hz 不能超过由 TR 决定的 Nyquist 频率。");
    const normalizationEnabled = normalizationMode !== "0";
    const bboxValues = numbers(boundingBox);
    if (normalizationEnabled && bboxValues.length !== 6) throw new Error("标准化 bounding box 必须填写六个数字。 ");
    if (normalizationEnabled && !normalizationTiming) throw new Error("必须明确选择标准化时点。");
    const normalizationModeNumber = Number(normalizationMode) as 0 | 1 | 2 | 3;
    const structural = normalizationModeNumber === 2 || normalizationModeNumber === 3;
    if (structural && !structuralArtifact.trim()) throw new Error("T1 标准化必须选择结构像 Artifact ID。");
    if (structural && affineRegularization !== "mni" && affineRegularization !== "eastern") {
      throw new Error("必须明确选择仿射正则化模板。");
    }
    const nuisanceCensor = useNuisance
      ? censoring(nuisanceCensoring, nuisanceFdType, nuisanceFdThreshold, nuisancePreviousPoints, nuisanceLaterPoints, "将头动异常点加入协变量回归")
      : null;
    let nuisanceBody: PreprocessingInput["nuisance"];
    if (useNuisance) {
      if (nuisanceTiming !== "after_realign" && nuisanceTiming !== "after_normalize") throw new Error("必须明确选择协变量回归时点。");
      const trend = Number(polynomialTrend);
      if (!Number.isInteger(trend) || trend < -1) throw new Error("多项式趋势阶数必须是大于等于 -1 的整数。");
      const motion = Number(headMotionModel);
      if (![0, 1, 2, 3, 4].includes(motion)) throw new Error("必须明确选择头动回归模型。");
      if (!useRealignment && (motion !== 0 || nuisanceCensor)) throw new Error("头动回归或异常点回归要求启用头动校正。");
      const useGsr = bool(globalSignal, "是否回归全局信号");
      if (useGsr && globalSignalMask !== "spm" && globalSignalMask !== "auto_mask") throw new Error("必须明确选择全局信号掩膜来源。");
      if (useGsr && globalSignalMethod !== "mean") throw new Error("必须明确选择全局信号回归方法。");
      nuisanceBody = {
        enabled: true,
        timing: nuisanceTiming,
        polynomial_trend: trend,
        head_motion_model: motion as 0 | 1 | 2 | 3 | 4,
        head_motion_scrubbing: nuisanceCensor,
        white_matter: tissueRegressor(whiteMatter, whiteMatterMask, whiteMatterThreshold, whiteMatterMethod, whiteMatterComponents, "白质信号"),
        csf: tissueRegressor(csf, csfMask, csfThreshold, csfMethod, csfComponents, "脑脊液信号"),
        global_signal: useGsr
          ? { enabled: true, mask_source: globalSignalMask as "spm" | "auto_mask", method: "mean" }
          : { enabled: false, mask_source: null, method: null },
        warp_masks_to_individual_space: bool(warpMasks, "是否将掩膜形变到个体空间"),
        add_mean_back: bool(nuisanceAddMean, "协变量回归后是否加回均值"),
      };
      if (useDetrend && trend >= 1) throw new Error("单独去趋势不能与协变量回归中的多项式趋势重复。");
    } else {
      nuisanceBody = {
        enabled: false,
        timing: null,
        polynomial_trend: null,
        head_motion_model: null,
        head_motion_scrubbing: null,
        white_matter: null,
        csf: null,
        global_signal: null,
        warp_masks_to_individual_space: null,
        add_mean_back: null,
      };
    }
    const scrubCensor = useScrubbing
      ? censoring("yes", fdType, fdThreshold, previousPoints, laterPoints, "Scrubbing")
      : null;
    if (useScrubbing && !useRealignment) throw new Error("Scrubbing 要求启用头动校正。");
    if (useScrubbing && scrubbingTiming !== "after_preprocessing") throw new Error("必须明确选择 Scrubbing 时点。");
    if (useScrubbing && !["cut", "nearest", "linear", "spline", "pchip"].includes(scrubbingMethod)) {
      throw new Error("必须明确选择 Scrubbing 插值或删除方法。");
    }
    if (smoothingTiming !== "disabled" && !["1", "2"].includes(smoothingMethod)) throw new Error("必须明确选择平滑方法。");
    if (smoothingTiming !== "disabled" && smoothingMethod === "2" && normalizationMode !== "3") {
      throw new Error("DARTEL 平滑要求选择 DARTEL 标准化。");
    }
    return {
      tr_seconds: trSeconds,
      expected_time_points: expectedTimePoints,
      dummy_scans: parsedDummyScans,
      slice_timing: useSliceTiming ? {
        enabled: true,
        slice_count: numbers(sliceOrder).length,
        slice_order: numbers(sliceOrder),
        reference_slice: positiveNumber(referenceSlice, "参考层"),
      } : { enabled: false, slice_count: null, slice_order: null, reference_slice: null },
      realignment: { enabled: useRealignment, options_source: useRealignment ? "dpabi_v82_jobmat" : null },
      nuisance: nuisanceBody,
      normalization: normalizationEnabled ? {
        mode: normalizationModeNumber,
        timing: normalizationTiming as "on_functional_data" | "on_results",
        bounding_box_mm: [bboxValues.slice(0, 3), bboxValues.slice(3, 6)] as [[number, number, number], [number, number, number]],
        voxel_size_mm: triple(voxelSize, "标准化体素大小"),
        structural_artifact_id: structural ? structuralArtifact.trim() : null,
        affine_regularization: structural ? affineRegularization as "mni" | "eastern" : null,
      } : { mode: 0 as const, timing: null, bounding_box_mm: null, voxel_size_mm: null, structural_artifact_id: null, affine_regularization: null },
      detrend: useDetrend,
      temporal_filter: {
        timing: filterTiming,
        frequency_band: band,
        add_mean_back: band ? bool(filterAddMean, "滤波后是否加回均值") : null,
      },
      scrubbing: useScrubbing ? {
        enabled: true,
        timing: "after_preprocessing",
        censoring: scrubCensor,
        method: scrubbingMethod as "cut" | "nearest" | "linear" | "spline" | "pchip",
      } : { enabled: false, timing: null, censoring: null, method: null },
      smoothing: smoothingTiming === "disabled" ? { timing: "disabled", method: null, fwhm_mm: null } : { timing: smoothingTiming, method: Number(smoothingMethod) as 1 | 2, fwhm_mm: triple(smoothingFwhm, "平滑 FWHM") },
      provenance: provenance(["tr_seconds", "expected_time_points", "dummy_scans", "slice_timing", "realignment", "nuisance", "normalization", "detrend", "temporal_filter", "scrubbing", "smoothing"]),
    };
  }

  async function compilePlan(): Promise<void> {
    if (!workspace.projectId || !workspace.projectVersion || !workspace.datasetId || !workspace.manifestHash) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      if (!protocol.trim()) throw new Error("必须填写课题方案或预注册依据。 ");
      if (selectedMetrics.length === 0) throw new Error("至少选择一个指标。 ");
      const trSeconds = Number(tr);
      const metricBand = { low_hz: Number(metricLow), high_hz: Number(metricHigh) };
      const alffRequested = alff || falff;
      if (alffRequested && !(metricBand.low_hz >= 0 && metricBand.high_hz > metricBand.low_hz)) throw new Error("ALFF/fALFF 指标频段必须满足 0 ≤ low < high。 ");
      if (alffRequested && alffScalings.length === 0) throw new Error("必须至少选择一种 ALFF/fALFF scaling。 ");
      if (reho && rehoScalings.length === 0) throw new Error("必须至少选择一种 ReHo scaling。 ");
      if (reho && !["7", "19", "27"].includes(rehoNeighbors)) throw new Error("必须明确选择 ReHo 邻域。 ");
      const metricMask = metricMaskArtifact.trim();
      if (!metricMask) throw new Error("ALFF、fALFF 和 ReHo 都必须选择已登记的脑掩膜 Artifact。 ");
      const useResultSmoothing = bool(resultSmoothing, "是否进行全局指标结果平滑");
      const useSmoothReho = reho ? bool(smoothReho, "是否执行 ReHo 专用 SmoothReHo") : false;
      if (useResultSmoothing && useSmoothReho) throw new Error("ReHo 专用 SmoothReHo 与全局结果平滑不能同时启用。 ");
      const preprocessing = preprocessingBody();
      if (preprocessing.expected_time_points === null) {
        throw new Error("同一工作流计算指标前必须填写期望时间点，并由运行时头信息检查确认。 ");
      }
      if (reho && preprocessing.scrubbing.enabled && preprocessing.scrubbing.method === "cut") {
        throw new Error("CUT Scrubbing 后实际保留时间点未知；请先完成预处理并选择已验证 Artifact，再规划 ReHo。 ");
      }
      const retainedVolumes = preprocessing.expected_time_points - preprocessing.dummy_scans;
      const frequencyResolution = 1 / (preprocessing.tr_seconds * retainedVolumes);
      if (
        alffRequested
        && ((metricBand.low_hz > 0 && metricBand.low_hz < frequencyResolution)
          || metricBand.high_hz < frequencyResolution)
      ) {
        throw new Error("指标频段低于有效时间点支持的频率分辨率。 ");
      }
      const globalResultFwhm = useResultSmoothing ? triple(resultSmoothingFwhm, "全局指标结果平滑 FWHM") : null;
      const resolvedSmoothRehoFwhm = useSmoothReho ? triple(smoothRehoFwhm, "ReHo 专用平滑 FWHM") : null;
      const request: SkillPlanResolveBody["request"] = {
        project_id: workspace.projectId,
        dataset_ref: workspace.datasetId,
        input_manifest_hash: workspace.manifestHash,
        requested_metrics: selectedMetrics,
        primary_outputs: selectedMetrics.map((metric) => `metric.${metric}`),
        input_artifact_id: null,
        alff_falff: alffRequested ? {
          tr_seconds: trSeconds,
          frequency_band: metricBand,
          requested_metrics: selectedMetrics.filter((metric) => metric === "alff" || metric === "falff"),
          requested_scalings: alffScalings,
          mask_artifact_id: metricMask,
          filter_timing: preprocessing.temporal_filter.timing,
          result_smoothing: useResultSmoothing,
          result_smoothing_fwhm_mm: globalResultFwhm,
          provenance: provenance(["tr_seconds", "frequency_band", "requested_metrics", "requested_scalings", "mask_artifact_id", "filter_timing", "result_smoothing"]),
        } : null,
        reho: reho ? {
          tr_seconds: trSeconds,
          temporal_filter_band: preprocessing.temporal_filter.frequency_band,
          temporal_filter_add_mean_back: preprocessing.temporal_filter.add_mean_back,
          cluster_voxels: Number(rehoNeighbors),
          mask_artifact_id: metricMask,
          requested_scalings: rehoScalings,
          smooth_reho: useSmoothReho,
          smooth_reho_fwhm_mm: resolvedSmoothRehoFwhm,
          global_result_smoothing: useResultSmoothing,
          global_result_smoothing_fwhm_mm: globalResultFwhm,
          provenance: provenance(["tr_seconds", "temporal_filter_band", "temporal_filter_add_mean_back", "cluster_voxels", "mask_artifact_id", "requested_scalings", "smooth_reho", "global_result_smoothing"]),
        } : null,
        study_protocol_ref: protocol.trim(),
        request_preprocessing: true,
        preprocessing,
      };
      const resolved = await api.resolveSkillPlan({
        request,
        expected_project_version: workspace.projectVersion,
        supersedes_plan_revision_id: workspace.planRevisionId ?? null,
      });
      planIdRef.current = resolved.plan_revision.plan_revision_id;
      setPlan(resolved.plan_revision);
      setSkillPlan(resolved.skill_plan);
      setSteps(resolved.skill_plan.steps as StepView[]);
      updateWorkspace({
        planRevisionId: resolved.plan_revision.plan_revision_id,
        planVersion: resolved.plan_revision.version,
        planHash: resolved.plan_revision.plan_hash,
        planState: resolved.plan_revision.state,
      });
      setMessage("不可变 SkillPlan 已编译并通过阻断校验。请审查 DAG、警告和哈希后再批准。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function approve(): Promise<void> {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      if (!approvalActor.trim() || !approvalReason.trim()) throw new Error("必须填写计划审批人和审批理由。");
      await api.approvePlan(plan.plan_revision_id, {
        expected_version: plan.version,
        plan_hash: plan.plan_hash,
        actor: approvalActor.trim(),
        decision: "approved",
        reason: approvalReason.trim(),
      });
      const approved = await api.plan(plan.plan_revision_id);
      setPlan(approved);
      updateWorkspace({ planVersion: approved.version, planHash: approved.plan_hash, planState: approved.state });
      setMessage("计划已批准。输入、参数、Skill、Tool 或环境发生变化后，此批准会自动失效。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  const planAuditReady = Boolean(skillPlan?.environment && Array.isArray(skillPlan.resolved_parameters));

  if (!workspace.manifestId) return <><PageHeader eyebrow="分析方案" title="检查顺序、参数与风险" description="先冻结数据清单，才能把科学参数绑定到确定的输入。" /><section className="panel"><EmptyState title="尚无冻结 manifest" detail="请先在数据页面完成只读检查。" /></section></>;

  return (
    <>
      <PageHeader eyebrow="分析方案" title="检查顺序、参数与风险" description="所有会改变科学结果的选择都在此显式填写；系统不会用隐藏默认值补齐。" action={plan && <StatusPill tone={plan.state === "approved" ? "good" : "warn"}>{plan.state}</StatusPill>} />
      <Feedback message={error || message} error={Boolean(error)} />
      <section className="panel plan-form">
        <div className="panel-heading"><div><span className="eyebrow">输入与指标</span><h2>课题级明确选择</h2></div><span className="muted">已注册 Skill：{skills.length}</span></div>
        <div className="form-grid">
          <label>课题方案 / 预注册依据<input value={protocol} onChange={(event) => setProtocol(event.target.value)} disabled={Boolean(plan)} /></label>
          <label>科学参数来源<select value={parameterSource} onChange={(event) => setParameterSource(event.target.value as ParameterSource | "")} disabled={Boolean(plan)}><option value="">明确选择</option><option value="user">研究者明确输入</option><option value="study_protocol">课题方案 / 预注册</option><option value="dataset_metadata">数据集元数据</option><option value="reviewed_preset">经审核的预设</option></select></label>
          <label>参数来源证据<input value={parameterEvidence} onChange={(event) => setParameterEvidence(event.target.value)} disabled={Boolean(plan)} placeholder="文档版本、元数据字段或预设版本；不可留空" /></label>
          <label>TR（秒）<input inputMode="decimal" value={tr} onChange={(event) => setTr(event.target.value)} disabled={Boolean(plan)} /></label>
          <label>期望时间点（同工作流指标必填）<input inputMode="numeric" value={timePoints} onChange={(event) => setTimePoints(event.target.value)} disabled={Boolean(plan)} /></label>
          <label>删除初始时间点数量<input inputMode="numeric" value={dummyScans} onChange={(event) => setDummyScans(event.target.value)} disabled={Boolean(plan)} /></label>
        </div>
        <fieldset><legend>指标</legend><label className="check-field"><input type="checkbox" checked={alff} onChange={(event) => setAlff(event.target.checked)} disabled={Boolean(plan)} /> ALFF</label><label className="check-field"><input type="checkbox" checked={falff} onChange={(event) => setFalff(event.target.checked)} disabled={Boolean(plan)} /> fALFF</label><label className="check-field"><input type="checkbox" checked={reho} onChange={(event) => setReho(event.target.checked)} disabled={Boolean(plan)} /> ReHo</label></fieldset>
        <div className="form-grid">
          {(alff || falff) && <><label>指标频段 low Hz<input value={metricLow} onChange={(event) => setMetricLow(event.target.value)} disabled={Boolean(plan)} /></label><label>指标频段 high Hz<input value={metricHigh} onChange={(event) => setMetricHigh(event.target.value)} disabled={Boolean(plan)} /></label></>}
          {reho && <label>ReHo 邻域<select value={rehoNeighbors} onChange={(event) => setRehoNeighbors(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="7">7</option><option value="19">19</option><option value="27">27</option></select></label>}
        </div>
        {selectedMetrics.length > 0 && <details open><summary>指标输出参数</summary>
          {(alff || falff) && <fieldset><legend>ALFF/fALFF scaling（至少一项）</legend>{(["raw", "global_mean", "z_score"] as const).map((scaling) => <label className="check-field" key={scaling}><input type="checkbox" checked={alffScalings.includes(scaling)} onChange={(event) => setAlffScalings((current) => toggleScaling(current, scaling, event.target.checked))} disabled={Boolean(plan)} /> ALFF/fALFF {scaling}</label>)}</fieldset>}
          {reho && <fieldset><legend>ReHo scaling（至少一项）</legend>{(["raw", "global_mean", "z_score"] as const).map((scaling) => <label className="check-field" key={scaling}><input type="checkbox" checked={rehoScalings.includes(scaling)} onChange={(event) => setRehoScalings((current) => toggleScaling(current, scaling, event.target.checked))} disabled={Boolean(plan)} /> ReHo {scaling}</label>)}</fieldset>}
          <div className="form-grid details-grid">
            <label>指标脑掩膜 Artifact ID（必填）<input value={metricMaskArtifact} onChange={(event) => setMetricMaskArtifact(event.target.value)} disabled={Boolean(plan)} /></label>
            <label>全局指标结果平滑<select value={resultSmoothing} onChange={(event) => setResultSmoothing(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
            {resultSmoothing === "yes" && <label>全局指标结果平滑 FWHM mm（3 数值）<input value={resultSmoothingFwhm} onChange={(event) => setResultSmoothingFwhm(event.target.value)} disabled={Boolean(plan)} /></label>}
            {reho && <label>ReHo 专用 SmoothReHo<select value={smoothReho} onChange={(event) => setSmoothReho(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>}
            {reho && smoothReho === "yes" && <label>ReHo 专用平滑 FWHM mm（3 数值）<input value={smoothRehoFwhm} onChange={(event) => setSmoothRehoFwhm(event.target.value)} disabled={Boolean(plan)} /></label>}
          </div>
        </details>}
        <details open><summary>公共预处理参数</summary>
          <div className="form-grid details-grid">
            <label>Slice timing<select value={sliceTiming} onChange={(event) => setSliceTiming(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="no">关闭</option><option value="yes">启用</option></select></label>
            {sliceTiming === "yes" && <><label>Slice order<input value={sliceOrder} onChange={(event) => setSliceOrder(event.target.value)} placeholder="例如 1,3,5,…" disabled={Boolean(plan)} /></label><label>参考层<input value={referenceSlice} onChange={(event) => setReferenceSlice(event.target.value)} disabled={Boolean(plan)} /></label></>}
            <label>Realign<select value={realignment} onChange={(event) => setRealignment(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
            <label>协变量回归<select value={nuisance} onChange={(event) => setNuisance(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
            {nuisance === "yes" && <>
              <label>协变量回归时点<select value={nuisanceTiming} onChange={(event) => setNuisanceTiming(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="after_realign">头动校正后</option><option value="after_normalize">标准化后</option></select></label>
              <label>多项式趋势阶数<input inputMode="numeric" value={polynomialTrend} onChange={(event) => setPolynomialTrend(event.target.value)} disabled={Boolean(plan)} /></label>
              <label>头动回归模型<select value={headMotionModel} onChange={(event) => setHeadMotionModel(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="0">不回归头动参数</option><option value="1">6 参数</option><option value="2">12 参数</option><option value="3">6 参数及平方项</option><option value="4">Friston 24</option></select></label>
              <label>头动异常点回归<select value={nuisanceCensoring} onChange={(event) => setNuisanceCensoring(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
              {nuisanceCensoring === "yes" && <><label>异常点回归 FD 类型<select value={nuisanceFdType} onChange={(event) => setNuisanceFdType(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="fd_power">Power FD</option><option value="fd_jenkinson">Jenkinson FD</option></select></label><label>异常点回归 FD 阈值 mm<input value={nuisanceFdThreshold} onChange={(event) => setNuisanceFdThreshold(event.target.value)} disabled={Boolean(plan)} /></label><label>异常点回归前向时间点<input value={nuisancePreviousPoints} onChange={(event) => setNuisancePreviousPoints(event.target.value)} disabled={Boolean(plan)} /></label><label>异常点回归后向时间点<input value={nuisanceLaterPoints} onChange={(event) => setNuisanceLaterPoints(event.target.value)} disabled={Boolean(plan)} /></label></>}
              <label>白质信号回归<select value={whiteMatter} onChange={(event) => setWhiteMatter(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
              {whiteMatter === "yes" && <><label>白质掩膜来源<select value={whiteMatterMask} onChange={(event) => setWhiteMatterMask(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="spm">SPM 掩膜</option><option value="segment">Segment 掩膜</option></select></label><label>白质掩膜阈值<input value={whiteMatterThreshold} onChange={(event) => setWhiteMatterThreshold(event.target.value)} disabled={Boolean(plan)} /></label><label>白质回归方法<select value={whiteMatterMethod} onChange={(event) => setWhiteMatterMethod(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="mean">均值</option><option value="compcor">CompCor</option></select></label>{whiteMatterMethod === "compcor" && <label>白质 CompCor 成分数<input value={whiteMatterComponents} onChange={(event) => setWhiteMatterComponents(event.target.value)} disabled={Boolean(plan)} /></label>}</>}
              <label>脑脊液信号回归<select value={csf} onChange={(event) => setCsf(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
              {csf === "yes" && <><label>脑脊液掩膜来源<select value={csfMask} onChange={(event) => setCsfMask(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="spm">SPM 掩膜</option><option value="segment">Segment 掩膜</option></select></label><label>脑脊液掩膜阈值<input value={csfThreshold} onChange={(event) => setCsfThreshold(event.target.value)} disabled={Boolean(plan)} /></label><label>脑脊液回归方法<select value={csfMethod} onChange={(event) => setCsfMethod(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="mean">均值</option><option value="compcor">CompCor</option></select></label>{csfMethod === "compcor" && <label>脑脊液 CompCor 成分数<input value={csfComponents} onChange={(event) => setCsfComponents(event.target.value)} disabled={Boolean(plan)} /></label>}</>}
              <label>全局信号回归<select value={globalSignal} onChange={(event) => setGlobalSignal(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
              {globalSignal === "yes" && <><label>全局信号掩膜来源<select value={globalSignalMask} onChange={(event) => setGlobalSignalMask(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="spm">SPM 掩膜</option><option value="auto_mask">自动掩膜</option></select></label><label>全局信号回归方法<select value={globalSignalMethod} onChange={(event) => setGlobalSignalMethod(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="mean">均值</option></select></label></>}
              <label>掩膜形变到个体空间<select value={warpMasks} onChange={(event) => setWarpMasks(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
              <label>协变量回归后加回均值<select value={nuisanceAddMean} onChange={(event) => setNuisanceAddMean(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
            </>}
            <label>标准化<select value={normalizationMode} onChange={(event) => setNormalizationMode(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="0">关闭</option><option value="1">EPI 模板</option><option value="2">T1 Segment</option><option value="3">DARTEL</option></select></label>
            {normalizationMode && normalizationMode !== "0" && <><label>标准化时点<select value={normalizationTiming} onChange={(event) => setNormalizationTiming(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="on_functional_data">功能像上</option><option value="on_results">指标结果上</option></select></label><label>Bounding box（6 数值）<input value={boundingBox} onChange={(event) => setBoundingBox(event.target.value)} disabled={Boolean(plan)} /></label><label>体素大小 mm（3 数值）<input value={voxelSize} onChange={(event) => setVoxelSize(event.target.value)} disabled={Boolean(plan)} /></label></>}
            {(normalizationMode === "2" || normalizationMode === "3") && <><label>结构像 Artifact ID<input value={structuralArtifact} onChange={(event) => setStructuralArtifact(event.target.value)} disabled={Boolean(plan)} /></label><label>仿射正则化<select value={affineRegularization} onChange={(event) => setAffineRegularization(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="mni">MNI</option><option value="eastern">East Asian</option></select></label></>}
            <label>单独去趋势<select value={detrend} onChange={(event) => setDetrend(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
            <label>滤波时点<select value={filterTiming} onChange={(event) => setFilterTiming(event.target.value as FilterTiming | "")} disabled={Boolean(plan)}><option value="">明确选择</option><option value="disabled">关闭</option><option value="before_normalize">标准化前</option><option value="after_normalize">标准化后</option></select></label>
            {filterTiming && filterTiming !== "disabled" && <><label>滤波 low Hz<input value={filterLow} onChange={(event) => setFilterLow(event.target.value)} disabled={Boolean(plan)} /></label><label>滤波 high Hz<input value={filterHigh} onChange={(event) => setFilterHigh(event.target.value)} disabled={Boolean(plan)} /></label><label>滤波后加回均值<select value={filterAddMean} onChange={(event) => setFilterAddMean(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label></>}
            <label>Scrubbing<select value={scrubbing} onChange={(event) => setScrubbing(event.target.value as BooleanChoice)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="yes">启用</option><option value="no">关闭</option></select></label>
            {scrubbing === "yes" && <><label>Scrubbing 时点<select value={scrubbingTiming} onChange={(event) => setScrubbingTiming(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="after_preprocessing">预处理后</option></select></label><label>Scrubbing FD 类型<select value={fdType} onChange={(event) => setFdType(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="fd_power">Power FD</option><option value="fd_jenkinson">Jenkinson FD</option></select></label><label>FD 阈值 mm<input value={fdThreshold} onChange={(event) => setFdThreshold(event.target.value)} disabled={Boolean(plan)} /></label><label>Scrubbing 前向时间点<input value={previousPoints} onChange={(event) => setPreviousPoints(event.target.value)} disabled={Boolean(plan)} /></label><label>Scrubbing 后向时间点<input value={laterPoints} onChange={(event) => setLaterPoints(event.target.value)} disabled={Boolean(plan)} /></label><label>Scrubbing 方法<select value={scrubbingMethod} onChange={(event) => setScrubbingMethod(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="cut">删除</option><option value="nearest">最近邻</option><option value="linear">线性</option><option value="spline">Spline</option><option value="pchip">PCHIP</option></select></label></>}
            <label>平滑时点<select value={smoothingTiming} onChange={(event) => setSmoothingTiming(event.target.value as SmoothingTiming | "")} disabled={Boolean(plan)}><option value="">明确选择</option><option value="disabled">关闭</option><option value="on_functional_data">功能像上</option><option value="on_results">指标结果上</option></select></label>
            {smoothingTiming && smoothingTiming !== "disabled" && <><label>平滑方法<select value={smoothingMethod} onChange={(event) => setSmoothingMethod(event.target.value)} disabled={Boolean(plan)}><option value="">明确选择</option><option value="1">SPM</option><option value="2">DARTEL</option></select></label><label>FWHM mm（3 数值）<input value={smoothingFwhm} onChange={(event) => setSmoothingFwhm(event.target.value)} disabled={Boolean(plan)} /></label></>}
          </div>
        </details>
        <div className="button-row"><button className="button button-primary" type="button" disabled={busy || Boolean(plan) || !protocol.trim() || !parameterSource || !parameterEvidence.trim()} onClick={compilePlan}>校验并编译计划</button></div>
      </section>
      {plan && <div className="two-column wide-left"><section className="panel"><div className="panel-heading"><div><span className="eyebrow">计划 DAG</span><h2>{steps.length} 个确定性步骤</h2></div><code>{plan.plan_hash.slice(0, 12)}…</code></div><div className="workflow-map">{steps.map((step, index) => <div className="workflow-node" key={step.step_id}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{step.step_id}</strong><small>{step.tool?.capability ?? step.capability ?? step.tool?.tool_id}</small></div>{index < steps.length - 1 && <i aria-hidden="true" />}</div>)}</div><details className="audit-details" open><summary>冻结参数与环境锁</summary><dl className="detail-list"><div><dt>完整计划哈希</dt><dd className="hash-value">{plan.plan_hash}</dd></div><div><dt>manifest 哈希</dt><dd className="hash-value">{plan.manifest_hash}</dd></div><div><dt>环境哈希</dt><dd className="hash-value">{plan.environment_hash}</dd></div>{skillPlan?.preprocessing_parameters_hash && <div><dt>预处理参数哈希</dt><dd className="hash-value">{skillPlan.preprocessing_parameters_hash}</dd></div>}{skillPlan?.environment && <><div><dt>MATLAB</dt><dd>{skillPlan.environment.matlab_version}</dd></div><div><dt>SPM</dt><dd>{skillPlan.environment.spm_version}</dd></div><div><dt>DPABI</dt><dd>{skillPlan.environment.dpabi_version}</dd></div><div><dt>适配器</dt><dd>{skillPlan.environment.adapter_version}</dd></div></>}</dl><h3>解析后的科学参数</h3>{skillPlan?.resolved_parameters?.length ? <dl className="frozen-parameter-list">{skillPlan.resolved_parameters.map(([name, value]) => <div key={name}><dt>{name}</dt><dd><pre>{readableValue(value)}</pre></dd></div>)}</dl> : <p className="muted">此记录没有可展示的解析参数条目；审批仍会绑定空参数集合、环境与完整哈希。</p>}<h3>Skill / Tool 锁</h3>{skillPlan?.skill_locks?.length ? <ul className="compact-list">{skillPlan.skill_locks.map((lock) => <li key={`${lock.skill_id}-${lock.version}`}>{lock.skill_id} · {lock.version}<small>{lock.content_hash}</small></li>)}</ul> : <p className="muted">此记录未提供 Skill 锁详情。</p>}{skillPlan?.warnings?.length ? <div className="issue-box"><strong>编译警告</strong><ul>{skillPlan.warnings.map((warning, index) => <li key={`${warning.code}-${index}`}>{warning.code}：{warning.message}</li>)}</ul></div> : null}</details></section><aside className="panel"><span className="eyebrow">审批绑定</span><h2>不可变 revision</h2><dl className="detail-list"><div><dt>manifest</dt><dd>{plan.manifest_hash.slice(0, 10)}…</dd></div><div><dt>environment</dt><dd>{plan.environment_hash.slice(0, 10)}…</dd></div><div><dt>计划版本</dt><dd>{plan.version}</dd></div><div><dt>阻断问题</dt><dd>{plan.validation_issues.filter((issue) => issue.severity === "blocking").length}</dd></div></dl>{!planAuditReady && <div className="issue-box"><StatusPill tone="danger">审批已阻断</StatusPill><p>当前响应缺少冻结的解析参数或环境快照，请刷新或联系维护者。</p></div>}<div className="form-grid"><label>计划审批人<input value={approvalActor} onChange={(event) => setApprovalActor(event.target.value)} disabled={plan.state !== "awaiting_approval" || !planAuditReady} /></label><label>计划审批理由<textarea value={approvalReason} onChange={(event) => setApprovalReason(event.target.value)} disabled={plan.state !== "awaiting_approval" || !planAuditReady} placeholder="说明已核对的 manifest、科学参数、DAG、Skill/Tool 与环境锁" /></label></div><button className="button button-primary button-full" type="button" disabled={busy || plan.state !== "awaiting_approval" || !planAuditReady || !approvalActor.trim() || !approvalReason.trim()} onClick={approve}>确认并批准此版本</button></aside></div>}
    </>
  );
}
