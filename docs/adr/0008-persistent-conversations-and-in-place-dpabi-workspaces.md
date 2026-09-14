# ADR 0008：持久化对话与显式 DPABI 原位工作区

## 状态

已接受，2026-09-07。

## 背景

Agent 页面需要分成 Chat 和 Work。Chat 面向 RAG + LLM 方法问答，并可逐次开启联网搜索；Work 负责选择 fMRI 工作区、
检查 DPABI 格式、启动已审批预处理并显示进度。仅在浏览器内保存消息会导致刷新后丢失上下文，
也无法审计 Agent 实际调用了哪个工具。

既有执行器总是把输入复制到独立 staging。用户明确要求 DPABI 在所选工作区内运行并把生成的
stage/Results 保留在那里。这改变了原始数据默认只读的边界，因此必须是显式模式，并只面向
已整理的 DPABI 工作副本。

## 决定

1. SQLite 保存 Conversation、Message 和 ToolCall。Chat 与 Work 使用独立会话；用户消息、Agent
   回复、工具输入摘要、状态、输出和安全错误均持久化。
2. Chat 先执行本地 RAG，再把经过 `OutboundContextPolicy` 脱敏的问题和证据交给所选模型。
   联网搜索必须由用户逐次开启，并只允许声明 `web_search` 能力的 Profile；Provider 返回的 URL
   引用与上下文哈希进入 ToolCall。Work 只路由已登记的确定性应用服务，不接受自由 Shell/MATLAB
   文本，也不直接推进 Workflow 状态。
3. DPABI 原位执行通过 `workspace_mode=in_place` 显式选择，只接受已冻结 manifest 的
   DPABI-ready 数据集。启动仍要求精确计划哈希、审批记录、环境锁、执行开关和逐次确认。
4. Worker 在启动前重新验证 manifest 中每个科学输入的大小和 SHA-256。验证后把数据集目录作为
   `DPARSFA_run` 的 `WorkingDir`，因此 DPABI stage 和 Results 写入该工作区。
5. 每次运行的脚本、配置、日志、证据和登记产物仍位于独立 attempt 目录。需要的掩膜复制到工作区
   内按 attempt 隔离的 `.neuroagent/` 路径，不修改 MATLAB、SPM 或 DPABI 安装目录。

## 后果

工作区应是可写的 DPABI 工作副本，而不是唯一原始归档。运行会在其中创建 DPABI 约定目录，用户在
确认框中会看到这一结果。输入内容在队列等待期间发生变化时，Worker 失败关闭。重试使用新的
attempt 记录，但 DPABI 已产生的 stage 仍留在工作区；覆盖已有同名 DPABI 产物仍需后续增加更细的
冲突预览，当前每次真实启动继续要求用户确认。
