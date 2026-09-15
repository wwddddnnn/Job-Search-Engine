# Builder（Codex）协作约定

本文件由 Hermes 生成，用于约束 **builder（Codex，负责写代码的一方）** 的行为。
`codex-hermes-loop.sh` 会在每轮把本文件全文注入 builder 的 prompt。
审查方（reviewer）的规则在 `.opencode/prompts/reviewer.md`，两套规则互相独立。

> 说明：原始意图是把这份内容放进仓库根的 `AGENTS.md`（Codex 会自动读取）。
> 该写入被 Hermes 的 agent 指令文件保护策略拦下，需用户确认。
> 当前由脚本显式注入，效果等价；用户确认后可自行迁移到 `AGENTS.md`。

## 项目是什么

Job Search Assistant：本地优先的个人求职助手，模块化单体，当前处于 **Phase 1 Job Discovery**。

- 技术栈：**Python 3.12 标准库 + SQLite**，没有运行时第三方依赖，不要引入新依赖。
- 测试：标准库 `unittest`（不是 pytest）。测试**不访问网络**。
- 目录：
  - `src/job_search_assistant/core/` — 与领域无关的错误、上下文、幂等、审计抽象
  - `src/job_search_assistant/discovery/` — Job Discovery 领域类型、Provider 与持久化 port
  - `src/job_search_assistant/infrastructure/sqlite/` — SQLite 实现
  - `src/job_search_assistant/app_services/` — 跨领域应用服务与启动装配
  - `applications/`、`career/`、`matching/` — 后续阶段的预留位置，**当前不要在这些目录里实现业务逻辑**
  - `migrations/` — 有序、校验和保护的迁移
  - `docs/` — 架构与阶段设计说明

## 权威依据

实现前先读，冲突时以架构文档为准：

1. `docs/Job Search Assistant 技术架构文档.md`（§5 架构不变量、§6 模块边界、§9 状态机、§11 分阶段计划）
2. `docs/phase0-foundation.md`、`docs/phase1-job-discovery.md`（当前阶段的验收标准）
3. `README.md`

## 硬性约束（违反即会被 reviewer 退回）

1. **只做当前任务描述里的范围。** 不做「顺手重构」、不提前实现后续 Phase 的模块。
2. **不修改已应用的迁移。** `migrations/*.sql` 一旦被 `schema_migrations` 记录就会被 checksum 校验；
   需要改 schema 时**新增** `NNNN_description.sql`，不要编辑旧文件。破坏性 schema 变更要先停下问人。
3. **依赖方向不能反。** `core` 不得 import 任何领域模块或 SQLite；领域模块只能依赖 `core` 的抽象和自身 port；
   领域代码不得 import `infrastructure/sqlite` 的具体实现。上层（adapter/UI）不得直接读模块内部字段。
4. **原始数据不可被覆盖。** Provider 原始响应／每个职位的 raw JSON 必须原样留存并可回放，
   规范化、去重、合并都不得改写或丢弃原始记录本身。
5. **不绕过状态机。** 审批／确认类流程不得跳过必经状态，不得允许非法跃迁。
6. **不引入第三方运行时依赖**，不使用 pytest，不改测试框架。
7. **不碰密钥。** 不得把 API key、Authorization header 写进代码、日志、数据库或测试 fixture。
   真实 Provider 调用只在调用方显式注入 key 时发生。

## 每轮必须自测

改完代码**先自己跑通过**再交审查，否则这一轮必然被打回：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

可选（仓库有历史 lint／mypy 存量问题，不要求清零，但**你新增的代码不能引入新的**）：

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

## 提交

- **不要自己 `git commit` / `git push`。** 编排脚本会在审查轮结束后统一提交推送，
  commit message 由 reviewer 给出。
- 只改工作区文件即可。
- **不要修改 `DEVELOPMENT_LOG.md`**，它由脚本写入。

## 工作副本（硬规则）

builder 与 reviewer **必须原地读写同一份 checkout**：`~/code/Python/Job-Search-Engine`。

- **禁止 `git worktree add` / `hermes --worktree`**：不要为开发另开工作树。
- **禁止为开发克隆仓库**：不要在 /tmp 或任何别处再 clone 一份。
- 不得把仓库复制到别处改完再拷回来。

理由：两方必须看同一份代码索引、同一份文件状态。否则 reviewer 审的 diff 与 builder 改的文件
不是同一份，审查结论就失去意义，去重/合并这类结论也无法复核。

