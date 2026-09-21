import { Layout } from "./components/Layout";
import { useApiHealth } from "./hooks/useApiHealth";
import { AgentPage } from "./pages/AgentPage";
import { SettingsPage } from "./pages/SettingsPage";
import { usePathname } from "./routing";

export default function App() {
  const connection = useApiHealth();
  const pathname = usePathname();
  return (
    <Layout connection={connection}>
      {pathname === "/settings"
        ? <SettingsPage />
        : <AgentPage />}
    </Layout>
  );
}
