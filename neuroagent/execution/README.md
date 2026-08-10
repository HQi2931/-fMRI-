# Execution 层

`neuroagent.execution` 负责结构化 Job、固定模板、受控进程和产物边界。它不决定科研参数、不修改工作流状态，也不接受 Agent 生成的任意 Shell 或 MATLAB 文本。

当前实现包括：

- `MatlabJobSpec`、路径绑定和必需产物合同。
- 固定 MATLAB 模板渲染与 dry-run，可审查输入清单、Cfg、脚本、运行目录和预期产物。
- `MockMatlabExecutor`，用于 CI 和完整工作流测试。
- 默认禁止真实运行的 `ControlledMatlabExecutor`，支持 Windows 空格路径、超时、取消、退出码、日志和必需产物检查。执行前会快照预期输出；成功后仅登记本次新建或内容发生变化的非空普通文件，并记录大小和 SHA-256。目录、符号链接、零字节文件以及前次失败或重试遗留的未变化产物都会被拒绝。stdout/stderr 持续写入 `logs/<job_id>/attempt-NNN/`，重试保留历史；`MatlabJobResult` 只携带最多 1 MiB 的日志尾部，避免大输出阻塞或占满内存。公共 Worker/API 当前未接入该执行器，也不提供这些日志的查询或下载接口。

每次 MATLAB 作业必须使用独立运行目录。真实运行只接受验证后的结构化 JobSpec，且需调用方显式授权；测试不会启动 MATLAB、SPM12 或 DPABI。
