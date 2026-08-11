# 计划 0003：扩展分析与长任务闭环

状态：实现中（本轮先交付安全的确定性预览、诊断、模板与 API 合同）

本计划把 Phase 10–17 拆成可审核的本地优先垂直切片：

| 阶段 | 本轮交付 | 后续边界 |
| --- | --- | --- |
| 10 | Worker 发出 staging/running/finished 阶段事件；RunView 预留进度、心跳、日志游标字段 | 进一步持久化 ETA、取消有界等待和分步恢复 |
| 11 | DPABI/MATLAB 日志确定性分类器与 `/runs/{id}/diagnosis` | 绑定完整 attempt 日志和人工确认后的新 PlanRevision |
| 12 | 预处理参数建议的 Skill 包与现有计划审批闭环 | DPARSF 风格分组表单和参数来源可视化 |
| 13–14 | ROI 表格合同、CSV/XLSX 导出、DPABI 整理预览与 Skill 包 | 真实 `y_ExtractROISignal`、复制执行和人口学 XLSX 模板服务 |
| 15 | CSV/TSV/XLSX 质量检查、subject 分组 ML 设计和固定 Python 模板 | 安装依赖后接入受批准的隔离 Runner 与科研图生成 |
| 16 | cluster 表解析与用户 atlas 坐标匹配 | NIfTI 网格采样、空间坐标系校验和完整报告 Artifact |
| 17 | 本地 rs-fMRI 证据检索、范围拒答和来源片段 | 脱敏后的显式联网检索与引用缓存 |

所有新 API 都是本地服务的结构化、可校验接口；不接受任意命令、自由 MATLAB/Python 文本或越界路径。真实 MATLAB、外部 Provider、联网 RAG 和源数据写入仍需要单独授权。
