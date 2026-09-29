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
