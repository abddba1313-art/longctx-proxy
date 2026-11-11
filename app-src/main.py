#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""longctx-proxy 图形启动器。

两种用法：
  * 单模型：压缩模型（本地或云端）→ 本地 OpenAI 兼容引擎 127.0.0.1:8080 完成任务。
  * 双模型：便宜模型负责压缩 → 云端「完成任务」模型负责答题（省长上下文的钱）。

界面里每条配置含两组字段：压缩模型 + 目标任务模型；另有「成本建议」页，
按分时段价目表算出该不该压、从多长开始压、压多狠。

配置存在 exe 同目录的 profiles.json / pricing.json（源码运行时在脚本同目录）。
本文件不含任何 API Key：Key 只存在于外部 profiles.json 里。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import (Tk, Canvas, StringVar, BooleanVar, IntVar, DoubleVar,
                     TclError, Menu, Toplevel, messagebox, simpledialog)
from tkinter import ttk, scrolledtext
from tkinter import font as tkfont

import price_source
import pricing as price_mod
import i18n

_TK_MESSAGEBOX = messagebox


class _TranslatingBox:
    """弹窗标题/正文按当前语言翻译；动态拼接的串不在表里，原样透传。"""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raw = getattr(_TK_MESSAGEBOX, name)

        def call(*args, **kw):
            return raw(*(i18n.t(a) if isinstance(a, str) else a for a in args), **kw)
        return call


messagebox = _TranslatingBox()

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8082
DEFAULT_LISTEN = f"{LISTEN_HOST}:{LISTEN_PORT}"
APP_VERSION = "5.1.0"
DEFAULT_THRESHOLD = 14000
DEFAULT_RATIO = 0.50  # v2.0 实测最佳中心值；自适应区间为 ±0.10
# 格子限幅 0.50~0.75：超过 0.75 时 proxy.py 的 DENSITY_MIN 会大于 DENSITY_MAX，
# 触发防反置塌缩成固定比例——动态自适应失效且压缩几乎不压缩，故界面上禁掉。
RATIO_MIN, RATIO_MAX = 0.50, 0.75


def ratio_band(ra: float) -> tuple:
    """与 proxy.py DENSITY_MIN/MAX 同公式；返回 lo>=hi 表示区间塌缩为固定值。"""
    return max(0.35, round(ra - 0.10, 2)), min(0.65, round(ra + 0.10, 2))
# 建议上下文区间：模型记忆最稳的窗口。原生 1M 不代表 1M 都好用；
# 社区实测多数模型在 200K 以内退化不明显，超过后开始“忘事”。
WORK_WINDOW_DEFAULT = 200000
WORK_WINDOW_MIN = 8000
WORK_WINDOW_MAX = 2_000_000

# 任务模式：值 -> (下拉框显示的文字, 适合什么任务)
TASK_MODES = {
    "auto":  ("自动", "日常对话 / 编码 / 混合内容：按结构占比判断，代码块与表格占比高才整块保留"),
    "table": ("表格", "财报、对账、报表、数据分析：连续 3 行以上的表格整块原样保留"),
    "legal": ("法律或工程规范",
              "合同、条款、合规、制度文件，以及 GB/API/ASME 等工程规范："
              "4 条以上编号条款整块原样保留，不改措辞"),
    "off":   ("关闭", "短对话 / 纯聊天：不做结构直通，全部按普通文本压缩"),
    "monitor": ("路由监控", "只想观测不想动内容：请求逐字节原样转发，"
                "不压缩、不截断、不注入；实况与超阈计数照常"),
    "record": ("上下文调制监测", "调制等于生成式改写，本档不改写：原文逐字直通不压缩；"
                "台账/探针/提醒/回声率全速运行——想对照「压缩在不在场」的差别用这档"),
}
TASK_MODE_LABELS = [v[0] for v in TASK_MODES.values()]
TASK_MODE_BY_LABEL = {v[0]: k for k, v in TASK_MODES.items()}

# 常驻上下文重压开关。触发点按引擎窗口的比例算，所以换引擎不用改数字。
RECOMPACT_DEFAULT_TRIGGER = 0.60
RECOMPACT_HINT_ON = (
    "勾上：更早的聊天记录涨到总长度的 {pct}% 时，把它们再压一遍，压到 {tgt}% 左右。\n"
    "压完会停顿几秒，之后又能接着聊很久。"
)
RECOMPACT_HINT_OFF = (
    "不勾：聊天记录只增不减。超过模型能装下的长度后，请求会直接失败。\n"
    "除非你有别的办法控制长度，否则别关。"
)

# 懒压缩：平时纯路由，涨到窗口比例才压一次。默认关，保持旧行为。
LAZY_DEFAULT_TRIGGER = 0.80
LAZY_HINT_ON = (
    "勾上：平时一个字都不改，聊天内容原样发给模型。\n"
    "等聊天记录涨到总长度的 {pct}% 时，才整体压一次，压到 {tgt}% 左右，然后继续原样发。\n"
    "好处是前面的细节不会被提前总结掉；代价是压缩那一下会停顿几秒。\n"
    "注意：上面那个开关也要勾上。只勾这个的话，到了 {pct}% 之后每轮都会重复压，钱花了体积也不降。"
)
LAZY_HINT_OFF = (
    "不勾（默认）：每轮都把老对话压到 1.4 万 token 左右。\n"
    "上下文小、出字快；但老内容很快被总结掉，细节会丢。"
)

# 百分比换算出的 token 数，必须带上基数和来源，否则看不出 64,000 是从哪算的。
CTX_TOKEN_TARGET = "≈ {n:,} token（任务模型窗口 {ctx:,}）"
CTX_TOKEN_MANUAL = "≈ {n:,} token（窗口 {ctx:,}）"
CTX_TOKEN_ENGINE = "≈ {n:,} token（本地引擎窗口 {ctx:,}）"
CTX_TOKEN_EST = "≈ {n:,} token（按窗口 {ctx:,} 估）"

# 本地模型窗口紧、预填充慢，压得太晚遇到一次大文件输入就可能直接失败。
LOCAL_ADVICE = (
    "本地模型建议：上面填 50、下面填 70。\n"
    "本地窗口留给大文件的余量不多，压得太晚，一次大文件输入就可能直接失败。"
)


def table_font(root: Tk) -> tuple[str, int]:
    """选一个「中文正好占 2 个 ASCII 宽」的等宽字体，否则表格补空格仍会错位。"""
    for name, size in (("新宋体", 10), ("NSimSun", 10),
                       ("宋体", 9), ("SimSun", 9)):
        f = tkfont.Font(root=root, family=name, size=size)
        if f.measure("中") == f.measure("00"):
            return name, size
    return "Consolas", 9


def new_target() -> dict:
    return {"enabled": False, "name": "", "base_url": "", "api_key": "",
            "model": "", "extra_json": "", "context": 0,
            "work_window": WORK_WINDOW_DEFAULT, "max_think": 0}


def derive_params(native_ctx: int, work_window: int) -> dict:
    """从「最大上下文 + 建议上下文」算出一整套后续参数。

    有效窗口 W = min(原生, 建议)。所有比例都相对 W：
      触发 85% / 压到 45% / 复位 90% / 正文目标 55%
      reserve 8% / 热层 8% / 检查点 1.2% / 锚点 2%
    这样换模型只改两个输入，后面的阈值、热层、回灌预算不用手调。
    """
    try:
        ww = int(work_window or WORK_WINDOW_DEFAULT)
    except (TypeError, ValueError):
        ww = WORK_WINDOW_DEFAULT
    ww = max(WORK_WINDOW_MIN, min(WORK_WINDOW_MAX, ww))
    try:
        native = int(native_ctx or 0)
    except (TypeError, ValueError):
        native = 0
    if native <= 0:
        native = 98304           # 与 proxy 的 CONTEXT 兜底保持一致
    w = max(WORK_WINDOW_MIN, min(native, ww))
    return {
        "window": w,
        "gate": int(w * 0.85),
        "compress_to": int(w * 0.45),
        "reset_at": int(w * 0.90),
        "target_msg": int(w * 0.55),
        "reserve": max(4096, min(24000, int(w * 0.08))),
        "hot_tokens": max(1024, min(48000, int(w * 0.08))),
        "checkpoint_budget": max(800, min(4000, int(w * 0.012))),
        "pin_max": max(600, min(8000, int(w * 0.02))),
    }


def normalize_profile(p: dict) -> dict:
    """老配置没有 target/threshold 字段，补齐后旧行为不变。"""
    t = p.get("target")
    if not isinstance(t, dict):
        t = new_target()
    for k, v in new_target().items():
        t.setdefault(k, v)
    p["target"] = t
    p.setdefault("threshold", DEFAULT_THRESHOLD)
    p.setdefault("target_msg", DEFAULT_THRESHOLD)
    # v2.3 起默认走「自动 200K 窗口」：不再要求用户手填目标正文。
    # 本地配置不自动放大（prefill 慢），仍按配置里的 target_msg。
    p["auto_window"] = True   # v2.4：不再保留手动压缩策略
    p.setdefault("recall_dump", False)
    p.setdefault("ratio", DEFAULT_RATIO)
    if p.get("task_mode") not in TASK_MODES:
        p["task_mode"] = "auto"
    if not isinstance(p.get("recompact"), bool):
        p["recompact"] = True
    try:
        p["recompact_trigger"] = min(
            0.95, max(0.30, float(p.get("recompact_trigger", RECOMPACT_DEFAULT_TRIGGER))))
    except (TypeError, ValueError):
        p["recompact_trigger"] = RECOMPACT_DEFAULT_TRIGGER
    if not isinstance(p.get("lazy_compress"), bool):
        p["lazy_compress"] = False
    try:
        p["lazy_trigger"] = min(
            0.95, max(0.50, float(p.get("lazy_trigger", LAZY_DEFAULT_TRIGGER))))
    except (TypeError, ValueError):
        p["lazy_trigger"] = LAZY_DEFAULT_TRIGGER
    try:
        # 0 = 自动（本地引擎探测；云端读不到按 128k 算）
        p["model_context"] = max(0, min(2_000_000, int(p.get("model_context") or 0)))
    except (TypeError, ValueError):
        p["model_context"] = 0
    try:
        # 建议上下文区间：决定“用多少窗口最稳”，不是模型原生上限。
        p["work_window"] = max(WORK_WINDOW_MIN, min(
            WORK_WINDOW_MAX, int(p.get("work_window") or WORK_WINDOW_DEFAULT)))
    except (TypeError, ValueError):
        p["work_window"] = WORK_WINDOW_DEFAULT
    try:
        t["work_window"] = max(WORK_WINDOW_MIN, min(
            WORK_WINDOW_MAX, int(t.get("work_window") or WORK_WINDOW_DEFAULT)))
    except (TypeError, ValueError):
        t["work_window"] = WORK_WINDOW_DEFAULT
    return p

DEFAULT_PROFILES = [
    {
        "name": "本地 swift-1.5",
        "base_url": "",
        "api_key": "",
        "model": "swift-1.5-iq3_xxs",
        "concurrency": 1,
        "extra_json": "",
        "local": True,
    },
    {
        "name": "百炼 token-plan",
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "model": "deepseek-v4.1-flash-1017",
        "concurrency": 2,
        "extra_json": '{"enable_thinking": false}',
        "local": False,
    },
    {
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-flash",
        "concurrency": 16,
        "extra_json": '{"reasoning_effort": "none"}',
        "local": False,
    },
    {
        "name": "百炼 按量付费",
        "base_url": "https://your-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "model": "deepseek-v4.1-flash-1017",
        "concurrency": 16,
        "extra_json": '{"enable_thinking": false}',
        "local": False,
    },
]


# ---------------------------------------------------------------- 路径
def app_dir() -> Path:
    """配置与日志所在目录。打包后必须用 exe 所在目录，不能用 __file__
    （onefile 模式下 __file__ 指向临时解包目录，配置会写进会被删掉的临时目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def profiles_path() -> Path:
    env = os.environ.get("LONGCTX_GUI_PROFILES")
    return Path(env) if env else app_dir() / "profiles.json"


def default_proxy_dir() -> Path:
    """代理本体所在的默认目录，按顺序找：
       1. exe 同级的 proxy/   （便携包：exe 和 proxy/ 并排）
       2. exe 上一级的 proxy/ （开发/安装布局：app/ 与 proxy/ 并排）
       3. 再上一级
    都没有时返回 1，启动时报「找不到 proxy.py」并提示设
    LONGCTX_PROXY_DIR / LONGCTX_PROXY_PATH。"""
    bases = [app_dir()]
    if getattr(sys, "frozen", False):
        bases += [app_dir().parent, app_dir().parent.parent]
    else:
        root = Path(__file__).resolve().parent.parent
        bases += [root, root.parent]
    for base in bases:
        for name in ("proxy", "longctx-proxy"):
            cand = base / name
            if (cand / "proxy.py").exists():
                return cand
    return bases[0] / "proxy"


def proxy_dir() -> Path:
    env = os.environ.get("LONGCTX_PROXY_DIR")
    return Path(env) if env else default_proxy_dir()


def proxy_path() -> Path:
    env = os.environ.get("LONGCTX_PROXY_PATH")
    return Path(env) if env else proxy_dir() / "proxy.py"


def python_exe() -> Path:
    """跑 proxy.py 用的解释器。打包后的 exe 自身不是 Python，必须另找。

    顺序：exe 同目录 python_path.txt → LONGCTX_PY 环境变量
          → 当前解释器（源码运行时）→ PATH 里的 python/python3。
    python_path.txt 让双击 exe 也能找到带 tiktoken/httpx 的解释器。
    """
    cands = []
    cfg = app_dir() / "python_path.txt"
    if cfg.exists():
        try:
            cands.append(cfg.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    cands.append(os.environ.get("LONGCTX_PY"))
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    cands.extend((shutil.which("python"), shutil.which("python3")))
    for cand in cands:
        if cand and Path(cand).exists():
            return Path(cand)
    return Path("python")


# ---------------------------------------------------------------- 配置存取
def pricing_path() -> Path:
    env = os.environ.get("LONGCTX_GUI_PRICING")
    return Path(env) if env else app_dir() / "pricing.json"


def load_pricing() -> dict:
    path = pricing_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("target") and data.get("compressor"):
                data.setdefault("advisor", {})
                return data
        except Exception:
            pass
    data = price_mod.default_pricing()
    save_pricing(data)
    return data


def _atomic_write(path: Path, text: str) -> None:
    """3.8 H：原子写。半写崩溃会让 load 解析失败、落入迁移分支整份重写
    （2026-11-06 覆盖事故的同一条代码路径）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_pricing(data: dict) -> None:
    _atomic_write(pricing_path(),
                  json.dumps(data, ensure_ascii=False, indent=2))


def rb_norm_time(s: str) -> str:
    """3.36：回滚时刻可手填——HH:MM 或 HH:MM:SS 规整成 HH:MM:SS，非法返回空串。"""
    parts = str(s or "").strip().split(":")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3:
        return ""
    try:
        h, m, sec = (int(x) for x in parts)
    except ValueError:
        return ""
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59):
        return ""
    return f"{h:02d}:{m:02d}:{sec:02d}"


def proxy_stats() -> dict:
    """读代理落盘的每轮增量统计；代理没跑过或没数据时返回空 dict。"""
    path = Path(os.environ.get("LONGCTX_STATS_PATH", str(proxy_dir() / "proxy-stats.json")))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fmt_tok(n) -> str:
    """token 数千分位；读不到就原样返回，绝不让实况显示抛异常拖垮界面。"""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def ctx_live_text(stats: dict) -> str:
    """状态条文案：最近一轮实际转发给任务模型的历史大小（压缩后口径）。

    有引擎实测（usage.prompt_tokens）时并排显示，给「估计 vs 真实」对账锚点。
    """
    act = stats.get("actual") if isinstance(stats, dict) else None
    if not isinstance(act, dict) or not act.get("total"):
        if stats.get("task_mode") == "monitor":
            return i18n.t("路由监控直通：等待请求（内容逐字节原样转发）")
        if stats.get("task_mode") == "record":
            return i18n.t("上下文调制监测：等待请求（原文直通，台账全速）")
        return i18n.t("尚未观测到请求")
    text = i18n.tf(
        "任务模型实况 {total} tok · 正文 {body} + 工具 {tools} · 压缩前 {raw}",
        total=_fmt_tok(act["total"]), body=_fmt_tok(act["body"]),
        tools=_fmt_tok(act["tools"]), raw=_fmt_tok(act["raw_total"]))
    usage = act.get("usage_prompt")
    if usage:
        text += i18n.tf(" · 引擎实测 {usage}", usage=_fmt_tok(usage))
    # 3.34：缓存命中（本地=conversation cache 的 reused，云端=隐式/更式命中）。
    # 0 要显式画出来——0 就是"前缀断了"或"云端不支持隐式缓存"，是数据不是缺测；
    # 只有上游没报（None）才不画。
    hit = act.get("cache_hit")
    rate = act.get("cache_rate")
    if hit is not None and isinstance(rate, (int, float)):
        text += i18n.tf(" · 缓存命中 {hit}（{rate}）",
                        hit=_fmt_tok(hit), rate=f"{rate:.1%}")
    anch = act.get("anchors")
    if isinstance(anch, dict) and anch.get("total"):
        text += i18n.tf(" · 锚点回声 {echoed}/{total}",
                        echoed=anch["echoed"], total=anch["total"])
        if anch.get("missed"):
            text += i18n.tf("（待提醒 {missed}）", missed=anch["missed"])
    # 3.18：路由监控档在实况条挂直通标记与超阈计数（只提示，不拦截）。
    if stats.get("task_mode") == "monitor":
        mon = stats.get("monitor") or {}
        text += f" · 路由监控直通 {int(mon.get('reqs') or 0)} 次"
        if int(mon.get("over") or 0):
            text += f"（超阈 {int(mon['over'])} 次，未拦截）"
    elif stats.get("task_mode") == "record":
        text += " · 调制监测：原文直通，台账全速"
    return text


def ctx_rows(stats: dict) -> list[tuple]:
    """会话实况表行：最近 8 个会话，新的在前（proxy.py 已按时间倒序）。"""
    convs = stats.get("conversations") if isinstance(stats, dict) else None
    out = []
    for c in (convs or [])[:8]:
        if not isinstance(c, dict) or not c.get("total"):
            continue
        usage = c.get("usage_prompt")
        # 3.34：命中 tok / 命中率两列。0 照实画 0（那是"前缀断了"或"云端不支持
        # 隐式缓存"的证据），上游没报才画 —，两者在表里必须看得出差别。
        hit = c.get("cache_hit")
        rate = c.get("cache_rate")
        hcell = "—" if hit is None else _fmt_tok(hit)
        rcell = "—" if not isinstance(rate, (int, float)) else f"{rate:.1%}"
        anch = c.get("anchors")
        if isinstance(anch, dict) and anch.get("total"):
            cell = f"{anch['echoed']}/{anch['total']}"
            if anch.get("followed"):
                cell += f" 默{anch['followed']}"
        else:
            cell = "—"
        out.append((str(c.get("key", ""))[:8], _fmt_tok(c.get("body")),
                    _fmt_tok(c.get("tools")), _fmt_tok(c.get("total")),
                    _fmt_tok(c.get("raw_total")),
                    _fmt_tok(usage) if usage else "—", hcell, rcell, cell,
                    str(c.get("ts", ""))))
    return out


def _sess_dir() -> Path:
    """会话台账目录，与 proxy.py 的 SESS_DIR 同规矩（PROXY_SESS_DIR 可覆盖）。"""
    return Path(os.environ.get("PROXY_SESS_DIR", str(proxy_dir() / "sessions")))


