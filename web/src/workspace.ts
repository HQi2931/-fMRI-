import { useSyncExternalStore } from "react";

export type WorkspaceState = {
  projectId?: string;
  projectVersion?: number;
  datasetId?: string;
  datasetVersion?: number;
  manifestId?: string;
  manifestHash?: string;
  subjectIds?: string[];
  demographicsId?: string;
  demographicsRevision?: number;
  splitId?: string;
  splitRevision?: number;
  planRevisionId?: string;
  planVersion?: number;
  planHash?: string;
  planState?: string;
  runId?: string;
  runVersion?: number;
  runState?: string;
  qcReviewId?: string;
  qcReviewVersion?: number;
  qcReviewHash?: string;
  statisticalDesignId?: string;
  statisticalDesignVersion?: number;
  statisticalDesignHash?: string;
};

const STORAGE_KEY = "rsfmri-workspace-v1";
const listeners = new Set<() => void>();
let memoryState: WorkspaceState = {};

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function read(): WorkspaceState {
  if (typeof window === "undefined") return memoryState;
  try {
    const persistent = storage()?.getItem(STORAGE_KEY);
    if (persistent) return JSON.parse(persistent) as WorkspaceState;

    // Migrate the pre-MVP tab-scoped workspace without losing an in-progress review.
    const legacy = window.sessionStorage.getItem(STORAGE_KEY);
    if (legacy) {
      storage()?.setItem(STORAGE_KEY, legacy);
      return JSON.parse(legacy) as WorkspaceState;
    }
    return {};
  } catch {
    return memoryState;
  }
}

function snapshot(): string {
  return JSON.stringify(read());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function updateWorkspace(patch: Partial<WorkspaceState>): WorkspaceState {
  memoryState = { ...read(), ...patch };
  storage()?.setItem(STORAGE_KEY, JSON.stringify(memoryState));
  listeners.forEach((listener) => listener());
  return memoryState;
}

export function selectWorkspaceProject(projectId: string, projectVersion: number): WorkspaceState {
  memoryState = { projectId, projectVersion };
  storage()?.setItem(STORAGE_KEY, JSON.stringify(memoryState));
  if (typeof window !== "undefined") window.sessionStorage.removeItem(STORAGE_KEY);
  listeners.forEach((listener) => listener());
  return memoryState;
}

export function resetWorkspace(): void {
  memoryState = {};
  storage()?.removeItem(STORAGE_KEY);
  if (typeof window !== "undefined") window.sessionStorage.removeItem(STORAGE_KEY);
  listeners.forEach((listener) => listener());
}

export function useWorkspace(): WorkspaceState {
  return JSON.parse(useSyncExternalStore(subscribe, snapshot, () => "{}")) as WorkspaceState;
}
