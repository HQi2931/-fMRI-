export type WorkDraftSnapshot = { draft: Record<string, unknown>; hash: string; workspace_path?: string; compiled?: boolean; sources?: Record<string, string> };

export function readWorkDraft(value: unknown): WorkDraftSnapshot | undefined {
  if (!value || typeof value !== "object") return;
  const item = value as Partial<WorkDraftSnapshot>;
  if (typeof item.hash !== "string" || !item.draft || typeof item.draft !== "object" || Array.isArray(item.draft)) return;
  return item as WorkDraftSnapshot;
}
