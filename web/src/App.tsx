import { Layout } from "./components/Layout";
import { useApiHealth } from "./hooks/useApiHealth";
import { AgentPage } from "./pages/AgentPage";
import { SettingsPage } from "./pages/SettingsPage";
import { businessCapabilityForPath, usePathname, WorkRoutingProvider } from "./routing";

export default function App() {
  const connection = useApiHealth();
  const pathname = usePathname();
  return (
    <Layout connection={connection}>
      {pathname === "/settings"
        ? <SettingsPage />
        : (
          <WorkRoutingProvider>
            <AgentPage initialCapability={businessCapabilityForPath(pathname)} />
          </WorkRoutingProvider>
        )}
    </Layout>
  );
}
