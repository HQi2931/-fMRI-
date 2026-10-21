import { request, requestSse, post } from "./transport";
import type { components } from "./schema.generated";
import type { Artifact, ClusterLocalization, MlTableInspection, MlTemplate, QcReview, RoiTable, Run, RunDiagnosis } from "./types";

type Schemas = components["schemas"];

export const runsApi = {
  runs: (projectId?: string, signal?: AbortSignal) => {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return request<Run[]>(`/runs${query}`, { signal });
  },
  createRun: (body: Schemas["RunCreate"], signal?: AbortSignal) => post<Run>("/runs", body, signal),
  run: (runId: string, signal?: AbortSignal) => request<Run>(`/runs/${runId}`, { signal }),
  skipQc: (
    runId: string,
    body: Schemas["QcSkipCreate"],
    signal?: AbortSignal,
  ) => post<Schemas["QcSkipView"]>(`/runs/${runId}/qc-skip`, body, signal),
  cancelRun: (runId: string, body: Schemas["RunAction"], signal?: AbortSignal) =>
    post<Run>(`/runs/${runId}/cancel`, body, signal),
  retryRun: (runId: string, body: Schemas["RunAction"], signal?: AbortSignal) =>
    post<Run>(`/runs/${runId}/retry`, body, signal),
  diagnoseRun: (runId: string, body: Schemas["RunDiagnosisRequest"], signal?: AbortSignal) =>
    post<RunDiagnosis>(`/runs/${runId}/diagnosis`, body, signal),
  latestDiagnosis: (runId: string, signal?: AbortSignal, expectedAttempt?: number) => {
    const query = expectedAttempt === undefined ? "" : `?expected_attempt=${expectedAttempt}`;
    return request<RunDiagnosis>(`/runs/${runId}/diagnosis${query}`, { signal });
  },
  artifacts: (runId: string, signal?: AbortSignal) =>
    request<Artifact[]>(`/runs/${runId}/artifacts`, { signal }),
  createHeadMotionQc: (
    runId: string,
    body: Schemas["HeadMotionQcCreate"],
    signal?: AbortSignal,
  ) => post<QcReview>(`/runs/${runId}/head-motion-qc`, body, signal),
  createQcReview: (body: Schemas["QcReviewCreate"], signal?: AbortSignal) =>
    post<QcReview>("/qc-reviews", body, signal),
  qcReview: (reviewId: string, signal?: AbortSignal) =>
    request<QcReview>(`/qc-reviews/${reviewId}`, { signal }),
  runQcReview: (runId: string, signal?: AbortSignal) =>
    request<QcReview | null>(`/runs/${encodeURIComponent(runId)}/qc-review`, { signal }),
  approveQcReview: (
    reviewId: string,
    body: Schemas["QcReviewApprove"],
    signal?: AbortSignal,
  ) => post<QcReview>(`/qc-reviews/${reviewId}/approve`, body, signal),
  runEvents: (runId: string, afterEventId = 0, signal?: AbortSignal) =>
    requestSse(`/runs/${runId}/events?once=true&after_event_id=${afterEventId}`, signal),
  inspectMlTable: (body: Schemas["MlTableInspectRequest"], signal?: AbortSignal) =>
    post<MlTableInspection>("/ml/datasets/inspect", body, signal),
  createMlTemplate: (body: Schemas["MlTemplateCreateRequest"], signal?: AbortSignal) =>
    post<MlTemplate>("/ml/templates", body, signal),
  validateRoiTable: (body: Schemas["RoiTableCreateRequest"], signal?: AbortSignal) =>
    post<RoiTable>("/roi/extractions/validate", body, signal),
  localizeClusters: (body: Schemas["ClusterLocalizationRequest"], signal?: AbortSignal) =>
    post<ClusterLocalization>("/cluster-localizations", body, signal),
};
