import type { components } from "./schema.generated";

type Schemas = components["schemas"];

export type Health = Schemas["HealthView"];
export type EnvironmentProbe = Schemas["EnvironmentProbeView"];
export type Project = Schemas["ProjectView"];
export type Dataset = Schemas["DatasetView"];
export type Manifest = Schemas["ManifestRevisionView"];
export type Demographics = Schemas["DemographicsRevisionView"];
export type DatasetSplit = Schemas["DatasetSplitView"];
export type Skill = Schemas["SkillSpec"];
export type CompiledSkillPlan = Schemas["SkillPlan"];
export type PlanRevision = Schemas["PlanRevisionView"];
export type Run = Schemas["RunView"];
export type Artifact = Schemas["ArtifactView"];
export type RuntimeEvent = Schemas["RuntimeEventView"];
export type QcReview = Schemas["QcReviewView"];
export type ModelProfile = Schemas["ModelProfileView"];
export type AgentTask = Schemas["AgentTaskView"];
export type StatisticalDesign = Schemas["StatisticalDesignView"];
export type StatisticalResult = Schemas["StatisticalResultView"];
export type StatisticalResultDetail = Schemas["StatisticalResultDetailView"];
export type RunDiagnosis = Schemas["RunDiagnosisView"];
export type MlTableInspection = Schemas["MlTableInspectView"];
export type MlTemplate = Schemas["MlTemplateView"];
export type RoiTable = Schemas["RoiTableView"];
export type ClusterLocalization = Schemas["ClusterLocalizationView"];
export type RsFmriAnswer = Schemas["RsFmriAnswerView"];
export type OrganizationPreview = Schemas["OrganizationPreviewView"];
export type SkillPlanResolveBody = Schemas["SkillPlanResolveRequest"];
export type PreprocessingInput = NonNullable<SkillPlanResolveBody["request"]["preprocessing"]>;

export type ProblemDetails = {
  code: string;
  message: string;
  details: Record<string, unknown>;
  trace_id: string | null;
};

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

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { idempotent = false, headers, ...init } = options;
  const requestHeaders = new Headers(headers);
  requestHeaders.set("Accept", "application/json");
  if (init.body !== undefined) requestHeaders.set("Content-Type", "application/json");

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
    const outcomeIsUncertain =
      error.status >= 500 ||
      [
        "idempotency_request_in_progress",
        "idempotency_lease_lost",
        "idempotency_race",
        "idempotency_completion_conflict",
      ].includes(error.code);
    if (mutationFingerprint !== undefined && !outcomeIsUncertain) {
      forgetMutationKey(mutationFingerprint);
    }
    throw error;
  }
  if (mutationFingerprint !== undefined) forgetMutationKey(mutationFingerprint);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function requestSse(path: string, signal?: AbortSignal): Promise<RuntimeEvent[]> {
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

function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: canonicalJson(body),
    idempotent: true,
    signal,
  });
}

