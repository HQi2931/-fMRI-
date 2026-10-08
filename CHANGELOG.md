# Changelog

本文件记录项目中对用户和开发者可见的重要变化。

## Unreleased

- 运行详情重新加载失败时保留同一次执行已读取的产物、方案和阶段记录，并提示详情可能未更新；重试进入下一次执行时清空旧结果。

- QC 审查读取失败时只提供重新加载，不把连接失败当成没有审查记录，避免重复生成审查；已登记产物仍可查看和下载。

- 最终确认弹窗补齐键盘首尾焦点循环，避免 Tab 从最后按钮移出弹窗；真实手机页面已验证正向及反向循环。

- 最终启动确认显示本次指标、TR、时间点数和删除的初始时间点数；首次编译与重新校验共用摘要，避免刷新确认后丢失参数信息。

- 方案流程预览保留用户指定的预期输出名称，已知产物标识转换为中文，避免不同结果都被笼统显示为“所选指标结果”。

- 确认提交时可在弹窗内停止等待，并明确该操作不会取消计算；停止或超时后关闭弹窗，确认口令和按钮文字不再回填为普通聊天输入，提示刷新核对提交结果。

- 方案审查兼容后端带中文名称的研究方案引用缺项提示，问题列表可直接定位必填字段，字段与摘要一致显示“待补充”。

- 方案并发变化造成明确版本冲突时，页面读取并展示最新方案、关闭旧确认弹窗，提示重新审查；响应丢失的恢复仍只做一次读取，浏览器回归验证不会再次提交启动。
- 启动确认发现输入文件已变化时，页面经方案复核恢复失效状态：保留参数选择、撤回旧事实并显示起点重确认入口。
- 输入修订变化后撤回旧读取事实与启动确认，保留用户参数并提供阶段重确认；重新冻结和重新读取均完成后才能生成新方案。历史读取来源标记待重读，已保存请求响应仍可幂等恢复。完整网页恢复验收继续进行。
- 窄屏方案审查支持打开时聚焦、键盘焦点循环、Escape 关闭并返回入口，背景不能操作或滚动；切换屏幕宽度保留未保存内容，最终确认弹窗可独立处理键盘。
- 修复工作区新建普通文本误报符号链接的问题；新建和应用补丁均拒绝默认排除目录，保留影像格式只读、原文件变化拒绝以及失败回滚。补充结构化文档读取和恢复测试。

- 起点审查使用服务端的简短按钮说明，不再沿用模型输出中的确认口令或未经执行的启动声明。已用于运行的参数面板显示冻结只读状态；取消、重试或 QC 操作的错误不会被后台详情刷新清除。
- 更新 Python PDF/HTTP 依赖锁及前端测试依赖，修复当前审计发现的漏洞；浏览器回归覆盖按钮确认及刷新后恢复同一运行，补充工作区手动路径、起点选择、授权和 QC 状态测试。完整门禁仍以验证记录为准。

- 修复真实规划时脱敏 JSON 片段无法推导层顺序的问题：单独提取有范围限制的 TR/层时间数值证据，不恢复敏感正文。用户明确停用 ALFF 前滤波或空间平滑时，清除模型遗留的不适用配置，避免反复要求用户处理无效参数。

- Work 等待过程显示实际模型处理、工具检查与方案校验阶段，正式正文在校验和保存后呈现，避免先显示内部字段或模型错误的执行声明再替换；断流保留原幂等键，Chat 正文增量继续有效。

- Work 跨回合保留已发现的采集参数冲突，单份重读不再覆盖已有冲突；非法影像头数值不会成为已核实事实。统一模型摘要、理由和问题的输出要求；历史方案仅在展开时加载只读参数面板，减少长对话中的隐藏控件。

- Work 增加折叠的本轮阶段耗时，保留幂等重放记录；问题可绑定参数以减少重复追问，模型建议理由单独标注并避免展示与明确选择冲突的说明。方法问答后可继续审查仍有效的确认版本，旧版本和已使用确认不能恢复启动。

- 修复真实网页计算完成后 QC 编号被事件脱敏损坏的问题：系统 UUID/哈希保留严格格式，敏感字段仍优先脱敏；结果页直接查询运行对应的 QC，单项详情失败不遮蔽产物。网页刷新保留已选模型，确认响应丢失后只读恢复已保存操作；单起点不再显示单选框。MATLAB 退出后分块读取剩余日志，避免遗漏末尾阶段事件。