def load_session_ckpt(conv: str):
    """读 sessions/<conv>/checkpoint.json；缺失/损坏返回 None，绝不抛。"""
    if not conv:
        return None
    try:
        d = json.loads((_sess_dir() / str(conv) / "checkpoint.json")
                       .read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _md_items(items, fmt=None) -> list:
    """台账列表 → markdown 列表行；dict 条目取 t 字段（兜底 k），空条目跳过。"""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            t = str(it.get("t") or it.get("k") or "").strip()
        else:
            t = str(it or "").strip()
        if t:
            out.append(fmt(it, t) if fmt else f"- {t}")
    return out


def session_md(conv: str, live, ckpt, now: str = "") -> str:
    """3.17.5：一条会话的检查点台账 + 实况行 → 完整 .md 文本。

    不受注入预算约束——台账里有什么就导什么，全量导出；纯函数，
    live/ckpt 任一为 None 也要出可读文本，绝不抛（弹窗按钮经不起崩）。
    """
    live = live if isinstance(live, dict) else {}
    ck = ckpt if isinstance(ckpt, dict) else {}
    conv = str(conv or live.get("key") or "")
    lines = [f"# 会话 {conv[:8] or '?'} · 历史台账导出", ""]
    head = []
    if now:
        head.append(f"- 导出时间：{now}")
    if conv:
        head.append(f"- 会话 ID：{conv}")
    if ck.get("created"):
        head.append(f"- 台账建立：{ck['created']}")
    if ck.get("updated"):
        head.append(f"- 台账更新：{ck['updated']}（游标 {ck.get('cursor', 0)} 轮）")
    if head:
        lines += head + [""]

    def block(title, rows):
        if rows:
            lines.extend([f"## {title}", ""] + rows + [""])

    goal = str(ck.get("goal") or "").strip()
    block("目标", [goal] if goal else [])
    block("硬性规则", _md_items(ck.get("rules")))

    def _todo_fmt(it, t):
        return f"- [{'x' if it.get('done') else ' '}] {t}"
    block("待办", _md_items(ck.get("todos"), _todo_fmt))

    block("进展", _md_items(ck.get("progress")))

    def _dec_fmt(it, t):
        why = str(it.get("why") or "").strip()
        return f"- {t}（理由：{why}）" if why and why not in t else f"- {t}"
    block("决定", _md_items(ck.get("decisions"), _dec_fmt))

    block("事实", _md_items(ck.get("facts")))

    files = []
    if ck.get("files_mod"):
        files.append("- 改：" + "；".join(str(p) for p in ck["files_mod"]))
    if ck.get("files_read"):
        files.append("- 读：" + "；".join(str(p) for p in ck["files_read"]))
    block("文件", files)

    block("错误/教训", _md_items(ck.get("errors")))

    srcs = []
    for s in ck.get("sources") or []:
        if not isinstance(s, dict):
            t = str(s or "").strip()
            if t:
                srcs.append(f"- 《{t}》")
            continue
        line = f"- 《{str(s.get('t') or '未命名来源')}》"
        if s.get("base"):
            line += f"（库 {s['base']}，{int(s.get('chunks') or 0)} 段）"
        srcs.append(line)
        srcs += [f"    {x}" for x in (s.get("lines") or []) if str(x).strip()]
    block("来源（知识库检索）", srcs)

    ledger_empty = not any((goal, ck.get("rules"), ck.get("todos"),
                           ck.get("progress"), ck.get("decisions"),
                           ck.get("facts"), files, ck.get("errors"), srcs))

    live_rows = []
    for label, key in (("正文", "body"), ("工具", "tools"),
                       ("压缩后合计", "total"), ("压缩前原始", "raw_total")):
        if live.get(key):
            live_rows.append(f"- {label}：{_fmt_tok(live[key])} tok")
    if live.get("usage_prompt"):
        live_rows.append(f"- 引擎实测：{_fmt_tok(live['usage_prompt'])} tok")
    anch = live.get("anchors")
    if isinstance(anch, dict) and anch.get("total"):
        cell = f"- 锚点回声 {anch.get('echoed', 0)}/{anch['total']}"
        if anch.get("followed"):
            cell += f"（默读 {anch['followed']}）"
        live_rows.append(cell)
    if live.get("ts"):
        live_rows.append(f"- 最近一轮：{live['ts']}")
    if ledger_empty:
        live_rows.append("- 台账未生成或读取失败——本文件仅含代理实况统计；"
                         "对话跑过几轮、检查点建立后再导出即可。")
    block("实况（代理转发统计）", live_rows)

    return "\n".join(lines).rstrip() + "\n"


def default_profiles() -> list[dict]:
    return [normalize_profile({**p, "target": new_target()}) for p in DEFAULT_PROFILES]


def load_profiles() -> list[dict]:
    path = profiles_path()
    if not path.exists():
        profiles = default_profiles()
        # 首次运行：把现有 env 文件里的 key 带进来，省得手抄
        env_map = {
            "百炼 token-plan": proxy_dir() / "longctx-cloud.env",
            "DeepSeek 官方": proxy_dir() / "longctx-deepseek.env",
            "百炼 按量付费": proxy_dir() / "longctx-bailian-payg.env",
        }
        for p in profiles:
            f = env_map.get(p["name"])
            if f and f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.startswith("LONGCTX_API_KEY="):
                        p["api_key"] = line.split("=", 1)[1].strip()
                    elif line.startswith("LONGCTX_MODEL="):
                        p["model"] = line.split("=", 1)[1].strip()
        save_profiles(profiles)
        return profiles
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [normalize_profile(p) for p in data if isinstance(p, dict)]
        return default_profiles()
    except Exception:
        return default_profiles()


def save_profiles(profiles: list[dict]) -> None:
    _atomic_write(profiles_path(),
                  json.dumps([normalize_profile(p) for p in profiles],
                             ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 任务模型
def targets_path() -> Path:
    env = os.environ.get("LONGCTX_GUI_TARGETS")
    return Path(env) if env else app_dir() / "targets.json"


_LOCK_FH = None


def acquire_single_instance() -> bool:
    """3.5：单实例锁。

    实测事故（2026-11-06 15:15）：用户切到 kimi 成功跑了两轮，旧 GUI 实例
    （内存里还是 qwen 启用）的自动回写路径（价目迁移/ctx 探测/保存价目表）
    把 targets.json 整体盖回，切换被无声回滚。多实例就是数据竞争，锁死一个。"""
    global _LOCK_FH
    try:
        import msvcrt
        # 3.8 F：锁放用户级目录。锁在 app_dir 时，dist/ 与 app/ 两份 exe
        # 各拿一把锁，双开覆盖漏洞依旧（用户实测有两份 exe）。
        lock_dir = Path(os.environ.get("LOCALAPPDATA", str(app_dir()))) / "longctx"
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = open(lock_dir / "gui.lock", "a+")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_FH = fh                      # 句柄保活到进程结束，锁自动释放
        return True
    except Exception:
        return False


def normalize_target(t: dict) -> dict:
    out = new_target()
    for k in out:
        if k in t:
            out[k] = t[k]
    out["enabled"] = bool(out.get("enabled"))
    for k in ("name", "base_url", "api_key", "model", "extra_json"):
        out[k] = str(out.get(k) or "")
    # 思考上限（token，0=不改客户端发的 max_tokens）。存成整数，别混进字符串。
    try:
        out["max_think"] = max(0, int(out.get("max_think") or 0))
    except (TypeError, ValueError):
        out["max_think"] = 0
    # 每条云端模型各自带一份价目；不参与身份去重，但要原样保存。
    if isinstance(t.get("pricing"), dict):
        out["pricing"] = t["pricing"]
    return out


def save_targets(targets: list[dict]) -> None:
    _atomic_write(targets_path(),
                  json.dumps([normalize_target(t) for t in targets],
                             ensure_ascii=False, indent=2))


def thinking_path() -> Path:
    """5.0.5 思考级别统一闸的档位文件。代理经 PROXY_THINKING_FILE 读同一份。"""
    env = os.environ.get("LONGCTX_GUI_THINKING")
    return Path(env) if env else app_dir() / "thinking.json"


def load_thinking() -> str | None:
    """None = 跟随客户端（不改写）。坏文件/坏值也按 None，别让 GUI 起不来。"""
    try:
        d = json.loads(thinking_path().read_text(encoding="utf-8"))
        v = d.get("level") if isinstance(d, dict) else None
        return v if v in ("none", "low", "medium", "high") else None
    except (OSError, ValueError):
        return None


def save_thinking(level: str | None) -> None:
    _atomic_write(thinking_path(),
                  json.dumps({"level": level}, ensure_ascii=False, indent=1))


def routing_path() -> Path:
    """3.33 路由接管配置（8080 面板搬到 8082）。代理经 PROXY_ROUTING_FILE
    读同一份，逐请求 mtime 重读——改参数不重启 8080/8082。"""
    env = os.environ.get("LONGCTX_GUI_ROUTING")
    return Path(env) if env else app_dir() / "routing.json"


def load_routing() -> dict:
    """坏文件/缺文件 = 全关（透传，旧行为）。"""
    try:
        d = json.loads(routing_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_routing(conf: dict) -> None:
    _atomic_write(routing_path(),
                  json.dumps(conf, ensure_ascii=False, indent=1))


def _target_identity(t: dict) -> tuple:
    return (t.get("name") or "", t.get("base_url") or "",
            t.get("api_key") or "", t.get("model") or "",
            t.get("extra_json") or "")


def load_targets(profiles: list[dict]) -> list[dict]:
    """读任务模型列表。

    老版本的 target 嵌在每条压缩配置里；首次升级时把它们去重后搬到
    targets.json，之后两边的列表互不影响。target 留空的不搬。
    """
    path = targets_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                items = [normalize_target(t) for t in data if isinstance(t, dict)]
                # 默认任务模型固定在第一行：老数据里 enabled 不在首位的，读入时提上来。
                idx = next((i for i, t in enumerate(items) if t.get("enabled")), None)
                if idx:
                    items.insert(0, items.pop(idx))
                return items
        except Exception:
            pass

    targets: list[dict] = []
    seen: dict[tuple, dict] = {}
    for p in profiles:
        t = p.get("target")
        if not isinstance(t, dict):
            continue
        if not any((t.get("name"), t.get("base_url"), t.get("model"),
                    t.get("api_key"))):
            continue
        key = _target_identity(t)
        if key in seen:
            seen[key]["enabled"] = bool(seen[key].get("enabled")
                                        or t.get("enabled"))
            continue
        entry = normalize_target(t)
        seen[key] = entry
        targets.append(entry)

    if not targets:
        targets = [dict(new_target(), name="任务模型")]
    save_targets(targets)
    return targets


# ---------------------------------------------------------------- 工具
def parse_listen(addr: str) -> tuple[str, int]:
    """把界面上的「服务 IP:端口」拆成 host / port。"""
    text = (addr or "").strip()
    if text.startswith("http://"):
        text = text[len("http://"):]
    elif text.startswith("https://"):
        text = text[len("https://"):]
    text = text.rstrip("/")
    if text.count(":") != 1:
        raise ValueError("格式应为 IP:端口，例如 127.0.0.1:8082")
    host, port_text = text.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise ValueError("服务 IP 不能为空")
    try:
        port = int(port_text.strip())
    except ValueError:
        raise ValueError("服务端口必须是数字") from None
    if not 1 <= port <= 65535:
        raise ValueError("服务端口需在 1-65535 之间")
    return host, port


def ascii_key(key: str) -> str:
    """Key 必须是 ASCII。

    从聊天窗口或网页复制时经常带进 •、全角空格、中文引号；
    http.client 用 latin-1 编码请求头，碰到非 ASCII 会直接抛编码异常，
    报错信息里根本看不出是 Key 的问题。这里统一过滤掉。"""
    k = (key or "").strip()
    return k if k.isascii() else ""


def _reply_text(data: dict) -> str:
    """从 chat/completions 响应里取一段可读文本。

    推理型模型在 max_tokens 很小时 content 会是 null，内容全在
    reasoning_content 里；直接对 None 切片会抛 'NoneType' object is not
    subscriptable，看起来像连接失败。"""
    try:
        first = (data.get("choices") or [{}])[0] or {}
        msg = first.get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return ""
    return msg.get("content") or msg.get("reasoning_content") or ""


def chat_urls(base_url: str) -> list[str]:
    """把「API 链接」补成真正能请求的 chat/completions 地址（可能不止一个）。

    用户填的形态有三种：
      http://127.0.0.1:8080           本地引擎，接口只在 /v1 下面
      https://api.deepseek.com        DeepSeek/OpenAI，/v1 和裸路径都认
      .../compatible-mode/v1          百炼；OpenRouter 是 .../api/v1
    所以带版本段的直接拼 /chat/completions，不带版本的两个都试。"""
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return []
    if b.endswith("/chat/completions"):
        return [b]
    if b.endswith("/v1") or "/compatible-mode/" in b:
        return [b + "/chat/completions"]
    return [b + "/v1/chat/completions", b + "/chat/completions"]


def detect_context(base_url: str, api_key: str = "", model: str = "") -> int:
    """尽力读一次模型的上下文窗口。读不到返回 0。

    本地引擎：GET /health 拿 max_context（最可靠）。
    云端：不少服务商的 /v1/models 会带 context_length / max_model_len；
           没有就只能手填。"""
    u = urlparse((base_url or "").strip())
    if not u.hostname:
        return 0
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        try:
            with urllib.request.urlopen(
                    f"http://{u.netloc}/health", timeout=3) as r:
                d = json.loads(r.read().decode("utf-8")) or {}
            n = int(d.get("max_context") or 0)
            if n > 0:
                return n
        except Exception:
            pass
    root = f"{u.scheme}://{u.netloc}"
    for url in (root + "/v1/models", root + "/models"):
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {ascii_key(api_key) or 'none'}"})
            with urllib.request.urlopen(req, timeout=6) as r:
                d = json.loads(r.read().decode("utf-8")) or {}
        except Exception:
            continue
        for m in (d.get("data") or []):
            mid = str(m.get("id") or "") if isinstance(m, dict) else ""
            if model and mid and mid != model:
                continue
            for k in ("context_length", "max_model_len", "context_window",
                      "max_context_length", "max_input_tokens"):
                try:
                    n = int(m.get(k) or 0) if isinstance(m, dict) else 0
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    return n
    return 0


def port_busy(port: int = LISTEN_PORT, host: str = LISTEN_HOST) -> bool:
    probe = (host or LISTEN_HOST).strip()
    if probe in ("0.0.0.0", "*"):
        probe = LISTEN_HOST
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((probe, port)) == 0


def port_pid(port: int = LISTEN_PORT) -> str:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line.upper():
                return line.split()[-1]
    except Exception:
        pass
    return ""


def mask(key: str) -> str:
    if not key:
        return "(空)"
    return key[:8] + "…" + key[-4:] if len(key) > 16 else "…"


TARGET_ENV_KEYS = ("PROXY_UPSTREAM", "PROXY_UPSTREAM_API_KEY",
                   "PROXY_UPSTREAM_MODEL", "PROXY_UPSTREAM_EXTRA_JSON",
                   "PROXY_MAX_THINK")


def build_env(p: dict, target: dict | None = None,
              listen_host: str = LISTEN_HOST,
              listen_port: int = LISTEN_PORT) -> dict:
    """把一条配置翻译成 proxy.py 需要的环境变量。

    target 为 None 时沿用配置里嵌入的 target（老配置/测试）；
    界面启动时传入「任务模型列表」当前选中的那条。
    listen_host / listen_port 是代理自己的监听地址，默认 127.0.0.1:8082。
    """
    env = os.environ.copy()
    try:
        threshold = max(1000, int(p.get("threshold") or DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    try:
        target_msg = max(0, int(p.get("target_msg") or DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        target_msg = DEFAULT_THRESHOLD
    try:
        ratio = max(RATIO_MIN, min(RATIO_MAX, float(p.get("ratio") or DEFAULT_RATIO)))
    except (TypeError, ValueError):
        ratio = DEFAULT_RATIO
    task_mode = p.get("task_mode") if p.get("task_mode") in TASK_MODES else "auto"
    try:
        rc_trig = min(0.95, max(0.30, float(
            p.get("recompact_trigger", RECOMPACT_DEFAULT_TRIGGER))))
    except (TypeError, ValueError):
        rc_trig = RECOMPACT_DEFAULT_TRIGGER
    recompact = bool(p.get("recompact", True))
    try:
        lz_trig = min(0.95, max(0.50, float(
            p.get("lazy_trigger", LAZY_DEFAULT_TRIGGER))))
    except (TypeError, ValueError):
        lz_trig = LAZY_DEFAULT_TRIGGER
    lazy = bool(p.get("lazy_compress", False))
    try:
        mctx = max(0, min(2_000_000, int(p.get("model_context") or 0)))
    except (TypeError, ValueError):
        mctx = 0
    # 自动窗口：云端默认开（把 1M 模型按 200K 记忆窗口用），本地默认沿用配置。
    # 用户点过「按本地速度算」或「成本建议」后，auto_window 会被置 False。
    auto_window = True   # v2.4：只保留自动策略
    # 建议上下文区间：完成任务模型有自己的就优先用它的，否则用主配置的。
    _tgt = target if target is not None else (p.get("target") or {})
    task_work = 0
    if isinstance(_tgt, dict) and _tgt.get("enabled"):
        try:
            task_work = int(_tgt.get("work_window") or 0)
        except (TypeError, ValueError):
            task_work = 0
    try:
        work_window = task_work or int(p.get("work_window") or WORK_WINDOW_DEFAULT)
    except (TypeError, ValueError):
        work_window = WORK_WINDOW_DEFAULT
    work_window = max(WORK_WINDOW_MIN, min(WORK_WINDOW_MAX, work_window))
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PROXY_HOST": listen_host,
        "PROXY_PORT": str(listen_port),
        "PROXY_THRESHOLD": str(threshold),
        # 0 = 自动：proxy 按 min(模型原生窗口, 200K) 的 55% 计算正文目标。
        "PROXY_TARGET_MSG": "0" if auto_window else str(target_msg),
        "PROXY_WORK_WINDOW": os.environ.get("LONGCTX_WORK_WINDOW", str(work_window)),
        "PROXY_DIGEST_RATIO": f"{ratio:g}",
        "PROXY_TASK_MODE": task_mode,
        # 5.0.5 思考级别统一闸：代理逐请求读这份档位文件（mtime 缓存），
        # GUI 改档只写文件，不重启代理。
        "PROXY_THINKING_FILE": str(thinking_path()),
        # 3.33 路由接管：采样参数/发送策略/隐藏思考/对话存档，同一份 routing.json，
        # 代理逐请求读 mtime，改参数不重启。
        "PROXY_ROUTING_FILE": str(routing_path()),
        # 常驻上下文重压：按引擎窗口比例触发，关掉就彻底不动冻结前缀。
        # 自动窗口 + 检查点下默认关：重压是“越压越差”的主要来源。
        "PROXY_RECOMPACT_CAP": "0",
        "PROXY_RECOMPACT_TRIGGER": ("0" if auto_window
                                    else (f"{rc_trig:g}" if recompact else "0")),
        # 懒压缩：平时纯路由，涨到窗口比例才压一次。
        # 自动窗口下固定 85% 触发、45% 目标，用满 200K 甜点区再一次性回落。
        "PROXY_LAZY_COMPRESS": ("auto" if auto_window
                                else ("1" if lazy else "0")),
        "PROXY_LAZY_TRIGGER": ("0.85" if auto_window else f"{lz_trig:g}"),
        "PROXY_LAZY_TARGET": "0.45" if auto_window else "0",
        "PROXY_CHECKPOINT_RESET": os.environ.get(
            "LONGCTX_CHECKPOINT_RESET", "0.90" if auto_window else "0.75"),
        # 默认开自适应密度：界面里的“压缩比”变成区间中心（±0.10），
        # 事实密集的段落自动多留、模板话多的段落自动少留。
        # 实测比固定比例更好（数字 97.9% vs 96.9%，路径 94.1% vs 88.2%）。
        # 想要老行为（严格按界面数字固定比例），启动前设 LONGCTX_DENSITY_CAP=0。
        "PROXY_DENSITY_CAP": os.environ.get("LONGCTX_DENSITY_CAP", "1"),
        # 召回审计：勾选后把原始/转发请求成对落盘；只在本机，默认关。
        "PROXY_DUMP": "1" if (p.get("recall_dump")
                              or os.environ.get("PROXY_DUMP") == "1") else "0",
        "PROXY_DUMP_DIR": str(proxy_dir() / "dumps"),
    })
    env.pop("PROXY_DOC_MODE", None)   # 旧变量名，避免和新任务模式打架

    # 双模型：压完转给云端「完成任务」模型
    t = _tgt
    if t.get("enabled"):
        env["PROXY_UPSTREAM"] = (t.get("base_url") or "").strip()
        task_key = ascii_key(t.get("api_key") or "")
        if task_key:
            env["PROXY_UPSTREAM_API_KEY"] = task_key
        else:
            env.pop("PROXY_UPSTREAM_API_KEY", None)   # 留空 = 透传 Cherry 的 Key
        if (t.get("model") or "").strip():
            env["PROXY_UPSTREAM_MODEL"] = t["model"].strip()
        else:
            env.pop("PROXY_UPSTREAM_MODEL", None)
        if (t.get("extra_json") or "").strip():
            env["PROXY_UPSTREAM_EXTRA_JSON"] = t["extra_json"].strip()
        else:
            env.pop("PROXY_UPSTREAM_EXTRA_JSON", None)
        # 思考上限（token）。只有本地引擎认 reasoning_budget_tokens，代理里
        # 还有一道 _local_upstream 判定，云端行填了也不会被改写。
        try:
            mt = int(t.get("max_think") or 0)
        except (TypeError, ValueError):
            mt = 0
        if mt > 0:
            env["PROXY_MAX_THINK"] = str(mt)
        else:
            env.pop("PROXY_MAX_THINK", None)
    else:
        for k in TARGET_ENV_KEYS:
            env.pop(k, None)

    # 窗口大小：任务模型（启用时）> 主配置手填 > 让代理自己问本地引擎。
    # 先 pop 掉，避免继承上一次会话的残留值。
    task_ctx = 0
    if t.get("enabled"):
        try:
            task_ctx = max(0, min(2_000_000, int(t.get("context") or 0)))
        except (TypeError, ValueError):
            task_ctx = 0
    final_ctx = task_ctx or mctx
    if final_ctx > 0:
        env["PROXY_CONTEXT"] = str(final_ctx)
    else:
        env.pop("PROXY_CONTEXT", None)

    # 后续参数不再手填：从「最大上下文 + 建议上下文」统一派生。
    # 显式设过的环境变量优先（给排障/复现留口子）。
    dp = derive_params(final_ctx, work_window)
    env["PROXY_RESERVE"] = os.environ.get("PROXY_RESERVE", str(dp["reserve"]))
    env["PROXY_HOT_TOKENS"] = os.environ.get("PROXY_HOT_TOKENS", str(dp["hot_tokens"]))
    env["PROXY_CHECKPOINT_BUDGET"] = os.environ.get(
        "PROXY_CHECKPOINT_BUDGET", str(dp["checkpoint_budget"]))
    env["PROXY_CHECKPOINT_PIN_MAX"] = os.environ.get(
        "PROXY_CHECKPOINT_PIN_MAX", str(dp["pin_max"]))
    if auto_window:
        env.update({
            "PROXY_THRESHOLD": str(dp["gate"]),   # 懒压缩关掉时的兜底阈值
            "PROXY_LAZY_TRIGGER": "0.85",
            "PROXY_LAZY_TARGET": "0.45",
            # 5.0.4：尊重 LONGCTX_CHECKPOINT_RESET 口子（=0 彻底关复位，只回灌）。
            # 此前这里硬编码 0.90，把上面 927 行留的环境变量旁路又覆盖掉了。
            "PROXY_CHECKPOINT_RESET": os.environ.get(
                "LONGCTX_CHECKPOINT_RESET", "0.90"),
            "PROXY_CHECKPOINT_RESET_TARGET": "0.45",
        })

    if p.get("local"):
        env.update({
            "PROXY_LOCAL_LLM": "1",
            "PROXY_CONCURRENCY": "1",
            "PROXY_CONC_CAP": "1",
            "LONGCTX_MODEL": p.get("model") or "swift-1.5-iq3_xxs",
        })
        for k in ("LONGCTX_BASE_URL", "LONGCTX_API_KEY", "PROXY_CLOUD_EXTRA_JSON"):
            env.pop(k, None)
        return env
    conc = max(1, min(64, int(p.get("concurrency") or 1)))
    env.update({
        "PROXY_LOCAL_LLM": "0",
        "LONGCTX_BASE_URL": p.get("base_url", ""),
        "LONGCTX_API_KEY": ascii_key(p.get("api_key", "")),
        "LONGCTX_MODEL": p.get("model", ""),
        "PROXY_CONCURRENCY": str(conc),
        "PROXY_CONC_CAP": str(conc),
    })
    if p.get("extra_json"):
        env["PROXY_CLOUD_EXTRA_JSON"] = p["extra_json"]
    else:
        env.pop("PROXY_CLOUD_EXTRA_JSON", None)
    return env


# ---------------------------------------------------------------- 价目表控件
class PriceTable(ttk.LabelFrame):
    """一个模型的价目表：时段 / 输入 / 输出 / 缓存命中 / 缓存写入（每百万 token）。

    完成模型的价目额外带一个缓存模式选择：账单上的「输入（缓存命中）」
    对应 cache_read，「显式缓存创建」对应 cache_write。
    """

    def __init__(self, master, title: str, model: dict,
                 show_cache_mode: bool = False, height: int = 4):
        super().__init__(master, text=title, padding=8)
        self.base_title = title
        self.model = model
        self.tree = ttk.Treeview(
            self, columns=("period", "input", "output", "cache", "write", "explicit"),
            show="headings", height=height)
        for col, text, width in (("period", "时段", 92), ("input", "输入", 48),
                                 ("output", "输出", 48), ("cache", "缓存命中", 60),
                                 ("write", "缓存写入", 60), ("explicit", "显式命中", 60)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(fill="x")

        self.v_start = StringVar(value="00:00")
        self.v_end = StringVar(value="23:59")
        self.v_in = StringVar()
        self.v_out = StringVar()
        self.v_cache = StringVar(value="0")
        self.v_write = StringVar(value="0")
        self.v_explicit = StringVar()

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(6, 0))
        for var, width, label in ((self.v_start, 6, "起"), (self.v_end, 6, "止")):
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=var, width=width).pack(side="left", padx=(2, 8))

        prow = ttk.Frame(self)
        prow.pack(fill="x", pady=(4, 0))
        for var, label in ((self.v_in, "输入"), (self.v_out, "输出")):
            ttk.Label(prow, text=label).pack(side="left")
            ttk.Entry(prow, textvariable=var, width=6).pack(side="left", padx=(2, 4))

        prow2 = ttk.Frame(self)
        prow2.pack(fill="x", pady=(4, 0))
        for var, label in ((self.v_cache, "命中"), (self.v_write, "写入"),
                           (self.v_explicit, "显式")):
            ttk.Label(prow2, text=label).pack(side="left")
            ttk.Entry(prow2, textvariable=var, width=6).pack(side="left", padx=(2, 4))
        ttk.Button(prow2, text="添加", width=5, command=self.add_window).pack(side="left")
        ttk.Button(prow2, text="删除", width=5,
                   command=self.delete_selected).pack(side="left", padx=4)

        self.show_cache_mode = show_cache_mode
        self.v_mode = None
        self._model_label = ""      # 标题里后缀的模型名，切换语言时要重画
        if show_cache_mode:
            mrow = ttk.Frame(self)
            mrow.pack(fill="x", pady=(4, 0))
            ttk.Label(mrow, text="缓存模式").pack(side="left")
            cur = price_mod.cache_mode(model, True)
            self.v_mode = StringVar(value=i18n.t(price_mod.CACHE_LABELS[cur]))
            cb = ttk.Combobox(mrow, textvariable=self.v_mode, state="readonly",
                              width=12,
                              values=tuple(i18n.t(v) for v in price_mod.CACHE_LABELS.values()))
            self.mode_combo = cb
            cb.pack(side="left", padx=(4, 6))
            cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_mode())
            ttk.Label(mrow, text="自动缓存不计写入费；显式缓存另算一次创建费",
                      foreground="#666").pack(side="left")
        self._i18n_skip = True      # 标题是动态的，不进登记表
        self.refresh()

    def apply_lang(self) -> None:
        """语言切换后重画动态部分：标题（含模型名）与缓存模式下拉。"""
        self.show_model(self.model, self._model_label)
        if self.v_mode is not None:
            self.mode_combo.configure(
                values=tuple(i18n.t(v) for v in price_mod.CACHE_LABELS.values()))
            mode = price_mod.cache_mode(self.model, True)
            self.v_mode.set(i18n.t(price_mod.CACHE_LABELS[mode]))

    def show_model(self, model: dict, label: str = "") -> None:
        """把价目表切到当前列表选中的那条模型，标题里带模型名。"""
        self.model = model
        self._model_label = (label or "").strip()
        text = i18n.t(self.base_title)
        if self._model_label:
            text = f"{text} · {self._model_label[:18]}"
        self.configure(text=text)
        if self.v_mode is not None:
            mode = price_mod.cache_mode(model, True)
            self.v_mode.set(i18n.t(price_mod.CACHE_LABELS[mode]))
        self.refresh()

    def _apply_mode(self) -> None:
        label = self.v_mode.get() if self.v_mode else ""
        # 下拉里显示的可能是英文，先换回中文 key 再比对
        label = i18n.other(label) or label
        for mode, text in price_mod.CACHE_LABELS.items():
            if text == label:
                self.model["cache"] = mode
                return

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for w in self.model.get("windows", []):
            explicit = w.get("cache_read_explicit")
            self.tree.insert("", "end", values=(
                f"{w['start']}-{w['end']}", f"{w['input']:g}",
                f"{w['output']:g}", f"{w.get('cache_read', 0):g}",
                f"{w.get('cache_write', 0):g}",
                "-" if explicit is None else f"{explicit:g}"))

    def read_form(self) -> dict | None:
        try:
            start = self.v_start.get().strip()
            end = self.v_end.get().strip()
            price_mod.hhmm_to_min(start)
            price_mod.hhmm_to_min(end)
            win = {"start": start, "end": end,
                   "input": float(self.v_in.get()),
                   "output": float(self.v_out.get()),
                   "cache_read": float(self.v_cache.get() or 0),
                   "cache_write": float(self.v_write.get() or 0)}
            explicit = self.v_explicit.get().strip()
            if explicit:
                win["cache_read_explicit"] = float(explicit)
            return win
        except ValueError as exc:
            messagebox.showwarning("价目表", f"填写有误：{exc}")
            return None

    def add_window(self) -> bool:
        win = self.read_form()
        if win is None:
            return False
        self.model.setdefault("windows", []).append(win)
        self.refresh()
        return True

    def delete_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        windows = self.model.get("windows", [])
        if 0 <= idx < len(windows):
            del windows[idx]
            self.refresh()


class ScrollColumn(ttk.Frame):
    """列内双向滚动：内容比窗口高/宽时，滚轮上下、Shift+滚轮左右，不挤压其他列。

    3.24：补横向滚动条（用户定案：四个部分每页上下、左右都要能滚）——横向条
    常显，内容比画布窄时不滚但可见，位置和纵向条对称。"""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = Canvas(self, highlightthickness=0, borderwidth=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal",
                                 command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vsb.set,
                              xscrollcommand=self.hsb.set)
        self.vsb.pack(side="right", fill="y")
        self.hsb.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.win = self.canvas.create_window((0, 0), window=self.inner,
                                             anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner(self, _evt=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, evt) -> None:
        # 3.24：内容仍按视口宽排（动态折行标签依赖这一点；用 reqwidth 会形成
        # "标称宽→折行宽→reqwidth"反馈环，wraplength 一路涨到 3569）。
        # 横向条保留：内容将来真实超宽时 scrollregion 自然生效。
        self.canvas.itemconfigure(self.win, width=evt.width)

    def _bind_wheel(self, _evt=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _wheel(self, evt) -> None:
        if evt.state & 0x0001:          # Shift+滚轮 = 横向
            self.canvas.xview_scroll(int(-evt.delta / 120), "units")
        else:
            self.canvas.yview_scroll(int(-evt.delta / 120), "units")


# ---------------------------------------------------------------- 主窗口
class App:
    def __init__(self, root: Tk):
        self.root = root
        self.pricing_data = load_pricing()
        self.profiles = load_profiles()
        self.targets = load_targets(self.profiles)
        self._ensure_model_pricing()
        self.proc: subprocess.Popen | None = None
        self.log_q: queue.Queue[tuple[str, str]] = queue.Queue()
        self.bench_q: queue.Queue[dict] = queue.Queue()
        self.price_q: queue.Queue[dict] = queue.Queue()
        self.last_bench: dict | None = None
        self._bench_busy = False
        self.current_index: int | None = None
        self.current_target_index: int | None = None

        i18n.set_lang(str(self.pricing_data.get("lang") or i18n.ZH))
        self._apply_title()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1900, sw - 60)}x{min(950, sh - 80)}")
        root.minsize(1100, 700)

        self._build_ui()
        # 静态文本登记完，再按当前语言套一遍（记住的上次是英文时生效）
        self._i18n_registry = i18n.collect(self.root)
        if i18n.get_lang() == i18n.EN:
            self._apply_lang()
        self._refresh_list(select=0)
        self._refresh_target_list(select=0)
        self.root.after(120, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_status()

    # ---- 界面语言
    def _apply_title(self) -> None:
        self.root.title(f"{i18n.t('longctx-proxy 启动器')} · GUI {APP_VERSION}")

    def _lang_btn_text(self) -> str:
        # 按钮写"要切去的那种语言"，两种语言下都一眼看懂
        return "English" if i18n.get_lang() == i18n.ZH else "中文"

    def _apply_lang(self) -> None:
        i18n.apply_lang(self.root, self._i18n_registry)
        self._update_ctx_live()          # 状态条实况是跳过登记的动态文本，切语言要重画
        self._apply_title()
        self.btn_lang.configure(text=self._lang_btn_text())
        self._refresh_thinking_hint()
        for table in (getattr(self, "price_comp", None),
                      getattr(self, "price_target", None)):
            if table is not None:
                table.apply_lang()
        self._refresh_task_mode_lang()
        self._update_recompact_hint()
        self._update_lazy_hint()
        # 结果框还停在占位提示时才重画；一旦算过就是中文正文，不动（见 R29 范围）
        for box_attr, ph_attr, flag_attr in (
                ("advice_text", "_advice_placeholder", "_advice_is_placeholder"),
                ("local_text", "_local_placeholder", "_local_is_placeholder")):
            box = getattr(self, box_attr, None)
            if box is not None and getattr(self, flag_attr, False):
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.insert("end", i18n.t(getattr(self, ph_attr)))
                box.configure(state="disabled")
        if getattr(self, "_status_args", None):      # 重画当前状态
            template, kw = self._status_args
            self._set_status(template, **kw)
        self._tick_status()

    def _toggle_lang(self) -> None:
        i18n.set_lang(i18n.EN if i18n.get_lang() == i18n.ZH else i18n.ZH)
        self.pricing_data["lang"] = i18n.get_lang()
        save_pricing(self.pricing_data)
        self._apply_lang()

    def _thinking_changed(self) -> None:
        """选档即写文件；代理下一个请求就读到，不重启代理。"""
        key = self.v_thinking.get()
        level = None if key == "pass" else key
        try:
            save_thinking(level)
        except OSError as exc:
            self._say(f"[思考] 档位写盘失败：{exc}\n", "err")
            return
        self._refresh_thinking_hint()
        self._say(f"[思考] 统一档位 -> {key}（代理逐请求读取，无需重启）\n", "ok")

    def _refresh_thinking_hint(self) -> None:
        key = self.v_thinking.get()
        if key == "pass":
            text = i18n.t("当前：跟随客户端（代理不改写思考档位）")
        else:
            shown = "xhigh" if key == "high" else key
            text = i18n.tf("当前：{level}（代理删客户端档位、统一换成它；即时生效）",
                           level=shown)
        self.lbl_thinking.configure(text=text)

    def _routing_save(self) -> None:
        """3.33 路由接管：点保存写 routing.json，代理逐请求读 mtime——
        采样参数/发送策略即时生效，8080 与 8082 都不用重启。"""
        conf = {"takeover": bool(self.v_rt_take.get()),
                "send_policy": {"pass": "passthrough", "wait": "wait",
                                "stop": "interrupt"}[self.v_rt_policy.get()],
                "hide_thinking": bool(self.v_rt_hide.get()),
                "chat_archive": bool(self.v_rt_arch.get())}
        for key in ("temperature", "top_p", "top_k", "seed", "max_tokens"):
            s = self.rt_entries[key].get().strip()
            v = None
            if s:
                try:
                    f = float(s)
                    v = int(f) if (key in ("top_k", "seed", "max_tokens")
                                   and f == int(f)) else f
                except ValueError:
                    v = None
            conf[key] = v
        try:
            save_routing(conf)
        except OSError as exc:
            self._say(f"[路由接管] 写盘失败：{exc}\n", "err")
            return
        self._refresh_routing_hint()
        self._say("[路由接管] 已保存，代理下一个请求即生效（无需重启 8080/8082）\n",
                  "ok")

    def _refresh_routing_hint(self) -> None:
        rc = load_routing()
        if not rc:
            self.lbl_rt.configure(text="未保存过：全部透传（旧行为）")
            return
        p = {"passthrough": "跟随", "wait": "等待",
             "interrupt": "打断"}.get(str(rc.get("send_policy")), "跟随")
        self.lbl_rt.configure(
            text="接管" + ("开" if rc.get("takeover") else "关")
                 + f" · 策略{p}"
                 + (" · 思考隐藏" if rc.get("hide_thinking") else "")
                 + (" · 存档中" if rc.get("chat_archive") else "")
                 + " · 即时生效")

    def _ensure_model_pricing(self) -> None:
        """每条压缩/云端模型各带一份价目。

        老版本只有 pricing.json 里的全局 target/compressor；升级后首次启动，
        把那份价目复制给每条模型（此后各改各的），缺少价目的新条目用模板补。
        """
        defaults = price_mod.default_pricing()
        comp_src = self.pricing_data.get("compressor")
        target_src = self.pricing_data.get("target")
        if not isinstance(comp_src, dict):
            comp_src = defaults["compressor"]
        if not isinstance(target_src, dict):
            target_src = defaults["target"]

        def clone(obj: dict) -> dict:
            return json.loads(json.dumps(obj, ensure_ascii=False))

        changed = False
        for p in self.profiles:
            if not isinstance(p.get("pricing"), dict):
                p["pricing"] = clone(comp_src)
                changed = True
        for t in self.targets:
            if not isinstance(t.get("pricing"), dict):
                t["pricing"] = clone(target_src)
                changed = True
        if changed:
            save_profiles(self.profiles)
            save_targets(self.targets)

    @staticmethod
    def _clone(obj: dict) -> dict:
        return json.loads(json.dumps(obj, ensure_ascii=False))

    def _new_comp_pricing(self) -> dict:
        """新建压缩模型时复制当前选中项的价目，之后各改各的。"""
        if (self.current_index is not None
                and 0 <= self.current_index < len(self.profiles)
                and isinstance(self.profiles[self.current_index].get("pricing"), dict)):
            return self._clone(self.profiles[self.current_index]["pricing"])
        return self._clone(price_mod.default_pricing()["compressor"])

    def _new_target_pricing(self) -> dict:
        """新建任务模型时复制当前选中项的价目，之后各改各的。"""
        if (self.current_target_index is not None
                and 0 <= self.current_target_index < len(self.targets)
                and isinstance(self.targets[self.current_target_index].get("pricing"), dict)):
            return self._clone(self.targets[self.current_target_index]["pricing"])
        return self._clone(price_mod.default_pricing()["target"])

    # ---- 布局
    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except TclError:
            pass

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="both", expand=True)

        # 四个区域从左到右同时显示；窗口较窄时用底部横向滚动条。
        canvas = Canvas(top, highlightthickness=0, borderwidth=0,
                        background="#f0f0f0")
        hsb = ttk.Scrollbar(top, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        canvas.pack(side="top", fill="both", expand=True)
        self.cols_canvas = canvas
        cols_frame = ttk.Frame(canvas)
        self.cols_frame = cols_frame
        cols_id = canvas.create_window((0, 0), window=cols_frame, anchor="nw")
        cols_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        def _fit_canvas(evt):
            # 四页等宽：窗口够宽时由四列平分；不够宽时保持最小宽度，底部横向滚动。
            canvas.itemconfigure(cols_id, height=evt.height,
                                 width=max(evt.width, cols_frame.winfo_reqwidth()))

        canvas.bind("<Configure>", _fit_canvas)
        # 四页等宽：uniform 让四个内容列取同一宽度（由最宽的一页决定），
        # 分隔条固定 8px。不再给某一页单独加宽——英文界面下会挤扁最后一页。
        for i in range(7):
            if i % 2:
                cols_frame.columnconfigure(i, weight=0, minsize=8)
            else:
                cols_frame.columnconfigure(i, weight=1, minsize=360,
                                           uniform="page")
        cols_frame.rowconfigure(0, weight=1)

        self.separators = []

        def add_separator(col: int) -> None:
            sep = Canvas(cols_frame, width=8, highlightthickness=0,
                         background="#f0f0f0")

            def draw(_evt=None, c=sep):
                c.delete("all")
                w, h = c.winfo_width(), c.winfo_height()
                c.create_line(w // 2, 0, w // 2, h, dash=(4, 4),
                              fill="#999999")

            sep.bind("<Configure>", draw)
            sep.grid(row=0, column=col, sticky="ns")
            self.separators.append(sep)

        add_separator(1)
        add_separator(3)
        add_separator(5)

        # ---- 第一列：压缩模型（列表 + 表单）
        comp_col = ttk.LabelFrame(cols_frame, text="压缩模型", padding=6)
        comp_col.grid(row=0, column=0, sticky="nsew")
        # 3.8 流程1：压缩列也包 ScrollColumn——布局冒烟要求所有按钮在滚动容器内，
        # 顺带根治这一列将来加行后的底部裁切（今天已报过 4 次同类）。
        comp_scroll = ScrollColumn(comp_col)
        comp_scroll.pack(fill="both", expand=True)
        comp_inner = comp_scroll.inner
        comp_inner.columnconfigure(0, weight=1)
        self.tab_comp = comp_col

        self.list_comp = ttk.LabelFrame(comp_inner, text="压缩模型列表", padding=6)
        self.list_comp.grid(row=0, column=0, sticky="ew")
        left = self.list_comp
        comp_row = ttk.Frame(left)
        comp_row.pack(fill="x")
        ccols = ("name", "conc", "threshold", "url")
        self.tree = ttk.Treeview(comp_row, columns=ccols, show="headings", height=5,
                                 selectmode="browse")
        self.tree.heading("name", text="名称")
        self.tree.heading("conc", text="并发")
        self.tree.heading("threshold", text="阈值")
        self.tree.heading("url", text="API 链接")
        self.tree.column("name", width=110, anchor="w")
        self.tree.column("conc", width=40, anchor="center")
        self.tree.column("threshold", width=65, anchor="e")
        self.tree.column("url", width=120, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(comp_row, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        left_btns = ttk.Frame(left)
        left_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(left_btns, text="新增", width=6,
                   command=self._add).pack(side="left")
        ttk.Button(left_btns, text="复制", width=6,
                   command=self._duplicate).pack(side="left", padx=4)
        ttk.Button(left_btns, text="删除", width=6,
                   command=self._delete).pack(side="left")

        form = ttk.Frame(comp_inner, padding=(0, 6, 0, 0))
        form.grid(row=1, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)

        self.v_name = StringVar()
        self.v_url = StringVar()
        self.v_key = StringVar()
        self.v_model = StringVar()
        self.v_price_url = StringVar()
        self.v_conc = IntVar(value=4)
        self.v_extra = StringVar()
        self.v_local = BooleanVar(value=False)
        self.v_show_key = BooleanVar(value=False)
        self.v_task_mode = StringVar(value=i18n.t(TASK_MODES["auto"][0]))
        self._task_mode_key = "auto"

        r = 0
        ttk.Label(form, text="列表名称").grid(row=r, column=0, sticky="w", pady=3)
        self.e_name = ttk.Entry(form, textvariable=self.v_name)
        self.e_name.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(form, text="API 链接").grid(row=r, column=0, sticky="w", pady=3)
        self.e_url = ttk.Entry(form, textvariable=self.v_url)
        self.e_url.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(form, text="API Key").grid(row=r, column=0, sticky="w", pady=3)
        key_row = ttk.Frame(form)
        key_row.grid(row=r, column=1, sticky="ew", pady=3)
        key_row.columnconfigure(0, weight=1)
        self.key_entry = ttk.Entry(key_row, textvariable=self.v_key, show="•")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(key_row, text="显示", variable=self.v_show_key,
                        command=self._toggle_key).grid(row=0, column=1, padx=(6, 0))
        r += 1
        ttk.Label(form, text="模型官方名称").grid(row=r, column=0, sticky="w", pady=3)
        self.e_model = ttk.Entry(form, textvariable=self.v_model)
        self.e_model.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(form, text="价目链接").grid(row=r, column=0, sticky="w", pady=3)
        self.e_price_url = ttk.Entry(form, textvariable=self.v_price_url)
        self.e_price_url.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(form, text="留空 = 按 API 链接自动识别（DeepSeek 官方 / 百炼）",
                  foreground="#666").grid(row=r, column=1, sticky="w", pady=(0, 3))
        r += 1
        ttk.Label(form, text="并发路数").grid(row=r, column=0, sticky="w", pady=3)
        conc_row = ttk.Frame(form)
        conc_row.grid(row=r, column=1, sticky="ew", pady=3)
        self.spin_conc = ttk.Spinbox(conc_row, from_=1, to=64, width=6,
                                     textvariable=self.v_conc)
        self.spin_conc.pack(side="left")
        ttk.Label(conc_row, text="  1-64；百炼按量付费与 DeepSeek 实测 16 路为拐点",
                  foreground="#666").pack(side="left")
        r += 1
        ttk.Label(form, text="任务模式").grid(row=r, column=0, sticky="w", pady=3)
        tm_row = ttk.Frame(form)
        tm_row.grid(row=r, column=1, sticky="ew", pady=3)
        # 3.20：原来 width=6，7 个汉字的「上下文调制监测」要 84px 而控件只有 65px，
        # 被裁成「上下文记」。18 格同时容得下英文档名（Legal / standards）。
        self.cb_task_mode = ttk.Combobox(tm_row, textvariable=self.v_task_mode,
                                         values=[i18n.t(x) for x in TASK_MODE_LABELS],
                                         state="readonly",
                                         width=18)
        self.cb_task_mode.pack(side="left")
        # 3.20：说明文字不再挤在下拉框右边拿 420px 假宽（窄列里两头裁字，
        # 用户报「文字被遮挡」）——改成独占一行，折行宽度跟着控件真实宽度走。
        # 注意：必须先 r += 1 换到下一行。3.20 首版漏了这一步，说明标签 grid 在
        # 同一行的 column 0 且 columnspan=2，把「任务模式」标签和下拉框整个盖住
        # （用户截图：下拉框"没了"）。
        r += 1
        self.lbl_task_hint = ttk.Label(form, text="", foreground="#666",
                                       justify="left", anchor="w", wraplength=240)
        self.lbl_task_hint.grid(row=r, column=0, columnspan=2, sticky="ew")
        self.lbl_task_hint.bind(
            "<Configure>",
            lambda e: self.lbl_task_hint.configure(
                wraplength=max(160, e.width - 4)))
        self.cb_task_mode.bind("<<ComboboxSelected>>", self._on_task_mode_change)
        self._on_task_mode_change()
        r += 1
        ttk.Label(form, text="额外参数").grid(row=r, column=0, sticky="w", pady=3)
        self.e_extra = ttk.Entry(form, textvariable=self.v_extra)
        self.e_extra.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Checkbutton(form, text="本地模型（127.0.0.1:8080，并发固定 1 路）",
                        variable=self.v_local, command=self._on_local_toggle
                        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 2))
        r += 1

        actions = ttk.Frame(form)
        actions.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="保存配置", command=self._save).pack(side="left")
        self.btn_test = ttk.Menubutton(actions, text="测试连接 ▾")
        test_menu = Menu(self.btn_test, tearoff=False)
        test_menu.add_command(label="Ping（1 token，几乎免费）",
                              command=self._test_conn)
        test_menu.add_command(label="测预填充 / 生成速度（消耗额度）",
                              command=self._bench_conn)
        self.test_menu = test_menu
        self.btn_test["menu"] = test_menu
        self.btn_test.pack(side="left", padx=6)
        ttk.Button(actions, text="获取价目",
                   command=self._fetch_comp_pricing).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="设为默认", command=self._set_default).pack(side="left")

        # ---- 第二列：任务模型（列表 + 表单）
        target_col = ttk.LabelFrame(cols_frame, text="任务模型", padding=6)
        target_col.grid(row=0, column=2, sticky="nsew")
        # 3.4.1：整页包 ScrollColumn（纵向滚动条）——窗口再矮也能滚到任何一行，
        # 根治"底部行被裁"这一类问题（此前 2.9.8/3.3.1 都是挪行位躲，不是治）。
        tgt_scroll = ScrollColumn(target_col)
        tgt_scroll.pack(fill="both", expand=True)
        tgt_inner = tgt_scroll.inner
        tgt_inner.columnconfigure(0, weight=1)
        self.tab_target = target_col

        self.list_tgt = ttk.LabelFrame(tgt_inner, text="任务模型列表", padding=6)
        self.list_tgt.grid(row=0, column=0, sticky="ew")
        tleft = self.list_tgt
        target_row = ttk.Frame(tleft)
        target_row.pack(fill="x")
        tcols = ("name", "enabled", "model", "url")
        self.tree_target = ttk.Treeview(
            target_row, columns=tcols, show="headings", height=4,
            selectmode="browse")
        self.tree_target.heading("name", text="名称")
        self.tree_target.heading("enabled", text="启用")
        self.tree_target.heading("model", text="模型")
        self.tree_target.heading("url", text="API 链接")
        self.tree_target.column("name", width=95, anchor="w")
        self.tree_target.column("enabled", width=35, anchor="center")
        self.tree_target.column("model", width=85, anchor="w")
        self.tree_target.column("url", width=110, anchor="w")
        self.tree_target.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(target_row, orient="vertical",
                            command=self.tree_target.yview)
        tsb.pack(side="left", fill="y")
        self.tree_target.configure(yscrollcommand=tsb.set)
        self.tree_target.bind("<<TreeviewSelect>>", self._on_target_select)

        target_btns = ttk.Frame(tleft)
        target_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(target_btns, text="新增", width=6,
                   command=self._target_add).pack(side="left")
        ttk.Button(target_btns, text="复制", width=6,
                   command=self._target_duplicate).pack(side="left", padx=4)
        ttk.Button(target_btns, text="删除", width=6,
                   command=self._target_delete).pack(side="left")
        ttk.Button(target_btns, text="设为默认", width=8,
                   command=self._target_set_default).pack(side="left", padx=(4, 0))

        # 3.5：一眼看清代理实际在用的任务模型（这次切换"失败"就是因为没处显示）
        self.lbl_active = ttk.Label(tleft, text="", foreground="#1a7f37")
        self.lbl_active.pack(anchor="w", pady=(4, 0))

        # 3.4.1：KPI 曲线区移到本页最底部（表单之后）；整页已有滚动条，不怕裁。
        # 实际构建在 tform 尾部（见下方 kpi 段）。

        tform = ttk.Frame(tgt_inner, padding=(0, 6, 0, 0))
        tform.grid(row=1, column=0, sticky="nsew")
        tform.columnconfigure(1, weight=1)

        self.v_t_enabled = BooleanVar(value=False)
        self.v_t_name = StringVar()
        self.v_t_url = StringVar()
        self.v_t_key = StringVar()
        self.v_t_model = StringVar()
        self.v_t_extra = StringVar()
        self.v_t_max_think = IntVar(value=0)
        self.v_t_ctx = IntVar(value=0)
        self.v_t_work = IntVar(value=WORK_WINDOW_DEFAULT)
        self.v_t_show = BooleanVar(value=False)

        ttk.Checkbutton(
            tform, text="启用云端完成任务模型（压缩完转给它答题）",
            variable=self.v_t_enabled).grid(row=0, column=0, columnspan=2,
                                            sticky="w", pady=(0, 2))
        ttk.Label(tform, foreground="#666",
                  text="左侧「任务模型列表」独立选择/保存；"
                       "启用后代理才把压缩结果转给它。"
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        r = 2
        ttk.Label(tform, text="列表名称").grid(row=r, column=0, sticky="w", pady=3)
        self.e_t_name = ttk.Entry(tform, textvariable=self.v_t_name)
        self.e_t_name.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(tform, text="API 链接").grid(row=r, column=0, sticky="w", pady=3)
        self.e_t_url = ttk.Entry(tform, textvariable=self.v_t_url)
        self.e_t_url.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(tform, text="API Key").grid(row=r, column=0, sticky="w", pady=3)
        tkey = ttk.Frame(tform)
        tkey.grid(row=r, column=1, sticky="ew", pady=3)
        tkey.columnconfigure(0, weight=1)
        self.t_key_entry = ttk.Entry(tkey, textvariable=self.v_t_key, show="•")
        self.t_key_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(tkey, text="显示", variable=self.v_t_show,
                        command=self._toggle_t_key).grid(row=0, column=1, padx=(6, 0))
        r += 1
        ttk.Label(tform, text="模型官方名称").grid(row=r, column=0, sticky="w", pady=3)
        self.e_t_model = ttk.Entry(tform, textvariable=self.v_t_model)
        self.e_t_model.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1
        ttk.Label(tform, text="上下文能力").grid(row=r, column=0, sticky="w", pady=3)
        tctxf = ttk.Frame(tform)
        tctxf.grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Spinbox(tctxf, from_=0, to=2000000, increment=8000, width=12,
                    textvariable=self.v_t_ctx).pack(side="left")
        ttk.Label(tctxf, text="token（按服务商填；填了才能换算压缩百分比）",
                  foreground="#666").pack(side="left", padx=(6, 0))
        r += 1
        ttk.Label(tform, text="建议上下文").grid(row=r, column=0, sticky="w", pady=3)
        tworkf = ttk.Frame(tform)
        tworkf.grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Spinbox(tworkf, from_=WORK_WINDOW_MIN, to=WORK_WINDOW_MAX,
                    increment=8000, width=12,
                    textvariable=self.v_t_work).pack(side="left")
        ttk.Label(tworkf, text="token（查社区：记得最稳的区间；原生 1M 也常填 200000）",
                  foreground="#005a9e").pack(side="left", padx=(6, 0))
        # 打字就刷新百分比后面的 token 数，不用等「保存列表」
        self.v_t_ctx.trace_add("write", self._on_t_ctx_change)
        self.v_t_work.trace_add("write", self._on_t_work_change)
        # 地址/模型/Key 一变，重新探一次上下文；不要把上一次模型的值当成本次的。
        for _v in (self.v_t_url, self.v_t_model, self.v_t_key):
            _v.trace_add("write", lambda *_: self._on_target_endpoint_change())
        r += 1
        ttk.Label(tform, text="额外参数").grid(row=r, column=0, sticky="w", pady=3)
        self.e_t_extra = ttk.Entry(tform, textvariable=self.v_t_extra)
        self.e_t_extra.grid(row=r, column=1, sticky="ew", pady=3)
        # 思考上限：代理会把 reasoning_budget_tokens 和 max_tokens 成对写进请求
        # （引擎把思考夹在 max_tokens 的 3/4 以内，只写一个数放大不了）。
        # 0 = 不动客户端发的 max_tokens。只有发给本地引擎的请求才会改写。
        r += 1
        ttk.Label(tform, text="思考上限 (0=不改)").grid(row=r, column=0, sticky="w", pady=3)
        self.e_t_max_think = ttk.Entry(tform, textvariable=self.v_t_max_think)
        self.e_t_max_think.grid(row=r, column=1, sticky="ew", pady=3)
        # 按钮排在帮助文字之前：列高不足时截掉的是参考文字，
        # 不能把「保存列表/测试目标连接」挤出窗口底缘（2.9.8 修）。
        r += 1
        tactions = ttk.Frame(tform)
        tactions.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(tactions, text="保存列表", command=self._save_targets).pack(side="left")
        ttk.Button(tactions, text="测试目标连接",
                   command=self._test_target).pack(side="left", padx=6)

        # 3.4.1：KPI 曲线（蓝=约束回声率，橙=k，灰虚线=近20轮均值基线）+ 文字行 +
        # 人话备注，移到本页最底部；整页有滚动条兜底，滚到即见。
        # 数据源是代理每轮落盘的 kpi.jsonl，2 秒随状态刷。
        r += 1
        # 3.4.8：图加高到 140px（40 太矮三条线贴零糊成一线；有滚动条不怕高）
        # 3.16：再加高到 190px（用户要求），底部 18px 留给时间刻度轴带。
        self.kpi_canvas = Canvas(tform, height=190, bg="#fafafa",
                                 highlightthickness=1, highlightbackground="#ddd")
        self.kpi_canvas.grid(row=r, column=0, columnspan=2, sticky="ew",
                             pady=(14, 0))
        r += 1
        # 3.17.2：文字行+按钮装进子框——3.17 把 lbl_kpi 直接 grid 在 column 0，
        # 它单行请求宽 ~700px 把 column 0 撑爆、输入框列（column 1）压成 1px
        # 全消失（用户报"格子都看不到了"）；子框里 pack 则不污染表单列宽。
        kpirow = ttk.Frame(tform)
        kpirow.grid(row=r, column=0, columnspan=2, sticky="ew")
        self.lbl_kpi = ttk.Label(kpirow, text="闭环 KPI：等代理出数据",
                                 foreground="#666", wraplength=360)
        self.lbl_kpi.pack(side="left", fill="x", expand=True)
        # 3.26：按钮从文字行拆成两栏——原来 9 个控件挤一行，窄列里后面的
        # 按钮被裁出可视区（用户报"界面上看不到新建线图"）。左对齐两行，
        # 每行 4 个控件，默认窗口宽度下全部可见。
        kpibar = ttk.Frame(tform)
        kpibar.grid(row=r + 1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.kpibar = kpibar
        # 4.1.0（用户定案）：绿线数据源与时间段各用一个按钮管；基线下拉只管灰线。
        # 「全部会话」→「线图时间范围」；「新建线图/保存线图」删除；
        # 「调取线图」改为选模型数据源。
        self.btn_kpi_win = ttk.Button(kpibar, text="线图时间范围",
                                      command=self._kpi_time_range)
        self.btn_kpi_win.pack(side="left")
        self.btn_kpi_model = ttk.Button(kpibar, text="调取线图",
                                        command=self._kpi_model_pick)
        self.btn_kpi_model.pack(side="left", padx=(6, 0))
        kpibar2 = ttk.Frame(tform)
        kpibar2.grid(row=r + 2, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.kpibar2 = kpibar2
        ttk.Label(kpibar2, text="基线:").pack(side="left")
        self.cb_kpi_base = ttk.Combobox(kpibar2, state="readonly", width=11)
        self.cb_kpi_base.pack(side="left", padx=(2, 6))
        self.cb_kpi_base.bind("<<ComboboxSelected>>", self._kpi_base_select)
        # 4.2.0（用户定案）：「采集基线」→「更新基线」（选模型→用其全部有效数据
        # 重算覆盖）；「清空基线」「保存基线」删除。
        self.btn_kpi_base = ttk.Button(kpibar2, text="更新基线",
                                       command=self._kpi_update_baseline)
        self.btn_kpi_base.pack(side="left")
        # 3.17.2：文字行也动态折行（旧版单行不换，窄列被裁——"这一行文字也遮挡"）。
        self.lbl_kpi.bind(
            "<Configure>",
            lambda e: self.lbl_kpi.configure(wraplength=max(120, e.width - 4)))
        r += 3
        # 3.17.1：wraplength 固定 480 在窄列会裁字（用户报"介绍文字被遮挡"）
        # ——绑 <Configure> 按标签实际宽度动态折行，多窄都跟得走。
        self.lbl_kpi_note = ttk.Label(tform, justify="left", foreground="#888",
                  text="""图怎么看（绿、紫、灰三条线共用一个百分比刻度）
· 绿线＝回声重心：旧内容里还有多少被模型照着复述出来，取最近 20 轮的平均。线越高，说明旧记忆越还在。
· 只丢不摘当轮（drp>0）绿线画断口、也不进基线：那轮旧内容被整条删除，测的是删除不是记忆。
· 紫线＝换说法命中重心：换个问法去问旧内容，答对比例的平均。它测"真记得"，不是"照抄"。
· 灰虚线＝基线（参考水位），右端标数值。攒够 20 轮合格数据才画；不够就空着，也不给降档建议。合格＝池子里≥5 条、上下文≤10 万 token、没在退避。
· 一段段小点＝数据还不够置信（绿线池子<5、紫线问得<3 次），只供看、不作判断依据。
· 没有基线时绿线照常画，自己就是参照；若基线是用"探针存活率"算的（回声数据不够时的退路），灰线只当参照，不跟绿线比、不给建议。

基线怎么来
· 「基线」下拉选"自成一组"＝这个模型自己攒自己的线，没攒够就不在图上显示基线；选某个组＝共用那组的基线。
· 本模型自己一条合格数据都没有时，共用线只作参照，不会拿别人的水位判你降档。
· 「更新基线」＝用某模型全部合格数据重算一条线（同样要够 20 轮）。

三个按钮各管一件事
· 「线图时间范围」＝绿线看哪段时间（近 30 分钟~3 天／近 60 轮／全部）。
· 「调取线图」＝绿线用哪款模型的历史数据。
· 「基线」下拉＝只决定灰线，跟前两个互不影响。

什么时候该调上下文
· 绿／紫线末端连续 3 轮以上压在灰线下面 → 文字行提示"建议降一档"。
· 线在灰线上面且探针全活 → 可以试升一档。
· 单轮忽高忽低不算数，只看连续趋势。

其它
· 点历史实况表某一行＝图只看那条会话；想取消，在「线图时间范围」弹窗里清掉过滤。
· 远期＝被丢掉的旧消息里的标识符，模型还能不能自发认出来；换说法＝代理换非字面问法搭车核实真召回。
· 历史实况表的「命中 tok／命中率」是引擎或云端自己报的缓存数（本地＝conversation cache 复用的 reused，云端＝隐式/显式命中），跟绿线紫线不是一回事：命中率掉 0 是前缀字节被改了或会话被踢出缓存（那一轮首包会明显变慢，工具定义变一个字节就能造成，日志里的 tools fp 变了会喊一声）；绿线掉是内容没被复述出来。命中 0 是数据不是缺测，— 才是上游没报。
· 探针只测"看没看见"，不测"懂没懂"。这条曲线只用来决定要不要降档，别拿它下"记忆力变好"的结论。"""
                  )
        self.lbl_kpi_note.grid(row=r, column=0, columnspan=2, sticky="ew",
                               pady=(2, 4))
        self.lbl_kpi_note.bind(
            "<Configure>",
            lambda e: self.lbl_kpi_note.configure(
                wraplength=max(200, e.width - 6)))

        # ---- 第三列：成本建议
        cost_col = ttk.LabelFrame(cols_frame, text="成本建议", padding=6)
        cost_col.grid(row=0, column=4, sticky="nsew")
        cost_scroll = ScrollColumn(cost_col)
        cost_scroll.pack(fill="both", expand=True)
        self.tab_cost = cost_col
        self._build_cost_tab(cost_scroll.inner)

        # ---- 第四列：本地模型时间账
        local_col = ttk.LabelFrame(cols_frame, text="综合参数计算", padding=6)
        local_col.grid(row=0, column=6, sticky="nsew")
        # 与「成本建议」列同款：包 ScrollColumn，窗口矮时按钮靠滚动够到，
        # 不再被裁在窗口底缘外（2.9.10 修）。
        local_scroll = ScrollColumn(local_col)
        local_scroll.pack(fill="both", expand=True)
        local = local_scroll.inner
        self.tab_local = local_col
        self._build_local_tab(local)

        run = ttk.Frame(outer)
        run.pack(fill="x", pady=(10, 6))
        self.btn_start = ttk.Button(run, text="启动代理", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(run, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Label(run, text="服务地址").pack(side="left", padx=(12, 2))
        saved_listen = self.pricing_data.get("service_addr")
        self.v_listen = StringVar(value=str(saved_listen or DEFAULT_LISTEN))
        self.e_listen = ttk.Entry(run, textvariable=self.v_listen, width=18)
        self.e_listen.pack(side="left")
        self.e_listen.bind("<FocusOut>", self._save_listen_addr)
        self.e_listen.bind("<Return>", self._save_listen_addr)
        self.status = ttk.Label(run, text=i18n.t("空闲"))
        self.status._i18n_skip = True      # 实时状态，由 _tick_status 维护
        self.status.pack(side="left", padx=12)
        self.ctx_live = ttk.Label(run, foreground="#0a7d3c",
                                  text=i18n.t("尚未观测到请求"))
        self.ctx_live._i18n_skip = True    # 实况文本每 2 秒由 _update_ctx_live 重画
        self.ctx_live.pack(side="left", padx=12)
        ttk.Label(run, text=f"GUI {APP_VERSION}", foreground="#666").pack(
            side="right", padx=8)
        self.btn_lang = ttk.Button(run, text=self._lang_btn_text(),
                                   command=self._toggle_lang)
        self.btn_lang._i18n_skip = True    # 文案随语言变，由 _apply_lang 维护
        self.btn_lang.pack(side="right")

        # 5.0.5 思考级别统一闸：客户端各发各的档位，换客户端思考强度就变。
        # 这里选一档，代理转发前删掉客户端自带拼法、换成统一档（只改发往本地
        # 引擎的任务请求）。只写 thinking.json，代理逐请求读 mtime——改档即时
        # 生效，不用重启代理，也不用再开浏览器去 8080 的网页里点。
        think = ttk.Frame(outer)
        # 挂最顶：pack 先给先 packs 的控件分配空间，窗口再矮也裁不到它。
        # 底部行被裁是本项目报过 4 次的老 bug，新控件不再往底部堆。
        think.pack(fill="x", pady=(0, 6), before=top)
        ttk.Label(think, text="思考级别").pack(side="left")
        self.v_thinking = StringVar(value=load_thinking() or "pass")
        for key, label in (("pass", "跟随客户端"), ("none", "Off"),
                           ("low", "Low"), ("medium", "Medium"),
                           ("high", "xhigh")):
            ttk.Radiobutton(think, text=label, variable=self.v_thinking,
                            value=key, command=self._thinking_changed).pack(
                                side="left", padx=4)
        self.lbl_thinking = ttk.Label(think, foreground="#666")
        self.lbl_thinking._i18n_skip = True   # 动态文本，_refresh_thinking_hint 重画
        self.lbl_thinking.pack(side="left", padx=10)
        self._refresh_thinking_hint()

        # 3.33 路由接管：8080 网页面板的采样参数/发送策略搬到这里统一。引擎的
        # Chat settings 只在"客户端没带该字段"时才生效（请求自带字段赢），所以
        # 代理剥掉客户端值、植入这里的值；字段留空=只剥不植，引擎自身默认生效。
        # 保存只写 routing.json，代理逐请求读 mtime——即时生效，两个服务都不重启。
        rout = ttk.LabelFrame(outer, text="路由接管（8080 面板 → 8082，保存即生效）",
                              padding=4)
        rout.pack(fill="x", pady=(0, 6), before=top)
        rc = load_routing()
        self.v_rt_take = BooleanVar(value=bool(rc.get("takeover")))
        ttk.Checkbutton(rout, text="采样接管", variable=self.v_rt_take).pack(
            side="left")
        rrow = ttk.Frame(rout)
        rrow.pack(side="left", padx=6)
        self.rt_entries = {}
        for key, label, w in (("temperature", "temp", 5), ("top_p", "top_p", 5),
                              ("top_k", "top_k", 5),
                              ("seed", "seed(空=随机)", 7),
                              ("max_tokens", "输出max_tok(空=跟随思考上限)", 8)):
            v = rc.get(key)
            ttk.Label(rrow, text=label).pack(side="left", padx=(4, 1))
            e = ttk.Entry(rrow, width=w)
            e.insert(0, "" if v is None else str(v))
            e.pack(side="left")
            self.rt_entries[key] = e
        ttk.Label(rout, text="发送策略").pack(side="left", padx=(8, 2))
        self.v_rt_policy = StringVar(
            value={"passthrough": "pass", "wait": "wait",
                   "interrupt": "stop"}.get(
                       str(rc.get("send_policy") or "passthrough"), "pass"))
        for key, label in (("pass", "跟随"), ("wait", "等待"), ("stop", "打断")):
            ttk.Radiobutton(rout, text=label, variable=self.v_rt_policy,
                            value=key).pack(side="left")
        self.v_rt_hide = BooleanVar(value=bool(rc.get("hide_thinking")))
        ttk.Checkbutton(rout, text="隐藏思考", variable=self.v_rt_hide).pack(
            side="left", padx=(8, 0))
        self.v_rt_arch = BooleanVar(value=bool(rc.get("chat_archive")))
        ttk.Checkbutton(rout, text="对话存档", variable=self.v_rt_arch).pack(
            side="left")
        ttk.Button(rout, text="保存", command=self._routing_save).pack(
            side="left", padx=8)
        self.lbl_rt = ttk.Label(rout, foreground="#666")
        self.lbl_rt._i18n_skip = True   # 动态文本，_refresh_routing_hint 重画
        self.lbl_rt.pack(side="left", padx=4)
        self._refresh_routing_hint()

        ctxbox = ttk.LabelFrame(outer, text="任务模型历史实况", padding=6)
        ctxbox.pack(fill="x", pady=(6, 0))
        self._ctx_hint = ttk.Label(
            ctxbox, foreground="#666",
            text="每轮实际发给任务模型的历史大小（压缩后正文 + 工具定义）；"
                 "「引擎实测」取响应 usage.prompt_tokens，未回传时显示 —；"
                 "「命中」取响应 cached_tokens：本地是 conversation cache 复用的 "
                 "reused，云端是隐式/显式缓存命中，0 是真 0（前缀断了或该模型不"
                 "支持隐式缓存）、— 是上游没报；代理停止后显示最后记录。")
        self._ctx_hint.pack(anchor="w", pady=(0, 4))
        self.ctx_tree = ttk.Treeview(
            ctxbox, columns=("conv", "body", "tools", "total", "raw",
                             "usage", "hit", "rate", "anchors", "ts"),
            show="headings", height=4)
        for cid, title, width, anchor in (
                ("conv", "会话编号", 120, "w"), ("body", "正文 tok", 90, "e"),
                ("tools", "工具 tok", 90, "e"), ("total", "合计 tok", 90, "e"),
                ("raw", "压缩前 tok", 95, "e"), ("usage", "引擎实测 tok", 95, "e"),
                ("hit", "命中 tok", 85, "e"), ("rate", "命中率", 70, "e"),
                ("anchors", "锚点回声", 90, "e"),
                ("ts", "更新时间", 90, "w")):
            self.ctx_tree.heading(cid, text=title)
            self.ctx_tree.column(cid, width=width, anchor=anchor,
                                 stretch=(cid == "conv"))
        self.ctx_tree.pack(fill="x")
        self._ctx_sig = None               # stats['updated']，变了才重灌表
        # 3.17 ②：点会话行 → 下方 KPI 图只看这条会话（None=全局）。
        self.kpi_conv = None
        self.ctx_tree.bind("<<TreeviewSelect>>", self._on_ctx_select)
        # 3.36 缓存回滚：选时刻 + 一次性恢复。命令走文件热读（同 routing.json
        # 的控制面规矩），下一轮对话生效；不按按钮就照常顺延记录。
        rb = ttk.Frame(ctxbox)
        rb.pack(fill="x", pady=(4, 0))
        ttk.Label(rb, text=i18n.tf("回滚会话：")).pack(side="left")
        self.rb_conv = ttk.Label(rb, text="—", width=18)
        self.rb_conv.pack(side="left")
        ttk.Label(rb, text=i18n.tf("恢复到：")).pack(side="left", padx=(8, 0))
        self.v_rb_time = StringVar()
        # 可手填：快照列表为空（本版启动后还没跑过一轮）时下拉没得选，
        # 至少要把时刻敲进去的可能留给用户。
        self.cb_rb_time = ttk.Combobox(rb, textvariable=self.v_rb_time,
                                       width=10)
        self.cb_rb_time.pack(side="left")
        self.btn_rb = ttk.Button(rb, text=i18n.tf("恢复缓存到该时刻"),
                                 command=self._on_cache_restore)
        self.btn_rb.pack(side="left", padx=(8, 0))
        self.rb_status = ttk.Label(rb, text="")
        self.rb_status.pack(side="left", padx=(8, 0))
        # 一次性语义写在按钮旁，别靠用户猜：只生效一次，之后照常顺延。
        ttk.Label(ctxbox, foreground="#666", text=i18n.tf(
            "备注：选定时间后按按钮确定，只对下一次对话生效；"
            "后续对话的缓存不会回到选定的时间点。")).pack(anchor="w")

        logbox = ttk.LabelFrame(outer, text="运行日志", padding=6)
        logbox.pack(fill="x", pady=(6, 0))
        self.log = scrolledtext.ScrolledText(logbox, height=6, wrap="none",
                                             font=("Consolas", 9), state="disabled")
        self.log.pack(fill="x")
        self.log.tag_config("err", foreground="#b00020")
        self.log.tag_config("ok", foreground="#0a7a2f")
        self.log.tag_config("hint", foreground="#555")

    # ---- 成本建议页
    def _build_cost_tab(self, cost: ttk.Frame) -> None:
        cost.columnconfigure(0, weight=1)
        cost.rowconfigure(1, weight=1)

        left = ttk.Frame(cost)
        left.grid(row=0, column=0, sticky="ew")
        left.columnconfigure(0, weight=1)
        self.price_target = PriceTable(left, "完成任务模型价目（每百万 token）",
                                       self.pricing_data["target"],
                                       show_cache_mode=True)
        self.price_target.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.price_comp = PriceTable(left, "压缩模型价目（每百万 token）",
                                     self.pricing_data["compressor"], height=5)
        self.price_comp.grid(row=1, column=0, sticky="ew")
        ttk.Label(left, foreground="#666", justify="left", wraplength=520,
                  text="五个价格字段对着账单填：输入 / 输出 / 缓存命中 / 显式命中 / 缓存写入。\n"
                       "「输入（缓存命中）」填命中列；「显式缓存命中」填显式列（不填沿用命中价）；\n"
                       "「显式缓存创建」填写入列，只在选显式缓存时计费。\n"
                       "时段支持跨零点（如 16:30-00:30），左闭右开。\n"
                       "压缩比越小越省钱，下限由你的质量底线决定，价格算不出来。"
                  ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        right = ttk.Frame(cost)
        right.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        right.columnconfigure(1, weight=1)
        adv = self.pricing_data.get("advisor", {})
        self.v_delta = IntVar(value=int(adv.get("delta", 8000)))
        self.v_ratio = DoubleVar(value=max(
            RATIO_MIN, min(RATIO_MAX, float(adv.get("ratio", DEFAULT_RATIO)))))
        self.v_ctx = IntVar(value=int(adv.get("context_tokens", 100000)))
        self.v_out_tokens = IntVar(value=int(adv.get("output_tokens", 1000)))
        self.v_min_net = DoubleVar(value=0.0)

        r = 0
        ttk.Label(right, text="每轮新增 token").grid(row=r, column=0, sticky="w", pady=3)
        drow = ttk.Frame(right)
        drow.grid(row=r, column=1, sticky="ew", pady=3)
        self.spin_delta = ttk.Spinbox(drow, from_=100, to=1000000, increment=500,
                                      width=10, textvariable=self.v_delta)
        self.spin_delta.pack(side="left")
        ttk.Button(drow, text="从代理读取", command=self._read_delta
                   ).pack(side="left", padx=6)
        self.lbl_delta_hint = ttk.Label(drow, text="代理按实际请求统计", foreground="#666")
        self.lbl_delta_hint.pack(side="left")

        r += 1
        self.lbl_ratio_name = ttk.Label(right, text="动态压缩比")
        self.lbl_ratio_name.grid(row=r, column=0, sticky="w", pady=3)
        rrow = ttk.Frame(right)
        rrow.grid(row=r, column=1, sticky="ew", pady=3)
        self.spin_ratio = ttk.Spinbox(rrow, from_=RATIO_MIN, to=RATIO_MAX,
                                      increment=0.01,
                                      width=8, textvariable=self.v_ratio)
        self.spin_ratio.pack(side="left")
        self.lbl_ratio_hint = ttk.Label(rrow, text="", foreground="#666",
                                        wraplength=380, justify="left")
        self.lbl_ratio_hint.pack(side="left", padx=(6, 0))
        self.v_ratio.trace_add("write", lambda *_: self._update_ratio_hint())
        self._update_ratio_hint()

        r += 1
        ttk.Label(right, text="当前历史 token").grid(row=r, column=0, sticky="w", pady=3)
        self.spin_ctx = ttk.Spinbox(right, from_=1000, to=2000000, increment=5000,
                                    width=12, textvariable=self.v_ctx)
        self.spin_ctx.grid(row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Label(right, text="每轮输出 token").grid(row=r, column=0, sticky="w", pady=3)
        orow = ttk.Frame(right)
        orow.grid(row=r, column=1, sticky="ew", pady=3)
        ttk.Spinbox(orow, from_=0, to=1000000, increment=100, width=10,
                    textvariable=self.v_out_tokens).pack(side="left")
        ttk.Label(orow, text="  含思考；只影响整轮花销，不影响净收益",
                  foreground="#666").pack(side="left")

        r += 1
        ttk.Label(right, text="最少每轮净省").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(right, textvariable=self.v_min_net, width=10
                  ).grid(row=r, column=1, sticky="w", pady=3)

        r += 1
        btns = ttk.Frame(right)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        ttk.Button(btns, text="计算建议", command=self._compute_advice).pack(side="left")
        ttk.Button(btns, text="应用到启动参数", command=self._apply_advice
                   ).pack(side="left", padx=6)
        ttk.Button(btns, text="保存价目表", command=self._save_pricing
                   ).pack(side="left")

        r += 1
        right.rowconfigure(r, weight=1)
        self.advice_text = scrolledtext.ScrolledText(right, width=1, height=10,
                                                     wrap="word",
                                                     font=("Consolas", 9))
        self.advice_text.grid(row=r, column=0, columnspan=2, sticky="nsew")
        self._advice_placeholder = "填好价目表后点「计算建议」。"
        self.advice_text.insert("end", i18n.t(self._advice_placeholder))
        self._advice_is_placeholder = True
        self.advice_text.configure(state="disabled")

    # ---- 本地模型时间账
    def _build_local_tab(self, local: ttk.Frame) -> None:
        loc = self.pricing_data.get("local", {})
        local.columnconfigure(0, weight=1)
        local.rowconfigure(1, weight=1)

        left = ttk.Frame(local)
        left.grid(row=0, column=0, sticky="ew")
        left.columnconfigure(1, weight=1)
        right = ttk.Frame(local)
        right.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.v_l_prefill = IntVar(value=int(loc.get("prefill", 1000)))
        self.v_l_decode = IntVar(value=int(loc.get("decode", 66)))
        self.v_c_prefill = IntVar(value=int(loc.get("comp_prefill", 3000)))
        self.v_c_decode = IntVar(value=int(loc.get("comp_decode", 100)))
        self.v_c_prefill_agg = IntVar(
            value=int(loc.get("comp_prefill_agg", 0) or 0))
        self.v_c_decode_agg = IntVar(
            value=int(loc.get("comp_decode_agg", 0) or 0))
        self.v_bench_conc = IntVar(
            value=int(loc.get("bench_concurrency", 0) or 0))
        self.v_c_latency = DoubleVar(value=float(loc.get("comp_latency", 1.0)))
        self.v_time_value = DoubleVar(value=float(loc.get("time_value", 0.0)))
        self.v_c_conc = IntVar(value=int(loc.get("concurrency", 16)))
        self.v_c_chunk = IntVar(value=int(loc.get("chunk", 12000)))
        self.v_local_threshold = StringVar(value="—")
        self.v_bulk_threshold = StringVar(value="—")
        self.v_profile_threshold = StringVar(value="—")
        self.v_current_period = StringVar(value="—")
        self.v_calc_mode = StringVar(value="计算口径：—")
        self.v_auto_period = BooleanVar(value=False)

        def add_pair(row: int, col: int, label: str, var, frm, to, inc,
                     suffix: str = "") -> None:
            cell = ttk.Frame(left)
            cell.grid(row=row, column=col, sticky="w", pady=2)
            ttk.Label(cell, text=label).pack(side="left")
            ttk.Spinbox(cell, from_=frm, to=to, increment=inc, width=8,
                        textvariable=var).pack(side="left", padx=(3, 0))
            if suffix:
                ttk.Label(cell, text=suffix, foreground="#666").pack(
                    side="left", padx=(2, 0))

        left.columnconfigure(0, weight=1)
        left.columnconfigure(1, weight=1)
        add_pair(0, 0, "本地预填充", self.v_l_prefill, 1, 100000, 100, "tok/s")
        add_pair(0, 1, "本地生成", self.v_l_decode, 1, 10000, 1, "tok/s")
        add_pair(1, 0, "压缩预填充", self.v_c_prefill, 1, 100000, 100, "tok/s")
        add_pair(1, 1, "压缩生成", self.v_c_decode, 1, 10000, 10, "tok/s")
        ttk.Label(left, foreground="#666", justify="left", wraplength=620,
                  text="压缩速度按单路实测填；测速自动填单路与合计。"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 2))

        brow = ttk.Frame(left)
        brow.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(brow, text="并发实测合计").pack(side="left")
        ttk.Spinbox(brow, from_=0, to=1000000, increment=100, width=7,
                    textvariable=self.v_c_prefill_agg).pack(side="left", padx=(4, 1))
        ttk.Label(brow, text="预填 /").pack(side="left")
        ttk.Spinbox(brow, from_=0, to=1000000, increment=100, width=7,
                    textvariable=self.v_c_decode_agg).pack(side="left", padx=(2, 1))
        ttk.Label(brow, text="生成 tok/s").pack(side="left")
        ttk.Spinbox(brow, from_=0, to=128, increment=1, width=4,
                    textvariable=self.v_bench_conc).pack(side="left", padx=(4, 1))
        ttk.Label(brow, text="路").pack(side="left")
        ttk.Label(left, foreground="#666", justify="left", wraplength=620,
                  text="0 = 未测；合计只用于首压 / 多段。"
                  ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 2))

        add_pair(5, 0, "压缩固定延迟", self.v_c_latency, 0, 120, 0.5, "秒")
        add_pair(5, 1, "压缩并发", self.v_c_conc, 1, 128, 1, "路")
        add_pair(6, 0, "分段长度", self.v_c_chunk, 256, 200000, 1000, "token")
        add_pair(6, 1, "时间价值", self.v_time_value, 0, 10000, 10, "元/时")
        ttk.Label(left, foreground="#666", wraplength=620, justify="left",
                  text="每轮新增 / 动态压缩比 / 当前历史 / 每轮输出与「成本建议」页共用。"
                  ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 2))

        out = ttk.Frame(left)
        out.grid(row=8, column=0, columnspan=2, sticky="ew")
        out.columnconfigure(1, weight=1)
        for i, (label, var, color) in enumerate((
                ("当前时段", self.v_current_period, "#005a9e"),
                ("计算口径", self.v_calc_mode, "#005a9e"),
                ("建议压缩阈值", self.v_local_threshold, "#087f23"),
                ("首压 / 多段", self.v_bulk_threshold, "#005a9e"),
                ("当前配置阈值", self.v_profile_threshold, "#666"))):
            ttk.Label(out, text=label).grid(row=i, column=0, sticky="nw", pady=1)
            lbl = ttk.Label(out, textvariable=var, foreground=color,
                            wraplength=380, justify="left")
            lbl.grid(row=i, column=1, sticky="w", pady=1)
            if label == "建议压缩阈值":
                self.lbl_suggest_threshold = lbl
            elif label == "当前配置阈值":
                self.lbl_profile_threshold = lbl

        ttk.Checkbutton(
            left, text="时段切换时自动更新配置（代理需重启生效）",
            variable=self.v_auto_period
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # 两个百分比都要换算成 token 才有意义，所以先准备一个「模型上下文」。
        # 0 = 自动：本地引擎启动后读 /health，读不到按 128k 算。
        self._engine_ctx = 0
        self._ctx_probe_running = False
        # 表单加载/程序化赋值期间不要触发“写回配置”，否则会用旧值覆盖新配置。
        self._loading_form = False
        self._effective_from_target = False
        self._ctx_save_job = None
        self._audit_running = False
        self._loading_target = False
        self._t_ctx_setting = False
        self._t_ctx_manual = False
        self._target_probe_gen = 0
        self._target_probe_job = None
        self.v_model_ctx = IntVar(value=0)
        # 建议上下文区间：社区实测“记得住”的窗口，通常远小于原生上限。
        self.v_work_window = IntVar(value=WORK_WINDOW_DEFAULT)
        self.v_auto_params = StringVar(value="")
        self.v_auto_window = BooleanVar(value=True)
        self.v_ctx_source = StringVar(value="")
        self.v_dump = BooleanVar(value=False)

        rc_row = ttk.Frame(left)
        rc_row.grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.v_recompact = BooleanVar(value=True)
        self.v_recompact_pct = IntVar(value=int(RECOMPACT_DEFAULT_TRIGGER * 100))
        ttk.Checkbutton(
            rc_row, text="更早的聊天记录太占地方时，再压一次",
            variable=self.v_recompact
        ).pack(side="left")
        ttk.Label(rc_row, text="涨到", foreground="#666").pack(side="left", padx=(8, 2))
        ttk.Spinbox(rc_row, from_=30, to=95, increment=5, width=5,
                    textvariable=self.v_recompact_pct).pack(side="left")
        ttk.Label(rc_row, text="% 时", foreground="#666").pack(side="left", padx=(2, 0))
        self.lbl_recompact_tok = ttk.Label(rc_row, text="", foreground="#005a9e")
        self.lbl_recompact_tok.pack(side="left", padx=(6, 0))
        self.lbl_recompact_hint = ttk.Label(
            left, foreground="#666", justify="left", wraplength=620, text="")
        self.lbl_recompact_hint._i18n_skip = True   # 文案随开关变，由 _update_recompact_hint 维护
        self.lbl_recompact_hint.grid(row=11, column=0, columnspan=2,
                                     sticky="w", pady=(2, 0))
        self.v_recompact.trace_add("write", lambda *_: self._update_recompact_hint())
        self.v_recompact_pct.trace_add("write", lambda *_: self._update_recompact_hint())
        self._update_recompact_hint()
        # v2.4：手动压缩策略下线，相关控件保留对象但不再显示。
        rc_row.grid_remove()
        self.lbl_recompact_hint.grid_remove()

        lz_row = ttk.Frame(left)
        lz_row.grid(row=12, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.v_lazy = BooleanVar(value=False)
        self.v_lazy_pct = IntVar(value=int(LAZY_DEFAULT_TRIGGER * 100))
        ttk.Checkbutton(
            lz_row, text="平时不压缩，攒够了再压",
            variable=self.v_lazy
        ).pack(side="left")
        ttk.Label(lz_row, text="涨到", foreground="#666").pack(side="left", padx=(8, 2))
        ttk.Spinbox(lz_row, from_=50, to=95, increment=5, width=5,
                    textvariable=self.v_lazy_pct).pack(side="left")
        ttk.Label(lz_row, text="% 时", foreground="#666").pack(side="left", padx=(2, 0))
        self.lbl_lazy_tok = ttk.Label(lz_row, text="", foreground="#005a9e")
        self.lbl_lazy_tok.pack(side="left", padx=(6, 0))
        self.lbl_lazy_hint = ttk.Label(
            left, foreground="#666", justify="left", wraplength=620, text="")
        self.lbl_lazy_hint._i18n_skip = True   # 文案随开关变，由 _update_lazy_hint 维护
        self.lbl_lazy_hint.grid(row=13, column=0, columnspan=2,
                                sticky="w", pady=(2, 0))
        self.v_lazy.trace_add("write", lambda *_: self._update_lazy_hint())
        self.v_lazy_pct.trace_add("write", lambda *_: self._update_lazy_hint())
        self._update_lazy_hint()
        lz_row.grid_remove()
        self.lbl_lazy_hint.grid_remove()

        ctx_row = ttk.Frame(left)
        ctx_row.grid(row=14, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ctx1 = ttk.Frame(ctx_row)
        ctx1.pack(anchor="w")
        ttk.Label(ctx1, text="最大上下文").pack(side="left")
        self.spin_model_ctx = ttk.Spinbox(
            ctx1, from_=0, to=2000000, increment=8000, width=10,
            textvariable=self.v_model_ctx)
        self.spin_model_ctx.pack(side="left", padx=(6, 4))
        ttk.Label(ctx1, text="token（0 = 自动读本地引擎，读不到按 12.8 万算）",
                  foreground="#666").pack(side="left")

        ctx2 = ttk.Frame(ctx_row)
        ctx2.pack(anchor="w", pady=(3, 0))
        ttk.Label(ctx2, text="建议上下文").pack(side="left")
        self.spin_work_window = ttk.Spinbox(
            ctx2, from_=WORK_WINDOW_MIN, to=WORK_WINDOW_MAX,
            increment=8000, width=10, textvariable=self.v_work_window)
        self.spin_work_window.pack(side="left", padx=(6, 4))
        ttk.Label(ctx2, text="token（去社区查：该模型记得最稳的区间；"
                            "原生 1M 也建议先填 200000）",
                  foreground="#005a9e").pack(side="left")

        ctx3 = ttk.Frame(ctx_row)
        ctx3.pack(anchor="w", pady=(2, 0))
        ttk.Label(ctx3, text="自动（推荐）", foreground="#087f23").pack(side="left")
        ttk.Label(ctx3, textvariable=self.v_auto_params, foreground="#087f23",
                  wraplength=760, justify="left").pack(side="left", padx=(8, 0))

        ctx4 = ttk.Frame(ctx_row)
        ctx4.pack(anchor="w", pady=(1, 0))
        ttk.Label(ctx4, textvariable=self.v_ctx_source,
                  foreground="#005a9e", wraplength=760,
                  justify="left").pack(side="left")

        ttk.Label(ctx_row, foreground="#666", wraplength=760, justify="left",
                  text="只改上面两个数：想记得更多就把「建议上下文」调大；"
                       "想更快/更省钱就调小。压缩时机、热层、复位都会自动算。"
                  ).pack(anchor="w", pady=(2, 0))

        self.v_model_ctx.trace_add("write", self._on_ctx_field_change)
        self.v_work_window.trace_add("write", self._on_ctx_field_change)
        if hasattr(self, "v_t_enabled"):
            self.v_t_enabled.trace_add(
                "write", lambda *_: self._sync_effective_ctx())

        btns = ttk.Frame(left)
        btns.grid(row=15, column=0, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Button(btns, text="计算本地账", command=self._compute_local
                   ).pack(side="left")
        ttk.Button(btns, text="保存价目表", command=self._save_pricing
                   ).pack(side="left")

        ttk.Label(left, foreground="#666", justify="left", wraplength=620,
                  text="本地模型不收 token 费，压缩在钱上是纯支出，买到的是时间。\n"
                       "压缩模型的预填充/生成速度要按实际 API 填，它决定结论。"
                  ).grid(row=16, column=0, columnspan=2, sticky="w", pady=(4, 0))

        diag = ttk.Frame(left)
        diag.grid(row=17, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(diag, text="记录原始数据与转发数据",
                        variable=self.v_dump,
                        command=self._on_dump_toggle).pack(side="left")
        ttk.Button(diag, text="打开数据目录",
                   command=self._open_dump_dir).pack(side="left", padx=(8, 0))
        ttk.Button(diag, text="分析数据",
                   command=self._run_audit_safe).pack(side="left", padx=(4, 0))
        ttk.Label(left, foreground="#666", justify="left", wraplength=620,
                  text="开启后，系统会保存您发送的原始请求和收到的转发回应，方便日后核对或排查问题。"
                  ).grid(row=18, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.local_text = scrolledtext.ScrolledText(
            right, width=1, height=10, wrap="word", font=table_font(self.root))
        self.local_text.grid(row=0, column=0, sticky="nsew")
        self._local_placeholder = "填好速度后点「计算本地账」。"
        self.local_text.insert("end", i18n.t(self._local_placeholder))
        self._local_is_placeholder = True
        self.local_text.configure(state="disabled")

        # 输入变 → 建议阈值同步变；选中配置变 → 当前配置阈值同步变
        for var in (self.v_l_prefill, self.v_l_decode, self.v_c_prefill,
                    self.v_c_decode, self.v_c_prefill_agg,
                    self.v_c_decode_agg, self.v_bench_conc,
                    self.v_c_latency, self.v_c_conc,
                    self.v_c_chunk, self.v_delta, self.v_ratio, self.v_ctx,
                    self.v_out_tokens):
            var.trace_add("write", self._sync_local_threshold)
        self._sync_local_threshold()
        if self.current_index is not None:
            self.v_profile_threshold.set(
                self._profile_threshold_text(self.profiles[self.current_index]))
        else:
            self.v_profile_threshold.set("未选中配置")

    # ---- 成本建议
    def _advice_params(self) -> dict:
        try:
            delta = max(1, int(self.v_delta.get()))
        except (TclError, ValueError):
            delta = 8000
        try:
            ratio = max(RATIO_MIN, min(RATIO_MAX, float(self.v_ratio.get())))
        except (TclError, ValueError):
            ratio = DEFAULT_RATIO
        try:
            ctx = max(1000, int(self.v_ctx.get()))
        except (TclError, ValueError):
            ctx = 100000
        try:
            out_tokens = max(0, int(self.v_out_tokens.get()))
        except (TclError, ValueError):
            out_tokens = 0
        try:
            min_net = float(self.v_min_net.get())
        except (TclError, ValueError):
            min_net = 0.0
        return {"delta": delta, "ratio": ratio, "context_tokens": ctx,
                "output_tokens": out_tokens, "min_net": min_net}

    def _compute_advice(self) -> dict:
        p = self._advice_params()
        self.price_target._apply_mode()
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        result = price_mod.compute_advice(
            self.price_target.model, self.price_comp.model,
            p["delta"], p["ratio"], p["context_tokens"],
            price_mod.cache_mode(self.price_target.model, True) != "none",
            minute, p["min_net"], output_tokens=p["output_tokens"])
        self.last_advice = result
        self.advice_text.configure(state="normal")
        self.advice_text.delete("1.0", "end")
        self.advice_text.insert("end", price_mod.format_advice(result))
        self.advice_text.configure(state="disabled")
        self._advice_is_placeholder = False   # 计算结果是中文正文，不在翻译范围
        self._save_pricing()
        return result

    def _local_params(self) -> dict:
        def as_int(var, default):
            try:
                return max(1, int(var.get()))
            except (TclError, ValueError):
                return default

        def as_float(var, default):
            try:
                return max(0.0, float(var.get()))
            except (TclError, ValueError):
                return default

        def as_nonneg(var, default=0):
            try:
                return max(0, int(var.get()))
            except (TclError, ValueError):
                return default

        return {
            "prefill": as_int(self.v_l_prefill, 1000),
            "decode": as_int(self.v_l_decode, 66),
            "comp_prefill": as_int(self.v_c_prefill, 3000),
            "comp_decode": as_int(self.v_c_decode, 100),
            "comp_prefill_agg": as_nonneg(self.v_c_prefill_agg),
            "comp_decode_agg": as_nonneg(self.v_c_decode_agg),
            "bench_concurrency": as_nonneg(self.v_bench_conc),
            "comp_latency": as_float(self.v_c_latency, 1.0),
            "time_value": as_float(self.v_time_value, 0.0),
            "concurrency": as_int(self.v_c_conc, 16),
            "chunk": as_int(self.v_c_chunk, 12000),
        }

    def _local_result(self) -> dict:
        p = self._advice_params()
        loc = self._local_params()
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        return price_mod.compute_local_time(
            self.price_comp.model, p["delta"], p["ratio"], p["context_tokens"],
            p["output_tokens"], loc["prefill"], loc["decode"],
            loc["comp_prefill"], loc["comp_decode"], loc["comp_latency"],
            minute, loc["time_value"], loc["concurrency"], loc["chunk"],
            comp_prefill_agg=loc["comp_prefill_agg"],
            comp_decode_agg=loc["comp_decode_agg"],
            bench_concurrency=loc["bench_concurrency"])

    def _calc_mode_text(self, r: dict) -> str:
        """说清楚这次阈值是按单路还是按并发合计算的。"""
        chunks = int(r.get("turn_chunks") or 1)
        conc = int(r.get("concurrency") or 1)
        bench = int(r.get("bench_concurrency") or 0)
        if chunks <= 1:
            return "稳态1段/单路"
        if r.get("comp_decode_agg", 0) > 0 and bench > 0:
            return f"稳态{chunks}段/{min(chunks, conc)}路合计"
        return f"稳态{chunks}段/{conc}路线性"

    def _bulk_text(self, r: dict) -> str:
        """首压/多段的墙钟，明确按单路还是按实测合计算。"""
        if not r.get("ok"):
            return "—"
        chunks = int(r.get("first_chunks") or 1)
        if chunks <= 1:
            return "只有 1 段，按单路"
        if int(r.get("concurrency") or 1) <= 1:
            mode = "单路"
        elif r.get("comp_decode_agg", 0) > 0:
            mode = f"{min(chunks, int(r['concurrency']))} 路合计"
        else:
            mode = f"{r['concurrency']} 路线性"
        ft = r.get("first_threshold_n")
        ft_txt = f"{ft:,.0f}" if ft else "无"
        return (f"{chunks} 段 / {r['first_waves']} 波，"
                f"{r['first_wall']:.1f}s（{mode}）；打平 {ft_txt}")

    def _show_local_threshold(self, r: dict) -> None:
        self.v_bulk_threshold.set(self._bulk_text(r))
        if not r.get("ok"):
            self.v_local_threshold.set(r.get("reason", "无法计算"))
            self.v_calc_mode.set("计算口径：—")
        elif r.get("threshold_n") is None or r.get("saved", 0) <= 0:
            self.v_local_threshold.set("按当前速度不划算，无建议阈值")
            self.v_calc_mode.set("计算口径：—")
        else:
            mode = self._calc_mode_text(r)
            self.v_local_threshold.set(
                f"{r['threshold_total']:,}（总正文·{mode}）  "
                f"纯历史 {r['threshold_history']:,}  "
                f"目标正文 {r['target_msg']:,}")
            if r.get("comp_decode_agg", 0) > 0:
                if r["turn_chunks"] == 1 and r["first_chunks"] > 1:
                    self.v_calc_mode.set(
                        f"计算口径：阈值按单路；首压 {r['first_chunks']} 段"
                        f"按 {r['bench_concurrency']} 路实测合计")
                else:
                    self.v_calc_mode.set(
                        f"计算口径：按 {r['bench_concurrency']} 路实测合计折算")
            else:
                self.v_calc_mode.set(
                    f"计算口径：未测并发合计，按 {r['concurrency']} 路线性放大")

    def _current_windows(self) -> tuple:
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        return (price_mod.window_for(self.price_target.model, minute),
                price_mod.window_for(self.price_comp.model, minute))

    def _period_key(self) -> tuple:
        tw, cw = self._current_windows()
        return ((tw or {}).get("start"), (tw or {}).get("end"),
                (cw or {}).get("start"), (cw or {}).get("end"))

    def _update_current_period(self) -> None:
        _, cw = self._current_windows()
        if cw:
            now = time.localtime()
            self.v_current_period.set(
                f"{cw.get('start')}-{cw.get('end')}  "
                f"（系统时间 {now.tm_hour:02d}:{now.tm_min:02d}）")
        else:
            self.v_current_period.set("当前时间不在压缩模型价目表内")

    def _on_period_change(self) -> None:
        """系统时间跨过价目边界：按新时段重算并刷新界面。"""
        self._update_current_period()
        self._sync_local_threshold()
        if self.v_auto_period.get():
            self._apply_local()
            self._say("时段切换：已按新时段更新当前配置；"
                      "代理正在运行的话，重启后才生效", "hint")
        else:
            self._compute_local()
            self._say("时段切换：已按新时段重算本地方案", "hint")
        if self.advice_text.get("1.0", "end").strip():
            self._compute_advice()

    def _sync_local_threshold(self, *_args) -> None:
        """输入一变就刷新建议阈值，不用先点「计算本地账」。"""
        try:
            self._update_current_period()
            self._show_local_threshold(self._local_result())
        except Exception:
            self.v_local_threshold.set("—")
            self.v_calc_mode.set("计算口径：—")

    def _compute_local(self) -> dict:
        result = self._local_result()
        self.last_local = result
        self._show_local_threshold(result)
        self.local_text.configure(state="normal")
        self.local_text.delete("1.0", "end")
        self.local_text.insert("end", price_mod.format_local_advice(result))
        self.local_text.configure(state="disabled")
        self._local_is_placeholder = False   # 计算结果是中文正文，不在翻译范围
        self._save_pricing()
        return result

    def _apply_local(self) -> None:
        result = self._compute_local()
        if not result.get("ok"):
            messagebox.showwarning("本地模型", result.get("reason", "无法计算"))
            return
        if result["threshold_n"] is None or result["saved"] <= 0:
            messagebox.showwarning(
                "本地模型",
                "按当前速度，压缩在时间上不划算，没有可用的触发阈值。\n"
                "把压缩模型的生成速度填成实际值再看，或不要开压缩。")
            return
        if self.current_index is None:
            return
        self._commit_form()
        prof = self.profiles[self.current_index]
        prof["threshold"] = int(result["threshold_total"])
        prof["target_msg"] = int(result["target_msg"])
        # 自动策略下，计算值只作参考，不再切换成手动模式。
        prof["auto_window"] = True
        prof["ratio"] = float(result["ratio"])
        save_profiles(self.profiles)
        self._sync_profile_display(prof)
        ratio_label = "动态压缩比" if self._ratio_is_dynamic() else "压缩比"
        self._say(f"已把本地时间阈值 {prof['threshold']:,} / 目标正文 "
                  f"{prof['target_msg']:,} / {ratio_label} {prof['ratio']:.2f} "
                  f"写进「{prof['name']}」", "ok")
        if result["first_saved"] <= 0:
            self._say(f"注意：首次整段压缩会多花 {-result['first_saved']:.1f}s，"
                      f"靠后续轮次省回来", "hint")

    def _apply_advice(self) -> None:
        result = self._compute_advice()
        if not result.get("ok"):
            messagebox.showwarning("成本建议", result.get("reason", "无法计算"))
            return
        if result["threshold_total"] is None:
            messagebox.showwarning("成本建议", "按当前价格压缩永远不划算，没有可用的阈值。")
            return
        if self.current_index is None:
            return
        self._commit_form()
        prof = self.profiles[self.current_index]
        prof["threshold"] = int(result["threshold_total"])
        prof["target_msg"] = int(result["target_msg"])
        # 自动策略下，成本建议只作参考。
        prof["auto_window"] = True
        prof["ratio"] = float(result["ratio"])
        save_profiles(self.profiles)
        self._sync_profile_display(prof)
        ratio_label = "动态压缩比" if self._ratio_is_dynamic() else "压缩比"
        self._say(f"已把阈值 {prof['threshold']:,} / 目标正文 {prof['target_msg']:,} / "
                  f"{ratio_label} {prof['ratio']:.2f} 写进「{prof['name']}」", "ok")

    def _save_pricing(self) -> None:
        self.price_target._apply_mode()
        # 价目表现在是「每条模型各一份」，表格里显示的是当前选中模型的那份。
        # 只有表格还挂着全局那份时才回写；否则选中一条空价目就会把全局表清空
        # （2026-11-04 实测：本地配置价目为空，每次启动都会清掉压缩模型价目）。
        if self.price_target.model is self.pricing_data.get("target"):
            self.pricing_data["target"] = self.price_target.model
        if self.price_comp.model is self.pricing_data.get("compressor"):
            self.pricing_data["compressor"] = self.price_comp.model
        p = self._advice_params()
        self.pricing_data["advisor"] = {
            "delta": p["delta"], "ratio": p["ratio"],
            "context_tokens": p["context_tokens"],
            "cache_mode": price_mod.cache_mode(self.price_target.model, True),
            "output_tokens": p["output_tokens"]}
        self.pricing_data["local"] = self._local_params()
        save_pricing(self.pricing_data)
        save_profiles(self.profiles)
        save_targets(self.targets)
        self._say("价目表已保存（按当前选中的压缩模型 / 云端模型）", "hint")

    def _read_delta(self) -> None:
        stats = proxy_stats()
        if not stats.get("samples"):
            self.lbl_delta_hint.configure(text="还没有统计数据（代理没跑过长对话）")
            return
        self.v_delta.set(int(stats.get("delta_avg") or 0))
        self.lbl_delta_hint.configure(
            text=f"样本 {stats.get('samples')}，中位 {stats.get('delta_median')}，"
                 f"更新 {stats.get('updated', '')}")

    # ---- 列表/表单联动
    @staticmethod
    def _row_values(p: dict) -> tuple:
        try:
            th = int(p.get("threshold") or 0)
        except (TypeError, ValueError):
            th = 0
        url = ("(本地 127.0.0.1:8080)" if p.get("local")
               else (p["base_url"] or "(空)"))
        return (p["name"], p["concurrency"], f"{th:,}", url)

    @staticmethod
    def _ratio_is_dynamic() -> bool:
        """与启动代理时的 PROXY_DENSITY_CAP 保持同一判定。"""
        return os.environ.get("LONGCTX_DENSITY_CAP", "1").strip() != "0"

    def _update_ratio_hint(self) -> None:
        """动态压缩比是区间中心，界面必须同时标出默认值和实际区间。"""
        if not hasattr(self, "lbl_ratio_hint"):
            return
        try:
            v = float(self.v_ratio.get())
        except (TypeError, ValueError, TclError):
            return
        v = max(RATIO_MIN, min(RATIO_MAX, v))
        if self._ratio_is_dynamic():
            self.lbl_ratio_name.configure(text="动态压缩比")
            lo, hi = ratio_band(v)
            dlo, dhi = ratio_band(DEFAULT_RATIO)
            band = (f"区间塌缩为固定值 {hi:.2f}" if lo >= hi
                    else f"实际区间 {lo:.2f}~{hi:.2f}")
            self.lbl_ratio_hint.configure(
                text=(f"默认 {DEFAULT_RATIO:.2f}，±0.10（{dlo:.2f}~{dhi:.2f}）；"
                      f"当前 {v:.2f} = 保留 {v * 100:.0f}% 正文，{band}"))
        else:
            self.lbl_ratio_name.configure(text="压缩比")
            self.lbl_ratio_hint.configure(
                text=f"{v:.2f} = 保留 {v * 100:.0f}% 正文（固定比例）")

    def _active_work_window(self) -> int:
        """当前生效的“建议上下文区间”：任务模型优先，其次主配置。"""
        try:
            if (hasattr(self, "v_t_work") and getattr(self, "current_target_index", None) is not None
                    and getattr(self, "v_t_enabled", None) is not None and self.v_t_enabled.get()):
                v = int(self.v_t_work.get())
                if v > 0:
                    return v
        except (TclError, ValueError, AttributeError):
            pass
        try:
            return max(0, int(self.v_work_window.get()))
        except (TclError, ValueError, AttributeError):
            return 0

    def _cap_ctx(self, v: int, src: str) -> tuple[int, str]:
        """百分比换算用“有效窗口”，不是 1M 原生上限。"""
        ww = self._active_work_window()
        if ww > 0 and v > ww:
            return ww, src
        return v, src

    def _ctx_source(self) -> tuple[int, str]:
        """(任务模型的窗口大小, 来源)。优先级：
        1. 已启用的「任务模型」里填的上下文能力；
        2. 「模型上下文」输入框（本地引擎 / 主配置）；
        3. 本地引擎探测值；
        4. 都没有 —— 按 12.8 万估，并在界面上标明是估算。

        第 1 条直接读表单里的实时值，不等「保存列表」：否则用户在
        上下文能力里打字，后面的 token 数不会跟着动。"""
        try:
            if (hasattr(self, "v_t_ctx")
                    and self.current_target_index is not None
                    and self.v_t_enabled.get()):
                v = int(self.v_t_ctx.get())
                if v > 0:
                    return self._cap_ctx(v, "target")
        except (TclError, ValueError):
            pass
        try:
            v = int(self.v_model_ctx.get())
        except (TclError, ValueError):
            v = 0
        if v > 0:
            return self._cap_ctx(v, "manual")
        if self._engine_ctx:
            return self._cap_ctx(self._engine_ctx, "engine")
        return 128000, "estimate"

    def _model_ctx_tokens(self) -> int:
        return self._ctx_source()[0]

    # ---- 表单同步：上下文/建议上下文一改就写回配置，不用再点保存
    def _on_ctx_field_change(self, *_args) -> None:
        self._update_ctx_hints()
        if (getattr(self, "_loading_form", False)
                or getattr(self, "_effective_from_target", False)):
            return
        job = getattr(self, "_ctx_save_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._ctx_save_job = self.root.after(700, self._save_ctx_fields)

    def _save_ctx_fields(self) -> None:
        self._ctx_save_job = None
        if getattr(self, "_effective_from_target", False):
            return
        ci = getattr(self, "current_index", None)
        if ci is None or not (0 <= ci < len(self.profiles)):
            return
        p = self.profiles[ci]
        try:
            p["model_context"] = max(0, min(2_000_000, int(self.v_model_ctx.get())))
        except (TclError, ValueError):
            p["model_context"] = 0
        try:
            p["work_window"] = max(WORK_WINDOW_MIN, min(
                WORK_WINDOW_MAX, int(self.v_work_window.get())))
        except (TclError, ValueError):
            p["work_window"] = WORK_WINDOW_DEFAULT
        save_profiles(self.profiles)
        self.tree.item(str(ci), values=self._row_values(p))
        self._sync_profile_display(p)

    def _on_dump_toggle(self) -> None:
        """召回审计 dump 开关：写回当前配置；代理在跑就停掉，提示重启。"""
        ci = getattr(self, "current_index", None)
        if ci is not None and 0 <= ci < len(self.profiles):
            self.profiles[ci]["recall_dump"] = bool(self.v_dump.get())
            save_profiles(self.profiles)
        if self.proc is not None and self.proc.poll() is None:
            self._stop()
            self._say("已停止代理：记录开关需要重新启动代理才生效。", "hint")
        else:
            self._say("记录开关已保存；下次启动代理生效。", "hint")

    def _open_dump_dir(self) -> None:
        d = proxy_dir() / "dumps"
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))
        except Exception as exc:
            self._say(f"打开 dump 目录失败：{exc}", "err")

    def _run_audit_safe(self) -> None:
        try:
            self._run_audit()
        except Exception as exc:
            self._say(f"分析失败：{exc}", "err")

    def _run_audit(self) -> None:
        if getattr(self, "_audit_running", False):
            self._say("正在分析，请稍候……", "hint")
            return
        script = app_dir().parent / "audit_recall.py"
        if not script.exists():
            self._say(f"找不到审计脚本：{script}", "err")
            return
        py = python_exe()
        if getattr(sys, "frozen", False):
            try:
                if Path(py).resolve() == Path(sys.executable).resolve():
                    self._say("找不到可用的 Python 解释器：请检查 app/python_path.txt。", "err")
                    return
            except Exception:
                pass
        if not Path(py).exists():
            self._say(f"找不到 Python 解释器：{py}", "err")
            return
        self._audit_running = True
        self._say("开始分析数据（最新一对）……", "hint")

        def work():
            try:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUNBUFFERED"] = "1"
                kwargs = {}
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                r = subprocess.run(
                    [str(py), str(script), "--latest"],
                    cwd=str(app_dir().parent), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180,
                    stdin=subprocess.DEVNULL, env=env, **kwargs)
                out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
                if len(out) > 200_000:
                    out = out[:200_000] + "\n……（输出过长，已截断）"
                self.log_q.put(("ok" if r.returncode == 0 else "err", out))
            except Exception as exc:
                self.log_q.put(("err", f"分析失败：{exc}\n"))
            finally:
                self._audit_running = False

        threading.Thread(target=work, daemon=True).start()

    def _sync_effective_ctx(self) -> None:
        """任务模型启用时，主界面的“最大/建议上下文”显示实际生效值。

        build_env 里是“任务模型优先”，但表单原来一直显示主配置自己的
        0/200000，看起来像没同步。启用任务模型时直接镜像过来并置灰。
        """
        if getattr(self, "_loading_target", False):
            return
        ti = getattr(self, "current_target_index", None)
        use_target = False
        t = None
        try:
            use_target = (ti is not None and 0 <= ti < len(self.targets)
                          and bool(self.v_t_enabled.get())
                          and bool((self.v_t_url.get() or "").strip()))
            if use_target:
                t = self.targets[ti]
        except (TclError, AttributeError):
            use_target = False

        if use_target:
            self._effective_from_target = True
            self._loading_form = True
            try:
                # 当前选中的任务模型以表单实时值为准（用户可能正在改），
                # 其他情况用已保存的值。
                if ti == getattr(self, "current_target_index", None):
                    ctx = int(self.v_t_ctx.get())
                    ww = int(self.v_t_work.get())
                else:
                    ctx = int(t.get("context") or 0)
                    ww = int(t.get("work_window") or WORK_WINDOW_DEFAULT)
                self.v_model_ctx.set(ctx)
                self.v_work_window.set(ww)
            except (TclError, ValueError):
                pass
            finally:
                self._loading_form = False
            for w in (getattr(self, "spin_model_ctx", None),
                      getattr(self, "spin_work_window", None)):
                if w is not None:
                    w.configure(state="disabled")
            name = t.get("name") or t.get("model") or "未命名"
            self.v_ctx_source.set(
                f"当前生效：来自任务模型「{name}」；改这里请到左侧任务模型列表")
        else:
            self._effective_from_target = False
            for w in (getattr(self, "spin_model_ctx", None),
                      getattr(self, "spin_work_window", None)):
                if w is not None:
                    w.configure(state="normal")
            ci = getattr(self, "current_index", None)
            if ci is not None and 0 <= ci < len(self.profiles):
                p = self.profiles[ci]
                self._loading_form = True
                try:
                    self.v_model_ctx.set(int(p.get("model_context") or 0))
                    self.v_work_window.set(int(
                        p.get("work_window") or WORK_WINDOW_DEFAULT))
                except (TclError, ValueError):
                    pass
                finally:
                    self._loading_form = False
            self.v_ctx_source.set(
                "当前生效：本配置；任务模型启用后会自动切到任务模型的值")
        self._update_ctx_hints()

    def _on_auto_window_toggle(self) -> None:
        """勾选/取消“自动派生”，立即写回配置并刷新绿色预览。"""
        if getattr(self, "_loading_form", False):
            return
        ci = getattr(self, "current_index", None)
        if ci is None or not (0 <= ci < len(self.profiles)):
            return
        p = self.profiles[ci]
        on = bool(self.v_auto_window.get())
        p["auto_window"] = on
        if on:
            # 自动窗口固定 85% 触发 / 45% 回落 / 关闭重压，界面跟着走。
            self.v_lazy.set(True)
            self.v_lazy_pct.set(85)
            if hasattr(self, "v_recompact"):
                self.v_recompact.set(False)
            if hasattr(self, "v_recompact_pct"):
                self.v_recompact_pct.set(90)
            p["lazy_compress"] = True
            p["lazy_trigger"] = 0.85
            p["recompact"] = False
            p["recompact_trigger"] = 0.90
        save_profiles(self.profiles)
        self._update_lazy_hint()
        self._update_ctx_hints()
        self._sync_profile_display(p)

    def _on_t_work_change(self, *_args) -> None:
        if getattr(self, "_t_ctx_setting", False):
            return
        self._update_ctx_hints()
        self._sync_effective_ctx()

    def _on_t_ctx_change(self, *_args) -> None:
        self._update_ctx_hints()
        if getattr(self, "_t_ctx_setting", False):
            return
        # 用户手填后，自动探测不再覆盖；改地址/换模型会把它复位。
        self._t_ctx_manual = True
        self._sync_effective_ctx()

    def _on_target_endpoint_change(self) -> None:
        if getattr(self, "_loading_target", False):
            return
        self._t_ctx_manual = False
        self._sync_effective_ctx()
        self._schedule_target_ctx_probe()

    def _schedule_target_ctx_probe(self, immediate: bool = False) -> None:
        if getattr(self, "current_target_index", None) is None:
            return
        job = getattr(self, "_target_probe_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._target_probe_job = self.root.after(
            50 if immediate else 900, self._auto_probe_target_ctx)

    def _auto_probe_target_ctx(self) -> None:
        self._target_probe_job = None
        try:
            base = self.v_t_url.get().strip()
            key = ascii_key(self.v_t_key.get())
            model = self.v_t_model.get().strip()
        except TclError:
            return
        if not base:
            return
        self._target_probe_gen = int(getattr(self, "_target_probe_gen", 0)) + 1
        gen = self._target_probe_gen

        def work():
            n = detect_context(base, key, model)
            if n > 0:
                self.root.after(0, lambda: self._apply_target_ctx(n, gen=gen))

        threading.Thread(target=work, daemon=True).start()

    def _apply_target_ctx(self, n: int, gen: int | None = None,
                          force: bool = False) -> None:
        if n <= 0:
            return
        if gen is not None and gen != getattr(self, "_target_probe_gen", 0):
            return
        try:
            cur = int(self.v_t_ctx.get())
        except (TclError, ValueError):
            cur = 0
        if not force and getattr(self, "_t_ctx_manual", False) and cur > 0:
            return
        prev = self._t_ctx_setting
        self._t_ctx_setting = True
        try:
            self.v_t_ctx.set(n)
        finally:
            self._t_ctx_setting = prev
        self._t_ctx_manual = False
        self._update_ctx_hints()
        self._sync_effective_ctx()
        ci = getattr(self, "current_target_index", None)
        if ci is not None and 0 <= ci < len(self.targets):
            t = self.targets[ci]
            if t.get("context") != n:
                t["context"] = n
                save_targets(self.targets)

    def _probe_engine_ctx(self) -> None:
        """本地引擎的窗口大小。探到就填进输入框并写回当前配置。"""
        idx = getattr(self, "current_index", None)
        if self._ctx_probe_running:
            return
        if self._engine_ctx:
            # 之前已经探过：切换回本地配置时也要把值填回去，不能直接 return。
            if idx is not None:
                self._apply_engine_ctx(self._engine_ctx, idx)
            return
        self._ctx_probe_running = True

        def work():
            n = 0
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:8080/health", timeout=3) as r:
                    n = int(json.loads(r.read().decode("utf-8")).get("max_context") or 0)
            except Exception:
                n = 0
            self._ctx_probe_running = False
            if n > 0:
                self.root.after(0, lambda: self._apply_engine_ctx(n, idx))

        threading.Thread(target=work, daemon=True).start()

    def _apply_engine_ctx(self, n: int, idx: int | None = None) -> None:
        self._engine_ctx = n
        # 先写回它所属的那条配置，哪怕用户已经切到别的配置。
        if idx is not None and 0 <= idx < len(self.profiles):
            p = self.profiles[idx]
            try:
                if int(p.get("model_context") or 0) <= 0:
                    p["model_context"] = n
                    save_profiles(self.profiles)
            except (TypeError, ValueError):
                pass
        if idx is None or idx != getattr(self, "current_index", None):
            return
        prev = self._loading_form
        self._loading_form = True
        try:
            if int(self.v_model_ctx.get()) <= 0:
                self.v_model_ctx.set(n)   # 自动值直接落到框里
        except (TclError, ValueError):
            pass
        finally:
            self._loading_form = prev
        self._update_ctx_hints()
        if 0 <= idx < len(self.profiles):
            self._sync_profile_display(self.profiles[idx])

    def _fill_target_ctx(self, n: int) -> None:
        """测试连通后把读到的窗口填进「上下文能力」。

        点“测试目标连接”是显式动作，测到就覆盖；后台自动探测才尊重手填值。"""
        if not hasattr(self, "v_t_ctx"):
            return
        self._apply_target_ctx(n, force=True)

    def _update_ctx_hints(self) -> None:
        """上下文一改，两个百分比后面的 token 数都要跟着变。

        可能在「综合参数计算」页还没建好时就被触发（任务模型表单先于它
        创建），所以先确认标签存在。"""
        if not hasattr(self, "lbl_recompact_hint"):
            return
        self._update_recompact_hint()
        self._update_lazy_hint()
        self._update_auto_summary()

    def _active_native_ctx(self) -> int:
        """有效“最大上下文”：任务模型优先，否则主配置/本地探测。"""
        try:
            if (hasattr(self, "v_t_ctx") and getattr(self, "current_target_index", None) is not None
                    and getattr(self, "v_t_enabled", None) is not None and self.v_t_enabled.get()):
                v = int(self.v_t_ctx.get())
                if v > 0:
                    return v
        except (TclError, ValueError, AttributeError):
            pass
        try:
            v = int(self.v_model_ctx.get())
            if v > 0:
                return v
        except (TclError, ValueError, AttributeError):
            pass
        return self._engine_ctx or 0

    def _update_auto_summary(self) -> None:
        """把“最大上下文 + 建议上下文”派生出的后续参数直接显示出来。"""
        if not hasattr(self, "v_auto_params"):
            return
        ci = getattr(self, "current_index", None)
        profiles = getattr(self, "profiles", [])
        if ci is None or not (0 <= ci < len(profiles)):
            self.v_auto_params.set("")
            return
        pr = profiles[ci]
        if not pr.get("auto_window", not pr.get("local")):
            self.v_auto_params.set("手动配置：正文目标按手动值；热层/回灌/锚点用默认值（自动派生只对云端自动窗口生效）")
            return
        native = self._active_native_ctx()
        d = derive_params(native, self._active_work_window())
        note = "（最大上下文未填，按 96K 估算）" if native <= 0 else ""
        self.v_auto_params.set(
            f"自动参数：窗口 {d['window'] // 1000}K · "
            f"涨到 {d['gate'] // 1000}K 压到 {d['compress_to'] // 1000}K · "
            f"复位 {d['reset_at'] // 1000}K · 热层 {d['hot_tokens'] // 1000}K · "
            f"回灌 {d['checkpoint_budget']} · 锚点 {d['pin_max']}{note}")

    def _token_suffix(self, pct: int) -> str:
        """百分比换成 token，并把基数和来源写在旁边 —— 否则看不出 64,000 是从哪算的。"""
        ctx, src = self._ctx_source()
        n = int(ctx * pct / 100)
        tmpl = {
            "target": CTX_TOKEN_TARGET,
            "manual": CTX_TOKEN_MANUAL,
            "engine": CTX_TOKEN_ENGINE,
            "estimate": CTX_TOKEN_EST,
        }.get(src, CTX_TOKEN_MANUAL)
        return i18n.tf(tmpl, n=n, ctx=ctx)

    def _update_recompact_hint(self) -> None:
        """开关或触发比例一变就重画备注：开与关各自的代价要写在明面上。"""
        if not hasattr(self, "lbl_recompact_hint"):
            return
        try:
            on = bool(self.v_recompact.get())
            pct = max(30, min(95, int(self.v_recompact_pct.get())))
        except (TclError, ValueError):
            return
        if hasattr(self, "lbl_recompact_tok"):
            self.lbl_recompact_tok.configure(text=self._token_suffix(pct))
        if on:
            text = i18n.tf(RECOMPACT_HINT_ON, pct=pct, tgt=int(pct * 0.85))
            if self.v_local.get():
                text += "\n" + i18n.t(LOCAL_ADVICE)
            self.lbl_recompact_hint.configure(text=text)
        else:
            self.lbl_recompact_hint.configure(text=i18n.t(RECOMPACT_HINT_OFF))

    def _update_lazy_hint(self) -> None:
        """懒压缩备注：压到哪由触发线推出来，和代理里的算法保持一致。"""
        if not hasattr(self, "lbl_lazy_hint"):
            return
        try:
            on = bool(self.v_lazy.get())
            pct = max(50, min(95, int(self.v_lazy_pct.get())))
        except (TclError, ValueError):
            return
        if hasattr(self, "lbl_lazy_tok"):
            self.lbl_lazy_tok.configure(text=self._token_suffix(pct))
        if on:
            auto = False
            ci = getattr(self, "current_index", None)
            profiles = getattr(self, "profiles", [])
            if ci is not None and 0 <= ci < len(profiles):
                pr = profiles[ci]
                auto = bool(pr.get("auto_window", not pr.get("local")))
            tgt = 45 if auto else max(30, min(60, pct - 20))
            text = i18n.tf(LAZY_HINT_ON, pct=pct, tgt=tgt)
            if self.v_local.get():
                text += "\n" + i18n.t(LOCAL_ADVICE)
            self.lbl_lazy_hint.configure(text=text)
        else:
            self.lbl_lazy_hint.configure(text=i18n.t(LAZY_HINT_OFF))

    def _profile_threshold_text(self, p: dict) -> str:
        try:
            th = int(p.get("threshold") or 0)
            tm = int(p.get("target_msg") or 0)
            ra = float(p.get("ratio") if p.get("ratio") is not None else DEFAULT_RATIO)
        except (TypeError, ValueError):
            return "—"
        if p.get("auto_window", not p.get("local")):
            try:
                ww = int(p.get("work_window") or WORK_WINDOW_DEFAULT)
            except (TypeError, ValueError):
                ww = WORK_WINDOW_DEFAULT
            return f"{th:,} / 自动 {ww // 1000}K 窗口（85% 触发，压到 45%）"
        if self._ratio_is_dynamic():
            lo, hi = ratio_band(ra)
            band = f"塌缩固定 {hi:.2f}" if lo >= hi else f"{lo:.2f}~{hi:.2f}"
            return f"{th:,} / 目标正文 {tm:,} / 动态压缩比 {ra:.2f}（{band}）"
        return f"{th:,} / 目标正文 {tm:,} / 压缩比 {ra:.2f}"

    def _sync_profile_display(self, p: dict) -> None:
        self.v_profile_threshold.set(self._profile_threshold_text(p))
        if self.current_index is not None:
            self.tree.item(str(self.current_index), values=self._row_values(p))
        if hasattr(self, "price_comp"):
            self.price_comp.show_model(
                p["pricing"], p.get("name") or p.get("model") or "未命名")

    def _load_profile_form(self, i: int) -> None:
        self.current_index = i
        self._loading_form = True
        p = self.profiles[i]
        self.v_name.set(p["name"])
        self.v_url.set(p["base_url"])
        self.v_key.set(p["api_key"])
        self.v_model.set(p["model"])
        self.v_price_url.set(p.get("pricing_url") or "")
        self.v_conc.set(int(p.get("concurrency") or 4))
        self.v_extra.set(p.get("extra_json") or "")
        self.v_local.set(bool(p.get("local")))
        if hasattr(self, "v_task_mode"):
            key = p.get("task_mode") if p.get("task_mode") in TASK_MODES else "auto"
            self._task_mode_key = key
            self.v_task_mode.set(i18n.t(TASK_MODES[key][0]))
            self._on_task_mode_change()
        if hasattr(self, "v_recompact"):
            self.v_recompact.set(bool(p.get("recompact", True)))
            try:
                pct = int(round(float(
                    p.get("recompact_trigger", RECOMPACT_DEFAULT_TRIGGER)) * 100))
            except (TypeError, ValueError):
                pct = int(RECOMPACT_DEFAULT_TRIGGER * 100)
            self.v_recompact_pct.set(max(30, min(95, pct)))
            self._update_recompact_hint()
        if hasattr(self, "v_lazy"):
            self.v_lazy.set(bool(p.get("lazy_compress", False)))
            try:
                lpct = int(round(float(
                    p.get("lazy_trigger", LAZY_DEFAULT_TRIGGER)) * 100))
            except (TypeError, ValueError):
                lpct = int(LAZY_DEFAULT_TRIGGER * 100)
            self.v_lazy_pct.set(max(50, min(95, lpct)))
            # 自动窗口下实际策略是“85% 触发、45% 回落、关闭重压”，
            # 界面跟着显示真实生效的值，避免勾选状态和运行行为对不上。
            if p.get("auto_window", not p.get("local")):
                self.v_lazy.set(True)
                self.v_lazy_pct.set(85)
                if hasattr(self, "v_recompact"):
                    self.v_recompact.set(False)
                if hasattr(self, "v_recompact_pct"):
                    self.v_recompact_pct.set(90)
            self._update_lazy_hint()
        if hasattr(self, "v_model_ctx"):
            try:
                self.v_model_ctx.set(int(p.get("model_context") or 0))
            except (TclError, ValueError):
                self.v_model_ctx.set(0)
            try:
                self.v_work_window.set(int(
                    p.get("work_window") or WORK_WINDOW_DEFAULT))
            except (TclError, ValueError):
                self.v_work_window.set(WORK_WINDOW_DEFAULT)
            if hasattr(self, "v_auto_window"):
                self.v_auto_window.set(bool(
                    p.get("auto_window", not p.get("local"))))
            if hasattr(self, "v_dump"):
                self.v_dump.set(bool(p.get("recall_dump")))
            if p.get("local"):
                self._probe_engine_ctx()   # 本地引擎在线时自动填真实窗口
            self._update_ctx_hints()
        if hasattr(self, "v_profile_threshold"):
            self.v_profile_threshold.set(self._profile_threshold_text(p))
        if hasattr(self, "price_comp"):
            self.price_comp.show_model(
                p["pricing"], p.get("name") or p.get("model") or "未命名")
        self._loading_form = False
        self._sync_effective_ctx()

    def _refresh_task_mode_lang(self) -> None:
        """切换语言后重画任务模式：下拉项、当前值、说明文字一起换。"""
        if not hasattr(self, "cb_task_mode"):
            return
        self.cb_task_mode.configure(
            values=[i18n.t(TASK_MODES[k][0]) for k in TASK_MODES])
        key = getattr(self, "_task_mode_key", "auto")
        self.v_task_mode.set(i18n.t(TASK_MODES[key][0]))
        if hasattr(self, "lbl_task_hint"):
            self.lbl_task_hint.configure(text=i18n.t(TASK_MODES[key][1]))

    def _on_task_mode_change(self, _evt=None) -> None:
        """下拉框旁边常驻一句「这个模式适合什么任务」。

        用户手动换档时，正在跑的代理立即停掉并把「启动代理」按钮点亮：
        任务模式只在启动时注入，不重启就还是旧档，必须强制重来一次。
        程序自己加载配置时（_evt 为 None）不触发停止。
        """
        display = self.v_task_mode.get()
        val = next((k for k in TASK_MODES if i18n.t(TASK_MODES[k][0]) == display),
                   TASK_MODE_BY_LABEL.get(display, "auto"))
        self._task_mode_key = val
        if hasattr(self, "lbl_task_hint"):
            self.lbl_task_hint.configure(text=i18n.t(TASK_MODES[val][1]))
        if _evt is None:
            return
        running = getattr(self, "_running_task_mode", None)
        if self.proc is not None and running is not None and running != val:
            self._say(i18n.tf(
                "任务模式切换为「{name}」——已停止代理，请重新点击「启动代理」使新档生效。",
                name=i18n.t(TASK_MODES[val][0])), "hint")
            self._stop()
            self._running_task_mode = None

    def _refresh_list(self, select: int | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.profiles):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(p))
        if self.profiles:
            idx = 0 if select is None else max(0, min(select, len(self.profiles) - 1))
            self.tree.selection_set(str(idx))
            self.tree.focus(str(idx))
            # 构建界面时控件还没映射，<<TreeviewSelect>> 不一定触发；
            # 这里直接加载，保证 current_index 和表单不会停在空状态。
            self._load_profile_form(idx)

    def _on_select(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        if not (0 <= i < len(self.profiles)):
            return
        self._commit_form()
        self._load_profile_form(i)

    def _commit_form(self) -> None:
        if self.current_index is None or not (0 <= self.current_index < len(self.profiles)):
            return
        p = self.profiles[self.current_index]
        p["name"] = self.v_name.get().strip() or p["name"]
        p["base_url"] = self.v_url.get().strip()
        p["api_key"] = self.v_key.get().strip()
        p["model"] = self.v_model.get().strip()
        p["pricing_url"] = self.v_price_url.get().strip()
        try:
            p["concurrency"] = max(1, min(64, int(self.v_conc.get())))
        except (TclError, ValueError):
            p["concurrency"] = 4
        p["extra_json"] = self.v_extra.get().strip()
        p["local"] = bool(self.v_local.get())
        if hasattr(self, "v_task_mode"):
            p["task_mode"] = getattr(self, "_task_mode_key", "auto")
        if hasattr(self, "v_recompact"):
            p["recompact"] = bool(self.v_recompact.get())
            try:
                p["recompact_trigger"] = max(
                    0.30, min(0.95, int(self.v_recompact_pct.get()) / 100))
            except (TclError, ValueError):
                p["recompact_trigger"] = RECOMPACT_DEFAULT_TRIGGER
        if hasattr(self, "v_lazy"):
            p["lazy_compress"] = bool(self.v_lazy.get())
            try:
                p["lazy_trigger"] = max(
                    0.50, min(0.95, int(self.v_lazy_pct.get()) / 100))
            except (TclError, ValueError):
                p["lazy_trigger"] = LAZY_DEFAULT_TRIGGER
        if hasattr(self, "v_model_ctx"):
            try:
                p["model_context"] = max(
                    0, min(2_000_000, int(self.v_model_ctx.get())))
            except (TclError, ValueError):
                p["model_context"] = 0
        if hasattr(self, "v_work_window"):
            try:
                p["work_window"] = max(WORK_WINDOW_MIN, min(
                    WORK_WINDOW_MAX, int(self.v_work_window.get())))
            except (TclError, ValueError):
                p["work_window"] = WORK_WINDOW_DEFAULT
        if hasattr(self, "v_auto_window"):
            p["auto_window"] = bool(self.v_auto_window.get())
        if hasattr(self, "v_dump"):
            p["recall_dump"] = bool(self.v_dump.get())
        self.tree.item(str(self.current_index), values=self._row_values(p))
        if hasattr(self, "price_comp"):
            self.price_comp.show_model(
                p["pricing"], p.get("name") or p.get("model") or "未命名")

    # ---- 任务模型列表
    @staticmethod
    def _target_row_values(t: dict) -> tuple:
        return (t.get("name") or "(未命名)",
                "是" if t.get("enabled") else "",
                t.get("model") or "(空)",
                t.get("base_url") or "(空)")

    def _clear_target_form(self) -> None:
        self._loading_target = True
        self._t_ctx_setting = True
        try:
            self.v_t_enabled.set(False)
            self.v_t_name.set("")
            self.v_t_url.set("")
            self.v_t_key.set("")
            self.v_t_model.set("")
            self.v_t_extra.set("")
            self.v_t_max_think.set(0)
            self.v_t_ctx.set(0)
            self.v_t_work.set(WORK_WINDOW_DEFAULT)
        finally:
            self._t_ctx_setting = False
            self._loading_target = False
        self._t_ctx_manual = False

    def _load_target_form(self, idx: int) -> None:
        self.current_target_index = idx
        self._loading_target = True
        self._t_ctx_setting = True
        t = self.targets[idx]
        try:
            self.v_t_enabled.set(bool(t.get("enabled")))
            self.v_t_name.set(t.get("name") or "")
            self.v_t_url.set(t.get("base_url") or "")
            self.v_t_key.set(t.get("api_key") or "")
            self.v_t_model.set(t.get("model") or "")
            self.v_t_extra.set(t.get("extra_json") or "")
            try:
                self.v_t_max_think.set(max(0, int(t.get("max_think") or 0)))
            except (TclError, ValueError):
                self.v_t_max_think.set(0)
            try:
                self.v_t_ctx.set(max(0, min(2_000_000, int(t.get("context") or 0))))
            except (TclError, ValueError):
                self.v_t_ctx.set(0)
            try:
                self.v_t_work.set(int(t.get("work_window") or WORK_WINDOW_DEFAULT))
            except (TclError, ValueError):
                self.v_t_work.set(WORK_WINDOW_DEFAULT)
        finally:
            self._t_ctx_setting = False
            self._loading_target = False
        # 刚加载的已存值不算“用户手填”，允许自动探测刷新成当前服务的真实值。
        self._t_ctx_manual = False
        if hasattr(self, "v_model_ctx"):
            self._update_ctx_hints()
        if hasattr(self, "price_target"):
            self.price_target.show_model(
                t["pricing"], t.get("name") or t.get("model") or "未命名")
        # 选中任务模型后自动探一次，不必再点“测试目标连接”。
        self._sync_effective_ctx()
        self._schedule_target_ctx_probe(immediate=True)

    def _refresh_target_list(self, select: int | None = None) -> None:
        self.tree_target.delete(*self.tree_target.get_children())
        for i, t in enumerate(self.targets):
            self.tree_target.insert("", "end", iid=str(i),
                                    values=self._target_row_values(t))
        if not self.targets:
            self.current_target_index = None
            self._clear_target_form()
            return
        idx = 0 if select is None else max(0, min(select, len(self.targets) - 1))
        self.tree_target.selection_set(str(idx))
        self.tree_target.focus(str(idx))
        self._load_target_form(idx)

    def _on_target_select(self, _evt=None) -> None:
        sel = self.tree_target.selection()
        if not sel:
            return
        i = int(sel[0])
        if not (0 <= i < len(self.targets)):
            return
        prev = self.current_target_index   # 3.21：初次建台的程序化选中不问，只问真换行
        self._commit_target_form()
        self._load_target_form(i)
        if prev is not None and prev != i:
            self._ask_baseline_binding(self.targets[i].get("model") or "")

    def _commit_target_form(self) -> None:
        if (self.current_target_index is None
                or not (0 <= self.current_target_index < len(self.targets))):
            return
        t = self.targets[self.current_target_index]
        t["enabled"] = bool(self.v_t_enabled.get())
        t["name"] = self.v_t_name.get().strip() or t.get("name") or "任务模型"
        t["base_url"] = self.v_t_url.get().strip()
        t["api_key"] = self.v_t_key.get().strip()
        t["model"] = self.v_t_model.get().strip()
        t["extra_json"] = self.v_t_extra.get().strip()
        try:
            t["max_think"] = max(0, int(self.v_t_max_think.get()))
        except (TclError, ValueError):
            t["max_think"] = 0
        try:
            t["context"] = max(0, min(2_000_000, int(self.v_t_ctx.get())))
        except (TclError, ValueError):
            t["context"] = 0
        try:
            t["work_window"] = max(WORK_WINDOW_MIN, min(
                WORK_WINDOW_MAX, int(self.v_t_work.get())))
        except (TclError, ValueError):
            t["work_window"] = WORK_WINDOW_DEFAULT
        self.tree_target.item(str(self.current_target_index),
                              values=self._target_row_values(t))
        if hasattr(self, "v_model_ctx"):
            self._update_ctx_hints()   # 任务模型窗口变了，百分比换算要跟着变
        if hasattr(self, "price_target"):
            self.price_target.show_model(
                t["pricing"], t.get("name") or t.get("model") or "未命名")

    def _kpi_time_range(self) -> None:
        """4.1.0（用户定案）：「线图时间范围」——弹窗选绿线依据哪段时间的数据。"""
        win = Toplevel(self.root)
        win.title("线图时间范围")
        win.transient(self.root)
        cur = getattr(self, "kpi_win_sec", None)
        var = StringVar(value=_kpi_range_label(cur))
        ttk.Label(win, justify="left",
                  text="绿线依据的时间段（灰线基线不受影响）：").pack(
            anchor="w", padx=12, pady=(10, 4))
        for lbl, _sec in KPI_RANGE_PRESETS:
            ttk.Radiobutton(win, text=lbl, value=lbl, variable=var).pack(
                anchor="w", padx=20)
        filt = getattr(self, "kpi_conv", None)
        clear_var = BooleanVar(value=False)
        if filt:
            ttk.Checkbutton(win, text=f"清除会话过滤（当前：{filt}）",
                            variable=clear_var).pack(anchor="w", padx=12,
                                                     pady=(8, 0))

        def _ok():
            for lbl, sec in KPI_RANGE_PRESETS:
                if lbl == var.get():
                    self.kpi_win_sec = sec
                    break
            if filt and clear_var.get():
                self.kpi_conv = None
            win.destroy()
            self._update_kpi()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="确定", command=_ok).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(
            side="left", padx=6)

    def _kpi_model_pick(self) -> None:
        """4.1.0（用户定案）：「调取线图」——弹窗选用哪款模型的历史数据
        生成绿线走势。基线下拉只管灰线，两者互不影响。"""
        p = proxy_dir() / "kpi.jsonl"
        if not p.exists():
            messagebox.showinfo("调取线图", "还没有 kpi.jsonl，代理跑起来再说。")
            return
        rows = []
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
            try:
                rows.append(json.loads(ln))
            except ValueError:
                pass
        rows = _kpi_filter_rows(rows, getattr(self, "kpi_conv", None))
        models = _kpi_models_in(rows)
        if not models:
            messagebox.showinfo("调取线图", "图上没有数据。")
            return
        cur_mdl = kpi_model_of(rows[-1])
        win = Toplevel(self.root)
        win.title("调取线图：绿线数据来源")
        win.transient(self.root)
        var = StringVar(value=getattr(self, "kpi_model_view", None) or "")
        ttk.Label(win, justify="left",
                  text="用哪款模型的历史数据生成绿线走势"
                       "（灰线仍由「基线」下拉决定）：").pack(
            anchor="w", padx=12, pady=(10, 4))
        ttk.Radiobutton(win, text=f"当前档（跟随实际运行：{cur_mdl}）",
                        value="", variable=var).pack(anchor="w", padx=20)
        for m, c in models:
            ttk.Radiobutton(win, text=f"{m}（{c} 轮）", value=m,
                            variable=var).pack(anchor="w", padx=20)

        def _ok():
            self.kpi_model_view = var.get() or None
            win.destroy()
            self._update_kpi()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="确定", command=_ok).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(
            side="left", padx=6)

    def _kpi_base_select(self, _e=None) -> None:
        """3.23：下拉给当前模型挑基线（用户定案：不同模型要用自己的线，
        换模型弹窗只问一次，反悔在这改）。选"自成一组"=写 model→model；
        选某组名=并入该组共用它的灰线（共用只当参照的 3.20 语义不变）。"""
        try:
            mdl = getattr(self, "kpi_mdl", None)
            sel = self.cb_kpi_base.get()
            if not mdl or not sel:
                return
            led = load_baselines()
            umap = led.get("model_group")
            umap = dict(umap) if isinstance(umap, dict) else {}
            umap[mdl] = sel
            led["model_group"] = umap
            try:
                save_baselines(led)
            except OSError:
                pass
            if getattr(self, "_base_asked", None) is None:
                self._base_asked = set()
            self._base_asked.add(mdl)   # 手选过=换模型弹窗不再打扰
            self._update_kpi()
        except Exception:
            pass

    def _ask_baseline_binding(self, model: str) -> None:
        """3.21：任务模型列表换模型 → 问这条模型用哪条基线（用户定案）。
        只在"这条模型没挑过、它自己组在台账里没线、且台账有别线可挑"时打扰。
        共用=只当参照、不给本档出降档建议（3.20 ref_only 语义不变）；
        自选成组写 model→model，同表记下"已问过"。映射存 baselines.json 的
        model_group 键，事后可手改。"""
        model = (model or "").strip()
        if not model or model == BASELINE_NO_MODEL:
            return
        if getattr(self, "_base_asked", None) is None:
            self._base_asked = set()
        if model in self._base_asked:
            return
        self._base_asked.add(model)
        led = load_baselines()
        umap = led.get("model_group")
        umap = dict(umap) if isinstance(umap, dict) else {}
        if model in umap:
            return
        if baseline_group(model, umap) in led:
            return   # 自己组已有线，没什么可挑
        cands = sorted((g, e) for g, e in led.items()
                       if isinstance(e, dict) and e.get("base") is not None)
        if not cands:
            return   # 没线可共用，安静自成组
        win = Toplevel(self.root)
        win.title("换了模型，用哪条基线？")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, justify="left", text=(
            f"任务模型换成了「{model}」，台账里还没有它的线。\n"
            "共用=只当参照、不给本档出降档建议；\n"
            "自成=攒够 20 合格轮再画灰线。")).pack(
            anchor="w", padx=12, pady=(10, 6))
        var = StringVar(value="")
        for g, e in cands:
            ttk.Radiobutton(win, text=(
                f"共用「{g}」（{e.get('metric')}·"
                f"{float(e['base']) * 100:.0f}%·n={e.get('n')}·"
                f"{e.get('src') or 'manual'}）"),
                value=g, variable=var).pack(anchor="w", padx=24)
        ttk.Radiobutton(win, text="先不共用——自己攒",
                        value="", variable=var).pack(anchor="w", padx=24,
                                                    pady=(0, 8))

        def _ok() -> None:
            led["model_group"] = dict(umap, **{model: var.get() or model})
            try:
                save_baselines(led)
            except OSError:
                pass
            win.destroy()
            self._update_kpi()
        ttk.Button(win, text="好", command=_ok).pack(pady=(0, 10))
        win.grab_set()

    def _selected_target(self) -> dict:
        if (self.current_target_index is None
                or not (0 <= self.current_target_index < len(self.targets))):
            return new_target()
        return self.targets[self.current_target_index]

    def _target_add(self) -> None:
        self._commit_target_form()
        t = dict(new_target(), name="新任务模型")
        t["pricing"] = self._new_target_pricing()
        self.targets.append(t)
        save_targets(self.targets)
        self._refresh_target_list(select=len(self.targets) - 1)

    def _target_duplicate(self) -> None:
        if self.current_target_index is None:
            return
        self._commit_target_form()
        t = json.loads(json.dumps(self.targets[self.current_target_index],
                                  ensure_ascii=False))
        t["name"] = (t.get("name") or "任务模型") + " 副本"
        self.targets.append(t)
        save_targets(self.targets)
        self._refresh_target_list(select=len(self.targets) - 1)

    def _target_delete(self) -> None:
        # 以列表里实际选中的行为准，不依赖可能尚未刷新的 current_target_index。
        sel = self.tree_target.selection()
        if not sel:
            messagebox.showinfo("删除", "先在「任务模型列表」里选一条。")
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self.targets)):
            return
        name = self.targets[idx].get("name") or "这条目标模型"
        if not messagebox.askyesno("删除", f"删除“{name}”？"):
            return
        del self.targets[idx]
        self.current_target_index = None
        save_targets(self.targets)
        self._refresh_target_list(select=min(idx, len(self.targets) - 1))

    def _target_set_default(self) -> None:
        """把选中的任务模型设为唯一启用项（默认走它答题）。"""
        sel = self.tree_target.selection()
        if not sel:
            messagebox.showinfo("设为默认", "先在「任务模型列表」里选一条。")
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self.targets)):
            return
        # 先提交表单，避免正在编辑的内容丢了
        self._commit_target_form()
        # 默认模型固定在第一行：从当前位置挪到 0，并持久化顺序
        selected = self.targets.pop(idx)
        self.targets.insert(0, selected)
        for i, t in enumerate(self.targets):
            t["enabled"] = (i == 0)
        self.current_target_index = 0
        save_targets(self.targets)
        self._refresh_target_list(select=0)
        name = (selected.get("name")
                or selected.get("model") or "未命名")
        if self.proc is not None and self.proc.poll() is None:
            self._stop()
            self._say(f"已把「{name}」设为默认任务模型，并停止代理；"
                      f"重新启动后生效。", "hint")
        else:
            self._say(f"已把「{name}」设为默认任务模型；"
                      f"下次启动代理生效。", "hint")

    def _promote_enabled_target(self) -> None:
        """把启用的任务模型挪到第一行，保存后顺序固定。"""
        idx = next((i for i, t in enumerate(self.targets) if t.get("enabled")), None)
        if idx and 0 < idx < len(self.targets):
            self.targets.insert(0, self.targets.pop(idx))
            self.current_target_index = 0

    def _save_targets(self) -> None:
        self._commit_target_form()
        self._promote_enabled_target()
        save_targets(self.targets)
        self._refresh_target_list(select=self.current_target_index or 0)
        self._say("任务模型列表已保存", "ok")
        if self.current_target_index is not None:
            # 3.21：改完模型名再点"保存列表"也算换模型——同样问一次基线
            self._ask_baseline_binding(
                self.targets[self.current_target_index].get("model") or "")

    # ---- 按钮
    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.v_show_key.get() else "•")

    def _toggle_t_key(self) -> None:
        self.t_key_entry.configure(show="" if self.v_t_show.get() else "•")

    def _on_local_toggle(self) -> None:
        if self.v_local.get():
            self.v_conc.set(1)
            self._probe_engine_ctx()
        # 本地/云端的建议值不同，勾选一变就重画备注
        if hasattr(self, "v_model_ctx"):
            self._update_ctx_hints()

    def _add(self) -> None:
        self._commit_form()
        self.profiles.append({"name": "新配置", "base_url": "https://", "api_key": "",
                              "model": "", "concurrency": 4, "extra_json": "",
                              "local": False, "target": new_target(),
                              "pricing": self._new_comp_pricing(),
                              "threshold": DEFAULT_THRESHOLD,
                              "target_msg": DEFAULT_THRESHOLD,
                              "auto_window": True,
                              "work_window": WORK_WINDOW_DEFAULT,
                              "ratio": DEFAULT_RATIO})
        save_profiles(self.profiles)
        self._refresh_list(select=len(self.profiles) - 1)

    def _duplicate(self) -> None:
        if self.current_index is None:
            return
        self._commit_form()
        # 深拷贝：target 是嵌套 dict，浅拷贝会让两条配置共用同一份
        p = json.loads(json.dumps(self.profiles[self.current_index], ensure_ascii=False))
        p["name"] = p["name"] + " 副本"
        self.profiles.append(p)
        save_profiles(self.profiles)
        self._refresh_list(select=len(self.profiles) - 1)

    def _delete(self) -> None:
        # 以列表里实际选中的行为准，不依赖可能尚未刷新的 current_index。
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("删除", "先在「压缩模型列表」里选一条。")
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self.profiles)):
            return
        if len(self.profiles) <= 1:
            messagebox.showinfo("删除", "至少要保留一条压缩模型。")
            return
        if not messagebox.askyesno("删除", f"删除“{self.profiles[idx]['name']}”？"):
            return
        del self.profiles[idx]
        self.current_index = None
        save_profiles(self.profiles)
        self._refresh_list(select=min(idx, len(self.profiles) - 1))

    def _save(self) -> None:
        self._commit_form()
        self._commit_target_form()
        save_profiles(self.profiles)
        save_targets(self.targets)
        self._refresh_list(select=self.current_index or 0)
        self._refresh_target_list(select=self.current_target_index or 0)
        self._say("配置已保存（压缩模型 + 任务模型）", "ok")

    def _set_default(self) -> None:
        if self.current_index is None:
            return
        self._commit_form()
        p = self.profiles.pop(self.current_index)
        self.profiles.insert(0, p)
        save_profiles(self.profiles)
        self._refresh_list(select=0)
        self._say(f"“{p['name']}”已设为默认", "ok")

    # ---- 测试连接
    def _test_conn(self) -> None:
        self._commit_form()
        p = self.profiles[self.current_index] if self.current_index is not None else None
        if not p:
            return
        if p.get("local"):
            self._say("本地模式：检查 127.0.0.1:8080 ...")
            ok = port_busy(8080)
            self._say("8080 可连接" if ok else "8080 未监听（本地引擎没开）",
                      "ok" if ok else "err")
            return
        if not p["base_url"] or not p["api_key"]:
            messagebox.showwarning("测试连接", "API 链接和 API Key 都要填。")
            return

        def work():
            url = chat_urls(p["base_url"])[0]
            body = {
                "model": p["model"],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
                "stream": False,
            }
            try:
                body.update(json.loads(p["extra_json"]) if p["extra_json"] else {})
            except Exception:
                pass
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {ascii_key(p['api_key'])}"})
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read()) or {}
                dt = time.time() - t0
                txt = _reply_text(data)
                self.log_q.put(("ok", f"[测试] 连接成功 {dt:.1f}s  "
                                      f"模型={data.get('model')}  回复={txt[:40]!r}\n"))
            except Exception as exc:
                detail = ""
                if hasattr(exc, "read"):
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                self.log_q.put(("err", f"[测试] 失败: {exc} {detail}\n"))

        threading.Thread(target=work, daemon=True).start()

    # ---- 获取价目：只读公开网页，不调用推理接口、不消耗 token
    def _fetch_comp_pricing(self) -> None:
        model = self.v_model.get().strip()
        kind, url = price_source.resolve_price_source(
            self.v_url.get(), self.v_price_url.get())
        if not kind:
            messagebox.showinfo(
                "获取价目",
                "请在「价目链接」填入支持的价格页：\n"
                "  DeepSeek 官方  https://api-docs.deepseek.com/zh-cn/quick_start/pricing\n"
                "  百炼        https://help.aliyun.com/zh/model-studio/model-pricing\n"
                "留空时会按 API 链接自动识别（api.deepseek.com / aliyuncs.com）。\n"
                "其他服务商请按账单在「成本建议 → 压缩模型价目」手动填。")
            return
        if not model:
            model = ("deepseek-flash" if kind == "deepseek"
                     else "deepseek-v4.1-flash")
        label = "DeepSeek 官方" if kind == "deepseek" else "百炼"
        token_plan = "token-plan" in self.v_url.get().lower()
        self._say(f"[价目] 正在读取{label}价目（{url}；网页请求，"
                  "不消耗 token）…\n")

        def work():
            try:
                if kind == "deepseek":
                    data = price_source.fetch_deepseek_pricing(model, url)
                else:
                    data = price_source.fetch_bailian_pricing(
                        model, url, token_plan=token_plan)
                self.price_q.put({"ok": True, "data": data})
            except Exception as exc:
                self.price_q.put({"ok": False, "error": str(exc)})

        threading.Thread(target=work, daemon=True).start()

    def _apply_price_fetch(self, res: dict) -> None:
        if not res.get("ok"):
            self._say(f"[价目] 获取失败：{res.get('error')}\n", "err")
            messagebox.showwarning(
                "获取价目",
                "读取官方价目失败，可能是网络或页面改版。\n"
                "可以在「成本建议 → 压缩模型价目」手动填，不影响其他功能。")
            return
        data = res["data"]
        comp = self.price_comp.model
        comp["name"] = data["name"]
        comp["unit"] = data.get("unit", "元")
        comp["windows"] = data["windows"]
        self.price_comp.refresh()
        self._say(f"[价目] 已填入「压缩模型价目」：{len(data['windows'])} 个时段"
                  f"（{data['unit']}/百万 token）\n", "ok")
        for w in data["windows"]:
            self._say(f"        {w['start']}-{w['end']}  "
                      f"输入 {w['input']:g}  输出 {w['output']:g}  "
                      f"命中 {w['cache_read']:g}\n")
        self._say(f"[价目] {data['note']}\n", "hint")
        self._sync_local_threshold()
        if self.advice_text.get("1.0", "end").strip():
            self._compute_advice()
        else:
            self._save_pricing()
        if "本地模型时间账" in self.local_text.get("1.0", "end"):
            self._compute_local()

    # ---- 测速：走真实 API，测压缩模型的预填充 / 生成速度
    BENCH_PROMPT_TOKENS = 12000
    BENCH_MAX_TOKENS = 256

    def _bench_tokens(self) -> int:
        """测速长度跟随「分段长度」，因为代理就是按这个长度切段压缩。"""
        try:
            chunk = int(self.v_c_chunk.get())
        except (TclError, ValueError):
            chunk = self.BENCH_PROMPT_TOKENS
        return max(2000, min(32000, chunk))

    @staticmethod
    def _bench_estimate_tokens(text: str) -> int:
        """粗略估 token：中文按 1 字 1 token，其余按 2 字符 1 token。"""
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        return max(1, int(round(cjk + (len(text) - cjk) / 2.0)))

    @classmethod
    def _bench_prompt(cls, target_tokens: int = 4000) -> str:
        """生成约 target_tokens 个 token 的中文填充，末尾要求持续输出。"""
        head = ("下面是一段用于测量预填充速度的填充材料。请先完整读完，"
                "然后从 1 数到 200，每行一个数字，不要解释，不要省略。\n\n")
        line = "填充段落 {i}：长上下文压缩测试材料，内容没有实际意义，只用于占用 token。\n"
        parts = [head]
        n, i = len(head), 0
        # 实测中文占比约 0.83 token/字符，按 1.2 倍字符数生成才接近目标 token
        while n < target_tokens * 1.2:
            s = line.format(i=i)
            parts.append(s)
            n += len(s)
            i += 1
        return "".join(parts)

    def _bench_conn(self) -> None:
        self._commit_form()
        p = self.profiles[self.current_index] if self.current_index is not None else None
        if not p:
            return
        if self._bench_busy:
            messagebox.showinfo(i18n.t("测速"),
                                i18n.t("上一轮测速还没结束，请稍等。"))
            return

        if p.get("local"):
            url = "http://127.0.0.1:8080/v1/chat/completions"
            key = p.get("api_key") or ""
            model = p.get("model") or "local-model"
            extra = p.get("extra_json") or ""
            if not port_busy(8080):
                messagebox.showwarning(
                    i18n.t("本地引擎没开"),
                    i18n.t("127.0.0.1:8080 没有监听，测速请求会被拒绝。\n\n"
                           "先把本地引擎启动（默认 127.0.0.1:8080）；"
                           "如果要测云端压缩模型，先在左侧列表选中那条云端配置，"
                           "再点「测试连接 → 测速」。"))
                return
        else:
            if not p["base_url"] or not p["api_key"]:
                messagebox.showwarning("测速", "API 链接和 API Key 都要填。")
                return
            url = chat_urls(p["base_url"])[0]
            key, model, extra = ascii_key(p["api_key"]), p["model"], p["extra_json"]

        prompt_tokens = self._bench_tokens()
        default_lanes = 1 if p.get("local") else int(p.get("concurrency") or 16)
        lanes = simpledialog.askinteger(
            i18n.t("测速并发"),
            i18n.tf("并发路数（1-64）：\n每路都会发一条约 {n} token 的请求"
                    "（取分段长度，最多 32,000）。", n=f"{prompt_tokens:,}"),
            initialvalue=default_lanes, minvalue=1, maxvalue=64,
            parent=self.root)
        if not lanes:
            return

        in_tokens = lanes * prompt_tokens
        out_tokens = lanes * self.BENCH_MAX_TOKENS
        now = time.localtime()
        win = price_mod.window_for(self.price_comp.model,
                                   now.tm_hour * 60 + now.tm_min)
        if p.get("local"):
            cost_line = i18n.t("本地模型不计 token 费，但会占用本机算力。")
        elif win:
            try:
                est = (in_tokens * float(win.get("input", 0) or 0)
                       + out_tokens * float(win.get("output", 0) or 0)) / 1e6
                cost_line = i18n.tf(
                    "按当前时段压缩价估算约 {yuan} 元"
                    "（输入 {i} / 输出上限 {o} token）。",
                    yuan=f"{est:.4f}", i=f"{in_tokens:,}", o=f"{out_tokens:,}")
            except (TypeError, ValueError):
                cost_line = i18n.t("价目表当前时段的数字填得不对，金额未知。")
        else:
            cost_line = i18n.t("价目表当前时段没填压缩价，金额未知。")

        if p.get("local"):
            charge_line = i18n.t("这会真实调用本地引擎，占用本机算力，不产生 token 费。")
            title = i18n.t("测速会占用本机算力")
        else:
            charge_line = i18n.t("这会真实调用 API，从预充值余额扣费；"
                                 "实际金额以服务商账单为准。")
            title = i18n.t("测速会消耗预充值额度")
        msg = i18n.tf(
            "目标配置：{name}\n{url}\n\n"
            "将按 {lanes} 路并发，各发一条约 {n} token 的请求"
            "（取分段长度，最多 32,000），并要求生成最多 {maxout} token。\n\n"
            "{cost}\n\n{charge}\n"
            "结果受网络、排队、限流影响，只作初步参考。\n\n继续？",
            name=p["name"], url=url, lanes=lanes, n=f"{prompt_tokens:,}",
            maxout=self.BENCH_MAX_TOKENS, cost=cost_line, charge=charge_line)
        if not messagebox.askyesno(title, msg, icon="warning", parent=self.root):
            return

        self._bench_busy = True
        self.btn_test.configure(state="disabled")
        self._say(f"[测速] 目标：{p['name']} → {url}")
        self._say(f"[测速] 开始：{lanes} 路并发 × 约 "
                  f"{prompt_tokens:,} token 输入 ...")
        prompt = self._bench_prompt(prompt_tokens)

        def work():
            t_all0 = time.perf_counter()
            results: list[dict] = []
            try:
                with ThreadPoolExecutor(max_workers=lanes) as pool:
                    futs = [
                        pool.submit(self._bench_one, url, key, model, extra,
                                    prompt, self.BENCH_MAX_TOKENS)
                        for _ in range(lanes)
                    ]
                    for fut in as_completed(futs):
                        try:
                            results.append(fut.result())
                        except Exception as exc:  # noqa: BLE001
                            results.append({"ok": False, "error": str(exc)})
                wall = time.perf_counter() - t_all0
                summary = self._bench_summary(results, lanes, wall)
                summary["target"] = url
                summary["profile"] = p["name"]
                self.bench_q.put(summary)
            except Exception as exc:  # noqa: BLE001
                self.bench_q.put({"ok": False, "lanes": lanes,
                                  "target": url, "profile": p["name"],
                                  "error": f"{type(exc).__name__}: {exc}"})

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _bench_one(url: str, key: str, model: str, extra_json: str,
                   prompt: str, max_tokens: int = 256,
                   timeout: float = 180.0) -> dict:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            body.update(json.loads(extra_json) if extra_json else {})
        except Exception:
            pass
        body["stream"] = True
        body["max_tokens"] = max_tokens
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers)

        opener = urllib.request.urlopen
        if url.startswith(("http://127.0.0.1", "http://localhost",
                           "http://[::1]")):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open

        t0 = time.perf_counter()
        t_first: float | None = None
        t_last = t0
        out_chars = 0
        usage: dict = {}
        try:
            with opener(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise RuntimeError(str(obj["error"])[:300])
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = (delta.get("content")
                                 or delta.get("reasoning_content") or "")
                        if piece:
                            now = time.perf_counter()
                            if t_first is None:
                                t_first = now
                            t_last = now
                            out_chars += len(piece)
        except Exception as exc:  # noqa: BLE001
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
            return {"ok": False, "error": f"{exc} {detail}".strip(),
                    "t0": t0, "t_end": time.perf_counter()}

        t_end = time.perf_counter()
        if t_first is None:
            return {"ok": False,
                    "error": "没有收到流式内容（服务端可能不支持 stream）",
                    "t0": t0, "t_end": t_end}

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        estimated = not (prompt_tokens and completion_tokens)
        if not prompt_tokens:
            prompt_tokens = App._bench_estimate_tokens(prompt)
        if not completion_tokens:
            completion_tokens = max(1, int(round(out_chars / 2.0)))
        ttft = max(0.001, t_first - t0)
        gen_span = t_last - t_first
        decode = (max(1, completion_tokens - 1) / gen_span
                  if gen_span > 0.001 else 0.0)
        return {
            "ok": True, "ttft": ttft, "prefill": prompt_tokens / ttft,
            "decode": decode, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated": estimated, "t0": t0, "t_end": t_end,
        }

    @staticmethod
    def _bench_summary(results: list[dict], lanes: int, wall: float) -> dict:
        ok = [r for r in results if r.get("ok")]
        fail = [r for r in results if not r.get("ok")]

        def median(xs: list[float]) -> float:
            if not xs:
                return 0.0
            xs = sorted(xs)
            mid = len(xs) // 2
            return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2

        if not ok:
            first = fail[0].get("error") if fail else "没有结果"
            return {"ok": False, "lanes": lanes, "wall": wall,
                    "ok_count": 0, "fail_count": len(fail),
                    "error": f"{len(fail)}/{lanes} 路失败；第一条：{first}"}

        starts = [r["t0"] for r in ok]
        ends = [r["t_end"] for r in ok]
        wall = max(0.001, max(ends) - min(starts))
        total_in = sum(r["prompt_tokens"] for r in ok)
        total_out = sum(r["completion_tokens"] for r in ok)
        decodes = [r["decode"] for r in ok if r["decode"] > 0]
        # 汇总预填充：所有请求的输入 token / 最慢的首 token 延迟。
        agg_prefill = total_in / max(0.001, max(r["ttft"] for r in ok))
        return {
            "ok": True,
            "lanes": lanes,
            "ok_count": len(ok),
            "fail_count": len(fail),
            "wall": wall,
            "ttft_p50": median([r["ttft"] for r in ok]),
            "prefill_p50": median([r["prefill"] for r in ok]),
            "decode_p50": median(decodes),
            "agg_prefill": agg_prefill,
            "agg_decode": total_out / wall,
            "in_tokens": total_in,
            "out_tokens": total_out,
            "estimated": any(r.get("estimated") for r in ok),
            "first_error": (fail[0].get("error") if fail else ""),
        }

    @staticmethod
    def _bench_error_hint(error: str) -> str:
        low = (error or "").lower()
        if ("10061" in low or "积极拒绝" in low or "refused" in low
                or "connection reset" in low):
            return ("目标端口拒绝连接：本地配置要先把 127.0.0.1:8080 的引擎启动；"
                    "要测云端模型，先在左侧列表选中云端那条配置再测。")
        if "401" in low or "unauthorized" in low:
            return "Key 不对或没有权限。"
        if "404" in low or "not found" in low:
            return "地址或模型名不对：链接应到 /v1 这一级，模型名按服务商文档填。"
        return ""

    def _apply_bench_result(self, r: dict) -> None:
        self._bench_busy = False
        self.btn_test.configure(state="normal")
        self.last_bench = r
        if not r.get("ok"):
            where = ""
            if r.get("target"):
                where = f"（{r.get('profile', '')} → {r['target']}）"
            self._say(f"[测速] 失败{where}：{r.get('error')}\n", "err")
            hint = self._bench_error_hint(str(r.get("error", "")))
            if hint:
                self._say(f"  提示：{hint}\n", "hint")
            return
        prefill = max(1, int(round(r["prefill_p50"])))
        decode = max(1, int(round(r["decode_p50"]))) if r["decode_p50"] > 0 else 0
        agg_prefill = max(0, int(round(r.get("agg_prefill", 0))))
        agg_decode = max(0, int(round(r.get("agg_decode", 0))))
        self.v_c_prefill.set(prefill)
        if decode:
            self.v_c_decode.set(decode)
        self.v_c_prefill_agg.set(agg_prefill)
        self.v_c_decode_agg.set(agg_decode)
        self.v_bench_conc.set(int(r.get("ok_count") or r["lanes"]))
        self._sync_local_threshold()

        self._say(f"[测速] 成功 {r['ok_count']}/{r['lanes']} 路，"
                  f"总墙钟 {r['wall']:.1f}s", "ok")
        self._say(f"  首 token p50 {r['ttft_p50']:.2f}s；"
                  f"预填充 p50 {r['prefill_p50']:.0f} tok/s"
                  "（输入 token / 首 token 延迟，含网络与排队）")
        if decode:
            self._say(f"  单路生成 p50 {r['decode_p50']:.0f} tok/s；"
                      f"{r['ok_count']} 路合计生成 {r['agg_decode']:.0f} tok/s")
            self._say(f"  {r['ok_count']} 路合计预填充 {agg_prefill:,} tok/s"
                      "（多段/首压按这个折算）")
        else:
            self._say("  生成速度没测出来（输出太短或服务端整段返回）")
        self._say(f"  实际消耗约 输入 {r['in_tokens']:,} / 输出 {r['out_tokens']:,} token"
                  + ("（服务端未回 usage，按字符估算）" if r.get("estimated") else ""))
        if r.get("first_error"):
            self._say(f"  有 {r['fail_count']} 路失败，第一条：{r['first_error']}")
        self._say(f"  已填入「综合参数计算」：单路 {prefill} / "
                  f"{self.v_c_decode.get()} tok/s；"
                  f"并发实测 {self.v_bench_conc.get()} 路合计 "
                  f"{agg_prefill:,} / {agg_decode:,} tok/s", "hint")
        self._say("  稳态只有 1 段时仍按单路算；首压/多段才用合计值", "hint")
        self._save_pricing()

    def _test_target(self) -> None:
        self._commit_target_form()
        t = self._selected_target()
        if not (t.get("base_url") or "").strip():
            messagebox.showwarning("测试目标连接", "先在左侧选一条任务模型，并填 API 链接。")
            return
        if not (t.get("api_key") or "").strip():
            host = (urlparse((t.get("base_url") or "").strip()).hostname or "")
            if host not in ("127.0.0.1", "localhost", "::1"):
                messagebox.showinfo(
                    "测试目标连接",
                    "Key 留空是透传模式：这里没有 Key 可测。\n"
                    "请在 Cherry Studio 里直接发一条消息验证。")
                return
            # 本地引擎不需要 Key，照测不误
        self._ping(t.get("base_url", ""), t.get("api_key", ""),
                   t.get("model", ""), t.get("extra_json", ""), "目标")

    def _ping(self, base_url: str, api_key: str, model: str,
              extra_json: str, label: str) -> None:
        def work():
            body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8, "stream": False}
            try:
                body.update(json.loads(extra_json) if extra_json else {})
            except Exception:
                pass
            payload = json.dumps(body).encode()
            key = ascii_key(api_key)
            if (api_key or "").strip() and not key:
                self.log_q.put(("err", f"[测试 {label}] Key 里有非 ASCII 字符（可能是 • 或"
                                      f"全角字符），已按空 Key 处理。\n"))
            t0 = time.time()
            errors = []
            # 有的地址要 /v1，有的不要：挨个试，第一个通的就返回
            for url in chat_urls(base_url):
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {key or 'local'}"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        data = json.loads(resp.read()) or {}
                    dt = time.time() - t0
                    txt = _reply_text(data)
                    self.log_q.put(("ok", f"[测试 {label}] 连接成功 {dt:.1f}s  {url}\n"
                                          f"    模型={data.get('model')}  回复={txt[:40]!r}\n"))
                    # 连上了再顺手读一次上下文窗口：本地有 /health，
                    # 部分云端 /v1/models 也会带，读到就填进「上下文能力」。
                    ctx = detect_context(base_url, key, model)
                    if ctx > 0:
                        self.log_q.put(("ok", f"    上下文能力：{ctx:,} token（已填入）\n"))
                        self.root.after(0, lambda n=ctx: self._fill_target_ctx(n))
                    else:
                        self.log_q.put(("hint", "    上下文能力：没能自动读到，请按服务商填写。\n"))
                    return
                except Exception as exc:
                    detail = ""
                    if hasattr(exc, "read"):
                        try:
                            detail = exc.read().decode("utf-8", "replace")[:200]
                        except Exception:
                            pass
                    errors.append(f"    {url} -> {exc} {detail}".rstrip())
            self.log_q.put(("err", f"[测试 {label}] 失败（试了 {len(errors)} 个地址）：\n"
                                  + "\n".join(errors) + "\n"))

        threading.Thread(target=work, daemon=True).start()

    # ---- 启动/停止
    def _listen_addr(self) -> tuple[str, int]:
        return parse_listen(self.v_listen.get())

    def _save_listen_addr(self, _evt=None) -> bool:
        """地址能解析就记住；格式没填完时不打扰，等点启动再提示。"""
        try:
            host, port = self._listen_addr()
        except ValueError:
            return False
        addr = f"{host}:{port}"
        if self.pricing_data.get("service_addr") != addr:
            self.pricing_data["service_addr"] = addr
            save_pricing(self.pricing_data)
        return True

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("已在运行", "代理已经在运行，先停止再启动。")
            return
        try:
            listen_host, listen_port = self._listen_addr()
        except ValueError as exc:
            messagebox.showwarning(
                "服务地址", f"{exc}。\n默认 {DEFAULT_LISTEN}。")
            return
        self._save_listen_addr()
        self._commit_form()
        self._commit_target_form()
        save_profiles(self.profiles)
        save_targets(self.targets)
        if self.current_index is None:
            return
        p = self.profiles[self.current_index]
        if not p.get("local") and (not p["base_url"] or not p["api_key"]):
            messagebox.showwarning("启动", "这条配置缺少 API 链接或 API Key。")
            return
        t = self._selected_target()
        if t.get("enabled") and not (t.get("base_url") or "").strip():
            messagebox.showwarning("启动", "启用了任务模型，但没填它的 API 链接。")
            return
        target = proxy_path()
        if not target.exists():
            messagebox.showerror("启动", f"找不到 {target}")
            return
        if port_busy(listen_port, listen_host):
            pid = port_pid(listen_port)
            messagebox.showerror(
                "端口被占用",
                f"{listen_host}:{listen_port} 已被占用（PID {pid or '?'}）。\n"
                "先停掉那个进程，或在任务管理器结束它。")
            return

        self._active_listen = (listen_host, listen_port)
        env = build_env(p, t, listen_host, listen_port)
        # 3.6：全部任务模型行编成路由目录传给代理——Cherry「同步模型」一次拿到
        # 全部，客户端选哪个模型 = 逐请求路由到哪个上游，不用再回 GUI 设默认。
        env["PROXY_MODEL_CATALOG"] = json.dumps([
            {"id": (x.get("model") or "").strip(),
             "base_url": (x.get("base_url") or "").strip(),
             "api_key": ascii_key(x.get("api_key") or ""),
             "extra_json": (x.get("extra_json") or "").strip(),
             # 每行自己的思考上限；代理按行取，没填才用全局 PROXY_MAX_THINK。
             "max_think": int(x.get("max_think") or 0)}
            for x in self.targets
            if (x.get("model") or "").strip() and (x.get("base_url") or "").strip()],
            ensure_ascii=False)
        self._say(f"启动：{p['name']} | 并发 {env.get('PROXY_CONCURRENCY')} 路 | "
                  f"监听 {listen_host}:{listen_port} | "
                  f"压缩 {p['base_url'] or '本地 127.0.0.1:8080'}", "hint")
        try:
            ra = float(env.get("PROXY_DIGEST_RATIO") or DEFAULT_RATIO)
        except (TypeError, ValueError):
            ra = DEFAULT_RATIO
        if self._ratio_is_dynamic():
            lo, hi = ratio_band(ra)
            band = f"塌缩固定 {hi:.2f}" if lo >= hi else f"区间 {lo:.2f}~{hi:.2f}"
            ratio_text = f"动态压缩比={ra:.2f}（{band}）"
        else:
            ratio_text = f"压缩比={ra:.2f}（固定）"
        task_label = TASK_MODES.get(env.get("PROXY_TASK_MODE"), TASK_MODES["auto"])[0]
        try:
            ww = int(env.get("PROXY_WORK_WINDOW") or WORK_WINDOW_DEFAULT)
        except (TypeError, ValueError):
            ww = WORK_WINDOW_DEFAULT
        target_text = (f"自动 {ww // 1000}K 窗口（85% 触发，压到 45%）"
                       if env.get("PROXY_TARGET_MSG") == "0"
                       else env.get("PROXY_TARGET_MSG"))
        self._say(f"  key={mask(p.get('api_key', ''))}  模型={p['model']}  "
                  f"压缩触发阈值={env.get('PROXY_THRESHOLD')} "
                  f"压缩目标正文={target_text} "
                  f"任务模式={task_label} {ratio_text}", "hint")
        if t.get("enabled"):
            self._say(f"  完成任务模型 = {t.get('model') or '(透传客户端)'} @ {t.get('base_url')}",
                      "hint")
        try:
            self.proc = subprocess.Popen(
                [str(python_exe()), str(target)],
                cwd=str(target.parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self._running_task_mode = env.get("PROXY_TASK_MODE")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_status("运行中…")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.root.after(700, self._check_started)

    def _check_started(self) -> None:
        host, port = getattr(self, "_active_listen", (LISTEN_HOST, LISTEN_PORT))
        if self.proc and self.proc.poll() is None:
            if port_busy(port, host):
                self._set_status("运行中 · {host}:{port} → 8080 · PID {pid}",
                                 host=host, port=port, pid=self.proc.pid)
                self._say(f"代理已就绪：http://{host}:{port}", "ok")
            else:
                self._set_status("启动中… PID {pid}", pid=self.proc.pid)

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tag = "err" if ("Traceback" in line or "ERROR" in line or "failed" in line) else ""
            self.log_q.put((tag, line))
        code = proc.wait()
        self.log_q.put(("hint", f"—— 代理进程退出，exit={code} ——\n"))
        self.log_q.put(("__exit__", ""))

    def _stop(self) -> None:
        if not self.proc:
            return
        self._say("正在停止…", "hint")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
        self.proc = None
        # 等监听端口真正释放，否则马上再点“启动”会误报被占用
        host, port = getattr(self, "_active_listen", (LISTEN_HOST, LISTEN_PORT))
        deadline = time.time() + 4
        while time.time() < deadline and port_busy(port, host):
            time.sleep(0.15)
        self._running_task_mode = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._set_status("已停止")

    # ---- 日志/状态
    def _set_status(self, template: str, **kw) -> None:
        """记中文模板再翻译显示，切换语言时可重画当前状态。"""
        self._status_args = (template, kw)
        self.status.configure(text=i18n.tf(template, **kw) if kw else i18n.t(template))

    def _say(self, text: str, tag: str = "") -> None:
        self.log_q.put((tag, text + ("" if text.endswith("\n") else "\n")))

    def _drain_log(self) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except TclError:
            return
        try:
            while True:
                tag, text = self.log_q.get_nowait()
                if tag == "__exit__":
                    self._running_task_mode = None
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self._set_status("已停止")
                    continue
                self.log.configure(state="normal")
                self.log.insert("end", text, tag)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                result = self.bench_q.get_nowait()
                try:
                    self._apply_bench_result(result)
                except Exception as exc:  # noqa: BLE001
                    self._say(f"[测速] 结果处理失败：{exc}\n", "err")
        except queue.Empty:
            pass
        try:
            while True:
                result = self.price_q.get_nowait()
                try:
                    self._apply_price_fetch(result)
                except Exception as exc:  # noqa: BLE001
                    self._say(f"[价目] 结果处理失败：{exc}\n", "err")
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._drain_log)
        except TclError:
            pass

    def _refresh_active_label(self) -> None:
        """3.5：显示当前唯一启用的任务模型（切换是否生效一眼可验）。"""
        try:
            t = next((x for x in self.targets if x.get("enabled")), None)
            if t:
                self.lbl_active.config(
                    text=f"生效中：{t.get('model') or '（透传客户端模型名）'} @ "
                         f"{t.get('base_url') or '（未填地址）'}")
            else:
                self.lbl_active.config(
                    text="生效中：无——列表没有启用项，代理没有任务模型可转发")
        except Exception:
            pass

    def _update_kpi(self) -> None:
        """3.3 A：读 kpi.jsonl 尾部画回声率/校准系数曲线并更新文字行。"""
        try:
            # 3.18：路由监控档没有台账与探针，回声率无从谈起——文字行明说
            # "已暂停"，别停在旧曲线上装还活着（降档建议也随之静默）。
            if proxy_stats().get("task_mode") == "monitor":
                self.lbl_kpi.config(text=i18n.t(
                    "路由监控模式：内容逐字节直通，无检查点台账——"
                    "回声率/探针/降档建议已暂停；token 实况见下方实况表"))
                return
            p = proxy_dir() / "kpi.jsonl"
            if not p.exists() or not hasattr(self, "kpi_canvas"):
                return
            all_rows = []
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
                try:
                    all_rows.append(json.loads(ln))
                except ValueError:
                    pass
            # 3.17 ②：先按会话过滤（读尾 400 行防过滤后不够 60）。
            conv_rows = _kpi_filter_rows(all_rows, getattr(self, "kpi_conv", None))
            if not conv_rows:
                return
            # 4.1.0（用户定案）：绿线数据源 = 「调取线图」选的模型（默认当前档）；
            # 时间段 = 「线图时间范围」选的窗口（默认近 60 轮）；「基线」下拉只决定
            # 灰线。三者互不干扰——换下拉不再改绿线的数据窗口。
            mdl = kpi_model_of(conv_rows[-1])
            grp = baseline_group(mdl)
            view_mdl = getattr(self, "kpi_model_view", None) or mdl
            src_rows = [r for r in conv_rows if kpi_model_of(r) == view_mdl]
            win_sec = getattr(self, "kpi_win_sec", None)
            if win_sec is None:
                rows = src_rows[-60:]
            else:
                rows = _kpi_window_rows(src_rows, win_sec)
            if not rows:
                return
            cv = self.kpi_canvas
            cv.delete("all")
            w = max(140, cv.winfo_width())
            h = 190
            # 3.16：底部 18px 留给时间刻度轴带，曲线区压到 hp。
            hp = h - 18
            # 浅地板线：绿紫两线都贴零时也能看出它们趴在 0 位而不是缺画
            cv.create_line(2, hp - 3, w - 2, hp - 3, fill="#e3e3e3")

            def poly(vals, color, lo, hi, dash=None):
                n = max(2, len(vals))
                for run in _kpi_runs(vals):     # 3.17.2：整段一次画，dash 才可见
                    flat = []
                    for i, v in run:
                        x = 2 + i * (w - 4) / (n - 1)
                        y = hp - 3 - (max(lo, min(hi, v)) - lo) / (hi - lo) * (hp - 6)
                        flat.extend((x, y))
                    if len(flat) >= 4:
                        cv.create_line(*flat, fill=color, dash=dash)

            # er=每轮原始回声率：3.22 起不再画蓝线，但仍供量程 hi 计算与文字行
            # 「回声率 x%」读数（口径不变，池<5 也照算，那是"发生了什么"）；
            # 绿线=重心：灰线在场必须与基线同口径才可比；灰线未成型时自显
            # （3.21 用户定案——旧版让绿线陪葬，usage 超 10 万全拦时蓝在绿没）。
            er = [((r.get("e", 0) + r.get("f", 0)) / r["a"]) if r.get("a") else 0.0
                  for r in rows]
            led = load_baselines()
            # 4.1.0：后台按实际模型收集基线（下拉选啥都收）；有变化才落盘。
            if collect_model_baselines(all_rows, led):
                try:
                    save_baselines(led)
                except OSError:
                    pass
            # 显示层与收集解耦（用户定案）：自成一组 → 用本模型自己的条目；
            # 选具体组 → 用那条线。收集在上一行，与这里无关。
            show_key = mdl if grp == BASELINE_SELF else grp
            ent = led.get(show_key) if isinstance(led.get(show_key), dict) else None
            # 自成一组只认后台自己攒的(auto)条目；模型名键下的手动/历史定标(manual)
            # 属于共用组的历史线，自成一组没攒够时不该借它画灰线（4% 历史线误现）。
            if grp == BASELINE_SELF and ent and str(ent.get("src") or "") != "auto":
                ent = None
            if not baseline_entry_valid(ent):
                ent = None      # 3.29：存量 100% 饱和基线作废，等同未定标
            if ent is not None:
                metric = str(ent.get("metric") or "echo")
                base = float(ent["base"])
                bn = int(ent.get("n") or 0)
                b_src = str(ent.get("src") or "manual")
                b_note = ""
            else:
                # 没条目 → 只用本模型行现算（绝不拿混合组的平均充数）。
                own = [r for r in conv_rows if kpi_model_of(r) == mdl]
                metric, base, bn, b_note = baseline_pick(own)
                b_src = "auto"
            pool_a = int(rows[-1].get("a") or 0)
            green, gname, comparable = kpi_green_series(
                rows, metric, pool_a, base is not None)
            self.kpi_grp = grp   # 3.23/4.2.0：基线下拉要用当前组与模型
            self.kpi_mdl = mdl
            if hasattr(self, "cb_kpi_base"):
                opts = [BASELINE_SELF] + sorted(
                    g for g, e in led.items()
                    if g != BASELINE_SELF and baseline_entry_valid(e))
                self.cb_kpi_base["values"] = opts
                self.cb_kpi_base.set(grp if grp in opts else BASELINE_SELF)
            ma = _kpi_roll(green)
            # 3.20：共用基线时，本档自己一条合格样本都没有 → 灰线只当参照，
            # 不出降档建议（否则本地量化档天天被云端水位误报成"注意力下滑"）。
            own_n = baseline_calc([r for r in conv_rows if kpi_model_of(r) == mdl],
                                  metric)[1]
            ref_only = own_n == 0
            hi = max(_kpi_scale([v for v in er + ma if v is not None]),
                     (base or 0.0) * 2)   # 3.20：基线存在时量程至少到基线×2，
                                          # 杜绝稀疏序列的 90 分位把灰线抬到半图高
            # 3.22：蓝线（每轮原始值）与橙线（k 系数线）已删——用户定案"除了让
            # 线图更复杂"；两个数没丢：文字行的「回声率 x%」与「k x.xx」还在。
            # 3.23：点线判据回到 3.19 形态——只看池子置信度，与基线口径无关。
            poly(ma, "#2a9a4a", 0.0, hi,
                 (1, 3) if pool_a < BASELINE_MIN_POOL else (8, 3))
            if base is not None:
                yb = hp - 3 - max(0.0, min(1.0, base / hi)) * (hp - 6)
                cv.create_line(2, yb, w - 2, yb, fill="#999", dash=(3, 2))
                # 3.22：灰线右端标数值；3.24 改白底矩形垫底（create_text 没有 background）
                _kpi_draw_base_label(cv, w, yb, base)
            # 3.17 ①：紫长线段=换说法命中率重心（非字面真召回，唯一的"理解级"曲线）；
            # 已问<3 时连续点=低置信；没问过整线不画。
            # 3.22：紫线与绿线共用 0..hi 一条量程——一张图不搞两种高度含义。
            hits = _kpi_hit_series(rows)
            cold_last = rows[-1].get("cold") or {}
            asked = int(cold_last.get("qhit", 0) or 0) + int(cold_last.get("qmiss", 0) or 0)
            if any(v is not None for v in hits):
                poly(hits, "#7a40c8", 0.0, hi,
                     (1, 3) if asked < 3 else (8, 3))
            # 3.22：纵轴百分比刻度最后画；3.24 白底改矩形垫底（文本项无 background 选项）
            _kpi_draw_y_ticks(cv, hi, hp)
            # 3.16：X 轴时间刻度（HH:MM），数据源是 kpi.jsonl 每行的 t 字段。
            _kpi_draw_time_ticks(cv, rows, w, h)
            last = rows[-1]
            can = last.get("can") or {}
            alive, lost = int(can.get("alive", 0)), int(can.get("lost", 0))
            thr = sum(1 for r in rows if r.get("thr"))
            # 3.4.7：重心和基线双双≈0 多半是"池子里全是旧任务约束"（3.4 分母
            # 切换后旧会话的算术必然），不是注意力问题——直接说人话，别让人误判降档。
            hint = ("（池子未成型：多为旧任务约束，看探针）"
                    if (ma and ma[-1] is not None and base is not None
                        and ma[-1] < 0.03 and base < 0.03) else "")
            rw = int(last.get("rw") or 0)          # 3.8 A：复位无效看门狗
            if rw >= 3:
                hint += f"（复位无效 {rw} 轮，查日志）"
            cold = last.get("cold") or {}          # 3.13 远期探针
            cold_txt = ""
            if cold:
                ca = int(cold.get("alive", 0))
                cl = int(cold.get("lost", 0))
                qh = int(cold.get("qhit", 0))
                qm = int(cold.get("qmiss", 0))
                cold_txt = (f" · 远期 {ca}/{ca + cl}"
                            f"（盯 {int(cold.get('watch', 0))} "
                            f"重锚 {int(cold.get('re', 0))}"
                            + (f" 换说法 {qh}/{qh + qm}" if qh or qm else "")
                            + "）")
            # 3.17 ①：命中率读数（紫线对应的数）；④：持续性降档建议（≥3 轮才算）。
            hit_txt = ""
            if asked:
                hv = hits[-1]
                hit_txt = (f" · 换说法命中 {hv * 100:.0f}%"
                           if hv is not None else f" · 换说法已问 {asked}")
            # 3.20：没成型基线（base=None）或本档只是共用参照（ref_only）时
            # 一律不出降档建议——拿别人的水位判自己的注意力就是误报。
            # 3.23：再加一条——基线是探针口径时也不出建议：绿线是回声重心，
            # 跨口径不互相打分（100% 探针基线会把任何回声波动判成"下滑"）。
            below = (_kpi_tail_below(ma, base)
                     if base is not None and not ref_only and comparable else 0)
            down_txt = (f" · 重心连续 {below} 轮低于基线 → 建议降一档"
                        if below >= 3
                        else (f" · 低于基线 {below} 轮（未满 3，不出建议）"
                              if below else ""))
            # 3.17.4：降档建议成立时弹窗给可执行动作（用户定稿文案两条路）。
            # 边沿触发——每个降档周期只弹一次，掉 1~2 轮再回不算新周期。
            _fire, self.kpi_down_latched = _kpi_down_edge(
                below, getattr(self, "kpi_down_latched", False))
            if _fire:
                self._kpi_down_popup(below)
            filt_txt = (f" · 过滤 {self.kpi_conv}"
                        if getattr(self, "kpi_conv", None) else "")
            m_last = ma[-1] if ma and ma[-1] is not None else None
            # 3.21：gname 由 kpi_green_series 给出（自显段带·自显后缀）
            if base is None:
                base_txt = f"基线未成型：{b_note or '合格 0 轮'}"
            else:
                base_txt = (f"基线 {base * 100:.0f}%（{grp}·"
                            f"{'探针存活' if metric == 'probe' else '回声率'}"
                            f"·n={bn}·{b_src}）"
                            + ("·共用参照不出降档建议" if ref_only else "")
                            + ("·探针口径·与回声绿线跨口径不互相打分"
                               if metric == "probe" else ""))
            model_txt = (f" · 当前档 {mdl}" if mdl != BASELINE_NO_MODEL
                         else " · 历史统计档（qwen3.8-flash 早期数据）")
            if view_mdl != mdl:
                model_txt += f" · 绿线数据 {view_mdl}"
            model_txt += f" · 时间范围 {_kpi_range_label(win_sec)}"
            self.lbl_kpi.config(
                text=(f"探针 {alive}/{alive + lost} · 回声率 {er[-1] * 100:.0f}% "
                      f"{gname} {'—' if m_last is None else f'{m_last * 100:.0f}%'} · "
                      + base_txt + " · "
                      f"k {last.get('k') or 1.0:.2f} · "
                      f"数据锚 {last.get('d', 0)} · 退役 {last.get('r', 0)} · "
                      f"退避 {thr} 轮" + cold_txt + hit_txt + down_txt
                      + filt_txt + model_txt + hint))
        except Exception:
            pass

    def _on_ctx_select(self, _e=None) -> None:
        """3.17 ②：点历史实况的会话行 → KPI 图只看这条会话。"""
        sel = self.ctx_tree.selection()
        if not sel:
            return
        vals = self.ctx_tree.item(sel[0], values=())
        if vals and vals[0]:
            self.kpi_conv = str(vals[0])
            self._update_kpi()
            self._rb_refresh()

    def _rb_conv_now(self) -> str:
        """回滚目标会话：KPI 过滤的那条；没选则实况表第一行（最新）。"""
        if self.kpi_conv:
            return self.kpi_conv
        kids = self.ctx_tree.get_children()
        if kids:
            vals = self.ctx_tree.item(kids[0], values=())
            if vals and vals[0]:
                return str(vals[0])
        return ""

    def _rb_snap_times(self, conv: str) -> list[str]:
        d = sess_dir() / conv / "snap"
        day = time.strftime("%Y-%m-%d")
        out = []
        try:
            names = sorted(d.glob("*.json"), key=lambda q: q.name)
        except OSError:
            return out
        for p in names:
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if s.get("day") == day and s.get("t"):
                out.append(str(s["t"]))
        return out

    def _rb_refresh(self, stats: dict | None = None) -> None:
        conv = self._rb_conv_now()
        self.rb_conv.configure(text=conv[:16] or "—")
        times = self._rb_snap_times(conv)[::-1]        # 新在前
        cur = self.v_rb_time.get()
        self.cb_rb_time["values"] = times
        if cur not in times:
            self.v_rb_time.set(times[0] if times else "")
        if stats is None:
            stats = proxy_stats()
        cmd = stats.get("cache_cmd") or {}
        if not times and conv and not cmd:
            # 下拉空不是坏了：快照从本版代理启动后才开始拍
            self.rb_status.configure(text=i18n.tf(
                "尚无快照：本版启动后每轮对话拍一张，也可手填时刻"))
        elif cmd:
            st = cmd.get("status")
            got = cmd.get("restored") or ""
            self.rb_status.configure(text=i18n.tf(
                "最近一次：{st} 恢复至 {t}", st=st, t=got or "—"))

    def _on_cache_restore(self) -> None:
        conv = self._rb_conv_now()
        as_of = rb_norm_time(self.v_rb_time.get())
        if not conv or not as_of:
            self.rb_status.configure(text=i18n.tf(
                "时刻格式：HH:MM 或 HH:MM:SS"))
            return
        cmd = {"conv": conv, "as_of": as_of, "status": "pending",
               "nonce": int(time.time())}
        try:
            p = sess_dir() / "cache_cmd.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cmd, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self.rb_status.configure(text=i18n.tf("写命令失败：{e}", e=exc))
            return
        self.rb_status.configure(text=i18n.tf("已下发，下一轮对话生效"))

    def _kpi_update_baseline(self) -> None:
        """4.2.0（用户定案）：「更新基线」——弹模型列表，选定按确定，
        用该模型**全部有效数据**重算并覆盖该模型基线组的基线。
        意义：数据变多了（原 20 轮有效 → 现在 40 轮），基线该跟着更新。"""
        p = proxy_dir() / "kpi.jsonl"
        if not p.exists():
            messagebox.showinfo("更新基线", "还没有 kpi.jsonl，代理跑起来再说。")
            return
        rows = []
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                pass
        models = _kpi_models_in(rows)
        if not models:
            messagebox.showinfo("更新基线", "还没有数据，无从更新。")
            return
        win = Toplevel(self.root)
        win.title("更新基线：选择模型")
        win.transient(self.root)
        var = StringVar(value=models[0][0])
        ttk.Label(win, justify="left",
                  text="用哪款模型的全部有效数据生成新基线"
                       "（覆盖该模型基线组的旧基线）：").pack(
            anchor="w", padx=12, pady=(10, 4))
        for m, c in models:
            ttk.Radiobutton(win, text=f"{m}（共 {c} 轮）", value=m,
                            variable=var).pack(anchor="w", padx=20)

        def _ok():
            win.destroy()
            self._do_update_baseline(rows, var.get())

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="确定", command=_ok).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=6)

    def _do_update_baseline(self, rows: list, model: str) -> None:
        """4.2.0：选定模型后重算并覆盖基线（退化/不足时弹窗说明，不写盘）。"""
        try:
            grp, metric, base, n, note = baseline_from_model(rows, model)
            if metric is None:
                messagebox.showwarning(
                    "更新基线",
                    f"「{model}」→ 组「{grp}」：{note}\n"
                    f"门槛：池≥{BASELINE_MIN_POOL} · 前 {BASELINE_MAX_USAGE // 1000} 千"
                    f" token · 非退避轮。\n多跑几轮再更新。")
                return
            led = load_baselines()
            led[grp] = {"metric": metric, "base": round(base, 4), "n": n,
                        "t": time.strftime("%H:%M:%S"), "src": "manual",
                        "from_model": model,
                        "gate": (f"池≥{BASELINE_MIN_POOL}·前"
                                 f"{BASELINE_MAX_USAGE // 1000}千·非退避")}
            save_baselines(led)
            self._update_kpi()
            messagebox.showinfo(
                "更新基线",
                f"已把「{grp}」的基线更新为 {base * 100:.1f}%"
                f"（口径：{'回声率' if metric == 'echo' else '探针存活率'}，"
                f"用「{model}」的 {n} 轮有效数据）。\n"
                f"台账：{baselines_path()}")
        except Exception as exc:
            messagebox.showwarning("更新基线",
                                   f"更新失败：{type(exc).__name__}: {exc}")

    def _current_conv_live(self):
        """3.17.5：「本会话」= KPI 图当前过滤的那条；没过滤则实况表第一行（最新）。"""
        stats = proxy_stats()
        convs = [c for c in (stats.get("conversations") or [])
                 if isinstance(c, dict) and c.get("key")]
        filt = getattr(self, "kpi_conv", None)
        if filt:
            for c in convs:
                if str(c.get("key")) == str(filt):
                    return c
        return convs[0] if convs else None

    def _export_session_md(self) -> None:
        """3.17.5：一键把本会话台账导出成 .md，落成后直接给绝对路径。

        数据源就是历史实况背后的两样东西：proxy-stats.json 的实况行 +
        sessions/<conv>/checkpoint.json 的台账——弹窗指路变成弹窗代办。
        """
        live = self._current_conv_live()
        if live is None:
            messagebox.showinfo("导出", "历史实况里还没有会话，没有可导出的内容。")
            return
        conv = str(live.get("key"))
        ckpt = load_session_ckpt(conv)
        md = session_md(conv, live, ckpt,
                        now=time.strftime("%Y-%m-%d %H:%M:%S"))
        out_dir = proxy_dir() / "exports"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            fp = out_dir / (f"历史台账-{conv[:8]}-"
                            f"{time.strftime('%Y%m%d-%H%M%S')}.md")
            fp.write_text(md, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("导出失败", f"写文件失败：{exc}")
            return
        tip = f"已导出：\n{fp}\n"
        if ckpt is None:
            tip += "\n（这条会话还没有检查点台账，文件里只有实况统计。）"
        tip += "\n要打开所在文件夹吗？"
        if messagebox.askyesno("导出完成", tip):
            try:
                os.startfile(str(out_dir))
            except Exception:
                pass

    def _kpi_down_popup(self, below: int) -> None:
        """3.17.5：降档弹窗由「指路」升级为「代办」——②不再让你手动导出，
        按钮就地导出本会话台账；①仍是你在界面里自己改。同一周期只开一次。"""
        old = getattr(self, "_down_pop", None)
        try:
            if old is not None and old.winfo_exists():
                return
        except TclError:
            pass
        top = Toplevel(self.root)
        self._down_pop = top
        top.title(i18n.t("降档建议"))
        top.resizable(False, False)
        ttk.Label(
            top, padding=(12, 12, 12, 4), wraplength=460, justify="left",
            text=(f"重心连续 {below} 轮低于上期基线，模型注意力效能下滑。二选一：\n\n"
                  "① 把「建议上下文」改小一档（如 320k→265k）——这条在你侧；\n\n"
                  "② 把本会话历史导出成 .md，新开客户端窗口贴入，轻装上阵"
                  "恢复推理效能——这条代理代办，点下面按钮即可。")).pack(fill="x")
        ttk.Label(
            top, padding=(12, 0, 12, 8), wraplength=460, foreground="#666",
            text="（本弹窗每个降档周期只提醒一次；判据解除后再次成立才会再弹。）"
            ).pack(fill="x")
        btns = ttk.Frame(top, padding=(12, 0, 12, 12))
        btns.pack(fill="x")

        def _do_export():
            try:
                self._export_session_md()
            except Exception as exc:
                messagebox.showerror("导出失败", f"导出出错：{exc}")

        ttk.Button(btns, text=i18n.t("一键导出本会话为 .md"),
                   command=_do_export).pack(side="left")
        ttk.Button(btns, text=i18n.t("知道了"),
                   command=top.destroy).pack(side="right")

    def _tick_status(self) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except TclError:
            return
        self._update_kpi()       # 3.3 A：曲线每 2 秒跟一次
        self._refresh_active_label()   # 3.5
        if not (self.proc and self.proc.poll() is None):
            try:
                host, port = self._listen_addr()
            except ValueError:
                self._set_status("地址格式不对")
            else:
                busy = port_busy(port, host)
                self._set_status("占用中" if busy else "空闲")
        key = self._period_key()
        last = getattr(self, "_last_period_key", None)
        if last is None:
            self._last_period_key = key
            self._update_current_period()
        elif key != last:
            self._last_period_key = key
            self._on_period_change()
        else:
            self._update_current_period()
        self._update_ctx_live()
        try:
            self.root.after(2000, self._tick_status)
        except TclError:
            pass

    def _update_ctx_live(self) -> None:
        """刷新任务模型历史实况：状态条一行文字 + 最近会话表（R22/R24）。

        读的是 proxy.py 每轮落盘的 proxy-stats.json——代理子进程崩了也不影响
        本方法，最多停在最后一次记录。updated 没变就不重灌表，避免闪烁。
        """
        try:
            stats = proxy_stats()
            self.ctx_live.configure(text=ctx_live_text(stats))
            updated = stats.get("updated")
            if updated == self._ctx_sig:
                return
            self._ctx_sig = updated
            tree = self.ctx_tree
            tree.delete(*tree.get_children())
            for row in ctx_rows(stats):
                tree.insert("", "end", values=row)
            self._rb_refresh(stats)
        except Exception:
            pass

    def _on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("退出", "代理正在运行，退出会一起停止它。继续？"):
                return
            self._stop()
        self.root.destroy()


def _kpi_y_ticks(hi: float, hp: int) -> list:
    """3.22：KPI 线图纵轴刻度，返回 [(y, "百分比标签")]，0/25/50/75/100% 五档。

    全图一条量程 0..hi（绿/紫/灰共用），"线的高低是什么含义"直接由刻度回答；
    标签先 round 再 :g——37.5% 保留一位、40% 不带小数尾巴。hi 异常（≤0）
    时返回空表，宁可不画不画错。
    """
    if not hi or hi <= 0:
        return []
    out = []
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = hp - 3 - f * (hp - 6)
        out.append((y, f"{round(hi * f * 100, 1):g}%"))
    return out


def _kpi_label(cv, x: float, y: float, text: str, anchor: str = "w",
               fill: str = "#999"):
    """3.24：白底标签——先压字、取 bbox、再垫一块同底色矩形（Canvas 文本项
    没有 background 选项；3.23 用 create_text(background=) 抛 TclError，把
    刻度/时间轴/文字行整条刷新链腰斩，这就是"横轴时间与纵轴标注都没了"的真凶）。"""
    t = cv.create_text(x, y, text=text, anchor=anchor, fill=fill)
    x1, y1, x2, y2 = cv.bbox(t)
    r = cv.create_rectangle(x1 - 2, y1 - 1, x2 + 2, y2 + 1,
                            fill=str(cv.cget("background")), outline="")
    cv.tag_lower(r, t)
    return t


def _kpi_draw_y_ticks(cv, hi: float, hp: int) -> int:
    """3.24：画纵轴百分比刻度，返回条数（行为判据用真 Canvas 数它）。"""
    ticks = _kpi_y_ticks(hi, hp)
    for y, lbl in ticks:
        _kpi_label(cv, 4, y, lbl, anchor="w")
    return len(ticks)


def _kpi_draw_time_ticks(cv, rows: list, w: int, h: int) -> int:
    """3.24：画横轴时间刻度，返回条数。"""
    ticks = _kpi_time_ticks(rows, w)
    for x, lbl, anc in ticks:
        _kpi_label(cv, x, h - 9, lbl, anchor=anc, fill="#666")
    return len(ticks)


def _kpi_draw_base_label(cv, w: int, yb: float, base: float) -> None:
    """3.24：灰线右端基线数值标注。"""
    _kpi_label(cv, w - 4, max(9, yb - 7),
               f"基线 {base * 100:.0f}%", anchor="e", fill="#666")


def kpi_snapshot_payload(rows: list, group: str, model: str, led: dict) -> dict:
    """3.25：线图快照的数据包——每模型一张图，存下来可调取重绘。
    带每轮原始读数（t/回声/探针/usage/k）、绿线（回声重心）、基线口径与数值。
    probe 口径基线照样记下（metric 字段），但重绘时只画 echo 口径（跨口径不混比）。"""
    rows = rows[-60:]
    vals = [_kpi_echo_rate(r) for r in rows]
    ma = _kpi_roll(vals)
    ent = led.get(group) if isinstance(led.get(group), dict) else None
    metric = str(ent.get("metric") or "echo") if ent else ""
    base = float(ent["base"]) if ent and ent.get("base") is not None else None
    return {
        "v": 1, "group": group, "model": model,
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metric": metric, "base": base,
        "base_n": int(ent.get("n") or 0) if ent else 0,
        "rounds": [{"t": r.get("t"), "a": r.get("a"), "e": r.get("e"),
                    "f": r.get("f"), "usage": r.get("usage"),
                    "alive": (r.get("can") or {}).get("alive", 0),
                    "lost": (r.get("can") or {}).get("lost", 0),
                    "k": r.get("k")} for r in rows],
        "echo": vals, "green": ma,
        "probe": [_kpi_probe_rate(r) for r in rows],
    }


def _kpi_draw_snapshot(cv, sn: dict, w: int, h: int) -> dict:
    """3.25：把线图快照重绘到画布（调取窗口用）——绿线+灰基线（仅 echo 口径）
    +纵轴百分比+横轴时间，全部走和主线同一套绘制函数。返回 {last, base, ticks}。"""
    hp = h - 18
    vals = sn.get("green") or []
    echo = sn.get("echo") or []
    base = sn.get("base")
    metric = str(sn.get("metric") or "")
    hi = max(_kpi_scale([v for v in echo + vals if v is not None]) or 1.0,
             (base or 0.0) * 2) or 1.0

    def poly(seq, color, dash=None):
        n = max(2, len(seq))
        for run in _kpi_runs(seq):
            flat = []
            for i, v in run:
                x = 2 + i * (w - 4) / (n - 1)
                y = hp - 3 - (max(0.0, min(hi, v)) / hi) * (hp - 6)
                flat.extend((x, y))
            if len(flat) >= 4:
                cv.create_line(*flat, fill=color, dash=dash)

    cv.create_line(2, hp - 3, w - 2, hp - 3, fill="#e3e3e3")
    poly(vals, "#2a9a4a", (8, 3))
    if base is not None and metric == "echo":
        yb = hp - 3 - max(0.0, min(1.0, base / hi)) * (hp - 6)
        cv.create_line(2, yb, w - 2, yb, fill="#999", dash=(3, 2))
        _kpi_label(cv, w - 4, max(9, yb - 7),
                   f"基线 {base * 100:.0f}%", anchor="e", fill="#666")
    n_ticks = _kpi_draw_y_ticks(cv, hi, hp)
    rows = [{"t": (r or {}).get("t")} for r in (sn.get("rounds") or [])]
    _kpi_draw_time_ticks(cv, rows, w, h)
    last = next((v for v in reversed(vals) if v is not None), None)
    return {"last": "—" if last is None else f"{last * 100:.0f}%",
            "base": base if metric == "echo" else None, "ticks": n_ticks}


def _kpi_time_ticks(rows: list, w: int) -> list:
    """3.16：算 KPI 线图的 X 轴时间刻度，返回 [(x, "HH:MM", anchor)]。

    规则：至多 4 个均匀刻度（首、1/3、2/3、尾）；宽度 <260px 只留首尾、
    <120px 或不足 2 行不画；行缺 t 字段（旧数据）跳过该刻度，
    全缺返回空表——宁可不画，不画错。
    """
    n = len(rows)
    if n < 2 or w < 120:
        return []
    idxs = [0, n - 1] if w < 260 else [0, (n - 1) // 3, 2 * (n - 1) // 3, n - 1]
    out = []
    for i in sorted(set(idxs)):
        t = str(rows[i].get("t") or "")[:5]
        if not t:
            continue
        x = int(round(2 + i * (w - 4) / (n - 1)))
        out.append((x, t, "w" if i == 0 else ("e" if i == n - 1 else "center")))
    return out


def _kpi_hit_series(rows: list) -> list:
    """3.17 ①：每轮换说法累计命中率 qhit/(qhit+qmiss)；没问过=None，
    poly 遇 None 断线——宁可不画，不画假 0。"""
    out = []
    for r in rows:
        c = r.get("cold") or {}
        qh = int(c.get("qhit", 0) or 0)
        qm = int(c.get("qmiss", 0) or 0)
        out.append(qh / (qh + qm) if qh + qm else None)
    return out


def _kpi_tail_below(series: list, base: float) -> int:
    """3.17 ④：末端连续低于基线的轮数（遇 None 或不低于即断）。
    连续 ≥3 才出降档建议——单轮尖峰不算数。"""
    n = 0
    for v in reversed(series):
        if v is None or v >= base:
            break
        n += 1
    return n


def _kpi_filter_rows(rows: list, conv) -> list:
    """3.17 ②：按会话过滤 kpi 行；conv 空=全量（老行为）。"""
    if not conv:
        return list(rows)
    return [r for r in rows if r.get("conv") == conv]


KPI_RANGE_PRESETS = (
    ("近 60 轮（默认）", None),
    ("近 30 分钟", 1800),
    ("近 1 小时", 3600),
    ("近 3 小时", 10800),
    ("近 6 小时", 21600),
    ("近 12 小时", 43200),
    ("近 24 小时", 86400),
    ("近 3 天", 259200),
    ("全部（尾 400 行）", 0),
)


def _kpi_window_rows(rows: list, window_sec, max_rows: int = 400) -> list:
    """4.1.0：按时间窗从尾部往回取行（kpi 行只有 HH:MM:SS，跨午夜按
    "时钟倒退=进入下一天"累计）。window_sec 为 None/0 → 全量（尾 max_rows）。"""
    if not rows:
        return []
    if not window_sec:
        return rows[-max_rows:]

    def _sec(t):
        try:
            h, m, s = str(t).split(":")
            return int(h) * 3600 + int(m) * 60 + int(s)
        except (ValueError, AttributeError):
            return None

    out = [rows[-1]]
    cum = 0.0
    for i in range(len(rows) - 1, 0, -1):
        a, b = _sec(rows[i - 1].get("t")), _sec(rows[i].get("t"))
        if a is not None and b is not None:
            dt = b - a
            if dt < 0:
                dt += 86400
            cum += dt
        if cum > window_sec:
            break
        out.append(rows[i - 1])
    return list(reversed(out))


def _kpi_models_in(rows: list) -> list:
    """4.1.0：窗口内出现的模型及轮数（多的在前）——调取线图的候选。"""
    cnt: dict = {}
    for r in rows:
        m = kpi_model_of(r)
        cnt[m] = cnt.get(m, 0) + 1
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))


