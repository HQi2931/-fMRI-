import type { StatisticalMethodSummary } from "../api/client";

const centeringLabels: Record<string, string> = { none: "不中心化", grand_mean: "总体均值中心化", within_group: "组内均值中心化" };
const numeric = (value: number | null | undefined) => value == null ? "未记录" : String(Number(value.toPrecision(5)));

export function StatisticalMethodPanel({ summary }: { summary: StatisticalMethodSummary | null | undefined }) {
  if (!summary) return <p>方法摘要暂不可用，请查看冻结报告和运行记录。</p>;
  return <section className="statistical-method-panel" aria-label="统计方法与方向">
    <h5>统计方法与方向</h5>
    <p>{summary.test_label} · {summary.subject_count} 名受试者 · {summary.image_count} 张影像</p>
    {Object.keys(summary.group_counts).length > 0 && <p>分组：{Object.entries(summary.group_counts).map(([group, count]) => `${group} ${count} 人`).join("；")}</p>}
    {summary.condition_order.length > 0 && <p>配对条件顺序：{summary.condition_order.join(" → ")}</p>}
    <dl className="statistical-method-grid">
      <dt>检验范围</dt><dd>{summary.tail_label}</dd>
      <dt>统计量与自由度</dt><dd>{summary.statistic_type} · df₁={numeric(summary.df1)}{summary.df2 != null ? `，df₂=${numeric(summary.df2)}` : ""}</dd>
      <dt>检验方向</dt><dd>{summary.positive_direction && summary.negative_direction
        ? `正值：${summary.positive_direction}；负值：${summary.negative_direction}`
        : "总体 F 检验不表示哪组更高；组间方向需另行审查明确的对比。"}</dd>
      <dt>冻结对比</dt><dd>{summary.contrast.join(", ")}</dd>
      <dt>协变量</dt><dd>{summary.covariates.length ? summary.covariates.map((column) => `${column.name}（${centeringLabels[column.centering] ?? column.centering}）`).join("；") : "无"}</dd>
      <dt>多重比较校正</dt><dd>{summary.correction_method?.toUpperCase() ?? "未校正，仅作描述"}</dd>
      {summary.correction_method === "fdr" && <>
        <dt>FDR q</dt><dd>{numeric(summary.q_threshold)}</dd>
        <dt>实际 p 截断值</dt><dd>{numeric(summary.actual_p_threshold)}</dd>
      </>}
      {summary.correction_method === "grf" && <>
        <dt>体素 p／簇 p</dt><dd>{numeric(summary.voxel_p_threshold)}／{numeric(summary.cluster_p_threshold)}</dd>
        <dt>实际平滑度来源</dt><dd>{summary.smoothness_source === "provided" ? "方案指定 DLH" : summary.smoothness_source === "header" ? "统计图头信息" : summary.smoothness_source === "estimated" ? "未阈值化 Z 图估计" : "未记录"}</dd>
        <dt>实际 DLH</dt><dd>{numeric(summary.effective_smoothness_dlh)}</dd>
        <dt>实际 FWHM（mm）</dt><dd>{summary.effective_fwhm?.map(numeric).join("，") || "未记录"}</dd>
        <dt>最小簇大小</dt><dd>{numeric(summary.minimum_cluster_size)} 个体素</dd>
        <dt>连通性</dt><dd>{summary.cluster_connectivity == null ? "未记录" : "26 邻域"}</dd>
      </>}
      <dt>校正证据</dt><dd>{summary.correction_evidence_status === "verified" ? "已核验登记文件" : summary.correction_evidence_status === "not_applicable" ? "不适用" : "实际证据暂不可用；参数以冻结方案为准"}</dd>
      <dt>科研软件</dt><dd>{Object.entries(summary.software_versions).map(([name, version]) => `${name} ${version}`).join("；") || "版本摘要暂不可用，请查看软件证据产物"}</dd>
    </dl>
    <p>方向指冻结设计中的效应或目标系数；有协变量时按方案控制这些变量。</p>
    {summary.assumptions.map((assumption) => <p key={assumption}>{assumption}</p>)}
    {summary.evidence_issues.length > 0 && <p role="status">部分方法证据无法复核，请检查运行产物。</p>}
  </section>;
}
