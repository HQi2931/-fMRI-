import { Layout } from "./components/Layout";
import { useApiHealth } from "./hooks/useApiHealth";
import { AgentPage } from "./pages/AgentPage";
import { AnalysisPage } from "./pages/AnalysisPage";
import { Dashboard } from "./pages/Dashboard";
import { DataPage } from "./pages/DataPage";
import { PlanPage } from "./pages/PlanPage";
import { QcPage } from "./pages/QcPage";
import { RunsPage } from "./pages/RunsPage";
import { usePathname } from "./routing";
import { SettingsPage } from "./pages/SettingsPage";
import { StatisticsPage } from "./pages/StatisticsPage";
import { api, type EnvironmentConfig } from "./api/client";
import { Link } from "./routing";
import { useEffect, useState } from "react";

function EnvironmentSetupPrompt({ hidden, config }: { hidden: boolean; config: EnvironmentConfig | null }) {

  if (hidden || !config || config.configured) return null;
  return (
    <section className="environment-setup-prompt" aria-label="本机环境首次配置提示">
      <div>
        <span className="eyebrow">首次使用</span>
        <strong>请先选择本机 MATLAB / SPM / DPABI</strong>
        <p>软件版本不作统一兼容承诺；请在本机配置页面填写实际安装位置，保存后系统会探测受控入口。</p>
      </div>
      <Link className="button button-primary" to="/settings">去选择路径</Link>
    </section>
  );
}

export default function App() {
  const connection = useApiHealth();
  const pathname = usePathname();
  const [environmentConfig, setEnvironmentConfig] = useState<EnvironmentConfig | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.environmentConfig(controller.signal).then(setEnvironmentConfig).catch(() => undefined);
    return () => controller.abort();
  }, []);
  const pages: Record<string, React.ReactNode> = {
    "/": <Dashboard />,
    "/data": <DataPage />,
    "/plan": <PlanPage />,
    "/runs": <RunsPage />,
    "/qc": <QcPage />,
    "/statistics": <StatisticsPage />,
    "/agent": <AgentPage />,
    "/analysis": <AnalysisPage />,
    "/settings": <SettingsPage />,
  };
  return (
    <Layout
      connection={connection}
      dpabiLabel={environmentConfig?.configured ? `DPABI ${environmentConfig.dpabi_version}` : "DPABI 未配置"}
    >
      <EnvironmentSetupPrompt hidden={pathname === "/settings"} config={environmentConfig} />
      {pages[pathname] ?? <Dashboard />}
    </Layout>
  );
}
