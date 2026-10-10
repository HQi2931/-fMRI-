import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type ExternalAnalysisImport } from "../api/client";
import { ExternalAnalysisImportPanel } from "./ExternalAnalysisImportPanel";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  return {
    ...original,
    api: {
      externalAnalysisImports: vi.fn(),
      inspectExternalAnalysisImport: vi.fn(),
      includeExternalAnalysisImport: vi.fn(),
      externalAnalysisImport: vi.fn(),
      externalDemographics: vi.fn(),
      importExternalDemographics: vi.fn(),
    },
  };
});

afterEach(() => cleanup());

const pendingImport: ExternalAnalysisImport = {
  import_id: "import-1",
  project_id: "project-1",
  source_path: "D:\\Study\\ALFF",
  status: "needs_review",
  version: 1,
  inspection: {
    layout: "grouped",
    schema_version: "1.0",
    source_tree_hash: "a".repeat(64),
    metric_kind: "alff",
    scaling: "z_score",
    group_order: ["ASD", "HC"],
    grid_signature: "b".repeat(64),
    coordinate_space: "unknown",
    blocking_issues: [],
    warnings: ["处理谱系未知"],
    unknown_processing_fields: ["TR and frequency band", "mask source and version"],
    images: [
      { subject_id: "sub-01", group: "ASD", relative_path: "ASD/sub-01-zALFFMap.nii", sha256: "c".repeat(64), size_bytes: 100, shape_ijk: [4, 5, 6], affine: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], grid_signature: "b".repeat(64), metric_kind: "alff", scaling: "z_score" },
      { subject_id: "sub-02", group: "HC", relative_path: "HC/sub-02-zALFFMap.nii", sha256: "d".repeat(64), size_bytes: 100, shape_ijk: [4, 5, 6], affine: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], grid_signature: "b".repeat(64), metric_kind: "alff", scaling: "z_score" },
    ],
  },
  inclusion: null,
  inclusion_hash: null,
  created_at: "2026-10-08T00:00:00Z",
  updated_at: "2026-10-08T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.externalAnalysisImports).mockResolvedValue([]);
  vi.mocked(api.externalDemographics).mockResolvedValue(null);
  vi.mocked(api.inspectExternalAnalysisImport).mockResolvedValue(pendingImport);
  vi.mocked(api.includeExternalAnalysisImport).mockResolvedValue({
    ...pendingImport,
    status: "included",
    version: 2,
    inclusion_hash: "e".repeat(64),
    inclusion: {
      included_subject_ids: ["sub-01", "sub-02"],
      excluded_subject_ids: [],
      group_order: ["ASD", "HC"],
      coordinate_space: "unknown",
      coordinate_space_source: "unknown",
      actor: "researcher",
      reason: "已核对来源目录并接受未知处理信息。",
      unknown_processing_acknowledged: true,
    },
  });
});

