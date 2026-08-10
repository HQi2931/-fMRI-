# 本地运行、诊断与恢复

本手册适用于 Windows 单机候选基线。公共 Worker 当前是 Mock-only；本手册中的启动命令不会自动启动 MATLAB/DPABI。

## 首次准备

在仓库根目录打开 PowerShell。不覆盖已有 `.env`：

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
.\scripts\bootstrap.ps1
```

然后只在未跟踪的 `.env` 中配置：

- 项目允许的只读数据根目录和独立工作根目录。
- MATLAB、SPM12 和 DPABI V8.2 的本机绝对路径。
- 随机的 `RSFMRI_REDACTION_SALT`。
- 如需外部 Provider，配置对应的 `*_API_KEY`。

不要把 `.env`、密钥、真实数据路径或诊断输出粘贴到 Issue、PR 或 Agent 上下文。

## 开发模式

首先升级数据库并启动 API：

```powershell
uv run alembic upgrade head
uv run rsfmri-api
```

另开一个 PowerShell 启动 Worker：

```powershell
uv run rsfmri-worker
```

再开一个 PowerShell 启动前端开发服务器：

```powershell
Set-Location web
npm run dev
```

前端开发服务器从仓库根目录 `.env` 读取 `RSFMRI_HOST` 和 `RSFMRI_PORT`，并通过本地代理访问同一 API 地址；`start-local.ps1` 和 `diagnose.ps1` 也使用这组经过校验的配置，IPv6 回环地址会自动加方括号。

## 本地集成模式

构建前端并在隐藏进程中启动 API 和 Worker：

```powershell
.\scripts\start-local.ps1
```

运行状态记录在 `tmp/local/`，日志写入 `logs/`，两者都不进入 Git。如果前端已经构建，可使用 `-SkipBuild`：

```powershell
.\scripts\start-local.ps1 -SkipBuild
```

安全停止由该脚本启动的两个进程：

```powershell
.\scripts\stop-local.ps1
```

停止脚本会核对 PID 和进程启动时间，避免误停止被系统重用 PID 的无关进程。

## 诊断

在 API 和 Worker 已启动、`.env` 已填写后运行：

```powershell
.\scripts\diagnose.ps1
```

诊断会检查本机开发命令、MATLAB/SPM/DPABI 环境和 API 健康状态。任一检查失败都返回非零退出码，因此在未配置科研软件或 API 未启动时失败是预期结果，不代表仓库单元测试失败。

故障排查顺序：

1. 查看 `logs/api.err.log` 和 `logs/worker.err.log`，分享前先移除路径、受试者信息和密钥。
2. 调用 `/api/v1/health` 确认数据库可读。
3. 调用 `/api/v1/environment/probe` 检查环境锁。只有 MATLAB、SPM12、
   `DPARSFA_run`、ALFF/ReHo、统计检验、FDR/GRF、统计影像 I/O 入口和仓库内
   受控适配器都存在时才会报告 `ready=true`；锁只返回无路径的内容指纹和缺失入口名。
4. 检查 `tmp/local/*.json` 中的 PID 是否仍对应原进程。
5. 停止后重启；SQLite Worker 会重新领取排队任务，过期租约按已批准重试预算恢复。

## 合成演示的真实边界

```powershell
uv run python scripts\synthetic-demo.py --root tmp\synthetic-demo
```

该脚本使用即时生成的只读 BIDS 占位文件、真实 SQLite/Application Service 和 Mock Worker，依次完成 manifest、ALFF Skill 与审批、测试夹具 typed Artifact、人工 QC、单样本 t + FDR 设计、统计 Mock 和确定性 Markdown/JSON 报告。输出报告位于该演示根目录下的 `work/project/reports/<run_id>/`。

这是测试流程，不是影像处理：所有指标与统计结果角色都标记为 `synthetic_non_scientific`，不含 t 值、p 值或显著性结论，不能用于科研或临床推断。重复运行请使用新的 `--root`，以免把旧演示数据库误认为新运行。

## 元数据备份

备份前建议先停止 API 和 Worker：

```powershell
.\scripts\stop-local.ps1
.\scripts\backup.ps1
```

备份与恢复脚本当前只支持仓库 `work/` 内的 SQLite 文件，并且不会从任意外部数据库或 PostgreSQL 导出数据。脚本会读取进程环境或本地 `.env` 中的 `RSFMRI_DATABASE_URL`，目标必须与该 URL 指向的文件完全一致；若使用 `work/` 内的自定义文件名，备份和恢复都显式传入同一个相对路径，例如：

```powershell
.\scripts\backup.ps1 -DatabasePath 'work\study-metadata.db'
.\scripts\restore.ps1 -BackupDirectory '.\backups\YYYYMMDD-HHMMSS' -DatabasePath 'work\study-metadata.db'
```

命令成功前先核对 `.env` 与 `-DatabasePath`。默认命令只适用于 `sqlite:///./work/neuroagent.db`；路径不一致或数据库位于 `work/` 外时脚本失败关闭，不能改去备份一个仍然存在的旧默认文件。

备份被写入 `backups/<timestamp>/`，并包含 SQLite 一致性副本、SHA-256 和 manifest。备份范围仅是元数据数据库，不包含：

- 原始影像或人口学源文件。
- 生成的产物、日志和报告。
- `.env`、Provider 密钥或 MATLAB 安装。

“仅元数据”不表示备份不敏感。SQLite 副本可能包含项目和数据源路径、受试者/会话标识符（系统不会自动伪名化，可能仍含可识别信息）与 manifest、文件哈希、人口学字段映射及已导入的人口学/协变量值、QC 纳入排除决定和审计事件。`backups/` 虽被 Git 忽略，仍必须按敏感科研元数据管理：保存在访问受控或加密的本地存储中，不提交 Git，不上传到未经批准的云盘或共享目录，也不发送给外部模型。

SHA-256 只用于发现损坏，既不是加密，也不是去标识化。备份保留和安全删除应遵循课题的数据管理与伦理要求。因此这不是完整的灾难恢复包；原始数据应由研究数据管理流程独立保护，不复制到 Git 或本备份目录。

## 恢复元数据

恢复会覆盖当前 SQLite 数据库，必须在 API 和 Worker 都已停止后手动执行。脚本会验证备份位于仓库 `backups/` 内并核对 SHA-256，覆盖前为现有数据库创建时间戳安全副本。

API 和 Worker 无论由 `start-local.ps1` 还是 `uv run` 手工启动，都会在数据库旁登记进程运行标记；恢复脚本先原子取得同目录恢复锁，再同时核对启动脚本状态文件与这些运行标记。存活进程会阻断恢复，已退出进程遗留的标记会被安全清理；无法解析的标记或恢复锁会失败关闭，只有确认没有恢复或服务进程后才可人工移除。该协议也会阻止服务在恢复覆盖窗口中启动。

```powershell
.\scripts\stop-local.ps1
.\scripts\restore.ps1 -BackupDirectory '.\backups\YYYYMMDD-HHMMSS'
```

恢复后重新启动，检查 `/api/v1/health`、项目数量和最近审计事件。恢复元数据不会恢复缺失的文件产物。

浏览器工作区不属于 SQLite 备份。工作台会把当前项目、manifest、受试者/会话标识符（系统不会自动伪名化，可能仍含可识别信息）、计划、运行、QC 和统计 revision/hash 指针保存在当前浏览器配置的 `localStorage`，直到在数据页点击“切换项目”清除或手工清理该站点数据；它不保存影像、人口学表内容或 Provider 密钥。浏览器站点数据仍属于敏感科研元数据；共享计算机应使用受控的独立浏览器配置，并在使用后清除工作区。数据库恢复后必须刷新页面，在总览重新选择项目并核对当前 revision；若仍显示恢复前的失效引用，先在数据页点击“切换项目”清除浏览器引用。待确认写请求的指纹与幂等 key 只保存在当前标签会话的 `sessionStorage`，浏览器状态不应复制到备份或提交 Git。

## 真实 MATLAB/DPABI smoke

常规启动和门禁不运行 MATLAB。真实 smoke 必须满足：

1. 用户对该次具体作业给出明确授权。
2. 仅使用小型合成、脱敏或公开许可数据。
3. 先审阅 dry-run 的输入清单、Cfg 投影、脚本、工作目录、预期产物和软件版本。
4. 输出只写入独立 staging/run 目录，不覆盖原始数据。
5. 单独记录 smoke 的数据来源、授权、版本、退出码和产物检查。

当前公共队列没有接线真实 Executor，因此本手册不提供可绕过运行授权的启动命令。
