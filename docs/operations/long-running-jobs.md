# 长任务等待、恢复与错误诊断

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

失败诊断先用确定性分类器读取有界日志片段，归类输入缺失、NIfTI 头、Cfg、软件环境、资源、路径、超时、取消或未知错误。建议只能解释和生成新计划提案；修改输入、排除受试者、改变统计参数和重跑必须重新审批。
