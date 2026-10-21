# 应用服务层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-10-21
- 关联：[系统设计](../../docs/architecture/system-design.md)、[目标架构](../../docs/architecture/target-architecture.md)、[ADR 0024](../../docs/adr/0024-work-dual-agents.md)

`neuroagent.application` 编排完整用例：数据检查、方案构建与审批、运行提交、QC 审核、统计设计与结果登记。它持有事务边界和权限检查，不实现影像算法。

当前能力：

- 分析角色槽通过 `analysis_intent_task_id` 保存当前复合目标。选择服务只恢复目标最新的未冻结草稿/提案，保留原始消息 ID；较新不完整草稿清除旧提案资格。后台不同目标的续接仍登记独立消息/任务/outbox，但不覆盖当前角色槽；草稿、历史、交接、来源上下文按目标过滤。已有 Plan/Run 的目标恢复尚不在该入口范围，见 [ADR 0046](../../docs/adr/0046-composite-goal-foreground-selection.md)。

- Work 草稿绑定快速输入修订；发现变化后撤回事实状态并阻止旧确认，保留用户参数。新编译须重新读取受影响事实并冻结输入，旧请求的持久化响应仍可重放。历史未绑定修订草稿及完整恢复交互继续按计划验收。
- 输入失效反馈提供明确的阶段重确认选项，确认接口允许该恢复操作并复查实时阶段与路径；重新冻结不能自动恢复采集事实，必须附本轮成功读取证据。历史带事实却未绑定修订的草稿同样撤回核实状态。
- Work 完整草稿可自动编译待审查候选，使用独立 `proposal` 来源；确认接口直接复用服务端签发的操作与执行授权，不调用模型解释启动意图。来源与校验状态分别展示，审批仍为独立记录。完整体验验收仍按[计划 0012](../../docs/plans/0012-work-conversation-ux.md)执行。
- `automation.get_step_plan_review()` 为多步骤 WorkTask 提供只读步骤级 Plan review 查询：重读并核验 Work conversation/project、合法线性序列中的唯一 READY 步骤、精确 `step-plan:<task>:<step>:<plan>` 等待引用，以及根/步骤 Plan 与输入投影。统计 Plan 校验 `work_context`、proposal/draft hash、design 和 Plan hash；预处理 Plan 校验精确 revision/project、冻结 manifest/SkillPlan hash 和推导出的 Artifact 集。预处理 `PlanRevision` 尚无 conversation 身份字段，故不能证明原始创建 conversation；该查询只返回 review DTO，不批准、不创建 Run。
- 确认请求使用独立幂等作用域并绑定完整请求；先恢复已保存响应，再校验当前草稿与签发选项，保证响应丢失后的重放不重复批准或创建运行。显式选择输入阶段时冻结完整文件证据，快速概况检查不能替代冻结。

- T1 重定向、裁剪与后续分割/DARTEL 合并为一次预处理运行；不再提供独立准备或中途审查 API；
- Work 参数面板通过幂等 PATCH 编辑草稿，保存不调用模型；版本冲突拒绝，草稿修改使旧确认失效；

