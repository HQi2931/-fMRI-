import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { Dataset, Manifest, Project, WorkspaceCheck } from "../api/types";
import { environmentApi } from "../api/environment";
import { projectsApi } from "../api/projects";
import { WorkspaceProjectSetupPanel } from "./WorkspaceProjectSetupPanel";

vi.mock("../api/projects", () => ({
  projectsApi: {
    createProject: vi.fn(),
    createDataset: vi.fn(),
    inspectDataset: vi.fn(),
  },
}));
vi.mock("../api/environment", () => ({
  environmentApi: { environmentConfig: vi.fn() },
}));

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

const sourcePath = "D:\\Study\\ds002748-v1.0.5";
const project: Project = {
  project_id: "project-demo", name: "rs-fMRI 项目 · ds002748-v1.0.5",
  source_roots: [sourcePath], work_root: "D:\\App\\work\\ds002748-v1.0.5-project",
  version: 1, created_at: "2026-10-21T00:00:00Z",
};
const dataset: Dataset = {
  dataset_id: "dataset-demo", project_id: project.project_id, name: "ds002748-v1.0.5",
  source_path: sourcePath, version: 1, current_manifest_id: null,
  created_at: "2026-10-21T00:00:00Z",
};
const manifest: Manifest = {
  manifest_id: "manifest-demo", dataset_id: dataset.dataset_id, revision: 1,
  content_hash: "a".repeat(64), created_at: "2026-10-21T00:00:00Z",
  profile: {
    kind: "bids", file_count: 48, nifti_count: 40, dicom_count: 0,
    subject_count: 20, issues: [], warnings: [],
  },
  subjects: Array.from({ length: 20 }, (_, index) => ({
    subject_id: `sub-${index + 1}`, session_id: null,
    functional_files: [`sub-${index + 1}_task-rest_bold.nii.gz`],
    anatomical_files: [`sub-${index + 1}_T1w.nii.gz`], dicom_files: [],
  })),
};
const check: WorkspaceCheck = {
  path: sourcePath, kind: "bids", file_count: 48, nifti_count: 40, dicom_count: 0,
  subject_count: 20, functional_subject_count: 20, anatomical_subject_count: 20,
  input_stage: null, selected_input_stage: null, stage_selection_required: false,
  stage_candidates: [], output_directories: [], invalid_nifti_files: [],
  issues: [], warnings: [], subjects: [], checked_at: "2026-10-21T00:00:00Z",
};

beforeEach(() => {
  vi.mocked(environmentApi.environmentConfig).mockResolvedValue({
    allowed_work_root: "D:\\App\\work",
    matlab_executable: null, spm_dir: null, dpabi_dir: null,
    matlab_version: "unspecified", spm_version: "unspecified", dpabi_version: "unspecified",
    configured: false,
  });
});

async function confirmAndRegister(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText("将当前 BIDS 数据集登记为项目"));
  await waitFor(() => expect(screen.getByLabelText("独立计算目录")).toHaveValue("D:\\App\\work\\ds002748-v1.0.5-project"));
  expect(screen.getByText(/默认位置在安全工作根目录/)).toHaveTextContent("D:\\App\\work");
  await user.click(screen.getByRole("checkbox", { name: /我已核对来源目录/ }));
  return screen.getByRole("button", { name: "登记项目并扫描清单" });
}

it("creates a project, dataset, frozen manifest, then opens the registered project", async () => {
  const user = userEvent.setup();
  vi.mocked(projectsApi.createProject).mockResolvedValue(project);
  vi.mocked(projectsApi.createDataset).mockResolvedValue(dataset);
  vi.mocked(projectsApi.inspectDataset).mockResolvedValue(manifest);
  const onProjectReady = vi.fn().mockResolvedValue(undefined);
  render(<WorkspaceProjectSetupPanel path={sourcePath} check={check} onProjectReady={onProjectReady} />);

  await user.click(await confirmAndRegister(user));

  await waitFor(() => expect(onProjectReady).toHaveBeenCalledWith({ project, dataset, manifest }));
  expect(projectsApi.createProject).toHaveBeenCalledWith({
    name: "rs-fMRI 项目 · ds002748-v1.0.5",
    source_roots: [sourcePath], work_root: "D:\\App\\work\\ds002748-v1.0.5-project",
  });
  expect(projectsApi.createDataset).toHaveBeenCalledWith(project.project_id, {
    name: "ds002748-v1.0.5", source_path: sourcePath, expected_project_version: project.version,
  });
  expect(projectsApi.inspectDataset).toHaveBeenCalledWith(dataset.dataset_id, {
    expected_dataset_version: dataset.version, report_only: false,
  });
  expect(screen.getByRole("status")).toHaveTextContent("20 名受试者 · 清单修订 1");
});

it("prevents overlapping source and calculation directories", async () => {
  const user = userEvent.setup();
  render(<WorkspaceProjectSetupPanel path={sourcePath} check={check} onProjectReady={vi.fn()} />);
  await user.click(screen.getByText("将当前 BIDS 数据集登记为项目"));
  const workRoot = screen.getByLabelText("独立计算目录");
  await user.clear(workRoot);
  await user.type(workRoot, `${sourcePath}\\derived`);

  expect(screen.getByRole("alert")).toHaveTextContent("必须与影像来源目录分开");
  expect(screen.getByRole("button", { name: "登记项目并扫描清单" })).toBeDisabled();
  expect(projectsApi.createProject).not.toHaveBeenCalled();
});

it("reuses a created project after dataset registration fails and prevents duplicate submits", async () => {
  const user = userEvent.setup();
  let finishProject: ((value: Project) => void) | undefined;
  vi.mocked(projectsApi.createProject).mockImplementationOnce(() => new Promise((resolve) => { finishProject = resolve; }));
  vi.mocked(projectsApi.createDataset)
    .mockRejectedValueOnce(new Error("数据集目录不可用"))
    .mockResolvedValueOnce(dataset);
  vi.mocked(projectsApi.inspectDataset).mockResolvedValue(manifest);
  const onProjectReady = vi.fn().mockResolvedValue(undefined);
  render(<WorkspaceProjectSetupPanel path={sourcePath} check={check} onProjectReady={onProjectReady} />);
  const submit = await confirmAndRegister(user);

  await user.dblClick(submit);
  await waitFor(() => expect(projectsApi.createProject).toHaveBeenCalledOnce());
  expect(submit).toBeDisabled();
  finishProject?.(project);
  await screen.findByRole("alert");
  expect(screen.getByRole("alert")).toHaveTextContent("数据集目录不可用");

  await user.click(screen.getByRole("button", { name: "登记项目并扫描清单" }));
  await waitFor(() => expect(onProjectReady).toHaveBeenCalledOnce());
  expect(projectsApi.createProject).toHaveBeenCalledOnce();
  expect(projectsApi.createDataset).toHaveBeenCalledTimes(2);
  expect(projectsApi.inspectDataset).toHaveBeenCalledOnce();
});
