# Observability 层

`neuroagent.observability` 提供运行事件脱敏和 trace 上下文。事件由 SQLite 持久化，可按 `event_id` 游标查询并由 SSE 断线续传。

当前记录项目、计划、审批、任务、QC、统计设计、模型路由与状态转换事件。事件 payload 在写入前移除常见密钥和敏感字段；原始影像、人口学明细、绝对路径和 Provider 密钥不得进入审计记录。

本层不改变业务状态，也不替代应用错误处理。指标聚合和远程遥测不在单机 MVP 范围内。
