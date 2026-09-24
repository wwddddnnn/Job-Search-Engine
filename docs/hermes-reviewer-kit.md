# Codex(builder) + Reviewer 循环：本地适配记录

来源：`hermes-reviewer-kit.zip`（2026-09-15 由 Hermes 导入并本地适配）。原始压缩包仍在仓库目录下，
已被 `.gitignore` 排除；套件内容以仓库里的脚本与文档为准。

循环怎么跑、三种 STATUS 什么含义、有哪些可调项：见
[README「开发方式」](../README.md#开发方式builder--reviewer-双-agent-循环)。
本文件只记录 README 不写的两件事：**本地适配改了什么、为什么**，以及**撞额度后怎么续跑**。

## 本地适配记录（相对导入的原始套件）

原套件按原样导入，以下是为让它在**本机真实可用**所做的改动：

1. **`AGENTS.md` 未创建。** 原意是把 builder 约定写进仓库根的 `AGENTS.md`（Codex 自动读取）。
   该写入被 Hermes 的「agent 指令文件保护策略」拦下，需要用户本人确认。
   现在换成 `docs/builder-conventions.md`，由脚本在每轮显式注入 builder 的 prompt，效果等价。
   *想改成原生 `AGENTS.md` 的话，把该文件内容复制过去即可，脚本会自动同时生效。*

2. **修掉「新增文件看不见」的致命 bug。** 原脚本用 `git diff` 收改动，只覆盖已跟踪文件——
   builder 每新建一个文件（这是常态）都会看到空 diff，然后直接 `exit 2` 判定「Codex 没干活」。
   改为先 `git add -A` 再 `git diff --cached`，并把 `DEVELOPMENT_LOG.md` 排除在 diff 之外，
   避免 reviewer 去审自己的日志。

3. **新增测试门（验收环节）。** 原套件只审 diff，不跑测试——reviewer 说 PASS 但代码跑不起来时无人知晓。
   现在每轮先跑 `unittest`，结果随 diff 一起交给 reviewer；reviewer 判 `PASS` 而测试未通过时，
   脚本自动降级为 `NEEDS_FIX`（日志里标注原因）。

4. **新增 builder 故障识别。** Codex 配额用尽/认证失效时会**退出码 0 但打印错误**，
   原脚本会把它当成「没有改动」而给出含糊提示。现在识别 `usage limit` / `401` / `not logged in`
   等特征并在终端打印原始输出尾部（额度中断时还会打印工作区残留清单）。

5. **builder / reviewer 可插拔。** `BUILDER=codex|hermes|cmd`、`REVIEWER=hermes|opencode`，
   默认 `codex` + `hermes`。

6. **reviewer 被物理限制为只读。** 调用命令见 README；这一步比原套件的
   `tools: {write:false, edit:false, bash:false}` 更彻底——本机跑 Hermes 时连读文件的能力都不给，
   reviewer 只能看到脚本喂给它的输入。

7. **STATUS 解析健壮化。** 原脚本硬取输出第 1/2/3 行，模型但凡多一句寒暄、或 opencode 输出带
   ANSI/spinner 就会「格式读不懂」而中止。现在剥离 ANSI/CR，用正则找 `STATUS:` / `COMMIT_MSG:` /
   `LOG_NOTE:` 三个字段行，并校验 STATUS 取值合法。

8. **架构文档路径参数化。** 原脚本硬编码 `ARCHITECTURE.md`。本项目把架构文档与四份阶段文档共 5 份
   交给 reviewer，由 `ARCH_DOCS` 配置（默认已指向这 5 份；路径含空格时用换行分隔）。

9. **`git push -u origin`**（原为 `git push origin`，首次推送没有上游分支）；
   推送失败不再让脚本静默结束，而是明确提示 + 退出码 2。

10. **reviewer 输入体积上限** `MAX_CONTEXT_KB`（默认 300）：超限时按文件截断 diff，并明确告知
    reviewer「已截断，缺上下文请按规则说明」。输入走 `--query-file`，不再把整份 prompt 当命令行
    参数传递，因此不受参数长度上限约束；这个上限只用来控制喂给 reviewer 的体量（成本与延迟）。

11. **修了一个仓库原有的 flaky 测试**（非套件内容）：`tests/test_discovery.py` 用
    `ORDER BY received_at, id` 排序，而同一页两个 raw payload 共享 `received_at`、`id` 又是
    `uuid4()`，导致断言顺序随机，实测 **25 次里 11 次失败**。改为 `ORDER BY received_at, rowid`
    （rowid 才是写入顺序），25/25 通过。这是纯测试改动，未触碰产品代码。

12. **reviewer 复用一个具名会话 + 归属补齐**（2026-09-17 追加。起因：每个切片跑一轮就新堆一个
    reviewer session，S2–S5 期间堆了 5 个，且它们的 `cwd` 都是 NULL，在桌面端落到 Home 桶而不是
    本项目。）——可调项与开关见 README 的关键文件表，这里只记原因与实测：
    - **复用**：`-c <REVIEWER_SESSION> --create-if-missing`（默认 `jse-reviewer`）。首次创建，之后
      每轮 resume 同一会话；**换个名字即换一个干净会话**（重置上下文）。
    - **上下文长度**：跨轮累积由 Hermes 的自动压缩兜底（`~/.hermes/config.yaml` 的
      `compression.enabled=true` / `threshold=0.5` / `target_ratio=0.2` / `protect_last_n=20`）；
      同时 reviewer prompt 里显式写了「忽略会话历史，只依据本轮输入判断」，避免历史结论锚定本轮。
    - **归属**：`hermes chat` 路径不写 session 的 `cwd`/`git_repo_root`（只有顶层 `-z` 的 oneshot
      路径才写），所以脚本每轮按输出里的 `session_id` 直写 `~/.hermes/state.db` 补一次归属
      （写前报 `journal_mode`、写后回读校验、不一致则非零退出并被 warn 捕获）。
      这是**耦合 Hermes 内部表结构**的临时兜底，Hermes 升级后需重新验证；失效表现 = 该会话出现在
      桌面端 Home 桶而非本项目。`FILE_REVIEWER_SESSION=0` 可关掉这段（关掉后需手动核对归属）。
    - **实证**（`DRY_RUN=1` 演练，Hermes Agent v0.20.5 / upstream 64ea66b0，2026-09-17）：首轮创建
      会话 → 次轮 resume 同一 session（消息数从 2 增到 4/6/8）→ `-Q` 输出能抽到
      `session_id` / `STATUS` / `COMMIT_MSG` / `LOG_NOTE` → 剥离后 `last_review.txt` 无 prompt 回显
      → 预置一个错误 `cwd`（`/Users/doriswu`）后该轮被自动纠正回本仓库 → `LOOP_EXIT=0`。
      演练用的探针文件、临时分支、会话与 run 目录均已清理，未留在仓库历史里。

## 断点续跑准则

builder 撞上配额/认证问题时脚本以退出码 2 停下，并提示「本轮没有可审查的产物」——
这句话的意思只是**这一轮没有提交任何东西**，工作区里可能留着改到一半的半成品。
脚本会同时打印改动条目数与未跟踪文件数。按下面顺序处理，别急着重跑或丢弃：

1. **先看清残留**：`git status --short`、`git diff --stat`，确认哪些文件被改、哪些是新增。
2. **判断残留是否自洽**：改动是否能编译、测试能否跑过（见 `builder-conventions.md` 的自测命令）。
   半成品若已经接近完成，丢掉比留下更贵。
3. **二选一**：
   - **丢弃**：`git checkout -- .` 撤销已跟踪文件的改动，`git clean -fd` 删掉本轮新增的未跟踪文件
     （动手前再确认一遍清单，`git clean` 不进回收站）。
   - **续跑**：用**同一份任务书**重跑，让 builder 从当前工作区状态接着干；若残留本身已经基本完成，
     把任务书写成「继续完成 X，当前工作区状态是 Y」，比原样重下任务书更省。
4. **不要跳过第 1 步**：带着未判定的残留重跑，reviewer 会把上一轮的半成品当成当轮改动一起审；
   直接丢弃则可能丢掉已经写好的实现。

## 本机环境

解释器、测试命令、conda 环境与「不要自建环境」的约束：见
[builder-conventions.md](builder-conventions.md) 的「每轮必须自测」与仓库根 `dev.env`
（`PYBIN` 的唯一定义处）。builder 的额度状态、reviewer 的可用性属于当天快照，不写进文档——
跑一轮看终端输出即可。

## 相关文档

- [README「开发方式」](../README.md#开发方式builder--reviewer-双-agent-循环) — 循环流程、STATUS 分流、可调项
- `docs/builder-conventions.md` — builder 必须遵守的约定（每轮由脚本注入）
- `.opencode/prompts/reviewer.md` — reviewer 的审查规则与输出格式
- `docs/Job Search Assistant 技术架构文档.md` §5 — 架构不变量（reviewer 的判据来源）
