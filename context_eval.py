# -*- coding: utf-8 -*-
"""云端模型原生上下文能力评测（直连，无代理）。

由 window_stress_600k_noproxy.py 的探针布线升级而来，修复其方法学短板：
  1. 多模型 × 长度梯度 × 每点多次重复（默认 3），temperature=0；
  2. 真值逐次随机化（口令/时间/阈值/归档号/计数），防跨次记忆与题面捞针；
  3. 判据收紧：聚合题精确整数、多值题按服务映射、干扰题排除项检查；
  4. 单针在早/中/晚三个深度各布一份，额外报告 lost-in-the-middle 深度命中；
  5. 超限(400 context)记为 unsupported 数据点，429/5xx 重试，其余记 error；
  6. 先用小探针标定 tokens/块，再按引擎 usage 记录真实达成长度；
  7. 聚合输出能力曲线（summary.json + summary.md）与逐次原始报告。

用法：
  python context_eval.py --dry-run
  python context_eval.py --lengths 16k --reps 1                 # 冒烟
  python context_eval.py --models qwen3.8-flash,deepseek-v4.1-flash-1017 \
      --lengths 128k,256k,512k,1m --reps 3 --yes                # 全量（先打印费用估算）

模型取自 app/targets.json（--models 逗号分隔；缺省=启用行）。
本脚本不经过 longctx 代理，测的是模型本体；A/B 对照请用原 window_stress 系列。
"""
import argparse
import json
import math
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL_VERSION = "5.0.0"  # 承接 GUI 4.2.0 之后的评测套件版号
WORDS = ("系统 日志 记录 网络 请求 响应 缓存 队列 线程 端口 配置 参数 阈值 窗口 压缩 "
         "摘要 检查点 锚点 回声 提醒 复位 前缀 令牌 上下文 模型 推理 延迟 吞吐 数据 文件 目录")
AGG_SENT = "熔断演练已登记。"
SYSTEM = "你是一个严格的记录助手。"
QUESTION_TYPES = ("q1_single", "q2_distractor", "q3_multivalue",
                  "q4_multihop", "q5_aggregation", "q6_nonliteral")
PROBE_LAYOUT = {"q1": 3 / 90, "q2a": 39 / 90, "q2b": 40 / 90, "q2c": 41 / 90,
                "q3a": 7 / 90, "q3b": 46 / 90, "q3c": 87 / 90,
                "q4_ptr": 11 / 90, "q4_tgt": 62 / 90, "q6": 49 / 90,
                "depth_mid": 0.50, "depth_late": 0.93}
TRANSIENT = {429, 500, 502, 503, 504}


def parse_len(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", s)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析长度: {s!r}（示例: 128k, 1m, 32000）")
    mult = {"": 1, "k": 1000, "m": 1000000}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def load_targets():
    return json.loads((HERE / "app" / "targets.json").read_text(encoding="utf-8"))


def filler(i: int, n_lines: int = 110) -> str:
    rng = (i * 2654435761) & 0xFFFFFFFF
    out = []
    for n in range(n_lines):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        a, b = rng % 30, (rng >> 5) % 30
        out.append(
            f"第{i}段-{n}：{WORDS[a]}{WORDS[b]}处理完成，耗时 {(rng % 900) + 3}ms，"
            f"batch_{rng % 9973:04d} 状态 {'OK' if n % 7 else 'RETRY'}，"
            f"参考 https://internal.example/svc/{i}/{n} 与 key_{(rng >> 3) % 65536}。")
    return "\n".join(out)