def _kpi_range_label(win) -> str:
    """4.1.0：时间范围的可读标签（None=近 60 轮）。"""
    for lbl, sec in KPI_RANGE_PRESETS:
        if sec == win:
            return lbl
    return "近 60 轮（默认）"


def _kpi_runs(vals: list) -> list:
    """3.17.2：把序列切成连续非 None 段，每段 [(i, v), ...]；None 断口不连。
    poly 整段一次 create_line——旧版逐段画，dash 每段从头重启，段长(~6px)
    小于实线长(8px) 时长短线段全糊成实线（用户报"长短线段看不见"）。"""
    runs, cur = [], []
    for i, v in enumerate(vals):
        if v is None:
            if cur:
                runs.append(cur)
                cur = []
        else:
            cur.append((i, v))
    if cur:
        runs.append(cur)
    return runs


def _kpi_down_edge(below: int, latched: bool, min_rounds: int = 3):
    """3.17.4：降档弹窗边沿触发——每个降档周期只弹一次。
    firing 期间 latch 保持（掉到 1~2 轮算同一周期，不重复弹）；
    below==0（周期真结束）才解锁，下次成立才再弹。"""
    firing = below >= min_rounds
    notify = firing and not latched
    new_latched = True if firing else (latched if below > 0 else False)
    return notify, new_latched


