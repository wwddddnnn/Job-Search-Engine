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
# 可选环境变量（都有默认值）:
#   BUILDER=codex|hermes        builder 实现，默认 codex
#   REVIEWER=hermes|opencode    reviewer 实现，默认 hermes
#   MAX_ATTEMPTS=3              最大循环轮数
#   ARCH_DOCS="a.md b.md"       喂给 reviewer 的架构/阶段文档（空格分隔）
#   TEST_CMD="..."              每轮验收测试命令
#   MAX_CONTEXT_KB=150          reviewer 输入上限，超出则截断 diff
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
MAX_CONTEXT_KB="${MAX_CONTEXT_KB:-150}"
LOG_FILE="DEVELOPMENT_LOG.md"
REVIEWER_PROMPT=".opencode/prompts/reviewer.md"
BUILDER_CONVENTIONS="docs/builder-conventions.md"
CODEX_APP_BIN="/Applications/ChatGPT.app/Contents/Resources/codex"

DEFAULT_ARCH_DOCS=(
  "docs/Job Search Assistant 技术架构文档.md"
  "docs/phase0-foundation.md"
  "docs/phase1-job-discovery.md"
)
DEFAULT_TEST_CMD='PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v'

if [ -n "${ARCH_DOCS:-}" ]; then
  read -r -a ARCH_FILES <<< "$ARCH_DOCS"
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
  [ -f "$f" ] || die "缺少架构文档：$f（可用 ARCH_DOCS 覆盖）"
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
else
  die "未知 BUILDER=$BUILDER（支持 codex|hermes）"
fi

# reviewer 可执行体
if [ "$REVIEWER" = "hermes" ]; then
  command -v hermes >/dev/null 2>&1 || die "找不到 hermes CLI。"
elif [ "$REVIEWER" = "opencode" ]; then
  command -v opencode >/dev/null 2>&1 || die "找不到 opencode CLI。"
else
  die "未知 REVIEWER=$REVIEWER（支持 hermes|opencode）"
fi

if [ ! -f "$LOG_FILE" ]; then
  {
    printf '# 开发日志\n\n'
    printf '每轮由 `codex-hermes-loop.sh` 自动追加：builder 产出 → reviewer 审查 → 提交推送。\n\n'
    printf '| 时间 | 轮次 | 审查结论 | 测试 | 说明 |\n|---|---|---|---|---|\n'
  } > "$LOG_FILE"
fi

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
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

run_builder() {
  local prompt_file="$1" out_file="$2"
  if [ "$BUILDER" = "codex" ]; then
    "$CODEX_BIN" exec --sandbox workspace-write "$(cat "$prompt_file")" > "$out_file" 2>&1
  else
    hermes -z -t coding --in "$REPO_ROOT" "$(cat "$prompt_file")" > "$out_file" 2>&1
  fi
}

builder_looks_broken() {
  # codex 配额用尽/认证失败时会退出码 0 但打印这类信息，必须显式识别
  grep -qiE "hit your usage limit|usage limit reached|insufficient_quota|401 Unauthorized|not logged in|please run .*login" "$1"
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
    hermes -z -t vision --ignore-rules --in "$REPO_ROOT" "$(cat "$prompt_file")" > "$out_file" 2>&1
  else
    opencode run --agent reviewer --file "$WORKDIR/context.txt" \
      "$(cat "$WORKDIR/review_instruction.txt")" > "$out_file" 2>&1
  fi
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
    printf '# 本次任务\n\n%s\n\n' "$TASK"
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
  BUILDER_RC=$?
  tail -25 "$WORKDIR/builder_out.txt" | sed 's/^/    /'

  if builder_looks_broken "$WORKDIR/builder_out.txt"; then
    warn "builder 报告配额/认证问题，无法继续。原始输出尾部："
    tail -6 "$WORKDIR/builder_out.txt" | sed 's/^/    /'
    warn "本轮未产生可审查的改动，请人工处理（换 builder 或补额度）后重跑。"
    exit 2
  fi
  if [ "$BUILDER_RC" -ne 0 ]; then
    warn "builder 退出码非 0（$BUILDER_RC），继续检查是否仍有改动。"
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
  DIFF_BYTES=$(wc -c < "$WORKDIR/diff.txt" | tr -d ' ')
  CAP_BYTES=$(( MAX_CONTEXT_KB * 1024 ))
  BUDGET=$(( CAP_BYTES - ARCH_BYTES - 8192 ))

  cp "$WORKDIR/diff.txt" "$WORKDIR/diff_for_review.txt"
  TRUNCATED="no"
  if [ "$DIFF_BYTES" -gt "$BUDGET" ]; then
    TRUNCATED="yes"
    head -c "$BUDGET" "$WORKDIR/diff.txt" > "$WORKDIR/diff_for_review.txt"
    printf '\n\n[... diff 超出 %sKB 预算被截断，只提交了前 %s 字节；如需完整改动请在本地查看 ...]\n' \
      "$MAX_CONTEXT_KB" "$BUDGET" >> "$WORKDIR/diff_for_review.txt"
  fi

  {
    printf '===== 本次任务 =====\n%s\n\n' "$TASK"
    printf '===== 项目架构与阶段文档（审查依据）=====\n'
    cat "$WORKDIR/arch.txt"
    printf '\n===== 本轮 git diff（含新增文件）=====\n'
    cat "$WORKDIR/diff_for_review.txt"
    printf '\n===== 本轮自动验收测试结果（命令: %s，结论: %s）=====\n' "$TEST_CMD" "$TESTS_STATE"
    cat "$WORKDIR/tests.txt"
    printf '\n===== 以上为全部输入 =====\n'
  } > "$WORKDIR/context.txt"

  {
    printf '审查下面这次改动。输入文件里依次是：本次任务、架构与阶段文档、本轮 git diff、自动验收测试结果。\n'
    printf '严格按你的 system prompt 输出格式作答：前三行必须是 STATUS / COMMIT_MSG / LOG_NOTE，然后另起一段写详细意见。\n'
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
  run_reviewer "$WORKDIR/review_prompt.txt" "$WORKDIR/review_raw.txt"
  REVIEW_RC=$?

  # 去掉可能的前导噪声行，便于人工阅读
  sed -e 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$WORKDIR/review_raw.txt" > "$WORKDIR/last_review.txt"

  STATUS="$(extract_field STATUS "$WORKDIR/last_review.txt")"
  COMMIT_MSG="$(extract_field COMMIT_MSG "$WORKDIR/last_review.txt")"
  LOG_NOTE="$(extract_field LOG_NOTE "$WORKDIR/last_review.txt")"

  case "$STATUS" in
    PASS|NEEDS_FIX|ESCALATE) ;;
    *) STATUS="" ;;
  esac

  if [ -z "$STATUS" ]; then
    warn "reviewer 没有输出可识别的 STATUS 行（退出码 $REVIEW_RC）。输出尾部："
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
      say "✅ 通过（测试: $TESTS_STATE）。"
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
