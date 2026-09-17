import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-08-06T00:00:00Z";
type MockCard = {
  card_id: string;
  kind: string;
  title: string;
  version: number;
  draft_ref: string;
  draft: Record<string, unknown>;
  bindings: Record<string, string | null | undefined>;
  allowed_operations: string[];
};

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installBaseApi(page: Page) {
  const cards: Record<string, Record<string, unknown>> = {};
  const messages: Record<string, unknown>[] = [];
  const conversation = () => ({
    conversation_id: "work-e2e", mode: "work", title: "Work", workspace_path: null,
    preferred_profile_id: null, project_id: null, active_run_id: null, version: 1,
    created_at: now, updated_at: now, messages, tool_calls: [],
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/conversations") && request.method() === "GET") return json(route, [conversation()]);
    if (path.endsWith("/conversations/work-e2e/turns")) {
      const body = request.postDataJSON();
      const kind = String(body.card_kind);
      const card = { card_id: `${kind}-card`, kind, title: "操作卡片", version: 1, draft_ref: `${kind}-card`, draft: {}, bindings: {}, allowed_operations: ["saveDraft"] };
      cards[kind] = card;
      messages.push({ message_id: `${kind}-message`, conversation_id: "work-e2e", sequence: messages.length + 1, role: "assistant", content: "已打开操作卡片。", payload: { work_cards: [card] }, created_at: now });
      return json(route, { conversation: conversation(), user_message: messages[0] ?? {}, assistant_message: messages.at(-1), tool_call: null }, 201);
    }
    if (path.endsWith("/context")) return json(route, { memories: [], summary: null });
    if (path.endsWith("/literature/papers")) return json(route, []);
    if (path.endsWith("/projects") || path.endsWith("/runs") || path.endsWith("/skills") || path.endsWith("/model-profiles")) return json(route, []);
    if (path.endsWith("/statistics/results")) return json(route, []);
    if (path.endsWith("/environment/probe")) return json(route, { ready: false, environment_hash: "e".repeat(64), components: [] });
    return json(route, { error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);
  });
}

test("uses Work as the only business navigation and opens routed cards", async ({ page }) => {
  await installBaseApi(page);
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link")).toHaveCount(1);
  await expect(page.getByRole("tab", { name: /Work rs-fMRI 工作流/ })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "项目与工作区" }).click();
  await expect(page.getByRole("heading", { name: "操作卡片" })).toBeVisible();
  await expect(page.getByRole("link", { name: "设置" })).toHaveAttribute("href", "/settings");
});

