import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, describeError, type ConversationStreamEvent, type ConversationTurn } from "./client";

describe("API client", () => {
  afterEach(() => vi.restoreAllMocks());

  it("parses a successful health response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), { status: 200 }),
    );
    await expect(api.health()).resolves.toEqual({ status: "ok", version: "0.1.0" });
  });
  it("recovers a confirmation with the same key when its successful response body is lost", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("{", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ conversation: { active_run_id: "one-run" } }), { status: 200 }));
    await expect(api.confirmWorkAction("lost-body", "issued command", "a".repeat(64))).rejects.toThrow();
    await expect(api.confirmWorkAction("lost-body", "issued command", "a".repeat(64))).resolves.toMatchObject({ conversation: { active_run_id: "one-run" } });
    const keys = fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });
  it("patches a work draft with revision guard and an idempotency key", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ conversation: {} }), { status: 200 }));
    await api.patchWorkDraft("work / 1", "hash", { preprocessing: { detrend: false } });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/conversations/work%20%2F%201/work-draft");
    const init = fetchMock.mock.calls[0][1];
    expect(init?.method).toBe("PATCH");
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBeTruthy();
    expect(JSON.parse(String(init?.body))).toEqual({ base_hash: "hash", patch: { preprocessing: { detrend: false } } });
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

  it("posts an explicit QC skip decision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ qc_skip_id: "skip-1" }), { status: 201 }),
    );
    await api.skipQc("run-1", {
      expected_run_version: 3,
      actor: "local-user",
      reason: "Proceed without QC.",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs/run-1/qc-skip",
      expect.objectContaining({ method: "POST" }),
    );
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
    await api.createHeadMotionQc("r", {} as never);
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


describe("conversation streaming", () => {
  afterEach(() => vi.restoreAllMocks());
  const turn = { conversation: { conversation_id: "stream-1" }, assistant_message: { content: "最终回答" } } as ConversationTurn;
  const body = { content: "你好" } as never;
  function streamed(content: string, chunkSize = 7): Response {
    const bytes = new TextEncoder().encode(content);
    return new Response(new ReadableStream({ start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize));
      controller.close();
    } }), { status: 201, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
  }
  it("decodes split UTF-8, CRLF, comments, reset, status and authoritative final", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(streamed(
      ': keep-alive\r\n\r\nevent: status\r\ndata: {"message":"正在检索"}\r\n\r\n'
      + 'event: delta\ndata: {"text":"你好"}\n\nevent: reset\ndata: {}\n\n'
      + 'event: delta\ndata: {\n' + 'data: "text":"新回答"}\n\n'
      + `event: final\ndata: ${JSON.stringify(turn)}\n\n`, 1,
    ));
    const events: ConversationStreamEvent[] = [];
    await expect(api.streamConversationTurn("stream-utf8", body, (event) => events.push(event))).resolves.toEqual(turn);
    expect(events).toEqual([
      { type: "status", message: "正在检索" }, { type: "delta", text: "你好" },
      { type: "reset" }, { type: "delta", text: "新回答" }, { type: "final", turn },
    ]);
    expect(new Headers(fetchSpy.mock.calls[0][1]?.headers).get("Accept")).toBe("text/event-stream");
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toMatchObject({ stream: true });
  });
  it("preserves the mutation key after disconnect and clears it after final", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(streamed('event: delta\ndata: {"text":"部分"}\n\n'))
      .mockResolvedValueOnce(streamed(`event: final\ndata: ${JSON.stringify(turn)}\n\n`))
      .mockResolvedValueOnce(streamed(`event: final\ndata: ${JSON.stringify(turn)}\n\n`));
    await expect(api.streamConversationTurn("stream-retry", body, () => undefined)).rejects.toThrow("尚未收到完整回答");
    await api.streamConversationTurn("stream-retry", body, () => undefined);
    await api.streamConversationTurn("stream-retry", body, () => undefined);
    const keys = fetchSpy.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy(); expect(keys[1]).toBe(keys[0]); expect(keys[2]).not.toBe(keys[0]);
  });
  it.each([422, 503])("maps SSE errors and retains uncertain keys for status %s", async (status) => {
    const error = { code: "provider_error", message: "失败", details: { reason: "test" }, trace_id: "stream-trace" };
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(streamed(`event: error\ndata: ${JSON.stringify({ error, status })}\n\n`))
      .mockResolvedValueOnce(streamed(`event: final\ndata: ${JSON.stringify(turn)}\n\n`));
    await expect(api.streamConversationTurn(`stream-error-${status}`, body, () => undefined)).rejects.toMatchObject({ status, code: "provider_error", traceId: "stream-trace" });
    await api.streamConversationTurn(`stream-error-${status}`, body, () => undefined);
    const keys = fetchSpy.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    if (status >= 500) expect(keys[1]).toBe(keys[0]); else expect(keys[1]).not.toBe(keys[0]);
  });
  it("preserves keys after abort and transport failures and accepts legacy JSON", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new DOMException("aborted", "AbortError"))
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(new Response(JSON.stringify(turn), { status: 201, headers: { "Content-Type": "application/json" } }));
    const onEvent = vi.fn();
    await expect(api.streamConversationTurn("stream-abort", body, onEvent)).rejects.toMatchObject({ name: "AbortError" });
    await expect(api.streamConversationTurn("stream-abort", body, onEvent)).rejects.toThrow("network");
    await expect(api.streamConversationTurn("stream-abort", body, onEvent)).resolves.toEqual(turn);
    const keys = fetchSpy.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(new Set(keys).size).toBe(1); expect(onEvent).toHaveBeenCalledWith({ type: "final", turn });
  });
  it("cancels active stream reading on abort while retaining the retry key", async () => {
    const cancelled = vi.fn();
    const abort = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(new ReadableStream({ cancel: cancelled }), { headers: { "Content-Type": "text/event-stream" } }))
      .mockResolvedValueOnce(streamed(`event: final\ndata: ${JSON.stringify(turn)}\n\n`));
    const pending = api.streamConversationTurn("stream-reader-abort", body, () => undefined, abort.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    abort.abort();
    await rejected;
    expect(cancelled).toHaveBeenCalled();
    await api.streamConversationTurn("stream-reader-abort", body, () => undefined);
    const keys = fetchSpy.mock.calls.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]);
  });

});
