import { request } from "./transport";
import type { PaperIngestResult } from "./types";

export const literatureApi = {
  papers: (signal?: AbortSignal) => request<PaperIngestResult[]>("/literature/papers", { signal }),
  uploadPaper: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<PaperIngestResult>("/literature/papers", { method: "POST", body });
  },
  indexPaper: (paperId: string) => request<PaperIngestResult>(`/literature/papers/${encodeURIComponent(paperId)}/index`, { method: "POST" }),
  paperSource: (paperId: string) => `/api/v1/literature/papers/${encodeURIComponent(paperId)}/source`,
};
