import { describe, expect, it } from "vitest";

import { resolveLocalApiTarget } from "./localApiTarget";

describe("local API target", () => {
  it.each([
    ["127.0.0.1", "http://127.0.0.1:8000"],
    ["localhost", "http://localhost:8000"],
    ["::1", "http://[::1]:8000"],
  ])("accepts loopback host %s", (host, origin) => {
    expect(resolveLocalApiTarget({ RSFMRI_HOST: host }, {})).toEqual({ host, port: 8000, origin });
  });

  it("gives the process environment precedence over the repository .env", () => {
    expect(
      resolveLocalApiTarget(
        { RSFMRI_HOST: "localhost", RSFMRI_PORT: "8000" },
        { RSFMRI_HOST: "::1", RSFMRI_PORT: "9000" },
      ),
    ).toEqual({ host: "::1", port: 9000, origin: "http://[::1]:9000" });
  });

  it.each(["remote.example", "127.0.0.1.attacker.example", "0.0.0.0", " localhost"])(
    "rejects non-exact local host %s",
    (host) => {
      expect(() => resolveLocalApiTarget({ RSFMRI_HOST: host }, {})).toThrow(/RSFMRI_HOST/);
    },
  );

  it.each(["0", "65536", "8.5", " 8000", "8000 ", "not-a-port", ""])(
    "rejects invalid port %s",
    (port) => {
      expect(() => resolveLocalApiTarget({ RSFMRI_PORT: port }, {})).toThrow(/RSFMRI_PORT/);
    },
  );
});
