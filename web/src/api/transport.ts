import type { ConversationTurn, ConversationTurnBody, ProblemDetails, RuntimeEvent } from "./types";

type RequestOptions = RequestInit & { idempotent?: boolean };

const pendingMutationKeys = new Map<string, string>();
const PENDING_MUTATIONS_STORAGE_SLOT = "rsfmri-pending-mutations-v1";
let pendingMutationKeysLoaded = false;

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, nestedValue: unknown) => {
    if (nestedValue === null || Array.isArray(nestedValue) || typeof nestedValue !== "object") {
      return nestedValue;
    }
    return Object.fromEntries(
      Object.entries(nestedValue as Record<string, unknown>).sort(([left], [right]) =>
        left < right ? -1 : left > right ? 1 : 0,
      ),
    );
  }) ?? "null";
}

function mutationStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function loadPendingMutationKeys(): void {
  if (pendingMutationKeysLoaded) return;
  pendingMutationKeysLoaded = true;
  try {
    const raw = mutationStorage()?.getItem(PENDING_MUTATIONS_STORAGE_SLOT);
    if (!raw) return;
    const stored = JSON.parse(raw) as Record<string, unknown>;
    for (const [fingerprint, key] of Object.entries(stored)) {
      if (/^[a-f0-9]{64}$/.test(fingerprint) && typeof key === "string" && /^[a-f0-9-]{36}$/i.test(key)) {
        pendingMutationKeys.set(fingerprint, key);
      }
    }
  } catch {
    mutationStorage()?.removeItem(PENDING_MUTATIONS_STORAGE_SLOT);
  }
}

function persistPendingMutationKeys(): void {
  const target = mutationStorage();
  if (!target) return;
  if (pendingMutationKeys.size === 0) {
    target.removeItem(PENDING_MUTATIONS_STORAGE_SLOT);
    return;
  }
  target.setItem(PENDING_MUTATIONS_STORAGE_SLOT, JSON.stringify(Object.fromEntries(pendingMutationKeys)));
}

