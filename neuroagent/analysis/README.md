# 分析辅助模块（analysis）

- 状态：Implementing
- 文档类型：Architecture
- 维护者：fmri_methodologist
- 更新日期：2026-10-10
- 关联：[项目结构](../../docs/architecture/project-structure.md)

`neuroagent.analysis` 是纯领域辅助模块：提供确定性的分析预览、诊断、模板与合同，**不启动进程、不修改原始数据、不读取密钥、不联网**。

服务层（`neuroagent.application`）负责把项目根目录转换为允许的只读路径，再调用本模块；输出模型包含内容哈希、证据或 lineage 所需字段。

## 模块

| 模块 | 职责 |
| --- | --- |
| `models.py` | 稳定的分析合同（Pydantic frozen 模型），不含绝对路径、可执行文本或可变状态 |
| `diagnostics.py` | 确定性 DPABI/MATLAB 日志分类器（含 Windows MATLAB `CreateFile` 权限错误规则、未知错误标记识别，以及路径/ID/邮箱脱敏；普通输出不作为错误证据） |
| `clusters.py` | DPABI cluster 表解析与 atlas 坐标最近邻匹配（几何启发式，校验坐标空间一致） |
| `dpabi_atlas.py` | 只读检查配置 DPABI 的 AAL/AAL3/Brodmann 图谱，以 NIfTI 仿射采样峰值并计算 26 邻域簇覆盖比例 |
| `cluster_roi.py` | 在复核峰值及正负簇的 26 邻域范围后，逐图读取同网格指标均值；仅用于描述性展示 |
| `external_metric_import.py` | 只读检查分组或显式配对条件指标目录及 mask；受试者/组别/条件是带来源的扫描候选，subject/session/run 不靠目录顺序连接，session/run 保持 unknown |
| `roi.py` | ROI 信号合同的 Nyquist/频带校验与长宽表导出 |
| `tables.py` | CSV/TSV/XLSX 只读检查（不修改上传文件，损坏文件失败关闭） |
| `templates.py` | 生成可审查的 Python ML 模板（不执行、不注入命令/绝对路径） |
| `organization.py` | 只读的 DPABI 目录整理预览（不复制文件，拒绝目录穿越） |
| `rag.py` | 本地 rs-fMRI 证据检索与范围拒答（不联网、不调模型） |

## 边界

- 本模块当前为**本地确定性分析辅助**：不启动 MATLAB、不训练模型、不联网检索。
- 结果定位只读取已登记统计图和受信的 DPABI 模板图谱；应用服务先验证来源 Artifact 边界与完整性哈希。合成结果、空间未知结果、簇峰值或连通范围不匹配时失败关闭。
- 真实 4D `y_ExtractROISignal`、隔离 ML Runner 和脱敏联网 RAG 均待单独接线与授权。外部分组/配对目录检查、纳入及 Work 来源选择已接入统计执行；显著簇内体积指标均值由确定性程序提取。
- 外部指标检查独立哈希原始文件树与 subject/group/condition 候选映射；用户纳入前需显式复核映射，纳入记录标为 `declared`，不能升为 `verified`。缺失的 session/run 身份维持 `unknown`；映射来源解释变化会要求重新检查。
- AAL/AAL3/Brodmann 的定位结果是描述性空间覆盖，不能当成簇内二次统计检验。
- 图谱头和标签解析使用有界内存缓存，键包含实际内容 SHA-256。大小和时间戳相同的内容变化也会重新解析；正式方案及报告的冻结版本由应用层验证，缓存不替代完整性检查。
