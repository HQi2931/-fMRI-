# MVP 验证与已知限制

- 状态：Implementing
- 文档类型：Development
- 维护者：qa_reviewer
- 更新日期：2026-10-26
- 关联：[MVP 产品范围](../product/mvp-scope.md)

## 2026-10-26 Work JSON 格式修复的 DeepSeek 兜底

普通 Work 最终响应未通过 `WorkInterpretation` JSON 校验时，最多追加一次项目 DeepSeek Profile 修复请求。修复提示只含本轮问题和服务端角色，不带历史、固定上下文、首次错误响应、工作区证据或工具结果，并关闭工具/Web。修复产物仍走现有 schema 与服务端动作授权；Provider 失败、截断、预算耗尽有独立安全诊断。AUTO 角色判别继续使用规则优先、DashScope `qwen3.8-flash` 小模型、项目 DeepSeek 回退。

修复路径及失败安全反馈的 Work 对话定向回归 6 项通过；Work 模型运行时/共享预算定向回归 15 项通过。完整 `test_work_dialogue.py` 为 32 项通过、5 项失败，涉及 TR 冲突时建议说明、页面起始阶段确认、确认挑战重放、旧 Gateway mock 方法，以及 ALFF 编译用例缺少 Aliyun/DeepSeek 路由 Provider；需另行清理，不能据专项测试推断整份 Work 对话回归通过。改动文件 Ruff、Mypy、92 份 Markdown 文档规范和 `git diff --check` 通过。验证使用 Mock Gateway；没有录入 Aliyun API Key，也没有调用真实 Provider、MATLAB/DPABI，未运行演示数据端到端验收。该切片不表示计划 0014 已完成。

## 2026-10-21 CompositeGoal 预算耗尽反馈

分析提案和统计结果解释 outbox 的累计模型预算耗尽状态，现在通过其 `last_error_code` 映射为专属进度原因。Work 流程卡显示相应阶段的预算耗尽和续接暂停说明；分析提案预算耗尽时不再显示“继续此分析”，结果解释阶段仍可只读查看。普通科研决定的继续入口保留。对话提示、outbox 不可重试语义及现有人工重试门禁未改变；没有增加预算编辑或重置能力，也未增加 Plan/Approval/Run 操作。

`test_composite_goal.py`、`test_analysis_only_result_continuation.py` 共 52 项通过；`AgentPage.test.tsx` 77 项通过。前端 TypeScript 类型检查和改动文件 ESLint、改动 Python 文件 Ruff check/format、`composite_goal.py` Mypy、`scripts/check-docs.ps1` 与 `git diff --check` 通过。验证使用本地 SQLite/Mock UI，不调用 Provider、MATLAB 或 DPABI，也未创建 Run。

## 2026-10-21 步骤 Run 授权重放与 MATLAB UI 修复

TaskStep 已绑定 Run 后，服务从对应持久 Job 的执行策略和 Run 的 QC 字段读取实际选择，只接受已消费到该 Run 的原确认挑战重放；未消费的新挑战或执行后端、工作区、QC 不匹配会返回冲突，不改变绑定和 Run。已消费挑战即使过期也只允许绑定 Run 的同挑战重放。Work 对每种 MATLAB 步骤显示真实计算确认框；切换后端会清除确认和授权，切到 Mock 时工作区回到 isolated。统计步骤保持其冻结 QC 来源及隔离工作区约束。

`test_step_plan_review_api.py` 与 `test_migrations.py` 共 27 项通过，覆盖新挑战拒绝、选择不匹配拒绝、挑战不被消费、过期的原挑战重放及迁移兼容；`StepPlanReviewPanel.test.tsx` 11 项通过，覆盖统计 MATLAB 确认和后端切换重置。改动文件 Ruff check/format、相关 Python Mypy、前端 TypeScript、ESLint 和生产构建通过。测试未创建真实运行，未调用 Provider、MATLAB 或 DPABI。

## 2026-10-21 Work 会话详情 GET 风暴修复

本地 Work 浏览器发现终态运行观察会在会话详情刷新后重新触发自身：刷新将返回的新 `work_tasks` 数组引用作为 effect 依赖，即使会话版本和任务内容不变也会再次请求详情。现场性能记录约 4 秒出现 231 次同一会话 GET，关闭页面后请求停止。前端改为按稳定会话/运行身份、会话版本和任务语义布尔值订阅，稳定终态快照只触发一次详情读取；待处理 outbox 的有限退避刷新仍保留。

新增 AgentPage 回归在旧实现下稳定复现第二次详情请求；修复后 `AgentPage.test.tsx` 76 项通过，覆盖无 outbox 的同快照只读取一次。TypeScript、改动文件 ESLint、生产构建和 `scripts/check-docs.ps1`（92 份 Markdown）通过。重新打开本机生产页面并保持已有终态运行卡片时，Performance Resource API 记录该会话详情 GET 1 次；页面稳定期间没有重复请求。验证使用 Mock API/现有只读会话，不调用真实 Provider、MATLAB 或 DPABI，也未发送新的 Work 回合。

## 2026-10-21 显式预处理目标 admission 与预算

显式 `preprocess_then_analyze` intent 会在 Work 模型生成之前将原始用户消息、复合目标身份和共享模型预算写入 SQLite，并以确定性消息 ID 与请求幂等键保证故障后恢复和重放稳定。成功准备 Plan 后，应用层从已接纳用户消息核对 conversation/project/hash，再在同一事务中保存预处理 WorkTask 并只绑定一个 Task。缺失 root、错项目、错 hash、错配第二个 Task 或模型 payload 改写身份均 fail closed。迁移 0035 保留旧库记录并允许未绑定 root；若存在未绑定 root，降级会拒绝而不丢失它。提案和结果解释 outbox 未改变；Plan 审查、批准、QC 审查与显式 Run 启动仍是既有边界。本切片不声称统计阶段已被自动启动。

`tests/backend/test_composite_goal_admission.py`、`test_work_task_conversation_bindings.py::test_preprocessing_work_task_binds_plan_and_run_atomically`、`test_migrations.py::test_preprocessing_goal_admission_migration_preserves_ledger_and_guards_downgrade` 与 `test_analysis_outbox_consumer.py` 共 21 项通过。测试使用 Mock Gateway/本地 SQLite，覆盖 budget-before-Gateway、admission 后中断及重放、显式 Analysis role 仍路由到预处理、从已接纳原始消息绑定 Plan/Task、admission-only 请求建立提案 outbox、outbox source 重核读取 root/raw-text hash、篡改原文失败关闭、legacy assistant dependency 兼容及迁移升级/降级保护。未运行真实 Provider、MATLAB 或 DPABI。

扩展执行 `test_work_dialogue.py` 与 WorkTask conversation-binding 回归时，有 5 项失败：`test_planning_asks_only_unresolved_questions_and_labels_recommendation_reason[3]`、`test_page_selected_stage_gets_a_confirmation_without_model_stage_recommendation`、`test_work_uses_redacted_gateway_json_protocol`、`test_truncated_work_response_records_safe_finish_reason` 和 `test_work_schema_error_does_not_log_model_values_or_unknown_keys`。这些结果仍需单独核查，不能据 7 项定向通过推断全套 Work 对话测试通过。Python Ruff 检查与文档检查结果以本次交付验证为准。

## 2026-10-21 MATLAB 运行错误自动跟踪

Work 运行卡片会在运行期间及失败、超时、取消后轮询只读 GET 诊断接口。API 只读取当前 attempt 目录下有界日志，在本机运行确定性分类器，并脱敏本机路径、受试者编号和邮箱。浏览器不会上传日志文本到 Provider。没有日志时返回空 evidence 和 `unknown/info`；识别到 `CreateFile failed`、拒绝访问和 `system:5` 时，会反馈 MATLAB 启动权限线索及本地检查建议。经过路径包含校验后，每个 attempt 的 `MATLAB_PREFDIR` 只传给对应 MATLAB 子进程，不改用户默认设置。这是针对偏好目录权限的本地隔离方案，不能证明根因唯一。

验证结果：诊断规则与 API 回归 29 项通过；MATLAB 子进程环境安全测试 25 项通过；`WorkRunPanel.test.tsx` 25 项通过；前端类型检查、生产构建和改动文件 ESLint 通过。组件测试覆盖运行中反馈、取消后反馈和无日志时不报错。日志不发送给模型。此前获准的 20 名被试 DPABI 运行尚未使用此修复重试，MATLAB 启动和预处理仍待真实验证。

## 2026-10-21 Metric-only Skill dry-run API

新增 `POST /api/v1/skill-plans/dry-run`，复用现有 `SkillPlanResolveRequest` 并返回 API-owned dry-run DTO。接口支持预处理与 metric-only；后者必须显式提交 registered `input_artifact_id`，不自动选择来源。应用服务仍执行现有冻结 manifest、Artifact、来源 Plan、lineage 和单主体核验，再调用真实 SkillResolver、Validator 和 Compiler。响应 `environment_probed=false`；已识别能力阻断返回 `ready=false/issues`，无效或身份错绑输入按统一 4xx 失败。dry-run 不写 Plan、Approval、Run、事件或工作区文件，不代表真实 MATLAB/DPABI 可执行。该 API slice 不创建持久 Work 草稿；当前独立 Work 面板见下文。

定向验证：`test_skill_plan_dry_run.py` 14 项通过；`test_science_agent_api.py -k skill_registry_resolution_and_blockers_are_api_enforced` 1 项通过。覆盖预处理既有 dry-run 流程、显式 registered metric-only checkpoint 成功、多 subject scope blocker、lineage 身份错绑 422、Plan/数据库表与工作区快照无变化，以及执行环境探测未调用。测试使用 `D:\rs` 下新建的 basetemp 并关闭 pytest cache provider。Ruff check/format、308 个 Python 源文件 Mypy、91 份 Markdown 文档规范、前端 TypeScript typecheck、OpenAPI/TypeScript schema 生成与 `git diff --check` 通过；存在一项既有 Starlette/httpx deprecation warning。未运行真实 Provider、MATLAB 或 DPABI。该 API slice 本身不创建持久 Work 草稿。

## 2026-10-21 Work metric-only checkpoint dry-run 面板

Work 会话现在可在当前工作区已有项目、dataset、manifest 且当前会话包含预处理 WorkTask/run 时打开临时 checkpoint 面板。候选 Run 仅来自该会话的预处理任务，必须由研究者明确选择；面板刷新 project、dataset 和当前 manifest，要求单 subject 清单，并核对成功 Run、功能时序 Artifact 的项目/Run/type、manifest、subject、session 与已验证 lineage。切换会话、工作区、清单或来源会清空选择和结果并取消未完成请求。指标、TR、频段、缩放、滤波、掩膜、平滑及 ReHo 邻域等科学参数均要求显式输入，没有预设参数；仅显式提交 `request_preprocessing=false` 和已核验的 `input_artifact_id` 到上述只读 dry-run。图像 preview 仅供查看，不是空间或科学 QC。面板不写 Work draft、Plan、Approval、Run、事件或工作区文件。

本切片前端定向测试 100 项通过（checkpoint 面板 10、AgentPage 68、API client 22）；TypeScript typecheck、ESLint、production build 和 91 份 Markdown 规范检查通过。全量 Vitest 当前为 23 个文件中 18 个通过、5 个失败，共 262 项测试 227 通过/35 失败；失败文件为 App、workspace、StatisticalClusterRoiPanel、WorkRunPanel 和 WorkStatisticalResults，定向的本切片三文件均通过。未运行真实 Provider、MATLAB 或 DPABI。

## 2026-10-20 Metric-only checkpoint subject scope preflight

正式 Skill resolve 只在 `request_preprocessing=false` 且 `requested_metrics` 非空时，复用 execution 的 `validate_preprocessing_subject_scope` 检查冻结 manifest，并要求其恰有一个 subject row。已登记 input Artifact lineage 的 `subject_id` 必须与该 manifest subject 一致。多 subject、跨 session、无效 scope 或身份不匹配均以 `METRIC_ONLY_CHECKPOINT_SCOPE_UNSUPPORTED` 返回精确字段路径和 remediation，在 Plan 写入前失败；执行层现有单主体和 subject identity 防线保留。预处理同 session 多 subject 不受影响。Work 当前的 Skill dry-run 只接受 preprocessing 请求，metric-only 通过 normal resolve 入口处理；反馈 adapter 将该 issue 分类为 `BLOCKED_CAPABILITY`，不提供自动修复。

metric-only scope、lineage mismatch、Work capability feedback、预处理双主体编译和单主体 metric-only 执行定向回归共 59 项通过，含 Plan 无写入断言。测试使用 `D:\rs` 下新建 basetemp，并关闭 pytest cache provider，未触碰仓库内 pytest artifacts。Ruff check/format、308 个 Python 源文件 Mypy、91 份 `docs/` Markdown 规范和 `git diff --check` 通过；存在一项既有 Starlette/httpx deprecation warning。未调用真实 Provider、MATLAB 或 DPABI。

## 2026-10-20 Work ReHo 与 CUT checkpoint 能力预检

Skill Validator 与执行编译器共用纯领域谓词：仅当请求 preprocessing、ReHo 参数存在、preprocessing 存在且 `scrubbing.method=CUT` 时，以 `REHO_CUT_CHECKPOINT_UNSUPPORTED` 阻断同计划组合，并指向 `preprocessing.scrubbing.method`。当前 DPARSFA 执行单元无法在同一计划中先生成并验证 CUT 后的 retained-volume checkpoint 再计算 ReHo。ALFF-only + CUT 与无 preprocessing 的 metric-only ReHo 不触发该 gate；executor 原 `ValueError` 防线保留。Work dry-run 返回 `ready=false` 和 remediation，正式 resolve 在 Plan 写入前拒绝，Work feedback 为 `BLOCKED_CAPABILITY` 且不自动修改参数。独立的 `EFFECTIVE_VOLUME_COUNT_UNKNOWN` 科学/证据提示仍保留，以说明保留时间点数须由后续 verified Artifact 确认。

科学、编译（包括原 executor guard）、dry-run/resolve、Work feedback 与 dialogue 定向回归共 68 项通过，测试使用 `D:\rs` 下新建的随机 basetemp。Ruff check/format、308 个 Python 源文件的 Mypy、91 份 Markdown 文档规范和 `git diff --check` 均通过；存在一项既有 Starlette/httpx deprecation warning。未调用真实 Provider、MATLAB 或 DPABI。

## 2026-10-20 Work 无指标的 `ON_RESULTS` 能力预检

Skill Validator 与执行编译器共用一个纯领域谓词：仅当 `request_preprocessing=true`、ALFF/fALFF 与 ReHo 参数都不存在、存在 preprocessing，且 normalization 或 smoothing 的 timing 为 `ON_RESULTS` 时，才以 `RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED` 阻断。问题分别指向 `preprocessing.normalization.timing` 或 `preprocessing.smoothing.timing`；两个字段都启用时各返回一项。请求任一指标参数、非 `ON_RESULTS` 以及 metric-only 不触发此 gate。执行编译器保留原拒绝防线；Work dry-run 返回 `ready=false` 和独立 remediation，正式 resolve 在 Plan 写入前失败，Work 反馈为 `BLOCKED_CAPABILITY` 且不自动修订。它说明当前执行能力边界，不推断实际影像或掩膜，也不声称是普遍科学规则。