- Work 增量修复：确认请求在幂等重放后校验当前版本，批准与运行创建保持原子性；响应正文丢失时保留重试键。修复完整输入冻结和已编译方案启动入口，移动端关闭审查保留未保存编辑；对话支持自然语言列表与重点格式，结果使用中文名称并自动预览，QC 读取失败保留其他产物。MATLAB 阶段识别兼容实际 SPM 多空格及 Old Segment 日志。

- Work 体验优化阶段实现：精简普通回复，增加分组可编辑方案、来源与校验状态、直接界面确认、运行结果与 QC 展示及产物下载/预览；只读数据概况跳过全量文件内容哈希，保留冻结和执行完整性检查。完整优化方案见计划 0012；新版真实网页计算与完整恢复场景仍待验收。

- 修复网页 Work 真实对话的模型预算、文件读取和阶段确认链路：设置页可调整已有模型的超时、上下文、输出及 DeepSeek 思考模式；工具测试区分超时与截断，Work 超时停止重复请求并保留审计。token 预算不再重复累计缓存/总量，文件授权哈希跨重启稳定。
- Work 支持手动填写工作区路径、停止客户端等待及首次工作区的起始阶段确认；只读工具可沿匿名路径读取嵌套目录、JSON 和 NIfTI 数值头信息，保持原始数据只读与受试者标识脱敏。
- DemoData Sub_001 的 Segment＋ALFF 组合已通过真实网页多轮对话、参数审查、单独启动确认及 MATLAB 运行验证；14 项产物校验通过，9 份 NIfTI 与后端对照运行体素值一致。模型早期方案需要纠正，计算完成仍等待人工 QC；其他组合尚未据此验证。

- 真实 ALFF 完整预处理验证修复产物收集：自动掩膜登记使用 DPABI 返回的实际指标掩膜；结果平滑后从返回的 `ResultsS` 目录收集影像，避免使用原生空间掩膜或误报平滑指标缺失。

- DPABI 预处理改为一次调用执行所选步骤与 ALFF/ReHo 指标；取消独立 T1 准备和中途审查 API，完成后统一登记产物。Segment＋ALFF 已完成真实 MATLAB 验证，其他组合仍待验证。

- Work 的 ALFF/ReHo 方案草稿增加服务端参数决策摘要，显示关键选择的来源状态、缺失项、冲突和指标分支，并在后续对话中提供相同信息；建议值不会标记为已核实的扫描事实。

- Work 增加 DPARSF 风格可编辑预处理参数面板、草稿版本校验及幂等保存；新增 T1 准备、配准、Segment/New Segment、DARTEL 参数和受控模板。按用户要求，T1 准备与下游处理合并到同一次预处理，不增加中途人工审查或批准。真实 MATLAB 新路径尚未验证。

- Work 增加选项式确认弹窗，覆盖起始阶段、方案参数与启动/QC 选择；支持审查摘要、修改要求、暂不执行与重新打开。选项不预选，提交沿用服务端版本校验和一次性执行授权。

- Work 失败回复增加安全诊断，区分模型请求、输出结构与参数草稿校验，记录模型结束原因及 token 用量；输出截断时明确提示。日志和消息 payload 不记录模型原文、字段值或异常原文。

- Chat 与 Work 对话接入流式正文：支持 Provider 原生增量、工具轮次与模型回退 reset、UTF-8 分块展示及落库后 final；Work 只预览 answer，完整结构校验和逐次执行确认仍由服务端控制。断流保留幂等键，已派发操作继续保存结果，支持显式重试恢复。

- Work 模型提示词支持主动规划和有依据的参数建议，区分数据事实、待审查建议与用户确认，说明 ALFF/fALFF 和 ReHo 的检查点、平滑顺序及已知执行限制；保留现有 JSON 契约和逐次执行确认。
- Work 方案草稿新增服务端确定性计划预览，展示公共步骤、ALFF/fALFF 与 ReHo 分支及各自检查点；模型上下文增加去标识化的工作区数据画像，只提供数据类型、数量、问题和警告，不把参数从文件名推断出来。
- Work 工作区选择集中为会话顶部面板：文件夹名称优先展示、完整路径可展开；预处理阶段采用可折叠单选卡片，展示处理含义、覆盖数量和不可用原因，文件授权与读取范围统一收纳。
- 修复 Work 文件授权被模型能力配置阻断的问题：设置页可编辑 `tool_use`，授权前以无工作区内容的探针验证具体模型，并在工作区或 Profile 配置改变后使旧授权失效。
- Work 在 DPABI 起始阶段已经确认且模型重复推荐同一阶段时会继续保存预处理草稿，避免卡在重复确认。
- 移除出站文本对临床词汇、年龄描述和中文姓名句式的拒绝规则；受试者 ID 仍会伪名化，路径、联系方式和凭据仍按原规则处理。
- 新增只读运行诊断工具：失败后可从当前 attempt 的有限 MATLAB/DPABI 日志生成分类结果与建议，只向模型提供摘要、不传日志证据文本，也不会自动改参数或重跑。

