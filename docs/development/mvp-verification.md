# MVP 验证与已知限制

状态：v0.1.0 发布验证记录
更新日期：2026-09-06

## 最终树必须通过的自动门禁

`scripts/quality-gate.ps1` 定义当前候选树的本地汇总门禁：

- Python：Ruff 格式/规则、strict mypy、依赖审计、pytest 与不低于 85% 覆盖率。
- Web：ESLint、TypeScript、Vitest 与不低于 80% 覆盖率、生产构建、Playwright Mock E2E 和依赖审计。
- 科研契约：Skill schema、稳定计划哈希、DPABI 稳定参数字段快照、ALFF/fALFF 与 ReHo 顺序、Nyquist/频段/ReHo 邻域、受试者与协变量对齐、统计方向和 QC 门禁。
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

这里的 Mock 闭环验证的是批准计划可创建作业，以及队列、状态、事件、DAG 步骤和通用 Artifact 收口协议。Mock 通过 `ToolRuntime` 逐节点执行；MATLAB 由 executor registry 分派至 `MatlabJobExecutor`，其中校验 `WorkflowFactory` 生成的冻结计划并编译分段 DPARSFA 作业，不逐节点经过 `ToolRuntime`。MATLAB 另有环境和逐次确认门。

## 已知实现限制

1. 公共 Web/API 默认创建 Mock 作业；首次使用时用户在环境页面选择 MATLAB/SPM/DPABI 路径。可显式选择 MATLAB，但必须满足配置开关、入口探测就绪和逐次确认。
2. 公共 Worker 通过 executor registry 分派；Mock 使用 `ToolRuntime`，MATLAB 适配器校验冻结计划后编译受控作业。未注册类型、锁漂移、谱系失配或缺失输出均失败关闭。
3. `ControlledMatlabExecutor`、固定模板和受控 DPABI 入口投影已接入公共路由；用户选择的版本标签仅作配置证据，运行产物保存实际观察到的软件版本。每次执行使用独立 attempt 工作目录，避免重试复用已处理 staging。
4. 真实 MATLAB 统计运行按冻结设计生成未校正统计图、可选校正图、效应量图、簇表、日志和软件版本证据，并由应用层组装确定性报告；任一角色缺失、哈希漂移或设计不一致都会失败关闭。下述小型合成 smoke 已验证三类 t 检验及报告合同，不代表全部数据集或统计配置已验证。
5. Artifact API 只提供元数据，不提供文件下载。
6. Playwright 端到端测试使用 Mock API，不是真实 FastAPI—Worker—MATLAB 端到端运行。
7. `matlab_preprocessing` 已从冻结 SkillPlan/manifest 编译 DPARSFA 输入和 typed JobSpec，实际读取产物元数据并登记 lineage。当前范围是单会话 4D 输入；metric-only 路径限单主体。OnResults normalization 与组合指标若需不同阶段掩膜，以及没有指标输出的纯预处理 OnResults 操作，当前合同明确拒绝。T1 分割和 DARTEL 在当前 DPARSFA 非交互入口仍可能调用 GUI，因此 headless 编译器失败关闭；EPI template normalization 要求先 realign 产生 mean image，仍未包含在本次真实 smoke 中。
8. 指标输入与已验证掩膜两端都通过 DPABI `y_ReadRPI` 做一致的轴翻转规范化，再严格比较维度和物理网格；这一步不重采样，不接受物理空间不匹配的掩膜。新产物的网格签名从本次实际输出计算，不复用掩膜 Artifact 的自报标签。

## 2026-09-04 已执行的授权 smoke

以下验证仅使用仓库外、确定性合成数据。输入、脚本、MATLAB 日志、产物及摘要保留于本机独立临时目录；仓库不存放影像或机器私有路径。测试参数仅为软件验证夹具，不成为科研默认值。

- 统计：`tests/science/test_matlab_statistics_smoke.py` 通过受控执行器运行三类 t 检验，影像为 10 × 10 × 10。检查单样本基线 2 与 FDR/BH、独立组混杂协变量、配对负尾 GRF、T 值与方向、效应量、分正负的 26 邻接簇表、必需产物和真实结果报告合同。
- 数值验证：真实输出与独立 OLS/配对差值公式比较，绝对容差 `1e-4`；`tests/science/test_statistics_runtime_contract.py` 直接执行模板效应量代码，验证协变量对比方差与无协变量退化公式。报告元数据改为从实际 provenance 文件及观察环境计算哈希后，两个文件重跑共 4 tests passed，105.42 秒；每个 MATLAB 作业均设置 180 秒上限。
- 实际软件证据：MATLAB R2024b（24.2.0.2712019）、SPM25（25.01.02）、DPABI V9.0_250415。运行时核验统计入口分别接受 7/6/6/9/12 个参数，模板原调用正确，不存在先前评审推测的参数过多问题。
- 预处理：公共 Worker 到 QC 路径的小型 smoke 已通过；124 个 volume 删除最初 4 个后保留 120 个，完成 detrend，源文件 SHA-256 不变。组合 metrics 同样通过公共 Worker 到 QC，登记了时序、ALFF、fALFF、ReHo、执行日志和 metadata evidence；所有指标记录实际 120-volume 时序依据，并绑定同一经物理网格核验的 mask。
- Provider：`zhipu / glm-5.3-flash` 的一次真实轻量调用成功，未发送受试者信息；发送上下文哈希为 `efaaa8c1639d09562745f1c9ac78758265d357005825b84515e8fb71459460ea`。

统计 opt-in 测试使用 `RSFMRI_RUN_MATLAB_STATISTICS_TESTS=1`；软件路径来自 `RSFMRI_MATLAB_EXECUTABLE`、`RSFMRI_SPM_DIR` 和 `RSFMRI_DPABI_DIR`。默认 CI 跳过真实 MATLAB 测试。以上结果不代替最终暂存树的质量门禁、多角色终审或远程 CI。

## 人工或外部验证项

以下检查不能在无人值守门禁中伪造为成功：

1. GitHub CLI 已安装且登录成功。`main` 分支已通过 API 回读确认 strict status checks（`agent-review`、`quality-gate`）、enforce admins、linear history、required conversation resolution 及禁止 force-push/delete；最终 Actions 状态和正式 tag 仍必须从对应发布提交核验。
2. Provider 和真实统计 smoke 已有上述成功记录；新的 Provider、环境或真实数据作业仍遵守相应授权与去标识化规则。
3. 组合指标 smoke 已完成。常规测试仍不启动 MATLAB；最终树门禁、远程 CI 和 tag 的结果以对应发布提交及 GitHub 检查为准。
4. 小型统计 smoke 覆盖受控 Executor、真实产物和报告构建合同；不把 Mock Playwright 测试表述为真实 Web—API—Worker—MATLAB 端到端验证。

## 非目标

本候选版不包含多用户权限、云部署、PACS、微服务、通用插件市场、长期记忆或临床诊断。输出仅用于科研流程辅助，不能替代方法学审查。
