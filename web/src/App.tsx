import { Layout } from "./components/Layout";
import { useApiHealth } from "./hooks/useApiHealth";
import { AgentPage } from "./pages/AgentPage";
import { Dashboard } from "./pages/Dashboard";
import { DataPage } from "./pages/DataPage";
import { PlanPage } from "./pages/PlanPage";
import { QcPage } from "./pages/QcPage";
import { RunsPage } from "./pages/RunsPage";
import { usePathname } from "./routing";
import { SettingsPage } from "./pages/SettingsPage";
import { StatisticsPage } from "./pages/StatisticsPage";

export default function App() {
  const connection = useApiHealth();
  const pathname = usePathname();
  const pages: Record<string, React.ReactNode> = {
    "/": <Dashboard />,
    "/data": <DataPage />,
    "/plan": <PlanPage />,
    "/runs": <RunsPage />,
    "/qc": <QcPage />,
    "/statistics": <StatisticsPage />,
    "/agent": <AgentPage />,
    "/settings": <SettingsPage />,
  };
  return (
    <Layout connection={connection}>
      {pages[pathname] ?? <Dashboard />}
    </Layout>
  );
}
