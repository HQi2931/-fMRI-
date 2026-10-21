import { useEffect, useState } from "react";

import { environmentApi } from "../api/environment";
import { projectsApi } from "../api/projects";
import { describeError } from "../api/transport";
import type { Dataset, Manifest, Project, WorkspaceCheck } from "../api/types";

export type WorkspaceProjectRegistration = {
  project: Project;
  dataset: Dataset;
  manifest: Manifest;
};

type Props = {
  path: string;
  check: WorkspaceCheck;
  disabled?: boolean;
  onProjectReady: (registration: WorkspaceProjectRegistration) => Promise<void>;
};

function pathParts(value: string): { parent: string; name: string; separator: string } {
  const trimmed = value.trim().replace(/[\\/]+$/, "");
  const index = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return {
    parent: index >= 0 ? trimmed.slice(0, index) : "",
    name: index >= 0 ? trimmed.slice(index + 1) : trimmed,
    separator: trimmed.includes("\\") ? "\\" : "/",
  };
}

function projectWorkRoot(allowedWorkRoot: string, sourcePath: string): string {
  const root = allowedWorkRoot.trim().replace(/[\\/]+$/, "");
  const source = pathParts(sourcePath);
  const separator = root.includes("\\") ? "\\" : "/";
  const slug = Array.from(source.name || "bids-dataset")
    .map((character) => character.charCodeAt(0) < 32 || '<>:"/\\|?*'.includes(character) ? "_" : character)
    .join("");
  return root ? `${root}${separator}${slug}-project` : "";
}

function normalizedPath(value: string): string {
  return value.trim().replace(/\\/g, "/").replace(/\/+$/, "").toLocaleLowerCase();
}

function pathsOverlap(left: string, right: string): boolean {
  const source = normalizedPath(left);
  const work = normalizedPath(right);
  if (!source || !work) return true;
  return source === work || source.startsWith(`${work}/`) || work.startsWith(`${source}/`);
}

