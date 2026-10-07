import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { WorkspaceCheck } from "../api/client";
import { WorkspacePanel } from "./WorkspacePanel";

afterEach(cleanup);
const defaults = (): ComponentProps<typeof WorkspacePanel> => ({
  path: "D:/synthetic", check: null, checking: false, checkError: "", selectedStage: "",
  busy: false, consentBusy: false, consentGranted: false, canAuthorize: true,
  authorizationUnavailableReason: "先选择模型", onChoose: vi.fn(), onStageChange: vi.fn(), onConsent: vi.fn(),
});
const candidate = (stage: string, selectable = true) => ({
  stage, selectable, label: "测试输入", completed_steps: [], requires_realign_parameters: false,
  subject_count: 1, file_count: 2, issues: selectable ? [] : ["缺少头动记录"], rule_version: "test",
});
const check = (...candidates: ReturnType<typeof candidate>[]): WorkspaceCheck => ({
  path: "D:/synthetic", kind: "dpabi_ready", file_count: 2, nifti_count: 2, dicom_count: 0,
  subject_count: 1, functional_subject_count: 1, anatomical_subject_count: 1,
  input_stage: null, selected_input_stage: null, stage_selection_required: true,
  stage_candidates: candidates, output_directories: [], invalid_nifti_files: [],
  issues: [], warnings: [], subjects: [], checked_at: "2026-10-07T00:00:00Z",
});

it("retains a manually typed path across closing and checks it only on explicit submit", async () => {
  const user = userEvent.setup();
  const submit = vi.fn();
  const props = { ...defaults(), onPathSubmit: submit };
  render(<WorkspacePanel {...props} />);
  await user.click(screen.getByText("手动填写工作区路径"));
  const input = await screen.findByLabelText("工作区路径");
  await user.clear(input);
  await user.type(input, "  D:/new-source  ");
  await user.click(screen.getByText("手动填写工作区路径"));
  await waitFor(() => expect(screen.queryByLabelText("工作区路径")).not.toBeInTheDocument());
  expect(submit).not.toHaveBeenCalled();
  await user.click(screen.getByText("手动填写工作区路径"));
  expect(await screen.findByLabelText("工作区路径")).toHaveValue("  D:/new-source  ");
  await user.click(screen.getByRole("button", { name: "使用此工作区" }));
  expect(submit).toHaveBeenCalledWith("D:/new-source");
});

it("allows selecting a single stage without a radio and keeps confirmation separate", async () => {
  const user = userEvent.setup();
  const props = { ...defaults(), check: check(candidate("FunImg")) };
  const view = render(<WorkspacePanel {...props} />);
  await user.click(screen.getByText("预处理起点"));
  expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "选择 FunImg" }));
  expect(props.onStageChange).toHaveBeenCalledWith("FunImg");
  view.rerender(<WorkspacePanel {...props} selectedStage="FunImg" />);
  expect(screen.getByRole("button", { name: "已选择，待确认" })).toBeDisabled();
  expect(screen.queryByText("起点已确认")).not.toBeInTheDocument();
  view.rerender(<WorkspacePanel {...props} selectedStage="FunImg" stageConfirmed />);
  expect(screen.getByRole("button", { name: "起点已确认" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "清除选择" }));
  expect(props.onStageChange).toHaveBeenLastCalledWith("");
});

it("exposes unavailable multi-stage inputs and prevents selecting them with a keyboard", async () => {
  const user = userEvent.setup();
  const props = { ...defaults(), check: check(candidate("FunImg"), candidate("FunImgAR", false)) };
  render(<WorkspacePanel {...props} />);
  await user.click(screen.getByText("预处理起点"));
  expect(screen.getByText("缺少头动记录")).toBeInTheDocument();
  const choices = screen.getAllByRole("radio");
  expect(choices[1]).toBeDisabled();
  choices[0].focus();
  await user.keyboard(" ");
  expect(props.onStageChange).toHaveBeenCalledOnce();
  expect(props.onStageChange).toHaveBeenCalledWith("FunImg");
});

it("disables file consent while authorizing and allows explicit revocation afterwards", async () => {
  const user = userEvent.setup();
  const props = defaults();
  const view = render(<WorkspacePanel {...props} consentBusy />);
  expect(screen.getByRole("button", { name: "测试并授权…" })).toBeDisabled();
  view.rerender(<WorkspacePanel {...props} consentGranted />);
  await user.click(screen.getByRole("button", { name: "撤销文件授权" }));
  expect(props.onConsent).toHaveBeenCalledOnce();
  expect(screen.getByText("文件分析已授权")).toBeInTheDocument();
});

it("keeps a failed workspace check visible and does not grant consent without a model", () => {
  render(<WorkspacePanel {...defaults()} canAuthorize={false} checkError="目录暂时不可用" />);
  expect(screen.getByRole("status")).toHaveTextContent("目录暂时不可用");
  expect(screen.getByText("先选择模型")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "授权文件分析" })).toBeDisabled();
});
