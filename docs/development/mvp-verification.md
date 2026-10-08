# MVP 验证与已知限制

- 状态：Implementing
- 文档类型：Development
- 维护者：qa_reviewer
- 更新日期：2026-10-07
- 关联：[MVP 产品范围](../product/mvp-scope.md)

## Work 体验增量验收（2026-10-07）

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
