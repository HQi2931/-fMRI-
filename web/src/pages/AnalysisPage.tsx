import { useState } from "react";

import {
  api,
  describeError,
  type ClusterLocalization,
  type MlTableInspection,
  type MlTemplate,
  type RoiTable,
  type RsFmriAnswer,
} from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { useWorkspace } from "../workspace";

function commaList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function AnalysisPage() {
  const workspace = useWorkspace();
  const [question, setQuestion] = useState("ALFF 的频段和 TR 有什么关系?");
  const [answer, setAnswer] = useState<RsFmriAnswer | null>(null);
  const [sourcePath, setSourcePath] = useState("");
  const [inspection, setInspection] = useState<MlTableInspection | null>(null);
  const [target, setTarget] = useState("group");
  const [subjectColumn, setSubjectColumn] = useState("subject_id");
  const [features, setFeatures] = useState("roi_1, roi_2, age");
  const [template, setTemplate] = useState<MlTemplate | null>(null);
  const [roiTable, setRoiTable] = useState<RoiTable | null>(null);
  const [localization, setLocalization] = useState<ClusterLocalization | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function ask(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      setAnswer(await api.answerRsFmriQuestion({ question, allow_remote_search: false }));
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function inspect(): Promise<void> {
    if (!workspace.projectId || !sourcePath.trim()) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.inspectMlTable({
        project_id: workspace.projectId,
        source_path: sourcePath.trim(),
        max_rows: 100000,
      });
      setInspection(result);
      setMessage("表格只读检查完成。目标列和受试者列仍需要你确认。");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function makeTemplate(): Promise<void> {
    const featureColumns = commaList(features);
    if (!target.trim() || !subjectColumn.trim() || featureColumns.length === 0) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.createMlTemplate({
        design: {
          target_column: target.trim(),
          group_column: subjectColumn.trim(),
          feature_columns: featureColumns,
          models: ["logistic_regression", "random_forest"],
          seed: 42,
          validation_strategy: "subject_grouped_stratified_cross_validation",
          metrics: ["roc_auc", "average_precision", "balanced_accuracy"],
          warnings: [],
          requires_approval: true,
        },
        source_filename: "features.csv",
      });
      setTemplate(result);
      setMessage("已生成可审查的 Python 模板，尚未执行任何模型训练。");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function validateRoi(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      setRoiTable(
        await api.validateRoiTable({
          design: {
            input_artifact_id: "approved-functional-artifact",
            atlas_artifact_id: "approved-atlas-artifact",
            mask_artifact_id: null,
            tr_seconds: 2,
            band_low_hz: 0.01,
            band_high_hz: 0.08,
            multiple_labels: true,
            selected_roi_indices: [],
            detrend: true,
            scrubbing_timing: "disabled",
            scrubbing_method: null,
            cut_number: 10,
          },
          records: [
            {
              subject_id: "synthetic-subject",
              session_id: null,
              metric: "roi_signal",
              atlas_id: "user-atlas",
              roi_index: 1,
              roi_label: "Example_ROI",
              value: 0.1,
            },
          ],
        }),
      );
      setMessage("ROI 合同已检查。示例不会启动 DPABI 或写入文件。");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function localize(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      setLocalization(
        await api.localizeClusters({
          clusters: [
            {
              cluster_id: "example-1",
              peak_x: 0,
              peak_y: 0,
              peak_z: 0,
              voxel_count: 10,
              statistic: 3.1,
              coordinate_space: "MNI",
            },
          ],
          atlas_points: [
            { x: 0, y: 0, z: 0, label: "User atlas example", coordinate_space: "MNI" },
          ],
          max_distance_mm: 8,
        }),
      );
      setMessage("定位示例只使用结构化坐标和用户 atlas 标签，不推断临床结论。");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="扩展分析"
        title="ROI、机器学习、脑区定位与方法学问答"
        description="先检查与设计，再由你审批。当前页面只调用本地确定性服务，不会启动 MATLAB、训练模型或联网检索。"
      />
      <Feedback message={error || message} error={Boolean(error)} />
      <div className="two-column wide-left">
        <section className="panel form-panel">
          <div className="panel-heading"><div><span className="eyebrow">机器学习准备</span><h2>表格检查和模板</h2></div></div>
          {!workspace.projectId ? <EmptyState title="请先选择项目" detail="表格路径必须位于当前项目允许的只读根目录。" /> : <>
            <label>CSV/TSV/XLSX 路径<input value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} placeholder="项目允许的源目录内路径" /></label>
            <button className="button button-secondary" type="button" disabled={busy || !sourcePath.trim()} onClick={inspect}>检查表格</button>
            {inspection && <p className="muted">{inspection.inspection.filename}: {inspection.inspection.row_count} 行, 候选目标列 {inspection.inspection.target_candidates.join(", ") || "无"}</p>}
            <label>目标列<input value={target} onChange={(event) => setTarget(event.target.value)} /></label>
            <label>受试者列<input value={subjectColumn} onChange={(event) => setSubjectColumn(event.target.value)} /></label>
            <label>特征列(逗号分隔)<input value={features} onChange={(event) => setFeatures(event.target.value)} /></label>
            <button className="button button-primary" type="button" disabled={busy} onClick={makeTemplate}>生成待审批 ML 模板</button>
            {template && <details><summary>查看模板 {template.template.filename}</summary><pre>{template.template.content}</pre></details>}
          </>}
        </section>
        <aside className="panel">
          <span className="eyebrow">rs-fMRI 问答</span><h2>本地证据优先</h2>
          <label>问题<textarea value={question} onChange={(event) => setQuestion(event.target.value)} /></label>
          <button className="button button-secondary" type="button" disabled={busy || !question.trim()} onClick={ask}>检索本地证据并回答</button>
          {answer && <div className="assistant-message"><div><strong>{answer.answer.in_scope ? "rs-fMRI 回答" : "范围提示"}</strong><p>{answer.answer.answer}</p><ul className="compact-list">{answer.answer.evidence.map((item) => <li key={`${item.source}-${item.title}`}>{item.title}</li>)}</ul></div></div>}
        </aside>
      </div>
      <div className="two-column">
        <section className="panel"><span className="eyebrow">ROI</span><h2>DPABI 信号提取合同</h2><p className="muted">检查 TR、Nyquist、scrubbing、ROI 标签及长宽表结构。真实提取需经过计划审批。</p><button className="button button-secondary" type="button" disabled={busy} onClick={validateRoi}>运行安全示例</button>{roiTable && <p className="muted">{roiTable.valid ? `合同通过，长表 ${roiTable.long_rows.length} 行。` : roiTable.issues.join(", ")}</p>}</section>
        <section className="panel"><span className="eyebrow">统计定位</span><h2>cluster 与 atlas</h2><p className="muted">只依据峰值坐标和你提供的 atlas 标签定位，未提供 atlas 时只返回坐标。</p><button className="button button-secondary" type="button" disabled={busy} onClick={localize}>运行安全示例</button>{localization && <p className="muted">{localization.results[0]?.atlas_label ?? "未匹配"}，置信度 {localization.results[0]?.confidence ?? 0}</p>}</section>
      </div>
    </>
  );
}