### Work DPABI 阶段续跑

- Work 现在对 DPABI 工作区返回完整阶段候选；模型可结合用户意图建议 `FunImgAR`、`FunImgARW` 或 `FunImgARWS`，但服务端拒绝候选集外的建议，且即使只有 `FunRaw`/`FunImg` 也必须由用户显式确认。
- 冻结 manifest、计划和审批绑定阶段名、规则版本及检查结果。`in_place` 续跑按已完成步骤关闭 DPABI 配置、禁止重复删除初始时间点，并在 Worker 启动前重验输入和伴随文件哈希；旧的隐式单阶段请求失败关闭。
- 新增 [ADR 0017](docs/adr/0017-explicit-dpabi-stage-selection.md) 与阶段选择契约说明。

### Agent 控制面加固

- 落地计划 0009：Provider/工具硬 deadline、profile 感知上下文预算、回退工具轨迹审计、Work conversation/project/run 作用域隔离，以及持久化一次性 ConfirmationChallenge；真实 MATLAB 运行不再由客户端布尔值单独授权。
- 新增 `0015_confirmation_challenges` 迁移并同步 API/OpenAPI/前端生成类型；确认挑战在运行创建事务内消费，过期、篡改、跨项目和重放均失败关闭。

### 文档

- 新增 [计划 0009](docs/plans/0009-agent-control-plane-hardening.md)：设计 Agent 真实超时、profile 感知上下文预算、回退审计保留、项目作用域工具和一次性执行确认挑战的分阶段加固方案。
- 新增 [ADR 0015](docs/adr/0015-agent-control-plane-hardening.md)：接受 Agent 控制面预算、项目作用域、回退审计和一次性执行授权的约束。
- 新增 [文档规范](docs/development/documentation-standard.md) 与 [文档索引](docs/README.md)：统一状态块与状态枚举、目录与命名规则、权威文档表、归档规则和禁用的发布版本包装。
- 新增 `scripts/check-docs.ps1` 并接入 `scripts/quality-gate.ps1`：校验链接与锚点、状态块与枚举、归档入链、索引覆盖和禁用版本标签。
- 归档 6 个已完成或被取代的文档到 `docs/archive/`（计划 0002、计划 0004、早期框架草案、Chat 模块分析、两次服务/仓储重构文档），并加归档横幅、修正全部入链；移除 `docs/superpowers/` 目录。
- 3 个 Chat 文档从 `docs/` 根目录归位到 `docs/development/`。
- 从现行文档移除 `v0.1.0`/`v0.2.0`/`v1.0.0`/`MVP1.0` 发布版本框架，改为按能力状态与阶段描述：计划 0007 重命名为 `0007-capability-modularization-roadmap.md`，目标架构重命名为 `target-architecture.md`，`README.md`、`AGENTS.md`、产品范围、验证记录、系统设计与相关 ADR 同步更新。ADR 0006 作为历史决策记录保持原样。
- 全部 `docs/` 现行文档与模块 README 补齐统一状态块；新增 `neuroagent/{agent,api,application,chat,literature}` 五个缺失的模块 README。

- 完成一次由用户授权的 **DemoData Sub_001 真实 DPABI 预处理验收**：经 Work 对话自然语言路径与服务端精确确认口令驱动，公共 Worker 调用本机 MATLAB R2024b / SPM25 / DPABI V9.0_250415，运行进入 `qc_review`，登记 6 个带 SHA-256 的 Artifact（时序、脑掩膜、raw ALFF、执行日志、metadata evidence、头动 QC 报告），源数据 SHA-256 不变。`raw_alff.nii` 的哈希与 2026-09-18 冒烟**逐字节相同**，证明版本证据修正只影响元数据与哈希链、没有改变计算，且本机计算可复现。证据见[验证记录](docs/development/mvp-verification.md)。
- 新增 `tests/science/test_demodata_acceptance.py`（标记 `demodata_smoke`，默认跳过，CI 不启用）与 `neuroagent/infrastructure/filesystem/nifti_header.py`：验收参数一律从 NIfTI 头推导而不是文件名。据此实测 DemoData 三受试者时间点数**不一致**（Sub_001 为 234、Sub_002/003 为 239），文件名中的 `240` 只是扫描仪描述符；三受试者计划因此必须先做逐受试者归一化，且该归一化属于测试夹具而非科研默认值。
- 修复 `scripts/docs-policy.ps1` 在工作树存在未暂存重命名时崩溃的问题（`git ls-files --cached` 仍返回旧路径），并修正 `docs/development/documentation-standard.md` 归档横幅示例中被误认为真实链接的占位符。

