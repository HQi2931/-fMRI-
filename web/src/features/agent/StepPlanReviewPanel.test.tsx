import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { agentApi } from "../../api/agent";
import type { StepPlanReviewDetail, StepPlanReviewSummary } from "../../api/types";
import { StepPlanReviewPanel } from "./StepPlanReviewPanel";

vi.mock("../../api/agent", () => ({ agentApi: {
  stepPlanReviews: vi.fn(),
  stepPlanReview: vi.fn(),
  approveStepPlan: vi.fn(),
  createStepRunAuthorization: vi.fn(),
  startStepRun: vi.fn(),
} }));

const summary = (overrides: Partial<StepPlanReviewSummary> = {}): StepPlanReviewSummary => ({
  conversation_id: "conversation-1",
  project_id: "project-1",
  task_id: "task-internal",
  task_version: 7,
  step_id: "step-internal",
  step_sequence: 1,
  step_version: 3,
  step_kind: "preprocessing_run",
  step_status: "ready",
  plan_revision_id: "plan-internal",
  plan_revision: 4,
  plan_version: 1,
  plan_hash: "a".repeat(64),
  plan_state: "awaiting_approval",
  execution_authorized: false,
  expected_output_count: 0,
  preprocessing: {
    tr_seconds: 2.5,
    expected_time_points: 100,
    dummy_scans: 0,
    detrend: true,
    slice_timing_enabled: true,
    slice_count: 25,
    reference_slice: 13,
    realignment_enabled: true,
    head_motion_model: 4,
    white_matter_regression_enabled: false,
    csf_regression_enabled: false,
    global_signal_regression_enabled: false,
    normalization_mode: 2,
    requested_metrics: ["alff", "falff"],
    frequency_low_hz: 0.01,
    frequency_high_hz: 0.08,
    mask_source: "dpabi_auto",
    requested_scalings: ["raw"],
    metric_smoothing: false,
  },
  ...overrides,
} as StepPlanReviewSummary);

const detail = (overrides: Partial<StepPlanReviewDetail> = {}): StepPlanReviewDetail => ({
  conversation_id: "conversation-1",
  project_id: "project-1",
  task_id: "task-internal",
  task_version: 7,
  step_id: "step-internal",
  step_sequence: 1,
  step_version: 3,
  plan_revision_id: "plan-internal",
  plan_revision: 4,
  plan_version: 1,
  plan_hash: "a".repeat(64),
  images: [
    { subject_id: "sub-01", group: "control", condition: null },
    { subject_id: "sub-02", group: "experimental", condition: null },
  ],
  covariates: [{ name: "age", centering: "grand_mean", values: [
    { subject_id: "sub-01", value: 32 }, { subject_id: "sub-02", value: 41 },
  ] }],
  ...overrides,
} as StepPlanReviewDetail);

const props = {
  conversationId: "conversation-1", projectId: "project-1", conversationVersion: 1, refreshWhileActive: false,
};
const emptyList = { items: [], truncated: false };

