#!/usr/bin/env python3
"""压后对账:拿转录里的压缩摘要,逐条核对检查点里要保留的条目,列出丢了哪条。

做法:
  - 从检查点取 🔴 原话 / ⚠ 未决 / 🚨 警告 / 🧠 被否选项 / 🛰 在跑的子代理 五段,
    每段逐条(编号行、列表行、表格行);
  - 每条抽「锚点」:反引号里的串、路径、文件名、提交号、日期、3 位以上的数、
    标识符、「」里引文的前 8 个字。摘要可能换一种语言或换一种说法,锚点更容易原样活下来,
    所以只比锚点;
  - 一条的锚点在摘要里找到超过一半 = 在;找到一部分 = 残;一个都没有 = 丢;
  - 抽不出锚点的条目比原文片段:超过一半对得上 = 在,否则 = 判不了(人看,不算核过);
  - 摘要没保住、但压缩边界原样保留的那几条消息里有的,单列为「只在保留段」,不算丢。

用法:
  python3 compare-summary.py <transcript.jsonl> <checkpoint.md> [--nth -1]
  --nth  取第几次压缩(默认 -1 = 最近一次,-2 = 上一次)
退出码:0 = 没有丢或残(「判不了」的不算核过);1 = 有丢或残;2 = 用法或输入错误。

限定:「在」只说明标识符活着,不保证意思没走样;零丢失也不证明检查点本身写对了。
只读这一份转录;转录格式不是公开接口,会随 Claude Code 版本变化。
"""
import json
import re
import sys

SECTIONS = [("🔴", "原话"), ("⚠", "未决"), ("🚨", "警告"), ("🧠", "被否选项"), ("🛰", "在跑的子代理")]


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            out.append(json.dumps(b.get("input", {}), ensure_ascii=False))
        elif b.get("type") == "tool_result":
            out.append(text_of(b.get("content")))
    return "\n".join(out)


def load(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                recs.append(r)
    return recs


def split_sections(md):
    """按 `## <emoji>` 切段,返回 {emoji: [条目文本]}。"""
    out, cur = {}, None
    for line in md.splitlines():
        if line.startswith("## "):
            cur = next((e for e, _ in SECTIONS if line[3:].lstrip().startswith(e)), None)
            if cur:
                out.setdefault(cur, [])
            continue
        if cur is None:
            continue
        s = line.strip()
        if not s:
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if cells and cells[0] in ("事项", "派出时间"):
                continue  # 表头
            out[cur].append(s)
        elif re.match(r"^(\d+[.、)](?!\d)\s*|[-*·]\s+)", s):
            out[cur].append(s)
        elif out[cur]:
            out[cur][-1] += " " + s  # 续行
    return out


ANCHOR_RES = [
    r"`([^`]{3,})`",                                   # 反引号
    r"((?:~|/)?[\w.\-]+/[\w.\-/]+)",                   # 路径
    r"\b([\w\-]+\.(?:md|py|js|ts|tsx|json|html|sh|jsonl))\b",  # 文件名
    r"\b([0-9a-f]{7,12})\b",                           # 提交号
    r"\b(\d{1,2}-\d{2}(?: \d{1,2}:\d{2})?)\b",         # 日期 / 时刻
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{3,})(?![\d])",  # 3 位以上的数
    r"#(\d{3,})",                                      # 编号
    r"\b([A-Za-z][A-Za-z0-9]*(?:[_\-][A-Za-z0-9]+)+)\b",  # snake_case / kebab-case
    r"\b([a-z]+[A-Z][A-Za-z0-9]+)\b",                  # camelCase
    r"\b([A-Z][a-z]{3,}[A-Za-z0-9]*)\b",               # 专名
    r"(\d+(?:\.\d+)?%)",                               # 百分比
]


def anchors(item):
    found = []
    for rx in ANCHOR_RES:
        for m in re.finditer(rx, item):
            a = m.group(1).strip()
            if len(a) >= 3 and a not in found:
                found.append(a)
    for q in re.findall(r"「([^」]{4,})」", item):  # 引文取前 8 个字,摘要常截断长引文
        a = re.sub(r"\s+", "", q)[:8]
        if a not in found:
            found.append(a)
    # 去掉被更长锚点包含的短锚点,免得一条路径算成好几个
    return [a for a in found if not any(a != b and a in b for b in found)]


def text_anchors(item):
    """抽不出标识符的条目(常见于全中文内容),退一步取原文片段:最长的几段,每段前 8 个字。"""
    s = re.sub(r"^(\d+[.、)]\s*|[-*·]\s+)", "", item.strip())
    parts = [p for p in re.split(r"[,,。;;::、()()|「」\s]+", s) if len(p) >= 4]
    parts.sort(key=len, reverse=True)
    return [p[:8] for p in parts[:4]]


def norm(s):
    return re.sub(r"\s+", "", s).replace("\\n", "")


