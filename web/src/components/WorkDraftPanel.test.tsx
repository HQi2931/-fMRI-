import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { WorkDraftPanel } from "./WorkDraftPanel";
import { readWorkDraft, type WorkDraftSnapshot } from "./workDraft";

afterEach(cleanup);
const snapshot = (draft: Record<string, unknown> = {}): WorkDraftSnapshot => ({ hash: "a".repeat(64), draft: { preprocessing: {}, requested_metrics: [], ...draft }, workspace_path: "D:/synthetic" });
const confirmation = { title: "确认方案参数", summary: "仅编译", options: [{ label: "确认", description: "审查参数", command: "确认方案参数 aaaaaaaaaaaa" }] };

describe("editable DPARSF draft", () => {
  it("does not aggregate retired child conflicts into a validated disabled step", () => {
    const view = render(<WorkDraftPanel snapshot={{ ...snapshot({ preprocessing: { slice_timing: { enabled: false, slice_count: null, slice_order: null, reference_slice: null } } }), field_states: { "preprocessing.slice_timing.slice_order": "conflict" } }} confirmation={confirmation} onSave={vi.fn()} />);
    const row = within(screen.getByRole("region", { name: "关键参数状态" })).getByText("层时间校正").closest("li")!;
    expect(row).toHaveTextContent("明确停用");
    expect(row).toHaveTextContent("状态：待审查");
    expect(row).not.toHaveTextContent("存在冲突");
    view.rerender(<WorkDraftPanel snapshot={{ ...snapshot({ preprocessing: { slice_timing: { enabled: false } } }), field_states: { "preprocessing.slice_timing.slice_order": "conflict" } }} issues={["preprocessing.slice_timing：停用配置仍含有效参数"]} onSave={vi.fn()} />);
    expect(row).toHaveTextContent("状态：存在冲突");
  });
  it.each([
    ["alff_falff", "result_smoothing", "结果图平滑", "结果图 平滑核（毫米）"],
    ["reho", "smooth_reho", "ReHo 结果图平滑", "ReHo 平滑核（毫米）"],
    ["reho", "global_result_smoothing", "ReHo 全局结果平滑", "ReHo 全局 平滑核（毫米）"],
  ])("clears the inactive kernel for %s.%s and requires a new choice on re-enable", async (section, flag, label, kernelLabel) => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ requested_metrics: [section === "reho" ? "reho" : "alff"], [section]: { [flag]: true, [`${flag}_fwhm_mm`]: [4, 4, 4] } })} onSave={save} />);
    fireEvent.change(screen.getByLabelText(kernelLabel), { target: { value: "[4," } });
    fireEvent.change(screen.getByLabelText(label), { target: { value: "false" } });
    expect(screen.getByLabelText(kernelLabel)).toBeDisabled();
    expect(screen.getByLabelText(kernelLabel)).toHaveValue("");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save).toHaveBeenCalledWith("a".repeat(64), { [section]: { [flag]: false, [`${flag}_fwhm_mm`]: null } });
    fireEvent.change(screen.getByLabelText(label), { target: { value: "true" } });
    expect(screen.getByLabelText(kernelLabel)).toBeEnabled();
    expect(screen.getByLabelText(kernelLabel)).toHaveValue("");
  });
  it("clears inactive array errors when slice timing is disabled and leaves re-enabled choices unresolved", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { tr_seconds: 2, slice_timing: { enabled: true, slice_count: 3, slice_order: [1, 2, 3], reference_slice: 2 } } })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("层采集顺序"), { target: { value: "[1," } });
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("层时间校正"), { target: { value: "false" } });
    expect(screen.getByLabelText("层采集顺序")).toBeDisabled();
    expect(screen.getByLabelText("层采集顺序")).toHaveValue("");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save).toHaveBeenCalledWith("a".repeat(64), { preprocessing: { slice_timing: { enabled: false, slice_count: null, slice_order: null, reference_slice: null } } });
    fireEvent.change(screen.getByLabelText("层时间校正"), { target: { value: "true" } });
    expect(screen.getByLabelText("层采集顺序")).toBeEnabled();
    expect(screen.getByLabelText("层采集顺序")).toHaveValue("");
    expect(screen.getByLabelText("层数")).toHaveValue(null);
    expect(screen.getByLabelText("重复时间 TR（秒）")).toHaveValue(2);
  });
  it("drops invalid buffers from removed metrics while retaining errors for active parameters", () => {
    render(<WorkDraftPanel snapshot={snapshot({ requested_metrics: ["alff", "reho"], alff_falff: { result_smoothing_fwhm_mm: [4, 4, 4] }, reho: { smooth_reho_fwhm_mm: [6, 6, 6] } })} onSave={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("结果图 平滑核（毫米）"), { target: { value: "[4," } });
    fireEvent.change(screen.getByLabelText("ReHo 平滑核（毫米）"), { target: { value: "[6," } });
    fireEvent.click(screen.getByLabelText("ALFF"));
    expect(screen.getByLabelText("结果图 平滑核（毫米）")).toBeDisabled();
    expect(screen.getByLabelText("结果图 平滑核（毫米）")).toHaveValue("");
    expect(screen.getByLabelText("ReHo 平滑核（毫米）")).toHaveValue("[6,");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("ReHo 平滑核（毫米）"), { target: { value: "6, 6, 6" } });
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    fireEvent.click(screen.getByLabelText("ALFF"));
    expect(screen.getByLabelText("结果图 平滑核（毫米）")).toBeEnabled();
    expect(screen.getByLabelText("结果图 平滑核（毫米）")).toHaveValue("");
  });
  it("withdraws saved metadata provenance from a locally edited summary and restores it on discard", () => {
    render(<WorkDraftPanel snapshot={{ ...snapshot({ preprocessing: { tr_seconds: 2 } }), sources: { "preprocessing.tr_seconds": "data" }, field_states: { "preprocessing.tr_seconds": "verified" } }} onSave={vi.fn()} />);
    const row = within(screen.getByRole("region", { name: "关键参数状态" })).getByText("TR").closest("li")!;
    fireEvent.change(screen.getByLabelText("重复时间 TR（秒）"), { target: { value: "3" } });
    expect(row).toHaveTextContent("来源：用户指定 · 状态：未保存");
    expect(within(row).getByText("3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放弃本地修改" }));
    expect(row).toHaveTextContent("来源：数据读取 · 状态：已核实");
    expect(within(row).getByText("2")).toBeInTheDocument();
  });
  it("marks a newly supplied required value as unsaved until server validation", () => {
    render(<WorkDraftPanel snapshot={snapshot()} issues={["study_protocol_ref：尚未提供"]} onSave={vi.fn()} />);
    const row = within(screen.getByRole("region", { name: "关键参数状态" })).getByText("研究方案引用").closest("li")!;
    fireEvent.change(screen.getByLabelText("研究方案引用（编译必填）"), { target: { value: "protocol-2026" } });
    expect(row).toHaveTextContent("来源：用户指定 · 状态：未保存");
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeDisabled();
  });
  it("distinguishes a partial group edit from the original recommendations and validation", () => {
    render(<WorkDraftPanel snapshot={{ ...snapshot({ preprocessing: { nuisance: { enabled: true, head_motion_model: 1 } } }), sources: { "preprocessing.nuisance": "suggested" } }} issues={["preprocessing.nuisance.head_motion_model：与研究方案冲突"]} onSave={vi.fn()} />);
    const row = within(screen.getByRole("region", { name: "关键参数状态" })).getByText("协变量回归").closest("li")!;
    expect(row).toHaveTextContent("来源：模型推荐 · 状态：存在冲突");
    fireEvent.change(screen.getByLabelText("头动回归模型"), { target: { value: "4" } });
    expect(row).toHaveTextContent("来源：用户修改（部分参数） · 状态：未保存");
    expect(row).toHaveTextContent("Friston-24");
    fireEvent.click(screen.getByRole("button", { name: "放弃本地修改" }));
    expect(row).toHaveTextContent("来源：模型推荐 · 状态：存在冲突");
  });
  it("preserves requested output names while translating known artifact identifiers", () => {
    render(<WorkDraftPanel snapshot={snapshot()} preview={{ outputs: ["raw_alff", "ALFF 标准化结果图", "头动质量报告"] }} onSave={vi.fn()} />);
    expect(screen.getByText("预期输出：原始 ALFF、ALFF 标准化结果图、头动质量报告")).toBeInTheDocument();
    expect(screen.queryByText(/预期输出：.*所选指标结果/)).not.toBeInTheDocument();
  });
  it("offers input recovery while withdrawn evidence blocks editing and startup", () => {
    const recover = vi.fn();
    render(<WorkDraftPanel snapshot={{ ...snapshot({ preprocessing: { tr_seconds: 2 } }), input_evidence_stale: true, input_manifest_stale: true, sources: { "preprocessing.tr_seconds": "stale_data" } }} issues={["输入文件已变化，请重新读取并核实采集信息。"]} confirmation={{ title: "重新确认输入阶段", summary: "不启动计算", options: [{ label: "使用 FunRaw", description: "冻结输入", command: "确认起始阶段 FunRaw aaaaaaaaaaaa" }] }} confirmLabel="重新确认输入阶段" onSave={vi.fn()} onConfirm={recover} />);
    expect(screen.getByRole("status")).toHaveTextContent("参数选择已保留");
    expect(screen.getByText("来源：历史读取（待重读）")).toBeInTheDocument();
    expect(screen.getByLabelText("重复时间 TR（秒）")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新确认输入阶段" }));
    expect(recover).toHaveBeenCalledOnce();
  });
  it("allows reviewing a compiled plan for startup and disables a consumed plan", () => {
    const start = vi.fn();
    const saved = { ...snapshot(), compiled: true };
    const view = render(<WorkDraftPanel snapshot={saved} onSave={vi.fn()} confirmation={confirmation} confirmLabel="确认并启动计算" onConfirm={start} />);
    fireEvent.click(screen.getByRole("button", { name: "确认并启动计算" }));
    expect(start).toHaveBeenCalledOnce();
    view.rerender(<WorkDraftPanel snapshot={{ ...saved, consumed: true }} onSave={vi.fn()} confirmation={confirmation} confirmLabel="确认并启动计算" onConfirm={start} />);
    expect(screen.getByRole("button", { name: "确认并启动计算" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "ALFF" })).toBeDisabled();
    expect(screen.getByLabelText("重复时间 TR（秒）")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("已用于本次计算，科学参数已冻结");
    expect(screen.queryByText("等待补充与校验")).not.toBeInTheDocument();
  });
  it("keeps unknown boolean distinct from explicitly disabled", () => {
    render(<WorkDraftPanel snapshot={snapshot()} onSave={vi.fn()} />);
    expect(screen.getByLabelText("层时间校正")).toHaveValue("__unknown");
    fireEvent.change(screen.getByLabelText("层时间校正"), { target: { value: "false" } });
    expect(screen.getByLabelText("层时间校正")).toHaveValue("false");
    expect(screen.getByLabelText("层数")).toBeDisabled();
    expect(screen.getByText("状态：未保存")).toBeInTheDocument();
    expect(screen.getByText("来源：用户指定")).toBeInTheDocument();
  });
  it("saves the base hash and explicit false/null choices without server evidence", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ confirmed_hash: "old" })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("层时间校正"), { target: { value: "false" } });
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("a".repeat(64), expect.objectContaining({ preprocessing: { slice_timing: { enabled: false, slice_count: null, slice_order: null, reference_slice: null } } })));
    expect(save.mock.calls[0][1]).not.toHaveProperty("confirmed_hash");
  });
  it("preserves the sources of untouched model suggestions by sending only changed fields", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { tr_seconds: 2, dummy_scans: 10 } })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("重复时间 TR（秒）"), { target: { value: "3" } });
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save).toHaveBeenCalledWith("a".repeat(64), { preprocessing: { tr_seconds: 3 } });
  });
  it("retains local edits on a version conflict", async () => {
    const scrollIntoView = vi.fn();
    const original = HTMLElement.prototype.scrollIntoView;
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    render(<WorkDraftPanel snapshot={snapshot()} onSave={vi.fn().mockRejectedValue(new ApiError("草稿已变化。", 409, { code: "conflict", trace_id: "trace-123" }))} />);
    fireEvent.change(screen.getByLabelText("重复时间 TR（秒）"), { target: { value: "2" } });
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("本地修改仍保留");
    expect(screen.getByRole("alert").textContent).toBe("草稿已变化。本地修改仍保留，请核对草稿版本后重试。");
    expect(screen.getByRole("alert").textContent).not.toContain("trace-123");
    expect(screen.getByLabelText("重复时间 TR（秒）")).toHaveValue(2);
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" }));
    if (original) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: original });
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  });
  it("prevents confirmation for dirty or invalid arrays and permits discarding", () => {
    const dirty = vi.fn();
    render(<WorkDraftPanel snapshot={snapshot()} confirmation={confirmation} onSave={vi.fn()} onDirty={dirty} />);
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("层采集顺序"), { target: { value: "[1," } });
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "放弃本地修改" }));
    expect(screen.getByLabelText("层采集顺序")).toHaveValue("");
    expect(dirty).toHaveBeenLastCalledWith(false);
  });
  it("synchronizes metric selection with its parameter section", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot()} onSave={save} />);
    expect(screen.getByLabelText("ReHo 重复时间 TR（秒）")).toBeDisabled();
    fireEvent.click(screen.getByLabelText("REHO"));
    expect(screen.getByLabelText("ReHo 重复时间 TR（秒）")).toBeEnabled();
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
    fireEvent.change(screen.getByLabelText("标准化方法"), { target: { value: "3" } });
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
    expect(screen.getByLabelText("重复时间 TR（秒）")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存修改" })).not.toBeInTheDocument();
    rerender(<WorkDraftPanel snapshot={snapshot()} busy onSave={vi.fn()} />);
    expect(screen.getByLabelText("T1 分割")).toBeDisabled();
  });
  it("places field validation errors and exposes full advanced data", () => {
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { tr_seconds: 2 }, extra_review_value: "check" })} issues={["preprocessing.tr_seconds：invalid"]} preview={{ common_steps: ["freeze"] }} onSave={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("重复时间 TR（秒）：invalid");
    expect(screen.getByText(/extra_review_value/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认并生成方案" })).toBeDisabled();
  });
  it.each(["study_protocol_ref：尚未提供", "研究方案引用（study_protocol_ref）：尚未提供"])("surfaces and locates the required protocol reference: %s", async (issue) => {
    render(<WorkDraftPanel snapshot={snapshot()} issues={[issue]} onSave={vi.fn()} />);
    expect(screen.getByRole("button", { name: "研究方案引用是编译必填项。填写真实方案编号、文档引用或 DOI；未知时不要编造。" })).toBeInTheDocument();
    const row = screen.getByLabelText("研究方案引用（编译必填）").closest(".draft-field") as HTMLElement;
    expect(within(row).getByText("状态：待补充")).toBeInTheDocument();
    const summaryRow = screen.getByRole("region", { name: "关键参数状态" }).querySelector("li")!;
    expect(within(summaryRow).getByText(/状态：待补充/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "研究方案引用是编译必填项。填写真实方案编号、文档引用或 DOI；未知时不要编造。" }));
    expect(screen.getByLabelText("研究方案引用（编译必填）")).toHaveFocus();
  });
  it("ignores malformed old payloads", () => {
    expect(readWorkDraft(undefined)).toBeUndefined();
    expect(readWorkDraft({ draft: [], hash: "a" })).toBeUndefined();
    expect(readWorkDraft(snapshot())).toEqual(snapshot());
  });
  it("separates proven metadata from recommendations and does not infer verification from approval", () => {
    render(<WorkDraftPanel snapshot={{ ...snapshot({ preprocessing: { tr_seconds: 2, dummy_scans: 0 }, confirmed_hash: "old" }), sources: { "preprocessing.tr_seconds": "data", "preprocessing.dummy_scans": "model" }, field_states: { "preprocessing.tr_seconds": "verified" } }} onSave={vi.fn()} />);
    const tr = screen.getByLabelText("重复时间 TR（秒）").parentElement!;
    expect(within(tr).getByText("来源：数据读取")).toBeInTheDocument();
    expect(within(tr).getByText("状态：已核实")).toBeInTheDocument();
    const scans = screen.getByLabelText("删除初始时间点").parentElement!;
    expect(within(scans).getByText("来源：模型推荐")).toBeInTheDocument();
    expect(within(scans).getByText("状态：待审查")).toBeInTheDocument();
  });
  it("summarizes key parameter values, sources, and conflicts before detailed editing", () => {
    render(<WorkDraftPanel snapshot={{
      ...snapshot({
        preprocessing: {
          tr_seconds: 2, expected_time_points: 234, dummy_scans: 0,
          slice_timing: { enabled: true, slice_count: 33, slice_order: [1, 2, 3], reference_slice: 33 },
          nuisance: { enabled: true, head_motion_model: 4, white_matter: { enabled: true }, csf: { enabled: true } },
        },
        alff_falff: { filter_timing: "after_normalize", result_smoothing: true, result_smoothing_fwhm_mm: [4, 4, 4] },
      }),
      sources: { "preprocessing.tr_seconds": "data", "preprocessing.expected_time_points": "data", "preprocessing.dummy_scans": "suggested", "preprocessing.slice_timing.slice_order": "system", "preprocessing.nuisance": "suggested", "alff_falff.filter_timing": "suggested", "alff_falff.result_smoothing": "user" },
      field_states: { "preprocessing.tr_seconds": "verified", "preprocessing.expected_time_points": "verified", "preprocessing.slice_timing.slice_order": "verified" },
    }} issues={["preprocessing.nuisance.head_motion_model：与研究方案冲突"]} onSave={vi.fn()} />);
    const summary = screen.getByRole("region", { name: "关键参数状态" });
    expect(summary).toHaveTextContent("来源与核验状态分别显示 · 启动仍需独立确认");
    expect(within(summary).getByText("TR")).toBeInTheDocument();
    expect(within(summary).getByText("2")).toBeInTheDocument();
    const trRow = within(summary).getByText("TR").closest("li")!;
    expect(within(trRow).getByText("来源：数据读取 · 状态：已核实")).toBeInTheDocument();
    expect(within(summary).getByText(/33 层 · 参考层 33 · 采集顺序 3 项/)).toBeInTheDocument();
    const sliceTimingRow = within(summary).getByText("层时间校正").closest("li")!;
    expect(within(sliceTimingRow).getByText("来源：系统计算 · 状态：已核实")).toBeInTheDocument();
    expect(within(summary).getByText(/Friston-24/)).toBeInTheDocument();
    const nuisanceRow = within(summary).getByText("协变量回归").closest("li")!;
    expect(within(nuisanceRow).getByText("来源：模型推荐 · 状态：存在冲突")).toBeInTheDocument();
    expect(within(summary).getByText("标准化后")).toBeInTheDocument();
    expect(within(summary).getByText(/来源：用户指定/)).toBeInTheDocument();
    expect(within(summary).getByText("结果图平滑核（毫米）")).toBeInTheDocument();
    expect(within(summary).getByText("4、4、4")).toBeInTheDocument();
  });
  it("shows semantic changes and saves comma separated values without model guidance", async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkDraftPanel snapshot={snapshot({ requested_metrics: ["alff"], alff_falff: { result_smoothing_fwhm_mm: [4, 4, 4] } })} onSave={save} />);
    fireEvent.change(screen.getByLabelText("结果图 平滑核（毫米）"), { target: { value: "6, 6, 6" } });
    expect(screen.getByLabelText("本次修改")).toHaveTextContent("4、4、4 → 6、6、6");
    await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(save).toHaveBeenCalledWith("a".repeat(64), { alff_falff: { result_smoothing_fwhm_mm: [6, 6, 6] } });
  });
  it("locates a problem inside a folded advanced section using the keyboard accessible action", async () => {
    render(<WorkDraftPanel snapshot={snapshot({ preprocessing: { nuisance: { white_matter: {} } } })} issues={["preprocessing.nuisance.white_matter.compcor_components：请核对成分数"]} onSave={vi.fn()} />);
    const field = screen.getByLabelText("WM 白质 CompCor 成分数");
    expect(field.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(screen.getByRole("button", { name: "WM 白质 CompCor 成分数：请核对成分数" }));
    expect(field).toHaveFocus();
    expect(field.closest("details")).toHaveAttribute("open");
    expect(field).toHaveAttribute("aria-invalid", "true");
  });
  it("keeps the internal revision token inside collapsed technical details", () => {
    render(<WorkDraftPanel snapshot={{ ...snapshot(), version: 3 }} onSave={vi.fn()} />);
    expect(screen.getByRole("heading", { name: /第 3 版/ })).toBeInTheDocument();
    expect(screen.getByText(/方案标识/).closest("details")).not.toHaveAttribute("open");
  });
});