# ---------------------------------------------------------------- 3.20 基线
# 用户定案：基线必须与绿线同口径才可比，且按模型分档——换模型自然换基线。
# 旧算法（同一条曲线里"当期 20 轮 vs 再往前 20 轮"）在样本 <40 时兜底成
# 全体均值，绿线是前缀均值，数学上必然长期低于它——这是用户报"绿线始终
# 低于基线"的真因，所以基线改成从独立的合格样本里算。
BASELINE_MIN_POOL = 5        # 进回声率基线的池子下限：a<5 时回声率是抛硬币
BASELINE_MAX_USAGE = 100_000  # 低压段=新对话前 10 万 token 上下文（用户口径）
BASELINE_AUTO_N = 20         # 自动成线要够这么多合格轮次，不够就不画
BASELINE_MANUAL_N = 20       # 3.25：手动定标也要 20 轮合格样本（用户定案「基线要真实」——
                             # n=5 的 100% 探针快照被当成监视尺，正是 3.23 那场的教训）
BASELINE_NO_MODEL = "qwen3.8 flash历史统计数据"   # 3.27 用户定名：3.20 前的 kpi 行
                                                  # 没有 model 字段，实测就是云端 qwen3.8-flash 的历史
BASELINE_SELF = "自成一组（自己攒）"      # 3.23：基线下拉的"不共用"选项