def make_truth(rng: random.Random) -> dict:
    two = lambda: f"{rng.randrange(100):02d}"
    hhmm = lambda: f"{rng.randrange(24):02d}:{rng.randrange(60):02d}"
    return {
        "code": f"W{rng.randrange(100, 999)}-{two()}",
        "pass": f"{two()}{two()}-{rng.randrange(1000, 9999)}",
        "t_true": hhmm(),
        "t_d1": hhmm(),
        "t_d2": hhmm(),
        "k_true": f"K-{rng.randrange(1000, 9999)}",
        "k_d1": f"K-{rng.randrange(1000, 9999)}",
        "k_d2": f"K-{rng.randrange(1000, 9999)}",
        "thr": [rng.randrange(100, 999) for _ in range(3)],
        "archive": f"AR-{rng.randrange(1000, 9999)}",
        "agg_k": rng.randrange(3, 7),
        "mid_pass": f"ZJ{two()}-{rng.randrange(1000, 9999)}",
        "late_pass": f"MS{two()}-{rng.randrange(1000, 9999)}",
    }


def build_corpus(n_blocks: int, truth: dict, n_lines: int = 110) -> tuple:
    b = [filler(i, n_lines) for i in range(1, n_blocks + 1)]
    idx = {k: min(n_blocks - 1, max(0, round(f * (n_blocks - 1))))
           for k, f in PROBE_LAYOUT.items()}
    taken = set(idx.values())

    def plant(key, text):
        b[idx[key]] += "\n" + text + "\n"

    t = truth
    plant("q1", f"巡检编号 {t['code']}：核心网关每日巡检时必须使用口令 {t['pass']}。")
    plant("q2a", f"限流密钥 {t['k_d1']} 生效于 {t['t_d1']}。")
    plant("q2b", f"限流密钥 {t['k_true']} 生效于 {t['t_true']}。")
    plant("q2c", f"限流密钥 {t['k_d2']} 生效于 {t['t_d2']}。")
    for key, name, v in zip(("q3a", "q3b", "q3c"), ("alpha", "beta", "gamma"), t["thr"]):
        plant(key, f"服务 {name} 的超时阈值为 {v}ms。")
    tgt_block = idx["q4_tgt"] + 1
    plant("q4_ptr", f"本次演练的归档编号存放在第 {tgt_block} 段的『批次台账』里。")
    plant("q4_tgt", f"批次台账：本次演练的归档编号 {t['archive']}。")
    plant("q6", "为防止重复提交，接口层加了幂等令牌机制。")
    plant("depth_mid", f"中段哨点：中继站识别码为 {t['mid_pass']}。")
    plant("depth_late", f"末段哨点：末梢站识别码为 {t['late_pass']}。")
    pool = [i for i in range(n_blocks) if i not in taken]
    agg_blocks = sorted(random.Random(t["agg_k"] * 7919 + n_blocks).sample(
        pool, min(t["agg_k"], len(pool))))
    for i in agg_blocks:
        b[i] += "\n" + AGG_SENT + "\n"
    return "\n".join(b), agg_blocks


def ask_turns(truth: dict, n_turns: int) -> list:
    t = truth
    turns = [
        f"请只回复：收到 {t['code']}。",
        "核心网关每日巡检口令是什么？只回复口令本身。",
        ("请按编号回答以下六问，每题一行，格式「N. 答案」；答不上来写「不知道」：\n"
         f"1. 核心网关每日巡检的口令和巡检编号是什么？\n"
         f"2. 限流密钥 {t['k_true']} 的生效时间是什么？\n"
         "3. 服务 alpha、beta、gamma 各自的超时阈值是多少？\n"
         "4. 本次演练的归档编号是多少？\n"
         f"5. 『{AGG_SENT}』这句话在本会话中一共出现了几次？\n"
         "6. 如果同一个订单请求被重复提交两次，系统靠什么机制挡住第二次？\n"
         "7. 请依次给出：核心网关每日巡检口令、中继站识别码、末梢站识别码（用顿号分隔）。"),
    ]
    return turns[:n_turns]


def numbered_lines(ans: str) -> dict:
    lines = {}
    for ln in (ans or "").splitlines():
        m = re.match(r"\s*([1-7])[.、．)]\s*(.*)", ln)
        if m:
            lines.setdefault(m.group(1), m.group(2))
    return lines


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def first_int(s: str):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else None


