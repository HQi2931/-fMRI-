import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import type { Conversation } from "./api/client";
import { Router } from "./routing";

const now = "2026-09-18T00:00:00Z";

function json(value: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

function pathOf(input: RequestInfo | URL): string {
  return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://local").pathname;
}

function conversation(): Conversation {
  return {
    conversation_id: "work-1", mode: "work", title: "DemoData ALFF", workspace_path: "D:/demo-work",
    preferred_profile_id: null, project_id: null, active_run_id: null, version: 1,
    created_at: now, updated_at: now, messages: [], tool_calls: [],
  };
}

function baseApi(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = pathOf(input);
  if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
  if (path.endsWith("/model-profiles")) return json([]);
  if (path.endsWith("/conversations") && init?.method !== "POST") return json([conversation()]);
  if (path.endsWith("/turns")) {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    const current = conversation();
    current.project_id = "project-1";
    current.messages = [
      { message_id: "u1", conversation_id: "work-1", sequence: 1, role: "user", content: String(body.content), payload: {}, created_at: now },
      { message_id: "a1", conversation_id: "work-1", sequence: 2, role: "assistant", content: "方案已准备，请明确确认后启动。", payload: { plan_revision: { plan_revision_id: "plan-1", version: 2, plan_hash: "a".repeat(64), state: "awaiting_approval" } }, created_at: now },
    ];
    return json({ conversation: current, user_message: current.messages[0], assistant_message: current.messages[1], tool_call: null });
  }
  if (path.endsWith("/environment/config")) return json({ matlab_executable: null, spm_dir: null, dpabi_dir: null, matlab_version: "unspecified", spm_version: "unspecified", dpabi_version: "unspecified", configured: false });
  if (path.endsWith("/environment/probe")) return json({ ready: false, environment_hash: "e".repeat(64), components: [] });
  return json({ error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);
}

describe("single conversation workbench", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.spyOn(globalThis, "fetch").mockImplementation(baseApi);
  });

  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  function renderAt(path: string) {
    window.history.replaceState({}, "", path);
    return render(<Router><App /></Router>);
  }

  it("shows only one navigation entry and the Chat/Work modes", async () => {
    renderAt("/");
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(within(navigation).getAllByRole("link")).toHaveLength(1);
    expect(screen.getByRole("tab", { name: /Work/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Chat/ })).toBeInTheDocument();
    expect(screen.queryByText("操作卡片")).not.toBeInTheDocument();
    expect(await screen.findByText("服务已连接")).toBeInTheDocument();
  });

  it("routes old business paths to the same Work conversation without cards", async () => {
    renderAt("/plan");
    expect(screen.getByRole("tab", { name: /Work/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("打开功能卡片")).not.toBeInTheDocument();
  });

  it("sends a natural Work request and persists returned plan context", async () => {
    const user = userEvent.setup();
    renderAt("/agent");
    await user.type(screen.getByPlaceholderText(/DemoData/), "用当前 DemoData 准备 ALFF 预处理方案");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("方案已准备，请明确确认后启动。")).toBeInTheDocument();
    await waitFor(() => expect(window.localStorage.getItem("rsfmri-workspace-v1")).toContain("plan-1"));
    const call = vi.mocked(globalThis.fetch).mock.calls.find(([input]) => pathOf(input).endsWith("/turns"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ content: "用当前 DemoData 准备 ALFF 预处理方案" });
  });

  it("keeps environment configuration behind Settings", async () => {
    const user = userEvent.setup();
    renderAt("/agent");
    await user.click(screen.getByRole("link", { name: /设置/ }));
    expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
  });
});
