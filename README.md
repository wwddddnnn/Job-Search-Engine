# Job Search Assistant

> English version: [README.en.md](README.en.md)

本地优先的个人求职助手。把「**找职位 → 核验职业事实 → 匹配 → 申请准备**」这条链路全部放在自己机器上：
**Python 3.12 标准库 + SQLite**，运行时**零第三方依赖**（不装包、不用 Node.js、不做前端构建），
每一步都可审计、可回放，LLM 在受控、可撤销的位置参与。

快速定位：

- **现在在做** — Phase 2.5 人工整理与核验 UI：S3a 已交付，S3b / S4 待开始
- **可运行基线** — `main` 上是 Phase 2 全部收尾（S1–S5）；Phase 2.5 在功能分支上，尚未合并
- **技术栈** — Python 3.12+ 标准库 · SQLite · 原生模块化 JS（无框架 / 无构建 / 不连 CDN）
- **测试** — 标准库 `unittest`（当前 130 项全绿，不访问网络 / LLM / MCP / 浏览器）
- **开发方式** — builder + reviewer 双 agent 循环（`codex-hermes-loop.sh`，见下文）

## 这个项目解决什么

求职材料散在简历、笔记和各种网页里。一旦交给模型「帮忙总结」，就再也分不清哪些是自己真做过的事实、
哪些是模型顺手写的漂亮话——而申请材料一旦出现这种句子，代价由用户承担。

所以这里反过来做：**事实必须先能追到原文出处，并由用户逐条确认；模型只能产出草稿。**
原文件、抽取文本、每次重跑、每次确认都留下不可覆盖的记录，任何可信结论都能回放到它的来源。

## 核心不变量

违反这些约定的实现会被 reviewer 退回；它们是这个项目存在的理由，不是风格偏好。

1. **LLM 输出一律是草稿**；未经用户显式确认的事实不会进入已发布版本，也不会出现在 `VerifiedEvidencePack` 里。
2. 只有**显式确认项**为 `verified`；无文档来源的用户补充记为 `user_assertion`，不伪装成简历来源。
3. 原文件与提取文本**长期留存且可回放**；替换文件产生**新** document，不覆盖历史。
4. 重跑抽取**新建** run；历史 run 与其输出不被改写或删除。
5. 每次确认产生可引用的 `ProfileVersion`，并有 before/after 修订审计可回溯。
6. **所有写路径必须经 Application Service**（携带 `RequestContext` + idempotency key，由服务层写审计）；
   adapter 不得直连领域表。

## 现在做到哪了

| 阶段 | 目标 | 状态 |
|---|---|---|
| **Phase 0** Foundation | 工程骨架与架构护栏（模块边界、迁移、错误模型、幂等、审计） | ✅ 已验收 |
| **Phase 1** Job Discovery | 可靠地发现、保存、查看职位（原始响应留存、去重、不可变 snapshot） | ✅ 已验收 |
| **Phase 2** Career Foundation | 可核验的个人职业知识库（导入 / 抽取 / 确认 / 证据包 / 快照） | ✅ S1–S5 已验收 |
| **Phase 2.5** 人工整理与核验 UI | 在浏览器里手工整理、核验并发布职业事实 | 🚧 见下表 |
| **B 步** 真实 LLM | 接入真实 provider（现在只有确定性假 provider） | ⏳ 待开始 |
| **Phase 3** Job Matching | 低成本、可解释、可失效的匹配结果 | ⏳ 未开始 |
| **Phase 4–7** | Application Core、Agent-ready Contracts、MCP Adapter、受控浏览器投递 | ⏳ 未开始 |

Phase 2.5 当前切片：

| 切片 | 内容 | 状态 |
|---|---|---|
| S1 | 审核草稿服务、来源保留、自动保存、部分发布与版本语义 | ✅ |
| S2a | 单命令启动、JSON HTTP adapter、原生前端骨架、中英文、Markdown 导入、只读双视图 | ✅ |
| S2b | 双栏编辑：选区建条目、编辑 / 核验 / 拒绝 / 删除 / 撤销、自动保存状态机、发布与版本冲突 | ✅ 已通过审查 |
| S3a | 多套 LLM API 配置、受控本地凭据、配置面板与契约 | ✅ 已交付 |
| S3b | 引用 / prompt 面板、Mock 优化、一键替换与撤销 | ⏳ 待开始 |
| S4 | 多文档补充已有经历、Mock 自动合并、完整人工验收说明 | ⏳ 待开始 |

分支约定：**每个切片一个功能分支、一轮 builder + reviewer 循环、每轮一次提交。**
每阶段的 DoD（可验证的完成定义）与验收标准不在 README 里堆着，看
[架构文档 §11](docs/Job%20Search%20Assistant%20技术架构文档.md) 与各阶段文档（见文末「文档」）。

