import { request, post } from "./transport";
import type { components } from "./schema.generated";
import type { PlanRevision, Skill, SkillPlanResolveBody } from "./types";

type Schemas = components["schemas"];

export const plansApi = {
  skills: (signal?: AbortSignal) => request<Skill[]>("/skills", { signal }),
  dryRunSkillPlan: (body: SkillPlanResolveBody, signal?: AbortSignal) =>
    post<Schemas["SkillPlanDryRunView"]>("/skill-plans/dry-run", body, signal),
  resolveSkillPlan: (body: SkillPlanResolveBody, signal?: AbortSignal) =>
    post<Schemas["SkillPlanResolveView"]>("/skill-plans/resolve", body, signal),
  plan: (revisionId: string, signal?: AbortSignal) =>
    request<PlanRevision>(`/plan-revisions/${revisionId}`, { signal }),
  approvePlan: (
    revisionId: string,
    body: Schemas["ApprovalCreate"],
    signal?: AbortSignal,
  ) => post<Schemas["ApprovalView"]>(`/plan-revisions/${revisionId}/approve`, body, signal),
};
