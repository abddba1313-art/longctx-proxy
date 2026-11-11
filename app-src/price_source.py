# -*- coding: utf-8 -*-
"""从服务商公开价目页读取分时段价格。

只发免费的网页 GET 请求，不调用推理接口，不消耗 token。
支持：
  - DeepSeek 官方中文价目页（元 / 百万 token）
  - 阿里云百炼「模型调用计费」页（华北 2 北京，元 / 百万 token）

也可以让用户直接给一个价目页链接；链接是唯一的来源判断依据，
避免把百炼的 deepseek-* 模型误当成 DeepSeek 官方价。

注意：DeepSeek 官方高峰时段还区分「周一至周五、非法定节假日」，而本程序的
价目表只支持每日时段。这里按每日时段近似，周末与节假日会按高峰计（偏保守）。
"""

from __future__ import annotations

import re
import urllib.request
from html import unescape
from urllib.parse import urlparse

DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
BAILIAN_PRICING_URL = "https://help.aliyun.com/zh/model-studio/model-pricing"
USER_AGENT = "Mozilla/5.0 (longctx-proxy-gui pricing reader)"


def source_kind(url: str) -> str:
    """按价目页链接判断解析器：'deepseek' / 'bailian' / ''（不支持）。"""
    low = (url or "").lower()
    if "deepseek" in low:
        return "deepseek"
    if "aliyun" in low or "bailian" in low:
        return "bailian"
    return ""


def resolve_price_source(api_url: str, pricing_url: str = "") -> tuple[str, str]:
    """返回 (解析器类型, 价目页链接)。

    填了「价目链接」就只认它；留空时按 API 链接推断：
    api.deepseek.com → DeepSeek 官方，aliyuncs.com / aliyun.com → 百炼。
    推断不出返回 ("", "")。
    """
    link = (pricing_url or "").strip()
    if link:
        return source_kind(link), link
    host = (urlparse((api_url or "").strip()).hostname or "").lower()
    if "deepseek.com" in host:
        return "deepseek", DEEPSEEK_PRICING_URL
    if "aliyuncs.com" in host or "aliyun.com" in host or "bailian" in host:
        return "bailian", BAILIAN_PRICING_URL
    return "", ""


