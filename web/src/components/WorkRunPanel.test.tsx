import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Artifact, type PlanRevision, type Run, type RunDiagnosis, type RuntimeEvent, type StatisticalAtlas, type StatisticalResultDetail } from "../api/client";
import { WorkRunPanel } from "./WorkRunPanel";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  return { ...original, api: { artifacts: vi.fn(), runEvents: vi.fn(), plan: vi.fn(), qcReview: vi.fn(), runQcReview: vi.fn(), diagnoseRun: vi.fn(), cancelRun: vi.fn(), retryRun: vi.fn(), createHeadMotionQc: vi.fn(), approveQcReview: vi.fn(), run: vi.fn(), statisticalResults: vi.fn(), statisticalResult: vi.fn(), statisticalResultSliceInfo: vi.fn(), localizeStatisticalResult: vi.fn(), statisticalAtlases: vi.fn() } };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const run = (state: Run["state"] = "qc_review"): Run => ({ run_id: "run-1", project_id: "project", plan_revision_id: "plan", version: 4, attempt: 1, state, stage: "running", cancel_requested: false, qc_status: "awaiting_review", created_at: "2026-10-06T10:00:00Z", updated_at: "2026-10-06T10:09:00Z", qc_mode: "require", diagnosis_available: false, log_cursor: 0 });
const event = (id: number, type: string, time: string): RuntimeEvent => ({ event_id: id, event_type: type, trace_id: "trace", run_id: "run-1", payload: { stage: "running" }, severity: "info", created_at: time });
const diagnosis: RunDiagnosis = { run_id: "run-1", diagnosis: { code: "input_format", severity: "error", summary: "T1 重定向与裁剪未完成", evidence: [], suggestions: ["检查本次转换工具日志与输入文件"], confidence: 0.6, requires_new_plan: true, source: "deterministic_log_classifier" } };
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.artifacts).mockResolvedValue([]);
  vi.mocked(api.runEvents).mockResolvedValue([event(1, "RunStageStarted", "2026-10-06T10:01:00Z"), event(2, "RunStageFinished", "2026-10-06T10:07:20Z")]);
  vi.mocked(api.plan).mockResolvedValue({ plan_revision_id: "plan", plan: {} } as PlanRevision);
  vi.mocked(api.runQcReview).mockResolvedValue(null);
  vi.mocked(api.diagnoseRun).mockResolvedValue(diagnosis);
  vi.mocked(api.statisticalResults).mockResolvedValue([]);
  vi.mocked(api.statisticalResult).mockRejectedValue(new Error("unexpected statistical result lookup"));
});

