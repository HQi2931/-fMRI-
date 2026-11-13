# 长任务等待、恢复与错误诊断

- 状态：Implementing
- 文档类型：Operations
- 维护者：backend_service_engineer
- 更新日期：2026-11-13
- 关联：[本地运行、诊断与恢复](local-operations.md)

前端不阻塞浏览器：创建运行后立即返回 `RunView`，通过 `/runs/{id}/events` 的 SSE 接收事件；刷新页面时带上最后事件 ID 继续回放。无法可靠估计的 MATLAB 阶段显示“正在运行”，不伪造百分比或 ETA。

Worker 的生命周期是：

```text
queued → staging → running → qc_review → succeeded
                         ├→ failed_retryable
                         ├→ failed_terminal
                         ├→ timed_out
                         └→ cancelled
```

取消先记录请求，再由执行器在有界等待内响应；超时后只能终止受控进程树。DPABI 没有通用断点协议，因此只有经过验证的步骤边界可以恢复，其他重试从新的 staging/attempt 开始，旧产物不得冒充新结果。

运行卡片可轮询 `GET /runs/{run_id}/diagnosis`。服务端只从本机当前 attempt 读取有界 stdout、stderr 和 `matlab.log`，按确定性规则归类输入缺失、NIfTI 头、Cfg、软件环境、资源、路径、超时、取消或未知错误；日志证据中的本机路径、受试者编号和邮箱会被脱敏。暂无日志时返回空 evidence 的 `unknown/info`，不会制造失败线索。兼容接口 `POST /runs/{run_id}/diagnosis` 仍接受有界日志文本，但不调用模型。建议只能解释和生成新计划提案；修改输入、排除受试者、改变统计参数和重跑必须重新审批。

MATLAB 启动日志出现 Windows `CreateFile failed`、拒绝访问及 `system:5` 时，诊断会指出启动阶段的文件创建被拒绝。2026-11-13 的内核文件跟踪把该错误对应到进程 `TEMP` 目录下的 `mathworks-<uuid>` 文件；把临时目录改到可写位置后，下一处被拒绝的创建是本机 `LocalAppData\MathWorks\mwEndpointRegistry\mwER*`。覆盖 `LOCALAPPDATA` 不会改走这个注册文件。每次 attempt 已有的独立 `MATLAB_PREFDIR` 只隔离偏好目录，不能代替这两个创建。分类器不更改机器设置，也不自动重启运行。
