import type { AgentHandoff } from "../shared/types";

const blockingMessages: Record<string, string> = {
  work_handoff_limit_reached: "本会话的指标需求交接次数已达上限，请先核对当前任务。",
  work_handoff_stale: "来源证据已变化，请回到发起交接的 Agent 更新需求。",
  source_run_not_succeeded: "来源运行尚未满足分析完成条件。",
  statistics_source_unapproved: "尚未确认来源数据的 QC。",
  source_manifest_superseded: "输入清单已更新，需要重新确认。",
  source_manifest_content_changed: "输入数据已变化，需要重新确认。",
  artifact_integrity_changed: "已登记的结果文件发生变化。",
  handoff_metric_versions_mixed: "指标来自不同处理版本，需要核对后再交接。",
};

export function AgentHandoffPanel({ handoff }: { handoff: AgentHandoff }) {
  const target = handoff.to_agent === "analysis" ? "数据分析 Agent" : "预处理 Agent";
  const state = handoff.status === "received" ? `${target} 已接收`
    : handoff.status === "ready" ? `数据证据已就绪，可交给${target}`
    : handoff.status === "stale" ? "来源证据已变化，交接待更新"
    : handoff.status === "blocked" ? "交接受阻，需要核对"
    : `等待${target}`;
  const issues = [...new Set(handoff.blocking_items?.map((code) => blockingMessages[code]
    ?? "交接暂时受阻，请核对来源与 QC。") ?? [])];
  return <details className="chat-technical" open>
    <summary>Agent 交接 · {state}</summary>
    {handoff.reason && <p>{handoff.reason}</p>}
    {handoff.required_outputs?.length ? <p>所需产物：{handoff.required_outputs.join("、")}</p> : null}
    {handoff.data_references?.input_selection_required && <p>补充指标计算前，需先选择并确认预处理输入。</p>}
    {issues.length > 0 && <ul>{issues.map((issue) => <li key={issue}>{issue}</li>)}</ul>}
  </details>;
}
