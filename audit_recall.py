#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""召回审计：对比 longctx 落盘的「原始请求」和「实际转发请求」。

用法：
    # 1) 启代理时打开 dump（只影响新进程）
    set PROXY_DUMP=1
    # 2) 在 Cherry 里复现一轮长对话
    # 3) 审计
    python audit_recall.py --latest
    python audit_recall.py --json recall-report.json

指标：
    T1 标识召回：数字/版本/路径/URL/环境变量/错误码/文件名:行号/哈希
    T2 约束召回：必须/不得/仅当/至少/不超过/定义为……
    检查点覆盖：这些标识和约束有多少真的进了 [longctx:checkpoint] 块
    token 保留率：原始正文 -> 转发正文

只做确定性匹配（精确串 / 归一化串 / 字符二元组），不调用任何模型。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proxy"))

try:
    import checkpoint
    import evidence
    import proxy
except Exception as exc:                       # pragma: no cover
    print(f"导入 proxy/evidence 失败：{exc}")
    raise SystemExit(2)


# ---------------------------------------------------------------- 文本与匹配

def _msg_text(msg) -> str:
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for p in c:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                out.append(str(p.get("text", "")))
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return ""


def body_text(body) -> str:
    return "\n\n".join(
        _msg_text(m) for m in (body.get("messages") or []) if isinstance(m, dict))


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _norm_lo(s: str) -> str:
    return _norm_space(s).lower()


class Matcher:
    """sent 文本只预处理一次，避免每条事实都重新算 bigram。"""

    def __init__(self, text: str):
        self.sent = _norm_lo(text)
        self.sent_slash = self.sent.replace("\\", "/")
        self.sent_norm = evidence._norm(text)
        self.sent_bigrams = evidence._bigrams(text)

    def has(self, item: str) -> bool:
        n = _norm_lo(item)
        if not n:
            return True
        if n in self.sent:
            return True
        if n.replace("\\", "/") in self.sent_slash:
            return True
        ns = evidence._norm(item)
        if ns and ns in self.sent_norm:
            return True
        if len(ns) < 12:
            return False
        a = evidence._bigrams(item)
        if not a:
            return True
        return len(a & self.sent_bigrams) / len(a) >= 0.62


def _checkpoint_block(body) -> str:
    """只认代理注入的那一块：整条消息以标记开头。

    早先用 find() 取“第一次出现标记之后的一切”，结果会抓到工具输出里引用的旧块
    （我打印过检查点，它就出现在 tool 消息中间），把 1,187 tok 的块量成 4,857 tok，
    覆盖率也因此算歪。
    """
    for msg in (body.get("messages") or []):
        t = _msg_text(msg).lstrip()
        if t.startswith("[longctx:checkpoint"):
            return t
    return ""


def _pct(a: int, b: int) -> float:
    return (100.0 * a / b) if b else 100.0


# ---------------------------------------------------------------- 单次审计

def _role_text(body, role: str) -> str:
    return "\n\n".join(
        _msg_text(m) for m in (body.get("messages") or [])
        if isinstance(m, dict) and m.get("role") == role)


def _extract_t1(text: str) -> list[tuple[str, str]]:
    """结构化标识 + 宽口径事实的并集，去重。"""
    ids, seen = [], set()
    for v, k in evidence.extract_identifier_items(text, limit=3000):
        nv = _norm_lo(v)
        if nv and nv not in seen:
            seen.add(nv)
            ids.append((v, k))
    for f in proxy.extract_facts(text, limit=5000):
        if checkpoint is not None and not checkpoint._fact_ok(f):
            continue
        nf = _norm_lo(f)
        if nf and nf not in seen:
            seen.add(nf)
            ids.append((f, "fact"))
    return ids


def _extract_t2(text: str) -> list[tuple[str, str]]:
    return evidence.extract_constraints(text, limit=500)