- 新增有界工具调用循环（[ADR 0014](docs/adr/0014-bounded-tool-loop.md)）：工具按 `READ`/`PROPOSE`/`EXECUTE` 三级声明权威，**EXECUTE 工具在目录构建阶段即被丢弃**，不进入发给模型的 tools 数组；循环有步数、墙钟、token 三重预算与重复失败熔断，停止原因是显式枚举。声明 `tool_use` 能力的模型走循环，未声明的模型保持原有单次路径不变。
- Chat 由此支持多跳检索：模型可连续调用 `search_evidence` 与 `list_skills`，工具分配的证据编号与最终引用编号一致；既有引用校验（无效 `[Cn]` 剔除）继续生效。循环未产出答案时自动回退单次路径。
- Work 接入同一循环，但只提供**只读**工具（运行状态、运行产物类型、已登记统计结果、多重比较校正能力、Skill 清单）——这些正是 `work_context` 未携带的状态。循环的最终文本就是 Work 原有的 JSON 契约，因此解析、校验与全部授权路径未变；若循环产出不是合法 JSON，则回退单次路径重试，保证 JSON 模式仍然有效。工具轮次不再同时要求 JSON 输出模式（二者组合会被多数 OpenAI 兼容网关拒绝）。
- Work 的模型可选用动作改由 `neuroagent/application/work_actions.py` 单一维护，`WorkInterpretation.action` 校验集合成员。原 `start_preprocessing` 更名为 `propose_run_confirmation` 并降为提案层级：它只渲染需要用户本人回复的确认指令。关键词回退路径不再能返回启动、批准、跳过 QC 或建立工作区等动作。
- 新增统计设计装配器 `neuroagent/application/statistics_draft.py`（对话式统计设计的第一步，尚未接入对话）：把「方法选择」与「数据来源」显式分开——模型/用户只能指定检验类型、分组列名、协变量列名与中心化、尾部和校正参数，**受试者顺序、各受试者产物 ID、掩膜产物 ID 与协变量取值全部由服务端提供**。装配器不重复领域层已有的科学校验（对齐、配对顺序、分组数、规范对比向量、设计矩阵满秩、残差自由度），只负责取值来源与顺序，并把领域拒绝翻译成可展示的阻断项而不是抛异常。设计说明见 [统计设计的取值来源与装配](docs/architecture/statistical-design-assembly.md)。
- 同模块的 `collect_design_sources()` 从运行的已登记产物与 QC 决定解析设计来源：**受试者顺序取自 QC 决定而非文件系统**；每个受试者必须恰好一个指定类型指标产物，掩膜必须恰好一个，缺失或歧义一律阻断而不擅自选择。
- 统计设计草稿 `StatisticsDraft` 支持多轮累积，并**剥离模型提交的服务端独占字段**（运行 ID、QC revision 与哈希、输入清单哈希、确认哈希），因此模型无法自行把设计绑定到某个运行或 QC 决定，也无法预先封印确认。草稿渲染会明确标注哪些值由服务端在确认时冻结登记。
- 新增 `0014_agent_tool_loop` 迁移：`conversation_tool_calls` 增加 `step_index`、`risk_tier`、`provider_call_id`、`duration_ms` 与 `(conversation_id, step_index)` 索引；循环步进与会话消息在同一事务内落库，并按 `step_index` 排序。Chat 与 Work 共用同一份步进收口逻辑。
- 工具循环的预算改为可配置并默认保守：`RSFMRI_AGENT_TOOL_LOOP_ENABLED`、`RSFMRI_AGENT_MAX_TOOL_STEPS`、`RSFMRI_AGENT_MAX_TOOL_SECONDS`、`RSFMRI_AGENT_MAX_TOOL_TOKENS`、`RSFMRI_AGENT_MAX_REPEAT_TOOL_FAILURES`、`RSFMRI_AGENT_TOOL_RESULT_MAX_BYTES`。关闭开关后即使模型声明 `tool_use` 也不会收到任何工具，行为回到循环之前。
- 前端在回复气泡下按执行顺序展示工具步骤（名称、层级、状态、耗时），刷新后可从会话记录恢复；`ConversationToolCallView` 与 `ConversationTurnView.tool_calls` 已同步到 OpenAPI 与生成类型。
- 修正 DPABI 版本证据：`evidence_refs` 与 Skill 兼容性标签从 `V8.2_240510` 更正为实际安装并核验的 **V9.0_250415**（MATLAB R2024b、SPM25 25.01.02）。此前每次编译都会产生三条不成立的版本不兼容告警。
- 修复一个真实缺陷：Alembic `fileConfig()` 默认 `disable_existing_loggers=True`，迁移一旦在进程内运行就会静默关闭全部 `neuroagent.*` logger，导致 API 未处理错误日志与 Worker 执行器故障日志在生产环境丢失。现已保留既有 logger。
- Worker 失败诊断改进：执行器异常在本地记录完整 traceback，落库原因改为经过脱敏的可定位摘要（例如 `executor_error:MatlabExecutionError: MATLAB executable not found: [REDACTED-PATH]`），并绑定同一次尝试的 trace id 以便事件关联。
- Chat 接入完整语义路由，修复 ALFF 方案查询误判为执行请求；Work 接入模型回答、多轮参数草稿、完整参数审阅与显式确认后的 Skill 编译。草稿变更使旧确认失效，启动需要当前方案版本与 QC 选择，模型不能授予执行权限。确认产生的数据库变更与会话记录在版本检查后的同一事务中提交，事务内查询可读取本次写入；模型上下文预算包含草稿及字段结构。

