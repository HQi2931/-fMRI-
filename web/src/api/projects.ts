import { request, post } from "./transport";
import type { components } from "./schema.generated";
import type { Dataset, DatasetSplit, Demographics, Manifest, OrganizationPreview, Project, RuntimeEvent } from "./types";

type Schemas = components["schemas"];

export const projectsApi = {
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
  organizationPreview: (body: Schemas["OrganizationPreviewRequest"], signal?: AbortSignal) =>
    post<OrganizationPreview>("/organization/previews", body, signal),
};
