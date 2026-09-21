import { describe, expect, it } from "vitest";

import { businessCapabilityForPath } from "./routing";

describe("legacy route compatibility", () => {
  it("does not expose legacy card capabilities", () => {
    expect(businessCapabilityForPath("/data")).toBeUndefined();
    expect(businessCapabilityForPath("/plan")).toBeUndefined();
    expect(businessCapabilityForPath("/qc")).toBeUndefined();
  });
});
