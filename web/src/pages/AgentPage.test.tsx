import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetWorkspace } from "../workspace";
import { AgentPage } from "./AgentPage";

const now = "2026-09-18T00:00:00Z";

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
  conversations?: () => Promise<Response>;
  created?: () => Promise<Response>;
  run?: () => Promise<Response>;
  picked?: () => Promise<Response>;
  profiles?: () => Promise<Response>;
  workspaceConsents?: () => Promise<Response>;
  grantWorkspaceConsent?: () => Promise<Response>;
  turn?: (init?: RequestInit) => Promise<Response>;
};

function installFetch(options: Options = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = pathOf(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/model-profiles")) return (options.profiles ?? (() => json([])))();
    if (path.endsWith("/providers/models")) return json({ models: ["deepseek-chat", "deepseek-reasoner"] });
    if (path.endsWith("/workspace/consents")) {
      if (method === "POST") return (options.grantWorkspaceConsent ?? (() => json({ error: { code: "workspace_tool_probe_failed", message: "所选模型未能通过工具调用测试。", details: {}, trace_id: "probe-trace" } }, 422)))();
      return (options.workspaceConsents ?? (() => json([])))();
    }
    if (path.endsWith("/workspaces/pick")) {
      return (options.picked ?? (() => json({ path: "D:/picked-work" })))();
    }
    if (path.endsWith("/conversations") && method === "POST") {
      return (options.created ?? (() => json(conversation({ conversation_id: "work-2", title: "新会话" }))))();
    }
    if (path.endsWith("/conversations")) {
      return (options.conversations ?? (() => json([conversation()])))();
    }
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
    if (path.includes("/runs/")) return (options.run ?? (() => json({})))();
    return notFound();
  });
}

describe("agent workbench page", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    resetWorkspace();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
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

    expect(await screen.findByText("任务状态：staging")).toBeInTheDocument();
    expect(screen.getByText("当前进度：42%")).toBeInTheDocument();
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
  it.each(["work", "chat"])("renders %s deltas immediately and replaces them only after final", async (mode) => {
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
    expect(await screen.findByText("临时")).toBeInTheDocument();
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
    expect(await screen.findByText(/回答未完成，临时内容可能不完整/)).toBeInTheDocument();
    expect(screen.getByText("部分结果")).toBeInTheDocument();
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

});