async function mutationFingerprintForRequest(method: string, path: string, body: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(`${method}\n${path}\n${String(body ?? "")}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function rememberMutationKey(fingerprint: string): string {
  loadPendingMutationKeys();
  const key = pendingMutationKeys.get(fingerprint) ?? newIdempotencyKey();
  pendingMutationKeys.set(fingerprint, key);
  persistPendingMutationKeys();
  return key;
}

function forgetMutationKey(fingerprint: string): void {
  pendingMutationKeys.delete(fingerprint);
  persistPendingMutationKeys();
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

export class ApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly traceId: string | null;

  constructor(message: string, readonly status: number, problem?: Partial<ProblemDetails>) {
    super(message);
    this.name = "ApiError";
    this.code = problem?.code ?? "http_error";
    this.details = problem?.details ?? {};
    this.traceId = problem?.trace_id ?? null;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  const fallback = `请求失败（${response.status}）`;
  try {
    const payload = (await response.json()) as { error?: Partial<ProblemDetails>; detail?: unknown };
    if (payload.error && typeof payload.error.message === "string") {
      return new ApiError(payload.error.message, response.status, payload.error);
    }
    if (typeof payload.detail === "string") {
      return new ApiError(payload.detail, response.status);
    }
  } catch {
    // The server may return an empty body for infrastructure-level failures.
  }
  return new ApiError(fallback, response.status);
}

function outcomeIsUncertain(error: ApiError): boolean {
  return error.status >= 500 || [
    "idempotency_request_in_progress", "idempotency_lease_lost", "idempotency_race",
    "idempotency_completion_conflict",
  ].includes(error.code);
}

export type ConversationStreamEvent =
  | { type: "reset" }
  | { type: "delta"; text: string }
  | { type: "status"; message: string }
  | { type: "final"; turn: ConversationTurn };

export async function streamConversationTurn(
  conversationId: string,
  body: ConversationTurnBody,
  onEvent: (event: ConversationStreamEvent) => void,
  signal?: AbortSignal,
): Promise<ConversationTurn> {
  const path = `/conversations/${conversationId}/turns`;
  const serialized = canonicalJson({ ...body, stream: true });
  const fingerprint = await mutationFingerprintForRequest("POST", path, serialized);
  const key = rememberMutationKey(fingerprint);
  const response = await fetch(`/api/v1${path}`, {
    method: "POST", body: serialized, signal,
    headers: { Accept: "text/event-stream", "Content-Type": "application/json", "Idempotency-Key": key },
  });
  if (!response.ok) {
    const error = await parseError(response);
    if (!outcomeIsUncertain(error)) forgetMutationKey(fingerprint);
    throw error;
  }
  if (!response.headers.get("Content-Type")?.includes("text/event-stream")) {
    const turn = await response.json() as ConversationTurn;
    forgetMutationKey(fingerprint);
    onEvent({ type: "final", turn });
    return turn;
  }
  if (!response.body) throw new Error("连接中断，尚未收到完整回答。请重试同一请求。");
  const reader = response.body.getReader();
  const abortReading = () => { void reader.cancel().catch(() => undefined); };
  signal?.addEventListener("abort", abortReading, { once: true });
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "message";
  let data: string[] = [];
  let final: ConversationTurn | undefined;
  function dispatch(): void {
    if (!data.length) { eventName = "message"; return; }
    const payload = JSON.parse(data.join("\n"));
    data = [];
    const type = eventName;
    eventName = "message";
    if (type === "error") {
      const error = new ApiError(payload.error.message, payload.status, payload.error);
      if (!outcomeIsUncertain(error)) forgetMutationKey(fingerprint);
      throw error;
    }
    if (type === "reset") onEvent({ type: "reset" });
    if (type === "delta" && typeof payload.text === "string") onEvent({ type: "delta", text: payload.text });
    if (type === "status" && typeof payload.message === "string") onEvent({ type: "status", message: payload.message });
    if (type === "final") {
      final = payload as ConversationTurn;
      forgetMutationKey(fingerprint);
      onEvent({ type: "final", turn: final });
    }
  }
  function consume(): void {
    let newline: number;
    while (!final && (newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (!line) dispatch();
      else if (!line.startsWith(":")) {
        const separator = line.indexOf(":");
        const field = separator < 0 ? line : line.slice(0, separator);
        const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
        if (field === "event") eventName = value;
        if (field === "data") data.push(value);
      }
    }
  }
  try {
    while (!final) {
      signal?.throwIfAborted();
      const { value, done } = await reader.read();
      signal?.throwIfAborted();
      buffer += decoder.decode(value, { stream: !done });
      consume();
      if (done) break;
    }
    if (!final) throw new Error("连接中断，尚未收到完整回答。请重试同一请求。");
    return final;
  } finally {
    // Releasing the browser stream does not cancel a dispatched server turn.
    signal?.removeEventListener("abort", abortReading);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { idempotent = false, headers, ...init } = options;
  const requestHeaders = new Headers(headers);
  requestHeaders.set("Accept", "application/json");
  if (init.body !== undefined && !(init.body instanceof FormData)) requestHeaders.set("Content-Type", "application/json");

  let mutationFingerprint: string | undefined;
  if (idempotent && !requestHeaders.has("Idempotency-Key")) {
    mutationFingerprint = await mutationFingerprintForRequest(init.method ?? "GET", path, init.body);
    const key = rememberMutationKey(mutationFingerprint);
    requestHeaders.set("Idempotency-Key", key);
  }

  // A transport error or AbortError cannot prove that the server did not commit.
  // Awaiting fetch directly leaves the persisted key in place for an explicit retry.
  const response = await fetch(`/api/v1${path}`, { ...init, headers: requestHeaders });

  if (!response.ok) {
    const error = await parseError(response);
    if (mutationFingerprint !== undefined && !outcomeIsUncertain(error)) {
      forgetMutationKey(mutationFingerprint);
    }
    throw error;
  }
  // Receiving headers does not prove the response body arrived intact.
  // Keep the key until decoding succeeds so a lost body can be recovered.
  const result = response.status === 204 ? undefined as T : (await response.json()) as T;
  if (mutationFingerprint !== undefined) forgetMutationKey(mutationFingerprint);
  return result;
}

export async function requestSse(path: string, signal?: AbortSignal): Promise<RuntimeEvent[]> {
  const response = await fetch(`/api/v1${path}`, {
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!response.ok) throw await parseError(response);
  const body = await response.text();
  return body
    .split(/\r?\n\r?\n/)
    .flatMap((block) => block.split(/\r?\n/).filter((line) => line.startsWith("data: ")))
    .map((line) => JSON.parse(line.slice(6)) as RuntimeEvent);
}

export function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: canonicalJson(body),
    idempotent: true,
    signal,
  });
}

export function patch<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: canonicalJson(body),
    idempotent: true,
    signal,
  });
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    // Keep request IDs available on ApiError for diagnostics, but avoid
    // exposing internal identifiers in ordinary user-facing messages.
    return error.message;
  }
  if (isAbortError(error)) return "请求已取消";
  return error instanceof Error ? error.message : "发生未知错误";
}
