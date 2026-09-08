import { useCallback, useEffect, useState } from "react";

import { api, describeError, type EnvironmentConfig, type EnvironmentProbe, type ModelProfile } from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";

const CAPABILITIES = ["json_object", "streaming", "reasoning", "web_search"] as const;
type Capability = (typeof CAPABILITIES)[number];

const CAPABILITY_LABELS: Record<Capability, string> = {
  json_object: "JSON 结构化输出",
  streaming: "流式输出",
  reasoning: "推理",
  web_search: "联网搜索（OpenAI-compatible）",
};

const PROVIDER_PRESETS = [
  { label: "DeepSeek 深度求索", baseUrl: "https://api.deepseek.com", keyEnv: "DEEPSEEK_API_KEY" },
  { label: "智谱 ZAI（国内 Coding）", baseUrl: "https://open.bigmodel.cn/api/coding/paas/v4", keyEnv: "ZHIPU_API_KEY" },
  { label: "智谱 ZAI（海外 Coding）", baseUrl: "https://api.z.ai/api/coding/paas/v4", keyEnv: "ZAI_API_KEY" },
  { label: "月之暗面 Kimi（国内）", baseUrl: "https://api.moonshot.cn/v1", keyEnv: "MOONSHOT_API_KEY" },
  { label: "月之暗面 Kimi（海外）", baseUrl: "https://api.moonshot.ai/v1", keyEnv: "MOONSHOT_API_KEY" },
  { label: "通义千问 Qwen（国内）", baseUrl: "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", keyEnv: "QWEN_API_KEY" },
  { label: "通义千问 Qwen（国际）", baseUrl: "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", keyEnv: "QWEN_API_KEY" },
  { label: "小米 MiMo", baseUrl: "https://api.xiaomimimo.com/v1", keyEnv: "XIAOMI_API_KEY" },
  { label: "蚂蚁灵语 Ant Ling", baseUrl: "https://api.ant-ling.com/v1", keyEnv: "ANT_LING_API_KEY" },
  { label: "OpenAI", baseUrl: "https://api.openai.com/v1", keyEnv: "OPENAI_API_KEY" },
  { label: "xAI Grok", baseUrl: "https://api.x.ai/v1", keyEnv: "XAI_API_KEY" },
  { label: "Groq", baseUrl: "https://api.groq.com/openai/v1", keyEnv: "GROQ_API_KEY" },
  { label: "Together AI", baseUrl: "https://api.together.ai/v1", keyEnv: "TOGETHER_API_KEY" },
  { label: "Fireworks AI", baseUrl: "https://api.fireworks.ai/inference/v1", keyEnv: "FIREWORKS_API_KEY" },
  { label: "Cerebras", baseUrl: "https://api.cerebras.ai/v1", keyEnv: "CEREBRAS_API_KEY" },
  { label: "NVIDIA NIM", baseUrl: "https://integrate.api.nvidia.com/v1", keyEnv: "NVIDIA_API_KEY" },
  { label: "Hugging Face", baseUrl: "https://router.huggingface.co/v1", keyEnv: "HF_API_KEY" },
  { label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1", keyEnv: "OPENROUTER_API_KEY" },
  { label: "GitHub Copilot", baseUrl: "https://api.individual.githubcopilot.com", keyEnv: "GITHUB_COPILOT_API_KEY" },
  { label: "自定义 OpenAI 兼容", baseUrl: "", keyEnv: "" },
] as const;

export function SettingsPage() {
  const [environment, setEnvironment] = useState<EnvironmentProbe | null>(null);
  const [environmentConfig, setEnvironmentConfig] = useState<EnvironmentConfig | null>(null);
  const [matlabExecutable, setMatlabExecutable] = useState("");
  const [spmDir, setSpmDir] = useState("");
  const [dpabiDir, setDpabiDir] = useState("");
  const [matlabVersion, setMatlabVersion] = useState("unspecified");
  const [spmVersion, setSpmVersion] = useState("unspecified");
  const [dpabiVersion, setDpabiVersion] = useState("unspecified");
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [presetLabel, setPresetLabel] = useState("");
  const [provider, setProvider] = useState("openai-compatible");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [keyEnv, setKeyEnv] = useState("");
  const [priority, setPriority] = useState(100);
  const [capabilities, setCapabilities] = useState<Capability[]>(["json_object"]);
  const [timeoutSeconds, setTimeoutSeconds] = useState(45);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [probe, localConfig, configured] = await Promise.all([
      api.environment(signal),
      api.environmentConfig(signal),
      api.profiles(signal),
    ]);
    setEnvironment(probe);
    setEnvironmentConfig(localConfig);
    setMatlabExecutable(localConfig.matlab_executable ?? "");
    setSpmDir(localConfig.spm_dir ?? "");
    setDpabiDir(localConfig.dpabi_dir ?? "");
    setMatlabVersion(localConfig.matlab_version);
    setSpmVersion(localConfig.spm_version);
    setDpabiVersion(localConfig.dpabi_version);
    setProfiles(configured);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [refresh]);

  async function saveEnvironmentConfig(): Promise<void> {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const saved = await api.updateEnvironmentConfig({
        matlab_executable: matlabExecutable.trim() || null,
        spm_dir: spmDir.trim() || null,
        dpabi_dir: dpabiDir.trim() || null,
        matlab_version: matlabVersion.trim() || "unspecified",
        spm_version: spmVersion.trim() || "unspecified",
        dpabi_version: dpabiVersion.trim() || "unspecified",
      });
      setEnvironmentConfig(saved);
      await refresh();
      setMessage("本机 MATLAB/SPM/DPABI 路径已保存，并已重新探测。版本文本仅作为本机证据标签，不作硬编码兼容承诺。");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  function applyPreset(label: string): void {
    setPresetLabel(label);
    const preset = PROVIDER_PRESETS.find((item) => item.label === label);
    if (preset) {
      setProvider("openai-compatible");
      setBaseUrl(preset.baseUrl);
      setKeyEnv(preset.keyEnv);
    }
  }

  function toggleCapability(capability: Capability): void {
    setCapabilities((previous) =>
      previous.includes(capability)
        ? previous.filter((item) => item !== capability)
        : [...previous, capability],
    );
  }

  function resetForm(): void {
    setPresetLabel("");
    setBaseUrl("");
    setApiKey("");
    setKeyEnv("");
  }

  async function bindProvider(): Promise<void> {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const models = await api.listProviderModels({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || null,
        api_key_env: apiKey.trim() ? null : keyEnv.trim() || null,
      });
      if (models.models.length === 0) throw new Error("该服务商没有返回可选择的模型。");
      const existing = profiles.find((item) =>
        item.profile.base_url === baseUrl.trim().replace(/\/$/, "") &&
        item.profile.api_key_env === keyEnv.trim(),
      );
      const derivedId = keyEnv.trim().toLowerCase().replace(/_api_key$/, "").replace(/_/g, "-");
      const baseId = derivedId.length >= 2 ? derivedId : "provider";
      let connectionId = existing?.profile.id ?? baseId.slice(0, 63);
      let suffix = 2;
      while (!existing && profiles.some((item) => item.profile.id === connectionId)) {
        connectionId = `${baseId.slice(0, 60)}-${suffix}`;
        suffix += 1;
      }
      await api.createProfile({
        profile: {
          id: connectionId,
          provider: provider.trim(),
          base_url: baseUrl.trim(),
          model: models.models[0],
          api_key_env: keyEnv.trim(),
          priority,
          capabilities,
          timeout_seconds: timeoutSeconds,
        },
        api_key: apiKey.trim() || null,
      });
      resetForm();
      await refresh();
      setMessage(`服务商 API 已绑定；Agent 对话框可直接选择其返回的 ${models.models.length} 个模型。API Key 仅写入本地 .env。`);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function testProfile(item: ModelProfile): Promise<void> {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api.listProviderModels({
        base_url: item.profile.base_url,
        api_key: null,
        api_key_env: item.profile.api_key_env,
      });
      setMessage(`服务商连接正常，当前返回 ${result.models.length} 个可选模型。`);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function removeProfile(item: ModelProfile): Promise<void> {
    if (!window.confirm(`确定解除服务商「${item.profile.base_url}」的 API 绑定吗？`)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api.deleteProfile(item.profile.id);
      await refresh();
      setMessage("已解除服务商 API 绑定。");
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
        title="运行条件与服务商 API"
        description="绑定模型服务商的 API Key 后，Agent 对话框会直接加载并显示该服务商提供的模型，无需逐个创建模型配置。"
        action={<button className="button button-secondary" type="button" disabled={busy} onClick={() => refresh().catch((caught) => setError(describeError(caught)))}>重新探测</button>}
      />
      <Feedback message={error || message} error={Boolean(error)} />
      <section className="panel environment-setup-panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">首次配置</span>
            <h2>选择本机 MATLAB / SPM / DPABI</h2>
            <p>路径由用户在前端填写，后端只验证当前机器上的文件、目录和受控入口函数。不会要求所有用户都使用同一版本。</p>
          </div>
          <StatusPill tone={environmentConfig?.configured ? "good" : "warn"}>
            {environmentConfig?.configured ? "已配置" : "待配置"}
          </StatusPill>
        </div>
        <div className="form-grid">
          <label>MATLAB 可执行文件<input aria-label="MATLAB 可执行文件" value={matlabExecutable} onChange={(event) => setMatlabExecutable(event.target.value)} placeholder="如 D:\\Matlab\\bin\\matlab.exe" /></label>
          <label>SPM 目录<input aria-label="SPM 目录" value={spmDir} onChange={(event) => setSpmDir(event.target.value)} placeholder="如 D:\\Matlab\\toolbox\\spm" /></label>
          <label>DPABI 目录<input aria-label="DPABI 目录" value={dpabiDir} onChange={(event) => setDpabiDir(event.target.value)} placeholder="如 D:\\Matlab\\toolbox\\DPABI" /></label>
          <label>MATLAB 版本标签<input aria-label="MATLAB 版本标签" value={matlabVersion} onChange={(event) => setMatlabVersion(event.target.value)} placeholder="如 R2024b" /></label>
          <label>SPM 版本标签<input aria-label="SPM 版本标签" value={spmVersion} onChange={(event) => setSpmVersion(event.target.value)} placeholder="如 SPM12 / SPM25" /></label>
          <label>DPABI 版本标签<input aria-label="DPABI 版本标签" value={dpabiVersion} onChange={(event) => setDpabiVersion(event.target.value)} placeholder="如 V9.0_250415" /></label>
        </div>
        <p className="muted">路径必须由运行 API/Worker 的本机可访问；保存后会写入工作目录中的本地配置文件，不进入 Git、数据库事件或科学结果。</p>
        <div className="button-row"><button className="button button-primary" type="button" disabled={busy || !matlabExecutable.trim() || !spmDir.trim() || !dpabiDir.trim()} onClick={saveEnvironmentConfig}>保存并重新探测</button></div>
      </section>
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
          <span className="eyebrow">绑定服务商 API</span><h2>OpenAI-compatible</h2>
          <div className="parameter-list">
            <label>服务商预设
              <select value={presetLabel} onChange={(event) => applyPreset(event.target.value)}>
                <option value="">选择服务商（自动填基址与密钥变量名）</option>
                {PROVIDER_PRESETS.map((preset) => <option key={preset.label} value={preset.label}>{preset.label}</option>)}
              </select>
            </label>
            <label>API Key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="填写后可直接拉取模型；保存时写入本地 .env（不进入数据库）" autoComplete="off" /></label>
            <label>Provider<select value={provider} onChange={(event) => setProvider(event.target.value)}><option value="openai-compatible">openai-compatible</option></select></label>
            <label>API 基址<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" /></label>
            <label>密钥环境变量名<input value={keyEnv} onChange={(event) => setKeyEnv(event.target.value)} placeholder="以 _API_KEY 结尾，如 DEEPSEEK_API_KEY" /></label>
            <div className="form-grid">
              <label>优先级<input type="number" min={0} max={10000} value={priority} onChange={(event) => setPriority(Number(event.target.value))} /></label>
              <label>超时秒<input type="number" min={1} max={300} value={timeoutSeconds} onChange={(event) => setTimeoutSeconds(Number(event.target.value))} /></label>
            </div>
            <fieldset className="capability-field">
              <legend>能力</legend>
              {CAPABILITIES.map((capability) => (
                <label key={capability} className="check-field">
                  <input type="checkbox" checked={capabilities.includes(capability)} onChange={() => toggleCapability(capability)} />
                  {CAPABILITY_LABELS[capability]}
                </label>
              ))}
            </fieldset>
          </div>
          <p className="muted">绑定时会立即验证 API 并读取模型列表。API Key 只写入未跟踪的本地 <code>.env</code>，不进入数据库、日志或浏览器状态；不填 Key 时使用 <code>.env</code> 中已配置的同名变量。</p>
          <div className="button-row"><button className="button button-primary" type="button" disabled={busy || !baseUrl.trim() || !keyEnv.trim()} onClick={bindProvider}>{busy ? "正在绑定…" : "绑定 API 并读取模型"}</button></div>
        </section>
        <section className="panel">
          <span className="eyebrow">已绑定服务商</span><h2>{profiles.length} 项</h2>
          {profiles.length === 0 ? <EmptyState title="尚未绑定服务商 API" detail="绑定后，服务商提供的模型会直接出现在 Agent 对话框中。" /> : (
            <div className="profile-list">
              {profiles.map((item) => (
                <div key={item.profile.id} className="profile-card">
                  <div className="profile-card-head">
                    <strong>{new URL(item.profile.base_url).hostname}</strong>
                    <StatusPill tone="info">API 已绑定</StatusPill>
                  </div>
                  <p className="muted">{item.profile.provider} · {item.profile.base_url}</p>
                  <p className="muted">密钥 {item.profile.api_key_env} · 优先级 {item.profile.priority} · 能力 {item.profile.capabilities.length > 0 ? item.profile.capabilities.join("、") : "普通对话"}</p>
                  <div className="button-row">
                    <button className="button button-secondary" type="button" disabled={busy} onClick={() => testProfile(item)}>刷新模型列表</button>
                    <button className="button button-danger" type="button" disabled={busy} onClick={() => removeProfile(item)}>解除绑定</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </>
  );
}