def _q3_mapping_ok(q3: str, thr) -> bool:
    names = ("alpha", "beta", "gamma")
    ms = list(re.finditer("|".join(names), q3))
    for name, v in zip(names, thr):
        ok = False
        for i, m in enumerate(ms):
            if m.group() != name:
                continue
            nxt = ms[i + 1].start() if i + 1 < len(ms) else len(q3)
            fwd = re.findall(r"\d+", q3[m.end():nxt])
            prv = ms[i - 1].end() if i > 0 else 0
            bwd = re.findall(r"\d+", q3[prv:m.start()])
            if (fwd and int(fwd[0]) == v) or (bwd and int(bwd[-1]) == v):
                ok = True
                break
        if not ok:
            return False
    return True


def score_answer(ans: str, truth: dict) -> dict:
    t = truth
    ln = numbered_lines(ans)
    q1, q2, q3 = ln.get("1", ""), ln.get("2", ""), ln.get("3", "")
    q4, q5, q6 = ln.get("4", ""), ln.get("5", ""), ln.get("6", "")
    q1n, q2n, q4n = norm(q1), norm(q2), norm(q4)
    return {
        "q1_single": bool(norm(t["pass"]) in q1n and norm(t["code"]) in q1n),
        "q2_distractor": bool(norm(t["t_true"]) in q2n and norm(t["t_d1"]) not in q2n
                              and norm(t["t_d2"]) not in q2n),
        "q3_multivalue": _q3_mapping_ok(q3, t["thr"]),
        "q4_multihop": bool(norm(t["archive"]) in q4n),
        "q5_aggregation": first_int(q5) == t["agg_k"],
        "q6_nonliteral": bool("幂等" in q6 or "idempot" in q6.lower()),
    }


def overall(scores: dict) -> float:
    return sum(bool(scores[k]) for k in QUESTION_TYPES) / len(QUESTION_TYPES)


class ContextLimitError(Exception):
    def __init__(self, code, body):
        self.code, self.body = code, body
        super().__init__(f"HTTP {code}: {body[:200]}")


def call_api(base: str, key: str, model: str, msgs: list, *, temperature=0.0,
             max_tokens=2048, timeout=900, retries=2):
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens,
            "stream": False, "temperature": temperature}
    req = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(f"{base.rstrip('/')}/chat/completions",
                                       data=req,
                                       headers={"Content-Type": "application/json",
                                                "Authorization": f"Bearer {key}"}),
                timeout=timeout)
            d = json.loads(r.read().decode("utf-8"))
            u = d.get("usage") or {}
            return {"content": d["choices"][0]["message"]["content"],
                    "prompt_tokens": int(u.get("prompt_tokens") or 0),
                    "completion_tokens": int(u.get("completion_tokens") or 0),
                    "latency": round(time.time() - t0, 1)}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            if e.code in TRANSIENT and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code == 400 and re.search(
                    r"context|token|length|maximum|too (long|large)|range of input",
                    detail, re.I):
                raise ContextLimitError(e.code, detail)
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"网络错误: {e!r}") from None
    raise RuntimeError(f"网络错误: {last!r}")


def calibrate(base, key, model, args):
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": filler(999) + "\n请只回复：OK"}]
    r = call_api(base, key, model, msgs, temperature=args.temperature,
                 max_tokens=8, timeout=args.timeout)
    if r["prompt_tokens"] <= 0:
        raise RuntimeError("标定失败：引擎未返回 usage.prompt_tokens")
    # 一整块(110行)的实测 token，扣 system/问句开销
    return max(500.0, r["prompt_tokens"] - 30)


FALLBACK_TPB = 4600.0  # 90 块≈416k 的历史实测（qwen tokenizer）


def plan_sizes(target_tokens: int, tokens_per_block: float) -> tuple:
    tpl = tokens_per_block / 110.0
    n_blocks = max(24, round(target_tokens / tokens_per_block))
    if target_tokens >= n_blocks * tokens_per_block * 0.9:
        n_lines = 110
    else:
        n_lines = max(6, round(target_tokens / (n_blocks * tpl)))
    return n_blocks, n_lines


