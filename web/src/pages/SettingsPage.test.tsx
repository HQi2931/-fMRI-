import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsPage } from "./SettingsPage";

const now = "2026-09-18T00:00:00Z";

function json(value: unknown, status = 200): Promise<Response> {
  const body = status === 204 ? null : JSON.stringify(value);
  return Promise.resolve(
    new Response(body, { status, headers: { "Content-Type": "application/json" } }),
  );
}

function pathOf(input: RequestInfo | URL): string {
  return new URL(
    typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
    "http://local",
  ).pathname;
}

function probe() {
  return {
    ready: false,
    environment_hash: "e".repeat(64),
    components: [
      { name: "MATLAB R2024b", available: true, evidence: "entry point fingerprint sha256:abc" },
      { name: "DPABI V9.0_250415", available: false, evidence: null },
    ],
  };
}

function environmentConfig(overrides: Record<string, unknown> = {}) {
  return {
    matlab_executable: null,
    spm_dir: null,
    dpabi_dir: null,
    matlab_version: "unspecified",
    spm_version: "unspecified",
    dpabi_version: "unspecified",
    configured: false,
    ...overrides,
  };
}

function modelProfile(id = "deepseek", baseUrl = "https://api.deepseek.com") {
  return {
    profile: {
      id,
      provider: "openai-compatible",
      base_url: baseUrl,
      model: "deepseek-chat",
      api_key_env: "DEEPSEEK_API_KEY",
      priority: 100,
      capabilities: ["json_object"],
      timeout_seconds: 45,
      context_window_tokens: 16384,
      max_output_tokens: 2048,
    },
    created_at: now,
  };
}

const notFound = () =>
  json({ error: { code: "not_found", message: "missing", details: {}, trace_id: null } }, 404);

type Overrides = {
  probe?: () => Promise<Response>;
  getConfig?: () => Promise<Response>;
  saveConfig?: () => Promise<Response>;
  profiles?: () => Promise<Response>;
  listModels?: () => Promise<Response>;
  deleteProfile?: () => Promise<Response>;
};

function installFetch(overrides: Overrides = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const path = pathOf(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/environment/probe")) return (overrides.probe ?? (() => json(probe())))();
    if (path.endsWith("/environment/config")) {
      if (method === "PUT") {
        return (overrides.saveConfig ?? (() => json(environmentConfig({ configured: true }))))();
      }
      return (overrides.getConfig ?? (() => json(environmentConfig())))();
    }
    if (path.endsWith("/providers/models")) {
      return (
        overrides.listModels ?? (() => json({ models: ["deepseek-chat", "deepseek-reasoner"] }))
      )();
    }
    if (path.endsWith("/model-profiles")) {
      if (method === "POST") return json(modelProfile());
      return (overrides.profiles ?? (() => json([])))();
    }
    if (path.includes("/model-profiles/") && method === "DELETE") {
      return (overrides.deleteProfile ?? (() => json(null, 204)))();
    }
    return notFound();
  });
}

async function fillEnvironment(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("MATLAB 可执行文件"), "D:/Matlab/bin/matlab.exe");
  await user.type(screen.getByLabelText("SPM 目录"), "D:/Matlab/toolbox/spm");
  await user.type(screen.getByLabelText("DPABI 目录"), "D:/Matlab/toolbox/DPABI");
}

