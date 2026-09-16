# Job Search Assistant

一个本地优先的个人求职助手，模块化单体：**Python 3.12 标准库 + SQLite，无运行时第三方依赖**。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **Phase 0 基础层** | 模块边界、校验和保护的 SQLite 迁移、错误模型、RequestContext、幂等、审计 | ✅ |
| **Phase 1 Job Discovery** | SearchConfig 持久化、JobsPipe Provider 适配、分页搜索运行、原始响应留存、规范化、同 source 去重、不可变 job snapshot | ✅ |
| **Phase 2 Career Foundation** | 迁移 0004（10 张表）、`career/` 领域层、受控文件存储、纯文本提取器、导入/抽取/确认/证据包/快照五个应用服务 | ✅ S1–S5 |
| Phase 2.5 核验 UI | 用户确认与编辑职业事实的界面 | ⏳ 待开始 |
| B 步 真实 LLM | 接入真实 provider（当前只有确定性假 provider） | ⏳ 待开始 |
| Phase 3 及以后 | Job Matching、申请管理、Agent/MCP、浏览器投递 | ⏳ 未开始 |

Phase 2 已完成的部分：

- **迁移 0004**：简历文档、提取文本、LLM 抽取运行、职业档案与版本、经历 / 成果 / 技能 / 证据。
- **受控文件存储**：按内容哈希寻址（`<root>/<sha256>/{original.bin, extracted-text.json, extraction-draft.json}`），原文件与提取文本长期留存、可回放，不覆盖历史。
- **`PlainTextResumeExtractor`**：纯标准库的 `.txt` / `.md` 提取器；不支持的格式抛结构化错误，不静默成功。
- **应用服务**：`ImportResumeDocument`、`StartExtractionRun`、`ConfirmExperienceFacts`、`GetVerifiedEvidencePack`、`GetCareerProfileSnapshot`。
- **确定性假 provider**：`DeterministicCareerExtractionProvider` 供测试与演练使用；未配置 provider 时报明确错误，不静默降级。

Career 目前**只有应用服务层，没有 CLI / adapter / UI**，调用方需自行装配（见下文）。

## 核心不变量

1. **LLM 输出一律是草稿**；未经用户显式确认的事实不会进入已发布版本，也不会出现在 `VerifiedEvidencePack` 里。
2. 只有**显式确认项**为 `verified`；无文档来源的用户补充记为 `user_assertion`，不伪装成简历来源。
3. 原文件与提取文本**长期留存且可回放**；替换文件产生**新** document，不覆盖历史。
4. 重跑抽取**新建** run；历史 run 与其输出不被改写或删除。
5. 每次确认产生可引用的 `ProfileVersion`，并有 before/after 修订审计可回溯。
6. **所有写路径必须经 Application Service**（携带 `RequestContext` + idempotency key，由服务层写审计）；adapter 不得直连领域表。

## 项目结构

```text
src/job_search_assistant/
├── app_services/       # 跨领域应用服务与启动装配（build_foundation）
├── career/             # Career / Experience Library 领域（Phase 2）
├── core/               # 错误、请求上下文、幂等与审计抽象
├── discovery/          # Job Discovery 领域类型、Provider 与持久化 port
├── infrastructure/     # SQLite 与受控文件存储实现
├── applications/       # Application 领域（后续阶段）
└── matching/           # Job Matching 领域（后续阶段）

migrations/             # 有序、校验和保护的 SQLite 迁移（0001–0004）
tests/                  # 标准库 unittest 测试（53 项，不访问网络）
docs/                   # 架构与各阶段设计说明
scripts/                # 开发辅助脚本
```

## 本地初始化

```bash
PYTHONPATH=src python -m job_search_assistant init-db
PYTHONPATH=src python -m job_search_assistant db-info
```

默认数据库为 `.job-search-assistant/job-search-assistant.sqlite`。`--database` 与 `--migrations`
是**全局参数，必须写在子命令之前**：

```bash
PYTHONPATH=src python -m job_search_assistant --database /tmp/x.sqlite init-db   # ✅
```

## 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

项目锁定的解释器记录在仓库根 `dev.env`（本机为 conda 环境 `Job-Search-Engine`，Python 3.12.14），用它跑更稳妥：

```bash
source dev.env
PYTHONPATH=src "$PYBIN" -m unittest discover -s tests
```

> 注意别用 `grep PYBIN dev.env` 取路径——该文件的注释里也含 `PYBIN=`，会把注释一起切出来。要取静态值就用 `grep '^PYBIN=' dev.env`。

测试使用标准库 `unittest`，**不访问网络、LLM、MCP 或浏览器**。

## Career 应用服务怎么装配

```python
from job_search_assistant.app_services import ImportResumeDocument, build_foundation
from job_search_assistant.infrastructure.files import (
    FileSystemDocumentStorage, PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import SQLiteCareerStore

foundation = build_foundation(
    database_path=".job-search-assistant/job-search-assistant.sqlite",
    migrations_path="migrations",
)
store = SQLiteCareerStore(foundation.database)
storage = FileSystemDocumentStorage(".job-search-assistant/documents")
service = ImportResumeDocument(
    store=store, storage=storage, text_extractor=PlainTextResumeExtractor(storage),
)
```

完整链路（导入 → 抽取 → 确认 → 证据包 / 快照）的调用顺序见 `tests/test_career_snapshot.py`，
它同时演示了上面每条不变量是怎么被断言的。

## 开发协作

本仓库用「builder（Codex）+ reviewer」双 agent 循环推进：`codex-hermes-loop.sh` 编排每个切片
（一片一个功能分支；一轮 = builder 写 → reviewer 审 → 测试门 → 提交推送 → 记入 `DEVELOPMENT_LOG.md`；
`NEEDS_FIX` 会带着审查意见再跑一轮，上限 3 轮）。

- `docs/builder-conventions.md` — builder 必须遵守的约定（脚本每轮把它注入 builder 的 prompt）
- `scripts/codex-stream-filter.py` — 把 `codex exec --json` 的事件流实时渲染成人可读行（不额外消耗 token）
- `scripts/cap-diff.py` — 按字节预算**按文件**裁剪 diff，并列出被舍弃的文件名
- `dev.env` — 本机解释器路径（`PYBIN`）

## 文档

- `docs/Job Search Assistant 技术架构文档.md` — 总架构，冲突时以它为准
- `docs/phase0-foundation.md` / `docs/phase1-job-discovery.md` / `docs/phase2-career-foundation.md`
  — 各阶段设计说明、验收标准与遗留清单
- `DEVELOPMENT_LOG.md` — 每轮 builder / reviewer 的结果与提交信息