## 快速开始

前置只有一个：**Python 3.12+**（`requires-python = ">=3.12"`）。不需要装依赖、Node.js、数据库服务。

```bash
# 1. 克隆代码
git clone https://github.com/wwddddnnn/Job-Search-Engine.git
cd Job-Search-Engine

# 2. 指一个解释器：把 python3.12 的绝对路径写进仓库根 dev.env 的 PYBIN= 一行
#    本机可用既有 conda 环境（3.12.14）：
#    conda create -n Job-Search-Engine python=3.12 -y --solver classic   # 本机必须带 --solver classic
#    PYBIN=/opt/homebrew/Caskroom/miniconda/base/envs/Job-Search-Engine/bin/python

# 3. 初始化数据库 + 跑测试
source dev.env
PYTHONPATH=src "$PYBIN" -m job_search_assistant init-db
PYTHONPATH=src "$PYBIN" -m job_search_assistant db-info
PYTHONPATH=src "$PYBIN" -m unittest discover -s tests

# 4. 起本地界面（S2a 起可用）
PYTHONPATH=src "$PYBIN" -m job_search_assistant serve --port 8000 --host 127.0.0.1
#    → 打开 http://127.0.0.1:8000/ ，终端 Ctrl-C 停止
```

几条容易踩的：

- `dev.env` 里是**原机器的绝对路径**，换机器必须改 `PYBIN=`；同名环境变量优先，可临时覆盖：
  `PYBIN=/path/to/python ./codex-hermes-loop.sh "任务"`。
- 默认数据库 `.job-search-assistant/job-search-assistant.sqlite`（已在 `.gitignore` 里）；
  删掉整个 `.job-search-assistant/` 目录即重置。
- `--database` / `--migrations` 是**全局参数，必须写在子命令之前**：
  `… python -m job_search_assistant --database /tmp/x.sqlite init-db`。
- 想直接用 `job-search-assistant` 命令名（而非 `python -m job_search_assistant`）：`pip install -e .`，
  零第三方依赖，不会联网拉包。
- 跑某个还在飞的切片：`git checkout phase2.5/s2a-web-entry`（先确认该分支已推送到远端）。

**运行细节与人工验收步骤**（含自动测试覆盖不到、必须人工点的三项）：
[docs/manual-acceptance.md](docs/manual-acceptance.md)；各切片的 HTTP 契约在
[docs/phase2.5-career-review-ui.md](docs/phase2.5-career-review-ui.md)。

## 开发方式：builder + reviewer 双 agent 循环

一个切片（slice）落在一条功能分支上，由 `codex-hermes-loop.sh` 编排成若干轮循环。每轮的节奏固定：
**reviewer 上一轮的结论给这一轮定活儿 → builder 动手写 → 测试门 → reviewer 检查并裁决 → 落盘提交。**
两个 agent 互相独立：builder 只写代码、只改工作区，不碰 git；reviewer 只输出裁决文本，没有任何写文件或
执行命令的能力；中间的收改动、跑测试、写日志、commit、push 全部由脚本做。

人在这条流水线上出现两次：开工时给一份任务书（第 1 轮的活儿从这里来），以及 `ESCALATE` 时拍板。

```text
功能分支
  └─ 第 N 轮
       ├─ builder  按任务书写代码
       ├─ 收改动   暂存全部改动，生成本轮 diff
       ├─ 测试门   跑单元测试 —— 不通过就没有 PASS 可谈
       ├─ reviewer 根据「架构 / 阶段文档 + 本轮 diff + 测试结果」审查
       ├─ 记日志   追加本轮结论
       └─ 提交推送（无论通过与否，保证随时可回滚）
```

### 一轮里的七步

1. **组装 builder 的输入。** 脚本在任务书后面追加三段：本项目的解释器与测试命令、builder 协作约定全文、
   以及**上一轮 reviewer 的审查意见与上一轮测试结果**。第 1 轮没有第三段，活儿全部来自人给的任务书；
   从第 2 轮起，这一轮要改成什么样、改到什么程度算过关，由 reviewer 上一轮的结论定下来——审查意见就是
   下一轮的施工单。
2. **builder 开发。** 默认用 headless Codex 跑，它的输出流会被本地脚本实时渲染成人可读行；它只改工作区
   文件，提交和推送都不碰。
3. **收改动。** 脚本把工作区的全部改动（含新增的未跟踪文件，开发日志除外）暂存下来，生成一份 diff。
   这份 diff 就是本轮唯一的审查对象，reviewer 每轮只看到当轮改动。
