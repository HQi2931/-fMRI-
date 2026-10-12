import type { Citation, Conversation, ConversationMode, ModelProfile, WorkspaceProposal } from "../../api/types";
import { readWorkDraft, type WorkDraftSnapshot } from "./workDraft";
import type { AgentHandoff } from "../../shared/types";
import type { WorkspaceState } from "../../workspace";

export type ToolStep = {
  key: string;
  name: string;
  status: string;
  tier: string | null;
  durationMs: number | null;
};
export type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  steps?: ToolStep[];
  workspaceCitations?: Array<{ path: string; locator: string }>;
  proposal?: WorkspaceProposal;
  streamingStatus?: string;
  workDraft?: WorkDraftSnapshot;
  draftIssues?: string[];
  planPreview?: unknown;
  workTimings?: unknown;
  agentRole?: "preprocessing" | "analysis";
  analysisDraft?: { draft: Record<string, unknown>; issues?: string[]; hash?: string; version?: number; review?: string };
  statisticalProposal?: { design: Record<string, unknown>; correction?: Record<string, unknown> | null; project_id?: string; project_version?: number; input_manifest_hash?: string; source_run_id?: string; frozen_subject_order?: string[]; demographics_revision_id?: string | null; demographics_hash?: string | null; review?: string; proposal_hash?: string };
  statisticalDesignIssues?: string[];
  agentHandoff?: AgentHandoff;
  analysisDependency?: { analysis_request?: string; status?: string; source_run_state?: string; task_id?: string };
  statisticalResults?: Array<{ result_id: string; run_id: string; mode: string; non_scientific: boolean; artifact_count: number; cluster_count: number }>;
};
export type WorkAgentRole = "auto" | "preprocessing" | "analysis";
export type ModelChoice = {
  key: string;
  profileId: string;
  model: string;
  label: string;
  capabilities: ModelProfile["profile"]["capabilities"];
};

export const MODEL_SELECTION_KEY = "rsfmri-agent-model-v1";
export function rememberedModel(): string {
  try { return window.sessionStorage.getItem(MODEL_SELECTION_KEY) ?? ""; }
  catch { return ""; }
}

export const WELCOME: Record<ConversationMode, Message> = {
  chat: { id: "chat-welcome", role: "assistant", text: "可以问我 rs-fMRI、DPABI 与统计方法问题。" },
  work: { id: "work-welcome", role: "assistant", text: "告诉我你的目标。我会在对话中准备方案、列出待确认参数，并在明确确认后启动任务。" },
};

export function messagesFrom(conversation?: Conversation): Message[] {
  if (!conversation) return [];
  // A tool-loop turn writes one row per step, keyed to the user message that
  // triggered it, so the steps are re-attached to its reply on every reload.
  const stepsByUser = new Map<string, ToolStep[]>();
  for (const call of conversation.tool_calls ?? []) {
    if (call.step_index === null || call.step_index === undefined) continue;
    const steps = stepsByUser.get(call.user_message_id) ?? [];
    steps.push({
      key: `${call.tool_call_id}`,
      name: call.tool_name,
      status: call.status,
      tier: call.risk_tier ?? null,
      durationMs: call.duration_ms ?? null,
    });
    stepsByUser.set(call.user_message_id, steps);
  }
  let pending: ToolStep[] | undefined;
  return conversation.messages
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => {
      if (item.role === "user") {
        pending = stepsByUser.get(item.message_id);
        return {
          id: item.message_id,
          role: "user" as const,
          text: item.content.startsWith("批准并启动 ") ? "已确认本次方案并启动计算。"
            : item.content.startsWith("确认起始阶段 ") ? "已确认预处理起始阶段。"
            : item.content.startsWith("确认方案参数 ") ? "已审查方案参数。" : item.content,
        };
      }
      const message: Message = {
        id: item.message_id,
        role: "assistant",
        text: item.content,
        citations: (item.payload.chat as { citations?: Citation[] } | undefined)?.citations,
        steps: pending,
        workDraft: readWorkDraft(item.payload.work_draft),
        draftIssues: Array.isArray(item.payload.draft_issues) ? item.payload.draft_issues.filter((value): value is string => typeof value === "string") : readWorkDraft(item.payload.work_draft)?.review_issues ?? [],
        planPreview: item.payload.plan_preview,
        workTimings: item.payload.work_timings,
        agentRole: item.payload.agent_role === "analysis" ? "analysis" : "preprocessing",
        analysisDraft: item.payload.analysis_draft && typeof item.payload.analysis_draft === "object"
          ? item.payload.analysis_draft as Message["analysisDraft"] : undefined,
        statisticalProposal: item.payload.statistical_design_proposal && typeof item.payload.statistical_design_proposal === "object"
          ? item.payload.statistical_design_proposal as Message["statisticalProposal"] : undefined,
        statisticalDesignIssues: Array.isArray(item.payload.statistical_design_issues)
          ? item.payload.statistical_design_issues.filter((value): value is string => typeof value === "string") : undefined,
        agentHandoff: item.payload.agent_handoff && typeof item.payload.agent_handoff === "object"
          ? item.payload.agent_handoff as Message["agentHandoff"] : undefined,
        analysisDependency: item.payload.analysis_dependency && typeof item.payload.analysis_dependency === "object"
          ? item.payload.analysis_dependency as Message["analysisDependency"] : undefined,
        statisticalResults: Array.isArray(item.payload.statistical_results)
          ? item.payload.statistical_results.filter((value): value is NonNullable<Message["statisticalResults"]>[number] => Boolean(value && typeof value === "object" && "result_id" in value && "run_id" in value))
          : undefined,
      };
      const proposal = item.payload.workspace_change_proposal as WorkspaceProposal | undefined;
      const workspaceCitations = item.payload.workspace_citations as Array<{ path: string; locator: string }> | undefined;
      if (proposal) message.proposal = proposal;
      if (workspaceCitations) message.workspaceCitations = workspaceCitations;
      pending = undefined;
      return message;
    });
}

