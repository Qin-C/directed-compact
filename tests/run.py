#!/usr/bin/env python3
"""用虚构的转录和检查点测两个脚本。只用标准库,不读本机任何配置。"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPARE = os.path.join(ROOT, "scripts", "compare-summary.py")
COUNT = os.path.join(ROOT, "scripts", "count-compactions.py")

CHECKPOINT = """> Checkpoint: 2026-01-15T10:00:00Z
本次必留:原话 1 条 · 未决 1 行 · 警告 2 条 · 被否选项 1 个 · 在跑的子代理 1 个
## 🔴 原话拍板
- 「默认关闭夜间同步任务」 → 已做,11-02
## ⚠ 未决
| 事项 | 建议 | 状态 |
|---|---|---|
| 升级 `parser-core` | 先跑 `tests/parse_smoke.py` | 等确认 |
## 🚨 警告
1. 端口 `8443` 不能改成 `8080`,配置在 `deploy/gateway.toml`
2. 这条没有任何标识符只有叙述
## 🧠 被否掉的选项与为什么否
- 不用 `redis-lock`,因为 `lease_timeout` 在 1200 毫秒时抖动
## 🛰 在跑的子代理
| 派出时间 | 任务 | 产物 | 断言 |
|---|---|---|---|
| 上午 | 跑回归 | `report/regress.html` | `pass_rate` 等于 100% |
## ▶️ 下一步
1. 合并解析器改动
"""

FULL = """用户决定:默认关闭夜间同步任务(11-02)。
未决:升级 parser-core,先跑 tests/parse_smoke.py。
警告:端口 8443 不能改成 8080,配置在 deploy/gateway.toml。
否决:不用 redis-lock,lease_timeout 在 1200 毫秒时抖动。
子代理:report/regress.html,pass_rate 等于 100%。
"""

failures = []
checks = 0


def check(name, ok, output=""):
    global checks
    checks += 1
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        failures.append(name)
        print(output)


def run(script, *args):
    p = subprocess.run([sys.executable, script, *args], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def write_jsonl(path, recs):
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def boundary(mid, pre, post, parent=None, segment=None):
    cm = {"preTokens": pre, "postTokens": post, "trigger": "manual"}
    if segment:
        cm["preservedSegment"] = segment
    return {"type": "system", "subtype": "compact_boundary", "uuid": mid, "parentUuid": parent,
            "timestamp": f"2026-01-15T10:{pre % 60:02d}:00Z", "compactMetadata": cm}


def summary(mid, parent, text):
    return {"type": "user", "uuid": mid, "parentUuid": parent, "isCompactSummary": True,
            "timestamp": "2026-01-15T10:30:00Z", "message": {"role": "user", "content": text}}


def user(mid, parent, text):
    return {"type": "user", "uuid": mid, "parentUuid": parent,
            "timestamp": "2026-01-15T09:00:00Z", "message": {"role": "user", "content": text}}


def row(output, label):
    """取对账表某一行的数字:[条数, 在, 残, 丢, 只在保留段, 判不了]。"""
    for line in output.splitlines():
        if line.startswith("| ") and label in line:
            return [int(x) for x in re.findall(r"\|\s*(\d+)(?=\s*\|)", line)]
    return None


def compare_case(d, name, text, *extra, recs=None, md=CHECKPOINT):
    t = os.path.join(d, f"{name}.jsonl")
    c = os.path.join(d, f"{name}.md")
    write_jsonl(t, recs if recs is not None else [boundary("b1", 500000, 20000), summary("s1", "b1", text)])
    with open(c, "w", encoding="utf-8") as f:
        f.write(md)
    return run(COMPARE, t, c, *extra)


def test_compare(d):
    code, out = compare_case(d, "all", FULL)
    check("全在:退出 0", code == 0, out)
    check("全在:警告段 2 条,1 在 1 判不了", row(out, "警告") == [2, 1, 0, 0, 0, 1], out)
    check("全在:原话段全在", row(out, "原话") == [1, 1, 0, 0, 0, 0], out)
    check("全在:未决段全在", row(out, "未决") == [1, 1, 0, 0, 0, 0], out)
    check("全在:子代理段全在", row(out, "在跑的子代理") == [1, 1, 0, 0, 0, 0], out)

    lost = FULL.replace("否决:不用 redis-lock,lease_timeout 在 1200 毫秒时抖动。\n", "")
    code, out = compare_case(d, "lost", lost)
    check("丢一条:退出 1", code == 1, out)
    check("丢一条:被否选项段记 1 丢", row(out, "被否选项") == [1, 0, 0, 1, 0, 0], out)
    check("丢一条:其他段不受影响", row(out, "原话") == [1, 1, 0, 0, 0, 0], out)
    check("丢一条:点名丢的锚点", "redis-lock" in out and "1 条丢或残" in out, out)

    partial = FULL.replace("端口 8443 不能改成 8080,", "端口要注意,")
    code, out = compare_case(d, "partial", partial)
    check("残一条:退出 1", code == 1, out)
    check("残一条:警告段记 1 残", row(out, "警告") == [2, 0, 1, 0, 0, 1], out)
    check("残一条:列出缺的锚点", "8443" in out and "8080" in out and "残" in out, out)

    seg = {"headUuid": "p1", "tailUuid": "p2"}
    recs = [user("p1", None, "不用 redis-lock,因为 lease_timeout 在 1200 毫秒时抖动"),
            user("p2", "p1", "收到"),
            boundary("b1", 500000, 20000, segment=seg), summary("s1", "b1", lost)]
    code, out = compare_case(d, "preserved", "", recs=recs)
    check("摘要没有但保留段有:不算丢", code == 0 and row(out, "被否选项") == [1, 0, 0, 0, 1, 0], out)

    recs = [boundary("b1", 400000, 18000), summary("s1", "b1", FULL),
            boundary("b2", 500000, 20000, parent="s1"), summary("s2", "b2", lost)]
    code, _ = compare_case(d, "nth", "", recs=recs)
    check("默认比最近一次压缩", code == 1)
    code, _ = compare_case(d, "nth", "", "--nth", "-2", recs=recs)
    check("--nth -2 比上一次压缩", code == 0)
    code, out = compare_case(d, "nth", "", "--nth", "-3", recs=recs)
    check("--nth 越界:退出 2", code == 2, out)

    code, out = compare_case(d, "none", "", recs=[user("m1", None, "还没压缩过")])
    check("转录里没有摘要:退出 2", code == 2, out)
    code, out = compare_case(d, "emptymd", FULL, md="# 这不是检查点\n没有约定的小节\n")
    check("检查点没有可核对条目:退出 2,不给假绿", code == 2, out)
    code, out = run(COMPARE, os.path.join(d, "missing.jsonl"), os.path.join(d, "missing.md"))
    check("文件不存在:退出 2", code == 2, out)


def last_count(output):
    m = re.search(r"压缩次数 = (\d+)", output)
    return int(m.group(1)) if m else None


def test_count(root):
    d = os.path.join(root, "single")
    os.mkdir(d)
    t = os.path.join(d, "a.jsonl")
    write_jsonl(t, [user("m1", None, "开始")])
    code, out = run(COUNT, t)
    check("没压缩过:0 次", code == 0 and last_count(out) == 0, out)

    b1, b2 = boundary("b1", 400000, 18000), boundary("b2", 500000, 20000)
    write_jsonl(t, [user("m1", None, "开始"), b1, b1, b2])
    with open(t, "a", encoding="utf-8") as f:
        f.write('{"写到一半的行\n')
    code, out = run(COUNT, t)
    check("重放的旧边界只算一次,损坏行跳过", code == 0 and last_count(out) == 2, out)
    check("列出压前压后 tokens", "400000 → 18000" in out and "500000 → 20000" in out, out)

    d = os.path.join(root, "fork")
    os.mkdir(d)
    old, new, other = (os.path.join(d, n) for n in ("old.jsonl", "new.jsonl", "other.jsonl"))
    b3 = boundary("b3", 600000, 22000)
    write_jsonl(old, [user("m1", None, "开始"), b1, b2])
    write_jsonl(new, [user("m1", None, "开始"), b2, b3])
    write_jsonl(other, [user("x1", None, "另一条会话"), boundary("x2", 700000, 25000)])
    code, out = run(COUNT, new)
    check("fork 后跨转录取并集,不算无关会话", code == 0 and last_count(out) == 3, out)
    check("报告同链转录 2 份", "同链转录 2 份" in out, out)
    check("无关会话的边界不出现", "700000" not in out, out)

    code, out = run(COUNT, os.path.join(d, "missing.jsonl"))
    check("转录不存在:退出 2", code == 2, out)
    code, out = run(COUNT)
    check("缺参数:退出 2", code == 2, out)


def main():
    with tempfile.TemporaryDirectory() as d:
        test_compare(d)
        test_count(d)
    print(f"\n{checks - len(failures)}/{checks} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
