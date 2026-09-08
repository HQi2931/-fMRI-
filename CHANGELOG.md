# Changelog

本文件记录项目中对用户和开发者可见的重要变化。

## Unreleased

- 新增 rs-fMRI Chat Mode Phase 1：独立 `ChatAgent`、窄接口、PDF 文献管理、科研 section 识别、section-aware chunking 和逐页 traceability；按后续要求直接复用 fMRIAnalysis 的 DashScope/Chroma RAG，修复融合去重并减少重复重排。新上传 chunks 尚未自动向量化，旧库缺失页码保持未知。
- 新增 MVP1.0 目标架构、能力化模块单体 ADR 和 10–12 周开发路线图，明确以显式应用服务、窄端口、版本化 JobEnvelope、服务端状态真源和前端 feature 分片渐进演进，不拆微服务。
- 新增 Agent 对话工作台：可选择本机 fMRI 工作区，调用只读检查并在右侧展示 DPABI 输入阶段、受试者配对、NIfTI 头标记问题和已有结果目录。
- Agent 工作台新增 `Chat`/`Work` 分类：Chat 调用本地 rs-fMRI RAG 并展示证据，Work 承载工作区检查、预处理和结果分析入口。
- Chat 现可把本地 RAG 证据交给用户选择的 LLM 生成回答；显式开启联网搜索时，仅路由到声明 `web_search` 能力的模型，并保存模型、脱敏上下文哈希和 URL 引用。
- 设置页改为服务商 API 绑定：绑定后立即读取 `/models`，Agent 对话框直接列出该服务商返回的全部模型，不再要求用户逐个创建“已配置模型”。
- 新增 `POST /api/v1/workspaces/check` 工作区检查接口；检查不会注册项目、修改原始数据或启动 MATLAB。
- Chat/Work 多轮消息、会话绑定和每次工具调用现已持久化到 SQLite，可在刷新页面后恢复。
- Work 对话已编排工作区检查、运行进度和 DPABI 启动；真实启动继续要求已审批计划和逐次确认，并进入现有 Workflow/Worker。
- 工作区目录只能通过系统文件夹选择器选取；新增显式 `in_place` DPABI 模式，在复核冻结输入哈希后把所选 DPABI-ready 工作副本作为 `WorkingDir`，结果 stage 写回该工作区，运行脚本、日志和登记产物保持 attempt 隔离。

### v0.1.0 release work

- 新增 `ExecutionBackend` 控制面：Mock 默认；MATLAB 必须逐次确认、配置开关和环境探测同时通过。
- 新增批准计划专用 `WorkflowFactory`/`ToolRuntime`：Mock 逐节点执行 DAG；MATLAB 适配器校验冻结计划并编译受控分段作业，不逐节点经过 `ToolRuntime`。
- Worker 已注册 `matlab_preprocessing`/`matlab_statistics` 路由；统计模板覆盖三类 t 检验、FDR/GRF、效应量、26 邻接簇表和结构化版本证据。
- 恢复脚本改用内置 .NET SHA-256，兼容无 Profile 的 Windows PowerShell 5.1/7。
- ADR 0006 取代原 0004 的延期策略；真实统计、小型预处理、组合指标和 Provider smoke 已完成，最终发布状态以 GitHub Release 与对应 tag 为准。
- 环境配置改为前端首次使用时由用户选择 MATLAB 可执行文件、SPM 目录和 DPABI 目录；版本标签仅作本机证据，不再硬编码为通用兼容条件。
- 补齐冻结 SkillPlan/manifest 到真实预处理的公共路径；每次执行使用独立 attempt/staging，校验源文件哈希，并从实际 NIfTI 登记元数据与谱系。
- 修正带协变量的独立组效应量：根据冻结设计矩阵的对比方差计算调整后标准化组差；保存效应定义与比例，正负效应分别提取 26 邻接簇，未校正簇明确标为描述性结果。
- 统计软件版本证据改为运行时观察值。当前安装的 7/6/6/9/12 参数签名与模板一致，撤回此前“统计参数过多”的阻断结论。
- 指标影像与掩膜统一经 DPABI `y_ReadRPI` 轴翻转规范化后严格比较物理网格，不进行重采样或放宽掩膜匹配。

### Added

- 扩展 Phase 10–17 的确定性基础：长任务阶段事件、DPABI/MATLAB 失败诊断、ROI 长宽表导出、CSV/XLSX 检查、subject-level ML 模板、cluster 坐标匹配、DPABI 整理预览和本地 rs-fMRI 证据问答。
- 新增 `diagnose-dpabi-failure`、`extract-roi-signals`、`organize-dpabi-input`、`prepare-demographics-template`、ML、cluster 和方法学问答 Skills（部分仍为契约/预览包，不属于 v0.1 真实执行范围）。

- 本地 FastAPI、SQLite Worker、React/TypeScript 前端与 MATLAB/DPABI 静态适配的 MVP 候选基线。
- 仓库安全策略、依赖锁定、Windows 质量门禁、GitHub Actions 和阶段自动发布脚本。
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
- DPABI-ready 扫描只绑定唯一的 `FunRaw` 或 `FunImg` 输入 stage；其他 `FunImg*` checkpoint 与 `Results` 仅进入 inventory/hash，多输入 stage、checkpoint-only 或不支持的 stage 均失败关闭。
- 阶段关闭报告必须用 `reviewed-tree` 绑定实际候选内容；候选在审查后发生任何变化都会在提交前失败关闭，所有暂存操作也会检查退出状态。
- 普通 NIfTI 仅从明确的 func/rest/bold/functional 与 anat/t1 目录建立科学候选；结果图、mask、未知角色和多功能候选不再静默进入预处理。

### Known limitations

- 真实 MATLAB/DPABI Executor 已接入 Worker；已完成的合成 smoke 仅覆盖验证记录中的本机环境和输入范围，不替代新环境的验证。
- 预处理限单会话 4D 输入，metric-only 限单主体；需要不同阶段掩膜的 OnResults normalization 与组合指标方案暂时拒绝。
- GitHub CLI 已登录；`main` 已启用严格 `agent-review`/`quality-gate`、管理员约束、线性历史、会话解决及禁止强推/删除，仓库只允许自动 squash 合并。
- 组合指标 smoke 使用仓库外合成 4D 数据通过公共 Worker，生成并登记 120-volume 时序及 ALFF、fALFF、ReHo 图；单会话 4D、metric-only 单主体和阶段掩膜仍是已知范围边界。