# 用户定案：本地档就是同一模型量化过的（qwen3.8-flash → IQ3_XXS），可共用基线。
# 共用只给参照线，不给降档建议（本档自己 0 条合格样本时，拿云端水位判它就是误报）。
BASELINE_GROUPS = {
    "qwen3.8-flash": "qwen3.8-flash",
    "swift-1.5-iq3_xxs": "qwen3.8-flash",
}


_MODEL_GROUP_CACHE: list = [None, {}]   # [台账 mtime, 用户映射表]——按 mtime 失效


def _user_model_group() -> dict:
    """用户换模型时选的 模型→基线组 映射（baselines.json 的 model_group 键）。
    按 mtime 缓存：_update_kpi 每 2 秒逐行调用，不能每次重读文件。"""
    try:
        mt = baselines_path().stat().st_mtime
    except OSError:
        mt = None
    if _MODEL_GROUP_CACHE[0] != mt:
        um = load_baselines().get("model_group")
        _MODEL_GROUP_CACHE[1] = dict(um) if isinstance(um, dict) else {}
        _MODEL_GROUP_CACHE[0] = mt
    return _MODEL_GROUP_CACHE[1]


def baseline_group(model: str, umap: dict | None = None) -> str:
    """模型名 → 基线组名；用户映射优先内置表（3.21：换模型时挑线），
    没登记过的一律自成一组（新模型不会被并进别人的基线）。"""
    if umap is None:
        umap = _user_model_group()
    if model in umap:
        return str(umap[model]) or model
    return BASELINE_GROUPS.get(model, model)


