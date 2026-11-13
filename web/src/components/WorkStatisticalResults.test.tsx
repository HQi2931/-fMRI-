import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { plansApi } from "../api/plans";
import { runsApi } from "../api/runs";
import { statisticsApi } from "../api/statistics";
import type { Artifact, PlanRevision, Run, StatisticalAtlas, StatisticalResultDetail, StatisticalResultLocalization } from "../api/types";
import { WorkRunPanel } from "./WorkRunPanel";

vi.mock("../api/plans", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/plans")>();
  return { ...original, plansApi: { ...original.plansApi, plan: vi.fn() } };
});
vi.mock("../api/runs", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/runs")>();
  return { ...original, runsApi: { ...original.runsApi, artifacts: vi.fn(), runEvents: vi.fn(), runQcReview: vi.fn() } };
});
vi.mock("../api/statistics", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/statistics")>();
  return { ...original, statisticsApi: { ...original.statisticsApi, statisticalResults: vi.fn(), statisticalResult: vi.fn(),
    statisticalResultSliceInfo: vi.fn(), statisticalAtlases: vi.fn(), localizeStatisticalResult: vi.fn(), statisticalClusterRoi: vi.fn() } };
});

const api = { ...plansApi, ...runsApi, ...statisticsApi };

const run: Run = { run_id: "run-current", project_id: "project", plan_revision_id: "plan", version: 3, attempt: 1,
  state: "succeeded", stage: "running", cancel_requested: false, qc_status: "skipped", qc_mode: "skip",
  created_at: "2026-10-10T09:00:00Z", updated_at: "2026-10-10T09:10:00Z", diagnosis_available: false, log_cursor: 0 };
const result: StatisticalResultDetail = {
  result_id: "result-current", run_id: run.run_id, project_id: "project", design_revision_id: "plan", bundle_hash: "a".repeat(64),
  artifact_count: 9, cluster_count: 2, created_at: run.created_at, mode: "real", non_scientific: false, non_scientific_reason: null, version: 1,
  manifest: { correction: { method: "fdr", q_threshold: 0.05 }, inference_status: "significant_voxels", significant_voxel_count: 10,
    clusters: [{ cluster_id: "positive", extent_voxels: 8, peak_statistic: 4.1, peak_coordinate_mm: [0, 2, 6], coordinate_space: "MNI", inference: "fdr", direction: 1, source_map_artifact_id: "t-map" },
      { cluster_id: "negative", extent_voxels: 2, peak_statistic: -3.2, peak_coordinate_mm: [-2, 0, 12], coordinate_space: "MNI", inference: "fdr", direction: -1, source_map_artifact_id: "t-map" }] },
  report_json: JSON.stringify({ methods: "frozen design" }), report_markdown: "# 两样本 t 检验\nFDR q=0.05；ASD-HC。",
};
const localization: StatisticalResultLocalization = {
  result_id: result.result_id, result_bundle_hash: result.bundle_hash, status: "localized", atlas_id: "aal", atlas_name: "AAL",
  atlas_content_hash: "b".repeat(64), coordinate_space: "MNI", localization_method: "nearest_neighbor_inverse_nifti_affine", significant_voxel_count: 10,
  clusters: [{ cluster_id: "positive", peak_coordinate_mm: [0, 2, 6], label: "Demo region", label_value: 7,
    atlas_voxel_ijk: [0, 1, 3], atlas_voxel_center_mm: [0, 2, 6], distance_to_voxel_center_mm: 0,
    coverage: [{ label_value: 7, label: "Demo region", voxel_count: 6, proportion: 0.75 }, { label_value: 0, label: "背景", voxel_count: 2, proportion: 0.25 }] }],
};
const atlas = (id: string, available = true) => ({ atlas_id: id, name: id.toUpperCase(), available } as StatisticalAtlas);
const dataContent = (link: HTMLElement) => decodeURIComponent(link.getAttribute("href")!.split(",").slice(1).join(","));

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.artifacts).mockResolvedValue([]);
  vi.mocked(api.runEvents).mockResolvedValue([]);
  vi.mocked(api.plan).mockResolvedValue({ plan_revision_id: "plan", plan: {} } as PlanRevision);
  vi.mocked(api.runQcReview).mockResolvedValue(null);
  vi.mocked(api.statisticalResults).mockResolvedValue([result]);
  vi.mocked(api.statisticalResult).mockResolvedValue(result);
  vi.mocked(api.statisticalResultSliceInfo).mockResolvedValue({ result_id: result.result_id, map_artifact_id: "t-map", slice_count: 10, initial_slice_index: 1,
    cluster_slices: [{ cluster_id: "positive", slice_index: 3 }, { cluster_id: "negative", slice_index: 6 }] });
  vi.mocked(api.statisticalAtlases).mockResolvedValue([atlas("aal"), atlas("aal3")]);
  vi.mocked(api.localizeStatisticalResult).mockResolvedValue(localization);
  vi.mocked(api.statisticalClusterRoi).mockResolvedValue({ result_id: result.result_id, result_bundle_hash: result.bundle_hash,
    cluster_id: "negative", status: "descriptive", voxel_count: 2, source_map_artifact_id: "t-map", limitation: "同一数据的描述，不能作为独立验证。",
    rows: [{ subject_id: "demo-01", group: "ASD", mean_value: 2 }, { subject_id: "demo-02", group: "HC", mean_value: 3 }] });
});
afterEach(cleanup);