- 对话工作台视觉更新：采用森林绿侧栏与浅色内容区，统一模式切换、会话工具栏、消息、输入框与设置表单；优化窄屏排版、键盘焦点和多行消息阅读。

- QC 改为 Work 启动时由用户明确选择。选择跳过时，DPABI 成功产物直接完成运行并可作为统计来源；新增 `/runs/{run_id}/qc-skip` 记录版本、操作者、原因和哈希。头动检测失败保留计算产物与失败证据，用户可随后显式跳过 QC；统计报告会标记 QC 跳过决定。

- 前端进一步收敛为 Chat/Work 双模式对话工作台，环境与模型配置保留在右上角设置；删除独立业务页面、消息操作卡片、卡片能力目录、草稿状态和卡片动作接口。旧业务地址统一显示同一工作台，既有底层 REST 业务服务继续保留。

- Work 新增受控的 DemoData 单受试者 ALFF 自然语言闭环：冻结独立工作副本，生成明确的 TR/时间点/Slice Timing/Realign/频段与 DPABI 自动掩膜计划，展示哈希并等待精确确认，再自动审批并排队真实 MATLAB。DPABI 自动掩膜作为生成型 Artifact 参与时序和 ALFF 谱系，不需要预先登记掩膜。

- DPABI 运行完成钩子新增自动头动 QC：成功登记产物后解析 `RealignParameter`，生成 `qc.head_motion_report` Artifact，并自动创建人工待审 QC 草稿；含指标图的运行进入 `pre_statistics` QC，纯预处理进入不可用于统计的 `head_motion` QC。新增 `/runs/{run_id}/head-motion-qc` 补跑接口，Work 可展示、补建并批准精确 QC hash。

- 2026-09-18 完成一次授权的真实 Work→Worker→MATLAB→DPABI 冒烟：单受试者 234 volume、TR 2.0 秒，完成 Slice Timing、Realign、自动掩膜和 raw ALFF，运行进入 `qc_review`；时序、脑掩膜、ALFF、执行日志和 metadata evidence 均登记 SHA-256，源 DemoData 哈希保持不变。该结果只证明技术闭环，不作为科研结论。

- Work 对话补齐可审计的项目建立与运行前闭环：可从用户选择的工作区显式创建项目、数据集和冻结 manifest，可用完整科研参数编译预处理计划，并在真实启动前重新校验审批、输入、环境、路径访问和磁盘空间。Agent 页面会自动恢复并轮询活动运行，也可通过 Work 查询 QC revision 与统计结果；多步 Work 准备阶段不再占用长 SQLite 写事务。

- Work 移除工作区格式检查、模型格式建议和整理预览：用户选择目录后自行维护其中的 DPABI 文件；系统只保留路径安全、文件清单审计、输入哈希、环境和磁盘空间等技术校验。通用数据检查与整理预览 API 不受影响。