def blocks_for(target_tokens: int, tokens_per_block: float) -> int:
    return plan_sizes(target_tokens, tokens_per_block)[0]


def run_point(t, target, rep, seed, args, tokens_per_block):
    rng = random.Random(seed)
    truth = make_truth(rng)
    n_blocks, n_lines = plan_sizes(target, tokens_per_block)
    corpus, agg_blocks = build_corpus(n_blocks, truth, n_lines)
    turns = ask_turns(truth, args.turns)
    msgs = [{"role": "system", "content": SYSTEM}]
    rows = []
    rep_dir_note = None
    for i, ask in enumerate(turns, 1):
        content = (corpus + "\n" + ask) if i == 1 else ask
        msgs.append({"role": "user", "content": content})
        try:
            r = call_api(t["base_url"], t["api_key"], t["model"], msgs,
                         temperature=args.temperature, timeout=args.timeout)
        except ContextLimitError as e:
            return {"status": "unsupported", "model": t["model"], "target_tokens": target,
                    "rep": rep, "seed": seed, "n_blocks": n_blocks,
                    "error": str(e), "turns": rows}
        except Exception as e:
            rows.append({"turn": i, "error": str(e)[:300]})
            return {"status": "error", "model": t["model"], "target_tokens": target,
                    "rep": rep, "seed": seed, "n_blocks": n_blocks,
                    "error": str(e)[:300], "turns": rows}
        msgs.append({"role": "assistant", "content": r["content"]})
        rows.append({"turn": i, "latency": r["latency"],
                     "prompt_tokens": r["prompt_tokens"],
                     "completion_tokens": r["completion_tokens"],
                     "answer": r["content"][:1200]})
        print(f"  rep{rep} turn{i}: {r['latency']}s prompt={r['prompt_tokens']} "
              f"ans={r['content'][:48]!r}", flush=True)
    final = rows[-1]["answer"] if rows else ""
    scores = score_answer(final, truth) if args.turns >= 3 else {}
    q7 = norm(numbered_lines(final).get("7", ""))
    depth = {"early": bool(norm(truth["pass"]) in q7),
             "mid": bool(norm(truth["mid_pass"]) in q7),
             "late": bool(norm(truth["late_pass"]) in q7)}
    return {"status": "ok", "model": t["model"], "target_tokens": target,
            "achieved_tokens": rows[-1]["prompt_tokens"], "rep": rep, "seed": seed,
            "n_blocks": n_blocks, "n_lines": n_lines, "truth": truth, "agg_blocks": agg_blocks,
            "scores": scores, "overall": overall(scores) if scores else None,
            "depth_hits": depth, "turns": rows,
            "sent_total": sum(r["prompt_tokens"] for r in rows)}


def pick_window(windows, now=None):
    now = now or datetime.now()
    hm = now.strftime("%H:%M")
    for w in windows:
        s, e = w["start"], w["end"]
        if (s <= hm < e) if s <= e else (hm >= s or hm < e):
            return w
    return windows[0] if windows else {}


def estimate_cost(targets, lengths, reps, turns):
    total = 0.0
    detail = []
    for t in targets:
        w = pick_window((t.get("pricing") or {}).get("windows") or [])
        pin = float(w.get("input", 12.0))
        pout = float(w.get("output", 36.0))
        tin = sum(L * turns for L in lengths) * reps
        tout = 300 * turns * len(lengths) * reps
        c = tin / 1e6 * pin + tout / 1e6 * pout
        total += c
        detail.append((t["model"], tin, round(c, 2)))
    return total, detail