科学、编译、dry-run/resolve、Work feedback 与 dialogue 定向回归共 65 项通过，含 normalization/smoothing 分别与同时启用、负例及 Plan 无写入检查。Ruff check/format、308 个 Python 源文件的 Mypy、91 份 Markdown 文档规范和 `git diff --check` 通过；测试使用 `D:\rs` 下新建的随机 basetemp，未调用真实 Provider、MATLAB 或 DPABI。存在一项既有 Starlette/httpx deprecation warning。

## 2026-10-20 Work `ON_RESULTS` 指标归一化能力预检

Skill Validator 与执行编译器现在共用一个纯领域谓词：只有同时请求 preprocessing、至少一个指标参数（按执行层定义，ALFF/fALFF 或 ReHo 参数存在）且 normalization timing 为 `ON_RESULTS` 时，才以 `RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED` 阻断。单指标也会阻断；disabled normalization、无 metric 和 metric-only 不触发此新 gate。原因是当前执行合同只承载一个 metric mask binding，不能表达所需的独立 mask-space 输入。这是当前执行能力边界，不代表检查了真实图像或掩膜空间，也不声称是普遍科学规则。执行编译器保留原有拒绝防线；Work dry-run 在不写入 Plan/Approval/Run 的情况下返回 `ready=false`、字段路径与独立 remediation，正式 resolve 在 Plan 写入前失败，Work 反馈为 `BLOCKED_CAPABILITY` 且不自动修订科学选择。

定向测试命令 `uv run python -m pytest tests/science/test_skill_compiler.py tests/science/test_preprocessing_compilation.py tests/backend/test_skill_plan_dry_run.py tests/backend/test_work_preflight_feedback.py tests/backend/test_work_dialogue_feedback.py -q -o addopts='' --basetemp D:\rs\pytest-on-results-20261020-e` 得到 60 passed，含单指标阻断、disabled/no-metric/metric-only 不触发、执行层旧拒绝、dry-run 与正式 resolve 无 Plan 写入，以及 capability feedback/no repair。Ruff check/format、7 个源文件 Mypy、91 份 Markdown 文档规范、前端 TypeScript 类型检查和 `git diff --check` 通过；OpenAPI 与生成 TypeScript schema 同步增加可选 remediation 字段。存在一项既有 Starlette/httpx deprecation warning。未运行真实影像、Provider、MATLAB 或 DPABI。

## 2026-10-20 CompositeGoal 阶段交接判定

`CompositeGoalSnapshot` 在同一只读 SQLite 快照内加入目标关联的最小 Plan、Run 与 QC 状态。纯 handoff decision 被 conversation progress 和两个既有 outbox consumer 共用：只有 Task wait reference、Plan/Run 项目、输入修订及绑定身份、活跃 Run 状态和 QC review 事实一致时，才细化为“等待审查”或“等待 Run”；错绑、未知或缺失证据不会据此放行。分析提案和统计结果解释 worker 只在当前 root 与 claim 精确匹配、各自 outbox 为 ready、action 对应目标一致时调用模型。阶段暂不可交接时不调用模型；proposal handoff 自动尝试达到五次后转为可人工重试的 `WAITING_USER`，不无限重试。来源复核、共享累计预算、worker 租约和 fencing 保持原有执行边界。

后端 CompositeGoal、服务、两类 outbox consumer、结果续接及 Worker 相关定向测试共 122 项通过。Agent 页面测试 67 项通过，TypeScript 类型检查和 ESLint 通过；相关 Python 文件 Ruff 与 5 个源文件 Mypy 通过；91 份 Markdown 文档规范检查和 `git diff --check` 通过。测试含既有 Starlette/httpx deprecation warning。该验证未调用真实 Provider、MATLAB 或 DPABI，也没有运行全量测试。决策见 [ADR 0052](../adr/0052-composite-goal-handoff-decision.md)；阶段范围和限制见[计划 0014](../plans/0014-general-agent-automation.md)。

## 2026-10-20 DPARSFA ExecutionUnit 派发前绑定核验

MATLAB Executor 现对含受支持 DPARSFA 逻辑步骤的编译结果，在创建 attempt 与 staging 前依据同一冻结 SkillPlan/JobSpec 重建 `ExecutionUnit` 并精确比较；同时核对 JobSpec 的 Plan、manifest、Approval、Run 和 Job 身份。单元缺失、身份漂移或任一输入/输出/gate/重试合同被篡改都会在 MATLAB 派发前失败关闭。有效合同仍只对应固定模板的一次 `DPARSFA_run`，整单元重试仍由 Worker 在新 attempt 目录重新开始。没有受支持逻辑步骤的旧编译夹具维持 `execution_unit=None` 兼容路径。

`test_execution_units.py`、`test_preprocessing_compilation.py`、`test_matlab_execution.py` 与 `test_preprocessing_execution.py` 共 51 项通过，1 项真实 MATLAB smoke 条件跳过；另 `test_preprocessing_runtime_edges.py`、`test_execution_control.py` 与 `test_environment.py` 共 42 项通过。覆盖有效/缺失/篡改单元、空逻辑步骤兼容、固定模板单次调用、Worker 派发前拒绝、运行时边界及显式执行控制。相关 4 个 Python 文件 Ruff check/format 通过，2 个源文件 Mypy 通过；91 份 Markdown 文档规范检查和 `git diff --check` 通过。未调用真实 Provider、MATLAB 或 DPABI；仍不提供物理 checkpoint、调用内暂停、分段恢复或多 ExecutionUnit 调度。

## 2026-10-20 ExecutionUnit capability 覆盖

预处理编译器现在使用穷尽 capability 分类，而不是按名称前缀推断计算类型。`fmri.dpabi.preprocess`、`fmri.dpabi.alff_falff`、`fmri.dpabi.prepare_reho` 与 `fmri.dpabi.reho` 四种计算映射到同一 `DPARSFA_run` 单元；`fmri.artifact.verify_metadata` 随单元的调用后 metadata gate 覆盖。manifest 核验、输入 staging、只读数据检查、BIDS 转换候选计划、指标 QC 和统计前人工 QC 显式留在该物理调用外。三个统计执行能力使用不同合同；它们以及任何未分类能力混入预处理 Plan 均在生成 staging 计划前拒绝。有逻辑步骤的预处理 Plan 必须至少映射一个 DPARSFA 能力，空步骤低层旧夹具仍兼容。

回归覆盖默认 ToolRegistry 和所有内置 Skill step capability 的分类并集/计算能力全覆盖，ALFF+ReHo 合并 unit 对四种计算及 metadata gate 的完整映射，外部 checks/gates 不被并入物理 unit；未知计算能力及统计执行能力均在 Worker 创建 attempt、staging 或启动 MATLAB 前失败。真实 MATLAB smoke 未运行；没有新增 checkpoint、调用内暂停、多单元派发或分段恢复。

`test_execution_units.py`、预处理编译/Worker、MATLAB 模板、运行时边界、执行控制及环境回归共 99 项通过、1 项真实 MATLAB smoke 条件跳过。改动涉及的 3 个 Python 文件 Ruff check/format、`preprocessing.py` Mypy、91 份 Markdown 文档规范和 `git diff --check` 均通过。验证使用注册目录与合成/Mock Worker；没有调用真实 Provider、MATLAB 或 DPABI。

## 2026-10-20 Work 预处理 subject/session 范围预检

Work Skill dry-run 与正式 resolve 现在共用执行编译器实际使用的 subject-scope helper。仅当 `request_preprocessing=true`、将进入 DPARSFA preprocessing path 时，多个 session 才作为当前执行器不支持的能力在 Plan 持久化前阻断；Work dry-run 返回 `manifest.subjects.session_id` 和逐 session 整理建议。metric-only intent 跳过此 preprocessing 限制，同一 session 中多个 subject 仍可编译。该检查不写 Plan、Approval、Run 或工作区文件，也不放宽 Worker/compiler 的既有边界。

覆盖双 session BIDS dry-run `ready=false`、无写入及正式 resolve 在环境探测/Plan 写入前失败；另验证 metric-only bypass、同 session 多 subject 编译、执行端仍以同一错误类型拒绝多 session，以及 Work feedback 将此问题映射为 `BLOCKED_CAPABILITY`。相关 dry-run、编译、Work feedback 与 Work dialogue 回归共 50 项通过（1 项既有 Starlette/httpx deprecation warning）。Ruff check/format、4 个源文件 Mypy、91 份 Markdown 文档规范检查和 `git diff --check` 通过。未运行真实 Provider、MATLAB 或 DPABI。

## 2026-10-18 analysis-only 结果解释续接与进度投影

已有外部来源的 analysis-only CompositeGoal 在真实统计 WorkTask 完成后绑定同一任务的结果解释 outbox；结果消费者继续使用目标累计模型预算。绑定和消费前后重核当前外部来源 revision、批准的统计 Plan、活动分析尝试、Run、唯一真实 StatisticalResult 及其登记 Artifact；解释消息写入时再次核对 intent、task、result ID/version。conversation-scoped 进度查询现在投影此类目标，并在 Work 页面显示“统计分析/结果解释”阶段。来源变化会阻断，缺少草稿或用户决定时保持等待；用户仍负责审查统计方案、批准并启动 Run。

`test_analysis_only_result_continuation.py` 后端定向回归 13 项通过；`AgentPage.test.tsx` 64 项通过。后端 Ruff 对本切片文件通过，Mypy 307 个 Python 源文件通过；前端 OpenAPI 类型重新生成后 TypeScript 检查与 ESLint 通过；文档规范检查覆盖 89 份 Markdown，`git diff --check` 通过。验证未运行全量后端或前端测试，也未调用真实 Provider、MATLAB 或 DPABI。设计边界见 [ADR 0050](../adr/0050-analysis-only-result-continuation.md)。

本切片仅覆盖用户明确选定的单个外部来源及现有统计能力。它没有完成通用目标调度、多来源/多指标自动拆分、跨 Run/session 编排、FC、任意 DAG 或整个计划 0014。

## 2026-10-18 analysis-only 前台续聊与显式新建

Work 前台现在按服务端 Analysis role slot 恢复 analysis-only 外部指标目标。正常补充复用同一 CompositeGoal root、原始请求 hash、冻结来源 revision 和累计模型预算；缺少账本或来源 revision 变化时，在模型调用前失败关闭。`analysis_intent_task_id` 仅断言当前服务端目标，目标切换仍走既有选择 API。用户显式勾选“新建独立分析目标”后，HTTP 幂等键摘要稳定寻址新 root，request/source 身份保存在 root 并在复用时核对：同键同身份重放幂等，同键改正文或来源冲突，不同键允许相同正文/来源建立另一个 root。Work 回合的持久幂等记录使用 key 摘要；升级前的原始 key 记录会先匹配旧版请求摘要（移除当时不存在的两个分析目标字段），再在同 key 重放前转为当前摘要；不匹配的旧请求身份不会被改写。新记录从不保存原始 key。选择和最终提交复核来源及前台目标，模型期间的来源/Analysis slot 变化会拒绝迟到回复。analysis-only 冻结 Plan/Run 恢复只读；显式切换到 preprocessing Agent 的回合不消费 Analysis 目标预算。

新增前台恢复回归 14 项，连同分析意图、预处理交接和 WorkTask 来源绑定回归共 57 项通过；Agent 页面 `AgentPage.test.tsx` 67 项通过。OpenAPI 与 TypeScript schema 已重新生成；前端 TypeScript、ESLint 通过，Ruff 检查涉及的 11 个后端/测试文件通过，Mypy 检查 10 个源文件通过，文档规范检查覆盖 90 份 Markdown，`git diff --check` 通过。定向 pytest 使用 `--no-cov`，此前无此选项的同类定向运行只因项目全局 coverage fail-under 退出，不是测试失败。本切片未运行全量测试、真实 Provider、MATLAB 或 DPABI；没有新增数据库迁移，也未完成计划 0014 的通用 dispatcher、多来源自动拆分、跨 Run/session、FC 或任意 DAG。

## 2026-10-18 普通分析任务只读结果兼容性

前台只读结果查询现在区分普通 standalone Analysis task 与 CompositeGoal task：没有 `analysis_intent_task_id` 的冻结引用，只有在持久 WorkTask ID 未关联任何复合目标时才保留既有 standalone 查询路径；无法确认任务身份或该任务确属复合目标时仍返回冲突，避免将缺失的目标身份降级为普通分析。

`test_work_task_bindings.py`、`test_frozen_analysis_goal_selection.py` 与 `test_frozen_analysis_readout.py` 定向回归共 32 项通过，覆盖模型/无模型 standalone 只读结果查询，以及真实 CompositeGoal task 丢失 intent 身份时拒绝读取。Ruff check/format、全量 neuroagent Mypy（305 个源文件）、文档规范与差异空白检查通过；测试仅有既有 Starlette/httpx deprecation warning，未调用真实 Provider、MATLAB 或 DPABI。

## 2026-10-18 已有外部来源的独立统计目标

Work 对明确选择的一个当前项目外部纳入来源，现在在首次草稿模型调用前写入 analysis-only CompositeGoal root 和累计模型预算。目标绑定导入 ID、版本、源树 hash、inclusion hash、manifest hash 及组合 revision hash；建根时服务端在同一事务再次核验来源仍处于 included 且身份未变化。此目标没有 preprocessing task、preprocessing Run 或异步 proposal/result outbox。冻结统计 proposal 随后绑定目标的分析 intent 与 Statistical WorkTask，保留既有用户审查、Plan Approval 和显式 Run 创建门禁。

纵向集成测试确认：Provider 首次调用开始时 root 与预算已持久化，首个 invocation 已记账；统计任务停在 `WAITING_USER`；未批准的 Run 被拒，显式验证/批准后才允许创建 Run。`test_composite_goal.py`、摘要/选择、两类预算测试、WorkTask 绑定、frozen-analysis/readout 与 migration 回归共 100 项通过，包含旧 CompositeGoal 预算回填和 0033 重建后根身份保留。Ruff check/format、全量 neuroagent Mypy（306 个源文件）、88 份文档规范检查和 `git diff --check` 通过；有既有 Starlette/httpx deprecation warning。迁移 0033 保留既有预处理根和预算/reservation 行；降级遇到 analysis-only 根则拒绝丢失 source identity。没有运行真实 Provider、MATLAB 或 DPABI。

本能力只使用现有外部导入和统计设计装配器支持的单 Work conversation、单个明确来源及现有统计组合。没有科学参数推断、来源猜测、自动批准/启动 Run、多 Run/session、多指标拆分、FC 或任意 DAG。analysis-only root 尚未接入现有仅针对预处理复合目标的进度列表、后台 outbox/续接与统一完成投影，因此不表示计划 0014 的通用自动化体验已完成。决策见 [ADR 0049](../adr/0049-analysis-only-external-import-goals.md)。

