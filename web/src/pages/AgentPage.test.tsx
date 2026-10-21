import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetWorkspace } from "../workspace";
import { AgentPage, AnalysisProposalActions } from "./AgentPage";
import type { Conversation } from "../api/types";

const now = "2026-09-18T00:00:00Z";
const initialWindowWidth = window.innerWidth;

function json(value: unknown, status = 200): Promise<Response> {
  const body = status === 204 ? null : JSON.stringify(value);
  return Promise.resolve(
    new Response(body, { status, headers: { "Content-Type": "application/json" } }),
  );
}

function pathOf(input: RequestInfo | URL): string {
  return new URL(
    typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
    "http://local",
  ).pathname;
}

function conversation(overrides: Record<string, unknown> = {}) {
  return {
    conversation_id: "work-1",
    mode: "work",
    title: "DemoData ALFF",
    workspace_path: "D:/demo-work",
    preferred_profile_id: null,
    project_id: null,
    active_run_id: null,
    version: 1,
    created_at: now,
    updated_at: now,
    messages: [],
    tool_calls: [],
    ...overrides,
  };
}

function compositeGoal(status: string, intentTaskId: string, reasonCode: string | null = null) {
  return {
    intent_task_id: intentTaskId,
    conversation_id: "work-1",
    project_id: "project-1",
    analysis_request_hash: "a".repeat(64),
    preprocessing_task_id: `prep-${intentTaskId}`,
    analysis_proposal_outbox_intent_task_id: intentTaskId,
    analysis_work_task_id: `analysis-${intentTaskId}`,
    result_interpretation_outbox_task_id: `analysis-${intentTaskId}`,
    status,
    preprocessing_status: "completed",
    analysis_proposal_status: "proposal_ready",
    analysis_status: "running",
    result_interpretation_status: null,
    continuation_message_id: null,
    reason_code: reasonCode,
  };
}

function externalAnalysisGoal(status: string, intentTaskId: string, reasonCode: string | null = null) {
  return {
    ...compositeGoal(status, intentTaskId, reasonCode),
    goal_kind: "analysis_only_external_import",
    preprocessing_task_id: null,
    preprocessing_status: null,
    analysis_proposal_outbox_intent_task_id: null,
    analysis_proposal_status: null,
  };
}

function message(id: string, role: "user" | "assistant", content: string, payload = {}) {
  return {
    message_id: id,
    conversation_id: "work-1",
    sequence: 1,
    role,
    content,
    payload,
    created_at: now,
  };
}

function toolCall(
  id: string,
  toolName: string,
  stepIndex: number | null,
  riskTier: string | null,
  durationMs: number | null,
) {
  return {
    tool_call_id: id,
    conversation_id: "work-1",
    user_message_id: "u1",
    tool_name: toolName,
    status: "succeeded",
    input: {},
    output: {},
    error: null,
    step_index: stepIndex,
    risk_tier: riskTier,
    duration_ms: durationMs,
    created_at: now,
    updated_at: now,
  };
}

function profile(id: string, capabilities: string[]) {
  return {
    profile: {
      id,
      provider: "openai-compatible",
      base_url: "https://api.deepseek.com",
      model: "deepseek-chat",
      api_key_env: "DEEPSEEK_API_KEY",
      priority: 100,
      capabilities,
      timeout_seconds: 45,
      context_window_tokens: 16384,
      max_output_tokens: 2048,
    },
    created_at: now,
  };
}

const notFound = () =>
  json({ error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);

type Options = {
  workspaceCheck?: () => Promise<Response>;
  conversations?: () => Promise<Response>;
  created?: () => Promise<Response>;
  run?: () => Promise<Response>;
  picked?: () => Promise<Response>;
  profiles?: () => Promise<Response>;
  project?: (projectId: string, signal?: AbortSignal | null) => Promise<Response>;
  dataset?: (datasetId: string, signal?: AbortSignal | null) => Promise<Response>;
  manifest?: (manifestId: string, signal?: AbortSignal | null) => Promise<Response>;
  artifacts?: (runId: string, signal?: AbortSignal | null) => Promise<Response>;
  diagnosis?: (runId: string, signal?: AbortSignal | null) => Promise<Response>;
  workspaceConsents?: (conversationId: string, signal?: AbortSignal | null) => Promise<Response>;
  workspaceFiles?: (conversationId: string, directory: string, signal?: AbortSignal | null) => Promise<Response>;
  grantWorkspaceConsent?: () => Promise<Response>;
  turn?: (init?: RequestInit) => Promise<Response>;
  draftPatch?: (init?: RequestInit) => Promise<Response>;
  confirmation?: (init?: RequestInit) => Promise<Response>;
  review?: (init?: RequestInit) => Promise<Response>;
  conversation?: () => Promise<Response>;
  compositeGoals?: (conversationId: string, signal?: AbortSignal | null) => Promise<Response>;
  retryOutbox?: (init?: RequestInit) => Promise<Response>;
  retryResultInterpretation?: (init?: RequestInit) => Promise<Response>;
  selectAgent?: (init?: RequestInit) => Promise<Response>;
  selectCompositeGoal?: (intentTaskId: string, init?: RequestInit) => Promise<Response>;
  createProject?: (init?: RequestInit) => Promise<Response>;
  createDataset?: (projectId: string, init?: RequestInit) => Promise<Response>;
  inspectDataset?: (datasetId: string, init?: RequestInit) => Promise<Response>;
};

function installFetch(options: Options = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = pathOf(input);
    const method = init?.method ?? "GET";
    const selectedGoal = path.match(/\/composite-goals\/([^/]+)\/select$/)?.[1];
    if (selectedGoal) return options.selectCompositeGoal?.(decodeURIComponent(selectedGoal), init) ?? notFound();
    if (path.endsWith("/work-agent")) return options.selectAgent?.(init) ?? notFound();
    if (path.endsWith("/work-review")) return options.review?.(init) ?? notFound();
    if (path.endsWith("/workspaces/check")) return options.workspaceCheck?.() ?? notFound();
    if (path.endsWith("/model-profiles")) return (options.profiles ?? (() => json([])))();
    if (path.endsWith("/providers/models")) return json({ models: ["deepseek-chat", "deepseek-reasoner"] });
    if (path.endsWith("/environment/config")) return json({
      allowed_work_root: "D:/app/work", matlab_executable: null, spm_dir: null, dpabi_dir: null,
      matlab_version: "unspecified", spm_version: "unspecified", dpabi_version: "unspecified", configured: false,
    });
    if (path.endsWith("/workspace/consents")) {
      if (method === "POST") return (options.grantWorkspaceConsent ?? (() => json({ error: { code: "workspace_tool_probe_failed", message: "所选模型未能通过工具调用测试。", details: {}, trace_id: "probe-trace" } }, 422)))();
      const conversationId = path.match(/\/conversations\/([^/]+)\/workspace\/consents$/)?.[1] ?? "";
      return options.workspaceConsents?.(decodeURIComponent(conversationId), init?.signal) ?? json([]);
    }
    if (path.endsWith("/workspace/files")) {
      const conversationId = path.match(/\/conversations\/([^/]+)\/workspace\/files$/)?.[1] ?? "";
      const directory = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://local").searchParams.get("directory") ?? "";
      return options.workspaceFiles?.(decodeURIComponent(conversationId), directory, init?.signal) ?? notFound();
    }
    if (path.endsWith("/projects") && method === "POST") return options.createProject?.(init) ?? notFound();
    const projectDatasets = path.match(/\/projects\/([^/]+)\/datasets$/)?.[1];
    if (projectDatasets && method === "POST") return options.createDataset?.(decodeURIComponent(projectDatasets), init) ?? notFound();
    const datasetInspection = path.match(/\/datasets\/([^/]+)\/inspect$/)?.[1];
    if (datasetInspection && method === "POST") return options.inspectDataset?.(decodeURIComponent(datasetInspection), init) ?? notFound();
    if (path.endsWith("/workspaces/pick")) {
      return (options.picked ?? (() => json({ path: "D:/picked-work" })))();
    }
    if (path.endsWith("/conversations") && method === "POST") {
      return (options.created ?? (() => json(conversation({ conversation_id: "work-2", title: "新会话" }))))();
    }
    if (path.endsWith("/conversations")) {
      return (options.conversations ?? (() => json([conversation()])))();
    }
    const projectId = path.match(/\/projects\/([^/]+)$/)?.[1];
    if (projectId) return options.project?.(decodeURIComponent(projectId), init?.signal) ?? notFound();
    const datasetId = path.match(/\/datasets\/([^/]+)$/)?.[1];
    if (datasetId) return options.dataset?.(decodeURIComponent(datasetId), init?.signal) ?? notFound();
    const manifestId = path.match(/\/manifests\/([^/]+)$/)?.[1];
    if (manifestId) return options.manifest?.(decodeURIComponent(manifestId), init?.signal) ?? notFound();
    if (path.endsWith("/composite-goals")) {
      const encodedConversationId = path.match(/\/conversations\/([^/]+)\/composite-goals$/)?.[1] ?? "";
      return options.compositeGoals?.(decodeURIComponent(encodedConversationId), init?.signal) ?? json([]);
    }
    if (path.endsWith("/work-draft")) return options.draftPatch?.(init) ?? notFound();
    if (path.endsWith("/work-confirmation")) return options.confirmation?.(init) ?? notFound();
    if (path.includes("/analysis-outbox/") && path.endsWith("/retry")) return options.retryOutbox?.(init) ?? notFound();
    if (path.includes("/statistical-result-interpretations/") && path.endsWith("/retry")) return options.retryResultInterpretation?.(init) ?? notFound();
    if (path.endsWith("/conversations/work-1")) return options.conversation?.() ?? notFound();
    if (path.endsWith("/turns")) {
      if (options.turn) return options.turn(init);
      const current = conversation({
        messages: [
          message("u1", "user", "准备方案"),
          message("a1", "assistant", "方案已准备，请明确确认后启动。"),
        ],
      });
      return json({
        conversation: current,
        user_message: current.messages[0],
        assistant_message: current.messages[1],
        tool_call: null,
      });
    }
    const runArtifacts = path.match(/\/runs\/([^/]+)\/artifacts$/)?.[1];
    if (runArtifacts) return options.artifacts?.(decodeURIComponent(runArtifacts), init?.signal) ?? json([]);
    const runDiagnosis = path.match(/\/runs\/([^/]+)\/diagnosis$/)?.[1];
    if (runDiagnosis) return options.diagnosis?.(decodeURIComponent(runDiagnosis), init?.signal) ?? notFound();
    if (path.includes("/runs/") && path.endsWith("/events")) return json([]);
    if (path.includes("/runs/") && path.endsWith("/qc-review")) return json(null);
    if (path.includes("/plans/") || path.includes("/plan-revisions/")) return json({});
    if (path.includes("/runs/")) {
      const runId = path.match(/\/runs\/([^/]+)/)?.[1] ?? "run-default";
      return (options.run ?? (() => json({
        run_id: decodeURIComponent(runId), project_id: null, plan_revision_id: "plan-default",
        state: "succeeded", version: 1, attempt: 1, cancel_requested: false, error: null,
        qc_mode: "require", qc_status: "skipped", stage: "completed", stage_progress: 1,
        created_at: now, updated_at: now,
      })))();
    }
    return notFound();
  });
}

