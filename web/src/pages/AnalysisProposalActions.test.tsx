import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, StatisticalAtlas, StatisticalDesign, Run } from "../api/types";
import { plansApi } from "../api/plans";
import { runsApi } from "../api/runs";
import { statisticsApi } from "../api/statistics";
import { AnalysisProposalActions } from "./AgentPage";

vi.mock("../api/plans", () => ({ plansApi: { approvePlan: vi.fn() } }));
vi.mock("../api/runs", () => ({ runsApi: { run: vi.fn() } }));
vi.mock("../api/statistics", () => ({ statisticsApi: { statisticalDesign: vi.fn(), createStatisticalDesign: vi.fn(),
  validateStatisticalDesign: vi.fn(), createStatisticsRun: vi.fn(), statisticalAtlases: vi.fn() } }));
const api = { ...plansApi, ...runsApi, ...statisticsApi };

const proposal = { project_id: "project-1", project_version: 1, input_manifest_hash: "b".repeat(64),
  proposal_hash: "a".repeat(64), design: { test: "independent_two_sample_t" }, correction: { method: "fdr", q_threshold: 0.05 } };
const conversation: Conversation = { conversation_id: "work-1", project_id: "project-1", mode: "work",
  title: "统计任务", workspace_path: "D:/demo-work", preferred_profile_id: null,
  active_run_id: "run-1", foreground_agent_role: "analysis", version: 4, created_at: "", updated_at: "",
  messages: [], tool_calls: [], work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash,
    proposal_message_id: "proposal-1", draft_hash: "c".repeat(64), plan_revision_id: "plan-1", run_id: "run-1" }] };
const design = { plan_revision: { plan_revision_id: "plan-1", version: 2, state: "awaiting_approval", plan_hash: "d".repeat(64) } } as StatisticalDesign;
const run = { run_id: "run-1", plan_revision_id: "plan-1", state: "succeeded" } as Run;