def _kpi_roll(vals: list, win: int = 20) -> list:
    """滚动重心：窗口内的 None 不计入（池<5 的轮次不进回声率重心，
    与基线同口径——宁可不画，不把不可比的轮次平均进来）。"""
    out = []
    for i in range(len(vals)):
        seg = [v for v in vals[max(0, i - win + 1):i + 1] if v is not None]
        out.append(sum(seg) / len(seg) if seg else None)
    return out


def kpi_model_of(row: dict) -> str:
    """这轮实际发给哪个模型；旧行没这个字段就归进不参与基线的桶（不回填，
    kpi 行只有 HH:MM:SS 没有日期，按日志时间轴回填跨天必归错组）。"""
    return str(row.get("model") or "") or BASELINE_NO_MODEL


def _kpi_echo_rate(row: dict):
    a = int(row.get("a") or 0)
    if not a:
        return None
    return (int(row.get("e", 0) or 0) + int(row.get("f", 0) or 0)) / a


def _kpi_probe_rate(row: dict):
    """探针存活率 alive/(alive+lost)：不依赖池子大小。本地模型窗口小、
    台账只剩 2-3 条锚点，回声率没统计意义，记忆读数走这条（用户定案）。"""
    can = row.get("can") or {}
    al = int(can.get("alive", 0) or 0)
    lo = int(can.get("lost", 0) or 0)
    return al / (al + lo) if al + lo else None


