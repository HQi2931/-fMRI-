import { describe, expect, it } from "vitest";
import { agentRoutingSummary, parseAgentRouting } from "./conversation-model";

describe("agent routing metadata", () => {
  it("parses only the allowlisted routing fields", () => {
    expect(parseAgentRouting({
      status: "routed",
      source: "small_model",
      model: "qwen3.8-flash",
      profile_id: "dashscope",
      confidence: 0.99,
      failure_reason_code: "private_failure_detail",
      untrusted: "do not render this",
    })).toEqual({
      status: "routed",
      source: "small_model",
      model: "qwen3.8-flash",
      profileId: "dashscope",
    });
  });

  it("distinguishes rules, the small model, fallback, and server constraints", () => {
    expect(agentRoutingSummary(parseAgentRouting({ status: "routed", source: "rule", model: "rules" })!))
      .toBe("规则分派");
    expect(agentRoutingSummary(parseAgentRouting({
      status: "routed", source: "small_model", model: "qwen3.8-flash", profile_id: "dashscope",
    })!)).toBe("小模型分派 · qwen3.8-flash · Profile dashscope");
    expect(agentRoutingSummary(parseAgentRouting({
      status: "routed", source: "large_model_fallback", model: "deepseek-v4-flash", profile_id: "deepseek",
    })!)).toBe("大模型兜底分派 · deepseek-v4-flash · Profile deepseek");
    expect(agentRoutingSummary(parseAgentRouting({ status: "routed", source: "explicit_safety_constraint" })!))
      .toBe("服务端约束分派");
  });

  it("does not infer fallback from a generic model source and separates clarification from failure", () => {
    expect(agentRoutingSummary(parseAgentRouting({
      status: "routed", source: "model", model: "deepseek-v4-flash",
    })!)).toBe("模型分派 · deepseek-v4-flash");
    expect(agentRoutingSummary(parseAgentRouting({
      status: "needs_clarification", source: "large_model_fallback", model: "deepseek-v4-flash",
    })!)).toBe("待澄清 · 大模型兜底 · deepseek-v4-flash");
    expect(agentRoutingSummary(parseAgentRouting({
      status: "failed", source: "model", model: "configured profiles", profile_id: "deepseek",
    })!)).toBe("分派失败 · 模型路由未完成");
  });

  it("hides invalid identifiers and unknown status values", () => {
    expect(agentRoutingSummary(parseAgentRouting({
      status: "new_status_with_private_details",
      source: "unrecognized source",
      model: "D:/private/sub-023/model",
      profile_id: "../../private/profile",
    })!)).toBe("分派状态不可用");
    expect(parseAgentRouting(null)).toBeUndefined();
  });
});
