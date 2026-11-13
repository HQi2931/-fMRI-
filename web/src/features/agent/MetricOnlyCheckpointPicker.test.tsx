import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { plansApi } from "../../api/plans";
import { projectsApi } from "../../api/projects";
import { runsApi } from "../../api/runs";
import type { Artifact, Dataset, Manifest, Project, Run } from "../../api/types";
import { MetricOnlyCheckpointPicker, type MetricOnlyContext, type MetricOnlySource } from "./MetricOnlyCheckpointPicker";

vi.mock("../../api/plans", () => ({ plansApi: { dryRunSkillPlan: vi.fn() } }));
vi.mock("../../api/projects", () => ({ projectsApi: { project: vi.fn(), dataset: vi.fn(), manifest: vi.fn() } }));
vi.mock("../../api/runs", () => ({ runsApi: { run: vi.fn(), artifacts: vi.fn() } }));
vi.mock("../../api/transport", () => ({ describeError: (error: unknown) => error instanceof Error ? error.message : String(error) }));

const context: MetricOnlyContext = {
  key: "current-context",
  conversationId: "conversation-1",
  workspacePath: "D:/work",
  projectId: "project-1",
  datasetId: "dataset-1",
  manifestId: "manifest-1",
  manifestHash: "m".repeat(64),
};
const sources: MetricOnlySource[] = [{ key: "task-1:run-1", taskId: "task-1", runId: "run-1" }];

const project = {
  project_id: "project-1", name: "Demo", source_roots: [], work_root: "D:/work", version: 13, created_at: "2026-01-01T00:00:00Z",
} as Project;
const dataset = {
  dataset_id: "dataset-1", project_id: "project-1", name: "Demo data", source_path: "D:/data", version: 4,
  current_manifest_id: "manifest-1", created_at: "2026-01-01T00:00:00Z",
} as Dataset;
const manifest = {
  manifest_id: "manifest-1", dataset_id: "dataset-1", revision: 3, content_hash: "m".repeat(64),
  profile: { kind: "bids", file_count: 1, nifti_count: 1, dicom_count: 0, subject_count: 1, issues: [], warnings: [] },
  subjects: [{ subject_id: "sub-01", session_id: null, functional_files: ["sub-01_bold.nii.gz"], anatomical_files: [], dicom_files: [] }],
  created_at: "2026-01-01T00:00:00Z",
} as unknown as Manifest;
const run = { run_id: "run-1", project_id: "project-1", state: "succeeded" } as Run;

function functionalArtifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    artifact_id: "functional-1",
    artifact_type: "preprocessing.functional_timeseries",
    checksum: "a".repeat(64),
    created_at: "2026-01-01T00:00:00Z",
    project_id: "project-1",
    run_id: "run-1",
    relative_path: "run-1/functional.nii.gz",
    size_bytes: 1024,
    provenance: {
      lineage: {
        artifact_id: "functional-1",
        kind: "functional_timeseries",
        metadata_verified: true,
        subject_manifest_hash: "m".repeat(64),
        subject_id: "sub-01",
        session_id: null,
        mask_artifact_id: "mask-1",
        mask_grid_signature: "grid-1",
      },
    },
    ...overrides,
  } as Artifact;
}

function successfulDryRun(ready = true) {
  return {
    ready,
    manifest_id: "manifest-1",
    manifest_hash: "m".repeat(64),
    plan_hash: ready ? "p".repeat(64) : null,
    scope: "skill_contract_compile_only" as const,
    environment_probed: false as const,
    issues: ready ? [] : [{ code: "FALFF_INPUT_ALREADY_FILTERED", path: "input_artifact_id", message: "fALFF 输入已经滤波。", severity: "blocking" as const }],
  };
}

function setupApis(artifacts = [functionalArtifact()]) {
  vi.mocked(projectsApi.project).mockResolvedValue(project);
  vi.mocked(projectsApi.dataset).mockResolvedValue(dataset);
  vi.mocked(projectsApi.manifest).mockResolvedValue(manifest);
  vi.mocked(runsApi.run).mockResolvedValue(run);
  vi.mocked(runsApi.artifacts).mockResolvedValue(artifacts);
  vi.mocked(plansApi.dryRunSkillPlan).mockResolvedValue(successfulDryRun());
}

