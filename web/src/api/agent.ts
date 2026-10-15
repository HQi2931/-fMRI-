import { request, post, patch, streamConversationTurn } from "./transport";
import type { components } from "./schema.generated";
import type { AgentTask, CompositeGoalProgress, Conversation, ConversationContext, ConversationMemory, ConversationMode, ConversationTurn, ConversationTurnBody, ModelProfile, RsFmriAnswer } from "./types";

type Schemas = components["schemas"];

export const agentApi = {
  streamConversationTurn,
  patchWorkDraft: (conversationId: string, baseHash: string, draftPatch: Record<string, unknown>, signal?: AbortSignal) =>
    patch<ConversationTurn>(`/conversations/${encodeURIComponent(conversationId)}/work-draft`, { base_hash: baseHash, patch: draftPatch }, signal),
  confirmWorkAction: (conversationId: string, command: string, expectedDraftHash: string | null, signal?: AbortSignal) =>
    post<ConversationTurn>(`/conversations/${encodeURIComponent(conversationId)}/work-confirmation`, { command, expected_draft_hash: expectedDraftHash }, signal),
  prepareWorkReview: (conversationId: string, baseHash: string, signal?: AbortSignal) =>
    post<ConversationTurn>(`/conversations/${encodeURIComponent(conversationId)}/work-review`, { base_hash: baseHash }, signal),
  retryAnalysisOutbox: (conversationId: string, intentTaskId: string, signal?: AbortSignal) =>
    post<Conversation>(`/conversations/${encodeURIComponent(conversationId)}/analysis-outbox/${encodeURIComponent(intentTaskId)}/retry`, {}, signal),
  retryStatisticalResultInterpretation: (conversationId: string, taskId: string, signal?: AbortSignal) =>
    post<Conversation>(`/conversations/${encodeURIComponent(conversationId)}/statistical-result-interpretations/${encodeURIComponent(taskId)}/retry`, {}, signal),
  conversations: (mode?: ConversationMode, signal?: AbortSignal) =>
    request<Conversation[]>(
      `/conversations${mode ? `?mode=${encodeURIComponent(mode)}` : ""}`,
      { signal },
    ),
  createConversation: (
    body: {
      mode: ConversationMode;
      title?: string | null;
      project_id?: string | null;
      workspace_path?: string | null;
      preferred_profile_id?: string | null;
    },
    signal?: AbortSignal,
  ) => post<Conversation>("/conversations", body, signal),
  conversation: (conversationId: string, signal?: AbortSignal) =>
    request<Conversation>(`/conversations/${conversationId}`, { signal }),
  compositeGoals: (conversationId: string, signal?: AbortSignal) =>
    request<CompositeGoalProgress[]>(`/conversations/${encodeURIComponent(conversationId)}/composite-goals`, { signal }),
  selectWorkAgent: (conversationId: string, body: Schemas["WorkAgentSelection"], signal?: AbortSignal) =>
    patch<Conversation>(`/conversations/${conversationId}/work-agent`, body, signal),
  conversationContext: (conversationId: string, signal?: AbortSignal) =>
    request<ConversationContext>(`/conversations/${conversationId}/context`, { signal }),
  indexConversationMemories: (conversationId: string, signal?: AbortSignal) =>
    post<Record<string, string>>(`/conversations/${conversationId}/context/index`, {}, signal),
  createConversationMemory: (
    conversationId: string,
    body: Schemas["MemoryCreate"],
    signal?: AbortSignal,
  ) => post<ConversationMemory>(`/conversations/${conversationId}/context/memories`, body, signal),
  updateConversationMemory: (
    conversationId: string,
    memoryId: string,
    body: Schemas["MemoryUpdate"],
    signal?: AbortSignal,
  ) => patch<ConversationMemory>(
    `/conversations/${conversationId}/context/memories/${memoryId}`,
    body,
    signal,
  ),
  sendConversationTurn: (
    conversationId: string,
    body: ConversationTurnBody,
    signal?: AbortSignal,
  ) => post<ConversationTurn>(`/conversations/${conversationId}/turns`, body, signal),
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
  answerRsFmriQuestion: (body: Schemas["RsFmriQuestionRequest"], signal?: AbortSignal) =>
    post<RsFmriAnswer>("/agent/rsfmri/questions", body, signal),
};
