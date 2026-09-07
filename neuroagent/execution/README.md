# Execution 层

`neuroagent.execution` 负责结构化 Job、固定模板、受控进程和产物边界。它不决定科研参数、不修改工作流状态，也不接受 Agent 生成的任意 Shell 或 MATLAB 文本。

当前实现包括：

- `MatlabJobSpec`、路径绑定、冻结 SkillPlan/manifest 编译和必需产物合同。
- 固定 MATLAB 模板渲染与 dry-run，可审查输入清单、Cfg、脚本、运行目录和预期产物。
- `MockMatlabExecutor`，用于 CI 和完整工作流测试。
- 默认禁止真实运行的 `ControlledMatlabExecutor` 已接入公共 Worker，支持 Windows 空格路径、超时、取消、退出码、日志和必需产物检查。执行前会快照预期输出；成功后仅登记本次新建或内容发生变化的非空普通文件，并记录大小和 SHA-256。目录、符号链接、零字节文件以及前次失败或重试遗留的未变化产物都会被拒绝。stdout/stderr 持续写入 `logs/<job_id>/attempt-NNN/`，重试保留历史；`MatlabJobResult` 只携带最多 1 MiB 的日志尾部，避免大输出阻塞或占满内存。Artifact API 目前提供元数据，不提供文件下载。
- 公共 MATLAB 适配器给每次执行建立独立 attempt 工作目录，重新从只读输入准备 staging，避免重试复用已删除 dummy volumes 的时间序列。
- 统计软件证据来自运行时 MATLAB/SPM 版本和 DPABI 源码 Release 标记；效应量使用冻结设计矩阵的对比方差，见 [ADR 0007](../../docs/adr/0007-adjusted-statistical-effects.md)。
- 指标影像与掩膜通过 DPABI `y_ReadRPI` 做相同轴方向规范化后核对维度和物理网格；仅做轴翻转，不重采样。

每次 MATLAB 作业必须使用独立运行目录。真实运行只接受验证后的结构化 JobSpec，且需调用方显式授权。常规测试不启动 MATLAB；明确 opt-in 的仓库外小型合成 smoke 记录在 [验证文档](../../docs/development/mvp-verification.md)。预处理当前限单会话 4D 输入，metric-only 限单主体；需要不同阶段掩膜的 OnResults normalization 与组合指标方案暂时拒绝；可能触发 DPARSFA GUI 的 T1 分割/DARTEL 在 headless 编译时失败关闭。
