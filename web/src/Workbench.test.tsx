import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import type { Conversation, WorkCard } from "./api/client";
import { businessCapabilityForPath, Router } from "./routing";
import { cardsFrom } from "./work/catalog";

const now = "2026-09-17T00:00:00Z";

function json(value: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

function pathOf(input: RequestInfo | URL): string {
  return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://local").pathname;
}

const dataCard: WorkCard = {
  card_id: "card-data",
  kind: "data",
  title: "数据与清单",
  version: 1,
  draft_ref: "draft-data",
  draft: { projectName: "恢复的项目", datasetName: "主数据集" },
  bindings: {},
  allowed_operations: ["saveDraft", "createProject", "createDataset", "inspectDataset", "importDemographics", "createSplit"],
};
const projectCard: WorkCard = {
  ...dataCard,
  card_id: "card-project",
  kind: "project",
  title: "项目与工作区",
  draft_ref: "draft-project",
  draft: {},
  allowed_operations: ["saveDraft", "selectProject", "createProject"],
};
const settingsCard: WorkCard = {
  ...dataCard,
  card_id: "card-settings",
  kind: "settings",
  title: "环境与模型设置",
  draft_ref: "draft-settings",
  draft: {},
  allowed_operations: ["saveDraft"],
};

function conversation(card: WorkCard | null = null): Conversation {
  return {
    conversation_id: "work-1", mode: "work", title: "Work", workspace_path: null,
    preferred_profile_id: null, project_id: null, active_run_id: null, version: 1,
    created_at: now, updated_at: now,
    messages: card ? [{
      message_id: "assistant-1", conversation_id: "work-1", sequence: 1,
      role: "assistant", content: "请补充数据参数。", payload: { work_cards: [card] }, created_at: now,
    }] : [],
    tool_calls: [],
  };
}

function baseApi(input: RequestInfo | URL): Promise<Response> {
  const path = pathOf(input);
  if (path.endsWith("/health")) return json({ status: "ok", database: "ok" });
  if (path.endsWith("/model-profiles") || path.endsWith("/literature/papers") || path.endsWith("/projects")) return json([]);
  if (path.endsWith("/conversations")) return json([conversation()]);
  if (path.endsWith("/context")) return json({ memories: [], summary: null });
  return json({ error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);
}

function renderAt(pathname: string) {
  window.history.replaceState({}, "", pathname);
  return render(<Router><App /></Router>);
}

describe("single Work workbench", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.spyOn(globalThis, "fetch").mockImplementation(baseApi);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps one business navigation item and defaults to Work", async () => {
    renderAt("/");
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(within(navigation).getAllByRole("link")).toHaveLength(1);
    expect(within(navigation).getByRole("link", { name: "对话工作台" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "设置" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("tab", { name: /Work rs-fMRI 工作流/ })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("服务已连接")).toBeInTheDocument();
  });

  it("turns a legacy business URL into a non-executing Work card prompt", async () => {
    expect(businessCapabilityForPath("/dashboard")).toBe("project");
    let current = conversation();
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/conversations")) return json([current]);
      if (path.endsWith("/conversations/work-1/turns") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        expect(body.card_kind).toBe("data");
        current = conversation(dataCard);
        return json({ conversation: current, user_message: {}, assistant_message: {}, tool_call: null });
      }
      return baseApi(input);
    });
    const user = userEvent.setup();
    renderAt("/data");
    expect(screen.getByText("这个功能已经移入 Work 对话")).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.some(([input]) => pathOf(input).endsWith("/turns"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "打开功能卡片" }));
    expect(await screen.findByDisplayValue("恢复的项目")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/data");
  });

  it("restores card drafts and saves edits through the versioned card endpoint", async () => {
    let current = conversation(dataCard);
    const savedBodies: Array<{ expected_version: number; operation: string; args: Array<Record<string, unknown>> }> = [];
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/conversations")) return json([current]);
      if (path.endsWith("/cards/card-data/actions") && init?.method === "POST") {
        const savedBody = JSON.parse(String(init.body)) as typeof savedBodies[number];
        savedBodies.push(savedBody);
        const savedDraft = savedBody.args[0];
        const nextCard = { ...dataCard, version: 2, draft: {
          projectName: String(savedDraft.projectName), datasetName: String(savedDraft.datasetName),
        } };
        current = conversation(nextCard);
        return json({ card: nextCard, conversation: current, result: nextCard.draft });
      }
      return baseApi(input);
    });
    const user = userEvent.setup();
    renderAt("/agent");
    const projectName = await screen.findByDisplayValue("恢复的项目");
    await user.clear(projectName);
    await user.type(projectName, "更新后的项目");
    await waitFor(() => expect(savedBodies).toHaveLength(1), { timeout: 2_000 });
    expect(savedBodies[0]).toMatchObject({ expected_version: 1, operation: "saveDraft" });
    expect(savedBodies[0].args[0].projectName).toBe("更新后的项目");
  });

  it("selects an existing project through the project card", async () => {
    const project = { project_id: "p1", name: "现有项目", source_roots: ["D:\\data"], work_root: "D:\\work", version: 3, created_at: now };
    let current = conversation(projectCard);
    vi.mocked(fetch).mockImplementation((input, init) => {
      const path = pathOf(input);
      if (path.endsWith("/conversations")) return json([current]);
      if (path.endsWith("/projects")) return json([project]);
      if (path.endsWith("/cards/card-project/actions") && init?.method === "POST") {
        const next = { ...projectCard, version: 2, bindings: { project_id: "p1" } };
        current = { ...conversation(next), project_id: "p1", workspace_path: "D:\\data" };
        return json({ card: next, conversation: current, result: project });
      }
      return baseApi(input);
    });
    const user = userEvent.setup();
    renderAt("/agent");
    await user.click(await screen.findByRole("button", { name: "现有项目" }));
    await waitFor(() => expect(JSON.parse(String(window.localStorage.getItem("rsfmri-workspace-v1"))).projectId).toBe("p1"));
    expect(screen.getByRole("button", { name: "建立新项目 / 登记数据" })).toBeInTheDocument();
  });

  it("renders the settings card and accepts the singular card payload shape", async () => {
    const singular = conversation();
    singular.messages = [{
      message_id: "single", conversation_id: "work-1", sequence: 1, role: "assistant",
      content: "设置", payload: { work_card: settingsCard }, created_at: now,
    }];
    expect(cardsFrom(singular)).toEqual([settingsCard]);
    vi.mocked(fetch).mockImplementation((input) => pathOf(input).endsWith("/conversations")
      ? json([conversation(settingsCard)]) : baseApi(input));
    renderAt("/agent");
    expect(await screen.findByText(/环境路径和模型连接统一在/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "设置" })).toHaveLength(2);
  });
});
