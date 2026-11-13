# Observability 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-23
- 关联：[项目结构](../../docs/architecture/project-structure.md)

`neuroagent.observability` 提供运行事件脱敏和 trace 上下文。事件由 SQLite 持久化，可按 `event_id` 游标查询并由 SSE 断线续传。

当前记录项目、计划、审批、任务、QC、统计设计、模型路由与状态转换事件。事件 payload 在写入前移除常见密钥和敏感字段；原始影像、人口学明细、绝对路径和 Provider 密钥不得进入审计记录。

完整 UUID 形式的系统引用与 64 位十六进制方案哈希按字段和格式识别，避免其中的数字被误当作手机号而损坏引用。敏感身份及凭据字段优先脱敏，不能通过 `_id` 或 `_hash` 后缀绕过；其他自由文本仍按原策略处理。

本层不改变业务状态，也不替代应用错误处理。指标聚合和远程遥测不在单机 MVP 范围内。

`work_timings.py` 通过请求上下文记录本轮实际发生的阶段耗时与调用次数，异常退出也记录已耗时间，并隔离并发请求。记录作为对话展示元数据保存，不修改冻结方案。嵌套计时可能重叠，页面明确提示不能相加；没有发生的阶段不生成记录。
