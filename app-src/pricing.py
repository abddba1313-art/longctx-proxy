# -*- coding: utf-8 -*-
"""分时段价目表 + 压缩参数建议。

纯函数，不碰 Tk、不读文件，GUI 与测试共用。

价格的单位统一是「每百万 token 的金额」，币种由 model["unit"] 描述，
本模块只做算术，不换算汇率。

五档价格对应账单上的五项：输入 / 输出 / 输入（缓存命中）/ 显式缓存创建 /
显式缓存命中。后两者用 cache_read、cache_write 两个字段表示，
缓存模式由 target["cache"] 决定（none / auto / explicit）。
"""

from __future__ import annotations

import math
import unicodedata

CURVE_RATIOS = (0.15, 0.20, 0.25, 0.30, 0.34, 0.40, 0.50)
DEFAULT_UNIT = "元"
CACHE_MODES = ("none", "auto", "explicit")
CACHE_LABELS = {"none": "无缓存", "auto": "自动缓存", "explicit": "显式缓存"}


# ---------------------------------------------------------------- 时段
def hhmm_to_min(text: str) -> int:
    """'16:30' -> 990。格式不对抛 ValueError。"""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
    except Exception as exc:
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间超出范围：{text!r}")
    return h * 60 + m


def min_to_hhmm(minute: int) -> str:
    minute %= 24 * 60
    return f"{minute // 60:02d}:{minute % 60:02d}"


def in_window(minute: int, start: str, end: str) -> bool:
    """左闭右开。start > end 表示跨零点（如 16:30-00:30）。"""
    s, e = hhmm_to_min(start), hhmm_to_min(end)
    if s == e:
        return True
    if s < e:
        return s <= minute < e
    return minute >= s or minute < e


def window_for(model: dict, minute: int) -> dict | None:
    for w in (model or {}).get("windows", []):
        try:
            if in_window(minute, w.get("start", ""), w.get("end", "")):
                return w
        except ValueError:
            continue
    return None


def next_change(model: dict, minute: int) -> str | None:
    """下一个时段边界的时刻（用于提示「等到谷时再压」）。"""
    best = None
    for w in (model or {}).get("windows", []):
        for key in ("start", "end"):
            try:
                m = hhmm_to_min(w.get(key, ""))
            except ValueError:
                continue
            d = (m - minute) % (24 * 60)
            if d and (best is None or d < best):
                best = d
    return min_to_hhmm(minute + best) if best else None


def timeline(models: list[dict]) -> list[tuple[str, str, int]]:
    """把几张价目表的时段起点并成一条时间轴。

    返回 [(显示起点, 显示终点, 区间内的一分钟)]。两个模型的边界不一致时，
    例如完成模型 16:30 换价、压缩模型 08:30 换价，会得到三段而不是两段。
    """
    starts: set[int] = set()
    first: dict[int, dict] = {}
    for model in models:
        for w in (model or {}).get("windows", []):
            try:
                s = hhmm_to_min(w.get("start", ""))
            except ValueError:
                continue
            starts.add(s)
            first.setdefault(s, w)
    if not starts:
        return []
    pts = sorted(starts)
    if len(pts) == 1:
        w = first[pts[0]]
        return [(w.get("start", "00:00"), w.get("end", "00:00"),
                 (pts[0] + 1) % (24 * 60))]
    return [(min_to_hhmm(pts[i]), min_to_hhmm(pts[(i + 1) % len(pts)]),
             (pts[i] + 1) % (24 * 60)) for i in range(len(pts))]


# ---------------------------------------------------------------- 默认价目
def default_pricing() -> dict:
    """模板价：只当占位，用之前按实际账单价改。"""
    return {
        "version": 1,
        "advisor": {"delta": 8000, "ratio": 0.34, "context_tokens": 100000,
                    "cache_mode": "auto", "output_tokens": 1000},
        "target": {
            "name": "云端完成任务模型",
            "unit": DEFAULT_UNIT,
            "cache": "auto",
            "windows": [
                {"start": "00:30", "end": "16:30", "input": 12.0, "output": 36.0,
                 "cache_read": 1.5, "cache_write": 15.0,
                 "cache_read_explicit": 1.0},
                {"start": "16:30", "end": "00:30", "input": 6.0, "output": 18.0,
                 "cache_read": 0.75, "cache_write": 7.5,
                 "cache_read_explicit": 0.5},
            ],
        },
        "compressor": {
            "name": "DeepSeek-V4.1-Flash（压缩）",
            "unit": DEFAULT_UNIT,
            "windows": [
                # 时段是占位，按服务商实际的高峰/空闲时间改
                {"start": "00:30", "end": "08:30", "input": 1.0,
                 "output": 4.0, "cache_read": 0.02},
                {"start": "08:30", "end": "00:30", "input": 2.0,
                 "output": 8.0, "cache_read": 0.04},
            ],
        },
        "local": {
            "prefill": 1000, "decode": 66,
            "comp_prefill": 3000, "comp_decode": 100,
            "comp_latency": 1.0, "time_value": 0.0,
            "concurrency": 16, "chunk": 12000,
        },
    }


