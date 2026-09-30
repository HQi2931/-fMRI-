import { useId, useState } from "react";
import { describeError } from "../api/client";
import type { ConfirmationRequest } from "./confirmation";
import type { WorkDraftSnapshot } from "./workDraft";
import "./work-draft.css";

type ObjectValue = Record<string, unknown>;
type Field = { path: string; label: string; kind?: "number" | "boolean" | "array"; choices?: Array<[string | number, string]> };
const f = (path: string, label: string, kind?: Field["kind"], choices?: Field["choices"]): Field => ({ path, label, kind, choices });
const choice = (path: string, label: string, values: Array<string | [string | number, string]>): Field => f(path, label, undefined, values.map((v) => typeof v === "string" ? [v, v] : v));
const bool = (path: string, label: string) => f(path, label, "boolean");
const number = (path: string, label: string) => f(path, label, "number");
const array = (path: string, label: string) => f(path, label, "array");
const band = (path: string) => [number(`${path}.low_hz`, "频段下限 (Hz)"), number(`${path}.high_hz`, "频段上限 (Hz)")];
const motion = (path: string) => [choice(`${path}.fd_type`, "头动 FD 类型", [["fd_power", "Power"], ["fd_jenkinson", "Jenkinson"]]), number(`${path}.fd_threshold_mm`, "FD 阈值 (mm)"), number(`${path}.previous_points`, "前向剔除时间点"), number(`${path}.later_points`, "后向剔除时间点")];
const tissue = (path: string, label: string) => [bool(`${path}.enabled`, `${label}回归`), choice(`${path}.mask_source`, `${label}掩膜`, [["spm", "SPM"], ["segment", "T1 分割"]]), number(`${path}.mask_threshold`, `${label}掩膜阈值`), choice(`${path}.method`, `${label}方法`, [["mean", "均值"], ["compcor", "CompCor"]]), number(`${path}.compcor_components`, `${label} CompCor 成分数`)];
const p = "preprocessing";
const groups: Array<{ title: string; fields: Field[] }> = [
  { title: "数据与采集", fields: [f("study_protocol_ref", "研究方案引用"), array("primary_outputs", "主要输出"), number(`${p}.tr_seconds`, "TR (s)"), number(`${p}.expected_time_points`, "Time Points 时间点数"), number(`${p}.dummy_scans`, "Remove First 删除初始时间点")] },
  { title: "基础预处理", fields: [bool(`${p}.slice_timing.enabled`, "Slice Timing 层时间校正"), number(`${p}.slice_timing.slice_count`, "Slice Number 层数"), array(`${p}.slice_timing.slice_order`, "Slice Order 采集顺序"), number(`${p}.slice_timing.reference_slice`, "Reference Slice 参考层"), bool(`${p}.realignment.enabled`, "Realign 头动校正"), choice(`${p}.realignment.options_source`, "头动校正选项来源", [["dpabi_v82_jobmat", "DPABI Jobmat"]])] },
  { title: "T1 处理", fields: [choice(`${p}.t1.preparation`, "T1 准备", [["reorient_crop", "Reorient / Crop 重定向与裁剪"], ["prepared", "使用已准备 T1"]]), f(`${p}.t1.structural_artifact_id`, "T1 结构像输入绑定"), bool(`${p}.t1.coregister_to_functional`, "T1 Coreg to Fun 配准"), choice(`${p}.t1.segmentation`, "T1 分割", [["disabled", "停用"], ["segment", "Segment"], ["new_segment", "New Segment"]]), choice(`${p}.t1.affine_regularization`, "T1 分割仿射正则化", [["mni", "European / MNI"], ["eastern", "East Asian"]]), bool(`${p}.t1.dartel`, "DARTEL 构建模板"), f(`${p}.normalization.structural_artifact_id`, "标准化结构像产物绑定"), choice(`${p}.normalization.affine_regularization`, "标准化仿射正则化", [["mni", "European / MNI"], ["eastern", "East Asian"]])] },
  { title: "协变量回归", fields: [bool(`${p}.nuisance.enabled`, "Nuisance Covariates Regression"), choice(`${p}.nuisance.timing`, "回归阶段", [["after_realign", "Realign 后"], ["after_normalize", "Normalize 后"]]), number(`${p}.nuisance.polynomial_trend`, "Polynomial Trend 多项式趋势"), choice(`${p}.nuisance.head_motion_model`, "Head Motion Model", [[0, "无"], [1, "Rigid-body 6"], [2, "Derivative 12"], [3, "6 + Squares"], [4, "Friston 24"]]), ...motion(`${p}.nuisance.head_motion_scrubbing`), ...tissue(`${p}.nuisance.white_matter`, "WM 白质"), ...tissue(`${p}.nuisance.csf`, "CSF 脑脊液"), bool(`${p}.nuisance.global_signal.enabled`, "Global Signal 全局信号"), choice(`${p}.nuisance.global_signal.mask_source`, "全局信号掩膜", [["spm", "SPM"], ["auto_mask", "AutoMask"]]), choice(`${p}.nuisance.global_signal.method`, "全局信号方法", [["mean", "均值"]]), bool(`${p}.nuisance.warp_masks_to_individual_space`, "Warp Masks to Individual Space"), bool(`${p}.nuisance.add_mean_back`, "Add Mean Back 加回均值")] },
  { title: "标准化", fields: [choice(`${p}.normalization.mode`, "Normalize 标准化方法", [[0, "停用"], [1, "EPI Template"], [2, "T1 Unified Segmentation"], [3, "DARTEL"]]), choice(`${p}.normalization.timing`, "标准化阶段", [["on_functional_data", "功能像"], ["on_results", "指标结果"]]), array(`${p}.normalization.bounding_box_mm`, "Bounding Box (mm)"), array(`${p}.normalization.voxel_size_mm`, "Voxel Size (mm)")] },
  { title: "滤波、剔除与平滑", fields: [bool(`${p}.detrend`, "Detrend 去趋势"), choice(`${p}.temporal_filter.timing`, "Filter 滤波阶段", [["disabled", "停用"], ["before_normalize", "标准化前"], ["after_normalize", "标准化后"]]), ...band(`${p}.temporal_filter.frequency_band`), bool(`${p}.temporal_filter.add_mean_back`, "滤波后加回均值"), bool(`${p}.scrubbing.enabled`, "Scrubbing 时间点剔除"), choice(`${p}.scrubbing.timing`, "剔除阶段", [["after_preprocessing", "预处理后"]]), ...motion(`${p}.scrubbing.censoring`), choice(`${p}.scrubbing.method`, "剔除方法", ["cut", "nearest", "linear", "spline", "pchip"]), choice(`${p}.smoothing.timing`, "Smooth 平滑阶段", [["disabled", "停用"], ["on_functional_data", "功能像"], ["on_results", "指标结果"]]), choice(`${p}.smoothing.method`, "平滑方法", [[1, "SPM"], [2, "DARTEL"]]), array(`${p}.smoothing.fwhm_mm`, "FWHM (mm)")] },
  { title: "ALFF / fALFF", fields: [number("alff_falff.tr_seconds", "指标 TR (s)"), ...band("alff_falff.frequency_band"), array("alff_falff.requested_scalings", "输出标准化方式"), choice("alff_falff.mask_source", "指标掩膜", [["registered_artifact", "已登记掩膜"], ["dpabi_auto", "DPABI AutoMask"]]), f("alff_falff.mask_artifact_id", "掩膜产物编号"), choice("alff_falff.filter_timing", "指标滤波阶段", ["disabled", "before_normalize", "after_normalize"]), bool("alff_falff.result_smoothing", "Smooth Derivatives 结果图平滑"), array("alff_falff.result_smoothing_fwhm_mm", "结果图 FWHM (mm)")] },
  { title: "ReHo", fields: [number("reho.tr_seconds", "ReHo TR (s)"), ...band("reho.temporal_filter_band"), bool("reho.temporal_filter_add_mean_back", "ReHo 滤波后加回均值"), choice("reho.cluster_voxels", "Cluster 邻域体素数", [[7, "7"], [19, "19"], [27, "27"]]), array("reho.requested_scalings", "ReHo 输出标准化方式"), choice("reho.mask_source", "ReHo 掩膜", [["registered_artifact", "已登记掩膜"], ["dpabi_auto", "DPABI AutoMask"]]), f("reho.mask_artifact_id", "ReHo 掩膜产物编号"), bool("reho.smooth_reho", "Smooth ReHo"), array("reho.smooth_reho_fwhm_mm", "ReHo FWHM (mm)"), bool("reho.global_result_smoothing", "ReHo 全局结果平滑"), array("reho.global_result_smoothing_fwhm_mm", "ReHo 全局 FWHM (mm)")] },
  { title: "质量控制 QC", fields: [choice("qc_mode", "质量控制", [["head_motion", "Head Motion 头动 QC"], ["skip", "明确跳过 QC"]])] },
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
  return <details><summary>服务端流程预览（仅供审查）</summary><p>{preview.status === "ready_for_review" ? "参数完整，等待确认" : "方案待补全"}</p><ol>{steps.filter((s) => s && typeof s.label === "string").map((step, index) => <li key={index}>{String(step.label)}{typeof step.warning === "string" && <small> · {step.warning}</small>}</li>)}</ol>{branches.filter((b) => b && typeof b.label === "string").map((branch, index) => <div key={index}><strong>{String(branch.label)}</strong>{typeof branch.consumes === "string" && <p>使用：{branch.consumes}</p>}{typeof branch.warning === "string" && <p>{branch.warning}</p>}</div>)}<ul>{constraints.map((constraint, index) => <li key={index}>{constraint}</li>)}</ul><details><summary>完整预览数据</summary><pre>{JSON.stringify(value, null, 2)}</pre></details></details>;
}