describe("settings page", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("loads the environment probe and prefills the saved local paths", async () => {
    installFetch({
      getConfig: () =>
        json(
          environmentConfig({
            matlab_executable: "D:/Matlab/bin/matlab.exe",
            spm_dir: "D:/Matlab/toolbox/spm",
            dpabi_dir: "D:/Matlab/toolbox/DPABI",
            matlab_version: "R2024b",
            spm_version: "SPM25",
            dpabi_version: "V9.0_250415",
            configured: true,
          }),
        ),
    });

    render(<SettingsPage />);

    expect(await screen.findByText("MATLAB R2024b")).toBeInTheDocument();
    expect(screen.getByText("DPABI V9.0_250415")).toBeInTheDocument();
    // A component without evidence falls back to the explicit placeholder.
    expect(screen.getByText("未发现安全的版本证据")).toBeInTheDocument();
    expect(screen.getByText("已配置")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText("MATLAB 可执行文件")).toHaveValue("D:/Matlab/bin/matlab.exe"),
    );
    expect(screen.getByLabelText("DPABI 版本标签")).toHaveValue("V9.0_250415");
  });

  it("shows empty states before an environment or provider exists", async () => {
    // A failed probe leaves every section unpopulated, which is the first-run
    // state: no probe, no local config, no bound provider.
    installFetch({
      probe: () =>
        json(
          {
            error: {
              code: "environment_unavailable",
              message: "环境探测失败",
              details: {},
              trace_id: null,
            },
          },
          503,
        ),
    });

    render(<SettingsPage />);

    expect(await screen.findByText("等待环境探测")).toBeInTheDocument();
    expect(screen.getByText("尚未绑定服务商 API")).toBeInTheDocument();
    expect(screen.getByText("待配置")).toBeInTheDocument();
    expect(screen.getByText("环境探测失败")).toBeInTheDocument();
  });

  it("saves the local paths and reports that versions are evidence only", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch();

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    await fillEnvironment(user);
    await user.click(screen.getByRole("button", { name: "保存并重新探测" }));

    expect(await screen.findByText(/本机 MATLAB\/SPM\/DPABI 路径已保存/)).toBeInTheDocument();
    expect(screen.getByText(/不作硬编码兼容承诺/)).toBeInTheDocument();
    const put = fetchSpy.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toMatchObject({
      matlab_executable: "D:/Matlab/bin/matlab.exe",
      spm_dir: "D:/Matlab/toolbox/spm",
      dpabi_dir: "D:/Matlab/toolbox/DPABI",
      // Blank labels fall back to the explicit "unspecified" evidence value.
      matlab_version: "unspecified",
      dpabi_version: "unspecified",
    });
  });

  it("surfaces a failed environment save and clears the busy state", async () => {
    const user = userEvent.setup();
    installFetch({
      saveConfig: () =>
        json(
          {
            error: {
              code: "path_not_found",
              message: "MATLAB 可执行文件不存在",
              details: {},
              trace_id: "t-1",
            },
          },
          422,
        ),
    });

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    await fillEnvironment(user);
    await user.click(screen.getByRole("button", { name: "保存并重新探测" }));

    expect(await screen.findByText(/MATLAB 可执行文件不存在/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存并重新探测" })).toBeEnabled(),
    );
  });

  it("applies a provider preset to the base url and key variable", async () => {
    const user = userEvent.setup();
    installFetch();

    render(<SettingsPage />);
    await user.selectOptions(screen.getByLabelText("服务商预设"), "DeepSeek 深度求索");

    expect(screen.getByLabelText("API 基址")).toHaveValue("https://api.deepseek.com");
    expect(screen.getByLabelText("密钥环境变量名")).toHaveValue("DEEPSEEK_API_KEY");
  });

  it("toggles capabilities on and off", async () => {
    const user = userEvent.setup();
    installFetch();

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    const webSearch = screen.getByLabelText("联网搜索（OpenAI-compatible）");
    expect(webSearch).not.toBeChecked();

    await user.click(webSearch);
    expect(webSearch).toBeChecked();

    await user.click(webSearch);
    expect(webSearch).not.toBeChecked();
  });

  it("binds a provider, derives the id from the key variable and reloads profiles", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch({
      profiles: () => json([modelProfile("deepseek")]),
    });

    render(<SettingsPage />);
    await screen.findByText("1 项");
    await user.selectOptions(screen.getByLabelText("服务商预设"), "DeepSeek 深度求索");
    await user.type(screen.getByLabelText("API Key"), "sk-synthetic");
    await user.click(screen.getByRole("button", { name: "绑定 API 并读取模型" }));

    expect(await screen.findByText(/服务商 API 已绑定/)).toBeInTheDocument();
    expect(screen.getByText(/可直接选择其返回的 2 个模型/)).toBeInTheDocument();
    const create = fetchSpy.mock.calls.find(
      ([input, init]) => pathOf(input).endsWith("/model-profiles") && init?.method === "POST",
    );
    expect(JSON.parse(String(create?.[1]?.body))).toMatchObject({
      profile: {
        id: "deepseek",
        provider: "openai-compatible",
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        api_key_env: "DEEPSEEK_API_KEY",
      },
      api_key: "sk-synthetic",
    });
  });

  it("refuses to bind a provider that returns no models", async () => {
    const user = userEvent.setup();
    installFetch({ listModels: () => json({ models: [] }) });

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    await user.selectOptions(screen.getByLabelText("服务商预设"), "DeepSeek 深度求索");
    await user.type(screen.getByLabelText("API Key"), "sk-synthetic");
    await user.click(screen.getByRole("button", { name: "绑定 API 并读取模型" }));

    expect(await screen.findByText("该服务商没有返回可选择的模型。")).toBeInTheDocument();
  });

  it("reports the model count when testing an existing binding", async () => {
    const user = userEvent.setup();
    installFetch({ profiles: () => json([modelProfile()]) });

    render(<SettingsPage />);
    await screen.findByText("1 项");
    await user.click(screen.getByRole("button", { name: "刷新模型列表" }));

    expect(await screen.findByText(/服务商连接正常，当前返回 2 个可选模型/)).toBeInTheDocument();
  });

  it("reports a failed binding test", async () => {
    const user = userEvent.setup();
    installFetch({
      profiles: () => json([modelProfile()]),
      listModels: () =>
        json(
          {
            error: {
              code: "provider_unavailable",
              message: "服务商暂时不可用",
              details: {},
              trace_id: null,
            },
          },
          503,
        ),
    });

    render(<SettingsPage />);
    await screen.findByText("1 项");
    await user.click(screen.getByRole("button", { name: "刷新模型列表" }));

    expect(await screen.findByText("服务商暂时不可用")).toBeInTheDocument();
  });

  it("keeps the binding when the user cancels the confirmation", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const fetchSpy = installFetch({ profiles: () => json([modelProfile()]) });

    render(<SettingsPage />);
    await screen.findByText("1 项");
    await user.click(screen.getByRole("button", { name: "解除绑定" }));

    expect(confirm).toHaveBeenCalled();
    expect(
      fetchSpy.mock.calls.some(([, init]) => init?.method === "DELETE"),
    ).toBe(false);
    expect(screen.getByText("1 项")).toBeInTheDocument();
  });

  it("removes a binding after confirmation", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let profiles: unknown[] = [modelProfile()];
    installFetch({
      profiles: () => json(profiles),
      deleteProfile: () => {
        profiles = [];
        return json(null, 204);
      },
    });

    render(<SettingsPage />);
    await screen.findByText("1 项");
    await user.click(screen.getByRole("button", { name: "解除绑定" }));

    expect(await screen.findByText("已解除服务商 API 绑定。")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("0 项")).toBeInTheDocument());
  });

  it("re-probes the environment from the header action", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch();

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    vi.mocked(globalThis.fetch).mockClear();

    await user.click(screen.getByRole("button", { name: "重新探测" }));

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(([input]) => pathOf(input).endsWith("/environment/probe")),
      ).toBe(true),
    );
  });

  it("submits the version labels as local evidence only", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch();

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    await fillEnvironment(user);
    for (const [label, value] of [
      ["MATLAB 版本标签", "R2024b"],
      ["SPM 版本标签", "SPM25"],
      ["DPABI 版本标签", "V9.0_250415"],
    ] as const) {
      const input = screen.getByLabelText(label);
      await user.clear(input);
      await user.type(input, value);
    }
    await user.click(screen.getByRole("button", { name: "保存并重新探测" }));

    await screen.findByText(/本机 MATLAB\/SPM\/DPABI 路径已保存/);
    const put = fetchSpy.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toMatchObject({
      matlab_version: "R2024b",
      spm_version: "SPM25",
      dpabi_version: "V9.0_250415",
    });
  });

  it("binds every configured field the operator can edit", async () => {
    const user = userEvent.setup();
    const fetchSpy = installFetch();

    render(<SettingsPage />);
    await screen.findByText("MATLAB R2024b");
    await user.type(screen.getByLabelText("API Key"), "sk-synthetic");
    await user.type(screen.getByLabelText("API 基址"), "https://api.deepseek.com");
    await user.type(screen.getByLabelText("密钥环境变量名"), "DEEPSEEK_API_KEY");
    await user.click(screen.getByLabelText("联网搜索（OpenAI-compatible）"));
    for (const [label, value] of [
      ["优先级", "7"],
      ["超时秒", "30"],
      ["上下文窗口", "8192"],
      ["回答 token 上限", "1024"],
    ] as const) {
      const input = screen.getByLabelText(label);
      await user.clear(input);
      await user.type(input, value);
    }
    await user.click(screen.getByRole("button", { name: "绑定 API 并读取模型" }));

    await screen.findByText(/服务商 API 已绑定/);
    const create = fetchSpy.mock.calls.find(
      ([input, init]) => pathOf(input).endsWith("/model-profiles") && init?.method === "POST",
    );
    expect(JSON.parse(String(create?.[1]?.body))).toMatchObject({
      profile: {
        id: "deepseek",
        provider: "openai-compatible",
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        api_key_env: "DEEPSEEK_API_KEY",
        priority: 7,
        timeout_seconds: 30,
        context_window_tokens: 8192,
        max_output_tokens: 1024,
        capabilities: ["json_object", "web_search"],
      },
      api_key: "sk-synthetic",
    });
  });
});
