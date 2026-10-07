import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-09-18T00:00:00Z";

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installConversationApi(page: Page, options: { dropStartResponse?: boolean } = {}) {
  const messages: Record<string, unknown>[] = [];
  let activeRunId: string | null = null;
  let projectId: string | null = null;
  let planningRequests = 0;
  let startRequests = 0;
  let invalidateOnStart = false;
  let simulatedRunState = "qc_review";
  let cancelRequests = 0;
  let confirmationCommand = "批准并启动 " + "b".repeat(64) + " 做头动QC mock-challenge";
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
        if (invalidateOnStart) return json(route, { error: { code: "work_input_changed", message: "输入文件已变化", details: {}, trace_id: null } }, 409);
        if (request.postDataJSON().command !== confirmationCommand) {
          return json(route, { error: { code: "work_confirmation_changed", message: "方案已更新，请审查最新版本。", details: {}, trace_id: null } }, 409);
        }
        expect(request.postDataJSON()).toEqual({ command: confirmationCommand, expected_draft_hash: "d".repeat(64) });
        startRequests += 1;
      } else {
        planningRequests += 1;
      }
      const content = isConfirmation ? confirmationCommand : String(request.postDataJSON().content);
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
              work_draft: { hash: "d".repeat(64), draft: { requested_metrics: ["alff"], preprocessing: { tr_seconds: 2 } }, compiled: true, sources: { "preprocessing.tr_seconds": "data" }, field_states: { "preprocessing.tr_seconds": "verified" } },
              confirmation_request: {
                title: "确认并启动计算", summary: "ALFF，保留全部时间点，结果图平滑 4 mm，执行头动 QC。",
                options: [{ label: "确认并启动计算", description: "按已审查方案启动本次计算。", command: confirmationCommand }],
              },
            },
        created_at: now,
      };
      messages.push(assistant);
      if (isConfirmation && options.dropStartResponse) return route.abort("failed");
      return json(route, { conversation: conversation(), user_message: user, assistant_message: assistant, tool_call: null }, 201);
    }
    if (path.endsWith("/work-review")) {
      const assistant = {
        message_id: "recovery-draft", conversation_id: "work-e2e", sequence: messages.length + 1,
        role: "assistant", content: "旧输入事实已撤回，请确认输入阶段并重新读取。", created_at: now,
        payload: {
          work_draft: { hash: "d".repeat(64), draft: { requested_metrics: ["alff"], preprocessing: { tr_seconds: 2 } }, compiled: false, input_evidence_stale: true, input_manifest_stale: true, sources: { "preprocessing.tr_seconds": "stale_data" }, field_states: { "preprocessing.tr_seconds": "review_pending" } },
          draft_issues: ["输入文件已变化，请重新读取并核实采集信息。"],
          confirmation_request: { title: "重新确认输入阶段", summary: "重新冻结后还需重读事实。", options: [{ label: "使用 FunRaw", description: "重新冻结输入", command: "确认起始阶段 FunRaw aaaaaaaaaaaa" }] },
        },
      };
      messages.push(assistant);
      return json(route, { conversation: conversation(), user_message: null, assistant_message: assistant, tool_call: null }, 201);
    }
    if (path.endsWith("/runs/run-12345678/cancel")) {
      expect(request.postDataJSON()).toEqual({ expected_version: 3, reason: "用户在运行卡片明确取消计算" });
      cancelRequests += 1;
      simulatedRunState = "cancelling";
      return json(route, {
        run_id: "run-12345678", project_id: "project-1", plan_revision_id: "plan-1",
        state: simulatedRunState, version: 4, attempt: 1, cancel_requested: true, error: null,
        stage: "realigning", heartbeat: now, log_cursor: 1, diagnosis_available: false,
        created_at: now, updated_at: now,
      });
    }
    if (path.endsWith("/runs/run-12345678")) {
      return json(route, {
        run_id: "run-12345678", project_id: "project-1", plan_revision_id: "plan-1",
        state: simulatedRunState, version: simulatedRunState === "running" ? 3 : 4, attempt: 1,
        cancel_requested: simulatedRunState === "cancelling", error: null,
        stage: simulatedRunState === "qc_review" ? "completed" : "realigning",
        stage_progress: simulatedRunState === "qc_review" ? 1 : 0.25, heartbeat: now, log_cursor: 1, eta_seconds: null,
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
  return {
    counts: () => ({ planningRequests, startRequests }),
    cancellationCount: () => cancelRequests,
    publishRevision: () => {
      confirmationCommand = "批准并启动 " + "c".repeat(64) + " 做头动QC new-challenge";
      messages.push({
        message_id: "new-plan", conversation_id: "work-e2e", sequence: messages.length + 1,
        role: "assistant", content: "结果图平滑已从 4 mm 修改为 6 mm，请审查新方案。", created_at: now,
        payload: {
          plan_revision: { plan_revision_id: "plan-2", plan_hash: "c".repeat(64), state: "awaiting_approval", version: 2 },
          confirmation_request: { title: "确认并启动计算", summary: "新方案：结果图平滑 6 mm。", options: [{ label: "确认并启动计算", description: "审查新版本", command: confirmationCommand }] },
        },
      });
    },
    invalidateInput: () => { invalidateOnStart = true; },
    simulateRunning: () => { simulatedRunState = "running"; },
  };
}

test("recovers an accepted start after its response is lost without posting again", async ({ page }) => {
  const fixture = await installConversationApi(page, { dropStartResponse: true });
  await page.goto("/");
  await page.getByPlaceholder("例如：用当前 DemoData 准备 ALFF 预处理方案").fill("准备 ALFF 方案");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "待确认 · 确认并启动计算" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "确认并启动计算" }).click();
  await expect(page.getByRole("heading", { name: "计算完成 · 待 QC 审查" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 1 });
  await page.reload();
  await expect(page.getByRole("heading", { name: "计算完成 · 待 QC 审查" })).toBeVisible();
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 1 });
});

