# Phase 0 Foundation 设计说明

## 目标

Phase 0 提供后续四个领域模块共享的最小基础设施，不实现 JobsPipe、简历解析、匹配算法、Agent/MCP 或浏览器自动化。所有实现仅使用 Python 3.12 标准库与 SQLite，测试使用标准库 `unittest`，避免在空项目中引入不必要依赖。

## 源码边界

```text
src/job_search_assistant/
├── app_services/              # 跨模块用例服务的预留位置
├── applications/              # Application 业务域的预留位置
├── career/                    # Career / Experience Library 业务域的预留位置
├── discovery/                 # Job Discovery 业务域的预留位置
├── matching/                  # Job Matching 业务域的预留位置
├── core/                      # 与领域无关的错误、上下文、ID、幂等、审计抽象
└── infrastructure/sqlite/     # SQLite 连接、迁移执行和 repository 基础设施
```

`core` 不得依赖任何领域模块或 SQLite 实现。领域模块未来依赖 `core` 中的抽象和类型；`infrastructure` 可依赖 core 与领域 ports，但领域不得导入 SQLite 或具体 I/O 实现。`app_services` 负责事务、应用用例和跨域只读协调，不存放 HTTP/MCP controller。

## Phase 0 提供的能力

| 能力 | 实现方式 | 后续使用者 |
|---|---|---|
| 数据库迁移 | 文件命名为 `NNNN_description.sql`；迁移历史记录于 `schema_migrations`；一个事务执行每个迁移。 | 所有模块。 |
| SQLite 数据库 | `SQLiteDatabase` 负责连接、外键、WAL、事务与迁移。 | Repositories 与应用服务。 |
| 统一错误模型 | `DomainError` 及 validation/not-found/conflict/authorization/invalid-state/infrastructure 子类；包含稳定 code、message、details、correlation ID。 | UI、HTTP/MCP adapter、测试。 |
| 请求上下文 | `RequestContext` 通过 context variable 保存 correlation ID、actor、source、timestamp。 | 应用服务、审计与日志。 |
| 幂等执行 | `IdempotencyService` 基于 scope/key/request hash 保存 `in_progress` 或 `completed` 结果；冲突键不能用于不同 payload。 | SearchRun、MatchRun、Application commands。 |
| 审计 | 不可变 `audit_events` 表与 `AuditService`；记录 actor、动作、目标、correlation ID、before/after/metadata JSON。 | 用户确认、数据修订、状态转移和高风险提交。 |

## 初始迁移

迁移 0001 创建以下基础表，不创建任何四模块业务表：

| 表 | 用途 | 关键约束 |
|---|---|---|
| `schema_migrations` | 记录已成功执行的迁移和内容校验。 | `version` 主键，`checksum` 防止已应用迁移被修改。 |
| `idempotency_records` | 防止写命令和外部副作用被重复执行。 | `(scope, idempotency_key)` 唯一，request hash 冲突失败。 |
| `audit_events` | 记录不可变用户/系统操作。 | 每条含 correlation ID，按 target 与时间索引。 |

## 应用服务使用规范

所有未来写用例遵循顺序：创建或接收 RequestContext → 校验输入/权限 → 以幂等 scope/key 取得或创建执行记录 → 在一个数据库事务中改变业务状态并写 audit event → 标记幂等记录完成 → 返回序列化安全结果。外部网络调用不应在数据库事务中长时间保持锁；其结果应以 run ID 和状态机记录。

缺少 idempotency key 的情况只允许真正只读查询。面向 UI 或 Agent/MCP 的写命令必须接受并审计 `actor` 和 `correlation_id`；高风险操作将在 Application 模块增加更强的 approval token，但 Phase 0 已提供记录和关联基础。

## Phase 0 验收标准

在新机器上，以给定数据库路径调用 `migrate()` 后，三张基础表存在，且迁移再次运行无副作用。修改已执行迁移的内容后必须报错。相同 scope/key/payload 应返回先前完成结果或阻止并发重复执行；相同 scope/key 但不同 payload 必须报冲突。每项审计记录应携带明确动作、目标、actor、source 和 correlation ID。所有基础测试通过，且无需外部 API、LLM、MCP 或浏览器。
