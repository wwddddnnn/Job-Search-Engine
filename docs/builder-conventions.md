# Builder（Codex）协作约定

本文件由 Hermes 生成，用于约束 **builder（Codex，负责写代码的一方）** 的行为。
`codex-hermes-loop.sh` 会在每轮把本文件全文注入 builder 的 prompt。
审查方（reviewer）的规则在 `.opencode/prompts/reviewer.md`，两套规则互相独立。

> 说明：原始意图是把这份内容放进仓库根的 `AGENTS.md`（Codex 会自动读取）。
> 该写入被 Hermes 的 agent 指令文件保护策略拦下，需用户确认。
> 当前由脚本显式注入，效果等价；用户确认后可自行迁移到 `AGENTS.md`。

## 项目是什么

Job Search Assistant：本地优先的个人求职助手，模块化单体，当前处于 **Phase 2 Career Foundation**（S1–S3 已落地：迁移 0004 + career 领域层 + 简历导入 + 抽取草稿）。

- 技术栈：**Python 3.12 标准库 + SQLite**，没有运行时第三方依赖，不要引入新依赖。
- 测试：标准库 `unittest`（不是 pytest）。测试**不访问网络**。
- 目录：
  - `src/job_search_assistant/core/` — 与领域无关的错误、上下文、幂等、审计抽象
  - `src/job_search_assistant/discovery/` — Job Discovery 领域类型、Provider 与持久化 port
  - `src/job_search_assistant/career/` — **Phase 2 正在实现的领域层**（types / ports / store / extraction）
  - `src/job_search_assistant/infrastructure/{sqlite,files}/` — SQLite 与受控文件存储实现
  - `src/job_search_assistant/app_services/` — 跨领域应用服务与启动装配
  - `src/job_search_assistant/adapters/web/` — **Phase 2.5 的本地 HTTP 入口与静态页面**（页面资源放其下 `static/`）
  - `src/job_search_assistant/{applications,matching}/` — 后续阶段的预留位置，**当前不要在这些目录里实现业务逻辑**
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
   **HTTP/UI adapter 不得直连数据库、也不得调用 store 内部写接口**，只能经 `app_services` 的应用服务与已定义契约；
   页面资源只放 `adapters/web/static/`，**不引前端框架、不连 CDN、不要求 Node.js 或前端构建**，DOM 操作只出现在 view 模块。
4. **原始数据不可被覆盖。** Provider 原始响应／每个职位的 raw JSON 必须原样留存并可回放，
   规范化、去重、合并都不得改写或丢弃原始记录本身。
5. **不绕过状态机。** 审批／确认类流程不得跳过必经状态，不得允许非法跃迁。
6. **不引入第三方运行时依赖**，不使用 pytest，不改测试框架。
7. **不碰密钥。** 不得把 API key、Authorization header 写进代码、日志、数据库或测试 fixture。
   真实 Provider 调用只在调用方显式注入 key 时发生。

## 效率约定（直接决定额度消耗，必须遵守）

每次工具调用都会重发全部上下文，所以**调用次数是最贵的成本**。实测一轮 S3 消耗 260–470 万 input tokens，其中相当一部分是逐文件 `cat`/`sed` 造成的。

1. **合并读取**：要看多个文件就用**一条**命令读完（`sed -n '1,400p' a.py b.py c.py`，或 `for f in ...; do ...; done`），不要一个文件一条命令。
2. **先扫后读**：先用一次 `grep -rn` / `rg --files` 定位，再只读相关片段，不要先通读整棵源码树。
3. **合并验证**：自测、`git diff --check`、行宽检查放在**同一条**命令里跑，不要拆成三次。
4. **不重复确认**：已经确认过的事实，不要换个命令再确认一遍。

## 每轮必须自测

改完代码**先自己跑通过**再交审查，否则这一轮必然被打回：

```bash
PYTHONPATH=src /opt/homebrew/Caskroom/miniconda/base/envs/Job-Search-Engine/bin/python -m unittest discover -s tests
```

- **这是本项目的唯一解释器**（记录在仓库根 `dev.env` 的 `PYBIN`），编排脚本每轮会在 prompt 顶部再声明一次。
- **不要用 `.venv/`、Hermes 自带 venv 或 conda base 的 python** ——仓库里**没有** `.venv`，用了只会白跑几次命令。
- **不要自建环境**：不建 venv、不建新 conda env、不 `pip install`。
- 本仓库**未安装** `ruff`/`mypy`：不要去找、也不要安装。静态自检用行宽（≤100 列）+ `compileall` 代替。

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

