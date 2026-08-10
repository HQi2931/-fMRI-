# 项目结构

本仓库采用模块化单体：FastAPI、独立 SQLite Worker、React 前端和受控 MATLAB 适配器共享一套领域契约，但依赖方向保持单向。当前主要目录如下。

```text
rs_fMRI_Agent/
├─ .agents/                    开发协作 Agent 的 Markdown 角色说明
├─ .github/workflows/          Windows CI、安全审计与质量汇总门禁
├─ config/                     可提交的非敏感配置与仓库文件白名单
├─ docs/
│  ├─ adr/                     重要架构决策记录
│  ├─ api/                     REST / SSE 公共协议说明
│  ├─ architecture/            系统、Skill 层和目录结构设计
│  ├─ development/             安装、验证、审查与发布流程
│  ├─ domain/                  数据集、QC、统计等科研契约
│  ├─ operations/              本地运行、诊断、备份恢复与外部 Provider 手册
│  ├─ plans/                   分阶段开发计划和验收标准
│  ├─ product/                 产品范围、发布条件和非目标
│  ├─ reviews/                 实际阶段终审记录；未审查前不得创建通过报告
│  └─ security/                数据边界与模型外发策略
├─ matlab/templates/           固定 MATLAB 模板；不接收任意脚本文本
├─ neuroagent/
│  ├─ agent/                   模型路由、Provider、脱敏和结构化输出
│  ├─ api/                     FastAPI 路由、错误映射、静态前端入口
│  ├─ application/             用例编排、公共契约、端口、环境锁和确定性报告
│  ├─ domain/fmri/             纯 fMRI 领域模型、结果合同与科学校验规则
│  ├─ execution/               Mock 与受控 MATLAB 执行适配
│  ├─ infrastructure/          SQLite、文件系统检查和环境探测
│  ├─ observability/           审计事件与追踪上下文
│  ├─ skills/                  Skill Registry、Resolver、Validator、Compiler
│  ├─ tools/                   类型化 DPABI、staging 与 Tool Registry
│  ├─ workflow/                状态机、队列 Worker 与任务领取
│  └─ bootstrap.py             API/Worker 共享的依赖装配组合根
├─ scripts/                    初始化、质量门禁、诊断、备份和阶段发布
├─ skills/                     六个可审查 Skill 包及机器可读 schema
├─ tests/
│  ├─ agent/                   路由、脱敏与结构输出测试
│  ├─ backend/                 API、持久化、任务和数据管理测试
│  ├─ integration/             真实 SQLite/Service/Mock Worker 的纯合成闭环
│  └─ science/                 DPABI 映射、顺序、统计和执行边界测试
├─ web/
│  ├─ e2e/                     Playwright Mock 端到端场景
│  ├─ src/                     React 非技术中文工作台
│  └─ openapi.json             后端导出的版本化 API 契约
├─ AGENTS.md                   Codex 项目级开发规则
├─ pyproject.toml / uv.lock    Python 配置与锁定依赖
└─ .env.example               本地配置模板；真实路径和密钥只进 .env
```

## 依赖方向

```text
API / Worker
    → bootstrap composition root
        → Application
        → Domain + Ports + Skill contracts
Infrastructure / Execution
    → implements Ports
```

领域层不能导入 FastAPI、SQLAlchemy 或进程执行代码。`neuroagent/bootstrap.py` 只负责装配应用服务、Repository、环境锁和执行端口，不承载领域规则；API 与 Worker 从该组合根取得共享依赖。API 不能直接启动 MATLAB；Worker 只能领取已批准且仍有效的结构化任务。`skills/` 是面向人和机器的能力包，`neuroagent/skills/` 是读取、校验和编译这些能力包的运行时代码。

## 不进入仓库的内容

真实 DICOM/NIfTI/MAT、人口学表、SQLite 运行库、日志、工作目录、产物和本地 `.env` 均由 `.gitignore` 与仓库安全门禁阻止。合成测试数据应优先在测试中即时生成。
