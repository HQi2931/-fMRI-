# MVP 验证与已知限制

状态：待最终质量门禁、多角色终审与首次发布
更新日期：2026-08-07

## 最终树必须通过的自动门禁

`scripts/quality-gate.ps1` 定义当前候选树的本地汇总门禁：

- Python：Ruff 格式/规则、strict mypy、依赖审计、pytest 与不低于 85% 覆盖率。
- Web：ESLint、TypeScript、Vitest 与不低于 80% 覆盖率、生产构建、Playwright Mock E2E 和依赖审计。
- 科研契约：Skill schema、稳定计划哈希、DPABI V8.2 字段快照、ALFF/fALFF 与 ReHo 顺序、Nyquist/频段/ReHo 邻域、受试者与协变量对齐、统计方向和 QC 门禁。
- 执行：SQLite 原子领取、租约心跳、失败/超时/取消/恢复、运行进程标记与原子恢复锁、带所有者和 Provider 等待心跳的请求幂等租约、固定 MATLAB 模板、Windows 空格路径、进程树终止、按 attempt 保留的大输出日志，以及仅登记本次执行新建或变化的非空普通文件（含 SHA-256/大小）。
- Web 恢复：多项目 `localStorage` 指针切换、Plan/QC/统计冻结内容重载、待确认幂等 key 的 `sessionStorage` 恢复，以及基于 `after_event_id` 的事件续传。
- 安全：源目录只读、工作目录边界、外发摘要去标识化、结构化模型输出、仓库敏感信息/科研二进制/大文件扫描。

本文不把历史子集测试替代最终门禁。发布证据必须记录最终暂存树哈希、执行日期、门禁结果和未执行项；在该记录存在前，不声称当前树已全绿。

对应风险回归主要位于 `tests/backend/test_database_runtime_lease.py`、`tests/backend/test_release_scripts.py`、`tests/backend/test_dataset_services.py`、`tests/science/test_execution_edges.py`、`web/src/api/client.test.ts` 和 `web/src/App.test.tsx`。

## 当前可验证的本地闭环

- 项目、数据集、只读扫描、manifest、人口学对齐和数据集划分。
- Skill 解析/校验/编译、批准计划 `WorkflowFactory`/`ToolRuntime` DAG 排队、步骤事件与 Artifact 元数据登记。
- 在测试中注册类型化指标 Artifact 后的 QC revision、人工审批和统计设计校验。
- 真实 SQLite、应用服务和 Mock Worker 组成的纯合成后端闭环：BIDS 扫描、ALFF Skill、审批、人工 QC、单样本 t + FDR、统计 Mock 与确定性 Markdown/JSON 报告。
- 前端对上述流程的 Mock API 交互。

`scripts/synthetic-demo.py` 会完成上述合成闭环，但 typed 指标和统计结果角色由内部测试 seam 注入为醒目标记的 `synthetic_non_scientific` 占位 Artifact。它不会运行影像算法、产生统计数值或证明真实科研结果。

这里的 Mock 闭环验证的是批准计划可创建作业，以及队列、状态、事件、DAG 步骤和通用 Artifact 收口协议。Mock 与 MATLAB 共享冻结计划和 ToolRuntime；MATLAB 还需环境和逐次确认门。

## 已知实现限制

1. 公共 Web/API 默认创建 Mock 作业；首次使用时用户在环境页面选择 MATLAB/SPM/DPABI 路径。可显式选择 MATLAB，但必须满足配置开关、入口探测就绪和逐次确认。
2. 公共 Worker 通过 executor registry 和 `ToolRuntime` 分派；未注册类型、锁漂移、谱系失配或缺失输出均失败关闭。
3. `ControlledMatlabExecutor`、固定模板和受控 DPABI 入口投影已接入公共路由；用户选择的版本标签仅作证据，真实小数据 smoke 仍是发布前授权门。
4. 真实 MATLAB 统计运行会按冻结设计生成未校正统计图、可选校正图、效应量图、簇表、日志和软件版本证据，并由应用层组装确定性报告；任一角色缺失、哈希漂移或设计不一致都会失败关闭。真实算法闭环仍需发布前 MATLAB smoke 证明。
5. Artifact API 只提供元数据，不提供文件下载。
6. Playwright 端到端测试使用 Mock API，不是真实 FastAPI—Worker—MATLAB 端到端运行。
7. `matlab_preprocessing` 当前保留为显式失败关闭路由；从冻结 SkillPlan/manifest 生成 DPARSFA 输入与 typed preprocessing JobSpec 仍是正式发布前缺口，不会被 Mock 结果冒充完成。

## 人工或外部验证项

以下检查不能在无人值守门禁中伪造为成功：

1. GitHub CLI 登录、首次推送、分支保护与 Actions 状态，需要仓库维护者完成一次 `gh auth login`。
2. 真实模型 Provider smoke，需要本机 `.env` 中存在有效 Key，并由用户显式触发可能产生费用的调用。
3. MATLAB/DPABI smoke，需要用户明确批准一个小型合成或脱敏数据作业。常规测试不启动 MATLAB。
4. 真实统计产物和报告闭环已完成 Executor/队列接线、实际产物发现与原子注册；仍需用户授权的 MATLAB smoke 记录 DPABI 实际版本和合成数据证据。

## 非目标

本候选版不包含多用户权限、云部署、PACS、微服务、通用插件市场、长期记忆或临床诊断。输出仅用于科研流程辅助，不能替代方法学审查。
