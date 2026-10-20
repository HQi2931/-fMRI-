import { useEffect, useMemo, useRef, useState } from "react";

import { plansApi } from "../../api/plans";
import { projectsApi } from "../../api/projects";
import { runsApi } from "../../api/runs";
import { describeError } from "../../api/transport";
import type { Artifact, Dataset, Manifest, Project, SkillPlanResolveBody } from "../../api/types";
import "./metric-only-checkpoint-picker.css";

export type MetricOnlyContext = {
  key: string;
  conversationId: string;
  workspacePath: string;
  projectId: string;
  datasetId: string;
  manifestId: string;
  manifestHash: string;
};

export type MetricOnlySource = { key: string; taskId: string; runId: string };

type VerifiedContext = { project: Project; dataset: Dataset; manifest: Manifest };
type MetricKind = "alff" | "falff" | "reho";
type Scaling = "raw" | "global_mean" | "z_score";
type MaskSource = "registered_artifact" | "dpabi_auto";

type FormState = {
  metrics: MetricKind[];
  alffTr: string;
  alffLow: string;
  alffHigh: string;
  alffScalings: Scaling[];
  alffMaskSource: "" | MaskSource;
  alffMaskId: string;
  alffFilterTiming: "" | "disabled" | "before_normalize" | "after_normalize";
  alffSmoothing: "" | "true" | "false";
  alffFwhm: [string, string, string];
  rehoTr: string;
  rehoTemporalFilter: "" | "disabled" | "enabled";
  rehoLow: string;
  rehoHigh: string;
  rehoAddMeanBack: "" | "true" | "false";
  rehoClusterVoxels: "" | "7" | "19" | "27";
  rehoScalings: Scaling[];
  rehoMaskSource: "" | MaskSource;
  rehoMaskId: string;
  rehoSmoothing: "" | "true" | "false";
  rehoFwhm: [string, string, string];
  rehoGlobalSmoothing: "" | "true" | "false";
  rehoGlobalFwhm: [string, string, string];
};

type ArtifactLineage = Record<string, unknown>;
type DryRunResult = Awaited<ReturnType<typeof plansApi.dryRunSkillPlan>>;
type AlffFalffParameters = NonNullable<SkillPlanResolveBody["request"]["alff_falff"]>;
type RehoParameters = NonNullable<SkillPlanResolveBody["request"]["reho"]>;

const emptyForm = (): FormState => ({
  metrics: [],
  alffTr: "", alffLow: "", alffHigh: "", alffScalings: [],
  alffMaskSource: "", alffMaskId: "", alffFilterTiming: "", alffSmoothing: "", alffFwhm: ["", "", ""],
  rehoTr: "", rehoTemporalFilter: "", rehoLow: "", rehoHigh: "", rehoAddMeanBack: "",
  rehoClusterVoxels: "", rehoScalings: [], rehoMaskSource: "", rehoMaskId: "",
  rehoSmoothing: "", rehoFwhm: ["", "", ""], rehoGlobalSmoothing: "", rehoGlobalFwhm: ["", "", ""],
});

