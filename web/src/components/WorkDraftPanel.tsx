import { useEffect, useId, useRef, useState } from "react";
import { describeError } from "../api/client";
import type { ConfirmationRequest } from "./confirmation";
import { inheritedFieldValue, reviewLabel, sourceLabel, type WorkDraftSnapshot } from "./workDraft";
import "./work-draft.css";

type ObjectValue = Record<string, unknown>;
type Field = { path: string; label: string; kind?: "number" | "boolean" | "array"; choices?: Array<[string | number, string]> };
const f = (path: string, label: string, kind?: Field["kind"], choices?: Field["choices"]): Field => ({ path, label, kind, choices });
const choice = (path: string, label: string, values: Array<string | [string | number, string]>): Field => f(path, label, undefined, values.map((v) => typeof v === "string" ? [v, v] : v));
const bool = (path: string, label: string) => f(path, label, "boolean");
const number = (path: string, label: string) => f(path, label, "number");
const array = (path: string, label: string) => f(path, label, "array");
const band = (path: string) => [number(`${path}.low_hz`, "频段下限（Hz）"), number(`${path}.high_hz`, "频段上限（Hz）")];
const motion = (path: string) => [choice(`${path}.fd_type`, "头动 FD 类型", [["fd_power", "Power"], ["fd_jenkinson", "Jenkinson"]]), number(`${path}.fd_threshold_mm`, "FD 阈值（毫米）"), number(`${path}.previous_points`, "前向剔除时间点"), number(`${path}.later_points`, "后向剔除时间点")];
const tissue = (path: string, label: string) => [bool(`${path}.enabled`, `${label}回归`), choice(`${path}.mask_source`, `${label}掩膜`, [["spm", "SPM"], ["segment", "T1 分割"]]), number(`${path}.mask_threshold`, `${label}掩膜阈值`), choice(`${path}.method`, `${label}方法`, [["mean", "均值"], ["compcor", "CompCor"]]), number(`${path}.compcor_components`, `${label} CompCor 成分数`)];
const p = "preprocessing";
const groups: Array<{ title: string; fields: Field[] }> = [
  { title: "数据与采集", fields: [f("study_protocol_ref", "研究方案引用（编译必填）"), array("primary_outputs", "主要输出"), number(`${p}.tr_seconds`, "重复时间 TR（秒）"), number(`${p}.expected_time_points`, "时间点数"), number(`${p}.dummy_scans`, "删除初始时间点")] },
  { title: "基础预处理", fields: [bool(`${p}.slice_timing.enabled`, "层时间校正"), number(`${p}.slice_timing.slice_count`, "层数"), array(`${p}.slice_timing.slice_order`, "层采集顺序"), number(`${p}.slice_timing.reference_slice`, "参考层"), bool(`${p}.realignment.enabled`, "头动校正"), choice(`${p}.realignment.options_source`, "头动校正选项来源", [["dpabi_v82_jobmat", "DPABI Jobmat"]])] },
  { title: "T1 处理", fields: [choice(`${p}.t1.preparation`, "T1 准备", [["reorient_crop", "重定向与裁剪"], ["prepared", "使用已准备 T1"]]), f(`${p}.t1.structural_artifact_id`, "T1 结构像输入绑定"), bool(`${p}.t1.coregister_to_functional`, "T1 与功能像配准"), choice(`${p}.t1.segmentation`, "T1 分割", [["disabled", "停用"], ["segment", "Segment"], ["new_segment", "New Segment"]]), choice(`${p}.t1.affine_regularization`, "T1 分割仿射正则化", [["mni", "MNI"], ["eastern", "东亚"]]), bool(`${p}.t1.dartel`, "DARTEL 构建模板"), f(`${p}.normalization.structural_artifact_id`, "标准化结构像产物绑定"), choice(`${p}.normalization.affine_regularization`, "标准化仿射正则化", [["mni", "MNI"], ["eastern", "东亚"]])] },
  { title: "协变量回归", fields: [bool(`${p}.nuisance.enabled`, "协变量回归"), choice(`${p}.nuisance.timing`, "回归阶段", [["after_realign", "头动校正后"], ["after_normalize", "标准化后"]]), number(`${p}.nuisance.polynomial_trend`, "多项式趋势"), choice(`${p}.nuisance.head_motion_model`, "头动回归模型", [[0, "无"], [1, "刚体 6 参数"], [2, "导数 12 参数"], [3, "6 参数及平方项"], [4, "Friston-24"]]), ...motion(`${p}.nuisance.head_motion_scrubbing`), ...tissue(`${p}.nuisance.white_matter`, "WM 白质"), ...tissue(`${p}.nuisance.csf`, "CSF 脑脊液"), bool(`${p}.nuisance.global_signal.enabled`, "全局信号回归"), choice(`${p}.nuisance.global_signal.mask_source`, "全局信号掩膜", [["spm", "SPM"], ["auto_mask", "AutoMask"]]), choice(`${p}.nuisance.global_signal.method`, "全局信号方法", [["mean", "均值"]]), bool(`${p}.nuisance.warp_masks_to_individual_space`, "将掩膜变换到个体空间"), bool(`${p}.nuisance.add_mean_back`, "加回均值")] },
  { title: "标准化", fields: [choice(`${p}.normalization.mode`, "标准化方法", [[0, "停用"], [1, "EPI 模板"], [2, "T1 Segment"], [3, "DARTEL"]]), choice(`${p}.normalization.timing`, "标准化阶段", [["on_functional_data", "功能像"], ["on_results", "指标结果"]]), array(`${p}.normalization.bounding_box_mm`, "空间边界（毫米）"), array(`${p}.normalization.voxel_size_mm`, "体素大小（毫米）")] },
  { title: "滤波、剔除与平滑", fields: [bool(`${p}.detrend`, "线性去趋势"), choice(`${p}.temporal_filter.timing`, "滤波阶段", [["disabled", "停用"], ["before_normalize", "标准化前"], ["after_normalize", "标准化后"]]), ...band(`${p}.temporal_filter.frequency_band`), bool(`${p}.temporal_filter.add_mean_back`, "滤波后加回均值"), bool(`${p}.scrubbing.enabled`, "时间点剔除"), choice(`${p}.scrubbing.timing`, "剔除阶段", [["after_preprocessing", "预处理后"]]), ...motion(`${p}.scrubbing.censoring`), choice(`${p}.scrubbing.method`, "剔除方法", [["cut", "删除时间点"], ["nearest", "最近邻插值"], ["linear", "线性插值"], ["spline", "样条插值"], ["pchip", "分段三次插值"]]), choice(`${p}.smoothing.timing`, "平滑阶段", [["disabled", "停用"], ["on_functional_data", "功能像"], ["on_results", "指标结果"]]), choice(`${p}.smoothing.method`, "平滑方法", [[1, "SPM"], [2, "DARTEL"]]), array(`${p}.smoothing.fwhm_mm`, "平滑核（毫米）")] },
  { title: "ALFF / fALFF", fields: [number("alff_falff.tr_seconds", "指标 重复时间 TR（秒）"), ...band("alff_falff.frequency_band"), array("alff_falff.requested_scalings", "输出标准化方式"), choice("alff_falff.mask_source", "指标掩膜", [["registered_artifact", "已登记掩膜"], ["dpabi_auto", "DPABI AutoMask"]]), f("alff_falff.mask_artifact_id", "掩膜产物编号"), choice("alff_falff.filter_timing", "指标滤波阶段", [["disabled", "不另行滤波"], ["before_normalize", "标准化前"], ["after_normalize", "标准化后"]]), bool("alff_falff.result_smoothing", "结果图平滑"), array("alff_falff.result_smoothing_fwhm_mm", "结果图 平滑核（毫米）")] },
  { title: "ReHo", fields: [number("reho.tr_seconds", "ReHo 重复时间 TR（秒）"), ...band("reho.temporal_filter_band"), bool("reho.temporal_filter_add_mean_back", "ReHo 滤波后加回均值"), choice("reho.cluster_voxels", "邻域体素数", [[7, "7"], [19, "19"], [27, "27"]]), array("reho.requested_scalings", "ReHo 输出标准化方式"), choice("reho.mask_source", "ReHo 掩膜", [["registered_artifact", "已登记掩膜"], ["dpabi_auto", "DPABI AutoMask"]]), f("reho.mask_artifact_id", "ReHo 掩膜产物编号"), bool("reho.smooth_reho", "ReHo 结果图平滑"), array("reho.smooth_reho_fwhm_mm", "ReHo 平滑核（毫米）"), bool("reho.global_result_smoothing", "ReHo 全局结果平滑"), array("reho.global_result_smoothing_fwhm_mm", "ReHo 全局 平滑核（毫米）")] },
  { title: "质量控制 QC", fields: [choice("qc_mode", "质量控制", [["head_motion", "头动质量控制"], ["skip", "明确跳过 QC"]])] },
];