describe("server-bound statistical task actions", () => {
  beforeEach(() => { vi.resetAllMocks(); window.sessionStorage.clear(); vi.mocked(api.statisticalAtlases).mockResolvedValue([]); });
  afterEach(cleanup);

  it("restores the approved design and run from server references with no browser cache", async () => {
    vi.mocked(api.statisticalDesign).mockResolvedValue({ ...design, plan_revision: { ...design.plan_revision, state: "approved" } });
    vi.mocked(api.run).mockResolvedValue(run);
    const changed = vi.fn();
    render(<AnalysisProposalActions proposal={proposal} conversation={conversation} isCurrent onRunChange={changed} onTaskChange={vi.fn()} />);
    expect(await screen.findByText(/统计运行已创建/)).toHaveTextContent("run-1");
    expect(api.statisticalDesign).toHaveBeenCalledWith("plan-1");
    expect(api.run).toHaveBeenCalledWith("run-1");
    expect(changed).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "启动真实 MATLAB 统计" })).not.toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
  });

  it("keeps historical and other-role proposals read-only", () => {
    render(<AnalysisProposalActions proposal={proposal} conversation={conversation} isCurrent={false} onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    expect(screen.getByText(/历史统计预览或其他 Agent 的方案/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(api.statisticalDesign).not.toHaveBeenCalled();
  });

  it("creates a design bound to the exact conversation proposal and refreshes server task references", async () => {
    const empty: Conversation = { ...conversation, active_run_id: null, work_tasks: [{ ...conversation.work_tasks![0], plan_revision_id: null, run_id: null }] };
    vi.mocked(api.createStatisticalDesign).mockResolvedValue(design);
    vi.mocked(api.validateStatisticalDesign).mockResolvedValue(design);
    const refresh = vi.fn().mockResolvedValue(undefined);
    render(<AnalysisProposalActions proposal={proposal} conversation={empty} isCurrent onRunChange={vi.fn()} onTaskChange={refresh} />);
    fireEvent.click(await screen.findByRole("button", { name: "建立并校验正式统计设计" }));
    await waitFor(() => expect(api.createStatisticalDesign).toHaveBeenCalledWith(expect.objectContaining({
      work_context: { conversation_id: "work-1", proposal_hash: proposal.proposal_hash, proposal_message_id: "proposal-1" },
    })));
    expect(await screen.findByRole("button", { name: "批准统计设计" })).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("shows the server rejection when the current draft changes before approval", async () => {
    vi.mocked(api.statisticalDesign).mockResolvedValue(design);
    vi.mocked(api.approvePlan).mockRejectedValue(new Error("统计草稿已变化，请审查当前统计方案。"));
    render(<AnalysisProposalActions proposal={proposal} conversation={{ ...conversation, work_tasks: [{ ...conversation.work_tasks![0], run_id: null }] }} isCurrent onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "批准统计设计" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("统计草稿已变化");
    expect(api.createStatisticsRun).not.toHaveBeenCalled();
  });

  it("recommends available AAL and sends the user's atlas choice into the formal plan", async () => {
    const empty: Conversation = { ...conversation, active_run_id: null, work_tasks: [{ ...conversation.work_tasks![0], plan_revision_id: null, run_id: null }] };
    vi.mocked(api.statisticalAtlases).mockResolvedValue([
      { atlas_id: "aal", name: "AAL", available: true },
      { atlas_id: "aal3", name: "AAL3", available: true },
    ] as StatisticalAtlas[]);
    const selected = { ...design, localization_plan: { atlas_id: "aal3", atlas_name: "AAL3", atlas_content_hash: "e".repeat(64), coordinate_space: "MNI", localization_method: "nearest_neighbor_inverse_nifti_affine", cluster_connectivity: 26 } } as StatisticalDesign;
    vi.mocked(api.createStatisticalDesign).mockResolvedValue(selected);
    vi.mocked(api.validateStatisticalDesign).mockResolvedValue(selected);
    render(<AnalysisProposalActions proposal={proposal} conversation={empty} isCurrent onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    const selector = screen.getByRole("combobox", { name: "结果定位图谱" });
    await waitFor(() => expect(selector).toHaveValue("aal"));
    fireEvent.change(selector, { target: { value: "aal3" } });
    fireEvent.click(screen.getByRole("button", { name: "建立并校验正式统计设计" }));
    await waitFor(() => expect(api.createStatisticalDesign).toHaveBeenCalledWith(expect.objectContaining({ atlas_id: "aal3" })));
    expect(await screen.findByText(/方案图谱：AAL3 · MNI/)).toHaveTextContent("eeeeeeeeeeee");
    expect(screen.queryByRole("combobox", { name: "结果定位图谱" })).not.toBeInTheDocument();
  });

  it("permits statistics without an atlas when the catalog cannot be loaded", async () => {
    const empty: Conversation = { ...conversation, active_run_id: null, work_tasks: [{ ...conversation.work_tasks![0], plan_revision_id: null, run_id: null }] };
    vi.mocked(api.statisticalAtlases).mockRejectedValue(new Error("unavailable"));
    vi.mocked(api.createStatisticalDesign).mockResolvedValue(design);
    vi.mocked(api.validateStatisticalDesign).mockResolvedValue(design);
    render(<AnalysisProposalActions proposal={proposal} conversation={empty} isCurrent onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    await screen.findByText(/可先完成统计分析/);
    const button = screen.getByRole("button", { name: "建立并校验正式统计设计" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(api.createStatisticalDesign).toHaveBeenCalledWith(expect.objectContaining({ atlas_id: null })));
  });

  it("validates a restored draft before approval and creates a simulated run only after the explicit action", async () => {
    const draft = { ...design, plan_revision: { ...design.plan_revision, state: "draft" } } as StatisticalDesign;
    const approved = { ...design, plan_revision: { ...design.plan_revision, state: "approved", version: 4 } } as StatisticalDesign;
    vi.mocked(api.statisticalDesign).mockResolvedValueOnce(draft).mockResolvedValueOnce(approved);
    vi.mocked(api.validateStatisticalDesign).mockResolvedValue(design);
    vi.mocked(api.approvePlan).mockResolvedValue({} as Awaited<ReturnType<typeof api.approvePlan>>);
    vi.mocked(api.createStatisticsRun).mockResolvedValue(run);
    const changed = vi.fn();
    const refresh = vi.fn().mockResolvedValue(undefined);
    render(<AnalysisProposalActions proposal={proposal} conversation={{ ...conversation, work_tasks: [{ ...conversation.work_tasks![0], run_id: null }] }} isCurrent onRunChange={changed} onTaskChange={refresh} />);
    fireEvent.click(await screen.findByRole("button", { name: "校验统计设计" }));
    await waitFor(() => expect(api.validateStatisticalDesign).toHaveBeenCalledWith("plan-1", { expected_version: 2 }));
    fireEvent.click(await screen.findByRole("button", { name: "批准统计设计" }));
    await screen.findByRole("button", { name: "启动模拟统计" });
    expect(api.approvePlan).toHaveBeenCalledWith("plan-1", expect.objectContaining({ expected_version: 2, plan_hash: "d".repeat(64), decision: "approved" }));
    expect(api.createStatisticsRun).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "启动模拟统计" }));
    await waitFor(() => expect(api.createStatisticsRun).toHaveBeenCalledWith({ project_id: "project-1", statistical_design_revision_id: "plan-1", expected_plan_hash: "d".repeat(64),
      execution_backend: "mock", max_attempts: 1, real_execution_confirmed: false }));
    expect(changed).toHaveBeenCalledWith(run);
    expect(refresh).toHaveBeenCalledOnce();
    expect(await screen.findByText(/统计运行已创建/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "启动真实 MATLAB 统计" })).not.toBeInTheDocument();
  });

  it("sends a separate explicit real-execution confirmation and retains the approved plan after a server rejection", async () => {
    vi.mocked(api.statisticalDesign).mockResolvedValue({ ...design, plan_revision: { ...design.plan_revision, state: "approved" } });
    vi.mocked(api.createStatisticsRun).mockRejectedValueOnce(new Error("来源指标已变化，需建立新修订。"));
    const changed = vi.fn();
    render(<AnalysisProposalActions proposal={proposal} conversation={{ ...conversation, work_tasks: [{ ...conversation.work_tasks![0], run_id: null }] }} isCurrent onRunChange={changed} onTaskChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "启动真实 MATLAB 统计" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("来源指标已变化");
    expect(api.createStatisticsRun).toHaveBeenCalledWith(expect.objectContaining({ execution_backend: "matlab", real_execution_confirmed: true, expected_plan_hash: "d".repeat(64) }));
    expect(changed).not.toHaveBeenCalled();
    expect(screen.queryByText(/统计运行已创建/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "启动真实 MATLAB 统计" })).toBeEnabled();
  });

  it("removes confirmation controls on a role switch while a restored design is still loading", async () => {
    let resolve!: (value: StatisticalDesign) => void;
    vi.mocked(api.statisticalDesign).mockReturnValue(new Promise((done) => { resolve = done; }));
    vi.mocked(api.run).mockResolvedValue(run);
    const view = render(<AnalysisProposalActions proposal={proposal} conversation={conversation} isCurrent onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在恢复已建统计设计与运行…");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    view.rerender(<AnalysisProposalActions proposal={proposal} conversation={conversation} isCurrent={false} onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    resolve(design);
    await waitFor(() => expect(screen.getByText(/历史统计预览或其他 Agent 的方案/)).toBeInTheDocument());
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(api.approvePlan).not.toHaveBeenCalled();
    expect(api.createStatisticsRun).not.toHaveBeenCalled();
  });

  it("rejects a saved Run bound to another Plan instead of offering replacement work", async () => {
    vi.mocked(api.statisticalDesign).mockResolvedValue(design);
    vi.mocked(api.run).mockResolvedValue({ ...run, plan_revision_id: "old-plan" });
    render(<AnalysisProposalActions proposal={proposal} conversation={conversation} isCurrent onRunChange={vi.fn()} onTaskChange={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("关联不一致");
    expect(screen.getByRole("button", { name: "重新读取统计分析" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "批准统计设计" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "建立并校验正式统计设计" })).not.toBeInTheDocument();
    expect(api.approvePlan).not.toHaveBeenCalled();
    expect(api.createStatisticalDesign).not.toHaveBeenCalled();
    expect(api.createStatisticsRun).not.toHaveBeenCalled();
  });

  it("retains failed saved identities and retries reading both objects without mutations", async () => {
    vi.mocked(api.statisticalDesign).mockRejectedValueOnce(new Error("读取暂时中断")).mockResolvedValueOnce(design);
    vi.mocked(api.run).mockResolvedValue(run);
    const changed = vi.fn();
    render(<AnalysisProposalActions proposal={proposal} conversation={conversation} isCurrent onRunChange={changed} onTaskChange={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("读取暂时中断");
    expect(screen.queryByRole("button", { name: "建立并校验正式统计设计" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /启动.*统计/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新读取统计分析" }));
    expect(await screen.findByText(/统计运行已创建/)).toHaveTextContent("run-1");
    expect(api.statisticalDesign).toHaveBeenNthCalledWith(2, "plan-1");
    expect(api.run).toHaveBeenNthCalledWith(2, "run-1");
    expect(changed).not.toHaveBeenCalled();
    expect(api.createStatisticalDesign).not.toHaveBeenCalled();
    expect(api.approvePlan).not.toHaveBeenCalled();
    expect(api.createStatisticsRun).not.toHaveBeenCalled();
  });

  it("discards a late startup response after switching to a same-hash goal", async () => {
    const approved = { ...design, plan_revision: { ...design.plan_revision, state: "approved" } } as StatisticalDesign;
    const nextDesign = { ...approved, plan_revision: { ...approved.plan_revision, plan_revision_id: "next-plan" } };
    vi.mocked(api.statisticalDesign).mockResolvedValueOnce(approved).mockResolvedValueOnce(nextDesign);
    vi.mocked(api.run).mockResolvedValue({ ...run, run_id: "next-run", plan_revision_id: "next-plan" });
    let finishStartup!: (value: Run) => void;
    vi.mocked(api.createStatisticsRun).mockReturnValue(new Promise((resolve) => { finishStartup = resolve; }));
    const changed = vi.fn();
    const refresh = vi.fn();
    const initial = { ...conversation, work_tasks: [{ ...conversation.work_tasks![0], analysis_intent_task_id: "first-goal", run_id: null }] };
    const next = { ...conversation, work_tasks: [{ ...conversation.work_tasks![0], analysis_intent_task_id: "next-goal", plan_revision_id: "next-plan", run_id: "next-run" }] };
    const view = render(<AnalysisProposalActions proposal={proposal} conversation={initial} isCurrent onRunChange={changed} onTaskChange={refresh} />);
    fireEvent.click(await screen.findByRole("button", { name: "启动模拟统计" }));
    view.rerender(<AnalysisProposalActions proposal={proposal} conversation={next} isCurrent onRunChange={changed} onTaskChange={refresh} />);
    expect(await screen.findByText(/统计运行已创建/)).toHaveTextContent("next-run");
    await act(async () => { finishStartup(run); });
    expect(screen.getByText(/统计运行已创建/)).toHaveTextContent("next-run");
    expect(changed).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
