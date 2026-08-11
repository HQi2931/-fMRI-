# 扩展分析服务设计

```text
React / SSE
    ↓
FastAPI contracts
    ↓
NeuroAgentService（路径策略、输入校验、审批边界）
    ↓
analysis helpers（诊断、ROI 表、ML 模板、cluster、RAG）
    ↓
注册 Tool / Workflow / Mock 或受控 MATLAB Executor
```

`neuroagent.analysis` 是纯领域辅助模块：它不启动进程、不修改原始数据、不读取密钥。服务层负责将项目根目录转换为允许的只读路径，再调用这些函数。输出模型包含内容哈希、证据、设计哈希或 lineage 所需字段。

长任务通过 SQLite Worker 领取；事件是 UI 的实时事实来源。Worker 在 staging 和 running 边界发出事件，成功、失败、超时和取消都保留 attempt 日志。当前 RunView 的进度字段向后兼容旧数据库，待后续迁移后再持久化精确心跳与 ETA。

ML 模板只生成可审查的 Python 文件，模型不能注入命令或绝对路径。ROI 与 cluster API 当前接受已验证的结构化记录，真实 DPABI/NIfTI 读写必须在批准的 Artifact Tool 中完成。