def preserved_text(recs, summ):
    """沿 parentUuid 从保留段尾走到头,取原样保留的消息;返回 (保留段文本, compactMetadata)。"""
    by = {r.get("uuid"): r for r in recs if r.get("uuid")}
    b = by.get(summ.get("parentUuid"))  # 摘要的父消息就是它的压缩边界
    if not b or b.get("subtype") != "compact_boundary":
        si = recs.index(summ)
        b = next((x for x in reversed(recs[:si]) if x.get("subtype") == "compact_boundary"), None)
    meta = (b.get("compactMetadata") or {}) if b else {}
    seg = meta.get("preservedSegment") or {}
    texts, u, guard = [], seg.get("tailUuid"), 0
    while u and u in by and guard < 5000:
        texts.append(text_of(by[u].get("message", {}).get("content")))
        if u == seg.get("headUuid"):
            break
        u, guard = by[u].get("parentUuid"), guard + 1
    return norm("\n".join(texts)), meta


def main():
    args = sys.argv[1:]
    nth = -1
    if "--nth" in args:
        i = args.index("--nth")
        try:
            nth = int(args[i + 1])
        except (IndexError, ValueError):
            print(__doc__)
            return 2
        del args[i:i + 2]
    if len(args) != 2:
        print(__doc__)
        return 2
    tpath, cpath = args
    try:
        recs = load(tpath)
        with open(cpath, encoding="utf-8") as f:
            md = f.read()
    except OSError as e:
        print(f"✗ 读不了输入文件:{e}")
        return 2
    summaries = [r for r in recs if r.get("isCompactSummary")]
    if not summaries:
        print(f"✗ 转录里没有压缩摘要(isCompactSummary):{tpath}")
        return 2
    try:
        summ = summaries[nth]
    except IndexError:
        print(f"✗ 只有 {len(summaries)} 次压缩,--nth {nth} 越界")
        return 2
    secs = split_sections(md)
    if not any(secs.values()):
        print(f"✗ 检查点里没有可核对的条目(需要 🔴 ⚠ 🚨 🧠 🛰 开头的二级标题):{cpath}")
        return 2
    summary = norm(text_of(summ.get("message", {}).get("content")))
    preserved, meta = preserved_text(recs, summ)
    must = re.search(r"本次必留[::]\s*(.+)", md)

    print(f"摘要:{summ.get('timestamp', '?')[:16]}Z · {len(summary)} 字(去空白)· 压前 {meta.get('preTokens', '?')} tokens · 保留段 {len(preserved)} 字")
    print(f"检查点:{cpath}")
    if must:
        print(f"本次必留(检查点自报):{must.group(1).strip()}")
    print()
    print("| 必留 | 检查点条数 | 在 | 残 | 丢 | 只在保留段 | 判不了 |")
    print("|---|---|---|---|---|---|---|")
    detail, bad = [], 0
    for emo, name in SECTIONS:
        items = secs.get(emo)
        if items is None:
            continue
        c = {"在": 0, "残": 0, "丢": 0, "保留段": 0, "判不了": 0}
        for idx, it in enumerate(items, 1):
            an = anchors(it)
            if not an:
                fallback = text_anchors(it)
                if fallback and sum(norm(a) in summary for a in fallback) * 2 > len(fallback):
                    c["在"] += 1
                    continue
                c["判不了"] += 1
                detail.append(f"  ? {emo}{idx} 抽不出锚点,人看:{it[:70]}")
                continue
            hit = [a for a in an if norm(a) in summary]
            miss = [a for a in an if a not in hit]
            if len(hit) * 2 > len(an):
                c["在"] += 1
                continue
            if preserved and all(norm(a) in preserved for a in miss):
                c["保留段"] += 1
                detail.append(f"  ~ {emo}{idx} 摘要没保住、保留段里有:{it[:70]}")
                continue
            bad += 1
            if hit:
                c["残"] += 1
                detail.append(f"  △ {emo}{idx} 残,缺 {', '.join(miss[:6])}:{it[:70]}")
            else:
                c["丢"] += 1
                detail.append(f"  ✗ {emo}{idx} 丢,锚点 {', '.join(an[:6])} 摘要里一个都没有:{it[:70]}")
        print(f"| {emo} {name} | {len(items)} | {c['在']} | {c['残']} | {c['丢']} | {c['保留段']} | {c['判不了']} |")
    if detail:
        print()
        print("\n".join(detail))
    print()
    print("✓ 没有丢或残" if bad == 0 else f"✗ {bad} 条丢或残,去转录里找回,补进当前上下文")
    unknown = sum(1 for d in detail if d.startswith("  ?"))
    if unknown:
        print(f"⚠ 另有 {unknown} 条判不了,不算核过,要人看")
    print("注意:机器只比锚点。「在」= 标识符活着,不保证意思没走样;「判不了」的要人看。摘要可能把数字更新成压前最后几轮的新事实,先分清是更新了还是丢了。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
