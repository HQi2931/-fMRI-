import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import {
  resetWorkspace,
  restoreWorkspace,
  selectWorkspaceProject,
  updateWorkspace,
  useWorkspace,
} from "./workspace";

const STORAGE_KEY = "rsfmri-workspace-v1";

describe("workspace pointer", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    resetWorkspace();
  });

  it("merges patches, persists them and re-renders subscribers", () => {
    const { result } = renderHook(() => useWorkspace());
    expect(result.current).toEqual({});

    act(() => {
      updateWorkspace({ planRevisionId: "plan-1", planHash: "a".repeat(64) });
    });
    expect(result.current.planRevisionId).toBe("plan-1");

    act(() => {
      updateWorkspace({ runId: "run-1" });
    });
    // The earlier review state survives a later patch.
    expect(result.current).toMatchObject({
      planRevisionId: "plan-1",
      planHash: "a".repeat(64),
      runId: "run-1",
    });
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}")).toMatchObject({
      runId: "run-1",
    });
  });

  it("migrates a legacy tab-scoped pointer without losing the review", () => {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ planRevisionId: "legacy-plan", planHash: "b".repeat(64) }),
    );

    const { result } = renderHook(() => useWorkspace());

    expect(result.current).toMatchObject({ planRevisionId: "legacy-plan" });
    // The legacy value is promoted so a reload keeps the same pointer.
    expect(window.localStorage.getItem(STORAGE_KEY)).toContain("legacy-plan");
  });

  it("falls back to the in-memory pointer when storage is corrupt", () => {
    window.localStorage.setItem(STORAGE_KEY, "{not json");

    const { result } = renderHook(() => useWorkspace());

    expect(result.current).toEqual({});
  });

  it("selects a project and drops the legacy tab pointer", () => {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ planRevisionId: "stale" }));

    const state = selectWorkspaceProject("project-1", 3);

    expect(state).toEqual({ projectId: "project-1", projectVersion: 3 });
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}")).toMatchObject({
      projectId: "project-1",
    });
  });

  it("restores a frozen pointer and clears the tab-scoped copy", () => {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ planRevisionId: "stale" }));

    restoreWorkspace({ planRevisionId: "plan-9", planHash: "c".repeat(64) });

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    const { result } = renderHook(() => useWorkspace());
    expect(result.current.planRevisionId).toBe("plan-9");
  });

  it("clears every storage on reset and notifies subscribers", () => {
    const { result } = renderHook(() => useWorkspace());
    act(() => {
      updateWorkspace({ planRevisionId: "plan-1" });
    });
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ planRevisionId: "plan-1" }));
    expect(result.current.planRevisionId).toBe("plan-1");

    act(() => {
      resetWorkspace();
    });

    expect(result.current).toEqual({});
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});
