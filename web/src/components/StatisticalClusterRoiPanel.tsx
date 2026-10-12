import { useState } from "react";

import { statisticsApi } from "../api/statistics";
import { describeError } from "../api/transport";
import type { StatisticalClusterRoi } from "../api/types";

export function StatisticalClusterRoiPanel({ resultId, bundleHash, clusterId }: {
  resultId: string; bundleHash: string; clusterId: string;
}) {
  const [roi, setRoi] = useState<StatisticalClusterRoi | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const current = roi?.result_id === resultId && roi.result_bundle_hash === bundleHash && roi.cluster_id === clusterId ? roi : null;
  async function load() {
    setBusy(true); setError(""); setRoi(null);
    try {
      const value = await statisticsApi.statisticalClusterRoi(resultId, clusterId);
      if (value.result_id !== resultId || value.result_bundle_hash !== bundleHash || value.cluster_id !== clusterId) {
        setError("ROI 来源与当前结果不一致，请重新加载结果后再读取。");
      } else setRoi(value);
    }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }
  const rows = current?.rows ?? [];
  const labels = [...new Set(rows.map((row) => row.condition ?? row.group ?? "全部受试者"))];
  const minimum = Math.min(...rows.map((row) => row.mean_value));
  const maximum = Math.max(...rows.map((row) => row.mean_value));
  const spread = maximum - minimum || 1;
  const tsv = current ? ["subject_id\tgroup\tcondition\tmean_value", ...rows.map((row) =>
    [row.subject_id, row.group ?? "", row.condition ?? "", String(row.mean_value)].map((value) => value.replace(/[\t\r\n]/g, " ")).join("\t"))].join("\n") : "";
  return <details className="statistical-roi-panel"><summary>簇 {clusterId} 的 ROI 描述</summary>
    <p>展示每名受试者在该显著簇内的指标均值。ROI 来自本次检验，不能作为独立验证。</p>
    <button type="button" disabled={busy} onClick={() => void load()}>{busy ? "读取中…" : "读取 ROI 分布"}</button>
    {error && <p role="alert">{error}</p>}
    {current && <>
      <p>{current.voxel_count} 个体素 · {rows.length} 张指标图 · {current.limitation}</p>
      {rows.length > 0 && <svg role="img" aria-label="逐受试者 ROI 均值分布" viewBox={`0 0 640 ${labels.length * 44 + 50}`}>
        {labels.map((label, index) => <g key={label}><text x="4" y={index * 44 + 24} fontSize="12">{label}</text><line x1="130" x2="600" y1={index * 44 + 20} y2={index * 44 + 20} stroke="#b4beca" /></g>)}
        {rows.map((row, index) => <circle key={`${row.subject_id}-${row.condition ?? ""}`} cx={130 + (row.mean_value - minimum) / spread * 470}
          cy={labels.indexOf(row.condition ?? row.group ?? "全部受试者") * 44 + 20 + (index % 3 - 1) * 4} r="4" fill="#32727a" fillOpacity="0.75"><title>{row.subject_id}：{row.mean_value.toFixed(4)}</title></circle>)}
        <text x="130" y={labels.length * 44 + 26} fontSize="12">{minimum.toFixed(3)}</text>
        <text x="560" y={labels.length * 44 + 26} fontSize="12">{maximum.toFixed(3)}</text>
      </svg>}
      <div className="work-run-actions">
        <a download={`roi-${clusterId}.tsv`} href={`data:text/tab-separated-values;charset=utf-8,${encodeURIComponent(tsv)}`}>下载 ROI 均值表</a>
        <a download={`roi-${clusterId}.json`} href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(current, null, 2))}`}>下载 ROI 来源记录</a>
      </div>
      <div className="table-scroll"><table><thead><tr><th>受试者</th><th>组别／条件</th><th>簇内均值</th></tr></thead><tbody>
        {rows.map((row) => <tr key={`${row.subject_id}-${row.condition ?? ""}`}><td>{row.subject_id}</td><td>{row.condition ?? row.group ?? "—"}</td><td>{row.mean_value.toFixed(4)}</td></tr>)}
      </tbody></table></div>
    </>}
  </details>;
}