4. **测试门。** 跑项目的单元测试。测试红着，这一轮就拿不到 `PASS`：即使 reviewer 判了通过，脚本也会降级
   成 `NEEDS_FIX`，并在日志里标出原因。
5. **reviewer 检查并裁决。** 脚本把「本次任务 + 架构与阶段文档全文 + 本轮 diff + 测试结果」拼成一份输入
   交给 Hermes。reviewer 的开头三行是三个固定字段——审查结论 STATUS、提交信息 COMMIT_MSG、日志说明
   LOG_NOTE——字段名写错这轮审查就作废；正文再按「文件与行号 / 违反的条款 / 现象 / 为什么有问题 /
   建议修改方向」逐条写问题。
6. **落盘。** 脚本把日志说明追加进开发日志，按结论给提交信息加前缀：通过的原样，待修改的加
   「第N轮/待修改」，需人工确认的加「需人工确认」。然后提交并推送——通过与否都提交，分支上始终留着
   一个可回滚的点。
7. **决定下一步。** `PASS` 结束（退出码 0）；`NEEDS_FIX` 回到第 1 步，把刚拿到的审查意见交给 builder
   再干一轮（默认最多 3 轮）；`ESCALATE` 停下等人。

| STATUS | reviewer 的意思 | 脚本接着做什么 |
|---|---|---|
| `PASS` | 未发现违反不变量 / 验收标准的问题 | 提交推送，退出码 0 |
| `NEEDS_FIX` | 有明确、可指出修改方向的问题 | 提交推送，把意见带回 builder 进下一轮（上限 `MAX_ATTEMPTS`，默认 3 轮） |
| `ESCALATE` | 方向存疑 / 不可回滚决策 / 同一问题反复 | 提交推送，退出码 2，停下等人 |

脚本会自动停下等人的几处情形（都以退出码 2 收尾，不会静默继续）：

- 跑满 `MAX_ATTEMPTS` 仍未通过，或 reviewer 输出里读不到合法 `STATUS`。
- 本轮 diff 为空——builder 没真正动手。
- builder 自己报出配额 / 认证问题。此时脚本会先打印工作区**残留清单**（改动条目数、未跟踪文件数），
  提醒「本轮没有可审查的产物」并不等于工作区干净：半成品可能已经躺在那里了。
- reviewer 输出带 prompt 回显、却找不到本轮 nonce 边界（CLI 输出格式变了）。

### 用法

```bash
git checkout -b phase2.5/s2b-editing          # 脚本拒绝在 main/master 上直接跑
./codex-hermes-loop.sh "实现双栏编辑与自动保存状态机"
```

### 为什么这样切分

- **写权限与审查权分在两个进程里。** reviewer 走
  `hermes chat -Q -t vision --ignore-rules -c <REVIEWER_SESSION> --create-if-missing --query-file <prompt>`：
  `-t vision` 只注入 `vision_analyze` 一个工具，写文件、bash、读文件的能力在它那儿根本不存在，独立性由
  能力削减保证；`--ignore-rules` 让它不吃本机记忆与 `AGENTS.md`，只按架构文档和本轮 diff 判断；`-Q` 让
  输出只剩最终回答加一行 `session_id`，格式说明不会和 STATUS 解析撞车。
- **两边看同一份文件状态。** builder 与 reviewer 原地读写同一个 checkout：禁止 `git worktree`、
  禁止为开发再 clone 一份、禁止改完拷回来。这样 reviewer 审的 diff 就是 builder 刚改过的那份文件，
  去重 / 合并这类结论事后复核得动（换机器后这条照样成立）。
- **每轮各提交一次，审查范围收敛到当轮。** reviewer 拿到的 diff 只含这一轮的改动，历史改动不会混进
  这次的判断。
- **成本账。** 每轮 = 一次 builder 调用 + 一次审查调用 + 一次提交；实测 S2–S5 每片都出现过 `NEEDS_FIX`
  收口轮，也就是一片常要两轮才过。所以 builder 侧有硬性的效率约定（见 `docs/builder-conventions.md`：
  合并读取、先扫后读、合并验证、不重复确认——逐文件 `cat` 是本项目最大的 token 开销来源）。

### 关键文件与可调项

