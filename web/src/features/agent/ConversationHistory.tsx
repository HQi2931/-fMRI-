import { useState } from "react";

import { type Conversation, type Run, type WorkspaceProposal } from "../../api/types";
import { AgentHandoffPanel } from "../../components/AgentHandoffPanel";
import { ConversationText } from "../../components/ConversationText";
import { WorkDraftPanel } from "../../components/WorkDraftPanel";
import { WorkTimingDetails } from "../../components/WorkTimingDetails";
import { type Message } from "./conversation-model";
import { AnalysisProposalActions } from "./AnalysisProposalActions";

function HistoricalPlan({ item }: { item: Message }) {
  const [open, setOpen] = useState(false);
  if (!item.workDraft) return null;
  return <details open={open}>
    <summary onClick={(event) => { event.preventDefault(); setOpen((value) => !value); }}>历史方案{item.workDraft.version ? ` · 第 ${item.workDraft.version} 版` : ""}</summary>
    {open && <WorkDraftPanel snapshot={item.workDraft} issues={item.draftIssues} preview={item.planPreview} readOnly onSave={async () => {}} />}
  </details>;
}

export function ConversationHistory({ messages, busy, onApply, onUndo, onRetryAnalysisOutbox, latestDraftId, onOpenPlan, onStatisticsRun, conversation, agentRole, onTaskChange }: { messages: Message[]; busy: boolean; onApply: (proposal: WorkspaceProposal) => void; onUndo: (proposal: WorkspaceProposal) => void; onRetryAnalysisOutbox: (intentTaskId: string) => void; latestDraftId?: string; onOpenPlan: () => void; onStatisticsRun: (run: Run) => void; conversation?: Conversation; agentRole: "preprocessing" | "analysis"; onTaskChange: () => Promise<void> }) {
  return <div className="chat-history" aria-live="polite">
    {messages.map((item) => <div className={`chat-bubble chat-${item.role}`} key={item.id}>
      <span>{item.role === "assistant" ? (item.agentRole === "analysis" ? "数据分析 Agent" : "预处理 Agent") : "你"}</span>
      {item.steps && item.steps.length > 0 && <details className="chat-technical"><summary>查看工具检查详情（{item.steps.length} 项）</summary><ol className="chat-tool-steps" aria-label="Agent 工具步骤">
        {item.steps.map((step) => <li key={step.key} data-tier={step.tier ?? "unknown"} data-status={step.status}>
          <code>{step.name}</code>
          {step.tier && <span className="tool-tier">{step.tier}</span>}
          <span className="tool-status">{step.status}</span>
          {step.durationMs !== null && <span className="tool-duration">{step.durationMs} ms</span>}
        </li>)}
      </ol></details>}
      <ConversationText text={item.text} />
      {item.analysisOutboxRecovery && conversation && <button className="button button-secondary" type="button" disabled={busy} onClick={() => onRetryAnalysisOutbox(item.analysisOutboxRecovery!.intentTaskId)}>重新尝试准备草稿</button>}
      {item.analysisDraft && <details className="chat-technical" open>
        <summary>统计分析草稿{item.analysisDraft.version ? ` · 第 ${item.analysisDraft.version} 版` : ""}</summary>
        {item.analysisDraft.review && <pre>{item.analysisDraft.review}</pre>}
        {item.analysisDraft.issues?.length ? <ul>{item.analysisDraft.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul> : <p>草稿结构已校验，仍需绑定服务端数据并建立正式统计设计。</p>}
      </details>}
      {item.statisticalProposal && <details className="chat-technical" open>
        <summary>服务端统计设计预览 · {item.statisticalProposal.design.test as string}</summary>
        <p>{item.statisticalProposal.design.external_import_id ? `外部纳入记录：${String(item.statisticalProposal.design.external_import_id)}` : `来源运行：${item.statisticalProposal.source_run_id}`} · 受试者：{item.statisticalProposal.frozen_subject_order?.length ?? 0}</p>
        {item.statisticalProposal.demographics_revision_id && <p>人口学版本：{item.statisticalProposal.demographics_revision_id}</p>}
        {item.statisticalProposal.review && <pre>{item.statisticalProposal.review}</pre>}
        <small>该预览已绑定登记产物、QC 决定、受试者顺序和来源哈希。正式设计创建、审批和运行分别由下方操作启动。</small>
        {conversation && <AnalysisProposalActions key={item.statisticalProposal.proposal_hash ?? item.id} proposal={item.statisticalProposal} conversation={conversation} isCurrent={!busy && agentRole === "analysis" && conversation.foreground_agent_role === "analysis" && conversation.work_tasks?.some((task) => task.agent_role === "analysis" && task.proposal_message_id === item.id && task.proposal_hash === item.statisticalProposal?.proposal_hash) === true} onTaskChange={onTaskChange} onRunChange={onStatisticsRun} />}
      </details>}
      {item.statisticalDesignIssues?.length ? <details className="chat-technical" open>
        <summary>统计设计阻断项</summary><ul>{item.statisticalDesignIssues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>
      </details> : null}
      {item.agentHandoff && <AgentHandoffPanel handoff={item.agentHandoff} />}
      {item.analysisDependency && <details className="chat-technical" open>
        <summary>两阶段任务 · {item.analysisDependency.status === "analysis_ready" ? "预处理与 QC 已满足，可进入统计分析" : item.analysisDependency.status === "analysis_started" ? "已交给数据分析 Agent" : item.analysisDependency.status === "blocked" ? "交接受阻，需更新来源证据" : "等待预处理与 QC"}</summary>
        {item.analysisDependency.analysis_request && <p>{item.analysisDependency.analysis_request}</p>}
      </details>}
      {item.statisticalResults?.length ? <details className="chat-technical" open>
        <summary>已登记统计结果（{item.statisticalResults.length}）</summary>
        <ul>{item.statisticalResults.map((result) => <li key={result.result_id}>
          {result.result_id} · {result.mode === "synthetic_non_scientific" || result.non_scientific ? "合成演示结果" : "真实统计结果"} · {result.artifact_count} 项产物 · {result.cluster_count} 个簇
        </li>)}</ul>
      </details> : null}
      <WorkTimingDetails value={item.workTimings} />
      {item.streamingStatus && <small role="status">{item.streamingStatus}</small>}
      {item.workDraft && (item.id === latestDraftId
        ? <button className="button button-secondary" type="button" onClick={onOpenPlan}>查看当前方案{item.workDraft.version ? ` · 第 ${item.workDraft.version} 版` : ""}</button>
        : <HistoricalPlan item={item} />)}
      {item.workspaceCitations && item.workspaceCitations.length > 0 && <details className="chat-technical"><summary>查看文件证据（{item.workspaceCitations.length} 项）</summary><ul className="workspace-citations">{item.workspaceCitations.map((citation) => <li key={`${citation.path}:${citation.locator}`}><code>{citation.path}</code> · {citation.locator}</li>)}</ul></details>}
      {item.proposal && <details className="workspace-proposal"><summary>查看文件修改 diff（{item.proposal.proposal_hash.slice(0, 12)}）</summary><p>不可变补丁哈希：<code>{item.proposal.proposal_hash}</code></p><p>文件数：{item.proposal.edits.length} · 增加 {patchStats(item.proposal.diff).added} 行 · 删除 {patchStats(item.proposal.diff).removed} 行</p><pre>{item.proposal.diff}</pre>{item.proposal.status === "proposed" && <button className="button button-primary" type="button" disabled={busy} onClick={() => onApply(item.proposal!)}>确认应用补丁</button>}{item.proposal.status === "applied" && <button className="button button-secondary" type="button" disabled={busy} onClick={() => onUndo(item.proposal!)}>撤销本次补丁</button>}</details>}
      {item.citations?.map((citation) => <a key={citation.citation_id} href={citation.source} target="_blank" rel="noreferrer">[{citation.citation_id}] {citation.title}</a>)}
    </div>)}
  </div>;
}

function patchStats(diff: string): { added: number; removed: number } {
  let added = 0; let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
    if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
  }
  return { added, removed };
}
