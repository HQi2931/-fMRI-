# Provider 配置与 smoke

外部模型是可选能力。未配置 Provider 时，数据检查、Skill 编译、审批、Mock Worker、QC 和统计设计仍可独立使用。

## 安全边界

- API Key 只保存在未跟踪的本地 `.env`，不写入 Profile、SQLite、日志、前端响应或 Git。
- Profile 只保存 `api_key_env`，例如 `DEEPSEEK_API_KEY`；名称必须以 `_API_KEY` 结尾。
- 必须配置至少 16 字符的私有 `RSFMRI_REDACTION_SALT`，用于稳定伪名化。
- `OutboundContextPolicy` 在调用前删除路径、联系方式、密钥和原始 subject ID，并对临床/人口学自由文本失败关闭。
- Provider 返回必须通过结构 schema；只允许同一 Provider 修复一次格式。
- fallback 只用于网络、限流或服务不可用，不用于规避科学校验或内容拒绝。

## 配置本地密钥

在 `.env` 中设置本地值，不要把示例占位符当作真实密钥：

```dotenv
RSFMRI_REDACTION_SALT=<local-random-value-at-least-16-characters>
DEEPSEEK_API_KEY=<local-secret>
OPENAI_COMPATIBLE_API_KEY=<local-secret>
```

只需配置实际使用的 Provider。修改 `.env` 后重启 API，使进程读取新环境。

## 创建 ModelProfile

推荐在中文 Web 工作台的“模型设置”页创建 Profile。也可调用 `POST /api/v1/model-profiles`：

```json
{
  "id": "local-compatible-profile",
  "provider": "openai-compatible",
  "base_url": "https://provider.example/v1",
  "model": "configure-a-provider-supported-model",
  "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
  "priority": 20,
  "capabilities": ["json_object"],
  "timeout_seconds": 45
}
```

要求：

- 非 localhost 端点必须使用 HTTPS，URL 不能携带用户名、密码或 query 密钥。
- `model` 由使用者按当前 Provider 账户可用模型显式填写，项目不硬编码模型名称。
- 优先级数值越小越先尝试。显式 `preferred_profile_id` 时只使用指定 Profile。
- 自动路由按请求能力、Profile 可用性和优先级选择。当前没有从文件加载按任务类型写死的路由表。

[ModelProfile 示例](../../config/model-profiles.example.json) 只是人工参考，不会被 API 自动加载。请通过 UI 或 API 逐个创建 `profiles` 数组中的条目。

## DeepSeek 官方接口来源

DeepSeek 配置和兼容性核对以官方 [Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion/) 与 [JSON Output 指南](https://api-docs.deepseek.com/guides/json_mode/) 为参考。当前项目通过可配置的 OpenAI-compatible adapter 调用，不硬编码模型名称；实际可用的模型、端点和 JSON 能力仍须按当前账户配置并通过用户显式触发的轻量 smoke 验证。

## 轻量 smoke

Provider smoke 会向外部服务发送一个已脱敏的连通性和 JSON schema 测试，可能产生费用。它只能由用户显式点击“测试”或调用 `POST /api/v1/providers/test` 触发，CI 不会使用真实密钥。

smoke 前确认：

1. `.env` 中的 Key 和 `RSFMRI_REDACTION_SALT` 已设置。
2. Profile 的 `base_url`、`model`、`api_key_env` 和能力已人工核对。
3. 测试摘要不包含真实数据、受试者信息或本机路径。

成功响应只证明当前 Profile 可连通且能返回合法结构，不证明科学建议正确，也不授权启动 Workflow 或 MATLAB。

## 故障处理

- `outbound_context_rejected`：摘要无法证明已脱敏。删减或结构化输入，不要降低策略。
- `model_route_unavailable`：没有具备所需能力且密钥可用的 Profile。
- `model_gateway_unavailable`：连通性、限流、Provider 错误或输出 schema 修复失败。

不要把 Key 加入请求体、Profile、URL、错误截图或日志。如果 Key 可能泄漏，先在 Provider 侧撤销，再停止自动发布并评估 Git 历史。
