#!/usr/bin/env python3
"""把 `codex exec --json` 的 JSONL 事件流渲染成人能读的实时行。

用途：codex-hermes-loop.sh 在 BUILDER=codex 时：
    codex exec --json ... 2>&1 | tee raw.jsonl | <本脚本> | tee builder_out.txt
让 Codex 的执行过程实时出现在终端面板，同时保留原始 JSONL 与可读日志。

零成本：纯本地解析，不调用 LLM、不发网络请求。

支持两种封装（都经实跑核对）：

A) `codex exec --json` 的 stdout（扁平点号类型，item 在顶层）——**主路径**
   {"type":"thread.started", ...}
   {"type":"turn.started"}
   {"type":"item.started"|"item.completed"|"item.updated",
    "item":{"id":..,"type":"command_execution",
            "command":"/bin/zsh -lc pwd","exit_code":0,
            "status":"completed","aggregated_output":"..."}}
   {"type":"item.completed","item":{"type":"agent_message","text":"OK"}}
   {"type":"turn.completed","usage":{input_tokens,cached_input_tokens,output_tokens,...}}

B) 会话轨迹文件（event_msg/payload + PascalCase item）——兜底，便于直接喂轨迹排查
   {"type":"event_msg","payload":{"type":"item_completed","item":{"type":"CommandExecution",...}}}
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from datetime import datetime

VERBOSE = os.environ.get("STREAM_VERBOSE") == "1"
MAX_TEXT = int(os.environ.get("STREAM_MAX_TEXT", "160"))
SHOW_STARTED = os.environ.get("STREAM_SHOW_STARTED", "0") == "1"

_SHELL_WRAPPER_RE = re.compile(r"^\S*(?:bash|zsh|sh)\s+-l?c\s+")
_CHANGE_RE = re.compile(r"^([AMD])\s+(\S+)\s*$", re.M)
_TOOL_CALL_RE = re.compile(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CMD_RE = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PATH_RE = re.compile(r'"(?:path|file|filename)"\s*:\s*"((?:[^"\\]|\\.)*)"')

_LAST_KEY = ""


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def _emit(icon: str, text: str, event: str = "") -> None:
    """打印一行。连续重复内容（同一句在两层封装里各来一次）只留一次。"""
    global _LAST_KEY
    if not text:
        return
    key = re.sub(r"^\((?:commentary|final|final_answer|analysis)\)\s*", "", " ".join(str(text).split()))
    if key and key == _LAST_KEY:
        return
    _LAST_KEY = key
    suffix = f"  [{event}]" if VERBOSE and event else ""
    print(f"[{_ts()}] {icon} {_clip(text)}{suffix}", flush=True)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x for x in (_text_of(i) for i in content) if x)
    if isinstance(content, dict):
        for key in ("text", "content", "message", "summary_text"):
            if content.get(key):
                return _text_of(content[key])
    return ""


def _unescape(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw


def _strip_output_preamble(text: str) -> str:
    """去掉 'Script completed' / 'Wall time 0.3 seconds' / 'Output:' 之类包装，
    把截断配额留给真正的内容。"""
    text = re.sub(r"^(?:Script completed|Exit code:\s*\d+)\s*", "", text.strip())
    text = re.sub(r"Wall time [\d.]+ seconds\s*", "", text)
    text = re.sub(r"^Output:\s*", "", text.strip())
    return text.strip()


def _clean_command(cmd) -> str:
    """命令可能是字符串（'/bin/zsh -lc pwd'）、列表或 python-repr 字符串。"""
    if isinstance(cmd, list):
        cmd = str(cmd[-1]) if cmd else ""
    elif not isinstance(cmd, str):
        cmd = str(cmd)
    raw = cmd.strip()
    if raw.startswith("["):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list) and parsed:
                raw = str(parsed[-1])
        except Exception:
            pass
    return _SHELL_WRAPPER_RE.sub("", raw).strip()   # 去掉 /bin/zsh -lc 包装


def _file_summary(item: dict) -> str:
    changes = _CHANGE_RE.findall(str(item.get("stdout") or ""))
    if changes:
        parts = [f"{flag} {os.path.basename(path)}" for flag, path in changes[:8]]
        if len(changes) > 8:
            parts.append(f"(+{len(changes) - 8} more)")
        return ", ".join(parts)
    paths = re.findall(r"/\S+\.\w+", str(item.get("changes") or ""))
    return ", ".join(os.path.basename(p) for p in list(dict.fromkeys(paths))[:8])


def _render_item(item: dict) -> None:
    """渲染一个 item —— 同时吃 snake_case（--json stdout）与 PascalCase（轨迹文件）。"""
    kind = str(item.get("type") or "")

    if kind in ("command_execution", "CommandExecution"):
        exit_code = item.get("exit_code")
        mark = f"[exit {exit_code}] " if exit_code is not None else ""
        _emit("⚡", mark + _clean_command(item.get("command")), "item")
        if exit_code not in (None, 0, "0"):
            detail = (
                item.get("aggregated_output")
                or item.get("stderr")
                or item.get("stdout")
                or item.get("formatted_output")
                or ""
            )
            first = _strip_output_preamble(str(detail)).splitlines()
            _emit("❗", f"exit {exit_code}: {first[0] if first else '(无输出)'}", "item")
        return

    if kind in ("file_change", "FileChange"):
        _emit("✏️ ", _file_summary(item) or "file change", "item")
        return

    if kind in ("agent_message", "AgentMessage"):
        text = _text_of(item.get("content")) or str(item.get("text") or "")
        phase = item.get("phase")
        if phase and phase not in ("final", "final_answer"):
            text = f"({phase}) {text}"
        _emit("💬", text, "item")
        return

    if kind in ("reasoning", "Reasoning"):
        text = str(item.get("text") or item.get("summary_text") or "").strip() or _text_of(item.get("content"))
        if text and text not in ("[]", "[ ]"):
            try:
                parts = json.loads(text)
                if isinstance(parts, list):
                    text = " ".join(str(p) for p in parts)
            except Exception:
                pass
            _emit("🧠", text, "item")
        return

    if kind in ("error", "Error"):
        _emit("❗", _text_of(item.get("message")) or _text_of(item.get("text")) or str(item), "item")
        return

    if VERBOSE and kind:
        _emit("·", json.dumps(item, ensure_ascii=False)[:400], "item")


def _describe_js_tool_call(payload: dict) -> str:
    """兜底：response_item/custom_tool_call 的 input 是一段 JS。"""
    name = str(payload.get("name") or "tool")
    raw = payload.get("input")
    if not isinstance(raw, str) or not raw.strip():
        return name
    tools_used = _TOOL_CALL_RE.findall(raw)
    head = f"{name} → {', '.join(dict.fromkeys(tools_used))}" if tools_used else name
    cmds = [_unescape(c) for c in _CMD_RE.findall(raw)]
    paths = [_unescape(p) for p in _PATH_RE.findall(raw)]
    if cmds:
        detail = _clip(cmds[0], 130) + (f"  (+{len(cmds) - 1} more)" if len(cmds) > 1 else "")
    elif paths:
        detail = _clip(" ".join(dict.fromkeys(paths)), 130)
    else:
        detail = ""
    return f"{head}: {detail}" if detail else head


def handle(obj: dict) -> None:
    etype = str(obj.get("type") or "")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = obj
    ptype = str(payload.get("type") or "")

    # ---- A) `codex exec --json` 的扁平封装（主路径）--------------------------
    if etype in ("item.started", "item.updated", "item.completed"):
        item = obj.get("item")
        if isinstance(item, dict):
            if etype == "item.started" and not SHOW_STARTED:
                return                      # 只关心结果，避免同一命令打两遍
            _render_item(item)
        return

    if etype == "turn.completed":
        usage = obj.get("usage") or {}
        if usage:
            _emit(
                "📊",
                f"tokens in={usage.get('input_tokens')} "
                f"(cached={usage.get('cached_input_tokens')}) "
                f"out={usage.get('output_tokens')}",
                "usage",
            )
        return

    if etype in ("thread.started", "turn.started", "turn.failed"):
        if VERBOSE:
            _emit("·", json.dumps(obj, ensure_ascii=False)[:200], etype)
        return

    if etype == "error":
        _emit("❗", obj.get("message") or json.dumps(obj, ensure_ascii=False)[:200], etype)
        return

    # ---- B) 轨迹文件封装（event_msg/payload）------------------------------
    if etype == "event_msg" and ptype in ("item_completed", "item_started", "item_updated"):
        item = payload.get("item")
        if isinstance(item, dict):
            if ptype == "item_started" and not SHOW_STARTED:
                return
            _render_item(item)
        return

    if etype == "response_item":
        if ptype == "message":
            _emit("💬", _text_of(payload.get("content")), ptype)
            return
        if ptype == "reasoning":
            _emit("🧠", _text_of(payload.get("content")), ptype)
            return
        if ptype in ("custom_tool_call", "function_call"):
            _emit("🔧", _describe_js_tool_call(payload), ptype)
            return
        if ptype in ("custom_tool_call_output", "function_call_output"):
            _emit("↩️ ", _strip_output_preamble(_text_of(payload.get("output"))), ptype)
            return
        return

    if etype == "event_msg":
        if ptype in ("agent_message", "agent_message_delta"):
            _emit("💬", payload.get("message") or payload.get("text") or "", ptype)
            return
        if ptype in ("agent_reasoning", "agent_reasoning_delta"):
            _emit("🧠", payload.get("text") or "", ptype)
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
