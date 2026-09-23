import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EmptyState, Feedback, MetricCard, PageHeader, ProgressBar, SafetyNotice } from "./Ui";

describe("shared ui primitives", () => {
  afterEach(cleanup);

  it("renders a page header including its optional action", () => {
    render(
      <PageHeader
        eyebrow="本机环境"
        title="设置"
        description="绑定模型服务商"
        action={<button type="button">重新探测</button>}
      />,
    );

    expect(screen.getByRole("heading", { name: "设置" })).toBeInTheDocument();
    expect(screen.getByText("本机环境")).toBeInTheDocument();
    expect(screen.getByText("绑定模型服务商")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新探测" })).toBeInTheDocument();
  });

  it("renders a metric card with its tone accent", () => {
    const { container } = render(
      <MetricCard label="活动运行" value="3" detail="1 个等待 QC" tone="warn" />,
    );

    expect(screen.getByText("活动运行")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1 个等待 QC")).toBeInTheDocument();
    expect(container.querySelector(".accent-warn")).not.toBeNull();
  });

  it("clamps the progress bar into the zero to one hundred range", () => {
    const { rerender } = render(<ProgressBar value={140} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");

    rerender(<ProgressBar value={-20} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-label", "完成 -20%");
  });

  it("hides empty feedback and marks errors as alerts", () => {
    const { rerender } = render(<Feedback message="" />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    rerender(<Feedback message="MATLAB 可执行文件不存在" error />);
    expect(screen.getByRole("alert")).toHaveTextContent("MATLAB 可执行文件不存在");

    rerender(<Feedback message="已保存" />);
    expect(screen.getByRole("status")).toHaveTextContent("已保存");
  });

  it("renders an empty state and the safety notice", () => {
    render(
      <>
        <EmptyState title="尚未绑定服务商 API" detail="绑定后模型会出现在对话框中。" />
        <SafetyNotice />
      </>,
    );

    expect(screen.getByText("尚未绑定服务商 API")).toBeInTheDocument();
    expect(screen.getByText("绑定后模型会出现在对话框中。")).toBeInTheDocument();
    expect(screen.getByText("安全边界")).toBeInTheDocument();
    expect(screen.getByText(/不会修改源文件/)).toBeInTheDocument();
  });
});