## 2026-10-18 复合目标累计模型预算

复合目标根现在拥有独立的持久模型预算和调用预留账本。分析提案自动续接与统计结果解释两个 outbox 共用 intent 级的步数、Gateway invocation 数、token 和累计耗时上限。工具循环每轮及 JSON 单次回退都会在模型分发前原子预留；只有 reservation 状态为 `reserved` 才允许请求。elapsed allowance 预先扣减并限制本地 Gateway await；超时会请求取消，取消清理仍运行的实际观测耗时也计入账本并可形成 overrun；进程退出会保留未结算的预扣。返回的 Provider usage 按实际 token 幂等结算；缺失 usage 时保留估算值，额外报告的 profile failover attempts 以每次一份请求估算 token 收费。预算耗尽时 outbox 转为 `WAITING_USER` 且不开放重试，确保手动 retry 不会重置额度。缺失预算记录、重复 reservation 和异常状态均失败关闭，不会误报成额度耗尽。claim fence 丢失会取消本地生成任务，旧结果不会提交。迁移 0031 为现有根回填精确预处理 WorkTask 的预算上限，0032 加入持久耗时预留字段。

分析 outbox、结果解释 outbox、CompositeGoal、预算存储、运行接线与持久化重启的扩展定向后端回归通过 175 项；其中覆盖预算重放/耗尽、elapsed 预扣、deadline、取消后迟到响应、fence 丢失、两类 outbox 缺失预算时失败关闭、重复 reservation、0031 到 0032 的旧预留行保留和已报告 failover 的保守 token 计费。Ruff check/format、305 个源文件 Mypy 和 87 份文档规范检查通过；验证没有调用真实 Provider、MATLAB 或 DPABI。

历史 CompositeGoal 的 Provider 使用记录无法从旧消息/outbox 可靠还原，0031 将旧已用计数初始化为零；只有迁移后发生的调用才由此账本记录。预算目前接入两个后台 outbox，尚未涵盖普通前台 Work 对话、建根前生成和其他通用调度阶段。Gateway 内部 attempts 按一次逻辑 invocation 计调用；成功返回且报告的额外 attempts 才能逐次估算收费，失败且没有 usage 的内部重试无法精确重建。本地超时可能等待取消清理；远端 Provider 也可能在取消后继续处理或计费。设计决策见 [ADR 0048](../adr/0048-composite-goal-model-call-budget.md)。本记录不代表完整计划 0014 或真实模型/计算路径验收已完成。

## 2026-10-17 未冻结分析目标选择与上下文隔离

新增目标选择 API 与 Work 流程卡片入口，恢复尚无统计 Plan/Run 的最新草稿及原始提案消息。不同目标的后台续接不抢占角色槽；草稿、历史、交接、自动路由和模型来源上下文使用同一 intent 范围。只读查询复用原草稿时显式标记不变，保留已有正式方案/运行引用；较新不完整修订仍清除旧提案资格。复合目标冻结请求必须携带原始 proposal message ID，相同 proposal hash 的另一目标不能被迟到请求误冻结。

最终定向后端回归 210 项通过，涵盖新增选择集成测试 22 项、跨轮身份、双角色交接、Work 任务绑定、outbox、统计结果、API 和流式回复。前端 AgentPage 52 项通过；全量 neuroagent Ruff 与 Mypy（301 个源文件）、相关格式检查、前端 ESLint/TypeScript/生产构建、文档检查（85 份）及差异检查通过。只观察到已有 Starlette/httpx deprecation warning。测试使用 Mock 模型/执行器和合成 fixture；没有真实 Provider、MATLAB 或 DPABI 运行，未作全量后端/前端测试通过声明。

Plan/Run/已完成目标的精确恢复仍未开放；下一切片须从冻结 Plan 的原始 work_context 核验消息、Task/Step、Run 和当前 attempt，防止恢复旧确认。通用目标 dispatch、累计模型预算和计划 0014 的其余阶段仍未完成。

## 2026-10-17 冻结分析目标恢复与只读查询

按 [ADR 0047](../adr/0047-frozen-analysis-goal-restoration.md)，选择入口现在可恢复原提案消息指向的冻结 Plan、WorkTask 和可选 Run，并严格匹配当前 analysis attempt、Task/Step、设计、manifest 与来源身份。冻结身份不完整或较新草稿确有变化时返回冲突；`analysis_draft_unchanged` 快照仍保留原消息引用。当前环境不参与历史只读身份判断，Plan 校验、审批和启动继续使用既有环境与授权门禁。

自动与显式只读查询均经只读恢复入口。统计结果、进度和 QC 查询限定到选中的统计 Run；未绑定 Run 的冻结方案返回尚未启动状态，不发起项目级结果列表读取，并拒绝携带可能属于其他 Run 的 QC review ID。模型输入仅含冻结方法和限量聚合事实，无 Run 时不取结果事实；工具调用关闭，执行上下文仅允许所选 Run。对只读回合提出的新科研修改不形成草稿或任务。

冻结选择、查询和隔离相关后端定向回归 43 项通过；前端提案与 AgentPage 定向回归 73 项通过，TypeScript 和相关四个文件的 ESLint 检查通过。后端命令需使用 `--no-cov`，因为单独运行定向测试模块不覆盖全仓 85% 覆盖率门槛。复合完成态测试只构造持久身份夹具，不提供有效统计结果证据。测试使用 Mock 与合成数据，未调用真实 Provider、MATLAB 或 DPABI；完整计划 0014 及真实模型/计算验收仍未完成。

## 2026-10-17 分析目标身份继承修复

`test_consumed_analysis_intent.py`、摘要、复合进度及两角色交接回归共 51 项通过。覆盖自动缺项草稿补齐、连续参数修订、模型不能修改目标身份、来源冲突不调用模型或写消息、分析补充 ALFF 路由、无关预处理运行和其他目标完成消息隔离。摘要对相同原请求的重复快照去重。未调用真实 Provider、MATLAB 或 DPABI；本记录不证明目标选择入口或完整计划 0014 已完成。

## 2026-10-16 复合目标摘要及来源登记回归

当前流程卡片可显示原分析请求的有界摘要。应用层只接受同一 snapshot 中可唯一核验、匹配 intent、会话、项目、任务类型与请求 hash 的原请求；相同请求可在续接快照中重复，歧义、hash 或身份错配时返回空值。显式来源登记和共享来源 fanout 已在后续切片接通，下面 2026-10-15 基础验证中“尚无前台登记入口”的限制是当时状态。

本次执行 `test_api.py::test_health_openapi_and_error_envelope`、`test_composite_goal_summary.py`、`test_composite_goal.py` 和 `test_work_task_conversation_bindings.py`：14 项通过，包含 API 字段合同、摘要长度/空白与身份拒绝、复合进度和来源登记纵向回归。文档规范检查通过 84 份 Markdown，差异空白检查通过。只有既有 Starlette/httpx deprecation warning；未运行真实 Provider、MATLAB 或 DPABI。多目标的明确选择与继续交互、通用目标级恢复仍未验证或完成，不能由这些定向测试推断整体自动化验收通过。

## 2026-10-15 多分析意图共享预处理来源的持久化基础

新增 SQLite 迁移 0030，允许多个 identity-only CompositeGoal 根和 analysis proposal outbox 引用同一预处理 WorkTask；仍以各自的 intent ID、会话、项目和原始请求 hash 隔离目标。迁移保留既有根、outbox、attempt、索引和触发器，并在无法无损表示多重来源关联时拒绝降级。预处理完成事务按稳定顺序读取 outbox，重核精确 Task、conversation、project、Plan、manifest、Run 与 intent 来源，仅激活仍待处理且证据匹配的目标；多个有效记录在同一个 Unit of Work 中 READY。错误 hash 的记录保持 PENDING，不阻断其他目标。

相关后端回归 77 项通过；Mypy 检查 300 个源文件，Ruff check/format、迁移测试及 `git diff --check` 通过。该验证覆盖持久化和应用层 fanout 基础，不覆盖前台多 intent 注册入口：当前 conversation 预处理绑定仍只创建单个 canonical intent/outbox，自然语言目标拆分和按 intent 隔离的统计 WorkTask ID 仍未接通。未运行 MATLAB/DPABI。

## 2026-10-14 Work 统计候选只读 dry-run

Work 统计候选在保存/确认前复用正式 proposal assembler 与确定性来源合同。预检要求明确检验 tail、缺失值策略、校正方式（显式 `none` 表示不校正）及协变量中心化；缺项与无法证明的旧草稿选择进入 `ASK_USER`。内部候选核验已登记 Artifact 的完整 lineage、内容哈希及当前 manifest；外部候选复核纳入记录、人口学 revision、影像目录和 mask 哈希。校正及设计矩阵、Skill 参数 Schema 通过后才生成仅供用户审查的 proposal。未识别的校正字段、来源变化、mask/lineage mismatch 和统计选择缺项均关闭候选。Work 直接 preparer 也应用选择 gate，因此内部调用方不能绕过只读反馈入口的决策合同。

