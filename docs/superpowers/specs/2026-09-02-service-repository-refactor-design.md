# 服务层与仓储层分层解耦设计

## 目标

在保留当前未提交拆分成果和现有业务/API/数据库契约的前提下，完成服务层与仓储层的标准化收尾，修复应用层对基础设施层的直接依赖，并降低相关模块的复杂度与维护成本。

## 范围

- 处理当前未提交的 `NeuroAgentService`、`SqliteRepository` 拆分及其 mixin。
- 修复应用层 `service_mixins/models.py` 对 `neuroagent.infrastructure.secrets` 的依赖。
- 仅对上述直接相关的过长代码进行职责拆分、命名统一和重复逻辑收敛。
- 增加边界和行为回归测试。
- 保持公开 API、数据库 schema、运行状态语义和真实 MATLAB/DPABI 执行路径不变。

不包含：全仓库重写、数据库迁移、前端重构、真实 MATLAB/Provider smoke、外部仓库操作。

## 架构设计

### 应用层密钥写入端口

在 `application/ports.py` 增加 `SecretWriterPort`，只表达“将指定环境变量写入受控本地密钥文件”的能力。`NeuroAgentService` 通过构造函数接收该端口；默认装配在 bootstrap 或应用工厂中完成。应用层 mixin 只调用端口，不导入 `infrastructure.secrets`。

为兼容既有直接构造服务的测试和调用方，默认装配逻辑集中在应用组合根；不在 mixin 中动态导入基础设施模块，也不把基础设施对象泄露到领域层。

### 服务层

`services.py` 保持为公开门面和依赖组合点。各 mixin 按业务边界保留，但将明显过长的方法拆为局部职责明确的私有辅助函数：输入准备、版本/状态校验、持久化和响应转换分别隔离。辅助方法放入对应 mixin 或 `_base.py`，不创建新的通用框架。

### 仓储层

`repository.py` 只负责组合 `SqliteRepository` 及其 mixin。公共 SQLAlchemy session、时间/JSON/ID 转换和版本校验继续集中在 `repository_mixins/_base.py`。各 mixin 只负责一个资源边界，保持现有方法名和返回模型不变。

## 数据流与错误处理

请求进入应用服务门面，经对应 mixin 完成输入校验和端口调用；持久化统一通过 RepositoryPort；密钥写入只经过 SecretWriterPort。已有 `ApplicationError`、`ConflictError`、`InputValidationError` 和 `NotFoundError` 语义保持不变。端口实现失败不得泄露密钥或本地路径到响应、日志或事件。

## 测试策略

- 先增加应用层导入边界回归测试，确保任何 `application` 模块都不导入 `infrastructure`。
- 增加 SecretWriterPort 注入测试，验证创建模型配置时只调用端口而非直接依赖具体实现。
- 保留并运行拆分 AST 校验，确保方法无遗漏、无重复、无意外改写。
- 运行后端全量测试、前端 lint/typecheck/unit tests。
- `restore.ps1` 的 PowerShell 兼容性失败单独记录和诊断，不借重构改动掩盖。

## 完成标准

- 应用层导入边界测试通过。
- 服务/仓储拆分校验通过。
- 后端测试相较基线无新增业务回归，覆盖率门槛满足。
- 前端现有门禁继续通过。
- 相关代码保持英文标识符、类型标注和单一职责。
- 工作区不新增真实数据、密钥、运行产物或 MATLAB 长任务。

