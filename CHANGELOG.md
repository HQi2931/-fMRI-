# Changelog

本文件记录项目中对用户和开发者可见的重要变化。

## Unreleased

### Added

- 扩展 Phase 10–17 的确定性基础：长任务阶段事件、DPABI/MATLAB 失败诊断、ROI 长宽表导出、CSV/XLSX 检查、subject-level ML 模板、cluster 坐标匹配、DPABI 整理预览和本地 rs-fMRI 证据问答。
- 新增 `diagnose-dpabi-failure`、`extract-roi-signals`、`organize-dpabi-input`、`prepare-demographics-template`、ML、cluster 和方法学问答 Skills。

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
- 统计结果登记与只读查询闭环：`/statistics/results` 查询 API、前端报告展示；合成演示结束时登记明确标记的合成复现报告。

### Changed

- 将 Skill 与 Workflow 纳入近期最小 fMRI 垂直闭环。
- 明确 Agent、Skill、Workflow、Tool、Plugin 与 Executor 的职责边界。
- 对齐六个 fMRI Skill 参数 schema 与运行时模型，并要求科学 Skill 具备方法学审核记录。
- 收紧 DPABI V8.2 科学适配：共享 CompCor 维数、ReHo 滤波加均值、统计尾部/自由度和 GRF 平滑度来源。
- 将 Artifact metadata 改为失败关闭：verified lineage 必须绑定执行端证据哈希、实际 TR 和实际保留 volume 数；指标按有效频率分辨率校验并拒绝 TR 不一致。
- ALFF/fALFF/ReHo 统一强制 typed 脑掩膜；预处理—指标 DAG 新增头信息验证门，CUT scrubbing 后的 ReHo 改为两阶段 verified Artifact 选择。
- MATLAB JobSpec 的输入 Artifact 强制只读，基础 Cfg 只允许显式白名单科学字段。
- 公共运行入口保持 Mock-only；真实 MATLAB 与真实 Provider smoke 被明确列为需要本机凭据或单独作业授权的验证项。
- 环境锁现已绑定 DPABI V8.2 的 ALFF/ReHo、统计检验、FDR/GRF 和统计影像 I/O
  入口内容；任一必需入口缺失时环境探测失败关闭。
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

- 公共 Worker 尚未接入真实 MATLAB/DPABI Executor。
- 统计运行当前只排队通用 Mock 任务；真实效应量、显著簇表、实际结果发现/登记和结果查询未接线。复现报告合同已实现，但真实证据不完整时会失败关闭。
- `v0.1.0` 未发布，首次推送、GitHub Actions、真实 Provider smoke 和真实 MATLAB smoke 仍未完成。