async function selectFunctionalArtifact(user = userEvent.setup()) {
  await screen.findByRole("combobox", { name: "来源预处理运行" });
  await user.selectOptions(screen.getByRole("combobox", { name: "来源预处理运行" }), sources[0].key);
  await screen.findByRole("option", { name: "run-1/functional.nii.gz" });
  await user.selectOptions(screen.getByRole("combobox", { name: "功能时序产物" }), "functional-1");
  return user;
}

async function fillAlffForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("checkbox", { name: "ALFF" }));
  await user.type(screen.getByRole("spinbutton", { name: "ALFF/fALFF TR（秒）" }), "2");
  await user.type(screen.getByRole("spinbutton", { name: "ALFF/fALFF 频段下限（Hz）" }), "0.01");
  await user.type(screen.getByRole("spinbutton", { name: "ALFF/fALFF 频段上限（Hz）" }), "0.08");
  await user.click(screen.getByRole("checkbox", { name: "ALFF/fALFF z 标准化" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "ALFF/fALFF 掩膜来源" }), "dpabi_auto");
  await user.selectOptions(screen.getByRole("combobox", { name: "指标滤波阶段" }), "disabled");
  await user.selectOptions(screen.getByRole("combobox", { name: "指标结果图平滑" }), "false");
}

beforeEach(() => {
  vi.clearAllMocks();
  setupApis();
});
afterEach(() => cleanup());

