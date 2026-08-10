import { useEffect, useMemo, useState } from "react";

import { api, describeError, type Project, type Run } from "../api/client";
import { EmptyState, Feedback, MetricCard, PageHeader, ProgressBar, SafetyNotice } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { Link } from "../routing";
import { resetWorkspace, selectWorkspaceProject, updateWorkspace, useWorkspace } from "../workspace";

const stageNames = ["数据检查与冻结", "分析方案确认", "DPABI / Mock 执行", "质量控制", "组统计"];

function stageIndex(workspace: ReturnType<typeof useWorkspace>): number {
  if (workspace.statisticalDesignId) return 4;
  if (workspace.qcReviewId || workspace.runState === "qc_review") return 3;
  if (workspace.runId) return 2;
  if (workspace.planRevisionId) return 1;
  if (workspace.manifestId) return 0;
  return -1;
}

function toneForRun(state?: string): "neutral" | "good" | "warn" | "danger" | "info" {
  if (state === "succeeded") return "good";
  if (state?.startsWith("failed") || state === "timed_out") return "danger";
  if (state === "running" || state === "queued") return "info";
  if (state === "qc_review" || state === "cancelling") return "warn";
  return "neutral";
}

export function Dashboard() {
  const workspace = useWorkspace();
  const [projects, setProjects] = useState<Project[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const currentStage = stageIndex(workspace);

  useEffect(() => {
    const controller = new AbortController();
    api.projects(controller.signal).then((items) => {
      setProjects(items);
      if (!workspace.projectId && items.length === 1) {
        selectWorkspaceProject(items[0].project_id, items[0].version);
      } else if (workspace.projectId) {
        const current = items.find((item) => item.project_id === workspace.projectId);
        if (current && current.version !== workspace.projectVersion) {
          updateWorkspace({ projectVersion: current.version });
        }
      }
    }).catch((caught) => setError(describeError(caught)));
    return () => controller.abort();
  }, [workspace.projectId, workspace.projectVersion]);

  useEffect(() => {
    if (!workspace.projectId) {
      setRuns([]);
      return;
    }
    const controller = new AbortController();
    api.runs(workspace.projectId, controller.signal).then(setRuns).catch((caught) => setError(describeError(caught)));
    return () => controller.abort();
  }, [workspace.projectId]);

  function chooseProject(projectId: string): void {
    if (!projectId) {
      resetWorkspace();
      return;
    }
    const project = projects.find((item) => item.project_id === projectId);
    if (project) selectWorkspaceProject(project.project_id, project.version);
  }

  const activeRun = useMemo(
    () => runs.find((run) => run.run_id === workspace.runId) ?? runs[0],
    [runs, workspace.runId],
  );
  const progress = currentStage < 0 ? 0 : Math.round(((currentStage + 1) / stageNames.length) * 100);

  return (
    <>
      <PageHeader
        eyebrow="工作总览"
        title="从数据到可信结果"
        description="这里仅显示后端已经持久化的项目、审批和运行状态；尚未完成的步骤不会伪装成成功。"
        action={<Link className="button button-primary" to={workspace.manifestId ? "/plan" : "/data"}>{workspace.manifestId ? "继续分析方案" : "创建或检查数据"}</Link>}
      />
      <Feedback message={error} error />
      {projects.length > 0 && (
        <section className="panel form-panel project-picker" aria-label="当前项目选择">
          <label className="field-grow">当前项目
            <select value={workspace.projectId ?? ""} onChange={(event) => chooseProject(event.target.value)}>
              <option value="">请选择项目</option>
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>{project.name} · {project.project_id.slice(0, 8)}</option>
              ))}
            </select>
            <small>切换项目会清空浏览器中的数据集、计划、运行、QC 和统计指针，不会删除后端记录。</small>
          </label>
        </section>
      )}
      <section className="metric-grid" aria-label="项目摘要">
        <MetricCard label="本机项目" value={String(projects.length)} detail={workspace.projectId ? "当前项目已选择" : "尚未选择项目"} tone={projects.length ? "info" : "neutral"} />
        <MetricCard label="冻结受试者" value={String(workspace.subjectIds?.length ?? 0)} detail={workspace.manifestId ? `manifest ${workspace.manifestId.slice(0, 8)}…` : "尚无 manifest"} tone={workspace.manifestId ? "good" : "neutral"} />
        <MetricCard label="运行记录" value={String(runs.length)} detail={activeRun ? `最近：${activeRun.state}` : "尚未运行"} tone={toneForRun(activeRun?.state)} />
        <MetricCard label="当前阶段" value={currentStage < 0 ? "尚未开始" : stageNames[currentStage]} detail={workspace.planState ? `计划：${workspace.planState}` : "状态来自本机数据库"} />
      </section>

      <div className="two-column">
        <section className="panel">
          <div className="panel-heading">
            <div><span className="eyebrow">研究流程</span><h2>当前进度</h2></div>
            <span className="muted">{Math.max(0, currentStage + 1)} / {stageNames.length}</span>
          </div>
          <ProgressBar value={progress} />
          <ol className="stage-list">
            {stageNames.map((name, index) => {
              const complete = index < currentStage;
              const active = index === currentStage;
              return (
                <li key={name}>
                  <span className="stage-index">{index + 1}</span>
                  <strong>{name}</strong>
                  <StatusPill tone={complete ? "good" : active ? "info" : "neutral"}>{complete ? "已记录" : active ? "当前" : "未开始"}</StatusPill>
                </li>
              );
            })}
          </ol>
        </section>
        <section className="panel panel-dark">
          <span className="eyebrow">下一步</span>
          <h2>{workspace.manifestId ? "明确参数并生成不可变计划" : "先建立只读数据清单"}</h2>
          <p>{workspace.manifestId ? "ALFF/fALFF、ReHo 会根据各自的产物契约编译顺序。审批会绑定输入、Skill、Tool 和环境哈希。" : "扫描只建立元数据、哈希与受试者顺序，不会整理或改写源目录。"}</p>
          <Link className="button button-light" to={workspace.manifestId ? "/plan" : "/data"}>继续</Link>
        </section>
      </div>
      {projects.length === 0 && <section className="panel"><EmptyState title="还没有项目" detail="从数据页面创建第一个本机项目；所有路径仍由后端边界策略校验。" /></section>}
      <SafetyNotice />
    </>
  );
}