# ---------------------------------------------------------------- 建议
def cache_mode(target: dict, use_cache: bool = True) -> str:
    """none / auto / explicit。老价目没有 cache 字段时按 use_cache 推断。"""
    mode = str((target or {}).get("cache") or "").lower()
    if mode in CACHE_MODES:
        return mode
    return "auto" if use_cache else "none"


def _prices(target: dict, compressor: dict, mode: str
            ) -> tuple[float, float, float, float, float, float, float, float]:
    """(目标输入, 实际命中, 实际缓存写, 目标输出, 压缩输入, 压缩输出,
        自动缓存命中, 显式缓存命中)，单位：每 token。

    账单上「输入（缓存命中）」和「显式缓存命中」是两个价，分别存在
    cache_read / cache_read_explicit；后者不填就沿用前者。
    cache_read / cache_write 显式填 0 要当「免费」，不能用 `or` 退回输入价。
    """
    p_in = float(target.get("input", 0))
    p_out = float(target.get("output", 0))
    p_auto = target.get("cache_read")
    if p_auto is None:
        p_auto = p_in
    p_auto = float(p_auto)
    p_expl = target.get("cache_read_explicit")
    if p_expl is None:
        p_expl = p_auto
    p_expl = float(p_expl)

    if mode == "none":
        p_hit, p_write = p_in, 0.0
    elif mode == "explicit":
        p_hit = p_expl
        # 显式缓存才有「创建」价；自动缓存按普通输入价计
        p_write = float(target.get("cache_write") or p_in)
    else:
        p_hit, p_write = p_auto, 0.0
    return (p_in / 1e6, p_hit / 1e6, p_write / 1e6, p_out / 1e6,
            float(compressor.get("input", 0)) / 1e6,
            float(compressor.get("output", 0)) / 1e6,
            p_auto / 1e6, p_expl / 1e6)


def compute_advice(target_model: dict, compressor_model: dict, delta: int,
                   ratio: float, context_tokens: int, use_cache: bool,
                   minute: int, min_net: float = 0.0,
                   output_tokens: int = 0,
                   with_periods: bool = True) -> dict:
    """给定时段与参数，算「该不该压、从多长开始压、压多狠」。

    注意：价格只能决定「压了划不划算」和「从多长开始划算」；
    压缩比本身是单调的（压得越狠越省钱），所以 ratio 由质量底线决定，
    这里只给出不同 ratio 的收益曲线。

    output_tokens 不影响净收益（两边一样，会抵消），但用来算整轮真实花销，
    否则 ¥36 的输出价根本不在视野里。
    """
    tw = window_for(target_model, minute)
    cw = window_for(compressor_model, minute)
    if not tw or not cw:
        return {"ok": False, "reason": "价目表没有覆盖当前时间，请检查时段设置"}

    mode = cache_mode(target_model, use_cache)
    p_in, p_hit, p_write, p_out, c_in, c_out, p_auto, p_expl = _prices(tw, cw, mode)
    if p_in <= 0:
        return {"ok": False, "reason": "完成模型输入价必须大于 0"}

    ratio = max(0.01, min(0.99, float(ratio)))
    delta = max(1, int(delta))
    context_tokens = max(1, int(context_tokens))
    output_tokens = max(0, int(output_tokens))

    keep = 1.0 - ratio
    # 省下的是「老历史」这一截；成本是「这一轮新增」被压一次
    saving = keep * p_hit * context_tokens
    cost = (c_in + ratio * c_out) * delta
    net = saving - cost

    # 整轮花销：老历史 + 新增 + 输出，压缩前后各算一遍
    out_cost = output_tokens * p_out
    baseline_total = context_tokens * p_hit + delta * p_in + out_cost
    compressed_total = ratio * context_tokens * p_hit + delta * p_in + out_cost + cost
    # 显式缓存：第一次要把压缩后的前缀写进缓存，之后才能按命中价读
    cache_build = ratio * context_tokens * p_write

    denom = keep * p_hit
    if denom <= 0:
        threshold_hist = None
    else:
        threshold_hist = int(round(cost / denom))
    threshold_total = None if threshold_hist is None else threshold_hist + delta
    target_msg = None if threshold_total is None else max(4096, int(round(ratio * threshold_hist)))

    curve = []
    for r in CURVE_RATIOS:
        s = (1 - r) * p_hit * context_tokens
        c = (c_in + r * c_out) * delta
        curve.append({"ratio": r, "saving": s, "cost": c, "net": s - c})

    result = {
        "ok": True,
        "reason": "",
        "cache_mode": mode,
        "target_window": tw,
        "compressor_window": cw,
        "unit": target_model.get("unit") or DEFAULT_UNIT,
        "prices": {
            "target_input": p_in * 1e6,
            "target_hit": p_hit * 1e6,
            "target_cache": p_auto * 1e6,
            "target_cache_explicit": p_expl * 1e6,
            "target_write": p_write * 1e6,
            "target_output": p_out * 1e6,
            "compressor_input": c_in * 1e6,
            "compressor_output": c_out * 1e6,
        },
        "ratio": ratio,
        "delta": delta,
        "context_tokens": context_tokens,
        "output_tokens": output_tokens,
        "use_cache": mode != "none",
        "saving": saving,
        "cost": cost,
        "net": net,
        "worth": net > min_net,
        "min_net": min_net,
        "baseline_total": baseline_total,
        "compressed_total": compressed_total,
        "cache_build": cache_build,
        "threshold_history": threshold_hist,
        "threshold_total": threshold_total,
        "target_msg": target_msg,
        "payback_turns": (cost / saving) if saving > 0 else None,
        "curve": curve,
        "next_change": next_change(target_model, minute),
    }
    if with_periods:
        result["periods"] = _advice_periods(
            target_model, compressor_model, delta, ratio, context_tokens,
            use_cache, min_net, output_tokens, tw, cw)
        vals = [p["threshold_total"] for p in result["periods"]
                if p.get("threshold_total") is not None]
        result["max_threshold_total"] = max(vals) if vals else None
    return result