- `services.py` 作为应用组合根显式装配环境、工作区、对话、项目、计划、运行、统计、技能、模型与外部导入等能力对象，不再通过多重继承组合用例，也不提供动态兼容转发。公共用例端口通过 `BaseServiceMixin.bind_shared_dependencies()` 显式绑定；跨能力服务及查询协作者单独注入。应用服务管理 `PlanRevision` 生命周期与追加式审批记录；
- `contracts/` 按项目、计划、运行、统计、Agent、对话等能力拆分应用 DTO；所有生产、测试和脚本导入均指向 DTO 所属模块，`contracts/__init__.py` 不再聚合重导出。HTTP 请求 schemas 位于 `api/schemas/` 并通过显式 mapper 转成应用命令；响应模型独立位于 `api/responses/`，由 HTTP 边界 mapper 转换。`ports.py` 声明执行器和环境端口，`repository_ports.py` 按能力拆分数据仓储协议，并把 `IdempotencyStorePort` 与 `EventRepositoryPort` 独立注入。Projects、Plans、External Imports、Models、Literature、Conversations、Workspace、Runs、QC 与 Artifact Catalog 使用共享 Unit of Work 的 SQLite adapter；Skills 只使用 Plan 写端口，Statistics 通过 Project、Plan、Run、Conversation、External Import、QC 与 Artifact 端口组合。Worker host 使用窄 `WorkerHostRepositoryPort` 和独立 `SqliteWorkerRepository`，在共享 Unit of Work 内原子完成 Artifact/QC 登记；旧 `RepositoryPort` 只留给迁移兼容调用；
- `conversation_work.py`、`work_dialogue.py`、`work_draft.py` 把 Work 自然语言收敛为受限动作与未批准草稿；
- `statistics_draft.py` 只负责统计设计的取值来源与顺序，科学校验仍由领域层负责；
- Work 可显式选择外部纳入记录；模型仅引用登记指标及 group 列，服务端复查输入后生成设计预览，来源 ID/hash 不接受模型修改。
- 外部人口学表复用只读解析器，要求编号恰好覆盖纳入名单；统计设计绑定独立修订及内容哈希，服务端读取协变量取值，模型仅接收可用列名。
- 显式外部配对布局按 subject_id 与 condition 绑定两张图；方案装配保留用户条件顺序，编号/条件缺失和源文件变化阻止旧设计执行。
- `work_actions.py` 是 Work 可选动作的唯一事实来源；
- `work_prompts.py` 按 Work 角色提供系统提示词；模型网关只接收显式注入的提示词，不再从 `work_context.agent_role` 推断业务角色。提示词中的动作清单直接读取 `work_actions.py`，避免模型契约在应用与 Gateway 之间重复维护；
- `work_model_runtime.py` 负责角色提示词注入、工具循环、单轮回退和本轮 token/时间预算；`work_dialogue.py` 保留草稿、证据、交接和业务动作编排。
- `work_models.py` 独立定义模型输出合同，动作字段只接受 `READ`/`PROPOSE` 白名单；`work_dialogue.py` 继续重导出类型以兼容现有调用点。
- Work 预处理 Skill 预检还会阻断同计划 ReHo + `CUT` scrubbing，定位 `preprocessing.scrubbing.method` 并要求先生成、核验 retained-volume checkpoint；ALFF-only + CUT 与无 preprocessing 的 metric-only ReHo 不触发该能力 gate。dry-run 与正式 resolve 在 Plan 写入前失败，feedback 为 `BLOCKED_CAPABILITY` 且不自动修改选择。原执行器拒绝和独立 `EFFECTIVE_VOLUME_COUNT_UNKNOWN` 科学证据提示仍保留。
- 正式 metric-only Skill resolve 与 API dry-run 复用已登记 checkpoint 的现有 manifest、来源 Plan 和 lineage 核验；只有 metric-only 请求要求冻结 manifest 恰有一个 subject，且 Artifact lineage.subject_id 与之相同。跨 session、批量、无效 scope 或身份不匹配在 Plan 写入前失败关闭；预处理同 session 多 subject 仍可走原路径。Work 内部 dry-run 仍只编译预处理草稿，尚无 metric-only Work draft 或 Artifact picker。
- `planning_feedback.py` 与 `preflight_adapter.py` 将 Work 草稿检查映射为有来源和修复边界的确定性反馈。预处理通用问题指纹同时绑定候选草稿修订和对应证据值；只有相同草稿与证据的重复问题才触发无进展停止，草稿变更可在本轮剩余预算内重验。预处理候选在首次草稿及后续反馈校验时，可用冻结项目/manifest 绑定运行只读 `SkillResolver → SkillValidator → SkillCompiler` dry-run；仅请求实际 preprocessing 时应用执行器共享的 subject/session scope gate，跨 session 输入以 `BLOCKED_CAPABILITY` 返回明确字段和整理建议，同 session 多 subject 与 metric-only 保持原路径。相同预检会阻断 preprocessing + 任一 metric + `ON_RESULTS` normalization，因为当前执行合同只有一个 metric mask binding，不能表达所需的独立 mask-space 输入；single metric 也会阻断，disabled normalization、无 metric 和 metric-only 不命中该 gate。另一共享执行谓词会阻断 preprocessing + 无指标参数 + `ON_RESULTS` normalization 或 smoothing，并分别指向 timing 字段；两者都启用时各返回一项。请求任一指标参数、非 `ON_RESULTS` 和 metric-only 不命中此 gate。两类问题都返回独立 remediation，正式 resolve 在 Plan 写入前失败，Work feedback 分类为 `BLOCKED_CAPABILITY`，不自动修订科研选择；提示不推断真实图像或掩膜空间。分析统计候选也会调用正式 proposal assembler，并复用 lineage、来源文件、校正、设计矩阵及 Skill 参数合同校验。两类 dry-run 都只验证本地结构与可核验来源，不探测本机 MATLAB/SPM/DPABI，不代表真实影像/统计可执行；统计来源文件核验后还会重读项目、manifest 或外部纳入修订。它们不创建 Plan、Approval、Run、事件或授权。分析的 tail、missing-value policy、校正（包括显式 `none`）及协变量中心化须有明确选择；旧草稿无法证明中心化选择时作为未决项交给用户，反馈环不自动补选。已知科研选择与输入冲突进入 `ASK_USER`，未支持的 DICOM/指标、缺失 Skill、编译或审核治理缺口进入 `BLOCKED_CAPABILITY`，未知阻断以通用问题要求处理；预处理草稿自动修订 allowlist 为空。
- `automation/` 定义持久 WorkTask/TaskStep 的传输中立状态及应用服务；`automation/step_lifecycle.py` 以纯函数共用预处理与统计 Run 的启动绑定和完成状态转换，类型专属权威检查仍由应用服务执行（见 [ADR 0043](../../docs/adr/0043-shared-worktask-step-lifecycle.md)）。SQLite 仓储通过组合根接入现有 Unit of Work。既有 `workflow/worker_main.py` 在启动和空闲轮询期间扫描索引化等待目标，以独立短租约和随机 fencing token 领取一个任务；除运行/QC/统计结果等待外，只恢复步骤、Plan 和 Run 身份匹配的预处理 `downstream` 目标，普通科研选择和通用 dependency 不进入扫描。`reconcile_waiting_run(task_id)` 只用绑定 Run 的事件身份做去重/唤醒，再通过 Run `version` 前后夹读重读 Run/QC/Artifact；三次读取仍不稳定时返回 `indeterminate` 并保留任务生命周期状态。`QC_REVIEW` 中 `pending`/`awaiting_review` 且审查尚无或为 draft，以及 `failed` 且审查为 draft，等待用户明确处理；失败状态缺少审查或证据冲突时阻断。通用 Artifact 引用按 ID 与项目归属核验，可指向上游 Run；当前 Run 输出清单独立返回，不用于推断通用引用缺失。预处理在 QC gate 通过后按冻结 SkillPlan/manifest 核验精确 Artifact 目标；目标不完整或 lineage 矛盾时阻断，全匹配才完成步骤与任务。统计结果仍按专属报告与 Artifact 证据核验。事件按最多 1000 项有限批次回放，启动和周期扫描也会在没有新事件时重读权威状态以弥补漏唤醒。Claim token 在事件消费和任务状态写入事务内核验，过期持有者不能提交。不读取事件 payload，不调用模型、不创建 Run 或批准 QC。统计方案/报告的自动下一步准备仍未实现。
- `automation/analysis_outbox.py` 为明确的 `preprocess_then_analyze` 请求持久化提案专用续接记录；记录通过 intent ID/hash 精确指向会话原请求，绑定 conversation/project、预处理 WorkTask、冻结 Plan/manifest/Run。只有预处理目标 Artifact 全部通过现有完整性核验后，任务完成与 outbox `ready` 状态及精确 Artifact IDs 才在同一 SQLite 事务提交。独立消费者会在模型调用前重新核验全部权威源，在事务外准备草稿，再以 fencing token 把 assistant-only continuation、分析 WorkTask/提案及 outbox 终态放在同一 Unit of Work 内原子提交。精确统计输入 allowlist 由本次目标 Artifact 和单独核验的 lineage mask 依赖组成。重复消费幂等；源变化、fencing 失效会丢弃模型结果；缺少用户科研决策或超过有界重试次数时等待用户，提供针对性重试入口。该通道与现有 workflow job/reconciliation 隔离，不创建/批准统计 Plan 或 Run；详见 [ADR 0041](../../docs/adr/0041-proposal-only-analysis-continuation-outbox.md)。
- 复合分析 intent ID 按会话与 HTTP 幂等键作用域化，并随 active 预处理草稿经编辑/审查/编译写入 WorkTask/TaskStep；启动后仅凭该任务引用精确恢复并校验原始请求。普通新预处理不继承历史 intent。显式来源登记允许用户选择精确预处理 Task/Run，为共享来源创建多个独立 intent/outbox；每个目标的统计 WorkTask ID 按 intent 隔离。目标摘要来自同一 snapshot 内匹配 intent、会话、项目及请求 hash 的唯一请求内容，相同请求的重复快照可合并，无法验证时为空。多目标的前台选择/继续和通用目标 dispatch 尚未接通，具体进度见计划 0014。
- Work 预处理在已提交的 `skill_plan` 确含冻结预处理参数后，按会话/角色/Plan 修订 ID 创建持久 WorkTask；目标文本来自可关联到预处理 Agent 的用户消息，任务引用冻结 manifest hash。显式授权创建的 Run 与任务状态、会话角色链接在同一 Unit of Work 中收口；`work-draft`、`work-review` 的成功 Plan 路径也写入任务及会话关联。部分入口的 Plan 在 finalize 前已经保存，因此不概括承诺所有 Plan 生成入口都与任务同事务。
- Work 分析提案按会话与 proposal hash 创建持久 `statistical_run` WorkTask，冻结统计 Plan 时绑定任务和会话角色链接，显式授权创建 Run 时再原子绑定 Run。统计任务不使用头动 QC 判据；唯一真实、非占位、且完整匹配 Run、设计修订、Plan 哈希、报告/manifest 和 Artifact 清单的结果完成任务。零显著结果有效；无结果时等待登记；mock/synthetic 或身份不一致时阻断。结果任务完成后会以同一 Unit of Work 创建独立的解释 outbox；模型调用仍由独立 Worker 完成，不在 reconciler 中执行。
- `automation/result_interpretation.py` 定义真实 StatisticalResult 解释续接状态和有界模型事实投影。完成器在任务收口事务内入队，消费者重核完整来源并只提交 assistant-only 解释/报告草稿；有界聚合事实不包含完整报告、受试者矩阵、路径或逐 Artifact 详情。该消费者不创建或批准统计 Plan/Run，详见 [ADR 0042](../../docs/adr/0042-statistical-result-interpretation-outbox.md)。
- `automation/composite_goal.py` 定义 `preprocess_then_analyze` 的身份、只读 snapshot 投影和完成条件；当前 progress/list 查询仍只面向该类型。另有 `analysis_only_external_import` identity root：仅用于用户显式选定的已纳入外部指标，为前台第一次分析模型调用预先绑定来源 revision 和累计预算，再关联 Statistical WorkTask；它不声明 preprocessing Task/Run，也没有 proposal/result outbox 或统一进度投影。两种目标均不替代 Plan、Approval、Run、QC 或 Artifact 的权威状态。
- `work_agents.py` 负责预处理/分析意图路由、角色专属历史与结构化交接；`statistics_draft.py` 的统计草稿与 `work_draft.py` 的预处理参数草稿分开保存；
- 角色路由支持中英文任务和两阶段依赖，预处理诊断与统计故障分派到各自角色。空任务的模糊续接先澄清；明确手动选择保持优先，已绑定任务不会因短续接切换。
- `work_tasks.py` 读取服务端角色任务引用并校验统计确认作用域。Work 统计方案绑定会话、提案消息/哈希与草稿哈希；方案和运行创建与角色引用保存原子提交。切换保留各自任务，旧草稿及另一角色的确认被拒绝，幂等重放不重新切换前台。
- Work 的只读结果查询保留会话工作区和原统计草稿；模型路径及无模型提交路径均可重新读取同一正式方案和运行，不依赖浏览器缓存。
- `work_handoffs.py` 从服务端记录生成完整的预处理到分析凭据，包含项目/数据清单、运行与方案、QC 纳入顺序、逐指标版本/谱系/来源、掩膜及哈希。接收时复查输入和产物；Work 正式统计方案冻结凭据，校验、批准及启动前再核对。耗时文件检查在 writer 事务外，事务内重复检查元数据；同一提案的重复创建被阻止。
- 共同有效掩膜的交接还包含各受试者原始掩膜，复核来源内容、空间和网格；原始掩膜变化也会使下游凭据失效。此共同引用只对应实际内容相同的掩膜，不重新定义指标计算范围。
- 反向交接只接受九类 ALFF/fALFF/ReHo 输出名称，规范化需求并冻结项目及内部/外部来源。接收前重新核验；外部指标保留原始输入未知状态。每会话/项目最多六项唯一需求，重复回执不重复计数，阻断请求不能通过重放绕过上限。
- 两角色的 Work 工具循环和结构回退共用一次请求的步数、累计 token 与墙钟预算；回退只使用剩余预算，停止时保留草稿和审计。
- `statistical_capabilities.py` 记录六类 DPABI 统计入口、设计要求、产物合同和验证状态；六类统计目前已通过本机 DPABI V9.0 真实 MATLAB smoke；
- `conversation_work.py` 与 `work_dialogue.py` 将 DPABI 阶段发现、模型建议和用户确认分开：模型只能引用服务端候选，`selected_input_stage` 由服务端复查后写入冻结 manifest；
- 首次工作区在用户回复服务端签发的阶段确认口令后登记项目/数据集并冻结清单；页面短校验码先验证前缀，再以完整候选哈希复查，不要求用户先完成另一个隐含登记步骤。
- 已绑定项目的 Work 对话若历史消息缺少 dataset/skill_plan 引用，只在项目下恰有一个已登记数据集时按登记记录续编；零个或多个数据集均明确阻断，不新建项目，并继续执行现有工作区与冻结 manifest 校验。
- 文件授权按稳定模型配置哈希绑定具体模型；工作区只读工具接受列表返回的匿名路径，所有本地解析仍受范围、目录排除和链接限制。NIfTI 仅返回数值头事实，不出站描述文本或体素数据。
- Work 草稿在模型建议后生成服务端确定性的 `plan_preview`，展示公共预处理、ALFF/fALFF 与 ReHo 分支及消费的中间检查点；该预览只用于审查，仍由 Skill 编译器和一次性执行确认控制实际运行；
- Work 草稿同时生成 `planning_decisions`，标出关键参数的用户草稿、候选建议、缺失及校验冲突状态；模型上下文、对话回复和参数面板更新共用该摘要。它仅解释草稿，科研事实与执行资格仍以来源证据、用户确认及 SkillValidator 为准；
- 运行失败后，Work 的只读诊断工具可对受限 attempt 日志调用确定性 DPABI 分类器；只向模型返回分类、摘要和建议，不传递日志证据文本，也不会自行变更方案或启动重试；
- `reporting.py` 生成不调用模型的确定性复现报告。
- `statistical_result_facts.py` 核验冻结报告、清单及设计绑定，生成方法与推断摘要。实际校正/软件证据通过登记文件哈希验证后读取；分析工具及初始上下文只提供有界簇、定位和按组/条件的 ROI 汇总，不发送完整报告或逐受试者值。初始 ROI 明确限首个登记簇；原生空间与未知空间分别报告。
- 正式统计方案可冻结图谱 ID、实际名称、文件内容版本及定位方法；校验、批准和启动复核，结果报告继承该记录。没有指定图谱的历史结果仍兼容；空结果先正常返回，不触发图谱或空间检查。
- `service_mixins/external_imports.py` 保存外部指标的只读检查与独立纳入凭据；统计设计引用导入哈希及稳定影像引用，重新校验源目录和 mask，不伪造预处理运行或已知 TR。
- 真实统计完成时从已校验 `clusters.tsv` 组装簇记录；结果定位用登记图像哈希和配置 DPABI 图谱重新验证峰值、26 邻域簇范围及脑区覆盖比例。
- 显著簇 ROI 查询从冻结报告恢复设计，校验内部 Artifact 或外部影像凭据及内容哈希，返回逐受试者/条件的描述性均值；零显著体素正常返回空表，不触发空间或图谱检查。

- 显式 TaskStep 的 Run 重核目前只接受唯一的预处理或统计步骤，且 Task 与 Step 必须引用同一 Run；未知/混合步骤在事件消费前返回 `indeterminate`。无步骤历史任务保留只读兼容重核路径；共享完成转换不会把 sibling step 隐式标记成功。

本层不导入 `neuroagent.infrastructure`（由导入边界测试守护），不实现具体影像算法，不直接启动进程。HTTP 细节属于 API 层。
