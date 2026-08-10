# Infrastructure 层

`neuroagent.infrastructure` 实现应用端口的本地适配器，当前包括：

- SQLAlchemy 2 + SQLite 持久化与 Alembic 迁移；
- 数据集只读扫描、文件哈希和允许根目录路径策略；
- CSV、TSV、XLSX 人口学读取与受试者对齐；
- MATLAB、SPM12、DPABI 环境探测；环境锁会失败关闭地校验并哈希
  `DPARSFA_run`、ALFF/ReHo、统计检验、FDR/GRF 及统计影像 I/O 入口；
- 确定性 Mock Executor。

基础设施层不决定科学参数或审批结论。源数据只读，派生产物只能写入允许的工作根目录。向量库、对象存储、Redis、消息总线和云服务不在当前单机 MVP 范围内。
