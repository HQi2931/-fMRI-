import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { Router } from "./routing";

const now = "2026-08-06T00:00:00Z";

function json(value: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

function pathOf(input: RequestInfo | URL): string {
  return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://local").pathname;
}

function defaultApi(input: RequestInfo | URL): Promise<Response> {
  const path = pathOf(input);
  if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
  if (path.endsWith("/projects") || path.endsWith("/runs") || path.endsWith("/skills") || path.endsWith("/model-profiles")) return json([]);
  if (path.endsWith("/statistics/results")) return json([]);
  if (path.endsWith("/environment/probe")) return json({ ready: false, environment_hash: "e".repeat(64), components: [] });
  if (path.endsWith("/environment/config")) return json({ matlab_executable: null, spm_dir: null, dpabi_dir: null, matlab_version: "unspecified", spm_version: "unspecified", dpabi_version: "unspecified", configured: false });
  return json({ error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);
}

function setWorkspace(value: Record<string, unknown>): void {
  window.localStorage.setItem("rsfmri-workspace-v1", JSON.stringify(value));
}

describe("App", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    vi.spyOn(globalThis, "fetch").mockImplementation(defaultApi);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  function renderAt(route: string) {
    window.history.replaceState({}, "", route);
    return render(<Router><App /></Router>);
  }

  it("shows only persisted dashboard facts and reports the API connection", async () => {
    renderAt("/");
    expect(screen.getByRole("heading", { name: "从数据到可信结果" })).toBeInTheDocument();
    expect(await screen.findByText("服务已连接")).toBeInTheDocument();
    expect(screen.getByText("还没有项目")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  });

  it("shows an offline state without inventing successful work", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("offline"));
    renderAt("/");
    expect(await screen.findByText("离线预览")).toBeInTheDocument();
    expect(screen.getByText("尚未开始")).toBeInTheDocument();
  });

  it("prompts for the local MATLAB stack and saves user-selected paths", async () => {
    let config = {
      matlab_executable: null,
      spm_dir: null,
      dpabi_dir: null,
      matlab_version: "unspecified",
      spm_version: "unspecified",
      dpabi_version: "unspecified",
      configured: false,
    };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/environment/config") && init?.method === "PUT") {
        config = { ...JSON.parse(String(init.body)), configured: true };
        return json(config);
      }
      if (path.endsWith("/environment/config")) return json(config);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/");
    await user.click(await screen.findByRole("link", { name: "去选择路径" }));
    expect(screen.getByRole("heading", { name: "选择本机 MATLAB / SPM / DPABI" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("MATLAB 可执行文件"), "C:\\MATLAB\\bin\\matlab.exe");
    await user.type(screen.getByLabelText("SPM 目录"), "C:\\MATLAB\\toolbox\\spm");
    await user.type(screen.getByLabelText("DPABI 目录"), "C:\\MATLAB\\toolbox\\DPABI");
    await user.type(screen.getByLabelText("MATLAB 版本标签"), "R-local");
    await user.click(screen.getByRole("button", { name: "保存并重新探测" }));
    expect(await screen.findByText(/本机 MATLAB\/SPM\/DPABI 路径已保存/)).toBeInTheDocument();
    const saveCall = vi.mocked(fetch).mock.calls.find(([url, request]) => pathOf(url).endsWith("/environment/config") && request?.method === "PUT");
    expect(JSON.parse(String(saveCall?.[1]?.body)).dpabi_dir).toBe("C:\\MATLAB\\toolbox\\DPABI");
  });

  it("renders persisted late-stage progress and navigates without a page reload", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 2, manifestId: "manifest1", subjectIds: ["sub-1", "sub-2"], planRevisionId: "plan1", planState: "approved", runId: "run1", runState: "succeeded", qcReviewId: "review1", statisticalDesignId: "stat1" });
    const project = { project_id: "p1", name: "研究", source_roots: ["D:\\data"], work_root: "D:\\work", version: 2, created_at: now };
    const run = { run_id: "run1", project_id: "p1", plan_revision_id: "plan1", state: "succeeded", version: 5, attempt: 1, cancel_requested: false, error: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/projects")) return json([project]);
      if (path.endsWith("/runs")) return json([run]);
      if (path.endsWith("/skills")) return json([]);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/");
    expect((await screen.findAllByText("组统计")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByText("最近：succeeded")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "继续分析方案" }));
    expect(screen.getByRole("heading", { name: "检查顺序、参数与风险" })).toBeInTheDocument();
  });

  it("persists an explicit multi-project selection and clears downstream pointers", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 2, datasetId: "d1", manifestId: "m1", planRevisionId: "plan1", runId: "run1" });
    const projects = [
      { project_id: "p1", name: "研究一", source_roots: ["D:\\data-1"], work_root: "D:\\work-1", version: 2, created_at: now },
      { project_id: "p2", name: "研究二", source_roots: ["D:\\data-2"], work_root: "D:\\work-2", version: 4, created_at: now },
    ];
    vi.mocked(fetch).mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/projects")) return json(projects);
      if (path.endsWith("/runs")) return json([]);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/");
    const picker = await screen.findByRole("combobox", { name: /当前项目/ });
    expect(picker).toHaveValue("p1");
    await user.selectOptions(picker, "p2");

    expect(JSON.parse(localStorage.getItem("rsfmri-workspace-v1") ?? "{}")).toEqual({ projectId: "p2", projectVersion: 4 });
    expect(screen.getByText("尚无 manifest")).toBeInTheDocument();
  });

  it("creates a project, registers a dataset, and performs a read-only inspection", async () => {
    let datasetVersion = 1;
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/projects") && init?.method === "POST") return json({ project_id: "p1", name: "研究", source_roots: ["D:\\data"], work_root: "D:\\work", version: 1, created_at: now }, 201);
      if (path.endsWith("/projects/p1")) return json({ project_id: "p1", name: "研究", source_roots: ["D:\\data"], work_root: "D:\\work", version: 2, created_at: now });
      if (path.endsWith("/projects/p1/datasets")) return json({ dataset_id: "d1", project_id: "p1", name: "主数据集", source_path: "D:\\data", version: 1, current_manifest_id: null, created_at: now }, 201);
      if (path.endsWith("/datasets/d1/inspect")) { datasetVersion = 2; return json({ manifest_id: "m1", dataset_id: "d1", revision: 1, content_hash: "a".repeat(64), profile: { kind: "nifti", file_count: 4, nifti_count: 4, dicom_count: 0, subject_count: 1, warnings: [] }, subjects: [{ subject_id: "sub-synthetic", session_id: "ses-01", functional_files: ["ses-01/func.nii.gz"], anatomical_files: ["ses-01/t1.nii.gz"], dicom_files: [] }, { subject_id: "sub-synthetic", session_id: "ses-02", functional_files: ["ses-02/func.nii.gz"], anatomical_files: ["ses-02/t1.nii.gz"], dicom_files: [] }], created_at: now }, 201); }
      if (path.endsWith("/datasets/d1/demographics/import")) { datasetVersion = 3; return json({ demographics_id: "demo1", dataset_id: "d1", revision: 1, content_hash: "c".repeat(64), row_count: 1, columns: ["id"], missing_subject_ids: [], extra_subject_ids: [], created_at: now }, 201); }
      if (path.endsWith("/datasets/d1/splits")) { datasetVersion = 4; return json({ split_id: "split1", dataset_id: "d1", revision: 1, content_hash: "d".repeat(64), seed: 20260806, stratify_by: null, train_subject_ids: ["sub-synthetic"], validation_subject_ids: [], test_subject_ids: [], created_at: now }, 201); }
      if (path.endsWith("/datasets/d1")) return json({ dataset_id: "d1", project_id: "p1", name: "主数据集", source_path: "D:\\data", version: datasetVersion, current_manifest_id: "m1", created_at: now });
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/data");
    await user.type(screen.getByLabelText("只读源目录"), "D:\\data");
    await user.type(screen.getByLabelText("独立工作目录"), "D:\\work");
    await user.click(screen.getByRole("button", { name: "开始只读检查" }));
    expect(await screen.findByText(/检查完成：识别 1 名受试者/)).toBeInTheDocument();
    expect(screen.getAllByText("sub-synthetic")).toHaveLength(2);
    await user.type(screen.getByLabelText("CSV、TSV 或 XLSX 路径"), "D:\\data\\synthetic.csv");
    await user.clear(screen.getByLabelText("受试者 ID 列"));
    await user.type(screen.getByLabelText("受试者 ID 列"), "id");
    await user.selectOptions(screen.getByLabelText("文本编码"), "utf-8-sig");
    await user.type(screen.getByLabelText(/字段映射/), "group=diagnosis");
    await user.click(screen.getByRole("button", { name: "检查人口学表" }));
    expect(await screen.findByText(/人口学表已核对/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("随机种子"), "20260806");
    await user.type(screen.getByLabelText("训练比例"), "0.7");
    await user.type(screen.getByLabelText("验证比例"), "0.15");
    await user.type(screen.getByLabelText("测试比例"), "0.15");
    await user.type(screen.getByLabelText(/分层字段/), "group");
    await user.click(screen.getByRole("button", { name: "生成划分 revision" }));
    expect(await screen.findByText(/划分已冻结/)).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("rsfmri-workspace-v1") ?? "{}")).toMatchObject({
      projectVersion: 2,
      subjectIds: ["sub-synthetic"],
    });
    await user.click(screen.getByRole("button", { name: "切换项目" }));
    expect(await screen.findByText(/已清除本机浏览器/)).toBeInTheDocument();
    const calls = vi.mocked(fetch).mock.calls;
    expect(calls.some(([url]) => pathOf(url).endsWith("/datasets/d1/inspect"))).toBe(true);
    expect(calls.some(([url]) => pathOf(url).endsWith("/projects/p1"))).toBe(true);
  });

  it("registers and inspects a dataset for a restored project that has no dataset pointer", async () => {
    setWorkspace({ projectId: "p-existing", projectVersion: 3 });
    const project = { project_id: "p-existing", name: "既有研究", source_roots: ["D:\\restored-data"], work_root: "D:\\restored-work", version: 3, created_at: now };
    const dataset = { dataset_id: "d-new", project_id: "p-existing", name: "主数据集", source_path: "D:\\restored-data", version: 1, current_manifest_id: null, created_at: now };
    const manifest = { manifest_id: "m-new", dataset_id: "d-new", revision: 1, content_hash: "a".repeat(64), profile: { kind: "nifti", file_count: 2, nifti_count: 2, dicom_count: 0, subject_count: 1, warnings: [] }, subjects: [{ subject_id: "sub-restored", session_id: null, functional_files: ["func.nii.gz"], anatomical_files: ["t1.nii.gz"], dicom_files: [] }], created_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/projects/p-existing/datasets")) return json(dataset, 201);
      if (path.endsWith("/datasets/d-new/inspect")) return json(manifest, 201);
      if (path.endsWith("/datasets/d-new")) return json({ ...dataset, version: 2, current_manifest_id: "m-new" });
      if (path.endsWith("/projects/p-existing")) return json(project);
      if (path.endsWith("/projects") && init?.method === "POST") throw new Error("restored project must not be recreated");
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/data");
    expect(await screen.findByDisplayValue("D:\\restored-data")).toBeInTheDocument();
    expect(screen.getByText(/数据集：/)).toHaveTextContent("尚未登记");
    await user.click(screen.getByRole("button", { name: "开始只读检查" }));
    expect(await screen.findByText(/检查完成：识别 1 名受试者/)).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => pathOf(url).endsWith("/projects/p-existing/datasets"))).toBe(true);
  });

  it("loads an immutable plan and sends the full approval binding", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, datasetId: "d1", manifestId: "m1", manifestHash: "a".repeat(64), planRevisionId: "plan1", planVersion: 2, planHash: "b".repeat(64), planState: "awaiting_approval" });
    let approved = false;
    const plan = () => ({ plan_revision_id: "plan1", project_id: "p1", revision: 1, version: approved ? 3 : 2, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: approved ? "approved" : "awaiting_approval", plan: { skill_plan: { steps: [{ step_id: "stage", tool: { capability: "fmri.dpabi.stage_input" } }], resolved_parameters: [["preprocessing", { tr_seconds: 2 }]], environment: { matlab_version: "R2023b", spm_version: "SPM12", dpabi_version: "V8.2_240510", adapter_version: "1.0.0", environment_hash: "e".repeat(64) }, skill_locks: [] } }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now });
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/skills")) return json([]);
      if (path.endsWith("/plan-revisions/plan1/approve") && init?.method === "POST") {
        approved = true;
        return json({ approval_id: "approval1", plan_revision_id: "plan1", plan_hash: "b".repeat(64), actor: "reviewer-a", decision: "approved", reason: "reviewed evidence", created_at: now }, 201);
      }
      if (path.endsWith("/plan-revisions/plan1")) return json(plan());
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/plan");
    const button = await screen.findByRole("button", { name: "确认并批准此版本" });
    expect(button).toBeDisabled();
    expect(screen.getByText("R2023b")).toBeInTheDocument();
    expect(screen.getByText(/"tr_seconds": 2/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("计划审批人"), "reviewer-a");
    await user.type(screen.getByLabelText("计划审批理由"), "reviewed evidence");
    await user.click(button);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/计划已批准/));
    const approvalCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/approve"));
    expect(JSON.parse(String(approvalCall?.[1]?.body))).toMatchObject({ expected_version: 2, plan_hash: "b".repeat(64), actor: "reviewer-a", reason: "reviewed evidence", decision: "approved" });
  });

  it("compiles explicit preprocessing and metric choices without client-authored environment or lineage", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, datasetId: "d1", datasetVersion: 2, manifestId: "m1", manifestHash: "a".repeat(64), subjectIds: ["sub-synthetic"] });
    const compiledPlan = { plan_revision_id: "plan2", project_id: "p1", revision: 1, version: 1, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: "awaiting_approval", plan: { kind: "skill_plan" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/skills")) return json([]);
      if (path.endsWith("/skill-plans/resolve") && init?.method === "POST") return json({ skill_plan: { plan_id: "skill-plan2", project_id: "p1", dataset_ref: "d1", input_manifest_hash: "a".repeat(64), input_artifact_id: "manifest:m1", base_cfg_artifact_id: "builtin:dpabi-v82", preprocessing_parameters_hash: "c".repeat(64), skill_locks: [], resolved_parameters: [], environment: { matlab_version: "R2023b", spm_version: "SPM12", dpabi_version: "V8.2_240510", adapter_version: "1.0.0", environment_hash: "e".repeat(64) }, steps: [{ step_id: "preprocess_common", tool: { capability: "fmri.dpabi.preprocess", tool_id: "dpabi", version: "1.0.0", content_hash: "d".repeat(64) }, needs: [], consumes: [], produces: [], parameter_names: [], qc_gate: true }], artifact_expectations: [], qc_gates: [], approval_requirements: [], warnings: [], plan_hash: "b".repeat(64) }, plan_revision: compiledPlan }, 201);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/plan");
    await user.type(screen.getByLabelText("课题方案 / 预注册依据"), "protocol-v1");
    await user.selectOptions(screen.getByLabelText("科学参数来源"), "user");
    await user.type(screen.getByLabelText("参数来源证据"), "researcher-entry-2026-08-07");
    await user.type(screen.getByLabelText("TR（秒）"), "2");
    await user.type(screen.getByLabelText("期望时间点（同工作流指标必填）"), "120");
    await user.type(screen.getByLabelText("删除初始时间点数量"), "5");
    await user.click(screen.getByLabelText("ALFF"));
    await user.click(screen.getByLabelText("ReHo"));
    await user.type(screen.getByLabelText("指标频段 low Hz"), "0.01");
    await user.type(screen.getByLabelText("指标频段 high Hz"), "0.08");
    await user.selectOptions(screen.getByLabelText("ReHo 邻域"), "27");
    await user.click(screen.getByLabelText("ALFF/fALFF global_mean"));
    await user.click(screen.getByLabelText("ReHo global_mean"));
    await user.type(screen.getByLabelText("指标脑掩膜 Artifact ID（必填）"), "mask-artifact");
    await user.selectOptions(screen.getByLabelText("全局指标结果平滑"), "yes");
    await user.type(screen.getByLabelText(/全局指标结果平滑 FWHM/), "6 6 6");
    await user.selectOptions(screen.getByLabelText("ReHo 专用 SmoothReHo"), "no");
    await user.selectOptions(screen.getByLabelText("Slice timing"), "no");
    await user.selectOptions(screen.getByLabelText("Realign"), "yes");
    await user.selectOptions(screen.getByLabelText("协变量回归"), "yes");
    await user.selectOptions(screen.getByLabelText("协变量回归时点"), "after_realign");
    await user.type(screen.getByLabelText("多项式趋势阶数"), "0");
    await user.selectOptions(screen.getByLabelText("头动回归模型"), "4");
    await user.selectOptions(screen.getByLabelText("头动异常点回归"), "no");
    await user.selectOptions(screen.getByLabelText("白质信号回归"), "yes");
    await user.selectOptions(screen.getByLabelText("白质掩膜来源"), "spm");
    await user.type(screen.getByLabelText("白质掩膜阈值"), "0.8");
    await user.selectOptions(screen.getByLabelText("白质回归方法"), "mean");
    await user.selectOptions(screen.getByLabelText("脑脊液信号回归"), "yes");
    await user.selectOptions(screen.getByLabelText("脑脊液掩膜来源"), "segment");
    await user.type(screen.getByLabelText("脑脊液掩膜阈值"), "0.7");
    await user.selectOptions(screen.getByLabelText("脑脊液回归方法"), "compcor");
    await user.type(screen.getByLabelText("脑脊液 CompCor 成分数"), "5");
    await user.selectOptions(screen.getByLabelText("全局信号回归"), "yes");
    await user.selectOptions(screen.getByLabelText("全局信号掩膜来源"), "auto_mask");
    await user.selectOptions(screen.getByLabelText("全局信号回归方法"), "mean");
    await user.selectOptions(screen.getByLabelText("掩膜形变到个体空间"), "no");
    await user.selectOptions(screen.getByLabelText("协变量回归后加回均值"), "yes");
    await user.selectOptions(screen.getByLabelText("标准化"), "2");
    await user.selectOptions(screen.getByLabelText("标准化时点"), "on_functional_data");
    await user.type(screen.getByLabelText(/Bounding box/), "-90 -126 -72 90 90 108");
    await user.type(screen.getByLabelText(/体素大小/), "3 3 3");
    await user.type(screen.getByLabelText("结构像 Artifact ID"), "t1-artifact");
    await user.selectOptions(screen.getByLabelText("仿射正则化"), "mni");
    await user.selectOptions(screen.getByLabelText("单独去趋势"), "no");
    await user.selectOptions(screen.getByLabelText("滤波时点"), "after_normalize");
    await user.type(screen.getByLabelText("滤波 low Hz"), "0.01");
    await user.type(screen.getByLabelText("滤波 high Hz"), "0.08");
    await user.selectOptions(screen.getByLabelText("滤波后加回均值"), "yes");
    await user.selectOptions(screen.getByLabelText("Scrubbing"), "yes");
    await user.selectOptions(screen.getByLabelText("Scrubbing 时点"), "after_preprocessing");
    await user.selectOptions(screen.getByLabelText("Scrubbing FD 类型"), "fd_power");
    await user.type(screen.getByLabelText("FD 阈值 mm"), "0.5");
    await user.type(screen.getByLabelText("Scrubbing 前向时间点"), "1");
    await user.type(screen.getByLabelText("Scrubbing 后向时间点"), "2");
    await user.selectOptions(screen.getByLabelText("Scrubbing 方法"), "nearest");
    await user.selectOptions(screen.getByLabelText("平滑时点"), "disabled");
    await user.click(screen.getByRole("button", { name: "校验并编译计划" }));
    expect(await screen.findByText(/不可变 SkillPlan 已编译/)).toBeInTheDocument();
    const resolveCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/skill-plans/resolve"));
    const payload = JSON.parse(String(resolveCall?.[1]?.body));
    expect(payload).not.toHaveProperty("environment");
    expect(payload.request).toMatchObject({ input_artifact_id: null, request_preprocessing: true, requested_metrics: ["alff", "reho"] });
    expect(payload.request).not.toHaveProperty("input_artifact");
    expect(payload.request.preprocessing).toMatchObject({
      slice_timing: { enabled: false },
      realignment: { enabled: true, options_source: "dpabi_v82_jobmat" },
      nuisance: {
        enabled: true,
        timing: "after_realign",
        polynomial_trend: 0,
        head_motion_model: 4,
        head_motion_scrubbing: null,
        white_matter: { enabled: true, mask_source: "spm", mask_threshold: 0.8, method: "mean", compcor_components: null },
        csf: { enabled: true, mask_source: "segment", mask_threshold: 0.7, method: "compcor", compcor_components: 5 },
        global_signal: { enabled: true, mask_source: "auto_mask", method: "mean" },
        warp_masks_to_individual_space: false,
        add_mean_back: true,
      },
      normalization: { mode: 2, timing: "on_functional_data", structural_artifact_id: "t1-artifact", affine_regularization: "mni" },
      temporal_filter: { timing: "after_normalize", frequency_band: { low_hz: 0.01, high_hz: 0.08 }, add_mean_back: true },
      scrubbing: { enabled: true, timing: "after_preprocessing", censoring: { fd_type: "fd_power", fd_threshold_mm: 0.5, previous_points: 1, later_points: 2 }, method: "nearest" },
      smoothing: { timing: "disabled", method: null, fwhm_mm: null },
    });
    expect(payload.request.preprocessing.provenance).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: "user", evidence_ref: "researcher-entry-2026-08-07" }),
    ]));
    expect(payload.request.alff_falff).toMatchObject({ requested_scalings: ["global_mean"], mask_artifact_id: "mask-artifact", result_smoothing: true, result_smoothing_fwhm_mm: [6, 6, 6] });
    expect(payload.request.reho).toMatchObject({ requested_scalings: ["global_mean"], mask_artifact_id: "mask-artifact", smooth_reho: false, global_result_smoothing: true, global_result_smoothing_fwhm_mm: [6, 6, 6] });
  }, 15_000);

  it("starts every result-changing preprocessing and metric option empty", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, datasetId: "d1", datasetVersion: 2, manifestId: "m1", manifestHash: "a".repeat(64), subjectIds: ["sub-synthetic"] });
    const user = userEvent.setup();
    renderAt("/plan");
    expect(await screen.findByLabelText("科学参数来源")).toHaveValue("");
    expect(screen.getByLabelText("参数来源证据")).toHaveValue("");
    for (const label of ["Slice timing", "Realign", "协变量回归", "标准化", "单独去趋势", "滤波时点", "Scrubbing", "平滑时点"]) {
      expect(await screen.findByLabelText(label)).toHaveValue("");
    }

    await user.click(screen.getByLabelText("ALFF"));
    await user.click(screen.getByLabelText("ReHo"));
    expect(screen.getByLabelText("ALFF/fALFF raw")).not.toBeChecked();
    expect(screen.getByLabelText("ReHo raw")).not.toBeChecked();
    expect(screen.getByLabelText("指标脑掩膜 Artifact ID（必填）")).toHaveValue("");
    expect(screen.getByLabelText("全局指标结果平滑")).toHaveValue("");
    expect(screen.getByLabelText("ReHo 专用 SmoothReHo")).toHaveValue("");

    await user.selectOptions(screen.getByLabelText("协变量回归"), "yes");
    for (const label of ["协变量回归时点", "头动回归模型", "头动异常点回归", "白质信号回归", "脑脊液信号回归", "全局信号回归", "掩膜形变到个体空间", "协变量回归后加回均值"]) {
      expect(screen.getByLabelText(label)).toHaveValue("");
    }
    expect(screen.getByLabelText("多项式趋势阶数")).toHaveValue("");

    await user.selectOptions(screen.getByLabelText("标准化"), "2");
    expect(screen.getByLabelText("标准化时点")).toHaveValue("");
    expect(screen.getByLabelText("仿射正则化")).toHaveValue("");
    await user.selectOptions(screen.getByLabelText("滤波时点"), "after_normalize");
    expect(screen.getByLabelText("滤波后加回均值")).toHaveValue("");
    await user.selectOptions(screen.getByLabelText("Scrubbing"), "yes");
    for (const label of ["Scrubbing 时点", "Scrubbing FD 类型", "Scrubbing 方法"]) {
      expect(screen.getByLabelText(label)).toHaveValue("");
    }
    expect(screen.getByLabelText("Scrubbing 前向时间点")).toHaveValue("");
    expect(screen.getByLabelText("Scrubbing 后向时间点")).toHaveValue("");
    await user.selectOptions(screen.getByLabelText("平滑时点"), "on_functional_data");
    expect(screen.getByLabelText("平滑方法")).toHaveValue("");
  });

  it("compiles the explicit disabled and alternate preprocessing branches", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, datasetId: "d1", datasetVersion: 2, manifestId: "m1", manifestHash: "a".repeat(64), subjectIds: ["sub-synthetic"] });
    const compiledPlan = { plan_revision_id: "plan-alt", project_id: "p1", revision: 1, version: 1, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: "awaiting_approval", plan: { kind: "skill_plan" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/skills")) return json([]);
      if (path.endsWith("/skill-plans/resolve") && init?.method === "POST") return json({ skill_plan: { steps: [] }, plan_revision: compiledPlan }, 201);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/plan");
    await user.type(screen.getByLabelText("课题方案 / 预注册依据"), "protocol-disabled-branches");
    await user.selectOptions(screen.getByLabelText("科学参数来源"), "reviewed_preset");
    await user.type(screen.getByLabelText("参数来源证据"), "preset:dpabi-study-v2");
    await user.type(screen.getByLabelText("TR（秒）"), "2");
    await user.type(screen.getByLabelText("期望时间点（同工作流指标必填）"), "120");
    await user.type(screen.getByLabelText("删除初始时间点数量"), "5");
    await user.click(screen.getByLabelText("fALFF"));
    await user.type(screen.getByLabelText("指标频段 low Hz"), "0.01");
    await user.type(screen.getByLabelText("指标频段 high Hz"), "0.08");
    await user.click(screen.getByLabelText("ALFF/fALFF raw"));
    await user.click(screen.getByLabelText("ALFF/fALFF raw"));
    await user.click(screen.getByLabelText("ALFF/fALFF raw"));
    await user.type(screen.getByLabelText("指标脑掩膜 Artifact ID（必填）"), "mask-artifact");
    await user.selectOptions(screen.getByLabelText("全局指标结果平滑"), "no");
    await user.selectOptions(screen.getByLabelText("Slice timing"), "yes");
    await user.type(screen.getByLabelText("Slice order"), "1 3 2");
    await user.type(screen.getByLabelText("参考层"), "2");
    await user.selectOptions(screen.getByLabelText("Realign"), "no");
    await user.selectOptions(screen.getByLabelText("协变量回归"), "no");
    await user.selectOptions(screen.getByLabelText("标准化"), "0");
    await user.selectOptions(screen.getByLabelText("单独去趋势"), "yes");
    await user.selectOptions(screen.getByLabelText("滤波时点"), "disabled");
    await user.selectOptions(screen.getByLabelText("Scrubbing"), "no");
    await user.selectOptions(screen.getByLabelText("平滑时点"), "on_functional_data");
    await user.selectOptions(screen.getByLabelText("平滑方法"), "1");
    await user.type(screen.getByLabelText(/FWHM mm/), "4 4 4");
    await user.click(screen.getByRole("button", { name: "校验并编译计划" }));
    expect(await screen.findByText(/不可变 SkillPlan 已编译/)).toBeInTheDocument();
    const call = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/skill-plans/resolve"));
    const body = JSON.parse(String(call?.[1]?.body)).request;
    expect(body.requested_metrics).toEqual(["falff"]);
    expect(body.preprocessing).toMatchObject({
      expected_time_points: 120,
      slice_timing: { enabled: true, slice_count: 3, slice_order: [1, 3, 2], reference_slice: 2 },
      realignment: { enabled: false, options_source: null },
      nuisance: { enabled: false, timing: null },
      normalization: { mode: 0, timing: null },
      detrend: true,
      temporal_filter: { timing: "disabled", frequency_band: null, add_mean_back: null },
      scrubbing: { enabled: false, timing: null },
      smoothing: { timing: "on_functional_data", method: 1, fwhm_mm: [4, 4, 4] },
    });
    expect(body.alff_falff).toMatchObject({ requested_metrics: ["falff"], requested_scalings: ["raw"], mask_artifact_id: "mask-artifact", result_smoothing: false });
    expect(body.reho).toBeNull();
  }, 12_000);

  it("queues an approved mock run and displays persisted run state", async () => {
    setWorkspace({ projectId: "p1", planRevisionId: "plan1", planHash: "b".repeat(64), planState: "approved" });
    let runs: unknown[] = [];
    const run = { run_id: "run1", project_id: "p1", plan_revision_id: "plan1", state: "queued", version: 1, attempt: 0, cancel_requested: false, error: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/runs") && init?.method === "POST") { runs = [run]; return json(run, 202); }
      if (path.endsWith("/runs/run1/cancel") && init?.method === "POST") {
        const cancelled = { ...run, state: "cancelling", version: 2, cancel_requested: true };
        runs = [cancelled];
        return json(cancelled);
      }
      if (path.endsWith("/runs")) return json(runs);
      if (path.endsWith("/runs/run1/events")) return Promise.resolve(new Response("", { status: 200, headers: { "Content-Type": "text/event-stream" } }));
      if (path.endsWith("/runs/run1/artifacts")) return json([]);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/runs");
    await user.click(screen.getByRole("button", { name: "创建已审批计划的 Mock 运行" }));
    expect(await screen.findByText(/任务已进入本机 SQLite 队列/)).toBeInTheDocument();
    expect((await screen.findAllByText("queued")).length).toBeGreaterThan(0);
    const cancel = screen.getByRole("button", { name: "请求取消" });
    expect(cancel).toBeDisabled();
    await user.type(screen.getByLabelText("运行操作理由"), "发现输入清单需要复核");
    await user.click(cancel);
    expect(await screen.findByText("取消请求已记录。")).toBeInTheDocument();
    const cancelCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/runs/run1/cancel"));
    expect(JSON.parse(String(cancelCall?.[1]?.body))).toMatchObject({ expected_version: 1, reason: "发现输入清单需要复核" });
  });

  it("requires explicit confirmation before queuing a MATLAB run", async () => {
    setWorkspace({ projectId: "p1", planRevisionId: "plan1", planHash: "b".repeat(64), planState: "approved" });
    const run = { run_id: "matlab-run", project_id: "p1", plan_revision_id: "plan1", state: "queued", version: 1, attempt: 0, cancel_requested: false, error: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/runs") && init?.method === "POST") return json(run, 202);
      if (path.endsWith("/runs")) return json([]);
      if (path.endsWith("/runs/matlab-run")) return json(run);
      if (path.endsWith("/runs/matlab-run/events")) return json([]);
      if (path.endsWith("/runs/matlab-run/artifacts")) return json([]);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/runs");
    await user.selectOptions(await screen.findByLabelText("执行后端"), "matlab");
    vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: "确认并创建 MATLAB 运行" }));
    expect(vi.mocked(fetch).mock.calls.some(([url, init]) => pathOf(url).endsWith("/runs") && init?.method === "POST")).toBe(false);
    await user.click(screen.getByRole("button", { name: "确认并创建 MATLAB 运行" }));
    expect(await screen.findByText(/真实 MATLAB 任务已进入隔离队列/)).toBeInTheDocument();
    const createCall = vi.mocked(fetch).mock.calls.find(([url, init]) => pathOf(url).endsWith("/runs") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ execution_backend: "matlab", real_execution_confirmed: true });
  });

  it("requeues a persisted retryable run through the versioned action", async () => {
    setWorkspace({ projectId: "p1", runId: "run-retry", runVersion: 3, runState: "failed_retryable" });
    let current = { run_id: "run-retry", project_id: "p1", plan_revision_id: "plan1", state: "failed_retryable", version: 3, attempt: 1, cancel_requested: false, error: "temporary", created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/runs/run-retry/retry") && init?.method === "POST") {
        current = { ...current, state: "queued", version: 4, error: "" };
        return json(current);
      }
      if (path.endsWith("/runs")) return json([current]);
      if (path.endsWith("/runs/run-retry/events")) return Promise.resolve(new Response("", { status: 200, headers: { "Content-Type": "text/event-stream" } }));
      if (path.endsWith("/runs/run-retry/artifacts")) return json([]);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/runs");
    const retry = await screen.findByRole("button", { name: "重试" });
    expect(retry).toBeDisabled();
    await user.type(screen.getByLabelText("运行操作理由"), "临时执行器故障已解除");
    await waitFor(() => expect(retry).toBeEnabled());
    await user.click(retry);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/重试已排队/));
    expect((await screen.findAllByText("queued")).length).toBeGreaterThan(0);
    const retryCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/runs/run-retry/retry"));
    expect(JSON.parse(String(retryCall?.[1]?.body))).toMatchObject({ expected_version: 3, reason: "临时执行器故障已解除" });
  });

  it("continues one-shot SSE polling from the last event cursor and refreshes run state", async () => {
    setWorkspace({ projectId: "p1", runId: "run-live", runVersion: 1, runState: "queued" });
    let runListCalls = 0;
    const observedCursors: string[] = [];
    const event = (eventId: number, eventType: string) => ({ event_id: eventId, trace_id: `trace-${eventId}`, project_id: "p1", run_id: "run-live", event_type: eventType, severity: "info", payload: {}, created_at: now });
    vi.mocked(fetch).mockImplementation((input) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://local");
      const path = url.pathname;
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/runs")) {
        runListCalls += 1;
        const state = runListCalls <= 1 ? "queued" : runListCalls === 2 ? "running" : "qc_review";
        return json([{ run_id: "run-live", project_id: "p1", plan_revision_id: "plan1", state, version: runListCalls, attempt: 1, cancel_requested: false, error: null, created_at: now, updated_at: now }]);
      }
      if (path.endsWith("/runs/run-live/events")) {
        const cursor = url.searchParams.get("after_event_id") ?? "0";
        observedCursors.push(cursor);
        const payload = cursor === "0" ? event(1, "RunStarted") : event(2, "RunAwaitingQc");
        return Promise.resolve(new Response(`id: ${payload.event_id}\nevent: ${payload.event_type}\ndata: ${JSON.stringify(payload)}\n\n`, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
      }
      if (path.endsWith("/runs/run-live/artifacts")) return json([]);
      return defaultApi(input);
    });

    renderAt("/runs");
    expect(await screen.findByText(/RunStarted/)).toBeInTheDocument();
    expect(await screen.findByText(/RunAwaitingQc/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(observedCursors.slice(0, 2)).toEqual(["0", "1"]);
    expect(JSON.parse(localStorage.getItem("rsfmri-workspace-v1") ?? "{}")).toMatchObject({ runState: "qc_review" });
  });

  it("creates and approves a typed QC revision", async () => {
    setWorkspace({ projectId: "p1", runId: "run-qc", runVersion: 4, runState: "qc_review", subjectIds: ["sub-1"] });
    let approved = false;
    const run = () => ({ run_id: "run-qc", project_id: "p1", plan_revision_id: "plan1", state: approved ? "succeeded" : "qc_review", version: approved ? 5 : 4, attempt: 1, cancel_requested: false, error: null, created_at: now, updated_at: now });
    const artifact = { artifact_id: "artifact-image", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "output/reho.nii", checksum: "c".repeat(64), size_bytes: 20, provenance: {}, created_at: now };
    const logArtifact = { artifact_id: "artifact-log", project_id: "p1", run_id: "run-qc", artifact_type: "log.matlab", relative_path: "logs/matlab.log", checksum: "d".repeat(64), size_bytes: 10, provenance: {}, created_at: now };
    const check = { code: "visual.metric_review", severity: "info", passed: true, evidence_artifact_ids: ["artifact-log"], message: "metric and log reviewed" };
    const review = () => ({ review: { review_revision_id: "review1", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["artifact-image"], checks: [check], included_subject_ids: ["sub-1"], excluded_subject_ids: [], exclusion_reasons: [], approved, approved_by: approved ? "qc-reviewer" : null, approval_reason: approved ? "reviewed evidence and subject list" : null, content_hash: "q".repeat(64) }, run_id: "run-qc", project_id: "p1", revision: 1, version: approved ? 2 : 1, state: approved ? "approved" : "draft", created_at: now, updated_at: now });
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/runs/run-qc/artifacts")) return json([artifact, logArtifact]);
      if (path.endsWith("/runs/run-qc")) return json(run());
      if (path.endsWith("/qc-reviews") && init?.method === "POST") return json(review(), 201);
      if (path.endsWith("/qc-reviews/review1/approve")) { approved = true; return json(review()); }
      if (path.endsWith("/qc-reviews/review1")) return json(review());
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/qc");
    const create = await screen.findByRole("button", { name: "创建 QC revision" });
    expect(create).toBeDisabled();
    expect(screen.queryByLabelText(/选择指标 Artifact artifact-log/)).not.toBeInTheDocument();
    await user.click(screen.getByLabelText(/选择指标 Artifact artifact-image/));
    await user.type(screen.getByLabelText("QC check code"), "visual.metric_review");
    await user.selectOptions(screen.getByLabelText("QC check severity"), "info");
    await user.selectOptions(screen.getByLabelText("QC check passed"), "yes");
    await user.type(screen.getByLabelText("QC check message"), "metric and log reviewed");
    await user.click(screen.getByLabelText(/QC check 证据 artifact-log/));
    await user.type(screen.getByLabelText(/纳入受试者/), "sub-1");
    await user.selectOptions(screen.getByLabelText("是否排除受试者"), "none");
    expect(create).toBeEnabled();
    await user.click(create);
    expect(await screen.findByText(/QC revision 已冻结/)).toBeInTheDocument();
    const createCall = vi.mocked(fetch).mock.calls.find(([url, init]) => pathOf(url).endsWith("/qc-reviews") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
      metric_artifact_ids: ["artifact-image"],
      checks: [check],
      included_subject_ids: ["sub-1"],
      excluded_subject_ids: [],
      exclusion_reasons: [],
    });
    const approveButton = screen.getByRole("button", { name: "人工批准并冻结" });
    expect(approveButton).toBeDisabled();
    await user.type(screen.getByLabelText("QC 审批人"), "qc-reviewer");
    await user.type(screen.getByLabelText("QC 审批理由"), "reviewed evidence and subject list");
    await user.click(approveButton);
    expect(await screen.findByText(/人工 QC 已批准/)).toBeInTheDocument();
    const approvalCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/qc-reviews/review1/approve"));
    expect(JSON.parse(String(approvalCall?.[1]?.body))).toMatchObject({ actor: "qc-reviewer", reason: "reviewed evidence and subject list", approved: true });
  });

  it("restores and displays every frozen QC decision from its revision run", async () => {
    setWorkspace({ projectId: "p1", runId: "later-stat-run", qcReviewId: "review-restored", subjectIds: ["sub-1", "sub-2"] });
    const run = { run_id: "run-qc-source", project_id: "p1", plan_revision_id: "plan1", state: "qc_review", version: 4, attempt: 1, cancel_requested: false, error: null, created_at: now, updated_at: now };
    const artifacts = [
      { artifact_id: "metric-1", project_id: "p1", run_id: run.run_id, artifact_type: "metric.reho", relative_path: "output/reho.nii", checksum: "c".repeat(64), size_bytes: 20, provenance: {}, created_at: now },
      { artifact_id: "evidence-1", project_id: "p1", run_id: run.run_id, artifact_type: "log.matlab", relative_path: "logs/matlab.log", checksum: "d".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
    ];
    const review = { review: { review_revision_id: "review-restored", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["metric-1"], checks: [{ code: "motion.review", severity: "warning", passed: true, evidence_artifact_ids: ["evidence-1"], message: "头动图已人工复核" }, { code: "coverage.review", severity: "info", passed: true, evidence_artifact_ids: ["metric-1"], message: "全脑覆盖完整" }], included_subject_ids: ["sub-1"], excluded_subject_ids: ["sub-2"], exclusion_reasons: [["sub-2", "超过预注册头动阈值"]], approved: false, approved_by: null, approval_reason: null, content_hash: "q".repeat(64) }, run_id: run.run_id, project_id: "p1", revision: 2, version: 1, state: "draft", created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/qc-reviews/review-restored")) return json(review);
      if (path.endsWith("/runs/run-qc-source/artifacts")) return json(artifacts);
      if (path.endsWith("/runs/run-qc-source")) return json(run);
      return defaultApi(input);
    });

    renderAt("/qc");
    const frozenReview = await screen.findByRole("region", { name: "冻结的 QC revision 内容" });
    expect(frozenReview).toHaveTextContent("sub-1");
    expect(frozenReview).toHaveTextContent("sub-2");
    expect(frozenReview).toHaveTextContent("全脑覆盖完整");
    expect(frozenReview).toHaveTextContent("超过预注册头动阈值");
    expect(vi.mocked(fetch).mock.calls.some(([url]) => pathOf(url).endsWith("/runs/later-stat-run"))).toBe(false);
  });

  it.each([
    ["/qc", "先审查，再进入统计"],
    ["/statistics", "版本化统计设计"],
    ["/settings", "运行条件与模型配置"],
  ])("renders the guarded %s page", async (route, heading) => {
    renderAt(route);
    expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalled());
  });

  it("keeps credentials out of the Agent request and submits only a redacted task summary", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1 });
    const profile = { profile: { id: "provider1", provider: "openai-compatible", base_url: "https://example.test", model: "configured-model", api_key_env: "TEST_API_KEY", priority: 10, capabilities: ["json_object"], timeout_seconds: 45 }, version: 1, created_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/model-profiles")) return json([profile]);
      if (path.endsWith("/agent/tasks") && init?.method === "POST") return json({ task_id: "task1", project_id: "p1", state: "succeeded", result: { recommendation: { summary: "结构化解释", proposed_skill_request: null, warnings: [], unresolved_questions: [], requires_user_confirmation: true }, routing: { task_type: "plan_explainer", selected_profile_id: "provider1", candidate_profile_ids: ["provider1"], required_capabilities: ["json_object"], reason: "capability" }, context_hash: "c".repeat(64), attempted_profile_ids: ["provider1"] }, created_at: now }, 201);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/agent");
    const send = screen.getByRole("button", { name: "发送安全结构摘要" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    expect(await screen.findByText("结构化解释")).toBeInTheDocument();
    const taskCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/agent/tasks"));
    const body = String(taskCall?.[1]?.body);
    expect(body).not.toContain("TEST_API_KEY");
    expect(body).not.toContain("user_question");
    expect(JSON.parse(body).request.summary).toEqual({
      purpose: "explain_current_plan",
      metric_kinds: [],
      workflow_state: "not_started",
      issue_count: 0,
      has_blocking_issues: false,
    });

    await user.selectOptions(screen.getByLabelText("任务类型"), "log_summarizer");
    await user.selectOptions(screen.getByLabelText("模型"), "provider1");
    await user.click(send);
    await user.selectOptions(screen.getByLabelText("任务类型"), "report_writer");
    await user.click(send);
    const taskCalls = vi.mocked(fetch).mock.calls.filter(([url]) => pathOf(url).endsWith("/agent/tasks"));
    const logRequest = JSON.parse(String(taskCalls.at(-2)?.[1]?.body)).request;
    const reportRequest = JSON.parse(String(taskCalls.at(-1)?.[1]?.body)).request;
    expect(logRequest.summary.purpose).toBe("summarize_registered_run");
    expect(reportRequest.summary.purpose).toBe("draft_method_report");
    expect(reportRequest.preferred_profile_id).toBe("provider1");
    expect(JSON.stringify(reportRequest)).not.toContain("TEST_API_KEY");
  });

  it("stores only non-secret provider metadata and runs a lightweight connectivity test", async () => {
    let profiles: unknown[] = [];
    const profile = { profile: { id: "deepseek-default", provider: "openai-compatible", base_url: "https://api.deepseek.com", model: "configured-model", api_key_env: "DEEPSEEK_API_KEY", priority: 10, capabilities: ["json_object"], timeout_seconds: 45 }, version: 1, created_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/environment/probe")) return json({ ready: true, environment_hash: "e".repeat(64), components: [{ name: "MATLAB", available: true, evidence: "R2023b" }] });
      if (path.endsWith("/model-profiles") && init?.method === "POST") { profiles = [profile]; return json(profile, 201); }
      if (path.endsWith("/model-profiles")) return json(profiles);
      if (path.endsWith("/providers/test")) return json({ profile_id: "deepseek-default", available: true, routing: { task_type: "plan_explainer", selected_profile_id: "deepseek-default", candidate_profile_ids: ["deepseek-default"], required_capabilities: ["json_object"], reason: "configured" }, context_hash: "c".repeat(64) });
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/settings");
    await user.type(screen.getByLabelText("配置 ID"), "deepseek-default");
    await user.type(screen.getByLabelText("API 基址"), "https://api.deepseek.com");
    await user.type(screen.getByLabelText("密钥环境变量名"), "DEEPSEEK_API_KEY");
    await user.type(screen.getByLabelText("模型名称（手动输入）"), "configured-model");
    await user.click(screen.getByRole("button", { name: "保存模型配置" }));
    expect(await screen.findByText(/模型配置已保存/)).toBeInTheDocument();
    const testButton = await screen.findByRole("button", { name: "测试" });
    await user.click(testButton);
    expect(await screen.findByText(/轻量测试成功/)).toBeInTheDocument();
    const saved = vi.mocked(fetch).mock.calls.find(([url, init]) => pathOf(url).endsWith("/model-profiles") && init?.method === "POST");
    const savedBody = JSON.parse(String(saved?.[1]?.body));
    expect(savedBody.profile.id).toBe("deepseek-default");
    expect(savedBody.api_key).toBeNull();
    expect(String(saved?.[1]?.body)).not.toContain("replace-locally");
  });

  it("restores a frozen statistical design and its approval evidence", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 2, runId: "run-qc", qcReviewId: "review-stat", statisticalDesignId: "stat-restored" });
    const qc = { review: { review_revision_id: "review-stat", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["metric-1"], checks: [], included_subject_ids: ["sub-1", "sub-2"], excluded_subject_ids: [], exclusion_reasons: [], approved: true, approved_by: "reviewer", approval_reason: "reviewed", content_hash: "q".repeat(64) }, run_id: "run-qc", project_id: "p1", revision: 1, version: 2, state: "approved", created_at: now, updated_at: now };
    const artifacts = [{ artifact_id: "mask-1", project_id: "p1", run_id: "run-qc", artifact_type: "mask.brain", relative_path: "output/mask.nii", checksum: "c".repeat(64), size_bytes: 20, provenance: {}, created_at: now }];
    const design = { design: { revision_id: "science-revision", test: "one_sample_t", subject_order: ["sub-1", "sub-2"], images: [{ subject_id: "sub-1", artifact_id: "image-1", group: null, condition: null }, { subject_id: "sub-2", artifact_id: "image-2", group: null, condition: null }], group_order: [], condition_order: [], covariates: [], contrast: [1], one_sample_baseline: 0, mask_artifact_id: "mask-1", tail: "two_sided", missing_value_policy: "error", qc_review_revision_id: "review-stat", qc_review_hash: "q".repeat(64) }, correction: { method: "fdr", q_threshold: 0.05, statistic_type: "T", mask_artifact_id: "mask-1", df1: 1, df2: null }, design_matrix: [[1], [1]], plan_revision: { plan_revision_id: "stat-restored", project_id: "p1", revision: 2, version: 3, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: "awaiting_approval", plan: { kind: "statistical_design" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now } };
    vi.mocked(fetch).mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/qc-reviews/review-stat")) return json(qc);
      if (path.endsWith("/runs/run-qc/artifacts")) return json(artifacts);
      if (path.endsWith("/statistical-designs/stat-restored")) return json(design);
      return defaultApi(input);
    });

    renderAt("/statistics");
    expect(await screen.findByRole("heading", { name: "2 × 1" })).toBeInTheDocument();
    expect(screen.getByLabelText("检验类型")).toHaveValue("one_sample_t");
    expect(screen.getByText("sub-1 → sub-2")).toBeInTheDocument();
    expect(screen.getByText("fdr")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批准统计设计" })).toBeDisabled();
    expect(JSON.parse(localStorage.getItem("rsfmri-workspace-v1") ?? "{}")).toMatchObject({ statisticalDesignVersion: 3, statisticalDesignHash: "b".repeat(64) });
  });

  it("builds, validates, approves, and submits a QC-bound statistical design", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, manifestHash: "a".repeat(64), runId: "run-qc", qcReviewId: "review1", qcReviewHash: "d".repeat(64) });
    let planState = "draft";
    let planVersion = 1;
    const subjects = ["sub-1", "sub-2", "sub-3"];
    const plan = () => ({ plan_revision_id: "stat-plan", project_id: "p1", revision: 1, version: planVersion, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: planState, plan: { kind: "statistical_design" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now });
    const design = { revision_id: "design1", test: "one_sample_t", subject_order: subjects, images: subjects.map((subject_id, index) => ({ subject_id, artifact_id: `image${index + 1}`, group: null, condition: null })), group_order: [], condition_order: [], covariates: [{ name: "age", centering: "grand_mean", values: [{ subject_id: "sub-1", value: 23 }, { subject_id: "sub-2", value: 30 }, { subject_id: "sub-3", value: 35 }] }], contrast: [1, 0], one_sample_baseline: 0, mask_artifact_id: "mask1", tail: "two_sided", missing_value_policy: "error", qc_review_revision_id: "review1", qc_review_hash: "d".repeat(64) };
    const view = () => ({ design, correction: { method: "fdr", q_threshold: 0.05, mask_artifact_id: "mask1", statistic_type: "T", df1: 1, df2: null }, design_matrix: [[1, -6.33], [1, 0.67], [1, 5.67]], plan_revision: plan() });
    const qc = { review: { review_revision_id: "review1", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["image1", "image2", "image3"], checks: [{ code: "qc.complete", severity: "info", passed: true, evidence_artifact_ids: ["image1", "image2", "image3"], message: "reviewed" }], included_subject_ids: subjects, excluded_subject_ids: [], exclusion_reasons: [], approved: true, approved_by: "reviewer", approval_reason: "reviewed", content_hash: "d".repeat(64) }, run_id: "run-qc", project_id: "p1", revision: 1, version: 2, state: "approved", created_at: now, updated_at: now };
    const artifacts = [
      { artifact_id: "image1", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "reho.nii", checksum: "1".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "image2", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "reho-2.nii", checksum: "3".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "image3", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "reho-3.nii", checksum: "4".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "mask1", project_id: "p1", run_id: "run-qc", artifact_type: "brain_mask", relative_path: "mask.nii", checksum: "2".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
    ];
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/qc-reviews/review1")) return json(qc);
      if (path.endsWith("/runs/run-qc/artifacts")) return json(artifacts);
      if (path.endsWith("/runs/stat-run/artifacts")) return json([]);
      if (path.endsWith("/statistical-designs") && init?.method === "POST") return json(view(), 201);
      if (path.endsWith("/statistical-designs/stat-plan/validate")) { planState = "awaiting_approval"; planVersion = 2; return json(view()); }
      if (path.endsWith("/plan-revisions/stat-plan/approve")) { planState = "approved"; planVersion = 3; return json({ approval_id: "approval-stat", plan_revision_id: "stat-plan", plan_hash: "b".repeat(64), actor: "statistics-reviewer", decision: "approved", reason: "reviewed design and correction", created_at: now }, 201); }
      if (path.endsWith("/plan-revisions/stat-plan")) return json(plan());
      if (path.endsWith("/statistics/runs")) return json({ run_id: "stat-run", project_id: "p1", plan_revision_id: "stat-plan", state: "queued", version: 1, attempt: 0, cancel_requested: false, error: null, created_at: now, updated_at: now }, 202);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/statistics");
    await user.selectOptions(await screen.findByLabelText("检验类型"), "one_sample_t");
    await user.selectOptions(screen.getByLabelText("尾部"), "two_sided");
    await user.type(screen.getByLabelText("单样本基线"), "0");
    await user.selectOptions(screen.getByLabelText("缺失值策略"), "error");
    const mapping = await screen.findByLabelText(/影像映射/);
    await user.clear(mapping);
    await user.type(mapping, "sub-1 | image1\nsub-2 | image2\nsub-3 | image3");
    await user.type(screen.getByLabelText(/协变量/), "age | grand_mean | sub-1=23, sub-2=30, sub-3=35");
    await user.selectOptions(screen.getByLabelText("脑掩膜 Artifact"), "mask1");
    await user.selectOptions(screen.getByLabelText("多重比较"), "fdr");
    await user.type(screen.getByLabelText("q 阈值"), "0.05");
    await user.click(screen.getByRole("button", { name: "生成设计矩阵" }));
    expect(await screen.findByText(/统计设计已创建/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "验证设计" }));
    expect(await screen.findByText(/等待明确批准/)).toBeInTheDocument();
    const approveButton = screen.getByRole("button", { name: "批准统计设计" });
    expect(approveButton).toBeDisabled();
    await user.type(screen.getByLabelText("统计设计审批人"), "statistics-reviewer");
    await user.type(screen.getByLabelText("统计设计审批理由"), "reviewed design and correction");
    await user.click(approveButton);
    expect(await screen.findByText(/统计设计已批准/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("执行后端"), "matlab");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "提交统计运行" }));
    expect(await screen.findByText(/真实 MATLAB 统计任务已进入隔离队列/)).toBeInTheDocument();
    const createCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/statistical-designs"));
    const createPayload = JSON.parse(String(createCall?.[1]?.body));
    expect(createPayload).not.toHaveProperty("environment_hash");
    expect(createPayload.design.covariates[0]).toMatchObject({ name: "age", centering: "grand_mean", values: [{ subject_id: "sub-1", value: 23 }, { subject_id: "sub-2", value: 30 }, { subject_id: "sub-3", value: 35 }] });
    const approvalCall = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/plan-revisions/stat-plan/approve"));
    expect(JSON.parse(String(approvalCall?.[1]?.body))).toMatchObject({ actor: "statistics-reviewer", reason: "reviewed design and correction", decision: "approved" });
    const statisticsRunCall = vi.mocked(fetch).mock.calls.find(([url, init]) => pathOf(url).endsWith("/statistics/runs") && init?.method === "POST");
    expect(JSON.parse(String(statisticsRunCall?.[1]?.body))).toMatchObject({ execution_backend: "matlab", real_execution_confirmed: true });
  });

  it("builds an explicitly ordered independent test with GRF and within-group covariate", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, manifestHash: "a".repeat(64), runId: "run-qc", qcReviewId: "review-two" });
    const qc = { review: { review_revision_id: "review-two", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["image1", "image2", "image3", "image4"], checks: [{ code: "qc.complete", severity: "info", passed: true, evidence_artifact_ids: ["image1", "image2", "image3", "image4"], message: "reviewed" }], included_subject_ids: ["sub-1", "sub-2", "sub-3", "sub-4"], excluded_subject_ids: [], exclusion_reasons: [], approved: true, approved_by: "reviewer", approval_reason: "reviewed", content_hash: "d".repeat(64) }, run_id: "run-qc", project_id: "p1", revision: 1, version: 2, state: "approved", created_at: now, updated_at: now };
    const artifacts = [
      { artifact_id: "image1", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "a.nii", checksum: "1".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "image2", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "b.nii", checksum: "2".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "image3", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "c.nii", checksum: "4".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "image4", project_id: "p1", run_id: "run-qc", artifact_type: "metric.reho", relative_path: "d.nii", checksum: "5".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
      { artifact_id: "mask1", project_id: "p1", run_id: "run-qc", artifact_type: "brain_mask", relative_path: "mask.nii", checksum: "3".repeat(64), size_bytes: 10, provenance: {}, created_at: now },
    ];
    const plan = { plan_revision_id: "stat-two", project_id: "p1", revision: 1, version: 1, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: "draft", plan: { kind: "statistical_design" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/qc-reviews/review-two")) return json(qc);
      if (path.endsWith("/runs/run-qc/artifacts")) return json(artifacts);
      if (path.endsWith("/statistical-designs") && init?.method === "POST") return json({
        design: { revision_id: "d-two", test: "independent_two_sample_t", subject_order: ["sub-1", "sub-2", "sub-3", "sub-4"], images: [{ subject_id: "sub-1", artifact_id: "image1", group: "control", condition: null }, { subject_id: "sub-2", artifact_id: "image2", group: "control", condition: null }, { subject_id: "sub-3", artifact_id: "image3", group: "patient", condition: null }, { subject_id: "sub-4", artifact_id: "image4", group: "patient", condition: null }], group_order: ["control", "patient"], condition_order: [], covariates: [{ name: "age", centering: "within_group", values: [{ subject_id: "sub-1", value: 20 }, { subject_id: "sub-2", value: 24 }, { subject_id: "sub-3", value: 30 }, { subject_id: "sub-4", value: 34 }] }], contrast: [1, 0, 0], one_sample_baseline: null, mask_artifact_id: "mask1", tail: "one_sided_positive", missing_value_policy: "exclude_explicitly", qc_review_revision_id: "review-two", qc_review_hash: "d".repeat(64) },
        correction: { method: "grf", voxel_p_threshold: 0.001, cluster_p_threshold: 0.05, two_tailed: false, mask_artifact_id: "mask1", statistic_type: "T", df1: 1, df2: null, smoothness_mode: "dpabi_header_or_estimate", smoothness_dlh: null },
        design_matrix: [[1, 0, -2], [1, 0, 2], [0, 1, -2], [0, 1, 2]],
        plan_revision: plan,
      }, 201);
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/statistics");
    await user.selectOptions(await screen.findByLabelText("检验类型"), "independent_two_sample_t");
    await user.selectOptions(screen.getByLabelText("尾部"), "one_sided_positive");
    await user.type(screen.getByLabelText(/组顺序/), "control | patient");
    await user.selectOptions(screen.getByLabelText("缺失值策略"), "exclude_explicitly");
    await user.clear(screen.getByLabelText(/影像映射/));
    await user.type(screen.getByLabelText(/影像映射/), "sub-1 | image1 | control\nsub-2 | image2 | control\nsub-3 | image3 | patient\nsub-4 | image4 | patient");
    await user.type(screen.getByLabelText(/协变量/), "age | within_group | sub-1=20, sub-2=24, sub-3=30, sub-4=34");
    await user.selectOptions(screen.getByLabelText("脑掩膜 Artifact"), "mask1");
    await user.selectOptions(screen.getByLabelText("多重比较"), "grf");
    await user.type(screen.getByLabelText("voxel p 阈值"), "0.001");
    await user.type(screen.getByLabelText("cluster p 阈值"), "0.05");
    await user.selectOptions(screen.getByLabelText("GRF 平滑度来源"), "dpabi_header_or_estimate");
    await user.click(screen.getByRole("button", { name: "生成设计矩阵" }));
    expect(await screen.findByText(/统计设计已创建/)).toBeInTheDocument();
    const call = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/statistical-designs"));
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body.design).toMatchObject({ test: "independent_two_sample_t", group_order: ["control", "patient"], tail: "one_sided_positive", missing_value_policy: "exclude_explicitly", contrast: [1, 0, 0] });
    expect(body.correction).toMatchObject({ method: "grf", voxel_p_threshold: 0.001, cluster_p_threshold: 0.05, two_tailed: false, smoothness_mode: "dpabi_header_or_estimate", smoothness_dlh: null });
  });

  it("builds a paired negative-tail design with explicit GRF smoothness", async () => {
    setWorkspace({ projectId: "p1", projectVersion: 1, manifestHash: "a".repeat(64), runId: "run-paired", qcReviewId: "review-paired" });
    const subjects = ["sub-1", "sub-2"];
    const qc = { review: { review_revision_id: "review-paired", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["a1", "b1", "a2", "b2"], checks: [{ code: "qc.complete", severity: "info", passed: true, evidence_artifact_ids: ["a1", "b1", "a2", "b2"], message: "reviewed" }], included_subject_ids: subjects, excluded_subject_ids: [], exclusion_reasons: [], approved: true, approved_by: "reviewer", approval_reason: "reviewed", content_hash: "d".repeat(64) }, run_id: "run-paired", project_id: "p1", revision: 1, version: 2, state: "approved", created_at: now, updated_at: now };
    const artifacts = ["a1", "b1", "a2", "b2"].map((artifact_id, index) => ({ artifact_id, project_id: "p1", run_id: "run-paired", artifact_type: "metric.reho", relative_path: `${artifact_id}.nii`, checksum: String(index + 1).repeat(64), size_bytes: 10, provenance: {}, created_at: now }));
    artifacts.push({ artifact_id: "mask1", project_id: "p1", run_id: "run-paired", artifact_type: "brain_mask", relative_path: "mask.nii", checksum: "9".repeat(64), size_bytes: 10, provenance: {}, created_at: now });
    const plan = { plan_revision_id: "stat-paired", project_id: "p1", revision: 1, version: 1, plan_hash: "b".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: "draft", plan: { kind: "statistical_design" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now };
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
      if (path.endsWith("/qc-reviews/review-paired")) return json(qc);
      if (path.endsWith("/runs/run-paired/artifacts")) return json(artifacts);
      if (path.endsWith("/statistical-designs") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return json({ design: body.design, correction: body.correction, design_matrix: [[1, 1, 0], [-1, 1, 0], [1, 0, 1], [-1, 0, 1]], plan_revision: plan }, 201);
      }
      return defaultApi(input);
    });
    const user = userEvent.setup();
    renderAt("/statistics");
    await user.selectOptions(await screen.findByLabelText("检验类型"), "paired_t");
    await user.selectOptions(screen.getByLabelText("尾部"), "one_sided_negative");
    await user.type(screen.getByLabelText(/条件顺序/), "before | after");
    await user.selectOptions(screen.getByLabelText("缺失值策略"), "error");
    await user.clear(screen.getByLabelText(/影像映射/));
    await user.type(screen.getByLabelText(/影像映射/), "sub-1 | a1 | b1\nsub-2 | a2 | b2");
    await user.selectOptions(screen.getByLabelText("脑掩膜 Artifact"), "mask1");
    await user.selectOptions(screen.getByLabelText("多重比较"), "grf");
    await user.type(screen.getByLabelText("voxel p 阈值"), "0.001");
    await user.type(screen.getByLabelText("cluster p 阈值"), "0.05");
    await user.selectOptions(screen.getByLabelText("GRF 平滑度来源"), "provided_dlh");
    await user.type(screen.getByLabelText("DLH"), "0.25");
    await user.click(screen.getByRole("button", { name: "生成设计矩阵" }));
    expect(await screen.findByText(/统计设计已创建/)).toBeInTheDocument();
    const call = vi.mocked(fetch).mock.calls.find(([url]) => pathOf(url).endsWith("/statistical-designs"));
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body.design).toMatchObject({ test: "paired_t", condition_order: ["before", "after"], tail: "one_sided_negative", covariates: [] });
    expect(body.design.images).toEqual([
      { subject_id: "sub-1", artifact_id: "a1", group: null, condition: "before" },
      { subject_id: "sub-2", artifact_id: "a2", group: null, condition: "before" },
      { subject_id: "sub-1", artifact_id: "b1", group: null, condition: "after" },
      { subject_id: "sub-2", artifact_id: "b2", group: null, condition: "after" },
    ]);
    expect(body.correction).toMatchObject({ method: "grf", two_tailed: false, smoothness_mode: "provided_dlh", smoothness_dlh: 0.25, df1: 1 });
  });

  it("renders unknown routes as the dashboard", () => {
    renderAt("/not-found");
    expect(screen.getByRole("heading", { name: "从数据到可信结果" })).toBeInTheDocument();
  });
});