const keyFields = new Set([
  "study_protocol_ref", "preprocessing.tr_seconds", "preprocessing.expected_time_points", "preprocessing.dummy_scans",
  "preprocessing.slice_timing.enabled", "preprocessing.slice_timing.slice_count", "preprocessing.slice_timing.slice_order", "preprocessing.slice_timing.reference_slice", "preprocessing.realignment.enabled",
  "preprocessing.t1.preparation", "preprocessing.t1.segmentation", "preprocessing.t1.dartel",
  "preprocessing.nuisance.enabled", "preprocessing.nuisance.timing", "preprocessing.nuisance.head_motion_model", "preprocessing.nuisance.polynomial_trend", "preprocessing.nuisance.white_matter.enabled", "preprocessing.nuisance.csf.enabled", "preprocessing.nuisance.global_signal.enabled",
  "preprocessing.normalization.mode", "preprocessing.normalization.voxel_size_mm", "preprocessing.detrend",
  "alff_falff.tr_seconds", "alff_falff.frequency_band.low_hz", "alff_falff.frequency_band.high_hz", "alff_falff.filter_timing", "alff_falff.result_smoothing", "alff_falff.result_smoothing_fwhm_mm",
  "reho.cluster_voxels", "reho.smooth_reho", "reho.smooth_reho_fwhm_mm", "qc_mode",
]);

