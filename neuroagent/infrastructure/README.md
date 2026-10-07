# Infrastructure 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-27
- 关联：[项目结构](../../docs/architecture/project-structure.md)

`neuroagent.infrastructure` 实现应用端口的本地适配器，当前包括：

- SQLAlchemy 2 + SQLite 持久化与 Alembic 迁移；
- 数据集只读扫描、文件哈希和允许根目录路径策略；
- 工作区 JSON 读取可附上请求范围内的 TR/层时间数值证据，独立于敏感行省略后的文本片段；仅允许有限、有效数值，不恢复身份正文。
- `dataset_inspector.py` 按已核验 DPABI 源码的版本化阶段表返回 `FunRaw`/`FunImg` 及中间 checkpoint 候选；不自动选择唯一目录，确认后的阶段和全量伴随文件哈希进入冻结 manifest；
- CSV、TSV、XLSX 人口学读取与受试者对齐；
- 用户选择的 MATLAB、SPM、DPABI 环境探测；环境锁会失败关闭地校验并哈希
  `DPARSFA_run`、ALFF/ReHo、统计检验、FDR/GRF 及统计影像 I/O 入口；
- 确定性 Mock Executor 与公共 Worker 使用的受控 MATLAB 适配器。
- 每次执行独立的 attempt/staging 目录、只读输入哈希校验、运行后从实际 NIfTI 头信息登记 metadata 和 lineage；失败或不完整输出不能注册为科研成功。
- `matlab_executor.py` 的 `in_place` 续跑只接受显式确认的阶段，按阶段关闭已完成 DPABI 步骤，并在 Worker 启动前重验输入、`RealignParameter` 等伴随证据。

版本标签不作为固定版本兼容条件；实际运行会保存观察到的软件版本。2026-09-04 的小型合成统计 smoke 在 MATLAB R2024b、SPM25 25.01.02、DPABI V9.0_250415 上通过，并不构成跨版本、跨输入形态的通用兼容承诺。详细范围见 [验证记录](../../docs/development/mvp-verification.md)。

基础设施层不决定科学参数或审批结论。源数据只读，派生产物只能写入允许的工作根目录。Chat 文献检索可选复用 Chroma；上传论文在工作根目录建立独立 collection。对象存储、Redis 和消息总线不在当前单机 MVP 范围内。
