const labels: Record<string, string> = {
  model_response: "模型响应", file_tools: "文件读取与检索", fact_derivation: "扫描事实推导",
  validation: "参数校验", hashing: "文件完整性检查", compilation: "方案编译",
  queueing: "创建执行队列", workspace_inspection: "工作区概况", other_tools: "其他工具检查",
  prepare_total: "本轮准备总耗时",
};

export function WorkTimingDetails({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || !("stages" in value) || !Array.isArray(value.stages)) return null;
  const rows = value.stages.slice(0, 20).flatMap((row: unknown) => {
    if (!row || typeof row !== "object" || !("stage" in row) || !("duration_ms" in row) || !("calls" in row)) return [];
    if (typeof row.stage !== "string" || !labels[row.stage] || typeof row.duration_ms !== "number" || !Number.isFinite(row.duration_ms) || row.duration_ms < 0 || typeof row.calls !== "number" || !Number.isInteger(row.calls) || row.calls < 1) return [];
    return [{ stage: row.stage, durationMs: row.duration_ms, calls: row.calls }];
  });
  if (!rows.length) return null;
  return <details className="chat-technical"><summary>查看本轮耗时</summary>
    <table><thead><tr><th>阶段</th><th>累计耗时</th><th>次数</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.stage}-${index}`}><td>{labels[row.stage]}</td><td>{row.durationMs >= 1000 ? `${(row.durationMs / 1000).toFixed(2)} 秒` : `${row.durationMs.toFixed(1)} 毫秒`}</td><td>{row.calls}</td></tr>)}</tbody></table>
    <p>各阶段可能包含子步骤，耗时不能直接相加。未列出的阶段本轮未测量。</p>
  </details>;
}
