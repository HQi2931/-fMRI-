import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Artifact, type PlanRevision, type Run, type RuntimeEvent } from "../api/client";
import { WorkRunPanel } from "./WorkRunPanel";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  return { ...original, api: { artifacts: vi.fn(), runEvents: vi.fn(), plan: vi.fn(), qcReview: vi.fn(), runQcReview: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn(), createHeadMotionQc: vi.fn(), approveQcReview: vi.fn(), run: vi.fn() } };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const run = (state: Run["state"] = "qc_review"): Run => ({ run_id: "run-1", project_id: "project", plan_revision_id: "plan", version: 4, attempt: 1, state, stage: "running", cancel_requested: false, qc_status: "awaiting_review", created_at: "2026-10-06T10:00:00Z", updated_at: "2026-10-06T10:09:00Z", qc_mode: "require", diagnosis_available: false, log_cursor: 0 });
const event = (id: number, type: string, time: string): RuntimeEvent => ({ event_id: id, event_type: type, trace_id: "trace", run_id: "run-1", payload: { stage: "running" }, severity: "info", created_at: time });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.artifacts).mockResolvedValue([]);
  vi.mocked(api.runEvents).mockResolvedValue([event(1, "RunStageStarted", "2026-10-06T10:01:00Z"), event(2, "RunStageFinished", "2026-10-06T10:07:20Z")]);
  vi.mocked(api.plan).mockResolvedValue({ plan_revision_id: "plan", plan: {} } as PlanRevision);
  vi.mocked(api.runQcReview).mockResolvedValue(null);
});

