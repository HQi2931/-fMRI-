import { request, post } from "./transport";
import type { WorkspaceConsent, WorkspaceFileListing, WorkspacePatchApplyView, WorkspacePatchUndoView, WorkspaceProposal } from "./types";

export const workspacesApi = {
  workspaceConsents: (conversationId: string, signal?: AbortSignal) =>
    request<WorkspaceConsent[]>(`/conversations/${encodeURIComponent(conversationId)}/workspace/consents`, { signal }),
  grantWorkspaceConsent: (conversationId: string, body: { profile_id: string; model: string; acknowledge_remote_processing: boolean }, signal?: AbortSignal) =>
    post<WorkspaceConsent>(`/conversations/${encodeURIComponent(conversationId)}/workspace/consents`, body, signal),
  revokeWorkspaceConsent: (conversationId: string, consentId: string, signal?: AbortSignal) =>
    request<WorkspaceConsent>(`/conversations/${encodeURIComponent(conversationId)}/workspace/consents/${encodeURIComponent(consentId)}`, { method: "DELETE", signal }),
  workspaceFiles: (conversationId: string, directory = "", signal?: AbortSignal) =>
    request<WorkspaceFileListing>(`/conversations/${encodeURIComponent(conversationId)}/workspace/files?directory=${encodeURIComponent(directory)}`, { signal }),
  workspaceProposal: (conversationId: string, proposalId: string, signal?: AbortSignal) =>
    request<WorkspaceProposal>(`/conversations/${encodeURIComponent(conversationId)}/workspace/proposals/${encodeURIComponent(proposalId)}`, { signal }),
  applyWorkspaceProposal: (conversationId: string, proposalId: string, proposalHash: string, signal?: AbortSignal) =>
    post<WorkspacePatchApplyView>(`/conversations/${encodeURIComponent(conversationId)}/workspace/proposals/${encodeURIComponent(proposalId)}/apply`, { proposal_hash: proposalHash }, signal),
  undoWorkspaceProposal: (conversationId: string, proposalId: string, signal?: AbortSignal) =>
    post<WorkspacePatchUndoView>(`/conversations/${encodeURIComponent(conversationId)}/workspace/proposals/${encodeURIComponent(proposalId)}/undo`, {}, signal),
};
