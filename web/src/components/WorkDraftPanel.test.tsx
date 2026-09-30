import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { WorkDraftPanel } from "./WorkDraftPanel";
import { readWorkDraft, type WorkDraftSnapshot } from "./workDraft";

afterEach(cleanup);
const snapshot = (draft: Record<string, unknown> = {}): WorkDraftSnapshot => ({ hash: "a".repeat(64), draft: { preprocessing: {}, requested_metrics: [], ...draft }, workspace_path: "D:/synthetic" });
const confirmation = { title: "确认方案参数", summary: "仅编译", options: [{ label: "确认", description: "审查参数", command: "确认方案参数 aaaaaaaaaaaa" }] };

describe("editable DPARSF draft", () => {
  it("keeps unknown boolean distinct from explicitly disabled", () => {
    render(<WorkDraftPanel snapshot={snapshot()} onSave={vi.fn()} />);
    expect(screen.getByLabelText("Slice Timing 层时间校正")).toHaveValue("__unknown");
    fireEvent.change(screen.getByLabelText("Slice Timing 层时间校正"), { target: { value: "false" } });
    expect(screen.getByLabelText("Slice Timing 层时间校正")).toHaveValue("false");
    expect(screen.getByLabelText("Slice Number 层数")).toBeDisabled();
    expect(screen.getByText("用户修改（未保存）")).toBeInTheDocument();
  });
  it("saves the base hash and explicit false/null choices without server evidence", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ confirmed_hash: "old" })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("Slice Timing 层时间校正"), { target: { value: "false" } });
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("a".repeat(64), expect.objectContaining({ preprocessing: { slice_timing: { enabled: false, slice_count: null, slice_order: null, reference_slice: null } } })));
    expect(save.mock.calls[0][1]).not.toHaveProperty("confirmed_hash");
  });
  it("preserves the sources of untouched model suggestions by sending only changed fields", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { tr_seconds: 2, dummy_scans: 10 } })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("TR (s)"), { target: { value: "3" } });
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save).toHaveBeenCalledWith("a".repeat(64), { preprocessing: { tr_seconds: 3 } });
  });
  it("retains local edits on a version conflict", async () => {
    render(<WorkDraftPanel snapshot={snapshot()} onSave={vi.fn().mockRejectedValue(new ApiError("草稿已变化", 409, { code: "conflict" }))} />);
    fireEvent.change(screen.getByLabelText("TR (s)"), { target: { value: "2" } });
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("本地修改仍保留");
    expect(screen.getByLabelText("TR (s)")).toHaveValue(2);
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
  });
  it("prevents confirmation for dirty or invalid arrays and permits discarding", () => {
    const dirty = vi.fn();
    render(<WorkDraftPanel snapshot={snapshot()} confirmation={confirmation} onSave={vi.fn()} onDirty={dirty} />);
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Slice Order 采集顺序"), { target: { value: "[1," } });
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "放弃本地修改" }));
    expect(screen.getByLabelText("Slice Order 采集顺序")).toHaveValue("");
    expect(dirty).toHaveBeenLastCalledWith(false);
  });
  it("synchronizes metric selection with its parameter section", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot()} onSave={save} />);
    expect(screen.getByLabelText("ReHo TR (s)")).toBeDisabled();
    fireEvent.click(screen.getByLabelText("REHO"));
    expect(screen.getByLabelText("ReHo TR (s)")).toBeEnabled();
    fireEvent.click(screen.getByLabelText("ALFF"));
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save.mock.calls[0][1]).toMatchObject({ requested_metrics: ["reho", "alff"], reho: {}, alff_falff: { requested_metrics: ["alff"] } });
  });
  it("shows complete T1/DARTEL choices and restricts DARTEL smoothing", () => {
    render(<WorkDraftPanel snapshot={snapshot()} onSave={vi.fn()} />);
    expect(screen.getByLabelText("T1 分割")).toContainHTML('value="new_segment"');
    expect(screen.getByLabelText("T1 结构像输入绑定")).toBeInTheDocument();
    expect(screen.getByLabelText("DARTEL 构建模板")).toBeInTheDocument();
    expect(screen.getByLabelText("平滑方法").querySelector('option[value="2"]')).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Normalize 标准化方法"), { target: { value: "3" } });
    expect(screen.getByLabelText("平滑方法").querySelector('option[value="2"]')).not.toBeDisabled();
  });
  it("enables T1 with symbolic input binding and leaves scientific choices unresolved", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { t1: null } })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("T1 处理配置状态"), { target: { value: "on" } });
    expect(screen.getByLabelText("T1 结构像输入绑定")).toHaveValue("workspace:anatomical-source");
    expect(screen.getByLabelText("T1 分割")).toHaveValue("__unknown");
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save).toHaveBeenCalledWith("a".repeat(64), { preprocessing: { t1: { structural_artifact_id: "workspace:anatomical-source" } } });
  });
  it("renders all fields read-only for history or busy streaming", () => {
    const { rerender } = render(<WorkDraftPanel snapshot={snapshot()} readOnly onSave={vi.fn()} />);
    expect(screen.getByLabelText("TR (s)")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存修改" })).not.toBeInTheDocument();
    rerender(<WorkDraftPanel snapshot={snapshot()} busy onSave={vi.fn()} />);
    expect(screen.getByLabelText("T1 分割")).toBeDisabled();
  });
  it("places field validation errors and exposes full advanced data", () => {
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { tr_seconds: 2 }, extra_review_value: "check" })} issues={["preprocessing.tr_seconds：invalid"]} preview={{ common_steps: ["freeze"] }} onSave={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("preprocessing.tr_seconds");
    expect(screen.getByText(/extra_review_value/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeDisabled();
  });
  it("ignores malformed old payloads", () => {
    expect(readWorkDraft(undefined)).toBeUndefined();
    expect(readWorkDraft({ draft: [], hash: "a" })).toBeUndefined();
    expect(readWorkDraft(snapshot())).toEqual(snapshot());
  });
});