export function WorkspaceProjectSetupPanel({ path, check, disabled = false, onProjectReady }: Props) {
  const parts = pathParts(path);
  const [projectName, setProjectName] = useState(`rs-fMRI 项目 · ${parts.name}`);
  const [datasetName, setDatasetName] = useState(parts.name || "BIDS 数据集");
  const [workRootBase, setWorkRootBase] = useState("");
  const [workRoot, setWorkRoot] = useState("");
  const [configLoading, setConfigLoading] = useState(true);
  const [configError, setConfigError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [project, setProject] = useState<Project | null>(null);
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [registration, setRegistration] = useState<WorkspaceProjectRegistration | null>(null);
  const [openError, setOpenError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    environmentApi.environmentConfig(controller.signal)
      .then((config) => {
        const root = config.allowed_work_root?.trim() ?? "";
        if (!root) throw new Error("应用尚未提供安全的派生产物根目录，无法建立项目。");
        setWorkRootBase(root);
        setWorkRoot(projectWorkRoot(root, path));
      })
      .catch((caught) => {
        if (!controller.signal.aborted) setConfigError(describeError(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setConfigLoading(false);
      });
    return () => controller.abort();
  }, [path]);

  const overlappingDirectories = Boolean(workRoot && pathsOverlap(path, workRoot));
  const canRegister = Boolean(
    !disabled && !busy && !registration && !configLoading && !configError && check.kind === "bids"
    && check.issues.length === 0 && projectName.trim() && datasetName.trim()
    && path.trim() && workRoot.trim() && !overlappingDirectories && confirmed,
  );

  async function openProject(registrationToOpen = registration): Promise<void> {
    if (!registrationToOpen || busy || disabled) return;
    setBusy(true);
    setOpenError("");
    try {
      await onProjectReady(registrationToOpen);
    } catch (caught) {
      setOpenError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function registerProject(): Promise<void> {
    if (!canRegister) return;
    setBusy(true);
    setError("");
    setOpenError("");
    try {
      const createdProject = project ?? await projectsApi.createProject({
        name: projectName.trim(),
        source_roots: [path],
        work_root: workRoot.trim(),
      });
      if (!project) setProject(createdProject);

      const createdDataset = dataset ?? await projectsApi.createDataset(createdProject.project_id, {
        name: datasetName.trim(),
        source_path: path,
        expected_project_version: createdProject.version,
      });
      if (!dataset) setDataset(createdDataset);

      const manifest = await projectsApi.inspectDataset(createdDataset.dataset_id, {
        expected_dataset_version: createdDataset.version,
        report_only: false,
      });
      if (manifest.dataset_id !== createdDataset.dataset_id || !manifest.manifest_id || !manifest.content_hash) {
        throw new Error("服务器返回的清单未能与当前数据集核对，项目没有进入可用状态。");
      }
      const completed = { project: createdProject, dataset: createdDataset, manifest };
      setRegistration(completed);
      await openProject(completed);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return <section className="dataset-demographics dataset-project-setup" aria-label="建立项目和数据集">
    <details>
      <summary>将当前 BIDS 数据集登记为项目</summary>
      <p>项目登记会创建本地项目、数据集和一份冻结清单。影像目录作为只读来源；计算文件写入应用配置的安全工作根目录下。</p>
      <label>项目名称
        <input value={projectName} disabled={busy || disabled || Boolean(project)} onChange={(event) => setProjectName(event.target.value)} />
      </label>
      <label>数据集名称
        <input value={datasetName} disabled={busy || disabled || Boolean(dataset)} onChange={(event) => setDatasetName(event.target.value)} />
      </label>
      <label>独立计算目录
        <input value={workRoot} disabled={busy || disabled || Boolean(project)} onChange={(event) => { setWorkRoot(event.target.value); setConfirmed(false); }} />
      </label>
      {configLoading && <p role="status">正在读取应用的安全工作根目录…</p>}
      {configError && <p className="dataset-demographics-error" role="alert">无法读取应用的安全工作根目录：{configError}</p>}
      {workRootBase && <p>默认位置在安全工作根目录 {workRootBase} 内；可编辑具体计算目录，服务端会再次校验。</p>}
      {overlappingDirectories && <p className="dataset-demographics-error" role="alert">计算目录必须与影像来源目录分开。</p>}
      <label className="dataset-demographics-confirm">
        <input type="checkbox" checked={confirmed} disabled={busy || disabled || overlappingDirectories || !projectName.trim() || !datasetName.trim()} onChange={(event) => setConfirmed(event.target.checked)} />
        我已核对来源目录和独立计算目录，确认只扫描并登记当前 BIDS 数据集。
      </label>
      {error && <p className="dataset-demographics-error" role="alert">登记未完成：{error}</p>}
      {registration && <div className="dataset-demographics-result is-valid" role="status">
        <strong>项目和冻结清单已登记</strong>
        <p>{registration.manifest.subjects.length} 名受试者 · 清单修订 {registration.manifest.revision}</p>
        {((registration.manifest.profile.issues?.length ?? 0) > 0 || (registration.manifest.profile.warnings?.length ?? 0) > 0) && <p>清单包含待处理事项；请先查看扫描摘要，再准备计算。</p>}
        {openError && <p className="dataset-demographics-error">项目已登记，但未能打开项目会话：{openError}</p>}
        <button className="button button-secondary" type="button" disabled={busy || disabled} onClick={() => void openProject()}>
          {busy ? "正在打开项目…" : "在项目中继续"}
        </button>
      </div>}
      {!registration && <button className="button button-primary" type="button" disabled={!canRegister} onClick={() => void registerProject()}>
        {busy ? (project ? dataset ? "正在检查并冻结 BIDS 清单…" : "正在登记数据集…" : "正在建立项目…") : "登记项目并扫描清单"}
      </button>}
    </details>
  </section>;
}
