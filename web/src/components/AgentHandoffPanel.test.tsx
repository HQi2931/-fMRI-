import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AgentHandoffPanel } from "./AgentHandoffPanel";

afterEach(cleanup);

describe("Agent handoff status", () => {
  it.each([
    ["analysis", "received", "数据分析 Agent 已接收"],
    ["preprocessing", "received", "预处理 Agent 已接收"],
    ["analysis", "ready", "数据证据已就绪，可交给数据分析 Agent"],
    ["preprocessing", "pending", "等待预处理 Agent"],
    ["analysis", "stale", "来源证据已变化，交接待更新"],
  ])("shows the recipient and state for %s / %s", (to_agent, status, expected) => {
    render(<AgentHandoffPanel handoff={{ to_agent, status }} />);
    expect(screen.getByText(`Agent 交接 · ${expected}`)).toBeInTheDocument();
  });

  it("explains a blocked request and required raw inputs in plain language", () => {
    render(<AgentHandoffPanel handoff={{ to_agent: "preprocessing", status: "blocked",
      required_outputs: ["zReHo"], blocking_items: ["work_handoff_limit_reached"],
      data_references: { input_selection_required: true } }} />);
    expect(screen.getByText("Agent 交接 · 交接受阻，需要核对")).toBeInTheDocument();
    expect(screen.getByText("所需产物：zReHo")).toBeInTheDocument();
    expect(screen.getByText("补充指标计算前，需先选择并确认预处理输入。")).toBeInTheDocument();
    expect(screen.getByText(/交接次数已达上限/)).toBeInTheDocument();
    expect(screen.queryByText("work_handoff_limit_reached")).not.toBeInTheDocument();
  });
});