describe("Work statistical result interactions", () => {
  it("links signed cluster selection to peak slices and ROI, then allows adjacent slices and recovery from image failure", async () => {
    render(<WorkRunPanel run={run} />);
    expect(await screen.findByRole("img", { name: "统计图第 2 层轴向切片" })).toHaveAttribute("src", "/api/v1/statistics/results/result-current/slice.png?slice_index=1");
    expect(screen.getByText("正向")).toBeInTheDocument();
    expect(screen.getByText("负向")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "簇 negative" }));
    expect(screen.getByRole("slider", { name: "统计图轴向切片" })).toHaveValue("6");
    expect(screen.getByRole("img", { name: "统计图第 7 层轴向切片" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    await screen.findByText("demo-01");
    expect(api.statisticalClusterRoi).toHaveBeenCalledWith(result.result_id, "negative");
    fireEvent.error(screen.getByRole("img", { name: "统计图第 7 层轴向切片" }));
    expect(screen.getByText("此统计图切片无法预览，请下载 NIfTI 文件检查。")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider", { name: "统计图轴向切片" }), { target: { value: "7" } });
    expect(screen.getByRole("img", { name: "统计图第 8 层轴向切片" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载当前切片 PNG" })).toHaveAttribute("download", "statistics-result-current-slice-8.png");
    fireEvent.click(screen.getByRole("button", { name: "簇 positive" }));
    expect(screen.queryByText("demo-01")).not.toBeInTheDocument();
    expect(screen.getByRole("slider")).toHaveValue("3");
  });

  it("exports registered NIfTI and scripts plus frozen reports and signed cluster tables", async () => {
    vi.mocked(api.artifacts).mockResolvedValue([
      { artifact_id: "t-map", artifact_type: "statistics.statistic_map", relative_path: "output/TMap.nii", size_bytes: 100, provenance: {} },
      { artifact_id: "script", artifact_type: "statistics.script", relative_path: "scripts/run_statistics.m", size_bytes: 150, provenance: {} },
    ] as Artifact[]);
    render(<WorkRunPanel run={run} />);
    await screen.findByText("统计簇表");
    const files = screen.getAllByRole("link", { name: "下载产物" });
    expect(files.map((link) => link.getAttribute("href"))).toEqual(["/api/v1/artifacts/t-map/content", "/api/v1/artifacts/script/content"]);
    expect(dataContent(screen.getByRole("link", { name: "下载 Markdown 报告" }))).toBe(result.report_markdown);
    expect(JSON.parse(dataContent(screen.getByRole("link", { name: "下载 JSON 报告" })))).toEqual({ methods: "frozen design" });
    const csv = dataContent(screen.getByRole("link", { name: "下载簇表 CSV" }));
    expect(csv).toContain('"negative","2","-3.2","-2","0","12","MNI","fdr","-1","t-map"');
    expect(dataContent(screen.getByRole("link", { name: "下载簇表 TSV" }))).toContain("positive\t8\t4.1\t0\t2\t6\tMNI\tfdr\t1\tt-map");
  });

  it("shows peak labels and whole-cluster coverage and clears old atlas results when a legacy result changes atlas", async () => {
    render(<WorkRunPanel run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "定位峰值" }));
    await screen.findByRole("link", { name: "下载脑区覆盖表" });
    expect(screen.getByText("75.0%")).toBeInTheDocument();
    expect(screen.getByText("25.0%")).toBeInTheDocument();
    expect(dataContent(screen.getByRole("link", { name: "下载脑区覆盖表" }))).toContain("positive\taal\t7\tDemo region\t6\t0.75");
    expect(JSON.parse(dataContent(screen.getByRole("link", { name: "下载定位及图谱来源记录" })))).toEqual(localization);
    expect(screen.getByText(/FDR 簇按空间汇总/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("图谱"), { target: { value: "aal3" } });
    expect(screen.queryByRole("link", { name: "下载脑区覆盖表" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "定位峰值" }));
    await waitFor(() => expect(api.localizeStatisticalResult).toHaveBeenLastCalledWith(result.result_id, "aal3"));
  });

  it("keeps known-space conflicts visible and permits a read-only localization retry", async () => {
    vi.mocked(api.localizeStatisticalResult).mockRejectedValueOnce(new Error("已确认原生空间与 MNI 图谱不同"));
    render(<WorkRunPanel run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "定位峰值" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("已确认原生空间与 MNI 图谱不同");
    expect(screen.getByRole("alert")).not.toHaveTextContent("未知空间");
    expect(screen.getByText("校正后保留 10 个显著体素，分布于 2 个簇。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "定位峰值" }));
    await screen.findByRole("link", { name: "下载脑区覆盖表" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("hides localization from a stale bundle and disables duplicate requests while locating", async () => {
    let resolve!: (value: StatisticalResultLocalization) => void;
    vi.mocked(api.localizeStatisticalResult).mockReturnValue(new Promise((done) => { resolve = done; }));
    render(<WorkRunPanel run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "定位峰值" }));
    expect(screen.getByRole("button", { name: "定位中…" })).toBeDisabled();
    resolve({ ...localization, result_bundle_hash: "c".repeat(64) });
    await screen.findByRole("button", { name: "定位峰值" });
    expect(screen.queryByRole("link", { name: "下载脑区覆盖表" })).not.toBeInTheDocument();
    expect(api.localizeStatisticalResult).toHaveBeenCalledOnce();
  });

  it("preserves cluster exports when slices and the atlas catalog are unavailable", async () => {
    vi.mocked(api.statisticalResultSliceInfo).mockRejectedValue(new Error("切片源文件已变化"));
    vi.mocked(api.statisticalAtlases).mockRejectedValue(new Error("图谱不可读取"));
    render(<WorkRunPanel run={run} />);
    await screen.findByText(/统计图切片信息不可用：切片源文件已变化/);
    await screen.findByText(/DPABI 图谱列表暂时不可用：图谱不可读取/);
    expect(screen.getByRole("link", { name: "下载簇表 TSV" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "定位峰值" })).not.toBeInTheDocument();
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(api.localizeStatisticalResult).not.toHaveBeenCalled();
  });

  it("selects an available alternative atlas when AAL is unavailable without starting localization", async () => {
    vi.mocked(api.statisticalAtlases).mockResolvedValue([atlas("aal", false), atlas("brodmann")]);
    render(<WorkRunPanel run={run} />);
    await waitFor(() => expect(screen.getByLabelText("图谱")).toHaveValue("brodmann"));
    expect(within(screen.getByLabelText("图谱")).queryByRole("option", { name: "AAL" })).not.toBeInTheDocument();
    expect(api.localizeStatisticalResult).not.toHaveBeenCalled();
  });

  it("keeps Mock results qualified and excludes another run's statistics from this task", async () => {
    vi.mocked(api.statisticalResults).mockResolvedValue([result, { ...result, result_id: "other-result", run_id: "other-run" }]);
    vi.mocked(api.statisticalResult).mockResolvedValue({ ...result, non_scientific: true, mode: "synthetic", manifest: { clusters: [], inference_status: "not_assessed_synthetic" } });
    render(<WorkRunPanel run={run} />);
    await screen.findByText("非科学占位结果不提供统计图切片预览。");
    expect(screen.getByText(/非科学演示结果/)).toBeInTheDocument();
    expect(api.statisticalResult).toHaveBeenCalledOnce();
    expect(api.statisticalResult).toHaveBeenCalledWith(result.result_id, expect.any(AbortSignal));
    expect(api.statisticalResultSliceInfo).not.toHaveBeenCalled();
    expect(api.statisticalAtlases).not.toHaveBeenCalled();
    expect(screen.queryByText(/显著差异体素/)).not.toBeInTheDocument();
  });
});
