# ADR 0018：Work 选项式确认弹窗

- 状态：Accepted
- 文档类型：ADR
- 维护者：frontend_ux_engineer
- 决策日期：2026-09-29
- 替代关系：部分取代 [ADR 0012](0012-chat-work-only.md) 中消息只展示文字的限制；保留 Chat/Work 导航与对话入口。
- 关联：[ADR 0015](0015-agent-control-plane-hardening.md)

## 背景与决定

用户要求为 plan-and-execute 的待确认内容提供弹出选项，避免手动复制确认口令。

协调器在消息 payload 中增加可选 `confirmation_request`，包含 `title`、`summary` 和一至三个 `options`，每项含 `label`、`description`、`command`。字段由服务端在现有确认分支构造，不接受模型提供的动作定义。缺少必需参数不生成参数确认。启动选项在签发绑定方案与 QC 的 challenge 后生成。

前端只展示当前工作区所绑定 Work 会话最后一条 assistant 消息中的有效确认请求，不从自然语言提取动作。原生 modal dialog 提供背景隔离、焦点限制和 Escape 关闭，选项默认不勾选。确认通过现有对话接口提交精确 command，不以客户端布尔值授权。提交期间禁用操作，失败保留错误；新回复替换旧确认。

关闭与修改仅改变本地 UI，不代表撤销服务端 challenge，也不发送批准。“待确认”可重新打开；服务端继续拒绝过期、变更、重放或跨作用域授权。旧会话仅含文字时继续支持原有确认口令，不自动生成按钮。

## 验证与边界

组件集成测试覆盖显式选择、关闭、重开、指令提交与历史确认抑制；后端测试覆盖完整草稿可确认、缺项不生成选项以及现有版本与 challenge 校验。此次变更不运行真实 MATLAB。
