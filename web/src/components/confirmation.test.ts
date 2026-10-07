import { describe, expect, it } from "vitest";
import { currentConfirmation } from "./confirmation";

const request = { title: "确认计算", summary: "当前方案", options: [{ label: "确认", description: "启动当前版本", command: "server-command" }] };
const message = (id: string, payload: Record<string, unknown>) => ({ message_id: id, role: "assistant", payload });
const draft = { hash: "a".repeat(64), compiled: true, workspace_path: "D:/example" };
describe("confirmation bound to the current draft", () => {
  it("keeps a valid review action after a method question without granting new authority", () => {
    const result = currentConfirmation([message("review", { work_draft: draft, confirmation_request: request }), message("question", {})], "D:/example");
    expect(result?.messageId).toBe("review");
    expect(result?.request.options[0].command).toBe("server-command");
  });
  it("does not resurrect an old action after changes, consumption or workspace switching", () => {
    const previous = message("old", { work_draft: draft, confirmation_request: request });
    for (const changed of [{ ...draft, hash: "b".repeat(64) }, { ...draft, compiled: false }, { ...draft, consumed: true }]) {
      expect(currentConfirmation([previous, message("new", { work_draft: changed })], "D:/example")).toBeNull();
    }
    expect(currentConfirmation([previous], "D:/another")).toBeNull();
  });
});
