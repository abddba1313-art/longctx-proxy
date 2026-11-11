#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""checkpoint.py — 会话检查点：把压缩会毁掉的状态逐字留住，每轮回灌。

为什么不是“更好的摘要”
----------------------
摘要的摘要必然有损。每折一次，数字、路径、约束、决策理由都会再掉一层，
长会话就表现为“越压越差”。这里换一种记账方式：

    preserve(原文) -> 结构化状态 -> 每轮重新注入（逐字，不再被摘要改写）

设计契约（借鉴 memory-anchor / amnesia / Nephila，见 README-v2.3）：
  1. 逐字或不要：规则、决策、待办、验证路径存原文，回灌时不改写。
  2. 状态来自原文/工具调用等确定性信号，不来自“模型对对话的转述”。
  3. 回灌是被动参考：最新一条用户消息优先，不命令模型继续做旧任务。
  4. 已完成的待办不复活；被推翻的决策不重复出现。
  5. 分层：L0 硬约束/目标永不被裁；L1 待办/决策/事实按预算裁；
     L2 细节不进来，留在冻结前缀或热层里。

这个模块不 import proxy，避免循环依赖；token 计数和事实抽取由调用方传入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

MARK = "[longctx:checkpoint"
MARK_END = "[longctx:checkpoint end]"
VERSION = 1

# 单条文本硬上限，防止一整段贴进状态把检查点撑爆
MAX_ITEM_CHARS = 320
# 各类条目的上限（超出后保留优先级高的/更新的）
MAX_RULES = 48
MAX_DECISIONS = 32
MAX_TODOS = 48
MAX_FACTS = 64
MAX_FACT_LINES = 24         # 整句事实断言（键值关联/机制/计数）的硬保留上限
MAX_FILES = 40
MAX_ERRORS = 12
MAX_PROGRESS = 20
MAX_SOURCES = 32            # 状态里最多记多少份来源（只存身份，很便宜）
RENDER_SOURCES = 12         # 回灌时展开证据行的份数；其余只列标题，提示可按标题重查
MAX_SOURCE_LINES = 8
MAX_SOURCE_CHARS = 2600          # 每份来源逐字摘录的上限（字符）
MAX_SOURCE_CHUNK_CHARS = 420     # 单段里最多取多少字符的原文
ENABLE_SOURCES = True            # 由 proxy 按 PROXY_CHECKPOINT_SOURCES 覆盖
PROJECT_ROOT = None              # 由 proxy 设成自己的上级目录；用来验相对路径真的存在


# ---------------------------------------------------------------- 基础工具

# 列表符号只在“符号后有空白”时才剥。否则 "1.docx" 会被当成编号列表，
# 把文件名削成 "docx"，多份同名文档会被错误合并成一份来源。
_BULLET = re.compile(r"^\s*(?:[-*+•·]\s+|[（(]\d+[)）]\s*|\d+[.、)]\s+)")
_CHECKBOX = re.compile(r"^\s*[-*+]\s*\[([ xX✓✔])\]\s*(.+)$")
_SPACE = re.compile(r"[ \t　]+")


def _okey(s: str) -> str:
    """去重键：去掉列表符号/空白/标点差异，保留语义。"""
    s = _BULLET.sub("", str(s or "")).strip()
    s = _SPACE.sub(" ", s)
    return s.lower()[:400]


def _path_key(p: str) -> str:
    """路径去重键：Windows 大小写不敏感，双反斜杠和尾部斜杠视为同一个路径。"""
    s = str(p or "").strip()
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    s = s.rstrip("\\/").lower()
    if re.match(r"^[a-z]:[\\/]", s):
        s = s.replace("/", "\\")
    return s


_REF_JUNK = "`*_|,，。；、:：'\"()（）[]{}<> \t"


def _tidy_ref(s: str) -> str:
    """路径/URL 事实与文件条目的归一：折叠字面双反斜杠、剥掉字面 \\r\\n 尾巴、
    尾随的 markdown 残渣（反引号、逗号、表格竖线），以及扩展名后面跟上的中文标点。"""
    t = str(s or "").strip().strip("`* ")
    while "\\\\" in t:
        t = t.replace("\\\\", "\\")
    t = re.sub(r"(?:\\+[rnt])+$", "", t)          # 文本里的字面 \r \n \t
    # 有扩展名就切在扩展名处：D:\a.py；见 / D:\b.exe`，还有 → 只留到 .py / .exe
    mm = re.match(r"^(.*\.[A-Za-z][A-Za-z0-9]{0,7})(?![/\\])(.*)$", t)
    if mm and not re.search(r"[\\/]", mm.group(2)):
        t = mm.group(1)
    t = t.rstrip(_REF_JUNK)
    while t.endswith("\\"):
        t = t[:-1].rstrip()
    return t.strip()


