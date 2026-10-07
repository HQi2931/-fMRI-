import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it } from "vitest";
import { WorkReviewRegion } from "./WorkReviewRegion";

const originalWidth = window.innerWidth;
afterEach(() => {
  cleanup();
  Object.defineProperty(window, "innerWidth", { configurable: true, value: originalWidth });
  document.body.style.overflow = "";
});
function resize(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  fireEvent(window, new Event("resize"));
}
function Harness() {
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState(false);
  return <main>
    <button onClick={() => setOpen(true)}>审查方案</button>
    <WorkReviewRegion open={open} onClose={() => setOpen(false)}>
      <button onClick={() => setOpen(false)}>关闭审查</button>
      <details><summary>参数</summary><input aria-label="折叠参数" /></details>
      <input aria-label="平滑宽度" defaultValue="4" />
      <button disabled>暂不可用</button>
      <button onClick={() => setConfirmation(true)}>最终确认</button>
      {confirmation && <dialog open aria-label="嵌套确认"><button onClick={() => setConfirmation(false)}>保留方案</button></dialog>}
    </WorkReviewRegion>
  </main>;
}

it("moves focus into mobile review, wraps keyboard navigation and restores the trigger", async () => {
  resize(390);
  document.body.style.overflow = "scroll";
  const user = userEvent.setup();
  render(<Harness />);
  const trigger = screen.getByRole("button", { name: "审查方案" });
  const originalInert = trigger.inert;
  await user.click(trigger);
  expect(screen.getByRole("dialog", { name: "当前方案审查" })).toHaveAttribute("aria-modal", "true");
  const close = screen.getByRole("button", { name: "关闭审查" });
  expect(close).toHaveFocus();
  expect(trigger.inert).toBe(true);
  expect(document.body.style.overflow).toBe("hidden");
  await user.keyboard("{Shift>}{Tab}{/Shift}");
  expect(screen.getByRole("button", { name: "最终确认" })).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
  trigger.focus();
  expect(close).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "当前方案审查" })).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(trigger.inert).toBe(originalInert);
  expect(document.body.style.overflow).toBe("scroll");
});

it("keeps edited values while resizing to desktop and closing then reopening", async () => {
  resize(390);
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "审查方案" }));
  const input = screen.getByLabelText("平滑宽度");
  await user.clear(input); await user.type(input, "6");
  resize(1200);
  expect(screen.getByRole("complementary", { name: "当前方案审查" })).toBeInTheDocument();
  expect(document.body.style.overflow).toBe("");
  expect(screen.getByLabelText("平滑宽度")).toHaveValue("6");
  await user.click(screen.getByRole("button", { name: "关闭审查" }));
  resize(390);
  await user.click(screen.getByRole("button", { name: "审查方案" }));
  expect(screen.getByLabelText("平滑宽度")).toHaveValue("6");
});

it("allows the final confirmation dialog to own Escape and focus", async () => {
  resize(390);
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "审查方案" }));
  await user.click(screen.getByRole("button", { name: "最终确认" }));
  const keep = screen.getByRole("button", { name: "保留方案" });
  keep.focus();
  await user.keyboard("{Escape}");
  expect(keep).toHaveFocus();
  expect(screen.getByRole("dialog", { name: "当前方案审查" })).toBeInTheDocument();
  await user.click(keep);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "当前方案审查" })).not.toBeInTheDocument();
});