describe("MetricOnlyCheckpointPicker", () => {
  it("requires explicit source and artifact selection and does not preflight by itself", async () => {
    render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    await screen.findByRole("combobox", { name: "来源预处理运行" });

    expect(screen.getByRole("combobox", { name: "来源预处理运行" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "功能时序产物" })).toHaveValue("");
    expect(runsApi.run).not.toHaveBeenCalled();
    expect(plansApi.dryRunSkillPlan).not.toHaveBeenCalled();
    expect(projectsApi.project).toHaveBeenCalledWith("project-1", expect.any(AbortSignal));
    expect(projectsApi.dataset).toHaveBeenCalledWith("dataset-1", expect.any(AbortSignal));
    expect(projectsApi.manifest).toHaveBeenCalledWith("manifest-1", expect.any(AbortSignal));
  });

  it("does not offer functional artifacts with mismatched lineage", async () => {
    const badArtifact = functionalArtifact({
      artifact_id: "wrong-lineage",
      provenance: { lineage: { ...functionalArtifact().provenance.lineage as Record<string, unknown>, artifact_id: "different-artifact", subject_manifest_hash: "x".repeat(64) } },
    });
    setupApis([
      badArtifact,
      functionalArtifact({ artifact_id: "wrong-project", project_id: "another-project" }),
      functionalArtifact({ artifact_id: "wrong-run", run_id: "another-run" }),
      functionalArtifact({ artifact_type: "preprocessing.brain_mask", artifact_id: "not-timeseries" }),
      functionalArtifact(),
    ]);
    render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = userEvent.setup();
    await screen.findByRole("combobox", { name: "来源预处理运行" });
    await user.selectOptions(screen.getByRole("combobox", { name: "来源预处理运行" }), sources[0].key);

    const artifactSelect = screen.getByRole("combobox", { name: "功能时序产物" });
    await screen.findByRole("option", { name: "run-1/functional.nii.gz" });
    expect(within(artifactSelect).getAllByRole("option")).toHaveLength(2);
    expect(artifactSelect).toHaveValue("");
    expect(within(artifactSelect).queryByRole("option", { name: "wrong-project" })).not.toBeInTheDocument();
    expect(within(artifactSelect).queryByRole("option", { name: "wrong-run" })).not.toBeInTheDocument();
    expect(plansApi.dryRunSkillPlan).not.toHaveBeenCalled();
  });

  it("shows the selected artifact image preview and labels it as non-QC", async () => {
    render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    await selectFunctionalArtifact();

    const image = screen.getByRole("img", { name: "预处理功能时序图像预览" });
    expect(image).toHaveAttribute("src", "/api/v1/artifacts/functional-1/preview.png");
    expect(screen.getByText("图像预览仅供查看，不是空间或科学质量控制。")).toBeInTheDocument();
    expect(plansApi.dryRunSkillPlan).not.toHaveBeenCalled();
  });

  it("keeps ALFF/fALFF and ReHo scientific controls uniquely named when both are open", async () => {
    render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = await selectFunctionalArtifact();
    await user.click(screen.getByRole("checkbox", { name: "ALFF" }));
    await user.click(screen.getByRole("checkbox", { name: "ReHo" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "ReHo 时间滤波" }), "enabled");
    await user.selectOptions(screen.getByRole("combobox", { name: "指标结果图平滑" }), "true");
    await user.selectOptions(screen.getByRole("combobox", { name: "ReHo 计算时平滑" }), "true");

    expect(screen.getByRole("spinbutton", { name: "ALFF/fALFF 频段下限（Hz）" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "ReHo 频段下限（Hz）" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "ALFF/fALFF z 标准化" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "ReHo z 标准化" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "ALFF/fALFF 结果图 X 方向（毫米）" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "ReHo X 方向（毫米）" })).toBeInTheDocument();
  });

  it("fails closed without requesting project data when context is incomplete", async () => {
    render(<MetricOnlyCheckpointPicker context={{ ...context, projectId: "" }} sources={sources} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("缺少项目、数据集或清单版本信息");
    expect(projectsApi.project).not.toHaveBeenCalled();
    expect(projectsApi.dataset).not.toHaveBeenCalled();
    expect(runsApi.run).not.toHaveBeenCalled();
    expect(plansApi.dryRunSkillPlan).not.toHaveBeenCalled();
  });

  it("posts only an explicit metric-only dry-run request using refreshed versions", async () => {
    render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = await selectFunctionalArtifact();
    await fillAlffForm(user);
    await user.click(screen.getByRole("button", { name: "只读预检指标方案" }));

    await waitFor(() => expect(plansApi.dryRunSkillPlan).toHaveBeenCalledTimes(1));
    expect(plansApi.dryRunSkillPlan).toHaveBeenCalledWith({
      expected_project_version: 13,
      validation_mode: "strict",
      request: {
        project_id: "project-1",
        dataset_ref: "dataset-1",
        input_manifest_hash: "m".repeat(64),
        input_artifact_id: "functional-1",
        request_preprocessing: false,
        requested_metrics: ["alff"],
        primary_outputs: ["zALFF"],
        alff_falff: {
          tr_seconds: 2,
          frequency_band: { low_hz: 0.01, high_hz: 0.08 },
          requested_metrics: ["alff"],
          requested_scalings: ["z_score"],
          mask_source: "dpabi_auto",
          mask_artifact_id: null,
          filter_timing: "disabled",
          result_smoothing: false,
          result_smoothing_fwhm_mm: null,
          provenance: ["tr_seconds", "frequency_band", "requested_metrics", "requested_scalings", "mask_source", "mask_artifact_id", "filter_timing", "result_smoothing", "result_smoothing_fwhm_mm"].map((name) => ({ name, source: "user", evidence_ref: "metric-only dry-run form" })),
        },
      },
    }, expect.any(AbortSignal));
    expect(screen.getByRole("heading", { name: "预检通过" })).toBeInTheDocument();
    expect(screen.getByText("方案哈希：" + "p".repeat(64))).toBeInTheDocument();
    expect(runsApi.run).toHaveBeenCalledTimes(1);
  });

  it("shows server blocking issues when a dry-run is not ready", async () => {
    vi.mocked(plansApi.dryRunSkillPlan).mockResolvedValue(successfulDryRun(false));
    render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = await selectFunctionalArtifact();
    await fillAlffForm(user);
    await user.click(screen.getByRole("button", { name: "只读预检指标方案" }));

    expect(await screen.findByRole("heading", { name: "仍有阻断项" })).toBeInTheDocument();
    expect(screen.getByText("fALFF 输入已经滤波。")).toBeInTheDocument();
  });

  it("aborts a pending run load when context changes and discards its response", async () => {
    let completeRun: ((value: Run) => void) | undefined;
    let observedSignal: AbortSignal | undefined;
    vi.mocked(runsApi.run).mockImplementation((_runId, signal) => {
      observedSignal = signal;
      return new Promise((resolve) => { completeRun = resolve; });
    });
    const view = render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = userEvent.setup();
    await screen.findByRole("combobox", { name: "来源预处理运行" });
    await user.selectOptions(screen.getByRole("combobox", { name: "来源预处理运行" }), sources[0].key);
    await waitFor(() => expect(runsApi.run).toHaveBeenCalledTimes(1));

    view.rerender(<MetricOnlyCheckpointPicker context={{ ...context, key: "next-context" }} sources={sources} />);
    await waitFor(() => expect(observedSignal?.aborted).toBe(true));
    await act(async () => { completeRun?.(run); });

    expect(screen.getByRole("combobox", { name: "来源预处理运行" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "功能时序产物" })).toHaveValue("");
    expect(screen.queryByRole("img", { name: "预处理功能时序图像预览" })).not.toBeInTheDocument();
  });

  it("aborts a pending dry-run when context changes and discards its response", async () => {
    let finishDryRun: ((value: Awaited<ReturnType<typeof plansApi.dryRunSkillPlan>>) => void) | undefined;
    let observedSignal: AbortSignal | undefined;
    vi.mocked(plansApi.dryRunSkillPlan).mockImplementation((_body, signal) => {
      observedSignal = signal;
      return new Promise((resolve) => { finishDryRun = resolve; });
    });
    const view = render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = await selectFunctionalArtifact();
    await fillAlffForm(user);
    await user.click(screen.getByRole("button", { name: "只读预检指标方案" }));
    await waitFor(() => expect(plansApi.dryRunSkillPlan).toHaveBeenCalledTimes(1));

    view.rerender(<MetricOnlyCheckpointPicker context={{ ...context, key: "next-context" }} sources={sources} />);
    await waitFor(() => expect(observedSignal?.aborted).toBe(true));
    await waitFor(() => expect(projectsApi.project).toHaveBeenCalledTimes(3));
    await act(async () => {
      finishDryRun?.(successfulDryRun() as Awaited<ReturnType<typeof plansApi.dryRunSkillPlan>>);
    });

    expect(screen.queryByRole("heading", { name: "预检通过" })).not.toBeInTheDocument();
    expect(screen.queryByText(`方案哈希：${"p".repeat(64)}`)).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "来源预处理运行" })).toHaveValue("");
  });

  it("aborts a pending dry-run on unmount and ignores its late response", async () => {
    let finishDryRun: ((value: Awaited<ReturnType<typeof plansApi.dryRunSkillPlan>>) => void) | undefined;
    let observedSignal: AbortSignal | undefined;
    vi.mocked(plansApi.dryRunSkillPlan).mockImplementation((_body, signal) => {
      observedSignal = signal;
      return new Promise((resolve) => { finishDryRun = resolve; });
    });
    const view = render(<MetricOnlyCheckpointPicker context={context} sources={sources} />);
    const user = await selectFunctionalArtifact();
    await fillAlffForm(user);
    await user.click(screen.getByRole("button", { name: "只读预检指标方案" }));
    await waitFor(() => expect(plansApi.dryRunSkillPlan).toHaveBeenCalledTimes(1));

    view.unmount();
    expect(observedSignal?.aborted).toBe(true);
    await act(async () => {
      finishDryRun?.(successfulDryRun() as Awaited<ReturnType<typeof plansApi.dryRunSkillPlan>>);
    });

    expect(screen.queryByRole("heading", { name: "预检通过" })).not.toBeInTheDocument();
  });
});
