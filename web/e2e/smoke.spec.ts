import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-08-06T00:00:00Z";

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installBaseApi(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/projects") || path.endsWith("/runs") || path.endsWith("/skills") || path.endsWith("/model-profiles")) return json(route, []);
    if (path.endsWith("/environment/probe")) return json(route, { ready: false, environment_hash: "e".repeat(64), components: [] });
    return json(route, { error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);
  });
}

test("navigates between the main research steps", async ({ page }) => {
  await installBaseApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "从数据到可信结果" })).toBeVisible();
  await page.getByRole("link", { name: "分析方案", exact: true }).click();
  await expect(page.getByRole("heading", { name: "检查顺序、参数与风险" })).toBeVisible();
  await page.getByRole("link", { name: /统计/ }).click();
  await expect(page.getByRole("heading", { name: "版本化统计设计" })).toBeVisible();
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

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/runs") && request.method() === "POST") { runCreated = true; return json(route, { ...run(), state: "queued", version: 1, attempt: 0 }, 202); }
    if (path.endsWith("/runs")) return json(route, runCreated ? [run()] : []);
    if (path.endsWith("/runs/run1/events")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: `data: ${JSON.stringify({ event_id: 1, project_id: "p1", run_id: "run1", event_type: "RunQueued", severity: "info", payload: {}, created_at: now })}\n\n` });
    if (path.endsWith("/runs/run1/artifacts")) return json(route, [artifact, secondArtifact, mask]);
    if (path.endsWith("/runs/run1")) return json(route, run());
    if (path.endsWith("/qc-reviews") && request.method() === "POST") return json(route, review(), 201);
    if (path.endsWith("/qc-reviews/review1/approve")) { reviewApproved = true; return json(route, review()); }
    if (path.endsWith("/qc-reviews/review1")) return json(route, review());
    if (path.endsWith("/statistical-designs") && request.method() === "POST") return json(route, designView(), 201);
    if (path.endsWith("/statistical-designs/stat-plan/validate")) { statisticalState = "awaiting_approval"; statisticalVersion = 2; return json(route, designView()); }
    if (path.endsWith("/plan-revisions/stat-plan/approve")) { statisticalState = "approved"; statisticalVersion = 3; return json(route, { approval_id: "approval1", plan_revision_id: "stat-plan", plan_hash: "9".repeat(64), actor: "statistics-reviewer", decision: "approved", reason: "reviewed design", created_at: now }, 201); }
    if (path.endsWith("/plan-revisions/stat-plan")) return json(route, plan());
    if (path.endsWith("/statistics/runs")) return json(route, { run_id: "stat-run", project_id: "p1", plan_revision_id: "stat-plan", state: "queued", version: 1, attempt: 0, cancel_requested: false, error: null, created_at: now, updated_at: now }, 202);
    return json(route, { error: { code: "not_found", message: `unhandled ${path}`, details: {}, trace_id: null } }, 404);
  });

  await page.goto("/runs");
  await page.getByRole("button", { name: "创建已审批计划的 Mock 运行" }).click();
  await expect(page.getByRole("status")).toContainText("SQLite 队列");
  await expect(page.getByText("qc_review", { exact: true }).first()).toBeVisible();

  await page.getByRole("link", { name: "质量控制" }).click();
  await page.getByLabel(/选择指标 Artifact image1/).check();
  await page.getByLabel(/选择指标 Artifact image2/).check();
  await page.getByLabel("QC check code").fill("visual.metric_review");
  await page.getByLabel("QC check severity").selectOption("info");
  await page.getByLabel("QC check passed").selectOption("yes");
  await page.getByLabel("QC check message").fill("reviewed");
  await page.getByLabel(/QC check 证据 image1/).check();
  await page.getByLabel(/QC check 证据 image2/).check();
  await page.getByLabel(/纳入受试者/).fill("sub-1\nsub-2");
  await page.getByLabel("是否排除受试者").selectOption("none");
  await expect(page.getByRole("button", { name: "创建 QC revision" })).toBeEnabled();
  await page.getByRole("button", { name: "创建 QC revision" }).click();
  await page.getByLabel("QC 审批人").fill("qc-reviewer");
  await page.getByLabel("QC 审批理由").fill("reviewed evidence");
  await page.getByRole("button", { name: "人工批准并冻结" }).click();
  await expect(page.getByRole("status")).toContainText("人工 QC 已批准");

  await page.getByRole("link", { name: "统计" }).click();
  await page.getByLabel("检验类型").selectOption("one_sample_t");
  await page.getByLabel("尾部").selectOption("two_sided");
  await page.getByLabel("单样本基线").fill("0");
  await page.getByLabel("缺失值策略").selectOption("error");
  await page.getByLabel(/影像映射/).fill("sub-1 | image1\nsub-2 | image2");
  await page.getByLabel("脑掩膜 Artifact").selectOption("mask1");
  await page.getByLabel("多重比较").selectOption("fdr");
  await page.getByLabel("q 阈值").fill("0.05");
  await page.getByRole("button", { name: "生成设计矩阵" }).click();
  await page.getByRole("button", { name: "验证设计" }).click();
  await page.getByLabel("统计设计审批人").fill("statistics-reviewer");
  await page.getByLabel("统计设计审批理由").fill("reviewed design");
  await page.getByRole("button", { name: "批准统计设计" }).click();
  await page.getByRole("button", { name: "提交统计运行" }).click();
  await expect(page.getByRole("status")).toContainText("统计任务已进入本机队列");
});

test("shows a server validation failure without claiming a successful scan", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/health")) return json(route, { status: "ok", database: "ok" });
    if (path.endsWith("/projects") && request.method() === "POST") return json(route, { project_id: "p1", name: "研究", source_roots: ["D:\\data"], work_root: "D:\\work", version: 1, created_at: now }, 201);
    if (path.endsWith("/projects/p1/datasets")) return json(route, { dataset_id: "d1", project_id: "p1", name: "主数据集", source_path: "D:\\data", version: 1, created_at: now }, 201);
    if (path.endsWith("/datasets/d1/inspect")) return json(route, { error: { code: "source_boundary_violation", message: "源目录超出允许范围", details: {}, trace_id: "trace-e2e" } }, 422);
    return json(route, []);
  });
  await page.goto("/data");
  await page.getByLabel("只读源目录").fill("D:\\data");
  await page.getByLabel("独立工作目录").fill("D:\\work");
  await page.getByRole("button", { name: "开始只读检查" }).click();
  await expect(page.getByRole("alert")).toContainText("源目录超出允许范围");
  await expect(page.getByText(/检查完成/)).toHaveCount(0);
});