def aggregate(runs):
    groups = {}
    for r in runs:
        groups.setdefault((r["model"], r["target_tokens"]), []).append(r)
    cells = []
    for (model, target), rs in sorted(groups.items()):
        ok = [r for r in rs if r["status"] == "ok" and r.get("overall") is not None]
        cell = {"model": model, "target_tokens": target, "reps": len(rs),
                "valid": len(ok),
                "unsupported": sum(r["status"] == "unsupported" for r in rs),
                "errors": sum(r["status"] == "error" for r in rs)}
        if ok:
            ov = [r["overall"] for r in ok]
            cell["accuracy_mean"] = round(statistics.mean(ov), 3)
            cell["accuracy_sd"] = round(statistics.pstdev(ov), 3) if len(ov) > 1 else 0.0
            cell["achieved_tokens_mean"] = round(statistics.mean(
                r["achieved_tokens"] for r in ok))
            cell["per_question"] = {
                q: round(statistics.mean(bool(r["scores"][q]) for r in ok), 3)
                for q in QUESTION_TYPES}
            cell["depth"] = {
                d: round(statistics.mean(r["depth_hits"][d] for r in ok), 3)
                for d in ("early", "mid", "late")}
            cell["sent_total"] = sum(r["sent_total"] for r in ok)
        cells.append(cell)
    return cells