| 文件 / 变量 | 作用 |
|---|---|
| `codex-hermes-loop.sh` | 编排脚本（builder / reviewer 可插拔：`BUILDER=codex\|hermes\|cmd`、`REVIEWER=hermes\|opencode`） |
| `.opencode/prompts/reviewer.md` | reviewer 的 system prompt（只读 agent 定义在 `opencode.json`） |
| `docs/builder-conventions.md` | builder 必须遵守的约定，每轮由脚本注入 builder 的 prompt |
| `DEVELOPMENT_LOG.md` | 每轮时间 / 轮次 / 审查结论 / 测试结果 / 说明，由脚本写入 |
| `REVIEWER_SESSION` | reviewer 复用的**具名会话**（默认 `jse-reviewer`）。上下文由 Hermes 自动压缩兜底；**换个名字即重置上下文**：`REVIEWER_SESSION=jse-reviewer-2026q4 ./codex-hermes-loop.sh "…"` |
| `FILE_REVIEWER_SESSION` | reviewer 会话归属补齐（写 Hermes `state.db`）；`=0` 关闭，关掉后需手动核对会话归属 |
| `ARCH_DOCS` | 喂给 reviewer 的架构 / 阶段文档（默认 5 份；路径含空格时用换行分隔） |
| `MAX_CONTEXT_KB` | reviewer 输入上限（默认 300KB），超限时按文件裁剪 diff 并列出未包含的文件 |
| `TEST_CMD` / `PYBIN` | 验收测试命令与解释器（`PYBIN` 默认读 `dev.env`） |
| `KEEP_RUNS=1` / `SKIP_TESTS=1` / `DRY_RUN=1` | 保留本轮中间产物 / 跳过测试门（不建议）/ 演练不提交不推送 |
| `scripts/codex-stream-filter.py` | 把 `codex exec --json` 的事件流实时渲染成人可读行（不额外消耗 token） |
| `scripts/cap-diff.py` | 按字节预算**按文件**裁剪 diff，并列出被舍弃的文件名 |

完整差异清单（相对导入的原始套件）与本地适配的实证记录见
[docs/hermes-reviewer-kit.md](docs/hermes-reviewer-kit.md)。

## 项目结构

```text
src/job_search_assistant/
├── app_services/       # 跨领域应用服务与启动装配（build_foundation、build_local_ui）
├── adapters/web/       # Phase 2.5 的本地 HTTP 入口与静态页面（页面资源在 static/）
├── career/             # Career / Experience Library 领域（Phase 2 起）
├── core/               # 错误、请求上下文、幂等与审计抽象
├── discovery/          # Job Discovery 领域类型、Provider 与持久化 port
├── infrastructure/     # SQLite 与受控文件存储实现
├── applications/       # Application 领域（后续阶段）
└── matching/           # Job Matching 领域（后续阶段）

migrations/             # 有序、校验和保护的 SQLite 迁移（新增迁移，绝不改旧文件）
tests/                  # 标准库 unittest 测试（HTTP 仅访问本机 loopback）
docs/                   # 架构、各阶段设计说明与运行 / 验收手册
scripts/                # 开发辅助脚本
codex-hermes-loop.sh    # builder + reviewer 循环编排
```

依赖方向：`core` ← 领域模块 ← `app_services` ← adapter。领域代码不 import SQLite 具体实现，
HTTP / UI adapter 不直连数据库、也不调用 store 内部写接口。
Career 应用服务的装配示例（当前没有 CLI 业务命令）见
[docs/manual-acceptance.md §4](docs/manual-acceptance.md#4-手工装配调用career-应用服务)。

## 文档

- [docs/Job Search Assistant 技术架构文档.md](docs/Job%20Search%20Assistant%20技术架构文档.md)
  — 总架构：§5 不变量、§6 模块边界、§9 状态机、§11 分阶段计划与 DoD，**冲突时以它为准**
- [docs/manual-acceptance.md](docs/manual-acceptance.md) — 本地运行细节、S2a / S2b / S3a 人工验收步骤、
  自动测试的覆盖边界、Career 应用服务手工装配
- [docs/phase0-foundation.md](docs/phase0-foundation.md) — Phase 0 设计说明与验收标准
- [docs/phase1-job-discovery.md](docs/phase1-job-discovery.md) — Phase 1 范围、管道、测试策略
- [docs/phase2-career-foundation.md](docs/phase2-career-foundation.md) — Phase 2 设计说明、退出条件、
  切片表与各切片审查遗留项
- [docs/phase2.5-career-review-ui.md](docs/phase2.5-career-review-ui.md) — Phase 2.5 交互、LLM 配置与
  Mock 范围、版本规则、验收标准与各切片契约
- [docs/builder-conventions.md](docs/builder-conventions.md) — builder 的硬性约束、效率约定、自测与提交规则
- [docs/hermes-reviewer-kit.md](docs/hermes-reviewer-kit.md) — 循环的本地适配记录与断点续跑准则
- `DEVELOPMENT_LOG.md` — 每轮 builder / reviewer 的结果与提交信息
