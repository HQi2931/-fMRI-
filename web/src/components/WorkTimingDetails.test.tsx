import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { WorkTimingDetails } from "./WorkTimingDetails";

afterEach(cleanup);
describe("Work measurements", () => {
  it("shows measured stages on request without implying missing phases were zero", () => {
    render(<WorkTimingDetails value={{ stages: [{ stage: "model_response", duration_ms: 2135, calls: 2 }, { stage: "compilation", duration_ms: 0.45, calls: 1 }] }} />);
    fireEvent.click(screen.getByText("查看本轮耗时"));
    expect(screen.getByText("模型响应")).toBeInTheDocument();
    expect(screen.getByText("2.13 秒")).toBeInTheDocument();
    expect(screen.getByText("0.5 毫秒")).toBeInTheDocument();
    expect(screen.queryByText("文件完整性检查")).not.toBeInTheDocument();
  });
  it("rejects unsupported or malformed measurements", () => {
    render(<WorkTimingDetails value={{ stages: [{ stage: "provider_secret", duration_ms: 1, calls: 1 }, { stage: "hashing", duration_ms: Infinity, calls: 1 }, { stage: "validation", duration_ms: -2, calls: 1 }] }} />);
    expect(screen.queryByText("查看本轮耗时")).not.toBeInTheDocument();
  });
});
