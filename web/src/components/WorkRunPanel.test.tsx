import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Artifact, type PlanRevision, type Run, type RuntimeEvent } from "../api/client";
import { WorkRunPanel } from "./WorkRunPanel";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  return { ...original, api: { artifacts: vi.fn(), runEvents: vi.fn(), plan: vi.fn(), qcReview: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn(), createHeadMotionQc: vi.fn(), approveQcReview: vi.fn(), run: vi.fn() } };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const run = (state: Run["state"] = "qc_review"): Run => ({ run_id: "run-1", project_id: "project", plan_revision_id: "plan", version: 4, attempt: 1, state, stage: "running", cancel_requested: false, qc_status: "awaiting_review", created_at: "2026-10-06T10:00:00Z", updated_at: "2026-10-06T10:09:00Z", qc_mode: "require", diagnosis_available: false, log_cursor: 0 });
const event = (id: number, type: string, time: string): RuntimeEvent => ({ event_id: id, event_type: type, trace_id: "trace", run_id: "run-1", payload: { stage: "running" }, severity: "info", created_at: time });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.artifacts).mockResolvedValue([]);
  vi.mocked(api.runEvents).mockResolvedValue([event(1, "RunStageStarted", "2026-10-06T10:01:00Z"), event(2, "RunStageFinished", "2026-10-06T10:07:20Z")]);
  vi.mocked(api.plan).mockResolvedValue({ plan_revision_id: "plan", plan: {} } as PlanRevision);
});

describe("Work run and results", () => {
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
    fireEvent.click(screen.getByText("metric.alff · ALFF.nii"));
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
