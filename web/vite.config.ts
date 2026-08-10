import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

import { resolveLocalApiTarget } from "./config/localApiTarget";

export default defineConfig(({ mode }) => {
  const repositoryRoot = fileURLToPath(new URL("../", import.meta.url));
  const environment = loadEnv(mode, repositoryRoot, "RSFMRI_");
  const apiTarget = resolveLocalApiTarget(environment, process.env);

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": apiTarget.origin,
      },
    },
    preview: {
      host: "127.0.0.1",
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      testTimeout: 15_000,
      exclude: ["e2e/**", "node_modules/**", "dist/**"],
      coverage: {
        provider: "v8",
        reporter: ["text", "json-summary"],
        exclude: [
          "**/schema.generated.ts",
          "**/main.tsx",
          "**/playwright.config.ts",
          "**/vite.config.ts",
          "**/eslint.config.js",
          "**/dist/**",
          "**/e2e/**",
        ],
        thresholds: {
          statements: 80,
          branches: 80,
          functions: 80,
          lines: 80,
        },
      },
    },
  };
});