function contextIdentity(context: MetricOnlyContext): string {
  return JSON.stringify([
    context.key, context.conversationId, context.workspacePath, context.projectId,
    context.datasetId, context.manifestId, context.manifestHash,
  ]);
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function artifactLineage(artifact: Artifact): ArtifactLineage | null {
  const provenance = record(artifact.provenance);
  return provenance ? record(provenance.lineage) : null;
}

function metricArtifactMatches(
  artifact: Artifact,
  projectId: string,
  runId: string,
  manifest: Manifest,
): boolean {
  const subject = manifest.subjects[0];
  const lineage = artifactLineage(artifact);
  return Boolean(
    manifest.subjects.length === 1 && subject && lineage
    && artifact.project_id === projectId
    && artifact.run_id === runId
    && artifact.artifact_type === "preprocessing.functional_timeseries"
    && lineage.artifact_id === artifact.artifact_id
    && lineage.kind === "functional_timeseries"
    && lineage.metadata_verified === true
    && lineage.subject_manifest_hash === manifest.content_hash
    && lineage.subject_id === subject.subject_id
    && lineage.session_id === (subject.session_id ?? null),
  );
}

function compatibleMasks(artifacts: Artifact[], selected: Artifact, manifest: Manifest): Artifact[] {
  const timeseries = artifactLineage(selected);
  if (!timeseries || typeof timeseries.mask_artifact_id !== "string" || typeof timeseries.mask_grid_signature !== "string") return [];
  return artifacts.filter((artifact) => {
    const lineage = artifactLineage(artifact);
    return Boolean(
      lineage
      && artifact.project_id === selected.project_id
      && artifact.run_id === selected.run_id
      && artifact.artifact_type === "preprocessing.brain_mask"
      && artifact.artifact_id === timeseries.mask_artifact_id
      && lineage.artifact_id === artifact.artifact_id
      && lineage.kind === "brain_mask"
      && lineage.metadata_verified === true
      && lineage.subject_manifest_hash === manifest.content_hash
      && lineage.subject_id === null
      && lineage.session_id === null
      && lineage.grid_signature === timeseries.mask_grid_signature,
    );
  });
}

function freshContextError(context: MetricOnlyContext, value: VerifiedContext): string | null {
  if (!context.key || !context.conversationId || !context.workspacePath || !context.projectId || !context.datasetId || !context.manifestId || !context.manifestHash) {
    return "当前会话缺少项目、数据集或清单版本信息，无法安全预检。";
  }
  if (value.project.project_id !== context.projectId || value.dataset.project_id !== context.projectId || value.dataset.dataset_id !== context.datasetId) {
    return "刷新后的项目或数据集与当前会话不一致，请重新选择工作区。";
  }
  if (value.dataset.current_manifest_id !== context.manifestId || value.manifest.manifest_id !== context.manifestId || value.manifest.dataset_id !== context.datasetId || value.manifest.content_hash !== context.manifestHash) {
    return "当前数据清单已变化，请刷新会话并重新核对来源。";
  }
  if (value.manifest.subjects.length !== 1) {
    return "当前 metric-only 执行器只支持一个清单受试者；请准备单受试者清单后再预检。";
  }
  return null;
}

function parsePositive(value: string): number | null {
  const parsed = Number(value);
  return value.trim() && Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function parseBand(low: string, high: string): { low_hz: number; high_hz: number } | null {
  const lowValue = parsePositive(low);
  const highValue = parsePositive(high);
  return lowValue !== null && highValue !== null && lowValue < highValue
    ? { low_hz: lowValue, high_hz: highValue } : null;
}

function parseFwhm(values: [string, string, string]): [number, number, number] | null {
  const parsed = values.map(parsePositive);
  return parsed.every((value): value is number => value !== null)
    ? [parsed[0], parsed[1], parsed[2]] : null;
}

function userProvenance(names: string[]) {
  return names.map((name) => ({ name, source: "user" as const, evidence_ref: "metric-only dry-run form" }));
}

function requestedOutputs(form: FormState): string[] {
  const metricNames: Record<MetricKind, string> = { alff: "ALFF", falff: "fALFF", reho: "ReHo" };
  const scalingPrefix: Record<Scaling, string> = { raw: "", global_mean: "m", z_score: "z" };
  return form.metrics.flatMap((metric) => {
    const scalings = metric === "reho" ? form.rehoScalings : form.alffScalings;
    return scalings.map((scaling) => `${scalingPrefix[scaling]}${metricNames[metric]}`);
  });
}

function buildBody(
  form: FormState,
  selected: Artifact,
  snapshot: VerifiedContext,
  alffMaskId: string | null,
  rehoMaskId: string | null,
): SkillPlanResolveBody | null {
  const metrics = [...form.metrics];
  if (!metrics.length) return null;
  const alffMetrics = metrics.filter((metric): metric is "alff" | "falff" => metric === "alff" || metric === "falff");
  const alffTr = parsePositive(form.alffTr);
  const alffBand = parseBand(form.alffLow, form.alffHigh);
  const alffFwhm = form.alffFwhm.every((value) => !value.trim()) ? null : parseFwhm(form.alffFwhm);
  const alffEnabled = alffMetrics.length > 0;
  if (alffEnabled && (
    alffTr === null || !alffBand || !form.alffScalings.length || !form.alffMaskSource
    || (form.alffMaskSource === "registered_artifact" && !alffMaskId)
    || !form.alffFilterTiming || !form.alffSmoothing
    || (form.alffSmoothing === "true" && !alffFwhm)
    || (form.alffSmoothing === "false" && form.alffFwhm.some((value) => value.trim()))
    || (form.alffSmoothing === "true" && form.alffFwhm.some((value) => !value.trim()))
  )) return null;

  const rehoEnabled = metrics.includes("reho");
  const rehoTr = parsePositive(form.rehoTr);
  const rehoBand = form.rehoTemporalFilter === "enabled" ? parseBand(form.rehoLow, form.rehoHigh) : null;
  const rehoFwhm = form.rehoFwhm.every((value) => !value.trim()) ? null : parseFwhm(form.rehoFwhm);
  const rehoGlobalFwhm = form.rehoGlobalFwhm.every((value) => !value.trim()) ? null : parseFwhm(form.rehoGlobalFwhm);
  if (rehoEnabled && (
    rehoTr === null || !form.rehoTemporalFilter || (form.rehoTemporalFilter === "enabled" && (!rehoBand || !form.rehoAddMeanBack))
    || (form.rehoTemporalFilter === "disabled" && (form.rehoLow.trim() || form.rehoHigh.trim() || form.rehoAddMeanBack))
    || !form.rehoClusterVoxels || !form.rehoScalings.length || !form.rehoMaskSource
    || (form.rehoMaskSource === "registered_artifact" && !rehoMaskId)
    || !form.rehoSmoothing || !form.rehoGlobalSmoothing
    || (form.rehoSmoothing === "true" && !rehoFwhm)
    || (form.rehoSmoothing === "false" && form.rehoFwhm.some((value) => value.trim()))
    || (form.rehoSmoothing === "true" && form.rehoFwhm.some((value) => !value.trim()))
    || (form.rehoGlobalSmoothing === "true" && !rehoGlobalFwhm)
    || (form.rehoGlobalSmoothing === "false" && form.rehoGlobalFwhm.some((value) => value.trim()))
    || (form.rehoGlobalSmoothing === "true" && form.rehoGlobalFwhm.some((value) => !value.trim()))
  )) return null;

  const alff: AlffFalffParameters | null = alffEnabled ? {
    tr_seconds: alffTr!,
    frequency_band: alffBand!,
    requested_metrics: alffMetrics,
    requested_scalings: form.alffScalings,
    mask_source: form.alffMaskSource as MaskSource,
    ...(form.alffMaskSource === "registered_artifact" ? { mask_artifact_id: alffMaskId } : { mask_artifact_id: null }),
    filter_timing: form.alffFilterTiming as "disabled" | "before_normalize" | "after_normalize",
    result_smoothing: form.alffSmoothing === "true",
    result_smoothing_fwhm_mm: form.alffSmoothing === "true" ? alffFwhm! : null,
    provenance: userProvenance([
      "tr_seconds", "frequency_band", "requested_metrics", "requested_scalings", "mask_source", "mask_artifact_id",
      "filter_timing", "result_smoothing", "result_smoothing_fwhm_mm",
    ]),
  } : null;

  const reho: RehoParameters | null = rehoEnabled ? {
    tr_seconds: rehoTr!,
    temporal_filter_band: form.rehoTemporalFilter === "enabled" ? rehoBand! : null,
    temporal_filter_add_mean_back: form.rehoTemporalFilter === "enabled" ? form.rehoAddMeanBack === "true" : null,
    cluster_voxels: Number(form.rehoClusterVoxels),
    requested_scalings: form.rehoScalings,
    mask_source: form.rehoMaskSource as MaskSource,
    ...(form.rehoMaskSource === "registered_artifact" ? { mask_artifact_id: rehoMaskId } : { mask_artifact_id: null }),
    smooth_reho: form.rehoSmoothing === "true",
    smooth_reho_fwhm_mm: form.rehoSmoothing === "true" ? rehoFwhm! : null,
    global_result_smoothing: form.rehoGlobalSmoothing === "true",
    global_result_smoothing_fwhm_mm: form.rehoGlobalSmoothing === "true" ? rehoGlobalFwhm! : null,
    provenance: userProvenance([
      "tr_seconds", "temporal_filter_band", "temporal_filter_add_mean_back", "cluster_voxels", "requested_scalings",
      "mask_source", "mask_artifact_id", "smooth_reho", "smooth_reho_fwhm_mm", "global_result_smoothing",
      "global_result_smoothing_fwhm_mm",
    ]),
  } : null;

  return {
    expected_project_version: snapshot.project.version,
    validation_mode: "strict",
    request: {
      project_id: snapshot.project.project_id,
      dataset_ref: snapshot.dataset.dataset_id,
      input_manifest_hash: snapshot.manifest.content_hash,
      input_artifact_id: selected.artifact_id,
      request_preprocessing: false,
      requested_metrics: metrics,
      primary_outputs: requestedOutputs(form),
      ...(alffEnabled ? { alff_falff: alff } : {}),
      ...(rehoEnabled ? { reho } : {}),
    },
  } as SkillPlanResolveBody;
}

function toggle<T extends string>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function MaskFields({
  label, source, artifactId, masks, disabled, onSource, onArtifact,
}: {
  label: string;
  source: "" | MaskSource;
  artifactId: string;
  masks: Artifact[];
  disabled: boolean;
  onSource: (value: "" | MaskSource) => void;
  onArtifact: (value: string) => void;
}) {
  return <>
    <label>{label}掩膜来源
      <select aria-label={`${label} 掩膜来源`} value={source} disabled={disabled} onChange={(event) => onSource(event.target.value as "" | MaskSource)}>
        <option value="">请选择掩膜来源</option>
        <option value="registered_artifact">选择同次运行的已登记掩膜</option>
        <option value="dpabi_auto">DPABI AutoMask</option>
      </select>
    </label>
    {source === "registered_artifact" && <label>{label}脑掩膜产物
      <select aria-label={`${label} 脑掩膜产物`} value={artifactId} disabled={disabled} onChange={(event) => onArtifact(event.target.value)}>
        <option value="">请选择脑掩膜</option>
        {masks.map((mask) => <option key={mask.artifact_id} value={mask.artifact_id}>{mask.artifact_id}</option>)}
      </select>
      {masks.length === 0 && <small>当前时序没有可核验的同网格已登记脑掩膜；可在掩膜来源中明确选择 AutoMask。</small>}
    </label>}
  </>;
}

function FwhmFields({ label, values, disabled, onChange }: {
  label: string; values: [string, string, string]; disabled: boolean;
  onChange: (values: [string, string, string]) => void;
}) {
  return <fieldset className="metric-only-fwhm">
    <legend>{label}平滑核（毫米）</legend>
    {(["x", "y", "z"] as const).map((axis, index) => <label key={axis}>{axis.toUpperCase()} 方向
      <input aria-label={`${label} ${axis.toUpperCase()} 方向（毫米）`} type="number" min="0" step="any" value={values[index]} disabled={disabled} onChange={(event) => {
        const next: [string, string, string] = [...values]; next[index] = event.target.value; onChange(next);
      }} />
    </label>)}
  </fieldset>;
}

export function MetricOnlyCheckpointPicker({ context, sources }: {
  context: MetricOnlyContext;
  sources: MetricOnlySource[];
}) {
  const identity = contextIdentity(context);
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const contextController = useRef<AbortController | null>(null);
  const runController = useRef<AbortController | null>(null);
  const dryRunController = useRef<AbortController | null>(null);
  const [snapshot, setSnapshot] = useState<VerifiedContext | null>(null);
  const [snapshotIdentity, setSnapshotIdentity] = useState("");
  const [contextLoading, setContextLoading] = useState(true);
  const [contextError, setContextError] = useState("");
  const [selectedSourceKey, setSelectedSourceKey] = useState("");
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState("");
  const [runArtifacts, setRunArtifacts] = useState<Artifact[]>([]);
  const [candidateArtifacts, setCandidateArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifactId, setSelectedArtifactId] = useState("");
  const [previewFailed, setPreviewFailed] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [dryRunLoading, setDryRunLoading] = useState(false);
  const [dryRunError, setDryRunError] = useState("");
  const [dryRunResult, setDryRunResult] = useState<DryRunResult | null>(null);
  const activeSnapshot = snapshotIdentity === identity ? snapshot : null;

  const sourceFingerprint = JSON.stringify(sources.map((source) => [source.key, source.taskId, source.runId]));
  const selectedSource = sources.find((source) => source.key === selectedSourceKey) ?? null;
  const selectedArtifact = candidateArtifacts.find((artifact) => artifact.artifact_id === selectedArtifactId) ?? null;
  const masks = useMemo(() => selectedArtifact && activeSnapshot
    ? compatibleMasks(runArtifacts, selectedArtifact, activeSnapshot.manifest) : [],
  [runArtifacts, selectedArtifact, activeSnapshot]);
  const alffMaskId = form.alffMaskSource === "registered_artifact" && masks.some((mask) => mask.artifact_id === form.alffMaskId) ? form.alffMaskId : null;
  const rehoMaskId = form.rehoMaskSource === "registered_artifact" && masks.some((mask) => mask.artifact_id === form.rehoMaskId) ? form.rehoMaskId : null;
  const requestBody = selectedArtifact && activeSnapshot ? buildBody(form, selectedArtifact, activeSnapshot, alffMaskId, rehoMaskId) : null;

  useEffect(() => {
    const [key, conversationId, workspacePath, projectId, datasetId, manifestId, manifestHash] = JSON.parse(identity) as [string, string, string, string, string, string, string];
    const requestedContext: MetricOnlyContext = { key, conversationId, workspacePath, projectId, datasetId, manifestId, manifestHash };
    const controller = new AbortController();
    contextController.current = controller;
    setSnapshot(null);
    setContextLoading(true);
    setContextError("");
    setSelectedSourceKey("");
    setRunLoading(false);
    setRunError("");
    setRunArtifacts([]);
    setCandidateArtifacts([]);
    setSelectedArtifactId("");
    setPreviewFailed(false);
    setDryRunLoading(false);
    setDryRunError("");
    setDryRunResult(null);
    setForm(emptyForm());
    const load = async () => {
      if (!key || !conversationId || !workspacePath || !projectId || !datasetId || !manifestId || !manifestHash) {
        throw new Error("当前会话缺少项目、数据集或清单版本信息，无法安全预检。");
      }
      const [project, dataset] = await Promise.all([
        projectsApi.project(projectId, controller.signal),
        projectsApi.dataset(datasetId, controller.signal),
      ]);
      const currentManifestId = dataset.current_manifest_id;
      if (!currentManifestId) throw new Error("当前数据集没有已登记清单，请先检查并登记数据清单。");
      const manifest = await projectsApi.manifest(currentManifestId, controller.signal);
      const value = { project, dataset, manifest };
      const issue = freshContextError(requestedContext, value);
      if (issue) throw new Error(issue);
      return value;
    };
    void load().then((value) => {
      if (controller.signal.aborted || identityRef.current !== identity) return;
      setSnapshot(value);
      setSnapshotIdentity(identity);
      setContextLoading(false);
    }).catch((caught) => {
      if (controller.signal.aborted || identityRef.current !== identity) return;
      setContextError(`当前预检输入不可用：${describeError(caught)}`);
      setContextLoading(false);
    });
    return () => {
      controller.abort();
      if (contextController.current === controller) contextController.current = null;
      runController.current?.abort();
      dryRunController.current?.abort();
    };
  }, [identity]);

  useEffect(() => {
    runController.current?.abort();
    dryRunController.current?.abort();
    setSelectedSourceKey("");
    setRunLoading(false);
    setRunArtifacts([]);
    setCandidateArtifacts([]);
    setSelectedArtifactId("");
    setPreviewFailed(false);
    setDryRunLoading(false);
    setRunError("");
    setDryRunError("");
    setDryRunResult(null);
    setForm(emptyForm());
  }, [sourceFingerprint]);

  useEffect(() => () => {
    contextController.current?.abort();
    runController.current?.abort();
    dryRunController.current?.abort();
  }, []);

  async function chooseSource(sourceKey: string) {
    runController.current?.abort();
    dryRunController.current?.abort();
    setSelectedSourceKey(sourceKey);
    setRunArtifacts([]);
    setCandidateArtifacts([]);
    setSelectedArtifactId("");
    setPreviewFailed(false);
    setRunError("");
    setDryRunError("");
    setDryRunResult(null);
    setForm(emptyForm());
    const source = sources.find((item) => item.key === sourceKey);
    if (!source || !activeSnapshot) return;
    const controller = new AbortController();
    runController.current = controller;
    const capturedIdentity = identity;
    setRunLoading(true);
    try {
      const run = await runsApi.run(source.runId, controller.signal);
      if (controller.signal.aborted || identityRef.current !== capturedIdentity) return;
      if (run.run_id !== source.runId || run.project_id !== activeSnapshot.project.project_id || run.state !== "succeeded") {
        throw new Error("来源运行尚未成功完成，或不属于当前项目；请选择已完成的预处理运行。");
      }
      const artifacts = await runsApi.artifacts(source.runId, controller.signal);
      if (controller.signal.aborted || identityRef.current !== capturedIdentity) return;
      setRunArtifacts(artifacts);
      const candidates = artifacts.filter((artifact) => metricArtifactMatches(
        artifact, activeSnapshot.project.project_id, source.runId, activeSnapshot.manifest,
      ));
      setCandidateArtifacts(candidates);
      if (candidates.length === 0) {
        setRunError("该运行没有与当前单受试者清单、版本和身份谱系一致的功能时序产物。请核验运行来源或清单版本。");
      }
    } catch (caught) {
      if (!controller.signal.aborted && identityRef.current === capturedIdentity) {
        setRunError(`无法读取所选预处理运行：${describeError(caught)}`);
      }
    } finally {
      if (!controller.signal.aborted && identityRef.current === capturedIdentity) setRunLoading(false);
    }
  }

  function chooseArtifact(artifactId: string) {
    dryRunController.current?.abort();
    setSelectedArtifactId(candidateArtifacts.some((item) => item.artifact_id === artifactId) ? artifactId : "");
    setPreviewFailed(false);
    setDryRunError("");
    setDryRunResult(null);
    setForm(emptyForm());
  }

  function updateForm(update: (current: FormState) => FormState) {
    dryRunController.current?.abort();
    setForm(update);
    setDryRunError("");
    setDryRunResult(null);
  }

  async function refreshContext(controller: AbortController): Promise<VerifiedContext> {
    const [project, dataset] = await Promise.all([
      projectsApi.project(context.projectId, controller.signal),
      projectsApi.dataset(context.datasetId, controller.signal),
    ]);
    const manifestId = dataset.current_manifest_id;
    if (!manifestId) throw new Error("当前数据集没有已登记清单。");
    const manifest = await projectsApi.manifest(manifestId, controller.signal);
    const value = { project, dataset, manifest };
    const issue = freshContextError(context, value);
    if (issue) throw new Error(issue);
    return value;
  }

  async function previewPlan() {
    if (!requestBody || !selectedSource || !selectedArtifact || dryRunLoading) return;
    dryRunController.current?.abort();
    const controller = new AbortController();
    dryRunController.current = controller;
    const capturedIdentity = identity;
    const capturedArtifactId = selectedArtifact.artifact_id;
    setDryRunLoading(true);
    setDryRunError("");
    setDryRunResult(null);
    try {
      const current = await refreshContext(controller);
      if (controller.signal.aborted || identityRef.current !== capturedIdentity || selectedArtifactId !== capturedArtifactId) return;
      const body = buildBody(form, selectedArtifact, current, alffMaskId, rehoMaskId);
      if (!body) return;
      const result = await plansApi.dryRunSkillPlan(body, controller.signal);
      if (controller.signal.aborted || identityRef.current !== capturedIdentity || selectedArtifactId !== capturedArtifactId) return;
      setDryRunResult(result);
    } catch (caught) {
      if (!controller.signal.aborted && identityRef.current === capturedIdentity) {
        setDryRunError(`方案预检未完成：${describeError(caught)}`);
      }
    } finally {
      if (!controller.signal.aborted && identityRef.current === capturedIdentity) setDryRunLoading(false);
    }
  }

  const formDisabled = !selectedArtifact || dryRunLoading;
  return <section className="metric-only-picker" aria-label="从预处理产物预检指标方案" aria-busy={contextLoading || runLoading || dryRunLoading}>
    <div className="metric-only-picker-heading">
      <div><span className="eyebrow">只读预检</span><h2>从预处理产物准备指标</h2></div>
    </div>
    <p>仅检查候选产物并编译指标方案，不创建正式计划、审批或运行。科学参数需由你明确选择；此处不会启动 MATLAB。</p>
    {contextLoading && <p role="status">正在刷新项目、数据集和清单版本…</p>}
    {contextError && <p role="alert">{contextError}</p>}
    {activeSnapshot && !contextError && <>
      <div className="metric-only-source-fields">
        <label>来源预处理运行
          <select aria-label="来源预处理运行" value={selectedSourceKey} disabled={runLoading || dryRunLoading} onChange={(event) => void chooseSource(event.target.value)}>
            <option value="">请明确选择任务和运行</option>
            {sources.map((source) => <option key={source.key} value={source.key}>任务 {source.taskId} · 运行 {source.runId}</option>)}
          </select>
        </label>
        <label>功能时序产物
          <select aria-label="功能时序产物" value={selectedArtifactId} disabled={!selectedSource || runLoading || dryRunLoading || candidateArtifacts.length === 0} onChange={(event) => chooseArtifact(event.target.value)}>
            <option value="">请明确选择产物</option>
            {candidateArtifacts.map((artifact) => <option key={artifact.artifact_id} value={artifact.artifact_id}>{artifact.relative_path}</option>)}
          </select>
        </label>
      </div>
      {runLoading && <p role="status">正在核验运行和产物身份…</p>}
      {runError && <p className="metric-only-error" role="alert">{runError}</p>}
      {selectedArtifact && <figure className="metric-only-preview">
        {previewFailed
          ? <p role="status">图像预览不可用；这不影响服务端对产物谱系的核验。</p>
          : <img
            alt="预处理功能时序图像预览"
            loading="lazy"
            src={`/api/v1/artifacts/${encodeURIComponent(selectedArtifact.artifact_id)}/preview.png`}
            onError={() => setPreviewFailed(true)}
          />}
        <figcaption>图像预览仅供查看，不是空间或科学质量控制。</figcaption>
      </figure>}
      {selectedArtifact && <div className="metric-only-form">
        <h3>指标与科研参数</h3>
        <p>这些数值会进入只读编译预检。请按研究方案、数据记录或已确认的方法填写，不确定时先向研究团队核实。</p>
        <fieldset>
          <legend>本次指标（至少选择一项）</legend>
          {(["alff", "falff", "reho"] as const).map((metric) => <label key={metric} className="metric-only-check">
            <input type="checkbox" checked={form.metrics.includes(metric)} disabled={formDisabled} onChange={() => updateForm((current) => ({ ...current, metrics: toggle(current.metrics, metric) }))} />
            {metric === "alff" ? "ALFF" : metric === "falff" ? "fALFF" : "ReHo"}
          </label>)}
        </fieldset>
        {(form.metrics.includes("alff") || form.metrics.includes("falff")) && <fieldset>
          <legend>ALFF / fALFF 参数</legend>
          <label>指标 TR（秒）<input aria-label="ALFF/fALFF TR（秒）" type="number" min="0" step="any" value={form.alffTr} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, alffTr: event.target.value }))} /></label>
          <div className="metric-only-two-columns">
            <label>频段下限（Hz）<input aria-label="ALFF/fALFF 频段下限（Hz）" type="number" min="0" step="any" value={form.alffLow} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, alffLow: event.target.value }))} /></label>
            <label>频段上限（Hz）<input aria-label="ALFF/fALFF 频段上限（Hz）" type="number" min="0" step="any" value={form.alffHigh} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, alffHigh: event.target.value }))} /></label>
          </div>
          <fieldset><legend>输出标准化方式</legend>{(["raw", "global_mean", "z_score"] as const).map((scaling) => <label className="metric-only-check" key={scaling}>
            <input aria-label={`ALFF/fALFF ${scaling === "raw" ? "原始值" : scaling === "global_mean" ? "全脑均值归一化" : "z 标准化"}`} type="checkbox" checked={form.alffScalings.includes(scaling)} disabled={formDisabled} onChange={() => updateForm((current) => ({ ...current, alffScalings: toggle(current.alffScalings, scaling) }))} />
            {scaling === "raw" ? "原始值" : scaling === "global_mean" ? "全脑均值归一化" : "z 标准化"}
          </label>)}</fieldset>
          <MaskFields label="ALFF/fALFF" source={form.alffMaskSource} artifactId={form.alffMaskId} masks={masks} disabled={formDisabled} onSource={(value) => updateForm((current) => ({ ...current, alffMaskSource: value, alffMaskId: "" }))} onArtifact={(value) => updateForm((current) => ({ ...current, alffMaskId: value }))} />
          <label>指标滤波阶段
            <select value={form.alffFilterTiming} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, alffFilterTiming: event.target.value as FormState["alffFilterTiming"] }))}>
              <option value="">请选择滤波阶段</option><option value="disabled">不另行滤波</option><option value="before_normalize">标准化前滤波</option><option value="after_normalize">标准化后滤波</option>
            </select>
          </label>
          <label>指标结果图平滑
            <select value={form.alffSmoothing} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, alffSmoothing: event.target.value as FormState["alffSmoothing"], alffFwhm: ["", "", ""] }))}>
              <option value="">请选择是否平滑</option><option value="false">不平滑</option><option value="true">平滑</option>
            </select>
          </label>
          {form.alffSmoothing === "true" && <FwhmFields label="ALFF/fALFF 结果图" values={form.alffFwhm} disabled={formDisabled} onChange={(value) => updateForm((current) => ({ ...current, alffFwhm: value }))} />}
        </fieldset>}
        {form.metrics.includes("reho") && <fieldset>
          <legend>ReHo 参数</legend>
          <label>ReHo TR（秒）<input type="number" min="0" step="any" value={form.rehoTr} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoTr: event.target.value }))} /></label>
          <label>ReHo 时间滤波
            <select value={form.rehoTemporalFilter} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoTemporalFilter: event.target.value as FormState["rehoTemporalFilter"], rehoLow: "", rehoHigh: "", rehoAddMeanBack: "" }))}>
              <option value="">请选择</option><option value="disabled">不使用 ReHo 时间滤波</option><option value="enabled">明确设置 ReHo 时间滤波</option>
            </select>
          </label>
          {form.rehoTemporalFilter === "enabled" && <>
            <div className="metric-only-two-columns">
              <label>频段下限（Hz）<input aria-label="ReHo 频段下限（Hz）" type="number" min="0" step="any" value={form.rehoLow} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoLow: event.target.value }))} /></label>
              <label>频段上限（Hz）<input aria-label="ReHo 频段上限（Hz）" type="number" min="0" step="any" value={form.rehoHigh} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoHigh: event.target.value }))} /></label>
            </div>
            <label>滤波后加回均值
              <select value={form.rehoAddMeanBack} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoAddMeanBack: event.target.value as FormState["rehoAddMeanBack"] }))}>
                <option value="">请选择</option><option value="true">加回均值</option><option value="false">不加回均值</option>
              </select>
            </label>
          </>}
          <label>邻域体素数
            <select value={form.rehoClusterVoxels} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoClusterVoxels: event.target.value as FormState["rehoClusterVoxels"] }))}>
              <option value="">请选择邻域体素数</option><option value="7">7</option><option value="19">19</option><option value="27">27</option>
            </select>
          </label>
          <fieldset><legend>ReHo 输出标准化方式</legend>{(["raw", "global_mean", "z_score"] as const).map((scaling) => <label className="metric-only-check" key={scaling}>
            <input aria-label={`ReHo ${scaling === "raw" ? "原始值" : scaling === "global_mean" ? "全脑均值归一化" : "z 标准化"}`} type="checkbox" checked={form.rehoScalings.includes(scaling)} disabled={formDisabled} onChange={() => updateForm((current) => ({ ...current, rehoScalings: toggle(current.rehoScalings, scaling) }))} />
            {scaling === "raw" ? "原始值" : scaling === "global_mean" ? "全脑均值归一化" : "z 标准化"}
          </label>)}</fieldset>
          <MaskFields label="ReHo" source={form.rehoMaskSource} artifactId={form.rehoMaskId} masks={masks} disabled={formDisabled} onSource={(value) => updateForm((current) => ({ ...current, rehoMaskSource: value, rehoMaskId: "" }))} onArtifact={(value) => updateForm((current) => ({ ...current, rehoMaskId: value }))} />
          <label>ReHo 计算时平滑
            <select value={form.rehoSmoothing} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoSmoothing: event.target.value as FormState["rehoSmoothing"], rehoFwhm: ["", "", ""] }))}>
              <option value="">请选择</option><option value="false">不平滑 ReHo</option><option value="true">平滑 ReHo</option>
            </select>
          </label>
          {form.rehoSmoothing === "true" && <FwhmFields label="ReHo" values={form.rehoFwhm} disabled={formDisabled} onChange={(value) => updateForm((current) => ({ ...current, rehoFwhm: value }))} />}
          <label>ReHo 全局结果图平滑
            <select value={form.rehoGlobalSmoothing} disabled={formDisabled} onChange={(event) => updateForm((current) => ({ ...current, rehoGlobalSmoothing: event.target.value as FormState["rehoGlobalSmoothing"], rehoGlobalFwhm: ["", "", ""] }))}>
              <option value="">请选择</option><option value="false">不做全局结果平滑</option><option value="true">做全局结果平滑</option>
            </select>
          </label>
          {form.rehoGlobalSmoothing === "true" && <FwhmFields label="ReHo 全局结果图" values={form.rehoGlobalFwhm} disabled={formDisabled} onChange={(value) => updateForm((current) => ({ ...current, rehoGlobalFwhm: value }))} />}
        </fieldset>}
        <button type="button" className="button button-primary" disabled={!requestBody || dryRunLoading} onClick={() => void previewPlan()}>
          {dryRunLoading ? "正在进行只读预检…" : "只读预检指标方案"}
        </button>
        {dryRunError && <p className="metric-only-error" role="alert">{dryRunError}</p>}
        {dryRunResult && <div className="metric-only-result" role="status">
          <h4>{dryRunResult.ready ? "预检通过" : "仍有阻断项"}</h4>
          <p>方案哈希：{dryRunResult.plan_hash ?? "尚未生成"}</p>
          {dryRunResult.issues && dryRunResult.issues.length > 0 && <ul>{dryRunResult.issues.map((issue, index) => <li key={`${issue.code}:${issue.path}:${index}`}>{issue.message}</li>)}</ul>}
          <p>此结果只表示 Skill 编译预检状态；不会创建正式方案、审批、工作任务或运行。</p>
        </div>}
      </div>}
    </>}
  </section>;
}