def _advice_periods(target_model: dict, compressor_model: dict, delta: int,
                    ratio: float, context_tokens: int, use_cache: bool,
                    min_net: float, output_tokens: int,
                    cur_target: dict, cur_comp: dict) -> list[dict]:
    """按两张价目表的所有边界切一天，逐段算方案。"""
    out = []
    for start, end, minute in timeline([target_model, compressor_model]):
        sub = compute_advice(
            target_model, compressor_model, delta, ratio, context_tokens,
            use_cache, minute, min_net=min_net, output_tokens=output_tokens,
            with_periods=False)
        if not sub.get("ok"):
            continue
        tw, cw = sub["target_window"], sub["compressor_window"]
        current = (
            (tw.get("start"), tw.get("end")) ==
            (cur_target.get("start"), cur_target.get("end"))
            and (cw.get("start"), cw.get("end")) ==
            (cur_comp.get("start"), cur_comp.get("end"))
        )
        out.append({
            "start": start, "end": end,
            "target_window": tw, "compressor_window": cw,
            "prices": sub["prices"],
            "saving": sub["saving"], "cost": sub["cost"], "net": sub["net"],
            "worth": sub["worth"],
            "threshold_total": sub["threshold_total"],
            "threshold_history": sub["threshold_history"],
            "target_msg": sub["target_msg"],
            "current": current,
        })
    return out


