import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-09-18T00:00:00Z";

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installConversationApi(page: Page) {
  const messages: Record<string, unknown>[] = [];
  let activeRunId: string | null = null;
  let projectId: string | null = null;
  let planningRequests = 0;
  let startRequests = 0;
  const confirmationCommand = "批准并启动 " + "b".repeat(64) + " 做头动QC mock-challenge";
  const conversation = () => ({
    conversation_id: "work-e2e",
    mode: "work",
    title: "DemoData ALFF",
    workspace_path: "D:\\demo-work",
    preferred_profile_id: null,
    project_id: projectId,
    active_run_id: activeRunId,
    version: 1,
    created_at: now,
    updated_at: now,
    messages,
    tool_calls: [],
  });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/conversations") && request.method() === "GET") return json(route, [conversation()]);
    if (path.endsWith("/model-profiles")) return json(route, []);
    if (path.endsWith("/conversations/work-e2e") && request.method() === "GET") return json(route, conversation());
    if (path.endsWith("/conversations/work-e2e/turns") || path.endsWith("/conversations/work-e2e/work-confirmation")) {
      const isConfirmation = path.endsWith("/work-confirmation");
      if (isConfirmation) {
        expect(request.postDataJSON()).toEqual({ command: confirmationCommand, expected_draft_hash: null });
        startRequests += 1;
      } else {
        planningRequests += 1;
      }
      const content = isConfirmation ? "已确认本次方案并启动计算。" : String(request.postDataJSON().content);
      const user = { message_id: `u-${messages.length}`, conversation_id: "work-e2e", sequence: messages.length + 1, role: "user", content, payload: {}, created_at: now };
      messages.push(user);
      projectId = "project-1";
      if (isConfirmation) activeRunId = "run-12345678";
      const assistant = {
        message_id: `a-${messages.length}`,
        conversation_id: "work-e2e",
        sequence: messages.length + 1,
        role: "assistant",
        content: isConfirmation
          ? "DPABI 任务已进入执行队列。本次将执行头动 QC；任务完成后我会报告结果和质量控制状态。"
          : "ALFF 方案已准备好，请审查后确认启动计算。",
        payload: isConfirmation
          ? { run: { run_id: activeRunId, state: "queued", version: 1 } }
          : {
              project: { project_id: projectId, version: 1 },
              dataset: { dataset_id: "dataset-1", version: 2 },
              manifest: { manifest_id: "manifest-1", content_hash: "a".repeat(64) },
              plan_revision: { plan_revision_id: "plan-1", plan_hash: "b".repeat(64), state: "awaiting_approval", version: 1 },
              confirmation_request: {
                title: "确认并启动计算", summary: "ALFF，保留全部时间点，结果图平滑 4 mm，执行头动 QC。",
                options: [{ label: "确认并启动计算", description: "按已审查方案启动本次计算。", command: confirmationCommand }],
              },
            },
        created_at: now,
      };
      messages.push(assistant);
      return json(route, { conversation: conversation(), user_message: user, assistant_message: assistant, tool_call: null }, 201);
    }
    if (path.endsWith("/runs/run-12345678")) {
      return json(route, {
        run_id: "run-12345678", project_id: "project-1", plan_revision_id: "plan-1",
        state: "qc_review", version: 3, attempt: 1, cancel_requested: false, error: null,
        stage: "completed", stage_progress: 1, heartbeat: now, log_cursor: 1, eta_seconds: 0,
        diagnosis_available: false, created_at: now, updated_at: now,
      });
    }
    if (path.endsWith("/projects")) return json(route, []);
    if (path.endsWith("/artifacts")) return json(route, []);
    if (path.endsWith("/qc-review")) return json(route, null);
    if (path.endsWith("/events")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    if (path.endsWith("/plan-revisions/plan-1")) return json(route, { plan_revision_id: "plan-1", state: "approved", version: 2 });
    return json(route, { error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);
  });
  return { counts: () => ({ planningRequests, startRequests }) };
}

test("keeps Chat and Work as the only workspace modes", async ({ page }) => {
  await installConversationApi(page);
  await page.goto("/");

  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link")).toHaveCount(1);
  await expect(page.getByRole("tab", { name: /Work/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: /Chat/ })).toBeVisible();
  await expect(page.locator(".work-message-card")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "设置" })).toHaveAttribute("href", "/settings");
});

test("prepares and starts DemoData ALFF naturally in Work", async ({ page }) => {
  const fixture = await installConversationApi(page);
  await page.goto("/data");

  const input = page.getByPlaceholder("例如：用当前 DemoData 准备 ALFF 预处理方案");
  await input.fill("使用当前 DemoData 的 Sub_001 完成 ALFF 预处理，先准备并展示方案");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("ALFF 方案已准备好，请审查后确认启动计算。")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await page.getByRole("button", { name: "待确认 · 确认并启动计算" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "确认并启动计算" }).click();
  await expect(page.getByRole("heading", { name: "计算完成 · 待 QC 审查" })).toBeVisible();
  await expect(page.getByText("指标计算已完成，科学质量审查尚未通过。")).toBeVisible();
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 1 });
  await page.reload();
  await expect(page.getByRole("heading", { name: "计算完成 · 待 QC 审查" })).toBeVisible();
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 1 });
  await expect(page.locator(".work-message-card")).toHaveCount(0);
});

test("shows Work errors without rendering an operation card", async ({ page }) => {
  await installConversationApi(page);
  await page.route("**/api/v1/conversations/work-e2e/turns", async (route) => json(route, {
    error: { code: "source_boundary_violation", message: "源目录超出允许范围", details: {}, trace_id: "trace-e2e" },
  }, 422));
  await page.goto("/");

  await page.getByPlaceholder("例如：用当前 DemoData 准备 ALFF 预处理方案").fill("处理当前工作区");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByRole("alert")).toContainText("源目录超出允许范围");
  await expect(page.locator(".work-message-card")).toHaveCount(0);
});
