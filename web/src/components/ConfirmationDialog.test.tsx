import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { ConfirmationDialog } from "./ConfirmationDialog";

beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute("open"); } });
});
afterEach(cleanup);
const option = { label: "确认并启动计算", description: "按当前方案处理独立副本", command: "opaque-server-command" };
const request = { title: "启动计算", summary: "ALFF · Segment · 头动 QC", options: [option] };

describe("final confirmation", () => {
  it("wraps keyboard focus between the first and last enabled controls", async () => {
    const user = userEvent.setup();
    render(<ConfirmationDialog request={request} busy={false} error="" onConfirm={vi.fn()} onDismiss={vi.fn()} onEdit={vi.fn()} />);
    const first = screen.getByRole("button", { name: "关闭确认" });
    const last = screen.getByRole("button", { name: option.label });
    last.focus();
    await user.tab();
    expect(first).toHaveFocus();
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
  });
  it("directly confirms one server option without an unnecessary radio or displaying its command", async () => {
    const confirm = vi.fn();
    render(<ConfirmationDialog request={request} busy={false} error="" onConfirm={confirm} onDismiss={vi.fn()} onEdit={vi.fn()} />);
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.queryByText(option.command)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: option.label }));
    expect(confirm).toHaveBeenCalledWith(option.command);
  });
  it("requires an explicit choice when QC alternatives exist", async () => {
    const confirm = vi.fn();
    render(<ConfirmationDialog request={{ ...request, options: [option, { label: "跳过头动 QC", description: "仍保留计算记录", command: "other-command" }] }} busy={false} error="" onConfirm={confirm} onDismiss={vi.fn()} onEdit={vi.fn()} />);
    expect(screen.getByRole("button", { name: "确认选择" })).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: /跳过头动 QC/ }));
    await userEvent.click(screen.getByRole("button", { name: "确认选择" }));
    expect(confirm).toHaveBeenCalledWith("other-command");
  });
  it("locks all actions during submission and preserves user choices on dismissal", async () => {
    const dismiss = vi.fn();
    const { rerender } = render(<ConfirmationDialog request={request} busy error="" onConfirm={vi.fn()} onDismiss={dismiss} onEdit={vi.fn()} />);
    expect(screen.getByRole("button", { name: "正在提交…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保留方案" })).toBeDisabled();
    rerender(<ConfirmationDialog request={request} busy={false} error="网络请求失败，方案仍保留" onConfirm={vi.fn()} onDismiss={dismiss} onEdit={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("方案仍保留");
    await userEvent.click(screen.getByRole("button", { name: "保留方案" }));
    expect(dismiss).toHaveBeenCalledOnce();
  });
});
