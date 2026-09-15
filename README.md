# Job Search Assistant

一个本地优先的个人求职助手。当前处于 **Phase 1 Job Discovery**：除 Phase 0 的模块边界、SQLite 迁移、错误模型、请求上下文、幂等与审计外，项目已具备 SearchConfig 持久化、JobsPipe Provider 适配、分页搜索运行、原始响应留存、规范化、同 Provider source 去重和不可变 job snapshot 的应用服务与 SQLite 实现。

职位查询/CLI adapter、简历解析、职位匹配、申请管理、Agent/MCP 与浏览器投递仍将在后续增量实现。真实搜索需要由调用方构造 `SearchRunService`，并配置 `JOBSPIPE_API_KEY`；测试不访问网络。

## 项目结构

```text
src/job_search_assistant/
├── app_services/       # 跨领域应用服务与启动装配
├── applications/       # Application 领域（后续阶段）
├── career/             # Career / Experience Library 领域（后续阶段）
├── core/               # 错误、请求上下文、幂等与审计抽象
├── discovery/          # Job Discovery 领域类型、Provider 与持久化 port
├── infrastructure/     # SQLite 等外部实现
└── matching/           # Job Matching 领域（后续阶段）

migrations/             # 有序、校验和保护的 SQLite 迁移
tests/                  # 标准库 unittest 测试
docs/                   # 架构与阶段设计说明
```

## 本地初始化

项目没有运行时第三方依赖。使用 Python 3.12 或更高版本执行：

```bash
PYTHONPATH=src python -m job_search_assistant init-db
PYTHONPATH=src python -m job_search_assistant db-info
```

默认数据库位置为 `.job-search-assistant/job-search-assistant.sqlite`。可传入 `--database <path>` 和 `--migrations <path>` 覆盖默认位置。

## 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 基础约束

所有未来写命令必须通过 Application Service，携带 RequestContext 和 idempotency key，在服务层写审计事件。Adapter（HTTP、CLI、MCP、Provider、LLM、Browser）不得直接修改领域表。原始数据、LLM 草稿、用户确认的职业事实、匹配结果和申请提交状态将在后续阶段保持清晰分层。
