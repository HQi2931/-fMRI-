import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { StatisticalMethodSummary } from "../api/client";
import { StatisticalMethodPanel } from "./StatisticalMethodPanel";

const method: StatisticalMethodSummary = {
  test: "independent_two_sample_t", test_label: "两样本 t 检验", function_name: "y_TTest2_Image", statistic_type: "T",
  subject_count: 6, image_count: 6, group_counts: { ASD: 3, HC: 3 }, condition_order: [], covariates: [{ name: "age", centering: "grand_mean" }],
  contrast: [1, 0, 0], tail: "two_sided", tail_label: "双侧", df1: 3, df2: null,
  positive_direction: "ASD > HC", negative_direction: "ASD < HC", assumptions: ["共同残差方差 GLM"],
  correction_method: "fdr", q_threshold: 0.05, actual_p_threshold: 0.0002,
  correction_evidence_status: "verified", software_versions: { DPABI: "V9.0" }, evidence_issues: [],
};

describe("statistical method facts", () => {
  afterEach(cleanup);
  it("keeps group direction, centered covariates and actual FDR p distinct from q", () => {
    render(<StatisticalMethodPanel summary={method} />);
    expect(screen.getByText(/正值：ASD > HC；负值：ASD < HC/)).toBeInTheDocument();
    expect(screen.getByText("age（总体均值中心化）")).toBeInTheDocument();
    expect(screen.getByText("FDR q").nextElementSibling).toHaveTextContent("0.05");
    expect(screen.getByText("实际 p 截断值").nextElementSibling).toHaveTextContent("0.0002");
    expect(screen.getByText(/df₁=3/)).toBeInTheDocument();
    expect(screen.getByText("分组：ASD 3 人；HC 3 人")).toBeInTheDocument();
  });
  it("does not turn the omnibus F test into a directional group comparison", () => {
    render(<StatisticalMethodPanel summary={{ ...method, test: "one_way_anova_ancova", test_label: "ANOVA 总体 F", statistic_type: "F", positive_direction: null, negative_direction: null, df1: 2, df2: 3, correction_method: "grf", q_threshold: null, actual_p_threshold: null, voxel_p_threshold: 0.001, cluster_p_threshold: 0.05, smoothness_mode: "dpabi_header_or_estimate", smoothness_source: "estimated", effective_smoothness_dlh: 0.05, effective_fwhm: [4, 5, 6], minimum_cluster_size: 7, cluster_connectivity: 26 }} />);
    expect(screen.getByText(/总体 F 检验不表示哪组更高/)).toBeInTheDocument();
    expect(screen.queryByText(/正值：ASD > HC/)).not.toBeInTheDocument();
    expect(screen.getByText("体素 p／簇 p").nextElementSibling).toHaveTextContent("0.001／0.05");
    expect(screen.getByText(/df₂=3/)).toBeInTheDocument();
    expect(screen.getByText("实际平滑度来源").nextElementSibling).toHaveTextContent("未阈值化 Z 图估计");
    expect(screen.getByText("实际 DLH").nextElementSibling).toHaveTextContent("0.05");
    expect(screen.getByText("最小簇大小").nextElementSibling).toHaveTextContent("7 个体素");
  });
  it("reports unavailable method evidence instead of inventing a cutoff", () => {
    render(<StatisticalMethodPanel summary={{ ...method, actual_p_threshold: null, correction_evidence_status: "unavailable", evidence_issues: ["correction_evidence_unavailable"] }} />);
    expect(screen.getByText("实际 p 截断值").nextElementSibling).toHaveTextContent("未记录");
    expect(screen.getByText(/部分方法证据无法复核/)).toBeInTheDocument();
  });
});
