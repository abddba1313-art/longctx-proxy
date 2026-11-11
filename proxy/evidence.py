#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""evidence.py — 证据分层与确定性补录（P1）。

LLMLingua-2 用分类器挑 token；这里不训练分类器，而是把“压缩时最容易丢、
丢了就答错”的内容显式分成两层，用确定性规则在摘要之后补录原文：

  T1 标识：环境变量、命令行参数、错误码、文件名:行号、哈希、带单位数值、
           日期、短代码片段。数字/URL/路径仍由 proxy.extract_facts 负责，
           这里只补它没覆盖的部分。
  T2 约束/定义：含“如果/仅当/除非/必须/不得/至少/最多/否则”的句子，
           以及“X 是指/称为/简称/定义为”这类指代锚点。这类句子一旦被
           改写成“有条件地执行”，条件和否定就丢了。

补录块放在摘要末尾，逐字引用原文，不再经过模型；已出现在摘要里的条目不重复补。
T1 先用少量原文窗口补高价值标识，剩余标识压成一行清单，保证逐字出现但不撑大摘要。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- T1 标识

_ID_RES = (
    # 文件名 + 可选行号：app.py:42、src/main.ts:7:3
    ("file", re.compile(
        r"(?<![\w./\\-])[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|"
        r"c|h|cc|cpp|hpp|cs|rb|php|swift|sh|ps1|bat|cmd|json|jsonl|ya?ml|"
        r"toml|ini|cfg|env|md|txt|log|exe|dll|so|bin|gguf|safetensors|onnx)"
        r"(?::\d+(?::\d+)?)?\b", re.I)),
    # 环境变量 / 常量：PROXY_PREFIX_STRICT、MAX_RETRY
    ("env", re.compile(r"\b[A-Z][A-Z0-9]{2,}_[A-Z0-9_]+\b")),
    # 命令行参数：--onefile、--max-tokens
    ("flag", re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{1,}")),
    # 错误码：E1234、ERR_5001、HTTP 404、ORA-00942
    ("code", re.compile(
        r"\b(?:E|ERR|WARN|HTTP|HTTPS|SQL|ORA|K8S|AWS)[-_ ]?\d{2,5}\b")),
    # 提交哈希 / md5 前缀（必须含字母，避免和普通数字混淆）
    ("hash", re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)),
    # 带单位数值：12.5GB、300ms、4.2%、16k tokens、20 层
    ("measure", re.compile(
        r"\d+(?:\.\d+)?\s*(?:GB|GiB|MB|MiB|KB|KiB|TB|TiB|ms|us|µs|ns|"
        r"min|h|px|dpi|fps|tok/s|tokens?|%|秒|毫秒|分钟|小时|天|次|层|张|"
        r"路|核|线程)(?![A-Za-z0-9])", re.I)),
    # 日期：2026-11-03、2026/11/03
    ("date", re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")),
    # 反引号里的短代码/字段名：`foo()`、`content`；跳过带空格的散文
    ("code_span", re.compile(r"`[^`\n]{1,60}`")),
    # 限定调用名：os.path.join()、client.complete()
    ("call", re.compile(r"\b(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*\(\)")),
)

# 排序权重：环境变量/参数/错误码/带行号文件最值钱；普通文件名和度量其次。
_KIND_WEIGHT = {
    "env": 3.2, "flag": 3.2, "code": 3.2, "date": 2.6, "hash": 2.6,
    "file": 2.2, "call": 1.8, "measure": 1.3, "code_span": 1.0,
}

_JUNK = "`*（(）)]}>,，。；：、|'\""


def _clean(s: str) -> str:
    s = s.strip().rstrip(".,;:")
    while s and s[-1] in _JUNK:
        s = s[:-1].rstrip(".,;:")
    return s


def _code_span_ok(body: str) -> bool:
    """反引号内容只留像代码/标识符的，跳过整句散文。"""
    body = body.strip()
    if not body or len(body) > 60:
        return False
    if len(body.split()) > 3:
        return False
    return bool(re.search(r"[_./\\:()\[\]<>=-]", body) or re.fullmatch(r"[\w-]+", body))


def extract_identifier_items(text: str, limit: int = 1200) -> list[tuple[str, str]]:
    """按出现顺序返回去重后的 [(标识, 类型)]。"""
    seen, out = set(), []
    for kind, rx in _ID_RES:
        for m in rx.finditer(text):
            raw = m.group(0)
            if kind == "code_span":
                raw = raw.strip("`")
                if not _code_span_ok(raw):
                    continue
            f = _clean(raw)
            if not f:
                continue
            # 同一标识的大小写/斜杠变体只留第一次出现的写法。
            key = f.lower().replace("/", "\\")
            if key in seen:
                continue
            seen.add(key)
            out.append((f, kind))
            if len(out) >= limit:
                return out
    return out


def extract_identifiers(text: str, limit: int = 1200) -> list[str]:
    return [v for v, _k in extract_identifier_items(text, limit)]


# ---------------------------------------------------------------- T2 约束/定义

_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")

_CONS_STRONG = (
    "必须", "不得", "禁止", "严禁", "不准", "不允许", "仅当", "除非", "否则",
    "至少", "最多", "不超过", "不少于", "不大于", "不小于", "除外", "前提",
    "务必", "只能", "仅限于", "必须不", "不可", "不能", "不应",
)
_CONS_WEAK = ("如果", "无需", "无须", "不必")
_WEAK_RE = re.compile(r"若(?!干)|只有[^。；]{0,20}(?:才|方可|方能)")
_DEF = (
    "是指", "指的是", "定义为", "称为", "简称", "全称", "别名", "代号",
    "命名为", "也就是", "即指", "特指",
)
_EN_CONS = re.compile(
    r"\b(must|shall|must not|may not|cannot|can't|required?|at least|at most|"
    r"no more than|not allowed|prohibited|only if|unless|except|provided that|"
    r"defaults? to)\b", re.I)
_EN_DEF = re.compile(r"\b(is defined as|refers to|also known as|aka|stands for)\b",
                     re.I)
# 文档里夹带的模型内心独白/追问，不是原文约束，补录只会增加噪声。
_NOISE = re.compile(
    r"^(let me|let's|i |i'll|i'm|we |we'll|the user|user said|hmm|okay|"
    r"looking at|now |next,|wait,)", re.I)
# 第一人称评价/自问自答不是约束；真正的“你必须/不得”仍保留。
_OPINION = re.compile(r"(我是|我觉得|我认为|我不|我记得|我想|我感觉|"
                      r"能不能|是不是|好不好|行不行)")


def _sentence_kind(s: str) -> str | None:
    if any(k in s for k in _DEF) or _EN_DEF.search(s):
        return "def"
    if any(k in s for k in _CONS_STRONG) or _EN_CONS.search(s):
        return "cons"
    if any(k in s for k in _CONS_WEAK) or _WEAK_RE.search(s):
        return "cons"
    return None


def extract_constraints(text: str, limit: int = 400) -> list[tuple[str, str]]:
    """返回 [(句子, 'cons'|'def')]，按原文顺序，已去重。"""
    seen, out = set(), []
    for raw in _SPLIT_RE.split(text):
        s = raw.strip().lstrip("-*+> \t#0123456789.、)）").strip()
        if len(s) < 6:
            continue
        if _NOISE.match(s) and not re.search(r"[一-鿿]", s):
            continue
        # 问句不是约束；第一人称评价/“能不能”也不是。
        if re.search(r"[？?]\s*$", s):
            continue
        if _OPINION.search(s) and not any(
                k in s for k in ("必须", "不得", "禁止", "严禁", "仅当",
                                 "除非", "至少", "最多", "不超过", "除外")):
            continue
        kind = _sentence_kind(s)
        if not kind:
            continue
        s = " ".join(s.split())
        if len(s) > 240:
            s = s[:240].rstrip() + "…"
        key = re.sub(r"\s+", "", s)
        if key in seen:
            continue
        seen.add(key)
        out.append((s, kind))
        if len(out) >= limit:
            break
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s.lower())


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 2 else {s}


