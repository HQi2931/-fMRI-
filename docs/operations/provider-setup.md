# Provider 配置与 smoke

- 状态：Implementing
- 文档类型：Operations
- 维护者：backend_service_engineer
- 更新日期：2026-10-22
- 关联：[模型数据外发策略](../security/data-egress.md)

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

### 能力声明与工具调用

`capabilities` 是服务端判断能否使用某项能力的唯一依据，声明错误会直接改变行为：

- `json_object`：Provider 支持 `response_format` 结构化输出。意图路由、参数草稿、记忆抽取都依赖它。
- `web_search`：允许联网搜索。用户每次都必须显式开启，且只会路由到声明了该能力的 Profile。
- `tool_use`：Profile 级能力声明，表示该连接下的模型支持 OpenAI 兼容的 `tools` / `tool_calls`。声明后可进入有界工具调用循环；用户仍需按实际情况确认该 Provider/模型支持，项目不会因返回了模型名称而自动推断能力。

```json
{
  "id": "tool-capable-profile",
  "provider": "openai-compatible",
  "base_url": "https://provider.example/v1",
  "model": "a-model-that-supports-function-calling",
  "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
  "priority": 20,
  "capabilities": ["json_object", "tool_use"],
  "timeout_seconds": 45
}
```

注意：

- 只有确认该 Provider 支持 function calling 才声明 `tool_use`。此配置会作用于 Profile 下列出的模型；具体 Work 文件授权会在用户点击授权时，向选中的具体模型发送一次仅含合成指令和无副作用工具的探针。只有模型按约定返回该工具调用，授权才会建立。探针不含工作区路径或文件内容，不执行工具。
- Work 授权绑定工作区、Profile 配置修订和具体模型。修改服务地址、模型配置或能力后必须重新授权；被撤销、过期配置或其他模型的授权不能用于读取工作区内容。
- 工具结果会重新进入模型上下文，因此工具输出同样经过外发脱敏策略处理，并被标记为不可信数据。
- 循环的步数与耗时由服务端预算控制，与 Profile 的 `timeout_seconds` 是两套独立限制，后者约束单次 HTTP 请求。

[ModelProfile 示例](../../config/model-profiles.example.json) 只是人工参考，不会被 API 自动加载。请通过 UI 或 API 逐个创建 `profiles` 数组中的条目。

### Work Agent 自动路由模型

自动路由先用本地规则处理明确请求；需要分类时默认调用小模型 Profile `dashscope`，默认模型为 `qwen3.8-flash`。低置信度、无效 JSON 或小模型调用失败时，再用现有 `deepseek` Profile 兜底。两者使用同一 OpenAI-compatible Gateway 和外发脱敏策略，路由模型只返回 `preprocessing`、`analysis` 或 `clarify`，不生成 Work 草稿。

在本机“设置 → 绑定服务商 API”完成百炼 Profile：

1. 选择“阿里云百炼 Model Studio”预设；或手工填写 `provider=openai-compatible`、`api_key_env=DASHSCOPE_API_KEY`。
2. 在本机设置页的密码框输入 API Key。它会由本机后端保存到未跟踪的 `.env`；不要把密钥粘贴到聊天。
3. 使用当前账号地域对应的 OpenAI-compatible Base URL。北京通用端点为 `https://dashscope.aliyuncs.com/compatible-mode/v1`；如果账户提供 workspace 专属端点，优先使用该端点。不同地域的端点见阿里云[Base URL 文档](https://help.aliyun.com/en/model-studio/base-url)。
4. 指定模型 ID `qwen3.8-flash`，保留 `json_object` 能力。百炼官方将此 ID 列为轻量低成本模型，并标明支持 OpenAI-compatible 与结构化输出：[模型说明](https://help.aliyun.com/en/model-studio/qwen3-8-flash)、[模型能力列表](https://help.aliyun.com/en/model-studio/text-generation-model/)。成功后 Profile ID 由 `DASHSCOPE_API_KEY` 自动生成为 `dashscope`。
5. 保留 ID 为 `deepseek` 的现有 DeepSeek Profile 作为大模型兜底。路由模型默认 ID 可在服务端 `.env` 中通过 `RSFMRI_WORK_ROUTER_SMALL_PROFILE_ID` 和 `RSFMRI_WORK_ROUTER_FALLBACK_PROFILE_ID` 调整；默认分别为 `dashscope` 与 `deepseek`。

`RSFMRI_WORK_ROUTER_FALLBACK_PROFILE_ID` 指定的 Profile 也用于 Work 最终 JSON schema 失败后的至多一次结构修复。修复调用会显式选择该 Profile，使用 Profile 自带模型（不覆盖 model ID），关闭联网并且不提供工具；发送内容限于本轮问题和 Agent 角色，Gateway 仍会按 `work_mode` 执行外发敏感内容拒绝与脱敏。它不发送会话历史、固定上下文、工作区读取结果或首次无效回复。修复调用消耗同一轮剩余模型预算；复合目标还会在 Provider 调用前预留持久预算。未配置可用 Profile 或预算不足时 Work 失败关闭，不分发动作或创建 Run。

路由使用本轮用户请求文字及最少布尔/角色状态，并通过 `OutboundContextPolicy` 清洗后发到所配置的模型服务。不会发送对话历史、工作区文件、影像或参与者清单；检测到参与者表、受试者-组别对应或影像/base64 payload 时，该轮路由模型调用会被阻止。本地规则命中的明确请求不调用路由模型。真实请求仍会离开本机；请在提供商侧确认数据处理条件。路由测试使用 mock provider，不会触发真实 API 调用。

## DeepSeek 官方接口来源

DeepSeek 配置和兼容性核对以官方 [Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion/) 与 [JSON Output 指南](https://api-docs.deepseek.com/guides/json_mode/) 为参考。当前项目通过可配置的 OpenAI-compatible adapter 调用，不硬编码模型名称；实际可用的模型、端点和 JSON 能力仍须按当前账户配置并通过用户显式触发的轻量 smoke 验证。

## 轻量 smoke

Provider smoke 会向外部服务发送一个已脱敏的连通性和 JSON schema 测试，可能产生费用。它只能由用户显式点击“测试”或调用 `POST /api/v1/providers/test` 触发，CI 不会使用真实密钥。Work 文件授权另外会发送一次合成工具调用探针；该探针不读取工作区内容，也不取代用户对后续文件内容外发的明确授权。

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
