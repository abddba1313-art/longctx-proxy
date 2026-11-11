#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""确定性的工具输出压缩（不调模型）。

设计原则（来自 sqz / caveman / LongLLMLingua 的实践）：
  * 只压“可判定”的内容：pytest/npm/cargo/tsc/git/docker/kubectl/日志/JSON；
  * 源码、diff、traceback、密钥、二进制一律原样通过；
  * 失败用例、断言、file:line、错误码、汇总行必须保留；
  * 输出比输入短不到 8% 就不改，避免为了压而压。

对外只暴露 format_tool_output()，返回 (新文本, 类型) 或 None。
"""

from __future__ import annotations

import json
import re

MIN_SAVE = 0.08

_SECRET_RES = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghu|ghs|github_pat|xox[baprs])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]

_CODE_LINE = re.compile(
    r"^\s*(?:def |class |async def |import |from \S+ import |#include|package |"
    r"public |private |protected |static |function |const |let |var |func |fn |"
    r"impl |struct |enum |interface |type |return |if\s*\(|for\s*\(|while\s*\(|"
    r"switch\s*\(|try\s*\{|catch\s*\()"
)
_DIFF_RE = re.compile(
    r"^(?:diff --git |index [0-9a-f]+\.\.|--- a/|\+\+\+ b/|@@ -\d+)", re.M
)
_TRACEBACK_RE = re.compile(r'^\s*File ".*", line \d+', re.M)
_TIMESTAMP_RE = re.compile(
    r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|"
    r"\d{2}:\d{2}:\d{2}[.,]\d{3}|[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2})"
)

_ERROR_WORD_RE = re.compile(
    r"(?i)\b(?:error|errors|fatal|critical|panic|exception|traceback|failed|failure|"
    r"timeout|timed out|refused|denied|unreachable|unhealthy|crashloopbackoff|"
    r"imagepullbackoff|oomkilled|evicted|no such|not found|cannot|can't|unable)\b"
)


def _is_secret(text: str) -> bool:
    return any(rx.search(text) for rx in _SECRET_RES)


def _is_binary(text: str) -> bool:
    return "\x00" in text[:4096]


def _looks_like_source(text: str) -> bool:
    if text.lstrip().startswith(("<!DOCTYPE", "<html", "<?xml")):
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    code = sum(1 for ln in lines if _CODE_LINE.search(ln))
    return code >= 5 and code / len(lines) >= 0.12


def _looks_like_diff(text: str) -> bool:
    return len(_DIFF_RE.findall(text)) >= 2


def _looks_like_traceback(text: str) -> bool:
    return ("Traceback (most recent call last)" in text
            or bool(_TRACEBACK_RE.search(text)))


def _pairs_no_dup(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate JSON key")
        out[k] = v
    return out


def _json_load(text: str):
    s = text.strip()
    if len(s) < 120 or s[0] not in "{[":
        return None
    try:
        return json.loads(s, object_pairs_hook=_pairs_no_dup)
    except Exception:
        return None


def _format_json(text: str) -> str | None:
    obj = _json_load(text)
    if obj is None:
        return None
    out = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if len(out) >= len(text.strip()) * 0.97:
        return None
    return out


def _keep_pytest(line: str) -> bool:
    s = line.rstrip()
    if not s:
        return False
    if re.fullmatch(r"[.FEsx]+", s.strip()):
        return False
    if re.match(r"^(?:PASSED|SKIPPED|XFAIL|XPASS)\b", s):
        return False
    if s.startswith("=") and any(k in s for k in (
            "FAILURES", "ERRORS", "short test summary", "test session starts",
            "warnings summary", "passed", "failed", "error")):
        return True
    if s.startswith(("FAILED ", "ERROR ")):
        return True
    if re.match(r"^\s*(?:def test_|async def test_|assert\b)", s):
        return True
    if re.match(r"^E\s+", s) or re.match(r"^>\s+", s):
        return True
    if re.match(r"^_{5,}", s):
        return True
    if re.search(r'File ".*", line \d+', s):
        return True
    if re.match(r"^\S+\.py:\d+(?::\d+)?: ", s):
        return True
    if re.search(r"\b(?:AssertionError|Exception|Error)\b", s):
        return True
    if re.match(r"^(?:rootdir|plugins|collected|platform|configfile):", s):
        return True
    if re.search(r"\b\d+ (?:passed|failed|error|errors|skipped|deselected|warnings?)\b", s):
        return True
    return False


def _format_pytest(text: str) -> str | None:
    kept = [ln for ln in text.splitlines() if _keep_pytest(ln)]
    if not kept:
        return None
    return "\n".join(kept)


def _format_tail(text: str, keep: re.Pattern[str], tail: int = 20) -> str | None:
    lines = text.splitlines()
    kept = [ln for ln in lines if keep.search(ln)]
    if len(kept) < 3:
        kept = lines[-tail:]
    elif len(lines) > len(kept) + 3:
        kept = kept + lines[-3:]
    if not kept:
        return None
    return "\n".join(kept)


def _format_git_status(text: str) -> str | None:
    lines = []
    for ln in text.splitlines():
        s = ln.rstrip()
        if not s:
            continue
        if s.lstrip().startswith("(use \"git"):
            continue
        lines.append(s)
    if not lines:
        return None
    return "\n".join(lines)


def _format_table_tail(text: str, problem: re.Pattern[str], tail: int = 10) -> str | None:
    lines = text.splitlines()
    if len(lines) <= 60:
        return None                     # 小表不动，免得丢行
    kept = [ln for ln in lines[:2] if ln.strip()]
    kept += [ln for ln in lines[2:] if problem.search(ln)]
    kept += lines[-tail:]
    out = []
    for ln in kept:
        if ln not in out:
            out.append(ln)
    return "\n".join(out) or None


def _format_generic(text: str) -> str | None:
    lines = text.splitlines()
    if len(lines) < 20:
        return None
    has_signal = bool(_ERROR_WORD_RE.search(text)) or bool(_TIMESTAMP_RE.search(text))
    if not has_signal:
        return None
    head = lines[:6]
    tail = lines[-30:]
    body = [ln for ln in lines[6:-30] if _ERROR_WORD_RE.search(ln)]
    out = []
    for ln in head + body + tail:
        out.append(ln)
    return "\n".join(out) or None


def detect_kind(text: str, tool_name: str = "", command: str = "") -> str:
    hay = f"{tool_name} {command}".lower()
    if _is_secret(text):
        return "secret"
    if _is_binary(text):
        return "binary"
    if _looks_like_diff(text):
        return "diff"
    # 先认明确的工具报告，再判源码：pytest 失败输出里也会出现 def/assert。
    if "pytest" in hay or "= test session starts =" in text or "= FAILURES =" in text \
            or "short test summary info" in text:
        return "pytest"
    if re.search(r"(?:^|\s)npm(?:\s|$)", hay) or "npm ERR!" in text or "npm error" in text:
        return "npm"
    if "cargo" in hay or re.search(r"^error\[E\d+\]", text, re.M):
        return "cargo"
    if "tsc" in hay or re.search(r"error TS\d+", text):
        return "tsc"
    if "git status" in hay or re.search(r"^On branch .+", text, re.M):
        return "git-status"
    if "kubectl" in hay:
        return "kubectl"
    if "docker" in hay:
        return "docker"
    if _looks_like_traceback(text):
        return "traceback"
    if _looks_like_source(text):
        return "source"
    if _json_load(text) is not None:
        return "json"
    if "journalctl" in hay or re.search(r"(?:^|\s)tail\s", hay):
        return "logs"
    if _TIMESTAMP_RE.search(text) or len(_ERROR_WORD_RE.findall(text)) >= 3:
        return "logs"
    return "generic"


def _format_kind(kind: str, text: str) -> str | None:
    if kind == "json":
        return _format_json(text)
    if kind == "pytest":
        return _format_pytest(text)
    if kind == "npm":
        return _format_tail(text, re.compile(
            r"npm (?:ERR!|error|WARN)|ERR!|EACCES|ENOENT|added \d+ packages|"
            r"found \d+ vulnerabilities|audited \d+"), 15)
    if kind == "cargo":
        return _format_tail(text, re.compile(
            r"^(?:error|warning:)|-->|^\s*\d*\s*\||^\s+\^|test result:|failures:"), 20)
    if kind == "tsc":
        return _format_tail(text, re.compile(
            r"error TS\d+|^\S+\(\d+,\d+\): error|Found \d+ error|^\s+\d+ |^\s+~"), 20)
    if kind == "git-status":
        return _format_git_status(text)
    if kind == "docker":
        return _format_table_tail(text, re.compile(
            r"(?i)error|failed|unhealthy|exit|restarting|no such|cannot|denied|panic"))
    if kind == "kubectl":
        return _format_table_tail(text, re.compile(
            r"(?i)error|warning|failed|crashloopbackoff|imagepullbackoff|oomkilled|"
            r"evicted|unhealthy|not ready|pending"))
    if kind == "logs":
        return _format_generic(text)
    if kind == "generic":
        return _format_generic(text)
    return None


def fold_duplicate_lines(text: str) -> str:
    """只折叠“连续完全相同”的行，避免把不同时间戳/不同数值的行误合并。"""
    out: list[str] = []
    prev = None
    count = 0
    for line in text.splitlines():
        if line == prev:
            count += 1
            continue
        if prev is not None:
            out.append(prev + (f" [×{count}]" if count > 1 else ""))
        prev = line
        count = 1
    if prev is not None:
        out.append(prev + (f" [×{count}]" if count > 1 else ""))
    return "\n".join(out)


def compact_table(text: str) -> str:
    """去掉 markdown 表格单元格两侧的空格；不改变单元格内容。"""
    out = []
    changed = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 3:
            new = re.sub(r"\s*\|\s*", "|", s)
            if len(new) < len(line):
                changed = True
            out.append(new)
        else:
            out.append(line)
    return "\n".join(out) if changed else text


def tidy(text: str) -> str:
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_tool_output(text: str, tool_name: str = "",
                       command: str = "") -> tuple[str, str] | None:
    """把工具输出压成更短但事实完整的文本。

    返回 (新文本, 类型)；不改（源码/diff/traceback/密钥/纯散文/收益不足）时返回 None。
    """
    if not text or len(text) < 300:
        return None
    kind = detect_kind(text, tool_name, command)
    if kind in ("source", "diff", "traceback", "secret", "binary"):
        return None
    new = _format_kind(kind, text)
    if new is None:
        return None
    new = fold_duplicate_lines(new)
    new = compact_table(new)
    new = tidy(new)
    if not new or len(new) >= len(text) * (1 - MIN_SAVE):
        return None
    return new, kind
