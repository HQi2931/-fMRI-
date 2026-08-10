import { useCallback, useEffect, useState } from "react";

import { api, describeError, type EnvironmentProbe, type ModelProfile } from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";

export function SettingsPage() {
  const [environment, setEnvironment] = useState<EnvironmentProbe | null>(null);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [profileId, setProfileId] = useState("deepseek-default");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com");
  const [model, setModel] = useState("");
  const [keyEnv, setKeyEnv] = useState("DEEPSEEK_API_KEY");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [probe, configured] = await Promise.all([api.environment(signal), api.profiles(signal)]);
    setEnvironment(probe);
    setProfiles(configured);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [refresh]);

  async function saveProfile(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      await api.createProfile({
        id: profileId.trim(),
        provider: "openai-compatible",
        base_url: baseUrl.trim(),
        model: model.trim(),
        api_key_env: keyEnv.trim(),
        priority: 10,
        capabilities: ["json_object"],
        timeout_seconds: 45,
      });
      await refresh();
      setMessage("模型配置元数据已保存；API Key 本身未进入数据库或浏览器。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function testProfile(item: ModelProfile): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const result = await api.testProvider({ profile_id: item.profile.id, expected_profile_version: item.version });
      setMessage(result.available ? `Provider ${item.profile.id} 轻量测试成功。` : `Provider ${item.profile.id} 当前不可用。`);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="本机环境"
        title="运行条件与模型配置"
        description="接口只返回可用性与版本证据，不返回绝对安装路径、API Key 或环境变量值。"
        action={<button className="button button-secondary" type="button" disabled={busy} onClick={() => refresh().catch((caught) => setError(describeError(caught)))}>重新探测</button>}
      />
      <Feedback message={error || message} error={Boolean(error)} />
      <section className="panel environment-list">
        {environment?.components.map((component) => (
          <div key={component.name}>
            <span className="environment-icon" aria-hidden="true">{component.name.slice(0, 1).toUpperCase()}</span>
            <div><strong>{component.name}</strong><p>{component.evidence ?? "未发现安全的版本证据"}</p></div>
            <StatusPill tone={component.available ? "good" : "warn"}>{component.available ? "可用" : "待配置"}</StatusPill>
          </div>
        ))}
        {!environment && <EmptyState title="等待环境探测" detail="后端连接后会显示 MATLAB、SPM、DPABI 与工作目录的安全摘要。" />}
      </section>
      <div className="two-column">
        <section className="panel">
          <span className="eyebrow">Provider 配置</span><h2>OpenAI-compatible</h2>
          <div className="parameter-list">
            <label>配置 ID<input value={profileId} onChange={(event) => setProfileId(event.target.value)} /></label>
            <label>API 基址<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
            <label>模型名称<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="按 Provider 当前文档在本机填写" /></label>
            <label>密钥环境变量名<input value={keyEnv} onChange={(event) => setKeyEnv(event.target.value)} /></label>
          </div>
          <p className="muted">界面只提交环境变量的名称；密钥值必须写在未跟踪的 <code>.env</code> 中。</p>
          <div className="button-row"><button className="button button-primary" type="button" disabled={busy || !model.trim()} onClick={saveProfile}>保存非敏感配置</button></div>
        </section>
        <section className="panel">
          <span className="eyebrow">已配置模型</span><h2>{profiles.length} 项</h2>
          {profiles.length === 0 ? <EmptyState title="尚无模型配置" detail="Agent 功能保持关闭；数据、Skill 与 Workflow 不依赖外部模型。" /> : (
            <div className="selection-list">{profiles.map((item) => (
              <button type="button" key={item.profile.id} disabled={busy} onClick={() => testProfile(item)}>
                <span>{item.profile.id}<small>{item.profile.model}</small></span><StatusPill tone="info">测试</StatusPill>
              </button>
            ))}</div>
          )}
        </section>
      </div>
    </>
  );
}
