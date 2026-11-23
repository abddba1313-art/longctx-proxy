# -*- coding: utf-8 -*-
"""回注后第一动作胜率评测（REMORY arXiv 2610.11287 §3.3/§3.1 协议移植）。

三臂配对：N（删提醒节）/ R（原样回注）/ E（等预算非定向文字）。
主判据 R vs E：收益来自「定向回注被忽视的锚点」，还是「字多」？
离线旁路：不经过代理、不写会话状态、不执行任何 tool_call（提案只评不执行）。
设计见 REMORY评估方法移植-设计.md。proxy/GUI 零改动。

用法：
  python first_action_eval.py --dry-run                 # 只列事件与三臂构造，零联网
  python first_action_eval.py --max-events 20           # 冒烟（费用门内）
  python first_action_eval.py --max-events 60 --budget 150 --yes
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _cand in (HERE / "proxy", HERE.parent / "proxy", HERE):
    if (_cand / "proxy.py").exists():
        sys.path.insert(0, str(_cand))
        break

import proxy  # noqa: E402
import context_eval as ce  # noqa: E402

cp = proxy.checkpoint
VERSION = "1.0.0"
REMIND_HEADER = proxy.REMIND_MARK          # "## 上一轮未提及的锚点"
END = cp.MARK_END                          # "[longctx:checkpoint end]"
BOOT_B = 10000
BOOT_SEED = 20261112
TRANSIENT = {429, 500, 502, 503, 504}
LEAK_RE = re.compile(r"提醒|锚点|代理核实")
CONSTRAINT_RE = re.compile(r"必须|不得|仅当|至少|不超过|定义为|禁止")
PAIRS = (("R", "E"), ("R", "N"), ("E", "N"))


# ---------- 纯函数：三臂构造 ----------

def msg_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def cp_index(messages: list) -> int:
    for i, m in enumerate(messages or []):
        if cp.is_mark(m):
            return i
    return -1


def split_remind(block: str) -> tuple[str, str, str]:
    """(提醒节前, 提醒节, end 标记起)。提醒节 = REMIND_HEADER 行到 END 之前。"""
    i = block.find(REMIND_HEADER)
    j = block.rfind(END)
    if i < 0 or j < i:
        return block, "", ""
    return block[:i], block[i:j], block[j:]


def remind_items(section: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in section.splitlines()
            if ln.strip().startswith("- ")]


def _overlaps_missed(t: str, missed: list[str]) -> bool:
    nt = proxy._norm(t)
    return any(nt and (proxy._norm(m) in nt or nt in proxy._norm(m))
               for m in missed)


def build_e_section(before: str, after: str, section: str, budget: int) -> tuple[str, str]:
    """等预算对照节：同格式同预算，内容=已回声锚点（非定向），不含任何被判漏信息。

    返回 (新节, 补齐来源)。预算口径 = proxy.count_tokens（与生产同一口径）。
    """
    header = section.splitlines()[0]
    missed = remind_items(section)
    base_block = before + after
    pool, src = [], "anchors"
    for t, kind in proxy.parse_anchors_typed(base_block):
        if kind == "probe" or _overlaps_missed(t, missed):
            continue
        if t not in pool:
            pool.append(t)
    if not pool:
        src = "facts"
        for ln in base_block.splitlines():
            s = ln.strip()
            if s.startswith("- "):
                t = s[2:].strip()
                if t and not _overlaps_missed(t, missed) and t not in pool:
                    pool.append(t)
    if not pool:
        src = "verbatim"
        pool = [ln.strip() for ln in before.splitlines()
                if ln.strip() and not ln.strip().startswith(("##", "[longctx"))]
    cur = header + "\n"
    if not pool:
        return cur, "empty"
    k = 0
    while proxy.count_tokens(cur) < budget * 0.95 and k < len(pool) * 8:
        t = pool[k % len(pool)][:120]
        k += 1
        nxt = cur + "- " + t + "\n"
        if proxy.count_tokens(nxt) > budget * 1.05:
            lo, hi = 0, len(t)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if proxy.count_tokens(cur + "- " + t[:mid] + "\n") <= budget * 1.05:
                    lo = mid
                else:
                    hi = mid - 1
            if lo > 0:
                cur += "- " + t[:lo] + "\n"
            break
        cur = nxt
    return cur, src


def make_arms(sent: dict) -> tuple[dict, dict]:
    """三臂请求：除提醒节外逐字节相同。N 删 / R 原样 / E 等预算换内容。"""
    idx = cp_index(sent.get("messages") or [])
    if idx < 0:
        raise ValueError("sent 里没有检查点消息")
    block = msg_text(sent["messages"][idx])
    before, section, after = split_remind(block)
    if not section:
        raise ValueError("检查点里没有提醒节（不是回注事件）")
    budget = proxy.count_tokens(section)
    e_sec, src = build_e_section(before, after, section, budget)
    arms = {}
    for name, new_block in (("N", before + after), ("R", block),
                            ("E", before + e_sec + after)):
        body = copy.deepcopy(sent)
        body["messages"][idx]["content"] = new_block
        arms[name] = body
    e_tokens = proxy.count_tokens(e_sec)
    info = {"budget_tokens": budget, "e_tokens": e_tokens, "fill_source": src,
            "budget_ok": bool(budget * 0.95 <= e_tokens <= budget * 1.05),
            "remind_count": len(remind_items(section))}
    return arms, info


# ---------- 纯函数：事件筛选与标准固定 ----------

def select_events(dump_dir, max_prompt_tokens: int, max_events: int) -> tuple[list, int]:
    """从 dump 三件套挑回注事件（meta 提醒计数≥1），按 sent 哈希去重。"""
    events, skipped_big, seen = [], 0, set()
    key_prefix = REMIND_HEADER[3:]
    for mp in sorted(Path(dump_dir).glob("*-meta.json")):
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        ck = meta.get("checkpoint") or {}
        secs = ck.get("sections") or {}
        n_remind = sum(int(v) for k, v in secs.items() if k.startswith(key_prefix))
        if not ck.get("present") or n_remind < 1:
            continue
        if int(meta.get("sent_tokens") or 0) > max_prompt_tokens:
            skipped_big += 1
            continue
        stem = mp.name[:-len("-meta.json")]
        sp = mp.with_name(stem + "-sent.json")
        if not sp.exists():
            continue
        try:
            sent = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            continue
        h = hashlib.sha256(json.dumps(sent, ensure_ascii=False,
                                      sort_keys=True).encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        if cp_index(sent.get("messages") or []) < 0 or not meta.get("upstream_model"):
            continue
        events.append({"stem": stem, "time": meta.get("time", ""),
                       "model": meta["upstream_model"], "base": meta.get("upstream", ""),
                       "sent": sent, "sent_tokens": int(meta.get("sent_tokens") or 0),
                       "cp_tokens": int(ck.get("tokens") or 0),
                       "remind_count": n_remind})
        if len(events) >= max_events:
            break
    return events, skipped_big


def build_criteria(ev: dict) -> dict:
    """裁判标准：生成任何候选之前固定。基础锚点（三臂一致）+ 被回注条目 + 任务上下文。"""
    sent = ev["sent"]
    idx = cp_index(sent["messages"])
    block = msg_text(sent["messages"][idx])
    before, section, after = split_remind(block)
    base = [(t, k) for t, k in proxy.parse_anchors_typed(before + after)
            if k != "probe"]
    focus = remind_items(section)
    kinds = []
    for f in focus:
        kind = next((k for t, k in base
                     if proxy._norm(f) and proxy._norm(f) in proxy._norm(t)), None)
        if kind is None:
            kind = "rule" if CONSTRAINT_RE.search(f) else "data"
        kinds.append(kind)
    real = [m for m in sent["messages"][:idx]
            if m.get("role") in ("user", "assistant") and not cp.is_mark(m)]
    parts = [f"[{m['role']}] {msg_text(m)}" for m in real[-2:]]
    ctx = "\n".join(parts)
    while proxy.count_tokens(ctx) > 2000 and parts:
        parts[0] = parts[0][:len(parts[0]) // 2]
        if not parts[0].strip():
            parts.pop(0)
        ctx = "\n".join(parts)
    return {"stem": ev["stem"], "base_anchors": base[:40], "focus": focus,
            "focus_kinds": kinds, "task_context": ctx,
            "remind_count": len(focus)}


# ---------- 纯函数：裁判协议 ----------

def action_text(resp: dict) -> str:
    """候选动作文本：content + tool_call 序列化。reasoning_content 永不读取。"""
    c = (resp.get("content") or "").strip()
    tcs = resp.get("tool_calls") or []
    if tcs:
        ser = "\n".join(
            f"[tool_call] {(t.get('function') or {}).get('name', '')}"
            f"({(t.get('function') or {}).get('arguments', '')})" for t in tcs)
        c = (c + "\n" + ser).strip() if c else ser
    return c


def judge_prompt(cr: dict, a: str, b: str) -> str:
    lines = ["你是评审。根据固定标准判断哪个候选动作更好地推进任务并遵守约束。",
             "忽略回执式措辞（如确认收到提醒），只判任务推进与约束遵守。",
             "【任务背景】", cr["task_context"] or "(无)", "【固定标准】"]
    lines += [f"- {t}" for t, _ in cr["base_anchors"]]
    lines += ["【重点核对（历史上曾被忽视的条目）】"]
    lines += [f"- {f}" for f in cr["focus"]]
    lines += ["【候选动作 甲】", a or "(空)", "【候选动作 乙】", b or "(空)",
              "第一行只输出 WIN甲 / WIN乙 / TIE，第二行一句话理由。"]
    return "\n".join(lines)


def parse_verdict(text: str):
    for ln in (text or "").splitlines():
        s = ln.strip().upper()
        if not s:
            continue
        if s.startswith("WIN甲") or s.startswith("WIN 甲"):
            return "A"
        if s.startswith("WIN乙") or s.startswith("WIN 乙"):
            return "B"
        if s.startswith("TIE"):
            return "TIE"
        return None
    return None


def combine_verdicts(v1, v2) -> tuple:
    """v1=顺序(X,Y)的判定，v2=顺序(Y,X)的判定。一致才计胜负，不一致记平。

    返回 (verdict, flipped)；任一 None → (None, False) 记无效。
    """
    if v1 is None or v2 is None:
        return None, False
    v2m = {"A": "B", "B": "A", "TIE": "TIE"}[v2]
    if v1 == v2m:
        return v1, False
    return "TIE", True


def bootstrap_net(outcomes: list, B: int = BOOT_B, seed: int = BOOT_SEED) -> tuple:
    """按事件重采样的净胜率 (胜-负)/n 与 95% 区间。固定种子可复现。"""
    n = len(outcomes)
    if not n:
        return None, None, None
    net = sum(outcomes) / n
    rng = random.Random(seed)
    vals = sorted(sum(outcomes[rng.randrange(n)] for _ in range(n)) / n
                  for _ in range(B))
    return net, vals[int(0.025 * B)], vals[min(B - 1, int(0.975 * B))]


# ---------- 网络层（旁路直连，不经代理） ----------

def call_action(base: str, key: str, model: str, messages: list, *,
                temperature=0.0, max_tokens=1024, timeout=600, retries=2) -> dict:
    """ce.call_api 同骨架，但保留 tool_calls——agent 流量的第一动作常是工具调用，
    ce.call_api 只回 content 会把它丢掉。不执行任何调用，只序列化给裁判。"""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "stream": False, "temperature": temperature}
    req = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # 路径拼接复用代理同款：本地 base 无路径→/v1/chat/completions，
    # 云端 base 带 /v1→去重，绝不拼出 /v1/v1（join_upstream_path 语义）
    url = proxy.join_upstream_path(base.rstrip("/"), "/v1/chat/completions")
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url,
                                       data=req,
                                       headers={"Content-Type": "application/json",
                                                "Authorization": f"Bearer {key}"}),
                timeout=timeout)
            d = json.loads(r.read().decode("utf-8"))
            msg = d["choices"][0]["message"]
            return {"content": msg.get("content") or "",
                    "tool_calls": msg.get("tool_calls") or [],
                    "usage": d.get("usage") or {},
                    "latency": round(time.time() - t0, 1)}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            if e.code in TRANSIENT and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"网络错误: {e!r}") from None
    raise RuntimeError(f"网络错误: {last!r}")


def judge_call(base: str, key: str, model: str, prompt: str, timeout=120) -> str:
    r = call_action(base, key, model, [{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=128, timeout=timeout)
    return r["content"]


def _is_local(base: str) -> bool:
    return base.startswith("http://127.0.0.1") or "localhost" in base


# ---------- 评测管线 ----------

def prepare(events: list) -> list:
    """标准先行：所有 criteria 在任何生成调用之前固定。"""
    return [build_criteria(ev) for ev in events]


def evaluate(events: list, criterias: list, key_of, judge: dict, args) -> list:
    results = []
    for _i, (ev, cr) in enumerate(zip(events, criterias), 1):
        print(f"[{_i}/{len(events)}] {ev['stem']} model={ev['model']} "
              f"tokens={ev['sent_tokens']} 提醒{ev['remind_count']}条", flush=True)
        arms, info = make_arms(ev["sent"])
        key = key_of(ev)
        if key is None:
            results.append({"stem": ev["stem"], "model": ev["model"],
                            "dropped": "no_api_key"})
            continue
        gens, texts, usages = {}, {}, {}
        dropped = None
        for name in ("N", "E", "R"):     # 顺序执行：E/R 吃 N 写入的前缀缓存
            try:
                r = call_action(ev["base"], key, ev["model"],
                                arms[name]["messages"],
                                temperature=args.temperature,
                                max_tokens=args.max_tokens, timeout=args.timeout)
                if _is_local(ev["base"]) and args.local_repeat:
                    r2 = call_action(ev["base"], key, ev["model"],
                                     arms[name]["messages"],
                                     temperature=args.temperature,
                                     max_tokens=args.max_tokens, timeout=args.timeout)
                    if (r["content"], r["tool_calls"]) != (r2["content"], r2["tool_calls"]):
                        dropped = "local_unstable"
                        break
            except Exception as e:
                dropped = f"gen_error: {str(e)[:160]}"
                break
            gens[name] = r
            texts[name] = action_text(r)
            usages[name] = r["usage"]
            print(f"  arm {name}: {r.get('latency', '?')}s "
                  f"prompt={(r.get('usage') or {}).get('prompt_tokens', '?')} "
                  f"ans={texts[name][:40]!r}", flush=True)
        if dropped:
            print(f"  dropped: {dropped}", flush=True)
            results.append({"stem": ev["stem"], "model": ev["model"],
                            "dropped": dropped})
            continue
        leaks = {k: bool(LEAK_RE.search(t)) for k, t in texts.items()}
        comps = {}
        for x, y in PAIRS:
            seed = int(hashlib.sha256(
                (ev["stem"] + x + y).encode("utf-8")).hexdigest()[:8], 16)
            first_is_x = random.Random(seed).random() < 0.5
            a, b = (texts[x], texts[y]) if first_is_x else (texts[y], texts[x])
            try:
                v1 = parse_verdict(judge_call(judge["base"], judge["key"],
                                              judge["model"], judge_prompt(cr, a, b),
                                              timeout=args.timeout))
                v2 = parse_verdict(judge_call(judge["base"], judge["key"],
                                              judge["model"], judge_prompt(cr, b, a),
                                              timeout=args.timeout))
            except Exception:
                v1 = v2 = None
            swap = {"A": "B", "B": "A", "TIE": "TIE", None: None}
            u1, u2 = (v1, v2) if first_is_x else (swap[v1], swap[v2])
            verdict, flipped = combine_verdicts(u1, u2)
            comps[f"{x}v{y}"] = {"verdict": verdict, "flipped": flipped,
                                 "leak": bool(leaks[x] or leaks[y]),
                                 "first_is_x": first_is_x}
        results.append({"stem": ev["stem"], "time": ev["time"], "model": ev["model"],
                        "remind_count": ev["remind_count"], "info": info,
                        "usage": usages, "texts": texts, "leaks": leaks,
                        "focus_kinds": cr["focus_kinds"], "comps": comps,
                        "dropped": None})
    return results


def aggregate(results: list) -> dict:
    stats = {}
    ok = [r for r in results if not r.get("dropped")]
    for x, y in PAIRS:
        pair = f"{x}v{y}"
        outcomes, flips, invalid, leaks = [], 0, 0, 0
        strata = {}
        for r in ok:
            c = r["comps"][pair]
            if c["verdict"] is None:
                invalid += 1
                continue
            o = 1 if c["verdict"] == "A" else (-1 if c["verdict"] == "B" else 0)
            outcomes.append(o)
            flips += int(c["flipped"])
            leaks += int(c["leak"])
            for key in (r["model"], f"提醒{r['remind_count']}条",
                        "/".join(sorted(set(r["focus_kinds"]))) or "无"):
                strata.setdefault(key, []).append(o)
        net, lo, hi = bootstrap_net(outcomes)
        n = len(outcomes)
        stats[pair] = {"n": n, "wins": outcomes.count(1), "ties": outcomes.count(0),
                       "losses": outcomes.count(-1), "flips": flips, "invalid": invalid,
                       "leak_rate": round(leaks / n, 3) if n else None,
                       "flip_rate": round(flips / n, 3) if n else None,
                       "net": round(net, 3) if net is not None else None,
                       "ci95": [round(lo, 3), round(hi, 3)] if lo is not None else None,
                       "strata": {k: {"n": len(v), "net": round(sum(v) / len(v), 3)}
                                  for k, v in sorted(strata.items())}}
    return stats


def estimate_cost(events: list, judge_model: str, pricing: dict,
                  assume_cache: bool = True) -> float:
    """元。N 臂全价输入，E/R 共享前缀按 cache_read（检查点块本身除外）；
    本地 actor 电费不计。裁判 6 次/事件 × ~3k 输入。"""
    tw = ce.pick_window((pricing.get("target") or {}).get("windows") or [])
    cw = ce.pick_window((pricing.get("compressor") or {}).get("windows") or [])

    def win(model):
        return cw if "deepseek" in (model or "") else tw

    total = 0.0
    for ev in events:
        c = 0.0
        if not _is_local(ev["base"]):   # 本地 actor 电费不计，但裁判费照算
            w = win(ev["model"])
            st, cpt = ev["sent_tokens"], ev["cp_tokens"]
            pin = float(w.get("input", 12.0))
            pcr = float(w.get("cache_read", pin))
            pout = float(w.get("output", 36.0))
            if assume_cache:
                c = st * pin + 2 * ((st - cpt) * pcr + cpt * pin) + 3 * 500 * pout
            else:
                c = 3 * st * pin + 3 * 500 * pout
        jw = win(judge_model)
        c += 6 * (3000 * float(jw.get("input", 2.0))
                  + 128 * float(jw.get("output", 8.0)))
        total += c / 1e6
    return total


def write_report(out: Path, args, events, results, stats, cost, skipped_big):
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"version": VERSION, "args": vars(args), "cost_estimate_cny": round(cost, 2),
         "events": len(events), "skipped_big": skipped_big,
         "stats": stats, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    lines = ["# 回注后第一动作胜率（REMORY 协议移植）", "",
             f"- 版本: {VERSION} · 时间: {datetime.now().isoformat(timespec='seconds')}",
             f"- 事件: {len(events)}（超长按 tokens 跳过 {skipped_big}）· "
             f"有效: {sum(not r.get('dropped') for r in results)}",
             f"- 费用估算: ≈{cost:.2f} 元 · temperature={args.temperature}", "",
             "| 对比 | n | 胜 | 平 | 负 | 净胜率 | CI95 | 翻转率 | 泄漏率 | 无效 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for pair, s in stats.items():
        ci = f"[{s['ci95'][0]:+.2f},{s['ci95'][1]:+.2f}]" if s["ci95"] else "—"
        lines.append(f"| {pair} | {s['n']} | {s['wins']} | {s['ties']} | {s['losses']} | "
                     f"{s['net'] if s['net'] is not None else '—'} | {ci} | "
                     f"{s['flip_rate'] if s['flip_rate'] is not None else '—'} | "
                     f"{s['leak_rate'] if s['leak_rate'] is not None else '—'} | "
                     f"{s['invalid']} |")
    lines += ["", "主判据 RvE：定向回注 vs 等预算非定向文字。RvN=G5 总效应，EvN=纯补字效应。",
              "", "## 分层（净胜率）", ""]
    for pair, s in stats.items():
        lines.append(f"- {pair}: " + "; ".join(
            f"{k} {v['net']:+.2f}(n={v['n']})" for k, v in s["strata"].items()))
    lines += ["", "## 诚实边界", "",
              "- 判的是提案质量，不是执行结果（反事实动作不执行）；胜率高≠任务成功率同幅涨。",
              "- 裁判为便宜模型+交换双跑兜底；翻转率>15% 时本批数据作废重采。",
              "- 本地 HIP actor temp=0 不保证逐字节复现：每臂双生成，不一致事件剔除。",
              "- 等预算对照证明「定向优于无差别补字」，不证明回注优于其它定向策略。",
              "- 样本量<60 事件时区间宽，只作冒烟，不下结论。"]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    cdir = out / "criteria"
    cdir.mkdir(exist_ok=True)
    for ev in events:
        (cdir / f"{ev['stem']}.json").write_text(json.dumps(
            build_criteria(ev), ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="回注后第一动作胜率评测（离线旁路）")
    ap.add_argument("--dump-dir", default=str(HERE / "proxy" / "dumps"))
    ap.add_argument("--max-events", type=int, default=20)
    ap.add_argument("--max-prompt-tokens", type=int, default=250000)
    ap.add_argument("--judge", default="", help="裁判模型名；缺省=与 actor 互换")
    ap.add_argument("--budget", type=float, default=30.0, help="费用上限（元），超过需 --yes")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只列事件与三臂构造，零联网")
    ap.add_argument("--no-cache-assume", action="store_true", help="费用按全无缓存上限估")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--local-repeat", type=int, default=1, help="本地 actor 每臂双生成查稳")
    ap.add_argument("--seed", type=int, default=BOOT_SEED)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    events, skipped_big = select_events(args.dump_dir, args.max_prompt_tokens,
                                        args.max_events)
    print(f"first_action_eval {VERSION}（REMORY 协议移植，离线旁路）")
    print(f"回注事件: {len(events)}（超长跳过 {skipped_big}）")
    if not events:
        print("没有可用事件：需要 PROXY_DUMP=1 且检查点带提醒节的 dump。")
        return 0
    targets = ce.load_targets()
    pricing = json.loads((HERE / "app" / "pricing.json").read_text(encoding="utf-8"))
    judge_name = args.judge or (
        "deepseek-v4.1-flash-1017" if "qwen" in events[0]["model"] else "qwen3.8-flash")
    criterias = prepare(events)
    cost = estimate_cost(events, judge_name, pricing,
                         assume_cache=not args.no_cache_assume)
    print(f"裁判: {judge_name} · 费用估算 ≈ {cost:.2f} 元"
          f"{'（无缓存上限）' if args.no_cache_assume else '（前缀缓存共享）'}")
    if args.dry_run:
        for ev, cr in zip(events, criterias):
            arms, info = make_arms(ev["sent"])
            print(f"[dry] {ev['stem']} model={ev['model']} "
                  f"tokens={ev['sent_tokens']} 提醒{info['remind_count']}条 "
                  f"E预算={info['budget_tokens']}tok 实配={info['e_tokens']}tok "
                  f"来源={info['fill_source']} 达标={info['budget_ok']} "
                  f"标准锚点={len(cr['base_anchors'])}")
        print("dry-run 完成：未发送任何请求")
        return 0
    if cost > args.budget and not args.yes:
        print(f"估算 {cost:.2f} 元超过预算 {args.budget} 元；确认后加 --yes 重跑。")
        return 2
    jt = next((t for t in targets if t["model"] == judge_name), None)
    if jt is None:
        print(f"targets.json 里找不到裁判模型 {judge_name}")
        return 3

    def key_of(ev):
        t = next((t for t in targets if t["base_url"] == ev["base"]
                  and t["model"] == ev["model"]), None)
        return t["api_key"] if t else None

    judge = {"base": jt["base_url"], "key": jt["api_key"], "model": jt["model"]}
    results = evaluate(events, criterias, key_of, judge, args)
    stats = aggregate(results)
    out = Path(args.out) if args.out else (
        HERE / "ctx-eval-reports" /
        f"first-action-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    write_report(out, args, events, results, stats, cost, skipped_big)
    print(f"\n报告: {out / 'summary.md'}")
    for pair, s in stats.items():
        print(f"  {pair}: {s['wins']}胜/{s['ties']}平/{s['losses']}负 "
              f"净胜率={s['net']} CI95={s['ci95']} 翻转率={s['flip_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
