#!/usr/bin/env bash
#
# codex-hermes-loop.sh — Codex(builder) + Reviewer(hermes/opencode) 编排脚本
#
# 用法:
#   ./codex-hermes-loop.sh "本次要实现的任务描述"
#
# 前提:
#   1) 已 checkout 到功能分支（不得在 main/master 上直接跑）
#   2) 项目根目录有 .opencode/prompts/reviewer.md（reviewer 的 system prompt）
#   3) 架构文档存在（见 ARCH_DOCS），reviewer 以它为审查依据
#   4) builder 与 reviewer 已安装并完成认证
#
# 硬规则: builder 与 reviewer 原地读写同一份 checkout。本脚本不创建 worktree、
#         不为开发克隆仓库；两者必须在同一个工作目录看到同一份文件状态。
#
# 可选环境变量（都有默认值）:
#   BUILDER=codex|hermes|cmd    builder 实现，默认 codex（cmd = 直接执行 BUILDER_CMD，用于演练/接入其它 agent）
#   BUILDER_CMD="..."           BUILDER=cmd 时执行的命令（在仓库根目录执行）
#   REVIEWER=hermes|opencode    reviewer 实现，默认 hermes
#   MAX_ATTEMPTS=3              最大循环轮数
#   ARCH_DOCS="a.md b.md"       喂给 reviewer 的架构/阶段文档（空格分隔；路径含空格时改用换行分隔）
#   TEST_CMD="..."              每轮验收测试命令
#   PYBIN=/path/to/python       验收测试用的解释器（也可写进仓库根的 dev.env）
#   MAX_CONTEXT_KB=300          reviewer 输入上限，超出则按文件裁剪 diff（并列出未包含的文件）
#   REVIEWER_SESSION=jse-reviewer
#                               reviewer 复用的具名 session（默认 jse-reviewer）：每轮 resume 同一个
#                               会话，而不是每轮新开一个；换个名字即换一个干净会话（重置上下文）
#   FILE_REVIEWER_SESSION=0     关掉 reviewer 会话的归属补齐（该段会写 Hermes 的 state.db；关掉后跑完手动核对）
#   KEEP_RUNS=1                 保留本轮中间产物（.job-search-assistant/loop-runs/<时间戳>/）
#   SKIP_TESTS=1                跳过测试门（不建议）
#   DRY_RUN=1                   本地演练：不 commit、不 push
#
# 退出码: 0 = PASS 通过；1 = 用法/环境错误；2 = 需要人工介入
#
# 与原始套件的差异见 docs/hermes-reviewer-kit.md「本地适配说明」。

set -uo pipefail

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
BUILDER="${BUILDER:-codex}"
REVIEWER="${REVIEWER:-hermes}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
MAX_CONTEXT_KB="${MAX_CONTEXT_KB:-300}"
# reviewer 的具名 session：上下文由 Hermes 自动压缩（config.yaml compression.enabled /
# threshold=0.5 / target_ratio=0.2）；需要彻底重置上下文时，换一个名字（另起一个干净会话）。
REVIEWER_SESSION="${REVIEWER_SESSION:-jse-reviewer}"
LOG_FILE="DEVELOPMENT_LOG.md"
REVIEWER_PROMPT=".opencode/prompts/reviewer.md"
BUILDER_CONVENTIONS="docs/builder-conventions.md"
CODEX_APP_BIN="/Applications/ChatGPT.app/Contents/Resources/codex"

DEFAULT_ARCH_DOCS=(
  "docs/Job Search Assistant 技术架构文档.md"
  "docs/phase0-foundation.md"
  "docs/phase1-job-discovery.md"
  "docs/phase2-career-foundation.md"
  "docs/phase2.5-career-review-ui.md"
)

# Python 解释器：环境变量 PYBIN > 仓库根 dev.env > PATH 上的 python3
if [ -z "${PYBIN:-}" ] && [ -f dev.env ]; then
  # shellcheck disable=SC1091
  . ./dev.env
