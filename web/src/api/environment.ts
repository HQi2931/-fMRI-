import { request, post } from "./transport";
import type { components } from "./schema.generated";
import type { EnvironmentConfig, EnvironmentProbe, Health, WorkspaceCheck, WorkspacePick } from "./types";

type Schemas = components["schemas"];

export const environmentApi = {
  health: (signal?: AbortSignal) => request<Health>("/health", { signal }),
  environment: (signal?: AbortSignal) => request<EnvironmentProbe>("/environment/probe", { signal }),
  environmentConfig: (signal?: AbortSignal) => request<EnvironmentConfig>("/environment/config", { signal }),
  updateEnvironmentConfig: (
    body: Schemas["EnvironmentConfigUpdate"],
    signal?: AbortSignal,
  ) => request<EnvironmentConfig>("/environment/config", { method: "PUT", body: JSON.stringify(body), signal }),
  checkWorkspace: (body: { path: string; selected_input_stage?: string | null }, signal?: AbortSignal) =>
    post<WorkspaceCheck>("/workspaces/check", body, signal),
  pickWorkspace: (signal?: AbortSignal) =>
    post<WorkspacePick>("/workspaces/pick", {}, signal),
};