- Chat/Work 接入统一上下文引擎：按模型窗口选择最近完整轮次、检索证据、确认记忆和权威工作状态；长对话按阈值生成滚动摘要，并保存脱敏结构化快照。新增会话/项目记忆 API、Agent 记忆管理入口，以及模型上下文窗口和回答 token 上限配置。

- Chat 科研问答闭环：最近 12 条消息接入脱敏模型上下文，寒暄免检索，意图与追问改写合并一次调用；PDF 可手动加入独立知识库，支持索引状态、失败重试和论文筛选；回答按证据编号保存引用，可逐消息展开并定位原 PDF 物理页。新增索引状态迁移，旧论文不自动建索引。

- 新增 rs-fMRI Chat Mode Phase 1：独立 `ChatAgent`、窄接口、PDF 文献管理、科研 section 识别、section-aware chunking 和逐页 traceability；按后续要求直接复用 fMRIAnalysis 的 DashScope/Chroma RAG，修复融合去重并减少重复重排。上传 chunks 由显式“加入知识库”操作向量化，旧库缺失页码保持未知。
- 新增目标架构、能力化模块单体 ADR 和阶段性演进路线图，明确以显式应用服务、窄端口、版本化 JobEnvelope、服务端状态真源和前端 feature 分片渐进演进，不拆微服务。
- 新增 Agent 对话工作台：可选择本机 fMRI 工作区，调用只读检查并在右侧展示 DPABI 输入阶段、受试者配对、NIfTI 头标记问题和已有结果目录。
- Agent 工作台新增 `Chat`/`Work` 分类：Chat 调用本地 rs-fMRI RAG 并展示证据，Work 承载工作区检查、预处理和结果分析入口。
- Chat 现可把本地 RAG 证据交给用户选择的 LLM 生成回答；显式开启联网搜索时，仅路由到声明 `web_search` 能力的模型，并保存模型、脱敏上下文哈希和 URL 引用。
- 设置页改为服务商 API 绑定：绑定后立即读取 `/models`，Agent 对话框直接列出该服务商返回的全部模型，不再要求用户逐个创建“已配置模型”。
- 新增 `POST /api/v1/workspaces/check` 工作区检查接口；检查不会注册项目、修改原始数据或启动 MATLAB。
- Chat/Work 多轮消息、会话绑定和每次工具调用现已持久化到 SQLite，可在刷新页面后恢复。
- Work 对话已编排工作区检查、运行进度和 DPABI 启动；真实启动继续要求已审批计划和逐次确认，并进入现有 Workflow/Worker。
- 工作区目录只能通过系统文件夹选择器选取；新增显式 `in_place` DPABI 模式，在复核冻结输入哈希后把所选 DPABI-ready 工作副本作为 `WorkingDir`，结果 stage 写回该工作区，运行脚本、日志和登记产物保持 attempt 隔离。

### 真实执行与证据闭环

- 新增 `ExecutionBackend` 控制面：Mock 默认；MATLAB 必须逐次确认、配置开关和环境探测同时通过。
- 新增批准计划专用 `WorkflowFactory`/`ToolRuntime`：Mock 逐节点执行 DAG；MATLAB 适配器校验冻结计划并编译受控分段作业，不逐节点经过 `ToolRuntime`。
- Worker 已注册 `matlab_preprocessing`/`matlab_statistics` 路由；统计模板覆盖三类 t 检验、FDR/GRF、效应量、26 邻接簇表和结构化版本证据。
- 恢复脚本改用内置 .NET SHA-256，兼容无 Profile 的 Windows PowerShell 5.1/7。
- ADR 0006 取代原 0004 的延期策略；真实统计、小型预处理、组合指标和 Provider smoke 已完成，最终状态以 GitHub Release 与对应 tag 为准。
- 环境配置改为前端首次使用时由用户选择 MATLAB 可执行文件、SPM 目录和 DPABI 目录；版本标签仅作本机证据，不再硬编码为通用兼容条件。
- 补齐冻结 SkillPlan/manifest 到真实预处理的公共路径；每次执行使用独立 attempt/staging，校验源文件哈希，并从实际 NIfTI 登记元数据与谱系。
- 修正带协变量的独立组效应量：根据冻结设计矩阵的对比方差计算调整后标准化组差；保存效应定义与比例，正负效应分别提取 26 邻接簇，未校正簇明确标为描述性结果。
- 统计软件版本证据改为运行时观察值。当前安装的 7/6/6/9/12 参数签名与模板一致，撤回此前“统计参数过多”的阻断结论。
- 指标影像与掩膜统一经 DPABI `y_ReadRPI` 轴翻转规范化后严格比较物理网格，不进行重采样或放宽掩膜匹配。

