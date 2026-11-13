import { request, post } from "./transport";
import type { components } from "./schema.generated";
import type { ExternalAnalysisImport, ExternalAnalysisImportSummary, ExternalDemographicsRevision, Run, StatisticalAtlas, StatisticalClusterRoi, StatisticalDesign, StatisticalResult, StatisticalResultDetail, StatisticalResultLocalization, StatisticalResultSliceInfo } from "./types";

type Schemas = components["schemas"];

export const statisticsApi = {
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
  externalAnalysisImports: (projectId: string, signal?: AbortSignal) =>
    request<ExternalAnalysisImportSummary[]>(
      `/statistics/external-imports?project_id=${encodeURIComponent(projectId)}`,
      { signal },
    ),
  externalAnalysisImport: (importId: string, signal?: AbortSignal) =>
    request<ExternalAnalysisImport>(`/statistics/external-imports/${encodeURIComponent(importId)}`, { signal }),
  externalDemographics: (importId: string, signal?: AbortSignal) =>
    request<ExternalDemographicsRevision | null>(`/statistics/external-imports/${encodeURIComponent(importId)}/demographics`, { signal }),
  statisticalClusterRoi: (resultId: string, clusterId: string, signal?: AbortSignal) =>
    request<StatisticalClusterRoi>(`/statistics/results/${encodeURIComponent(resultId)}/roi?cluster_id=${encodeURIComponent(clusterId)}`, { signal }),
  importExternalDemographics: (importId: string, body: Schemas["ExternalDemographicsImportRequest"], signal?: AbortSignal) =>
    post<ExternalDemographicsRevision>(`/statistics/external-imports/${encodeURIComponent(importId)}/demographics`, body, signal),
  inspectExternalAnalysisImport: (
    body: Schemas["ExternalAnalysisImportInspectRequest"], signal?: AbortSignal,
  ) => post<ExternalAnalysisImport>("/statistics/external-imports/inspect", body, signal),
  includeExternalAnalysisImport: (
    importId: string,
    body: Schemas["ExternalAnalysisImportIncludeRequest"],
    signal?: AbortSignal,
  ) => post<ExternalAnalysisImport>(
    `/statistics/external-imports/${encodeURIComponent(importId)}/include`, body, signal,
  ),
  statisticalResult: (resultId: string, signal?: AbortSignal) =>
    request<StatisticalResultDetail>(`/statistics/results/${resultId}`, { signal }),
  statisticalResultSliceInfo: (resultId: string, signal?: AbortSignal) =>
    request<StatisticalResultSliceInfo>(
      `/statistics/results/${encodeURIComponent(resultId)}/slice-info`,
      { signal },
    ),
  statisticalResultSliceUrl: (resultId: string, sliceIndex: number) =>
    `/api/v1/statistics/results/${encodeURIComponent(resultId)}/slice.png?slice_index=${sliceIndex}`,
  statisticalAtlases: (signal?: AbortSignal) =>
    request<StatisticalAtlas[]>("/statistics/atlases", { signal }),
  localizeStatisticalResult: (resultId: string, atlasId: string, signal?: AbortSignal) =>
    request<StatisticalResultLocalization>(
      `/statistics/results/${encodeURIComponent(resultId)}/localization?atlas_id=${encodeURIComponent(atlasId)}`,
      { signal },
    ),
};