def _similar(a: str, b: str, threshold: float = 0.60) -> bool:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return False
    return len(A & B) / len(A) >= threshold


def _covered(sentence: str, summary: str) -> bool:
    """摘要里已有整句，或字符二元组重合度 >= 0.62，就不再补。"""
    ns, nsum = _norm(sentence), _norm(summary)
    if not ns:
        return True
    if ns in nsum:
        return True
    if len(ns) < 12:
        return False
    a, b = _bigrams(sentence), _bigrams(summary)
    if not a:
        return True
    return len(a & b) / len(a) >= 0.62


def count_evidence(text: str) -> int:
    """估算一段文本里的证据条数，用于密度预算。"""
    n_ids = len(extract_identifier_items(text))
    n_cons = len(extract_constraints(text, limit=200))
    return n_ids + n_cons


# ---------------------------------------------------------------- 原文窗口

# 与 proxy.py 的同名常量成对：proxy 可能导入不到 evidence，两边各留一份。
ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/\\-+")
SNAP_MAX = 40       # 边界吸附最多外扩多少字符
TAIL_SLACK = 80     # 行尾只剩这么多字符就带到行尾（结论常在行尾）


def snap_edges(source: str, a: int, b: int,
               line_start: int, line_end: int) -> tuple[int, int]:
    """把窗口两端推到词/标识符边界，绝不把单词、路径、哈希拦腰截断。"""
    lim_a = max(line_start, a - SNAP_MAX)
    while a > lim_a and source[a] in ID_CHARS and source[a - 1] in ID_CHARS:
        a -= 1
    lim_b = min(line_end, b + SNAP_MAX)
    while b < lim_b and source[b - 1] in ID_CHARS and source[b] in ID_CHARS:
        b += 1
    if line_end - b <= TAIL_SLACK:
        b = line_end
    return a, b