def format_advice(r: dict) -> str:
    """把 compute_advice 的结果排成人能读的多行文本。"""
    if not r.get("ok"):
        return "无法计算：" + r.get("reason", "未知原因")

    u = r["unit"]
    p = r["prices"]
    mode = r["cache_mode"]
    write = f"，缓存写 {p['target_write']:g}" if mode == "explicit" else ""
    lines = [
        f"当前时段价格（{u}/百万 token）",
        f"  完成模型 {r['target_window']['start']}-{r['target_window']['end']}："
        f"输入 {p['target_input']:g}，输出 {p['target_output']:g}，"
        f"缓存命中 {p['target_cache']:g}，显式命中 {p['target_cache_explicit']:g}"
        f"{write}（{CACHE_LABELS.get(mode, mode)}）",
        f"  压缩模型 {r['compressor_window']['start']}-{r['compressor_window']['end']}："
        f"输入 {p['compressor_input']:g}，输出 {p['compressor_output']:g}",
        "",
        f"按每轮新增 {r['delta']:,} token、压缩比 {r['ratio']:.2f}、"
        f"当前历史 {r['context_tokens']:,} token、"
        f"每轮输出 {r['output_tokens']:,} token 估算：",
        f"  每轮省 {r['saving']:.4f} {u}，压缩成本 {r['cost']:.4f} {u}，"
        f"净收益 {r['net']:+.4f} {u}/轮",
        f"  整轮花销：{r['baseline_total']:.4f} → {r['compressed_total']:.4f} {u}",
    ]
    if r["cache_build"] > 0:
        lines.append(f"  显式缓存首次写入：{r['cache_build']:.4f} {u}（一次性）")
    if r["payback_turns"] is not None:
        lines.append(f"  回本：约 {r['payback_turns']:.2f} 轮")
    if not r["worth"]:
        lines.append("  结论：不划算，别开压缩（或把压缩模型换便宜一点）")
    else:
        lines.append("  结论：划算")
    periods = r.get("periods") or []
    if periods:
        lines.append("")
        lines.append("分时段方案（按系统时间自动同步，← 当前时段）：")
        lines.append(f"  {'时段':<13}{'完成模型':<10}{'压缩模型':<10}"
                     f"{'建议阈值':>10}{'每轮净':>11}")
        for p in periods:
            th = (f"{p['threshold_total']:,}"
                  if p.get("threshold_total") is not None else "—")
            pp = p["prices"]
            mark = "  ←当前" if p.get("current") else ""
            lines.append(
                f"  {p['start']}-{p['end']:<8}"
                f"{pp['target_input']:g}/{pp['target_output']:g}    "
                f"{pp['compressor_input']:g}/{pp['compressor_output']:g}     "
                f"{th:>10}{p['net']:>+10.4f}{mark}")
        if r.get("max_threshold_total") is not None:
            lines.append(f"  全时段保守阈值：{r['max_threshold_total']:,}"
                         f"（取各段最大，整天只用一个值）")
        lines.append("  价格只影响阈值和净收益；压缩比仍由质量底线决定。")
    if r["threshold_history"] is not None:
        lines.append("")
        lines.append(f"建议触发阈值：正文超过 {r['threshold_history']:,} token 开始压")
        lines.append(f"  → 压缩触发阈值 = {r['threshold_total']:,}"
                     f"（历史阈值 + 本轮新增 {r['delta']:,}）")
        lines.append(f"  → 压缩目标正文 = {r['target_msg']:,}")
        lines.append("  说明：价格只能决定划不划算；压缩比越小越省钱，"
                     "真正的下限是你的质量底线。")
    else:
        lines.append("  建议触发阈值：目标模型的缓存命中价或输入价为 0，"
                     "压缩永远不划算，不建议开启")
    if r.get("next_change"):
        lines.append(f"  下一个价目边界：{r['next_change']}")

    lines.append("")
    lines.append(f"{'压缩比':>6} {'每轮省':>10} {'压缩成本':>10} {'净收益':>10}")
    for c in r["curve"]:
        mark = "  ← 当前" if abs(c["ratio"] - r["ratio"]) < 1e-9 else ""
        lines.append(f"{c['ratio']:>6.2f} {c['saving']:>10.4f} "
                     f"{c['cost']:>10.4f} {c['net']:>+10.4f}{mark}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 本地模型时间账
LOCAL_SIZES = (50000, 100000, 200000, 500000, 1000000)


def _effective_lane_rate(per_lane: float, agg: float, bench_conc: int,
                         lanes: int) -> float:
    """按实测并发合计值折算每路速度。

    实测给出的是 bench_conc 路同时跑时的合计吞吐。段数不足时并发效率
    介于单路（100%）和满并发之间，这里按路数线性插值；没有实测值时
    保持原来的“线性放大”假设。聚合吞吐比线性还高时不额外奖励。
    """
    per_lane = max(0.001, float(per_lane))
    try:
        agg = float(agg or 0)
        bench_conc = int(bench_conc or 0)
        lanes = max(1, int(lanes))
    except (TypeError, ValueError):
        return per_lane
    if agg <= 0 or bench_conc < 2:
        return per_lane
    eff_full = agg / (per_lane * bench_conc)
    if eff_full >= 1.0:
        return per_lane
    if lanes >= bench_conc:
        return max(0.001, per_lane * eff_full)
    if lanes <= 1:
        return per_lane
    eff = 1.0 + (eff_full - 1.0) * (lanes - 1) / (bench_conc - 1)
    return max(0.001, per_lane * eff)


def _digest_wall(tokens: float, chunk: float, conc: int,
                 comp_prefill: float, comp_decode: float,
                 ratio: float, latency: float,
                 agg_prefill: float = 0.0,
                 agg_decode: float = 0.0,
                 bench_conc: int = 0) -> tuple[float, int, int]:
    """压缩 tokens 的墙钟时间。

    longctx 按 CHUNK 切段、用 CONCURRENCY 路并发；返回 (秒, 段数, 波数)。
    一段以内按单路实际长度算；多段优先用测速得到的并发合计值折算，
    没有实测值时退回“每路线性放大”的旧假设。
    """
    tokens = max(1.0, float(tokens))
    chunk = max(1.0, float(chunk))
    conc = max(1, int(conc))
    latency = max(0.0, float(latency or 0))
    if tokens <= chunk:
        return (latency + tokens / comp_prefill + ratio * tokens / comp_decode,
                1, 1)
    chunks = int(math.ceil(tokens / chunk))
    waves = int(math.ceil(chunks / conc))
    lanes = min(chunks, conc)
    p_rate = _effective_lane_rate(comp_prefill, agg_prefill, bench_conc, lanes)
    d_rate = _effective_lane_rate(comp_decode, agg_decode, bench_conc, lanes)
    wave_time = latency + chunk / p_rate + ratio * chunk / d_rate
    return waves * wave_time, chunks, waves


def _first_threshold(ratio: float, prefill: float, comp_prefill: float,
                     comp_decode: float, chunk: float, conc: int,
                     latency: float, agg_prefill: float = 0.0,
                     agg_decode: float = 0.0,
                     bench_conc: int = 0) -> float | None:
    """首次压缩整段历史时，从多长开始省时（分段/并发是阶梯函数）。"""
    small = (1 - ratio) / prefill - 1 / comp_prefill - ratio / comp_decode
    candidates = []
    if small > 0:
        n = latency / small
        if n <= chunk:
            candidates.append(n)
    p_rate = _effective_lane_rate(comp_prefill, agg_prefill, bench_conc, conc)
    d_rate = _effective_lane_rate(comp_decode, agg_decode, bench_conc, conc)
    wave_time = latency + chunk / p_rate + ratio * chunk / d_rate
    for waves in range(1, 10000):
        n = waves * wave_time * prefill / (1 - ratio)
        if n <= waves * conc * chunk:
            candidates.append(max(n, chunk))
            break
    return min(candidates) if candidates else None


def compute_local_time(compressor_model: dict, delta: int, ratio: float,
                       context_tokens: int, output_tokens: int,
                       prefill_tps: float, decode_tps: float,
                       comp_prefill_tps: float, comp_decode_tps: float,
                       comp_latency: float, minute: int,
                       time_value_per_hour: float = 0.0,
                       concurrency: int = 16,
                       chunk_tokens: int = 12000,
                       comp_prefill_agg: float = 0.0,
                       comp_decode_agg: float = 0.0,
                       bench_concurrency: int = 0,
                       with_periods: bool = True) -> dict:
    """本地模型当答题端：压缩能省多少时间、花多少钱。

    两个口径：
    - 稳态：老消息已有 [longctx] 标记，每轮只压新增 delta；
    - 首次：整段历史还没压，要按 CHUNK 切段、CONCURRENCY 路并发。

    本地模型不收 token 费，所以压缩在钱上是纯支出；它买到的只有时间。
    """
    cw = window_for(compressor_model, minute)
    if not cw:
        return {"ok": False, "reason": "压缩模型价目表没有覆盖当前时间，请检查时段设置"}
    try:
        prefill_tps = float(prefill_tps)
        decode_tps = float(decode_tps)
        comp_prefill_tps = float(comp_prefill_tps)
        comp_decode_tps = float(comp_decode_tps)
        comp_latency = max(0.0, float(comp_latency or 0))
        comp_prefill_agg = max(0.0, float(comp_prefill_agg or 0))
        comp_decode_agg = max(0.0, float(comp_decode_agg or 0))
        bench_concurrency = max(0, int(bench_concurrency or 0))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "预填充 / decode 速度必须是数字"}
    if min(prefill_tps, decode_tps, comp_prefill_tps, comp_decode_tps) <= 0:
        return {"ok": False, "reason": "速度必须大于 0"}

    ratio = max(0.01, min(0.99, float(ratio)))
    delta = max(1, int(delta))
    context_tokens = max(1, int(context_tokens))
    output_tokens = max(0, int(output_tokens))
    concurrency = max(1, int(concurrency))
    chunk_tokens = max(256, int(chunk_tokens))
    n = context_tokens + delta

    p_in = float(cw.get("input", 0)) / 1e6
    p_out = float(cw.get("output", 0)) / 1e6
    # 稳态只压新增；首次要压整段历史
    cost_turn = delta * p_in + ratio * delta * p_out
    cost_first = n * p_in + ratio * n * p_out

    turn_wall, turn_chunks, turn_waves = _digest_wall(
        delta, chunk_tokens, concurrency, comp_prefill_tps, comp_decode_tps,
        ratio, comp_latency, comp_prefill_agg, comp_decode_agg,
        bench_concurrency)
    first_wall, first_chunks, first_waves = _digest_wall(
        n, chunk_tokens, concurrency, comp_prefill_tps, comp_decode_tps,
        ratio, comp_latency, comp_prefill_agg, comp_decode_agg,
        bench_concurrency)

    baseline = n / prefill_tps + output_tokens / decode_tps
    decode_time = output_tokens / decode_tps
    after_prefill = ratio * n / prefill_tps
    compressed = turn_wall + after_prefill + decode_time
    saved = baseline - compressed
    first_compressed = first_wall + after_prefill + decode_time
    first_saved = baseline - first_compressed

    # 稳态阈值：每轮压缩耗时固定，省时随 n 线性增长
    threshold_n = turn_wall * prefill_tps / (1 - ratio)
    total = max(delta, int(round(threshold_n)))
    hist = max(0, total - delta)
    target_msg = max(4096, int(round(ratio * hist)))

    # 代理不是每轮都压：老消息带 [longctx] 标记后直接跳过，只有正文重新超过
    # PROXY_THRESHOLD 才触发；触发时把这段时间攒下的新增一起压。
    # 压缩后正文约 ratio*n，再长回 threshold 需要 interval 轮。
    compressed_total = ratio * n
    if compressed_total >= total:
        batch_interval = 1
    else:
        batch_interval = max(1, int(math.ceil((total - compressed_total) / delta)))
    batch_tokens = batch_interval * delta
    batch_wall, batch_chunks, batch_waves = _digest_wall(
        batch_tokens, chunk_tokens, concurrency, comp_prefill_tps,
        comp_decode_tps, ratio, comp_latency, comp_prefill_agg,
        comp_decode_agg, bench_concurrency)
    batch_avg_wall = batch_wall / batch_interval
    first_threshold_n = _first_threshold(
        ratio, prefill_tps, comp_prefill_tps, comp_decode_tps,
        chunk_tokens, concurrency, comp_latency, comp_prefill_agg,
        comp_decode_agg, bench_concurrency)

    rate = max(0.0, float(time_value_per_hour or 0)) / 3600
    time_gain = saved * rate
    net = time_gain - cost_turn
    econ_threshold_n = None
    econ_total = None
    if rate > 0:
        econ_threshold_n = (turn_wall + cost_turn / rate) * prefill_tps / (1 - ratio)
        econ_total = max(delta, int(round(econ_threshold_n)))

    rows = []
    for size in LOCAL_SIZES:
        n2 = size + delta
        tw, _, _ = _digest_wall(delta, chunk_tokens, concurrency,
                                comp_prefill_tps, comp_decode_tps, ratio,
                                comp_latency, comp_prefill_agg,
                                comp_decode_agg, bench_concurrency)
        fw, _, _ = _digest_wall(n2, chunk_tokens, concurrency,
                                comp_prefill_tps, comp_decode_tps, ratio,
                                comp_latency, comp_prefill_agg,
                                comp_decode_agg, bench_concurrency)
        s2 = n2 * (1 - ratio) / prefill_tps - tw
        fs2 = n2 * (1 - ratio) / prefill_tps - fw
        rows.append({
            "context": size,
            "n": n2,
            "baseline": n2 / prefill_tps + decode_time,
            "steady": tw + ratio * n2 / prefill_tps + decode_time,
            "first": fw + ratio * n2 / prefill_tps + decode_time,
            "saved": s2,
            "first_saved": fs2,
            "net": s2 * rate - cost_turn,
        })

    result = {
        "ok": True,
        "reason": "",
        "compressor_window": cw,
        "unit": compressor_model.get("unit") or DEFAULT_UNIT,
        "prices": {"input": p_in * 1e6, "output": p_out * 1e6},
        "ratio": ratio,
        "delta": delta,
        "context_tokens": context_tokens,
        "output_tokens": output_tokens,
        "n": n,
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "comp_prefill_tps": comp_prefill_tps,
        "comp_decode_tps": comp_decode_tps,
        "comp_prefill_agg": comp_prefill_agg,
        "comp_decode_agg": comp_decode_agg,
        "bench_concurrency": bench_concurrency,
        "comp_latency": comp_latency,
        "concurrency": concurrency,
        "chunk_tokens": chunk_tokens,
        "baseline": baseline,
        "decode_time": decode_time,
        "after_prefill": after_prefill,
        "turn_wall": turn_wall,
        "turn_chunks": turn_chunks,
        "turn_waves": turn_waves,
        "batch_interval": batch_interval,
        "batch_tokens": batch_tokens,
        "batch_chunks": batch_chunks,
        "batch_waves": batch_waves,
        "batch_wall": batch_wall,
        "batch_avg_wall": batch_avg_wall,
        "compressed": compressed,
        "saved": saved,
        "first_wall": first_wall,
        "first_chunks": first_chunks,
        "first_waves": first_waves,
        "first_compressed": first_compressed,
        "first_saved": first_saved,
        "cost_turn": cost_turn,
        "cost_first": cost_first,
        "time_value_per_hour": max(0.0, float(time_value_per_hour or 0)),
        "time_gain": time_gain,
        "net": net,
        "worth_time": saved > 0,
        "worth_money": net > 0,
        "payback_turns": (first_wall / saved) if saved > 0 else None,
        "threshold_n": threshold_n,
        "threshold_history": hist,
        "threshold_total": total,
        "target_msg": target_msg,
        "first_threshold_n": first_threshold_n,
        "econ_threshold_n": econ_threshold_n,
        "econ_threshold_total": econ_total,
        "rows": rows,
    }
    if with_periods:
        result["periods"] = _local_periods(
            compressor_model, delta, ratio, context_tokens, output_tokens,
            prefill_tps, decode_tps, comp_prefill_tps, comp_decode_tps,
            comp_latency, time_value_per_hour, concurrency, chunk_tokens, cw,
            comp_prefill_agg, comp_decode_agg, bench_concurrency)
        vals = [p["threshold_total"] for p in result["periods"]
                if p.get("threshold_total") is not None]
        result["max_threshold_total"] = max(vals) if vals else None
    return result