def _is_ref(s: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|https?://|ftps?://)", str(s or "").strip()))


def _clean(s: str) -> str:
    s = _BULLET.sub("", str(s or "")).strip()
    s = _SPACE.sub(" ", s).strip(" \t\r\n-—–")
    if len(s) > MAX_ITEM_CHARS:
        s = s[:MAX_ITEM_CHARS].rstrip() + "…"
    return s


def _text(msg: dict) -> str:
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


_BLOCK_RX = re.compile(re.escape(MARK) + r".*?(?:" + re.escape(MARK_END) + r"|$)", re.S)


def _strip_block(text: str) -> str:
    """剥掉消息文本里**任何位置**的旧检查点块。

    客户端不只会整条回显检查点，还会把它贴在工具结果或助手续写里
    （我审计脚本打印过它，下一轮它就以“对话内容”的身份回来了）。
    不剥就会自我放大：块里的抬头被当成错误、URL 被当成事实。
    """
    t = str(text or "")
    if MARK not in t:
        return t
    return _BLOCK_RX.sub("", t).strip()


def is_mark(msg: dict) -> bool:
    return isinstance(msg, dict) and _text(msg).lstrip().startswith(MARK)


def strip_marks(messages: list[dict]) -> int:
    """去掉客户端回显的旧检查点，保证一轮只会注入一份。"""
    if not isinstance(messages, list):
        return 0
    keep = [m for m in messages if not is_mark(m)]
    removed = len(messages) - len(keep)
    if removed:
        messages[:] = keep
    return removed


def _split_units(text: str):
    """按行切，再按句号/分号切过长行；保留原文，不做改写。"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= MAX_ITEM_CHARS:
            yield line
            continue
        # 中文长段落通常句号后不带空格；只对超长行按句切，短行保持原样。
        for part in re.split(r"(?<=[。！？；;.!?])\s*", line):
            part = part.strip()
            if part:
                yield part


# ---------------------------------------------------------------- 信号识别

_RULE_HEAD = re.compile(
    r"^(?:规则|约束|要求|硬性要求|注意事项|禁止事项|验收标准|"
    r"rules?|constraints?|requirements?|must|never|do not|don't)\s*[:：]",
    re.I)
_RULE_WORD = re.compile(
    r"必须|不得|不要|不能|只能|一定要|务必|记住|永远|禁止|请勿|只允许|"
    r"统一用|保持一致|严禁|不许|別|别用|"
    r"\bmust\b|\bnever\b|\balways\b|\bdo not\b|\bdon't\b|\bonly\b|"
    r"\brequired\b|\bavoid\b|\bshould\b|\bkeep\b",
    re.I)
# 3.32 ①：认知不确定性表述不是约束。实测事故：助手自己的犹豫
# （"ltd 没有出现过定义，我不能靠猜回答——至少有三种指向"）被用户引用回贴后，
# 因含"不能"命中 _RULE_WORD 成了 pri=1 硬规则，每轮回注把模型锁死在歧义里。
# 只拦"对内容本身的认知犹豫"，不拦"不确定时先问"这类真祈使。
_HEDGE = re.compile(
    r"不能靠猜|靠猜回答|靠猜测回答|没有出现过定义|未出现过定义|没有明确定义|"
    r"至少有[两三四几\d]+种|无法确定[它其是]|不能确定[它其是]|我不确定|"
    r"答案差别很大|可能是[^，。；]{0,16}也可能是",
    re.I)
# 3.32 ①：带角色前缀的引用转述行（assistant: … / 助手：…）不是用户自己的话，
# 不进规则/锚点计分。用户引用助手输出抱怨时，助手的句子不许借壳成用户约束。
_QUOTED_ROLE = re.compile(
    r"^(?:assistant|助手|tool|工具|system|系统|agent)\s*[:：]", re.I)
# harness / 系统提示注入的内容不是用户对助手的约束，不能进检查点。
# 典型来源：<system-reminder>、<agent_identity>、hooks 的 additionalContext、
# “Tool loaded.”、FACT.md 的技能维护纪律等。
_HARNESS = re.compile(
    r"<agent_identity>|<user>|<facts>|<system-reminder>|"
    r"UserPromptSubmit hook|PreToolUse|PostToolUse|"
    r"The task tools haven't been used recently|"
    r"Tool loaded\.|Only use these if relevant to the current work|"
    r"You are powered by the model|Current identity from|"
    r"<persisted-output>|Full output saved to:|"
    r"<tool_use_error>|<command-name>|<local-command|"
    r"cc_version=|cc_entrypoint=|"
    r"# 长期知识|技能维护纪律|防回退流程|skill_release\.py|"
    r"CHERRY_STUDIO_SKILLS_DIR|"
    r"attached knowledge base|kb_list only to browse|"
    r"kb_list output is not retrieved|\[longctx:checkpoint",
    re.I)
# 技能注入全文（harness 加载 SKILL.md 进会话）不是用户说话；
# 只保留用户委托执行时附在尾部的真意图。
_SKILL_DOC = re.compile(r"Base directory for this skill:", re.I)
# 检查点块的整段回显（用户贴回/工具打印）：剜出块本身，块后的用户真话留下。
_CK_BLOCK = re.compile(
    r"\[longctx:checkpoint[^\]]*\][\s\S]*?\[longctx:checkpoint end\][^\S\n]*\n?")
# 开关：PROXY_CK_DOC_FILTER=0 整体还原 2.9.6 抽取行为。
CK_DOC_FILTER = os.environ.get("PROXY_CK_DOC_FILTER", "1") == "1"
# 3.11 槽位覆写：同键不同值的键值型约束/决策，新值整条顶替旧值。
# 公理：最毒的干扰项是"旧值未覆写"——不覆写，旧值就以合法锚点身份留在块里，
# 回声判据反而优先保留它。只动"单槽"行（整行恰好一个数字）；多数字行、
# 无数字行、异槽行一律不碰——宁可不覆写，不可误杀。
SLOT_OVERWRITE = os.environ.get("PROXY_CK_SLOT", "1") == "1"
MAX_OVERWRITES = 20
# 3.12 goal 换题换血：不按固定轮数轮换——连续若干条真实用户消息与当前
# goal 重叠都低才换（单次岔题不算，长线任务受保护）；与 goal 高重叠的
# 消息清零计龄；短应答（<12 字）不计龄。seen 表防原地改写触发全量重扫
# 时同一条消息重复计龄。
GOAL_WATCH = os.environ.get("PROXY_GOAL_WATCH", "1") == "1"
GOAL_STALE_TURNS = int(os.environ.get("PROXY_GOAL_STALE_TURNS", "3"))
GOAL_SIM = float(os.environ.get("PROXY_GOAL_SIM", "0.18"))
MAX_GOAL_SWAPS = 5
MAX_GOAL_SEEN = 50
_SLOT_KV = re.compile(r"^(?P<key>[^=:：\d]{1,24}?)\s*[=:：]\s*(?P<val>\S.*)$")
_SLOT_UNIT = re.compile(
    r"(?P<key>[^=:：\d]{0,12}?(?:端口|超时|版本|阈值|上限|下限|预算|窗口|并发|"
    r"间隔|比例|半径|大小|尺寸|温度|轮次|次数|延迟|port|timeout|version|"
    r"budget|window|threshold|limit|retries))\D{0,3}?(?P<val>\d[\d.,]*)",
    re.I)
# 成对的注入壳。用户真话常被包在里面整条送进来（Cherry + Claude Code 的形态），
# 所以判定顺序是：先把这些壳剥掉，剥完还剩字就照常用，剥完空了才算纯噪声。
_HARNESS_BLOCK = re.compile(
    r"<(system-reminder|agent_identity|agent_instructions|soul|user|facts|"
    r"persisted-output|task-notification|command-args|local-command-stdout|"
    r"system|hook)>\s*.*?\s*</\1>", re.S | re.I)
# 残缺壳：只有开标签没有闭标签（注入快照被截断时的常态），整行算噪声。
_HARNESS_TORN = re.compile(
    r"^[ \t*</]*(?:system-reminder|agent_identity|agent_instructions|soul|"
    r"user|facts|persisted-output|task-notification|command-args|"
    r"local-command-stdout|system|hook)[^>\n]*>[^\S\n]*(?:\n|$)",
    re.I | re.M)
# 知识库附件模板句：常与用户真话拼在同一行，整行会污染目标/约束，只剜句子。
_KB_ATTACH = re.compile(
    r"The user attached knowledge base.*?not retrieved evidence\.|"
    r"The user attached knowledge base.*?kb_list only to browse its structure;?",
    re.I | re.S)
# 壳外残留的钩子抬头行（“PreToolUse:Bash hook additional context:” 这种）。
_HARNESS_LEAD = re.compile(
    r"^\s*(?:(?:UserPromptSubmit|PreToolUse|PostToolUse)(?::[\w-]+)?"
    r"\s*hook additional context:|hook additional context:)\s*", re.I | re.M)


def _strip_harness(text: str) -> str:
    """剥掉成对的 harness 注入壳，只留下真正说话的内容；嵌套壳循环剥干净。"""
    t = str(text or "")
    for _ in range(5):
        nt = _HARNESS_BLOCK.sub("\n", t)
        nt = _HARNESS_LEAD.sub("", nt)
        nt = _KB_ATTACH.sub("", nt)
        nt = _CK_BLOCK.sub("", nt)
        if nt == t:
            break
        t = nt
    # 残缺开标签（真实快照常被截断，没有闭标签）：整行丢掉。
    t = _HARNESS_TORN.sub("", t)
    return t.strip()


_NOISE_LINE = re.compile(
    r"^\s*(?:Tool loaded\.?|Only use these if relevant|You are powered by|"
    r"Current identity from|The task tools haven't been used|"
    r"cc_version=|cc_entrypoint=)",
    re.I)
# FACT.md 里的“技能维护纪律”不是当前任务的用户约束；旧状态里已经混进去的，
# 靠 sanitize_state 用这个规则清掉。
_FENCE = re.compile(r"```.*?```", re.S)
_CODE_LINE = re.compile(
    r"^\s*(?:#|//|/\*|\*|--)\s*|"
    r"^\s*(?:def|class|import|from|return|if|for|while|try|except|elif|"
    r"else|with|async|yield)\b")

# 旧版本写进状态的规则没有 src；只保留“像用户指令”的，丢掉代码注释/助手叙述。
_USER_RULE_HINT = re.compile(
    r"给我|帮我|请你|你(?:要|别|不|先|再)|做一下|优化一下|"
    r"不要|不用|不能关闭|不做|改成|改用|保留|记住|务必|不得|禁止")
# 代码清单行（706- state["errors"]...）不是运行错误。
_CODE_ERR = re.compile(
    r"^\s*\d+[-:]\s*(?:r?\"|if |def |return |#|state\[|try|except|del |else|elif)|"
    # 带行号的字典/元组行：974 ("最近错误", errs, 7),
    r"^\s*\d+\s*[\(\[]\s*[\"']|"
    # 带行号+制表符的代码/帮助文本：14\tT1 标识召回……
    r"^\s*\d+\t|"
    # 带行号的列表项/表格行：59 · 代理没启动时…… / 171 | MAX_ERRORS | 12 |
    r"^\s*\d+\s*[·・|｜]\s*|"
    # markdown 表格行整行不是错误
    r"^\s*\||"
    # 行号 + 纯 ASCII 代码片段：827- or _ERROR_OK.search(line)…
    r"^\s*\d+[-:]\s+[\x21-\x7e ]+$|"
    # 源码调用行原文：log(f" [{tag}] dump failed: {exc}")
    r"^\s*[a-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*\(")
# 代码常量/字段名，不是任务事实。
_CODE_CONST = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

_SKILL_NOISE = re.compile(
    r"skill-creator|skill_release\.py|CHERRY_STUDIO_SKILLS_DIR|"
    r"工作副本在|防回退流程|镜像 == 新基线|work 与基线|"
    r"verify 抽查|技能自检|有牙|CHERRY_STUDIO_SKILLS_DIR|"
    r"notes 里别用 ASCII 冒号|zip 命名沿用|技能打包成 zip|"
    r"CHANGELOG\.md 是跨技能",
    re.I)

_NOISE_FACT = re.compile(r"^[A-Za-z]:\\?n?\d*$|^[A-Za-z]:$")
_SYNTAX_TODO = re.compile(r"`- \[[ xX]\]`|TODO、下一步|^\s*\[[ xX]\]\s*$")

_DECISION = re.compile(
    r"决定|采用|选用|定为|改成|改用|换成|结论是|最终选择|方案用|就按|"
    r"\bdecided\b|\bwe chose\b|\bgoing with\b|\binstead of\b|\bwe'll use\b|\buse\b.*\bbecause\b",
    re.I)
_DECISION_WHY = re.compile(r"因为|原因是|由于|理由|考虑到|\bbecause\b|\bsince\b|\bdue to\b", re.I)
_TODO_WORD = re.compile(
    r"^(?:TODO|FIXME|XXX|HACK|下一步|接下来|后续|待办|待处理|待验证|还需|需要再)\s*[:：]?\s*",
    re.I)
_DONE_WORD = re.compile(r"已完成|已修复|已实现|已通过|已解决|搞定|完成了|\bdone\b|\bfixed\b|\bpassed\b|\bimplemented\b", re.I)
# 英文词加标识符边界：_ERROR_DOC / _CODE_ERR / MAX_ERRORS 这些代码标识符
# 里含 ERROR，re.I 会把注释行当成运行错误。
_ERROR = re.compile(
    # 下划线侧用 (?<!_) / (?![A-Za-z_]) 挡代码标识符（_ERROR_DOC、MAX_ERRORS、_CODE_ERR），
    # 但保留 ValueError/TypeError 这类驼峰异常名（它们只是字母相邻，不是下划线）。
    r"(?:(?<!_)(?:Traceback|Exception|Errors?|FAILED|FATAL|PANIC)(?![A-Za-z_])"
    r"|错误|失败|异常|超时|拒绝"
    # 英文真故障短语：不带“error”字样的失败也得认
    # （rsync: connection unexpectedly closed / connection refused / No such file …）
    r"|connection (?:refused|reset|timed out|closed unexpectedly)"
    r"|unexpectedly closed|broken pipe|No such file|Permission denied"
    r"|Access denied|out of memory|Segmentation fault|core dumped|Killed"
    r"|assert(?:ion)?\s+failed|exit\s+code\s+[1-9])", re.I)
# “0 failed / all green / exit code 0”不是错误，别让测试汇总行进最近错误。
_ERROR_OK = re.compile(
    r"\b0\s+(?:failed|errors?)\b|passed,\s*0\s+failed|"
    r"全绿|all\s+green|exit\s*code\s*0", re.I)
# 讲“错误码/错误率/错误处理”的说明行不是运行错误。
_ERROR_DOC = re.compile(r"错误码|错误率|错误处理|错误信息|错误提示")
# 硬标志：只有带这些的才算“真跑挂了”。中文“错误/失败”太容易出现在
# 说明文里（“不再污染新状态”“报错”），助手复述改动时会被整段记成错误。
_ERROR_HARD = re.compile(
    # 后面紧跟 = 的是日志键名（errors=12 files=49），不是报错
    r"(?:(?<!_)(?:Traceback|Exception|Errors?|FAILED|FATAL|PANIC)(?![A-Za-z_=])"
    r"|assert(?:ion)?\s+failed|exit\s+code\s+[1-9]"
    r"|connection (?:refused|reset|timed out|closed unexpectedly)|unexpectedly closed"
    r"|broken pipe|No such file|Permission denied|Access denied|out of memory"
    r"|Segmentation fault|core dumped|Killed)", re.I)
# 散文/条目行：带中文引号、以 · - 「 开头，或在讲“第 N 类”，不是错误现场。
_PROSE_MARK = re.compile(
    r"[「」【】]|^\s*[·•\-—]\s|^\s*类\s*\d|^\s*\d+[.、]\s+\*\*"
    r"|→|^\s*[`'\"|]")          # 反引号/竖线开头的是引用代码或表格，不是错误现场
# Read/Grep 那种带行号的代码转储不是对话内容，整条不参与事实与路径。
_LISTING = re.compile(r"(?:\n|^)\s*\d+(?:\t|[-:]\s)")

_PATH = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'<>|*?\r\n]+|(?:\.{0,2}/)?[\w一-龥.-]+/[\w一-龥./-]+|"
    r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|md|txt|yaml|yml|toml|ini|cfg|log|csv|sql|ps1|bat|sh|exe)\b)")
_WRITE_TOOLS = re.compile(r"write|edit|create|apply|patch|replace|insert|delete|move|rename|mkdir", re.I)
_READ_TOOLS = re.compile(r"read|cat|open|view|grep|search|find|list|glob|ls", re.I)


def _is_noise_line(line: str) -> bool:
    return bool(_NOISE_LINE.match(line) or _SKILL_NOISE.search(line))


def _code_free(line: str) -> str:
    """把 `反引号里的代码/标识符` 换成空格再判错误。

    散文里写“同时保留 `ValueError`/`TypeError` 驼峰异常名”不是报错现场，
    但反引号内的标识符会被 _ERROR_HARD 当真标志命中。
    """
    return re.sub(r"`[^`\n]*`", " ", str(line or ""))


def _is_search_dump(text: str) -> bool:
    """搜索引擎/仓库列表类转储：一行标题一行 URL 的成对结构。

    这类内容里全是别人的仓库地址，一旦被当对话抽取，检查点的关键事实段
    会被 20 条 github.com 链接占满，把自己的任务标识挤掉。
    """
    t = str(text or "")
    if ("\"url\"" in t and "\"title\"" in t) or "[{" in t[:40]:
        return True
    lines = [ln for ln in t.splitlines() if ln.strip()]
    http = sum(1 for ln in lines if re.search(r"https?://", ln))
    return len(lines) >= 6 and http >= max(4, len(lines) // 2)


def _is_listing(text: str) -> bool:
    """Read/Grep 风格的带行号转储：≥3 行 "123\\t…" 就当文件内容，不当对话。"""
    return len(_LISTING.findall(str(text or ""))) >= 3


def _is_numeric(s) -> bool:
    """纯数字/千分位/小数/百分比：日志行号和统计数字基本都是这类。"""
    return bool(re.fullmatch(r"[\d,.\s%+\-]+", str(s).strip()))


def _fact_ok(f: str) -> bool:
    s = str(f).strip()
    if len(s) < 4 or _NOISE_FACT.match(s):
        return False
    if "\\n" in s and len(s) < 12:
        return False
    return bool(re.search(r"[A-Za-z0-9一-鿿]", s))


# 不带数字的连字符/下划线标识符（THUOCL_animal、data-file 这种）多半是
# 文档里的示例或分词碎片；真编号（BD-5582、REV_1008、S-141）都含数字。
_ID_NO_DIGIT = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+$")


def _is_env_const(s: str) -> bool:
    """全大写、带下划线、无数字的 token 是配置/环境变量名，不是任务事实。

    PROXY_HOT_TOKENS / LONGCTX_MODEL 这类是本次要挡的对象；
    REV_1008 / DEV_00A7 带数字，是真实标识，照旧保留。
    """
    t = str(s or "").strip()
    return bool(_CODE_CONST.match(t)) and "_" in t and not re.search(r"\d", t)


def _has_path(lst: list, p: str) -> bool:
    k = _path_key(p)
    return any(_path_key(x) == k for x in lst)


def _norm_key(k: str) -> str:
    return re.sub(r"[\s，,。.、:：;；!！?？【】\[\]()（）\"'`*_—-]", "",
                  str(k or "")).lower()


def _slot_of(text) -> "tuple[str, str] | None":
    """键值型单槽行的 (归一化键, 值)；不是就返回 None。

    保守门槛：整行恰好一个数字段。多数字（一行塞两个参数）、无数字
    （"记住保留页脚"）、键里带数字（svc-07 这类实体 ID）一律不成槽。"""
    t = _clean(text)
    if not t:
        return None
    if len(re.findall(r"\d[\d.,]*", t)) != 1:
        return None
    m = _SLOT_KV.match(t)
    if m and m.group("key").strip():
        return _norm_key(m.group("key")), m.group("val").strip().lower()
    m2 = _SLOT_UNIT.search(t)
    if m2 and m2.group("key"):
        return _norm_key(m2.group("key")), m2.group("val").lower()
    return None


def _slot_replace(items: list, text: str, turn: int, ledger: list) -> str:
    """对 items 做同槽顶替：命中旧值则整条摘除、记台账，返回 "ov"；
    同槽同值（措辞变体）去重返回 "dup"；其余返回 "add"（照常追加）。"""
    s_new = _slot_of(text)
    if not s_new:
        return "add"
    key, val = s_new
    changed = False
    for i, it in enumerate(items):
        s = _slot_of(it.get("t", ""))
        if not s or s[0] != key:
            continue
        if s[1] == val:
            return "dup"
        if int(turn) <= int(it.get("w", -1)):
            # 同轮/更早轮出现的另一值不是"改口"，多半是客户端原地改写
            # 消息后的重扫——旧值必须保留（test_checkpoint_cursor_replace 钉死）。
            continue
        ledger.append({"k": key, "old": it["t"], "new": text, "w": int(turn)})
        del ledger[:-MAX_OVERWRITES]
        items[i] = None
        changed = True
    while None in items:
        items.remove(None)
    return "ov" if changed else "add"


def _topic_sig(t):
    """话题指纹：去空白标点的字符二元组 + 实体（英文标识符/多位数字）。"""
    s = re.sub(r"[\s，。,.、:：;；!！?？()（）\"'`*]", "", str(t or ""))
    grams = {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else \
        ({s} if s else set())
    ents = {x.lower() for x in
            re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}|\d[\d.,]{1,}", s)}
    return grams, ents


def _topic_overlap(a, b) -> float:
    ga, ea = _topic_sig(a)
    gb, eb = _topic_sig(b)
    if not ga or not gb:
        return 1.0
    j = len(ga & gb) / max(1, min(len(ga), len(gb)))
    e = (len(ea & eb) / max(1, min(len(ea), len(eb)))) if (ea and eb) else 0.0
    return max(j, e)


def _goal_watch(state: dict, text: str) -> None:
    """3.12：真实用户消息驱动的目标换血。判据与计数都只认新消息。"""
    if not GOAL_WATCH:
        return
    t = _clean(text)
    if len(t) < 12:
        return
    sig = hashlib.sha1(t.encode("utf-8", "replace")).hexdigest()[:16]
    seen = state.setdefault("goal_seen", [])
    if sig in seen:
        return
    seen.append(sig)
    del seen[:-MAX_GOAL_SEEN]
    goal = str(state.get("goal") or "")
    if not goal:
        state["goal"] = t[:600]
        state["goal_stale"] = 0
        return
    if _topic_overlap(goal, t) >= GOAL_SIM:
        state["goal_stale"] = 0
        return
    n = int(state.get("goal_stale") or 0) + 1
    if n >= GOAL_STALE_TURNS:
        state.setdefault("goal_swaps", []).append({"old": goal, "new": t[:600]})
        del state["goal_swaps"][:-MAX_GOAL_SWAPS]
        state["goal"] = t[:600]
        n = 0
    state["goal_stale"] = n


# ---------------------------------------------------------------- 3.32 ②
# 用户显式纠正（"不是 ltd，是 ldt"/"应该查 ldt"）当轮同步改写检查点，
# 不再只等 reset 时 reseed_goal。实测事故：笔误 ltd 被 reset 重播成永久
# 目标，用户纠正句只是易耗尾部消息，几轮后被只丢不摘删除，模型永远看不到。
_CORR_NOT = re.compile(
    r"不是\s*(?P<x>[^\s，。,；;：:!?！？]{1,24})\s*[，,]?\s*(?:而|就)?是\s*"
    r"(?P<y>[^\s，。,；;：:!?！？]{1,24})")
_CORR_TYPO = re.compile(
    r"(?P<x>[^\s，。,；;：:!?！？]{1,24})\s*(?:是笔误|写错了|打错了|弄错了|错了)\s*"
    r"[，,]?\s*(?:应(?:该)?是|应为|改成|改为|是)\s*(?P<y>[^\s，。,；;：:!?！？]{1,24})")
_CORR_SEARCH_X = re.compile(
    r"(?:应该|应当|要|得|请|统一)\s*(?:查|搜|搜索|检索|用)\s*"
    r"(?P<y>[A-Za-z0-9_一-龥]{1,24})\s*(?:不是|别查|不要用|而非)\s*"
    r"(?P<x>[^\s，。,；;：:!?！？]{1,24})")
_CORR_SEARCH_Y = re.compile(
    r"(?:应该|应当|要|得|请)\s*(?:查|搜|搜索|检索)\s*(?P<y>[A-Za-z0-9_一-龥]{1,24})")


def _term_in(s: str, t: str) -> bool:
    """词项出现判定：ASCII 按词边界（ltd 不匹配 ldt-seq 里的子串误报除外），
    中文按子串。"""
    if not s or not t:
        return False
    if re.fullmatch(r"[A-Za-z0-9_]+", t):
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(t)
                              + r"(?![A-Za-z0-9_])", s))
    return t in s


def _replace_term(s: str, x: str, y: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_]+", x):
        return re.sub(r"(?<![A-Za-z0-9_])" + re.escape(x)
                      + r"(?![A-Za-z0-9_])", lambda _: y, s)
    return s.replace(x, y)


def _edit1(a: str, b: str) -> bool:
    """编辑距离 ≤1（同长换位/替换、差一插入/删除）。短词专用。"""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2
                                  and a[diff[0]] == b[diff[1]]
                                  and a[diff[1]] == b[diff[0]])
    lo, hi = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(lo) and lo[i] == hi[i]:
        i += 1
    return lo[i:] == hi[i + 1:]


def _near_token(goal: str, y: str) -> str | None:
    """目标里与 y 形近的词（ltd↔ldt 换位）：只认 ASCII 形近，中文不猜。"""
    if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", y):
        return None
    for tok in re.findall(r"[A-Za-z0-9_]{3,24}", goal):
        if tok != y and (sorted(tok) == sorted(y) or _edit1(tok, y)):
            return tok
    return None


def detect_corrections(state: dict, text: str) -> list[tuple]:
    """从用户消息里认显式纠正，返回 [(x|None, y)]。
    带 x 的配对必须 x 已在检查点里（goal/rules/facts）才认——纠正针对的是
    本会话既有词项，避免把"我不是医生，是护士"这类自我介绍误当纠正。"""
    if not text or len(text) > 4000:
        return []
    hay = [str(state.get("goal") or "")]
    hay += [str(r.get("t") or "") for r in state.get("rules") or []]
    hay += [str(f.get("t") or "") for f in state.get("facts") or []]
    blob = "\n".join(hay)
    out = []
    for rx in (_CORR_NOT, _CORR_TYPO, _CORR_SEARCH_X):
        for m in rx.finditer(text):
            x, y = m.group("x"), m.group("y")
            if x == y or len(y) < 2 or not _term_in(blob, x):
                continue
            if (x, y) not in out:
                out.append((x, y))
    if not out:
        for m in _CORR_SEARCH_Y.finditer(text):
            y = m.group("y")
            if len(y) < 2:
                continue
            x = _near_token(str(state.get("goal") or ""), y)
            pair = (x, y) if x else (None, y)
            if pair not in out:
                out.append(pair)
    return out[:4]


def apply_correction(state: dict, x: str | None, y: str, turn: int = -1) -> int:
    """把纠正落进检查点：goal/rules/facts 同步换词 + 高优先级纠正规则/事实。
    含 x 的认知犹豫规则整条删除（它正是锁死模型的毒规则）；URL/路径类事实
    不改写（历史检索证据保留原样）。返回是否新增纠正规则（幂等去重）。"""
    corr_t = (f"用户纠正 {x} 应为 {y}，检索与回答统一用 {y}" if x
              else f"用户指定 检索与回答统一用 {y}")
    k = _okey(corr_t)
    fresh = not any(r.get("k") == k for r in state["rules"])
    if x:
        goal = str(state.get("goal") or "")
        if _term_in(goal, x):
            new = _replace_term(goal, x, y)
            state.setdefault("goal_swaps", []).append(
                {"old": goal, "new": new, "why": f"纠正 {x}→{y}"})
            del state["goal_swaps"][:-MAX_GOAL_SWAPS]
            state["goal"] = new
            state["goal_stale"] = 0
        keep = []
        for r in state["rules"]:
            t = str(r.get("t") or "")
            if _term_in(t, x):
                if _HEDGE.search(t):
                    continue
                r["t"] = _replace_term(t, x, y)
                r["k"] = _okey(r["t"])
            keep.append(r)
        state["rules"] = keep
        for f in state["facts"]:
            t = str(f.get("t") or "")
            if _term_in(t, x) and not _is_ref(t):
                f["t"] = _replace_term(t, x, y)
                f["k"] = _okey(f["t"])
    _add_rule(state, corr_t, pri=2, turn=turn)
    if not any(f.get("k") == k for f in state["facts"]):
        state["facts"].append({"k": k, "t": corr_t, "src": "user"})
        if len(state["facts"]) > MAX_FACTS:
            del state["facts"][: len(state["facts"]) - MAX_FACTS]
    cs = state.setdefault("corrections", [])
    if not any(c.get("x") == x and c.get("y") == y for c in cs):
        cs.append({"x": x, "y": y, "w": int(turn)})
        del cs[:-MAX_GOAL_SWAPS]
    return 1 if fresh else 0


def _fact_key(s: str) -> str:
    """事实去重键：盘符路径按 Windows 规则归一，其它按 _okey。"""
    t = str(s or "").strip()
    if re.match(r"^[A-Za-z]:[\\/]", t):
        return _path_key(t)
    return _okey(t)


def _path_ok(p: str) -> bool:
    """只把真正的文件路径当文件；过滤 14/305、1b/1c、列表/表单联动 这类斜杠词。"""
    t = str(p).strip()
    if not t or len(t) > 300:
        return False
    if re.fullmatch(r"[\d/.,%]+", t) or re.fullmatch(r"\d+[a-z]?/\d+[a-z]?", t, re.I):
        return False
    if "一-龥" in t:
        return False
    # 相对斜杠路径必须落到一个真扩展名上：v2.9、zipball/refs/tags 这类 URL 碎片不算文件
    return bool(re.search(r"\.[A-Za-z][A-Za-z0-9]{0,7}$", t) or re.match(r"[A-Za-z]:", t)
                or t.startswith("\\\\"))


def _is_rule_line(line: str) -> int:
    """0 = 不是规则；1 = 普通祈使；2 = 显式规则/约束标题。"""
    s = _BULLET.sub("", line).strip()
    if len(s) < 6 or len(s) > MAX_ITEM_CHARS * 2:
        return 0
    # 3.32 ①：引用转述行与认知犹豫句不算用户约束（见 _HEDGE/_QUOTED_ROLE）。
    if _QUOTED_ROLE.match(s) or _HEDGE.search(s):
        return 0
    if _RULE_HEAD.search(s):
        return 2
    if _RULE_WORD.search(s):
        return 1
    return 0


def pin_score(text: str) -> int:
    """这条消息里有多少条“约束/规则”句；用于挑原文锚点。"""
    return sum(1 for ln in _split_units(text) if _is_rule_line(ln))


# 普通事实句的断言标志：含这些词的句子在陈述一个具体事实（键值/阈值/编号/
# 识别码/机制），而不是日志遥测或散文。extract_facts 只留裸 token，会丢掉
# “K-4490 生效于 04:11” 这种键值关联；这里把整句逐字留住。
_FACT_PRED = re.compile(
    r"生效|阈值|编号|识别码|口令|密码|密钥|归档|存放|台账|机制|令牌|幂等|"
    r"称为|代号|定义为|是指|上限|下限|超时|登记")
# 遥测/日志流水行：有数字有标识但不是事实断言，整句保留只会挤占预算。
_TELEMETRY = re.compile(
    r"处理完成|耗时\s*\d|状态\s*(?:OK|RETRY)|参考\s*https?://|batch_\d+", re.I)
# 问句/祈使指令即使含断言标志词也不是事实（“口令是什么？只回复口令本身。”
# 含“口令”但它是问题）。含问号或这些疑问/指令短语的一律不进。
_FACT_SKIP = re.compile(
    r"[？?]|是什么|是多少|有哪些|请问|请依次|请给出|请按|只回复|回答以下|"
    r"答不上来|每题一行")
_FACT_IMP = re.compile(r"^(?:请|帮我|给我|回答|只回复|麻烦)")


def extract_fact_sentences(text: str, limit: int = MAX_FACT_LINES) -> list[str]:
    """逐字挑出普通事实断言句 + 重复句计数，不改写。

    两类：
      1. 含断言标志（生效/阈值/编号/识别码/机制…）的短句，整句保留——
         保住裸 token 丢掉的键值关联（哪个密钥对应哪个时间、哪个服务哪个阈值）。
      2. 逐字重复出现 >=2 次的短句，记成「X 共出现 N 次」——聚合计数一旦
         去重就再也数不出来，这里显式把次数留住。
    遥测流水行、问句、harness 噪声、代码行一律不进。
    """
    units = [u.strip() for u in _split_units(text) if u.strip()]
    out: list[str] = []
    seen: set[str] = set()

    def emit(s: str) -> None:
        k = _okey(s)
        if k and k not in seen:
            seen.add(k)
            out.append(s)

    short_counts = Counter(
        u for u in units
        if 4 <= len(u) <= 60 and u.endswith(("。", "！", "!"))
        and not _TELEMETRY.search(u) and not _HARNESS.search(u))
    for u, c in short_counts.items():
        if c >= 2:
            emit(f"「{u}」在本段共出现 {c} 次")

    for s in units:
        if len(out) >= limit:
            break
        if not (8 <= len(s) <= 160) or s.endswith(("？", "?")):
            continue
        if (_TELEMETRY.search(s) or _HARNESS.search(s)
                or _FACT_SKIP.search(s) or _FACT_IMP.match(s)
                or _is_noise_line(s) or _CODE_LINE.match(s)):
            continue
        if not _FACT_PRED.search(s):
            continue
        if short_counts.get(s, 0) >= 2:      # 已由计数事实覆盖，不重复整句
            continue
        emit(s)
    return out[:limit]


def _tool_files(msg: dict) -> tuple[list[str], list[str]]:
    """从 assistant.tool_calls 的参数里取读写文件路径（比从文本里猜可靠）。"""
    reads, writes = [], []
    for tc in (msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            continue
        path = ""
        for k in ("path", "file_path", "filename", "file", "target"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                path = v.strip()
                break
        if not path:
            continue
        if _WRITE_TOOLS.search(name):
            writes.append(path)
        elif _READ_TOOLS.search(name):
            reads.append(path)
    return reads, writes


# ---------------------------------------------------------------- 知识库来源
# Cherry Studio 的 kb_search 返回的是 JSON 数组：
#   [{"id","baseId","conceptId","title","type","content","score"}, ...]
# 一次 50 段、十几万字符很常见。这里不存全文，只存“来源身份 + 逐字证据行”，
# 并明确提示可以按标题重新检索；这样既不撑爆上下文，也不会两轮后忘光。

_KB_SIGNAL = re.compile(
    r'"baseId"|"conceptId"|knowledge[_ -]?base|知识库|检索结果|参考资料', re.I)
_KB_HEADER = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:来源|文件|文档|资料|source|file)\s*[:：]\s*(.+?)\s*$", re.I)
_STD_CODE = re.compile(
    r"\b(?:API|ASME|GB|GBT|ISO|ASTM|EN|DIN|JB|HG|SH|NB|TSG)\s*[A-Z]*\s*\d", re.I)
_NUM_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|kPa|MPa|psi|bar|℃|°C|mm|cm|m|kg|t|h|min|s|L|kW|MW)\b",
    re.I)
_CONSTRAINT = re.compile(
    r"应(?:当)?|必须|不得|不应|不允许|不超过|至少|最多|最大|最小|允许|"
    r"\bshall\b|\bmust\b|\bshould\b|\bnot\s+exceed\b|\bat\s+least\b|"
    r"\bmaximum\b|\bminimum\b|\bpermitted\b",
    re.I)
# 知识库来源里的代码/配置片段（("max_tokens", ...)、foo(bar)）不是原文证据。
_SOURCE_CODE = re.compile(
    r"^\s*[\(\[\{]\s*[\"'{]|"
    r"^\s*[\(\[\{].*[,:].*[\)\]\}][\s,;]*$|"
    r"^\s*[\"'][\w-]+[\"']\s*[:=]|"
    r"^\s*[a-z_][\w.]*\s*\(.*\)\s*[,;]?$")

_CLAUSE_NO = re.compile(r"^\s*(?:\d+(?:\.\d+)+|[（(]\w[)）]|第[一二三四五六七八九十百]+[章节条款])")
# 页眉/页脚/版权/电话这类“有数字但不是证据”的行，采样时最容易混进来源账本。
_BOILER = re.compile(
    r"\(F\)\s*[\d\-+ ]{6,}|\(T\)\s*[\d\-+ ]{6,}|ISO\s*9001|"
    r"all rights reserved|copyright\s*©|©\s*\d{4}|"
    r"page\s+\d+\s+of\s+\d+|registered (?:trademark|iso)|"
    r"ansys,\s*inc\.?|www\.ansys\.com",
    re.I)


def _source_units(content: str):
    """知识库段落按行/句切，保留原文；超长行才切句。"""
    for line in str(content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # 超过 _clean 的 320 字符上限就先切句，避免“逐字段落”被截成半句。
        if len(line) <= MAX_ITEM_CHARS:
            yield line
            continue
        # 英文长段常以 ". " 分句；中文以句号分句
        for part in re.split(r"(?<=[。！？；;.!?])\s*", line):
            part = part.strip()
            if len(part) >= 8:
                yield part


def _garbled(line: str) -> bool:
    """OCR 碎片：大量 1~2 字母的短词（正常英文条款不会这样）。"""
    toks = re.findall(r"[A-Za-z]+", line)
    if len(toks) >= 6:
        short = sum(1 for t in toks if len(t) <= 2)
        if short / len(toks) > 0.45:
            return True
    return False


def _evidence_score(line: str) -> int:
    if _BOILER.search(line) or _garbled(line):
        return -1
    s = 0
    if _STD_CODE.search(line):
        s += 5
    if _NUM_UNIT.search(line):
        s += 4
    if _CONSTRAINT.search(line):
        s += 4
    if _CLAUSE_NO.match(line):
        s += 3
    if line.startswith("#") or re.match(r"^\d+(?:\.\d+)+\s+\S", line):
        s += 2
    if _PATH.search(line):
        s += 2
    return s


def _source_digest(contents: list[str], max_lines: int = MAX_SOURCE_LINES,
                   max_chars: int = MAX_SOURCE_CHARS) -> list[str]:
    """从一段或多段原文里挑“最像证据”的行，逐字保留，不改写。"""
    scored = []
    seen = set()
    order = 0
    for content in contents:
        for line in _source_units(content):
            if len(line) < 8:
                continue
            k = _okey(line)
            if not k or k in seen:
                continue
            if _SOURCE_CODE.match(line):
                continue
            sc = _evidence_score(line)
            if sc <= 0:
                continue
            seen.add(k)
            scored.append((sc, order, _clean(line)))
            order += 1
    # 同分时优先短行：长段落里往往混着整段叙述，短行更接近条款/参数
    scored.sort(key=lambda t: (-t[0], len(t[2]), t[1]))
    picked = []
    used = 0
    for _, o, line in scored:
        if len(picked) >= max_lines:
            break
        if used + len(line) > max_chars:
            continue
        picked.append((o, line))
        used += len(line)
    picked.sort(key=lambda t: t[0])
    return [line for _, line in picked]


def _kb_entries(text: str) -> list[dict]:
    """识别知识库检索结果，返回 [{title, base, ids, content}]。

    只认带知识库特征的 JSON（baseId/conceptId/score）或带“来源/文件”标题的
    Markdown 块，避免把普通 JSON 工具输出误当知识库。
    """
    if not ENABLE_SOURCES:
        return []
    s = str(text or "").strip()
    if len(s) < 200 or not _KB_SIGNAL.search(s[:20000]):
        return []
    data = None
    if s[:1] in "[{":
        try:
            data = json.loads(s)
        except Exception:
            data = None
    if data is not None:
        if isinstance(data, dict):
            for key in ("results", "chunks", "data", "items", "records"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if isinstance(data, list):
            out = []
            for it in data:
                if not isinstance(it, dict):
                    continue
                content = it.get("content") or it.get("text") or it.get("chunk") or ""
                if not isinstance(content, str) or len(content) < 20:
                    continue
                if not any(k in it for k in ("baseId", "conceptId", "score",
                                             "chunkId", "unitId", "relativePath")):
                    continue
                title = (it.get("title") or it.get("conceptId") or it.get("source")
                         or it.get("relativePath") or "")
                out.append({
                    "title": str(title or "未命名来源")[:300],
                    "base": str(it.get("baseId") or "")[:80],
                    "id": str(it.get("id") or it.get("chunkId") or "")[:80],
                    "content": content,
                })
            if out:
                return out

    # Markdown 形态：至少两个“来源/文件：”标题，才当成知识库注入
    blocks, cur_title, cur = [], "", []
    for line in s.splitlines():
        m = _KB_HEADER.match(line)
        if m:
            if cur_title and cur:
                blocks.append((cur_title, "\n".join(cur)))
            cur_title, cur = m.group(1), []
        else:
            cur.append(line)
    if cur_title and cur:
        blocks.append((cur_title, "\n".join(cur)))
    if len(blocks) >= 2:
        return [{"title": t[:300], "base": "", "id": "", "content": c}
                for t, c in blocks]
    return []


def _add_source(state: dict, title: str, base: str, chunk_id: str,
                content: str) -> None:
    title = _clean(title) or "未命名来源"
    k = _okey(title)
    src = next((x for x in state["sources"] if x.get("k") == k), None)
    if src is None:
        src = {"k": k, "t": title, "base": base, "ids": [], "lines": [], "chunks": 0}
        state["sources"].append(src)
    src["chunks"] = int(src.get("chunks") or 0) + 1
    if chunk_id and chunk_id not in src["ids"]:
        src["ids"].append(chunk_id)
        if len(src["ids"]) > 12:
            del src["ids"][:-12]
    # 先收集到临时池，最后统一按证据分重排（增量更新也不会越攒越杂）
    digest = _source_digest([content], MAX_SOURCE_LINES, MAX_SOURCE_CHARS)
    if not digest:
        # 没有任何“标准号/数值/约束/条款”的块也要留身份：取前两行原文，
        # 否则来源只剩一个标题，模型不知道这份文件讲什么。
        digest = [_clean(ln)[:200] for ln in content.splitlines()
                  if len(ln.strip()) >= 12][:2]
    for line in digest:
        if not any(_okey(line) == _okey(x) for x in src["lines"]):
            src["lines"].append(line)
    # 每次合并后重排一次：单份来源的字符上限是硬约束，不能按 chunk 各存一份。
    # 没有证据分的兜底行（纯说明文本）不能在重排时被丢掉。
    reordered = _source_digest(src["lines"], MAX_SOURCE_LINES, MAX_SOURCE_CHARS)
    if reordered:
        src["lines"] = reordered
    elif src["lines"]:
        src["lines"] = src["lines"][:MAX_SOURCE_LINES]
    if len(state["sources"]) > MAX_SOURCES:
        # 保留最近被检索到的来源
        state["sources"] = state["sources"][-MAX_SOURCES:]


# ---------------------------------------------------------------- 状态

def new_state(conv: str = "") -> dict:
    return {
        "v": VERSION, "conv": conv, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": "", "cursor": 0, "cursor_key": "", "goal": "",
        "rules": [], "decisions": [], "todos": [], "progress": [],
        "facts": [], "fact_lines": [], "files_read": [], "files_mod": [], "errors": [],
        "sources": [], "overwrites": [],
        "goal_stale": 0, "goal_swaps": [], "goal_seen": [],
    }


_CORPUS_CACHE: dict = {"blob": "", "built": 0.0}


def _corpus_dirs() -> list:
    """F 的技能语料目录：PROXY_SKILL_CORPUS 显式指定（os.pathsep 分隔）优先，
    否则取 CHERRY_STUDIO_SKILLS_DIR 与代理快照技能目录。"""
    raw = os.environ.get("PROXY_SKILL_CORPUS", "")
    if raw:
        return [Path(p) for p in raw.split(os.pathsep) if p and Path(p).is_dir()]
    out = []
    for cand in (os.environ.get("CHERRY_STUDIO_SKILLS_DIR", ""),
                 str(Path.home() / "AppData/Roaming/CherryStudio/Data/Agents"
                     "/.claude/skills")):
        if cand and Path(cand).is_dir():
            out.append(Path(cand))
    return out


def _corpus_blob() -> str:
    now = time.time()
    if _CORPUS_CACHE["blob"] and now - _CORPUS_CACHE["built"] < 600:
        return _CORPUS_CACHE["blob"]
    parts = []
    for d in _corpus_dirs():
        try:
            for f in d.rglob("*.md"):
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        except OSError:
            pass
    blob = re.sub(r"\s+", "", "\n".join(parts))
    _CORPUS_CACHE["blob"] = blob
    _CORPUS_CACHE["built"] = now
    return blob


def purge_corpus_legacy(state: dict) -> int:
    """F：把旧版抽取收编的技能文档文本一次性清掉（存量污染的最后 mile）。

    技能语料在磁盘上，逐条做去空白子串匹配；只跑一次（corpus_purged 旗标），
    找不到语料目录时不设旗标、下轮再试。返回清除条数。"""
    if state.get("corpus_purged") or os.environ.get("PROXY_CORPUS_PURGE", "1") == "0":
        return 0
    blob = _corpus_blob()
    if not blob:
        return 0

    def hit(t) -> bool:
        n = re.sub(r"\s+", "", str(t or ""))
        return len(n) >= 16 and n in blob

    dropped = 0
    for key in ("rules", "todos", "decisions"):
        lst = state.get(key) or []
        keep = [x for x in lst if isinstance(x, dict) and not hit(x.get("t"))]
        dropped += len(lst) - len(keep)
        state[key] = keep
    state["corpus_purged"] = 1
    return dropped


def sanitize_state(state: dict) -> dict:
    """清掉旧版本已经写进状态的 harness 噪声（升级后立即生效）。"""
    goal = str(state.get("goal") or "")
    if goal and (_is_noise_line(goal) or _HARNESS.search(goal)):
        state["goal"] = ""
    state["rules"] = [
        r for r in state.get("rules") or []
        if isinstance(r, dict) and r.get("t")
        and not _is_noise_line(r["t"]) and not _HARNESS.search(r["t"])
        and not _SYNTAX_TODO.search(r["t"])
        and (r.get("src") == "user" or _USER_RULE_HINT.search(r["t"]))
    ]
    state["todos"] = [
        t for t in state.get("todos") or []
        if isinstance(t, dict) and t.get("t")
        and not _HARNESS.search(t["t"]) and not _SYNTAX_TODO.search(t["t"])
    ]
    # 2.8：决策只信用户消息。旧状态里的决策没有 src，全部丢掉，
    # 宁可少记，也不要把助手的过程叙述当成用户拍板的结论。
    state["decisions"] = [
        d for d in state.get("decisions") or []
        if isinstance(d, dict) and d.get("t") and d.get("src") == "user"
        and not _HARNESS.search(d["t"]) and not _is_noise_line(d["t"])
    ]
    def _keep_fact(f) -> bool:
        t = f.get("t") if isinstance(f, dict) else f
        if not _fact_ok(t):
            return False
        # 旧状态里的 MAX_FILES/MAX_FACTS/MARK_END 是代码常量，不是任务事实。
        if isinstance(f, dict) and f.get("src") != "user":
            if _is_env_const(t):
                return False
            if _ID_NO_DIGIT.match(str(t).strip()) and not re.search(r"\d", str(t)):
                return False
            # 搜索转储留下的“仓库根链接”（github.com/owner/repo）不是任务标识
            if re.match(r"^https?://(?:www\.)?github\.com/[^/]+/[^/]+/?$", str(t).strip()):
                return False
        return True

    facts, seen_facts = [], set()
    for f in state.get("facts") or []:
        if isinstance(f, dict) and _is_ref(f.get("t") or ""):
            f["t"] = _tidy_ref(f["t"])              # 旧状态里的字面 \r\n、反引号尾巴
            f["k"] = _okey(f["t"])
        if not _keep_fact(f):
            continue
        k = _fact_key(f.get("t") if isinstance(f, dict) else f)
        if k in seen_facts:
            continue
        seen_facts.add(k)
        facts.append(f)
    # 同一个 URL/路径的截短版只是前缀，留最长的那份
    refs = [str(x.get("t") if isinstance(x, dict) else x) for x in facts if _is_ref(
        x.get("t") if isinstance(x, dict) else x)]
    state["facts"] = [
        x for x in facts
        if not (_is_ref(t := str(x.get("t") if isinstance(x, dict) else x))
                and any(o != t and o.startswith(t) for o in refs))]
    state["errors"] = [
        e for e in state.get("errors") or []
        # 判据用去掉反引号后的文本，原文照样存着
        if _ERROR.search(_code_free(e)) and _ERROR_HARD.search(_code_free(e))
        and not _CODE_LINE.match(e) and not _CODE_ERR.match(e)
        and not _ERROR_DOC.search(e) and not _ERROR_OK.search(e)
        and not _is_noise_line(e) and not _HARNESS.search(e)
        and not _PROSE_MARK.search(e)
    ]
    def _dedupe_paths(lst) -> list:
        out, seen = [], set()
        for p in lst or []:
            p = _tidy_ref(p)                 # 旧状态里的反引号/逗号尾巴
            # 被截半的 URL 路径（/github.com/owner/repo/blob/…、xxx.git）不是本机文件；
            # 以 / 开头的 POSIX 路径（/usr/bin）不算在这里，别误杀。
            low = p.lower()
            if "github.com" in low or low.endswith(".git") or "/blob/" in low:
                continue
            if p.startswith("/") and ("/repos/" in low or "/tree/" in low):
                continue
            if not _path_ok(p):
                continue
            # 相对路径要能在项目根下真的找到，否则是搜索摘要里的别人的文件名
            if (PROJECT_ROOT and not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", p.replace("\\\\", "\\"))
                    and not (Path(PROJECT_ROOT) / p.replace("\\", "/")).exists()):
                continue
            k = _path_key(p)
            if k and k not in seen:
                seen.add(k)
                out.append(p)
        return out

    state["files_read"] = _dedupe_paths(state.get("files_read"))
    state["files_mod"] = _dedupe_paths(state.get("files_mod"))
    # 同一个文件既有绝对又有相对写法时只留绝对的那份
    absolutes = {c for c in (state["files_read"] + state["files_mod"])
                 if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", c.replace("/", "\\"))}
    abs_norm = [a.replace("/", "\\").lower() for a in absolutes]

    def _has_abs_twin(p: str) -> bool:
        if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", p.replace("\\\\", "\\")):
            return False
        tail = "\\" + p.replace("/", "\\").lower().lstrip("\\")
        return any(a.endswith(tail) for a in abs_norm)

    state["files_read"] = [p for p in state["files_read"] if not _has_abs_twin(p)]
    state["files_mod"] = [p for p in state["files_mod"] if not _has_abs_twin(p)]
    # 旧知识库来源里的代码片段/OCR 碎片：重新按证据分筛一遍，只留能读的行。
    for src in state.get("sources") or []:
        if isinstance(src, dict) and src.get("lines"):
            src["lines"] = _source_digest(
                src["lines"], MAX_SOURCE_LINES, MAX_SOURCE_CHARS)
    # 3.3 F：技能语料清库（存量污染一次消；旗标后不再重复）
    try:
        purge_corpus_legacy(state)
    except Exception:
        pass
    return state


def load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("v") == VERSION:
            base = new_state(data.get("conv", ""))
            base.update({k: data[k] for k in base if k in data})
            return sanitize_state(base)
    except Exception:
        pass
    return new_state()


def save(path: Path, state: dict) -> bool:
    """原子写；内容没变就不落盘（减少无效 IO）。"""
    state = dict(state)
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    raw = json.dumps(state, ensure_ascii=False, sort_keys=True)
    try:
        p = Path(path)
        if p.exists() and p.read_text(encoding="utf-8").strip() == raw.strip():
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(raw, encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:
        return False


def _add(items: list, text: str, key: str | None = None) -> bool:
    text = _clean(text)
    if not text:
        return False
    k = key or _okey(text)
    if any((i.get("k") if isinstance(i, dict) else _okey(i)) == k for i in items):
        return False
    items.append({"k": k, "t": text})
    return True


def _add_rule(state: dict, text: str, pri: int = 1, turn: int = -1) -> None:
    text = _clean(text)
    if _is_noise_line(text) or _HARNESS.search(text) or _SYNTAX_TODO.search(text):
        return
    k = _okey(text)
    if not k:
        return
    for r in state["rules"]:
        if r.get("k") == k:
            r["pri"] = max(int(r.get("pri", 1)), pri)
            return
    if SLOT_OVERWRITE:
        act = _slot_replace(state["rules"], text, turn,
                            state.setdefault("overwrites", []))
        if act == "dup":
            key, _ = _slot_of(text)
            for x in state["rules"]:
                s = _slot_of(x.get("t", ""))
                if s and s[0] == key:
                    x["pri"] = max(int(x.get("pri", 1)), pri)
                    break
            return
    state["rules"].append({"k": k, "t": text, "pri": pri, "src": "user",
                           "w": int(turn)})
    # 超上限时先丢优先级最低、最老的
    if len(state["rules"]) > MAX_RULES:
        state["rules"].sort(key=lambda r: (-int(r.get("pri", 1)), 0))
        del state["rules"][MAX_RULES:]


def _add_decision(state: dict, text: str, turn: int = -1) -> None:
    text = _clean(text)
    if _HARNESS.search(text):
        return
    k = _okey(text)
    if not k:
        return
    if any(d.get("k") == k for d in state["decisions"]):
        return
    if SLOT_OVERWRITE and _slot_replace(
            state["decisions"], text, turn,
            state.setdefault("overwrites", [])) == "dup":
        return
    why = ""
    m = _DECISION_WHY.search(text)
    if m:
        why = text[m.start():].strip()
    state["decisions"].append({"k": k, "t": text, "why": why[:MAX_ITEM_CHARS],
                              "src": "user", "w": int(turn)})
    if len(state["decisions"]) > MAX_DECISIONS:
        del state["decisions"][: len(state["decisions"]) - MAX_DECISIONS]


def _add_todo(state: dict, text: str, done: bool, turn: int = -1) -> None:
    text = _clean(text)
    if _HARNESS.search(text) or _SYNTAX_TODO.search(text):
        return
    k = _okey(text)
    if not k:
        return
    for t in state["todos"]:
        if t.get("k") == k:
            # 已完成不复活：后出现的未完成勾选不能把 done 改回 False
            if done:
                t["done"] = True
            return
    state["todos"].append({"k": k, "t": text, "done": bool(done),
                           "w": int(turn)})
    if len(state["todos"]) > MAX_TODOS:
        # 先丢已完成的旧条目，再丢最老的
        pending = [t for t in state["todos"] if not t.get("done")]
        done_items = [t for t in state["todos"] if t.get("done")]
        state["todos"] = (pending + done_items)[-MAX_TODOS:]


def _msg_sig(msg: dict) -> str:
    return hashlib.sha1(_text(msg).encode("utf-8", "replace")).hexdigest()[:16]


def update(state: dict, messages: list[dict], counter, fact_extractor=None,
           max_scan: int = 0) -> dict:
    """从原文增量抽取状态。cursor 之前的消息不再重复扫描。"""
    if not isinstance(messages, list):
        return state
    start = int(state.get("cursor") or 0)
    if start < 0 or start > len(messages):
        start = 0
    # 客户端可能原地改写消息（不是只追加）。只比条数会把“替换掉的那条”漏掉，
    # 所以再核对上一条的哈希；对不上就全量重扫（去重逻辑保证不会重复记账）。
    if start > 0 and start <= len(messages):
        if _msg_sig(messages[start - 1]) != state.get("cursor_key"):
            start = 0
    # 客户端历史被截短/重写时全量重扫；已有状态保留（merge，不重建）
    if max_scan and len(messages) - start > max_scan:
        start = max(0, len(messages) - max_scan)

    for i in range(start, len(messages)):
        m = messages[i]
        if not isinstance(m, dict) or is_mark(m):
            continue
        role = m.get("role") or ""
        text = _strip_block(_text(m))    # 旧块贴在任意位置都不许当对话内容回流

        # 知识库检索结果：只记“来源 + 逐字证据行”，并且不再把文档里的
        # “必须/应/不得”当成用户对助手下的约束。
        kb = _kb_entries(text) if text and role in ("tool", "user", "assistant") else []
        if kb:
            for e in kb[:120]:
                _add_source(state, e["title"], e["base"], e["id"], e["content"])
            continue

        # harness 注入先**剥壳**，不是丢整条：用户的真话常整条包在
        # <system-reminder> 里（Cherry + Claude Code 就是这样）。以前判到标签
        # 就整条跳过，实测 29 条用户消息里含约束的 10 条全被吞，
        # 结果规则/待办/决策常年 0。剥完只剩空白才认定这条确实只是注入。
        if text and _HARNESS.search(text):
            text = _strip_harness(text)
            if len(text) < 12:
                continue

        # 技能全文注入（harness 把 SKILL.md 当 user 消息送进来）不是用户在说话：
        # 剜掉技能正文，只留用户委托的 "ARGUMENTS:" 尾段继续参与抽取。
        if CK_DOC_FILTER and text and _SKILL_DOC.search(text):
            m = re.search(r"ARGUMENTS:[^\S\n]*(?P<rest>.*)", text, re.S)
            text = m.group("rest").strip() if m else ""
            if len(text) < 12:
                continue

        # 带行号的代码转储（Read/Grep 结果）、搜索结果转储不是对话，整条跳过；
        # 用户自己贴的一串链接不算转储，只对手/工具输出生效。
        if text and (_is_listing(text)
                     or (role != "user" and _is_search_dump(text))):
            continue

        # 超大粘贴/附件不是用户在说话：规则和待办不从这里抽（事实仍由尾部账本抽）。
        if role == "user" and len(text) > 30000:
            continue

        if role == "user" and text.strip():
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip() and not _is_noise_line(ln)
                          and not _HARNESS.search(ln)), "")
            if first and not state.get("goal"):
                state["goal"] = _clean(first)[:600]
            elif first:
                _goal_watch(state, first)
            # 3.32 ②：显式纠正当轮同步改写 goal/rules/facts，不等 reset。
            for cx, cy in detect_corrections(state, text):
                apply_correction(state, cx, cy, turn=i)

        # 代码块里的“必须/不得”是注释，不是用户约束；先剥掉 fenced code。
        analysis = _FENCE.sub("", text) if text else ""

        # --- 规则：只从 user 取（assistant 自己写的“必须”不算用户约束）
        if role == "user" and analysis:
            for line in _split_units(analysis):
                if _is_noise_line(line) or _CODE_LINE.match(line):
                    continue
                pri = _is_rule_line(line)
                if pri:
                    _add_rule(state, line, pri, turn=i)

        # --- 待办：勾选框 + TODO 词
        if role in ("user", "assistant") and analysis:
            for line in _split_units(analysis):
                if _SYNTAX_TODO.search(line) or _CODE_LINE.match(line):
                    continue
                mcb = _CHECKBOX.match(line)
                if mcb:
                    _add_todo(state, mcb.group(2),
                              mcb.group(1).lower() in ("x", "✓", "✔"), turn=i)
                    continue
                if _TODO_WORD.match(_BULLET.sub("", line)):
                    done = bool(_DONE_WORD.search(line))
                    _add_todo(state, _TODO_WORD.sub("", _BULLET.sub("", line)),
                              done, turn=i)

        # --- 决策
        # 决策只从用户消息取：助手自己的叙述里“决定/采用”大多是过程说明，
        # 不是用户拍板的结论；放进检查点会越攒越像会议纪要。
        if role == "user" and analysis:
            for line in _split_units(analysis):
                if _is_noise_line(line) or _CODE_LINE.match(line):
                    continue
                if _DECISION.search(line) and 8 <= len(line) <= MAX_ITEM_CHARS:
                    _add_decision(state, line, turn=i)

        # --- 已完成事项（进度，低优先级）
        if role == "assistant" and text:
            for line in _split_units(text):
                if _DONE_WORD.search(line) and 6 <= len(line) <= MAX_ITEM_CHARS:
                    if not any(_okey(line) == _okey(p) for p in state["progress"]):
                        state["progress"].append(_clean(line))
            if len(state["progress"]) > MAX_PROGRESS:
                del state["progress"][: len(state["progress"]) - MAX_PROGRESS]

        # --- 错误：工具输出/assistant 里的失败行
        # 消息整体有硬故障标志才允许“软词行”（错误/失败）入账：
        # 否则我自己打印的判据表（“2 源码/编号行当错误 OK”）会被当成错误现场。
        # 短的工具输出（一两行的命令失败，如“连接被重置”）没有硬标志也要收；
        # 长的报告/判据表里没有硬标志就不算错误现场。
        msg_hard = (bool(_ERROR_HARD.search(_code_free(text)))
                    or len(text) < 600) if text else False
        if (role in ("tool", "assistant") and text and _ERROR.search(_code_free(text))
                and not _is_listing(text)):
            for line in _split_units(text):
                if (_CODE_LINE.match(line) or _CODE_ERR.match(line)
                        or _ERROR_OK.search(line) or _ERROR_DOC.search(line)
                        or _is_noise_line(line) or _HARNESS.search(line)
                        or _PROSE_MARK.search(line)):
                    continue
                # 助手复述“某某报错”不算错误现场；工具输出里的软词（失败/超时）才算。
                bare = _code_free(line)
                if not (_ERROR_HARD.search(bare)
                        or (role == "tool" and msg_hard and _ERROR.search(bare))):
                    continue
                if _ERROR.search(line) and 4 <= len(line) <= MAX_ITEM_CHARS:
                    if not any(_okey(line) == _okey(e) for e in state["errors"]):
                        state["errors"].append(_clean(line))
            if len(state["errors"]) > MAX_ERRORS:
                del state["errors"][: len(state["errors"]) - MAX_ERRORS]

        # --- 文件读写
        if role == "assistant":
            reads, writes = _tool_files(m)
            for p in writes:
                p = _tidy_ref(p)
                if _path_ok(p) and not _has_path(state["files_mod"], p):
                    state["files_mod"].append(p)
            for p in reads:
                p = _tidy_ref(p)
                if (_path_ok(p) and not _has_path(state["files_read"], p)
                        and not _has_path(state["files_mod"], p)):
                    state["files_read"].append(p)

    # 文本里出现的路径（没有 tool_calls 的客户端也能记）
    user_parts, other_parts = [], []
    for m in messages[start:]:
        if not isinstance(m, dict):
            continue
        t = _strip_block(_text(m))
        if t and _HARNESS.search(t):
            t = _strip_harness(t)      # 同上：剥壳，别把壳里的路径与标识一起丢
        if not t or _HARNESS.search(t) or _is_listing(t):
            continue
        if m.get("role") == "user":
            user_parts.append(t)
        elif not _is_search_dump(t):          # 搜索转储不进事实/路径账本
            other_parts.append(t)
    user_tail, other_tail = "\n".join(user_parts), "\n".join(other_parts)
    tail = "\n".join(x for x in (user_tail, other_tail) if x)
    # 路径账本：用户消息里的相对路径（proxy/checkpoint.py）算；
    # 助手/工具输出里只认绝对路径，免得把 data/THUOCL_animal.txt 这种
    # 搜索摘要里的片段当成项目文件记下来。
    for src, want_abs in ((user_tail, False), (other_tail, True)):
        for p in _PATH.findall(src)[:60]:
            p = _tidy_ref(p)
            if not _path_ok(p):
                continue
            if want_abs and not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", p):
                continue
            if not _has_path(state["files_mod"], p) and not _has_path(state["files_read"], p):
                state["files_read"].append(p)

    for key, cap in (("files_read", MAX_FILES), ("files_mod", MAX_FILES)):
        if len(state[key]) > cap:
            del state[key][: len(state[key]) - cap]

    # --- 事实账本：复用 proxy 已验证的 extract_facts
    if fact_extractor is not None and tail:
        facts = []                       # [(文本, "user"|"other")]
        for part, is_user in ((user_tail, True), (other_tail, False)):
            if not part:
                continue
            try:
                got = fact_extractor(part, limit=MAX_FACTS * 3)
            except Exception:
                got = []
            for f in got:
                # 纯整数只留用户亲口说的（端口/版本等）；工具和助手输出里的
                # 行号、统计数字不是任务事实，放进来只会吃光事实预算。
                if not is_user and _is_numeric(f):
                    continue
                if not is_user and _is_env_const(f):
                    continue
                if not is_user and _ID_NO_DIGIT.match(str(f).strip()) \
                        and not re.search(r"\d", str(f)):
                    continue          # 无数字的 a_b 型碎片：文档示例，不是任务事实
                facts.append((f, "user" if is_user else "other"))
        for f, src in facts:
            if _is_ref(f):
                f = _tidy_ref(f)          # 尾巴上的字面 \r\n、反引号、逗号
            if not _fact_ok(f):
                continue
            old = next((x for x in state["facts"] if _fact_key(x.get("t")) == _fact_key(f)), None)
            if old is not None:
                if src == "user":
                    old["src"] = "user"   # 用户亲口说过，升级优先级
                continue
            # 同一个 URL/路径被截短又截长时留长的那份：短的只是前缀，不是新事实。
            if _is_ref(f):
                pre = next((x for x in state["facts"]
                            if _is_ref(x.get("t") or "")
                            and str(x.get("t") or "").startswith(f)), None)
                if pre is not None:
                    continue
                longer = [x for x in state["facts"]
                          if _is_ref(x.get("t") or "") and f.startswith(str(x.get("t") or ""))]
                for x in longer:
                    state["facts"].remove(x)
            state["facts"].append({"k": _okey(f), "t": _clean(f), "src": src})
        if len(state["facts"]) > MAX_FACTS:
            del state["facts"][: len(state["facts"]) - MAX_FACTS]

    # 整句事实断言：保住裸 token 丢掉的键值关联（密钥→时间、服务→阈值、
    # 编号→值、机制名）与重复句计数。与 token 事实分账，回灌时硬保留。
    if tail:
        fl = state.setdefault("fact_lines", [])
        for s in extract_fact_sentences(tail):
            k = _okey(s)
            if any(x.get("k") == k for x in fl):
                continue
            fl.append({"k": k, "t": _clean(s)})
        if len(fl) > MAX_FACT_LINES:
            del fl[: len(fl) - MAX_FACT_LINES]

    state["cursor"] = len(messages)
    state["cursor_key"] = _msg_sig(messages[-1]) if messages else ""
    return state


# ---------------------------------------------------------------- 回灌

def _line_cost(lines: list[str], counter) -> int:
    return sum(counter(x) + 1 for x in lines)


def _render_sources(sources: list[dict], counter, budget: int) -> list[str]:
    """文件很多时的公平分配：先保所有来源标题，再逐份轮流给证据行。

    直接按“来源→它所有行”的顺序拼，预算一紧就会把排在后面的文件整份删掉；
    这里改成轮转，保证每份来源至少有一行，剩下的预算再轮到第二、第三行。
    """
    all_src = (sources or [])[-MAX_SOURCES:]
    if not all_src:
        return []

    # 证据行只展开最相关的 RENDER_SOURCES 份（按检索到的段数排）；
    # 其余文件不消失，压缩成一行标题清单，模型仍知道它们被检索过。
    ranked = sorted(range(len(all_src)),
                    key=lambda i: (-int(all_src[i].get("chunks") or 0), i))
    primary = set(ranked[:RENDER_SOURCES])
    picked_src = [s for i, s in enumerate(all_src) if i in primary]
    extras = [s for i, s in enumerate(all_src) if i not in primary]

    footer = ""
    if extras:
        names = [s.get("t", "") for s in extras if s.get("t")]
        while names:
            footer = "- 另检索过：" + "、".join(names) + "（未展开，可按标题重查）"
            if counter(footer) <= 220:
                break
            names.pop()
        if not names:
            footer = ""

    budget2 = budget - (counter(footer) + 1 if footer else 0)
    headers, line_lists = [], []
    for s in picked_src:
        line = f"- 《{s.get('t', '')}》"
        if s.get("base"):
            line += f"（库 {s['base']}，{s.get('chunks', 0)} 段）"
        headers.append(line)
        line_lists.append(list(s.get("lines") or [])[:MAX_SOURCE_LINES])

    # 标题优先：预算连标题都放不下时，只保留能放下的标题
    used = 0
    out_headers = []
    for h in headers:
        c = counter(h) + 1
        if used + c > budget2:
            break
        out_headers.append(h)
        used += c
    if len(out_headers) < len(headers):
        return out_headers + ([footer] if footer else [])

    remaining = budget2 - used
    picked: list[list[str]] = [[] for _ in headers]
    pos = [0] * len(headers)
    progressed = True
    while progressed and remaining > 0:
        progressed = False
        for k, lines in enumerate(line_lists):
            if pos[k] >= len(lines):
                continue
            ln = lines[pos[k]]
            pos[k] += 1
            c = counter(ln) + 2
            if c > remaining:
                continue
            picked[k].append(ln)
            remaining -= c
            progressed = True

    out = []
    for h, ls in zip(headers, picked):
        out.append(h)
        out.extend(f"  - {ln}" for ln in ls)
    if footer:
        out.append(footer)
    return out


def render(state: dict, counter, budget: int = 1600) -> str:
    """按优先级装配检查点块；L0（目标/硬约束）最后才裁，裁也要留标记。"""
    rules = [f"- {r['t']}" for r in state.get("rules") or []]
    pend = [f"- [ ] {t['t']}" for t in state.get("todos") or [] if not t.get("done")]
    done = [f"- [x] {t['t']}" for t in state.get("todos") or [] if t.get("done")]
    decs = []
    for d in state.get("decisions") or []:
        line = f"- {d['t']}"
        if d.get("why") and d["why"] not in d["t"]:
            line += f"（理由：{d['why']}）"
        decs.append(line)
    # 事实按“用户亲口说 > 普通事实 > 裸数字”排；裸数字（日志行号/统计）
    # 最多保留 8 条，避免把检查点预算全吃掉。
    raw_facts = sorted(
        state.get("facts") or [],
        key=lambda x: (0 if (isinstance(x, dict) and x.get("src") == "user") else 1,
                       1 if _is_numeric(x.get("t") if isinstance(x, dict) else x)
                       else 0))
    facts, pure_other, other_count = [], 0, 0
    for x in raw_facts:
        t = x.get("t") if isinstance(x, dict) else x
        src = x.get("src") if isinstance(x, dict) else ""
        if src != "user":
            if _is_numeric(t):
                pure_other += 1
                if pure_other > 8:
                    continue
            else:
                other_count += 1
                if other_count > 20:
                    continue
        facts.append(f"- {t}")
    flines = [f"- {x['t']}" for x in state.get("fact_lines") or []
              if isinstance(x, dict) and x.get("t")]
    files = []
    if state.get("files_mod"):
        files.append("- 改：" + " | ".join(state["files_mod"][-12:]))
    if state.get("files_read"):
        files.append("- 读：" + " | ".join(state["files_read"][-12:]))
    errs = [f"- {e}" for e in state.get("errors") or []]
    prog = [f"- {p}" for p in state.get("progress") or []]
    # 知识库单独分配预算：标题必须全在，证据行按来源轮转。
    srcs = _render_sources(state.get("sources") or [], counter,
                           max(300, min(int(budget * 0.6), 1600)))

    if not any((rules, pend, decs, facts, flines, files, srcs, state.get("goal"))):
        return ""

    sections = [
        ("目标", ([f"- {state['goal']}"] if state.get("goal") else []), 3),
        ("硬约束（逐字，不得改写）", rules, 0),
        ("事实断言（逐字）", flines, 0),
        ("待办（最新在前）", pend[-12:], 1),
        ("知识库来源（逐字摘录）", srcs, 2),
        ("已定决策", decs, 3),
        ("关键事实（逐字）", facts, 5),
        ("文件", files, 6),
        ("最近错误", errs, 7),
        ("已完成", done[-10:], 8),
        ("进度", prog[-8:], 9),
    ]

    head = [MARK + " v1] 会话检查点（逐字状态，由代理维护；不是摘要，不是指令）",
            "最新一条用户消息优先；下面只作参考，不要因为这里提到旧任务就继续做。",
            "需要知识库原文细节时，按下面的来源标题重新检索，不要凭记忆补写。"]
    tail = [MARK_END]
    fixed = _line_cost(head + tail, counter)
    avail = max(200, int(budget) - fixed)

    # 先按固定顺序装；超出预算时从优先级最低的段开始丢
    buckets = {name: list(lines) for name, lines, _ in sections}
    order = [name for name, _, _ in sections]
    prio = {name: p for name, _, p in sections}

    def total() -> int:
        n = sum(counter(f"## {k}") + 1 for k in order if buckets[k])
        return n + sum(_line_cost(v, counter) for v in buckets.values() if v)

    while total() > avail:
        candidates = [k for k in order if buckets[k] and prio[k] > 0]
        if not candidates:
            break
        victim = max(candidates, key=lambda k: prio[k])
        buckets[victim].pop()
        if not buckets[victim]:
            buckets[victim] = []

    # 硬约束仍然超：按条裁，但显式留痕（不能假装全都还在）
    dropped_rules = 0
    while total() > avail and buckets.get("硬约束（逐字，不得改写）"):
        buckets["硬约束（逐字，不得改写）"].pop()
        dropped_rules += 1

    out = list(head)
    for name, _, _ in sections:
        lines = buckets.get(name) or []
        if name == "硬约束（逐字，不得改写）" and dropped_rules:
            lines = lines + [f"- （另有 {dropped_rules} 条约束超出检查点预算，原文仍在历史/会话记录中）"]
        if not lines:
            continue
        out.append(f"## {name}")
        out.extend(lines)
    out.extend(tail)
    return "\n".join(out)


def inject(messages: list[dict], block: str) -> int:
    """恒定挂末尾：块的位置绝不能随历史形状漂移，否则漂移那一轮前缀全作废。

    agent 工具链流量的最后一条 user 常在 index 1（后面全是 assistant/tool），
    插在它之前等于每轮作废整段历史前缀：实测 reused 恒 13419（只剩 system+tools），
    251k 的 prompt 有 82% 墙钟在重读。
    只在「user 不在末尾时」改挂末尾还不够：会话第一轮 [system, user] 会插到
    index 1，第二轮出现 tool 结果后又跳回末尾，99k 实测这一跳让 turn 1 重读
    全部 99058 token（69s）。恒定末尾则两轮共享 [system, user] 整段前缀。
    块头自带「不是摘要，不是指令」，且 role=user，挂在问句之后不改变末条角色。
    """
    if not block:
        return -1
    idx = len(messages)
    messages.insert(idx, {"role": "user", "content": block})
    return idx


# ---------------------------------------------------------------- 受控复位

def _safe_cut(messages: list[dict], idx: int) -> int:
    """不要把 assistant(tool_calls) 和它后面的 tool 结果切开。"""
    n = len(messages)
    idx = max(0, min(idx, n))
    # 落在 tool 结果里：往前退到发起调用的 assistant
    j = idx
    while j > 0 and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
        j -= 1
    if j > 0 and isinstance(messages[j], dict) and messages[j].get("tool_calls"):
        idx = j
    # 尽量落在 user 边界（往前找，最多退 6 条）
    for k in range(idx, max(0, idx - 6) - 1, -1):
        if k < len(messages) and isinstance(messages[k], dict) and messages[k].get("role") == "user":
            return k
    return idx


def _size_text(msg: dict) -> str:
    """只用于**体量估算**的取文：text 之外还认 tool_calls / tool_use / tool_result。

    _text 只管可抽取的纯文本（语义），reset 的圈体必须看见全部占位内容——
    生产实测：工具链会话里 assistant 载荷在 tool_calls 字段、content 为 None，
    _text 返回空 → 733 条消息被当成 ~0 token → 复位切了个空（3.4.3 修）。"""
    c = msg.get("content")
    out = []
    if isinstance(c, str):
        out.append(c)
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                t = p.get("type")
                if t in ("text", "input_text"):
                    out.append(str(p.get("text", "")))
                elif t == "tool_use":
                    out.append(json.dumps(p.get("input", ""), ensure_ascii=False))
                elif t == "tool_result":
                    rc = p.get("content")
                    if isinstance(rc, str):
                        out.append(rc)
                    elif isinstance(rc, list):
                        out.append("\n".join(
                            str(x.get("text", "")) if isinstance(x, dict) else str(x)
                            for x in rc))
    if msg.get("tool_calls"):
        out.append(json.dumps(msg["tool_calls"], ensure_ascii=False))
    return "\n".join(out)


def reset(messages: list[dict], block: str, counter, target_tokens: int) -> list[dict]:
    """把历史换成「system + 检查点 + 最近热层」，一次断前缀换长期不退化。"""
    if not messages:
        return messages
    systems = [m for m in messages if isinstance(m, dict)
               and m.get("role") in ("system", "developer")]
    body = [m for m in messages if not (isinstance(m, dict)
            and (m.get("role") in ("system", "developer") or is_mark(m)))]
    if not body:
        return messages

    # 从尾部往前圈出 target 以内的热层
    used = 0
    cut = len(body)
    for i in range(len(body) - 1, -1, -1):
        n = counter(_size_text(body[i])) + 4
        if used + n > target_tokens and used > 0:
            break
        used += n
        cut = i
    cut = _safe_cut(body, cut)
    tail = body[cut:]
    out = systems + [{"role": "user", "content": block}] + tail
    return out


def drop_data(state: dict, texts: list[str]) -> int:
    """3.4：自动抛弃长期不回声的数据类锚点（事实/进度/错误）。

    约束类（规则/待办/决策）绝不动——它们可能被沉默遵守，删了才是真丢。
    按去空白全文匹配删除，返回删除条数。"""
    norm = {re.sub(r"\s+", "", str(t)) for t in texts or [] if t}
    if not norm:
        return 0
    n = 0
    for key in ("facts", "progress", "errors"):
        lst = state.get(key) or []
        keep = []
        for x in lst:
            t = str(x.get("t") if isinstance(x, dict) else x)
            if re.sub(r"\s+", "", t) in norm:
                n += 1
            else:
                keep.append(x)
        state[key] = keep
    return n


def reseed_goal(state: dict, messages: list[dict]) -> bool:
    """复位=软重启：目标换成最近一条真实用户消息，不再拿整段会话的第一条
    常驻（实测旧任务会霸占「## 目标」一整天）。找不到可用消息返回 False。"""
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user" or is_mark(m):
            continue
        text = _text(m)
        if _HARNESS.search(text):
            text = _strip_harness(text)
        # 3.32 ②：纠正句本身不当目标原文（否则"应该查 ldt 不是 ltd"会成
        # 新目标）；跳过它取上一条真任务消息，再按已落账的纠正换词。
        if detect_corrections(state, text):
            continue
        first = next((ln.strip() for ln in text.splitlines()
                      if ln.strip() and not _is_noise_line(ln)
                      and not _HARNESS.search(ln)), "")
        if first:
            for c in state.get("corrections") or []:
                if c.get("x") and c.get("y"):
                    first = _replace_term(first, c["x"], c["y"])
            state["goal"] = _clean(first)[:600]
            return True
    return False