const statusSummaryFields = [
  { path: "study_protocol_ref", label: "研究方案引用" },
  { path: "preprocessing.tr_seconds", label: "TR" },
  { path: "preprocessing.expected_time_points", label: "时间点数" },
  { path: "preprocessing.dummy_scans", label: "删除初始时间点" },
  { path: "preprocessing.slice_timing", label: "层时间校正" },
  { path: "preprocessing.nuisance", label: "协变量回归" },
  { path: "alff_falff.filter_timing", label: "ALFF/fALFF 指标滤波" },
  { path: "alff_falff.result_smoothing", label: "ALFF/fALFF 结果图平滑" },
  { path: "alff_falff.result_smoothing_fwhm_mm", label: "结果图平滑核（毫米）" },
];

function get(value: ObjectValue, path: string): unknown {
  return path.split(".").reduce<unknown>((current, key) => current && typeof current === "object" ? (current as ObjectValue)[key] : undefined, value);
}
function put(value: ObjectValue, path: string, next: unknown): void {
  const keys = path.split("."); let current = value;
  for (const key of keys.slice(0, -1)) { if (!current[key] || typeof current[key] !== "object") current[key] = {}; current = current[key] as ObjectValue; }
  current[keys.at(-1)!] = next;
}
function changedValues(base: ObjectValue, next: ObjectValue): ObjectValue {
  const patch: ObjectValue = {};
  for (const [key, value] of Object.entries(next)) {
    if (key === "confirmed_hash" || JSON.stringify(value) === JSON.stringify(base[key])) continue;
    const previous = base[key];
    patch[key] = value && previous && typeof value === "object" && typeof previous === "object" && !Array.isArray(value) && !Array.isArray(previous)
      ? changedValues(previous as ObjectValue, value as ObjectValue) : value;
  }
  return patch;
}
function issueState(issues: string[]): string | undefined {
  if (!issues.length) return undefined;
  return issues.every((issue) => /尚未提供|尚未填写|尚未选择|Field required|missing|required/i.test(issue))
    ? "待补充"
    : "存在冲突";
}
function disabledReason(draft: ObjectValue, path: string): string | undefined {
  if (path.startsWith("alff_falff.") && !draft.alff_falff) return "未选择 ALFF/fALFF";
  if (path.startsWith("reho.") && !draft.reho) return "未选择 ReHo";
  if (path.startsWith("preprocessing.t1.") && get(draft, "preprocessing.t1") === null) return "T1 处理已停用";
  for (const prefix of ["preprocessing.slice_timing", "preprocessing.realignment", "preprocessing.nuisance", "preprocessing.scrubbing", "preprocessing.nuisance.white_matter", "preprocessing.nuisance.csf", "preprocessing.nuisance.global_signal"]) {
    if (path.startsWith(`${prefix}.`) && path !== `${prefix}.enabled` && get(draft, `${prefix}.enabled`) === false) return "此步骤已停用";
  }
  for (const prefix of ["preprocessing.temporal_filter", "preprocessing.smoothing"]) if (path.startsWith(`${prefix}.`) && path !== `${prefix}.timing` && get(draft, `${prefix}.timing`) === "disabled") return "此步骤已停用";
  if (path.startsWith("preprocessing.normalization.") && !["mode", "structural_artifact_id", "affine_regularization"].includes(path.split(".").at(-1)!) && get(draft, "preprocessing.normalization.mode") === 0) return "标准化已停用";
  return undefined;
}