def _local_periods(compressor_model: dict, delta: int, ratio: float,
                   context_tokens: int, output_tokens: int,
                   prefill_tps: float, decode_tps: float,
                   comp_prefill_tps: float, comp_decode_tps: float,
                   comp_latency: float, time_value_per_hour: float,
                   concurrency: int, chunk_tokens: int,
                   cur_window: dict,
                   comp_prefill_agg: float = 0.0,
                   comp_decode_agg: float = 0.0,
                   bench_concurrency: int = 0) -> list[dict]:
    """按压缩模型价目表的所有时段，逐段算成本和阈值。"""
    out = []
    for start, end, minute in timeline([compressor_model]):
        sub = compute_local_time(
            compressor_model, delta, ratio, context_tokens, output_tokens,
            prefill_tps, decode_tps, comp_prefill_tps, comp_decode_tps,
            comp_latency, minute, time_value_per_hour, concurrency,
            chunk_tokens, comp_prefill_agg, comp_decode_agg,
            bench_concurrency, with_periods=False)
        if not sub.get("ok"):
            continue
        w = sub["compressor_window"]
        out.append({
            "start": start, "end": end,
            "window": w,
            "prices": sub["prices"],
            "cost_turn": sub["cost_turn"],
            "cost_first": sub["cost_first"],
            "threshold_total": sub["threshold_total"],
            "threshold_history": sub["threshold_history"],
            "target_msg": sub["target_msg"],
            "econ_threshold_total": sub["econ_threshold_total"],
            "saved": sub["saved"],
            "net": sub["net"],
            "current": (w.get("start"), w.get("end")) ==
                       (cur_window.get("start"), cur_window.get("end")),
        })
    return out


