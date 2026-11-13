import { useSyncExternalStore } from "react";

export type WorkspaceState = {
  workspacePath?: string;
  workspaceKind?: string;
  workspaceCheckedAt?: string;
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
let memoryState: WorkspaceState | null = null;
type PersistedWorkspaceState = Pick<WorkspaceState, "workspacePath" | "projectId">;

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function read(): WorkspaceState {
  if (typeof window === "undefined") return {};
  try {
    const persistent = storage()?.getItem(STORAGE_KEY);
    if (persistent) return persistedSelection(JSON.parse(persistent));

    // Keep only user-selected context from the previous tab-scoped snapshot.
    const legacy = window.sessionStorage.getItem(STORAGE_KEY);
    if (legacy) {
      const selected = persistedSelection(JSON.parse(legacy));
      persist(selected);
      return selected;
    }
    return {};
  } catch {
    return {};
  }
}

function persistedSelection(value: unknown): PersistedWorkspaceState {
  if (!value || typeof value !== "object") return {};
  const candidate = value as Partial<WorkspaceState>;
  return {
    ...(typeof candidate.workspacePath === "string" ? { workspacePath: candidate.workspacePath } : {}),
    ...(typeof candidate.projectId === "string" ? { projectId: candidate.projectId } : {}),
  };
}

function persist(state: WorkspaceState): void {
  try {
    storage()?.setItem(STORAGE_KEY, JSON.stringify(persistedSelection(state)));
  } catch {
    // Runtime state remains usable when browser storage is unavailable.
  }
}

function currentState(): WorkspaceState {
  if (memoryState === null) memoryState = read();
  return memoryState;
}

function snapshot(): string {
  return JSON.stringify(currentState());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function updateWorkspace(patch: Partial<WorkspaceState>): WorkspaceState {
  memoryState = { ...currentState(), ...patch };
  persist(memoryState);
  listeners.forEach((listener) => listener());
  return memoryState;
}

export function selectWorkspaceProject(projectId: string, projectVersion: number): WorkspaceState {
  memoryState = { projectId, projectVersion };
  persist(memoryState);
  if (typeof window !== "undefined") window.sessionStorage.removeItem(STORAGE_KEY);
  listeners.forEach((listener) => listener());
  return memoryState;
}

export function resetWorkspace(): void {
  memoryState = null;
  try {
    storage()?.removeItem(STORAGE_KEY);
    if (typeof window !== "undefined") window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // The in-memory selection is still reset.
  }
  listeners.forEach((listener) => listener());
}

export function restoreWorkspace(state: WorkspaceState): void {
  memoryState = { ...state };
  persist(memoryState);
  try {
    if (typeof window !== "undefined") window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // The in-memory selection is still restored.
  }
  listeners.forEach((listener) => listener());
}

export function useWorkspace(): WorkspaceState {
  return JSON.parse(useSyncExternalStore(subscribe, snapshot, () => "{}")) as WorkspaceState;
}
