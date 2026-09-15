#!/usr/bin/env python3
"""把 `codex exec --json` 的 JSONL 事件流渲染成人能读的实时行。

用途：codex-hermes-loop.sh 在 BUILDER=codex 时：
    codex exec --json ... 2>&1 | tee raw.jsonl | <本脚本> | tee builder_out.txt
让 Codex 的执行过程实时出现在终端面板，同时保留原始 JSONL 与可读日志。

零成本：纯本地解析，不调用 LLM、不发网络请求。

本机实测的事件形状（codex-cli 0.154.0-alpha.6.2）：
  response_item / reasoning                → 🧠 思考
  response_item / message                  → 💬 结论
  response_item / custom_tool_call         → 🔧 工具调用；input 是 JS，形如
                                              tools.exec_command({"cmd":"...","workdir":"..."})
  response_item / custom_tool_call_output  → ↩️  结果摘要（截断）
  event_msg / item_completed, token_count  → 噪声，丢弃
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

VERBOSE = os.environ.get("STREAM_VERBOSE") == "1"
MAX_TEXT = int(os.environ.get("STREAM_MAX_TEXT", "200"))
SHOW_OUTPUT = os.environ.get("STREAM_SHOW_OUTPUT", "1") == "1"

# 已知噪声事件类型：量大且与「在干什么」无关
NOISE = {
    "token_count",
    "token_usage",
    "item_completed",
    "item_started",
    "turn_context",
    "session_configured",
    "session_meta",
    "world_state",
    "task_started",
    "user_message",
    "input_text",
    "token_usage_record",
}

_TOOL_CALL_RE = re.compile(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CMD_RE = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PATH_RE = re.compile(r'"(?:path|file|filename)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def _emit(icon: str, text: str, event: str = "") -> None:
    if not text:
        return
    suffix = f"  [{event}]" if VERBOSE and event else ""
    print(f"[{_ts()}] {icon} {_clip(text)}{suffix}", flush=True)


def _unescape(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text_of(i) for i in content if i)
    if isinstance(content, dict):
        for key in ("text", "content", "message"):
            if content.get(key):
                return _text_of(content[key])
        return ""
    return ""


def _describe_tool_call(payload: dict) -> str:
    """从 custom_tool_call 的 JS input 里抽出工具名与命令/路径。"""
    name = str(payload.get("name") or "tool")
    raw_input = payload.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        return f"{name}"

    tools_used = _TOOL_CALL_RE.findall(raw_input)
    cmds = [_unescape(c) for c in _CMD_RE.findall(raw_input)]
    paths = [_unescape(p) for p in _PATH_RE.findall(raw_input)]

    head = f"{name} → {', '.join(dict.fromkeys(tools_used))}" if tools_used else name
    detail = ""
    if cmds:
        detail = _clip(cmds[0], 140)
        if len(cmds) > 1:
            detail += f"   (+{len(cmds) - 1} more)"
    elif paths:
        detail = _clip(" ".join(dict.fromkeys(paths)), 140)
    return f"{head}: {detail}" if detail else head


def handle(obj: dict) -> None:
    etype = str(obj.get("type") or "")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = obj
    ptype = str(payload.get("type") or "")

    if ptype in NOISE and etype != "response_item":
        return

    if etype == "response_item":
        if ptype == "reasoning":
            _emit("🧠", _text_of(payload.get("content")) or payload.get("text") or "", ptype)
            return
        if ptype == "message":
            _emit("💬", _text_of(payload.get("content")) or "", ptype)
            return
        if ptype in ("custom_tool_call", "function_call"):
            _emit("🔧", _describe_tool_call(payload), ptype)
            return
        if ptype in ("custom_tool_call_output", "function_call_output"):
            if SHOW_OUTPUT:
                out = _text_of(payload.get("output")) or ""
                # 结果里最有用的是首行状态与输出开头
                out = re.sub(r"^Script completed\s*", "", out)
                _emit("↩️ ", out, ptype)
            return

    if etype == "event_msg":
        if ptype in ("agent_message", "agent_message_delta"):
            _emit("💬", payload.get("message") or payload.get("text") or "", ptype)
            return
        if ptype in ("agent_reasoning", "agent_reasoning_delta"):
            _emit("🧠", payload.get("text") or "", ptype)
            return
        if ptype in ("exec_command_begin", "shell_command_begin"):
            cmd = payload.get("command") or payload.get("cmd") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(c) for c in cmd)
            _emit("⚡", cmd, ptype)
            return
        if ptype in ("error", "stream_error"):
            _emit("❗", payload.get("message") or payload.get("error") or "", ptype)
            return

    if VERBOSE and etype:
        label = f"{etype}/{ptype}" if ptype else etype
        _emit("·", json.dumps(payload, ensure_ascii=False)[:400], label)


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            print(f"[{_ts()}] · {line}", flush=True)   # 非 JSON 原样透传，别吞警告
            continue
        if not isinstance(obj, dict):
            continue
        try:
            handle(obj)
        except Exception as exc:                        # 单行失败绝不影响整条流
            print(f"[{_ts()}] · [filter-error] {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