test("refreshes a concurrently changed plan and closes the stale confirmation", async ({ page }) => {
  const fixture = await installConversationApi(page);
  await page.goto("/");
  await page.getByPlaceholder("例如：用当前 DemoData 准备 ALFF 预处理方案").fill("准备 ALFF 方案");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "待确认 · 确认并启动计算" }).click();
  fixture.publishRevision();
  await page.getByRole("dialog").getByRole("button", { name: "确认并启动计算" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText("已刷新当前方案");
  await expect(page.getByText("结果图平滑已从 4 mm 修改为 6 mm，请审查新方案。")).toBeVisible();
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 0 });
  await page.getByRole("button", { name: "待确认 · 确认并启动计算" }).last().click();
  await expect(page.getByRole("dialog")).toContainText("结果图平滑 6 mm");
});

test("recovers a rejected stale input into a read-only, clearly invalidated draft", async ({ page }) => {
  const fixture = await installConversationApi(page);
  await page.goto("/");
  await page.getByPlaceholder("例如：用当前 DemoData 准备 ALFF 预处理方案").fill("准备 ALFF 方案");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "待确认 · 确认并启动计算" }).click();
  fixture.invalidateInput();
  await page.getByRole("dialog").getByRole("button", { name: "确认并启动计算" }).click();
  await expect(page.getByText("旧输入事实已撤回，请确认输入阶段并重新读取。", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "预处理方案参数" }).getByRole("status")).toContainText("参数选择已保留");
  await expect(page.getByText("来源：历史读取（待重读）")).toBeVisible();
  await expect(page.getByRole("spinbutton", { name: "重复时间 TR（秒）" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "使用 FunRaw", exact: true })).toBeEnabled();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 0 });
});

test("shows cancellation requested until the worker reports that execution stopped", async ({ page }) => {
  const fixture = await installConversationApi(page);
  fixture.simulateRunning();
  await page.goto("/");
  await page.getByPlaceholder("例如：用当前 DemoData 准备 ALFF 预处理方案").fill("准备 ALFF 方案");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "待确认 · 确认并启动计算" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "确认并启动计算" }).click();
  await expect(page.getByRole("heading", { name: "计算中" })).toBeVisible();
  await page.getByRole("button", { name: "取消计算" }).click();
  await expect(page.getByText("正在等待执行器确认取消。")).toBeVisible();
  await expect(page.getByText("计算已取消")).toHaveCount(0);
  expect(fixture.counts()).toEqual({ planningRequests: 1, startRequests: 1 });
  expect(fixture.cancellationCount()).toBe(1);
});

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