fi
PYBIN="${PYBIN:-$(command -v python3 || true)}"
DEFAULT_TEST_CMD="PYTHONPATH=src ${PYBIN} -m unittest discover -s tests -v"

if [ -n "${ARCH_DOCS:-}" ]; then
  if [[ "$ARCH_DOCS" == *$'\n'* ]]; then
    # 换行分隔：路径含空格时只能这么传（空格分隔会把 "docs/Job Search Assistant …md" 切碎）
    ARCH_FILES=()
    while IFS= read -r _arch_doc; do
      [ -n "$_arch_doc" ] && ARCH_FILES+=("$_arch_doc")
    done <<< "$ARCH_DOCS"
  else
    read -r -a ARCH_FILES <<< "$ARCH_DOCS"   # 旧的空格分隔写法，保持兼容
  fi
else
  ARCH_FILES=("${DEFAULT_ARCH_DOCS[@]}")
fi
TEST_CMD="${TEST_CMD:-$DEFAULT_TEST_CMD}"

say()  { printf '%s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die()  { printf '❌ %s\n' "$*" >&2; exit "${2:-1}"; }

# ----------------------------------------------------------------------------
# 0. 环境校验
# ----------------------------------------------------------------------------
TASK="${1:-}"
if [ -z "$TASK" ]; then
  say "用法: ./codex-hermes-loop.sh \"本次要实现的任务描述\""
  exit 1
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "当前目录不是 git 仓库。"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT" || die "无法进入仓库根目录 $REPO_ROOT"

# 本地辅助脚本（纯本地解析，零 token 成本）
STREAM_FILTER="$REPO_ROOT/scripts/codex-stream-filter.py"
CAP_DIFF="$REPO_ROOT/scripts/cap-diff.py"

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [ -z "$BRANCH" ]; then
  die "当前处于 detached HEAD 状态，请先 checkout 到功能分支。"
fi
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
  warn "当前在主分支 ($BRANCH) 上，请先 git checkout -b 一个功能分支再运行本脚本。"
  exit 1
fi

[ -f "$REVIEWER_PROMPT" ] || die "缺少 reviewer system prompt：$REVIEWER_PROMPT"
for f in "${ARCH_FILES[@]}"; do
  [ -f "$f" ] || die "缺少架构文档：${f}（可用 ARCH_DOCS 覆盖）"
done

# builder 可执行体
CODEX_BIN=""
if [ "$BUILDER" = "codex" ]; then
  if command -v codex >/dev/null 2>&1; then
    CODEX_BIN="$(command -v codex)"
  elif [ -x "$CODEX_APP_BIN" ]; then
    CODEX_BIN="$CODEX_APP_BIN"
  else
    die "找不到 codex CLI。请 npm i -g @openai/codex，或确认 $CODEX_APP_BIN 存在。"
  fi
elif [ "$BUILDER" = "hermes" ]; then
  command -v hermes >/dev/null 2>&1 || die "找不到 hermes CLI。"
elif [ "$BUILDER" = "cmd" ]; then
  [ -n "${BUILDER_CMD:-}" ] || die "BUILDER=cmd 时必须同时提供 BUILDER_CMD。"
else
  die "未知 BUILDER=${BUILDER}（支持 codex|hermes|cmd）"
fi

# reviewer 可执行体
if [ "$REVIEWER" = "hermes" ]; then
  command -v hermes >/dev/null 2>&1 || die "找不到 hermes CLI。"
elif [ "$REVIEWER" = "opencode" ]; then
  command -v opencode >/dev/null 2>&1 || die "找不到 opencode CLI。"
else
  die "未知 REVIEWER=${REVIEWER}（支持 hermes|opencode）"
fi

if [ ! -f "$LOG_FILE" ]; then
  {
    printf '# 开发日志\n\n'
    printf '每轮由 `codex-hermes-loop.sh` 自动追加：builder 产出 → reviewer 审查 → 提交推送。\n\n'
    printf '| 时间 | 轮次 | 审查结论 | 测试 | 说明 |\n|---|---|---|---|---|\n'
  } > "$LOG_FILE"
fi

# 本轮所有临时产物都留在项目内（.gitignore 已忽略 .job-search-assistant/），
# 不在项目外创建目录：任务描述、diff、审查输出、测试输出都落在这里。
RUNS_DIR="$REPO_ROOT/.job-search-assistant/loop-runs"
mkdir -p "$RUNS_DIR"
WORKDIR="$(mktemp -d "$RUNS_DIR/$(date '+%Y%m%d-%H%M%S')-XXXXXX")"
KEEP_RUNS="${KEEP_RUNS:-0}"
cleanup() {
  if [ "$KEEP_RUNS" = "1" ]; then
    say "KEEP_RUNS=1：本轮中间产物保留在 $WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

printf '%s\n' "$TASK" > "$WORKDIR/task.txt"

# ----------------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------------
build_architecture_text() {
  : > "$WORKDIR/arch.txt"
  local f
  for f in "${ARCH_FILES[@]}"; do
    printf '\n===== 文件: %s =====\n' "$f" >> "$WORKDIR/arch.txt"
    cat "$f" >> "$WORKDIR/arch.txt"
  done
}

# builder 的真实退出码经由全局 BUILDER_RC 传出：
# 流式管道下 $? 只会反映最后一个 tee 的状态，不能用它判断 builder 是否失败。
BUILDER_RC=0
run_builder() {
  local prompt_file="$1" out_file="$2"
  local raw_file="$WORKDIR/builder_raw.jsonl"
  BUILDER_RC=0
  if [ "$BUILDER" = "codex" ]; then
    if [ -f "$STREAM_FILTER" ]; then
      # --json 只改输出格式，不增加 token 消耗。
      # tee 落一份原始 JSONL 供排查，过滤器渲染成人可读行实时显示在终端面板，
      # 再 tee 一份可读日志给 reviewer 与后续解析。
      "$CODEX_BIN" exec --sandbox workspace-write --json "$(cat "$prompt_file")" </dev/null 2>&1 \
        | tee "$raw_file" \
        | "$PYBIN" "$STREAM_FILTER" \
        | tee "$out_file"
      BUILDER_RC=${PIPESTATUS[0]}
    else
      warn "找不到 $STREAM_FILTER，本轮退化为不流式（过程不可见）。"
      "$CODEX_BIN" exec --sandbox workspace-write "$(cat "$prompt_file")" > "$out_file" 2>&1
      BUILDER_RC=$?
    fi
  elif [ "$BUILDER" = "cmd" ]; then
    ( eval "$BUILDER_CMD" ) > "$out_file" 2>&1
    BUILDER_RC=$?
  else
    # 注意: -z 的取值必须紧跟在 -z 后面，否则 argparse 会把下一个选项当成它的值
    # （顶层 -z 的 oneshot 路径无法 resume，所以 BUILDER=hermes 时仍是每轮新开一个 session；
    #   本项目 builder 用 codex，不产生 Hermes session，这里保持原样）
    hermes -t coding --in "$REPO_ROOT" -z "$(cat "$prompt_file")" > "$out_file" 2>&1
    BUILDER_RC=$?
  fi
  return 0
}

builder_looks_broken() {
  # codex 配额用尽/认证失败时会退出码 0 但打印这类信息，必须显式识别。
  # 可读日志与原始 JSONL 都查：渲染后错误文本可能只在其中一份里完整。
  local pattern="hit your usage limit|usage limit reached|insufficient_quota|401 Unauthorized|not logged in|please run .*login"
  grep -qiE "$pattern" "$1" 2>/dev/null && return 0
  grep -qiE "$pattern" "$2" 2>/dev/null && return 0
  return 1
}

TESTS_STATE=""
run_tests() {
  local out_file="$1"
  if [ "${SKIP_TESTS:-0}" = "1" ]; then
    printf 'SKIP_TESTS=1，本轮跳过测试门。\n' > "$out_file"
    TESTS_STATE="SKIPPED"
    return 0
  fi
  if ( eval "$TEST_CMD" ) > "$out_file" 2>&1; then
    TESTS_STATE="PASS"
  else
    TESTS_STATE="FAIL"
  fi
  return 0
}

run_reviewer() {
  local prompt_file="$1" out_file="$2"
  if [ "$REVIEWER" = "hermes" ]; then
    # -t vision: 只给 vision_analyze 一个工具，物理上无法写文件/执行命令
    # 复用同一个具名 session：--create-if-missing 首次创建，之后每轮 resume 同一个会话。
    # 不能靠顶层 -z 来 resume：顶层 oneshot 分支不认 -c/--resume，每轮都会新开一个 session
    # （S2–S5 期间就是这样堆了 5 个 reviewer session）。改用 chat 子命令 + --query-file，
    # 顺带不再受命令行参数长度上限的约束。
    # -Q/--quiet: 不打印 banner/spinner/工具预览，也不回显 prompt —— 输出只剩最终回答
    # 和一行 session_id，STATUS 字段不会跟 prompt 回显里的格式说明撞车。
    hermes chat -Q --in "$REPO_ROOT" -t vision --ignore-rules \
      -c "$REVIEWER_SESSION" --create-if-missing \
      --query-file "$prompt_file" > "$out_file" 2>&1
  else
    opencode run --agent reviewer --file "$WORKDIR/context.txt" \
      "$(cat "$WORKDIR/review_instruction.txt")" > "$out_file" 2>&1
  fi
}

# chat 路径不写 session 的 cwd/git_repo_root（顶层 -z 的 oneshot 路径才写），
# 派生的 reviewer 会话会因此掉进桌面端的 Home 桶而不是本仓库项目 —— 所以这里补一次归属。
# 已知代价（审查意见提过，这里明确取舍）：这是直接写 Hermes 的 state.db，耦合了内部表结构；
# Hermes 升级后可能失效（失效只会 warn，不会中断本轮），也没有官方入口可用
# （`hermes project` 只有 create/add-folder/use/…，没有"把某个 session 归到项目"的子命令）。
# 想完全不碰 Hermes 内部状态：FILE_REVIEWER_SESSION=0 关掉本段，跑完手动核对归属。
file_reviewer_session() {
  local sid="${1:-}"
  [ "${FILE_REVIEWER_SESSION:-1}" = "1" ] || return 0
  [ "$REVIEWER" = "hermes" ] || return 0
  [ -n "$sid" ] || { warn "未能从 reviewer 输出里读到 session_id，跳过归属检查。"; return 0; }
  "$PYBIN" - "$sid" "$REPO_ROOT" <<'PY' || warn "reviewer session 归属检查失败（不影响本轮审查结果）"
import os
import sqlite3
import sys

sid, repo = sys.argv[1], sys.argv[2]
db = os.path.join(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), "state.db")
con = sqlite3.connect(db, timeout=15)
con.execute("PRAGMA busy_timeout=8000")
journal = con.execute("PRAGMA journal_mode").fetchone()[0]
if str(journal).lower() != "wal":
    print(f"    注意：state.db 的 journal_mode={journal}（非 WAL），与运行中的 Hermes 并发写有小概率抢锁。")
row = con.execute("SELECT cwd, git_repo_root FROM sessions WHERE id = ?", (sid,)).fetchone()
if row is None:
    print(f"    session {sid} 不在 state.db 里，跳过归属检查。")
    sys.exit(0)
cwd, repo_root = (row[0] or ""), (row[1] or "")
if cwd == repo and repo_root == repo:
    sys.exit(0)
con.execute(
    "UPDATE sessions SET cwd = ?, git_repo_root = ?, git_metadata_generation = 0 WHERE id = ?",
    (repo, repo, sid),
)
con.commit()
after = con.execute("SELECT cwd, git_repo_root FROM sessions WHERE id = ?", (sid,)).fetchone()
if after and after[0] == repo and after[1] == repo:
    print(f"    reviewer session {sid} 已归位到 {repo}")
else:
    print(f"    归位写入未生效（回读得到 {after}），请手动核对。")
    sys.exit(1)
PY
}

# 兜底：剥掉 prompt 回显。reviewer 现在走 `hermes chat -Q`（quiet：只输出最终回答 + session_id），
# 正常情况下根本不回显 prompt；万一回显回来了（换 CLI、换 reviewer），prompt 末行的哨兵带本轮
# 随机 nonce，取「首次出现 nonce 之后」的内容即为模型回答。nonce 随机，diff/文档里不可能出现，
# 所以不会被被审内容伪造；找不到 nonce（quiet 输出、opencode）时按原样返回全文。
# 过滤器语义：从 stdin 读（与 sed 管道串联），不做参数解析。
strip_prompt_echo() {
  local nonce="${1:-}"
  awk -v nonce="$nonce" -v marker="nonce=$nonce" '
    { lines[NR] = $0 }
    found == 0 && nonce != "" && index($0, marker) > 0 { found = NR }
    END { for (i = found + 1; i <= NR; i++) print lines[i] }
  '
}

# 从 reviewer 输出里抽取一行字段（容忍 ANSI 转义、前导空行、模型寒暄）
extract_field() {
  local key="$1" file="$2"
  sed -e 's/\x1b\[[0-9;]*[a-zA-Z]//g' -e 's/\r$//' "$file" \
    | grep -m1 -E "^[[:space:]]*${key}:" \
    | sed -E "s/^[[:space:]]*${key}:[[:space:]]*//; s/[[:space:]]+$//"
}

# ----------------------------------------------------------------------------
# 1. 主循环
# ----------------------------------------------------------------------------
build_architecture_text
ATTEMPT=1

while [ "$ATTEMPT" -le "$MAX_ATTEMPTS" ]; do
  say ""
  say "================ 第 $ATTEMPT / $MAX_ATTEMPTS 轮 | 分支: $BRANCH ================"

  # ---- 1.1 builder 产出代码 -------------------------------------------------
  {
    # 任务书里的 <PYBIN> 占位符必须替换成实际路径。不替换时 builder 会自己猜解释器——
    # 实测它猜过 hermes 自带的 venv 和 conda base，还试过不存在的 .venv，都不是本项目环境。
    printf '# 本次任务\n\n%s\n\n' "${TASK//'<PYBIN>'/$PYBIN}"
    printf '===== 运行环境（必须使用；不要自建环境）=====\n'
    printf '项目解释器 = %s\n' "$PYBIN"
    printf '运行测试 = PYTHONPATH=src %s -m unittest discover -s tests\n' "$PYBIN"
    printf '不得改用 .venv/、hermes 自带 venv 或 conda base 的解释器（仓库里没有 .venv）。\n\n'
    if [ -f "$BUILDER_CONVENTIONS" ]; then
      printf '===== 项目协作约定（必须遵守）=====\n'
      cat "$BUILDER_CONVENTIONS"
      printf '\n'
    fi
    if [ "$ATTEMPT" -gt 1 ] && [ -f "$WORKDIR/last_review.txt" ]; then
      printf '===== 上一轮 reviewer 的审查意见（请据此修改）=====\n'
      cat "$WORKDIR/last_review.txt"
      printf '\n'
      if [ -s "$WORKDIR/last_tests.txt" ]; then
        printf '===== 上一轮的测试结果 =====\n'
        cat "$WORKDIR/last_tests.txt"
        printf '\n'
      fi
    fi
    printf '只修改工作区文件，不要执行 git commit / git push。\n'
  } > "$WORKDIR/builder_prompt.txt"

  say "--- builder ($BUILDER) 开工 ---"
  run_builder "$WORKDIR/builder_prompt.txt" "$WORKDIR/builder_out.txt"
  tail -25 "$WORKDIR/builder_out.txt" | sed 's/^/    /'

  if builder_looks_broken "$WORKDIR/builder_out.txt" "$WORKDIR/builder_raw.jsonl"; then
    warn "builder 报告配额/认证问题，无法继续。原始输出尾部："
    tail -6 "$WORKDIR/builder_out.txt" | sed 's/^/    /'
    # 「本轮没有可审查的产物」= 这一轮没提交任何东西，**不代表工作区干净**：
    # builder 可能已经改到一半，半成品留在工作区。把残留清单直接打出来，避免被误读成「什么都没做」。
    RESIDUE_FILES="$(git status --porcelain -- . | wc -l | tr -d ' ')"
    RESIDUE_NEW="$(git status --porcelain -- . | grep -c '^??' || true)"
    warn "本轮没有可审查的产物（未提交任何东西），但工作区可能有半成品："
    warn "    分支 $(git branch --show-current) · 改动条目 ${RESIDUE_FILES} 个（其中未跟踪 ${RESIDUE_NEW} 个）"
    git diff --stat | tail -3 | sed 's/^/    /'
    warn "不要在没有判定残留之前重跑或丢弃：先 git status --short / git diff --stat 看清残留，"
    warn "再按 docs/hermes-reviewer-kit.md「断点续跑准则」决定丢弃还是用同一份任务书续跑。"
    exit 2
  fi
  if [ "$BUILDER_RC" -ne 0 ]; then
    warn "builder 退出码非 0（${BUILDER_RC}），继续检查是否仍有改动。"
  fi

  # ---- 1.2 收集改动（含未跟踪的新文件）--------------------------------------
  git add -A -- . ':!'"$LOG_FILE" >/dev/null 2>&1
  git diff --cached > "$WORKDIR/diff.txt"
  git diff --cached --stat > "$WORKDIR/diffstat.txt"

  if [ ! -s "$WORKDIR/diff.txt" ]; then
    warn "没有检测到任何文件改动，builder 可能没有真正执行任务，判定为需要人工介入。"
    warn "builder 输出尾部："
    tail -10 "$WORKDIR/builder_out.txt" | sed 's/^/    /'
    exit 2
  fi
  say "--- 本轮改动 ---"
  sed 's/^/    /' "$WORKDIR/diffstat.txt"

  # ---- 1.3 测试门 ----------------------------------------------------------
  say "--- 验收测试 ---"
  run_tests "$WORKDIR/tests.txt"
  TEST_SNIPPET="$(tail -4 "$WORKDIR/tests.txt" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')"
  say "    [$TESTS_STATE] $TEST_SNIPPET"

  # ---- 1.4 组装 reviewer 输入 ---------------------------------------------
  ARCH_BYTES=$(wc -c < "$WORKDIR/arch.txt" | tr -d ' ')
  CAP_BYTES=$(( MAX_CONTEXT_KB * 1024 ))
  BUDGET=$(( CAP_BYTES - ARCH_BYTES - 16384 ))
  if [ "$BUDGET" -lt 32768 ]; then BUDGET=32768; fi

  # 改动清单单独给一份：即使 diff 内容被裁剪，reviewer 也能看到「改了哪些文件」的全貌。
  # （上一轮就是因为朴素截断，reviewer 看不到 core/discovery 有无改动，只能靠测试结果推断。）
  git diff --cached --name-status > "$WORKDIR/diffnames.txt"

  TRUNCATED="no"
  if [ -f "$CAP_DIFF" ]; then
    "$PYBIN" "$CAP_DIFF" "$WORKDIR/diff.txt" "$BUDGET" "$WORKDIR/diff_for_review.txt" \
      2> "$WORKDIR/cap_info.txt"
    grep -q "被裁剪" "$WORKDIR/cap_info.txt" && TRUNCATED="yes"
    say "    diff 预算：$(cat "$WORKDIR/cap_info.txt")"
  else
    cp "$WORKDIR/diff.txt" "$WORKDIR/diff_for_review.txt"
    warn "找不到 $CAP_DIFF，本轮 diff 未做按文件裁剪。"
  fi

  # 末行哨兵带一个本轮随机 nonce：reviewer 输出的 prompt 回显里它只可能出现一次，
  # 而 diff/文档内容不可能造出这个随机串，所以"首次出现 nonce 之后"就是模型回答的可靠起点。
  NONCE="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  {
    printf '===== 本次任务 =====\n%s\n\n' "$TASK"
    printf '===== 项目架构与阶段文档（审查依据）=====\n'
    cat "$WORKDIR/arch.txt"
    printf '\n===== 本轮全部改动文件清单（name-status，完整）=====\n'
    cat "$WORKDIR/diffnames.txt"
    printf '\n===== 本轮改动量统计（--stat，完整）=====\n'
    cat "$WORKDIR/diffstat.txt"
    printf '\n===== 本轮 git diff（含新增文件；若被裁剪，缺失清单见其末尾）=====\n'
    cat "$WORKDIR/diff_for_review.txt"
    printf '\n===== 本轮自动验收测试结果（命令: %s，结论: %s）=====\n' "$TEST_CMD" "$TESTS_STATE"
    cat "$WORKDIR/tests.txt"
    printf '\n===== 以上为全部输入 nonce=%s =====\n' "$NONCE"
  } > "$WORKDIR/context.txt"

  {
    cat <<'FMT'
审查下面这次改动。输入里依次是：本次任务、架构与阶段文档、本轮 git diff、自动验收测试结果。

【输出格式硬性要求 — 脚本按字面值解析，写错等于本轮审查作废并触发人工介入】
第 1 行：STATUS 后面只允许三选一，必须原样照抄下面三个字面值之一：
    STATUS: PASS
    STATUS: NEEDS_FIX
    STATUS: ESCALATE
  不允许使用 APPROVE / LGTM / OK / REJECT / BLOCK / 通过 / 拒绝 等任何同义写法。
第 2 行：COMMIT_MSG: 一句话描述这次改动做了什么（中文，≤50 字，用于 git commit message）
第 3 行：LOG_NOTE: 一句话说清这一轮做了什么、卡在哪、要不要人管（中文，面向人类浏览）
第 4 行起：空一行，再写详细意见。
FMT
    # 复用同一个会话会让上下文跨轮累积，这里显式要求只看本轮输入（防历史结论锚定这次判断）
    printf '注意：如果你能看到本会话更早的审查记录，一律忽略——只依据这一次输入里的任务、架构文档、\n'
    printf 'git diff 与测试结果判断，不要引用历史轮次的结论。\n'
    if [ "$TRUNCATED" = "yes" ]; then
      printf '注意：本轮 diff 因体积超限被截断，若关键上下文缺失请按你的规则明确指出缺少哪些文件。\n'
    fi
  } > "$WORKDIR/review_instruction.txt"

  # hermes -z 只接受命令行参数，把输入拼进 prompt
  {
    cat "$WORKDIR/review_instruction.txt"
    printf '\n\n'
    cat "$WORKDIR/context.txt"
  } > "$WORKDIR/review_prompt.txt"

  # ---- 1.5 reviewer 审查 ---------------------------------------------------
  say "--- reviewer ($REVIEWER) 审查 ---"
  if [ "$REVIEWER" = "hermes" ]; then
    say "    session: ${REVIEWER_SESSION}（复用具名会话；上下文过长时由 Hermes 自动压缩）"
  fi
  run_reviewer "$WORKDIR/review_prompt.txt" "$WORKDIR/review_raw.txt"
  REVIEW_RC=$?

  # 回显出现、却没有本轮 nonce 边界 = 无法可靠定位模型回答。宁可停下，也不要让 extract_field
  # 去命中回显里的格式说明（那会把"本轮作废"伪装成一个看似正常的结论）。
  if grep -qE '^Query: ' "$WORKDIR/review_raw.txt" && ! grep -q "nonce=${NONCE}" "$WORKDIR/review_raw.txt"; then
    warn "reviewer 输出带 prompt 回显，但找不到本轮 nonce 边界（疑似 CLI 输出格式变了）。"
    warn "原始输出：$WORKDIR/review_raw.txt"
    exit 2
  fi

  # 去掉 ANSI 噪声与 prompt 回显，只留模型回答本体（便于人工阅读，
  # 也避免把整份 prompt 当"审查意见"回灌给 builder）
  sed -e 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$WORKDIR/review_raw.txt" \
    | strip_prompt_echo "$NONCE" > "$WORKDIR/last_review.txt"

  # -Q 输出里带一行 session_id：按 id 补归属比按标题猜会话更准
  file_reviewer_session "$(extract_field session_id "$WORKDIR/last_review.txt")"

  STATUS="$(extract_field STATUS "$WORKDIR/last_review.txt")"
  COMMIT_MSG="$(extract_field COMMIT_MSG "$WORKDIR/last_review.txt")"
  LOG_NOTE="$(extract_field LOG_NOTE "$WORKDIR/last_review.txt")"

  case "$STATUS" in
    PASS|NEEDS_FIX|ESCALATE) ;;
    *) STATUS="" ;;
  esac

  if [ -z "$STATUS" ]; then
    warn "reviewer 没有输出可识别的 STATUS 行（退出码 ${REVIEW_RC}）。输出尾部："
    tail -15 "$WORKDIR/last_review.txt" | sed 's/^/    /'
    warn "为安全起见按需要人工介入处理。改动仍在工作区，未提交。"
    exit 2
  fi

  say "    STATUS = $STATUS"

  # 测试门与 reviewer 结论冲突时，以更保守的一方为准
  GATE_NOTE=""
  if [ "$STATUS" = "PASS" ] && [ "$TESTS_STATE" = "FAIL" ]; then
    GATE_NOTE="（测试门未通过，自动将 PASS 降级为 NEEDS_FIX）"
    STATUS="NEEDS_FIX"
    warn "reviewer 判定 PASS，但自动验收测试未通过 → 降级为 NEEDS_FIX。"
  fi

  [ -n "$COMMIT_MSG" ] || COMMIT_MSG="第${ATTEMPT}轮改动（reviewer 未提供 commit message）"
  [ -n "$LOG_NOTE" ] || LOG_NOTE="reviewer 未提供日志说明"

  say ""
  cat "$WORKDIR/last_review.txt"
  say ""

  # ---- 1.6 落盘：日志 + 提交 + 推送 ---------------------------------------
  TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '| %s | 第 %s 轮 | %s%s | %s | %s |\n' \
    "$TIMESTAMP" "$ATTEMPT" "$STATUS" "$GATE_NOTE" "$TESTS_STATE" "$LOG_NOTE" >> "$LOG_FILE"
  git add -A

  if [ "${DRY_RUN:-0}" = "1" ]; then
    say "--- DRY_RUN=1：跳过 commit / push（改动留在工作区）---"
  else
    case "$STATUS" in
      PASS)     git commit -q -m "$COMMIT_MSG" ;;
      ESCALATE) git commit -q -m "[需人工确认] $COMMIT_MSG" ;;
      *)        git commit -q -m "[第${ATTEMPT}轮/待修改] $COMMIT_MSG" ;;
    esac
    if ! git push -u origin "$BRANCH" 2>&1 | sed 's/^/    /'; then
      warn "git push 失败（远端未配置或未认证）。本轮改动已在本地提交，未推送。"
      warn "修好远端后执行：git push -u origin $BRANCH"
      exit 2
    fi
    say "    已提交并推送到 origin/$BRANCH"
  fi

  # ---- 1.7 决定下一步 ------------------------------------------------------
  case "$STATUS" in
    "PASS")
      say "✅ 通过（测试: ${TESTS_STATE}）。"
      exit 0
      ;;
    "ESCALATE")
      warn "reviewer 请求人工介入（ESCALATE），已停止循环。"
      exit 2
      ;;
    "NEEDS_FIX")
      if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
        break
      fi
      say "→ 需要修改，进入第 $((ATTEMPT+1)) 轮。"
      cp "$WORKDIR/tests.txt" "$WORKDIR/last_tests.txt"
      ATTEMPT=$((ATTEMPT+1))
      ;;
  esac
done

warn "已达到最大重试次数（$MAX_ATTEMPTS 次）仍未通过，请人工介入。"
exit 2