定向验证命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/science/test_statistics_draft.py tests/backend/test_work_dialogue_feedback.py tests/backend/test_work_analysis_handoffs.py tests/backend/test_external_single_group_import.py tests/backend/test_external_analysis_imports.py tests/backend/test_statistics_producer_environment.py tests/backend/test_statistics_mock_finalization.py tests/backend/test_statistics_failure_matrix.py tests/backend/test_analysis_outbox_consumer.py -q -o addopts=''
```

结果为 158 passed、1 项依赖 deprecation warning。另行验证：相关 Python 文件 `ruff check` 与 `ruff format --check` 通过，全量 `neuroagent` Mypy 检查 291 个源文件通过，`scripts/check-docs.ps1` 通过。回归包含 no-side-effects 表/文件快照、内部 Artifact lineage 与精确 allowlist、外部文件哈希后重新读取纳入 revision、outbox continuation 进入同一 dry-run callback、未知校正参数拒绝及直接 Work preparer 拒绝未决选择。本切片未运行真实模型、MATLAB 或 DPABI；dry-run 不探测本机环境，也不创建 Plan、Approval、Run、事件、授权、任务或产物，不能作为实际执行成功证据。

## 2026-10-10 分析解释事实与方法概况

### 内部来源的真实网页与完整下载验收

`DemoData/internal-browser-files-20261010/browser-verification.json` 记录从已完成合成预处理与 QC 的只读副本，到新统计草稿、正式设计、独立审批和真实公共 Worker 的网页操作。方案 `59837428-24d7-4fe7-9d22-d0e1e2bab1f6` 冻结 AAL 版本，运行 `e39f22c2-67d3-419e-bf43-9fe73d1a46be` 成功。按原参数两样本 t 与双侧 FDR q=0.05，独立 T 图最大绝对误差 `4.6573917167336276e-7`，ROI 均值误差为 0；7 份原来源输入与实际清单内容哈希一致，14 份登记产物下载完整性均通过。

网页实际下载了统计脚本、bootstrap、配置、输入清单、校正 NIfTI、Markdown/JSON 报告、CSV/TSV 簇表及 PNG 切片，共10份文件，见同目录 `browser-downloads/`。四份执行证据为本次实际生成、执行前后校验并登记的文件，不重建历史脚本。结果得到1个负向体素；簇表联动到峰值切片10/36，native 与 MNI 的已知空间冲突明确阻止 AAL 定位，不使用未知空间提示。

手动切换恢复预处理运行/QC与分析运行，参数草稿保留。网页解释读取实际 df=4、组别方向及 FDR p 截断；新浏览器发现 READ 路径会丢失工作区引用，已修复并补两条模型/无模型服务回归。通过页面重新选回原工作区后，再次查询及另一全新浏览器恢复相同方案、运行、图谱和结果面板，草稿版本保持不变。证据包含 `page-preprocessing.txt`、`page-analysis-restored.txt`、`page-readout-fixed.txt`、`page-fresh-browser-fixed.txt`、`page-cluster-linked.txt` 和 `page-native-roi.txt`。

本验收使用 Mock 规划与确定性解释、真实 MATLAB 计算，原预处理数据库和产物未改写，不声称验证了真实 Provider 质量或 ASD/HC 研究差异。角色任务与草稿复用现有 SQLite；浏览器及图谱解析缓存可重建，不承担科研状态持久化。

方法摘要从冻结报告和结果清单复核 bundle、设计及产物绑定，实际校正和软件数值读取通过路径、大小及内容哈希核验的登记文件。分析角色的三个 READ 工具分别提供方法/推断事实、冻结图谱定位、按组与条件的 ROI 聚合；不发送完整报告、受试者标识或逐受试者取值。初始上下文在解释请求中也提供同一事实，ROI 明确限首个登记簇，不作独立验证或第二次统计检验。

`scripts/verify-statistical-interpretation.py` 只读已有合成验收数据库并克隆到新目录，以明确标记的 Mock Provider 验证公共 Work API、上下文和工具审计。已有真实结果、源文件哈希及原运行状态均未改变：

| 场景 | 验证行为 | 工作区外证据目录 |
| --- | --- | --- |
| AAL 显著结果，单次模型 | 已核验方法、峰值标签、整簇覆盖及 ROI 聚合进入模型；无工具能力也能解释 | `DemoData/interpretation-aal-single-shot-20261010-r2` |
| 原生空间结果，工具模型 | 汇报空间已知但与 MNI 不同，阻止该图谱定位；ROI 仍为同网格描述 | `DemoData/interpretation-native-20261010` |
| FDR 零结果，单次模型 | 正常报告本次校正阈值下无显著体素、无脑区可定位；定位/ROI 空结果，没有空间未知提示 | `DemoData/interpretation-zero-20261010` |

这些记录证明事实和权限接线，不证明真实 Provider 的规划或科学推理质量。最终树全量后端 `1299 passed, 7 skipped`，覆盖率 `85.55%`，日志及覆盖率证据位于 `DemoData/quality-gate-backend-final-20261010-r6`；前端 `215 passed`，语句/分支/函数/行覆盖率分别 `86.29%/81.46%/83.73%/90.09%`，均通过既定门槛。统计故障矩阵包括75项运行、输入谱系和产物缺失/损坏回归；另有23项图谱几何/覆盖验证、12项定位服务和12项方法证据回归。执行文件登记及完整/缺失/修改的结果合同回归通过。Ruff、199个源文件Mypy、生产构建、7项Mock浏览器E2E、依赖审计和文档检查通过。Python审计不包含未在PyPI发布的本项目；619个仓库文件模式扫描通过，本机未安装gitleaks。内部网页真实执行、下载与新浏览器恢复通过，计划0013的20项原范围证据矩阵审计完成；不把Mock规划或浏览器测试视为真实模型质量证明。

最终路由增加49项中英文/承接/歧义回归，路由、交接和任务绑定定向检查109项通过；实际服务的4项路由检查保存为 `DemoData/internal-browser-files-20261010/routing-verification.json`，既有真实运行内容未变。空任务模糊请求先询问，不调用模型或创建科研方案；预处理诊断与统计故障分别路由。全量后端首次因一项旧文案断言失败，修正为检查澄清状态及无工具调用后，最终1299项通过。最终前端门禁重新执行锁定安装、客户端生成、Lint、类型、覆盖率、构建、7项浏览器回归及依赖审计，完整通过；日志为 `DemoData/quality-gate-frontend-final-20261010-r2.log`。Ruff检查460个文件，Mypy检查199个源文件；72份docs文档规范和124份Markdown政策检查通过。本机未安装gitleaks，已运行项目模式扫描，不将其描述为gitleaks验证。

最终20项范围、14份真实验收记录、六类smoke及门禁日志的SHA-256索引保存为工作区外 `DemoData/dual-agent-final-audit-20261010.json`；[计划0013](../plans/0013-work-dual-agent-analysis.md)已标记Implemented。此完成状态对应本计划首期范围，不将计划0012仍未完成的其他预处理体验场景宣称完成。

## 2026-10-10 补齐六类公共 Worker 与 GRF 实际证据

`scripts/verify-statistical-capabilities.py --test <类型> --real-matlab --output-root <新目录>` 分别生成 12 名受试者的 10³ 合成指标，乱序纳入与人口学表。通过外部检查/纳入、统计草稿装配、正式方案、校验、独立审批、公共 Worker 到结果登记；独立 NumPy/SciPy 模型核对冻结矩阵/对比、统计图、效应、自由度及校正。来源文件和产物大小/哈希全部核验。

| 新补能力 | 运行 | 统计最大绝对误差 | 效应最大绝对误差 | 证据目录 |
| --- | --- | --- | --- | --- |
| 单样本 t，基线 2，双侧 GRF | `90c581a9-1275-4e07-b7fb-6d0b6adf1dff` | `4.753e-7` | `2.350e-7` | `DemoData/capability-one-sample-20261010-r3` |
| 偏相关，控制 nuisance，双侧 FDR | `f04d27ad-f6c5-40cf-9ccd-787762d9a892` | `2.974e-8` | `2.974e-8` | `DemoData/capability-correlation-20261010` |
| 多变量回归，目标 unit contrast，双侧 FDR | `03b5add2-4de5-4111-b122-6717be5ff541` | `3.574e-6` | `2.949e-8` | `DemoData/capability-regression-20261010` |
| 三组 ANCOVA 总体 F，GRF | `9da463e9-ff75-477e-a056-555adbf61a12` | `7.313e-6` | `1.828e-6` | `DemoData/capability-anova-20261010` |

结合既有两样本、带协变量两样本与配对公共 Worker 证据，六类入口均有真实服务链路记录。回归刻意将目标放在第二协变量，冻结对比为 `(0,0,1)`；总体 F 不登记组间方向。两个 FDR 独立 BH 实际 p 截断分别为 `5.17658574846394e-5` 和 `5.1765884496866845e-5`，与 q=0.05 分开记录。

单样本 GRF 实际读取统计头：DLH=`0.285068`，FWHM=`(2.409310,23.394576,8.206001)` mm，最小簇 4；ANCOVA 指定 DLH=`0.05`，最小簇 1，不推算 FWHM。两者有效 mask 为 512 体素。独立簇阈值公式与 26 邻域支持一致；额外固定脚本直接调用同参数 DPABI，校正原统计图和校正 Z 图误差均为 0。单样本前两目录为验收程序字段假设失败，已有计算结果保留，未修改科学参数寻找显著性。

`scripts/verify-grf-estimated-smoothness.py` 从上述合成T图生成数值及仿射完全相同、只移除平滑度描述的独立副本，真实调用DPABI自动估计分支，并从当前模板提取原样平滑度证据函数核对直接`y_Smoothest`。`DemoData/grf-estimated-20261010-r2/summary.json`记录 estimated DLH=`0.000714986056532791`、FWHM=`(6.090677,472.504707,60.579082)` mm、mask=512、最小簇=1；独立GRF公式及双侧26邻域支持核对通过。异常大的FWHM如实保留，它来自小型合成图，不能代表研究数据的有效平滑度。原图、mask和bootstrap哈希不变；本记录证明DPABI/模板分支，不声称该分支再次经过公共Worker或真实Provider。首目录计算产物已保留，但MATLAB退出超时，不列为通过证据；第二目录正常退出并完成核对。

## 2026-10-10 图谱方案冻结、缓存失效与三图谱真实验收

正式统计设计的图谱选择和联合文件 SHA-256 写入不可变方案，实际执行完成后进入冻结 Markdown/JSON 报告和结果 bundle。校验、批准及启动前重新验证；定位请求不能替换方案图谱或使用变化后的内容。历史没有图谱字段的方案及结果保持兼容。零显著体素先返回正常空结果，不触发图谱或空间检查。

通过 `scripts/verify-external-statistics.py --real-matlab --with-signal --atlas <图谱> --output-root <新目录>`，在三份隔离目录中分别执行六张合成 MNI 几何指标的真实两样本 t/FDR q=0.05、公共 Worker 登记、冻结报告读取、定位和 ROI 查询。固定八体素组效应保持相同，未改变方法或阈值寻找结果。三次独立 T 图最大绝对误差均为 `1.83091709970995e-6`，ROI 均值误差为 0；直接读取安装图谱并对已知八体素立方体进行逆仿射最近邻采样，峰值标签值与每个覆盖计数/比例全部一致。

| 图谱 | 实际运行 | 独立覆盖核对 | 本地证据目录 |
| --- | --- | --- | --- |
| AAL | `61fed1ee-7321-44b3-9c57-3373af214790` | 标签值 57：6/8；背景：2/8；峰值 57 | `DemoData/frozen-atlas-aal-20261010` |
| AAL3 | `400940a0-91cd-4d7d-bede-d64fa5ab6fb9` | 标签值 61：8/8；峰值 61 | `DemoData/frozen-atlas-aal3-20261010` |
| Brodmann | `4169d282-aa87-466b-a2e6-d207de9ca54d` | 标签值 30：8/8；峰值 30 | `DemoData/frozen-atlas-brodmann-20261010` |

每份目录的 `summary.json`、`localization.json`、`roi.json`、冻结报告和 SQLite 保存结果及版本指纹；来源 NIfTI 的运行前后哈希一致，未修改科研软件安装。标签值是对应离散图谱的编码，不把编码直接当作 Brodmann 区编号。本验收采用显式合成空间几何，不证明真实影像已完成标准化，也不表示 ASD/HC 的实际研究差异。

图谱解析缓存改用内容 SHA-256 作键、保持八项上限。独立回归用内容改变而大小/纳秒时间戳不变的 NIfTI 仿射与 MATLAB 标签表证明重新解析。正式方案版本变化在校验、批准、执行和定位各入口阻断；前端回归证明 AAL 可用时建议、用户选择写入正式设计、图谱目录失败时允许无图谱统计，以及结果仅使用方案冻结选择。这些证据不代替最终网页、完整能力和故障矩阵验收，仍按[计划 0013](../plans/0013-work-dual-agent-analysis.md)继续。

当前增量的后端、Agent 和分析辅助回归为 725 passed、1 skipped；相关前端 29 passed，生产构建通过。Mypy 检查 198 个源文件、Ruff 和 69 份文档规范检查通过。本次未运行最终全量覆盖率门禁，也不把三图谱验收等同于完整计划已交付。

## 2026-10-10 内部预处理、QC、交接与统计真实服务验收

`scripts/verify-internal-statistics.py --real-matlab --output-root <新目录>` 在隔离工作区生成六名合成受试者的 4D 序列和乱序分组表，通过现有服务及公共 Worker 执行真实头动校正、去趋势和 zALFF。每份序列 TR=2 秒、84 个时间点，明确删除 4 个；不做标准化、回归或平滑，空间保留为 native。只有此受控合成 fixture 的实际 QC 记录被确认，不修改既有研究运行的 QC。

验收修复了两个内部闭环缺陷：同一有效指标掩膜的多个受试者引用无法满足统计组级掩膜合同；结果登记将已解析的 lineage 对象再次传入 JSON 字符串解析器。前者现在在实际掩膜内容、空间和网格完全相同时建立共同组级引用，同时冻结并复核每位受试者的原始掩膜；后者正确读取嵌套结构，保留实际空间。不同掩膜不自动合并或重采样。

实际完成证据位于工作区外的 `DemoData/internal-handoff-acceptance-20261010-r2/verification.json`、`handoff.json`、`qc-review.json`、`roi.json` 和 Markdown/JSON 报告，以及该目录 SQLite 的运行、方案、QC 和结果记录：

- 项目 `e1410a55-82ca-432a-b036-d6302fd7549f`；真实预处理运行 `d4607285-84eb-4fb3-a22c-dec600400a70` 已成功并通过合成 QC，登记六张指标、六份受试者掩膜与一份共同掩膜。
- 分析交接哈希 `79a3e990e6f195ae94944a8adaba94ea9976efef44413726ac7a8f8c350c845c`，冻结完整证据。分组来自按 subject_id 对齐的服务端人口学修订；Work 方案和运行绑定独立确认作用域。
- 真实统计运行 `f43caa35-31b4-410e-a9eb-774bfe66c940` 完成两样本共同残差方差 GLM 和双侧 FDR q=0.05；从实际预处理指标独立计算 T 图，最大绝对误差 `4.6573917167336276e-7`；原始序列及分组源文件未变。
- 固定合成输入得到 1 个显著体素、1 个簇；其六名受试者的描述性 ROI 均值与直接读取该体素一致，误差为 0。该结果不表示实际 ASD/HC 研究差异；未改变参数寻找显著结果。簇空间正确保留 native，不声称通过 MNI 图谱定位。

首个较小视野 fixture 的头动重采样边缘非有限值落入有效掩膜，真实固定模板拒绝登记；运行 `9a251bd2-2d05-4885-9a4b-f6ff2bfb494f` 为 failed_terminal，仅登记执行日志。扩大合成视野后实际预处理通过。首次统计登记因上述 JSON 缺陷失败的运行 `881b5a64-56f7-41c1-87e1-1eedb6ffa32f` 保留失败历史；修复后用 `--resume-source` 复用已成功、已批准 QC 的来源，另行创建和确认统计方案，未重跑预处理或绕过失败状态机。该选项拒绝仍活动或已有成功统计结果的目录。

共同掩膜增量后的后端/Agent 全量回归为 674 passed、1 skipped；最后结果登记修复及相邻接口/交接/任务测试 30 passed。当前源树 Mypy 198 个源文件、涉及文件 Ruff 和 68 份文档检查通过；这不代替最终全量覆盖率门禁。本验收为真实 MATLAB 的服务集成与确定性提案，不证明内部流程网页操作或真实 Provider 的规划质量；完整交付仍按[计划 0013](../plans/0013-work-dual-agent-analysis.md)继续。

## Work 体验增量验收（2026-10-07）

## 2026-10-08 双 Agent 与六类 DPABI 统计验收

Work 已注册预处理 Agent 与数据分析 Agent，按任务意图路由并分别保存预处理草稿、统计草稿和角色审计信息；工具目录按角色隔离。统计结果面板支持簇表与轴向切片联动、哈希校验预览，以及 Markdown/JSON 复现报告下载。空间未确认时脑区定位仍会阻断。

六类统计入口（单样本 t、两独立样本 t、配对 t、相关、回归、单因素 ANOVA/ANCOVA 总体 F）已用本机 MATLAB R2024b、SPM25、DPABI V9.0_250415 执行真实 smoke；FDR/GRF 及对应统计量、自由度与产物检查通过。工作树回归 983 passed、7 skipped；Ruff、Mypy、前端 Lint、前端生产构建通过；前端测试 172 passed。覆盖率门禁未在本轮运行。

另以工作区副本中的少量 zALFF 图执行两样本 t + 双侧 FDR 技术演示，结果 0 个显著体素、0 个簇。该小样本结果不支持科学推断。零结果明确报告无显著差异及无可定位脑区；这条直接执行器演示不证明应用完整导入链路。外部目录的后续服务/Worker 验收见 2026-10-09 记录。

当前工作树全量后端回归 946 项通过、7 项条件跳过；覆盖率 84.20%，未达到 85% 门槛。全仓库 Ruff 规则与 400 个文件格式检查、183 个源文件严格 Mypy 通过。输入修订、事实撤回与重新冻结的合成集成测试未启动 MATLAB。

并发反馈修正后的前端全量 147 项测试通过；语句 84.21%、分支 81.07%、函数 80.97%、行 88.97%，均达到现有 80% 门槛。前端 Lint、生产构建和 5 项 Mock 浏览器回归通过。

Mock 浏览器回归 5 项通过，新增场景实际中断已接受启动的响应，随后通过只读查询恢复同一运行，刷新仍只有一次启动请求；并发方案变化时拒绝旧确认，关闭旧弹窗并显示新方案。前端已据此区分明确版本冲突与结果不确定的网络错误。这些场景使用模拟接口，不证明真实服务故障注入闭环。完整目标仍按[计划 0012](../plans/0012-work-conversation-ux.md)验收。

后续 Mock 浏览器增量 7 项通过：启动时发现输入变化后，通过 `work-review` 获取失效草稿，页面保留参数、标注旧来源待重读并提供起点重确认；不会再次提交启动命令。模拟 Worker 取消验证确认取消中态与实际终态分开显示。服务端对应输入恢复和新清单编译有合成集成覆盖，真实服务故障注入与执行中取消仍未验证。

当前前端最终回归 148 项通过，语句/分支/函数/行覆盖率分别为 84.36%/81.13%/81.17%/89.11%，前端既定 80% 门槛通过；Lint、严格类型与生产构建通过。对应提交尚未创建，本结果描述当前工作树。

Work 的未校验模型正文不再进入 SSE 或页面，等待期间显示实际模型请求、工具派发和方案校验阶段；正式结果通过持久化 final 收口。Chat 保留原正文增量。相关后端流式/对话/工具循环测试 74 项通过，前端阶段/断连/问答后确认测试 4 项通过，Lint、构建和相关 Python 类型检查通过。后继合同见 [ADR 0023](../adr/0023-work-validated-answer-stream.md)。

新版真实浏览器会话已观察到模型阶段状态与正式草稿，没有展示模型原始增量。此流程发现 JSON 敏感行省略会破坏片段解析，已增加限范围的数值元数据证据，身份正文仍省略；并修复明确停用滤波/平滑时遗留的活动配置。工具与规划相关回归 28 项通过，数值证据及明确停用回归 20 项通过。该阶段尚未启动计算，不作为完整端到端成功证据。

继续网页复核时，已读取数值证据并推导层顺序，当前草稿的科学参数与原批准 ALFF 草稿一致（对比忽略输入绑定编号、确认哈希与展示标签）。暴露的重复结构像编号提问已通过 Segment 复用已选 T1 输入修复，显式不同输入仍保留；页面已选起点而模型没有填写阶段建议时，服务端现在仍签发对应的审查入口，防止错误地先要求编译再报缺少起点。

本轮确定性增量修复保留跨回合采集参数冲突，拒绝非法影像头数值，统一模型摘要/理由/问题契约，并将历史方案改为展开时加载只读控件。冻结输入测试证明 Work 阶段计时不改变输入内容哈希。全量 Python 测试 922 项通过、7 项条件跳过；覆盖率 83.29%，低于 85%，因此覆盖率门禁尚未通过。相关 Python 类型与规则检查、前端 Lint 与构建通过。

前端全量覆盖率运行 131 项通过；语句/行覆盖率 93.02%、分支 86.43%、函数 80.36%，达到配置的 80% 门槛。历史只读完整面板测试在覆盖率插桩下需要更长时间，采用独立 30 秒预算，断言不减少；此测试耗时不能作为实际浏览器性能结论。后续参数冲突提问与推荐理由正反场景的后端增量回归 44 项通过。

真实网页通过科研自然语言补充方案、起始阶段按钮和最终启动按钮创建了独立 ALFF 运行；没有通过开发接口注入方案或启动。运行 `f6651354-0697-46f4-b2d4-5e7a695f3442` 已进入 `qc_review`，16 项产物大小及 SHA-256 有效。9 份 NIfTI 与原后端对照的几何信息、数值缩放、体素值完全一致，输入副本校验和未改变。

页面刷新后自动恢复同一运行、三张 ALFF 图像预览、头动 QC 和下载记录。实际点击下载的原始 ALFF 与登记校验和一致。结果尚未进行人工科学质量批准。验收中发现 QC 事件编号被误脱敏，已修复系统引用策略并用运行范围的持久化 QC 查询恢复页面；历史事件未修改。

该次 Worker 在阶段识别修正前启动。修正后的解析器先在实际日志回放中识别层时间、头动、配准、分割、标准化、回归和指标计算等固定消息；日志回放本身不能证明实时展示。更新后独立运行的实时证据见下方记录，性能分段分析及完整交互矩阵仍需完成。全量门禁要求不因本节增量验证而降低；实施进度见[计划 0012](../plans/0012-work-conversation-ux.md)。

## 最终树必须通过的自动门禁

2026-10-07 本次阶段提交前复核：相关后端回归 111 项通过、1 项条件跳过，AgentPage 组件测试 29 项通过。全仓库 Ruff 规则检查、183 个源文件的严格 Mypy、前端 Lint 与类型检查、59 份文档规范检查通过。全仓库格式检查仍有 13 个未修改文件待整理；此前后端覆盖率 83.29% 的缺口未消除，尚未通过完整门禁。

更新后真实 Worker 的独立运行 `0606d631-3fa8-4ad9-9a9a-24990fc41be3` 已通过实际网页起点与最终启动按钮创建。启动前草稿科学参数与原批准方案一致；此前阶段保存时回读状态为 `running`。

该运行现已进入 `qc_review`，页面记录计算耗时 11 分 17 秒。实时浏览器观察到层时间、头动、T1 配准/分割、标准化、回归、指标和平滑的已完成事件；实际页面自动呈现 16 项产物、三张 ALFF 预览和头动 QC。所有登记产物大小及校验和有效，9 份 NIfTI 的几何信息、缩放和体素值与原后端对照完全一致，输入副本未改变。实际下载原始 ALFF 的校验和与登记值一致，刷新后恢复同一结果。尚未人工批准 QC。

该会话两轮读取型规划的总耗时分别为 12.498 秒、9.592 秒，其中模型阶段分别为 12.048 秒、9.204 秒，文件工具分别为 195、198 毫秒，各含 6 次模型请求。起点确认/自动编译总耗时 258 毫秒，其中完整性哈希 108 毫秒；最终启动总耗时 183 毫秒，其中哈希 107 毫秒、排队 3 毫秒。阶段可能嵌套，不能相加；此单受试者样本显示主要等待来自模型往返，不能据此取消冻结及启动完整性保证，也不能推广到大型队列。

窄屏真实审查发现已执行方案仍可本地编辑、并显示等待补充；已改为冻结只读状态，更新后的真实页面已核实只读标题、冻结说明及禁用 TR/保存操作。已执行方案不能重新签发启动审查，新增页面测试通过。新增取消失败回归发现操作错误被详情刷新清除，现将操作错误按运行单独保留。起点审查正文和弹窗使用确定性说明，保留内部授权签名但不展示模型的技术口令或虚假启动声明。相关后端 34 项、面板相关前端 34 项测试通过；窄屏焦点与滚动约束、起点回复和取消失败的完整页面路径仍需复核。

完整后端测试 928 项通过、7 项跳过，覆盖率 83.43%，仍低于 85%。全仓库格式、规则与严格类型检查通过。依赖锁更新至 pypdf 6.19.0、urllib3 2.8.0 和 Vitest/覆盖率工具 4.1.11，Python 与前端依赖审计通过；升级后的 PDF/文件工具回归 11 项通过。面板修复后的前端全量 142 项测试通过，语句 83.55%、分支 80.37%、函数 80.58%、行 88.54%，四项均通过 80% 门槛；Lint、类型、构建、3 项 Mock 浏览器测试及 API 类型生成检查通过。

后续窄屏审查使用同一个编辑器容器切换桌面/全屏视图，打开时将焦点移入、隔离背景并停止背景滚动。390 像素真实浏览器观察到打开后聚焦“收起方案”，Shift+Tab 到最后可见入口、Tab 返回首项，Escape 后聚焦原入口且滚动恢复。组件测试覆盖屏幕宽度切换后未保存值保留、嵌套确认不被外层 Escape 关闭。前端全量 145 项通过，语句 84.11%、分支 80.73%、函数 80.93%、行 88.90%；Lint、类型、构建和 3 项 Mock 浏览器测试通过。实际嵌套最终确认及窄屏保存冲突路径仍需进一步验收。

文件恢复测试发现新建文本的不存在节点被误判为符号链接。现仅在明确新建路径时允许缺少文件节点；其余路径错误仍拒绝，构建及应用均保持默认目录排除。文件工具/授权增量 30 项通过，覆盖 DOCX/XLSX 有界证据、敏感行省略、格式错误后的再次读取、审查后文件变化拒绝、部分替换失败的完整恢复、新文件目录与影像格式边界。此前全量后端 939 项通过、7 项跳过，覆盖率 83.99%，仍未达到 85%；之后的目录排除修复已通过上述增量验证，最终树全量门禁仍需复核。

`scripts/quality-gate.ps1` 定义当前工作树的本地汇总门禁：

- Python：Ruff 格式/规则、strict mypy、依赖审计、pytest 与不低于 85% 覆盖率。
- Web：ESLint、TypeScript、Vitest 与不低于 80% 覆盖率、生产构建、Playwright Mock E2E 和依赖审计。
- 科研契约：Skill schema、稳定计划哈希、DPABI 稳定参数字段快照、ALFF/fALFF 与 ReHo 顺序、Nyquist/频段/ReHo 邻域、受试者与协变量对齐、统计方向，以及可审计的 QC 选择。
- 执行：SQLite 原子领取、租约心跳、失败/超时/取消/恢复、运行进程标记与原子恢复锁、带所有者和 Provider 等待心跳的请求幂等租约、固定 MATLAB 模板、Windows 空格路径、进程树终止、按 attempt 保留的大输出日志，以及仅登记本次执行新建或变化的非空普通文件（含 SHA-256/大小）。
- Web 恢复：多项目 `localStorage` 指针切换、Plan/QC/统计冻结内容重载、待确认幂等 key 的 `sessionStorage` 恢复，以及基于 `after_event_id` 的事件续传。
- 安全：源目录只读、工作目录边界、外发摘要去标识化、结构化模型输出、仓库敏感信息/科研二进制/大文件扫描。

本文不把历史子集测试替代最终门禁。验证证据必须记录最终暂存树哈希、执行日期、门禁结果和未执行项；在该记录存在前，不声称当前树已全绿。

对应风险回归主要位于 `tests/backend/test_database_runtime_lease.py`、`tests/backend/test_release_scripts.py`、`tests/backend/test_dataset_services.py`、`tests/science/test_execution_edges.py`、`web/src/api/client.test.ts` 和 `web/src/App.test.tsx`。

## 2026-09-29 对话流式变更验证

本次仅验证 [流式对话计划](../plans/0010-conversation-streaming.md) 的变更范围，未运行完整汇总门禁，
未调用真实 Provider、RAG 服务或 MATLAB。

- 相关后端与模型回归：125 项通过，覆盖原生增量、模型回退 reset、工具调用分片、Work 正文隔离、非法动作拒绝、会话落库、断连和幂等重放。
- 前端 API 与工作台测试：36 项通过；TypeScript、所改文件 ESLint 与生产构建通过。
- 相关 Python Ruff、strict mypy、文档规范和差异空白检查通过，OpenAPI 及生成类型已同步。
- 扩大后端检查有 2 项原有断言失败：`test_clinical_tool_result_field_is_dropped_not_fatal` 仍要求已移除的临床文本过滤，`test_work_tool_loop_answers_in_the_json_contract` 的固定目录缺少已加入的 `get_run_diagnosis`。两项均在本次修改前的 HEAD 独立副本复现，因此不归为流式变更回归；完整门禁仍未全绿。

## 2026-09-29 Work 通用错误提示诊断

用户明确授权不含对话、工作区或科研数据的轻量模型检查。当前 DeepSeek 配置的合成流式 JSON 请求
返回 HTTP 200、finish_reason=stop，Work 结构校验通过；合成工具调用及后续响应均返回 HTTP 200。
本地重建上下文的出站校验通过。没有重发原对话或处理科研数据。

原失败轮次将模型请求、响应结构和参数校验异常合并，且未记录原因，因此无法从历史通用提示确定该轮具体根因。
已增加安全的 model_error 诊断，规则见 [API 契约](../api/api-v1.md#持久化对话与工具编排)。
Work/流式相关测试 30 项通过，覆盖输出截断、字段值保密、Provider 状态码及原有草稿保留行为；Ruff 与文档检查通过。
本次涉及模块的 mypy 有 6 项原有错误，已从修改前 HEAD 文件副本复现，未声称完整门禁通过。

## 当前可验证的本地闭环

- 项目、数据集、只读扫描、manifest、人口学对齐和数据集划分。
- Skill 解析/校验/编译、批准计划 `WorkflowFactory`/`ToolRuntime` DAG 排队、步骤事件与 Artifact 元数据登记。
- 在测试中注册类型化指标 Artifact 后的 QC revision、人工审批和统计设计校验；DPABI RealignParameter 头动证据可自动生成 QC 草稿。
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
7. `matlab_preprocessing` 已从冻结 SkillPlan/manifest 编译 DPARSFA 输入和 typed JobSpec，实际读取产物元数据并登记 lineage。当前范围是单会话 4D 输入；metric-only 路径限单主体。启用 Realign 的运行成功后会解析 `RealignParameter` 并登记 `qc.head_motion_report`；有指标图时自动生成 `pre_statistics` QC 草稿，无指标时生成仅供头动筛选记录的 `head_motion` 草稿。OnResults normalization 与组合指标若需不同阶段掩膜，以及没有指标输出的纯预处理 OnResults 操作，当前合同明确拒绝。2026-09-29 新增 T1 自动准备、分割和 DARTEL 的受控无 GUI 路径；仅 Mock/合成夹具验证，尚未完成真实 MATLAB 验证；EPI template normalization 要求先 realign 产生 mean image，仍未包含在本次真实 smoke 中。
8. 指标输入与已验证掩膜两端都通过 DPABI `y_ReadRPI` 做一致的轴翻转规范化，再严格比较维度和物理网格；这一步不重采样，不接受物理空间不匹配的掩膜。新产物的网格签名从本次实际输出计算，不复用掩膜 Artifact 的自报标签。

## 2026-09-04 已执行的授权 smoke

以下验证仅使用仓库外、确定性合成数据。输入、脚本、MATLAB 日志、产物及摘要保留于本机独立临时目录；仓库不存放影像或机器私有路径。测试参数仅为软件验证夹具，不成为科研默认值。

- 统计：`tests/science/test_matlab_statistics_smoke.py` 通过受控执行器运行三类 t 检验，影像为 10 × 10 × 10。检查单样本基线 2 与 FDR/BH、独立组混杂协变量、配对负尾 GRF、T 值与方向、效应量、分正负的 26 邻接簇表、必需产物和真实结果报告合同。
- 数值验证：真实输出与独立 OLS/配对差值公式比较，绝对容差 `1e-4`；`tests/science/test_statistics_runtime_contract.py` 直接执行模板效应量代码，验证协变量对比方差与无协变量退化公式。报告元数据改为从实际 provenance 文件及观察环境计算哈希后，两个文件重跑共 4 tests passed，105.42 秒；每个 MATLAB 作业均设置 180 秒上限。
- 实际软件证据：MATLAB R2024b（24.2.0.2712019）、SPM25（25.01.02）、DPABI V9.0_250415。运行时核验统计入口分别接受 7/6/6/9/12 个参数，模板原调用正确，不存在先前评审推测的参数过多问题。
- 预处理：公共 Worker 到 QC 路径的小型 smoke 已通过；124 个 volume 删除最初 4 个后保留 120 个，完成 detrend，源文件 SHA-256 不变。组合 metrics 同样通过公共 Worker 到 QC，登记了时序、ALFF、fALFF、ReHo、执行日志和 metadata evidence；所有指标记录实际 120-volume 时序依据，并绑定同一经物理网格核验的 mask。
- Provider：`zhipu / glm-5.3-flash` 的一次真实轻量调用成功，未发送受试者信息；发送上下文哈希为 `efaaa8c1639d09562745f1c9ac78758265d357005825b84515e8fb71459460ea`。

统计 opt-in 测试使用 `RSFMRI_RUN_MATLAB_STATISTICS_TESTS=1`；软件路径来自 `RSFMRI_MATLAB_EXECUTABLE`、`RSFMRI_SPM_DIR` 和 `RSFMRI_DPABI_DIR`。默认 CI 跳过真实 MATLAB 测试。以上结果不代替最终暂存树的质量门禁、多角色终审或远程 CI。

## 2026-09-18 Work 对话真实 DemoData ALFF 冒烟

本次由用户明确授权，仅使用仓库外 DemoData `Sub_001` 的独立可写副本。Work 对话自然语言创建项目、数据集、冻结 manifest 和待审批计划；用户复核参数后用精确确认语句批准并排队真实 MATLAB 作业。执行器记录为 `controlled_matlab`，Worker 最终将运行推进到 `qc_review`。

- 参数：TR 2.0 秒、234 个时间点、dummy 0、33 层奇偶 Slice Timing（参考层 33）、Realign、0.01–0.08 Hz raw ALFF；协变量回归、标准化、去趋势、滤波、Scrubbing 与平滑关闭。
- 实际步骤：DPARSFA 完成 Slice Timing、Realign 和自动掩膜，随后计算 ALFF。Docker Desktop 未运行的探测信息没有阻断 DPABI 自带的本地自动掩膜路径。
- 产物：登记 `timeseries.nii`、`brain_mask.nii`、`raw_alff.nii`、`preprocessing-evidence.json` 和 MATLAB 日志。时序证据为 TR 2.0 秒、234 volume；脑掩膜和 ALFF 的非有限体素数为 0。ALFF Artifact SHA-256 为 `55bd1a3ac3ca1caf9e7e8998e1c87bb50508f756a81cfcd6b5b3a7c02d8fa1f9`。
- 谱系：时序与 ALFF 都绑定本次生成并登记的脑掩膜 Artifact，网格签名一致；计划哈希为 `8087a71c59d4171a4be610c4930e3925093522564da663661ab73f9becb4c7d5`。
- 只读性：运行前后原始 NIfTI 与 JSON 的 SHA-256 一致；实际运行目录、日志和大型 NIfTI 保留在本机工作区，不提交仓库。

> 上述计划哈希**已被本轮真实验收取代**。2026-09 的版本证据修正把 Skill 兼容性标签与 `evidence_refs` 从 `V8.2_240510` 更正为实际安装并核验的 `V9.0_250415`，本地 `.env` 的 MATLAB/SPM/DPABI 版本标签同步更正。Skill `content_hash` 与 `environment_hash` 都参与 `plan_hash` 计算，因此修正后同一参数组合得到**不同**的计划哈希（见下一节的 `a94c3dd1…`）。旧哈希不再用于批准绑定，仅作历史记录。

该冒烟只证明当前机器上的 Work→应用服务→Worker→MATLAB/DPABI→Artifact 登记闭环，以及此次参数组合能够生成 raw ALFF；不验证完整科研方案，也不把单受试者输出解释为科研结论。

## 2026-09-23 授权真实验收：DemoData Sub_001 完整 DPABI 预处理

本次由用户明确授权，使用仓库外 DemoData `Sub_001` 的独立可写副本（`FunImg/Sub_001/` 单受试者 DPABI 工作区），经 **Work 对话自然语言**路径驱动，包含服务端签发的精确确认口令，因此验证的是真实授权门而不是绕过服务直接调用。测试为 `tests/science/test_demodata_acceptance.py::test_demodata_real_dpabi_preprocessing_reaches_qc`，实测耗时 7 分 19 秒（含环境探测与数据集普查）。

- **运行结果**：状态 `qc_review`，QC 状态 `awaiting_review`，attempt 1，`run_error` 为空。运行 ID `57be4cd7-e2cb-4892-bd34-1100471f9766`。
- **计划与作业**：计划哈希 `a94c3dd1a6f2541455ccdc92da0efc3df3905d4c94d6ac2b6aa42fca513442de`；job hash `1e6d1a0bd5d1477a43b95457866c5a985e7a3c740f74d23d2f011c03c5b9eb36`；执行器 `controlled_matlab`。
- **登记的 6 个 Artifact**（均以 SHA-256 登记）：

  | 类型 | 相对路径 | SHA-256（前 16 位） | 字节数 |
  | --- | --- | --- | --- |
  | `preprocessing.functional_timeseries` | `…/output/Sub_001/timeseries.nii` | `9549e14c6375d8db` | 126 517 600 |
  | `preprocessing.brain_mask` | `…/output/Sub_001/brain_mask.nii` | `b947834abc0a4f7d` | 541 024 |
  | `metric.alff.raw` | `…/output/Sub_001/raw_alff.nii` | `55bd1a3ac3ca1caf` | 541 024 |
  | `preprocessing.metadata_evidence` | `…/output/preprocessing-evidence.json` | `3bd89bcf2a736010` | 1 779 |
  | `preprocessing.execution_log` | `…/logs/matlab.log` | `676b6e3facd51358` | 6 595 |
  | `qc.head_motion_report` | `…/output/qc/head-motion-qc.json` | `a791f95825594254` | 1 253 |

- **几何一致性**：时序、脑掩膜与 ALFF 三者共享同一 `grid_signature` `05d55877776288daa665f1d59d5cae4a2520179e993a9dad69c414a9fb84b64c`，且 ALFF 的 `mask_grid_signature` 与之相等；时序登记的 `volume_count` 为 **234**、TR 2.0 秒、体素 3.125 × 3.125 × 3.6 mm —— 与 NIfTI 头实测一致。
- **头动 QC 证据**：解析真实 DPABI `RealignParameter`，`realignment_parameter_count` 与 `fd_power_sample_count` 均为 **234**（与头部时间点数一致）；Sub_001 判定通过：max translation 0.579 mm、max rotation 0.506°、mean FD_Power 0.126、FD_Power > 0.2 比例 0.141、FD_Power > 0.5 计数 0。
- **只读性**：运行前后原始 DemoData NIfTI 的 SHA-256 一致，验收只读取源数据。

**可复现性结论**：本次 `raw_alff.nii` 的 SHA-256 为 `55bd1a3ac3ca1caf9e7e8998e1c87bb50508f756a81cfcd6b5b3a7c02d8fa1f9`，与 2026-09-18 冒烟记录的 ALFF Artifact 哈希**逐字节相同**。两次运行的 `plan_hash` 不同（版本证据修正所致），但输出影像完全一致——这同时证明：(1) 版本标签修正只影响元数据与哈希链，没有改变任何计算；(2) 同一输入与参数在本机的计算是确定性的。

该验收只覆盖单受试者、纯预处理加 raw ALFF 的参数组合；不验证三受试者组合指标（D-2）、组统计（D-3），也不把单受试者输出解释为科研结论。


## 已实测的 DemoData 数据集事实

以下数值来自直接读取 NIfTI 头（`neuroagent/infrastructure/filesystem/nifti_header.py`），不是文件名推断：

| 受试者 | 功能像 dim | TR | 体素大小 | 类型 | T1 dim |
| --- | --- | --- | --- | --- | --- |
| Sub_001 | 64 × 64 × 33 × **234** | 2.0 s | 3.125 × 3.125 × 3.6 mm | float32 | 128 × 256 × 170 |
| Sub_002 | 64 × 64 × 33 × **239** | 2.0 s | 3.125 × 3.125 × 3.6 mm | float32 | 128 × 256 × 170 |
| Sub_003 | 64 × 64 × 33 × **239** | 2.0 s | 3.125 × 3.125 × 3.6 mm | float32 | 128 × 256 × 170 |

两个必须记住的结论：

1. **文件名里的 `240`（`Sub_001_ge_func_31x31x30_240_RS_...`）不是真实几何**：它是扫描仪描述符，真实网格是 64 × 64 × 33，时间点数是 234/239。任何按文件名推断维度的代码都是错的，参数必须来自头部。
2. **三个受试者的时间点数不一致（234 vs 239 vs 239）**。因此单一 `expected_time_points` **无法**描述一个三受试者计划，D-2/D-3 级别必须先通过逐受试者 `dummy_scans` 把保留时间点数归一化。该归一化是**本测试的软件夹具决定，不是科研默认值**，不得复制进研究方案。

验收工具位于 `tests/science/test_demodata_acceptance.py`，标记为 `demodata_smoke`，默认整体跳过：无 `RSFMRI_DEMODATA_ROOT` 时数据集普查跳过，无 `RSFMRI_RUN_DEMODATA_SMOKE=1` 时真实 MATLAB 运行跳过。CI 不设置这两个变量。

## 人工或外部验证项

以下检查不能在无人值守门禁中伪造为成功：

1. GitHub CLI 已安装且登录成功。`main` 分支已通过 API 回读确认 strict status checks（`agent-review`、`quality-gate`）、enforce admins、linear history、required conversation resolution 及禁止 force-push/delete；最终 Actions 状态和 tag 仍必须从对应提交核验。
2. Provider 和真实统计 smoke 已有上述成功记录；新的 Provider、环境或真实数据作业仍遵守相应授权与去标识化规则。
3. 组合指标 smoke 已完成。常规测试仍不启动 MATLAB；最终树门禁、远程 CI 和 tag 的结果以对应提交及 GitHub 检查为准。
4. 小型统计 smoke 覆盖受控 Executor、真实产物和报告构建合同；不把 Mock Playwright 测试表述为真实 Web—API—Worker—MATLAB 端到端验证。

## 非目标

本候选版不包含多用户权限、云部署、PACS、微服务、通用插件市场、长期记忆或临床诊断。输出仅用于科研流程辅助，不能替代方法学审查。


## 2026-09-29 DPARSF 面板与 T1 一体化预处理

Work 提供按 DPARSF 分组的可编辑草稿，保存经过服务端科学校验，并使用基准哈希及幂等键控制版本。修改使旧参数确认失效。T1 重定向、裁剪、配准、分割和 DARTEL 在同一次预处理运行中衔接；不要求中途人工审查。检查图作为可查看产物。

Mock/合成夹具覆盖参数编辑、版本冲突、确认失效、旧方案兼容、输入配对、准备产物完整性和完整服务—Worker 流程；Mock 产物明确标为非科研结果。真实 MATLAB、T1 分割和 DARTEL 未在本轮运行，仍需单独授权验证。

## 2026-09-30 单次 DPARSFA 预处理复核

根据本机只读 DPARSFA 源码核对，ALFF/ReHo 与 T1 分割/DARTEL 均由同一配置内的顺序分支处理。本轮将包装脚本中的指标二次调用改为一次完整调用；指标图从 DPABI `Results` 目录登记。独立 T1 准备/中途审查 API 已移除，旧作业若仍要求该操作将失败关闭。冻结输入启动前一致性与最终产物完整性检查保留。

后端、科学和 Agent 回归 815 项通过、7 项跳过；2 项已知基线失败按原有记录排除。前端构建通过，Ruff 与文档检查通过。未启动真实 MATLAB；T1/DARTEL/指标组合的实际产物路径与数值仍待授权 smoke 验证。

## 2026-10-08 Work 执行中取消复验

真实 Work 页面经最终确认启动 DemoData Sub_001 的独立 ALFF 任务 `5dce21ac-e0a2-4f67-a63e-1f88b77b09ad`。MATLAB/DPABI 已进入 Slice Timing（33 层、TR 2 秒）后，在运行卡片发起取消。界面先显示等待执行器确认，随后终态为“计算已取消”；数据库运行和作业均为 `cancelled`、attempt 1，并记录一个取消请求及 Worker 确认后的终态。对应 MATLAB 进程已退出；刷新后页面保持取消状态，仍可查看 stdout/stderr 日志，并提示本次执行未成功完成。该实测验证真实网页—服务—Worker—MATLAB 的执行中取消及恢复，不覆盖取消与完成竞争，也不构成 ALFF 结果验证。此前用于本轮的另一项新增 MATLAB 任务在 T1 准备阶段失败；两次新增运行授权额度已用尽，未启动第三项任务。

该取消任务的第 20 版方案由真实网页自然语言生成，和上版相比仅改变 T1 输入准备为 prepared、启用 BET。结构化投影与实际配置均开启 DPABI 原生 BET、T1 配准与 Segment，关闭交互式重定向及 DARTEL；本机原生 BET 同时处理 T1 与功能均值像。任务在 BET 前取消，因此只证明方案编译、实际配置和执行中取消，未证明 BET 或后续 T1 分割已完成。BET 选项、两种分割投影与日志识别有确定性测试；新增路径的完整真实结果仍待验证。

2026-10-08 失败说明与技术详情验收：运行卡片调用已有的本地确定性诊断接口，将 T1 转换失败整理为简短原因与建议，不调用模型、不自动更改已批准方案；原始 MATLAB 错误默认折叠，日志下载保留。诊断不可用时使用中性说明；迟到诊断按运行、执行次数和错误内容隔离，避免覆盖后续结果。长堆栈在窄屏换行并限制滚动高度。使用原真实验证数据库的独立副本，仅把副本会话的 active_run_id 绑定到既有失败运行 5609db2c-a13e-44bf-9f72-0fff3ae873a6，保留原库与历史运行状态；正常服务无 Worker、无响应替换。真实浏览器核对默认折叠、展开原始错误、日志入口、390 像素窄屏布局和刷新恢复，截图在本地 output/playwright/failure-natural-desktop.png 与 failure-natural-mobile.png。此为历史失败记录展示验收，不是新对话规划或新增 MATLAB 执行。前端全部 174 项通过，语句覆盖率 85.72%、分支 82.99%、函数 83.01%、行 90.29%；构建、Lint 通过。后端诊断及 Worker 诊断 27 项通过，Ruff 通过；本轮未重新执行全量 Python 门禁。BET 与后续 T1 处理的完整真实产物、取消与完成竞争及第 13 节其他剩余矩阵继续保留，未启动第三次 MATLAB 任务。

2026-10-08 压缩与深层目录性能补验：将原 DemoData Sub_001 的 NIfTI 只读压缩到独立副本，按 visit/session/acquisition/series 四层嵌套保留 FunImg/T1Img 角色，正常 DatasetInspector 识别 1 名受试者、4 文件、63,263,585 字节。五次概况耗时约 14.66/3.93/3.92/3.82/4.29 ms，内容哈希调用 0；冻结调用 4 次，约 52.12 ms；执行器既有完整性校验约 47.73 ms。修改独立副本 gzip 头的时间字节并更新文件时间，不改变文件大小或体素数据：普通概况撤回旧输入修订，执行前校验拒绝旧冻结清单。新增合成回归同时核对压缩头可正常解压、深层目录的受试者归属、哈希调用数及同大小变更拒绝，4 项性能测试通过。另一独立合成数据集含 128 名受试者、128 个有效头影像文件、1,073,786,880 字节，同样四层嵌套；概况首次约 652.60 ms，后续约 145–206 ms，内容哈希调用 0；冻结全部 128 次约 948.15 ms，执行前校验约 835.75 ms。此为本机热缓存有限样本测量，不作普遍速度承诺；大样本为合成数据，只有压缩副本来自 DemoData。原 DemoData 四文件前后 SHA-256 相等，未启动 MATLAB。原始 measurement.json 及测量脚本保存在工作区外独立验证目录，不提交影像与大文件。此补齐压缩影像、深层目录和更大规模的本地读取测量；模型多回合性能评估与页面输入变更完整矩阵仍需继续验证。

2026-10-08 当前实现树全量门禁：Python 977 项通过、7 项条件跳过，118.55 秒，分支覆盖率合计 85.05474860335195%，达到 85% 项目门槛，报告保留在本地 output/python-coverage-current.json；前端全量 174 项通过（语句 85.72%、分支 82.99%、函数 83.01%、行 90.29%），Mock 浏览器 7 项通过。全仓库 Ruff 规则通过、403 个 Python 文件格式通过，Mypy 183 个源文件通过，前端构建与 Lint 通过，59 份 Markdown 规范检查通过。Python 测试显示既有 Starlette/httpx 弃用警告；coverage 合并另提示一个子进程文件不是有效覆盖率数据，随后只读 SQLite 核对该文件结构完整但 coverage_schema/meta/file/arc 均无记录，因此该空文件没有可合并的覆盖率证据；本次覆盖率数值仅以成功生成的主报告为准，不把空文件计入覆盖率。当前门禁涵盖 BET 意图解析、失败说明及压缩深目录回归增量；真实交互矩阵、BET 后续真实产物和取消完成竞争未被测试门禁替代，计划继续为 Implementing。

## 2026-10-08 Work 数据分析真实 MATLAB/DPABI 验收

用户授权后，在本机 MATLAB R2024b、SPM25、DPABI V9.0_250415 上执行了真实统计 smoke。六种统计设计使用确定性生成的微型合成 NIfTI，经受控 MATLAB Executor 调用 DPABI；单样本 t、两样本 t、相关使用双侧 FDR，配对 t 与单因素 ANOVA/ANCOVA 总体 F 使用 GRF，多变量回归验证统计图和效应图生成。MATLAB 脚本逐体素比较六类统计图与解析解、核对效应图和校正产物；另有 MATLAB 实际效应量代数测试。四个真实/静态检查通过。完整证据与运行日志保存在 `DemoData/Slow-1-statistical-acceptance/synthetic-statistics-smoke-batch-20261008/`，六类 DPABI 入口的版本证据与报告哈希见其中 `summary.json`；MATLAB 启动现使用 `-batch`，复验日志没有无法识别的启动参数警告。

随后使用用户指定并复制到工作区的 `Slow-1` zALFF 数据，按文件名顺序从 children 与 adolescent 各选 3 名进行两独立样本、双侧共同残差方差 t 检验，并调用 DPABI FDR（q=0.05）。运行 `slow1-two-sample-alff-fdr-batch-20261008` 在六个输入、一个掩膜和 9 个统计产物上完成；MATLAB/DPABI 实测版本分别为 R2024b、SPM25、V9.0_250415，残差自由度 4。校正 NIfTI 的非零体素数、`clusters.tsv` 簇数及簇体素总数均为 0，结果登记状态为 `no_significant_voxels`，报告的 `localization_applicable` 为 false；9 个产物哈希全部匹配。此结果明确报告“本次比较经 FDR 后未发现显著差异，没有脑区可定位”，不标记为空间未知。运行输入清单、脚本、MATLAB 日志、NIfTI、簇表、配置、版本证据和报告保存在 `DemoData/Slow-1-statistical-acceptance/runs/slow1-two-sample-alff-fdr-batch-20261008/`；该 3+3 人子集仅用于本机技术验收，不代表研究结论。

验收边界：以上证明统计设计适配、固定模板、DPABI 真实执行、FDR/GRF 产物与数值核验路径可在该版本栈运行；它不等同于浏览器 Work 对话至服务、Worker、审批、MATLAB 的完整统计端到端验收。外部来源的后续应用验收见下节。

## 2026-10-09 外部指标到公共 Worker 的真实验收

新增外部指标只读检查、独立纳入决定、同网格 mask 及稳定统计引用。指标和 mask 在创建设计及执行前绑定大小/哈希；目录变化使旧统计方案失效。Slow-1 全目录只读扫描得到 325 张 z-score ALFF、children/adolescent 两组、统一网格且无阻断项。文件名中的站点前缀保留在 subject ID 中，避免 NYU/UCL/USM 重复局部编号被误合并。本次没有对这 325 张图自动作出研究纳入或执行决定。

用户已授权真实 MATLAB 验收。`scripts/verify-external-statistics.py` 在独立目录生成 6 张微型合成指标图和 mask，按应用服务完成检查、纳入、打乱设计顺序、审批及真实 MATLAB 运行，再由公共 Worker 原子登记统计结果。首轮暴露额外进程 stdout 日志无法进入 canonical 统计结果合同的错误；修复为进程日志继续保存在运行产物中、复现清单使用统计模板日志，并跳过空进程日志。

修复后的独立重跑 `90e45f19-cbc2-497b-892b-c7c8cfbbeb03` 成功。两样本 t 图与按原始输入解析计算的最大绝对误差为 `2.670728965181024e-14`；FDR q=0.05 后显著体素 0、簇 0。输入空间故意保持 unknown，结果及定位查询仍均返回 `no_significant_voxels`，报告 `localization_applicable=false`。保留 9 项 canonical 统计产物和 1 项非空进程日志。证据、SQLite 记录、输入、脚本、日志和报告在工作区 `DemoData/external-statistics-acceptance-20261009-r2/`；失败首轮目录保留用于诊断。

验收范围是应用服务、审批、编译、公共 Worker、真实 DPABI 两样本 t/FDR、结果登记与零结果定位查询。外部配对、外部人口学 revision 和浏览器端到端操作尚待验收；不能据此宣称整个双 Agent 计划通过。

后续增量已接通 Work 的显式外部来源选择与统计方案装配。合成测试覆盖冻结纳入顺序、反向组别对比、模型伪造来源被忽略、跨项目及输入变更阻断；分析对话测试验证外部来源进入服务端方案。前端组件测试验证已纳入且带 mask 的选择与取消选择。此增量未重新运行 MATLAB，仍引用上段公共 Worker 的执行证据。

本轮统计与接口定向回归 122 项通过，目录/纳入增量 13 项通过（部分接口用例重叠），前端导入及结果面板 23 项通过；Ruff、193 个源文件 Mypy、前端 Lint/生产构建与 61 份文档规范检查通过。未重跑全仓库覆盖率门禁。

## 2026-10-09 外部人口学与协变量真实验收

外部人口学 CSV/TSV/XLSX 复用现有只读解析器，要求恰好覆盖纳入名单，独立存储修订和映射后的表格快照。统计设计、复现报告保存修订 ID/hash；模型仅接收列名。两样本、相关与回归的设计装配均有服务测试，覆盖人口学乱序、重复/缺失编号、无效数值、矩阵秩不足以及新修订使旧方案和执行编译失效。

用户授权的真实验收通过：6 张合成 ALFF 图、打乱行顺序的年龄表、两样本 t＋年龄协变量＋双侧 FDR，经方案审批和公共 Worker 调用本机 MATLAB/DPABI，T 图对独立最小二乘 GLM 的最大绝对误差为 `1.9690280569562208e-14`。运行 `8f735ea1-3126-4299-8540-b977e02a5493` 成功；0 显著体素、0 簇、定位状态 `no_significant_voxels`，报告包含人口学修订和内容哈希。工作区证据位于 `DemoData/external-demographics-acceptance-20261009/summary.json`，脚本为 `scripts/verify-external-statistics.py --with-covariate --real-matlab --output-root <新目录>`。

这证明外部人口学到两样本协变量设计的真实执行链路，不证明外部配对、完整浏览器流程或研究结论。相关/回归本轮只验证服务装配，既有真实函数 smoke 仍作为其执行适配证据。完整双 Agent 计划继续实施。

## 2026-10-09 外部配对条件真实验收

外部导入新增显式 `paired_conditions` 布局，两个条件的编号集合必须一致且条件内唯一。纳入清单逐受试者冻结，影像引用同时绑定受试者和条件；指标、mask 在 attempt 中核对并复制。服务与前端测试覆盖完整配对、缺失、重复、条件顺序改变以及纳入名单去重。普通分组布局仍拒绝跨组重复编号。

真实配对 t＋双侧 FDR 通过：3 名合成受试者的 6 张 ALFF 图，冻结顺序为 sub-03、sub-02、sub-01，差值方向为 first 减 second，公共 Worker 调用本机 MATLAB/DPABI。T 图对独立差值均值/标准误公式的最大绝对误差为 `4.927492833182198e-14`。运行 `366cb027-8d86-4a20-981f-22bc3001b4d7` 成功；零显著体素正常完成，定位为不适用。工作区证据为 `DemoData/external-paired-acceptance-20261009/summary.json`，验收脚本支持 `--paired --real-matlab --output-root <新目录>`。

这证明外部条件输入、统计服务、审批、Worker、真实配对适配和零结果登记，不证明完整浏览器端到端或临床/研究差异。首期配对 t 不接受静态受试者协变量；重复测量 ANOVA 和混合效应仍不在范围内。

## 2026-10-09 显著簇 ROI 与结果展示真实验收

新增显著簇 ROI 查询，复核冻结设计、指标来源哈希及簇连通范围；Work 簇选择联动切片、均值分布及 TSV/JSON 导出，另提供簇表 CSV/TSV、脑区覆盖 TSV 和切片 PNG 下载。真实执行发现 DPABI 会翻转统计图存储轴；现仅做无插值轴重排后比较世界坐标网格，真正的空间网格变化仍阻断。定向回归覆盖轴翻转/排列、正负簇、峰值/范围不符、输入变化、非有限值及零结果正常空表。

固定合成局部组差异与 FDR q=0.05 的独立运行 `85c3e0a6-c7ea-423e-85ce-9b4976359edd` 经外部导入、审批及公共 Worker 调用真实 MATLAB/DPABI 成功。保留 8 个显著体素、1 个正向簇，T 图对独立解析公式的最大绝对误差为 `1.83091709970995e-06`。AAL 峰值在 `Postcentral_L`，覆盖为该标签 6 个体素（75%）、背景 2 个体素（25%）；6 名受试者 ROI 均值与原始合成值完全一致。证据位于工作区 `DemoData/external-roi-acceptance-20261009-r3/`，可用 `scripts/verify-external-statistics.py --with-signal --real-matlab --output-root <新目录>` 复现。

浏览器验收使用该运行数据库副本和实际服务，已验证簇切片联动、ROI 图表、AAL 定位、页面刷新恢复同一结果，以及实际下载的 ROI JSON/TSV、簇表 CSV、覆盖 TSV 和 PNG。下载表的顺序、均值、覆盖计数与服务端证据一致，控制台无错误；截图位于本地 `output/playwright/roi-distribution-20261009.png`。没有调用模型或从网页创建本次运行，因此这仍不证明完整 Work 规划、审批至真实 MATLAB 的浏览器闭环。首轮失败诊断及背景标签验收目录保留，后续修正未调整检验阈值。

另用已完成的零结果运行数据库副本验证真实页面：即使输入空间 unknown，页面正常显示 0 显著体素、无显著差异及无可定位脑区，不显示定位入口或 ROI 面板；统计完成提示不要求预处理 QC。截图位于 `output/playwright/zero-result-20261009.png`，控制台无错误。

本轮相关后端 61 项通过、1 项条件跳过，最终导出及文案修改后的前端组件 28 项通过；195 个源文件的严格类型检查、相关规则、前端 Lint/构建、63 份文档规范及差异空白检查通过。最终全量门禁仍需通过。合成差异和簇内均值不构成真实数据研究结论或独立验证。

## 2026-10-09 Work 网页规划、审批与真实统计执行

独立合成验收从空会话开始，在实际网页完成六张 zALFF 指标的目录检查、纳入、掩膜及 MNI 确认、外部来源选择、分析 Agent 草稿、正式设计校验、审批及真实 MATLAB 启动。规划采用明确标记的 Mock Provider，科学执行为本机真实 DPABI；未通过开发接口注入方案、审批或运行。固定双侧两样本 t＋FDR q=0.05，运行 `f689d668-2350-4da2-84c3-6e66ea4c2840` 成功，8 个体素、1 个正向簇，只创建 1 次运行。

独立校验器核对全部 10 项下载产物大小及 SHA-256、源影像哈希、原始合成影像解析 T 值及 ROI 均值。T 最大绝对误差 `1.83091709970995e-06`，ROI 误差 0，AAL 峰值 `Postcentral_L`。证据、数据库及报告位于工作区 `DemoData/work-browser-statistics-20261009/`，其中 `browser-verification.json` 保存机器核验结果。工具入口见[计划 0013](../plans/0013-work-dual-agent-analysis.md)。

当前验收限制：首轮验收配置缺少脱敏 salt，模型阶段正常拒绝，未生成方案或运行；修正配置后成功。模型发现出现 503，原因是验收服务只注册 Mock Provider，页面使用已配置模型回退；验收工具已补 OpenAI-compatible 本机发现适配并复核 HTTP 200。错误及原会话证据保留，不归为 DPABI 科研失败。该次页面刷新恢复同一运行依赖 sessionStorage；服务端 conversation.active_run_id 仍为空，跨页面持久任务绑定未满足用户计划，需继续修复。这次证据证明 Mock 规划至真实统计的网页链路，不证明全量双 Agent 交接、真实模型规划质量或所有能力验收完成。

## 2026-10-10 Work 持久任务与真实统计恢复

通过 ADR 0028 和迁移 0021，在现有 SQLite 保存前台角色、各角色的草稿/提案/方案/运行引用。统计方案创建校验服务端提案内容，冻结会话、提案消息和草稿哈希；校验、审批与启动再次核对当前作用域。方案/运行与引用在同一事务提交；幂等重放不创建重复运行或重新切换角色。迁移、独立服务恢复、双角色引用、篡改/过期草稿、跨角色确认和准备期间草稿变化均有定向回归。

### 正向交接凭据的后续补齐

ADR 0029 的正向交接记录逐字段冻结项目、数据集/清单、来源方案与运行 attempt、QC 决定和纳入顺序、逐指标版本/完整 lineage/来源及哈希、组级掩膜及校验和。接收时重新读取元数据和文件，Work 统计方案冻结相同凭据。校验、批准及启动前再复查；慢文件检查在 SQLite writer 外，事务内复查元数据。模型只接收数量、指标类型和必要来源摘要，不传递完整指标谱系。

新增 10 项回归验证完整记录与乱序对齐、QC 未决、指标版本混用、同大小文件 SHA 变化、原始输入变化、清单修订、校验/批准/启动的旧方案阻断以及两阶段对话记录和受限模型摘要。全量后端及 Agent 650 项通过、1 项跳过；其后新增同一提案重复创建保护与审批拒绝兼容，27 项定向回归通过。严格类型检查 198 份源文件、Ruff 和 66 份文档规范通过。本次没有批准既有真实数据的未决 QC，也没有把这些合成完整性夹具宣称为真实 MATLAB 内部闭环；反向需求交接、共享交接上限和内部真实端到端仍按计划 0013 推进。

使用六张固定合成 zALFF 图，在真实网页检查/纳入、选择分析来源、建方案、切到预处理再切回分析、审批并启动公共 Worker。规划仍明确采用 Mock Provider，运行 `fc6fd8fe-d69c-4e4b-8263-a563098b08c3` 调用真实 MATLAB/DPABI 成功。两样本 t、双侧 FDR q=0.05 得到 8 个体素和 1 个正向簇，T 最大绝对误差 `1.83091709970995e-06`，ROI 均值误差 0，AAL 峰值 `Postcentral_L`；10 项产物大小和哈希全部匹配。校验器还复核服务端会话与不可变方案的提案/草稿绑定。

关闭原浏览器，使用新的无缓存浏览器会话打开页面，自动恢复同一个正式方案、已完成运行和结果，`sessionStorage.length=0`，控制台无错误或警告。角色切换时分析预览只读，切回后恢复原方案。停止并重启 API/Worker 后再次独立核验同一运行和绑定。另以 Mock 为执行后端发送重复启动请求，HTTP 409 返回已有任务编号，运行集合不变，`duplicate_start_blocked=true`。证据位于工作区 `DemoData/work-task-bindings-20261010/browser-verification.json`，截图为本地 `output/playwright/work-task-restored-20261010.png`。本轮后端及 Agent 回归 640 项通过、1 项跳过；前端 183 项通过，最终显示文字修改后 38 项页面回归通过，重复启动保护的 18 项定向后端回归通过；严格类型检查、Lint/构建和 65 份文档规范通过。此证据修复上一条记录发现的缓存依赖；完整交接、六能力最终审计和全量门禁仍按计划 0013 继续。

## 2026-10-10 上游指标需求与共享请求预算

ADR 0030 补齐分析到预处理的反向交接。输出名称规范化为九类 ALFF/fALFF/ReHo，身份只使用服务端证据与排序去重后的需求，不依赖模型措辞或接收状态。内部来源保存完整正向凭据，外部来源保存检查、纳入和选择记录，保留原始输入未知项；接收时复查文件，跨项目凭据被隔离。外部来源损坏时保留需求及既有证据，记录阻断，不把需求丢弃或启动计算。

每会话/项目最多六项唯一上游需求，可通过 RSFMRI_WORK_MAX_HANDOFFS 调整。重复投递与接收回执不增加计数；被阻断请求不进入已接受集合，重放同一阻断请求不能绕过上限。分析参数不合并到预处理草稿，接收方模型只得到需求和受限数据摘要。网页根据目标角色显示已接收、就绪、待接收、受阻及失效状态，并以中文说明阻断及原始输入选择要求。

Work 两角色使用同一请求预算策略，工具循环与结构回退共用步数、累计 token 和剩余时间。usage 缺失时逐调用估算并累计，避免混合报告造成漏计；回退前检查剩余预算，超时/预算停止保留草稿与工具审计。能力查询也纳入同一截止时间。

全量后端及 Agent 回归 664 项通过、1 项跳过；其后补充来源损坏需求保留及能力查询截止，23 项定向回归通过，最终 14 项交接/预算回归通过。前端 Agent 页面和交接面板 40 项通过，Lint 与生产构建通过；严格类型检查 198 份源文件、Ruff 和 67 份文档规范通过。本轮未运行新的 MATLAB 作业，内部真实预处理→QC→统计闭环及最终六能力审计仍按计划 0013 执行。

## 2026-10-14 WorkTask 后台恢复扫描

既有 Worker 启动后和空闲轮询时会领取并重核一个可恢复 WorkTask。SQLite 为等待目标建立独立索引与短租约，随机 claim token 对事件消费和状态写入提供 fencing；租约过期后旧持有者不能提交。协调器只处理绑定 Run 的运行等待、QC 决策等待和统计结果登记等待，事件按最多 1000 项一批回放，周期扫描在没有新事件时也重新读取 Run、QC、Artifact 和统计结果权威状态，避免把事件 payload 或瞬时通知当成事实。普通分析方案等待和尚无下游检查器的预处理依赖不会被自动领取。

相关自动化 service/持久化/状态机/绑定及 Worker 主循环回归 55 项通过，旧数据库目标回填迁移测试 1 项通过。扩展回归 92 项通过、1 项失败；失败来自已有的 `test_database_restart_preserves_queued_run`，它调用不存在的 `ApplicationServices.get_run`（组合根通过 `runs.get_run` 暴露该用例），不是本切片引入。Ruff、格式检查、Mypy 和 78 份 Markdown 规范检查通过。该切片不调用模型、不创建 Run、不审批 Plan/QC，也未运行 MATLAB。目标级 Artifact 完整性、自动准备下游分析/报告和跨步骤目标完成仍未实现。

## 2026-10-14 Work 统计任务持久绑定与结果重核

统计提案现在创建或复用持久 WorkTask；正式统计 Plan、显式授权创建的 Run 与会话角色引用按既有 Unit of Work 原子绑定，旧会话可在服务端提案/草稿证据完整时补建任务。Run 完成后的统计专属重核对照唯一真实 StatisticalResult、Run、冻结设计修订与 Plan 哈希，并核验结果 detail、报告、manifest 和登记 Artifact 元数据。统计不依赖预处理头动 QC；零显著体素属于有效结果，结果暂未登记则继续等待，mock/synthetic、占位产物、重复结果或身份不一致会阻断。

自动化 service、持久化、状态机和 Work 任务绑定定向回归共 47 项通过；Ruff、Mypy、文档规范检查和差异空白检查通过。未启动 MATLAB。此项实现持久任务与统计结果证据核验，不包含后台事件订阅、任务领取/恢复调度、自动准备下游步骤或跨步骤目标完成判定。

## 2026-10-14 预处理目标产物完成判定与旧等待恢复

DPABI 执行输出与 WorkTask 共用不可变目标合同，精确表达冻结 manifest 的受试者、指标/缩放、登记类型和相对路径。预处理 Run 成功且 QC approved 或有显式 skip 记录后，应用服务重读冻结 Plan/SkillPlan 与 manifest，检查 Run/项目/会话/Plan 绑定，再按每个目标的 Artifact 类型和路径核验唯一登记项、项目/Run、ID/路径唯一性、SHA-256、非空大小、typed lineage、受试者、manifest hash、指标缩放和已验证 metadata。指标结果的掩膜引用还需指向同项目、类型为 brain mask 且网格兼容的 Artifact；自动掩膜必须是同受试者的本次输出。全部通过后 WorkTask 与预处理 TaskStep 同步完成；缺少目标或存在冲突/错误谱系时阻断。Run 读取期间版本持续变化仍保持 `indeterminate`。

迁移 0024 和 SQLite 目标索引只恢复 Plan/Run 引用相符的预处理 `waiting_dependency/downstream:<run_id>` 任务；普通 dependency 与用户科研决定等待仍不调度。统计任务依旧按 StatisticalResult 专属凭据完成；本切片不自动创建统计设计、批准 QC、创建下游 Run、调用模型或生成报告。

定向测试覆盖 DPABI 多受试者/指标/缩放输出合同、auto mask 与重复 scaling、成功后显式 QC skip 和精确产物验证、approved QC 下目标缺失阻断、错 subject/项目的产物拒绝、旧下游等待领取与重核、普通 dependency 排除及迁移选择性回填。自动化 service、持久化、迁移、Work 绑定、Worker 主循环、预处理执行与科学编译器回归共 81 项通过、1 项跳过；Ruff 规则与格式检查、6 个 Python 源文件 strict Mypy、79 份 Markdown 规范和 `git diff --check` 通过。未运行 MATLAB；通过 Mock 权威记录的应用层验证不构成 DPABI 数值路径证据。

## 2026-10-14 统计结果解释自动续接

真实统计 WorkTask 在唯一、非 synthetic 的 StatisticalResult、冻结设计/bundle 和已登记 Artifact lineage 通过服务端复核后，会与任务完成在同一 Unit of Work 中写入独立结果解释 outbox。后台消费者在模型调用前后重核来源，并只把有限、白名单统计事实与确定性 readout 交给模型；仅追加 assistant 解释/报告草稿。零显著结果作为正常结果处理。暂态消费错误最多自动尝试 5 次，随后进入可人工重试状态；重试使用独立 generation 和幂等 API。计划、统计 Run、QC 和科学方法选择均不由该续接改变。

本切片定向后端/持久化/Worker/迁移回归 29 项通过；Agent 页面测试 41 项通过，前端 TypeScript 类型检查通过；Mypy 291 个 Python 源文件通过，Ruff 与格式检查覆盖 317 个文件，文档规范检查覆盖 81 份 Markdown。没有调用真实模型或 MATLAB/DPABI；因此验证的是任务、证据核验、队列消费、重试和页面状态的应用层行为，不代表模型解释质量、真实统计数值路径或完整浏览器至 MATLAB 端到端验收。

## 2026-10-14 Work 预处理 Skill dry-run 反馈切片

Work 预处理候选现在通过同一只读准备流程调用真实 SkillResolver、SkillValidator 和 SkillCompiler；执行前后重核项目版本、数据集归属、当前 manifest ID 与 hash。该 dry-run 仅验证已登记 Skill 合同，不探测本机 MATLAB/SPM/DPABI 文件、不读取或验证真实影像头、不创建 Plan、Approval、Run、事件、授权或任务。`ready` 明确带 `scope=skill_contract_compile_only` 和 `environment_probed=false`，模型提示与回复均限定其含义。科研参数阻断和输入冲突要求用户决定，编译能力缺口以 `BLOCKED_CAPABILITY` 停止；预处理自动模型修订 allowlist 为空，统计设计装配仍未纳入该回路。

定向回归 24 项通过、1 项跳过，覆盖真实解析/校验/编译、dry-run 前后项目版本和 manifest 重核、并发输入变化失败关闭、数据库所有表计数和工作目录内容不变、首轮未持久化草稿预检、科学 Skill blocker 阻止模型改写参数、相同 issue/evidence 指纹稳定、DICOM/未审查 Skill/编译器能力与治理缺口分类，以及常规编译路径先校验 manifest 再探测本机环境。Ruff 检查和格式检查通过。没有调用真实模型或 MATLAB/DPABI；dry-run 的 `ready` 不是环境可用、真实影像有效、方案已批准或可运行的证据。

## 2026-10-21 MATLAB 错误反馈时序补强

Work 运行卡片继续只在本机对当前 attempt 的有界日志做确定性诊断。原本已知错误在运行中会立即显示，但未识别类别的脱敏证据被隐藏；终态若先于 stderr 落盘，首次空诊断也不会再检查。现在只有带明确错误标记的未识别输出会显示摘要和建议，最多四条脱敏线索放在默认折叠的详情中；普通 MATLAB 输出不会被误报。失败、超时或取消后，如果诊断尚无证据，则最多每秒检查一次、共五次；出现证据即停止。日志不发送给模型，不自动改方案或重试。

WorkRunPanel 定向前端测试 30 项通过，覆盖未识别错误线索默认折叠与展开、普通输出不误报、终态延迟写入后补获错误、无日志时五次后停止，以及已知错误、诊断不可用、取消、活动运行持续轮询和跨运行隔离。未启动 MATLAB/DPABI，也未访问演示影像；该验证只证明前端重试和本机诊断反馈合同。

## 2026-10-21 复合目标 MATLAB 失败自动追踪

复合目标进度 API 为每个目标返回经过服务端核验的预处理/分析 Run ID。只有会话、目标来源、任务、冻结 Plan、输入修订、Run 和 TaskStep 身份精确匹配，且 Run 仍处于可观察状态时才返回；成功 Run、错绑或证据不足的 Run 不暴露给轮询器。前端恢复所有目标的 Run，按唯一 Run ID 去重并读取状态和已有本机诊断；失败、超时或取消但暂时没有日志证据时，短暂重查以捕获迟到日志。错误字段不直接展示，确定性诊断只显示脱敏摘要、建议和有限日志线索，不发送给模型，不自动修改方案或重试。

定向验证：CompositeGoal、分析结果续接、API 与 WorkTask 来源绑定后端回归共 75 项通过；Agent 页面测试 73 项通过，TypeScript 类型检查、ESLint、后端 Ruff、Mypy、OpenAPI/TypeScript schema 同步、文档规范检查与 `git diff --check` 通过。存在一项既有 Starlette/httpx deprecation warning。未启动 MATLAB/DPABI，未访问演示影像或调用 Provider，因此验证的是目标 Run 恢复、状态/诊断轮询和本机反馈行为，不是一次真实 MATLAB 计算验收，也不表示计划 0014 整体完成。

诊断读取状态补强：WorkRunPanel 与复合目标反馈分别区分“诊断请求不可用”和“成功读取但没有可识别错误线索”；终态请求失败经过五次检查仍不可用时，可由“重新检查本机日志”重新启动原有读取流程。新增两条前端回归覆盖暂不可用、原始异常/Run 错误隐藏、手动重查后成功空诊断；这两份前端测试共 107 项通过，TypeScript 类型检查与涉及文件 ESLint 通过。未启动 MATLAB 或调用 Provider。

## 2026-10-21 多步骤方案只读审查面板

Work 页面现列出当前会话中经服务端身份重核的 READY 步骤 Plan 摘要。预处理摘要仅包含已登记的采集、去噪、标准化和指标参数；统计摘要返回检验与矩阵计数，不传被试、组别标签、协变量值、Artifact ID 或路径。用户主动展开统计纳入表后，前端才请求精确步骤详情；API 返回严格 allow-list 字段。列表查询最多验证 100 个候选并返回截断标记，不使用 SQLite writer transaction；详情查询在 task、conversation、project、plan 仓库共享的 deferred read snapshot 中重核绑定。活动目标和已有审查项会每 10 秒刷新，后台 Worker 更新无需改变 conversation version。

验证：步骤审查 API/服务定向测试 12 项通过；StepPlanReviewPanel、AgentPage、App 定向前端测试 85 项通过；TypeScript typecheck、ESLint、生产构建、变更文件 Ruff/Mypy 与差异空白检查通过。全量前端 Vitest 仍有 14 项既有失败，集中在 workspace 迁移、StatisticalClusterRoiPanel 和 WorkStatisticalResults 测试；本次新增组件测试及相关页面测试通过。未调用 Provider、MATLAB 或 DPABI；当前仍没有步骤级批准/启动闭环，也没有生产 READY successor，因此这些检查不证明真实 20 人预处理已运行。

## 2026-10-21 WorkTask 精确步骤批准与 Run 绑定

Work 页面现在可对一个 READY TaskStep 的冻结方案执行批准，针对同一 task/step、task/step/plan 版本、Plan revision/hash、会话和项目签发一次性执行授权，再显式创建并绑定对应 Run。服务端在授权和启动时重新验证 READY、前驱、方案身份与现有批准；一次性 challenge 在 Run 创建和精确 TaskStep 绑定事务中消费。统计步骤通过独立的统计 Run 创建路径绑定，并拒绝错绑目标。真实 MATLAB 执行仍要求显式确认；统计步骤使用隔离工作区；Mock 参数仅对 Mock 后端有效，MATLAB 请求携带非默认 Mock 控制会被拒绝。

验证：步骤 API/服务定向回归 16 项与授权迁移升级/降级测试 1 项通过；StepPlanReviewPanel 测试 9 项通过；TypeScript 类型检查、生产构建、ESLint、变更文件 Ruff/Mypy、OpenAPI 与前端 schema 同步、92 份 Markdown 规范检查和 `git diff --check` 通过。端到端只创建了 queued Mock Run；未调用 Provider、MATLAB 或 DPABI，也未批准或启动真实 Run。多步骤 successor 自动调度、完成判定与后续步骤的自动执行仍未实现。