describe("step plan review panel", () => {
  beforeEach(() => vi.resetAllMocks());
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("renders the privacy-minimal server summary without per-subject details or local paths", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({ items: [summary()], truncated: false });
    render(<StepPlanReviewPanel {...props} />);

    expect(await screen.findByText("第 1 步：预处理与指标计算")).toBeInTheDocument();
    expect(screen.getByText("TR").parentElement).toHaveTextContent("2.5 秒");
    expect(screen.getByText("指标").parentElement).toHaveTextContent("ALFF、fALFF");
    expect(screen.getByText("指标频段").parentElement).toHaveTextContent("0.01–0.08 Hz");
    expect(screen.getByText("脑掩膜").parentElement).toHaveTextContent("DPABI AutoMask");
    expect(screen.getByRole("button", { name: "批准此步骤方案" })).toBeInTheDocument();
    expect(agentApi.stepPlanReview).not.toHaveBeenCalled();
    expect(screen.queryByText(/sub-01|artifact-private|participants\.tsv|task-internal|plan-internal/)).not.toBeInTheDocument();
  });

  it("reads exact statistical inclusion details only after an explicit user request", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({
      items: [summary({ step_kind: "statistical_run", preprocessing: null, statistical: {
        test: "independent_two_sample_t", group_count: 2, condition_count: 1, image_count: 2,
        covariate_count: 1, contrast: [1, -1], tail: "two_sided", missing_value_policy: "error",
      } })],
      truncated: false,
    });
    vi.mocked(agentApi.stepPlanReview).mockResolvedValue(detail());
    render(<StepPlanReviewPanel {...props} />);

    expect(await screen.findByText("第 1 步：统计分析")).toBeInTheDocument();
    expect(screen.queryByText("sub-01")).not.toBeInTheDocument();
    expect(agentApi.stepPlanReview).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "查看精确纳入明细" }));
    expect(await screen.findByText(/以下明细仅在用户主动展开后从本机读取/)).toBeInTheDocument();
    expect(screen.getAllByText("sub-01")).toHaveLength(2);
    expect(screen.queryByText(/artifact-private/)).not.toBeInTheDocument();
    expect(agentApi.stepPlanReview).toHaveBeenCalledWith("conversation-1", "task-internal", "step-internal");
  });

  it("fails closed on snapshots scoped to another conversation or project", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({ items: [
      summary({ project_id: "other-project" }),
      summary({ conversation_id: "other-conversation" }),
    ], truncated: false });
    render(<StepPlanReviewPanel {...props} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("不匹配");
    expect(screen.queryByText("第 1 步：预处理与指标计算")).not.toBeInTheDocument();
  });

  it("shows API failures and allows a manual retry", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockRejectedValueOnce(new Error("连接失败"))
      .mockResolvedValueOnce(emptyList);
    render(<StepPlanReviewPanel {...props} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("连接失败");
    fireEvent.click(screen.getByRole("button", { name: "刷新方案" }));
    await waitFor(() => expect(screen.getByText("当前会话没有等待审查的多步骤方案。"))
      .toBeInTheDocument());
    expect(agentApi.stepPlanReviews).toHaveBeenCalledTimes(2);
  });

  it("detects Worker updates while the conversation version stays unchanged", async () => {
    vi.useFakeTimers();
    vi.mocked(agentApi.stepPlanReviews)
      .mockResolvedValueOnce(emptyList)
      .mockResolvedValueOnce({ items: [summary()], truncated: false });
    render(<StepPlanReviewPanel {...props} refreshWhileActive />);

    await act(async () => { await Promise.resolve(); });
    expect(agentApi.stepPlanReviews).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByText("第 1 步：预处理与指标计算")).toBeInTheDocument();
    expect(agentApi.stepPlanReviews).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale response after the active conversation version changes", async () => {
    let resolveFirst: ((value: { items: StepPlanReviewSummary[]; truncated: boolean }) => void) | undefined;
    vi.mocked(agentApi.stepPlanReviews)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ items: [summary()], truncated: false });
    const view = render(<StepPlanReviewPanel {...props} />);
    view.rerender(<StepPlanReviewPanel {...props} conversationVersion={2} />);

    expect(await screen.findByText("第 1 步：预处理与指标计算")).toBeInTheDocument();
    resolveFirst?.({ items: [summary({ plan_hash: "b".repeat(64) })], truncated: false });
    await waitFor(() => expect(screen.getByText(/方案版本 4 · 内容校验码 a{12}…/)).toBeInTheDocument());
  });

  it("warns when the local candidate scan reaches its display limit", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({ items: [summary()], truncated: true });
    render(<StepPlanReviewPanel {...props} />);

    expect(await screen.findByText(/超过 100 项/)).toBeInTheDocument();
  });

  it("approves only the exact task, step, and plan versions shown in the review", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({ items: [summary()], truncated: false });
    vi.mocked(agentApi.approveStepPlan).mockResolvedValue({
      conversation_id: "conversation-1", project_id: "project-1", task_id: "task-internal",
      step_id: "step-internal", plan_revision_id: "plan-internal", plan_hash: "a".repeat(64),
      approval: { approval_id: "approval-1", plan_revision_id: "plan-internal", plan_hash: "a".repeat(64),
        actor: "用户", decision: "approved", reason: "reviewed", created_at: "2026-10-21T00:00:00Z" },
    } as never);
    render(<StepPlanReviewPanel {...props} />);

    fireEvent.click(await screen.findByRole("button", { name: "批准此步骤方案" }));
    await waitFor(() => expect(agentApi.approveStepPlan).toHaveBeenCalledWith(
      "conversation-1", "task-internal", "step-internal", {
        task_version: 7, step_version: 3, plan_revision_id: "plan-internal", plan_version: 1,
        plan_hash: "a".repeat(64), actor: "用户",
        reason: "用户在步骤级方案审查面板中明确批准此冻结 Plan。",
      },
    ));
  });

  it("requires a separate one-time authorization before creating a Mock Run", async () => {
    const approved = summary({ plan_state: "approved" });
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({ items: [approved], truncated: false });
    vi.mocked(agentApi.createStepRunAuthorization).mockResolvedValue({
      conversation_id: "conversation-1", project_id: "project-1", task_id: "task-internal",
      step_id: "step-internal", plan_revision_id: "plan-internal", plan_hash: "a".repeat(64),
      confirmation_id: "confirmation-1", expires_at: "2026-10-21T00:05:00Z",
    } as never);
    vi.mocked(agentApi.startStepRun).mockResolvedValue({
      conversation_id: "conversation-1", project_id: "project-1", task_id: "task-internal",
      step_id: "step-internal", plan_revision_id: "plan-internal",
      run: { run_id: "run-1", project_id: "project-1", plan_revision_id: "plan-internal", state: "queued",
        version: 1, attempt: 1, cancel_requested: false, error: null, stage: "queued", stage_progress: null,
        heartbeat: null, log_cursor: 0, eta_seconds: null, diagnosis_available: false,
        qc_mode: "require", qc_status: "pending", created_at: "2026-10-21T00:00:00Z",
        updated_at: "2026-10-21T00:00:00Z" },
    } as never);
    render(<StepPlanReviewPanel {...props} />);

    fireEvent.click(await screen.findByRole("button", { name: "签发此步骤的一次性启动授权" }));
    await waitFor(() => expect(agentApi.createStepRunAuthorization).toHaveBeenCalledWith(
      "conversation-1", "task-internal", "step-internal", {
        task_version: 7, step_version: 3, plan_revision_id: "plan-internal", plan_version: 1,
        plan_hash: "a".repeat(64), execution_backend: "mock", workspace_mode: "isolated",
        real_execution_confirmed: false, qc_mode: "require",
      },
    ));
    expect(agentApi.startStepRun).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "确认并创建 Run" }));
    await waitFor(() => expect(agentApi.startStepRun).toHaveBeenCalledWith(
      "conversation-1", "task-internal", "step-internal", {
        task_version: 7, step_version: 3, plan_revision_id: "plan-internal", plan_version: 1,
        plan_hash: "a".repeat(64), confirmation_id: "confirmation-1", execution_backend: "mock",
        workspace_mode: "isolated", real_execution_confirmed: false, qc_mode: "require",
        max_attempts: 1, mock_outcome: "succeed", mock_delay_ms: 0,
      },
    ));
  });

  it("requires explicit real-execution confirmation for a MATLAB statistical step", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({
      items: [summary({
        step_kind: "statistical_run", plan_state: "approved", preprocessing: null, statistical: null,
      })],
      truncated: false,
    });
    vi.mocked(agentApi.createStepRunAuthorization).mockResolvedValue({
      conversation_id: "conversation-1", project_id: "project-1", task_id: "task-internal",
      step_id: "step-internal", plan_revision_id: "plan-internal", plan_hash: "a".repeat(64),
      confirmation_id: "confirmation-statistics", expires_at: "2026-10-21T00:05:00Z",
    } as never);
    render(<StepPlanReviewPanel {...props} />);

    fireEvent.change(await screen.findByLabelText("执行后端"), { target: { value: "matlab" } });
    const confirmation = screen.getByRole("checkbox", { name: "我明确授权启动本机 MATLAB / DPABI 真实计算。" });
    expect(confirmation).not.toBeChecked();
    const issueButton = screen.getByRole("button", { name: "签发此步骤的一次性启动授权" });
    expect(issueButton).toBeDisabled();

    fireEvent.click(confirmation);
    expect(issueButton).toBeEnabled();
    fireEvent.click(issueButton);
    await waitFor(() => expect(agentApi.createStepRunAuthorization).toHaveBeenCalledWith(
      "conversation-1", "task-internal", "step-internal", {
        task_version: 7, step_version: 3, plan_revision_id: "plan-internal", plan_version: 1,
        plan_hash: "a".repeat(64), execution_backend: "matlab", workspace_mode: "isolated",
        real_execution_confirmed: true, qc_mode: "require",
      },
    ));
  });

  it("clears authorization and resets real-execution and workspace choices when switching backends", async () => {
    vi.mocked(agentApi.stepPlanReviews).mockResolvedValue({
      items: [summary({ plan_state: "approved" })], truncated: false,
    });
    vi.mocked(agentApi.createStepRunAuthorization).mockImplementation(async (_conversation, _task, _step, body) => ({
      conversation_id: "conversation-1", project_id: "project-1", task_id: "task-internal",
      step_id: "step-internal", plan_revision_id: "plan-internal", plan_hash: "a".repeat(64),
      confirmation_id: `confirmation-${body.execution_backend}-${body.workspace_mode}`,
      expires_at: "2026-10-21T00:05:00Z",
    } as never));
    render(<StepPlanReviewPanel {...props} />);

    const backend = await screen.findByLabelText("执行后端");
    fireEvent.change(backend, { target: { value: "matlab" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "我明确授权启动本机 MATLAB / DPABI 真实计算。" }));
    fireEvent.change(screen.getByLabelText("工作区模式"), { target: { value: "in_place" } });
    fireEvent.click(screen.getByRole("button", { name: "签发此步骤的一次性启动授权" }));
    expect(await screen.findByRole("button", { name: "确认并创建 Run" })).toBeInTheDocument();

    fireEvent.change(backend, { target: { value: "mock" } });
    expect(screen.queryByRole("button", { name: "确认并创建 Run" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "签发此步骤的一次性启动授权" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "签发此步骤的一次性启动授权" }));
    await waitFor(() => expect(agentApi.createStepRunAuthorization).toHaveBeenLastCalledWith(
      "conversation-1", "task-internal", "step-internal", {
        task_version: 7, step_version: 3, plan_revision_id: "plan-internal", plan_version: 1,
        plan_hash: "a".repeat(64), execution_backend: "mock", workspace_mode: "isolated",
        real_execution_confirmed: false, qc_mode: "require",
      },
    ));

    fireEvent.change(backend, { target: { value: "matlab" } });
    expect(screen.getByRole("checkbox", { name: "我明确授权启动本机 MATLAB / DPABI 真实计算。" })).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "确认并创建 Run" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "签发此步骤的一次性启动授权" })).toBeDisabled();
  });
});