def write_summary_md(cells, path, args, cost_est):
    lines = ["# 云端模型原生上下文能力评测（直连，无代理）", "",
             f"- 套件版本: {EVAL_VERSION}（longctx-proxy 4.2.0 之后）",
             f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
             f"- 长度梯度: {args.lengths} · 每点重复: {args.reps} · temperature={args.temperature}",
             f"- 费用估算（无缓存折扣上限）: ≈{cost_est:.2f} 元", "",
             "| 模型 | 目标长度 | 实测长度 | 有效/总 | 准确率 | sd | 超限 | 错误 |",
             "|---|---|---|---|---|---|---|---|"]
    for c in cells:
        acc = f"{c['accuracy_mean']:.0%}" if "accuracy_mean" in c else "—"
        ach = c.get("achieved_tokens_mean", "—")
        lines.append(f"| {c['model']} | {c['target_tokens']} | {ach} | "
                     f"{c['valid']}/{c['reps']} | {acc} | {c.get('accuracy_sd', '—')} | "
                     f"{c['unsupported']} | {c['errors']} |")
    lines += ["", "## 分题型准确率", "",
              "| 模型 | 长度 | " + " | ".join(QUESTION_TYPES) + " | 早/中/晚深度 |",
              "|---|---|" + "---|" * 7]
    for c in cells:
        if "per_question" not in c:
            continue
        pq = " | ".join(f"{c['per_question'][q]:.0%}" for q in QUESTION_TYPES)
        dp = "/".join(f"{c['depth'][d]:.0%}" for d in ("early", "mid", "late"))
        lines.append(f"| {c['model']} | {c['target_tokens']} | {pq} | {dp} |")
    lines += ["", "判据：6 类探针客观判分（单针/干扰/多值映射/多跳/精确计数/非字面），",
              "标识匹配忽略空白；真值逐次随机化；深度命中=第7问显式召回早/中/晚哨点。",
              "unsupported=引擎拒绝该长度（400 context），本身即窗口上限证据。"]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="云端模型原生上下文能力评测（直连）")
    ap.add_argument("--models", default="", help="逗号分隔模型名；缺省=targets.json 启用行")
    ap.add_argument("--lengths", default="128k,256k,512k,1m", help="逗号分隔目标 token 长度")
    ap.add_argument("--reps", type=int, default=3, help="每点重复次数（默认 3）")
    ap.add_argument("--turns", type=int, default=3, choices=(1, 2, 3),
                    help="对话轮数；评分需 3 轮（默认 3）")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default="", help="报告目录（默认 ctx-eval-reports/<时间戳>）")
    ap.add_argument("--budget", type=float, default=50.0, help="费用估算上限（元），超过需 --yes")
    ap.add_argument("--yes", action="store_true", help="跳过费用确认")
    ap.add_argument("--dry-run", action="store_true", help="只构造语料与计划，不联网")
    args = ap.parse_args(argv)
    lengths = [parse_len(s) for s in args.lengths.split(",")]
    targets = load_targets()
    if args.models:
        names = [s.strip() for s in args.models.split(",")]
        targets = [t for t in targets if t["model"] in names]
        missing = set(names) - {t["model"] for t in targets}
        if missing:
            ap.error(f"targets.json 中找不到模型: {missing}")
    else:
        targets = [t for t in targets if t.get("enabled")]
    if not targets:
        ap.error("没有可用模型：targets.json 无启用行，请用 --models 指定")
    out = Path(args.out) if args.out else HERE / "ctx-eval-reports" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    cost, detail = estimate_cost(targets, lengths, args.reps, args.turns)
    print(f"context_eval {EVAL_VERSION}（直连评测，无代理）")
    print(f"计划: {len(targets)} 模型 × {len(lengths)} 长度 × {args.reps} 重复 × {args.turns} 轮")
    for m, tin, c in detail:
        print(f"  {m}: 输入≈{tin / 1e6:.2f}M tok, ≈{c:.2f} 元")
    print(f"费用估算上限 ≈ {cost:.2f} 元（前缀缓存命中时实际更低）")
    if args.dry_run:
        for t in targets:
            for L in lengths:
                rng = random.Random(f"{t['model']}|{L}|1")
                truth = make_truth(rng)
                nb, nl = plan_sizes(L, FALLBACK_TPB)
                corpus, agg = build_corpus(nb, truth, nl)
                assert corpus.count(AGG_SENT) == len(agg)
                print(f"[dry] {t['model']} L={L}: 块={nb} 行/块={nl} "
                      f"字符={len(corpus)} 聚合埋点={len(agg)} 真值样例={truth['code']}/{truth['pass']}")
        print("dry-run 完成：未发送任何请求")
        return 0
    if cost > args.budget and not args.yes:
        print(f"估算 {cost:.2f} 元超过预算 {args.budget} 元；确认后加 --yes 重跑。")
        return 2
    runs = []
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    for t in targets:
        base, key, model = t["base_url"], t["api_key"], t["model"]
        print(f"\n=== 模型 {model} ===", flush=True)
        try:
            tpb = calibrate(base, key, model, args)
        except ContextLimitError:
            tpb = FALLBACK_TPB
        except Exception as e:
            print(f"标定失败（{e}），回退估算 {FALLBACK_TPB:.0f} tok/块", flush=True)
            tpb = FALLBACK_TPB
        print(f"标定: {tpb:.2f} tok/块", flush=True)
        for L in lengths:
            unsupported = False
            for rep in range(1, args.reps + 1):
                if unsupported:
                    runs.append({"status": "unsupported", "model": model,
                                 "target_tokens": L, "rep": rep,
                                 "error": "同长度首已超限，跳过"})
                    continue
                seed = f"{run_id}|{model}|{L}|{rep}"
                print(f"[{model} L={L} rep{rep}/{args.reps}]", flush=True)
                r = run_point(t, L, rep, seed, args, tpb)
                runs.append(r)
                safe = re.sub(r"[^\w.-]", "_", model)
                (out / f"{safe}__L{L}__r{rep}.json").write_text(
                    json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
                if r["status"] == "unsupported":
                    unsupported = True
                    print(f"  引擎拒绝长度 {L}：记为 unsupported（窗口上限证据）", flush=True)
                elif r["status"] == "ok":
                    print(f"  得分 {r['overall']:.0%} 实测={r['achieved_tokens']} tok", flush=True)
                else:
                    print(f"  错误: {r['error'][:120]}", flush=True)
    cells = aggregate(runs)
    (out / "summary.json").write_text(json.dumps(
        {"version": EVAL_VERSION, "args": vars(args), "cost_estimate_cny": round(cost, 2),
         "cells": cells, "runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_md(cells, out / "summary.md", args, cost)
    print(f"\n报告: {out / 'summary.md'}")
    for c in cells:
        acc = f"{c['accuracy_mean']:.0%}" if "accuracy_mean" in c else "无有效样本"
        print(f"  {c['model']} L={c['target_tokens']}: {acc} "
              f"(有效 {c['valid']}/{c['reps']}, 超限 {c['unsupported']}, 错误 {c['errors']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())