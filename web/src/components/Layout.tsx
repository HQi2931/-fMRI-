import type { ConnectionState } from "../hooks/useApiHealth";
import { Link, NavLink } from "../routing";
import { StatusPill } from "./StatusPill";

const navigation = [
  ["/agent", "对话工作台", "✦"],
] as const;

function connectionLabel(state: ConnectionState) {
  if (state === "online") return ["服务已连接", "good"] as const;
  if (state === "offline") return ["离线预览", "warn"] as const;
  return ["正在连接", "neutral"] as const;
}

export function Layout({ children, connection }: { children: React.ReactNode; connection: ConnectionState }) {
  const [label, tone] = connectionLabel(connection);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M8 23V9l16 14V9M4 16h5m14 0h5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><circle cx="8" cy="9" r="3" fill="currentColor"/><circle cx="24" cy="23" r="3" fill="currentColor"/></svg></div>
          <div>
            <strong>NeuroAgent</strong>
            <span>rs-fMRI 工作台</span>
          </div>
        </div>
        <span className="nav-caption">研究空间 / WORKSPACE</span>
        <nav aria-label="主导航">
          {navigation.map(([to, labelText, icon]) => (
            <NavLink key={to} to={to}>
              <span aria-hidden="true">{icon}</span>
              {labelText}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-note"><span className="eyebrow">从问题到发现</span><p>专注研究，<br />让工作流更有条理。</p><span>方法探索 · 任务执行 · 结果追溯</span></div>
        <div className="sidebar-foot">
          <StatusPill tone={tone}>{label}</StatusPill>
          <p>原始数据只读 · 本机运行</p>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div>
            <span className="eyebrow">NeuroAgent</span>
            <strong>静息态 fMRI 对话工作台</strong>
          </div>
          <div className="topbar-actions">
            <Link className="button button-light" to="/settings"><span aria-hidden="true">⚙</span> 设置</Link>
          </div>
        </header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}