def _window(source: str, start: int, end: int,
            radius: int = 40) -> tuple[int, int]:
    """事实周围最小可读片段；表格整行保留，其余向句读靠拢。"""
    line_start = source.rfind("\n", 0, start) + 1
    line_end = source.find("\n", end)
    if line_end < 0:
        line_end = len(source)
    line = source[line_start:line_end]
    if line.count("|") >= 2:
        return line_start, line_end
    a = max(line_start, start - radius)
    b = min(line_end, end + radius)
    for ch in ("。", "；", "！", "？", "\n"):
        j = source.rfind(ch, max(line_start, a - 20), a)
        if j >= 0:
            a = j + 1
            break
    for ch in ("。", "；", "！", "？", "\n"):
        j = source.find(ch, b, min(line_end, b + 20))
        if j >= 0:
            b = j + 1
            break
    return snap_edges(source, a, b, line_start, line_end)


def _score(sentence: str, kind: str, ids: list[str]) -> float:
    s = 2.0 if kind == "cons" else 1.5
    s += sum(0.6 for k in _CONS_STRONG if k in sentence)
    s += 0.3 * sum(1 for i in ids if i in sentence)
    return s


def _clean_line(s: str, max_chars: int = 220) -> str:
    s = " ".join(s.split())
    if len(s) > max_chars:
        cut = max_chars
        while cut > 1 and s[cut] in ID_CHARS and s[cut - 1] in ID_CHARS:
            cut -= 1                      # 别把单词/路径/哈希拦腰截断
        if cut < max_chars - 60:
            cut = max_chars               # 整段就是一个长标识符，回退没有意义
        s = s[:cut].rstrip() + "…"
    return s


