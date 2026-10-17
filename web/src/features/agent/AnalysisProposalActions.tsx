import { useEffect, useState } from "react";

import { describeError } from "../../api/transport";
import type { Conversation, Run, StatisticalDesign, StatisticalDesignCreateBody } from "../../api/types";
import { plansApi } from "../../api/plans";
import { runsApi } from "../../api/runs";
import { statisticsApi } from "../../api/statistics";
import { type Message } from "./conversation-model";

const api = { ...plansApi, ...runsApi, ...statisticsApi };

export function AnalysisProposalActions({ proposal, conversation, isCurrent, onTaskChange, onRunChange }: { proposal: NonNullable<Message["statisticalProposal"]>; conversation: Conversation; isCurrent: boolean; onTaskChange: () => Promise<void>; onRunChange: (run: Run) => void }) {
  const [atlasChoices, setAtlasChoices] = useState<Array<{ atlas_id: "aal" | "aal3" | "brodmann"; name: string }>>([]);
  const [atlasId, setAtlasId] = useState<"aal" | "aal3" | "brodmann" | "">("");
  const [atlasLoading, setAtlasLoading] = useState(true);
  const [atlasError, setAtlasError] = useState("");
  const [design, setDesign] = useState<StatisticalDesign | null>(null);
  const [statisticsRun, setStatisticsRun] = useState<Run | null>(null);
  const [recovering, setRecovering] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const task = conversation.work_tasks?.find((item) => item.agent_role === "analysis" && item.proposal_hash === proposal.proposal_hash);
  const planId = task?.plan_revision_id;
  const runId = task?.run_id;

  useEffect(() => {
    if (!isCurrent || planId) return;
    const controller = new AbortController();
    void api.statisticalAtlases(controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      const available = items.filter((item) => item.available && ["aal", "aal3", "brodmann"].includes(item.atlas_id)) as Array<{ atlas_id: "aal" | "aal3" | "brodmann"; name: string }>;
      setAtlasChoices(available);
      if (available.some((item) => item.atlas_id === "aal")) setAtlasId("aal");
    }).catch((caught) => {
      if (!controller.signal.aborted) setAtlasError(`图谱列表暂不可用，可先完成统计分析：${describeError(caught)}`);
    }).finally(() => { if (!controller.signal.aborted) setAtlasLoading(false); });
    return () => controller.abort();
  }, [isCurrent, planId]);

  useEffect(() => {
    if (!isCurrent || !planId) { setRecovering(false); return; }
    let current = true;
    setRecovering(true);
    void Promise.all([api.statisticalDesign(planId), runId ? api.run(runId) : Promise.resolve(null)])
      .then(([value, run]) => {
        if (!current) return;
        setDesign(value);
        setStatisticsRun(run?.plan_revision_id === planId ? run : null);
      })
      .catch((caught) => { if (current) setError(describeError(caught)); })
      .finally(() => { if (current) setRecovering(false); });
    return () => { current = false; };
  }, [isCurrent, planId, runId]);

  async function createDesign(): Promise<void> {
    if (!proposal.project_id || !proposal.project_version || !proposal.input_manifest_hash || !proposal.proposal_hash) {
      setError("统计预览缺少项目版本或输入清单哈希，无法创建正式设计。");
      return;
    }
    setBusy(true); setError("");
    try {
      const workContext: NonNullable<StatisticalDesignCreateBody["work_context"]> = { conversation_id: conversation.conversation_id, proposal_hash: proposal.proposal_hash, proposal_message_id: task?.proposal_message_id ?? null };
      const body = {
        project_id: proposal.project_id,
        expected_project_version: proposal.project_version,
        input_manifest_hash: proposal.input_manifest_hash,
        design: proposal.design,
        correction: proposal.correction ?? null,
        atlas_id: atlasId || null,
        work_context: workContext,
      } as StatisticalDesignCreateBody;
      const created = await api.createStatisticalDesign(body);
      setDesign(created);
      await onTaskChange();
      const validated = await api.validateStatisticalDesign(created.plan_revision.plan_revision_id, { expected_version: created.plan_revision.version });
      setDesign(validated);
    } catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  async function validateDesign(): Promise<void> {
    if (!design) return;
    setBusy(true); setError("");
    try { setDesign(await api.validateStatisticalDesign(design.plan_revision.plan_revision_id, { expected_version: design.plan_revision.version })); }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  async function approveDesign(): Promise<void> {
    if (!design) return;
    setBusy(true); setError("");
    try {
      await api.approvePlan(design.plan_revision.plan_revision_id, {
        expected_version: design.plan_revision.version,
        plan_hash: design.plan_revision.plan_hash,
        actor: "Work 用户",
        decision: "approved",
        reason: "用户在 Work 界面审阅并批准统计设计",
      });
      setDesign(await api.statisticalDesign(design.plan_revision.plan_revision_id));
    } catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  async function startRun(executionBackend: "mock" | "matlab"): Promise<void> {
    if (!design || !proposal.project_id) return;
    setBusy(true); setError("");
    try {
      const run = await api.createStatisticsRun({
        project_id: proposal.project_id,
        statistical_design_revision_id: design.plan_revision.plan_revision_id,
        expected_plan_hash: design.plan_revision.plan_hash,
        execution_backend: executionBackend,
        max_attempts: 1,
        real_execution_confirmed: executionBackend === "matlab",
      });
      setStatisticsRun(run);
      onRunChange(run);
      await onTaskChange();
    } catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  }

  const state = design?.plan_revision.state;
  if (!isCurrent) return <p>历史统计预览或其他 Agent 的方案，仅供查看。请在当前数据分析方案中确认。</p>;
  return <div className="analysis-proposal-actions">
    {!design && !recovering && <>
      <label>结果定位图谱 <select disabled={busy || atlasLoading} value={atlasId} onChange={(event) => setAtlasId(event.target.value as typeof atlasId)}>
        <option value="">暂不选择图谱</option>
        {atlasChoices.map((atlas) => <option key={atlas.atlas_id} value={atlas.atlas_id}>{atlas.name}{atlas.atlas_id === "aal" ? "（建议）" : ""}</option>)}
      </select></label>
      <p>所选图谱及其版本将写入正式方案；定位仍需影像与图谱空间一致。</p>
      {atlasError && <p>{atlasError}</p>}
    </>}
    {!design && <button className="button button-secondary" type="button" disabled={busy || recovering || atlasLoading} onClick={() => void createDesign()}>{busy ? "正在建立并校验…" : recovering ? "正在恢复已建统计设计…" : "建立并校验正式统计设计"}</button>}
    {design && <>
      <p>正式设计：<code>{design.plan_revision.plan_revision_id}</code> · 状态：{state}</p>
      <p>方案图谱：{design.localization_plan ? `${design.localization_plan.atlas_name} · ${design.localization_plan.coordinate_space} · 版本 ${design.localization_plan.atlas_content_hash.slice(0, 12)}` : "未指定；历史结果可另行选择图谱查看"}</p>
      {state === "draft" && <button className="button button-secondary" type="button" disabled={busy} onClick={() => void validateDesign()}>校验统计设计</button>}
      {state === "awaiting_approval" && <button className="button button-secondary" type="button" disabled={busy} onClick={() => void approveDesign()}>{busy ? "正在审批…" : "批准统计设计"}</button>}
      {state === "approved" && !statisticsRun && <div className="analysis-run-actions">
        <button className="button button-secondary" type="button" disabled={busy || recovering} onClick={() => void startRun("mock")}>启动模拟统计</button>
        <button className="button button-primary" type="button" disabled={busy || recovering} onClick={() => void startRun("matlab")}>启动真实 MATLAB 统计</button>
      </div>}
      {statisticsRun && <p>统计运行已创建：<code>{statisticsRun.run_id}</code> · {statisticsRun.state}</p>}
    </>}
    {error && <p className="analysis-action-error" role="alert">{error}</p>}
  </div>;
}
