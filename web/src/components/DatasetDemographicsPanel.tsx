import { useEffect, useMemo, useState } from "react";

import { projectsApi } from "../api/projects";
import { describeError } from "../api/transport";
import type { Dataset, Demographics, Manifest } from "../api/types";
import { updateWorkspace } from "../workspace";

type Props = {
  datasetId: string;
  disabled?: boolean;
};

function defaultTablePath(datasetPath: string): string {
  if (!datasetPath) return "";
  const trimmed = datasetPath.replace(/[\\/]+$/, "");
  const separator = datasetPath.includes("\\") ? "\\" : "/";
  return `${trimmed}${separator}participants.tsv`;
}

function supportedTablePath(path: string): boolean {
  return /\.(csv|tsv)$/i.test(path.trim());
}

export function DatasetDemographicsPanel({ datasetId, disabled = false }: Props) {
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [subjectIdColumn, setSubjectIdColumn] = useState("");
  const [groupColumn, setGroupColumn] = useState("");
  const [ageColumn, setAgeColumn] = useState("");
  const [sexColumn, setSexColumn] = useState("");
  const [mappingConfirmed, setMappingConfirmed] = useState(false);
  const [revision, setRevision] = useState<Demographics | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    setDataset(null);
    setManifest(null);
    setRevision(null);
    setSourcePath("");
    setSubjectIdColumn("");
    setGroupColumn("");
    setAgeColumn("");
    setSexColumn("");
    setMappingConfirmed(false);
    void projectsApi.dataset(datasetId, controller.signal)
      .then(async (value) => {
        if (controller.signal.aborted) return;
        setDataset(value);
        setSourcePath(defaultTablePath(value.source_path));
        if (!value.current_manifest_id) {
          setManifest(null);
          return;
        }
        const frozen = await projectsApi.manifest(value.current_manifest_id, controller.signal);
        if (!controller.signal.aborted) setManifest(frozen);
      })
      .catch((caught) => {
        if (!controller.signal.aborted) setLoadError(describeError(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [datasetId]);

  const selectedSourceColumns = useMemo(
    () => [subjectIdColumn.trim(), groupColumn.trim(), ageColumn.trim(), sexColumn.trim()].filter(Boolean),
    [ageColumn, groupColumn, sexColumn, subjectIdColumn],
  );
  const duplicateColumns = selectedSourceColumns.length !== new Set(selectedSourceColumns).size;
  const validPath = supportedTablePath(sourcePath);
  const canImport = Boolean(
    dataset && manifest && !loading && !busy && !disabled && validPath
    && subjectIdColumn.trim() && groupColumn.trim() && !duplicateColumns && mappingConfirmed,
  );

  async function importTable(): Promise<void> {
    if (!dataset || !manifest || !canImport) return;
    setBusy(true);
    setError("");
    setRevision(null);
    try {
      const columnMapping: Record<string, string> = { group: groupColumn.trim() };
      if (ageColumn.trim()) columnMapping.age = ageColumn.trim();
      if (sexColumn.trim()) columnMapping.sex = sexColumn.trim();
      const imported = await projectsApi.importDemographics(datasetId, {
        source_path: sourcePath.trim(),
        subject_id_column: subjectIdColumn.trim(),
        column_mapping: columnMapping,
        encoding: "utf-8-sig",
        expected_dataset_version: dataset.version,
      });
      setRevision(imported);
      // Creating an immutable demographics revision increments the dataset version.
      // Keep the next explicit import on the version returned by this successful write.
      setDataset({ ...dataset, version: dataset.version + 1 });
      updateWorkspace({ demographicsId: imported.demographics_id, demographicsRevision: imported.revision });
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  const alignmentMatches = revision
    && revision.missing_subject_ids.length === 0
    && revision.extra_subject_ids.length === 0;

  return <section className="dataset-demographics" aria-label="项目人口学表">
    <details>
      <summary>登记人口学表{revision ? ` · 第 ${revision.revision} 版` : ""}</summary>
      <p>从当前数据集登记分组和协变量，服务端会按冻结的受试者清单校验编号。只填写已核对的源列名；不会根据相似名称自动映射。</p>
      {loading && <p role="status">正在读取当前数据集和冻结清单…</p>}
      {loadError && <p className="dataset-demographics-error" role="alert">无法读取数据集：{loadError}</p>}
      {!loading && !loadError && !manifest && <p role="status">请先检查并冻结当前数据集的受试者清单，再登记人口学表。</p>}
      {!loading && !loadError && manifest && <>
        <p>冻结清单：{manifest.subjects.length} 名受试者 · 修订 {manifest.revision}</p>
        <label>CSV/TSV 文件路径
          <input value={sourcePath} disabled={busy || disabled} onChange={(event) => { setSourcePath(event.target.value); setMappingConfirmed(false); setRevision(null); }} placeholder="数据集根目录下的 participants.tsv" />
        </label>
        {!validPath && sourcePath.trim() && <p className="dataset-demographics-error" role="status">文件路径需以 .csv 或 .tsv 结尾。</p>}
        <label>受试者 ID 列（例如 participant_id）
          <input value={subjectIdColumn} disabled={busy || disabled} onChange={(event) => { setSubjectIdColumn(event.target.value); setMappingConfirmed(false); setRevision(null); }} placeholder="填写源表中的列名" />
        </label>
        <label>组别列 → 分析字段 group（例如 group）
          <input value={groupColumn} disabled={busy || disabled} onChange={(event) => { setGroupColumn(event.target.value); setMappingConfirmed(false); setRevision(null); }} placeholder="填写源表中的列名" />
        </label>
        <details className="dataset-demographics-optional">
          <summary>可选协变量映射</summary>
          <label>年龄列 → age
            <input value={ageColumn} disabled={busy || disabled} onChange={(event) => { setAgeColumn(event.target.value); setMappingConfirmed(false); setRevision(null); }} placeholder="不使用时留空" />
          </label>
          <label>性别列 → sex
            <input value={sexColumn} disabled={busy || disabled} onChange={(event) => { setSexColumn(event.target.value); setMappingConfirmed(false); setRevision(null); }} placeholder="不使用时留空" />
          </label>
        </details>
        {duplicateColumns && <p className="dataset-demographics-error" role="alert">同一个源列不能同时用于 ID、组别或协变量。</p>}
        {subjectIdColumn.trim() && groupColumn.trim() && !duplicateColumns && <div className="dataset-demographics-preview" aria-label="列映射预览">
          <strong>列映射预览</strong>
          <p>{subjectIdColumn.trim()} → 受试者 ID · {groupColumn.trim()} → group{ageColumn.trim() ? ` · ${ageColumn.trim()} → age` : ""}{sexColumn.trim() ? ` · ${sexColumn.trim()} → sex` : ""}</p>
          <small>登记后由服务器读取文件并返回受试者对齐检查；这会创建一个新的不可变人口学修订。</small>
        </div>}
        <label className="dataset-demographics-confirm">
          <input type="checkbox" checked={mappingConfirmed} disabled={busy || disabled || duplicateColumns || !subjectIdColumn.trim() || !groupColumn.trim()} onChange={(event) => setMappingConfirmed(event.target.checked)} />
          我已核对文件路径以及 ID、组别列含义，并确认上述映射。
        </label>
        {error && <p className="dataset-demographics-error" role="alert">登记失败：{error}</p>}
        <button className="button button-primary" type="button" disabled={!canImport} onClick={() => void importTable()}>
          {busy ? "正在登记并校验…" : "登记并校验人口学表"}
        </button>
        {revision && <div className={`dataset-demographics-result${alignmentMatches ? " is-valid" : " has-warning"}`} role="status">
          <strong>{alignmentMatches ? "登记完成，受试者与清单完全对齐" : "登记完成，但受试者对齐存在差异"}</strong>
          <p>第 {revision.revision} 版 · {revision.row_count} 行 · 已登记列：{revision.columns.join("、") || "无"}</p>
          <p>缺少 {revision.missing_subject_ids.length} 人；额外 {revision.extra_subject_ids.length} 人。</p>
          {revision.missing_subject_ids.length > 0 && <p>缺少 ID：{revision.missing_subject_ids.join("、")}</p>}
          {revision.extra_subject_ids.length > 0 && <p>额外 ID：{revision.extra_subject_ids.join("、")}</p>}
          {!alignmentMatches && <small>请核对源表的 ID 列及纳入范围；组统计会因缺少分组信息而被阻断或不完整。修订源表后可重新登记新版本。</small>}
        </div>}
      </>}
    </details>
  </section>;
}
