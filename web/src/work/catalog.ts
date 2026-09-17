import type { Conversation, WorkCard, WorkCardKind } from "../api/client";

export const capabilities: { kind: WorkCardKind; label: string; prompt: string }[] = [
  { kind: "project", label: "项目与工作区", prompt: "选择或建立项目" },
  { kind: "data", label: "数据与清单", prompt: "登记数据并管理清单" },
  { kind: "plan", label: "预处理与指标", prompt: "准备预处理和指标方案" },
  { kind: "runs", label: "运行与产物", prompt: "查看运行、日志和产物" },
  { kind: "qc", label: "质量控制", prompt: "准备质量控制审核" },
  { kind: "statistics", label: "统计与结果", prompt: "准备统计分析并查看结果" },
  { kind: "analysis", label: "扩展分析", prompt: "打开扩展分析工具" },
];

export function cardsFrom(conversation?: Conversation): WorkCard[] {
  const latest = new Map<string, WorkCard>();
  for (const message of conversation?.messages ?? []) {
    const cards = message.payload.work_cards;
    if (Array.isArray(cards)) for (const card of cards) {
      if (card && typeof card === "object" && typeof card.card_id === "string") latest.set(card.card_id, card as WorkCard);
    }
    const card = message.payload.work_card;
    if (card && typeof card === "object" && "card_id" in card) latest.set(String(card.card_id), card as WorkCard);
  }
  return [...latest.values()];
}