def _cell(text: str, width: int, align: str = "right") -> str:
    """按显示宽度补齐；中文/全角字符算 2 格，保证表头和数据列对齐。"""
    used = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)
    pad = max(0, width - used)
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    if align == "left":
        return text + " " * pad
    return " " * pad + text


def format_local_advice(r: dict) -> str:
    """把 compute_local_time 的结果排成人能读的文本。"""
    if not r.get("ok"):
        return "无法计算：" + r.get("reason", "未知原因")

    u = r["unit"]
    p = r["prices"]
    w = r["compressor_window"]
    lines = [
        "本地模型时间账",
        f"  本地 {r['prefill_tps']:g} tok/s 预填充，{r['decode_tps']:g} tok/s 生成",
        f"  压缩模型 {w['start']}-{w['end']}：输入 {p['input']:g}，"
        f"输出 {p['output']:g}（{u}/百万 token）",
        f"  分段 {r['chunk_tokens']:,} token，{r['concurrency']} 路并发",
    ]
    if r.get("comp_decode_agg", 0) > 0:
        lines.append(
            f"  压缩速度：单路 {r['comp_prefill_tps']:g} 预填 / "
            f"{r['comp_decode_tps']:g} 生成 tok/s；"
            f"{r['bench_concurrency']} 路实测合计 "
            f"{r['comp_prefill_agg']:g} / {r['comp_decode_agg']:g}")
    else:
        lines.append(
            f"  压缩速度：单路 {r['comp_prefill_tps']:g} 预填 / "
            f"{r['comp_decode_tps']:g} 生成 tok/s（没有并发实测值）")
    def _mode(chunks: int) -> str:
        if chunks == 1:
            return "单路"
        if r.get("comp_decode_agg", 0) > 0:
            return f"按 {r['bench_concurrency']} 路实测合计"
        return "按线性并发假设"

    steady_mode = _mode(r["turn_chunks"])
    batch_mode = _mode(r["batch_chunks"])
    first_mode = _mode(r["first_chunks"])
    lines += [
        "",
        f"正文 {r['context_tokens']:,} + 本轮新增 {r['delta']:,} "
        f"= {r['n']:,} token",
        f"  不压：预填 {r['n'] / r['prefill_tps']:.1f}s"
        f" + 生成 {r['decode_time']:.1f}s = {r['baseline']:.1f}s",
        f"  稳态（最坏情况：每轮都触发）：只压新增 {r['delta']:,}，"
        f"{r['turn_chunks']} 段 / {r['turn_waves']} 波，{r['turn_wall']:.1f}s"
        f"（{steady_mode}）",
        f"      + 本地预填 {r['after_prefill']:.1f}s"
        f" + 生成 {r['decode_time']:.1f}s = {r['compressed']:.1f}s",
        f"      时间差：{r['saved']:+.1f}s"
        + ("（压完更快）" if r["saved"] > 0 else "（压完更慢）"),
        f"  批量（按建议阈值，约每 {r['batch_interval']} 轮触发一次）："
        f"一次压 {r['batch_tokens']:,}，"
        f"{r['batch_chunks']} 段 / {r['batch_waves']} 波，{r['batch_wall']:.1f}s，"
        f"摊到每轮 {r['batch_avg_wall']:.1f}s（{batch_mode}）",
        f"  首次（整段未压）：{r['n']:,} token，"
        f"{r['first_chunks']} 段 / {r['first_waves']} 波，{r['first_wall']:.1f}s"
        f"（{first_mode}）",
        f"      压后 {r['first_compressed']:.1f}s，"
        f"时间差 {r['first_saved']:+.1f}s",
    ]
    if r["payback_turns"] is not None:
        lines.append(f"      首次压缩的一次性耗时，约 {r['payback_turns']:.2f} 轮省回来")

    lines += [
        "",
        f"钱：稳态每轮 {r['cost_turn']:.4f} {u}（只压新增），"
        f"首次 {r['cost_first']:.4f} {u}",
        f"  本地模型不收 token 费，压缩在钱上是纯支出",
    ]
    if r["time_value_per_hour"] > 0:
        lines.append(f"  按时间价值 {r['time_value_per_hour']:g} {u}/小时折算："
                     f"省时值 {r['time_gain']:+.4f} {u}，"
                     f"净 {r['net']:+.4f} {u}/轮")
    else:
        lines.append("  时间价值填 0：只看钱，结论一定是「不划算」")

    periods = r.get("periods") or []
    if periods:
        lines.append("")
        lines.append("分时段方案（按系统时间自动同步，← 当前时段）：")
        lines.append(f"  {'时段':<13}{'压缩价':<10}{'每轮成本':>9}{'经济阈值':>10}")
        for p in periods:
            econ = (f"{p['econ_threshold_total']:,}"
                    if p.get("econ_threshold_total") is not None else "—")
            mark = "  ←当前" if p.get("current") else ""
            lines.append(
                f"  {p['start']}-{p['end']:<8}"
                f"{p['prices']['input']:g}/{p['prices']['output']:g}      "
                f"{p['cost_turn']:>9.4f}{econ:>10}{mark}")
        lines.append(f"  时间阈值 {r['threshold_total']:,} 与价格无关；"
                     "价格只改每轮成本和经济阈值。")
        if r.get("max_threshold_total") is not None:
            lines.append(f"  全时段保守阈值：{r['max_threshold_total']:,}")

    lines.append("")
    lines.append(f"稳态时间阈值：正文超过 {r['threshold_total']:,} token 开始压才省时")
    lines.append(f"  → 压缩触发阈值 = {r['threshold_total']:,}")
    lines.append(f"  → 压缩目标正文 = {r['target_msg']:,}")
    if r["first_threshold_n"] is None:
        lines.append("首次整段压缩：按当前并发与速度，整段压完仍不省时；"
                     "别把阈值调低后硬开")
    else:
        lines.append(f"首次整段压缩打平：正文约 {r['first_threshold_n']:,.0f} token")
    if r["econ_threshold_total"] is not None:
        lines.append(f"经济阈值（含时间价值）：{r['econ_threshold_total']:,} token")

    # 表头按显示宽度居中：中文占 2 格；每个槽位含数据列后那个空格。
    lines += ["", (_cell("正文", 11, "center") + _cell("不压", 10, "center")
                   + _cell("稳态压", 10, "center") + _cell("稳态省", 10, "center")
                   + _cell("首轮省", 10, "center") + _cell("每轮净", 12, "center"))]
    for row in r["rows"]:
        net = f"{row['net']:>+9.4f} {u}"
        lines.append(f"{row['n']:>10,} {row['baseline']:>8.1f}s "
                     f"{row['steady']:>8.1f}s {row['saved']:>+8.1f}s "
                     f"{row['first_saved']:>+8.1f}s {_cell(net, 12)}")
    lines.append("")
    lines.append("说明：老消息压过一次后带 [longctx] 标记，后续轮次直接跳过，"
                 "不会重压整段历史。")
    lines.append("      每轮仍要预填压缩后的历史（约 压缩比 × 正文），这部分省不掉。")
    if r.get("comp_decode_agg", 0) > 0:
        lines.append(
            f"说明：多段压缩按实测 {r['bench_concurrency']} 路合计值折算；"
            "段数不足时按路数插值，单段仍按单路速度。")
    else:
        lines.append("说明：没有并发实测值，暂按并发路数线性放大吞吐。"
                     "到「压缩模型 → 测试连接 → 测速」实测一次更准。")
    if r["turn_chunks"] == 1 and r["delta"] <= r["chunk_tokens"]:
        lines.append(
            f"      本轮新增 {r['delta']:,} ≤ 分段 {r['chunk_tokens']:,}，"
            f"稳态只有 1 段，{r['concurrency']} 路用不上；"
            "并发合计只影响首次整段和大块压缩。")
    lines.append("本地引擎若保留跨请求 KV 缓存，省下的时间会明显缩水。")
    lines.append("压缩成本按缓存未命中价算；压缩请求能命中缓存时更便宜。")
    return "\n".join(lines)