describe("Work run and results", () => {
  it.each([
    ["skipped", "QC 已明确跳过，请自行评估使用限制。"],
    ["approved", "QC 审查已通过。"],
    ["awaiting_review", "请查看 QC 记录确认审查结论。"],
  ] as const)("presents the actual %s QC decision after calculation", async (qcStatus, label) => {
    render(<WorkRunPanel run={{ ...run("succeeded"), qc_status: qcStatus }} />);
    await screen.findByText(label);
    if (qcStatus !== "approved") expect(screen.queryByText("QC 审查已通过。")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消计算" })).not.toBeInTheDocument();
  });
  it("keeps image results visible when only plan and phase records are unavailable", async () => {
    vi.mocked(api.plan).mockRejectedValue(new Error("plan unavailable"));
    vi.mocked(api.runEvents).mockRejectedValue(new Error("events unavailable"));
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "a1", artifact_type: "metric.alff", relative_path: "output/ALFF.nii", size_bytes: 400, provenance: {} } as Artifact]);
    render(<WorkRunPanel run={run()} />);
    await screen.findByText(/对应方案暂时不可用/);
    expect(screen.getByRole("img")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载产物" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "下载对应方案" })).not.toBeInTheDocument();
  });
  it("loads QC by authoritative run scope and keeps artifacts if the review request fails", async () => {
    vi.mocked(api.runEvents).mockResolvedValue([{ ...event(1, "QcReviewRevisionCreated", "2026-10-06T10:07:20Z"), payload: { review_revision_id: "823ac579-a[REDACTED-PHONE]c-5791bfa49be4" } }]);
    vi.mocked(api.runQcReview).mockRejectedValue(new Error("review unavailable"));
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "a1", artifact_type: "metric.alff", relative_path: "output/ALFF.nii", size_bytes: 400, provenance: {} } as Artifact]);
    render(<WorkRunPanel run={run()} />);
    await screen.findByText(/QC 审查记录暂时无法读取/);
    expect(screen.getByRole("link", { name: "下载产物" })).toHaveAttribute("href", "/api/v1/artifacts/a1/content");
    expect(api.runQcReview).toHaveBeenCalledWith("run-1", expect.any(AbortSignal));
    expect(api.qcReview).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "采用这些阈值生成审查" })).not.toBeInTheDocument();
    expect(api.createHeadMotionQc).not.toHaveBeenCalled();
    vi.mocked(api.runQcReview).mockResolvedValue(null);
    fireEvent.click(screen.getByRole("button", { name: "重新加载详情" }));
    expect(await screen.findByRole("button", { name: "采用这些阈值生成审查" })).toBeEnabled();
  });
  it("keeps registered artifacts available when only the QC report is broken", async () => {
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "qc-broken", run_id: "run-1", project_id: "project", artifact_type: "qc.head_motion_report", relative_path: "qc/report.json", size_bytes: 800, checksum: "a".repeat(64), created_at: "2026-10-06T10:07:20Z", provenance: {} }]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("broken", { status: 200 })));
    render(<WorkRunPanel run={run()} />);
    await screen.findByText(/自动 QC 报告暂时无法读取/);
    expect(screen.getByRole("link", { name: "下载产物" })).toHaveAttribute("href", "/api/v1/artifacts/qc-broken/content");
    expect(screen.getByText("指标计算已完成，科学质量审查尚未通过。")).toBeInTheDocument();
  });
  it("shows only observed phases from the current attempt without invented completion", async () => {
    vi.mocked(api.runEvents).mockResolvedValue([
      { ...event(1, "MatlabPhaseFinished", "2026-10-06T10:01:00Z"), payload: { phase: "segmentation", attempt: 0 } },
      { ...event(2, "MatlabPhaseFinished", "2026-10-06T10:02:00Z"), payload: { phase: "slice_timing", attempt: 1 } },
      { ...event(3, "MatlabPhaseStarted", "2026-10-06T10:03:00Z"), payload: { phase: "realignment", attempt: 1 } },
    ]);
    render(<WorkRunPanel run={run("running")} />);
    await screen.findByText("已观察到完成：层时间校正");
    expect(screen.getByRole("status")).toHaveTextContent("当前阶段：头动校正");
    expect(screen.queryByText(/T1 组织分割/)).not.toBeInTheDocument();
  });
  it("separates completed calculation from QC approval and uses execution events for elapsed time", async () => {
    render(<WorkRunPanel run={run()} />);
    expect(screen.getByText("计算完成 · 待 QC 审查")).toBeInTheDocument();
    expect(screen.getByText("指标计算已完成，科学质量审查尚未通过。")).toBeInTheDocument();
    await screen.findByText("计算耗时：6 分 20 秒");
    expect(screen.queryByText("QC 审查已通过。")).not.toBeInTheDocument();
    expect(screen.queryByText(/当前进度/)).not.toBeInTheDocument();
  });
  it("automatically shows registered images, integrity checked downloads and mock warnings", async () => {
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "a1", run_id: "run-1", project_id: "project", artifact_type: "metric.alff", relative_path: "output/ALFF.nii", size_bytes: 400, checksum: "a".repeat(64), created_at: "2026-10-06T10:07:20Z", provenance: { non_scientific: true } } as Artifact]);
    render(<WorkRunPanel run={run()} />);
    await screen.findByText(/Mock 演示结果/);
    fireEvent.click(screen.getByText("ALFF 指标图 · ALFF.nii"));
    expect(screen.getByRole("link", { name: "下载产物" })).toHaveAttribute("href", "/api/v1/artifacts/a1/content");
    const image = screen.getByRole("img");
    expect(image).toHaveAttribute("src", "/api/v1/artifacts/a1/preview.png");
    fireEvent.error(image);
    expect(screen.getByText("此图像无法预览，请下载文件检查。")).toBeInTheDocument();
  });
  it("sends an explicit cancel with the current run version and reports cancelling instead of cancelled", async () => {
    const updated = { ...run("running"), state: "cancelling", version: 5 } as Run;
    vi.mocked(api.cancelRun).mockResolvedValue(updated);
    const change = vi.fn();
    const view = render(<WorkRunPanel run={run("running")} onRunChange={change} />);
    fireEvent.click(screen.getByRole("button", { name: "取消计算" }));
    await waitFor(() => expect(change).toHaveBeenCalledWith(updated));
    expect(api.cancelRun).toHaveBeenCalledWith("run-1", { expected_version: 4, reason: "用户在运行卡片明确取消计算" });
    view.rerender(<WorkRunPanel run={updated} onRunChange={change} />);
    expect(screen.getByText("正在等待执行器确认取消。")).toBeInTheDocument();
    expect(screen.queryByText("计算已取消")).not.toBeInTheDocument();
  });
  it("retains the run and provides an explicit recovery action when details fail", async () => {
    vi.mocked(api.artifacts).mockRejectedValueOnce(new Error("network lost"));
    render(<WorkRunPanel run={run()} />);
    await screen.findByRole("alert");
    expect(screen.getByText("计算完成 · 待 QC 审查")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载详情" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(api.artifacts).toHaveBeenCalledTimes(2);
  });
  it("keeps a failed cancellation visible without claiming the running task has stopped", async () => {
    vi.mocked(api.cancelRun).mockRejectedValueOnce(new Error("版本已经变化，请刷新运行"));
    render(<WorkRunPanel run={run("running")} />);
    fireEvent.click(screen.getByRole("button", { name: "取消计算" }));
    await screen.findByText("版本已经变化，请刷新运行");
    expect(screen.getByRole("heading", { name: "计算中" })).toBeInTheDocument();
    expect(screen.queryByText("计算已取消")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消计算" })).toBeEnabled();
  });
  it("shows automatic QC measurements without treating them as human approval", async () => {
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "qc-report", run_id: "run-1", project_id: "project", artifact_type: "qc.head_motion_report", relative_path: "qc/report.json", size_bytes: 800, checksum: "a".repeat(64), created_at: "2026-10-06T10:07:20Z", provenance: {} }]);
    const fetchReport = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ subjects: [{ subject_id: "Sub_001", max_translation_mm: 0.7, max_rotation_deg: 0.4, mean_fd_power: 0.12, passed: true }] }) });
    vi.stubGlobal("fetch", fetchReport);
    render(<WorkRunPanel run={run()} />);
    await screen.findByText("自动头动 QC");
    expect(screen.getByText("0.120")).toBeInTheDocument();
    expect(screen.getByText("自动测量不代表人工科学审查已通过。")).toBeInTheDocument();
    expect(screen.queryByText("QC 审查已通过。")).not.toBeInTheDocument();
    expect(fetchReport).toHaveBeenCalledWith("/api/v1/artifacts/qc-report/content", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });
});
