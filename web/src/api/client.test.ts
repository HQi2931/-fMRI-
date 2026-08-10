import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, describeError } from "./client";

describe("API client", () => {
  afterEach(() => vi.restoreAllMocks());

  it("parses a successful health response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), { status: 200 }),
    );
    await expect(api.health()).resolves.toEqual({ status: "ok", version: "0.1.0" });
  });

  it("converts a failed response to ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "missing", message: "不存在", details: { id: "x" }, trace_id: "trace-1" } }), { status: 404 }),
    );
    await expect(api.skills()).rejects.toMatchObject({ status: 404, code: "missing", traceId: "trace-1" });
  });

  it("handles validation-detail and empty infrastructure error responses", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "字段无效" }), { status: 422 }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 503 }));

    await expect(api.skills()).rejects.toMatchObject({ status: 422, message: "字段无效" });
    await expect(api.skills()).rejects.toMatchObject({ status: 503, message: "请求失败（503）" });
  });

  it("sends approval revision and idempotency headers", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "approved" }), { status: 200 }),
    );
    await api.approvePlan("revision-1", {
      expected_version: 3,
      plan_hash: "a".repeat(64),
      actor: "local-user",
      decision: "approved",
      reason: "reviewed",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/plan-revisions/revision-1/approve",
      expect.objectContaining({ method: "POST" }),
    );
    const init = fetchMock.mock.calls[0][1];
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBeTruthy();
    expect(JSON.parse(String(init?.body))).toMatchObject({ expected_version: 3 });
  });

  it("reuses one logical mutation key after an indeterminate network failure", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ project_id: "project-retry" }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ project_id: "project-new" }), { status: 201 }));
    const body = {
      name: "idempotency-retry-case",
      source_roots: ["D:\\synthetic"],
      work_root: "D:\\work",
    };

    await expect(api.createProject(body as never)).rejects.toThrow("Failed to fetch");
    await expect(
      api.createProject({ work_root: body.work_root, source_roots: body.source_roots, name: body.name } as never),
    ).resolves.toMatchObject({ project_id: "project-retry" });
    await expect(api.createProject(body as never)).resolves.toMatchObject({ project_id: "project-new" });

    const keys = fetchMock.mock.calls.map(([, init]) =>
      new Headers(init?.headers).get("Idempotency-Key"),
    );
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[1]);
    expect(new Set(keys.slice(0, 2))).toHaveLength(1);
    expect(fetchMock.mock.calls[0][1]?.body).toBe(fetchMock.mock.calls[1][1]?.body);
  });

  it("clears the mutation key after a definite HTTP error response", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: "conflict", message: "冲突" } }), { status: 409 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ project_id: "project-after-conflict" }), { status: 201 }));
    const body = {
      name: "idempotency-http-error-case",
      source_roots: ["D:\\synthetic"],
      work_root: "D:\\work",
    };

    await expect(api.createProject(body as never)).rejects.toMatchObject({ status: 409 });
    await expect(api.createProject(body as never)).resolves.toMatchObject({ project_id: "project-after-conflict" });

    const firstKey = new Headers(fetchMock.mock.calls[0][1]?.headers).get("Idempotency-Key");
    const secondKey = new Headers(fetchMock.mock.calls[1][1]?.headers).get("Idempotency-Key");
    expect(firstKey).toBeTruthy();
    expect(secondKey).toBeTruthy();
    expect(secondKey).not.toBe(firstKey);
  });

  it("treats AbortError as an ambiguous delivery and reuses its mutation key", async () => {
    const cancellation = new DOMException("cancelled", "AbortError");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(cancellation)
      .mockResolvedValueOnce(new Response(JSON.stringify({ project_id: "project-after-cancel" }), { status: 201 }));
    const body = {
      name: "idempotency-cancel-case",
      source_roots: ["D:\\synthetic"],
      work_root: "D:\\work",
    };

    await expect(api.createProject(body as never)).rejects.toBe(cancellation);
    expect(describeError(cancellation)).toBe("请求已取消");
    await expect(api.createProject(body as never)).resolves.toMatchObject({ project_id: "project-after-cancel" });

    const firstKey = new Headers(fetchMock.mock.calls[0][1]?.headers).get("Idempotency-Key");
    const secondKey = new Headers(fetchMock.mock.calls[1][1]?.headers).get("Idempotency-Key");
    expect(firstKey).toBeTruthy();
    expect(secondKey).toBeTruthy();
    expect(secondKey).toBe(firstKey);
  });

  it("retains the mutation key while the original request is still in progress", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: "idempotency_request_in_progress", message: "仍在处理" } }), { status: 409 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ project_id: "project-recovered" }), { status: 201 }));
    const body = {
      name: "idempotency-in-progress-case",
      source_roots: ["D:\\synthetic"],
      work_root: "D:\\work",
    };

    await expect(api.createProject(body as never)).rejects.toMatchObject({ status: 409, code: "idempotency_request_in_progress" });
    const persisted = sessionStorage.getItem("rsfmri-pending-mutations-v1") ?? "";
    expect(persisted).not.toContain("idempotency-in-progress-case");
    await expect(api.createProject(body as never)).resolves.toMatchObject({ project_id: "project-recovered" });

    const keys = fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
    expect(sessionStorage.getItem("rsfmri-pending-mutations-v1")).toBeNull();
  });

  it("retains the mutation key when idempotency ownership is lost", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error: { code: "idempotency_lease_lost", message: "ownership changed" } }),
          { status: 409 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ project_id: "project-after-owner-change" }), { status: 201 }),
      );
    const body = {
      name: "idempotency-owner-change-case",
      source_roots: ["D:\\synthetic"],
      work_root: "D:\\work",
    };

    await expect(api.createProject(body as never)).rejects.toMatchObject({
      status: 409,
      code: "idempotency_lease_lost",
    });
    await expect(api.createProject(body as never)).resolves.toMatchObject({
      project_id: "project-after-owner-change",
    });

    const keys = fetchMock.mock.calls.map(([, init]) =>
      new Headers(init?.headers).get("Idempotency-Key"),
    );
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("retains the mutation key after an indeterminate server failure", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error: { code: "internal_server_error", message: "暂时无法确认结果" } }),
          { status: 500 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ project_id: "project-replayed" }), { status: 201 }),
      );
    const body = {
      name: "idempotency-server-failure-case",
      source_roots: ["D:\\synthetic"],
      work_root: "D:\\work",
    };

    await expect(api.createProject(body as never)).rejects.toMatchObject({ status: 500 });
    await expect(api.createProject(body as never)).resolves.toMatchObject({
      project_id: "project-replayed",
    });

    const keys = fetchMock.mock.calls.map(([, init]) =>
      new Headers(init?.headers).get("Idempotency-Key"),
    );
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("covers every typed endpoint wrapper and parses one-shot SSE", async () => {
    const event = { event_id: 1, trace_id: "trace", project_id: "p", run_id: "r", event_type: "RunQueued", severity: "info", payload: {}, created_at: "2026-08-06T00:00:00Z" };
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/events?once=true")) return Promise.resolve(new Response(`id: 1\nevent: RunQueued\ndata: ${JSON.stringify(event)}\n\n`, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
      return Promise.resolve(new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } }));
    });
    await api.environment();
    await api.projects();
    await api.project("p");
    await api.createProject({} as never);
    await api.projectEvents("p", 2);
    await api.createDataset("p", {} as never);
    await api.dataset("d");
    await api.inspectDataset("d", {} as never);
    await api.manifest("m");
    await api.importDemographics("d", {} as never);
    await api.createSplit("d", {} as never);
    await api.resolveSkillPlan({} as never);
    await api.plan("plan");
    await api.runs("p");
    await api.createRun({} as never);
    await api.run("r");
    await api.cancelRun("r", {} as never);
    await api.retryRun("r", {} as never);
    await api.artifacts("r");
    await api.createQcReview({} as never);
    await api.qcReview("q");
    await api.approveQcReview("q", {} as never);
    await expect(api.runEvents("r", 0)).resolves.toEqual([event]);
    await api.correctionCapabilities();
    await api.createStatisticalDesign({} as never);
    await api.validateStatisticalDesign("s", {} as never);
    await api.statisticalDesign("s");
    await api.createStatisticsRun({} as never);
    await api.profiles();
    await api.createProfile({} as never);
    await api.testProvider({} as never);
    await api.createAgentTask({} as never);
    expect(fetch).toHaveBeenCalled();
  });

  it("formats API, cancellation, generic, and unknown errors", () => {
    expect(describeError(new ApiError("bad", 409, { trace_id: "t" }))).toBe("bad（追踪号：t）");
    expect(describeError(new DOMException("cancel", "AbortError"))).toBe("请求已取消");
    expect(describeError(new Error("boom"))).toBe("boom");
    expect(describeError(null)).toBe("发生未知错误");
  });
});