describe("External DPABI metric import", () => {
  it("blocks unknown exclusions and an emptied group, and freezes the reviewed group order, mask and space", async () => {
    render(<ExternalAnalysisImportPanel projectId="project-1" initialPath="D:\\Study\\ALFF" />);
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    await screen.findByLabelText("纳入理由");
    fireEvent.change(screen.getByLabelText("纳入理由"), { target: { value: "已检查空间、掩膜及组别顺序。" } });
    fireEvent.click(screen.getByRole("checkbox"));
    const exclusions = screen.getByLabelText("受试者 ID（每行或逗号分隔）");
    fireEvent.change(exclusions, { target: { value: "unknown-subject" } });
    expect(screen.getByText(/以下 ID 不在检查清单中/)).toHaveTextContent("unknown-subject");
    expect(screen.getByRole("button", { name: "确认纳入 2 名受试者" })).toBeDisabled();
    fireEvent.change(exclusions, { target: { value: "sub-01" } });
    expect(screen.getByText(/每个组至少保留一名受试者/)).toHaveTextContent("ASD");
    expect(screen.getByRole("button", { name: "确认纳入 1 名受试者" })).toBeDisabled();
    expect(api.includeExternalAnalysisImport).not.toHaveBeenCalled();
    fireEvent.change(exclusions, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "下移组别 ASD" }));
    expect(screen.getByRole("button", { name: "上移组别 HC" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "上移组别 ASD" }));
    fireEvent.click(screen.getByRole("button", { name: "上移组别 HC" }));
    fireEvent.change(screen.getByLabelText("图像空间"), { target: { value: "MNI" } });
    fireEvent.change(screen.getByLabelText("统计 mask（相对导入目录的 NIfTI 路径，可留空）"), { target: { value: " Mask.nii " } });
    fireEvent.change(screen.getByLabelText("审核人"), { target: { value: " Demo researcher " } });
    fireEvent.click(screen.getByRole("button", { name: "确认纳入 2 名受试者" }));
    await waitFor(() => expect(api.includeExternalAnalysisImport).toHaveBeenCalledWith("import-1", expect.objectContaining({
      expected_version: 1, group_order: ["HC", "ASD"], coordinate_space: "MNI", mask_relative_path: "Mask.nii", actor: "Demo researcher",
    })));
  });

  it("reports a changed source at inclusion without selecting or freezing the stale inspection", async () => {
    vi.mocked(api.includeExternalAnalysisImport).mockRejectedValue(new Error("目录内容已变化，请重新检查并确认纳入。"));
    const selected = vi.fn();
    render(<ExternalAnalysisImportPanel projectId="project-1" initialPath="D:\\Study\\ALFF" onSelect={selected} />);
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    fireEvent.change(await screen.findByLabelText("纳入理由"), { target: { value: "已检查目录。" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "确认纳入 2 名受试者" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("目录内容已变化");
    expect(screen.queryByText(/纳入决定已冻结/)).not.toBeInTheDocument();
    expect(selected).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    await waitFor(() => expect(api.inspectExternalAnalysisImport).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByLabelText("纳入理由")).toHaveValue("");
  });

  it("keeps blocking inspection issues and failures explicit, and does not carry a review into another project", async () => {
    vi.mocked(api.inspectExternalAnalysisImport).mockRejectedValueOnce(new Error("指标图网格不一致"))
      .mockResolvedValueOnce({ ...pendingImport, inspection: { ...pendingImport.inspection, blocking_issues: ["重复受试者编号"] } });
    const view = render(<ExternalAnalysisImportPanel projectId="project-1" initialPath="D:\\Study\\ALFF" />);
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("指标图网格不一致");
    expect(screen.queryByText(/目录检查结果/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    expect(await screen.findByText("重复受试者编号")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("纳入理由"), { target: { value: "待检查重复编号" } });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "确认纳入 2 名受试者" })).toBeDisabled();
    view.rerender(<ExternalAnalysisImportPanel projectId="project-2" initialPath={"D:\\OtherStudy\\ALFF"} />);
    await waitFor(() => expect(api.externalAnalysisImports).toHaveBeenCalledWith("project-2", expect.any(AbortSignal)));
    expect(screen.queryByText("重复受试者编号")).not.toBeInTheDocument();
    expect(screen.getByLabelText("指标目录路径")).toHaveValue("D:\\OtherStudy\\ALFF");
  });

  it("does not register duplicate covariate names and displays a server revision conflict", async () => {
    vi.mocked(api.inspectExternalAnalysisImport).mockResolvedValue({ ...pendingImport, status: "included", version: 2 });
    vi.mocked(api.importExternalDemographics).mockRejectedValue(new Error("纳入修订已变化，需重新准备统计方案。"));
    render(<ExternalAnalysisImportPanel projectId="project-1" initialPath="D:\\Study\\ALFF" />);
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    fireEvent.change(await screen.findByLabelText("人口学表路径"), { target: { value: "D:\\Study\\age.csv" } });
    const mapping = screen.getByLabelText("字段对应（每行填写：分析列名=表中列名）");
    fireEvent.change(mapping, { target: { value: "age=Age\nage=OtherAge" } });
    fireEvent.click(screen.getByRole("button", { name: "登记并对齐人口学修订" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("每行需填写一对不同的列名");
    expect(api.importExternalDemographics).not.toHaveBeenCalled();
    fireEvent.change(mapping, { target: { value: "age=Age" } });
    fireEvent.click(screen.getByRole("button", { name: "登记并对齐人口学修订" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("纳入修订已变化");
    expect(screen.getByRole("button", { name: "登记并对齐人口学修订" })).toBeEnabled();
    expect(screen.queryByText(/第 1 版/)).not.toBeInTheDocument();
  });

  it("uses explicit paired layout and includes each subject once for both conditions", async () => {
    const pairedImport: ExternalAnalysisImport = {
      ...pendingImport,
      inspection: { ...pendingImport.inspection, layout: "paired_conditions", group_order: ["before", "after"],
        images: ["before", "after"].flatMap((condition) => pendingImport.inspection.images.map((image) => ({
          ...image, group: condition, condition, relative_path: `${condition}/${image.subject_id}-zALFFMap.nii`,
        }))) },
    };
    vi.mocked(api.inspectExternalAnalysisImport).mockResolvedValue(pairedImport);
    render(<ExternalAnalysisImportPanel projectId="project-1" />);
    fireEvent.change(screen.getByLabelText("数据布局"), { target: { value: "paired_conditions" } });
    fireEvent.change(screen.getByLabelText("指标目录路径"), { target: { value: "D:\\Study\\paired" } });
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));
    expect(await screen.findByText("已按编号检查配对；条件顺序表示“前一条件减后一条件”。排除受试者会同时排除两张图。"))
      .toBeInTheDocument();
    expect(api.inspectExternalAnalysisImport).toHaveBeenCalledWith({ project_id: "project-1", source_path: "D:\\Study\\paired", layout: "paired_conditions" });
    fireEvent.change(screen.getByLabelText("纳入理由"), { target: { value: "已检查两个条件的配对编号。" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "确认纳入 2 名受试者" }));
    await waitFor(() => expect(api.includeExternalAnalysisImport).toHaveBeenCalledWith("import-1", expect.objectContaining({
      included_subject_ids: ["sub-01", "sub-02"], group_order: ["before", "after"],
    })));
  });
  it("registers a subject-aligned covariate table and shows its frozen revision", async () => {
    const detail = { ...pendingImport, status: "included" as const, version: 2 };
    vi.mocked(api.externalAnalysisImport).mockResolvedValue(detail);
    vi.mocked(api.externalAnalysisImports).mockResolvedValue([{
      import_id: "import-1", project_id: "project-1", status: "included", version: 2, layout: "grouped",
      subject_count: 2, group_counts: { ASD: 1, HC: 1 }, metric_kind: "alff", scaling: "z_score",
      coordinate_space: "unknown", blocking_issues: [], warnings: [], source_tree_hash: "a".repeat(64),
      unknown_processing_fields: [], inclusion_hash: "e".repeat(64), created_at: pendingImport.created_at, updated_at: pendingImport.updated_at,
    }]);
    const revision = { demographics_id: "demo-1", external_import_id: "import-1", inclusion_hash: "e".repeat(64),
      revision: 1, content_hash: "f".repeat(64), source_sha256: "b".repeat(64), row_count: 2,
      columns: ["subject_id", "age"], created_at: pendingImport.created_at };
    vi.mocked(api.importExternalDemographics).mockResolvedValue(revision);
    vi.mocked(api.externalDemographics).mockResolvedValue(revision);
    render(<ExternalAnalysisImportPanel projectId="project-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "查看" }));
    fireEvent.change(await screen.findByLabelText("人口学表路径"), { target: { value: "D:\\Study\\age.csv" } });
    fireEvent.change(screen.getByLabelText("受试者编号列"), { target: { value: "ID" } });
    fireEvent.change(screen.getByLabelText("字段对应（每行填写：分析列名=表中列名）"), { target: { value: "age=Age" } });
    fireEvent.click(screen.getByRole("button", { name: "登记并对齐人口学修订" }));
    await waitFor(() => expect(api.importExternalDemographics).toHaveBeenCalledWith("import-1", {
      source_path: "D:\\Study\\age.csv", subject_id_column: "ID", column_mapping: { age: "Age" },
      encoding: "utf-8-sig", expected_import_version: 2,
    }));
    expect(await screen.findByText("第 1 版 · 2 名受试者 · 可用列：subject_id、age")).toBeInTheDocument();
  });
  it("selects only a reviewed import with a registered mask for analysis", async () => {
    const onSelect = vi.fn();
    vi.mocked(api.externalAnalysisImports).mockResolvedValue([{
      import_id: "import-1", project_id: "project-1", status: "included", version: 2, layout: "grouped",
      subject_count: 2, group_counts: { ASD: 1, HC: 1 }, metric_kind: "alff", scaling: "z_score",
      coordinate_space: "unknown", blocking_issues: [], warnings: ["Unknown lineage"],
      source_tree_hash: "a".repeat(64), unknown_processing_fields: [],
      inclusion_hash: "e".repeat(64), created_at: pendingImport.created_at, updated_at: pendingImport.updated_at,
    }]);
    vi.mocked(api.externalAnalysisImport).mockResolvedValue({
      ...pendingImport, status: "included", version: 2,
      selection: { input_manifest_hash: "e".repeat(64), mask_artifact_id: "mask-1",
        group_order: ["ASD", "HC"], images: [
          { subject_id: "sub-01", artifact_id: "map-1", group: "ASD" },
          { subject_id: "sub-02", artifact_id: "map-2", group: "HC" },
        ] },
    });
    render(<ExternalAnalysisImportPanel projectId="project-1" onSelect={onSelect} selectedImportId="import-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "查看" }));
    fireEvent.click(await screen.findByRole("button", { name: "已选为本次分析来源" }));
    expect(onSelect).toHaveBeenCalledWith("import-1");
    fireEvent.click(screen.getByRole("button", { name: "改用工作区运行结果" }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });
  it("requires a separate review before it freezes an inclusion decision", async () => {
    render(<ExternalAnalysisImportPanel projectId="project-1" />);
    fireEvent.click(screen.getByText("导入外部 DPABI 指标目录"));
    fireEvent.change(screen.getByLabelText("指标目录路径"), { target: { value: "D:\\Study\\ALFF" } });
    fireEvent.click(screen.getByRole("button", { name: "检查目录" }));

    expect(await screen.findByText("alff · z_score · 2 张影像")).toBeInTheDocument();
    expect(screen.getByText("保留未知空间；若将来产生显著簇，系统会阻止脑区定位。"))
      .toBeInTheDocument();
    const include = screen.getByRole("button", { name: "确认纳入 2 名受试者" });
    expect(include).toBeDisabled();

    fireEvent.change(screen.getByLabelText("纳入理由"), {
      target: { value: "已核对来源目录并接受未知处理信息。" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(include).toBeEnabled();
    fireEvent.click(include);

    await screen.findByText(/纳入决定已冻结/);
    await waitFor(() => expect(api.includeExternalAnalysisImport).toHaveBeenCalledWith(
      "import-1",
      expect.objectContaining({
        included_subject_ids: ["sub-01", "sub-02"],
        group_order: ["ASD", "HC"],
        coordinate_space: "unknown",
        acknowledge_unknown_processing: true,
      }),
    ));
  });
});