test("continues an approved plan through Mock execution, QC, and statistics", async ({ page }) => {
  await page.addInitScript((workspace) => {
    window.sessionStorage.setItem("rsfmri-workspace-v1", JSON.stringify(workspace));
  }, {
    projectId: "p1",
    projectVersion: 1,
    datasetId: "d1",
    datasetVersion: 2,
    manifestId: "manifest1",
    manifestHash: "a".repeat(64),
    subjectIds: ["sub-1", "sub-2"],
    planRevisionId: "plan1",
    planVersion: 2,
    planHash: "b".repeat(64),
    planState: "approved",
  });

  let runCreated = false;
  let reviewApproved = false;
  let statisticalState = "draft";
  let statisticalVersion = 1;
  const run = () => ({ run_id: "run1", project_id: "p1", plan_revision_id: "plan1", state: runCreated ? (reviewApproved ? "succeeded" : "qc_review") : "queued", version: reviewApproved ? 3 : runCreated ? 2 : 1, attempt: runCreated ? 1 : 0, cancel_requested: false, error: null, created_at: now, updated_at: now });
  const artifact = { artifact_id: "image1", project_id: "p1", run_id: "run1", artifact_type: "metric.reho", relative_path: "output/reho.nii", checksum: "c".repeat(64), size_bytes: 10, provenance: { executor: "mock" }, created_at: now };
  const secondArtifact = { ...artifact, artifact_id: "image2", relative_path: "output/reho-2.nii", checksum: "2".repeat(64) };
  const mask = { ...artifact, artifact_id: "mask1", artifact_type: "brain_mask", relative_path: "output/mask.nii", checksum: "d".repeat(64) };
  const review = () => ({ review: { review_revision_id: "review1", input_manifest_hash: "a".repeat(64), metric_artifact_ids: ["image1", "image2"], checks: [{ code: "visual.metric_review", severity: "info", passed: true, evidence_artifact_ids: ["image1", "image2"], message: "reviewed" }], included_subject_ids: ["sub-1", "sub-2"], excluded_subject_ids: [], exclusion_reasons: [], approved: reviewApproved, approved_by: reviewApproved ? "qc-reviewer" : null, approval_reason: reviewApproved ? "reviewed evidence" : null, content_hash: "f".repeat(64) }, run_id: "run1", project_id: "p1", revision: 1, version: reviewApproved ? 2 : 1, state: reviewApproved ? "approved" : "draft", created_at: now, updated_at: now });
  const plan = () => ({ plan_revision_id: "stat-plan", project_id: "p1", revision: 1, version: statisticalVersion, plan_hash: "9".repeat(64), manifest_hash: "a".repeat(64), environment_hash: "e".repeat(64), state: statisticalState, plan: { kind: "statistical_design" }, validation_issues: [], supersedes_plan_revision_id: null, created_at: now, updated_at: now });
  const design = { revision_id: "design1", test: "one_sample_t", subject_order: ["sub-1", "sub-2"], images: [{ subject_id: "sub-1", artifact_id: "image1", group: null, condition: null }, { subject_id: "sub-2", artifact_id: "image2", group: null, condition: null }], group_order: [], condition_order: [], covariates: [], contrast: [1], one_sample_baseline: 0, mask_artifact_id: "mask1", tail: "two_sided", missing_value_policy: "error", qc_review_revision_id: "review1", qc_review_hash: "f".repeat(64) };
  const designView = () => ({ design, correction: { method: "fdr", q_threshold: 0.05, mask_artifact_id: "mask1", statistic_type: "T", df1: 1, df2: null }, design_matrix: [[1]], plan_revision: plan() });
  const cardOperations: Record<string, string[]> = {
    runs: ["saveDraft", "createRun", "cancelRun", "retryRun", "diagnoseRun"],
    qc: ["saveDraft", "createQcReview", "approveQcReview"],
    statistics: ["saveDraft", "createStatisticalDesign", "validateStatisticalDesign", "approvePlan", "createStatisticsRun"],
  };
  const cards = new Map<string, MockCard>();
  const messages: Record<string, unknown>[] = [];
  const conversation = () => ({
    conversation_id: "work-flow", mode: "work", title: "Mock 全流程", workspace_path: "D:\\data",
    preferred_profile_id: null, project_id: "p1", active_run_id: runCreated ? "run1" : null,
    version: 1, created_at: now, updated_at: now, messages, tool_calls: [],
  });
  const recordCard = (card: MockCard, content: string) => {
    cards.set(String(card.card_id), card);
    messages.push({ message_id: `message-${messages.length + 1}`, conversation_id: "work-flow", sequence: messages.length + 1, role: "assistant", content, payload: { work_cards: [card] }, created_at: now });
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/conversations") && request.method() === "GET") return json(route, [conversation()]);
    if (path.endsWith("/conversations/work-flow/turns")) {
      const body = request.postDataJSON();
      const kind = String(body.card_kind);
      const card: MockCard = {
        card_id: `${kind}-card`, kind, title: { runs: "运行与结果", qc: "质量控制审核", statistics: "统计设计与结果" }[kind] ?? kind,
        version: 1, draft_ref: `${kind}-card`, draft: {},
        bindings: { project_id: "p1", plan_revision_id: "plan1", run_id: runCreated ? "run1" : null, qc_review_id: reviewApproved ? "review1" : null },
        allowed_operations: cardOperations[kind] ?? ["saveDraft"],
      };
      recordCard(card, `已打开${card.title}。`);
      return json(route, { conversation: conversation(), user_message: {}, assistant_message: messages.at(-1), tool_call: null }, 201);
    }
    if (path.includes("/cards/") && path.endsWith("/actions")) {
      const cardId = path.split("/cards/")[1].split("/actions")[0];
      const card = cards.get(cardId)!;
      const body = request.postDataJSON();
      const operation = String(body.operation);
      let result: unknown = null;
      if (operation === "saveDraft") card.draft = body.args[0];
      if (operation === "createRun") { runCreated = true; result = { ...run(), state: "queued", version: 1, attempt: 0 }; card.bindings.run_id = "run1"; }
      if (operation === "createQcReview") { result = review(); card.bindings.qc_review_id = "review1"; }
      if (operation === "approveQcReview") { reviewApproved = true; result = review(); }
      if (operation === "createStatisticalDesign") { result = designView(); card.bindings.statistical_design_id = "stat-plan"; }
      if (operation === "validateStatisticalDesign") { statisticalState = "awaiting_approval"; statisticalVersion = 2; result = designView(); }
      if (operation === "approvePlan") { statisticalState = "approved"; statisticalVersion = 3; result = { approval_id: "approval1", plan_revision_id: "stat-plan", plan_hash: "9".repeat(64), actor: "statistics-reviewer", decision: "approved", reason: "reviewed design", created_at: now }; }
      if (operation === "createStatisticsRun") result = { run_id: "stat-run", project_id: "p1", plan_revision_id: "stat-plan", state: "queued", version: 1, attempt: 0, cancel_requested: false, error: null, created_at: now, updated_at: now };
      card.version += 1;
      recordCard({ ...card }, `${card.title}操作已完成。`);
      return json(route, { card, conversation: conversation(), result });
    }
    if (path.endsWith("/context")) return json(route, { memories: [], summary: null });
    if (path.endsWith("/literature/papers") || path.endsWith("/model-profiles")) return json(route, []);
    if (path.endsWith("/runs")) return json(route, runCreated ? [run()] : []);
    if (path.endsWith("/runs/run1/events")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: `data: ${JSON.stringify({ event_id: 1, project_id: "p1", run_id: "run1", event_type: "RunQueued", severity: "info", payload: {}, created_at: now })}\n\n` });
    if (path.endsWith("/runs/run1/artifacts")) return json(route, [artifact, secondArtifact, mask]);
    if (path.endsWith("/runs/run1")) return json(route, run());
    if (path.endsWith("/runs/stat-run/artifacts")) return json(route, []);
    if (path.endsWith("/qc-reviews/review1")) return json(route, review());
    if (path.endsWith("/plan-revisions/stat-plan")) return json(route, plan());
    if (path.endsWith("/statistics/results")) return json(route, []);
    return json(route, { error: { code: "not_found", message: `unhandled ${path}`, details: {}, trace_id: null } }, 404);
  });

  await page.goto("/");
  await page.getByRole("button", { name: "运行与产物" }).click();
  const runsCard = page.locator(".work-message-card").last();
  await runsCard.getByRole("button", { name: "创建已审批计划的 Mock 运行" }).click();
  await expect(page.getByText(/任务已进入本机 SQLite 队列/)).toBeVisible();
  await expect(page.getByText("qc_review", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "质量控制" }).click();
  const qcCard = page.locator(".work-message-card").last();
  await qcCard.getByLabel(/选择指标 Artifact image1/).check();
  await qcCard.getByLabel(/选择指标 Artifact image2/).check();
  await qcCard.getByLabel("QC check code").fill("visual.metric_review");
  await qcCard.getByLabel("QC check severity").selectOption("info");
  await qcCard.getByLabel("QC check passed").selectOption("yes");
  await qcCard.getByLabel("QC check message").fill("reviewed");
  await qcCard.getByLabel(/QC check 证据 image1/).check();
  await qcCard.getByLabel(/QC check 证据 image2/).check();
  await qcCard.getByLabel(/纳入受试者/).fill("sub-1\nsub-2");
  await qcCard.getByLabel("是否排除受试者").selectOption("none");
  await expect(qcCard.getByRole("button", { name: "创建 QC revision" })).toBeEnabled();
  await qcCard.getByRole("button", { name: "创建 QC revision" }).click();
  await qcCard.getByLabel("QC 审批人").fill("qc-reviewer");
  await qcCard.getByLabel("QC 审批理由").fill("reviewed evidence");
  await qcCard.getByRole("button", { name: "人工批准并冻结" }).click();
  await expect(page.getByText(/人工 QC 已批准/)).toBeVisible();

  await page.getByRole("button", { name: "统计与结果" }).click();
  const statisticsCard = page.locator(".work-message-card").last();
  await statisticsCard.getByLabel("检验类型").selectOption("one_sample_t");
  await statisticsCard.getByLabel("尾部").selectOption("two_sided");
  await statisticsCard.getByLabel("单样本基线").fill("0");
  await statisticsCard.getByLabel("缺失值策略").selectOption("error");
  await statisticsCard.getByLabel(/影像映射/).fill("sub-1 | image1\nsub-2 | image2");
  await statisticsCard.getByLabel("脑掩膜 Artifact").selectOption("mask1");
  await statisticsCard.getByLabel("多重比较").selectOption("fdr");
  await statisticsCard.getByLabel("q 阈值").fill("0.05");
  await statisticsCard.getByRole("button", { name: "生成设计矩阵" }).click();
  await statisticsCard.getByRole("button", { name: "验证设计" }).click();
  await statisticsCard.getByLabel("统计设计审批人").fill("statistics-reviewer");
  await statisticsCard.getByLabel("统计设计审批理由").fill("reviewed design");
  await statisticsCard.getByRole("button", { name: "批准统计设计" }).click();
  await expect(statisticsCard.getByText(/统计设计已批准/)).toBeVisible();
  await statisticsCard.getByRole("button", { name: "提交统计运行" }).click();
  await expect(page.getByText(/统计任务已进入本机队列/)).toBeVisible();
});