### Added

- 扩展 Phase 10–17 的确定性基础：长任务阶段事件、DPABI/MATLAB 失败诊断、ROI 长宽表导出、CSV/XLSX 检查、subject-level ML 模板、cluster 坐标匹配、DPABI 整理预览和本地 rs-fMRI 证据问答。
- 新增 `diagnose-dpabi-failure`、`extract-roi-signals`、`organize-dpabi-input`、`prepare-demographics-template`、ML、cluster 和方法学问答 Skills（部分仍为契约/预览包，不在当前真实执行范围内）。

- 本地 FastAPI、SQLite Worker、React/TypeScript 前端与 MATLAB/DPABI 静态适配的首个可运行基线。
- 仓库安全策略、依赖锁定、Windows 质量门禁、GitHub Actions 和阶段关门脚本。
- 多 Provider ModelGateway、自动能力路由、结构化模型输出和数据外发去标识化策略。
- 数据检查、人口学对齐、受试者级数据集划分、QC、t 检验、FDR 和 GRF 设计文档。
- 静息态 fMRI 运行时 Skill 层架构，覆盖 SkillSpec、解析、校验、编译、审批、Workflow 调用与 provenance。
- ALFF/fALFF 与 ReHo 的类型化检查点、DPABI V8.2 字段、产物和 QC 设计基线。
- `skill_workflow_engineer` 开发协作角色。
- 非技术中文工作台已连接真实 API，覆盖数据检查、显式科研参数、计划审批、Mock 运行、人工 QC、统计设计、Provider 配置和 Agent 建议。
- 数据集划分显式记录随机种子、训练/验证/测试比例与可选分层字段；人口学导入显式记录编码和字段映射。
- 统计界面支持显式检验方向、基线、缺失策略、协变量对齐/中心化以及独立的 FDR/GRF 配置。
- Playwright Mock E2E 覆盖前端运行—QC—统计提交交互及服务端校验失败路径。
- 纯合成后端 E2E 覆盖只读 BIDS 扫描、ALFF Skill、审批、Mock Worker、测试夹具 Artifact、人工 QC、单样本 t + FDR、统计 Mock 和确定性报告；所有结果均标记为不可用于科学推断。
- 新增 `StatisticalResultManifest`、显式簇记录、真实/合成证据完整性规则，以及不依赖 Agent 的确定性 Markdown/JSON 复现报告生成器。
- 统计结果登记与只读查询闭环：`/statistics/results` 查询 API、前端报告展示；真实结果必须通过完整证据合同，合成结果继续显式标记为不可用于科学推断。
- 模型配置管理增强：新增 `DELETE /model-profiles/{profile_id}`；设置页支持多 Provider/多模型列表管理（完整字段、服务商预设、能力多选与删除），Agent 页模型下拉直接显示模型名与配置 ID。
- 模型名自动获取：新增 `POST /providers/models`，设置页在填写 base_url 与密钥环境变量名后可一键拉取该 Provider 的可用模型并从下拉选择，同时保留手动输入兜底。
- 服务商预设与前端填 Key：设置页内置 DSH（pi-ai）OpenAI 兼容服务商预设（DeepSeek、智谱、Kimi、Qwen、OpenAI、xAI、Groq、OpenRouter 等）；可直接在前端填写 API Key，拉取模型后保存时后端将 Key 写入本地 `.env`（不进入数据库、日志或审计事件），Profile 仍只记录密钥环境变量名。

### Changed

- 将 Skill 与 Workflow 纳入近期最小 fMRI 垂直闭环。
- 明确 Agent、Skill、Workflow、Tool、Plugin 与 Executor 的职责边界。
- 对齐六个 fMRI Skill 参数 schema 与运行时模型，并要求科学 Skill 具备方法学审核记录。
- 收紧 DPABI V8.2 科学适配：共享 CompCor 维数、ReHo 滤波加均值、统计尾部/自由度和 GRF 平滑度来源。
- 将 Artifact metadata 改为失败关闭：verified lineage 必须绑定执行端证据哈希、实际 TR 和实际保留 volume 数；指标按有效频率分辨率校验并拒绝 TR 不一致。
- ALFF/fALFF/ReHo 统一强制 typed 脑掩膜；预处理—指标 DAG 新增头信息验证门，CUT scrubbing 后的 ReHo 改为两阶段 verified Artifact 选择。
- MATLAB JobSpec 的输入 Artifact 强制只读，基础 Cfg 只允许显式白名单科学字段。
- 公共运行入口默认 Mock；真实 MATLAB 与真实 Provider smoke 受本机配置、环境和单独作业授权门控制。
- 环境锁现已绑定稳定参数映射所需的 ALFF/ReHo、统计检验、FDR/GRF 和统计影像 I/O
  入口内容；任一必需入口缺失时环境探测失败关闭，版本标签不作为精确匹配门槛。
