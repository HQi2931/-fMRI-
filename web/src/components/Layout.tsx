import type { ConnectionState } from "../hooks/useApiHealth";
import { NavLink } from "../routing";
import { StatusPill } from "./StatusPill";

const navigation = [
  ["/", "总览", "⌂"],
  ["/data", "数据", "▦"],
  ["/plan", "分析方案", "◇"],
  ["/runs", "运行", "▶"],
  ["/qc", "质量控制", "✓"],
  ["/statistics", "统计", "∑"],
  ["/agent", "智能助手", "✦"],
  ["/analysis", "扩展分析", "◌"],
  ["/settings", "环境", "⚙"],
] as const;

function connectionLabel(state: ConnectionState) {
  if (state === "online") return ["服务已连接", "good"] as const;
  if (state === "offline") return ["离线预览", "warn"] as const;
  return ["正在连接", "neutral"] as const;
}

export function Layout({ children, connection, dpabiLabel = "DPABI 未配置" }: { children: React.ReactNode; connection: ConnectionState; dpabiLabel?: string }) {
  const [label, tone] = connectionLabel(connection);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">N</div>
          <div>
            <strong>NeuroAgent</strong>
            <span>rs-fMRI 工作台</span>
          </div>
        </div>
        <nav aria-label="主导航">
          {navigation.map(([to, labelText, icon]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              <span aria-hidden="true">{icon}</span>
              {labelText}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <StatusPill tone={tone}>{label}</StatusPill>
          <p>原始数据只读 · 本机运行</p>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div>
            <span className="eyebrow">研究项目</span>
            <strong>静息态功能连接研究</strong>
          </div>
          <div className="topbar-actions">
            <StatusPill tone="info">{dpabiLabel}</StatusPill>
            <button className="avatar" type="button" aria-label="本地用户">HQ</button>
          </div>
        </header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}
