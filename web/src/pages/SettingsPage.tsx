import { useCallback, useEffect, useState } from "react";

import { api, describeError, type EnvironmentProbe, type ModelProfile } from "../api/client";
import { EmptyState, Feedback, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";

const CAPABILITIES = ["json_object", "streaming", "reasoning"] as const;
type Capability = (typeof CAPABILITIES)[number];

const CAPABILITY_LABELS: Record<Capability, string> = {
  json_object: "JSON 结构化输出",
  streaming: "流式输出",
  reasoning: "推理",
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
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [profileId, setProfileId] = useState("");
  const [presetLabel, setPresetLabel] = useState("");
  const [provider, setProvider] = useState("openai-compatible");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [keyEnv, setKeyEnv] = useState("");
  const [model, setModel] = useState("");
  const [pickedModel, setPickedModel] = useState("");
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [priority, setPriority] = useState(100);
  const [capabilities, setCapabilities] = useState<Capability[]>(["json_object"]);
  const [timeoutSeconds, setTimeoutSeconds] = useState(45);
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

  function applyPreset(label: string): void {
    setPresetLabel(label);
    const preset = PROVIDER_PRESETS.find((item) => item.label === label);
    if (preset) {
      setProvider("openai-compatible");
      setBaseUrl(preset.baseUrl);
      setKeyEnv(preset.keyEnv);
      setAvailableModels([]);
      setPickedModel("");
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
    setProfileId("");
    setPresetLabel("");
    setBaseUrl("");
    setApiKey("");
    setKeyEnv("");
    setModel("");
    setPickedModel("");
    setAvailableModels([]);
  }

  async function fetchModels(): Promise<void> {
    setLoadingModels(true);
    setError("");
    setMessage("");
    try {
      const result = await api.listProviderModels({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || null,
        api_key_env: apiKey.trim() ? null : keyEnv.trim() || null,
      });
      setAvailableModels(result.models);
      setMessage(result.models.length > 0 ? `已获取 ${result.models.length} 个可用模型。` : "该服务商未返回可用模型。");
    } catch (caught) {
      setError(describeError(caught));
      setAvailableModels([]);
    } finally {
      setLoadingModels(false);
    }
  }

  async function saveProfile(): Promise<void> {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api.createProfile({
        profile: {
          id: profileId.trim(),
          provider: provider.trim(),
          base_url: baseUrl.trim(),
          model: (pickedModel || model).trim(),
          api_key_env: keyEnv.trim(),
          priority,
          capabilities,
          timeout_seconds: timeoutSeconds,
        },
        api_key: apiKey.trim() || null,
      });
      resetForm();
      await refresh();
      setMessage("模型配置已保存；API Key 已写入本地 .env，未进入数据库或浏览器。");
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
      const result = await api.testProvider({ profile_id: item.profile.id, expected_profile_version: item.version });
      setMessage(result.available ? `Provider ${item.profile.id} 轻量测试成功。` : `Provider ${item.profile.id} 当前不可用。`);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function removeProfile(item: ModelProfile): Promise<void> {
    if (!window.confirm(`确定删除模型配置「${item.profile.id}」（${item.profile.model}）吗？`)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api.deleteProfile(item.profile.id);
      await refresh();
      setMessage(`已删除模型配置 ${item.profile.id}。`);
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
        description="选择服务商、填写 API Key 即可拉取该服务商的可用模型；可配置多个模型，Agent 按任务能力与优先级路由。"
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
          <span className="eyebrow">新增模型配置</span><h2>OpenAI-compatible</h2>
          <div className="parameter-list">
            <label>配置 ID<input value={profileId} onChange={(event) => setProfileId(event.target.value)} placeholder="如 zhipu-glm、kimi-k2（小写字母/数字/连字符）" /></label>
            <label>服务商预设
              <select value={presetLabel} onChange={(event) => applyPreset(event.target.value)}>
                <option value="">选择服务商（自动填基址与密钥变量名）</option>
                {PROVIDER_PRESETS.map((preset) => <option key={preset.label} value={preset.label}>{preset.label}</option>)}
              </select>
            </label>
            <label>API Key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="填写后可直接拉取模型；保存时写入本地 .env（不进入数据库）" autoComplete="off" /></label>
            <label>Provider<select value={provider} onChange={(event) => setProvider(event.target.value)}><option value="openai-compatible">openai-compatible</option></select></label>
            <label>API 基址<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" /></label>
            <div className="model-field">
              <div className="model-pick-row">
                <select value={pickedModel} onChange={(event) => setPickedModel(event.target.value)} disabled={loadingModels} aria-label="模型名称">
                  <option value="">{availableModels.length > 0 ? "从列表选择（或下方手动输入）" : "先点「获取模型」"}</option>
                  {availableModels.map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
                <button type="button" className="button button-secondary" onClick={fetchModels} disabled={busy || loadingModels || !baseUrl.trim()}>{loadingModels ? "获取中…" : "获取模型"}</button>
              </div>
              <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="手动输入模型名（下拉为空或需自定义时）" aria-label="模型名称（手动输入）" />
            </div>
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
          <p className="muted">API Key 只写入未跟踪的本地 <code>.env</code>，不进入数据库、日志或浏览器状态；不填 Key 时使用 <code>.env</code> 中已配置的同名变量。</p>
          <div className="button-row"><button className="button button-primary" type="button" disabled={busy || !profileId.trim() || !baseUrl.trim() || !keyEnv.trim() || (!pickedModel && !model.trim())} onClick={saveProfile}>保存模型配置</button></div>
        </section>
        <section className="panel">
          <span className="eyebrow">已配置模型</span><h2>{profiles.length} 项</h2>
          {profiles.length === 0 ? <EmptyState title="尚无模型配置" detail="Agent 功能保持关闭；数据、Skill 与 Workflow 不依赖外部模型。" /> : (
            <div className="profile-list">
              {profiles.map((item) => (
                <div key={item.profile.id} className="profile-card">
                  <div className="profile-card-head">
                    <strong>{item.profile.model}</strong>
                    <StatusPill tone="info">{item.profile.id}</StatusPill>
                  </div>
                  <p className="muted">{item.profile.provider} · {item.profile.base_url}</p>
                  <p className="muted">密钥 {item.profile.api_key_env} · 优先级 {item.profile.priority} · 能力 {item.profile.capabilities.length > 0 ? item.profile.capabilities.join("、") : "无"}</p>
                  <div className="button-row">
                    <button className="button button-secondary" type="button" disabled={busy} onClick={() => testProfile(item)}>测试</button>
                    <button className="button button-danger" type="button" disabled={busy} onClick={() => removeProfile(item)}>删除</button>
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