describe("Work run and results", () => {
  it("reads the frozen result atlas and prevents a different atlas from replacing the plan", async () => {
    const detail: StatisticalResultDetail = { result_id: "result-1", run_id: "run-1", project_id: "project", bundle_hash: "a".repeat(64), version: 1, non_scientific: false,
      artifact_count: 9, cluster_count: 1, created_at: "2026-10-10T09:00:00Z", design_revision_id: "plan", mode: "real", non_scientific_reason: null,
      manifest: { correction: { method: "fdr", q_threshold: 0.05 }, inference_status: "significant_voxels", significant_voxel_count: 8,
        clusters: [{ cluster_id: "cluster-1", extent_voxels: 8, peak_statistic: 12, peak_coordinate_mm: [0, 0, 0], coordinate_space: "MNI", inference: "fdr", direction: 1 }] },
      report_json: JSON.stringify({ localization_plan: { atlas_id: "aal3", atlas_name: "AAL3", atlas_content_hash: "b".repeat(64), coordinate_space: "MNI", cluster_connectivity: 26, localization_method: "nearest_neighbor_inverse_nifti_affine" } }), report_markdown: "",
    };
    vi.mocked(api.statisticalResults).mockResolvedValue([detail]);
    vi.mocked(api.statisticalResult).mockResolvedValue(detail);
    vi.mocked(api.statisticalResultSliceInfo).mockRejectedValue(new Error("slice not requested in this test"));
    vi.mocked(api.statisticalAtlases).mockResolvedValue([
      { atlas_id: "aal", name: "AAL", available: true }, { atlas_id: "aal3", name: "AAL3", available: true },
    ] as StatisticalAtlas[]);
    vi.mocked(api.localizeStatisticalResult).mockResolvedValue({ result_id: "result-1", result_bundle_hash: detail.bundle_hash, status: "localized", atlas_id: "aal3", atlas_name: "AAL3", localization_method: "nearest-neighbor sampling through inverse NIfTI affine", clusters: [] });
    render(<WorkRunPanel run={run("succeeded")} />);
    await screen.findByText("DPABI 图谱峰值定位");
    const selector = await screen.findByLabelText("图谱");
    expect(selector).toBeDisabled();
    expect(selector).toHaveValue("aal3");
    expect(screen.getByText(/变更图谱需另建方案/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("定位峰值"));
    await waitFor(() => expect(api.localizeStatisticalResult).toHaveBeenCalledWith("result-1", "aal3"));
  });
  it("shows a deterministic explanation and keeps the original stack collapsed with logs downloadable", async () => {
    const raw = "{���� assert\nT1 converter failed.\nrun_preprocessing>prepare_t1 (line 331)\nrsfmri:t1:ConverterFailed}";
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "log", artifact_type: "execution.matlab_log", relative_path: "logs/matlab.stderr.log", size_bytes: 400, provenance: {} } as Artifact]);
    render(<WorkRunPanel run={{ ...run("failed_terminal"), error: raw }} />);
    await screen.findByText(diagnosis.diagnosis.summary);
    expect(screen.getByRole("alert")).not.toHaveTextContent("assert");
    const technical = screen.getByText((_content, element) => element?.tagName === "PRE" && element.textContent === raw);
    expect(technical.closest("details")).not.toHaveAttribute("open");
    expect(technical).not.toBeVisible();
    expect(screen.getByRole("link", { name: "下载产物" })).toHaveAttribute("href", "/api/v1/artifacts/log/content");
    expect(api.diagnoseRun).toHaveBeenCalledWith("run-1", { log_text: raw }, expect.any(AbortSignal));
  });
  it("retains neutral failure guidance when diagnosis is unavailable", async () => {
    vi.mocked(api.diagnoseRun).mockRejectedValue(new Error("diagnosis unavailable"));
    render(<WorkRunPanel run={{ ...run("failed_terminal"), error: "internal stack" }} />);
    await waitFor(() => expect(api.diagnoseRun).toHaveBeenCalledOnce());
    expect(screen.getByRole("alert")).toHaveTextContent("具体原因尚未确定");
    expect(screen.getByRole("alert")).not.toHaveTextContent("internal stack");
    expect(screen.getByText("internal stack")).not.toBeVisible();
  });
  it("does not attach a late failure diagnosis to a subsequent successful run", async () => {
    let resolve!: (result: RunDiagnosis) => void;
    vi.mocked(api.diagnoseRun).mockReturnValue(new Promise((done) => { resolve = done; }));
    const view = render(<WorkRunPanel run={{ ...run("failed_terminal"), error: "failed" }} />);
    await waitFor(() => expect(api.diagnoseRun).toHaveBeenCalledOnce());
    view.rerender(<WorkRunPanel run={{ ...run("succeeded"), run_id: "run-2", error: "stale error" }} />);
    resolve(diagnosis);
    await screen.findByText("计算完成");
    expect(screen.queryByText(diagnosis.diagnosis.summary)).not.toBeInTheDocument();
    expect(screen.queryByText("stale error")).not.toBeInTheDocument();
    expect(api.diagnoseRun).toHaveBeenCalledOnce();
  });
  it.each(["failed_retryable", "failed_terminal", "timed_out", "cancelled"] as const)("keeps registered files clearly qualified after %s", async (state) => {
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "partial", artifact_type: "metric.alff", relative_path: "output/ALFF.nii", size_bytes: 400, provenance: {} } as Artifact]);
    render(<WorkRunPanel run={run(state)} />);
    await screen.findByText("本次执行未成功完成。以下为已登记文件，请核对处理是否完整及科研适用性。");
    expect(screen.getByRole("link", { name: "下载产物" })).toHaveAttribute("href", "/api/v1/artifacts/partial/content");
    expect(screen.queryByText("QC 审查已通过。")).not.toBeInTheDocument();
  });
  it("retains loaded results after a refresh fails but never carries them into another attempt", async () => {
    vi.mocked(api.artifacts).mockResolvedValue([{ artifact_id: "a1", artifact_type: "metric.alff", relative_path: "output/ALFF.nii", size_bytes: 400, provenance: {} } as Artifact]);
    vi.mocked(api.runQcReview).mockRejectedValue(new Error("review unavailable"));
    const view = render(<WorkRunPanel run={run()} />);
    await screen.findByText(/QC 审查记录暂时无法读取/);
    expect(screen.getByRole("link", { name: "下载产物" })).toBeInTheDocument();
    vi.mocked(api.artifacts).mockRejectedValue(new Error("artifacts unavailable"));
    vi.mocked(api.runEvents).mockRejectedValue(new Error("events unavailable"));
    vi.mocked(api.plan).mockRejectedValue(new Error("plan unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "重新加载详情" }));
    await screen.findByText(/产物列表暂时不可用/);
    expect(screen.getByRole("link", { name: "下载产物" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载对应方案" })).toBeInTheDocument();
    expect(screen.getByText("计算耗时：6 分 20 秒")).toBeInTheDocument();
    view.rerender(<WorkRunPanel run={{ ...run(), attempt: 2 }} />);
    await waitFor(() => expect(api.artifacts).toHaveBeenCalledTimes(3));
    expect(screen.queryByRole("link", { name: "下载产物" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "下载对应方案" })).not.toBeInTheDocument();
  });
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
  it("reports an empty corrected result as no significant differences without requesting localization", async () => {
    vi.mocked(api.statisticalResults).mockResolvedValue([{
      artifact_count: 9,
      bundle_hash: "a".repeat(64),
      cluster_count: 0,
      created_at: "2026-10-08T10:00:00Z",
      design_revision_id: "design-1",
      mode: "real",
      non_scientific: false,
      non_scientific_reason: null,
      project_id: "project",
      result_id: "result-1",
      run_id: "run-1",
      version: 1,
    }]);
    vi.mocked(api.statisticalResult).mockResolvedValue({
      artifact_count: 9,
      bundle_hash: "a".repeat(64),
      cluster_count: 0,
      created_at: "2026-10-08T10:00:00Z",
      design_revision_id: "design-1",
      manifest: {
        correction: { method: "fdr", q_threshold: 0.05 },
        clusters: [],
        inference_status: "no_significant_voxels",
        significant_voxel_count: 0,
      },
      mode: "real",
      non_scientific: false,
      non_scientific_reason: null,
      project_id: "project",
      report_json: "{}",
      report_markdown: "",
      result_id: "result-1",
      run_id: "run-1",
      version: 1,
    });

    render(<WorkRunPanel run={run("succeeded")} />);

    expect(await screen.findByText("本次检验未发现通过多重比较校正的显著差异体素（0 个）。没有显著簇或可定位脑区；空间定位不适用于这个空结果。"))
      .toBeInTheDocument();
    expect(screen.getByText("统计计算及结果登记已完成，请结合本次检验和校正阈值查看结果。")).toBeInTheDocument();
    expect(screen.queryByText("请查看 QC 记录确认审查结论。")).not.toBeInTheDocument();
    expect(screen.queryByText("DPABI 图谱峰值定位")).not.toBeInTheDocument();
    expect(api.localizeStatisticalResult).not.toHaveBeenCalled();
    expect(api.statisticalResultSliceInfo).not.toHaveBeenCalled();
  });
});
