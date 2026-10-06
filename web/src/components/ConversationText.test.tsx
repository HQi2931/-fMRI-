import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { ConversationText } from "./ConversationText";

afterEach(cleanup);

it("renders scientific summaries with semantic lists, emphasis and headings", () => {
  render(<ConversationText text={"已核实 TR 2 秒。\n\n### 需要决定\n- **标准化**：T1 Segment\n- 结果图平滑：4 mm\n\n1. 审查方案\n2. 确认启动"} />);
  expect(screen.getByRole("heading", { name: "需要决定" })).toBeInTheDocument();
  const lists = screen.getAllByRole("list");
  expect(lists[0].tagName).toBe("UL");
  expect(lists[1].tagName).toBe("OL");
  expect(within(lists[0]).getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByText("标准化").tagName).toBe("STRONG");
});

it("keeps untrusted markup as text rather than creating executable HTML", () => {
  const view = render(<ConversationText text={'<img src=x onerror="alert(1)">\n- `0.01–0.08 Hz`'} />);
  expect(view.container.querySelector("img")).toBeNull();
  expect(screen.getByText("0.01–0.08 Hz").tagName).toBe("CODE");
  expect(screen.getByText(/<img/)).toBeInTheDocument();
});