export const api = {
  health: (signal?: AbortSignal) => request<Health>("/health", { signal }),
  environment: (signal?: AbortSignal) => request<EnvironmentProbe>("/environment/probe", { signal }),
  projects: (signal?: AbortSignal) => request<Project[]>("/projects", { signal }),
  project: (projectId: string, signal?: AbortSignal) =>
    request<Project>(`/projects/${projectId}`, { signal }),
  createProject: (body: Schemas["ProjectCreate"], signal?: AbortSignal) =>
    post<Project>("/projects", body, signal),
  projectEvents: (projectId: string, afterEventId = 0, signal?: AbortSignal) =>
    request<RuntimeEvent[]>(`/projects/${projectId}/audit-events?after_event_id=${afterEventId}`, { signal }),
  createDataset: (projectId: string, body: Schemas["DatasetCreate"], signal?: AbortSignal) =>
    post<Dataset>(`/projects/${projectId}/datasets`, body, signal),
  dataset: (datasetId: string, signal?: AbortSignal) =>
    request<Dataset>(`/datasets/${datasetId}`, { signal }),
  inspectDataset: (datasetId: string, body: Schemas["ManifestScanRequest"], signal?: AbortSignal) =>
    post<Manifest>(`/datasets/${datasetId}/inspect`, body, signal),
  manifest: (manifestId: string, signal?: AbortSignal) =>
    request<Manifest>(`/manifests/${manifestId}`, { signal }),
  importDemographics: (
    datasetId: string,
    body: Schemas["DemographicsImportRequest"],
    signal?: AbortSignal,
  ) => post<Demographics>(`/datasets/${datasetId}/demographics/import`, body, signal),
  createSplit: (datasetId: string, body: Schemas["DatasetSplitCreate"], signal?: AbortSignal) =>
    post<DatasetSplit>(`/datasets/${datasetId}/splits`, body, signal),
  skills: (signal?: AbortSignal) => request<Skill[]>("/skills", { signal }),
  resolveSkillPlan: (body: SkillPlanResolveBody, signal?: AbortSignal) =>
    post<Schemas["SkillPlanResolveView"]>("/skill-plans/resolve", body, signal),
  plan: (revisionId: string, signal?: AbortSignal) =>
    request<PlanRevision>(`/plan-revisions/${revisionId}`, { signal }),
  approvePlan: (
    revisionId: string,
    body: Schemas["ApprovalCreate"],
    signal?: AbortSignal,
  ) => post<Schemas["ApprovalView"]>(`/plan-revisions/${revisionId}/approve`, body, signal),
  runs: (projectId?: string, signal?: AbortSignal) => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return request<Run[]>(`/runs${query}`, { signal });
  },
  createRun: (body: Schemas["RunCreate"], signal?: AbortSignal) => post<Run>("/runs", body, signal),
  run: (runId: string, signal?: AbortSignal) => request<Run>(`/runs/${runId}`, { signal }),
  cancelRun: (runId: string, body: Schemas["RunAction"], signal?: AbortSignal) =>
    post<Run>(`/runs/${runId}/cancel`, body, signal),
  retryRun: (runId: string, body: Schemas["RunAction"], signal?: AbortSignal) =>
    post<Run>(`/runs/${runId}/retry`, body, signal),
  diagnoseRun: (runId: string, body: Schemas["RunDiagnosisRequest"], signal?: AbortSignal) =>
    post<RunDiagnosis>(`/runs/${runId}/diagnosis`, body, signal),
  artifacts: (runId: string, signal?: AbortSignal) =>
    request<Artifact[]>(`/runs/${runId}/artifacts`, { signal }),
  createQcReview: (body: Schemas["QcReviewCreate"], signal?: AbortSignal) =>
    post<QcReview>("/qc-reviews", body, signal),
  qcReview: (reviewId: string, signal?: AbortSignal) =>
    request<QcReview>(`/qc-reviews/${reviewId}`, { signal }),
  approveQcReview: (
    reviewId: string,
    body: Schemas["QcReviewApprove"],
    signal?: AbortSignal,
  ) => post<QcReview>(`/qc-reviews/${reviewId}/approve`, body, signal),
  runEvents: (runId: string, afterEventId = 0, signal?: AbortSignal) =>
    requestSse(`/runs/${runId}/events?once=true&after_event_id=${afterEventId}`, signal),
  correctionCapabilities: (signal?: AbortSignal) =>
    request<Schemas["CorrectionCapabilityView"][]>("/corrections", { signal }),
  createStatisticalDesign: (body: Schemas["StatisticalDesignCreate"], signal?: AbortSignal) =>
    post<StatisticalDesign>("/statistical-designs", body, signal),
  validateStatisticalDesign: (
    revisionId: string,
    body: Schemas["StatisticalDesignValidationRequest"],
    signal?: AbortSignal,
  ) => post<StatisticalDesign>(`/statistical-designs/${revisionId}/validate`, body, signal),
  statisticalDesign: (revisionId: string, signal?: AbortSignal) =>
    request<StatisticalDesign>(`/statistical-designs/${revisionId}`, { signal }),
  createStatisticsRun: (body: Schemas["StatisticsRunCreate"], signal?: AbortSignal) =>
    post<Run>("/statistics/runs", body, signal),
  statisticalResults: (projectId: string, signal?: AbortSignal) =>
    request<StatisticalResult[]>(`/statistics/results?project_id=${encodeURIComponent(projectId)}`, { signal }),
  statisticalResult: (resultId: string, signal?: AbortSignal) =>
    request<StatisticalResultDetail>(`/statistics/results/${resultId}`, { signal }),
  profiles: (signal?: AbortSignal) => request<ModelProfile[]>("/model-profiles", { signal }),
  createProfile: (body: Schemas["ModelProfileCreate"], signal?: AbortSignal) =>
    post<ModelProfile>("/model-profiles", body, signal),
  deleteProfile: (profileId: string, signal?: AbortSignal) =>
    request<void>(`/model-profiles/${encodeURIComponent(profileId)}`, { method: "DELETE", signal }),
  listProviderModels: (body: Schemas["ModelListRequest"], signal?: AbortSignal) =>
    post<Schemas["ModelListView"]>("/providers/models", body, signal),
  testProvider: (body: Schemas["ProviderTestRequest"], signal?: AbortSignal) =>
    post<Schemas["ProviderTestView"]>("/providers/test", body, signal),
  createAgentTask: (body: Schemas["AgentTaskCreate"], signal?: AbortSignal) =>
    post<AgentTask>("/agent/tasks", body, signal),
  inspectMlTable: (body: Schemas["MlTableInspectRequest"], signal?: AbortSignal) =>
    post<MlTableInspection>("/ml/datasets/inspect", body, signal),
  createMlTemplate: (body: Schemas["MlTemplateCreateRequest"], signal?: AbortSignal) =>
    post<MlTemplate>("/ml/templates", body, signal),
  validateRoiTable: (body: Schemas["RoiTableCreateRequest"], signal?: AbortSignal) =>
    post<RoiTable>("/roi/extractions/validate", body, signal),
  localizeClusters: (body: Schemas["ClusterLocalizationRequest"], signal?: AbortSignal) =>
    post<ClusterLocalization>("/cluster-localizations", body, signal),
  answerRsFmriQuestion: (body: Schemas["RsFmriQuestionRequest"], signal?: AbortSignal) =>
    post<RsFmriAnswer>("/agent/rsfmri/questions", body, signal),
  organizationPreview: (body: Schemas["OrganizationPreviewRequest"], signal?: AbortSignal) =>
    post<OrganizationPreview>("/organization/previews", body, signal),
};

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const trace = error.traceId ? `（追踪号：${error.traceId}）` : "";
    return `${error.message}${trace}`;
  }
  if (isAbortError(error)) return "请求已取消";
  return error instanceof Error ? error.message : "发生未知错误";
}