def audit_pair(orig_path: Path, sent_path: Path, lost_n: int = 10) -> dict:
    orig = json.loads(orig_path.read_text(encoding="utf-8"))
    sent = json.loads(sent_path.read_text(encoding="utf-8"))
    ot, st = body_text(orig), body_text(sent)
    m = Matcher(st)

    ids = _extract_t1(ot)
    cons = _extract_t2(ot)
    # 用户消息单独算一组：代码/工具输出会把整体召回率稀释，用户原话才是任务要求。
    ou, su = _role_text(orig, "user"), _role_text(sent, "user")
    u_ids, u_cons = _extract_t1(ou), _extract_t2(ou)
    mu = Matcher(su)
    u_id_kept = [(v, k) for v, k in u_ids if mu.has(v)]
    u_con_kept = [(s, k) for s, k in u_cons if mu.has(s)]

    id_kept = [(v, k) for v, k in ids if m.has(v)]
    con_kept = [(s, k) for s, k in cons if m.has(s)]

    cp = _checkpoint_block(sent)
    cpm = Matcher(cp) if cp else None
    cp_id_kept = sum(1 for v, _k in ids if cpm and cpm.has(v))
    cp_con_kept = sum(1 for s, _k in cons if cpm and cpm.has(s))

    by_kind: dict[str, list[int]] = {}
    for v, k in ids:
        by_kind.setdefault(k, [0, 0])[1] += 1
        if m.has(v):
            by_kind[k][0] += 1

    lost_ids = [v for v, _k in ids if not m.has(v)]
    lost_cons = [s for s, _k in cons if not m.has(s)]

    orig_tok = proxy.count_tokens(ot)
    sent_tok = proxy.count_tokens(st)
    return {
        "time": orig_path.name.split("-")[0],
        "tag": orig_path.stem,
        "orig_tokens": orig_tok,
        "sent_tokens": sent_tok,
        "token_retention": _pct(sent_tok, orig_tok),
        "t1_total": len(ids),
        "t1_kept": len(id_kept),
        "t1_recall": _pct(len(id_kept), len(ids)),
        "t2_total": len(cons),
        "t2_kept": len(con_kept),
        "t2_recall": _pct(len(con_kept), len(cons)),
        "t1_user_total": len(u_ids),
        "t1_user_kept": len(u_id_kept),
        "t1_user_recall": _pct(len(u_id_kept), len(u_ids)),
        "t2_user_total": len(u_cons),
        "t2_user_kept": len(u_con_kept),
        "t2_user_recall": _pct(len(u_con_kept), len(u_cons)),
        "by_kind": {k: {"kept": v[0], "total": v[1],
                        "recall": _pct(v[0], v[1])}
                    for k, v in sorted(by_kind.items())},
        "checkpoint_tokens": proxy.count_tokens(cp) if cp else 0,
        "checkpoint_t1": cp_id_kept,
        "checkpoint_t1_recall": _pct(cp_id_kept, len(ids)),
        "checkpoint_t2": cp_con_kept,
        "checkpoint_t2_recall": _pct(cp_con_kept, len(cons)),
        "lost_ids": lost_ids[:lost_n],
        "lost_constraints": lost_cons[:lost_n],
    }


# ---------------------------------------------------------------- 汇总输出

def _print_one(i: int, r: dict, lost_n: int) -> None:
    print("=" * 78)
    print(f"[{i}] {r['time']}  {r['tag']}")
    print(f"  token : {r['orig_tokens']:,} -> {r['sent_tokens']:,} "
          f"（保留 {r['token_retention']:.1f}%）")
    print(f"  T1 标识: {r['t1_kept']}/{r['t1_total']} = {r['t1_recall']:.1f}%"
          f"（用户消息 {r['t1_user_kept']}/{r['t1_user_total']} = "
          f"{r['t1_user_recall']:.1f}%）")
    if r["by_kind"]:
        parts = [f"{k} {v['kept']}/{v['total']} ({v['recall']:.0f}%)"
                 for k, v in r["by_kind"].items()]
        print("    " + " · ".join(parts))
    print(f"  T2 约束: {r['t2_kept']}/{r['t2_total']} = {r['t2_recall']:.1f}%"
          f"（用户消息 {r['t2_user_kept']}/{r['t2_user_total']} = "
          f"{r['t2_user_recall']:.1f}%）")
    print(f"  检查点: {r['checkpoint_tokens']:,} tok；"
          f"覆盖 T1 {r['checkpoint_t1']}/{r['t1_total']} "
          f"({r['checkpoint_t1_recall']:.1f}%)、"
          f"T2 {r['checkpoint_t2']}/{r['t2_total']} "
          f"({r['checkpoint_t2_recall']:.1f}%)")
    if r["lost_ids"]:
        print("  丢失标识样例:")
        for x in r["lost_ids"][:lost_n]:
            print(f"    - {x}")
    if r["lost_constraints"]:
        print("  丢失约束样例:")
        for x in r["lost_constraints"][:lost_n]:
            print(f"    - {x[:200]}")


