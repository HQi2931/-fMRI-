# Infrastructure 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-10-10
- 关联：[项目结构](../../docs/architecture/project-structure.md)

`neuroagent.infrastructure` 实现应用端口的本地适配器，当前包括：

- SQLAlchemy 2 + SQLite 持久化与 Alembic 迁移；
- 会话保存前台角色与两个角色各自的方案/运行引用；迁移将旧 Work 运行归属预处理。统计运行及绑定共用 writer 事务；客户端缓存不承担任务持久化。
- 数据集只读扫描、文件哈希和允许根目录路径策略；
- 快速概况附带基于相对路径、文件大小、时间戳及顶层目录的 `metadata_revision`，用于发现规划输入变化；它不进入冻结清单，也不能替代执行前完整内容哈希。保留或伪造时间戳的内容变化仍由冻结与 Worker 校验识别。
- 工作区 JSON 读取可附上请求范围内的 TR/层时间数值证据，独立于敏感行省略后的文本片段；仅允许有限、有效数值，不恢复身份正文。
- 工作区文本补丁的新文件路径可缺少最后的文件节点，仍拒绝符号链接、目录越界、默认排除目录及影像写入；应用前验证旧文件哈希，部分替换失败恢复原文件并移除新增文件。
- `dataset_inspector.py` 按已核验 DPABI 源码的版本化阶段表返回 `FunRaw`/`FunImg` 及中间 checkpoint 候选；不自动选择唯一目录，确认后的阶段和全量伴随文件哈希进入冻结 manifest；
- CSV、TSV、XLSX 人口学读取与受试者对齐；
- 外部指标的人口学修订独立绑定纳入哈希，保留映射后的表格快照；不创建预处理数据集，最新修订在执行编译前复查。
- 用户选择的 MATLAB、SPM、DPABI 环境探测；环境锁会失败关闭地校验并哈希
  `DPARSFA_run`、ALFF/ReHo、统计检验、FDR/GRF 及统计影像 I/O 入口；
- 确定性 Mock Executor 与公共 Worker 使用的受控 MATLAB 适配器。
- 每次执行独立的 attempt/staging 目录、只读输入哈希校验、运行后从实际 NIfTI 头信息登记 metadata 和 lineage；失败或不完整输出不能注册为科研成功。
- DPABI 的逐受试者有效指标掩膜保留为 `preprocessing.subject_brain_mask`。实际文件内容、空间和网格全部一致时，复制并登记唯一的组级 `preprocessing.brain_mask`，指标引用该共同内容；交接同时冻结和复核各原始掩膜证据。不同有效掩膜不自动合并或重采样。
- `matlab_executor.py` 的 `in_place` 续跑只接受显式确认的阶段，按阶段关闭已完成 DPABI 步骤，并在 Worker 启动前重验输入、`RealignParameter` 等伴随证据。

版本标签不作为固定版本兼容条件；实际运行会保存观察到的软件版本。2026-09-04 的小型合成统计 smoke 在 MATLAB R2024b、SPM25 25.01.02、DPABI V9.0_250415 上通过，并不构成跨版本、跨输入形态的通用兼容承诺。详细范围见 [验证记录](../../docs/development/mvp-verification.md)。

基础设施层不决定科学参数或审批结论。源数据只读，派生产物只能写入允许的工作根目录。Chat 文献检索可选复用 Chroma；上传论文在工作根目录建立独立 collection。对象存储、Redis 和消息总线不在当前单机 MVP 范围内。
