import { useEffect, useMemo, useState } from "react";

import { statisticsApi } from "../api/statistics";
import { describeError } from "../api/transport";
import type { ExternalAnalysisImport, ExternalAnalysisImportSummary, ExternalDemographicsRevision } from "../api/types";

type Props = {
  projectId: string;
  initialPath?: string;
  disabled?: boolean;
  selectedImportId?: string | null;
  onSelect?: (importId: string | null) => void;
};

function moveItem(items: string[], index: number, delta: -1 | 1): string[] {
  const destination = index + delta;
  if (destination < 0 || destination >= items.length) return items;
  const updated = [...items];
  [updated[index], updated[destination]] = [updated[destination], updated[index]];
  return updated;
}

export function ExternalAnalysisImportPanel({ projectId, initialPath = "", disabled = false, selectedImportId, onSelect }: Props) {
  const [sourcePath, setSourcePath] = useState(initialPath);
  const [layout, setLayout] = useState<"grouped" | "paired_conditions">("grouped");
  const [demographics, setDemographics] = useState<ExternalDemographicsRevision | null>(null);
  const [demographicsPath, setDemographicsPath] = useState("");
  const [subjectIdColumn, setSubjectIdColumn] = useState("subject_id");
  const [columnMapping, setColumnMapping] = useState("");
  const [imports, setImports] = useState<ExternalAnalysisImportSummary[]>([]);
  const [inspection, setInspection] = useState<ExternalAnalysisImport | null>(null);
  const [groupOrder, setGroupOrder] = useState<string[]>([]);
  const [space, setSpace] = useState<"unknown" | "MNI" | "Talairach">("unknown");
  const [maskRelativePath, setMaskRelativePath] = useState("");
  const [excludedIds, setExcludedIds] = useState("");
  const [actor, setActor] = useState("researcher");
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!inspection || inspection.status !== "included") return;
    const controller = new AbortController();
    void statisticsApi.externalDemographics(inspection.import_id, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setDemographics(value); })
      .catch((caught) => { if (!controller.signal.aborted) setError(describeError(caught)); });
    return () => controller.abort();
  }, [inspection]);

  useEffect(() => {
    setSourcePath(initialPath);
  }, [initialPath]);

  useEffect(() => {
    const controller = new AbortController();
    setInspection(null);
    setImports([]);
    statisticsApi.externalAnalysisImports(projectId, controller.signal)
      .then((items) => { if (!controller.signal.aborted) setImports(items); })
      .catch((caught) => { if (!controller.signal.aborted) setError(describeError(caught)); });
    return () => controller.abort();
  }, [projectId]);

  const allImages = inspection?.inspection.images ?? [];
  const excluded = useMemo(() => new Set(
    excludedIds.split(/[\n,;]+/).map((value) => value.trim()).filter(Boolean),
  ), [excludedIds]);
  const includedImages = allImages.filter((image) => !excluded.has(image.subject_id));
  const includedSubjectIds = [...new Set(includedImages.map((image) => image.subject_id))];
  const paired = inspection?.inspection.layout === "paired_conditions";
  const unknownExclusions = [...excluded].filter(
    (subjectId) => !allImages.some((image) => image.subject_id === subjectId),
  );
  const includedByGroup = new Map<string, number>();
  for (const group of groupOrder) includedByGroup.set(group, 0);
  for (const image of includedImages) {
    includedByGroup.set(image.group, (includedByGroup.get(image.group) ?? 0) + 1);
  }
  const emptyIncludedGroups = [...includedByGroup].filter(([, count]) => count === 0).map(([group]) => group);
  const canInclude = Boolean(
    inspection && inspection.status === "needs_review" && !inspection.inspection.blocking_issues.length
    && includedSubjectIds.length >= 2 && !unknownExclusions.length && !emptyIncludedGroups.length
    && acknowledged && actor.trim() && reason.trim() && !disabled,
  );

  async function refreshList(): Promise<void> {
    setImports(await statisticsApi.externalAnalysisImports(projectId));
  }

  async function inspect(): Promise<void> {
    if (!sourcePath.trim()) return;
    setBusy(true);
    setError("");
    try {
      const item = await statisticsApi.inspectExternalAnalysisImport({
        project_id: projectId,
        source_path: sourcePath.trim(),
        layout,
      });
      setInspection(item);
      setGroupOrder([...item.inspection.group_order]);
      setExcludedIds("");
      setSpace("unknown");
      setMaskRelativePath("");
      setAcknowledged(false);
      setReason("");
      await refreshList();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function include(): Promise<void> {
    if (!inspection || !canInclude) return;
    setBusy(true);
    setError("");
    try {
      const item = await statisticsApi.includeExternalAnalysisImport(inspection.import_id, {
        expected_version: inspection.version,
        included_subject_ids: includedSubjectIds,
        group_order: groupOrder,
        coordinate_space: space,
        acknowledge_unknown_processing: true,
        actor: actor.trim(),
        reason: reason.trim(),
        ...(maskRelativePath.trim() ? { mask_relative_path: maskRelativePath.trim() } : {}),
      });
      setInspection(item);
      await refreshList();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return <section className="external-analysis-import" aria-label="外部 DPABI 指标导入">
    <details>
      <summary>导入外部 DPABI 指标目录</summary>
      <p>按一级目录记录独立组别或配对条件，按 NIfTI 文件名识别受试者；检查通过后仍须单独确认纳入。未知处理过程会保留为未知。</p>
      <label>数据布局<select value={layout} disabled={busy || disabled} onChange={(event) => setLayout(event.target.value as "grouped" | "paired_conditions")}>
        <option value="grouped">独立组别：每人一张指标图</option>
        <option value="paired_conditions">配对条件：每人在两个条件各有一张图</option>
      </select></label>
      <label>指标目录路径
        <input
          type="text"
          value={sourcePath}
          onChange={(event) => setSourcePath(event.target.value)}
          placeholder="例如 D:\\Study\\ALFF\\zALFFMap"
          disabled={busy || disabled}
        />
      </label>
      <button className="button button-secondary" type="button" onClick={() => void inspect()} disabled={busy || disabled || !sourcePath.trim()}>
        {busy ? "检查中…" : "检查目录"}
      </button>
      {error && <p className="external-analysis-import-error" role="alert">{error}</p>}

      {inspection && <div className="external-analysis-import-review">
        <h4>目录检查结果 · {inspection.status === "included" ? "已纳入" : "待人工确认"}</h4>
        <p>
          {inspection.inspection.metric_kind ?? "指标未识别"} · {inspection.inspection.scaling ?? "缩放未识别"} · {allImages.length} 张影像
        </p>
        <ul>
          {groupOrder.map((group, index) => <li key={group}>
            {group}：检查到 {inspection.inspection.images.filter((image) => image.group === group).length} 名，当前纳入 {includedByGroup.get(group) ?? 0} 名
            <button type="button" className="button button-secondary" aria-label={`上移${paired ? "条件" : "组别"} ${group}`} disabled={index === 0 || busy || inspection.status === "included"} onClick={() => setGroupOrder((items) => moveItem(items, index, -1))}>上移</button>
            <button type="button" className="button button-secondary" aria-label={`下移${paired ? "条件" : "组别"} ${group}`} disabled={index === groupOrder.length - 1 || busy || inspection.status === "included"} onClick={() => setGroupOrder((items) => moveItem(items, index, 1))}>下移</button>
          </li>)}
        </ul>
        {paired && <p>已按编号检查配对；条件顺序表示“前一条件减后一条件”。排除受试者会同时排除两张图。</p>}
        {inspection.inspection.blocking_issues.length > 0 && <div role="alert">
          <strong>存在阻断项，不能纳入：</strong>
          <ul>{inspection.inspection.blocking_issues.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>}
        {inspection.inspection.warnings.length > 0 && <ul>{inspection.inspection.warnings.map((item) => <li key={item}>{item}</li>)}</ul>}
        <details><summary>排除受试者</summary>
          <label>受试者 ID（每行或逗号分隔）
            <textarea value={excludedIds} disabled={busy || inspection.status === "included"} onChange={(event) => setExcludedIds(event.target.value)} rows={4} />
          </label>
          {unknownExclusions.length > 0 && <p role="alert">以下 ID 不在检查清单中：{unknownExclusions.join(", ")}</p>}
          {emptyIncludedGroups.length > 0 && <p role="alert">每个组至少保留一名受试者：{emptyIncludedGroups.join(", ")}</p>}
        </details>
        <label>图像空间
          <select value={space} disabled={busy || inspection.status === "included"} onChange={(event) => setSpace(event.target.value as typeof space)}>
            <option value="unknown">未知</option><option value="MNI">MNI（由审核者确认）</option><option value="Talairach">Talairach（由审核者确认）</option>
          </select>
        </label>
        {space === "unknown" && <p>保留未知空间；若将来产生显著簇，系统会阻止脑区定位。</p>}
        <label>统计 mask（相对导入目录的 NIfTI 路径，可留空）
          <input value={maskRelativePath} disabled={busy || inspection.status === "included"} onChange={(event) => setMaskRelativePath(event.target.value)} placeholder="例如 Mask.nii" />
        </label>
        {!maskRelativePath.trim() && <p>未登记 mask 时仍可保存数据纳入记录，但统计运行会要求先提供与指标图网格一致的 mask。</p>}
        <label>审核人
          <input value={actor} disabled={busy || inspection.status === "included"} onChange={(event) => setActor(event.target.value)} />
        </label>
        <label>纳入理由
          <textarea value={reason} disabled={busy || inspection.status === "included"} onChange={(event) => setReason(event.target.value)} rows={2} placeholder="记录数据来源及纳入依据" />
        </label>
        {inspection.status === "needs_review" && <>
          <label><input type="checkbox" checked={acknowledged} disabled={busy} onChange={(event) => setAcknowledged(event.target.checked)} /> 我已阅读检查结果，并接受未记录的处理参数保持未知。</label>
          <button className="button button-primary" type="button" onClick={() => void include()} disabled={!canInclude || busy}>
            确认纳入 {includedSubjectIds.length} 名受试者
          </button>
        </>}
        {inspection.status === "included" && <>
          <p role="status">纳入决定已冻结 · 记录版本 {inspection.version} · 哈希 {inspection.inclusion_hash?.slice(0, 12)}</p>
          {onSelect && <button type="button" disabled={busy || disabled || !inspection.selection} onClick={() => onSelect(inspection.import_id)}>
            {selectedImportId === inspection.import_id ? "已选为本次分析来源" : "用于本次分析"}
          </button>}
          {!inspection.selection && <p>需登记同网格掩膜后才能用于统计分析。</p>}
          <details>
            <summary>人口学与协变量</summary>
            {demographics?.external_import_id === inspection.import_id && <p>第 {demographics.revision} 版 · {demographics.row_count} 名受试者 · 可用列：{demographics.columns.join("、")}</p>}
            <p>按受试者编号与纳入名单对齐。登记新修订后，引用旧人口学表的方案需重新准备。</p>
            <label>人口学表路径<input value={demographicsPath} disabled={busy || disabled} onChange={(event) => setDemographicsPath(event.target.value)} /></label>
            <label>受试者编号列<input value={subjectIdColumn} disabled={busy || disabled} onChange={(event) => setSubjectIdColumn(event.target.value)} /></label>
            <label>字段对应（每行填写：分析列名=表中列名）<textarea value={columnMapping} disabled={busy || disabled} onChange={(event) => setColumnMapping(event.target.value)} placeholder={"age=Age\nsex=SexCode"} /></label>
            <button type="button" disabled={busy || disabled || !demographicsPath.trim() || !subjectIdColumn.trim()} onClick={() => {
              setBusy(true); setError("");
              void (async () => {
                try {
                  const mapping: Record<string, string> = Object.create(null);
                  for (const line of columnMapping.split(/\r?\n/).filter((value) => value.trim())) {
                    const parts = line.split("=").map((value) => value.trim());
                    if (parts.length !== 2 || !parts[0] || !parts[1] || parts[0] in mapping) throw new Error("每行需填写一对不同的列名，如 age=Age。");
                    mapping[parts[0]] = parts[1];
                  }
                  const revision = await statisticsApi.importExternalDemographics(inspection.import_id, {
                    source_path: demographicsPath.trim(), subject_id_column: subjectIdColumn.trim(),
                    column_mapping: mapping, encoding: "utf-8-sig",
                    expected_import_version: inspection.version,
                  });
                  setDemographics(revision);
                  setInspection(await statisticsApi.externalAnalysisImport(inspection.import_id));
                } catch (caught) { setError(describeError(caught)); }
                finally { setBusy(false); }
              })();
            }}>登记并对齐人口学修订</button>
          </details>
        </>}
      </div>}
    </details>
    {selectedImportId && onSelect && <p>当前分析使用外部纳入记录。<button type="button" disabled={busy || disabled} onClick={() => onSelect(null)}>改用工作区运行结果</button></p>}
    {imports.length > 0 && <details className="external-analysis-import-history">
      <summary>已登记外部目录（{imports.length}）</summary>
      <ul>{imports.map((item) => <li key={item.import_id}>
        {item.metric_kind ?? "未识别指标"} · {Object.entries(item.group_counts).map(([group, count]) => `${group} ${count}`).join(" / ") || "无有效组别"} · {item.status === "included" ? "已纳入" : "待确认"}
        <button className="button button-secondary" type="button" disabled={busy} onClick={() => {
          setBusy(true); setError("");
          void statisticsApi.externalAnalysisImport(item.import_id)
            .then((detail) => { setInspection(detail); setSourcePath(detail.source_path); setGroupOrder([...(detail.inclusion?.group_order ?? detail.inspection.group_order)]); setExcludedIds(detail.inclusion?.excluded_subject_ids.join("\n") ?? ""); setSpace(detail.inclusion?.coordinate_space ?? "unknown"); setMaskRelativePath(detail.inclusion?.mask_relative_path ?? ""); setAcknowledged(detail.status === "included"); setReason(detail.inclusion?.reason ?? ""); setActor(detail.inclusion?.actor ?? "researcher"); })
            .catch((caught) => setError(describeError(caught)))
            .finally(() => setBusy(false));
        }}>查看</button>
      </li>)}</ul>
    </details>}
  </section>;
}
