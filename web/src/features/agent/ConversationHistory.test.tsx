import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "../../api/types";
import { messagesFrom } from "./conversation-model";
import { ConversationHistory } from "./ConversationHistory";

afterEach(cleanup);

function assistantMessages(payload: Record<string, unknown>) {
  const conversation = {
    conversation_id: "work-1",
    mode: "work",
    messages: [{
      message_id: "assistant-1",
      conversation_id: "work-1",
      sequence: 1,
      role: "assistant",
      content: "已收到请求。",
      payload,
      created_at: "2026-10-26T00:00:00Z",
    }],
    tool_calls: [],
  } as unknown as Conversation;
  return messagesFrom(conversation);
}

function renderHistory(payload: Record<string, unknown>) {
  return render(<ConversationHistory
    messages={assistantMessages(payload)}
    busy={false}
    onApply={vi.fn()}
    onUndo={vi.fn()}
    onRetryAnalysisOutbox={vi.fn()}
    onRetryResultInterpretation={vi.fn()}
    onOpenPlan={vi.fn()}
    onStatisticsRun={vi.fn()}
    agentRole="preprocessing"
    onTaskChange={async () => {}}
  />);
}

describe("ConversationHistory routing visibility", () => {
  it("shows the selected model source and profile from assistant routing metadata", () => {
    renderHistory({
      agent_routing: {
        status: "routed",
        source: "small_model",
        model: "qwen3.8-flash",
        profile_id: "dashscope",
      },
    });

    expect(screen.getByText("本轮分派：小模型分派 · qwen3.8-flash · Profile dashscope")).toBeInTheDocument();
  });

  it("shows an unavailable state without rendering unknown metadata", () => {
    renderHistory({
      agent_routing: {
        status: "unexpected_status",
        source: "private-routing-source",
        failure_reason_code: "sensitive-detail",
      },
    });

    expect(screen.getByText("本轮分派：分派状态不可用")).toBeInTheDocument();
    expect(screen.queryByText(/unexpected_status|private-routing-source|sensitive-detail/)).not.toBeInTheDocument();
  });
});