def _cell_text(cell: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", cell)).strip()


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _off_windows(peaks: list[tuple[str, str]], prices: dict) -> list[dict]:
    """peaks 已按起点排序；返回它们之间的空闲窗口（含跨零点）。"""
    out: list[dict] = []
    cursor = 0
    for start, end in peaks:
        s, e = _minutes(start), _minutes(end)
        if cursor < s:
            out.append({
                "start": _hhmm(cursor), "end": _hhmm(s),
                "input": prices["input_off"], "output": prices["output_off"],
                "cache_read": prices["cache_read_off"],
            })
        cursor = e
    if cursor < 24 * 60:
        out.append({
            "start": _hhmm(cursor), "end": "00:00",
            "input": prices["input_off"], "output": prices["output_off"],
            "cache_read": prices["cache_read_off"],
        })
    return out


def parse_deepseek_pricing(html: str, model: str = "deepseek-flash") -> dict:
    """从 DeepSeek 中文价目页 HTML 解析指定模型的分时段价目。

    返回 {name, unit, windows, source, note}。解析失败抛 ValueError。
    """
    low = (model or "").lower()
    if "deepseek" not in low:
        raise ValueError(f"不支持的模型：{model!r}")
    is_pro = "pro" in low

    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [_cell_text(td)
                 for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if cells:
            rows.append(cells)

    peaks: dict[str, float] = {}
    current: str | None = None
    for cells in rows:
        blob = " ".join(cells)
        if "缓存命中" in blob and "未命中" not in blob:
            current = "cache_read"
        elif "缓存未命中" in blob or ("输入" in blob and "未命中" in blob):
            current = "input"
        elif "输出" in blob and "tokens" in blob:
            current = "output"
        if current is None:
            continue
        period = "off" if "空闲时段" in blob else "peak" if "高峰时段" in blob else ""
        if not period:
            continue
        amounts = [c for c in cells if "元" in c]
        if len(amounts) < 2:
            continue
        raw = amounts[-2] if not is_pro else amounts[-1]
        m = re.search(r"\d+(?:\.\d+)?", raw)
        if not m:
            continue
        key = current if period == "peak" else f"{current}_off"
        peaks[key] = float(m.group())

    needed = ("input", "output", "cache_read",
              "input_off", "output_off", "cache_read_off")
    missing = [k for k in needed if k not in peaks]
    if missing:
        raise ValueError(f"价目页里没找到这些价格：{', '.join(missing)}")

    text = _cell_text(html)
    hours = re.findall(
        r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", text)
    peak_hours = sorted({(h, m) for h, m in hours if h != m},
                        key=lambda p: _minutes(p[0]))
    peak_hours = [(h, m) for h, m in peak_hours if _minutes(h) < _minutes(m)]
    if not peak_hours:
        raise ValueError("价目页里没找到高峰时段")

    peak_windows = []
    for start, end in peak_hours:
        peak_windows.append({
            "start": _hhmm(_minutes(start)), "end": _hhmm(_minutes(end)),
            "input": peaks["input"], "output": peaks["output"],
            "cache_read": peaks["cache_read"],
        })
    windows = peak_windows + _off_windows(peak_hours, peaks)
    label = "deepseek-v4-pro" if is_pro else "deepseek-flash"
    return {
        "name": f"DeepSeek {label}（官方价）",
        "unit": "元",
        "windows": windows,
        "source": DEEPSEEK_PRICING_URL,
        "note": ("官方高峰为北京时间周一至周五 09:00-12:00、14:00-18:00"
                 "（不含法定节假日）；本表按每日时段近似，周末/节假日按高峰计。"),
    }


def fetch_deepseek_pricing(model: str = "deepseek-flash",
                           url: str = DEEPSEEK_PRICING_URL,
                           timeout: float = 15.0) -> dict:
    """读取官方中文价目页并解析；只发网页 GET，不消耗 token。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "ignore")
    return parse_deepseek_pricing(html, model)


# ---------------------------------------------------------------- 百炼
def _bailian_rows(html: str, model: str) -> list[list[str]]:
    """取出华北 2（北京）里该模型的中国内地价格行。

    页面按地域分节，模型同名行还会出现在「全球 / 美国」部署范围里；
    这里只保留模型单元格精确等于 model、且不带部署范围标记的行。
    """
    low = html.lower()
    needle = model.lower()
    pos = low.find(f">{needle}<")
    if pos < 0:
        pos = low.find(needle)
    if pos < 0:
        raise ValueError(f"百炼价目页里没找到模型 {model!r}")
    start = html.rfind("华北2-北京", 0, pos)
    if start < 0:
        raise ValueError("百炼价目页结构变了：没找到「华北 2（北京）」分节")
    end = html.find("新加坡", pos)
    section = html[start:end if end > 0 else len(html)]

    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", section, re.S | re.I):
        cells = [_cell_text(td)
                 for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if not cells:
            continue
        # 模型 ID 后面紧跟「上下文缓存享有折扣」，中间可能没有分隔符，
        # 所以只取开头的 ASCII 标识符，不能整段比较。
        m = re.match(r"[A-Za-z0-9._-]+", cells[0].lstrip())
        head = m.group(0).lower() if m else ""
        if head != needle:
            continue
        if any(c.strip() in ("全球", "美国") for c in cells[1:]):
            continue
        if len([c for c in cells[1:] if "元" in c]) >= 2:
            rows.append(cells)
    if not rows:
        raise ValueError(
            f"百炼华北 2（北京）价目里没找到 {model!r} 的输入/输出价")
    return rows


def parse_bailian_pricing(html: str, model: str = "deepseek-v4.1-flash",
                          token_plan: bool = False) -> dict:
    """从百炼「模型调用计费」页解析华北 2（北京）的分时段价目。

    忙时 08:00-22:00、闲时 22:00-08:00（北京时间）。模型不分忙闲时
    返回一条 00:00-00:00 的全天窗口。解析失败抛 ValueError。
    """
    model = (model or "deepseek-v4.1-flash").strip()
    cells = _bailian_rows(html, model)[0]
    price_cells = [c for c in cells[1:] if "元" in c]
    in_cell, out_cell = price_cells[0], price_cells[1]

    busy_in = re.search(r"忙时\s*([\d.]+)\s*元", in_cell)
    busy_out = re.search(r"忙时\s*([\d.]+)\s*元", out_cell)
    idle_in = re.search(r"闲时\s*([\d.]+)\s*元", in_cell)
    idle_out = re.search(r"闲时\s*([\d.]+)\s*元", out_cell)

    def window(start: str, end: str, pin: float, pout: float) -> dict:
        # 页面说明：缓存命中按输入 10%、显式缓存创建按 125% 计。
        return {
            "start": start, "end": end,
            "input": pin, "output": pout,
            "cache_read": round(pin * 0.10, 6),
            "cache_write": round(pin * 1.25, 6),
            "cache_read_explicit": round(pin * 0.10, 6),
        }

    if busy_in and busy_out and idle_in and idle_out:
        windows = [
            window("08:00", "22:00", float(busy_in.group(1)),
                   float(busy_out.group(1))),
            window("22:00", "08:00", float(idle_in.group(1)),
                   float(idle_out.group(1))),
        ]
        note = ("百炼按量价（华北 2 北京）：忙时 08:00-22:00、"
                "闲时 22:00-08:00（北京时间）。缓存价按页面通用规则估算："
                "命中按输入 10%、显式创建按 125%；以账单为准。")
    else:
        pin = float(re.search(r"([\d.]+)\s*元", in_cell).group(1))
        pout = float(re.search(r"([\d.]+)\s*元", out_cell).group(1))
        windows = [window("00:00", "00:00", pin, pout)]
        note = "百炼按量价（华北 2 北京）：该模型不分忙闲时，全天同价。"

    if token_plan:
        note = ("Token Plan 实际按 Credits 扣费（夜间 22:00-08:00 对部分 "
                "DeepSeek 模型 5 折）；这里用同模型按量价折算，只用于比较"
                "时段和相对成本，不是账单金额。" + note)

    return {
        "name": f"百炼 {model}" + ("（Token Plan 折算）" if token_plan
                                   else "（按量价）"),
        "unit": "元",
        "windows": windows,
        "source": BAILIAN_PRICING_URL,
        "note": note,
    }


def fetch_bailian_pricing(model: str = "deepseek-v4.1-flash",
                          url: str = BAILIAN_PRICING_URL,
                          token_plan: bool | None = None,
                          timeout: float = 15.0) -> dict:
    """读取百炼公开价目页并解析；只发网页 GET，不消耗 token。

    token_plan 为 None 时按价目页链接推断；调用方知道 API 是 Token Plan
    时应显式传 True，因为价目页本身是公开的按量价页。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "ignore")
    if token_plan is None:
        token_plan = "token-plan" in url.lower()
    return parse_bailian_pricing(html, model, token_plan=token_plan)