- SQLite 写接口的幂等键使用带所有者和过期时间的持久租约；数据库恢复使用跨手工/脚本启动方式的运行标记与原子恢复锁，阻断活动 API/Worker 和启动—恢复竞态。
- 受控 MATLAB 执行将 stdout/stderr 持续写入按 Job 和 attempt 隔离的日志目录，保留重试历史并限制内联日志大小，避免大输出管道阻塞和内存无限增长。
- 受控 MATLAB 执行对进程树终止和日志收尾使用有界等待并失败关闭；统计 JSON 以显式对象边界传递组、路径和协变量行，避免 MATLAB `jsondecode` 折叠矩形数组。
- 受控 MATLAB 执行在启动前快照预期输出，成功后仅登记本次新建或变化的非空普通文件及其大小和 SHA-256；重试不会复用前次遗留的未变化产物。
- 中文工作台把当前项目到统计设计的恢复指针保存在浏览器本地存储，支持多项目切换、冻结计划/QC/统计内容重载和事件游标续传；待确认写请求只在标签会话中保存请求指纹与幂等 key，不保存请求体。
- 本地备份/恢复脚本仅操作仓库 `work/` 内且与 `RSFMRI_DATABASE_URL` 精确一致的 SQLite 文件；浏览器工作区不属于数据库备份。
- 本地启动、诊断和 Vite 开发代理统一读取已校验的 `RSFMRI_HOST`/`RSFMRI_PORT`，支持非默认端口与 IPv6 回环地址。
- Provider 的明文 HTTP 地址只允许解析后的精确 `localhost` 或回环 IP，拒绝主机名前缀伪装、歧义端口和带凭据 URL，避免密钥被发送到远端明文主机。
- 异步 Provider 请求在等待模型响应期间按所有者令牌续租幂等记录；租约所有权丢失时取消仍在进行的调用并拒绝保存结果，以降低长调用被并发接管和重复调用的风险。远端已接受请求时仍可能产生不可确定的执行或计费结果。
- 人口学字段映射保留规范 `subject_id`，拒绝空白名称、修剪后冲突和重复来源，避免导入内容覆盖已对齐的受试者身份。
- 数据集扫描严格区分 BIDS BOLD/T1w 与 fmap、dwi、mask、derivative；多 BOLD run、普通目录受试者 ID 清洗碰撞以及未经角色映射的 DICOM 输入都会在科学计划前失败关闭。
- DPABI-ready 扫描返回全部已核验的输入/中间 stage 候选；不再按目录数量隐式绑定，未知 `FunImg*` 后缀只进入 inventory/hash。`FunRaw`、`FunImg` 或 checkpoint 即使唯一也必须经过模型建议后的用户确认，确认阶段才进入冻结 manifest。
- 阶段关闭报告必须用 `reviewed-tree` 绑定实际候选内容；候选在审查后发生任何变化都会在提交前失败关闭，所有暂存操作也会检查退出状态。
- 普通 NIfTI 仅从明确的 func/rest/bold/functional 与 anat/t1 目录建立科学候选；结果图、mask、未知角色和多功能候选不再静默进入预处理。

### Known limitations

- 真实 MATLAB/DPABI Executor 已接入 Worker；已完成的合成 smoke 仅覆盖验证记录中的本机环境和输入范围，不替代新环境的验证。
- 预处理限单会话 4D 输入，metric-only 限单主体；需要不同阶段掩膜的 OnResults normalization 与组合指标方案暂时拒绝。
- GitHub CLI 已登录；`main` 已启用严格 `agent-review`/`quality-gate`、管理员约束、线性历史、会话解决及禁止强推/删除，仓库只允许自动 squash 合并。
- 组合指标 smoke 使用仓库外合成 4D 数据通过公共 Worker，生成并登记 120-volume 时序及 ALFF、fALFF、ReHo 图；单会话 4D、metric-only 单主体和阶段掩膜仍是已知范围边界。
- Work 方案页支持纯预处理计划：ALFF/fALFF/ReHo 和脑掩膜均可留空，仍保留指标计划的显式校验；补充单受试者 DPABI 真实冒烟验证入口。