def _aggregate(rows: list[dict]) -> dict:
    def s(k):
        return sum(r[k] for r in rows)

    return {
        "pairs": len(rows),
        "orig_tokens": s("orig_tokens"),
        "sent_tokens": s("sent_tokens"),
        "t1_kept": s("t1_kept"), "t1_total": s("t1_total"),
        "t2_kept": s("t2_kept"), "t2_total": s("t2_total"),
        "cp_t1": s("checkpoint_t1"), "cp_t2": s("checkpoint_t2"),
        "u_t1_kept": s("t1_user_kept"), "u_t1_total": s("t1_user_total"),
        "u_t2_kept": s("t2_user_kept"), "u_t2_total": s("t2_user_total"),
    }


# ---------------------------------------------------------------- CLI

def _selftest() -> int:
    """用一份合成对话验证脚本本身能跑、能算。"""
    tmp = Path(tempfile.mkdtemp(prefix="audit-selftest-"))
    orig = {"messages": [
        {"role": "user", "content":
            "必须保留端口 8082。\n"
            "路径 C:\\\\x\\\\app.py，版本 v2.4，错误码 E1024；\n"
            "下一步：测试。"},
    ]}
    # 代理是 inject() 单独插一条 user 消息（checkpoint.py:1292），
    # 所以真块永远“整条就是它”；夹具要跟真实形状一致，否则测不出抓错对象的问题。
    sent = {"messages": [
        {"role": "user", "content": "保留端口 8082。路径 C:\\\\x\\\\app.py，错误码 E1024。"},
        {"role": "user", "content":
            "[longctx:checkpoint v1]\n## 硬约束（逐字，不得改写）\n"
            "- 必须保留端口 8082。\n"
            "- 路径 C:\\\\x\\\\app.py，错误码 E1024。\n"
            "[longctx:checkpoint end]"},
    ]}
    (tmp / "t-orig.json").write_text(json.dumps(orig, ensure_ascii=False), encoding="utf-8")
    (tmp / "t-sent.json").write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    r = audit_pair(tmp / "t-orig.json", tmp / "t-sent.json")
    _print_one(1, r, 5)
    ok = (r["t1_total"] > 0 and r["t1_kept"] > 0
          and r["t2_total"] > 0 and r["checkpoint_t1"] > 0)
    print("\nselftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="longctx 召回审计")
    ap.add_argument("--dump-dir", default=str(HERE / "proxy" / "dumps"))
    ap.add_argument("--latest", action="store_true", help="只看最新一对")
    ap.add_argument("--json", default="", help="把报告写到 JSON")
    ap.add_argument("--lost", type=int, default=10, help="每对显示多少丢失样例")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    dump_dir = Path(args.dump_dir)
    pairs = []
    for orig in sorted(dump_dir.glob("*-orig.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        sent = Path(str(orig).replace("-orig.json", "-sent.json"))
        if sent.exists():
            pairs.append((orig, sent))
    if not pairs:
        print(f"没有找到 dump：{dump_dir}")
        print("启动代理前设 PROXY_DUMP=1，复现一轮对话后再跑。")
        return 1
    if args.latest:
        pairs = pairs[:1]

    rows = []
    for i, (orig, sent) in enumerate(pairs, 1):
        try:
            r = audit_pair(orig, sent, args.lost)
        except Exception as exc:
            print(f"[{i}] {orig.name}: 审计失败 {exc}")
            continue
        rows.append(r)
        _print_one(i, r, args.lost)

    if rows:
        a = _aggregate(rows)
        print("=" * 78)
        print(f"合计 {a['pairs']} 对：")
        print(f"  token {a['orig_tokens']:,} -> {a['sent_tokens']:,} "
              f"（保留 {_pct(a['sent_tokens'], a['orig_tokens']):.1f}%）")
        print(f"  T1 标识 {a['t1_kept']}/{a['t1_total']} = "
              f"{_pct(a['t1_kept'], a['t1_total']):.1f}%"
              f"（用户消息 {a['u_t1_kept']}/{a['u_t1_total']} = "
              f"{_pct(a['u_t1_kept'], a['u_t1_total']):.1f}%）")
        print(f"  T2 约束 {a['t2_kept']}/{a['t2_total']} = "
              f"{_pct(a['t2_kept'], a['t2_total']):.1f}%"
              f"（用户消息 {a['u_t2_kept']}/{a['u_t2_total']} = "
              f"{_pct(a['u_t2_kept'], a['u_t2_total']):.1f}%）")
        print(f"  检查点覆盖 T1 {a['cp_t1']}/{a['t1_total']}、"
              f"T2 {a['cp_t2']}/{a['t2_total']}")
        if args.json:
            Path(args.json).write_text(json.dumps(
                {"pairs": rows, "aggregate": a},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())