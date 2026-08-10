import { useEffect, useRef, useState } from "react";

import { api, describeError, type Manifest } from "../api/client";
import { EmptyState, Feedback, PageHeader, SafetyNotice } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { resetWorkspace, updateWorkspace, useWorkspace } from "../workspace";

function parseColumnMapping(value: string): Record<string, string> {
  const mapping: Record<string, string> = {};
  for (const line of value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)) {
    const [target, source, ...rest] = line.split("=").map((item) => item.trim());
    if (!target || !source || rest.length) throw new Error("字段映射每行必须是“标准字段=原表字段”。");
    if (mapping[target]) throw new Error(`字段映射重复：${target}`);
    mapping[target] = source;
  }
  return mapping;
}

export function DataPage() {
  const workspace = useWorkspace();
  const [projectName, setProjectName] = useState("静息态 fMRI 项目");
  const [datasetName, setDatasetName] = useState("主数据集");
  const [sourceRoot, setSourceRoot] = useState("");
  const [workRoot, setWorkRoot] = useState("");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const manifestIdRef = useRef<string | null>(null);
  const [demographicsPath, setDemographicsPath] = useState("");
  const [subjectColumn, setSubjectColumn] = useState("");
  const [columnMapping, setColumnMapping] = useState("");
  const [encoding, setEncoding] = useState("");
  const [splitSeed, setSplitSeed] = useState("");
  const [trainRatio, setTrainRatio] = useState("");
  const [validationRatio, setValidationRatio] = useState("");
  const [testRatio, setTestRatio] = useState("");
  const [stratifyBy, setStratifyBy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!workspace.manifestId || manifestIdRef.current === workspace.manifestId) return;
    const controller = new AbortController();
    api.manifest(workspace.manifestId, controller.signal).then((stored) => {
      manifestIdRef.current = stored.manifest_id;
      setManifest(stored);
    }).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [workspace.manifestId]);

  useEffect(() => {
    if (!workspace.projectId || workspace.datasetId) return;
    const controller = new AbortController();
    api.project(workspace.projectId, controller.signal).then((project) => {
      setProjectName(project.name);
      setSourceRoot((current) => current || project.source_roots[0] || "");
      setWorkRoot(project.work_root);
      if (project.version !== workspace.projectVersion) updateWorkspace({ projectVersion: project.version });
    }).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [workspace.datasetId, workspace.projectId, workspace.projectVersion]);

  async function inspect(): Promise<void> {
    setBusy(true);
    setMessage("");
    setError("");
    try {
      let projectId = workspace.projectId;
      let projectVersion = workspace.projectVersion;
      if (!projectId || !projectVersion) {
        const project = await api.createProject({
          name: projectName.trim(),
          source_roots: [sourceRoot.trim()],
          work_root: workRoot.trim(),
        });
        projectId = project.project_id;
        projectVersion = project.version;
        updateWorkspace({ projectId, projectVersion });
      }

      let datasetId = workspace.datasetId;
      let datasetVersion = workspace.datasetVersion;
      if (!datasetId || !datasetVersion) {
        const dataset = await api.createDataset(projectId, {
          name: datasetName.trim(),
          source_path: sourceRoot.trim(),
          expected_project_version: projectVersion,
        });
        datasetId = dataset.dataset_id;
        datasetVersion = dataset.version;
        updateWorkspace({ datasetId, datasetVersion });
      }

      const scanned = await api.inspectDataset(datasetId, {
        expected_dataset_version: datasetVersion,
      });
      const [refreshedDataset, refreshedProject] = await Promise.all([
        api.dataset(datasetId),
        api.project(projectId),
      ]);
      manifestIdRef.current = scanned.manifest_id;
      setManifest(scanned);
      updateWorkspace({
        projectVersion: refreshedProject.version,
        datasetVersion: refreshedDataset.version,
        manifestId: scanned.manifest_id,
        manifestHash: scanned.content_hash,
        subjectIds: Array.from(new Set(scanned.subjects.map((subject) => subject.subject_id))),
      });
      setMessage(`检查完成：识别 ${scanned.profile.subject_count} 名受试者，源目录未被修改。`);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function importDemographics(): Promise<void> {
    if (!workspace.datasetId || !workspace.datasetVersion) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.importDemographics(workspace.datasetId, {
        source_path: demographicsPath.trim(),
        subject_id_column: subjectColumn.trim(),
        column_mapping: parseColumnMapping(columnMapping),
        encoding,
        expected_dataset_version: workspace.datasetVersion,
      });
      const refreshedDataset = await api.dataset(workspace.datasetId);
      updateWorkspace({
        datasetVersion: refreshedDataset.version,
        demographicsId: result.demographics_id,
        demographicsRevision: result.revision,
      });
      setMessage(
        `人口学表已核对：${result.row_count} 行，缺失 ${result.missing_subject_ids.length}，额外 ${result.extra_subject_ids.length}。`,
      );
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function createSplit(): Promise<void> {
    if (!workspace.datasetId || !workspace.datasetVersion) return;
    setBusy(true);
    setError("");
    try {
      const seed = Number(splitSeed);
      const ratios = [Number(trainRatio), Number(validationRatio), Number(testRatio)];
      if (!Number.isInteger(seed)) throw new Error("随机种子必须是整数。");
      if (ratios.some((ratio) => !(ratio >= 0 && ratio <= 1))) throw new Error("各集合比例必须在 0 到 1 之间。");
      if (Math.abs(ratios.reduce((sum, ratio) => sum + ratio, 0) - 1) > 1e-9) throw new Error("训练、验证、测试比例之和必须为 1。");
      if (stratifyBy.trim() && !workspace.demographicsId) throw new Error("分层划分前必须先导入人口学 revision。");
      const result = await api.createSplit(workspace.datasetId, {
        expected_dataset_version: workspace.datasetVersion,
        seed,
        train_ratio: ratios[0],
        validation_ratio: ratios[1],
        test_ratio: ratios[2],
        stratify_by: stratifyBy.trim() || null,
        demographics_revision_id: stratifyBy.trim() ? workspace.demographicsId ?? null : null,
      });
      const refreshedDataset = await api.dataset(workspace.datasetId);
      updateWorkspace({ datasetVersion: refreshedDataset.version, splitId: result.split_id, splitRevision: result.revision });
      setMessage(
        `划分已冻结：训练 ${result.train_subject_ids.length}，验证 ${result.validation_subject_ids.length}，测试 ${result.test_subject_ids.length}。`,
      );
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  function startAnother(): void {
    resetWorkspace();
    manifestIdRef.current = null;
    setManifest(null);
    setMessage("已清除本机浏览器中的当前项目引用；后端记录未删除。可返回总览选择已有项目，或在此创建新项目。");
  }

  const canInspect = Boolean(
    (workspace.projectId && workspace.datasetId) ||
      (workspace.projectId && workspace.projectVersion && datasetName.trim() && sourceRoot.trim()) ||
      (!workspace.projectId && projectName.trim() && datasetName.trim() && sourceRoot.trim() && workRoot.trim()),
  );

  return (
    <>
      <PageHeader
        eyebrow="数据"
        title="只读检查与受试者清单"
        description="后端只读取允许范围内的源目录；整理、转换和预处理仅能进入独立工作目录。"
        action={workspace.projectId ? <button className="button button-secondary" type="button" onClick={startAnother}>切换项目</button> : undefined}
      />
      <Feedback message={error || message} error={Boolean(error)} />
      <section className="panel form-panel data-form">
        {!workspace.projectId && (
          <>
            <label>项目名称<input value={projectName} onChange={(event) => setProjectName(event.target.value)} /></label>
            <label className="field-grow">独立工作目录<input value={workRoot} onChange={(event) => setWorkRoot(event.target.value)} placeholder="例如 D:\\rsfmri-runs\\study" /></label>
          </>
        )}
        {!workspace.datasetId && (
          <>
            <label>数据集名称<input value={datasetName} onChange={(event) => setDatasetName(event.target.value)} /></label>
            <label className="field-grow">只读源目录<input value={sourceRoot} onChange={(event) => setSourceRoot(event.target.value)} placeholder="例如 D:\\data\\study" /></label>
          </>
        )}
        {workspace.projectId && <p className="muted field-grow">当前项目：<code>{workspace.projectId}</code> · 数据集：<code>{workspace.datasetId ?? "尚未登记"}</code></p>}
        <button className="button button-primary" type="button" disabled={!canInspect || busy} onClick={inspect}>
          {busy ? "正在检查…" : "开始只读检查"}
        </button>
      </section>

      <section className="panel table-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">冻结清单</span><h2>{manifest ? `revision ${manifest.revision}` : "尚未生成"}</h2></div>
          {manifest && <StatusPill tone={(manifest.profile.warnings ?? []).length ? "warn" : "good"}>{manifest.profile.kind}</StatusPill>}
        </div>
        {!manifest ? (
          <EmptyState title="等待数据检查" detail="填写允许的本机目录后，系统会建立文件哈希和显式受试者顺序。" />
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>受试者</th><th>会话</th><th>功能像</th><th>T1</th><th>DICOM</th></tr></thead>
              <tbody>
                {manifest.subjects.map((subject) => (
                  <tr key={`${subject.subject_id}-${subject.session_id ?? "default"}`}>
                    <td>{subject.subject_id}</td><td>{subject.session_id ?? "默认"}</td>
                    <td>{subject.functional_files?.length ?? 0}</td><td>{subject.anatomical_files?.length ?? 0}</td><td>{subject.dicom_files?.length ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="two-column">
        <section className="panel">
          <span className="eyebrow">人口学信息</span><h2>导入并对齐</h2>
          <div className="parameter-list">
            <label>CSV、TSV 或 XLSX 路径<input value={demographicsPath} onChange={(event) => setDemographicsPath(event.target.value)} /></label>
            <label>受试者 ID 列<input value={subjectColumn} onChange={(event) => setSubjectColumn(event.target.value)} /></label>
            <label>文本编码<select value={encoding} onChange={(event) => setEncoding(event.target.value)}><option value="">明确选择</option><option value="utf-8-sig">UTF-8（可含 BOM）</option><option value="utf-8">UTF-8</option><option value="gb18030">GB18030</option></select></label>
            <label>字段映射（每行：标准字段=原表字段）<textarea value={columnMapping} onChange={(event) => setColumnMapping(event.target.value)} placeholder="group=diagnosis&#10;age=age_years" /></label>
          </div>
          <div className="button-row"><button className="button button-secondary" type="button" disabled={!workspace.manifestId || !demographicsPath.trim() || !subjectColumn.trim() || !encoding || busy} onClick={importDemographics}>检查人口学表</button></div>
        </section>
        <section className="panel">
          <span className="eyebrow">数据集划分</span><h2>受试者级固定划分</h2>
          <p>随机种子、比例和分层字段均由课题明确填写；同一受试者不会跨集合。</p>
          <div className="form-grid">
            <label>随机种子<input inputMode="numeric" value={splitSeed} onChange={(event) => setSplitSeed(event.target.value)} /></label>
            <label>训练比例<input inputMode="decimal" value={trainRatio} onChange={(event) => setTrainRatio(event.target.value)} /></label>
            <label>验证比例<input inputMode="decimal" value={validationRatio} onChange={(event) => setValidationRatio(event.target.value)} /></label>
            <label>测试比例<input inputMode="decimal" value={testRatio} onChange={(event) => setTestRatio(event.target.value)} /></label>
            <label>分层字段（不分层则留空）<input value={stratifyBy} onChange={(event) => setStratifyBy(event.target.value)} placeholder="例如 group" /></label>
          </div>
          <div className="button-row"><button className="button button-secondary" type="button" disabled={!workspace.manifestId || !splitSeed || !trainRatio || !validationRatio || !testRatio || busy} onClick={createSplit}>生成划分 revision</button></div>
        </section>
      </div>
      <SafetyNotice />
    </>
  );
}
