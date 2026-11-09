#!/usr/bin/env python3
"""数一条会话压缩过几次,并列出每次压缩前后的 tokens。

为什么不能直接搜 compactMetadata 的行数:转录会重放旧的压缩边界,行数可能多于真实次数。
为什么不能只看当前转录:回退重发会 fork 出一份新转录,早期的压缩边界可能留在旧文件里。

做法:在同一目录的 *.jsonl 里,找出和当前转录共享消息 id 的文件(fork 会复制一段历史,
所以有共享;全新的会话没有),把这些文件里的压缩边界按消息 id 去重。

用法:python3 count-compactions.py <transcript.jsonl>
转录格式不是公开接口,会随 Claude Code 版本变化。
"""
import glob
import json
import os
import sys


def records(path):
    """逐行读 JSON;写到一半或损坏的行跳过。"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def message_ids(path):
    return {r["uuid"] for r in records(path) if r.get("uuid")}


def main():
    if len(sys.argv) != 2:
        print("用法: count-compactions.py <transcript.jsonl>", file=sys.stderr)
        return 2
    cur = os.path.abspath(sys.argv[1])
    if not os.path.isfile(cur):
        print(f"✗ 找不到转录: {cur}", file=sys.stderr)
        return 2
    d = os.path.dirname(cur)
    cache = {p: message_ids(p) for p in glob.glob(os.path.join(d, "*.jsonl"))}
    cache.setdefault(cur, message_ids(cur))
    chain, grew = {cur}, True
    while grew:  # 传递闭包:fork 的 fork 也串得上
        grew = False
        for p, ids in cache.items():
            if p not in chain and any(ids & cache[c] for c in chain):
                chain.add(p)
                grew = True
    seen = {}
    for p in sorted(chain):
        before = len(seen)
        for r in records(p):
            cm = r.get("compactMetadata")
            if not cm:
                continue
            ts = r.get("timestamp", "")[:19]
            key = r.get("uuid") or (ts, cm.get("preTokens"))
            seen[key] = (ts, cm.get("preTokens"), cm.get("postTokens"), cm.get("trigger"))
        print(f"  {os.path.basename(p)}  新增 {len(seen) - before}")
    print(f"\n同链转录 {len(chain)} 份(按共享消息 id 串联)")
    for ts, pre, post, trigger in sorted(seen.values(), key=lambda x: x[0]):
        print(f"   {ts}  {pre} → {post}  [{trigger}]")
    print(f"\n压缩次数 = {len(seen)}")
    if len(chain) > 1:
        print("  注意:这条会话换过转录(回退重发 / fork),只数当前转录会漏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