def kpi_green_series(rows: list, metric, pool_a: int, has_base: bool):
    """3.23：绿线永久=回声率重心（用户定案：3.21 的"跟基线走"让 probe 口径
    基线把绿线钉成 100% 平线——600+ 轮 lost 全 0 的读数没有监视价值，图把
    唯一会动的信号逐出了图面）。池<5 画点线=低置信（3.19 原始形态），a=0 断口。
    基线口径只管灰线与降档建议：echo 口径才与绿线可比；probe 口径=参照线，
    跨口径不互相打分（探针存活率读数在文字行「探针 x/y」）。
    返回 (每轮值序列, 口径名, 是否与基线可比)。"""
    # 3.32 ④：只丢不摘当轮（drp>0）旧内容已被整条删除，回声测的是删除不是
    # 记忆——置 None 断口，不进滚动重心，免得病态轮次把绿线钉到基线。
    vals = [None if int(r.get("drp") or 0) else _kpi_echo_rate(r) for r in rows]
    name = "回声重心" + ("" if has_base else "·自显")
    return vals, name, bool(has_base and metric == "echo")


def baseline_gate(row: dict, metric: str = "echo") -> bool:
    """进基线的轮次门槛：非退避 · 非只丢不摘 · usage≤10 万 · echo 口径还要池≥5。"""
    if row.get("thr") or row.get("drp"):
        return False
    u = row.get("usage")
    if not u or int(u) > BASELINE_MAX_USAGE:
        return False
    if metric == "echo":
        return (int(row.get("a") or 0) >= BASELINE_MIN_POOL
                and _kpi_echo_rate(row) is not None)
    return _kpi_probe_rate(row) is not None