def _est_tokens(s: str) -> int:
    """预算估算：CJK 约 1 字 1 token，其余约 4 字符 1 token。
    比 len//3 准，避免中文补录块实际体积是估算的 2~3 倍。"""
    cjk = len(re.findall(r"[一-鿿]", s))
    return max(1, cjk + (len(s) - cjk) // 4)


def backfill_evidence(source: str, summary: str, budget_tokens: int,
                      radius: int = 40) -> tuple[str, dict]:
    """把摘要里缺失的 T1 标识和 T2 约束/定义逐字补回。

    budget_tokens 是新补内容的硬上限；返回 (新摘要, 统计)。
    """
    items = extract_identifier_items(source)
    missing = [(v, k) for v, k in items if v not in summary]
    constraints = extract_constraints(source)
    missing_cons = [(s, k) for s, k in constraints if not _covered(s, summary)]

    stats = {
        "ids_total": len(items), "ids_missing": len(missing),
        "cons_total": len(constraints), "cons_missing": len(missing_cons),
        "ids_added": 0, "cons_added": 0, "tokens": 0,
    }
    if not missing and not missing_cons:
        return summary, stats

    pos = {v: source.find(v) for v, _k in missing}
    ranked_ids = sorted(
        missing,
        key=lambda vk: (-_KIND_WEIGHT.get(vk[1], 1.0), pos.get(vk[0], 10 ** 9)))
    # T1 用 55% 预算：先给高价值标识补原文窗口，其余压成一行清单。
    t1_cap = int(budget_tokens * 0.55)
    picked: list[tuple[int, str]] = []
    used = 0
    added: set[str] = set()
    seen_snips: list[str] = []

    for v, _kind in ranked_ids:
        if used >= t1_cap or len(added) >= 16:
            break
        if v in added:
            continue
        i = pos.get(v, -1)
        if i < 0:
            continue
        # 标识窗口比事实窗口宽：一行里常并列 5~10 个参数，一次窗口全收进来，
        # 后面同类标识就不会各补一段。
        a, b = _window(source, i, i + len(v), max(radius, 120))
        if b - a > 420:                       # 长段落只留标识附近
            a, b = max(a, i - 80), min(b, i + len(v) + 260)
            ls = source.rfind("\n", 0, i) + 1
            le = source.find("\n", i + len(v))
            a, b = snap_edges(source, a, b, ls, le if le >= 0 else len(source))
        snippet = _clean_line(source[a:b], 420)
        ns = re.sub(r"\s+", "", snippet)
        if any(ns == x or ns in x or _similar(ns, x) for x in seen_snips):
            added.add(v)                      # 同一行的参数，窗口已经覆盖
            continue
        t = _est_tokens(snippet)
        if used + t > t1_cap:
            break
        picked.append((a, f"- [T1] …{snippet}…"))
        used += t
        seen_snips.append(ns)
        # 一个窗口里出现的其他标识也算补上了，避免同一行参数重复补 10 次。
        added.update(v2 for v2, _k2 in ranked_ids if v2 in snippet)

    rest = [v for v, _k in ranked_ids if v not in added]
    if rest:
        line = "- [T1 清单] " + "、".join(rest)
        room = max(0, t1_cap - used)
        if room >= 20:
            line = _clean_line(line, max(20, room * 4))
            # 截断时别切在标识中间
            if line.endswith("…"):
                cut = line.rfind("、")
                if cut > 12:
                    line = line[:cut] + "…"
            picked.append((len(source), line))
            used += _est_tokens(line)
            stats["ids_added"] += sum(1 for v in rest if v in line)
    stats["ids_added"] += len(added)

    ranked_cons = [(source.find(s), s, k) for s, k in missing_cons]
    ranked_cons = [x for x in ranked_cons if x[0] >= 0]
    ranked_cons.sort(key=lambda x: -_score(x[1], x[2], [v for v, _ in missing]))
    t1_text = "\n".join(line for _p, line in picked)
    for p, s, k in ranked_cons:
        if used >= budget_tokens:
            break
        if _covered(s, t1_text):          # T1 窗口已经逐字包含这句
            continue
        t = _est_tokens(s)
        if used + t > budget_tokens:
            continue
        label = "T2 约束" if k == "cons" else "T2 定义"
        picked.append((p, f"- [{label}] {s}"))
        used += t
        stats["cons_added"] += 1

    if not picked:
        return summary, stats

    picked.sort(key=lambda x: x[0])
    block = "\n".join(line for _pos, line in picked)
    new = summary.rstrip() + "\n\n【证据补录·逐字核对】\n" + block
    stats["tokens"] = used
    return new, stats