test("shows a server validation failure without claiming a successful scan", async ({ page }) => {
  let card: MockCard = { card_id: "data-failure", kind: "data", title: "数据登记与清单", version: 1, draft_ref: "data-failure", draft: {}, bindings: {}, allowed_operations: ["saveDraft", "createProject", "createDataset", "inspectDataset", "importDemographics", "createSplit"] };
  const conversation = () => ({ conversation_id: "work-failure", mode: "work", title: "Work", workspace_path: null, preferred_profile_id: null, project_id: card.bindings.project_id ?? null, active_run_id: null, version: 1, created_at: now, updated_at: now, messages: [{ message_id: "failure-card", conversation_id: "work-failure", sequence: 1, role: "assistant", content: "请填写数据参数。", payload: { work_cards: [card] }, created_at: now }], tool_calls: [] });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/conversations") && request.method() === "GET") return json(route, [conversation()]);
    if (path.endsWith("/conversations/work-failure/turns")) return json(route, { conversation: conversation(), user_message: {}, assistant_message: conversation().messages[0], tool_call: null }, 201);
    if (path.includes("/cards/data-failure/actions")) {
      const body = request.postDataJSON();
      if (body.operation === "createProject") {
        card = { ...card, version: 2, bindings: { project_id: "p1" } };
        return json(route, { card, conversation: conversation(), result: { project_id: "p1", name: "研究", source_roots: ["D:\\data"], work_root: "D:\\work", version: 1, created_at: now } });
      }
      if (body.operation === "createDataset") {
        card = { ...card, version: 3, bindings: { project_id: "p1", dataset_id: "d1" } };
        return json(route, { card, conversation: conversation(), result: { dataset_id: "d1", project_id: "p1", name: "主数据集", source_path: "D:\\data", version: 1, created_at: now } });
      }
      if (body.operation === "inspectDataset") return json(route, { error: { code: "source_boundary_violation", message: "源目录超出允许范围", details: {}, trace_id: "trace-e2e" } }, 422);
      card = { ...card, version: card.version + 1, draft: body.args[0] };
      return json(route, { card, conversation: conversation(), result: card.draft });
    }
    if (path.endsWith("/context")) return json(route, { memories: [], summary: null });
    if (path.endsWith("/literature/papers") || path.endsWith("/model-profiles") || path.endsWith("/projects")) return json(route, []);
    return json(route, []);
  });
  await page.goto("/data");
  await page.getByRole("button", { name: "打开功能卡片" }).click();
  const dataCard = page.locator(".work-message-card").last();
  await dataCard.getByLabel("只读源目录").fill("D:\\data");
  await dataCard.getByLabel("独立工作目录").fill("D:\\work");
  await dataCard.getByRole("button", { name: "开始只读检查" }).click();
  await expect(page.getByRole("alert")).toContainText("源目录超出允许范围");
  await expect(page.getByText(/检查完成/)).toHaveCount(0);
});