export function confirmedInputStage(conversation: Conversation | undefined, path: string): string | undefined {
  if (!conversation || conversation.workspace_path !== path) return undefined;
  for (const message of [...conversation.messages].reverse()) {
    const stage = message.payload.selected_input_stage;
    if (typeof stage === "string") return stage;
  }
  return undefined;
}

export function conversationWorkspacePatch(conversation: Conversation): Partial<WorkspaceState> {
  const patch: Record<string, unknown> = {
    projectId: conversation.project_id ?? undefined,
    runId: conversation.active_run_id ?? undefined,
    workspacePath: conversation.workspace_path ?? undefined,
    planRevisionId: undefined,
    planVersion: undefined,
    planHash: undefined,
    planState: undefined,
  };
  for (const message of conversation.messages) {
    const payload = message.payload as Record<string, unknown>;
    const project = payload.project as Record<string, unknown> | undefined;
    const dataset = payload.dataset as Record<string, unknown> | undefined;
    const manifest = payload.manifest as Record<string, unknown> | undefined;
    const plan = payload.plan_revision as Record<string, unknown> | undefined;
    const run = payload.run as Record<string, unknown> | undefined;
    if (project) { patch.projectId = project.project_id; patch.projectVersion = project.version; }
    if (dataset) { patch.datasetId = dataset.dataset_id; patch.datasetVersion = dataset.version; }
    if (manifest) { patch.manifestId = manifest.manifest_id; patch.manifestHash = manifest.content_hash; }
    if (plan) {
      patch.planRevisionId = plan.plan_revision_id;
      patch.planVersion = plan.version;
      patch.planHash = plan.plan_hash;
      patch.planState = plan.state;
    }
    if (run) { patch.runId = run.run_id; patch.runVersion = run.version; patch.runState = run.state; }
    const draft = payload.work_draft as { compiled?: boolean } | undefined;
    if (draft && !draft.compiled) {
      patch.planRevisionId = undefined;
      patch.planVersion = undefined;
      patch.planHash = undefined;
      patch.planState = undefined;
    }
  }
  return patch as Partial<WorkspaceState>;
}

export function modelChoice(profile: ModelProfile, model: string): ModelChoice {
  let provider = profile.profile.provider;
  try { provider = new URL(profile.profile.base_url).hostname; } catch { /* validated server-side */ }
  return {
    key: `${profile.profile.id}:${model}`,
    profileId: profile.profile.id,
    model,
    label: `${provider} · ${model}`,
    capabilities: profile.profile.capabilities,
  };
}
