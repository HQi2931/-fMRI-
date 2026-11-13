import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { statisticsApi as api } from "../api/statistics";
import { StatisticalClusterRoiPanel } from "./StatisticalClusterRoiPanel";

vi.mock("../api/statistics", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/statistics")>(),
  statisticsApi: { statisticalClusterRoi: vi.fn() },
}));
afterEach(() => cleanup());

describe("Descriptive cluster ROI", () => {
  it("reports an integrity failure and retries the same cluster without retaining an old distribution", async () => {
    vi.mocked(api.statisticalClusterRoi).mockRejectedValueOnce(new Error("来源影像校验失败，请重新准备方案。"))
      .mockResolvedValueOnce({ result_id: "result-1", result_bundle_hash: "a".repeat(64), cluster_id: "cluster-1", status: "descriptive", voxel_count: 1,
        rows: [{ subject_id: "demo-01", group: null, condition: null, mean_value: 4 }], limitation: "同一簇的描述，不能作为独立验证。" });
    render(<StatisticalClusterRoiPanel resultId="result-1" bundleHash={"a".repeat(64)} clusterId="cluster-1" />);
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("来源影像校验失败");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "下载 ROI 均值表" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    await screen.findByText("demo-01");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("全部受试者")).toBeInTheDocument();
    expect(screen.getByText("4.0000")).toBeInTheDocument();
  });

  it("preserves paired conditions in the descriptive plot and source export", async () => {
    vi.mocked(api.statisticalClusterRoi).mockResolvedValue({ result_id: "result-1", result_bundle_hash: "a".repeat(64), cluster_id: "cluster-1", status: "descriptive", voxel_count: 1,
      rows: [{ subject_id: "demo-01", group: "paired", condition: "before", mean_value: 2 }, { subject_id: "demo-01", group: "paired", condition: "after", mean_value: 2 }], limitation: "同一数据，不能独立验证。" });
    render(<StatisticalClusterRoiPanel resultId="result-1" bundleHash={"a".repeat(64)} clusterId="cluster-1" />);
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    const plot = await screen.findByRole("img", { name: "逐受试者 ROI 均值分布" });
    expect(plot).toHaveTextContent("before");
    expect(plot).toHaveTextContent("after");
    expect(plot.querySelectorAll("circle")).toHaveLength(2);
    expect([...plot.querySelectorAll("circle")].every((circle) => circle.getAttribute("cx") === "130")).toBe(true);
    const exportContent = decodeURIComponent(screen.getByRole("link", { name: "下载 ROI 均值表" }).getAttribute("href")!.split(",")[1]);
    expect(exportContent).toContain("demo-01\tpaired\tbefore\t2");
    expect(exportContent).toContain("demo-01\tpaired\tafter\t2");
    expect(screen.queryByText(/p 值/)).not.toBeInTheDocument();
  });

  it("keeps a registered empty ROI response empty without drawing or claiming a significant distribution", async () => {
    vi.mocked(api.statisticalClusterRoi).mockResolvedValue({ result_id: "result-1", result_bundle_hash: "a".repeat(64), cluster_id: "cluster-1", status: "no_significant_voxels", voxel_count: 0,
      rows: [], limitation: "没有显著体素，未提取 ROI 均值。" });
    render(<StatisticalClusterRoiPanel resultId="result-1" bundleHash={"a".repeat(64)} clusterId="cluster-1" />);
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    await screen.findByText(/没有显著体素，未提取 ROI 均值/);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载 ROI 来源记录" })).toBeInTheDocument();
  });

  it("shows subject means and exports provenance without a second significance test", async () => {
    vi.mocked(api.statisticalClusterRoi).mockResolvedValue({
      result_id: "result-1", result_bundle_hash: "a".repeat(64), cluster_id: "cluster-1",
      status: "descriptive", voxel_count: 20, source_map_artifact_id: "map-1",
      limitation: "仅描述，不作为独立验证。", rows: [
        { subject_id: "sub-01", group: "ASD", condition: null, mean_value: 2 },
        { subject_id: "sub-02", group: "HC", condition: null, mean_value: 5 },
      ],
    });
    render(<StatisticalClusterRoiPanel resultId="result-1" bundleHash={"a".repeat(64)} clusterId="cluster-1" />);
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    expect(await screen.findByRole("img", { name: "逐受试者 ROI 均值分布" })).toBeInTheDocument();
    expect(screen.getByText("2.0000")).toBeInTheDocument();
    expect(screen.getByText("5.0000")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载 ROI 均值表" })).toHaveAttribute("download", "roi-cluster-1.tsv");
    expect(screen.getByRole("link", { name: "下载 ROI 来源记录" })).toHaveAttribute("download", "roi-cluster-1.json");
    expect(screen.queryByText(/p 值/)).not.toBeInTheDocument();
  });

  it("does not show ROI rows bound to a different result bundle", async () => {
    vi.mocked(api.statisticalClusterRoi).mockResolvedValue({
      result_id: "result-1", result_bundle_hash: "b".repeat(64), cluster_id: "cluster-1",
      status: "descriptive", voxel_count: 20, rows: [{ subject_id: "stale-subject", mean_value: 5 }],
      limitation: "仅描述",
    });
    render(<StatisticalClusterRoiPanel resultId="result-1" bundleHash={"a".repeat(64)} clusterId="cluster-1" />);
    fireEvent.click(screen.getByRole("button", { name: "读取 ROI 分布" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ROI 来源与当前结果不一致");
    expect(screen.queryByText("stale-subject")).not.toBeInTheDocument();
  });
});
