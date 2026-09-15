#!/usr/bin/env python3
"""按字节预算裁剪 git diff，**保证 reviewer 至少知道改了哪些文件**。

问题背景：把架构文档 + 整份 diff 拼成 reviewer 的输入时，diff 可能超出上限。
朴素的 `head -c` 会从中间切断，导致 reviewer 看不到后面的文件，
只能靠「测试全绿」推断——审查结论的可靠性因此下降（真实发生过）。

本脚本的策略：
1. **按文件切块**（`diff --git` 为界），整文件包含，不做文件内截断；
2. 预算不够时，尽量把当前这个文件包含完；再放不下就只放它的头部并标记；
3. 剩余文件的 diff **不包含**，但把它们的路径**完整列出来**（放在输出尾部 + stderr），
   这样 reviewer 即使看不到原文，也能准确知道哪些文件没看到、可以主动要求补。

用法:
    cap-diff.py <输入diff> <预算字节> <输出文件>
退出码: 0 正常；1 参数错误
"""

from __future__ import annotations

import sys

MARK = "diff --git "


def split_chunks(text: str) -> list[tuple[str, str]]:
    """切分成 [(文件路径, 整块文本)]，保留非 diff 前言。"""
    lines = text.splitlines(keepends=True)
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    path = "(前言)"
    for line in lines:
        if line.startswith(MARK):
            if buf:
                chunks.append((path, "".join(buf)))
            buf = [line]
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else line[len(MARK):].strip()
        else:
            buf.append(line)
    if buf:
        chunks.append((path, "".join(buf)))
    return chunks


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 1
    src, budget_raw, dst = argv[1], argv[2], argv[3]
    budget = int(budget_raw)

    text = open(src, encoding="utf-8", errors="replace").read()
    chunks = split_chunks(text)
    total = len(text.encode("utf-8", errors="replace"))

    if total <= budget:
        open(dst, "w", encoding="utf-8").write(text)
        print(f"diff 完整：{total} 字节 / {len(chunks)} 个文件块（预算 {budget}）", file=sys.stderr)
        return 0

    out: list[str] = []
    used = 0
    omitted: list[str] = []
    truncated_within: str | None = None

    for path, chunk in chunks:
        size = len(chunk.encode("utf-8", errors="replace"))
        if used + size <= budget:
            out.append(chunk)
            used += size
            continue
        if not truncated_within and not omitted:
            # 本文件放不下完整内容：放多少算多少，并明确标记
            room = max(budget - used, 0)
            head = chunk.encode("utf-8", errors="replace")[:room].decode("utf-8", errors="ignore")
            out.append(head)
            out.append(
                f"\n[... 本文件 diff 在此处被截断（预算 {budget} 字节已用尽），"
                f"以下内容未包含 ...]\n"
            )
            truncated_within = path
            used = budget
            continue
        omitted.append(path)

    tail = ""
    if omitted or truncated_within:
        names = ([truncated_within] if truncated_within else []) + omitted
        tail = (
            "\n\n===== 以下文件的 diff 未包含在本次输入中（文件列表是完整的，内容缺失）=====\n"
            + "\n".join(f"  - {n}" for n in names)
            + "\n如果你需要其中某个文件的原文才能作出判断，请按你的规则明确指出缺少哪些文件。\n"
        )
        print("\n".join(names), file=sys.stderr)

    open(dst, "w", encoding="utf-8").write("".join(out) + tail)
    print(
        f"diff 超预算被裁剪：原始 {total} 字节 → 输出 {used} 字节（预算 {budget}）；"
        f"整块保留 {len(out) // 2} 个文件，未包含 {len(omitted) + (1 if truncated_within else 0)} 个文件",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
