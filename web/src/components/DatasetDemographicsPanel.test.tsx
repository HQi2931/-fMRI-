import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { projectsApi } from "../api/projects";
import type { Dataset, Demographics, Manifest } from "../api/types";
import { DatasetDemographicsPanel } from "./DatasetDemographicsPanel";

vi.mock("../api/projects", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/projects")>();
  return {
    ...original,
    projectsApi: {
      dataset: vi.fn(),
      manifest: vi.fn(),
      importDemographics: vi.fn(),
    },
  };
});

vi.mock("../workspace", () => ({ updateWorkspace: vi.fn() }));

afterEach(() => cleanup());

const dataset = {
  dataset_id: "dataset-1",
  project_id: "project-1",
  name: "OpenNeuro demo",
  source_path: "D:\\Study\\ds002748-v1.0.5",
  version: 4,
  current_manifest_id: "manifest-1",
  created_at: "2026-10-21T00:00:00Z",
} as Dataset;

const manifest = {
  manifest_id: "manifest-1",
  dataset_id: "dataset-1",
  revision: 2,
  content_hash: "a".repeat(64),
  profile: { kind: "bids", file_count: 40, nifti_count: 40, dicom_count: 0, subject_count: 2, issues: [], warnings: [] },
  subjects: [
    { subject_id: "sub-01", session_id: null, functional_files: ["sub-01/func/sub-01_task-rest_bold.nii.gz"], anatomical_files: ["sub-01/anat/sub-01_T1w.nii.gz"], dicom_files: [] },
    { subject_id: "sub-02", session_id: null, functional_files: ["sub-02/func/sub-02_task-rest_bold.nii.gz"], anatomical_files: ["sub-02/anat/sub-02_T1w.nii.gz"], dicom_files: [] },
  ],
  selected_input_stage: null,
  stage_candidates: [],
  created_at: "2026-10-21T00:00:00Z",
} as Manifest;

const alignedRevision = {
  demographics_id: "demographics-1",
  dataset_id: "dataset-1",
  revision: 1,
  content_hash: "b".repeat(64),
  row_count: 2,
  columns: ["group", "age", "sex"],
  missing_subject_ids: [],
  extra_subject_ids: [],
  created_at: "2026-10-21T00:00:00Z",
} as Demographics;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(projectsApi.dataset).mockResolvedValue(dataset);
  vi.mocked(projectsApi.manifest).mockResolvedValue(manifest);
  vi.mocked(projectsApi.importDemographics).mockResolvedValue(alignedRevision);
});

describe("project demographics registration", () => {
  it("requires explicit source columns and confirmation before registering a CSV/TSV", async () => {
    render(<DatasetDemographicsPanel datasetId="dataset-1" />);

    expect(await screen.findByText("冻结清单：2 名受试者 · 修订 2")).toBeInTheDocument();
    const path = screen.getByLabelText("CSV/TSV 文件路径");
    expect(path).toHaveValue("D:\\Study\\ds002748-v1.0.5\\participants.tsv");
    const subjectId = screen.getByLabelText("受试者 ID 列（例如 participant_id）");
    const group = screen.getByLabelText("组别列 → 分析字段 group（例如 group）");
    expect(subjectId).toHaveValue("");
    expect(group).toHaveValue("");
    const submit = screen.getByRole("button", { name: "登记并校验人口学表" });
    expect(submit).toBeDisabled();

    fireEvent.change(subjectId, { target: { value: "participant_id" } });
    fireEvent.change(group, { target: { value: "group" } });
    expect(await screen.findByLabelText("列映射预览")).toHaveTextContent("participant_id → 受试者 ID · group → group");
    expect(submit).toBeDisabled();
    expect(projectsApi.importDemographics).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("checkbox", { name: /我已核对文件路径/ }));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(projectsApi.importDemographics).toHaveBeenCalledWith("dataset-1", {
      source_path: "D:\\Study\\ds002748-v1.0.5\\participants.tsv",
      subject_id_column: "participant_id",
      column_mapping: { group: "group" },
      encoding: "utf-8-sig",
      expected_dataset_version: 4,
    }));
    expect(await screen.findByText("登记完成，受试者与清单完全对齐")).toBeInTheDocument();
    expect(screen.getByText("第 1 版 · 2 行 · 已登记列：group、age、sex")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "登记并校验人口学表" }));
    await waitFor(() => expect(projectsApi.importDemographics).toHaveBeenCalledTimes(2));
    expect(vi.mocked(projectsApi.importDemographics).mock.calls[1][1]).toMatchObject({
      expected_dataset_version: 5,
    });
  });

  it("reports missing and extra IDs from the server-side alignment check", async () => {
    vi.mocked(projectsApi.importDemographics).mockResolvedValue({
      ...alignedRevision,
      missing_subject_ids: ["sub-02"],
      extra_subject_ids: ["sub-99"],
    });
    render(<DatasetDemographicsPanel datasetId="dataset-1" />);
    await screen.findByText("冻结清单：2 名受试者 · 修订 2");
    fireEvent.change(screen.getByLabelText("受试者 ID 列（例如 participant_id）"), { target: { value: "participant_id" } });
    fireEvent.change(screen.getByLabelText("组别列 → 分析字段 group（例如 group）"), { target: { value: "group" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /我已核对文件路径/ }));
    fireEvent.click(screen.getByRole("button", { name: "登记并校验人口学表" }));
    expect(await screen.findByText("登记完成，但受试者对齐存在差异")).toBeInTheDocument();
    expect(screen.getByText("缺少 ID：sub-02")).toBeInTheDocument();
    expect(screen.getByText("额外 ID：sub-99")).toBeInTheDocument();
  });

  it("shows server validation failures and refuses repeated source columns", async () => {
    vi.mocked(projectsApi.importDemographics).mockRejectedValue(new Error("重复的受试者编号"));
    render(<DatasetDemographicsPanel datasetId="dataset-1" />);
    await screen.findByText("冻结清单：2 名受试者 · 修订 2");
    const subjectId = screen.getByLabelText("受试者 ID 列（例如 participant_id）");
    const group = screen.getByLabelText("组别列 → 分析字段 group（例如 group）");
    fireEvent.change(subjectId, { target: { value: "participant_id" } });
    fireEvent.change(group, { target: { value: "participant_id" } });
    expect(screen.getByText("同一个源列不能同时用于 ID、组别或协变量。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登记并校验人口学表" })).toBeDisabled();

    fireEvent.change(group, { target: { value: "group" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /我已核对文件路径/ }));
    fireEvent.click(screen.getByRole("button", { name: "登记并校验人口学表" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("登记失败：重复的受试者编号");
  });

  it("blocks registration when there is no frozen manifest", async () => {
    vi.mocked(projectsApi.dataset).mockResolvedValue({ ...dataset, current_manifest_id: null });
    render(<DatasetDemographicsPanel datasetId="dataset-1" />);
    expect(await screen.findByText("请先检查并冻结当前数据集的受试者清单，再登记人口学表。")).toBeInTheDocument();
    expect(projectsApi.importDemographics).not.toHaveBeenCalled();
  });
});