def _baseline_vals(rows: list, metric: str = "echo"):
    """3.29：门槛筛出的合格值序列——baseline_calc 的原料，也给区分度判定用。
    返回 (vals, 池拦数, 超usage拦数, 退避拦数)。"""
    vals, n_pool, n_usage, n_thr = [], 0, 0, 0
    for r in rows:
        # 3.32 ④：只丢不摘当轮不进基线原料（与 baseline_gate 同口径）。
        if r.get("drp"):
            continue
        if r.get("thr"):
            n_thr += 1
            continue
        u = r.get("usage")
        if not u or int(u) > BASELINE_MAX_USAGE:
            n_usage += 1
            continue
        if metric == "echo" and int(r.get("a") or 0) < BASELINE_MIN_POOL:
            n_pool += 1
            continue
        v = _kpi_echo_rate(r) if metric == "echo" else _kpi_probe_rate(r)
        if v is not None:
            vals.append(v)
    return vals, n_pool, n_usage, n_thr


def baseline_degenerate(vals: list, metric: str = "probe",
                        eps: float = 1e-9) -> bool:
    """3.29：零方差且贴饱和端=没区分度（用户报"21 轮全活恒 100% 是胡扯"）。
    探针全活恒 100%、回声全 0 都属此类——拿它当尺子，任何波动都会被判
    "低于基线"。区间内的稳定值（如恒 0.8）不算退化，仍可用。
    不足 2 轮也判退化（单样本无方差可言）。"""
    if len(vals) < 2:
        return True
    lo, hi = min(vals), max(vals)
    if hi - lo > 1e-6:
        return False
    return lo <= eps or hi >= 1.0 - eps


def baseline_calc(rows: list, metric: str = "echo"):
    """按门槛算基线。返回 (值或 None, 合格轮数, 缺口语)。"""
    vals, n_pool, n_usage, n_thr = _baseline_vals(rows, metric)
    if not vals:
        return None, 0, (f"合格 0 轮：池<{BASELINE_MIN_POOL} 拦 {n_pool} · "
                         f"超 10 万拦 {n_usage} · 退避 {n_thr}")
    return sum(vals) / len(vals), len(vals), ""


def collect_model_baselines(rows: list, led: dict) -> dict:
    """4.1.0：后台按实际模型收集基线（用户定案："不管下拉框怎么选，都在后台
    收集合格信息，数目够了直接生成相应基线"）。

    窗口内出现的每个模型只用自己的行算（绝不混模型平均）；够 20 轮且非退化
    → 写入台账（键=模型名，src=auto）。手动条目（src=manual）永不覆盖；
    值没变不重写（_update_kpi 每 2 秒跑，别刷盘）。
    返回 {模型: (metric, base, n)}——本次新写入/更新的。"""
    models: dict = {}
    for r in rows:
        models.setdefault(kpi_model_of(r), []).append(r)
    changed = {}
    for mdl, rs in models.items():
        ent = led.get(mdl) if isinstance(led.get(mdl), dict) else None
        if ent and str(ent.get("src") or "") == "manual":
            continue                      # 用户手动定的线，后台不动
        m, b, n, _ = baseline_pick(rs)
        if m is None or b is None:
            continue
        new = (m, round(b, 4))
        old = None
        if isinstance(ent, dict) and ent.get("base") is not None:
            try:
                old = (str(ent.get("metric") or ""), round(float(ent["base"]), 4))
            except (TypeError, ValueError):
                old = None
        if old == new:
            continue
        led[mdl] = {"metric": m, "base": new[1], "n": n,
                    "t": time.strftime("%H:%M:%S"), "src": "auto",
                    "gate": (f"池≥{BASELINE_MIN_POOL}·前"
                             f"{BASELINE_MAX_USAGE // 1000}千·非退避")}
        changed[mdl] = (m, new[1], n)
    return changed


def baseline_entry_valid(ent) -> bool:
    """3.29：台账条目有效性。探针口径的 1.0 饱和基线（全活恒 100%）没有
    区分度，直接作废——存量条目也照此判（用户报的那条 100% 即此例），
    同时从基线下拉里摘掉，避免"选了还画"。"""
    if not isinstance(ent, dict) or ent.get("base") is None:
        return False
    try:
        b = float(ent["base"])
    except (TypeError, ValueError):
        return False
    m = str(ent.get("metric") or "echo")
    return not (m == "probe" and b >= 1.0 - 1e-9)


def baseline_from_model(rows: list, model: str):
    """4.2.0（用户定案）：用某模型**全部有效数据**算基线（更新基线用）。
    返回 (grp, metric, base, n, note)；不合格时 metric/base=None，note 说清缺什么。
    退化样本同样拒收（3.29 规则）；组由该模型的映射决定（写入该组）。"""
    rs = [r for r in rows if kpi_model_of(r) == model]
    grp = baseline_group(model)
    if not rs:
        return grp, None, None, 0, "该模型没有数据"
    eb, en, e_why = baseline_calc(rs, "echo")
    if eb is not None and en >= BASELINE_MANUAL_N:
        vals = _baseline_vals(rs, "echo")[0]
        if baseline_degenerate(vals, "echo"):
            return grp, None, None, en, (f"回声率恒 {eb * 100:.0f}%（零方差）"
                                         "→ 无区分度")
        return grp, "echo", eb, en, ""
    pb, pn, p_why = baseline_calc(rs, "probe")
    if pb is not None and pn >= BASELINE_MANUAL_N:
        vals = _baseline_vals(rs, "probe")[0]
        if baseline_degenerate(vals, "probe"):
            return grp, None, None, pn, (f"探针存活率恒 {pb * 100:.0f}%（全活）"
                                         "→ 无区分度")
        return grp, "probe", pb, pn, ""
    why = e_why or p_why
    return grp, None, None, max(en, pn), (
        f"合格样本不足：回声 {en} / 探针 {pn} 轮，最低要 {BASELINE_MANUAL_N} 轮"
        + (f"（{why}）" if why else ""))


def baseline_pick(rows: list):
    """该模型该用哪条基线。返回 (metric, base, n, note)：
      metric 'echo'/'probe'/None；base 在合格样本 <20 轮时一律 None
      （宁可不画灰线，也不画一条兜底公式造出来的假参照）。
      3.29：合格但零方差且贴饱和端（探针全活恒 100%/回声恒 0）同样不出线——
      那不是参照，是天花板。回声退化时仍可继续试探针（它可能有波动）。"""
    eb, en, e_why = baseline_calc(rows, "echo")
    evals = _baseline_vals(rows, "echo")[0]
    if (eb is not None and en >= BASELINE_AUTO_N
            and not baseline_degenerate(evals, "echo")):
        return "echo", eb, en, ""
    pb, pn, p_why = baseline_calc(rows, "probe")
    if pb is not None and pn >= BASELINE_AUTO_N:
        pvals = _baseline_vals(rows, "probe")[0]
        if baseline_degenerate(pvals, "probe"):
            return None, None, pn, (f"探针存活率恒 {pb * 100:.0f}%（全活/零方差）"
                                    "→ 无区分度，不作参照")
        note = (f"回声率合格 {en}/{BASELINE_AUTO_N} 不足 → 基线改用探针存活率"
                if eb is not None
                else f"池<{BASELINE_MIN_POOL} 的轮次不进回声率基线 → 用探针存活率")
        return "probe", pb, pn, note
    if eb is not None or pb is not None:
        _why = e_why or p_why
        return None, None, max(en, pn), (f"合格样本 {max(en, pn)}/{BASELINE_AUTO_N} 不足"
                                        + (f"（{_why}）" if _why else ""))
    return None, None, 0, e_why or p_why or "该模型没有可用轮次"


def baselines_path() -> Path:
    return proxy_dir() / "baselines.json"


def load_baselines() -> dict:
    try:
        d = json.loads(baselines_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_baselines(d: dict) -> None:
    _atomic_write(baselines_path(),
                  json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True))


def _kpi_scale(vals: list, floor: float = 0.25, pct: float = 0.90) -> float:
    """3.17.3：纵轴量程取 90 百分位而不是 max——几根 100% 回声尖峰不该把
    8% 重心压进底部 15px（用户报图判读被压扁）；尖峰由 min(hi,v) 截到顶，
    仍看得见（线贴顶），判档带放大。95 分位实测仍被 3/60 的尖峰绑架，故 90。
    下限 floor 防全零时除零/过度放大。"""
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return floor
    i = min(len(vs) - 1, int(len(vs) * pct))
    return max(floor, vs[i])


def build_root() -> Tk:
    """构建界面并返回 root（不进 mainloop，方便自动化测试）。"""
    root = Tk()
    app = App(root)
    setattr(root, "app", app)
    return root


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        root = Tk()
        root.withdraw()
        app = App(root)
        root.update_idletasks()
        print("profiles:", len(app.profiles))
        for p in app.profiles:
            print(f"  {p['name']!r} conc={p['concurrency']} "
                  f"url={p['base_url'][:60]!r} key={'set' if p['api_key'] else 'empty'}")
        # 3.8 流程1：布局冒烟——所有按钮必须在 ScrollColumn 内，否则出包即失败。
        # 这类"底部行被窗口裁掉"的 bug 今天由用户报了 4 次，必须进自动闸门。
        bad = []

        def _walk(w):
            for c in w.winfo_children():
                if isinstance(c, ttk.Button):
                    a = c.master
                    inside = False
                    while a is not None:
                        if isinstance(a, ScrollColumn):
                            inside = True
                            break
                        a = getattr(a, "master", None)
                    if not inside:
                        bad.append(str(c.cget("text"))[:20])
                _walk(c)

        _walk(root)
        root.destroy()
        if bad:
            print("LAYOUT FAIL: 按钮不在 ScrollColumn 内:", bad)
            return 1
        print("layout smoke: 全部按钮在滚动容器内")
        return 0
    if not acquire_single_instance():
        _r = Tk()
        _r.withdraw()
        messagebox.showwarning(
            "longctx GUI",
            "已有一个 longctx GUI 实例在运行。\n多开会用旧内存状态覆盖配置"
            "（任务模型切换会被回滚），请用已有窗口。")
        _r.destroy()
        return 1
    root = build_root()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
