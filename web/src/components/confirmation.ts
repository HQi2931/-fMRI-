type ConfirmationOption = { label: string; description: string; command: string };
export type ConfirmationRequest = { title: string; summary: string; options: ConfirmationOption[] };

// Only structured server payloads may create actions; never parse model prose.
export function readConfirmation(value: unknown): ConfirmationRequest | null {
  if (!value || typeof value !== "object") return null;
  const request = value as Partial<ConfirmationRequest>;
  if (typeof request.title !== "string" || typeof request.summary !== "string"
    || !Array.isArray(request.options) || !request.options.length || request.options.length > 3) return null;
  if (!request.options.every((option) => option && typeof option.label === "string"
    && typeof option.description === "string" && typeof option.command === "string" && option.command.trim())) return null;
  return request as ConfirmationRequest;
}

type ConfirmationMessage = { message_id: string; role: string; payload: Record<string, unknown> };
export function currentConfirmation(messages: ConfirmationMessage[], workspacePath: string | null | undefined) {
  const draftMessages = [...messages].reverse().filter((message) => message.payload.work_draft && typeof message.payload.work_draft === "object");
  const draft = draftMessages[0]?.payload.work_draft as { hash?: unknown; consumed?: unknown; compiled?: unknown; workspace_path?: unknown } | undefined;
  if (draft?.consumed === true) return null;
  if (typeof draft?.workspace_path === "string" && draft.workspace_path !== workspacePath) return null;
  for (const [index, message] of [...messages].reverse().entries()) {
    if (message.role !== "assistant") continue;
    const request = readConfirmation(message.payload.confirmation_request);
    if (!request) continue;
    const offered = message.payload.work_draft as { hash?: unknown; compiled?: unknown } | undefined;
    if (index > 0 && (!draft || !offered || offered.hash !== draft.hash)) continue;
    if (offered && draft && offered.hash !== draft.hash) continue;
    if (offered?.compiled === true && draft?.compiled !== true) continue;
    return { request, messageId: message.message_id };
  }
  return null;
}
