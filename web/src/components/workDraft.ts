export type WorkDraftSnapshot = { draft: Record<string, unknown>; hash: string; workspace_path?: string; compiled?: boolean; consumed?: boolean; sources?: Record<string, string>; version?: number | string; field_states?: Record<string, string>; review_issues?: string[]; changes?: Array<{ path: string; before: unknown; after: unknown }> };

export function inheritedFieldValue(values: Record<string, string> | undefined, path: string): string | undefined {
  for (let parts = path.split("."); parts.length; parts = parts.slice(0, -1)) {
    const value = values?.[parts.join(".")];
    if (value) return value;
  }
}

export function sourceLabel(source: string | undefined): string {
  return ({ user: "用户指定", suggested: "模型推荐", model: "模型推荐", data: "数据读取", evidence: "数据读取", metadata: "数据读取", system: "系统计算", computed: "系统计算" } as Record<string, string>)[source ?? ""] ?? "来源待核实";
}

export function reviewLabel(state: string | undefined): string {
  return ({ verified: "已核实", missing: "待选择", conflict: "存在冲突", included: "已纳入运行方案" } as Record<string, string>)[state ?? ""] ?? "待审查";
}

export function readWorkDraft(value: unknown): WorkDraftSnapshot | undefined {
  if (!value || typeof value !== "object") return;
  const item = value as Partial<WorkDraftSnapshot>;
  if (typeof item.hash !== "string" || !item.draft || typeof item.draft !== "object" || Array.isArray(item.draft)) return;
  return item as WorkDraftSnapshot;
}