function PlanPreview({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const preview = value as ObjectValue;
  const steps = Array.isArray(preview.common_steps) ? preview.common_steps as ObjectValue[] : [];
  const branches = Array.isArray(preview.branches) ? preview.branches as ObjectValue[] : [];
  const constraints = Array.isArray(preview.shared_constraints) ? preview.shared_constraints.filter((s): s is string => typeof s === "string") : [];
  const stepLabels: Record<string, string> = { calculate_alff_falff: "计算所选 ALFF / fALFF 指标", smooth_alff_falff_result: "指标结果图平滑", register_alff_falff: "登记指标图", prepare_reho_temporal_filter_checkpoint: "准备 ReHo 的滤波检查点", calculate_reho: "计算 ReHo", smooth_reho_result: "ReHo 结果图平滑", register_reho: "登记 ReHo 指标图" };
  const outputLabels: Record<string, string> = { raw_alff: "原始 ALFF", normalized_alff: "均值归一化 ALFF", z_alff: "z 标准化 ALFF", raw_falff: "原始 fALFF", raw_reho: "原始 ReHo", preprocessed_bold: "预处理功能时序", "ALFF 原始值": "原始 ALFF", "ALFF 全脑均值归一化": "均值归一化 ALFF", "ALFF z 标准化": "z 标准化 ALFF" };
  const outputs = Array.isArray(preview.outputs) ? preview.outputs.filter((item): item is string => typeof item === "string") : [];
  return <details open><summary>执行流程与输出（仅供审查）</summary><p>{preview.status === "ready_for_review" ? "参数完整，等待确认" : "方案待补全"}</p><ol>{steps.filter((s) => s && typeof s.label === "string").map((step, index) => <li key={index}>{String(step.label)}{typeof step.warning === "string" && <small> · {step.warning}</small>}</li>)}</ol>{branches.filter((b) => b && typeof b.label === "string").map((branch, index) => <div key={index}><strong>{String(branch.label)}</strong>{typeof branch.consumes === "string" && <p>使用：{branch.consumes.replace("verified", "已核验")}</p>}<ol>{Array.isArray(branch.steps) && branch.steps.map((step, item) => <li key={item}>{step === "calculate_alff_falff" && branch.label === "ALFF 分支" ? "计算 ALFF" : stepLabels[String(step)] ?? "指标处理"}</li>)}</ol>{typeof branch.warning === "string" && <p>{branch.warning}</p>}</div>)}{outputs.length > 0 && <p>预期输出：{outputs.map((output) => outputLabels[output] ?? output).join("、")}</p>}{typeof preview.qc_mode === "string" && <p>质量控制：{preview.qc_mode === "head_motion" ? "计算后生成头动 QC，等待人工审查" : "本次明确跳过头动 QC"}</p>}<details><summary>处理约束与技术记录</summary><ul>{constraints.map((constraint, index) => <li key={index}>{constraint}</li>)}</ul><pre>{JSON.stringify(value, null, 2)}</pre></details></details>;
}

type Props = { snapshot: WorkDraftSnapshot; issues?: string[]; preview?: unknown; readOnly?: boolean; busy?: boolean; confirmation?: ConfirmationRequest | null; confirmLabel?: string; onSave: (baseHash: string, patch: ObjectValue) => Promise<void>; onConfirm?: () => void; onDirty?: (dirty: boolean) => void };
export function WorkDraftPanel({ snapshot, issues: rawIssues = [], preview, readOnly = false, busy = false, confirmation, confirmLabel = "确认并生成方案", onSave, onConfirm, onDirty }: Props) {
  const issues = rawIssues.map((issue) => issue.replace(/^研究方案引用（study_protocol_ref）[：:]/, "study_protocol_ref："));
  const panelId = useId();
  const [draft, setDraft] = useState(snapshot.draft);
  const [edited, setEdited] = useState<string[]>([]);
  const [arrayText, setArrayText] = useState<Record<string, string>>({});
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState("");
  const saveErrorRef = useRef<HTMLParagraphElement>(null);
  const [saving, setSaving] = useState(false);
  const [advanced, setAdvanced] = useState("");
  const actionLocked = readOnly || snapshot.consumed || busy || saving;
  const inputNeedsReview = snapshot.input_evidence_stale || snapshot.input_manifest_stale;
  const locked = actionLocked || inputNeedsReview;
  const inputRecovery = snapshot.input_manifest_stale && confirmation?.options.length && confirmation.options.every((option) => option.command.startsWith("确认起始阶段 "));
  const dirty = edited.length > 0;
  useEffect(() => {
    if (saveError) saveErrorRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [saveError]);
  function locate(path: string) {
    const element = document.getElementById(`draft-${panelId}-${path}`);
    if (!element) return;
    let parent = element.parentElement;
    while (parent) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement; }
    element.focus(); element.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }
  function humanIssue(issue: string): { path: string | undefined; text: string } {
    if (issue.startsWith("study_protocol_ref：") || issue.startsWith("study_protocol_ref:")) return { path: "study_protocol_ref", text: "研究方案引用是编译必填项。填写真实方案编号、文档引用或 DOI；未知时不要编造。" };
    const field = groups.flatMap((group) => group.fields).find((field) => issue.startsWith(`${field.path}：`) || issue.startsWith(`${field.path}:`));
    let text = field ? `${field.label}：${issue.slice(field.path.length + 1)}` : issue;
    text = text.replace(/（[a-z_][a-z_0-9.]*）/g, "").replace(/Field required/g, "尚未填写，请补充此选择").replace(/尚未提供（停用项也需明确填写 null 或 false）/g, "尚未确定，请选择启用、停用或不适用");
    text = text.replace("preprocessing.nuisance.head_motion_scrubbing", "回归中的头动剔除");
    return { path: field?.path, text };
  }
  function display(path: string, value: unknown): string {
    if (value === undefined) return "未填写";
    if (value === null) return "不适用";
    if (typeof value === "boolean") return value ? "启用" : "停用";
    const field = groups.flatMap((group) => group.fields).find((field) => field.path === path);
    return field?.choices?.find(([key]) => key === value)?.[1] ?? (Array.isArray(value) ? value.map((item) => Array.isArray(item) ? item.join("、") : String(item)).join("、") : String(value));
  }
  function summaryValue(path: string): string {
    if (path.startsWith("alff_falff.") && !draft.alff_falff) return "未选择 ALFF/fALFF 指标";
    const value = get(draft, path);
    if (value === undefined) return "未填写";
    if (value === null) return "不适用";
    if (path === "preprocessing.slice_timing") {
      if (typeof value !== "object" || Array.isArray(value)) return "待审查";
      const timing = value as ObjectValue;
      if (timing.enabled === false) return "明确停用";
      return [
        timing.enabled === true ? "已启用" : "启用状态未定",
        timing.slice_count ? String(timing.slice_count) + " 层" : "层数未定",
        timing.reference_slice ? "参考层 " + String(timing.reference_slice) : "参考层未定",
        Array.isArray(timing.slice_order) ? "采集顺序 " + timing.slice_order.length + " 项" : "采集顺序未定",
      ].join(" · ");
    }
    if (path === "preprocessing.nuisance") {
      if (typeof value !== "object" || Array.isArray(value)) return "待审查";
      const nuisance = value as ObjectValue;
      if (nuisance.enabled === false) return "明确停用";
      const model = nuisance.head_motion_model;
      const modelLabel = groups.flatMap((group) => group.fields).find((field) => field.path === "preprocessing.nuisance.head_motion_model")?.choices?.find(([key]) => key === model)?.[1];
      const wm = get(draft, "preprocessing.nuisance.white_matter.enabled");
      const csf = get(draft, "preprocessing.nuisance.csf.enabled");
      return [
        nuisance.enabled === true ? "已启用" : "启用状态未定",
        modelLabel ? "头动 " + modelLabel : "头动模型未定",
        "WM " + (wm === true ? "启用" : wm === false ? "停用" : "未定"),
        "CSF " + (csf === true ? "启用" : csf === false ? "停用" : "未定"),
      ].join(" · ");
    }
    return display(path, value);
  }
  function summaryState(path: string): string {
    if (path.startsWith("alff_falff.") && !draft.alff_falff) return "不适用";
    if (edited.some((item) => item === path || item.startsWith(path + ".") || path.startsWith(item + "."))) return "未保存";
    const matchingIssues = issues.filter((item) => item.startsWith(path + "：") || item.startsWith(path + ":") || item.startsWith(path + "."));
    const validationState = issueState(matchingIssues);
    if (validationState) return validationState;
    if (get(draft, path) === undefined || get(draft, path) === null) return "待选择";
    const direct = inheritedFieldValue(snapshot.field_states, path);
    if (direct) return reviewLabel(direct);
    const descendants = Object.entries(snapshot.field_states ?? {}).filter(([key]) => key.startsWith(path + ".")).map(([, value]) => reviewLabel(value));
    if (descendants.includes("存在冲突")) return "存在冲突";
    if (descendants.length && descendants.every((state) => state === "已核实")) return "已核实";
    if (descendants.includes("待选择")) return "待选择";
    return "待审查";
  }
  function summarySource(path: string): string {
    if (edited.some((item) => item === path || path.startsWith(item + "."))) return "用户指定";
    if (edited.some((item) => item.startsWith(path + "."))) return "用户修改（部分参数）";
    const direct = inheritedFieldValue(snapshot.sources, path);
    if (direct) return sourceLabel(direct);
    const descendants = Object.entries(snapshot.sources ?? {}).filter(([key]) => key.startsWith(path + ".")).map(([, value]) => sourceLabel(value));
    return [...new Set(descendants)].join("、") || sourceLabel(undefined);
  }
  function change(path: string, value: unknown) {
    const next = structuredClone(draft); put(next, path, value);
    if (path === "preprocessing.normalization.mode" && (value === 2 || value === 3) && !get(next, "preprocessing.normalization.structural_artifact_id")) put(next, "preprocessing.normalization.structural_artifact_id", "workspace:anatomical-source");
    // Disabled choices explicitly clear their inactive parameters, never populate study defaults.
    const parts = path.split("."); const leaf = parts.pop(); const parent = parts.join(".");
    if ((leaf === "enabled" && value === false) || (leaf === "timing" && value === "disabled") || (leaf === "mode" && value === 0)) {
      const object = get(next, parent) as ObjectValue;
      for (const field of groups.flatMap((g) => g.fields).filter((field) => field.path.startsWith(`${parent}.`) && field.path !== path)) object[field.path.slice(parent.length + 1).split(".")[0]] = null;
    }
    if (path === "requested_metrics" && Array.isArray(value)) {
      const metrics = value as string[];
      if (metrics.some((m) => m === "alff" || m === "falff")) {
        next.alff_falff = { ...(next.alff_falff as ObjectValue ?? {}), requested_metrics: metrics.filter((m) => m === "alff" || m === "falff") };
      } else next.alff_falff = null;
      if (metrics.includes("reho")) next.reho ??= {}; else next.reho = null;
    }
    setDraft(next); setEdited((items) => [...new Set([...items, path])]); onDirty?.(true); setSaveError("");
  }
  async function save() {
    setSaving(true); setSaveError("");
    try {
      const patch = changedValues(snapshot.draft, draft);
      await onSave(snapshot.hash, patch); setEdited([]); onDirty?.(false);
    } catch (error) {
      const message = describeError(error).replace(/[。.!！?？\s]+$/u, "");
      setSaveError(`${message}。本地修改仍保留，请核对草稿版本后重试。`);
    }
    finally { setSaving(false); }
  }
  const renderField = (field: Field) => {
      const value = get(draft, field.path); const reason = disabledReason(draft, field.path);
      const fieldIssues = issues.filter((issue) => issue.startsWith(`${field.path}：`) || issue.startsWith(`${field.path}:`));
      const source = edited.includes(field.path) ? "用户指定" : sourceLabel(inheritedFieldValue(snapshot.sources, field.path));
      const state = edited.includes(field.path) ? "未保存" : issueState(fieldIssues) ?? (value === undefined ? "待选择" : value === null ? "不适用" : reviewLabel(inheritedFieldValue(snapshot.field_states, field.path)));
      const controlDisabled = locked || Boolean(reason);
      const id = `draft-${panelId}-${field.path}`;
      return <div className="draft-field" key={field.path}><label htmlFor={id}>{field.label}</label><small className="draft-field-meta"><span>来源：{source}</span><span>状态：{reason ?? state}</span></small>
        {field.kind === "boolean" || field.choices ? <select id={id} aria-invalid={fieldIssues.length > 0 || Boolean(inputErrors[field.path])} aria-describedby={`${id}-status`} disabled={controlDisabled} value={value === undefined ? "__unknown" : value === null ? "__null" : String(value)} onChange={(event) => {
          const raw = event.target.value;
          const selected = field.kind === "boolean" ? raw === "true" : field.choices?.find(([key]) => String(key) === raw)?.[0];
          change(field.path, raw === "__null" ? null : selected);
        }}><option value="__unknown" disabled>未填写</option><option value="__null">不适用 / 清空</option>{field.kind === "boolean" ? <><option value="true">启用 / 是</option><option value="false">停用 / 否</option></> : field.choices?.map(([key, label]) => <option key={String(key)} value={String(key)} disabled={field.path === "preprocessing.smoothing.method" && key === 2 && get(draft, "preprocessing.normalization.mode") !== 3}>{label}</option>)}</select>
        : <input id={id} aria-invalid={fieldIssues.length > 0 || Boolean(inputErrors[field.path])} aria-describedby={`${id}-status`} disabled={controlDisabled} type={field.kind === "number" ? "number" : "text"} step="any" placeholder={field.kind === "array" ? "用逗号分隔，例如 3, 3, 3" : "未填写"} value={arrayText[field.path] ?? (value === undefined || value === null ? "" : field.kind === "array" ? Array.isArray(value) && !value.some(Array.isArray) ? value.join(", ") : JSON.stringify(value) : String(value))} onChange={(event) => {
          const raw = event.target.value;
          if (field.kind === "array") {
            setArrayText((items) => ({ ...items, [field.path]: raw }));
            try { const parsed: unknown = raw.trim() ? raw.trim().startsWith("[") ? JSON.parse(raw) : raw.split(/[,，]/).map((item) => { const text = item.trim(); if (!text) throw new Error(); return Number.isFinite(Number(text)) ? Number(text) : text; }) : null; if (parsed !== null && !Array.isArray(parsed)) throw new Error(); change(field.path, parsed); setInputErrors((items) => ({ ...items, [field.path]: "" })); }
            catch { setInputErrors((items) => ({ ...items, [field.path]: "请填写完整的列表，使用逗号分隔" })); setEdited((items) => [...new Set([...items, field.path])]); onDirty?.(true); }
          } else change(field.path, raw === "" ? null : field.kind === "number" ? Number(raw) : raw);
        }} />}
        <small id={`${id}-status`} role={fieldIssues.length > 0 || inputErrors[field.path] ? "alert" : undefined} className="draft-error">{[...fieldIssues.map((issue) => humanIssue(issue).text), inputErrors[field.path]].filter(Boolean).join("；")}</small>
      </div>;
    };
  return <section className="work-draft-panel" aria-label={readOnly ? "历史预处理方案（只读）" : "预处理方案参数"}>
    <h3>预处理方案 {snapshot.version !== undefined ? `· 第 ${snapshot.version} 版` : ""} {readOnly ? "· 历史只读" : snapshot.consumed ? "· 本次执行方案（只读）" : snapshot.compiled ? "· 已生成方案" : "· 待审查"}</h3>
    <p>工作区：{snapshot.workspace_path ?? "当前会话工作区"}</p>
    <p role="status">{snapshot.consumed ? "已用于本次计算，科学参数已冻结" : busy || saving ? "正在检查方案，请稍候" : inputNeedsReview ? "输入已变化，参数选择已保留；请重新确认输入并读取采集信息" : dirty ? "修改尚未保存，保存后重新校验" : issues.length ? `需要处理 ${issues.length} 项问题` : confirmation ? "方案可审查，请核对关键科学选择" : "等待补充与校验"}</p>
    {issues.length > 0 && <nav aria-label="方案问题"><strong>需要处理（{issues.length}）</strong><ul>{issues.map((issue, index) => { const item = humanIssue(issue); return <li key={index}>{item.path ? <button type="button" className="draft-issue-link" onClick={() => locate(item.path!)}>{item.text}</button> : item.text}</li>; })}</ul></nav>}
    {dirty && <aside className="draft-changes" aria-label="本次修改"><strong>本次修改 · 保存后重新校验</strong><ul>{edited.map((path) => <li key={path}>{groups.flatMap((group) => group.fields).find((field) => field.path === path)?.label ?? (path === "requested_metrics" ? "计算指标" : "高级参数")}：{display(path, get(snapshot.draft, path))} → {display(path, get(draft, path))}</li>)}</ul></aside>}
    {!dirty && snapshot.changes && snapshot.changes.length > 0 && <details className="draft-changes"><summary>此版本的修改（{snapshot.changes.length} 项）</summary><ul>{snapshot.changes.map((change) => <li key={change.path}>{groups.flatMap((group) => group.fields).find((field) => field.path === change.path)?.label ?? (change.path === "requested_metrics" ? "计算指标" : "高级参数")}：{display(change.path, change.before)} → {display(change.path, change.after)}</li>)}</ul></details>}
    <p>{snapshot.consumed ? "本次执行方案只读。如需修改分析选择，请在对话中准备新方案。" : "未填写项保持未知；建议需审阅。保存参数和生成方案不会启动计算。"}</p>
    <section className="draft-status-summary" aria-label="关键参数状态">
      <h4>关键参数状态 <small>来源与核验状态分别显示 · 启动仍需独立确认</small></h4>
      <ul>{statusSummaryFields.map((field) => <li key={field.path}><strong>{field.label}</strong><span>{summaryValue(field.path)}</span><small>来源：{summarySource(field.path)} · 状态：{summaryState(field.path)}</small></li>)}</ul>
    </section>
    <div className="draft-metrics" role="group" aria-label="计算指标">{["alff", "falff", "reho"].map((metric) => <label key={metric}><input type="checkbox" disabled={locked} checked={Array.isArray(draft.requested_metrics) && draft.requested_metrics.includes(metric)} onChange={(event) => {
      const current = Array.isArray(draft.requested_metrics) ? draft.requested_metrics : [];
      change("requested_metrics", event.target.checked ? [...current, metric] : current.filter((m) => m !== metric));
    }} />{metric.toUpperCase()}</label>)}</div>
    <div className="draft-metrics">{[["preprocessing.t1", "T1 处理"], ["preprocessing.nuisance.head_motion_scrubbing", "头动回归时间点剔除"], ["reho.temporal_filter_band", "ReHo 分支滤波"]].map(([path, label]) => <label key={path}>{label}<select aria-label={`${label}配置状态`} disabled={locked || (path.startsWith("reho") && !draft.reho)} value={get(draft, path) === undefined ? "unknown" : get(draft, path) === null ? "off" : "on"} onChange={(event) => change(path, event.target.value === "off" ? null : path === "preprocessing.t1" ? { structural_artifact_id: "workspace:anatomical-source" } : {})}><option value="unknown" disabled>未填写</option><option value="off">停用 / 不适用</option><option value="on">配置参数</option></select></label>)}</div>
    {groups.map((group) => {
      const main = group.fields.filter((field) => keyFields.has(field.path) && !disabledReason(draft, field.path));
      const advancedFields = group.fields.filter((field) => !main.includes(field));
      const inactive = group.title === "ALFF / fALFF" && !draft.alff_falff ? "未选择该指标" : group.title === "ReHo" && !draft.reho ? "未选择该指标" : group.title === "T1 处理" && get(draft, "preprocessing.t1") === null ? "已停用" : undefined;
      return <details key={group.title} open={!readOnly && main.length > 0}><summary>{group.title}{inactive ? ` · ${inactive}` : ""}</summary><div className="draft-fields">{main.map(renderField)}</div>{advancedFields.length > 0 && <details><summary>更多{group.title}参数</summary><div className="draft-fields">{advancedFields.map(renderField)}</div></details>}</details>;
    })}
    <details><summary>技术详情与高级配置</summary><p>方案标识：{snapshot.hash}。尚未通过校验的值不会执行。</p><pre>{JSON.stringify(draft, null, 2)}</pre>{!readOnly && <><label htmlFor={`advanced-${panelId}`}>完整参数 JSON（替换本地编辑）</label><textarea id={`advanced-${panelId}`} disabled={locked} value={advanced} onChange={(event) => setAdvanced(event.target.value)} placeholder="粘贴完整草稿 JSON；不含 confirmed_hash 或 provenance" /><button type="button" className="button button-secondary" disabled={locked || !advanced.trim()} onClick={() => {
      try { const parsed: unknown = JSON.parse(advanced); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("需要 JSON 对象"); const object = parsed as ObjectValue; if ("confirmed_hash" in object || JSON.stringify(object).includes('"provenance"')) throw new Error("不能编辑服务端确认与来源字段"); setDraft(object); setEdited(["advanced"]); setArrayText({}); setInputErrors({}); onDirty?.(true); setSaveError(""); }
      catch (error) { setSaveError(describeError(error)); }
    }}>应用到面板</button></>}</details>
    {preview !== undefined && <PlanPreview value={preview} />}
    <p className="draft-help">暂不支持：DICOM/BIDS 转换、外部 DARTEL 模板复用、FC、度中心性。T1 准备、配准、分割及 DARTEL 在获批任务中自动衔接；启动计算需独立授权。</p>
    {saveError && <p ref={saveErrorRef} role="alert" className="draft-error">{saveError}</p>}
    {!readOnly && <div className="draft-actions"><span>{dirty ? "有未保存修改" : "当前参数已保存"}</span><button type="button" className="button button-secondary" disabled={locked || !dirty} onClick={() => { setDraft(snapshot.draft); setEdited([]); setArrayText({}); setInputErrors({}); setSaveError(""); onDirty?.(false); }}>放弃本地修改</button><button type="button" className="button button-secondary" disabled={locked || !dirty || Object.values(inputErrors).some(Boolean)} onClick={() => void save()}>{saving ? "保存中…" : "保存修改"}</button><button type="button" className="button button-primary" disabled={actionLocked || dirty || Object.values(inputErrors).some(Boolean) || (issues.length > 0 && !inputRecovery) || (inputNeedsReview && !inputRecovery) || !confirmation || snapshot.consumed} onClick={onConfirm}>{confirmLabel}</button></div>}
  </section>;
}
