import { useEffect, useState } from "react";
import { api, describeError, type Project, type WorkCard, type WorkCardKind, type WorkCardResult } from "../api/client";
import { DataPage } from "../pages/DataPage";
import { PlanPage } from "../pages/PlanPage";
import { RunsPage } from "../pages/RunsPage";
import { QcPage } from "../pages/QcPage";
import { StatisticsPage } from "../pages/StatisticsPage";
import { AnalysisPage } from "../pages/AnalysisPage";
import { Link } from "../routing";
import { WorkCardProvider, useWorkCard } from "./WorkCardContext";

function ProjectCard({ open }: { open: (kind: WorkCardKind) => void }) {
  const context = useWorkCard();
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api.projects(controller.signal).then(setProjects).catch((caught) => { if (!controller.signal.aborted) setError(describeError(caught)); });
    return () => controller.abort();
  }, []);
  const select = async (project: Project) => {
    setBusy(true);
    try { await context?.execute("selectProject", [project.project_id]); }
    catch (caught) { setError(describeError(caught)); }
    finally { setBusy(false); }
  };
  return <div className="work-project-picker">
    <p>选择已有项目，或填写新项目与数据目录。</p>
    {error && <p role="alert">{error}</p>}
    <div className="selection-list">{projects.map((project) => <button type="button" key={project.project_id} disabled={busy} onClick={() => void select(project)}>{project.name}</button>)}</div>
    <button className="button button-secondary" type="button" disabled={busy} onClick={() => open("data")}>建立新项目 / 登记数据</button>
  </div>;
}

export function WorkCardView({ card, conversationId, onUpdate, open }: {
  card: WorkCard; conversationId: string; onUpdate: (result: WorkCardResult) => void;
  open: (kind: WorkCardKind) => void;
}) {
  return <WorkCardProvider card={card} conversationId={conversationId} onUpdate={onUpdate}>
    <div className="work-card-content">
      {card.kind === "project" && <ProjectCard open={open} />}
      {card.kind === "data" && <DataPage embedded />}
      {card.kind === "plan" && <PlanPage embedded />}
      {card.kind === "runs" && <RunsPage embedded />}
      {card.kind === "qc" && <QcPage embedded />}
      {card.kind === "statistics" && <StatisticsPage embedded />}
      {card.kind === "analysis" && <AnalysisPage embedded />}
      {card.kind === "settings" && <p>环境路径和模型连接统一在 <Link to="/settings">设置</Link> 中管理。</p>}
    </div>
  </WorkCardProvider>;
}