type Props = { snapshot: WorkDraftSnapshot; issues?: string[]; preview?: unknown; readOnly?: boolean; busy?: boolean; confirmation?: ConfirmationRequest | null; onSave: (baseHash: string, patch: ObjectValue) => Promise<void>; onConfirm?: () => void; onDirty?: (dirty: boolean) => void };
export function WorkDraftPanel({ snapshot, issues = [], preview, readOnly = false, busy = false, confirmation, onSave, onConfirm, onDirty }: Props) {
  const panelId = useId();
  const [draft, setDraft] = useState(snapshot.draft);
  const [edited, setEdited] = useState<string[]>([]);
  const [arrayText, setArrayText] = useState<Record<string, string>>({});
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [advanced, setAdvanced] = useState("");
  const locked = readOnly || busy || saving;
  const dirty = edited.length > 0;
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
    } catch (error) { setSaveError(`${describeError(error)}。本地修改仍保留，请核对草稿版本后重试。`); }
    finally { setSaving(false); }
  }
  return <section className="work-draft-panel" aria-label={readOnly ? "历史预处理方案（只读）" : "预处理方案参数"}>
    <h3>DPARSF 预处理方案 {readOnly ? "· 历史只读" : snapshot.compiled ? "· 已生成方案" : "· 待审查"}</h3>
    <p>工作区：<code>{snapshot.workspace_path ?? "当前会话工作区"}</code> · 草稿 {snapshot.hash.slice(0, 12)}</p>
    <p>未填写项保持未知；建议需审阅。保存参数和生成方案不会启动计算。</p>
    <div className="draft-metrics" role="group" aria-label="计算指标">{["alff", "falff", "reho"].map((metric) => <label key={metric}><input type="checkbox" disabled={locked} checked={Array.isArray(draft.requested_metrics) && draft.requested_metrics.includes(metric)} onChange={(event) => {
      const current = Array.isArray(draft.requested_metrics) ? draft.requested_metrics : [];
      change("requested_metrics", event.target.checked ? [...current, metric] : current.filter((m) => m !== metric));
    }} />{metric.toUpperCase()}</label>)}</div>
    <div className="draft-metrics">{[["preprocessing.t1", "T1 处理"], ["preprocessing.nuisance.head_motion_scrubbing", "头动回归时间点剔除"], ["reho.temporal_filter_band", "ReHo 分支滤波"]].map(([path, label]) => <label key={path}>{label}<select aria-label={`${label}配置状态`} disabled={locked || (path.startsWith("reho") && !draft.reho)} value={get(draft, path) === undefined ? "unknown" : get(draft, path) === null ? "off" : "on"} onChange={(event) => change(path, event.target.value === "off" ? null : path === "preprocessing.t1" ? { structural_artifact_id: "workspace:anatomical-source" } : {})}><option value="unknown" disabled>未填写</option><option value="off">停用 / 不适用</option><option value="on">配置参数</option></select></label>)}</div>
    {groups.map((group) => <details key={group.title} open={!readOnly && ["数据与采集", "基础预处理", "T1 处理", "标准化", "质量控制 QC"].includes(group.title)}><summary>{group.title}</summary><div className="draft-fields">{group.fields.map((field) => {
      const value = get(draft, field.path); const reason = disabledReason(draft, field.path);
      const source = edited.includes(field.path) ? "用户修改（未保存）" : value === undefined ? "未填写" : value === null ? "明确不适用" : snapshot.draft.confirmed_hash ? "用户已确认" : snapshot.sources?.[field.path] === "user" ? "用户填写（待确认）" : "建议 / 待审查";
      const fieldIssues = issues.filter((issue) => issue.startsWith(`${field.path}：`) || issue.startsWith(`${field.path}:`));
      const controlDisabled = locked || Boolean(reason);
      const id = `draft-${panelId}-${field.path}`;
      return <div className="draft-field" key={field.path}><label htmlFor={id}>{field.label}</label><small>{reason ?? source}</small>
        {field.kind === "boolean" || field.choices ? <select id={id} disabled={controlDisabled} value={value === undefined ? "__unknown" : value === null ? "__null" : String(value)} onChange={(event) => {
          const raw = event.target.value;
          const selected = field.kind === "boolean" ? raw === "true" : field.choices?.find(([key]) => String(key) === raw)?.[0];
          change(field.path, raw === "__null" ? null : selected);
        }}><option value="__unknown" disabled>未填写</option><option value="__null">不适用 / 清空</option>{field.kind === "boolean" ? <><option value="true">启用 / 是</option><option value="false">停用 / 否</option></> : field.choices?.map(([key, label]) => <option key={String(key)} value={String(key)} disabled={field.path === "preprocessing.smoothing.method" && key === 2 && get(draft, "preprocessing.normalization.mode") !== 3}>{label}</option>)}</select>
        : <input id={id} disabled={controlDisabled} type={field.kind === "number" ? "number" : "text"} step="any" placeholder={field.kind === "array" ? "JSON 数组，例如 [3,3,3]" : "未填写"} value={arrayText[field.path] ?? (value === undefined || value === null ? "" : field.kind === "array" ? JSON.stringify(value) : String(value))} onChange={(event) => {
          const raw = event.target.value;
          if (field.kind === "array") {
            setArrayText((items) => ({ ...items, [field.path]: raw }));
            try { const parsed: unknown = raw.trim() ? JSON.parse(raw) : null; if (parsed !== null && !Array.isArray(parsed)) throw new Error(); change(field.path, parsed); setInputErrors((items) => ({ ...items, [field.path]: "" })); }
            catch { setInputErrors((items) => ({ ...items, [field.path]: "请输入有效 JSON 数组" })); setEdited((items) => [...new Set([...items, field.path])]); onDirty?.(true); }
          } else change(field.path, raw === "" ? null : field.kind === "number" ? Number(raw) : raw);
        }} />}
        {(fieldIssues.length > 0 || inputErrors[field.path]) && <small role="alert" className="draft-error">{[...fieldIssues, inputErrors[field.path]].filter(Boolean).join("；")}</small>}
      </div>;
    })}</div></details>)}
    <details><summary>高级参数：全部实际执行选择</summary><p>可审查完整结构；尚未通过校验的值不会执行。需要清空可选对象时明确填写 null。</p><pre>{JSON.stringify(draft, null, 2)}</pre>{!readOnly && <><label htmlFor={`advanced-${snapshot.hash}`}>完整参数 JSON（替换本地编辑）</label><textarea id={`advanced-${snapshot.hash}`} disabled={locked} value={advanced} onChange={(event) => setAdvanced(event.target.value)} placeholder="粘贴完整草稿 JSON；不含 confirmed_hash 或 provenance" /><button type="button" className="button button-secondary" disabled={locked || !advanced.trim()} onClick={() => {
      try { const parsed: unknown = JSON.parse(advanced); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("需要 JSON 对象"); const object = parsed as ObjectValue; if ("confirmed_hash" in object || JSON.stringify(object).includes('"provenance"')) throw new Error("不能编辑服务端确认与来源字段"); setDraft(object); setEdited(["advanced"]); setArrayText({}); setInputErrors({}); onDirty?.(true); setSaveError(""); }
      catch (error) { setSaveError(describeError(error)); }
    }}>应用到面板</button></>}</details>
    {issues.length > 0 && <details open><summary>待补充或修正（{issues.length}）</summary><ul>{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul></details>}
    {preview !== undefined && <PlanPreview value={preview} />}
    <p className="draft-help">暂不支持：DICOM/BIDS 转换、外部 DARTEL 模板复用、FC、度中心性。T1 准备、配准、分割及 DARTEL 在获批任务中自动衔接；启动计算需独立授权。</p>
    {saveError && <p role="alert" className="draft-error">{saveError}</p>}
    {!readOnly && <div className="draft-actions"><span role="status">{dirty ? "有未保存修改" : "当前参数已保存"}</span><button type="button" className="button button-secondary" disabled={locked || !dirty} onClick={() => { setDraft(snapshot.draft); setEdited([]); setArrayText({}); setInputErrors({}); setSaveError(""); onDirty?.(false); }}>放弃本地修改</button><button type="button" className="button button-secondary" disabled={locked || !dirty || Object.values(inputErrors).some(Boolean)} onClick={() => void save()}>{saving ? "保存中…" : "保存修改"}</button><button type="button" className="button button-primary" disabled={locked || dirty || Object.values(inputErrors).some(Boolean) || issues.length > 0 || !confirmation || snapshot.compiled} onClick={onConfirm}>确认并生成方案</button></div>}
  </section>;
}