describe("agent workbench page", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    window.localStorage.clear();
    window.sessionStorage.clear();
    resetWorkspace();
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute("open", ""); } });
    Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute("open"); } });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    Object.defineProperty(window, "innerWidth", { configurable: true, value: initialWindowWidth });
    vi.restoreAllMocks();
  });

  it("hides preprocessing-source registration when no exact preprocessing task and Run pair is available", async () => {
    installFetch({ conversations: () => json([conversation({
      project_id: "project-1",
      work_tasks: [
        { agent_role: "preprocessing", work_task_id: "prep-without-run", run_id: null },
        { agent_role: "analysis", work_task_id: "analysis-1", run_id: "run-analysis" },
      ],
    })]) });

    render(<AgentPage />);

    await screen.findByRole("option", { name: "DemoData ALFF" });
    expect(screen.queryByRole("checkbox", { name: "以指定的预处理结果新建分析目标" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "新建独立分析目标" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("预处理来源")).not.toBeInTheDocument();
  });

  it("registers a scanned BIDS source and opens a project session while keeping the unbound conversation", async () => {
    const user = userEvent.setup();
    const project = {
      project_id: "project-demo", name: "rs-fMRI 项目 · demo-work",
      source_roots: ["D:/demo-work"], work_root: "D:/app/work/demo-work-project", version: 1, created_at: now,
    };
    const dataset = {
      dataset_id: "dataset-demo", project_id: project.project_id, name: "demo-work",
      source_path: "D:/demo-work", version: 1, current_manifest_id: "manifest-demo", created_at: now,
    };
    const manifest = {
      manifest_id: "manifest-demo", dataset_id: dataset.dataset_id, revision: 1,
      content_hash: "a".repeat(64), created_at: now,
      profile: { kind: "bids", file_count: 4, nifti_count: 2, dicom_count: 0, subject_count: 1, issues: [], warnings: [] },
      subjects: [{ subject_id: "sub-01", session_id: null, functional_files: ["sub-01_task-rest_bold.nii.gz"], anatomical_files: ["sub-01_T1w.nii.gz"], dicom_files: [] }],
    };
    const boundConversation = conversation({
      conversation_id: "work-bound", title: project.name, project_id: project.project_id,
      workspace_path: "D:/demo-work",
    });
    const fetchMock = installFetch({
      conversations: () => json([conversation()]),
      workspaceCheck: () => json({
        path: "D:/demo-work", kind: "bids", file_count: 4, nifti_count: 2, dicom_count: 0,
        subject_count: 1, functional_subject_count: 1, anatomical_subject_count: 1,
        input_stage: null, selected_input_stage: null, stage_selection_required: false,
        stage_candidates: [], output_directories: [], invalid_nifti_files: [], issues: [], warnings: [],
        subjects: [], checked_at: now,
      }),
      createProject: () => json(project, 201),
      createDataset: () => json(dataset, 201),
      inspectDataset: () => json(manifest),
      created: () => json(boundConversation, 201),
      dataset: () => json(dataset),
      manifest: () => json(manifest),
    });

    render(<AgentPage />);

    await user.click(await screen.findByText("将当前 BIDS 数据集登记为项目"));
    expect(await screen.findByLabelText("项目名称")).toHaveValue("rs-fMRI 项目 · demo-work");
    await waitFor(() => expect(screen.getByLabelText("独立计算目录")).toHaveValue("D:/app/work/demo-work-project"));
    await user.click(screen.getByRole("checkbox", { name: /我已核对来源目录/ }));
    await user.click(screen.getByRole("button", { name: "登记项目并扫描清单" }));

    await screen.findByText("登记人口学表");
    expect(screen.getByRole("option", { name: "DemoData ALFF" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: project.name })).toBeInTheDocument();
    const projectCall = fetchMock.mock.calls.find(([input]) => pathOf(input) === "/api/v1/projects");
    expect(projectCall?.[1]?.body).toBe(JSON.stringify({
      name: project.name, source_roots: ["D:/demo-work"], work_root: "D:/app/work/demo-work-project",
    }));
    expect(fetchMock.mock.calls.some(([input]) => pathOf(input) === "/api/v1/datasets/dataset-demo/inspect")).toBe(true);
  });

  it("limits metric-only checkpoint sources to preprocessing Runs from the active conversation", async () => {
    const manifestHash = "a".repeat(64);
    const active = conversation({
      project_id: "project-1",
      foreground_agent_role: "analysis",
      messages: [message("context-1", "assistant", "上下文", {
        project: { project_id: "project-1", version: 7 },
        dataset: { dataset_id: "dataset-1", version: 3 },
        manifest: { manifest_id: "manifest-1", content_hash: manifestHash },
      })],
      work_tasks: [
        { agent_role: "preprocessing", work_task_id: "prep-current", run_id: "run-current" },
      ],
    });
    const inactive = conversation({
      conversation_id: "work-2",
      title: "另一会话",
      project_id: "project-1",
      work_tasks: [{ agent_role: "preprocessing", work_task_id: "prep-other", run_id: "run-other" }],
    });
    const fetchMock = installFetch({
      conversations: () => json([active, inactive]),
      project: (projectId) => json({ project_id: projectId, version: 7 }),
      dataset: (datasetId) => json({ dataset_id: datasetId, project_id: "project-1", version: 3, current_manifest_id: "manifest-1" }),
      manifest: (manifestId) => json({
        manifest_id: manifestId,
        dataset_id: "dataset-1",
        content_hash: manifestHash,
        subjects: [{ subject_id: "sub-01", session_id: "ses-01" }],
      }),
      run: () => json({ run_id: "run-current", project_id: "project-1", state: "succeeded", version: 1 }),
    });

    render(<AgentPage />);

    const sourceRun = await screen.findByLabelText("来源预处理运行");
    expect(sourceRun).toHaveValue("");
    expect(within(sourceRun).getByRole("option", { name: /run-current/ })).toBeInTheDocument();
    expect(within(sourceRun).queryByRole("option", { name: /run-other|run-analysis|run-unrelated/ })).not.toBeInTheDocument();
    const requestedPaths = fetchMock.mock.calls.map(([input]) => pathOf(input));
    expect(requestedPaths.some((path) => path.endsWith("/runs"))).toBe(false);
    expect(requestedPaths.some((path) => path.endsWith("/runs/run-other"))).toBe(false);
    expect(requestedPaths.some((path) => path.endsWith("/runs/run-unrelated"))).toBe(false);
    expect(requestedPaths.some((path) => path.endsWith("/skill-plans/dry-run"))).toBe(false);
  });

  it("sends the explicitly selected preprocessing task and Run only when opted in", async () => {
    let requestBody: Record<string, unknown> | null = null;
    const responseTurn = vi.fn((init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      const current = conversation({ project_id: "project-1" });
      const userMessage = message("u1", "user", "分析这个预处理结果");
      const assistantMessage = message("a1", "assistant", "已登记分析目标");
      return json({ conversation: current, user_message: userMessage, assistant_message: assistantMessage, tool_call: null });
    });
    installFetch({
      conversations: () => json([conversation({
        project_id: "project-1",
        foreground_agent_role: "analysis",
        work_tasks: [
          { agent_role: "preprocessing", work_task_id: "prep-first", run_id: "run-first" },
          { agent_role: "preprocessing", work_task_id: "prep-selected", run_id: "run-selected" },
        ],
      })]),
      turn: responseTurn,
    });

    render(<AgentPage />);
    const user = userEvent.setup();
    const registration = await screen.findByRole("checkbox", { name: "以指定的预处理结果新建分析目标" });
    await user.click(registration);
    const sourceSelect = screen.getByLabelText("预处理来源");
    await user.selectOptions(sourceSelect, "[\"prep-selected\",\"run-selected\"]");
    await user.type(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), "分析这个预处理结果");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(responseTurn).toHaveBeenCalledOnce());
    expect(requestBody).toMatchObject({
      agent_role: "analysis",
      project_id: "project-1",
      preprocessing_task_id: "prep-selected",
      target_run_id: "run-selected",
      external_analysis_import_id: null,
      plan_revision_id: null,
      expected_plan_hash: null,
    });
  });

  it("leaves preprocessing-source identifiers out of ordinary analysis turns", async () => {
    let requestBody: Record<string, unknown> | null = null;
    const responseTurn = vi.fn((init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      const current = conversation({ project_id: "project-1" });
      return json({ conversation: current, user_message: message("u1", "user", "准备分析"), assistant_message: message("a1", "assistant", "继续"), tool_call: null });
    });
    installFetch({
      conversations: () => json([conversation({
        project_id: "project-1",
        foreground_agent_role: "analysis",
        work_tasks: [{ agent_role: "preprocessing", work_task_id: "prep-1", run_id: "run-1" }],
      })]),
      turn: responseTurn,
    });

    render(<AgentPage />);
    const user = userEvent.setup();
    await screen.findByRole("checkbox", { name: "以指定的预处理结果新建分析目标" });
    await user.type(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), "准备分析");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(responseTurn).toHaveBeenCalledOnce());
    expect(requestBody).not.toHaveProperty("preprocessing_task_id");
    expect(requestBody).not.toHaveProperty("target_run_id", "run-1");
  });

  it("shows the conversation-scoped composite goal stages and refreshes on request", async () => {
    const goals = [
      { ...compositeGoal("waiting_preprocessing", "goal-prep", "preprocessing_in_progress"), analysis_summary: "对 ALFF 做两组比较", preprocessing_status: "running", analysis_proposal_status: null, analysis_status: null },
      { ...compositeGoal("waiting_analysis", "goal-analysis", "analysis_proposal_pending"), analysis_proposal_status: "pending", analysis_status: null },
      { ...compositeGoal("waiting_result_interpretation", "goal-result", "result_interpretation_pending"), analysis_status: "completed", result_interpretation_status: "ready" },
      { ...compositeGoal("waiting_user", "goal-user", "analysis_decision_required"), analysis_status: "waiting_user" },
      { ...compositeGoal("blocked", "goal-blocked", "analysis_terminal_block"), analysis_status: "blocked" },
      { ...compositeGoal("completed", "goal-complete", "result_interpretation_delivered"), analysis_status: "completed", result_interpretation_status: "completed", continuation_message_id: "assistant-result" },
      { ...compositeGoal("indeterminate", "goal-check", "result_continuation_mismatch") },
      { ...compositeGoal("waiting_review", "goal-review", "preprocessing_plan_review"), preprocessing_status: "waiting_user" },
      { ...compositeGoal("waiting_run", "goal-run", "analysis_run_pending"), analysis_status: "waiting_run" },
    ];
    const loadGoals = vi.fn(() => json(goals));
    installFetch({ compositeGoals: loadGoals });

    render(<AgentPage />);

    expect(await screen.findByRole("region", { name: "自动化流程进度" })).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(9);
    expect(within(screen.getAllByRole("article")[0]).getByText("对 ALFF 做两组比较")).toBeInTheDocument();
    for (const label of ["等待预处理", "等待审查", "等待运行完成", "等待分析", "等待结果解释", "需要你处理", "已阻断", "已完成", "状态待核验"]) {
      expect(screen.getByText(label, { selector: "span" })).toBeInTheDocument();
    }
    expect(within(screen.getAllByRole("article")[2]).getByText("已就绪")).toBeInTheDocument();
    expect(within(screen.getAllByRole("article")[5]).getAllByText("已完成")).toHaveLength(4);
    expect(screen.getByText("结果说明与当前任务的关联需要核验。")).toBeInTheDocument();
    expect(screen.queryByText("goal-prep")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "刷新进度" }));
    await waitFor(() => expect(loadGoals).toHaveBeenCalledTimes(2));
  });

  it("shows mixed goal types with only their recorded stages and supported actions", async () => {
    const goals = [
      { ...compositeGoal("waiting_user", "prep-goal"), analysis_status: "waiting_user" },
      { ...externalAnalysisGoal("waiting_user", "external-user", "analysis_user_decision"), analysis_status: "waiting_user" },
      { ...externalAnalysisGoal("waiting_result_interpretation", "external-result", "result_interpretation_pending"), analysis_status: "completed", result_interpretation_status: "ready" },
      { ...externalAnalysisGoal("completed", "external-complete", "result_interpretation_delivered"), analysis_status: "completed", result_interpretation_status: "completed", continuation_message_id: "delivered" },
      { ...externalAnalysisGoal("waiting_analysis", "external-draft", "analysis_task_not_created"), analysis_work_task_id: null, analysis_status: null },
      { ...externalAnalysisGoal("waiting_user", "external-draft-user", "analysis_draft_user_decision"), analysis_work_task_id: null, analysis_status: null },
      { ...externalAnalysisGoal("blocked", "external-source-changed", "analysis_only_source_changed"), analysis_status: "blocked" },
    ];
    installFetch({ compositeGoals: () => json(goals) });
    render(<AgentPage />);
    await screen.findByRole("region", { name: "自动化流程进度" });
    const cards = screen.getAllByRole("article");
    expect(within(cards[0]).getByText("预处理与分析")).toBeInTheDocument();
    expect(within(cards[0]).getAllByRole("listitem")).toHaveLength(4);
    expect(within(cards[0]).getByRole("button", { name: "继续此分析" })).toBeInTheDocument();
    for (const card of cards.slice(1)) {
      expect(within(card).getByText("外部指标分析")).toBeInTheDocument();
      expect(within(card).getAllByRole("listitem")).toHaveLength(2);
      expect(within(card).queryByText("预处理：")).not.toBeInTheDocument();
      expect(within(card).queryByText("分析方案：")).not.toBeInTheDocument();
    }
    expect(within(cards[1]).getByRole("button", { name: "继续此分析" })).toBeInTheDocument();
    expect(within(cards[2]).getByRole("button", { name: "查看此分析" })).toBeInTheDocument();
    expect(within(cards[3]).getByRole("button", { name: "查看此分析" })).toBeInTheDocument();
    expect(within(cards[4]).getByRole("button", { name: "继续此分析" })).toBeInTheDocument();
    expect(within(cards[5]).getByRole("button", { name: "继续此分析" })).toBeInTheDocument();
    expect(within(cards[6]).queryByRole("button")).not.toBeInTheDocument();
    expect(within(cards[1]).getByText("统计分析需要你的决定。")).toBeInTheDocument();
    expect(within(cards[2]).getByText("已就绪")).toBeInTheDocument();
    expect(within(cards[3]).getByText("结果说明已送达当前会话。")).toBeInTheDocument();
    expect(within(cards[4]).getByText("正在准备统计分析草稿。")).toBeInTheDocument();
    expect(within(cards[4]).getAllByText("尚未进入")).toHaveLength(2);
    expect(within(cards[5]).getByText("统计分析草稿需要你补充或确认。")).toBeInTheDocument();
    expect(within(cards[6]).getByText("外部指标的纳入修订已变化，请重新核实分析来源。")).toBeInTheDocument();
    expect(screen.queryByText("等待预处理")).not.toBeInTheDocument();
    expect(screen.queryByText("external-user")).not.toBeInTheDocument();
    expect(screen.queryByText("external-complete")).not.toBeInTheDocument();
  });

  it("selects an analysis-only goal and sends the server-selected intent as a turn assertion", async () => {
    let requestBody: Record<string, unknown> | null = null;
    const savedDraft = message("draft-external", "assistant", "外部指标分析草稿", {
      agent_role: "analysis",
      analysis_draft: {
        analysis_intent_task_id: "external-goal",
        hash: "b".repeat(64),
        draft: { external_import_id: "included-external" },
      },
    });
    const initial = conversation({ project_id: "project-1", foreground_agent_role: "preprocessing", messages: [savedDraft] });
    const selected = conversation({
      project_id: "project-1",
      version: 2,
      foreground_agent_role: "analysis",
      messages: [savedDraft],
      work_tasks: [{ agent_role: "analysis", work_task_id: "stat-task", analysis_intent_task_id: "external-goal" }],
    });
    const selectGoal = vi.fn((intentTaskId: string, init?: RequestInit) => {
      expect(intentTaskId).toBe("external-goal");
      expect(JSON.parse(String(init?.body))).toEqual({ expected_version: 1 });
      return json(selected);
    });
    const turn = vi.fn((init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      return json({ conversation: selected, user_message: message("u", "user", "继续目标"), assistant_message: message("a", "assistant", "继续分析"), tool_call: null });
    });
    installFetch({
      conversations: () => json([initial]),
      compositeGoals: () => json([{ ...externalAnalysisGoal("waiting_user", "external-goal", "analysis_draft_user_decision"), analysis_status: null, analysis_work_task_id: null }]),
      selectCompositeGoal: selectGoal,
      turn,
    });

    render(<AgentPage />);
    fireEvent.click(await screen.findByRole("button", { name: "继续此分析" }));
    await waitFor(() => expect(selectGoal).toHaveBeenCalledOnce());
    await screen.findByText("当前负责：数据分析 Agent");
    fireEvent.change(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), { target: { value: "继续这个外部指标分析" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(turn).toHaveBeenCalledOnce());
    expect(requestBody).toMatchObject({
      agent_role: "analysis",
      analysis_intent_task_id: "external-goal",
      external_analysis_import_id: "included-external",
    });
    expect(requestBody).not.toHaveProperty("start_new_analysis_goal");
  });

  it("lets auto dispatch a new preprocessing plan without inheriting the prior analysis goal", async () => {
    let requestBody: Record<string, unknown> | null = null;
    const current = conversation({
      project_id: "project-1",
      foreground_agent_role: "analysis",
      messages: [message("external-draft", "assistant", "外部指标分析草稿", {
        agent_role: "analysis",
        analysis_draft: {
          analysis_intent_task_id: "external-goal",
          hash: "e".repeat(64),
          draft: { external_import_id: "included-external" },
        },
      })],
      work_tasks: [{ agent_role: "analysis", work_task_id: "analysis-task", analysis_intent_task_id: "external-goal" }],
    });
    const turn = vi.fn((init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      return json({ conversation: current, user_message: message("u", "user", "新建预处理方案"), assistant_message: message("a", "assistant", "已准备预处理草稿"), tool_call: null });
    });
    installFetch({ conversations: () => json([current]), turn });

    render(<AgentPage />);
    const prompt = "请为当前项目创建一份待审 DPABI 预处理草稿，只计算 ALFF/fALFF，不做统计分析。";
    fireEvent.change(await screen.findByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), { target: { value: prompt } });
    expect(screen.getByText("当前负责：预处理 Agent")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(turn).toHaveBeenCalledOnce());
    expect(requestBody).toMatchObject({
      agent_role: null,
      external_analysis_import_id: null,
    });
    expect(requestBody).not.toHaveProperty("analysis_intent_task_id");
  });

  it("starts an independent external-import goal only after an explicit choice", async () => {
    let requestBody: Record<string, unknown> | null = null;
    const current = conversation({
      project_id: "project-1",
      foreground_agent_role: "analysis",
      messages: [message("draft", "assistant", "已选外部指标", {
        agent_role: "analysis",
        analysis_draft: {
          analysis_intent_task_id: "current-goal",
          hash: "c".repeat(64),
          draft: { external_import_id: "included-import" },
        },
      })],
      work_tasks: [{ agent_role: "analysis", work_task_id: "current-task", analysis_intent_task_id: "current-goal" }],
    });
    const turn = vi.fn((init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      return json({ conversation: current, user_message: message("u", "user", "独立分析"), assistant_message: message("a", "assistant", "已登记独立目标"), tool_call: null });
    });
    installFetch({ conversations: () => json([current]), turn });

    render(<AgentPage />);
    const createGoal = await screen.findByRole("checkbox", { name: "新建独立分析目标" });
    expect(createGoal).not.toBeChecked();
    expect(screen.getByText("仅使用当前已纳入的外部指标；勾选后，本轮将创建独立目标，不会继续当前已选分析目标。")).toBeInTheDocument();
    fireEvent.click(createGoal);
    expect(createGoal).toBeChecked();
    fireEvent.change(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), { target: { value: "独立分析这个指标" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(turn).toHaveBeenCalledOnce());
    expect(requestBody).toMatchObject({
      agent_role: null,
      external_analysis_import_id: "included-import",
      start_new_analysis_goal: true,
    });
    expect(requestBody).not.toHaveProperty("analysis_intent_task_id");
  });

  it("does not carry an independent-goal choice across conversation switches", async () => {
    let requestBody: Record<string, unknown> | null = null;
    const savedConversation = (conversationId: string, intentId: string, importId: string) => conversation({
      conversation_id: conversationId,
      project_id: "project-1",
      foreground_agent_role: "analysis",
      messages: [message(`draft-${conversationId}`, "assistant", "外部指标草稿", {
        agent_role: "analysis",
        analysis_draft: { analysis_intent_task_id: intentId, hash: "d".repeat(64), draft: { external_import_id: importId } },
      })],
      work_tasks: [{ agent_role: "analysis", work_task_id: `task-${conversationId}`, analysis_intent_task_id: intentId }],
    });
    const first = savedConversation("work-1", "goal-first", "import-first");
    const second = savedConversation("work-2", "goal-second", "import-second");
    const turn = vi.fn((init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      return json({ conversation: second, user_message: message("u", "user", "继续第二个目标"), assistant_message: message("a", "assistant", "已继续"), tool_call: null });
    });
    installFetch({ conversations: () => json([first, second]), turn });

    render(<AgentPage />);
    const createGoal = await screen.findByRole("checkbox", { name: "新建独立分析目标" });
    fireEvent.click(createGoal);
    expect(createGoal).toBeChecked();
    fireEvent.change(screen.getByLabelText("当前会话"), { target: { value: "work-2" } });
    const secondCreateGoal = await screen.findByRole("checkbox", { name: "新建独立分析目标" });
    expect(secondCreateGoal).not.toBeChecked();
    fireEvent.change(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), { target: { value: "继续第二个外部分析" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(turn).toHaveBeenCalledOnce());
    expect(requestBody).toMatchObject({
      analysis_intent_task_id: "goal-second",
      external_analysis_import_id: "import-second",
    });
    expect(requestBody).not.toHaveProperty("start_new_analysis_goal");
  });

  it("selects an exact analysis goal and uses its saved source instead of a newer unrelated draft", async () => {
    const goals = [
      { ...compositeGoal("waiting_user", "goal/selected"), analysis_status: "waiting_user" },
      { ...compositeGoal("waiting_user", "goal-other"), analysis_status: null, analysis_proposal_status: "waiting_user" },
      compositeGoal("completed", "goal-complete"),
      compositeGoal("waiting_analysis", "goal-running"),
    ];
    const messages = [
      message("draft-selected", "assistant", "已保存选中目标", { agent_role: "analysis", analysis_draft: {
        analysis_intent_task_id: "goal/selected", hash: "b".repeat(64), draft: { external_import_id: null },
      } }),
      message("draft-newer", "assistant", "另一个分析目标", { agent_role: "analysis", analysis_draft: {
        analysis_intent_task_id: "goal-other", hash: "c".repeat(64), draft: { external_import_id: "unrelated-external" },
      } }),
    ];
    const preprocessingTasks = [
      { agent_role: "preprocessing", work_task_id: "prep-1", run_id: "run-1" },
    ];
    const initial = conversation({ project_id: "project-1", version: 5, foreground_agent_role: "analysis", work_tasks: preprocessingTasks });
    const selectGoal = vi.fn((intentTaskId: string, init?: RequestInit) => {
      expect(intentTaskId).toBe("goal/selected");
      expect(JSON.parse(String(init?.body))).toEqual({ expected_version: 5 });
      return json({ ...initial, version: 6, foreground_agent_role: "analysis", messages, work_tasks: [
        ...preprocessingTasks,
        { agent_role: "analysis", analysis_intent_task_id: "goal/selected", draft_hash: "b".repeat(64) },
      ] });
    });
    const turn = vi.fn((init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      expect(body.agent_role).toBe("analysis");
      expect(body.external_analysis_import_id).toBeNull();
      expect(body).not.toHaveProperty("preprocessing_task_id");
      return json({ conversation: initial, user_message: message("u", "user", "继续"), assistant_message: message("a", "assistant", "已继续"), tool_call: null });
    });
    installFetch({ conversations: () => json([initial]), compositeGoals: () => json(goals), selectCompositeGoal: selectGoal, turn });
    render(<AgentPage />);
    const registration = await screen.findByRole("checkbox", { name: "以指定的预处理结果新建分析目标" });
    fireEvent.click(registration);
    expect(registration).toBeChecked();
    const buttons = await screen.findAllByRole("button", { name: "继续此分析" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(selectGoal).toHaveBeenCalledOnce());
    await screen.findByText("当前负责：数据分析 Agent");
    expect(registration).not.toBeChecked();
    expect(screen.getByLabelText("Work 负责 Agent")).toHaveValue("analysis");
    fireEvent.change(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案"), { target: { value: "继续这个分析目标" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(turn).toHaveBeenCalledOnce());
  });

  it("disables goal selection while a selection is pending and refreshes after a conflict", async () => {
    let finishSelection!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finishSelection = resolve; });
    const refresh = vi.fn(() => json(conversation({ version: 8, foreground_agent_role: "preprocessing" })));
    const selectGoal = vi.fn(() => pending);
    installFetch({ compositeGoals: () => json([{ ...compositeGoal("waiting_user", "goal-1"), analysis_status: "waiting_user" }]), selectCompositeGoal: selectGoal, conversation: refresh });
    render(<AgentPage />);
    const button = await screen.findByRole("button", { name: "继续此分析" });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
    await act(async () => { finishSelection(await json({ error: { code: "version_conflict", message: "会话已变化，请刷新后重试。", details: {}, trace_id: null } }, 409)); });
    expect(await screen.findByRole("alert")).toHaveTextContent("会话已变化");
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(button).toBeEnabled();
    expect(screen.getByLabelText("Work 负责 Agent")).toHaveValue("auto");
  });

  it("requires unsaved preprocessing edits to be handled before selecting an analysis goal", async () => {
    const selectGoal = vi.fn();
    installFetch({
      conversations: () => json([conversation({ messages: [message("draft", "assistant", "方案已准备", {
        work_draft: { hash: "a".repeat(64), draft: { preprocessing: { tr_seconds: 2 }, requested_metrics: [] }, compiled: false },
      })] })]),
      compositeGoals: () => json([{ ...compositeGoal("waiting_user", "goal-1"), analysis_status: "waiting_user" }]),
      selectCompositeGoal: selectGoal,
    });
    render(<AgentPage />);
    const button = await screen.findByRole("button", { name: "继续此分析" });
    expect(button).toBeEnabled();
    fireEvent.change(screen.getByLabelText("重复时间 TR（秒）"), { target: { value: "3" } });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(selectGoal).not.toHaveBeenCalled();
  });

  it("freezes the statistical design with the current server proposal message identity", async () => {
    const proposal = { project_id: "project-1", project_version: 2, input_manifest_hash: "b".repeat(64), proposal_hash: "a".repeat(64), design: { test: "one_sample_t" } };
    const current = conversation({ work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash, proposal_message_id: "selected-proposal-message", analysis_intent_task_id: "goal-selected" }] }) as unknown as Conversation;
    const design = { plan_revision: { plan_revision_id: "plan-selected", version: 1, state: "draft" } };
    const create = vi.fn((init?: RequestInit) => {
      expect(JSON.parse(String(init?.body)).work_context).toEqual({ conversation_id: "work-1", proposal_hash: proposal.proposal_hash, proposal_message_id: "selected-proposal-message" });
      return json(design);
    });
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/statistics/atlases")) return json([]);
      if (path.endsWith("/statistical-designs")) return create(init);
      if (path.endsWith("/validate")) return json(design);
      return notFound();
    });
    render(<AnalysisProposalActions proposal={proposal} conversation={current} isCurrent onTaskChange={async () => {}} onRunChange={() => {}} />);
    const button = await screen.findByRole("button", { name: "建立并校验正式统计设计" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
  });

  it.each(["frozen", "running", "completed"])("restores a %s statistical target without creating or approving work", async (stage) => {
    const proposal = { proposal_hash: "a".repeat(64), design: { test: "one_sample_t" } };
    const runId = stage === "frozen" ? null : "existing-run";
    const current = conversation({ work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash, analysis_intent_task_id: "existing-goal", proposal_message_id: "existing-message", plan_revision_id: "existing-plan", run_id: runId }] }) as unknown as Conversation;
    const mutations = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (init?.method === "POST") { mutations(); return notFound(); }
      if (pathOf(input).endsWith("/statistical-designs/existing-plan")) return json({ plan_revision: { plan_revision_id: "existing-plan", state: "awaiting_approval", version: 2 } });
      if (pathOf(input).endsWith("/runs/existing-run")) return json({ run_id: runId, plan_revision_id: "existing-plan", state: stage === "completed" ? "succeeded" : "running" });
      return notFound();
    });
    render(<AnalysisProposalActions proposal={proposal} conversation={current} isCurrent onTaskChange={async () => {}} onRunChange={() => {}} />);
    expect(await screen.findByText("existing-plan")).toBeInTheDocument();
    if (runId) expect(screen.getByText(/统计运行已创建/)).toHaveTextContent(runId);
    expect(screen.queryByRole("button", { name: "建立并校验正式统计设计" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /启动.*统计/ })).not.toBeInTheDocument();
    expect(mutations).not.toHaveBeenCalled();
  });

  it("clears an earlier goal's design and run while restoring a same-hash target and then a draft", async () => {
    const proposal = { proposal_hash: "a".repeat(64), design: { test: "one_sample_t" } };
    const target = (id: string, frozen = true) => conversation({ work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash, analysis_intent_task_id: id, proposal_message_id: `message-${id}`, plan_revision_id: frozen ? `plan-${id}` : null, run_id: frozen ? `run-${id}` : null }] }) as unknown as Conversation;
    let finishSecond!: (response: Response) => void;
    const pendingSecond = new Promise<Response>((resolve) => { finishSecond = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/statistical-designs/plan-B")) return pendingSecond;
      if (path.includes("/statistical-designs/plan-")) return json({ plan_revision: { plan_revision_id: "plan-A", state: "approved" } });
      if (path.includes("/runs/run-")) { const id = path.endsWith("run-B") ? "B" : "A"; return json({ run_id: `run-${id}`, plan_revision_id: `plan-${id}`, state: "succeeded" }); }
      if (path.endsWith("/statistics/atlases")) return json([]);
      return notFound();
    });
    const props = { proposal, isCurrent: true, onTaskChange: async () => {}, onRunChange: () => {} };
    const rendered = render(<AnalysisProposalActions {...props} conversation={target("A")} />);
    expect(await screen.findByText("plan-A")).toBeInTheDocument();
    expect(screen.getByText(/统计运行已创建/)).toHaveTextContent("run-A");
    rendered.rerender(<AnalysisProposalActions {...props} conversation={target("B")} />);
    expect(screen.queryByText("plan-A")).not.toBeInTheDocument();
    expect(screen.queryByText(/统计运行已创建/)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在恢复");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    await act(async () => { finishSecond(await json({ plan_revision: { plan_revision_id: "plan-B", state: "approved" } })); });
    expect(await screen.findByText("plan-B")).toBeInTheDocument();
    expect(screen.getByText(/统计运行已创建/)).toHaveTextContent("run-B");
    rendered.rerender(<AnalysisProposalActions {...props} conversation={target("C", false)} />);
    expect(screen.queryByText("plan-B")).not.toBeInTheDocument();
    expect(screen.queryByText(/统计运行已创建/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "建立并校验正式统计设计" })).toBeEnabled());
  });

  it.each(["network", "mismatched_run"])("fails closed on %s recovery and retries the exact saved plan and run", async (failure) => {
    const proposal = { proposal_hash: "a".repeat(64), design: { test: "one_sample_t" } };
    const current = conversation({ work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash, analysis_intent_task_id: "goal-selected", plan_revision_id: "plan-selected", run_id: "run-selected" }] }) as unknown as Conversation;
    let retry = false;
    const mutate = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (init?.method === "POST") { mutate(); return notFound(); }
      if (pathOf(input).endsWith("/statistical-designs/plan-selected")) {
        if (!retry && failure === "network") return json({ error: { code: "unavailable", message: "暂时无法读取方案", details: {}, trace_id: null } }, 503);
        return json({ plan_revision: { plan_revision_id: "plan-selected", state: "approved" } });
      }
      if (pathOf(input).endsWith("/runs/run-selected")) return json({ run_id: "run-selected", plan_revision_id: !retry && failure === "mismatched_run" ? "another-plan" : "plan-selected", state: "succeeded" });
      return notFound();
    });
    render(<AnalysisProposalActions proposal={proposal} conversation={current} isCurrent onTaskChange={async () => {}} onRunChange={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法恢复已保存的统计分析");
    expect(screen.queryByRole("button", { name: "建立并校验正式统计设计" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准统计设计" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /启动.*统计/ })).not.toBeInTheDocument();
    retry = true;
    fireEvent.click(screen.getByRole("button", { name: "重新读取统计分析" }));
    expect(await screen.findByText("plan-selected")).toBeInTheDocument();
    expect(screen.getByText(/统计运行已创建/)).toHaveTextContent("run-selected");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
  });

  it("ignores an earlier target's late run-start response after switching targets", async () => {
    const proposal = { project_id: "project-1", proposal_hash: "a".repeat(64), design: { test: "one_sample_t" } };
    const target = (id: string) => conversation({ work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash, analysis_intent_task_id: id, proposal_message_id: `message-${id}`, plan_revision_id: `plan-${id}`, run_id: id === "B" ? "run-B" : null }] }) as unknown as Conversation;
    let finishStart!: (response: Response) => void;
    const pendingStart = new Promise<Response>((resolve) => { finishStart = resolve; });
    const started = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/statistics/runs")) return pendingStart;
      if (path.includes("/statistical-designs/plan-")) return json({ plan_revision: { plan_revision_id: path.endsWith("plan-B") ? "plan-B" : "plan-A", state: "approved", plan_hash: "b".repeat(64) } });
      if (path.endsWith("/runs/run-B")) return json({ run_id: "run-B", plan_revision_id: "plan-B", state: "running" });
      return notFound();
    });
    const props = { proposal, isCurrent: true, onTaskChange: async () => {}, onRunChange: started };
    const rendered = render(<AnalysisProposalActions {...props} conversation={target("A")} />);
    fireEvent.click(await screen.findByRole("button", { name: "启动模拟统计" }));
    rendered.rerender(<AnalysisProposalActions {...props} conversation={target("B")} />);
    expect(await screen.findByText("plan-B")).toBeInTheDocument();
    await act(async () => { finishStart(await json({ run_id: "late-run-A", plan_revision_id: "plan-A", state: "queued" })); });
    expect(started).not.toHaveBeenCalled();
    expect(screen.getByText(/统计运行已创建/)).toHaveTextContent("run-B");
    expect(screen.queryByText(/late-run-A/)).not.toBeInTheDocument();
  });

  it("refreshes new design bindings after validation finishes", async () => {
    const proposal = { project_id: "project-1", project_version: 1, input_manifest_hash: "b".repeat(64), proposal_hash: "a".repeat(64), design: { test: "one_sample_t" } };
    const current = conversation({ work_tasks: [{ agent_role: "analysis", proposal_hash: proposal.proposal_hash, proposal_message_id: "proposal-message" }] }) as unknown as Conversation;
    let finishValidation!: (response: Response) => void;
    const pendingValidation = new Promise<Response>((resolve) => { finishValidation = resolve; });
    const refresh = vi.fn(async () => {});
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const path = pathOf(input);
      if (path.endsWith("/statistics/atlases")) return json([]);
      if (path.endsWith("/statistical-designs")) return json({ plan_revision: { plan_revision_id: "new-plan", version: 1, state: "draft" } });
      if (path.endsWith("/validate")) return pendingValidation;
      return notFound();
    });
    render(<AnalysisProposalActions proposal={proposal} conversation={current} isCurrent onTaskChange={refresh} onRunChange={() => {}} />);
    const button = await screen.findByRole("button", { name: "建立并校验正式统计设计" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByText("new-plan")).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => { finishValidation(await json({ plan_revision: { plan_revision_id: "new-plan", version: 2, state: "awaiting_approval" } })); });
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "批准统计设计" })).toBeEnabled();
  });

  it("offers saved analysis targets across lifecycle stages while hiding preprocessing-only decisions", async () => {
    const goals = [
      { ...compositeGoal("waiting_user", "goal-frozen"), analysis_status: "waiting_user" },
      { ...compositeGoal("waiting_analysis", "goal-running"), analysis_status: "waiting_run" },
      { ...compositeGoal("waiting_result_interpretation", "goal-result"), analysis_status: "completed" },
      { ...compositeGoal("completed", "goal-complete"), analysis_status: "completed" },
      { ...compositeGoal("waiting_user", "prep-only"), analysis_work_task_id: null, analysis_proposal_status: "pending", analysis_status: null },
    ];
    const select = vi.fn(() => json(conversation({ foreground_agent_role: "analysis" })));
    installFetch({ compositeGoals: () => json(goals), selectCompositeGoal: select });
    render(<AgentPage />);
    expect(await screen.findAllByRole("button", { name: "查看此分析" })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: "继续此分析" })).toHaveLength(1);
    expect(within(screen.getAllByRole("article")[4]).queryByRole("button")).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "查看此分析" })[2]);
    await waitFor(() => expect(select).toHaveBeenCalledWith("goal-complete", expect.anything()));
  });

  it("keeps progress API errors separate from the conversation error and lets the user retry", async () => {
    const loadGoals = vi.fn(() => json({ error: { code: "unavailable", message: "backend error", details: {}, trace_id: null } }, 503));
    installFetch({ compositeGoals: loadGoals });

    render(<AgentPage />);

    expect(await screen.findByText("自动化进度暂时无法更新。对话仍可继续使用，请稍后重试。")).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新进度" }));
    await waitFor(() => expect(loadGoals).toHaveBeenCalledTimes(2));
  });

  it("aborts a previous conversation progress request and ignores its late response", async () => {
    let finishFirstRequest!: (response: Response) => void;
    let firstSignal: AbortSignal | null = null;
    const firstResponse = new Promise<Response>((resolve) => { finishFirstRequest = resolve; });
    const loadGoals = vi.fn((conversationId: string, signal?: AbortSignal | null) => {
      if (conversationId === "work-1") {
        firstSignal = signal ?? null;
        return firstResponse;
      }
      return json([{ ...externalAnalysisGoal("completed", "current-goal", "result_interpretation_delivered"), analysis_status: "completed", result_interpretation_status: "completed" }]);
    });
    installFetch({
      conversations: () => json([conversation(), conversation({ conversation_id: "work-2", title: "第二个 Work 会话" })]),
      compositeGoals: loadGoals,
    });

    render(<AgentPage />);
    await waitFor(() => expect(loadGoals).toHaveBeenCalledWith("work-1", expect.any(AbortSignal)));
    fireEvent.change(screen.getByLabelText("当前会话"), { target: { value: "work-2" } });

    expect(await screen.findByRole("heading", { name: "自动化流程 1 已完成" })).toBeInTheDocument();
    expect(screen.getByText("外部指标分析")).toBeInTheDocument();
    await waitFor(() => expect(firstSignal?.aborted).toBe(true));
    await act(async () => {
      finishFirstRequest(await json([compositeGoal("waiting_user", "old-goal", "analysis_decision_required")]));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.queryByText("需要你处理", { selector: "span" })).not.toBeInTheDocument();
    expect(screen.queryByText("预处理与分析")).not.toBeInTheDocument();
    expect(loadGoals).toHaveBeenCalledWith("work-2", expect.any(AbortSignal));
  });

  it.each(["waiting_user", "completed"])("stops external analysis polling after interpretation reaches %s", async (terminalStatus) => {
    let settled = false;
    const loadGoals = vi.fn(() => json([settled
      ? { ...externalAnalysisGoal(terminalStatus, "external-poll"), analysis_status: "completed", result_interpretation_status: terminalStatus }
      : { ...externalAnalysisGoal("waiting_result_interpretation", "external-poll", "result_interpretation_pending"), analysis_status: "completed", result_interpretation_status: "ready" }]));
    installFetch({ compositeGoals: loadGoals });
    render(<AgentPage />);
    expect(await screen.findByText("等待结果解释", { selector: "span" })).toBeInTheDocument();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "刷新进度" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(loadGoals).toHaveBeenCalledTimes(2);
    settled = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(loadGoals).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("heading", { name: `自动化流程 1 ${terminalStatus === "completed" ? "已完成" : "需要你处理"}` })).toBeInTheDocument();
    expect(screen.queryByText("进行中的任务每 10 秒自动更新。")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(loadGoals).toHaveBeenCalledTimes(3);
  });

  it("periodically refreshes only while the composite goal is active", async () => {
    const loadGoals = vi.fn(() => json([compositeGoal("waiting_run", "goal-active", "analysis_run_pending")]));
    installFetch({ compositeGoals: loadGoals });
    render(<AgentPage />);

    expect(await screen.findByText("等待运行完成", { selector: "span" })).toBeInTheDocument();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "刷新进度" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(loadGoals).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(loadGoals).toHaveBeenCalledTimes(3);
  });

  it("switches the responsible Agent on the server while retaining drafts and hiding the other role's confirmation", async () => {
    const messages = [message("pre-draft", "assistant", "已保存预处理草稿", { agent_role: "preprocessing",
      work_draft: { hash: "a".repeat(64), version: 2, draft: { preprocessing: { tr_seconds: 2 }, requested_metrics: [] }, compiled: false },
      confirmation_request: { title: "确认预处理参数", summary: "当前预处理草稿", options: [{ label: "确认参数", description: "建立正式方案", command: "confirm-current-draft" }] },
    }), message("analysis-draft", "assistant", "已保存统计草稿", { agent_role: "analysis", analysis_draft: { hash: "b".repeat(64), draft: { test: "independent_two_sample_t" } } })];
    let version = 4;
    const switchAgent = vi.fn((init?: RequestInit) => {
      const body = JSON.parse(String(init?.body));
      expect(body.expected_version).toBe(version);
      version += 1;
      return json(conversation({ messages, version, foreground_agent_role: body.agent_role }));
    });
    const confirmation = vi.fn();
    installFetch({ conversations: () => json([conversation({ messages, version, foreground_agent_role: "preprocessing" })]), selectAgent: switchAgent, confirmation });
    render(<AgentPage />);
    await screen.findByText("当前负责：预处理 Agent");
    expect(screen.getByRole("button", { name: /待确认 · 确认预处理参数/ })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Work 负责 Agent"), { target: { value: "analysis" } });
    await screen.findByText("当前负责：数据分析 Agent");
    expect(screen.queryByRole("button", { name: /待确认/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("预处理方案参数")).not.toBeInTheDocument();
    expect(screen.getByText("已保存预处理草稿")).toBeInTheDocument();
    expect(screen.getByText("已保存统计草稿")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Work 负责 Agent"), { target: { value: "preprocessing" } });
    await screen.findByText("当前负责：预处理 Agent");
    expect(screen.getByLabelText("重复时间 TR（秒）")).toHaveValue(2);
    expect(screen.getByRole("button", { name: /待确认 · 确认预处理参数/ })).toBeInTheDocument();
    expect(switchAgent).toHaveBeenCalledTimes(2);
    expect(confirmation).not.toHaveBeenCalled();
  });

  it("retains the authoritative Agent after a version-conflicted switch and refreshes the current conversation", async () => {
    const current = conversation({ version: 6, foreground_agent_role: "preprocessing" });
    const refresh = vi.fn(() => json({ ...current, version: 7 }));
    installFetch({ conversations: () => json([current]), conversation: refresh,
      selectAgent: () => json({ error: { code: "version_conflict", message: "会话已变化，请重新核对当前任务。", details: {}, trace_id: null } }, 409) });
    render(<AgentPage />);
    await screen.findByRole("option", { name: "DemoData ALFF" });
    fireEvent.change(screen.getByLabelText("Work 负责 Agent"), { target: { value: "analysis" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("会话已变化");
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(screen.getByText("当前负责：预处理 Agent")).toBeInTheDocument();
    expect(screen.getByLabelText("Work 负责 Agent")).toHaveValue("auto");
  });

  it("restores the confirmed input stage when loading a conversation", async () => {
    installFetch({
      conversations: () => json([conversation({ messages: [message("a1", "assistant", "阶段已确认", { selected_input_stage: "FunImg" })] })]),
      workspaceCheck: () => json({ path: "D:/demo-work", selected_input_stage: null, stage_candidates: [{ stage: "FunImg", label: "功能输入", selectable: true, subject_count: 1, file_count: 1, issues: [] }] }),
    });
    render(<AgentPage />);
    await screen.findByText("FunImg · 已确认");
    await userEvent.click(screen.getByText("预处理起点"));
    expect(screen.getByRole("button", { name: "起点已确认" })).toBeDisabled();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("opens mobile review only on request and retains unsaved edits after closing", async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    try {
      installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "方案已准备", { work_draft: { hash: "a".repeat(64), version: 2, draft: { preprocessing: { tr_seconds: 2 }, requested_metrics: [] }, compiled: false } })] })]) });
      render(<AgentPage />);
      await screen.findByText("方案已准备");
      expect(screen.queryByRole("complementary", { name: "当前方案审查" })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "查看当前方案 · 第 2 版" }));
      fireEvent.change(screen.getByLabelText("重复时间 TR（秒）"), { target: { value: "3" } });
      await userEvent.click(screen.getByRole("button", { name: "收起方案" }));
      await userEvent.click(screen.getByRole("button", { name: "查看当前方案 · 第 2 版" }));
      expect(screen.getByLabelText("重复时间 TR（秒）")).toHaveValue(3);
      expect(screen.getByText("修改尚未保存，保存后重新校验")).toBeInTheDocument();
    } finally { Object.defineProperty(window, "innerWidth", { configurable: true, value: originalWidth }); }
  });
  it("embeds and saves the latest structured draft while preserving historical read-only panels", async () => {
    const draft = { preprocessing: { tr_seconds: 2 }, requested_metrics: [] };
    const payload = { work_draft: { draft, hash: "a".repeat(64), workspace_path: "D:/demo-work", compiled: false }, draft_issues: [] };
    const first = message("a1", "assistant", "参数建议", payload);
    const changed = message("a2", "assistant", "修改已保存", { ...payload, work_draft: { ...payload.work_draft, hash: "b".repeat(64), draft: { ...draft, preprocessing: { tr_seconds: 3 } } } });
    const patch = vi.fn((init?: RequestInit) => { expect(init?.method).toBe("PATCH"); return json({ conversation: conversation({ messages: [first, changed] }), user_message: message("u2", "user", "参数修改"), assistant_message: changed, tool_calls: [], tool_call: null }); });
    installFetch({ conversations: () => json([conversation({ messages: [first] })]), draftPatch: patch });
    render(<AgentPage />);
    const panel = await screen.findByLabelText("预处理方案参数");
    expect(screen.getByLabelText("当前方案审查")).toContainElement(panel);
    fireEvent.change(within(panel).getByLabelText("重复时间 TR（秒）"), { target: { value: "3" } });
    expect(screen.getByText("发送", { selector: "button" })).toBeDisabled();
    await userEvent.click(within(panel).getByText("保存修改", { selector: "button" }));
    await screen.findByText("修改已保存");
    expect(JSON.parse(String(patch.mock.calls[0]?.[0]?.body))).toEqual({ base_hash: "a".repeat(64), patch: { preprocessing: { tr_seconds: 3 } } });
    expect(within(screen.getByLabelText("预处理方案参数")).getByLabelText("重复时间 TR（秒）")).toHaveValue(3);
    expect(screen.queryByLabelText("历史预处理方案（只读）")).not.toBeInTheDocument();
  });

  it("keeps the executed plan frozen and prevents reissuing its startup review", async () => {
    installFetch({ conversations: () => json([conversation({ messages: [
      message("used", "assistant", "本次方案已用于计算", {
        work_draft: { hash: "a".repeat(64), compiled: true, consumed: true, draft: { preprocessing: { tr_seconds: 2 }, requested_metrics: [] } },
      }),
    ] })]) });
    render(<AgentPage />);
    await screen.findByText("本次方案已用于计算");
    expect(screen.getByLabelText("重复时间 TR（秒）")).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新校验方案与启动确认" })).toBeDisabled();
    expect(screen.getByText("已用于本次计算，科学参数已冻结")).toBeInTheDocument();
  });

  it("loads a historical plan only when expanded and keeps it read-only", async () => {
    const snapshot = { draft: { preprocessing: { tr_seconds: 2 }, requested_metrics: [] }, hash: "a".repeat(64), compiled: false };
    installFetch({ conversations: () => json([conversation({ messages: [
      message("old", "assistant", "原方案", { work_draft: snapshot }),
      message("current", "assistant", "当前方案", { work_draft: { ...snapshot, hash: "b".repeat(64), draft: { ...snapshot.draft, preprocessing: { tr_seconds: 3 } } } }),
    ] })]) });
    render(<AgentPage />);
    await screen.findByText("原方案");
    fireEvent.click(screen.getByText("收起方案", { selector: "button" }));
    const history = screen.getByText("历史方案").closest("details")!;
    expect(history).not.toHaveAttribute("open");
    expect(screen.queryByLabelText("历史预处理方案（只读）")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("历史方案"));
    expect(within(await screen.findByLabelText("历史预处理方案（只读）")).getByLabelText("重复时间 TR（秒）")).toBeDisabled();
  // Rendering the full read-only editor under V8 coverage exceeds the usual budget.
  }, 30_000);

  it("opens confirmation only on request and submits the selected server action without calling the model", async () => {
    const user = userEvent.setup();
    const command = "批准并启动 abcdef123456 做头动QC challenge-1";
    const onTurn = vi.fn();
    const confirm = vi.fn(() => json({ conversation: conversation(), user_message: message("u2", "user", "启动并做头动 QC"), assistant_message: message("a2", "assistant", "已提交"), tool_call: null }));
    installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "请审查", {
      confirmation_request: { title: "批准并启动计算", summary: "方案 abcdef123456", options: [
        { label: "启动并做头动 QC", description: "生成 QC 草稿", command },
        { label: "启动并跳过 QC", description: "记录跳过", command: "skip-command" },
      ] },
    })] })]), turn: onTurn, confirmation: confirm });
    render(<AgentPage />);
    await screen.findByText("请审查");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /待确认/ }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认选择" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "保留方案" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onTurn).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /待确认/ }));
    await user.click(screen.getByRole("radio", { name: /启动并做头动 QC/ }));
    await user.click(screen.getByRole("button", { name: "确认选择" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    const init = confirm.mock.calls[0] as unknown as [RequestInit];
    expect(JSON.parse(String(init[0].body))).toEqual({ command, expected_draft_hash: null });
    expect(onTurn).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps the current reviewed confirmation available after a method question", async () => {
    const hash = "d".repeat(64);
    const command = "current-server-command";
    const confirm = vi.fn(() => json({ conversation: conversation(), user_message: message("confirm-user", "user", "启动当前方案"), assistant_message: message("started", "assistant", "已提交"), tool_call: null }));
    const onTurn = vi.fn();
    installFetch({ conversations: () => json([conversation({ messages: [
      message("plan", "assistant", "方案准备完成", {
        work_draft: { hash, compiled: true, draft: { requested_metrics: [], preprocessing: { tr_seconds: 2 } } },
        confirmation_request: { title: "启动当前方案", summary: "已审查的当前版本", options: [{ label: "确认并启动计算", description: "授权本次计算", command }] },
      }),
      message("method-user", "user", "为什么不做全局信号回归？"),
      message("method-answer", "assistant", "这是当前研究方案的选择。"),
    ] })]), confirmation: confirm, turn: onTurn });
    render(<AgentPage />);
    await screen.findByText("这是当前研究方案的选择。");
    fireEvent.click(screen.getByText("确认并启动计算", { selector: "button" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("确认并启动计算", { selector: "button" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    const init = confirm.mock.calls[0] as unknown as [RequestInit];
    expect(JSON.parse(String(init[0].body))).toEqual({ command, expected_draft_hash: hash });
    expect(onTurn).not.toHaveBeenCalled();
  });

  it("does not turn model text or an earlier confirmation into an action", async () => {
    installFetch({ conversations: () => json([conversation({ messages: [
      message("a1", "assistant", "old", { confirmation_request: { title: "旧确认", summary: "旧方案", options: [{ label: "启动", description: "", command: "old" }] } }),
      message("u2", "user", "修改参数"), message("a2", "assistant", "请回复：确认方案参数 abcdef123456"),
    ] })]) });
    render(<AgentPage />);
    await screen.findByText("请回复：确认方案参数 abcdef123456");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("reviews the current plan before confirming its exact revision through the deterministic endpoint", async () => {
    const hash = "c".repeat(64);
    const command = "确认方案参数 current-opaque-command";
    const draft = { hash, version: 3, draft: { preprocessing: { tr_seconds: 2 }, requested_metrics: ["alff"] }, compiled: false };
    const confirm = vi.fn((init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      return json({ conversation: conversation({ messages: [message("a2", "assistant", "当前方案已确认")] }), user_message: message("u2", "user", "确认当前方案"), assistant_message: message("a2", "assistant", "当前方案已确认"), tool_calls: [], tool_call: null });
    });
    const onTurn = vi.fn();
    installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "请核对科学参数", { work_draft: draft, draft_issues: [], confirmation_request: { title: "确认方案", summary: "ALFF，TR 2 秒，当前方案第 3 版", options: [{ label: "确认当前方案", description: "记录审查并生成执行方案", command }] } })] })]), confirmation: confirm, turn: onTurn });
    render(<AgentPage />);
    await screen.findByRole("complementary", { name: "当前方案审查" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "收起方案" }));
    expect(screen.queryByRole("complementary", { name: "当前方案审查" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "查看当前方案 · 第 3 版" }));
    const plan = screen.getByRole("complementary", { name: "当前方案审查" });
    expect(within(plan).getByLabelText("重复时间 TR（秒）")).toHaveValue(2);
    await userEvent.click(within(plan).getByRole("button", { name: "确认当前方案" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByRole("radio")).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "确认当前方案" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    expect(JSON.parse(String(confirm.mock.calls[0][0]?.body))).toEqual({ command, expected_draft_hash: hash });
    expect(onTurn).not.toHaveBeenCalled();
    expect(await screen.findByText("当前方案已确认")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each(["stop", "timeout"])("keeps confirmation commands out of the composer after %s", async (reason) => {
    let expire: (() => void) | undefined;
    const originalTimeout = window.setTimeout.bind(window);
    vi.spyOn(window, "setTimeout").mockImplementation((handler, timeout, ...args) => {
      if (timeout === 300_000 && typeof handler === "function") expire = () => handler(...args);
      return originalTimeout(handler, timeout, ...args) as unknown as ReturnType<typeof setTimeout>;
    });
    let signal: AbortSignal | undefined | null;
    const command = "确认方案参数 private-command";
    const draft = { hash: "d".repeat(64), draft: { requested_metrics: ["alff"], preprocessing: { tr_seconds: 2 } }, compiled: false };
    const confirm = vi.fn((init?: RequestInit) => {
      signal = init?.signal;
      return new Promise<Response>((_resolve, reject) => signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    });
    const turn = vi.fn();
    installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "准备审查", { work_draft: draft, confirmation_request: { title: "确认方案", summary: "审查当前方案", options: [{ label: "确认当前方案", description: "记录审查", command }] } })] })]), confirmation: confirm, turn });
    render(<AgentPage />);
    const plan = await screen.findByRole("complementary", { name: "当前方案审查" });
    await userEvent.click(within(plan).getByRole("button", { name: "确认当前方案" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "确认当前方案" }));
    await waitFor(() => expect(signal).toBeTruthy());
    if (reason === "stop") {
      await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "停止等待" }));
    } else {
      expect(expire).toBeDefined();
      await act(async () => expire!());
    }
    expect(signal?.aborted).toBe(true);
    expect(screen.getByPlaceholderText("例如：用当前 DemoData 准备 ALFF 预处理方案")).toHaveValue("");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("停止等待不会取消计算");
    expect(confirm).toHaveBeenCalledOnce();
    expect(turn).not.toHaveBeenCalled();
  });

  it("recovers a saved confirmation after response loss with one read and no second execution request", async () => {
    const command = "确认方案参数 recover-command";
    const draft = { hash: "d".repeat(64), draft: { requested_metrics: ["alff"], preprocessing: { tr_seconds: 2 } }, compiled: false };
    const confirm = vi.fn(() => Promise.reject(new TypeError("response lost")));
    const recover = vi.fn(() => json(conversation({ messages: [message("u2", "user", command), message("a2", "assistant", "已恢复保存的确认结果")] })));
    installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "准备审查", { work_draft: draft, confirmation_request: { title: "确认方案", summary: "审查当前方案", options: [{ label: "确认当前方案", description: "记录审查", command }] } })] })]), confirmation: confirm, conversation: recover });
    render(<AgentPage />);
    const plan = await screen.findByRole("complementary", { name: "当前方案审查" });
    await userEvent.click(within(plan).getByRole("button", { name: "确认当前方案" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "确认当前方案" }));
    expect(await screen.findByText("已恢复保存的确认结果")).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledOnce();
    expect(recover).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText(/回答未完成/)).not.toBeInTheDocument();
  });

  it("refreshes the latest draft after a definite version conflict without retrying the old command", async () => {
    const draft = { hash: "d".repeat(64), draft: { requested_metrics: ["alff"], preprocessing: { tr_seconds: 2 } }, compiled: false };
    const option = { label: "确认当前方案", description: "记录审查", command: "确认方案参数 old-command" };
    const confirm = vi.fn(() => json({ error: { code: "work_draft_changed", message: "方案已更新", details: {} } }, 409));
    const recover = vi.fn(() => json(conversation({ messages: [message("a2", "assistant", "已载入另一处保存的新方案", { work_draft: { ...draft, hash: "e".repeat(64), draft: { ...draft.draft, preprocessing: { tr_seconds: 3 } } }, confirmation_request: { title: "确认方案", summary: "新方案", options: [{ ...option, command: "确认方案参数 new-command" }] } })] })));
    installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "准备审查", { work_draft: draft, confirmation_request: { title: "确认方案", summary: "原方案", options: [option] } })] })]), confirmation: confirm, conversation: recover });
    render(<AgentPage />);
    const plan = await screen.findByRole("complementary", { name: "当前方案审查" });
    await userEvent.click(within(plan).getByRole("button", { name: "确认当前方案" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "确认当前方案" }));
    expect(await screen.findByText("已载入另一处保存的新方案")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("已刷新当前方案");
    expect(screen.getByLabelText("重复时间 TR（秒）")).toHaveValue(3);
    expect(confirm).toHaveBeenCalledOnce(); expect(recover).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText(/未能确认操作是否完成/)).not.toBeInTheDocument();
  });

  it("withdraws verified fields after input rejection through deterministic review recovery", async () => {
    const hash = "d".repeat(64);
    const draft = { hash, draft: { requested_metrics: ["alff"], preprocessing: { tr_seconds: 2 } }, compiled: true, sources: { "preprocessing.tr_seconds": "data" }, field_states: { "preprocessing.tr_seconds": "verified" } };
    const confirm = vi.fn(() => json({ error: { code: "work_input_changed", message: "输入文件已变化", details: {} } }, 409));
    const recovery = message("a2", "assistant", "输入确认已撤回", {
      work_draft: { ...draft, compiled: false, input_evidence_stale: true, input_manifest_stale: true, sources: { "preprocessing.tr_seconds": "stale_data" }, field_states: { "preprocessing.tr_seconds": "review_pending" } },
      draft_issues: ["请重新核实输入"],
      confirmation_request: { title: "重新确认输入阶段", summary: "不启动计算", options: [{ label: "使用 FunRaw", description: "冻结输入", command: "确认起始阶段 FunRaw aaaaaaaaaaaa" }] },
    });
    const review = vi.fn(() => json({ conversation: conversation({ messages: [recovery] }), assistant_message: recovery }));
    installFetch({ conversations: () => json([conversation({ messages: [message("a1", "assistant", "原方案", { work_draft: draft, confirmation_request: { title: "确认并启动计算", summary: "原输入", options: [{ label: "确认并启动计算", description: "启动", command: "批准并启动 old-command" }] } })] })]), confirmation: confirm, review });
    render(<AgentPage />);
    const plan = await screen.findByRole("complementary", { name: "当前方案审查" });
    await userEvent.click(within(plan).getByRole("button", { name: "确认并启动计算" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "确认并启动计算" }));
    expect(await screen.findByText("输入确认已撤回")).toBeInTheDocument();
    expect(screen.getByText("来源：历史读取（待重读）")).toBeInTheDocument();
    expect(screen.getAllByRole("status").some((node) => node.textContent?.includes("参数选择已保留"))).toBe(true);
    expect(screen.getByLabelText("重复时间 TR（秒）")).toHaveValue(2);
    expect(screen.getByLabelText("重复时间 TR（秒）")).toBeDisabled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^使用 FunRaw$/ })).toBeEnabled();
    expect(confirm).toHaveBeenCalledOnce(); expect(review).toHaveBeenCalledOnce();
  });

  it("reconnects an interrupted run observation and restores results without starting another computation", async () => {
    const run = vi.fn()
      .mockRejectedValueOnce(new TypeError("暂时断网"))
      .mockImplementation(() => json({ run_id: "run-1", project_id: null, plan_revision_id: "plan-1", state: "qc_review", version: 3, attempt: 1, cancel_requested: false, error: null, qc_mode: "require", qc_status: "pending", stage: "qc_review", stage_progress: 1, created_at: now, updated_at: now }));
    const fetchSpy = installFetch({ conversations: () => json([conversation({ active_run_id: "run-1", messages: [message("a1", "assistant", "计算已提交")] })]), run });
    render(<AgentPage />);
    expect(await screen.findByText(/运行状态连接中断/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByRole("heading", { name: "计算完成 · 待 QC 审查" })).toBeInTheDocument();
    expect(screen.getByText("指标计算已完成，科学质量审查尚未通过。")).toBeInTheDocument();
    expect(screen.queryByText(/运行状态连接中断/)).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls.filter(([input, init]) => pathOf(input).includes("/runs/") && init?.method === "POST")).toHaveLength(0);
    expect(fetchSpy.mock.calls.filter(([input]) => /^\/api\/v1\/runs\/run-1$/.test(pathOf(input))).length).toBeGreaterThanOrEqual(2);
  });

  it("tracks and reports a preprocessing MATLAB failure while the analysis Agent is selected", async () => {
    const run = vi.fn(() => json({
      run_id: "run-preprocessing", project_id: "project-1", plan_revision_id: "plan-1",
      state: "failed_terminal", version: 4, attempt: 1, cancel_requested: false,
      error: "MATLAB exited with code 1", qc_mode: "require", qc_status: "pending",
      stage: "failed", stage_progress: 0, created_at: now, updated_at: now,
    }));
    const diagnosis = vi.fn(() => json({
      run_id: "run-preprocessing",
      diagnosis: {
        code: "path_policy", severity: "error", summary: "MATLAB 启动时无权写入偏好设置目录",
        evidence: ["CreateFile failed: 拒绝访问。 [system:5]"],
        suggestions: ["检查 MATLAB 偏好目录写入权限"], confidence: 0.8,
        requires_new_plan: false, source: "deterministic_log_classifier",
      },
    }));
    const fetchMock = installFetch({
      conversations: () => json([conversation({
        foreground_agent_role: "analysis",
        active_run_id: null,
        work_tasks: [
          { agent_role: "analysis", work_task_id: "analysis-task", run_id: null },
          { agent_role: "preprocessing", work_task_id: "preprocessing-task", run_id: "run-preprocessing" },
        ],
      })]),
      run,
      diagnosis,
    });

    render(<AgentPage />);

    expect(await screen.findByRole("heading", { name: "计算失败" })).toBeInTheDocument();
    expect(await screen.findByText("MATLAB 启动时无权写入偏好设置目录")).toBeInTheDocument();
    expect(screen.getByText("当前负责：数据分析 Agent")).toBeInTheDocument();
    expect(run).toHaveBeenCalled();
    expect(diagnosis).toHaveBeenCalledWith("run-preprocessing", expect.any(AbortSignal));
    expect(fetchMock.mock.calls.some(([input]) => pathOf(input) === "/api/v1/runs/run-preprocessing" )).toBe(true);
  });

  it("refreshes a delayed assistant analysis continuation after preprocessing succeeds", async () => {
    const intentTaskId = "analysis-intent-1";
    const original = conversation({
      project_id: "project-1",
      active_run_id: "run-1",
      messages: [message("intent", "assistant", "预处理完成后继续分析", {
        analysis_dependency: {
          task_kind: "preprocess_then_analyze",
          task_id: intentTaskId,
          project_id: "project-1",
          status: "analysis_ready",
        },
        analysis_intent_envelope: {
          status: "active",
          task_id: intentTaskId,
          project_id: "project-1",
        },
      })],
    });
    const delivered = conversation({
      ...original,
      version: 2,
      active_run_id: null,
      foreground_agent_role: "analysis",
      messages: [...original.messages, message("continuation", "assistant", "后台准备的统计草稿已到达。", {
        agent_role: "analysis",
        analysis_intent_envelope: { status: "consumed", task_id: intentTaskId, project_id: "project-1" },
        analysis_outbox_continuation: { intent_task_id: intentTaskId },
        analysis_draft: { draft: { test: "independent_two_sample_t" }, hash: "a".repeat(64) },
      })],
    });
    let conversationReads = 0;
    const refresh = vi.fn(() => json(conversationReads++ === 0 ? original : delivered));
    const run = () => json({ run_id: "run-1", project_id: "project-1", plan_revision_id: "plan-1", state: "succeeded", version: 4, attempt: 1, cancel_requested: false, error: null, qc_mode: "require", qc_status: "approved", stage: "completed", stage_progress: 1, created_at: now, updated_at: now });
    installFetch({ conversations: () => json([original]), conversation: refresh, run });

    render(<AgentPage />);

    expect(await screen.findByText("后台准备的统计草稿已到达。", {}, { timeout: 8000 })).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(2);
    expect(screen.getByText("当前负责：数据分析 Agent")).toBeInTheDocument();
  });

  it("resumes polling after a manual analysis outbox retry and hides the prior retry action", async () => {
    const intentTaskId = "analysis-intent-retry";
    const intent = message("intent", "assistant", "预处理完成后继续分析", {
      analysis_dependency: {
        task_kind: "preprocess_then_analyze",
        task_id: intentTaskId,
        project_id: "project-1",
        status: "analysis_ready",
      },
      analysis_intent_envelope: {
        status: "active",
        task_id: intentTaskId,
        project_id: "project-1",
      },
    });
    const paused = message("paused", "assistant", "模型尚未配置，请配置后重试。", {
      agent_role: "analysis",
      analysis_outbox_continuation: { intent_task_id: intentTaskId, retry_generation: 0 },
      analysis_outbox_result: { status: "waiting_user", retry_available: true },
    });
    const original = conversation({
      project_id: "project-1",
      active_run_id: "run-1",
      messages: [intent, paused],
    });
    const queued = conversation({
      ...original,
      version: 3,
      messages: [
        ...original.messages,
        message("retry", "assistant", "已排队重新准备草稿。", {
          analysis_outbox_retry: {
            intent_task_id: intentTaskId,
            retry_generation: 1,
            status: "ready",
          },
        }),
      ],
    });
    const delivered = conversation({
      ...queued,
      version: 4,
      active_run_id: null,
      foreground_agent_role: "analysis",
      messages: [
        ...queued.messages,
        message("continued", "assistant", "重试后的统计草稿已到达。", {
          agent_role: "analysis",
          analysis_outbox_continuation: { intent_task_id: intentTaskId, retry_generation: 1 },
          analysis_outbox_result: { status: "proposal_ready", retry_available: false },
          analysis_draft: { draft: { metric_artifact_type: "alff_map" }, hash: "b".repeat(64) },
        }),
      ],
    });
    let conversationReads = 0;
    let deliverResponse: ((response: Response) => void) | undefined;
    const refresh = vi.fn(() => {
      if (conversationReads++ === 0) return json(original);
      return new Promise<Response>((resolve) => { deliverResponse = resolve; });
    });
    const retry = vi.fn((init?: RequestInit) => {
      expect(init?.body).toBe("{}");
      return json(queued);
    });
    const run = () => json({ run_id: "run-1", project_id: "project-1", plan_revision_id: "plan-1", state: "succeeded", version: 4, attempt: 1, cancel_requested: false, error: null, qc_mode: "require", qc_status: "approved", stage: "completed", stage_progress: 1, created_at: now, updated_at: now });
    installFetch({ conversations: () => json([original]), conversation: refresh, retryOutbox: retry, run });

    render(<AgentPage />);

    await userEvent.click(await screen.findByRole("button", { name: "重新尝试准备草稿" }));
    await waitFor(() => expect(retry).toHaveBeenCalledOnce());
    await waitFor(() => expect(deliverResponse).toBeDefined());
    expect(screen.queryByRole("button", { name: "重新尝试准备草稿" })).not.toBeInTheDocument();
    expect(screen.queryByText("重试后的统计草稿已到达。")).not.toBeInTheDocument();

    await act(async () => {
      deliverResponse!(new Response(JSON.stringify(delivered), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    expect(await screen.findByText("重试后的统计草稿已到达。")).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("refreshes a completed statistical WorkTask until its result interpretation arrives", async () => {
    const taskId = "statistical-task-1";
    const original = conversation({
      project_id: "project-1",
      active_run_id: "run-1",
      foreground_agent_role: "analysis",
      work_tasks: [{ agent_role: "analysis", work_task_id: taskId, run_id: "run-1" }],
      messages: [message("result-pending", "assistant", "统计运行已完成，正在整理结果。", { agent_role: "analysis" })],
    });
    const delivered = conversation({
      ...original,
      version: 2,
      messages: [...original.messages, message("result-interpretation", "assistant", "统计结果解释报告草稿已生成。", {
        agent_role: "analysis",
        statistical_result_interpretation: {
          task_id: taskId,
          retry_generation: 0,
          status: "completed",
          retry_available: false,
        },
      })],
    });
    let reads = 0;
    const refresh = vi.fn(() => json(reads++ === 0 ? original : delivered));
    const run = () => json({ run_id: "run-1", project_id: "project-1", plan_revision_id: "plan-1", state: "succeeded", version: 4, attempt: 1, cancel_requested: false, error: null, qc_mode: "require", qc_status: "approved", stage: "completed", stage_progress: 1, created_at: now, updated_at: now });
    installFetch({ conversations: () => json([original]), conversation: refresh, run });

    render(<AgentPage />);

    expect(await screen.findByText("统计结果解释报告草稿已生成。", {}, { timeout: 8000 })).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("refreshes the conversation after a failed statistical Run", async () => {
    const taskId = "failed-statistical-task";
    const original = conversation({
      project_id: "project-1",
      active_run_id: "run-1",
      foreground_agent_role: "analysis",
      work_tasks: [{ agent_role: "analysis", work_task_id: taskId, run_id: "run-1" }],
      messages: [message("prior-result", "assistant", "上一次结果解释已完成。", {
        statistical_result_interpretation: {
          task_id: taskId,
          retry_generation: 0,
          status: "completed",
          retry_available: false,
        },
      })],
    });
    const delivered = conversation({
      ...original,
      version: 2,
      messages: [...original.messages, message("run-failure", "assistant", "统计运行失败，已刷新最新状态。")],
    });
    const refresh = vi.fn(() => json(delivered));
    const run = () => json({ run_id: "run-1", project_id: "project-1", plan_revision_id: "plan-1", state: "failed_terminal", version: 5, attempt: 1, cancel_requested: false, error: null, qc_mode: "require", qc_status: "not_required", stage: "failed_terminal", stage_progress: 0, created_at: now, updated_at: now });
    installFetch({ conversations: () => json([original]), conversation: refresh, run });

    render(<AgentPage />);

    expect(await screen.findByText("统计运行失败，已刷新最新状态。", {}, { timeout: 8000 })).toBeInTheDocument();
    expect(refresh).toHaveBeenCalled();
  });

  it("keeps result interpretation retry separate and restores its action for the new generation", async () => {
    const taskId = "statistical-task-retry";
    const paused = message("result-paused", "assistant", "模型不可用，请配置后重试。", {
      agent_role: "analysis",
      statistical_result_interpretation: {
        task_id: taskId,
        retry_generation: 0,
        status: "waiting_user",
        retry_available: true,
      },
    });
    const original = conversation({
      project_id: "project-1",
      active_run_id: "run-1",
      foreground_agent_role: "analysis",
      work_tasks: [{ agent_role: "analysis", work_task_id: taskId, run_id: "run-1" }],
      messages: [paused],
    });
    const queued = conversation({
      ...original,
      version: 2,
      messages: [...original.messages, message("result-retry", "assistant", "已排队重新生成解释草稿。", {
        statistical_result_interpretation_retry: {
          task_id: taskId,
          retry_generation: 1,
          status: "ready",
        },
      })],
    });
    const delivered = conversation({
      ...queued,
      version: 3,
      messages: [...queued.messages, message("result-paused-again", "assistant", "模型仍不可用，请检查配置。", {
        agent_role: "analysis",
        statistical_result_interpretation: {
          task_id: taskId,
          retry_generation: 1,
          status: "waiting_user",
          retry_available: true,
        },
      })],
    });
    const refresh = vi.fn(() => json(delivered));
    const retry = vi.fn(() => json(queued));
    const run = () => json({ run_id: "run-1", project_id: "project-1", plan_revision_id: "plan-1", state: "succeeded", version: 4, attempt: 1, cancel_requested: false, error: null, qc_mode: "require", qc_status: "approved", stage: "completed", stage_progress: 1, created_at: now, updated_at: now });
    installFetch({ conversations: () => json([original]), conversation: refresh, retryResultInterpretation: retry, run });

    render(<AgentPage />);
    await userEvent.click(await screen.findByRole("button", { name: "重新生成结果解读草稿" }));
    await waitFor(() => expect(retry).toHaveBeenCalledOnce());
    expect(await screen.findByText("模型仍不可用，请检查配置。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新生成结果解读草稿" })).toBeInTheDocument();
    expect(refresh).toHaveBeenCalled();
  });

  it("lists every model the bound provider returns and lets the operator pick one", async () => {
    const user = userEvent.setup();
    installFetch({ profiles: () => json([profile("deepseek", ["json_object"])]) });

    render(<AgentPage />);

    // Both provider models become selectable, labelled with the provider host.
    expect(await screen.findByRole("option", { name: "api.deepseek.com · deepseek-chat" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "api.deepseek.com · deepseek-reasoner" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("模型"), "deepseek:deepseek-reasoner");

    expect(screen.getByLabelText("模型")).toHaveValue("deepseek:deepseek-reasoner");
  });

  it("restores the selected model when reopening without granting file authorization", async () => {
    installFetch({ profiles: () => json([profile("deepseek", ["json_object", "tool_use"])]) });
    window.sessionStorage.setItem("rsfmri-agent-model-v1", "deepseek:deepseek-reasoner");
    render(<AgentPage />);
    await screen.findByRole("option", { name: "api.deepseek.com · deepseek-reasoner" });
    expect(screen.getByLabelText("模型")).toHaveValue("deepseek:deepseek-reasoner");
    expect(vi.mocked(globalThis.fetch).mock.calls.filter(([input, init]) => pathOf(input).endsWith("/workspace/consents") && init?.method === "POST")).toHaveLength(0);
  });

  it("falls back to the configured model when the provider cannot be reached", async () => {
    installFetch({
      profiles: () => json([profile("deepseek", [])]),
    });
    vi.mocked(globalThis.fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/providers/models")) {
        return json(
          {
            error: {
              code: "provider_unavailable",
              message: "服务商暂时不可用",
              details: {},
              trace_id: null,
            },
          },
          503,
        );
      }
      if (path.endsWith("/model-profiles")) return json([profile("deepseek", [])]);
      if (path.endsWith("/conversations")) return json([conversation()]);
      void init;
      return notFound();
    });

    render(<AgentPage />);

    expect(await screen.findByRole("option", { name: "api.deepseek.com · deepseek-chat" })).toBeInTheDocument();
  });

  it("explains that automatic or non-tool profiles cannot authorize workspace files", async () => {
    const user = userEvent.setup();
    installFetch({ profiles: () => json([profile("deepseek", ["json_object"])]) });

    render(<AgentPage />);

    await screen.findByRole("option", { name: "api.deepseek.com · deepseek-chat" });
    await user.selectOptions(screen.getByLabelText("模型"), "deepseek:deepseek-chat");
    const authorize = await screen.findByRole("button", { name: "授权文件分析" });
    expect(authorize).toBeDisabled();
    expect(screen.getByText(/模型配置未声明工具调用/)).toBeInTheDocument();
  });

  it("shows the server probe failure when workspace authorization is rejected", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    installFetch({
      profiles: () => json([profile("deepseek", ["json_object", "tool_use"])]),
      grantWorkspaceConsent: () => json({
        error: {
          code: "workspace_tool_probe_failed",
          message: "所选模型未能按要求调用测试工具。",
          details: {},
          trace_id: "probe-trace",
        },
      }, 422),
    });

    render(<AgentPage />);
    await screen.findByRole("option", { name: "api.deepseek.com · deepseek-chat" });
    await user.selectOptions(await screen.findByLabelText("模型"), "deepseek:deepseek-chat");
    await user.click(await screen.findByRole("button", { name: "授权文件分析" }));

    expect(await screen.findByText(/未能按要求调用测试工具/)).toBeInTheDocument();
  });

  it("refreshes valid workspace consent when the model or workspace changes", async () => {
    const user = userEvent.setup();
    installFetch({
      profiles: () => json([profile("deepseek", ["json_object", "tool_use"])]),
      workspaceConsents: () => json([{
        consent_id: "consent-1",
        conversation_id: "work-1",
        workspace_hash: "a".repeat(64),
        profile_id: "deepseek",
        profile_revision: "b".repeat(64),
        model: "deepseek-chat",
        policy_version: "workspace-files-v1",
        provider_host: "api.deepseek.com",
        granted_at: now,
        revoked_at: null,
        valid_for_current_context: true,
      }]),
    });

    render(<AgentPage />);
    const modelPicker = await screen.findByLabelText("模型");
    await screen.findByRole("option", { name: "api.deepseek.com · deepseek-chat" });
    await user.selectOptions(modelPicker, "deepseek:deepseek-chat");
    expect(await screen.findByText("文件分析已授权")).toBeInTheDocument();

    await user.selectOptions(modelPicker, "deepseek:deepseek-reasoner");
    expect(await screen.findByText("文件分析未授权")).toBeInTheDocument();

    await user.selectOptions(modelPicker, "deepseek:deepseek-chat");
    expect(await screen.findByText("文件分析已授权")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "更换工作区" }));
    expect(await screen.findByText("文件分析未授权")).toBeInTheDocument();
  });

  it("keeps the new conversation's file scope and consent when stale requests reject after cancellation", async () => {
    const user = userEvent.setup();
    let rejectStaleConsent: ((reason: unknown) => void) | undefined;
    let rejectStaleFiles: ((reason: unknown) => void) | undefined;
    let staleConsentSignal: AbortSignal | null | undefined;
    let staleFilesSignal: AbortSignal | null | undefined;
    const validConsent = {
      consent_id: "consent-2",
      conversation_id: "work-2",
      workspace_hash: "a".repeat(64),
      profile_id: "deepseek",
      profile_revision: "b".repeat(64),
      model: "deepseek-chat",
      policy_version: "workspace-files-v1",
      provider_host: "api.deepseek.com",
      granted_at: now,
      revoked_at: null,
      valid_for_current_context: true,
    };
    installFetch({
      conversations: () => json([
        conversation({ conversation_id: "work-1", title: "第一个会话" }),
        conversation({ conversation_id: "work-2", title: "第二个会话" }),
      ]),
      profiles: () => json([profile("deepseek", ["json_object", "tool_use"])]),
      workspaceConsents: (conversationId, signal) => {
        if (conversationId === "work-1") {
          staleConsentSignal = signal;
          return new Promise<Response>((_resolve, reject) => { rejectStaleConsent = reject; });
        }
        return json([validConsent]);
      },
      workspaceFiles: (conversationId, _directory, signal) => {
        if (conversationId === "work-1") {
          staleFilesSignal = signal;
          return new Promise<Response>((_resolve, reject) => { rejectStaleFiles = reject; });
        }
        return json({ directory: "", total: 1, entries: [{ path: "dataset_description.json", kind: "file", readable: true }] });
      },
    });

    render(<AgentPage />);
    await screen.findByRole("option", { name: "第二个会话" });
    const modelPicker = await screen.findByLabelText("模型");
    await user.selectOptions(modelPicker, "deepseek:deepseek-chat");
    await waitFor(() => {
      expect(rejectStaleConsent).toBeTypeOf("function");
      expect(rejectStaleFiles).toBeTypeOf("function");
    });

    await user.selectOptions(screen.getByLabelText("当前会话"), "work-2");
    expect(await screen.findByText("文件分析已授权")).toBeInTheDocument();
    expect(await screen.findByRole("checkbox", { name: /dataset_description\.json/ })).toBeInTheDocument();
    expect(staleConsentSignal?.aborted).toBe(true);
    expect(staleFilesSignal?.aborted).toBe(true);

    await act(async () => {
      rejectStaleConsent?.(new DOMException("The operation was aborted.", "AbortError"));
      rejectStaleFiles?.(new DOMException("The operation was aborted.", "AbortError"));
    });

    expect(screen.getByText("文件分析已授权")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /dataset_description\.json/ })).toBeInTheDocument();
  });

  it("starts a new conversation and switches between existing ones", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch({
      conversations: () =>
        json([
          conversation({ conversation_id: "work-1", title: "第一个会话" }),
          conversation({ conversation_id: "work-2", title: "第二个会话" }),
        ]),
    });

    render(<AgentPage />);
    await screen.findByRole("option", { name: "第一个会话" });

    await user.selectOptions(screen.getByLabelText("当前会话"), "work-2");
    expect(screen.getByLabelText("当前会话")).toHaveValue("work-2");

    await user.click(screen.getByRole("button", { name: "新会话" }));

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([input, init]) => pathOf(input).endsWith("/conversations") && init?.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("picks a workspace through the system directory picker", async () => {
    const user = userEvent.setup();
    installFetch({ conversations: () => json([]) });

    render(<AgentPage />);
    await screen.findByRole("button", { name: "选择工作区" });

    await user.click(screen.getByRole("button", { name: "选择工作区" }));

    expect(await screen.findByText(/D:\/picked-work/)).toBeInTheDocument();
    // The pointer is persisted so a reload keeps the same workspace.
    await waitFor(() =>
      expect(window.localStorage.getItem("rsfmri-workspace-v1")).toContain("D:/picked-work"),
    );
  });

  it("reports the run stage and progress for an active work run", async () => {
    installFetch({
      conversations: () =>
        json([
          conversation({
            active_run_id: "run-1",
            messages: [message("a1", "assistant", "已排队。"), message("u1", "user", "开始")],
          }),
        ]),
      run: () =>
        json({
          run_id: "run-1",
          project_id: null,
          plan_revision_id: "plan-1",
          state: "running",
          version: 2,
          attempt: 1,
          cancel_requested: false,
          error: null,
          qc_mode: "require",
          qc_status: "pending",
          stage: "staging",
          stage_progress: 0.42,
          created_at: now,
          updated_at: now,
        }),
    });

    render(<AgentPage />);

    expect(await screen.findByText("当前阶段：准备输入")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "运行与结果" })).toHaveTextContent("计算中");
    expect(screen.queryByText("当前进度：42%")).not.toBeInTheDocument();
  });

  it("sends a prompt with Enter and keeps the returned conversation", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch();

    render(<AgentPage />);
    await screen.findByText(/告诉我你的目标/);

    await user.type(screen.getByPlaceholderText(/DemoData/), "准备 ALFF 方案{Enter}");

    expect(await screen.findByText("方案已准备，请明确确认后启动。")).toBeInTheDocument();
    const turn = fetchSpy.mock.calls.find(([input]) => pathOf(input).endsWith("/turns"));
    expect(JSON.parse(String(turn?.[1]?.body))).toMatchObject({ content: "准备 ALFF 方案" });
  });

  it("offers remote search only when a model declares the capability", async () => {
    const user = userEvent.setup();
    installFetch({ profiles: () => json([profile("deepseek", ["web_search"])]) });

    render(<AgentPage />);
    await user.click(screen.getByRole("tab", { name: /Chat/ }));

    const toggle = await screen.findByLabelText("联网搜索");
    expect(toggle).toBeEnabled();
    expect(toggle).not.toBeChecked();

    await user.click(toggle);
    expect(toggle).toBeChecked();
  });

  it("keeps remote search disabled when no model declares the capability", async () => {
    const user = userEvent.setup();
    installFetch({ profiles: () => json([profile("deepseek", ["json_object"])]) });

    render(<AgentPage />);
    await user.click(screen.getByRole("tab", { name: /Chat/ }));

    expect(await screen.findByLabelText("联网搜索")).toBeDisabled();
  });

  it("renders the ordered tool-loop steps attached to the reply", async () => {
    installFetch({
      conversations: () =>
        json([
          conversation({
            messages: [
              message("u1", "user", "ALFF 的生理含义是什么?"),
              message("a1", "assistant", "ALFF 反映低频振幅 [C1]。"),
            ],
            tool_calls: [
              toolCall("t1", "search_evidence", 1, "read", 12),
              toolCall("t2", "list_skills", 2, "read", 3),
            ],
          }),
        ]),
    });

    render(<AgentPage />);

    await userEvent.click(await screen.findByText("查看工具检查详情（2 项）"));
    const steps = await screen.findByRole("list", { name: "Agent 工具步骤" });
    expect(steps.querySelectorAll("li")).toHaveLength(2);
    // Step order is the order the loop ran, not insertion order in the response.
    expect(screen.getByText("search_evidence")).toBeInTheDocument();
    expect(screen.getByText("list_skills")).toBeInTheDocument();
    expect(screen.getByText("12 ms")).toBeInTheDocument();
  });

  it("does not render steps for a single-shot turn", async () => {
    installFetch({
      conversations: () =>
        json([
          conversation({
            messages: [
              message("u1", "user", "你好"),
              message("a1", "assistant", "你好，我可以协助 rs-fMRI 分析。"),
            ],
            tool_calls: [toolCall("t1", "rsfmri_chat_llm", null, null, null)],
          }),
        ]),
    });

    render(<AgentPage />);

    expect(await screen.findByText(/你好，我可以协助/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Agent 工具步骤" })).not.toBeInTheDocument();
  });
  it.each(["work", "chat"])("shows %s processing feedback and displays the authoritative final", async (mode) => {
    const user = userEvent.setup();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    const fetchSpy = installFetch({
      conversations: () => json([conversation({ mode })]),
      turn: () => Promise.resolve(new Response(stream, { status: 201, headers: { "Content-Type": "text/event-stream" } })),
    });
    render(<AgentPage />);
    if (mode === "chat") await user.click(screen.getByRole("tab", { name: /Chat/ }));
    await screen.findByRole("option", { name: "DemoData ALFF" });
    await user.type(screen.getByRole("textbox"), "流式问题{Enter}");
    await waitFor(() => expect(fetchSpy.mock.calls.some(([input]) => pathOf(input).endsWith("/turns"))).toBe(true));
    expect(screen.getByText("流式问题")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Work/ })).toBeDisabled();
    expect(screen.getByLabelText("当前会话")).toBeDisabled();
    const emit = async (type: string, data: unknown) => act(async () => {
      controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`));
    });
    await emit("delta", { text: "临时" });
    if (mode === "chat") expect(await screen.findByText("临时")).toBeInTheDocument();
    else expect(screen.queryByText("临时")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认应用补丁" })).not.toBeInTheDocument();
    await emit("reset", {});
    expect(screen.queryByText("临时")).not.toBeInTheDocument();
    await emit("delta", { text: "新临时回答" });
    await emit("status", { message: "正在整理结果" });
    expect(await screen.findByText("正在整理结果")).toBeInTheDocument();
    const current = conversation({ mode, messages: [message("u1", "user", "流式问题"), message("a1", "assistant", "最终完整回答")] });
    await emit("final", { conversation: current, user_message: current.messages[0], assistant_message: current.messages[1], tool_calls: [], tool_call: null });
    expect(await screen.findByText("最终完整回答")).toBeInTheDocument();
    expect(screen.queryByText("新临时回答")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Work/ })).toBeEnabled();
    expect(screen.getAllByText("流式问题")).toHaveLength(1);
  });

  it("marks disconnected output incomplete and restores the prompt for an idempotent retry", async () => {
    const user = userEvent.setup();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const fetchSpy = installFetch({ turn: () => Promise.resolve(new Response(new ReadableStream({ start(value) { controller = value; } }), { headers: { "Content-Type": "text/event-stream" } })) });
    render(<AgentPage />);
    await screen.findByRole("option", { name: "DemoData ALFF" });
    await user.type(screen.getByRole("textbox"), "重试问题{Enter}");
    await waitFor(() => expect(fetchSpy.mock.calls.some(([input]) => pathOf(input).endsWith("/turns"))).toBe(true));
    await act(async () => {
      controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"text":"部分结果"}\n\n'));
      controller.close();
    });
    expect(await screen.findByText("本轮结果尚未确认，请重试恢复。")).toBeInTheDocument();
    expect(screen.queryByText("部分结果")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("重试问题");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(fetchSpy.mock.calls.filter(([input]) => pathOf(input).endsWith("/turns"))).toHaveLength(2));
    const keys = fetchSpy.mock.calls.filter(([input]) => pathOf(input).endsWith("/turns")).map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]);
  });

  it("aborts stream reading when the page unmounts", async () => {
    const user = userEvent.setup();
    let signal: AbortSignal | undefined | null;
    installFetch({ turn: (init) => {
      signal = init?.signal;
      return new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    } });
    const view = render(<AgentPage />);
    await screen.findByRole("option", { name: "DemoData ALFF" });
    await user.type(screen.getByRole("textbox"), "问题{Enter}");
    await waitFor(() => expect(signal).toBeTruthy());
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("stops waiting and restores the composer without claiming the operation was cancelled", async () => {
    const user = userEvent.setup();
    let signal: AbortSignal | undefined | null;
    installFetch({ turn: (init) => {
      signal = init?.signal;
      return new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    } });
    render(<AgentPage />);
    await screen.findByRole("option", { name: "DemoData ALFF" });
    await user.type(screen.getByRole("textbox"), "等待中的问题{Enter}");
    await waitFor(() => expect(signal).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "停止等待" }));
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole("textbox")).toBeEnabled();
    expect(screen.getByRole("textbox")).toHaveValue("等待中的问题");
    expect(screen.getByText(/服务端可能继续保存本轮结果/)).toBeInTheDocument();
  });

});
