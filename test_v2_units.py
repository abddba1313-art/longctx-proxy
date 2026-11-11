#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2.0 离线单元测试：不调模型，验证确定性召回增强。"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 测的是要发布的那份代码。仓库布局是 tests/ 和 proxy/ 并列，本地布局是同一目录，
# 所以按顺序找第一个真正含 proxy.py 的目录——否则会在过期副本上跑出一堆假绿。
for _cand in (HERE / "proxy", HERE.parent / "proxy", HERE):
    if (_cand / "proxy.py").exists():
        sys.path.insert(0, str(_cand))
        break

import proxy  # noqa: E402
# 同 test_hotlayer：把估计系数钉回默认，防机器级标定环境变量造成假红。
proxy.ENGINE_FACTOR = 1.15
proxy.TOKEN_FACTOR = 1.2

# 这里测的是压缩/重放机制本身，关掉热层（热层另有 test_hotlayer.py）。
# 生产默认 PROXY_HOT_TOKENS=48000，会把下面的短测试消息全部当成热层。
proxy.HOT_MSGS = 0
proxy.HOT_TOKENS = 0
# 检查点默认开会让下面这些短用例多出一块注入文本；单独用 test_checkpoint_* 验证。
proxy.CHECKPOINT = False
proxy.CHECKPOINT_PIN = False
# 3.10 只丢不摘是新默认；这里的压缩/摘要用例测的是旧摘要路径本身，
# 显式关掉新策略，另由 test_drop_only_policy 单独验证新默认。
proxy.DROP_ONLY = 0

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_extract_facts():
    text = (
        "服务器 192.168.1.20 上跑 24 层，显存 20GB，版本 v2.5.1，"
        "路径 D:\\Strata\\model.bin，邮箱 ops@example.com，"
        "参考 https://example.com/a?b=1 。"
    )
    facts = proxy.extract_facts(text)
    for want in ("192.168.1.20", "24", "20", "v2.5.1", "D:\\Strata\\model.bin",
                 "ops@example.com", "https://example.com/a?b=1"):
        check(f"fact {want!r}", want in facts, str(facts))
    check("没有 2.5 碎片", "2.5" not in facts, str(facts))


def test_backfill_full():
    text = (
        "第一阶段 耗时 12.5 秒，内存 8GB。\n"
        "第二阶段 耗时 30 秒，内存 16GB。\n"
        "结论：总耗时 42.5 秒，峰值内存 16GB。\n"
    )
    summary = "压缩后只保留了一句概述。"
    out, st = proxy.backfill_facts(text, summary, budget_tokens=10000)
    missing = [f for f in proxy.extract_facts(text) if f not in out]
    check("回填后无缺失事实", not missing, str(missing))
    check("统计 missing>0", st["missing"] > 0, str(st))
    check("补录块存在", "原文补录" in out)


def test_backfill_budget():
    text = "\n".join(f"第{i}项 数值 {1000 + i}，说明文字若干。" for i in range(200))
    summary = "概述。"
    out, st = proxy.backfill_facts(text, summary, budget_tokens=120)
    check("补录受预算约束", st["tokens"] <= 160, str(st))
    check("补录不空", st["added"] > 0, str(st))


def test_density_cap():
    dense = "数值 1001 1002 1003 1004 1005 1006 1007 1008 1009 1010 1011 1012 " * 20
    sparse = "这是一段没有数字的叙述文字，重复很多次。" * 80
    cap_d, ratio_d, n_d = proxy._digest_cap(dense)
    cap_s, ratio_s, n_s = proxy._digest_cap(sparse)
    check("密集段事实更多", n_d > n_s, f"{n_d} vs {n_s}")
    check("密集段比例更高", ratio_d > ratio_s, f"{ratio_d:.2f} vs {ratio_s:.2f}")
    if len(dense) <= len(sparse) * 1.5:
        check("密集段 cap 更高", cap_d > cap_s, f"{cap_d} vs {cap_s}")


def test_dedupe_lines():
    line = "这是一条比较长的重复行，用来模拟分块 overlap 造成的重复内容。"
    text = f"{line}\n短行\n{line}\n另一行"
    out = proxy._dedupe_lines(text)
    check("长重复行只留一次", out.count(line) == 1, out)
    check("短行保留", "短行" in out)


def test_fold_text():
    text = (
        "[第 1/2 段]\n"
        "这是一段很长的叙述性文字，没有数字，也没有标题，应该被折叠掉。\n"
        "版本 v3.2.1，端口 8082，路径 D:\\Strata\\proxy.py。\n"
        "## 第二节\n"
        "另一段叙述文字。\n"
    )
    out = proxy._fold_text(text, 10000)
    check("L2 保留事实行", "v3.2.1" in out and "8082" in out, out)
    check("L2 保留标题", "## 第二节" in out, out)
    check("L2 丢掉纯叙述", "另一段叙述文字" not in out, out)


def test_trim_keeps_facts():
    text = "\n".join(
        ["无关叙述 " + "填充" * 50] * 20
        + ["关键参数：端口 8082，版本 v9.9.9，路径 D:\\x\\y.bin。"]
        + ["无关叙述 " + "填充" * 50] * 20
    )
    out = proxy._trim_to_tokens(text, 120)
    check("裁剪保留事实行", "8082" in out and "v9.9.9" in out, out[:400])
    check("裁剪确实变短", proxy.count_tokens(out) <= 160,
          str(proxy.count_tokens(out)))


def test_overlap():
    from longctx_mcp.splitter import split_text
    text = "。".join(f"第{i}句有内容 {i}" for i in range(4000)) + "。"
    res = split_text(text, max_tokens=1000, overlap_tokens=100, tokenizer="o200k_base")
    chunks = [c.text for c in res.chunks]
    check("产生多个分块", len(chunks) > 2, str(len(chunks)))
    # 相邻块应有约 100 token 的重叠：前块结尾出现在后块开头附近
    tail = chunks[0][-80:]
    check("相邻块有重叠", tail[:40] in chunks[1], tail[:40])


def test_slice_block_extractive():
    middle = "关键参数：端口 8082，版本 v9.9.9，路径 D:\\x\\y.bin。"
    text = ("无关叙述。" * 400) + middle + ("无关叙述。" * 400)
    out = proxy._slice_block(text, proxy.count_tokens(text))
    check("兜底保留中间事实", "8082" in out and "v9.9.9" in out, out[:500])


def test_query_alloc():
    chunks = [
        "Qwen3.6 模型在 H200 上的推理速度是 32 tokens/s。" * 60,
        "无关内容：安装步骤、字体设置、界面颜色、快捷键说明。" * 60,
        "Clover 冷专家缓存读取延迟 3.72ms，显存占用 5.2GB。" * 60,
    ]
    caps = proxy._allocate_caps(chunks, "Qwen3.6 的推理速度是多少？", 30000)
    check("分配总预算不超上限", sum(caps) <= 30000, str(caps))
    check("相关段不少于无关段", caps[0] >= caps[1], str(caps))
    check("每段都有下限", min(caps) >= 1000, str(caps))
    uni = proxy._allocate_caps(chunks, proxy.GENERIC_TASK, 30000)
    check("通用任务退化为均分", len(set(uni)) == 1, str(uni))


def test_structured_detect():
    code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(900)) + "\n```"
    prose = "这是一段普通的说明文字，没有代码也没有表格。" * 40
    table = "| 指标 | 值 |\n| --- | --- |\n" + "| 延迟 | 3.72ms |\n" * 200
    check("代码块识别为结构化", proxy._structured_fraction(code) > 0.9,
          f"{proxy._structured_fraction(code):.2f}")
    check("表格识别为结构化", proxy._structured_fraction(table) > 0.9,
          f"{proxy._structured_fraction(table):.2f}")
    check("散文不是结构化", proxy._structured_fraction(prose) < 0.1,
          f"{proxy._structured_fraction(prose):.2f}")
    check("大代码块可直通", proxy._is_verbatim_chunk(code), "")
    check("短代码块不直通", not proxy._is_verbatim_chunk("```py\nx=1\n```"), "")


def test_fact_window():
    text = "前言一行。\n| 指标 | 值 | 备注 |\n| --- | --- | --- |\n| 延迟 | 3.72ms | 冷专家 |\n后记。"
    i = text.index("3.72ms")
    a, b = proxy._fact_window(text, i, i + 6, 10)
    seg = text[a:b].strip()
    check("表格事实整行保留", seg.startswith("| 延迟") and seg.endswith("|"), seg)

    text2 = "第一句话没有事实。第二句：端口是 8082。第三句话结尾。"
    j = text2.index("8082")
    a2, b2 = proxy._fact_window(text2, j, j + 4, 3)
    seg2 = text2[a2:b2]
    check("窗口包含事实", "8082" in seg2, seg2)
    check("窗口不越到上一句", "第一句" not in seg2, seg2)


_IDCH = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/\\-+")
# 收尾判定不含句点：片段以 "locations." 这种整句收尾是对的，不算切词。
_TAILID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:/\\-+")


def test_window_no_midword_cut():
    """窗口和补录都不许把单词/哈希/路径拦腰截断（2026-11-05 实测缺陷）。

    旧实现的边界吸附只认「。；！？\\n」，遇到英文、路径、哈希就在词中间下刀，
    实测把 "…installed at all five…" 切成 "…l at all five…"，
    把 "… -> shareable folder risk" 切成 "… -> share…"：数字保住了，结论没了。
    """
    text = (
        "Security verification: live scan flagged the fixture ops@example.com "
        "with sha256 prefix 574d24bf889e903c at all five locations.\n"
        "Flagged: targets.json contains 1 non-empty key field -> shareable folder risk.\n"
    )
    for fact in ("574d24bf889e903c", "targets.json", "ops@example.com"):
        i = text.index(fact)
        a, b = proxy._fact_window(text, i, i + len(fact), 40)
        head_ok = not (a > 0 and text[a - 1] in _IDCH and text[a] in _IDCH)
        tail_ok = not (b < len(text) and text[b - 1] in _IDCH and text[b] in _IDCH)
        check(f"窗口起头不切词 {fact}", head_ok, repr(text[max(0, a - 10):a + 10]))
        check(f"窗口收尾不切词 {fact}", tail_ok, repr(text[b - 10:b + 10]))

    i = text.index("targets.json")
    a, b = proxy._fact_window(text, i, i + len("targets.json"), 40)
    check("结论尾巴整块保留", "shareable folder risk" in text[a:b], repr(text[a:b]))

    out, _st = proxy.backfill_facts(text, "概述。", budget_tokens=400)
    segs = [ln[2:] for ln in out.split("\n") if ln.startswith("- …")]
    check("有补录片段", bool(segs), out)
    cut_tail = [s for s in segs if s.endswith("…") and s[-2] in _TAILID]
    check("补录不在标识符中间收尾", not cut_tail, str(cut_tail))

    line = proxy.evidence._clean_line(
        "Flagged: targets.json contains 1 non-empty key field -> shareable risk", 60)
    check("_clean_line 不切词", not (line[-2] in _TAILID and line[-1] == "…"), repr(line))


def test_repair_cut_tokens():
    """模型在词中间断行时按原文补全；没有原文依据的一律不动。"""
    source = (
        "## Key Features\n"
        "Fun-ASR-Nano is an 800M-parameter end-to-end speech recognition model.\n"
        "The service listens on port 8080 and the portal sits behind it.\n"
    )
    cases = [
        ("### Key Feat", "### Key Features"),          # 原文里只以 Features 出现
        ("模型是 end-t...", "模型是 end-to-end"),        # 省略号 = 强截断信号
        ("The service listens on port", "The service listens on port"),  # 独立词，不许改
    ]
    for line, want in cases:
        out, _n = proxy.repair_cut_tokens(line, source)
        check(f"补全或不动 {line!r}", out == want, f"got {out!r}")

    out, n = proxy.repair_cut_tokens("## Key Feat", source)
    check("统计补全次数", n == 1, str(n))

    out, n = proxy.repair_cut_tokens("一句完整的话。", source)
    check("正常行不动", out == "一句完整的话。" and n == 0, f"{out!r} {n}")


def test_fact_coverage_metric():
    chunk = "端口 8082，版本 v9.9.9，路径 D:\\x\\y.bin。"
    check("全覆盖 = 1.0", proxy._fact_coverage(chunk, chunk) == 1.0,
          str(proxy._fact_coverage(chunk, chunk)))
    cov = proxy._fact_coverage(chunk, "端口 8082")
    check("部分覆盖在 0~1 之间", 0.0 < cov < 1.0, str(cov))
    check("无事实段视为全覆盖", proxy._fact_coverage("纯叙述文字。", "") == 1.0)


def test_repeat_dedupe():
    body = "重复的工具输出内容 " * 300
    other = "另一份不同的工具输出 " * 300
    msgs = ([{"role": "tool", "content": body} for _ in range(3)]
            + [{"role": "tool", "content": other}]
            + [{"role": "user", "content": "继续"}] * 4)
    n = proxy._dedupe_repeated_tools(msgs, "t")
    check("删除 2 份完全重复", n == 2, str(n))
    check("保留第一份原文", msgs[0]["content"] == body, "")
    check("重复份替换为占位", "已省略" in msgs[1]["content"], msgs[1]["content"][:60])
    check("不同内容不误删", msgs[3]["content"] == other, "")
    check("最近 4 条不动", all(m["content"] == "继续" for m in msgs[-4:]), "")


def test_toolfmt_pytest():
    import toolfmt
    text = (
        "============================= test session starts =============================\n"
        "platform win32 -- Python 3.12.4, pytest-8.3.2\n"
        "rootdir: D:\\proj\n"
        "collected 120 items\n\n"
        "tests/test_a.py " + "." * 60 + " [ 50%]\n"
        "tests/test_b.py " + "." * 52 + "F.. [ 100%]\n\n"
        "================================== FAILURES ===================================\n"
        "_________________________________ test_port __________________________________\n\n"
        "    def test_port():\n"
        ">       assert config.port == 8082\n"
        "E       AssertionError: assert 8080 == 8082\n\n"
        "tests/test_b.py:42: AssertionError\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_b.py::test_port - AssertionError: assert 8080 == 8082\n"
        "======================== 1 failed, 119 passed in 2.31s ========================\n"
    )
    got = toolfmt.format_tool_output(text, "Bash", '{"command":"pytest -q"}')
    check("pytest 能被格式化", got is not None, "")
    if got:
        out, kind = got
        check("识别为 pytest", kind == "pytest", kind)
        check("保留失败位置", "tests/test_b.py:42" in out, out)
        check("保留断言数值", "8080" in out and "8082" in out, out)
        check("保留汇总", "1 failed, 119 passed" in out, out)
        check("明显变短", len(out) < len(text) * 0.8, f"{len(out)}/{len(text)}")


def test_toolfmt_passthrough():
    import toolfmt
    samples = {
        "源码": "def foo():\n    return 1\n" * 200,
        "diff": ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n" * 20),
        "traceback": ("Traceback (most recent call last):\n"
                      '  File "x.py", line 3, in <module>\n'
                      "    raise ValueError('boom')\nValueError: boom\n") * 20,
        "密钥": ("-----BEGIN PRIVATE KEY-----\n" + "MIIE" * 100 + "\n"
                 "-----END PRIVATE KEY-----\n"),
    }
    for name, text in samples.items():
        check(f"{name}不压缩", toolfmt.format_tool_output(text, "Bash", "cat x") is None, "")


def test_toolfmt_json():
    import json
    import toolfmt
    obj = {"server": {"host": "127.0.0.1", "port": 8082},
           "items": [{"id": i, "name": f"item-{i}", "ok": True} for i in range(80)]}
    text = json.dumps(obj, ensure_ascii=False, indent=4)
    got = toolfmt.format_tool_output(text, "Bash", "cat config.json")
    check("JSON 能被压缩", got is not None, "")
    if got:
        out, kind = got
        check("识别为 json", kind == "json", kind)
        check("JSON 语义不变", json.loads(out) == obj, "")
        check("JSON 明显变短", len(out) < len(text) * 0.7, f"{len(out)}/{len(text)}")


def test_toolfmt_logs():
    import toolfmt
    lines = []
    for i in range(120):
        lines.append(f"2026-11-03 10:00:{i % 60:02d}.000 INFO request {i} ok")
    lines[40] = "2026-11-03 10:00:40.000 ERROR connect refused port 8082"
    lines[41] = "2026-11-03 10:00:41.000 ERROR connect refused port 8082"
    text = "\n".join(lines) + "\n" + "\n".join([lines[41]] * 10)
    got = toolfmt.format_tool_output(text, "Bash", "tail -n 200 app.log")
    check("日志能被压缩", got is not None, "")
    if got:
        out, kind = got
        check("识别为日志", kind == "logs", kind)
        check("保留错误行", "ERROR connect refused port 8082" in out, out[:300])
        check("重复行被折叠", "[×" in out, out[-300:])


def test_prefix_replay():
    import tempfile
    from pathlib import Path

    old_dir = proxy.PREFIX_DIR
    old_strict = proxy.PREFIX_STRICT
    proxy.PREFIX_DIR = Path(tempfile.mkdtemp(prefix="longctx-prefix-"))
    proxy.PREFIX_STRICT = True
    proxy._prefix_mem.clear()
    try:
        msgs = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "原始长文档 " * 500},
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "总结一下"},
        ]
        keys = proxy._message_keys(msgs)
        conv, stable, stored = proxy._prefix_state(msgs, keys)
        check("首次没有稳定前缀", stable == 0, str(stable))
        summary = "[longctx] 原始消息共 1 字符，已汇总：关键端口 8082"
        msgs[1]["content"] = summary
        proxy._save_prefix(conv, msgs, keys, stored, set(), {1: "digest"})

        msgs2 = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "原始长文档 " * 500},
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "总结一下"},
            {"role": "assistant", "content": "端口是 8082"},
            {"role": "user", "content": "继续"},
        ]
        keys2 = proxy._message_keys(msgs2)
        conv2, stable2, stored2 = proxy._prefix_state(msgs2, keys2)
        check("第二轮稳定前缀覆盖旧消息", stable2 == len(msgs), str(stable2))
        applied = proxy._apply_stored_prefix(msgs2, keys2, stored2, stable2)
        check("旧消息重放压缩结果", applied >= 1 and msgs2[1]["content"] == summary,
              msgs2[1]["content"][:80])
        check("新消息未被改动", msgs2[5]["content"] == "继续", "")
    finally:
        proxy.PREFIX_DIR = old_dir
        proxy.PREFIX_STRICT = old_strict
        proxy._prefix_mem.clear()


def test_transform_prefix_replay():
    import tempfile
    from pathlib import Path

    saved = {}
    for name in ("PREFIX_DIR", "PREFIX_STRICT", "THRESHOLD", "TARGET_MSG",
                 "CONTEXT", "RESERVE", "MIN_MSG", "LOW_MSG", "REPEAT_DEDUPE",
                 "L2_FOLD", "get_summary"):
        saved[name] = getattr(proxy, name)
    old_mem = dict(proxy._prefix_mem)
    calls = {"n": 0}

    def fake_summary(text, task, chunk_conc=None):
        calls["n"] += 1
        return "摘要：端口 8082，版本 v9.9.9，路径 D:\\x\\y.bin。"

    proxy.PREFIX_DIR = Path(tempfile.mkdtemp(prefix="longctx-transform-"))
    proxy.PREFIX_STRICT = True
    proxy.THRESHOLD = 100
    proxy.TARGET_MSG = 300
    proxy.CONTEXT = 100000
    proxy.RESERVE = 1000
    proxy.MIN_MSG = 50
    proxy.LOW_MSG = 20
    proxy.REPEAT_DEDUPE = False
    proxy.L2_FOLD = False
    proxy.get_summary = fake_summary
    proxy._prefix_mem.clear()
    try:
        doc = "这是一段很长的原始文档，里面有端口 8082 和版本 v9.9.9。" * 400
        body1 = {"max_tokens": 4096, "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": doc},
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "总结"},
        ]}
        proxy.transform(body1, "t1")
        first = body1["messages"][1]["content"]
        check("第一轮确实压缩了", first.startswith(proxy.MARKER), first[:40])
        check("第一轮调用一次摘要", calls["n"] == 1, str(calls))

        body2 = {"max_tokens": 4096, "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": doc},
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "总结"},
            {"role": "assistant", "content": "端口 8082"},
            {"role": "user", "content": "继续"},
        ]}
        proxy.transform(body2, "t2")
        second = body2["messages"][1]["content"]
        check("第二轮旧前缀逐字相同", second == first, second[:80])
        check("第二轮不重复调用摘要", calls["n"] == 1, str(calls))
        check("第二轮新消息未动", body2["messages"][5]["content"] == "继续", "")
    finally:
        for name, val in saved.items():
            setattr(proxy, name, val)
        proxy._prefix_mem.clear()
        proxy._prefix_mem.update(old_mem)


def test_prefix_emergency_unfreeze():
    import tempfile
    from pathlib import Path

    saved = {}
    for name in ("PREFIX_DIR", "PREFIX_STRICT", "THRESHOLD", "TARGET_MSG",
                 "CONTEXT", "RESERVE", "MIN_MSG", "LOW_MSG", "REPEAT_DEDUPE",
                 "L2_FOLD", "get_summary"):
        saved[name] = getattr(proxy, name)
    old_mem = dict(proxy._prefix_mem)
    calls = {"n": 0}

    def fake_summary(text, task, chunk_conc=None):
        calls["n"] += 1
        return "摘要：端口 8082。"

    proxy.PREFIX_DIR = Path(tempfile.mkdtemp(prefix="longctx-emergency-"))
    proxy.PREFIX_STRICT = True
    proxy.THRESHOLD = 100
    proxy.TARGET_MSG = 1
    proxy.CONTEXT = 5000
    proxy.RESERVE = 1000
    proxy.MIN_MSG = 50
    proxy.LOW_MSG = 20
    proxy.REPEAT_DEDUPE = False
    proxy.L2_FOLD = False
    proxy.get_summary = fake_summary
    proxy._prefix_mem.clear()
    try:
        doc = "这是一段很长的原始文档，里面有端口 8082 和版本 v9.9.9。" * 400
        msgs = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": doc},
            {"role": "assistant", "content": "已读"},
            {"role": "user", "content": "总结"},
        ]
        keys = proxy._message_keys(msgs)
        conv, stable, stored = proxy._prefix_state(msgs, keys)
        check("紧急回退前无稳定前缀", stable == 0, str(stable))
        proxy._save_prefix(conv, msgs, keys, stored, set(), {})

        body = {"max_tokens": 4096, "messages": msgs}
        proxy.transform(body, "t")
        out = body["messages"][1]["content"]
        check("超硬上限时解冻旧前缀", out.startswith(proxy.MARKER), out[:50])
        check("紧急回退调用一次摘要", calls["n"] == 1, str(calls))
        check("请求回到硬上限内", proxy.request_tokens(body)[2] <= 4096,
              str(proxy.request_tokens(body)[2]))
    finally:
        for name, val in saved.items():
            setattr(proxy, name, val)
        proxy._prefix_mem.clear()
        proxy._prefix_mem.update(old_mem)


def test_truncate_tools_uses_toolfmt():
    saved = {n: getattr(proxy, n) for n in ("CONTEXT", "RESERVE", "TARGET_MSG", "TOOLFMT")}
    old_d = getattr(proxy._xform_local, "d", None)
    proxy.CONTEXT = 100000
    proxy.RESERVE = 1000
    proxy.TARGET_MSG = 1                 # _budget 的硬底是 4096
    proxy.TOOLFMT = True
    proxy._xform_local.d = {}
    try:
        report = (
            "============================= test session starts =============================\n"
            "collected 900 items\n\n"
            + "\n".join(f"tests/test_{i}.py " + "." * 70 + " [ 50%]" for i in range(300))
            + "\n\n================================== FAILURES ===================================\n"
            "_________________________________ test_port __________________________________\n"
            "    def test_port():\n"
            ">       assert config.port == 8082\n"
            "E       AssertionError: assert 8080 == 8082\n"
            "tests/test_b.py:42: AssertionError\n"
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_b.py::test_port - AssertionError: assert 8080 == 8082\n"
            "======================== 1 failed, 899 passed in 2.31s ========================\n"
        )
        body = {"max_tokens": 4096, "messages":[{"role": "tool", "content": report}]
                + [{"role": "user", "content": "ok"} for _ in range(9)]}
        before = proxy.request_tokens(body)[2]
        n = proxy._truncate_tools(body, body["messages"], "t", frozen=set())
        out = body["messages"][0]["content"]
        check("toolfmt 接入截断层", out != report and n < before, f"{n} vs {before}")
        check("保留失败位置", "tests/test_b.py:42" in out, out[:200])
        check("保留断言数值", "8080" in out and "8082" in out, out[:200])
        check("记录转换类型", proxy._xform_local.d.get(0, "").startswith("toolfmt:pytest"),
              str(proxy._xform_local.d))

        body2 = {"max_tokens": 4096, "messages":[{"role": "tool", "content": report}]
                 + [{"role": "user", "content": "ok"} for _ in range(9)]}
        proxy._truncate_tools(body2, body2["messages"], "t", frozen={0})
        check("冻结前缀不被 toolfmt", body2["messages"][0]["content"] == report, "")
    finally:
        for name, val in saved.items():
            setattr(proxy, name, val)
        proxy._xform_local.d = old_d


def test_prefix_freeze_dedupe():
    body = "重复的工具输出内容 " * 300
    msgs = [
        {"role": "tool", "content": body},
        {"role": "tool", "content": body},
        {"role": "tool", "content": body},
        {"role": "user", "content": "继续"},
        {"role": "user", "content": "继续"},
        {"role": "user", "content": "继续"},
        {"role": "user", "content": "继续"},
    ]
    n = proxy._dedupe_repeated_tools(msgs, "t", frozen={0, 1})
    check("只改写未冻结的重复项", n == 1, str(n))
    check("冻结项保持原文", msgs[0]["content"] == body and msgs[1]["content"] == body, "")
    check("新重复项被省略", "已省略" in msgs[2]["content"], msgs[2]["content"][:60])


def test_evidence_identifiers():
    text = (
        "启动前设置 PROXY_PREFIX_STRICT=1，打包用 --onefile，"
        "报错 E1234，位置 app.py:42，占用 12.5GB，提交 a3f9c2b。"
    )
    out, st = proxy.evidence.backfill_evidence(text, "只有一句概述。", 600)
    for want in ("PROXY_PREFIX_STRICT", "--onefile", "E1234", "app.py:42",
                 "12.5GB", "a3f9c2b"):
        check(f"补录标识 {want!r}", want in out, out[-1200:])
    check("证据统计 ids_added>0", st["ids_added"] > 0, str(st))


def test_evidence_constraints():
    text = (
        "部署前必须确认端口 8082。\n"
        "如果没有备份，禁止执行迁移。\n"
        "Bridge 是指把请求转发到上游的组件。\n"
        "今天天气很好。\n"
    )
    out, st = proxy.evidence.backfill_evidence(text, "概述。", 800)
    check("补录强约束", "必须确认端口 8082" in out, out[-800:])
    check("补录条件否定", "禁止执行迁移" in out, out[-800:])
    check("补录定义句", "Bridge 是指" in out, out[-800:])
    check("不补无关叙述", "今天天气很好" not in out, out[-800:])
    check("约束统计 cons_added>=3", st["cons_added"] >= 3, str(st))


def test_evidence_covered():
    text = "如果没有备份，禁止执行迁移。"
    summary = "注意：如果没有备份，禁止执行迁移。"
    out, st = proxy.evidence.backfill_evidence(text, summary, 800)
    check("摘要已有约束不重复补", st["cons_added"] == 0, str(st))
    check("输出不重复", out.count("禁止执行迁移") == 1, out)


def test_evidence_budget():
    text = "\n".join(
        f"第{i}步：如果条件 {i} 成立，必须执行操作 {i}，至少 {i} 次。"
        for i in range(200))
    out, st = proxy.evidence.backfill_evidence(text, "概述。", 160)
    check("证据补录受预算约束", st["tokens"] <= 200, str(st))
    check("预算内仍有补录", st["cons_added"] > 0, str(st))


def test_query_alloc_no_starve():
    chunks = ["端口 8082 的说明 " * 40, "无关内容 " * 40,
              "无关内容 " * 40, "无关内容 " * 40]
    caps = proxy._allocate_caps(chunks, "端口是多少", 4000)
    check("每段都有 60% 保底", min(caps) >= int(4000 / 4 * 0.60) - 1, str(caps))
    check("相关段仍然更多", max(caps) > min(caps), str(caps))
    check("总预算不超", sum(caps) <= 4000, str(caps))


def test_digest_cap_evidence_density():
    cons = "如果端口 8082 被占用，必须改用 8083；否则禁止启动。" * 40
    narrative = "这是一段普通的叙述文字，没有条件和标识。" * 40
    cap_c, ratio_c, _ = proxy._digest_cap(cons)
    cap_n, ratio_n, _ = proxy._digest_cap(narrative)
    check("约束段证据更多", proxy.evidence.count_evidence(cons) >
          proxy.evidence.count_evidence(narrative))
    check("约束段预算不低于叙述段", (ratio_c, cap_c) >= (ratio_n, cap_n),
          f"{ratio_c:.2f}/{cap_c} vs {ratio_n:.2f}/{cap_n}")


def test_tool_path_prefix():
    body = {"max_tokens": 4096, "messages": [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "function": {"name": "read",
                                     "arguments": '{"path": "src/main.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read",
         "content": "x" * 40000},
    ] + [{"role": "user", "content": "ok"} for _ in range(9)]}
    old = getattr(proxy._xform_local, "d", None)
    proxy._xform_local.d = {}
    saved = (proxy.CONTEXT, proxy.RESERVE, proxy.TARGET_MSG, proxy.TOOLFMT)
    proxy.CONTEXT, proxy.RESERVE, proxy.TARGET_MSG, proxy.TOOLFMT = 100000, 1000, 1, False
    try:
        proxy._truncate_tools(body, body["messages"], "t", frozen=set())
        out = body["messages"][1]["content"]
        check("截断后保留文件路径", "src/main.py" in out, out[:120])
    finally:
        proxy.CONTEXT, proxy.RESERVE, proxy.TARGET_MSG, proxy.TOOLFMT = saved
        proxy._xform_local.d = old


def test_checkpoint_state():
    import checkpoint
    st = checkpoint.new_state("t")
    msgs = [
        {"role": "user", "content":
            "目标：把端口从 8080 改到 8082。\n"
            "必须保留 API_KEY=sk-123。\n"
            "- [ ] 改配置\n- [x] 备份"},
        {"role": "user", "content": "决定采用方案 B，因为回滚简单。"},
        {"role": "assistant", "content": "已完成备份。", "tool_calls": [{
            "function": {"name": "edit",
                         "arguments": '{"path": "C:\\\\x\\\\config.yaml"}'}}]},
        {"role": "tool", "content": "Error: connect ECONNREFUSED 127.0.0.1:8080"},
    ]
    st = checkpoint.update(st, msgs, proxy.count_tokens, proxy.extract_facts)
    check("检查点目标逐字", "8080" in st["goal"] and "8082" in st["goal"], st["goal"])
    check("检查点抓到硬约束",
          any("API_KEY=sk-123" in r["t"] for r in st["rules"]), str(st["rules"]))
    check("检查点抓到决策与理由",
          any("方案 B" in d["t"] and "回滚" in d["why"] for d in st["decisions"]),
          str(st["decisions"]))
    todos = {t["t"]: t["done"] for t in st["todos"]}
    check("检查点待办状态", todos.get("改配置") is False and todos.get("备份") is True,
          str(todos))
    check("检查点记录改写文件",
          any("config.yaml" in p for p in st["files_mod"]), str(st["files_mod"]))
    check("检查点记录错误",
          any("ECONNREFUSED" in e for e in st["errors"]), str(st["errors"]))

    # 第二轮增量：同一批消息不重复记账
    n_rules = len(st["rules"])
    st = checkpoint.update(st, msgs, proxy.count_tokens, proxy.extract_facts)
    check("检查点增量不重复", len(st["rules"]) == n_rules, str(st["rules"]))

    # 已完成的待办不复活
    checkpoint._add_todo(st, "改配置", True)
    checkpoint._add_todo(st, "改配置", False)
    check("完成待办不复活",
          next(t["done"] for t in st["todos"] if t["t"] == "改配置") is True,
          str(st["todos"]))


def test_checkpoint_render_budget():
    import checkpoint
    st = checkpoint.new_state("t")
    st["goal"] = "在 200K 上下文里保持记忆"
    st["rules"] = [{"k": f"r{i}", "t": f"必须遵守约束 {i}", "pri": 2}
                   for i in range(30)]
    st["todos"] = [{"k": f"t{i}", "t": f"待办 {i}", "done": False}
                   for i in range(30)]
    st["facts"] = [{"k": f"f{i}", "t": f"数值 {i}"} for i in range(50)]
    full = checkpoint.render(st, proxy.count_tokens, 100000)
    check("检查点块有头", full.startswith(checkpoint.MARK))
    check("检查点块有尾", full.rstrip().endswith(checkpoint.MARK_END))
    check("检查点逐字保留约束", "必须遵守约束 0" in full)
    tight = checkpoint.render(st, proxy.count_tokens, 120)
    check("小预算仍完整闭合", tight.rstrip().endswith(checkpoint.MARK_END))
    check("小预算裁减有留痕", "超出检查点预算" in tight or len(tight) < len(full),
          tight[-120:])


def test_checkpoint_inject():
    import checkpoint
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "当前问题"},
    ]
    block = checkpoint.MARK + " v1]\n## 目标\n- 测试\n" + checkpoint.MARK_END
    idx = checkpoint.inject(msgs, block)
    check("检查点恒定挂末尾", idx == 4, str(idx))
    check("检查点位置正确",
          msgs[4]["content"] == block and msgs[3]["content"] == "当前问题")
    check("检查点可被识别", checkpoint.is_mark(msgs[4]))
    n = checkpoint.strip_marks(msgs)
    check("检查点回显被清除", n == 1 and len(msgs) == 4)


def test_checkpoint_inject_agent_tail():
    """agent 工具链流量的最后一条 user 在 index 1，块必须挂末尾而不是插在它之前。

    插在 index 1 会每轮作废整段历史前缀：生产实测 reused 恒 13419（只剩
    system+tools），251k prompt 有 82% 墙钟在重读。"""
    import checkpoint
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
    ]
    block = checkpoint.MARK + " v1]\n## 目标\n- 测试\n" + checkpoint.MARK_END
    idx = checkpoint.inject(msgs, block)
    check("agent 流量块挂末尾", idx == 4 and msgs[-1]["content"] == block, str(idx))
    check("历史前缀逐字不动",
          [m.get("content") for m in msgs[:4]] == ["sys", "任务", None, "文件内容"],
          str([m.get("content") for m in msgs[:4]]))
    check("无 user 时仍挂末尾",
          checkpoint.inject([{"role": "system", "content": "s"}], block) == 1)


def test_checkpoint_inject_no_position_drift():
    """块的位置不能随历史形状漂移：漂移那一轮引擎前缀全作废。

    99k 实测：第一轮 [system, user] 插 index 1、第二轮出现 tool 结果后跳回
    末尾，turn 1 重读全部 99058 token（69s）；恒定末尾则共享 [system, user]。"""
    import checkpoint
    block = checkpoint.MARK + " v1]\n## 目标\n- 测试\n" + checkpoint.MARK_END
    hist = [{"role": "system", "content": "sys"}, {"role": "user", "content": "任务"}]
    shapes = [
        list(hist),
        hist + [{"role": "assistant", "content": "答"},
                {"role": "user", "content": "追问"}],
        hist + [{"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "文件"}],
    ]
    for n, msgs in enumerate(shapes):
        idx = checkpoint.inject(msgs, block)
        check(f"形状 {n} 块恒在末尾", idx == len(msgs) - 1, str(idx))
        check(f"形状 {n} 历史前缀逐字不动", msgs[:len(hist)] == hist,
              str(msgs[:len(hist)]))
    msgs = list(hist)
    check("空 block 返回 -1 且不改历史",
          checkpoint.inject(msgs, "") == -1 and msgs == hist)


def test_checkpoint_reset():
    import checkpoint
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(20):
        msgs.append({"role": "user", "content": f"问题 {i} " + "内容" * 200})
        msgs.append({"role": "assistant", "content": f"回答 {i} " + "内容" * 200})
    out = checkpoint.reset(msgs, "BLOCK", proxy.count_tokens, 800)
    check("复位后 system 在最前", out[0]["role"] == "system", str(out[:2]))
    check("复位后接着是检查点", out[1]["content"] == "BLOCK", str(out[1])[:40])
    check("复位确实变小", len(out) < len(msgs), f"{len(out)} vs {len(msgs)}")
    check("复位保留最近的对话", out[-1]["content"].startswith("回答 19"),
          str(out[-1])[:40])


def test_checkpoint_kb_sources():
    import checkpoint, json
    kb = [
        {"id": "a-1", "baseId": "b1", "conceptId": "API 520.pdf",
         "title": "API 520.pdf", "type": "file", "score": 0.9,
         "content": "9.2.1 整定压力\n"
                    "泄压阀的整定压力不得超过设计压力的 110%。\n"
                    "这是一段普通叙述，没有任何可引用的数值或约束。\n"
                    "最小排放口径 25 mm。"},
        {"id": "a-2", "baseId": "b1", "conceptId": "API 520.pdf",
         "title": "API 520.pdf", "type": "file", "score": 0.8,
         "content": "对于蒸汽工况，必须至少设置 2 个安全阀，且排放量不低于 100 kg/h。"},
        {"id": "b-1", "baseId": "b1", "conceptId": "ASME B31.3.docx",
         "title": "ASME B31.3.docx", "type": "file", "score": 0.7,
         "content": "ASME B31.3 规定：管道试验压力不得低于设计压力的 1.5 倍。"},
    ]
    st = checkpoint.new_state("t")
    # 故意放在 user 消息里：文档里的“必须”不能变成用户对助手的约束
    st = checkpoint.update(st, [{"role": "user", "content": json.dumps(kb, ensure_ascii=False)}],
                           proxy.count_tokens, proxy.extract_facts)
    check("知识库识别出两份来源", len(st["sources"]) == 2,
          str([s["t"] for s in st["sources"]]))
    api = next(s for s in st["sources"] if "API 520" in s["t"])
    asme = next(s for s in st["sources"] if "ASME B31.3" in s["t"])
    check("知识库来源保留逐字数值",
          any("110%" in ln for ln in api["lines"]), str(api["lines"]))
    check("知识库来源保留逐字约束",
          any("2 个安全阀" in ln and "100 kg/h" in ln for ln in api["lines"]),
          str(api["lines"]))
    check("知识库来源保留标准号",
          any("1.5 倍" in ln for ln in asme["lines"]), str(asme["lines"]))
    check("知识库纯叙述被过滤",
          not any("普通叙述" in ln for ln in api["lines"]), str(api["lines"]))
    check("知识库文档不污染用户硬约束", not st["rules"], str(st["rules"]))
    check("知识库来源记录 chunk id",
          "a-1" in api["ids"] and "a-2" in api["ids"], str(api["ids"]))
    block = checkpoint.render(st, proxy.count_tokens, 3000)
    check("回灌块包含来源标题", "API 520.pdf" in block and "ASME B31.3" in block)
    check("回灌块提示可按来源重查", "重新检索" in block)


def test_checkpoint_noise_filter():
    import checkpoint
    st = checkpoint.new_state("t")
    noise = (
        "<agent_identity>\n<facts>\n# 长期知识\n## 技能维护纪律\n"
        "改动任何技能都必须走防回退流程\nskill_release.py\n</facts>\n"
        "Tool loaded.\nOnly use these if relevant to the current work.\n"
        "You are powered by the model abc:def.\n"
    )
    st = checkpoint.update(st, [{"role": "user", "content": noise}],
                           proxy.count_tokens, proxy.extract_facts)
    check("harness 不设置目标", st["goal"] == "", str(st["goal"]))
    check("harness 不进硬约束", not st["rules"], str(st["rules"]))

    st = checkpoint.update(st, [{"role": "user",
                                 "content": "目标：迁移服务到新机器。\n必须保留端口 8082。"}],
                           proxy.count_tokens, proxy.extract_facts)
    check("真实目标仍能抽到", "迁移服务" in st["goal"], st["goal"])
    check("真实约束仍能抽到", any("8082" in r["t"] for r in st["rules"]),
          str(st["rules"]))

    st2 = checkpoint.new_state("t")
    st2 = checkpoint.update(st2, [{"role": "user",
                                   "content": "`- [ ]`、`- [x]`、TODO、下一步、待验证"}],
                            proxy.count_tokens, proxy.extract_facts)
    check("语法示例不变成待办", not st2["todos"], str(st2["todos"]))

    check("垃圾事实被过滤", not checkpoint._fact_ok(r"r:\n1"))
    check("正常事实保留", checkpoint._fact_ok("8082"))

    st3 = checkpoint.new_state("t")
    st3["goal"] = "Tool loaded."
    st3["rules"] = [
        {"k": "a", "t": "Only use these if relevant to the current work.", "pri": 2},
        {"k": "b", "t": "用 skill-creator 改（工作副本在 G: 的 work 目录）", "pri": 2},
        {"k": "c", "t": "**verify 抽查** —— 跑技能自检，必须全绿", "pri": 1},
    ]
    st3["todos"] = [{"k": "b", "t": "`- [ ]`、`- [x]`、TODO、下一步、待验证",
                     "done": False}]
    st3["facts"] = [{"k": "c", "t": r"r:\n1"}]
    st3["decisions"] = [
        {"k": "d1", "t": "旧助手叙述：longctx 已经改成检查点回灌。", "why": ""},
        {"k": "d2", "t": "决定采用方案 C，因为更快。", "why": "", "src": "user"},
    ]
    checkpoint.sanitize_state(st3)
    check("旧状态清理目标", not st3["goal"], st3["goal"])
    check("旧状态清理规则", not st3["rules"], str(st3["rules"]))
    check("旧状态清理待办", not st3["todos"], str(st3["todos"]))
    check("旧状态清理事实", not st3["facts"], str(st3["facts"]))
    check("旧决策只留用户来源",
          len(st3["decisions"]) == 1 and st3["decisions"][0]["k"] == "d2",
          str(st3["decisions"]))

    st5 = checkpoint.new_state("t")
    st5 = checkpoint.update(st5, [{
        "role": "user",
        "content": "```python\n# 必须保留端口 9999\ndef f():\n    pass\n```\n"
                   "必须保留真实端口 8082。"
    }], proxy.count_tokens, proxy.extract_facts)
    check("代码注释不算规则",
          not any("9999" in r["t"] for r in st5["rules"]), str(st5["rules"]))
    check("代码块外规则保留",
          any("8082" in r["t"] for r in st5["rules"]), str(st5["rules"]))

    st6 = checkpoint.new_state("t")
    st6["facts"] = [{"k": str(i), "t": f"id_{i}.py", "src": "other"}
                    for i in range(30)]
    block6 = checkpoint.render(st6, proxy.count_tokens, 5000)
    other_lines = [ln for ln in block6.splitlines()
                   if ln.startswith("- id_")]
    check("非用户事实最多 20 条", len(other_lines) <= 20, str(len(other_lines)))

    st7 = checkpoint.new_state("t")
    st7 = checkpoint.update(st7, [
        {"role": "tool", "content": "结果：186 passed, 0 failed"},
        {"role": "tool", "content": "Error: E1024"},
    ], proxy.count_tokens, proxy.extract_facts)
    check("0 failed 不算错误",
          not any("0 failed" in e for e in st7["errors"]), str(st7["errors"]))
    check("真错误保留",
          any("E1024" in e for e in st7["errors"]), str(st7["errors"]))


def test_checkpoint_role_scoped():
    import checkpoint
    st = checkpoint.new_state("t")
    st = checkpoint.update(st, [
        {"role": "assistant", "content": "我们决定采用方案 B，因为回滚简单。"},
        {"role": "tool", "content": "行号 2897，统计 2940"},
        {"role": "user", "content": "决定采用方案 C，因为更快。端口 8082。"},
    ], proxy.count_tokens, proxy.extract_facts)
    check("助手叙述不算决策",
          len(st["decisions"]) == 1 and "方案 C" in st["decisions"][0]["t"],
          str(st["decisions"]))
    check("用户端口保留", any(f["t"] == "8082" for f in st["facts"]),
          str(st["facts"]))
    check("用户端口标记来源",
          any(f["t"] == "8082" and f.get("src") == "user" for f in st["facts"]),
          str(st["facts"]))
    check("工具行号不进事实",
          not any(f["t"] == "2897" for f in st["facts"]), str(st["facts"]))

    st4 = checkpoint.new_state("t")
    st4["facts"] = [{"k": str(i), "t": str(1000 + i)} for i in range(20)]
    st4["facts"].append({"k": "p", "t": "8082", "src": "user"})
    block = checkpoint.render(st4, proxy.count_tokens, 5000)
    numeric = [ln for ln in block.splitlines()
               if ln.startswith("- ") and ln[2:].strip().isdigit()]
    check("裸数字最多 8 条", len(numeric) <= 9, str(numeric))
    check("用户端口优先保留", "- 8082" in block, block)


def test_checkpoint_cursor_replace():
    import checkpoint
    st = checkpoint.new_state("t")
    st = checkpoint.update(st, [{"role": "user", "content": "必须保留端口 8082。"}],
                           proxy.count_tokens, proxy.extract_facts)
    # 客户端原地改写同一条消息（消息条数不变）
    st = checkpoint.update(st, [{"role": "user", "content": "必须保留端口 9090。"}],
                           proxy.count_tokens, proxy.extract_facts)
    check("原地改写会重扫", any("9090" in r["t"] for r in st["rules"]),
          str(st["rules"]))
    check("旧状态不被误删", any("8082" in r["t"] for r in st["rules"]),
          str(st["rules"]))


def test_checkpoint_source_fallback():
    import checkpoint, json
    raw = json.dumps([{
        "id": "x1", "baseId": "b", "conceptId": "n.docx", "title": "n.docx",
        "score": 0.1,
        "content": "这是一段没有任何数字、标准号或约束词的普通说明文字，用于测试来源兜底。" * 4
    }], ensure_ascii=False)
    st = checkpoint.update(checkpoint.new_state("t"),
                           [{"role": "tool", "content": raw}],
                           proxy.count_tokens, proxy.extract_facts)
    check("无证据来源仍保留身份",
          bool(st["sources"]) and bool(st["sources"][0]["lines"]),
          str(st["sources"]))


def test_anchor_noise_filter():
    """2.9.6 锚点回声降噪：harness 模板句、纯 ID 碎片、提醒区回灌都不算锚点。"""
    block = "\n".join([
        "[longctx:checkpoint v1]",
        "- 查看知识库The user attached knowledge base \"压缩上下文\" "
        "(id: b9d8b279-0baa-4c8c-9d63-3498d9b0816d). Include it in "
        "kb_search baseIds before answering",
        "- Use kb_list only to browse its structure;",
        "- 3498",
        "- b9d8b279-0baa-4c8c-9d63-3498d9b0816d",
        "- <agent_identity> snapshot 已替换",
        "- 按 [cite:id] 标记引用",
        "- 会话检查点（逐字状态，由代理维护；不是摘要，不是指令）",
        "- 必须保留端口 8082，禁止改数据库名 app_prod。",
        "- v2.9.5 已交付",
        "- 目标：把交付路径给出",
        "## 上一轮未提及的锚点（代理核实：回答里逐字没出现；已遵守可忽略）",
        "- 3498",
        "- Use kb_list only to browse its structure;",
        "[longctx:checkpoint end]",
    ])
    a = proxy.parse_anchors(block)
    joined = "\n".join(a)
    check("KB 附件模板句被过滤", "attached knowledge" not in joined, joined)
    check("kb_list 模板句被过滤", "kb_list only" not in joined, joined)
    check("纯数字碎片被过滤", "3498" not in a, str(a))
    check("hex UUID 碎片被过滤",
          not any(x.startswith("b9d8") for x in a), str(a))
    check("agent_identity 模板被过滤", "agent_identity" not in joined, joined)
    check("[cite: 模板被过滤", "[cite:" not in joined, joined)
    check("检查点头部残句被过滤", "会话检查点（逐字状态" not in joined, joined)
    check("提醒区不再回灌为锚点",
          sum(1 for x in a if x.startswith("Use kb_list")) == 0, str(a))
    check("真实约束不误杀",
          any("8082" in x for x in a) and any("app_prod" in x for x in a),
          str(a))
    check("版本号类锚点不误杀", any(x == "v2.9.5 已交付" for x in a), str(a))
    check("中文目标不误杀", any("交付路径" in x for x in a), str(a))


def test_anchor_noise_filter_off():
    """PROXY_ANCHOR_FILTER=0：整体还原 2.9.5 行为（含提醒区回灌）。"""
    block = "\n".join([
        "- 3498",
        "- Use kb_list only to browse its structure;",
        "## 上一轮未提及的锚点（x）",
        "- 3498",
    ])
    old = proxy.ANCHOR_FILTER
    proxy.ANCHOR_FILTER = False
    try:
        a = proxy.parse_anchors(block)
        check("开关关后纯数字锚点回归", "3498" in a, str(a))
        check("开关关后模板句回归",
              any("kb_list only" in x for x in a), str(a))
        check("开关关后提醒区重新算锚点", a.count("3498") == 2, str(a))
    finally:
        proxy.ANCHOR_FILTER = old


def test_anchor_echo_after_filter():
    """过滤后回声判定只对真约束负责：噪声不进 missed，真约束未提及仍进。"""
    block = "\n".join([
        "- The user attached knowledge base \"压缩上下文\" kb_search baseIds",
        "- 3498",
        "- 必须保留端口 8082。",
    ])
    a = proxy.parse_anchors(block)
    missed = proxy.anchor_echo(a, "好的，这次没有提端口的事。")
    check("噪声锚点不参与回声统计", len(a) == 1, str(a))
    check("真约束未提及仍被识别", missed == a, str(missed))
    missed2 = proxy.anchor_echo(a, "已确认必须保留端口 8082。")
    check("真约束被回声则不算 missed", missed2 == [], str(missed2))


def test_harness_goal_sane():
    """真实形态：附件模板句与用户真话拼在同一行——剜句子，不丢真目标。"""
    import checkpoint
    st = checkpoint.new_state("t2")
    msg = ("查看知识库The user attached knowledge base \"压缩上下文\" "
           "(id: b9d8b279-0baa-4c8c-9d63-3498d9b0816d). Include "
           "\"b9d8b279-0baa-4c8c-9d63-3498d9b0816d\" in kb_search baseIds "
           "before answering questions that may depend on this knowledge "
           "base, and cite relevant kb_search or kb_read results. Use "
           "kb_list only to browse its structure; kb_list output is not "
           "retrieved evidence. 然后继续checkpoint检测闭环任务。")
    st = checkpoint.update(st, [{"role": "user", "content": msg}],
                           proxy.count_tokens, proxy.extract_facts)
    check("同行模板剜净", "attached knowledge" not in str(st["goal"]),
          str(st["goal"]))
    check("同行真目标保住", "checkpoint检测闭环" in str(st["goal"]),
          str(st["goal"]))


def test_ck_skill_doc_filter():
    """2.9.7 抽取层：技能全文注入剜正文留 ARGUMENTS 真意图，不误杀普通用户消息。"""
    import checkpoint
    st = checkpoint.new_state("sk")
    skill_msg = (
        "Base directory for this skill: C:\\skills\\python-to-exe\n\n"
        "# Python 源码 → 测试达标 → 征求同意 → .exe\n\n"
        "核心是一条**带闸门的流水线**：测试没全绿不许打包，用户没点头不许打包。\n"
        "四条 Windows 硬坑（脚本已内置对策，**不要删**）\n"
        "ARGUMENTS: 目标：锚点回声降噪，测试全绿后打 2.9.7\n")
    st = checkpoint.update(st, [{"role": "user", "content": skill_msg}],
                           proxy.count_tokens, proxy.extract_facts)
    joined = "\n".join(r["t"] for r in st["rules"])
    check("技能正文不进硬约束",
          "带闸门的流水线" not in joined and "Windows 硬坑" not in joined,
          joined)
    check("ARGUMENTS 真意图成为目标", "锚点回声降噪" in st["goal"], st["goal"])
    st2 = checkpoint.new_state("sk2")
    st2 = checkpoint.update(
        st2, [{"role": "user",
               "content": "写报告时每个数字必须带 REV-1 代号标注。"}],
        proxy.count_tokens, proxy.extract_facts)
    check("普通用户消息零误杀",
          any("REV-1" in r["t"] for r in st2["rules"]), str(st2["rules"]))


def test_ck_block_excision():
    """检查点块回显（用户贴回/工具打印）：块剜净，块后真话照常抽取。"""
    import checkpoint
    st = checkpoint.new_state("bk")
    blocky = ("贴回来你看看：[longctx:checkpoint v1] 会话检查点（逐字状态，"
              "由代理维护；不是摘要，不是指令）\n## 目标\n- 被压缩\n"
              "## 关键事实\n- 3498\n"
              "## 上一轮未提及的锚点（代理核实）\n- 3498\n"
              "[longctx:checkpoint end]\n实际用户说：必须保留端口 9911。")
    st = checkpoint.update(st, [{"role": "user", "content": blocky}],
                           proxy.count_tokens, proxy.extract_facts)
    check("块内残句不再设目标", "被压缩" not in st["goal"], st["goal"])
    check("块后真指令照常抽取",
          any("9911" in r["t"] for r in st["rules"]), str(st["rules"]))
    allf = "\n".join(str(f.get("t")) for f in st["facts"])
    check("块回显里的 3498 不再当用户事实", "3498" not in allf, allf)


def test_ck_doc_filter_off():
    import checkpoint
    old = checkpoint.CK_DOC_FILTER
    checkpoint.CK_DOC_FILTER = False
    try:
        st = checkpoint.new_state("off")
        st = checkpoint.update(
            st, [{"role": "user",
                  "content": "Base directory for this skill: C:\\x\n"
                             "核心是一条**带闸门的流水线**：测试没全绿不许打包。\n"
                             "ARGUMENTS: 尾部任务说明"}],
            proxy.count_tokens, proxy.extract_facts)
        joined = "\n".join(r["t"] for r in st["rules"])
        check("开关关后技能正文回归抽取", "带闸门的流水线" in joined, joined)
    finally:
        checkpoint.CK_DOC_FILTER = old


def test_anchor_remind_ttl():
    """3.1 待提醒 TTL：连续 3 轮不回声退役让位；回声后清龄可回队。"""
    key = "ttl-31"
    old_max, old_ttl = proxy.ANCHOR_REMIND_MAX, proxy.ANCHOR_TTL
    proxy.ANCHOR_REMIND_MAX = 3
    proxy.ANCHOR_TTL = 3
    for d in (proxy._MISSED, proxy._REMIND_AGE, proxy._RETIRED):
        d.pop(key, None)
    anchors = ["A1", "B2", "C3", "D4"]
    try:
        for i in range(1, 4):
            proxy.record_anchors(key, anchors, 0, [], anchors)
            check(f"TTL 前 {i} 轮照常提醒 A/B/C",
                  proxy._MISSED[key] == ["A1", "B2", "C3"],
                  str(proxy._MISSED[key]))
        proxy.record_anchors(key, anchors, 0, [], anchors)   # 第 4 轮
        check("第 4 轮 A/B/C 退役、槽位让给 D",
              proxy._MISSED[key] == ["D4"], str(proxy._MISSED[key]))
        check("退役集合记录 A/B/C", {"A1", "B2", "C3"} <= proxy._RETIRED[key],
              str(proxy._RETIRED[key]))
        proxy.record_anchors(key, anchors, 3, [], ["D4"])    # A/B/C 回声了
        check("回声项清龄并解除退役",
              "A1" not in proxy._RETIRED[key]
              and "A1" not in proxy._REMIND_AGE[key],
              str(proxy._RETIRED[key]))
        proxy.record_anchors(key, anchors, 0, [], anchors)   # 又全漏
        check("回声过的 A/B/C 重新回队",
              proxy._MISSED[key] == ["A1", "B2", "C3"],
              str(proxy._MISSED[key]))
    finally:
        proxy.ANCHOR_REMIND_MAX, proxy.ANCHOR_TTL = old_max, old_ttl
        for d in (proxy._MISSED, proxy._REMIND_AGE, proxy._RETIRED):
            d.pop(key, None)


def test_anchor_remind_ttl_off():
    old_max, old_ttl = proxy.ANCHOR_REMIND_MAX, proxy.ANCHOR_TTL
    key = "ttl-off-31"
    proxy.ANCHOR_REMIND_MAX = 3
    proxy.ANCHOR_TTL = 0
    for d in (proxy._MISSED, proxy._REMIND_AGE, proxy._RETIRED):
        d.pop(key, None)
    anchors = ["A1", "B2", "C3", "D4"]
    try:
        for _ in range(6):
            proxy.record_anchors(key, anchors, 0, [], anchors)
        check("TTL=0 永不退役（3.0 行为）",
              proxy._MISSED[key] == ["A1", "B2", "C3"]
              and not proxy._RETIRED[key], str(proxy._MISSED[key]))
    finally:
        proxy.ANCHOR_REMIND_MAX, proxy.ANCHOR_TTL = old_max, old_ttl
        for d in (proxy._MISSED, proxy._REMIND_AGE, proxy._RETIRED):
            d.pop(key, None)


def test_reseed_goal_on_reset():
    """3.1 复位重播 goal：换成最近真实用户消息；纯回显不重播。"""
    import checkpoint
    st = checkpoint.new_state("g31")
    st["goal"] = "旧任务：需网络通着啊"
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "旧任务：需网络通着啊"},
        {"role": "assistant", "content": "好"},
        {"role": "user",
         "content": "[longctx:checkpoint v1]\n## 目标\n- 霸位\n"
                    "[longctx:checkpoint end]\n顺带说一句"},
        {"role": "assistant", "content": "……"},
        {"role": "user", "content": "新任务：审计闭环记录，给出结论。"},
    ]
    ok = checkpoint.reseed_goal(st, msgs)
    check("复位重播换成最新任务", ok and "审计闭环记录" in st["goal"],
          st["goal"])
    check("旧 goal 不再霸位", "需网络" not in st["goal"], st["goal"])
    st2 = checkpoint.new_state("g31b")
    st2["goal"] = "旧"
    ok2 = checkpoint.reseed_goal(
        st2, [{"role": "user",
               "content": "[longctx:checkpoint v1]\n- x\n"
                          "[longctx:checkpoint end]"}])
    check("纯回显消息不重播", not ok2 and st2["goal"] == "旧", st2["goal"])


def test_calib_factor():
    """3.2 实测标定：k = usage_prompt/total 同点比，钳位、缺样本=1.0、可关。"""
    import time as _t
    key = "calib-t1"
    proxy._CONV_ACTUAL[key] = {"key": key, "body": 100000, "tools": 0,
                               "total": 200000, "usage_prompt": 100000,
                               "t": _t.time()}
    try:
        check("k = 实测/估算 = 0.5", abs(proxy._calib(key) - 0.5) < 1e-9,
              str(proxy._calib(key)))
        proxy._CONV_ACTUAL[key]["usage_prompt"] = 1000
        check("异常低比钳到 0.4", proxy._calib(key) == 0.4, str(proxy._calib(key)))
        proxy._CONV_ACTUAL[key]["usage_prompt"] = 900000
        check("异常高比钳到 2.5", proxy._calib(key) == 2.5, str(proxy._calib(key)))
        proxy._CONV_ACTUAL[key]["usage_prompt"] = None
        check("无实测样本 → 1.0（3.1 行为）", proxy._calib(key) == 1.0)
        old = proxy.CALIB
        proxy.CALIB = False
        try:
            proxy._CONV_ACTUAL[key]["usage_prompt"] = 100000
            check("PROXY_CALIB=0 整体关闭", proxy._calib(key) == 1.0)
        finally:
            proxy.CALIB = old
    finally:
        proxy._CONV_ACTUAL.pop(key, None)


def test_calib_gate_effect():
    """同一份报文：k=0.5 时不触发压缩、k=1.0 时触发——实测真改变决策。

    关掉检查点/钉住路径（本测只验阈值决策），会话键带纳秒戳保证前缀存储全新。
    """
    import time as _t
    old_ck, old_pin = proxy.CHECKPOINT, proxy.CHECKPOINT_PIN
    old_cov = proxy.DIGEST_COVERAGE
    proxy.CHECKPOINT = False
    proxy.CHECKPOINT_PIN = False
    proxy.DIGEST_COVERAGE = 0      # 本测只看阈值决策，不叠 E 的重试计数
    proxy.HOT_MSGS = 0
    proxy.HOT_TOKENS = 0
    proxy.THRESHOLD = 10000
    proxy.TARGET_MSG = 200
    proxy.MIN_MSG = 4000
    proxy.LOW_MSG = 1000          # 大消息(~12k tok)够候选：无校准时必须真压缩
    calls = []
    proxy.get_summary = lambda *a, **k: calls.append(1) or "SUMMARY"

    def body():
        big = "第一段事实：端口 8082。\n" * 900
        return {"messages": [
            {"role": "system", "content": f"sys-{_t.time_ns()}"},
            {"role": "user", "content": big},
            {"role": "assistant", "content": "好的。"},
            {"role": "user", "content": "总结端口。"}]}

    try:
        b1 = body()
        proxy.transform(b1, "calib-off")
        check("无实测样本：超估算阈值 → 压缩", len(calls) == 1, str(len(calls)))

        b2 = body()
        conv2 = proxy.conversation_key(b2["messages"])
        mt2 = proxy.request_tokens(b2)[0]
        proxy._CONV_ACTUAL[conv2] = {"key": conv2, "body": mt2, "tools": 0,
                                     "total": mt2,
                                     "usage_prompt": int(mt2 * 0.5),
                                     "t": _t.time()}
        try:
            proxy.transform(b2, "calib-on")
            check("k=0.5 实测校准后未超阈值 → 原样透传", len(calls) == 1,
                  str(len(calls)))
        finally:
            proxy._CONV_ACTUAL.pop(conv2, None)
    finally:
        proxy.CHECKPOINT, proxy.CHECKPOINT_PIN = old_ck, old_pin
        proxy.DIGEST_COVERAGE = old_cov


def test_judge_every():
    """3.2 判活降频：每 N 轮放行一次；EVERY=1 还原 3.1；无 missed 不调。"""
    key = "jdg-t"
    old_every, old_judge = proxy.ANCHOR_JUDGE_EVERY, proxy.ANCHOR_JUDGE
    proxy.ANCHOR_JUDGE = True
    proxy._JUDGE_TICK.pop(key, None)
    try:
        proxy.ANCHOR_JUDGE_EVERY = 3
        seq = [proxy._should_judge(key, ["a"]) for _ in range(6)]
        check("每 3 轮才放行一次",
              seq == [False, False, True, False, False, True], str(seq))
        proxy._JUDGE_TICK.pop(key, None)
        proxy.ANCHOR_JUDGE_EVERY = 1
        check("EVERY=1 还原每轮（3.1 行为）",
              all(proxy._should_judge(key, ["a"]) for _ in range(4)))
        check("无 missed 永不判活", not proxy._should_judge(key, []))
    finally:
        proxy.ANCHOR_JUDGE_EVERY, proxy.ANCHOR_JUDGE = old_every, old_judge
        proxy._JUDGE_TICK.pop(key, None)


def test_canary_probe():
    """3.3 B：探针注入/收编/计分/超窗判丢。"""
    conv = "cn-33"
    for d in (proxy._CANARY_SEQ, proxy._CANARY_STATE, proxy._CANARY_STATS):
        d.pop(conv, None)
    try:
        blk = proxy.canary_block("## 硬约束\n- 真约束\n[longctx:checkpoint end]", conv)
        check("探针挂到块尾且明说非任务", "## 探针（记忆测量标记" in blk
              and "- CAN-001" in blk, blk)
        a = proxy.parse_anchors(blk)
        check("探针被收编为锚点", "CAN-001" in a, str(a))
        st = proxy.canary_scan(conv, ["CAN-001"])
        check("未回声首轮：计龄不判丢", st == {"alive": 0, "lost": 0}, str(st))
        st = proxy.canary_scan(conv, [])
        check("回声判活", st["alive"] == 1 and st["lost"] == 0, str(st))
        proxy.canary_block("## 硬约束\n- 真约束\n[longctx:checkpoint end]", conv)
        for _ in range(4):
            st = proxy.canary_scan(conv, ["CAN-002"])
        check("超窗（3 轮）判丢", st["lost"] == 1, str(st))
        proxy.record_anchors(conv, ["真约束A", "CAN-003"], 0, [],
                             ["真约束A", "CAN-003"])
        check("探针不占提醒槽", proxy._MISSED.get(conv) == ["真约束A"],
              str(proxy._MISSED.get(conv)))
    finally:
        for d in (proxy._CANARY_SEQ, proxy._CANARY_STATE, proxy._CANARY_STATS):
            d.pop(conv, None)


def test_throttle_backoff():
    """3.3 D：429 指数退避封顶 600s；正常清零；退避内停判活。"""
    import time as _t
    old = dict(proxy._THROTTLE)
    old_every = proxy.ANCHOR_JUDGE_EVERY
    try:
        proxy._THROTTLE.update(until=0.0, streak=0)
        proxy.ANCHOR_JUDGE_EVERY = 1
        check("未退避时正常", not proxy._throttled())
        proxy.note_throttle(429)
        check("429 起退避", proxy._throttled(), str(proxy._THROTTLE))
        check("退避中不跑判活", not proxy._should_judge("x33", ["a"]))
        t1 = proxy._THROTTLE["until"]
        proxy.note_throttle(429)
        check("连续 429 退避窗变宽", proxy._THROTTLE["until"] > t1)
        for _ in range(6):
            proxy.note_throttle(429)
        check("退避封顶 600s", proxy._THROTTLE["until"] - _t.time() <= 601)
        proxy.note_throttle(200)
        check("正常响应清零连击", proxy._THROTTLE["streak"] == 0)
    finally:
        proxy._THROTTLE.update(old)
        proxy.ANCHOR_JUDGE_EVERY = old_every
        proxy._JUDGE_TICK.pop("x33", None)


def test_digest_coverage_retry():
    """3.3 E：摘要事实保留率低于阈值 → 带提示重试并取优。"""
    old_cov = proxy.DIGEST_COVERAGE
    seq = []

    def fake(text, task, cc=None):
        seq.append(task)
        return "保留了 8082"

    proxy.get_summary = fake
    joined = "必须保留端口 8082。" + "内容" * 300
    try:
        proxy.DIGEST_COVERAGE = 0.5
        out = proxy._coverage_retry(joined, "烂摘要没有事实", "T", None)
        check("低覆盖触发重试取优", "8082" in out and len(seq) == 1, out)
        seq.clear()
        out2 = proxy._coverage_retry(joined, "保留了 8082", "T", None)
        check("高覆盖不重试", out2 == "保留了 8082" and not seq)
        proxy.DIGEST_COVERAGE = 0
        seq.clear()
        out3 = proxy._coverage_retry(joined, "烂摘要", "T", None)
        check("开关 0=关", out3 == "烂摘要" and not seq)
    finally:
        proxy.DIGEST_COVERAGE = old_cov


def test_provenance_turn():
    """3.3 C：新收规则带来源消息号 w，旧字段不破坏。"""
    import checkpoint
    st = checkpoint.new_state("prov33")
    st = checkpoint.update(st, [{"role": "user", "content": "必须保留端口 8083。"}],
                           proxy.count_tokens, proxy.extract_facts)
    r = next((x for x in st["rules"] if "8083" in x["t"]), None)
    check("规则带来源消息号", r is not None and r.get("w") == 0, str(r))
    check("旧字段完好", r is not None and r.get("src") == "user", str(r))


def test_corpus_purge():
    """3.3 F：存量技能文档文本按语料签名清除；真实约束不误杀。"""
    import checkpoint
    import os as _os
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="corpus33-"))
    (d / "skill-x").mkdir()
    (d / "skill-x" / "SKILL.md").write_text(
        "核心是一条带闸门的流水线：测试没全绿不许打包，用户没点头不许打包。\n"
        "别的无关文字。\n", encoding="utf-8")
    old_env = _os.environ.get("PROXY_SKILL_CORPUS")
    _os.environ["PROXY_SKILL_CORPUS"] = str(d)
    checkpoint._CORPUS_CACHE.update(blob="", built=0.0)
    try:
        st = checkpoint.new_state("cp33")
        st["rules"] = [
            {"k": "a", "t": "核心是一条带闸门的流水线：测试没全绿不许打包，"
                            "用户没点头不许打包。", "pri": 1, "src": "user"},
            {"k": "b", "t": "必须保留端口 8084，禁止改库名 app_prod。",
             "pri": 1, "src": "user"},
        ]
        n = checkpoint.purge_corpus_legacy(st)
        check("技能文本条被清", n == 1 and len(st["rules"]) == 1, str(st["rules"]))
        check("真实约束保留", "8084" in st["rules"][0]["t"], str(st["rules"]))
        check("旗标后不重复清", checkpoint.purge_corpus_legacy(st) == 0)
    finally:
        if old_env is None:
            _os.environ.pop("PROXY_SKILL_CORPUS", None)
        else:
            _os.environ["PROXY_SKILL_CORPUS"] = old_env
        checkpoint._CORPUS_CACHE.update(blob="", built=0.0)
        shutil.rmtree(d, ignore_errors=True)


def test_kpi_write():
    """3.3 A：kpi.jsonl 逐行追加、可解析。"""
    import json
    import shutil
    import tempfile
    old = proxy.KPI_PATH
    d = Path(tempfile.mkdtemp(prefix="kpi33-"))
    proxy.KPI_PATH = d / "kpi.jsonl"
    try:
        proxy.kpi_write({"t": "1", "a": 3, "e": 1})
        proxy.kpi_write({"t": "2", "a": 4, "e": 2})
        lines = (d / "kpi.jsonl").read_text(encoding="utf-8").splitlines()
        check("追加两行", len(lines) == 2, str(lines))
        check("JSON 可解析", json.loads(lines[1])["a"] == 4)
    finally:
        proxy.KPI_PATH = old
        shutil.rmtree(d, ignore_errors=True)


def test_parse_anchors_typed():
    """3.4：锚点按节区分型——约束类进分母，数据类只查回声。"""
    block = "\n".join([
        "## 目标\n- 完成蓝线可解释",
        "## 硬约束（逐字，不得改写）\n- 必须保留端口 9001",
        "## 已定决策\n- 定了用 ScrollColumn 包裹",
        "## 关键事实（逐字）\n- D:\\Strata\\x\\y.png\n- REV-4471",
        "## 进度\n- 扫描点重写",
        "## 探针\n- CAN-001",
        "## 上一轮未提及的锚点（x）\n- 不该进来",
        "[longctx:checkpoint end]",
    ])
    typed = proxy.parse_anchors_typed(block)
    d = dict(typed)
    check("目标/约束/决策=rule",
          d.get("完成蓝线可解释") == "rule"
          and d.get("必须保留端口 9001") == "rule"
          and d.get("定了用 ScrollColumn 包裹") == "rule", str(typed))
    check("路径/事实/进度=data",
          d.get("D:\\Strata\\x\\y.png") == "data"
          and d.get("REV-4471") == "data"
          and d.get("扫描点重写") == "data", str(typed))
    check("探针=probe（测量标记，不是约束）", d.get("CAN-001") == "probe", str(typed))
    check("提醒区仍不进锚点", "不该进来" not in d, str(typed))


def test_data_evict_aging():
    """3.4：数据锚连续 8 轮不回声判弃；回声清龄；约束类永不自动删。"""
    key = "devict-34"
    old = proxy.ANCHOR_EVICT
    proxy.ANCHOR_EVICT = 3
    proxy._DATA_AGE.pop(key, None)
    typed = [("必须保留 9002", "rule"), ("C:\\tmp\\a.png", "data"),
             ("CAN-001", "rule")]
    try:
        for i in range(1, 4):
            drop = proxy.data_evict(key, typed, ["C:\\tmp\\a.png", "CAN-001"])
            check(f"前 3 轮不弃（龄 {i}）", drop == [], str(drop))
        drop = proxy.data_evict(key, typed, ["C:\\tmp\\a.png", "CAN-001"])
        check("第 4 轮超窗判弃", drop == ["C:\\tmp\\a.png"], str(drop))
        drop = proxy.data_evict(key, typed, ["CAN-001"])   # 路径回声了
        check("回声过/已弃的不复现", drop == [], str(drop))
        drop = proxy.data_evict(key, typed, ["必须保留 9002", "CAN-001"])
        check("约束类永不自动删", drop == [], str(drop))
        proxy.ANCHOR_EVICT = 0
        proxy._DATA_AGE.pop(key, None)
        for _ in range(5):
            drop = proxy.data_evict(key, typed, ["C:\\tmp\\a.png"])
        check("开关 0=关", drop == [], str(drop))
    finally:
        proxy.ANCHOR_EVICT = old
        proxy._DATA_AGE.pop(key, None)


def test_drop_data_keeps_rules():
    """3.4 状态删除：只动事实/进度/错误，规则/待办/决策一字不碰。"""
    import checkpoint
    st = checkpoint.new_state("dd34")
    st["facts"] = [{"t": "C:\\tmp\\a.png"}, {"t": "真实现 9003"}]
    st["progress"] = ["旧进度一条"]
    st["errors"] = []
    st["rules"] = [{"k": "x", "t": "必须保留端口 9003", "pri": 1, "src": "user"}]
    st["todos"] = [{"k": "y", "t": "待办：保留", "done": False}]
    n = checkpoint.drop_data(st, ["C:\\tmp\\a.png", "旧进度一条",
                                  "必须保留端口 9003"])
    check("删数据不删约束", n == 2
          and len(st["facts"]) == 1 and st["facts"][0]["t"] == "真实现 9003"
          and st["rules"] and st["todos"], str(n))


def test_reset_uses_passed_k():
    """3.4.2：复位校准必须由调用方显式传 k。

    生产实况：CACHE BREAK 重压后 conv 漂移，复位内部 _calib 落空按 1.0 算，
    268k×1.0 < 288k 复位永不触发，全靠每轮 CACHE BREAK 硬顶。"""
    big = "第一段事实内容说明。" * 600          # 单条估算 ~4.8k，全体 ~58k
    # 体量必须大于复位目标(40.5k)：否则 reset 判"无需切"是正确行为，测不出触发
    msgs = [{"role": "system", "content": "sys-kfix"}]
    for i in range(12):
        msgs.append({"role": "user", "content": f"第{i}段：" + big})
        msgs.append({"role": "assistant", "content": f"收到{i}"})
    blk = ("[longctx:checkpoint v1] 会话检查点（逐字状态）\n## 目标\n- 测试\n"
           "[longctx:checkpoint end]")
    proxy.checkpoint.inject(msgs, blk)
    body = {"messages": msgs}
    final = proxy.request_tokens(body)[2]
    conv = proxy.conversation_key(msgs)
    n0 = len(msgs)
    old_ck, old_reset = proxy.CHECKPOINT, proxy.CHECKPOINT_RESET
    proxy.CHECKPOINT = True          # 本文件第 28 行全局关了检查点，本测要自开
    proxy.CHECKPOINT_RESET = 0.90
    try:
        proxy._maybe_checkpoint_reset(body, "kfix", final)     # 无实测样本 k=1.0
        check("k=1.0 未超线不误触", len(body["messages"]) == n0,
              f"{n0} -> {len(body['messages'])}")
        proxy._maybe_checkpoint_reset(body, "kfix", final, k=2.5)
        check("显式传 k 复位触发（消息数骤减）",
              len(body["messages"]) < n0, f"{n0} -> {len(body['messages'])}")
    finally:
        proxy._RESET_MEM.pop(conv, None)
        proxy.CHECKPOINT, proxy.CHECKPOINT_RESET = old_ck, old_reset


def test_reset_counts_tool_blocks():
    """3.4.3：reset 圈体必须认 tool_use/tool_result/tool_calls。

    生产实测（14:22-14:23）：工具链会话里 checkpoint._text 对这些块返回空，
    733 条消息被当成 ~0 token → 复位触发却一刀没切（733->733，usage 不降）。
    """
    import json
    import checkpoint
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(60):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"c{i}", "name": "Bash",
             "input": {"command": "grep -r 上下文 " + "路径abc" * 300}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"c{i}",
             "content": "结果行\n" * 400}]})
    blk = "[longctx:checkpoint v1] 测试\n[longctx:checkpoint end]"
    out = checkpoint.reset(msgs, blk, proxy.count_tokens, 30000)
    check("工具链会话真被切开", len(out) < len(msgs) and len(out) < 40,
          f"{len(msgs)} -> {len(out)}")
    check("切后保留块与热尾",
          any(checkpoint.is_mark(m) for m in out)
          and sum(proxy.count_tokens(checkpoint._size_text(m)) for m in out) < 40000,
          str([m.get("role") for m in out[-2:]]))
    # OpenAI 形态：assistant.tool_calls 载荷同样要计入
    msgs2 = [{"role": "system", "content": "s"}]
    for i in range(40):
        msgs2.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"t{i}", "type": "function", "function": {
                "name": "w", "arguments": json.dumps({"p": "x" * 4000})}}]})
        msgs2.append({"role": "tool", "tool_call_id": f"t{i}", "content": "ok"})
    out2 = checkpoint.reset(msgs2, blk, proxy.count_tokens, 20000)
    check("tool_calls 形态也被切开", len(out2) < len(msgs2),
          f"{len(msgs2)} -> {len(out2)}")


def test_record_sent_keeps_usage():
    """3.4.6：record_sent 重建实况字典必须继承 usage_prompt。

    不继承的话，每轮发出后、响应回来前（长上下文 30-60s）GUI 实况的
    「引擎实测」整段显示"—"（用户实测截图 14:35:53）。"""
    key = "rs-usage-346"
    proxy._CONV_ACTUAL[key] = {"key": key, "body": 1, "tools": 0, "total": 1,
                               "usage_prompt": 123456, "t": 0.0}
    try:
        proxy.record_sent(key, 2000, 500, 2500, 9000)
        check("发出瞬间继承上轮实测",
              proxy._CONV_ACTUAL[key].get("usage_prompt") == 123456,
              str(proxy._CONV_ACTUAL[key].get("usage_prompt")))
        proxy.record_usage(key, 654321)
        check("响应回写覆盖旧值",
              proxy._CONV_ACTUAL[key].get("usage_prompt") == 654321,
              str(proxy._CONV_ACTUAL[key].get("usage_prompt")))
    finally:
        proxy._CONV_ACTUAL.pop(key, None)


def test_model_catalog_routing():
    """3.6：多目标目录——解析、逐请求选路、/v1/models 供给、非法行剔除。"""
    import json as _j
    old = proxy.CATALOG
    proxy.CATALOG = proxy._parse_catalog(_j.dumps([
        {"id": "kimi-k3.1-pro", "base_url": "https://api.moonshot.cn/v1",
         "api_key": "sk-k"},
        {"id": "qwen3.8-flash", "base_url": "https://token-plan.example/compatible-mode/v1"},
        {"id": "", "base_url": "x"},
        {"id": "no-url"},
    ]))
    try:
        r = proxy.route_for("kimi-k3.1-pro")
        check("目录命中返回路由行", r and r.get("api_key") == "sk-k", str(r))
        check("未命中返回 None（回落启用行）", proxy.route_for("gpt-9") is None)
        check("空模型名视为未命中", proxy.route_for("") is None)
        check("非法行被剔除", len(proxy.CATALOG) == 2 and "no-url" not in proxy.CATALOG,
              str(list(proxy.CATALOG)))
        data = _j.loads(proxy.models_catalog_json())
        check("同步目录含全部模型",
              data["object"] == "list"
              and sorted(x["id"] for x in data["data"])
              == ["kimi-k3.1-pro", "qwen3.8-flash"], str(data))
        scheme, host, port, prefix = proxy.parse_upstream(r["base_url"])
        check("路由行地址可解析",
              host == "api.moonshot.cn" and prefix == "/v1",
              str((scheme, host, port, prefix)))
    finally:
        proxy.CATALOG = old


def test_upstream_retry_gate():
    """3.7：连接级错误可重发，超时不重发（上游可能已在处理，重发会双计费）。"""
    import ssl
    import socket as _s
    check("TLS EOF 可重试", proxy._retryable(ssl.SSLEOFError("EOF")))
    check("连接重置可重试", proxy._retryable(ConnectionResetError("reset")))
    check("超时不重试", not proxy._retryable(_s.timeout("t")))
    check("普通异常不重试", not proxy._retryable(ValueError("x")))


def test_reset_watchdog():
    """3.8 A：复位触发但没切动 → 计数到 3 报警；真切动 → 清零。"""
    import checkpoint
    big = "第一段事实内容说明。" * 600

    def body(n_big, seed):
        msgs = [{"role": "system", "content": f"sys-wd-{seed}"}]
        for i in range(n_big):
            msgs.append({"role": "user", "content": f"第{i}段：" + big})
            msgs.append({"role": "assistant", "content": f"收到{i}"})
        checkpoint.inject(msgs, "[longctx:checkpoint v1] 测试\n"
                                "[longctx:checkpoint end]")
        return msgs

    old_ck, old_reset = proxy.CHECKPOINT, proxy.CHECKPOINT_RESET
    proxy.CHECKPOINT, proxy.CHECKPOINT_RESET = True, 0.9
    try:
        # 每轮重建 body：复位输出会把块消息挪到下标 1，对同一 body 连调会让
        # conversation_key 漂移（生产每轮只调一次、客户端历史不含块，无此问题）
        conv = proxy.conversation_key(body(5, "noop"))
        for _ in range(3):
            msgs = body(5, "noop")      # 总量 < 复位目标：触发但不切
            b = {"messages": msgs}
            final = proxy.request_tokens(b)[2]
            proxy._maybe_checkpoint_reset(b, "wd", final, k=50.0)
        check("空转 3 轮 → 看门狗计到 3",
              proxy._RESET_WATCH.get(conv) == 3, str(proxy._RESET_WATCH.get(conv)))
        msgs2 = body(40, "cut")         # 总量远超目标：真切
        b2 = {"messages": msgs2}
        final2 = proxy.request_tokens(b2)[2]
        conv2 = proxy.conversation_key(msgs2)
        proxy._maybe_checkpoint_reset(b2, "wd2", final2, k=50.0)
        check("真切动 → 清零且消息数降",
              proxy._RESET_WATCH.get(conv2, 0) == 0
              and len(b2["messages"]) < len(msgs2),
              str(proxy._RESET_WATCH.get(conv2)))
    finally:
        proxy.CHECKPOINT, proxy.CHECKPOINT_RESET = old_ck, old_reset
        proxy._RESET_WATCH.clear()
        proxy._RESET_MEM.clear()


def test_reset_noop_keeps_prefix():
    """3.29：reset 切不动（全体 < 复位目标，但 k 虚高超线）时，不得把检查点块
    从末尾挪到 system 之后——那是纯亏的前缀断裂（实测 reused 恒 14016）。
    有牙核验：旧代码会把块挪到下标 1，"第 2 条不是块" 必红。"""
    import checkpoint
    big = "事实内容说明。" * 200          # 单条 ~1k，全体远小于复位目标
    msgs = [{"role": "system", "content": "sys-noop-prefix"}]
    for i in range(5):                  # 12 条 > 8，避开 len<=8 早退；全体仍 < 目标
        msgs.append({"role": "user", "content": f"第{i}段：" + big})
        msgs.append({"role": "assistant", "content": f"收到{i}"})
    blk = "[longctx:checkpoint v1] 测试\n[longctx:checkpoint end]"
    checkpoint.inject(msgs, blk)        # 块在末尾
    body = {"messages": msgs}
    final = proxy.request_tokens(body)[2]
    conv = proxy.conversation_key(msgs)   # 注入后再算，与 _maybe_checkpoint_reset 内部一致
    n0 = len(msgs)
    saved = (proxy.CHECKPOINT, proxy.CHECKPOINT_RESET, proxy.CONTEXT,
             proxy.WORK_WINDOW, proxy.RESERVE)
    proxy.CHECKPOINT, proxy.CHECKPOINT_RESET = True, 0.9
    proxy.CONTEXT, proxy.WORK_WINDOW, proxy.RESERVE = 100000, 100000, 1000
    try:
        # k=50 把估算顶过复位线（90k），但全体 ~4k < 复位目标（40.5k）→ 切不动
        proxy._maybe_checkpoint_reset(body, "noopfix", final, k=50.0)
        check("切不动 → 消息数不变", len(body["messages"]) == n0,
              f"{n0} -> {len(body['messages'])}")
        check("切不动 → 检查点块仍在末尾（前缀头部未改写）",
              checkpoint.is_mark(body["messages"][-1]),
              str([m.get("role") for m in body["messages"][-2:]]))
        check("切不动 → 第 2 条不是检查点块（没挪到头部）",
              not checkpoint.is_mark(body["messages"][1]),
              "block moved to head")
        check("切不动 → 看门狗计数 ≥1", proxy._RESET_WATCH.get(conv, 0) >= 1,
              str(proxy._RESET_WATCH.get(conv)))
    finally:
        (proxy.CHECKPOINT, proxy.CHECKPOINT_RESET, proxy.CONTEXT,
         proxy.WORK_WINDOW, proxy.RESERVE) = saved
        proxy._RESET_WATCH.pop(conv, None)
        proxy._RESET_MEM.pop(conv, None)


def test_clamp_calibrated_thinking():
    """3.31：clamp 按实测校准算剩余窗口，中文虚高估算不再误杀思考预算。

    复现生产事故（proxy.log 07:41:33）：估算 330843、context 389120，裸算剩余
    57765 把 100k 思考（max_tokens 133334）夹死；引擎实测 prompt 仅 147804
    （k≈0.45），校准后剩余 ~240k 装得下完整 133334。
    有牙核验：k=None 必复现 57765 误杀；传 k 后保住 133334。窗口真不够时仍夹取。
    另验 _apply_max_think 把 _calib(conv) 接进 clamp（conv 缺失退回 None 旧行为）。"""
    import time as _t
    saved = (proxy.CONTEXT, proxy.CTX_SLACK, proxy.CALIB)
    proxy.CONTEXT, proxy.CTX_SLACK, proxy.CALIB = 389120, 512, True
    try:
        # --- A. clamp 数值行为：同一份虚高估算，传 k 与不传 k ---
        b_old = {"max_tokens": 133334}
        proxy.clamp_max_tokens(b_old, 330843, "clamp-old")          # k=None 旧行为
        check("无校准复现误杀：133334 -> 57765", b_old["max_tokens"] == 57765,
              str(b_old["max_tokens"]))
        b_new = {"max_tokens": 133334}
        proxy.clamp_max_tokens(b_new, 330843, "clamp-new", k=0.45)  # 实测校准
        check("校准后保住完整思考预算：仍 133334", b_new["max_tokens"] == 133334,
              str(b_new["max_tokens"]))
        # 窗口真不够（校准后 prompt 仍逼近上限）→ 兜底夹取不失效
        b_tight = {"max_tokens": 133334}
        proxy.clamp_max_tokens(b_tight, 800000, "clamp-tight", k=0.45)
        check("窗口真不够仍夹取（兜底未失效）", b_tight["max_tokens"] < 133334,
              str(b_tight["max_tokens"]))

        # --- B. _apply_max_think 把 _calib(conv) 接进 clamp ---
        conv = "clamp-wire-331"
        proxy._CONV_ACTUAL[conv] = {"key": conv, "body": 100000, "tools": 0,
                                    "total": 200000, "usage_prompt": 100000,
                                    "t": _t.time()}        # k = 0.5
        seen = []
        real_clamp = proxy.clamp_max_tokens
        proxy.clamp_max_tokens = (
            lambda body, total, tag, cap=None, k=None: seen.append(k))
        try:
            body = {"max_tokens": 8192,
                    "messages": [{"role": "user", "content": "你好"}]}
            proxy._apply_max_think(body, "wire", 100000, conv=conv)
            check("思考预算写入 100000", body.get("reasoning_budget_tokens") == 100000,
                  str(body.get("reasoning_budget_tokens")))
            check("max_tokens 抬到 133334", body.get("max_tokens") == 133334,
                  str(body.get("max_tokens")))
            check("clamp 收到 k=_calib(conv)=0.5", seen == [0.5], str(seen))
            seen.clear()
            proxy._apply_max_think({"max_tokens": 8192, "messages": []},
                                   "wire-none", 100000)            # conv 缺失
            check("conv 缺失 → k=None（退回旧行为）", seen == [None], str(seen))
        finally:
            proxy.clamp_max_tokens = real_clamp
            proxy._CONV_ACTUAL.pop(conv, None)
    finally:
        proxy.CONTEXT, proxy.CTX_SLACK, proxy.CALIB = saved


def test_hyg_persist_roundtrip():
    """3.8 B：卫生计数进状态再回来，重启不清零。"""
    conv = "hyg-38"
    dicts = (proxy._DATA_AGE, proxy._REMIND_AGE, proxy._RETIRED, proxy._MISSED)
    for d in dicts:
        d.pop(conv, None)
    try:
        proxy._DATA_AGE[conv] = {"C:\\x.png": 5}
        proxy._REMIND_AGE[conv] = {"旧约束": 2}
        proxy._RETIRED[conv] = {"退役条"}
        proxy._MISSED[conv] = ["待提醒条"]
        st = {}
        proxy.persist_hyg(conv, st)
        for d in dicts:
            d.pop(conv, None)          # 模拟重启
        proxy.hydrate_hyg(conv, st)
        check("往返一致",
              proxy._DATA_AGE[conv] == {"C:\\x.png": 5}
              and proxy._REMIND_AGE[conv] == {"旧约束": 2}
              and proxy._RETIRED[conv] == {"退役条"}
              and proxy._MISSED[conv] == ["待提醒条"],
              str((proxy._DATA_AGE.get(conv), proxy._MISSED.get(conv))))
    finally:
        for d in dicts:
            d.pop(conv, None)


def test_atomic_write():
    """3.8 H：原子写无半文件、无 .tmp 残留。"""
    import sys as _sys
    import shutil
    import tempfile
    _sys.path.insert(0, str(Path(__file__).resolve().parent / "app-src"))
    import main as M
    d = Path(tempfile.mkdtemp(prefix="aw38-"))
    p = d / "t.json"
    try:
        M._atomic_write(p, '{"a": 1}')
        check("内容正确", p.read_text(encoding="utf-8") == '{"a": 1}')
        check("无 tmp 残留", not (d / "t.json.tmp").exists())
        M._atomic_write(p, '{"a": 2}')
        check("覆盖正确", p.read_text(encoding="utf-8") == '{"a": 2}')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_drop_only_policy():
    """3.10：只丢不摘——超预算整条丢弃最旧原文，保留消息一字不改、不出摘要。"""
    old = proxy.DROP_ONLY
    proxy.DROP_ONLY = 1
    try:
        big = "配置项说明文本。" * 4000
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(30):
            msgs.append({"role": "user", "content": f"第{i}条：{big}"})
            msgs.append({"role": "assistant", "content": "收到"})
        body = {"messages": msgs}
        before = [m["content"] for m in msgs]
        proxy.transform(body, "drop-only")
        out = body["messages"]
        check("超预算后消息被丢弃", len(out) < len(before), str(len(out)))
        kept = [m.get("content") for m in out
                if isinstance(m, dict) and m.get("role") == "user"]
        check("保留消息原文未改",
              all(c in before for c in kept), str(kept[:1])[:60])
        check("无摘要痕迹",
              all("[长上下文压缩摘要]" not in str(c) for c in kept))
    finally:
        proxy.DROP_ONLY = old


def test_cold_probe_register():
    """3.13 ①：只丢不摘丢弃旧消息时，逐字标识符入冷存表（限量、滤纯数字）。"""
    import re as _re
    saved = (proxy.DROP_ONLY, proxy.COLD_PROBE, proxy.COLD_PICK, proxy.COLD_MAX)
    proxy._COLD.clear()
    proxy.DROP_ONLY, proxy.COLD_PROBE = 1, True
    proxy.COLD_PICK, proxy.COLD_MAX = 4, 20
    try:
        pad = "配置项说明文本。" * 3000
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(30):
            msgs.append({"role": "user",
                         "content": f"第{i}条：部署在 192.168.1.{i}，版本 "
                                    f"v2.5.{i}，路径 D:\\Strata\\f{i}.bin。{pad}"})
            msgs.append({"role": "assistant", "content": "收到"})
        proxy.transform({"messages": msgs}, "cold-reg")
        tables = [t for t in proxy._COLD.values() if t]
        check("丢弃时入冷存表", bool(tables), str(proxy._COLD)[:80])
        vals = set().union(*tables) if tables else set()
        check("抽的是逐字标识符",
              any("192.168.1." in v for v in vals)
              and any(".bin" in v for v in vals), str(list(vals))[:160])
        check("纯数字不进表（IP 除外）",
              not any(_re.fullmatch(r"[\d.,:/%-]+", v)
                      and not _re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", v)
                      for v in vals), str(list(vals))[:120])
        check("冷存表封顶 COLD_MAX", all(len(t) <= 20 for t in tables),
              str([len(t) for t in tables]))
    finally:
        (proxy.DROP_ONLY, proxy.COLD_PROBE,
         proxy.COLD_PICK, proxy.COLD_MAX) = saved
        proxy._COLD.clear()


def test_cold_probe_reanchor_lost():
    """3.13 ①②：自发回声=活；超龄重锚一次（逐字进块、不进分母）；再超龄=丢。"""
    saved = (proxy.COLD_PROBE, proxy.COLD_AGE, proxy.COLD_RECALL_WINDOW)
    proxy._COLD.clear()
    proxy._COLD_STATS.clear()
    proxy.COLD_PROBE = True
    proxy.COLD_AGE, proxy.COLD_RECALL_WINDOW = 3, 3
    try:
        proxy._COLD["cc"] = {"D:\\Strata\\model.bin": {"age": 0, "re": 0},
                             "ops@example.com": {"age": 0, "re": 0}}
        s = proxy.cold_scan("cc", "已更新 D:\\Strata\\model.bin 完成")
        check("自发回声判活",
              s["alive"] == 1
              and "D:\\Strata\\model.bin" not in proxy._COLD["cc"], str(s))
        for _ in range(4):
            s = proxy.cold_scan("cc", "无关内容")
        check("超龄重锚一次",
              s["re"] == 1 and proxy._COLD["cc"]["ops@example.com"]["re"] == 1,
              str(s))
        blk = proxy.cold_reanchor_block(
            "## 目标\n- x\n[longctx:checkpoint end]", "cc")
        check("重锚逐字进块", "ops@example.com" in blk
              and proxy.COLD_MARK in blk, blk[:120])
        check("重锚不进锚点分母",
              not any("ops@" in p for p in proxy.parse_anchors(blk)),
              str(proxy.parse_anchors(blk)))
        check("重锚不进分型表",
              not any("ops@" in t for t, _ in proxy.parse_anchors_typed(blk)),
              str(proxy.parse_anchors_typed(blk)))
        for _ in range(4):
            s = proxy.cold_scan("cc", "还是无关")
        check("重锚宽限后判丢", s["lost"] == 1 and not proxy._COLD["cc"],
              str(s))
    finally:
        (proxy.COLD_PROBE, proxy.COLD_AGE,
         proxy.COLD_RECALL_WINDOW) = saved
        proxy._COLD.clear()
        proxy._COLD_STATS.clear()


def test_cold_probe_off():
    """PROXY_COLD_PROBE=0 还原 3.12：不登记、不注入。"""
    saved = proxy.COLD_PROBE
    proxy._COLD.clear()
    proxy.COLD_PROBE = False
    try:
        check("关时不登记", proxy.cold_register("c", ["x"]) == 0)
        blk = "a[longctx:checkpoint end]"
        check("关时不注入", proxy.cold_reanchor_block(blk, "c") == blk)
    finally:
        proxy.COLD_PROBE = saved


def test_cold_qmark_question():
    """3.14 ③：换说法问法按形态分类、与锚点零字面重叠；入冷存即配对存问法。"""
    saved = proxy.COLD_PROBE
    proxy.COLD_PROBE = True
    proxy._COLD.clear()
    try:
        pairs = [("192.168.1.7", proxy.cold_question("192.168.1.7")),
                 ("D:\\Strata\\f0.bin", proxy.cold_question("D:\\Strata\\f0.bin")),
                 ("v2.5.31", proxy.cold_question("v2.5.31")),
                 ("REV-4471", proxy.cold_question("REV-4471")),
                 ("ops@example.com", proxy.cold_question("ops@example.com"))]
        def overlap4(a: str, q: str) -> bool:
            la, lq = a.lower(), q.lower()
            return any(la[i:i + 4] in lq for i in range(len(la) - 3))
        check("问法与锚点无 ≥4 字面重叠（NoLiMa 口径）",
              not any(overlap4(a, q) for a, q in pairs),
              str([(a, q) for a, q in pairs if overlap4(a, q)])[:120])
        check("IP/邮箱各有专属问法",
              "IP" in pairs[0][1] and "邮箱" in pairs[4][1])
        n = proxy.cold_register("cq1", ["192.168.1.7", "ops@example.com"])
        ent = proxy._COLD.get("cq1", {})
        check("入冷存即存问法对（原文+改写+qs/qh 初值）",
              n == 2 and all(e.get("q") and e.get("qs") == 0 and e.get("qh") == 0
                             and e.get("age") == 0 for e in ent.values()),
              str(list(ent.values())[:1])[:150])
    finally:
        proxy.COLD_PROBE = saved
        proxy._COLD.clear()


def test_cold_qmark_inject_score():
    """3.14 ③：复位轮注入（够龄/限额/只问一次/整节不进分母）；命中落空各计一次。"""
    saved = (proxy.COLD_PROBE, proxy.COLD_QMARK, proxy.COLD_Q_PICK,
             proxy.COLD_Q_MINAGE, proxy.COLD_Q_GATE)
    proxy._COLD.clear()
    proxy._COLD_STATS.clear()
    proxy.COLD_PROBE, proxy.COLD_QMARK = True, True
    proxy.COLD_Q_PICK, proxy.COLD_Q_MINAGE = 2, 5
    proxy.COLD_Q_GATE = 0  # 本用例测注入/判分本身，开口线另有专测
    try:
        proxy._COLD["cq2"] = {
            "192.168.1.9": {"age": 6, "re": 0, "qs": 0, "qh": 0,
                            "q": proxy.cold_question("192.168.1.9")},
            "ops@x.com": {"age": 7, "re": 0, "qs": 0, "qh": 0,
                          "q": proxy.cold_question("ops@x.com")},
            "D:\\a\\b.bin": {"age": 2, "re": 0, "qs": 0, "qh": 0, "q": "x"},
        }
        blk = "## 目标\n- 修代理\n[longctx:checkpoint end]"
        out = proxy.cold_question_block(blk, "cq2")
        check("换说法节进块且在 end 前",
              proxy.COLD_QMARK_MARK in out
              and 0 < out.index(proxy.COLD_QMARK_MARK)
              < out.index("[longctx:checkpoint end]"), out[:150])
        check("只问够龄的且限额 2 条",
              out.count("\n- ") - blk.count("\n- ") == 2, out[:220])
        check("问过即盖 qs", proxy._COLD["cq2"]["192.168.1.9"]["qs"] == 1
              and proxy._COLD["cq2"]["ops@x.com"]["qs"] == 1)
        out2 = proxy.cold_question_block(out, "cq2")
        check("同批锚点不重复问", out2.count(proxy.COLD_QMARK_MARK) == 1, out2[:150])
        check("换说法节不进锚点分母",
              not any("邮箱" in p or "IP" in p for p in proxy.parse_anchors(out2)),
              str(proxy.parse_anchors(out2))[:150])
        check("换说法节不进分型表",
              not any("邮箱" in t or "IP 地址" in t for t, _ in proxy.parse_anchors_typed(out2)))
        # 注入后的第一次回答：192 被回声=命中；ops 未被回声=落空（同轮各计一次，
        # 且落空只记账不删条目；b.bin 没被问过、不参与判分）
        s = proxy.cold_scan("cq2", "顺带确认：192.168.1.9 已配置")
        check("命中落空同轮各计一次且不删",
              s["qhit"] == 1 and s["qmiss"] == 1
              and "ops@x.com" in proxy._COLD["cq2"]
              and proxy._COLD["cq2"]["ops@x.com"]["qh"] == 2
              and proxy._COLD["cq2"]["D:\\a\\b.bin"]["qh"] == 0, str(s))
        s = proxy.cold_scan("cq2", "ops@x.com 提到了")
        check("判分只判一次：落空后回声不翻案",
              s["qhit"] == 1 and s["qmiss"] == 1, str(s))
    finally:
        (proxy.COLD_PROBE, proxy.COLD_QMARK, proxy.COLD_Q_PICK,
         proxy.COLD_Q_MINAGE, proxy.COLD_Q_GATE) = saved
        proxy._COLD.clear()
        proxy._COLD_STATS.clear()


def test_cold_qmark_off_and_wiring():
    """PROXY_COLD_QMARK=0 还原 3.13；复位接线与 ④ 备注在源码里（接线断言口径同 R23）。"""
    saved = (proxy.COLD_PROBE, proxy.COLD_QMARK)
    proxy._COLD.clear()
    proxy.COLD_PROBE, proxy.COLD_QMARK = True, False
    try:
        proxy._COLD["cq3"] = {"192.168.1.9": {"age": 9, "re": 0, "q": "怎么问？",
                                              "qs": 0, "qh": 0}}
        blk = "## 目标\n- x\n[longctx:checkpoint end]"
        check("关时不注入", proxy.cold_question_block(blk, "cq3") == blk)
        root = Path(__file__).resolve().parent
        src = (root / "proxy" / "proxy.py").read_text(encoding="utf-8")
        check("每轮接线：块构建时按体量过线注入（3.15）",
              "block = cold_question_block(block, conv0," in src, "missing")
        check("复位钩子已撤（3.15 由 50% 线接管）",
              "cold_question_block(block, conv)" not in src, "still there")
        msrc = (root / "app-src" / "main.py").read_text(encoding="utf-8")
        check("④ 备注接线：降档依据+不下记忆力结论",
              "压在灰线下面" in msrc
              and '别拿它下"记忆力变好"的结论' in msrc, "missing")
    finally:
        proxy.COLD_PROBE, proxy.COLD_QMARK = saved
        proxy._COLD.clear()


def test_cold_qmark_gate():
    """3.15：换说法开口线=工作窗口×COLD_Q_GATE（默认 0.5）——400k 下 200k，
    线随建议上下文等比平移；线下不注入也不盖 qs；0=无条件。"""
    saved = (proxy.COLD_PROBE, proxy.COLD_QMARK, proxy.COLD_Q_GATE,
             proxy.COLD_Q_MINAGE, proxy.CONTEXT, proxy.WORK_WINDOW)
    proxy._COLD.clear()
    proxy.COLD_PROBE, proxy.COLD_QMARK = True, True
    proxy.COLD_Q_GATE, proxy.COLD_Q_MINAGE = 0.5, 5
    proxy.CONTEXT, proxy.WORK_WINDOW = 1_000_000, 400_000
    try:
        proxy._COLD["cq4"] = {"192.168.1.9": {"age": 6, "re": 0, "qs": 0, "qh": 0,
                                              "q": "之前连过的那个 IP 地址，具体是多少？"}}
        blk = "## 目标\n- x\n[longctx:checkpoint end]"
        check("400k 窗口线下（199999<200k）不注入不盖 qs",
              proxy.cold_question_block(blk, "cq4", 199_999) == blk
              and proxy._COLD["cq4"]["192.168.1.9"]["qs"] == 0)
        out = proxy.cold_question_block(blk, "cq4", 200_001)
        check("过 400k×50%=200k 开问",
              proxy.COLD_QMARK_MARK in out
              and proxy._COLD["cq4"]["192.168.1.9"]["qs"] == 1, out[:120])
        proxy.WORK_WINDOW = 265_000
        proxy._COLD["cq4"]["192.168.1.9"]["qs"] = 0
        check("线随窗口等比走（265k×50%=132.5k，140k 即问）",
              proxy.COLD_QMARK_MARK in proxy.cold_question_block(blk, "cq4", 140_000))
        proxy.COLD_Q_GATE = 0.0
        proxy._COLD["cq4"]["192.168.1.9"]["qs"] = 0
        check("gate=0 无条件（测试/无门槛模式）",
              proxy.COLD_QMARK_MARK in proxy.cold_question_block(blk, "cq4", 1))
    finally:
        (proxy.COLD_PROBE, proxy.COLD_QMARK, proxy.COLD_Q_GATE,
         proxy.COLD_Q_MINAGE, proxy.CONTEXT, proxy.WORK_WINDOW) = saved
        proxy._COLD.clear()


def test_cold_deep_record():
    """3.21：record 档不丢消息时换说法也要有题源——深位旧消息登记锚点。
    行为判据：旧消息事实进冷存、近因尾巴不进、同条消息只扫一次、块里已
    提醒的跳过、TASK_MODE!=record 不登记；关 COLD_DEEP 必须为空（有牙）。"""
    saved = (proxy.COLD_PROBE, proxy.COLD_DEEP, proxy.COLD_DEEP_TAIL,
             proxy.COLD_DEEP_MAX_SCAN, proxy.TASK_MODE, proxy.COLD_MAX)
    old_cold = {k: dict(v) for k, v in proxy._COLD.items()}
    old_seen = {k: set(v) for k, v in proxy._COLD_SEEN.items()}
    proxy.COLD_PROBE, proxy.COLD_DEEP = True, True
    proxy.COLD_DEEP_TAIL, proxy.COLD_DEEP_MAX_SCAN = 6, 40
    proxy.COLD_MAX, proxy.TASK_MODE = 20, "record"
    try:
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(4):
            msgs.append({"role": "user",
                         "content": f"旧记录{i}：服务器 10.0.0.{i + 1}，"
                                    f"文件 D:\\deep\\f{i}.bin，版本 v9.9.{i}"})
            msgs.append({"role": "assistant", "content": "收到"})
        for i in range(6):
            msgs.append({"role": "user", "content": f"最近{i}：服务器 172.16.0.{i + 1}"})
        for conv in ("deep1", "deep2", "deep3"):
            proxy._COLD.pop(conv, None)
            proxy._COLD_SEEN.pop(conv, None)
        n = proxy.register_deep_anchors(msgs, "deep1", "")
        cold = proxy._COLD.get("deep1") or {}
        check("深位锚点：旧消息事实被登记",
              n > 0 and any("10.0.0." in t for t in cold),
              f"n={n} {list(cold)[:5]}")
        check("深位锚点：近因尾巴不登记",
              not any("172.16.0." in t for t in cold), str(list(cold)[:6]))
        proxy._COLD.pop("deep1", None)
        n2 = proxy.register_deep_anchors(msgs, "deep1", "")
        check("深位锚点：同一条消息只扫一次", n2 == 0, f"n2={n2}")
        proxy.register_deep_anchors(msgs, "deep2", "## 规则\n- 10.0.0.1\n")
        check("深位锚点：块里已提醒的跳过",
              all(t != "10.0.0.1" for t in (proxy._COLD.get("deep2") or {})),
              str(list(proxy._COLD.get("deep2") or {})[:6]))
        proxy.TASK_MODE = "auto"
        n3 = proxy.register_deep_anchors(msgs, "deep3", "")
        check("深位锚点：只在 record 档生效", n3 == 0 and not proxy._COLD.get("deep3"),
              f"n3={n3}")
        proxy.TASK_MODE = "record"
        proxy.COLD_DEEP = False
        proxy._COLD.pop("deep3", None)
        proxy._COLD_SEEN.pop("deep3", None)
        n4 = proxy.register_deep_anchors(msgs, "deep3", "")
        check("深位锚点：关掉开关不登记（有牙）",
              n4 == 0 and not proxy._COLD.get("deep3"), f"n4={n4}")
    finally:
        (proxy.COLD_PROBE, proxy.COLD_DEEP, proxy.COLD_DEEP_TAIL,
         proxy.COLD_DEEP_MAX_SCAN, proxy.TASK_MODE, proxy.COLD_MAX) = saved
        proxy._COLD.clear()
        proxy._COLD.update(old_cold)
        proxy._COLD_SEEN.clear()
        proxy._COLD_SEEN.update(old_seen)
    psrc = (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8")
    check("深位锚点接线：排在换说法之前",
          psrc.index("register_deep_anchors(body.get(\"messages\") or [], conv0, block)")
          < psrc.index("block = cold_question_block(block, conv0,"), "order")
    check("换说法仍是一次性（同锚点不重复问）",
          'table[t]["qs"] = 1' in psrc, "missing")


def test_auto_work_window():
    names = ("CONTEXT", "WORK_WINDOW", "TARGET_MSG", "LAZY_COMPRESS",
             "LAZY_TRIGGER", "LAZY_TARGET", "RESERVE", "CHECKPOINT",
             "CHECKPOINT_RESET", "RECOMPACT_TRIGGER", "RECOMPACT_CAP")
    saved = {n: getattr(proxy, n) for n in names}
    try:
        proxy.CONTEXT, proxy.WORK_WINDOW, proxy.TARGET_MSG = 1_000_000, 200_000, 0
        proxy.LAZY_COMPRESS, proxy.LAZY_TRIGGER, proxy.LAZY_TARGET = True, 0.85, 0.45
        proxy.RESERVE, proxy.CHECKPOINT = 12000, False
        check("1M 模型按 200K 工作窗口", proxy._work_context() == 200_000,
              str(proxy._work_context()))
        check("85% 触发线", proxy._lazy_gate() == 170_000, str(proxy._lazy_gate()))
        check("45% 压缩目标", proxy._budget(0) == 90_000, str(proxy._budget(0)))
        proxy.CONTEXT = 32_000
        check("原生窗口小于 200K 不放大", proxy._work_context() == 32_000,
              str(proxy._work_context()))
        # 触发线不能超过「原生窗口 - 回答余量」：32K - 12K = 20K
        check("小窗口触发线跟着走", proxy._lazy_gate() == 20_000,
              str(proxy._lazy_gate()))
        # 检查点模式下不再做“重压冻结前缀”，否则又回到越压越差
        proxy.CONTEXT = 1_000_000
        proxy.CHECKPOINT, proxy.CHECKPOINT_RESET = True, 0.90
        proxy.RECOMPACT_TRIGGER, proxy.RECOMPACT_CAP = 0.50, 0
        check("检查点模式关闭重压", proxy._recompact_limit() == 0,
              str(proxy._recompact_limit()))
    finally:
        for n, v in saved.items():
            setattr(proxy, n, v)


def test_transform_checkpoint_inject():
    import tempfile
    body = {"max_tokens": 4096, "messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "必须保留端口 8082。" + "这是一段文档。" * 800},
        {"role": "assistant", "content": "已读"},
        {"role": "user", "content": "现在总结"},
    ]}
    saved = (proxy.CHECKPOINT, proxy.CHECKPOINT_PIN, proxy.CHECKPOINT_RESET,
             proxy.SESS_DIR, proxy.THRESHOLD, proxy.TARGET_MSG, proxy.CONTEXT,
             proxy.RESERVE, proxy.MIN_MSG, proxy.LOW_MSG, proxy.REPEAT_DEDUPE,
             proxy.L2_FOLD, proxy.get_summary, proxy.PREFIX_STRICT)
    old_mem = dict(proxy._prefix_mem)
    proxy.CHECKPOINT, proxy.CHECKPOINT_PIN, proxy.CHECKPOINT_RESET = True, True, 0.0
    proxy.SESS_DIR = Path(tempfile.mkdtemp(prefix="longctx-cp-"))
    proxy.THRESHOLD, proxy.TARGET_MSG, proxy.CONTEXT = 100, 1, 100000
    proxy.RESERVE, proxy.MIN_MSG, proxy.LOW_MSG = 1000, 50, 20
    proxy.REPEAT_DEDUPE, proxy.L2_FOLD = False, False
    proxy.PREFIX_STRICT = False
    proxy.get_summary = lambda text, task, chunk_conc=None: "摘要：端口 8082。"
    proxy._prefix_mem.clear()
    try:
        proxy.transform(body, "t")
        msgs = body["messages"]
        marks = [i for i, m in enumerate(msgs) if proxy.checkpoint.is_mark(m)]
        check("转换后注入一份检查点", len(marks) == 1, str(marks))
        check("检查点恒挂末尾（位置不随历史形状漂移）",
              marks and marks[0] == len(msgs) - 1, f"{marks} / {len(msgs)}")
        check("末条角色仍是 user", msgs[-1].get("role") == "user", str(msgs[-1].get("role")))
        check("检查点保留硬事实",
              "8082" in msgs[marks[0]]["content"], msgs[marks[0]]["content"][:200])
        # 压缩改写了第一条 user，conversation_key 会变；按目录找落盘文件
        check("检查点状态已落盘",
              bool(list(proxy.SESS_DIR.rglob("checkpoint.json"))))
    finally:
        (proxy.CHECKPOINT, proxy.CHECKPOINT_PIN, proxy.CHECKPOINT_RESET,
         proxy.SESS_DIR, proxy.THRESHOLD, proxy.TARGET_MSG, proxy.CONTEXT,
         proxy.RESERVE, proxy.MIN_MSG, proxy.LOW_MSG, proxy.REPEAT_DEDUPE,
         proxy.L2_FOLD, proxy.get_summary, proxy.PREFIX_STRICT) = saved
        proxy._prefix_mem.clear()
        proxy._prefix_mem.update(old_mem)


def test_slot_overwrite_rule():
    """3.11 槽位覆写：同键不同值的键值型约束，新值整条顶替旧值并进台账。"""
    import checkpoint
    st = checkpoint.new_state("slot-311")
    checkpoint._add_rule(st, "请求超时=320ms", turn=1)
    checkpoint._add_rule(st, "请求超时=480ms", turn=2)
    texts = [r["t"] for r in st["rules"]]
    check("覆写后只剩一条", len(texts) == 1, str(texts))
    check("新值存活", bool(texts) and "480" in texts[0], str(texts))
    check("旧值离开块", all("320" not in t for t in texts), str(texts))
    ov = st.get("overwrites") or []
    check("覆写台账一条", len(ov) == 1, str(ov))
    check("台账含旧值与新值", bool(ov) and ov[0]["old"] == "请求超时=320ms"
          and ov[0]["new"] == "请求超时=480ms", str(ov))

    # 不同槽位不合并
    st2 = checkpoint.new_state("slot-311b")
    checkpoint._add_rule(st2, "服务端口=8082", turn=1)
    checkpoint._add_rule(st2, "管理端口=9000", turn=2)
    check("异槽并存", len(st2["rules"]) == 2, str([r["t"] for r in st2["rules"]]))

    # 同槽同值的措辞变体：合并且不记覆写
    checkpoint._add_rule(st2, "服务端口 = 8082", turn=3)
    check("同槽同值合并不重复", len(st2["rules"]) == 2,
          str([r["t"] for r in st2["rules"]]))
    check("同值不记台账", len(st2.get("overwrites") or []) == 0,
          str(st2.get("overwrites")))

    # 整行多个数字（多槽行）保守不合并
    st3 = checkpoint.new_state("slot-311c")
    checkpoint._add_rule(st3, "超时=60s 端口=8080", turn=1)
    checkpoint._add_rule(st3, "超时=90s 端口=8080", turn=2)
    check("多数字行不覆写", len(st3["rules"]) == 2,
          str([r["t"] for r in st3["rules"]]))

    # 无数字约束永不参与槽位判定；覆写后的新值落在列表末尾（最新）
    st4 = checkpoint.new_state("slot-311d")
    checkpoint._add_rule(st4, "端口=8082", turn=1)
    checkpoint._add_rule(st4, "记住保留页脚", turn=2)
    checkpoint._add_rule(st4, "端口=9000", turn=3)
    check("覆写后新值在末尾", st4["rules"][-1]["t"] == "端口=9000",
          str([r["t"] for r in st4["rules"]]))
    check("无数字约束不受影响", any("页脚" in r["t"] for r in st4["rules"]), "")

    # 中文单位词形（无等号）同样成槽
    st5 = checkpoint.new_state("slot-311e")
    checkpoint._add_rule(st5, "最大并发 8", turn=1)
    checkpoint._add_rule(st5, "最大并发 12", turn=2)
    check("单位词形也覆写", len(st5["rules"]) == 1
          and "12" in st5["rules"][0]["t"], str([r["t"] for r in st5["rules"]]))


def test_slot_overwrite_decision():
    """3.11 决策同槽覆写：改口的结论只留最新一条。"""
    import checkpoint
    st = checkpoint.new_state("slotdec")
    checkpoint._add_decision(st, "决定端口=8082", turn=1)
    checkpoint._add_decision(st, "决定端口=9000", turn=2)
    check("决策覆写剩最新", len(st["decisions"]) == 1
          and "9000" in st["decisions"][0]["t"], str(st["decisions"]))
    check("决策覆写进台账", len(st.get("overwrites") or []) == 1,
          str(st.get("overwrites")))


def test_slot_overwrite_off():
    """PROXY_CK_SLOT=0 还原 3.10 行为：同槽旧值照常并存。"""
    import checkpoint
    old = checkpoint.SLOT_OVERWRITE
    checkpoint.SLOT_OVERWRITE = False
    try:
        st = checkpoint.new_state("slotoff")
        checkpoint._add_rule(st, "请求超时=320ms", turn=1)
        checkpoint._add_rule(st, "请求超时=480ms", turn=2)
        check("关闭后两条并存", len(st["rules"]) == 2,
              str([r["t"] for r in st["rules"]]))
        check("关闭后不记台账", not st.get("overwrites"), str(st.get("overwrites")))
    finally:
        checkpoint.SLOT_OVERWRITE = old


def test_goal_watch_topic_swap():
    """3.12 goal 换血：连续低重叠的真实用户消息才换，单次岔题不换。"""
    import checkpoint
    U = lambda st, c: checkpoint.update(st, [{"role": "user", "content": c}],
                                        proxy.count_tokens, proxy.extract_facts)
    st = checkpoint.new_state("gw312")
    st = U(st, "帮我调一下 longctx 代理的绿线曲线显示")
    check("goal 初始设置", "曲线" in st["goal"], st["goal"])
    st = U(st, "明天上午要带猫去医院打疫苗")
    st = U(st, "晚饭想去市场买点西红柿和鸡蛋")
    check("两次岔题不换 goal", "曲线" in st["goal"], st["goal"])
    check("岔题计龄到 2", st.get("goal_stale") == 2, str(st.get("goal_stale")))
    st = U(st, "阳台那几盆花该浇水了顺便修枝")
    check("第三次岔题换血", "浇水" in st["goal"], st["goal"])
    check("旧 goal 进换血台账", len(st.get("goal_swaps") or []) == 1
          and "曲线" in st["goal_swaps"][0]["old"], str(st.get("goal_swaps")))
    check("换血后计龄归零", st.get("goal_stale") == 0, str(st.get("goal_stale")))
    # 同话题延续：重叠高 → 不换也不计龄
    st = U(st, "浇水的时候顺便看看阳台花盆有没有漏水")
    check("同题延续不换 goal", "浇水" in st["goal"], st["goal"])
    check("同题延续清零计龄", st.get("goal_stale") == 0, str(st.get("goal_stale")))
    # 短应答不计龄
    st = U(st, "好的")
    check("短应答不计龄", st.get("goal_stale") == 0, str(st.get("goal_stale")))


def test_goal_watch_rescan_idempotent():
    """3.12 原地改写触发全量重扫时，已计龄的旧消息不得重复计龄。"""
    import checkpoint
    st = checkpoint.new_state("gw312b")
    msgs = [{"role": "user", "content": "帮我调一下 longctx 代理的绿线曲线显示"},
            {"role": "user", "content": "明天上午要带猫去医院打疫苗"},
            {"role": "user", "content": "晚饭想去市场买点西红柿和鸡蛋"}]
    st = checkpoint.update(st, msgs, proxy.count_tokens, proxy.extract_facts)
    check("首扫计龄 2 不换血", st.get("goal_stale") == 2
          and "曲线" in st["goal"], str(st.get("goal_stale")))
    # 改写最后一条 → cursor_key 对不上 → 全量重扫：
    # goal 行自重叠清零，旧条 seen 跳过，只有改写后的新内容计 1
    msgs2 = msgs[:2] + [{"role": "user",
                         "content": "阳台那几盆花该浇水了顺便修枝"}]
    st = checkpoint.update(st, msgs2, proxy.count_tokens, proxy.extract_facts)
    check("重扫后 seen 去重只剩新条计龄", st.get("goal_stale") == 1,
          str(st.get("goal_stale")))
    check("重扫不触发换血", "曲线" in st["goal"], st["goal"])


def test_goal_watch_off():
    """PROXY_GOAL_WATCH=0 还原 3.11 行为：goal 常驻不轮换。"""
    import checkpoint
    old = checkpoint.GOAL_WATCH
    checkpoint.GOAL_WATCH = False
    try:
        U = lambda st, c: checkpoint.update(st, [{"role": "user", "content": c}],
                                            proxy.count_tokens, proxy.extract_facts)
        st = checkpoint.new_state("gw312off")
        st = U(st, "帮我调一下 longctx 代理的绿线曲线显示")
        for c in ("明天上午要带猫去医院打疫苗", "晚饭想去市场买点西红柿和鸡蛋",
                  "阳台那几盆花该浇水了顺便修枝"):
            st = U(st, c)
        check("关闭后 goal 不换血", "曲线" in st["goal"], st["goal"])
        check("关闭后无台账", not st.get("goal_swaps"), str(st.get("goal_swaps")))
    finally:
        checkpoint.GOAL_WATCH = old


def test_kpi_time_axis():
    """3.16：KPI 线图 X 轴时间刻度 + 图加高。纯函数判据 + 接线断言。
    有牙核验：LONGCTX_GUI_MAIN 指到 _bak-316-pre/main.py 应全红。"""
    import importlib.util
    import os as _os
    p = Path(_os.environ.get("LONGCTX_GUI_MAIN",
                             str(HERE / "app-src" / "main.py")))
    mod = None
    try:
        sys.path.insert(0, str(p.parent))
        spec = importlib.util.spec_from_file_location("gui_main_under_test", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        mod = None
    fn = getattr(mod, "_kpi_time_ticks", None) if mod else None
    check("3.16 刻度函数存在", fn is not None, str(p))
    if fn:
        rows = [{"t": f"10:{i:02d}:00"} for i in range(60)]
        ticks = fn(rows, 600)
        check("60 行带 t → 4 刻度", len(ticks) == 4, str(ticks))
        check("首刻度 x=2 锚 w", ticks[0] == (2, "10:00", "w"), str(ticks[0]))
        check("尾刻度 x=w-2 锚 e", ticks[-1] == (598, "10:59", "e"),
              str(ticks[-1]))
        check("全缺 t → 空表（旧数据不画不崩）", fn([{}, {}], 600) == [])
        check("窄宽 <260 → 只首尾", len(fn(rows, 240)) == 2)
        check("w<120 → 空表", fn(rows, 100) == [])
        mid = list(rows)
        mid[19] = {}
        check("中间行缺 t → 跳过该刻度", len(fn(mid, 600)) == 3, str(fn(mid, 600)))
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("3.16 图加高 190", "height=190" in msrc, "missing")
    check("轴带 18px（曲线区压到 hp）", "hp = h - 18" in msrc, "missing")
    check("刻度接线：_update_kpi 调用刻度函数",
          "_kpi_time_ticks(rows, w)" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    check("代理已动（PROXY_VERSION=3.34，版本双查）",
          'PROXY_VERSION = "3.36"' in
          (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8"),
          "proxy 版本漂移")


def _gui_main_mod():
    """按 LONGCTX_GUI_MAIN 覆盖加载 GUI 模块（有牙核验指备份用）。"""
    import importlib.util
    import os as _os
    p = Path(_os.environ.get("LONGCTX_GUI_MAIN",
                             str(HERE / "app-src" / "main.py")))
    try:
        sys.path.insert(0, str(p.parent))
        spec = importlib.util.spec_from_file_location("gui_main_under_test", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, p
    except Exception:
        return None, p


def test_kpi_line_styles():
    """3.17：线型语义四件套——①命中率线 ②会话过滤 ③低置信点线 ④持续性判据。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-317-pre/main.py 应全红。"""
    mod, p = _gui_main_mod()
    hs = getattr(mod, "_kpi_hit_series", None) if mod else None
    tb = getattr(mod, "_kpi_tail_below", None) if mod else None
    fr = getattr(mod, "_kpi_filter_rows", None) if mod else None
    check("3.17 三个纯函数齐备", hs and tb and fr, str(p))
    if hs:
        rows = [{"cold": {"qhit": 0, "qmiss": 0}},
                {"cold": {"qhit": 1, "qmiss": 1}},
                {"cold": {"qhit": 3, "qmiss": 1}}]
        check("命中率序列 None→0.5→0.75（没问过不画假 0）",
              hs(rows) == [None, 0.5, 0.75], str(hs(rows)))
        check("无 cold 字段安全", hs([{}, {"cold": None}]) == [None, None])
    if tb:
        check("末端连续低于计数（2 轮）", tb([0.2, 0.1, 0.05, 0.04], 0.10) == 2)
        check("不低于即断（只算 1）", tb([0.05, 0.2, 0.04], 0.10) == 1)
        check("遇 None 断", tb([0.2, 0.04, None, 0.03], 0.10) == 1)
        check("全不低于 → 0", tb([0.3, 0.4], 0.10) == 0)
    if fr:
        rws = [{"conv": "a"}, {"conv": "b"}, {"conv": "a"}]
        check("过滤只留本会话", fr(rws, "a") == [{"conv": "a"}, {"conv": "a"}])
        check("空 conv=全量（老行为）", fr(rws, None) == rws and fr(rws, "") == rws)
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("长线段=重心 dash=(8, 3)", "dash=None" in msrc and "(8, 3)" in msrc, "missing")
    check("连续点=低置信 dash=(1, 3)（3.23：只看池子置信度，与基线口径无关）",
          '(1, 3) if pool_a < BASELINE_MIN_POOL else (8, 3)' in msrc,
          "missing")
    check("已问<3 也降点线", "(1, 3) if asked < 3" in msrc, "missing")
    check("命中率线接线：_kpi_hit_series 被调用", "_kpi_hit_series(rows)" in msrc, "missing")
    check("会话过滤接线：尾400→过滤→尾60",
          "_kpi_filter_rows(all_rows" in msrc, "missing")
    check("会话行点击绑定", 'self.ctx_tree.bind("<<TreeviewSelect>>", self._on_ctx_select)' in msrc, "missing")
    check("线图时间范围按钮", 'text="线图时间范围"' in msrc
          and 'text="全部会话"' not in msrc, "missing")
    check("④ 持续性建议话术在源码", "建议降一档" in msrc and "未满 3，不出建议" in msrc, "missing")
    check("线型图例备注已改写", "图怎么看" in msrc
          and "蓝线每轮原始值与橙线" not in msrc, "missing")
    check("3.17.1 图例动态折行（窄列不裁字）",
          'wraplength=max(200, e.width - 6)' in msrc
          and 'self.lbl_kpi_note.bind(\n            "<Configure>"' in msrc,
          "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    check("代理已动（PROXY_VERSION=3.34，版本双查）",
          'PROXY_VERSION = "3.36"' in
          (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8"),
          "proxy 版本漂移")


def test_kpi_entry_layout():
    """3.17.2：修 3.17 回归——文字行撑爆表单 column 0 把输入框列压成 1px；
    dash 逐段重置看不见；文字行裁字；标题改名。行为级断言：输入框必须真可见。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-3172-pre/main.py（3.17.1）必须红。"""
    mod, p = _gui_main_mod()
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("_kpi_runs 连续段函数存在",
          mod is not None and hasattr(mod, "_kpi_runs"), str(p))
    if mod and hasattr(mod, "_kpi_runs"):
        r = mod._kpi_runs([1, None, 2, 3, None])
        check("None 断口切段", r == [[(0, 1)], [(2, 2), (3, 3)]], str(r))
        check("全 None → 空段表", mod._kpi_runs([None, None]) == [])
    check("整段一次 create_line（dash 不重置）",
          "cv.create_line(*flat, fill=color, dash=dash)" in msrc, "missing")
    check("文字行进子框（不撑表单列宽）",
          "self.lbl_kpi = ttk.Label(kpirow" in msrc, "missing")
    check("文字行动态折行", "wraplength=max(120, e.width - 4)" in msrc, "missing")
    check("标题已改：任务模型列表", '"任务模型列表"' in msrc
          and "云端任务模型列表" not in msrc, "still old")
    check("i18n 键同步", (p.parent / "i18n.py").read_text(encoding="utf-8")
          .count("云端任务模型") == 0, "i18n 漏改")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    # 行为级：真建界面量输入框——3.17/3.17.1 在这里 mapped=0/宽 1px，必红
    if mod and hasattr(mod, "build_root"):
        try:
            root = mod.build_root()
            root.geometry("1600x900+30+30")
            for _ in range(10):
                root.update()
            e = root.app.e_t_name
            mapped = e.winfo_ismapped()
            width = e.winfo_width()
            wl = int(root.app.lbl_kpi_note.cget("wraplength") or 0)
            root.destroy()
            check("输入框真可见（mapped 且宽>40px）",
                  mapped == 1 and width > 40, f"mapped={mapped} w={width}")
            check("图例折行宽≤列宽（不再 843 假宽）", 0 < wl <= 420, f"wl={wl}")
        except Exception as exc:
            check("输入框真可见（Tk 构建）", False, repr(exc))


def test_kpi_scale_p95():
    """3.17.3：纵轴量程 90 分位——尖峰贴顶不压扁判档带（用户报"看图"）。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-3173-pre/main.py（3.17.2）必须红。"""
    mod, p = _gui_main_mod()
    sc = getattr(mod, "_kpi_scale", None) if mod else None
    check("_kpi_scale 存在", sc is not None, str(p))
    if sc:
        vals = [0.08] * 55 + [0.09, 0.10] + [1.0, 1.0, 1.0]
        hi = sc(vals)
        check("尖峰不绑架量程（3/60 尖峰下 90 分位≈判档带）", hi < 0.5, f"hi={hi}")
        check("90 分位参数（95 实测被绑架故降）", "pct: float = 0.90" in
              p.read_text(encoding="utf-8"), "missing")
        check("全零有下限", sc([0.0, 0.0]) == 0.25)
        check("空表有下限", sc([None, None]) == 0.25)
        check("None 不参与分位", sc([None, 0.3] * 40) >= 0.25)
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("接线：hi 用 _kpi_scale(er+ma)（3.20：None 不参与分位，且至少到基线×2）",
          "_kpi_scale([v for v in er + ma if v is not None])" in msrc
          and "(base or 0.0) * 2" in msrc, "missing")
    check("旧 max 量程已撤", "hi = max(0.25, max(er + ma))" not in msrc, "still there")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_popup_edge():
    """3.17.4：降档建议弹窗（用户定稿：改小建议上下文 或 历史转 .md/知识库+新窗口）。
    判据核心=边沿触发，每个降档周期只弹一次——KPI 每 2s 刷新，非边沿会刷屏。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-3174-pre/main.py（3.17.3）必须红。"""
    mod, p = _gui_main_mod()
    fn = getattr(mod, "_kpi_down_edge", None) if mod else None
    check("_kpi_down_edge 存在", fn is not None, str(p))
    if fn:
        check("首次成立(3,未latch)→弹并锁", fn(3, False) == (True, True), str(fn(3, False)))
        check("同周期持续(5,已latch)→不弹不解锁", fn(5, True) == (False, True))
        check("掉到1~2轮(同一周期)→不弹、保持锁", fn(1, True) == (False, True))
        check("清零(below=0)→解锁，新周期可再弹", fn(0, True) == (False, False))
        check("未满3不弹不锁", fn(2, False) == (False, False))
        check("min_rounds 参数化", fn(3, False, min_rounds=4) == (False, False))
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("接线：弹窗用边沿结果", "if _fire:" in msrc
          and "_kpi_down_edge(below" in msrc, "missing")
    check("弹窗文案含两条路（改小/转md或知识库）",
          "改小一档" in msrc and "知识库" in msrc and ".md" in msrc, "missing")
    check("弹窗标题=降档建议", 'showinfo(\n                    "降档建议"' in msrc
          or '"降档建议"' in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_export_md():
    """3.17.5：降档弹窗「一键导出本会话为 .md」——弹窗指路变成弹窗代办。
    判据核心=session_md 纯函数：台账字段全量成 markdown、缺数据不抛。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-3175-pre/main.py（3.17.4）必须红。"""
    mod, p = _gui_main_mod()
    fn = getattr(mod, "session_md", None) if mod else None
    check("session_md 存在", fn is not None, str(p))
    check("load_session_ckpt 存在",
          getattr(mod, "load_session_ckpt", None) is not None)
    check("读台账目录与 proxy SESS_DIR 同规矩",
          getattr(mod, "_sess_dir", None) is not None)
    ck = {
        "goal": "把导出按钮做完",
        "rules": [{"k": "a", "t": "不许动生产配置"}],
        "todos": [{"k": "b", "t": "出 exe", "done": True},
                  {"k": "c", "t": "写测试", "done": False}],
        "progress": ["弹窗已接线"],
        "decisions": [{"k": "d", "t": "选纯函数", "why": "可单测"}],
        "facts": [{"k": "e", "src": "user", "t": "导出目录在 proxy/exports"}],
        "files_mod": ["app-src/main.py"], "files_read": ["proxy/proxy.py"],
        "errors": ["test_hotlayer 漂移"],
        "sources": [{"k": "f", "t": "GB50311", "base": "kb1", "chunks": 2,
                     "lines": ["4.2.1 风速"]}],
        "created": "2026-11-07 12:00:16", "updated": "2026-11-07 12:01:19",
        "cursor": 30,
    }
    live = {"key": "abcd1234ef567890", "body": 100, "tools": 20, "total": 120,
            "raw_total": 900, "usage_prompt": 130, "ts": "12:01:01",
            "anchors": {"echoed": 3, "total": 5, "followed": 1}}
    md = fn("abcd1234ef567890", live, ck, now="2026-11-07 13:00:00") if fn else ""
    check("md 带会话短 ID 标题", "# 会话 abcd1234" in md, md[:60])
    check("md 带目标", "把导出按钮做完" in md and "## 目标" in md)
    check("md 带规则", "不许动生产配置" in md)
    check("待办出勾选框", "- [x] 出 exe" in md and "- [ ] 写测试" in md)
    check("决定带理由", "选纯函数（理由：可单测）" in md)
    check("事实/进展/错误/文件全在", all(s in md for s in (
        "导出目录在 proxy/exports", "弹窗已接线", "test_hotlayer 漂移",
        "- 改：app-src/main.py", "- 读：proxy/proxy.py")))
    check("来源带标题与证据行", "《GB50311》" in md and "4.2.1 风速" in md)
    check("实况数字在", "压缩后合计：120 tok" in md and "引擎实测：130 tok" in md)
    check("锚点带默读", "锚点回声 3/5（默读 1）" in md)
    md2 = fn("x" * 16, live, None)
    check("空台账仍出实况+说明", "120 tok" in md2 and "台账未生成" in md2)
    md3 = fn("y" * 16, None, None)
    check("双缺也不抛且非空", isinstance(md3, str) and md3.strip() != "")
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("弹窗接线：降档建议改用代办弹窗(Toplevel)",
          '"降档建议"' in msrc and "Toplevel(self.root)" in msrc
          and "_kpi_down_popup(below)" in msrc, "missing")
    check("按钮文案原文「一键导出本会话为 .md」",
          "一键导出本会话为 .md" in msrc, "missing")
    check("导出落 proxy_dir/exports 并回报绝对路径",
          '"exports"' in msrc and "已导出" in msrc, "missing")
    check("本会话=过滤优先否则最新行",
          "_current_conv_live" in msrc and "kpi_conv" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_task_mode_monitor():
    """3.18：任务模式「路由监控」——逐字节直通、只记统计、不碰内容。
    判据核心=行为：阈值钉到极低，body 仍一字不变、前后计数相等、超阈只计数不拦。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-318-pre/main.py（3.17.5）必须红
    （monitor 档、直通标记、KPI 暂停、版本钉全缺）。"""
    import json
    mod, p = _gui_main_mod()
    old_mode, old_thr = proxy.TASK_MODE, proxy.THRESHOLD
    try:
        proxy.TASK_MODE = "monitor"
        proxy.THRESHOLD = 50          # 钉到极低：正常档这条必进压缩路径
        body = {"model": "m", "messages": [
            {"role": "user", "content": "甲" * 3000},
            {"role": "assistant", "content": "乙" * 3000},
            {"role": "user", "content": "新问题"}]}
        snap = json.dumps(body, ensure_ascii=False, sort_keys=True)
        before, after = proxy.transform(body, "test-monitor")
        check("monitor：body 逐字节不变",
              json.dumps(body, ensure_ascii=False, sort_keys=True) == snap)
        check("monitor：前后计数相等（没压）",
              before == after and before > 50, f"{before}/{after}")
        check("monitor：超阈计数只加不拦",
              proxy._MONITOR["over"] >= 1 and proxy._MONITOR["reqs"] >= 1,
              str(proxy._MONITOR))
        snap2 = proxy._stats_snapshot()
        check("monitor：stats 暴露 task_mode/monitor",
              snap2.get("task_mode") == "monitor"
              and isinstance(snap2.get("monitor"), dict), str(snap2)[:120])
    finally:
        proxy.TASK_MODE, proxy.THRESHOLD = old_mode, old_thr
    check("monitor 已进提示表", "monitor" in proxy.TASK_MODE_HINTS)
    psrc = (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8")
    check("PROXY_VERSION=3.34（代理本次动了）",
          'PROXY_VERSION = "3.36"' in psrc, "missing")
    check("monitor 短路在 transform 入口（摘要判定之前）",
          'if TASK_MODE == "monitor":' in psrc
          and psrc.index('if TASK_MODE == "monitor":')
          < psrc.index("if _is_summarization(body):"), "order")
    tm = getattr(mod, "TASK_MODES", {}) if mod else {}
    check("GUI 下拉有路由监控档",
          "monitor" in tm and tm["monitor"][0] == "路由监控", str(list(tm)))
    base = {"total": 123, "body": 100, "tools": 20, "raw_total": 456}
    txt = mod.ctx_live_text({"actual": base, "task_mode": "monitor",
                             "monitor": {"reqs": 5, "over": 2}}) if mod else ""
    check("实况条带直通标记+超阈计数",
          "路由监控直通 5 次" in txt and "超阈 2 次" in txt, txt)
    txt0 = mod.ctx_live_text({"actual": base, "task_mode": "monitor",
                              "monitor": {"reqs": 1, "over": 0}}) if mod else ""
    check("没超阈不出超阈话术", "路由监控直通 1 次" in txt0
          and "超阈" not in txt0, txt0)
    txt1 = mod.ctx_live_text({"actual": base, "task_mode": "auto"}) if mod else ""
    check("非 monitor 模式不带直通标记", "直通" not in txt1, txt1)
    txt2 = mod.ctx_live_text({"actual": None, "task_mode": "monitor"}) if mod else ""
    check("没请求时也说清在监控档", "路由监控直通" in txt2, txt2)
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("KPI 面板接线 monitor 暂停",
          'proxy_stats().get("task_mode") == "monitor"' in msrc
          and "已暂停" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_task_mode_record():
    """3.19：任务模式「上下文调制监测」——调制=改写，本档不改写；监测全速。
    判据核心=行为：阈值钉极低时原消息一字不变、摘要一次不调、复位钉死，
    但检查点块照常注入、台账照常落盘；auto 对照组同输入必压（判据有牙）。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-319-pre/main.py（3.18）必须红。"""
    import tempfile
    import json
    mod, p = _gui_main_mod()
    body = {"max_tokens": 4096, "messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "必须保留端口 8082。" + "这是一段文档。" * 800},
        {"role": "assistant", "content": "已读"},
        {"role": "user", "content": "现在总结"},
    ]}
    orig_msgs = json.dumps(body["messages"], ensure_ascii=False)
    saved = (proxy.CHECKPOINT, proxy.CHECKPOINT_PIN, proxy.CHECKPOINT_RESET,
             proxy.SESS_DIR, proxy.THRESHOLD, proxy.TARGET_MSG, proxy.CONTEXT,
             proxy.RESERVE, proxy.MIN_MSG, proxy.LOW_MSG, proxy.REPEAT_DEDUPE,
             proxy.L2_FOLD, proxy.get_summary, proxy.PREFIX_STRICT,
             proxy.TASK_MODE)
    old_mem = dict(proxy._prefix_mem)
    proxy.CHECKPOINT, proxy.CHECKPOINT_PIN = True, True
    proxy.CHECKPOINT_RESET = 0.9        # 窗口 100k，复位线远够不着；行为另查静态
    proxy.SESS_DIR = Path(tempfile.mkdtemp(prefix="longctx-rec-"))
    proxy.THRESHOLD, proxy.TARGET_MSG, proxy.CONTEXT = 100, 1, 100000
    proxy.RESERVE, proxy.MIN_MSG, proxy.LOW_MSG = 1000, 50, 20
    proxy.REPEAT_DEDUPE, proxy.L2_FOLD = False, False
    proxy.PREFIX_STRICT = False
    called = {"summary": 0}

    def _spy(text, task, chunk_conc=None):
        called["summary"] += 1
        return "摘要：端口 8082。"
    proxy.get_summary = _spy
    proxy.TASK_MODE = "record"
    proxy._prefix_mem.clear()
    try:
        before, after = proxy.transform(body, "t")
        msgs = body["messages"]
        check("调制监测：原消息一字不变",
              json.dumps([m for m in msgs if not proxy.checkpoint.is_mark(m)],
                         ensure_ascii=False) == orig_msgs, "content rewritten")
        marks = [i for i, m in enumerate(msgs) if proxy.checkpoint.is_mark(m)]
        check("调制监测：检查点块照常注入", len(marks) == 1, str(marks))
        check("调制监测：摘要一次没调（不压缩）",
              called["summary"] == 0, str(called))
        check("调制监测：前后计数只多台账块",
              after > before and before > 100, f"{before}->{after}")
        check("调制监测：台账照常落盘",
              bool(list(proxy.SESS_DIR.rglob("checkpoint.json"))))
        body2 = {"max_tokens": 4096, "messages": json.loads(orig_msgs)}
        proxy.TASK_MODE = "auto"
        proxy.transform(body2, "t2")
        check("auto 对照：同输入必压（判据有牙）",
              called["summary"] > 0, str(called))
    finally:
        (proxy.CHECKPOINT, proxy.CHECKPOINT_PIN, proxy.CHECKPOINT_RESET,
         proxy.SESS_DIR, proxy.THRESHOLD, proxy.TARGET_MSG, proxy.CONTEXT,
         proxy.RESERVE, proxy.MIN_MSG, proxy.LOW_MSG, proxy.REPEAT_DEDUPE,
         proxy.L2_FOLD, proxy.get_summary, proxy.PREFIX_STRICT,
         proxy.TASK_MODE) = saved
        proxy._prefix_mem.clear()
        proxy._prefix_mem.update(old_mem)
    psrc = (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8")
    check("直通钉在入口（前缀重放之前）",
          'if TASK_MODE == "record":' in psrc
          and psrc.index('if TASK_MODE == "record":')
          < psrc.index("orig_keys = _message_keys(messages)"), "order")
    check("复位钉死（record 跳过 _maybe_checkpoint_reset）",
          'if TASK_MODE != "record":' in psrc
          and psrc.index('if TASK_MODE != "record":')
          < psrc.index("_maybe_checkpoint_reset(body, tag, final"), "order")
    check("record 已进提示表", "record" in proxy.TASK_MODE_HINTS)
    check("PROXY_VERSION=3.34（代理本次动了）",
          'PROXY_VERSION = "3.36"' in psrc, "missing")
    tm = getattr(mod, "TASK_MODES", {}) if mod else {}
    check("GUI 下拉有上下文调制监测档",
          "record" in tm and tm["record"][0] == "上下文调制监测", str(list(tm)))
    check("两档区别写死在说明里（路由监控连账不记 vs 本档账全记）",
          "逐字节原样转发" in str(tm.get("monitor", ""))
          and "全速" in str(tm.get("record", "")), str(tm))
    base = {"total": 123, "body": 100, "tools": 20, "raw_total": 456}
    txt = mod.ctx_live_text({"actual": base, "task_mode": "record"}) if mod else ""
    check("实况条带调制监测标记", "调制监测" in txt and "直通" in txt, txt)
    txt1 = mod.ctx_live_text({"actual": base, "task_mode": "auto"}) if mod else ""
    check("auto 不带调制监测标记", "调制监测" not in txt1, txt1)
    txt2 = mod.ctx_live_text({"actual": None, "task_mode": "record"}) if mod else ""
    check("没请求时也说清在监测档", "调制监测" in txt2, txt2)
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def _font_of(widget):
    """控件自带字体优先；控件字体是已销毁解释器的内部字号名（font3 之类）时退回默认。"""
    from tkinter import font as tkfont
    try:
        return tkfont.Font(font=widget.cget("font"))
    except Exception:
        return tkfont.nametofont("TkDefaultFont")


def test_task_mode_text_visible():
    """3.20：任务模式两处「文字被遮挡」+ 首句改「调制等于生成式改写」。
    ① 下拉框写死 width=6，7 个汉字的「上下文调制监测」要 84px 而控件只有 65px
       （用户截图「上下文记」）；② 说明文字挤在框右边拿 420px 假宽，真实只有
       229px，两头裁（用户截图）。
    判据核心=真建界面量出来的像素，不是字符串自慰。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-320-pre/main.py（3.19）必须红。"""
    mod, p = _gui_main_mod()
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    tm = getattr(mod, "TASK_MODES", {}) if mod else {}
    hint = str(tm.get("record", ("", ""))[1]) if tm else ""
    check("首句已改：调制等于生成式改写",
          hint.startswith("调制等于生成式改写") and "调制=改写" not in hint, hint[:24])
    check("说明全文没丢（全速句+对照句都在）",
          "全速" in hint and "压缩在不在场" in hint, hint[-16:])
    i18n_txt = ""
    try:
        i18n_txt = (p.parent / "i18n.py").read_text(encoding="utf-8")
    except Exception:
        pass
    check("旧文案全站消失（main 与 i18n 都不留 调制=改写）",
          "调制=改写" not in msrc and "调制=改写" not in i18n_txt, "still old")
    import re as _re
    check("下拉框已加宽到 18 格",
          bool(_re.search(r'state="readonly",\s*width=18\)', msrc)), "missing")
    check("说明改成独占整行（grid 跨两列）",
          "self.lbl_task_hint.grid(row=r, column=0, columnspan=2" in msrc, "missing")
    check("说明折行宽度跟真实宽走",
          "wraplength=max(160, e.width - 4)" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    if mod and hasattr(mod, "build_root"):
        try:
            root = mod.build_root()
            root.geometry("1120x780+30+30")   # 用户报裁字时的窗口量级
            for _ in range(25):
                root.update()
            cb = root.app.cb_task_mode
            lbl = root.app.lbl_task_hint
            cbf = _font_of(cb)
            need = max(cbf.measure(str(v)) for v in cb.cget("values"))
            cbw = cb.winfo_width()
            # 切到 record 档（用户截图那档）再量说明文字
            root.app.v_task_mode.set(mod.i18n.t("上下文调制监测"))
            root.app._task_mode_key = "record"
            root.app._on_task_mode_change()
            for _ in range(15):
                root.update()
            txt = str(lbl.cget("text"))
            wl = int(lbl.cget("wraplength") or 0)
            reqw, reqh = lbl.winfo_reqwidth(), lbl.winfo_reqheight()
            gotw, goth = lbl.winfo_width(), lbl.winfo_height()
            # 3.20 首版的坑：说明标签 grid 在下拉框同一行且 columnspan=2，
            # 把「任务模式」标签和下拉框整个盖住——必须量矩形，光量自己宽高看不出来。
            tm_row = cb.master
            g_cb, g_lb = tm_row.grid_info(), lbl.grid_info()
            cb_mapped, cb_h = cb.winfo_ismapped(), cb.winfo_height()
            top_cb = tm_row.winfo_rooty()
            bot_cb = top_cb + tm_row.winfo_height()
            top_lb = lbl.winfo_rooty()
            root.destroy()
            check("下拉框真可见（mapped 且有高度）",
                  cb_mapped == 1 and cb_h > 10, f"mapped={cb_mapped} h={cb_h}")
            row_cb = int(g_cb.get("row", -1))
            row_lb = int(g_lb.get("row", -1))
            check("说明独占下一行（不与下拉框同 grid 行）",
                  row_cb >= 0 and row_lb == row_cb + 1,
                  f"cb行={row_cb} 说明行={row_lb}")
            check("说明矩形不压住下拉框", top_lb >= bot_cb,
                  f"说明顶={top_lb} 下拉框底={bot_cb}")
            check("下拉框装得下最长档名（真实宽≥文字需宽）",
                  cbw > 0 and cbw >= need, f"cb={cbw} need={need}")
            check("说明按真实宽度折行（0<wraplength≤真实宽）",
                  0 < wl <= gotw, f"wl={wl} w={gotw}")
            check("说明整行不裁字（reqwidth≤真实宽）",
                  reqw <= gotw and gotw > 0, f"req={reqw} got={gotw}")
            check("record 说明全文排进控件（Tk 自算需高≤真实高）",
                  0 < reqh <= goth and goth > 0, f"reqh={reqh} got={goth}")
            check("record 说明全文一字没少", txt == hint, txt[:20])
        except Exception as exc:
            check("任务模式界面构建", False, repr(exc))


def test_kpi_model_baseline():
    """3.20：基线按模型分档，且必须与绿线同口径才可比（用户定案）。
    旧算法「当期 20 轮 vs 再往前 20 轮」在样本 <40 时兜底成全体均值，绿线是
    前缀均值 → 数学上必然长期低于基线（用户实测「绿线始终低于基线」的真因）。
    新口径：合格轮次=池≥5·前 10 万 token·非退避；够 20 轮才成线，不够就不画
    也不出降档建议；本地量化档与云端同族共用，但本档 0 合格样本时只作参照。
    有牙核验：本判据在 3.19 源码上全红（没有这些函数）。"""
    mod, p = _gui_main_mod()
    check("3.20 基线函数齐备",
          all(callable(getattr(mod, f, None)) for f in
              ("kpi_model_of", "baseline_group", "baseline_gate", "baseline_calc",
               "baseline_pick", "_kpi_roll", "_kpi_echo_rate", "_kpi_probe_rate",
               "load_baselines", "save_baselines", "baselines_path")), str(p))
    if not callable(getattr(mod, "baseline_pick", None)):
        return
    # --- 归组
    check("旧行无 model 字段→归不进基线",
          mod.kpi_model_of({"t": "13:00"}) == mod.BASELINE_NO_MODEL, "")
    check("本地量化档与云端同族共用基线",
          mod.baseline_group("swift-1.5-iq3_xxs", {})
          == mod.baseline_group("qwen3.8-flash", {}) == "qwen3.8-flash", "")
    check("陌生模型自成一组（不乱并别人基线）",
          mod.baseline_group("kimi-k3.1-pro", {}) == "kimi-k3.1-pro", "")
    # --- 门槛
    ok = {"a": 8, "e": 2, "f": 0, "usage": 60000, "can": {"alive": 3, "lost": 1}}
    check("合格轮次放行", mod.baseline_gate(ok, "echo"), str(ok))
    check("池<5 不进回声率基线",
          not mod.baseline_gate(dict(ok, a=3), "echo"), "should reject")
    check("超 10 万上下文不进基线（那是高压段）",
          not mod.baseline_gate(dict(ok, usage=120000), "echo"), "should reject")
    check("退避轮不进基线", not mod.baseline_gate(dict(ok, thr=1), "echo"), "")
    check("池小但探针有数→探针口径放行",
          mod.baseline_gate(dict(ok, a=2), "probe")
          and not mod.baseline_gate(dict(ok, a=2, can={}), "probe"), "")
    check("回声率读数=（e+f)/a",
          abs(mod._kpi_echo_rate({"a": 4, "e": 1, "f": 1}) - 0.5) < 1e-9, "")
    check("探针存活率=alive/(alive+lost)",
          abs(mod._kpi_probe_rate({"can": {"alive": 3, "lost": 1}}) - 0.75) < 1e-9, "")
    # --- 均值与缺口语
    rows20 = [dict(ok, e=i % 3, usage=50000 + i) for i in range(20)]
    b, n, why = mod.baseline_calc(rows20, "echo")
    check("20 轮合格→算得出基线", b is not None and n == 20, f"{b} {n}")
    b2, n2, why2 = mod.baseline_calc([dict(ok, a=2) for _ in range(30)], "echo")
    check("全被池门槛拦掉→无基线且报出拦截数",
          b2 is None and n2 == 0 and "池<5" in why2, why2)
    # --- 选口径
    m, base, bn, note = mod.baseline_pick(rows20)
    check("回声率够 20 轮→用回声率", m == "echo" and base is not None and bn >= 20,
          f"{m} {base} {bn}")
    probe_rows = [dict(ok, a=2, can={"alive": 4, "lost": 1}) for _ in range(25)]
    m2, base2, bn2, note2 = mod.baseline_pick(probe_rows)
    check("池子长期<5（本地档）→退到探针存活率",
          m2 == "probe" and abs(base2 - 0.8) < 1e-9 and "探针" in note2,
          f"{m2} {base2} {note2}")
    m3, base3, bn3, note3 = mod.baseline_pick(rows20[:9])
    check("合格样本不足 20 轮→宁可不画（base=None）",
          m3 is None and base3 is None and "不足" in note3, f"{m3} {base3} {note3}")
    # --- 重心同口径：池<5 的轮次不得进回声率重心
    r = mod._kpi_roll([0.0, None, 1.0], win=2)
    check("重心窗口内 None 不计入", r == [0.0, 0.0, 1.0], str(r))
    # --- 台账落盘往返
    import tempfile
    old_pd = mod.proxy_dir
    tmp = Path(tempfile.mkdtemp(prefix="longctx-base-"))
    mod.proxy_dir = lambda: tmp
    try:
        led = mod.load_baselines()
        check("没有台账文件→空表不崩", led == {}, str(led))
        led["qwen3.8-flash"] = {"metric": "echo", "base": 0.1234, "n": 25,
                                "src": "manual"}
        mod.save_baselines(led)
        check("台账写读往返一致",
              mod.load_baselines()["qwen3.8-flash"]["base"] == 0.1234
              and (tmp / "baselines.json").exists(), "")
        # --- 接线（静态）
        msrc = p.read_text(encoding="utf-8")
        check("绿线数据源=调取线图所选（4.1.0）",
              'view_mdl = getattr(self, "kpi_model_view", None) or mdl' in msrc
              and "_kpi_window_rows(src_rows, win_sec)" in msrc, "missing")
        check("基线未成型时不画灰线", "if base is not None:\n                yb =" in msrc,
              "missing")
        check("共用参照档不出降档建议（3.23 加 comparable 闸门）",
              "base is not None and not ref_only and comparable else 0" in msrc,
              "missing")
        check("量程至少到基线×2（治灰线被抬半图高）",
              "(base or 0.0) * 2" in msrc, "missing")
        check("更新基线按钮已接线（4.2.0）",
              'text="更新基线"' in msrc and "_kpi_update_baseline" in msrc, "missing")
        psrc = (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8")
        check("代理 kpi 行带实际转发模型名",
              '"model": str(data.get("model") or "")' in psrc, "missing")
        check("PROXY_VERSION=3.34（代理动了 kpi 字段）",
              'PROXY_VERSION = "3.36"' in psrc, "missing")
        check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    finally:
        mod.proxy_dir = old_pd


def test_kpi_green_selfview():
    """3.21：基线未成型时绿线自显，不再陪灰线藏（用户定案「绿线当然是自显」）。
    根因：baseline_pick 被 usage>10 万全拦 → metric=None → 绿线落 echo 分支 →
    池<5 全 None → 一条不画；蓝线不过闸照画——用户实见「蓝线还有绿线没了」。
    自显规则：池<5 用探针存活率、池≥5 用回声率，整段点线=只看不判；
    灰线一成型自动恢复与基线同口径。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-321-pre/main.py 必红（没有该函数、
    旧 dash 表达式、旧版本号）。"""
    mod, p = _gui_main_mod()
    gs = getattr(mod, "kpi_green_series", None) if mod else None
    check("3.21 kpi_green_series 存在", gs is not None, str(p))
    if gs:
        low_pool = [{"a": 3, "e": 0, "f": 0, "usage": 282253,
                     "can": {"alive": 10, "lost": 0}},
                    {"a": 3, "e": 3, "f": 0, "usage": 294921,
                     "can": {"alive": 20, "lost": 0}}]
        g, name, cmp_ = gs(low_pool, None, 3, False)
        check("3.23 无基线+池 3 → 回声重心自显（0/3→3/3），不再全 None",
              not cmp_ and [round(v, 4) for v in g] == [0.0, 1.0]
              and name == "回声重心·自显", f"{g} {name} {cmp_}")
        g3, name3, cmp3 = gs(low_pool, "echo", 3, True)
        check("3.23 灰线 echo 口径 → 回声重心·可比（池<5 有值，点线低置信）",
              cmp3 and [round(v, 4) for v in g3] == [0.0, 1.0]
              and name3 == "回声重心", f"{g3} {cmp3}")
        g4, name4, cmp4 = gs(low_pool, "probe", 3, True)
        check("3.23 灰线 probe 口径 → 绿线仍是回声重心且不可比（跨口径不打分）",
              not cmp4 and [round(v, 4) for v in g4] == [0.0, 1.0]
              and name4 == "回声重心", f"{g4} {cmp4}")
        g5, _, _ = gs([{"a": 0}, {"a": 6, "e": 1, "f": 0}], None, 6, False)
        check("自显回声口径 a=0 → None（不画假 0）",
              g5[0] is None and abs(g5[1] - 1 / 6) < 1e-9, str(g5))
    msrc = p.read_text(encoding="utf-8")
    check("接线：_update_kpi 用 kpi_green_series",
          "= kpi_green_series(" in msrc, "missing")
    check("池<5 降点线（3.23：只看池子置信度）",
          '(1, 3) if pool_a < BASELINE_MIN_POOL else (8, 3)' in msrc, "missing")
    check("旧 gname 硬编码已删（口径名由 kpi_green_series 给出）",
          'gname = "探针存活重心" if metric == "probe"' not in msrc, "还在")
    check("图例已写明自显语义", "没有基线时绿线照常画" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_baseline_pick_on_switch():
    """3.21：任务模型列表换模型 → 问用哪条基线（用户定案「应该加上」）。
    映射存 baselines.json 的 model_group 键，用户映射优先内置 BASELINE_GROUPS；
    选"先不共用"写 model→model（自成组且不再打扰）。共用=只当参照、
    不给本档出降档建议（沿用 3.20 ref_only 语义，own_n==0 那条不动）。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-321-pre/main.py 必红。"""
    mod, p = _gui_main_mod()
    check("baseline_group 收显式映射表（可测，不碰盘）",
          mod.baseline_group("kimi-k3.1-pro", {"kimi-k3.1-pro": "qwen3.8-flash"})
          == "qwen3.8-flash", "")
    check("用户映射优先内置表",
          mod.baseline_group("swift-1.5-iq3_xxs",
                             {"swift-1.5-iq3_xxs": "kimi-k3.1-pro"})
          == "kimi-k3.1-pro", "")
    check("不共用映射（model→model）= 仍自成组",
          mod.baseline_group("qwen3.8-flash", {"qwen3.8-flash": "qwen3.8-flash"})
          == "qwen3.8-flash", "")
    check("空映射→内置行为不变",
          mod.baseline_group("swift-1.5-iq3_xxs", {}) == "qwen3.8-flash", "")
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="longctx-umap-"))
    old_pd = mod.proxy_dir
    mod.proxy_dir = lambda: tmp
    try:
        mod._MODEL_GROUP_CACHE[0] = None
        led = mod.load_baselines()
        led["qwen3.8-flash"] = {"metric": "echo", "base": 0.1, "n": 20,
                                "src": "manual"}
        led["model_group"] = {"glm-5": "qwen3.8-flash"}
        mod.save_baselines(led)
        check("映射落盘读回", mod._user_model_group() == {"glm-5": "qwen3.8-flash"}, "")
        check("baseline_group 缺省走盘上映射",
              mod.baseline_group("glm-5") == "qwen3.8-flash", "")
        check("model_group 键不会被当成基线条目（候选枚举要 base 非空）",
              led["model_group"].get("base") is None, "")
    finally:
        mod._MODEL_GROUP_CACHE[0] = None
        mod.proxy_dir = old_pd
    msrc = p.read_text(encoding="utf-8")
    check("问基线弹窗已接线：换行选中+保存列表两个入口",
          "_ask_baseline_binding(" in msrc
          and msrc.count("_ask_baseline_binding(") >= 3, "missing")
    check("初次建台程序化选中不打扰",
          "if prev is not None and prev != i:" in msrc, "missing")
    check("候选只挑有 base 的台账行",
          'isinstance(e, dict) and e.get("base") is not None' in msrc, "missing")
    check("图例写明换模型可挑线", "选某个组＝共用那组的基线" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_axis_cleanup():
    """3.22：删蓝线（每轮原始值）与橙线（k 系数线）、纵轴加百分比刻度、
    灰线右端标数值、紫线与绿线共用 0..hi 一条量程（用户定案：蓝线"除了让
    线图更复杂"、橙线"没用的话也清除掉"、"线图左侧应该增加上下轴的坐标"）。
    数字不丢：回声率与 k 仍在文字行读数里。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-322-pre/main.py 必红（蓝橙线还在、
    无 _kpi_y_ticks、紫线旧量程、图例旧文案）。"""
    mod, p = _gui_main_mod()
    yt = getattr(mod, "_kpi_y_ticks", None) if mod else None
    check("3.22 _kpi_y_ticks 存在", yt is not None, str(p))
    if yt:
        ticks = yt(1.0, 172)
        check("hi=1 → 五档刻度 0/25/50/75/100%",
              [lbl for _, lbl in ticks] == ["0%", "25%", "50%", "75%", "100%"],
              str(ticks))
        check("0% 在地板线位、100% 在顶部（y 递减）",
              abs(ticks[0][0] - 169) < 1e-6 and abs(ticks[-1][0] - 3) < 1e-6,
              str(ticks))
        check("hi=0.4 → 顶档标 40%（小数尾巴已 round）",
              [lbl for _, lbl in yt(0.4, 172)][-1] == "40%", "")
        check("hi=0.15 → 25% 档标 3.8%（一位小数够用）",
              [lbl for _, lbl in yt(0.15, 172)][1] == "3.8%", "")
        check("hi 异常（0/None）→ 空表不画错", yt(0, 172) == [] and yt(None, 172) == [])
    msrc = p.read_text(encoding="utf-8")
    check("蓝线已删", 'poly(er, "#2a86c8"' not in msrc, "还在")
    check("橙线已删", '"#d08030"' not in msrc, "还在")
    check("回声率/k 数字仍在文字行",
          "回声率 {er[-1] * 100:.0f}%" in msrc
          and "k {last.get('k')" in msrc, "missing")
    check("紫线与绿线共用一条量程（一张图不搞两种高度含义）",
          'poly(hits, "#7a40c8", 0.0, hi,' in msrc, "missing")
    check("纵轴刻度接线（3.24 白底矩形垫底，不走 create_text(background=)）",
          "_kpi_draw_y_ticks(cv, hi, hp)" in msrc and "_kpi_label(" in msrc,
          "missing")
    check("灰线右端标数值（3.24 抽成 _kpi_draw_base_label）",
          "_kpi_draw_base_label(cv, w, yb, base)" in msrc, "missing")
    check("图例改为纵轴语义", "共用一个百分比刻度" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_clear_and_gate():
    """3.23：降档建议口径闸门 + 清空基线 + 基线下拉 + 保存基线备份
    （用户定案：基线要真实、绿线也要真实；不同模型要用各自基线；坏参照要能
    一键清、台账要能起名备份）。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-323-pre/main.py 必红。"""
    mod, p = _gui_main_mod()
    msrc = p.read_text(encoding="utf-8")
    check("降档建议只认与绿线同口径的基线（probe 口径不出建议）",
          "not ref_only and comparable else 0" in msrc, "missing")
    check("探针口径基线在文字行标注跨口径不打分",
          "跨口径不互相打分" in msrc, "missing")
    check("清空基线按钮已删（4.2.0）",
          'text="清空基线"' not in msrc and "_kpi_clear_baseline" not in msrc
          and "led.pop(grp)" not in msrc, "still there")
    check("基线下拉已接线（只读·绑事件·写 model_group）",
          "self.cb_kpi_base = ttk.Combobox(" in msrc
          and 'state="readonly"' in msrc.split("self.cb_kpi_base = ttk.Combobox(")[1][:80]
          and 'bind("<<ComboboxSelected>>", self._kpi_base_select)' in msrc
          and "umap[mdl] = sel" in msrc, "missing")
    check("下拉选项=自成一组+台账各组（只列有 base 的行）",
          'opts = [BASELINE_SELF] + sorted(' in msrc
          and 'isinstance(e, dict) and e.get("base") is not None' in msrc,
          "missing")
    check("当前组/模型接线（清空与下拉找得到对象）",
          "self.kpi_grp = grp" in msrc and "self.kpi_mdl = mdl" in msrc,
          "missing")
    check("保存基线按钮已删（4.2.0）",
          'text="保存基线"' not in msrc and "_kpi_save_baseline" not in msrc,
          "still there")
    check("空台账备份提示已删", "备份出来是空表" not in msrc, "still there")
    check("图例写明绿线永久回声口径",
          "绿线＝回声重心" in msrc, "missing")
    check("图例写明基线下拉可反悔", "「基线」下拉＝只决定灰线" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_axis_draw_324():
    """3.24：横轴时间/纵轴标注被腰斩的真凶修复——Canvas 文本项没有 background
    选项，3.23 的 create_text(background=) 每次刷新抛 TclError，把刻度、时间轴、
    文字行整条刷新链全部中断（用户截图：图有线没刻度，文字行停在"等代理出数据"）。
    判据必须真开 Tk Canvas 画一遍——静态断言查不出 TclError，这正是上一版假绿的
    教训。附带验 ScrollColumn 双滚动条（四个栏目共用该类）。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-324-pre/main.py 必红（无绘制函数、
    仍有 background=、无 hsb）。"""
    mod, p = _gui_main_mod()
    check("3.24 绘制函数齐备",
          all(callable(getattr(mod, f, None)) for f in
              ("_kpi_label", "_kpi_draw_y_ticks", "_kpi_draw_time_ticks",
               "_kpi_draw_base_label")), str(p))
    msrc = p.read_text(encoding="utf-8")
    check("源码不再有 create_text(background=)",
          'background="#ffffff"' not in msrc, "还在")
    check("四个栏目都走 ScrollColumn",
          msrc.count("ScrollColumn(") >= 5, str(msrc.count("ScrollColumn(")))
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        cv = tk.Canvas(root, width=420, height=190, bg="#fafafa")
        root.update_idletasks()
        n1 = mod._kpi_draw_y_ticks(cv, 1.0, 172)
        texts = [cv.itemcget(i, "text") for i in cv.find_all()
                 if cv.type(i) == "text"]
        check("真 Canvas 画纵轴：5 条刻度且不抛异常", n1 == 5, str(n1))
        check("纵轴标签内容 0/25/50/75/100%",
              texts == ["0%", "25%", "50%", "75%", "100%"], str(texts))
        rows = [{"t": f"10:{i:02d}:00"} for i in range(30)]
        n2 = mod._kpi_draw_time_ticks(cv, rows, 420, 190)
        all_texts = [cv.itemcget(i, "text") for i in cv.find_all()
                     if cv.type(i) == "text"]
        check("真 Canvas 画横轴：4 条时间刻度且为 HH:MM",
              n2 == 4 and sum(1 for t in all_texts if t.startswith("10:")) == 4,
              f"{n2} {all_texts}")
        mod._kpi_draw_base_label(cv, 420, 50, 0.42)
        all_texts = [cv.itemcget(i, "text") for i in cv.find_all()
                     if cv.type(i) == "text"]
        check("基线数值标签画出", "基线 42%" in all_texts, str(all_texts))
        rects = [i for i in cv.find_all() if cv.type(i) == "rectangle"]
        check("白底矩形与文字一一对应（垫在字底下）",
              len(rects) == len(all_texts), f"{len(rects)}/{len(all_texts)}")
        check("文字全部有 bbox（没有被 TclError 腰斩）",
              all(cv.bbox(i) is not None for i in cv.find_all()
                  if cv.type(i) == "text"), "")
        root.destroy()
    except Exception as exc:
        check("真 Canvas 绘制不抛异常", False, f"{type(exc).__name__}: {exc}")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        sc = mod.ScrollColumn(root)
        check("ScrollColumn 纵向+横向条齐备",
              hasattr(sc, "vsb") and hasattr(sc, "hsb")
              and str(sc.hsb.cget("orient")) == "horizontal", "")
        ev_h = type("E", (), {"delta": -120, "state": 0x0001})()
        ev_v = type("E", (), {"delta": -120, "state": 0})()
        sc._wheel(ev_h)   # Shift → 横向分支
        sc._wheel(ev_v)   # 普通 → 纵向分支
        check("滚轮双分支可用（Shift=横向）", True, "")
        root.destroy()
    except Exception as exc:
        check("ScrollColumn 行为可用", False, f"{type(exc).__name__}: {exc}")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_chart_snapshots_325():
    """3.25：每模型一张图——线图快照保存/调取/新建（用户定案：不同模型的线图
    要能存能调，横纵轴必须有数据标注）；手动定标门槛 20 轮（基线必须真实）。
    判据：快照数据包往返、真 Canvas 重绘（纵轴百分比/横轴时间/基线标注）、
    三个按钮接线。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-325-pre/main.py 必红。"""
    mod, p = _gui_main_mod()
    snp = getattr(mod, "kpi_snapshot_payload", None) if mod else None
    draw = getattr(mod, "_kpi_draw_snapshot", None) if mod else None
    check("3.25 快照函数齐备", snp and draw, str(p))
    check("手动定标门槛提为 20 轮（基线必须真实）",
          getattr(mod, "BASELINE_MANUAL_N", None) == 20,
          str(getattr(mod, "BASELINE_MANUAL_N", None)))
    if snp:
        rows = [{"t": "10:00:00", "a": 3, "e": 1, "f": 0, "usage": 50000,
                 "can": {"alive": 3, "lost": 1}, "k": 0.9} for _ in range(25)]
        led = {"qwen3.8-flash": {"metric": "echo", "base": 0.1234, "n": 25,
                                 "src": "manual"}}
        sn = snp(rows, "qwen3.8-flash", "qwen3.8-flash", led)
        check("快照带组/模型/时间/基线口径",
              sn["group"] == "qwen3.8-flash" and sn["model"] == "qwen3.8-flash"
              and sn["metric"] == "echo" and abs(sn["base"] - 0.1234) < 1e-9, "")
        check("快照带每轮读数（截 60 轮）与绿线序列",
              len(sn["rounds"]) == 25 and len(sn["green"]) == 25
              and abs(sn["echo"][0] - 1 / 3) < 1e-9, "")
        check("快照可 JSON 往返（存盘调取的前提）",
              __import__("json").loads(__import__("json").dumps(sn))["base"]
              == sn["base"], "")
        led_probe = {"g": {"metric": "probe", "base": 1.0, "n": 20}}
        sn2 = snp(rows, "g", "m", led_probe)
        check("probe 口径基线照样记下（metric 字段区分）",
              sn2["metric"] == "probe" and sn2["base"] == 1.0, "")
    if draw and snp:
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.update_idletasks()
            cv = tk.Canvas(root, width=780, height=280, bg="#fafafa")
            st = draw(cv, sn, 780, 280)
            texts = [cv.itemcget(i, "text") for i in cv.find_all()
                     if cv.type(i) == "text"]
            check("重绘：纵轴 5 档刻度", st["ticks"] == 5, str(st))
            check("重绘：横轴时间标注在", sum(1 for t in texts if ":" in t) >= 2,
                  str(texts))
            check("重绘：echo 基线标注画出", "基线 12%" in texts, str(texts))
            check("重绘：绿线末值返回", st["last"] == "33%", str(st["last"]))
            cv2 = tk.Canvas(root, width=780, height=280, bg="#fafafa")
            st2 = draw(cv2, sn2, 780, 280)
            t2 = [cv2.itemcget(i, "text") for i in cv2.find_all()
                  if cv2.type(i) == "text"]
            check("probe 口径快照不画基线（跨口径不混比）",
                  st2["base"] is None and not any("基线" in t for t in t2), str(t2))
            root.destroy()
        except Exception as exc:
            check("快照真 Canvas 重绘不抛异常", False,
                  f"{type(exc).__name__}: {exc}")
    msrc = p.read_text(encoding="utf-8")
    check("4.1.0 按钮：新建/保存已删；调取线图改为模型数据源选择",
          'text="新建线图"' not in msrc and 'text="保存线图"' not in msrc
          and 'text="调取线图"' in msrc
          and "_kpi_model_pick" in msrc and "_kpi_time_range" in msrc,
          "missing")
    check("4.1.0 旧线图快照入口已删（新建/保存/调取快照）",
          '"_kpi_save_chart"' not in msrc and '"_kpi_new_chart"' not in msrc
          and '"_kpi_load_chart"' not in msrc and '"_kpi_chart_window"' not in msrc,
          "still there")
    check("4.2.0 起线图/基线存档入口全部收起（无残留清洗代码）",
          '"/" if c in' not in msrc and '"·" if c in' not in msrc, "still there")
    check("图例写明每模型一张图", "每模型一张图" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_kpi_buttons_visible_326():
    """3.26：KPI 按钮从单行拆成两栏——原来 9 个控件挤一行，窄列里后面的按钮
    被裁出可视区（用户报"界面上看不到新建线图/调取线图"）。行为判据：真建界面，
    七个按钮全部 mapped、宽>30、且不越过父栏右缘。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-326-pre/main.py 必红（单行挤爆）。"""
    mod, p = _gui_main_mod()
    msrc = p.read_text(encoding="utf-8")
    check("按钮已拆两栏（kpibar/kpibar2）",
          "self.kpibar = kpibar" in msrc and "self.kpibar2 = kpibar2" in msrc,
          "missing")
    check("文字行只留标签，按钮进栏",
          "self.btn_kpi_win = ttk.Button(kpibar" in msrc
          and "self.btn_kpi_base = ttk.Button(kpibar2" in msrc, "missing")
    if mod and hasattr(mod, "build_root"):
        try:
            root = mod.build_root()
            root.geometry("1600x900+30+30")
            for _ in range(10):
                root.update()
            app = root.app
            bad = []
            for name in ("btn_kpi_win", "btn_kpi_model", "btn_kpi_base"):
                b = getattr(app, name)
                x, wd = b.winfo_x(), b.winfo_width()
                pw = b.master.winfo_width()
                if not b.winfo_ismapped() or wd < 30 or x + wd > pw + 2:
                    bad.append((name, b.winfo_ismapped(), x, wd, pw))
            combo_ok = (app.cb_kpi_base.winfo_ismapped()
                        and app.cb_kpi_base.winfo_x() + app.cb_kpi_base.winfo_width()
                        <= app.cb_kpi_base.master.winfo_width() + 2)
            root.destroy()
            check("七个 KPI 按钮全部可见且在栏内", not bad, str(bad))
            check("基线下拉可见且在栏内", combo_ok, "")
        except Exception as exc:
            check("KPI 按钮可见性（Tk 构建）", False, repr(exc))
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_old_bucket_rename_327():
    """3.27：旧桶定名「qwen3.8 flash历史统计数据」（用户定案：旧数据实测就是
    云端 qwen3.8-flash 的历史）——归组常量、文字行文案、台账键名三处同步。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-327-pre/main.py 必红。"""
    mod, p = _gui_main_mod()
    msrc = p.read_text(encoding="utf-8")
    check("归组常量已定名",
          getattr(mod, "BASELINE_NO_MODEL", "") == "qwen3.8 flash历史统计数据",
          str(getattr(mod, "BASELINE_NO_MODEL", None)))
    if mod:
        check("无 model 行归到新名",
              mod.kpi_model_of({"t": "13:00"}) == "qwen3.8 flash历史统计数据", "")
    check("文字行文案同步", "历史统计档（qwen3.8-flash 早期数据）" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_baseline_degenerate():
    """3.29：退化基线拒收——零方差且贴饱和端（探针全活恒 100%/回声全 0）
    不构成参照线（用户报"21 轮合格基线 100%，是胡扯"）。区间内稳定值
    （如恒 0.8）不算退化，仍可用——3.20 的既有用例不许被误杀。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-329-pre/main.py（3.28）必须红。"""
    mod, p = _gui_main_mod()
    dg = getattr(mod, "baseline_degenerate", None) if mod else None
    ev = getattr(mod, "baseline_entry_valid", None) if mod else None
    check("baseline_degenerate / baseline_entry_valid 齐备",
          dg is not None and ev is not None, str(p))
    if dg:
        check("21 轮全活恒 100% → 退化（用户报的那条）", dg([1.0] * 21, "probe"))
        check("回声全 0 → 退化", dg([0.0] * 20, "echo"))
        check("区间内恒定 0.8 不算退化（既有用例不许误杀）",
              not dg([0.8] * 25, "probe"))
        check("有波动 → 不退化", not dg([0.8, 0.9, 0.7], "probe"))
        check("单样本 → 退化", dg([0.5], "probe"))
        check("空表 → 退化", dg([], "probe"))
    if ev:
        check("台账存量 probe 1.0 → 作废", not ev({"metric": "probe", "base": 1.0}))
        check("台账 probe 0.8 → 有效", ev({"metric": "probe", "base": 0.8}))
        check("台账 echo 1.0 不受此闸（条目无值序列）",
              ev({"metric": "echo", "base": 1.0}))
        check("空条目 → 无效", not ev({}) and not ev(None))
    if mod and dg:
        ok = {"a": 2, "usage": 50000}
        all_alive = [dict(ok, can={"alive": 5, "lost": 0}) for _ in range(25)]
        m, b, n, note = mod.baseline_pick(all_alive)
        check("自动成线：全活恒 100% → 不出线且注记无区分度",
              m is None and b is None and "无区分度" in note, f"{m} {b} {note}")
        mixv = [dict(ok, can={"alive": 5, "lost": 0} if i % 3 else
                     {"alive": 4, "lost": 1}) for i in range(25)]
        m2, b2, n2, note2 = mod.baseline_pick(mixv)
        check("自动成线：有 lost 波动 → 正常出探针线",
              m2 == "probe" and b2 is not None, f"{m2} {b2} {note2}")
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("接线：更新基线走 baseline_from_model（退化在函数内拒收）",
          "baseline_from_model(rows, model)" in msrc
          and 'if baseline_degenerate(vals, "echo")' in msrc, "missing")
    check("接线：台账条目过 validity 闸",
          "if not baseline_entry_valid(ent):" in msrc, "missing")
    check("接线：下拉只列有效基线（4.1.0 起并去重）",
          "g != BASELINE_SELF and baseline_entry_valid(e)" in msrc, "missing")
    check("接线：更新基线按模型选择（不再有混组平均）",
          "用哪款模型的全部有效数据生成新基线" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_baseline_bg_collect():
    """4.1.0：后台按实际模型收集基线——下拉选什么都收集；每模型只用自己的行；
    够 20 轮且非退化才写；手动条目永不覆盖；值不变不重写。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-410-pre/main.py（4.0.0）必须红。"""
    mod, p = _gui_main_mod()
    fn = getattr(mod, "collect_model_baselines", None) if mod else None
    check("collect_model_baselines 存在", fn is not None, str(p))
    if fn:
        ok = {"model": "mA", "a": 8, "e": 2, "f": 0, "usage": 60000}
        rowsA = [dict(ok, e=i % 3) for i in range(25)]
        rowsB = [dict(ok, model="mB", a=2, can={"alive": 5, "lost": 0})
                 for _ in range(25)]
        led = {}
        ch = fn(rowsA + rowsB, led)
        check("mA 自动成线（键=模型名，src=auto，回声口径）",
              "mA" in ch and led.get("mA", {}).get("src") == "auto"
              and led["mA"]["metric"] == "echo", str(ch))
        check("mB 退化不出条目（全活恒 100%）", "mB" not in led, str(list(led)))
        led2 = {"mA": {"metric": "echo", "base": 0.2, "n": 30, "src": "manual"}}
        fn(rowsA, led2)
        check("手动条目不被覆盖",
              abs(float(led2["mA"]["base"]) - 0.2) < 1e-9
              and led2["mA"]["src"] == "manual", str(led2["mA"]))
        ch2 = fn(rowsA, dict(led))
        check("值没变不重写（2 秒一跑不刷盘）", len(ch2) == 0, str(ch2))
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("接线：_update_kpi 后台收集",
          "collect_model_baselines(all_rows, led)" in msrc, "missing")
    check("接线：自成一组 → 本模型条目",
          "show_key = mdl if grp == BASELINE_SELF else grp" in msrc, "missing")
    check("接线：无条目现算只用本模型行",
          "own = [r for r in conv_rows if kpi_model_of(r) == mdl]" in msrc, "missing")
    check("接线：下拉去重（自成一组只列一次）",
          "g != BASELINE_SELF and baseline_entry_valid(e)" in msrc, "missing")
    if mod and hasattr(mod, "baseline_pick"):
        rows10 = [dict({"model": "mC", "a": 8, "f": 0, "usage": 60000},
                       e=i % 3) for i in range(10)]
        m3, b3, n3, note3 = mod.baseline_pick(rows10)
        check("4.0.2 n<20 不足话术不带空括号",
              m3 is None and "（）" not in note3 and "不足" in note3, str(note3))
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    check("代理已动（PROXY_VERSION 仍 3.31，版本双查）",
          'PROXY_VERSION = "3.36"' in
          (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8"),
          "proxy 版本漂移")


def test_kpi_range_and_model():
    """4.1.0（用户定案）：「线图时间范围」选绿线时间段；「调取线图」选绿线的
    模型数据源；「基线」下拉只决定灰线；「新建线图/保存线图」已删。
    有牙核验：LONGCTX_GUI_MAIN 指 _bak-41-pre/main.py（4.0.3）必红。"""
    mod, p = _gui_main_mod()
    win_rows = getattr(mod, "_kpi_window_rows", None) if mod else None
    models_in = getattr(mod, "_kpi_models_in", None) if mod else None
    rlabel = getattr(mod, "_kpi_range_label", None) if mod else None
    check("4.1.0 三个纯函数齐备", win_rows and models_in and rlabel, str(p))
    if win_rows:
        rows = [{"t": f"10:{m:02d}:00"} for m in (0, 30)] + \
               [{"t": f"11:{m:02d}:00"} for m in (0, 30)] + [{"t": "12:00:00"}]
        r = win_rows(rows, 3600)
        check("时间窗 1 小时 → 尾 3 行", len(r) == 3
              and r[0]["t"] == "11:00:00", str([x["t"] for x in r]))
        mid = [{"t": "23:50:00"}, {"t": "00:10:00"}, {"t": "00:30:00"}]
        check("跨午夜累计", win_rows(mid, 3600) == mid, str(win_rows(mid, 3600)))
        check("window=0 → 全量", win_rows(rows, 0) == rows, "")
        check("空表安全", win_rows([], 3600) == [])
    if models_in:
        ms = models_in([{"model": "a"}, {"model": "b"}, {"model": "a"}])
        check("模型枚举按轮数降序", ms == [("a", 2), ("b", 1)], str(ms))
    if rlabel:
        check("范围标签", rlabel(None) == "近 60 轮（默认）"
              and rlabel(3600) == "近 1 小时", "")
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("线图时间范围弹窗接线",
          "_kpi_time_range" in msrc and 'text="线图时间范围"' in msrc, "missing")
    check("调取线图=模型选择弹窗",
          "_kpi_model_pick" in msrc and "_kpi_models_in(rows)" in msrc
          and "（跟随实际运行：" in msrc, "missing")
    check("绿线数据=view_mdl + 时间窗",
          'view_mdl = getattr(self, "kpi_model_view", None) or mdl' in msrc
          and "_kpi_window_rows(src_rows, win_sec)" in msrc, "missing")
    check("基线下拉仍只管灰线",
          "show_key = mdl if grp == BASELINE_SELF else grp" in msrc, "missing")
    check("说明文案已写两按钮分工",
          "「线图时间范围」＝绿线看哪段时间" in msrc, "missing")
    check("文字行标出时间范围",
          "· 时间范围 {_kpi_range_label(win_sec)}" in msrc, "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_baseline_update_model():
    """4.2.0（用户定案）：更新基线=选模型→用其全部有效数据重算覆盖；
    清空基线/保存基线按钮已删。有牙核验：_bak-42-pre（4.1.0）必红。"""
    mod, p = _gui_main_mod()
    bfm = getattr(mod, "baseline_from_model", None) if mod else None
    check("baseline_from_model 存在", bfm is not None, str(p))
    if bfm:
        ok = {"model": "mZ", "a": 8, "f": 0, "usage": 60000}
        rows = [dict(ok, e=i % 3) for i in range(25)] + \
               [dict(ok, model="mY", a=2, can={"alive": 5, "lost": 0})
                for _ in range(25)]
        grp, metric, base, n, note = bfm(rows, "mZ")
        check("用模型全部有效数据算基线（回声口径 n=25）",
              metric == "echo" and base is not None and n == 25,
              f"{metric} {base} {n}")
        g2, m2, b2, n2, note2 = bfm(rows, "mY")
        check("退化样本拒收（全活恒 100%）",
              m2 is None and b2 is None and "无区分度" in note2, str(note2))
        g3, m3, b3, n3, note3 = bfm(rows, "none")
        check("无数据模型给出说明", m3 is None and "没有数据" in note3, str(note3))
        rows10 = [dict(ok, e=i % 3) for i in range(10)]
        g4, m4, b4, n4, note4 = bfm(rows10, "mZ")
        check("不足 20 轮不出基线且无空括号",
              m4 is None and "不足" in note4 and "（）" not in note4, str(note4))
    msrc = ""
    try:
        msrc = p.read_text(encoding="utf-8")
    except Exception:
        pass
    check("按钮：更新基线在、清空/保存已删",
          'text="更新基线"' in msrc and 'text="清空基线"' not in msrc
          and 'text="保存基线"' not in msrc
          and "_kpi_update_baseline" in msrc
          and "_kpi_clear_baseline" not in msrc
          and "_kpi_save_baseline" not in msrc, "missing")
    check("更新基线=选模型弹窗（全部有效数据）",
          "_kpi_models_in(rows)" in msrc
          and "用哪款模型的全部有效数据生成新基线" in msrc, "missing")
    check("覆盖写 manual 条目并记来源模型",
          '"from_model": model' in msrc and '"src": "manual"' in msrc,
          "missing")
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_thinking_override():
    """5.0.5 思考级别统一闸：删客户端拼法、换统一档；只改发往本地引擎的任务
    请求；改档读文件 mtime，不重启代理。有牙核验：旧 proxy（缺这三个函数）应红。"""
    import json
    import os as _os
    import tempfile
    import time as _time
    old_file, old_mode = proxy.THINKING_FILE, proxy.TASK_MODE
    old_state = dict(proxy._THINKING_STATE)
    tmp = Path(tempfile.mkdtemp(prefix="longctx-think-")) / "thinking.json"

    def put(level):
        tmp.write_text(json.dumps({"level": level}), encoding="utf-8")
        _os.utime(tmp, (_time.time() + 2, _time.time() + 2))

    try:
        proxy.THINKING_FILE = str(tmp)
        proxy._THINKING_STATE.update(stamp=None, level=None)
        proxy.TASK_MODE = "auto"
        check("缺文件=跟随客户端", proxy.thinking_level() is None)
        put("medium")
        check("读到 medium", proxy.thinking_level() == "medium")
        put("high")
        check("改档即时生效（high 即 xhigh）", proxy.thinking_level() == "high")
        tmp.write_text("{bad json", encoding="utf-8")
        _os.utime(tmp, (_time.time() + 4, _time.time() + 4))
        check("坏文件按跟随客户端", proxy.thinking_level() is None)
        put("huge")
        check("坏值按跟随客户端", proxy.thinking_level() is None)
        put("low")
        body = {"model": "m", "messages": [{"role": "user", "content": "你好"}]}
        check("本地任务请求要统一",
              proxy.thinking_override_for(body, "http://127.0.0.1:8080") == "low")
        check("云端不改", proxy.thinking_override_for(
            body, "https://api.example.com/v1") is None)
        summ = {"system": proxy.SUMMARY_MARK,
                "messages": [{"role": "user", "content": "压缩一下"}]}
        check("压缩请求不碰（它自己关思考）",
              proxy.thinking_override_for(summ, "http://127.0.0.1:8080") is None)
        proxy.TASK_MODE = "monitor"
        check("监控直通不碰", proxy.thinking_override_for(
            body, "http://127.0.0.1:8080") is None)
        proxy.TASK_MODE = "auto"
        b = {"reasoning_effort": "medium",
             "reasoning": {"effort": "high"},
             "thinking": {"type": "enabled", "budget_tokens": 4000},
             "output_config": {"effort": "low"},
             "enable_thinking": True,
             "reasoning_budget_tokens": 16000,
             "chat_template_kwargs": {"enable_thinking": True,
                                      "reasoning_effort": "medium", "keep": 1},
             "messages": []}
        ch = proxy.apply_thinking_level(b, "high")
        check("统一档写顶层", b["reasoning_effort"] == "high")
        check("客户端拼法清光", all(k not in b for k in (
            "reasoning", "thinking", "output_config", "enable_thinking")))
        check("ctk 只留无关键", b["chat_template_kwargs"] == {"keep": 1})
        check("改动有日志清单", "reasoning_effort=high" in ch and "thinking" in ch,
              str(ch))
        b2 = {"reasoning_budget_tokens": 16000,
              "chat_template_kwargs": {"enable_thinking": True}, "messages": []}
        proxy.apply_thinking_level(b2, "none")
        check("none 关思考", b2["reasoning_effort"] == "none"
              and b2["chat_template_kwargs"]["enable_thinking"] is False)
        check("none 删预算（免得被当成要思考）",
              "reasoning_budget_tokens" not in b2)
    finally:
        proxy.THINKING_FILE, proxy.TASK_MODE = old_file, old_mode
        proxy._THINKING_STATE.update(old_state)


def test_gui_thinking_control():
    """5.0.5 GUI：档位文件读写、控件接线、build_env 把文件传给代理。"""
    import json
    import os as _os
    import tempfile
    mod, p = _gui_main_mod()
    check("GUI 模块可加载", mod is not None, str(p))
    if mod is None:
        return
    tmp = Path(tempfile.mkdtemp(prefix="longctx-gui-think-")) / "thinking.json"
    old = _os.environ.get("LONGCTX_GUI_THINKING")
    try:
        _os.environ["LONGCTX_GUI_THINKING"] = str(tmp)
        check("缺文件=跟随客户端", mod.load_thinking() is None)
        mod.save_thinking("high")
        check("写读 roundtrip", mod.load_thinking() == "high")
        check("落盘形状代理认",
              json.loads(tmp.read_text(encoding="utf-8")) == {"level": "high"})
        mod.save_thinking(None)
        check("pass 落 null", mod.load_thinking() is None
              and json.loads(tmp.read_text(encoding="utf-8"))["level"] is None)
        env = mod.build_env(mod.default_profiles()[0])
        check("build_env 传档位文件", env.get("PROXY_THINKING_FILE") == str(tmp))
    finally:
        if old is None:
            _os.environ.pop("LONGCTX_GUI_THINKING", None)
        else:
            _os.environ["LONGCTX_GUI_THINKING"] = old
    msrc = p.read_text(encoding="utf-8")
    check("控件接线：选档即写", "command=self._thinking_changed" in msrc)
    check("xhigh 档在控件里", '("high", "xhigh")' in msrc)
    check("改档不重启代理（只写文件）",
          "def _thinking_changed" in msrc and "save_thinking(level)" in msrc)


def test_rule_hedge_quoted_332():
    """3.32 ①：认知犹豫句与带角色前缀的引用转述行不进规则/锚点计分。
    实测事故：助手自己的"ltd 没有定义我不能靠猜"被用户引用回贴后成 pri=1
    硬规则，每轮回注把模型锁死在歧义里。"""
    import checkpoint as ck
    bad = 'assistant: "ltd" 在我们上文中没有出现过定义，我不能靠猜回答'
    check("引用角色行不算规则", ck._is_rule_line(bad) == 0, bad)
    check("认知犹豫不算规则",
          ck._is_rule_line("我不能靠猜回答，它至少有三种完全不同的指向") == 0)
    check("真祈使不被误拦", ck._is_rule_line("必须先查原文再回答") == 1)
    check("不确定性祈使不被误拦", ck._is_rule_line("不确定时必须先问") == 1)
    st = ck.new_state("t")
    msgs = [{"role": "user", "content":
             "那九院检验科申请的ltd 具体是什么项目？\n" + bad}]
    st = ck.update(st, msgs, proxy.count_tokens, proxy.extract_facts)
    check("update 不收引用犹豫入规则",
          not any("不能靠猜" in r["t"] for r in st["rules"]), str(st["rules"]))


def test_correction_sync_332():
    """3.32 ②：显式纠正当轮同步改写 goal/rules/facts；reset 重播不再复活笔误。"""
    import checkpoint as ck
    st = ck.new_state("t")
    st["goal"] = "那九院检验科申请的ltd 具体是什么项目？"
    st["rules"] = [{"k": "poison", "t": "ltd 没有出现过定义，不能靠猜回答",
                    "pri": 1, "src": "user"}]
    st["facts"] = [{"k": "f1", "t": "九院检验科 ltd 项目", "src": "user"}]
    msgs = [{"role": "user", "content": st["goal"]},
            {"role": "assistant", "content": "检索 ltd。"},
            {"role": "user", "content": "应该查 ldt 不是 ltd"}]
    st = ck.update(st, msgs, proxy.count_tokens, proxy.extract_facts)
    check("goal 当轮换词 ldt",
          "ldt" in st["goal"] and "ltd" not in st["goal"], st["goal"])
    check("犹豫毒规则被删",
          not any("不能靠猜" in r["t"] for r in st["rules"]), str(st["rules"]))
    check("纠正规则 pri=2",
          any(r["pri"] == 2 and "ldt" in r["t"] for r in st["rules"]),
          str(st["rules"]))
    check("facts 同步换词（纠正账本除外）",
          all("ltd" not in f["t"] for f in st["facts"]
              if "用户纠正" not in f["t"]), str(st["facts"]))
    n = len(st["rules"])
    st = ck.update(st, msgs, proxy.count_tokens, proxy.extract_facts)
    check("纠正幂等不重复", len(st["rules"]) == n, str(st["rules"]))
    ok = ck.reseed_goal(st, msgs)
    check("reset 重播不复活笔误", ok and "ltd" not in st["goal"], st["goal"])
    st2 = ck.new_state("t2")
    st2["goal"] = "聊职业规划"
    check("自我介绍不误判纠正",
          ck.detect_corrections(st2, "我不是医生，是护士") == [])


def test_drop_fold_corrections_332():
    """3.32 ③④：只丢不摘丢弃前把用户纠正句折入检查点；本轮 drp 标记落字典。"""
    import checkpoint as ck
    import tempfile
    import shutil
    old_drop, old_sess = proxy.DROP_ONLY, proxy.SESS_DIR
    proxy.DROP_ONLY = 1
    tmp = Path(tempfile.mkdtemp(prefix="ck332_"))
    proxy.SESS_DIR = tmp
    key = ""
    try:
        big = "配置项说明文本。" * 4000
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "应该查 ldt 不是 ltd"}]
        for i in range(30):
            msgs.append({"role": "user", "content": f"第{i}条：{big}"})
            msgs.append({"role": "assistant", "content": "收到"})
        key = proxy.conversation_key(msgs)
        d = tmp / key
        d.mkdir(parents=True, exist_ok=True)
        st = ck.new_state(key)
        st["goal"] = "那九院检验科申请的ltd 具体是什么项目？"
        ck.save(d / "checkpoint.json", st)
        body = {"messages": msgs}
        proxy.transform(body, "fold-332")
        st2 = ck.load(d / "checkpoint.json")
        check("丢前纠正折入检查点 goal",
              "ldt" in st2["goal"] and "ltd" not in st2["goal"], st2["goal"])
        check("纠正规则落盘",
              any(r["pri"] == 2 and "ldt" in r["t"] for r in st2["rules"]),
              str(st2["rules"]))
        check("只丢不摘轮 drp 标记",
              proxy._DROP_LAST.get(key, 0) > 0, str(proxy._DROP_LAST.get(key)))
    finally:
        proxy.DROP_ONLY, proxy.SESS_DIR = old_drop, old_sess
        proxy._DROP_LAST.pop(key, None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_kpi_drp_gate_332():
    """3.32 ④：drp>0 轮绿线断口、不进基线原料；降档建议在断口处断开。"""
    mod, p = _gui_main_mod()
    gs = getattr(mod, "kpi_green_series", None) if mod else None
    check("kpi_green_series 存在", gs is not None, str(p))
    if gs:
        rows = [{"a": 24, "e": 0, "f": 0, "drp": 31, "usage": 50000},
                {"a": 24, "e": 12, "f": 0, "usage": 50000}]
        g, _, _ = gs(rows, "echo", 24, True)
        check("只丢不摘轮绿线断口", g[0] is None and g[1] == 0.5, str(g))
        bg = getattr(mod, "baseline_gate", None)
        check("只丢不摘轮不进基线",
              bg is not None and not bg(rows[0]) and bg(rows[1]))
        bv = getattr(mod, "_baseline_vals", None)
        if bv:
            vals, _np, _nu, _nt = bv(rows, "echo")
            check("基线原料剔除只丢不摘轮", vals == [0.5], str(vals))
        tb = getattr(mod, "_kpi_tail_below", None)
        check("降档建议遇断口断开",
              tb is not None and tb([0.01, 0.01, None, 0.01], 0.04) == 1)
    psrc = Path(proxy.__file__).read_text(encoding="utf-8")
    check("代理 KPI 行带 drp 字段",
          '"drp": int(_DROP_LAST.pop(ckey, 0) or 0)' in psrc)


def test_routing_takeover_333():
    """3.33 采样接管：剥客户端字段、植入 GUI 值；空=只剥不植；越界=不设；
    云端不碰；mtime 热读即时生效；GUI 显式 max_tokens 不被 max_think 抬。"""
    import json
    import tempfile
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="rt333_"))
    f = tmp / "routing.json"
    old_file = proxy.ROUTING_FILE
    proxy.ROUTING_FILE = str(f)
    proxy._ROUTING_STATE["stamp"] = None
    try:
        body = {"messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.7, "top_p": 0.8, "top_k": 40, "seed": 1,
                "max_tokens": 8192, "max_completion_tokens": 8192}
        ch, mx = proxy.apply_sampling_takeover(dict(body), "t")
        check("接管关=全透传（旧行为）", ch == [] and mx is False, str(ch))
        f.write_text(json.dumps({"takeover": True, "temperature": 1.0,
                                 "top_p": 0.95, "top_k": 20, "seed": None,
                                 "max_tokens": None}), encoding="utf-8")
        b2 = dict(body)
        ch2, mx2 = proxy.apply_sampling_takeover(b2, "t")
        check("mtime 热读即时生效", sorted(ch2) == sorted(
            ["temperature", "top_p", "top_k", "seed", "max_tokens",
             "max_completion_tokens"]), str(ch2))
        check("GUI 值植入", b2.get("temperature") == 1.0
              and b2.get("top_p") == 0.95 and b2.get("top_k") == 20, str(b2))
        check("空字段=只剥不植", "seed" not in b2 and "max_tokens" not in b2
              and "max_completion_tokens" not in b2 and mx2 is False, str(b2))
        f.write_text(json.dumps({"takeover": True, "temperature": 9,
                                 "top_k": 200}), encoding="utf-8")
        b3 = dict(body)
        proxy.apply_sampling_takeover(b3, "t")
        check("越界值当没设（引擎校验器口径）",
              "temperature" not in b3 and "top_k" not in b3, str(b3))
        f.write_text(json.dumps({"takeover": True, "max_tokens": 4096}),
                     encoding="utf-8")
        b4 = {"messages": [{"role": "user", "content": "hi"}]}
        _, mx4 = proxy.apply_sampling_takeover(b4, "t")
        proxy._apply_max_think(b4, "t", 100000, raise_max=not mx4)
        check("GUI 显式 max_tokens 不被 max_think 抬",
              b4.get("max_tokens") == 4096
              and b4.get("reasoning_budget_tokens") == 100000, str(b4))
        old_up = proxy.UPSTREAM
        proxy.UPSTREAM = "https://api.deepseek.com/v1"
        b5 = {"messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.5}
        ch5, _ = proxy.apply_sampling_takeover(b5, "t")
        check("云端请求不碰", ch5 == [] and b5["temperature"] == 0.5, str(ch5))
        proxy.UPSTREAM = old_up
    finally:
        proxy.ROUTING_FILE = old_file
        proxy._ROUTING_STATE["stamp"] = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_send_policy_333():
    """3.33 发送策略：wait=排队等在途完成；interrupt=给在途发取消信号。
    生产里在途方由 _forward 的 cancel 检查退出并 release；测试用线程模拟。"""
    import threading
    import time as _t
    old_file = proxy.ROUTING_FILE
    proxy.ROUTING_FILE = ""
    try:
        check("无配置文件=透传策略",
              proxy.routing_send_policy(
                  {"messages": [{"role": "user", "content": "hi"}]}) is None)
        a = proxy._policy_acquire("ckA", "wait", "t")
        started = _t.monotonic()

        def rel():
            _t.sleep(0.2)
            proxy._policy_release("ckA", a)

        threading.Thread(target=rel, daemon=True).start()
        b = proxy._policy_acquire("ckA", "wait", "t")
        waited = _t.monotonic() - started
        check("wait 策略排队到在途完成", 0.15 <= waited < 5, f"{waited:.2f}s")
        proxy._policy_release("ckA", b)
        c = proxy._policy_acquire("ckB", "interrupt", "t")

        def rel2():
            _t.sleep(0.05)
            proxy._policy_release("ckB", c)

        threading.Thread(target=rel2, daemon=True).start()
        d = proxy._policy_acquire("ckB", "interrupt", "t")
        check("interrupt 策略给在途发取消信号", c["cancel"].is_set(), "未置位")
        proxy._policy_release("ckB", d)
        check("注销后该会话位为空", "ckB" not in proxy._INFLIGHT,
              str(list(proxy._INFLIGHT)))
    finally:
        proxy.ROUTING_FILE = old_file


def test_strip_thinking_333():
    """3.33 隐藏思考：SSE 行级剥离的判据——思考事件丢、正文/usage/非 data 行过。"""
    check("reasoning_content 事件被剥",
          proxy._strip_thinking_line(
              b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x80\x9d"}}]}'))
    check("anthropic thinking_delta 被剥",
          proxy._strip_thinking_line(
              b'data: {"type":"content_block_delta","thinking_delta":{"text":"x"}}'))
    check("正文事件放行",
          not proxy._strip_thinking_line(
              b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}'))
    check("非 data 行放行", not proxy._strip_thinking_line(b"event: ping\n"))
    check("usage 尾事件放行",
          not proxy._strip_thinking_line(
              b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}'))


def test_routing_archive_333():
    """3.33 对话存档：chat_archive 开 → 每轮追加 最后用户消息+回答 到 jsonl。"""
    import json
    import tempfile
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="arc333_"))
    f = tmp / "routing.json"
    old = proxy.ROUTING_FILE
    proxy.ROUTING_FILE = str(f)
    proxy._ROUTING_STATE["stamp"] = None
    try:
        f.write_text(json.dumps({"chat_archive": True}), encoding="utf-8")
        proxy.routing_archive("abcdef1234567890", [
            {"role": "user", "content": "问题一"},
            {"role": "assistant", "content": "答"}], "回答一")
        files = list((tmp / "chat_archive").glob("*.jsonl"))
        check("存档文件生成", len(files) == 1, str(files))
        if files:
            row = json.loads(files[0].read_text(
                encoding="utf-8").splitlines()[-1])
            check("存档=末条用户消息+回答",
                  row["user"] == "问题一" and row["answer"] == "回答一",
                  str(row))
    finally:
        proxy.ROUTING_FILE = old
        proxy._ROUTING_STATE["stamp"] = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_gui_routing_panel_333():
    """3.33 GUI 路由接管面板：文件函数、面板控件、build_env 传路径、保存接线。"""
    mod, p = _gui_main_mod()
    check("GUI 模块加载", mod is not None, str(p))
    if mod is not None:
        check("routing_path/save_routing 存在",
              callable(getattr(mod, "routing_path", None))
              and callable(getattr(mod, "save_routing", None)))
    msrc = p.read_text(encoding="utf-8")
    check("路由接管面板在 GUI", "路由接管（8080 面板" in msrc)
    check("build_env 传 PROXY_ROUTING_FILE", '"PROXY_ROUTING_FILE"' in msrc)
    check("保存按钮接线 _routing_save", "command=self._routing_save" in msrc)
    check("发送策略三档控件", '"等待"' in msrc and '"打断"' in msrc
          and '"跟随"' in msrc)


def test_cache_watch_334():
    """3.34 命中读数：字节级旁观解析、0 与缺测严格区分、开关、旧签名不动。"""
    import json as _j
    blob = _j.dumps({"usage": {
        "prompt_tokens": 150000, "completion_tokens": 300,
        "prompt_tokens_details": {"cached_tokens": 149000},
        "cache_creation_input_tokens": 800}}).encode()
    hit, made, tail = proxy.scan_cache_chunk(b"", blob)
    check("命中 token 抓到", hit == 149000, str(hit))
    check("创建 token 抓到", made == 800, str(made))
    check("尾窗封顶 96 字节", isinstance(tail, bytes) and len(tail) <= 96)

    # 命中 0 是数据不是缺测（云端不支持隐式缓存时恒 0，正是要看的东西）
    zero = (b'{"usage": {"prompt_tokens": 80100, '
            b'"prompt_tokens_details": {"cached_tokens": 0}}}')
    h0, m0, _ = proxy.scan_cache_chunk(b"", zero)
    check("命中 0 报 0（不是 None）", h0 == 0 and m0 is None, f"{h0}/{m0}")

    # 流式：字段名被 chunk 边界切断，靠尾窗接上
    full = b'{"usage": {"prompt_tokens_details": {"cached_tokens": 12345}}}'
    cut = full.find(b"cached_tok") + 6
    _, _, t1 = proxy.scan_cache_chunk(b"", full[:cut])
    h2, _, _ = proxy.scan_cache_chunk(t1, full[cut:])
    check("跨 chunk 切断仍解析", h2 == 12345, str(h2))

    # 多次更新取最后一次（中间块可能先报残值，与 scan_usage_chunk 同语义）
    h3, _, _ = proxy.scan_cache_chunk(
        b"", b'{"cached_tokens": 12} xx {"cached_tokens": 1234}')
    check("取最后一次更新", h3 == 1234, str(h3))

    old = proxy.CACHE_WATCH
    proxy.CACHE_WATCH = False
    try:
        check("PROXY_CACHE_WATCH=0 全关（退回 3.33）",
              proxy.scan_cache_chunk(b"", blob) == (None, None, b""))
    finally:
        proxy.CACHE_WATCH = old

    found, t4 = proxy.scan_usage_chunk(b"", blob)
    check("scan_usage_chunk 仍是二元组（签名没被动）",
          found == 150000 and isinstance(t4, bytes) and len(t4) <= 64,
          str((found, t4)))


def test_cache_record_334():
    """3.34 会话实况：命中挂账、命中率用实测分母、record_sent 继承、缺测不覆盖。"""
    key = "cache-334"
    # record_sent/record_usage 会落盘 proxy-stats.json：必须改道到临时目录，
    # 否则单测的假会话（cache-334）会写进线上那份，GUI 实况表显示一条假行。
    import shutil
    import tempfile
    old_stats = proxy.STATS_PATH
    d = Path(tempfile.mkdtemp(prefix="cache334-"))
    proxy.STATS_PATH = d / "proxy-stats.json"
    proxy._CONV_ACTUAL[key] = {"key": key, "body": 1, "tools": 0, "total": 1,
                               "t": 0.0}
    try:
        proxy.record_usage(key, 150000, 149000, 800)
        c = proxy._CONV_ACTUAL[key]
        check("命中挂上实况", c.get("cache_hit") == 149000, str(c.get("cache_hit")))
        check("命中率用实测分母（不是估算）",
              abs(float(c.get("cache_rate")) - 149000 / 150000) < 1e-4,
              str(c.get("cache_rate")))
        check("创建量挂上实况", c.get("cache_made") == 800, str(c.get("cache_made")))

        proxy.record_sent(key, 2000, 500, 2500, 9000)
        c = proxy._CONV_ACTUAL[key]
        check("发出瞬间继承命中读数（GUI 不闪 —）",
              c.get("cache_hit") == 149000 and c.get("cache_rate") is not None,
              str(c.get("cache_hit")))

        proxy.record_usage(key, 80100, 0, None)
        c = proxy._CONV_ACTUAL[key]
        check("命中 0 落账为 0（冷启动那轮长这样）",
              c.get("cache_hit") == 0 and float(c.get("cache_rate")) == 0.0,
              str(c.get("cache_hit")))

        proxy.record_usage(key, None, None, None)
        check("上游没报就不动旧值",
              proxy._CONV_ACTUAL[key].get("cache_hit") == 0
              and proxy._CONV_ACTUAL[key].get("usage_prompt") == 80100)
        proxy.record_usage("no-such-conv-334", 100, 1, 1)
        check("没登记的会话不炸（record_usage 早退）", True)

        # kpi 落行读的是 cache_turn_set 那份「本轮实测」：不继承，缺测写 None。
        # 抄上一轮的 149000 等于伪造一次观测，观测一天的命中率会被高估，
        # 「要不要做改 1」的判据也就跟着错了。
        proxy.cache_turn_set(key, 150000, 149000, 800)
        row = proxy._CACHE_TURN[key]
        check("本轮命中落账（kpi 的 ch/cw）",
              row["hit"] == 149000 and row["made"] == 800, str(row))
        check("本轮命中率用实测分母（kpi 的 hr）",
              abs(float(row["rate"]) - 149000 / 150000) < 1e-4, str(row["rate"]))
        proxy.cache_turn_set(key, 150000, None, None)
        row = proxy._CACHE_TURN[key]
        check("上游没报：kpi 那份写 None（不抄上一轮）",
              row["hit"] is None and row["rate"] is None and row["made"] is None,
              str(row))
        check("同一轮 GUI 那份仍继承旧值（两列各有各的语义）",
              proxy._CONV_ACTUAL[key].get("cache_hit") == 0,
              str(proxy._CONV_ACTUAL[key].get("cache_hit")))
        proxy.cache_turn_set(key, 0, 5, None)
        check("没分母时命中率写 None（不炸 ZeroDivision）",
              proxy._CACHE_TURN[key]["rate"] is None
              and proxy._CACHE_TURN[key]["hit"] == 5)
        proxy.cache_turn_set(key, 90000, 0, 0)
        check("命中 0 照写 0（0 是数据，None 才是没测到）",
              proxy._CACHE_TURN[key]["hit"] == 0
              and float(proxy._CACHE_TURN[key]["rate"]) == 0.0)
    finally:
        proxy._CONV_ACTUAL.pop(key, None)
        proxy._CACHE_TURN.pop(key, None)
        proxy.STATS_PATH = old_stats
        shutil.rmtree(d, ignore_errors=True)


def test_tools_fp_334():
    """3.34 工具指纹：键序无关、内容敏感、无工具为空、变化被喊出来。"""
    a = [{"type": "function", "function": {"name": "read", "parameters": {"x": 1}}}]
    b = [{"function": {"parameters": {"x": 1}, "name": "read"}, "type": "function"}]
    c = [{"type": "function", "function": {"name": "read", "parameters": {"x": 2}}}]
    check("键序无关（同定义同指纹）",
          proxy.tools_fingerprint(a) == proxy.tools_fingerprint(b))
    check("内容变了指纹就变",
          proxy.tools_fingerprint(a) != proxy.tools_fingerprint(c))
    check("指纹 8 位十六进制", len(proxy.tools_fingerprint(a)) == 8)
    check("无工具返回空串（不参与比较）", proxy.tools_fingerprint([]) == "")

    conv = "fp-conv-334"
    # tools_fp_watch 会 log()：改道临时文件，别把假的「指纹变了」喊进线上日志
    # （观测一天时那种行会被当成真实事故去追）。
    import shutil
    import tempfile
    old_log = proxy.LOG_PATH
    d = Path(tempfile.mkdtemp(prefix="fp334-"))
    proxy.LOG_PATH = d / "proxy.log"
    proxy._TOOLS_FP.pop(conv, None)
    try:
        check("首轮不喊（没有对照）",
              proxy.tools_fp_watch(conv, proxy.tools_fingerprint(a), "t") == "")
        check("第二轮同指纹不喊",
              proxy.tools_fp_watch(conv, proxy.tools_fingerprint(b), "t") == "")
        ch = proxy.tools_fp_watch(conv, proxy.tools_fingerprint(c), "t")
        check("指纹变了喊出来（缓存静默失效的头号成因）", "->" in ch, ch)
        check("喊完记住新指纹", proxy._TOOLS_FP[conv] == proxy.tools_fingerprint(c))
        check("空指纹不登记", proxy.tools_fp_watch("fp-x-334", "", "t") == ""
              and "fp-x-334" not in proxy._TOOLS_FP)
    finally:
        proxy._TOOLS_FP.pop(conv, None)
        proxy._TOOLS_FP.pop("fp-x-334", None)
        proxy.LOG_PATH = old_log
        shutil.rmtree(d, ignore_errors=True)


def test_probe_boundary_335():
    """3.35 故障修：探针是测量标记不是任务——独立类别、不进约束分母、块内明说非任务。

    事故（2026-11-11 09:19 Cherry Studio agent）：探针节长得像约束节，小模型
    回「探针 CAN-024 已收到」当最终回答，agent 循环见不到 tool_call 即收工。
    """
    block = "\n".join([
        "[longctx:checkpoint v3]",
        "## 目标", "- 查上海高中直录比例",
        "## 硬约束（逐字，不得改写）", "- 必须保留端口 9001",
        "## 探针（记忆测量标记，非任务指令：不必回应或确认，"
        "不要因它停止当前工作）",
        "- CAN-024",
        "[longctx:checkpoint end]",
    ])
    typed = proxy.parse_anchors_typed(block)
    d = dict(typed)
    check("探针类别=probe", d.get("CAN-024") == "probe", str(typed))
    check("真约束仍是 rule", d.get("必须保留端口 9001") == "rule", str(typed))
    rules = [t for t, k in typed if k == "rule"]
    check("探针不进回声分母（测量归测量）", "CAN-024" not in rules, str(rules))
    texts = [t for t, _ in typed]
    check("探针仍进回声测量（canary 计分不断）", "CAN-024" in texts, str(texts))
    check("探针节不在约束节清单里",
          proxy.PROBE_SECTION not in proxy.RULE_SECTIONS)
    blk = proxy.canary_block("## 硬约束\n- 真约束\n[longctx:checkpoint end]",
                             "pb-335")
    check("块内明说非任务、不必回应",
          "非任务指令" in blk and "不要因它停止当前工作" in blk, blk)
    for dd in (proxy._CANARY_SEQ, proxy._CANARY_STATE, proxy._CANARY_STATS):
        dd.pop("pb-335", None)


def test_drop_evidence_335():
    """3.35 故障修：只丢不摘丢工具结果前，事实句折进检查点——证据不随删除消失。"""
    import shutil
    import tempfile
    old_sess, old_log = proxy.SESS_DIR, proxy.LOG_PATH
    d = Path(tempfile.mkdtemp(prefix="ev335-"))
    proxy.SESS_DIR = d
    proxy.LOG_PATH = d / "proxy.log"
    conv = "ev-335"
    try:
        st = proxy.checkpoint.new_state(conv)
        st["goal"] = "查上海高中直录比例"
        pth = d / conv / "checkpoint.json"
        pth.parent.mkdir(parents=True, exist_ok=True)
        proxy.checkpoint.save(pth, st)
        tool_txt = ("搜索摘要：直录比例阈值 15% 自 2026 年生效。"
                    "名额编号 REV-4471 对应浦东片区。")
        n = proxy._fold_dropped_evidence(conv, [tool_txt], "t")
        st2 = proxy.checkpoint.load(pth)
        got = [f["t"] for f in st2.get("facts") or [] if f.get("src") == "tool"]
        check("工具证据折入关键事实", n >= 1 and bool(got), f"n={n} got={got}")
        n2 = proxy._fold_dropped_evidence(conv, [tool_txt], "t")
        check("重复折入去重（检查点不无限长）", n2 == 0, str(n2))
        check("折入喊进日志",
              "工具证据" in (d / "proxy.log").read_text(encoding="utf-8"))
    finally:
        proxy.SESS_DIR, proxy.LOG_PATH = old_sess, old_log
        shutil.rmtree(d, ignore_errors=True)


def test_drop_fold_wiring_335():
    """3.35 故障修接线：真走 transform 的只丢不摘路径，工具结果被丢但证据进检查点。"""
    import shutil
    import tempfile
    old = (proxy.SESS_DIR, proxy.LOG_PATH, proxy.THRESHOLD, proxy.DROP_ONLY)
    d = Path(tempfile.mkdtemp(prefix="evw335-"))
    proxy.SESS_DIR, proxy.LOG_PATH = d, d / "proxy.log"
    proxy.THRESHOLD, proxy.DROP_ONLY = 500, True
    try:
        big = "搜索摘要：直录比例阈值 15% 自 2026 年生效。" * 400
        body = {"model": "m", "messages": [
            {"role": "system", "content": "你是助手。"},
            {"role": "user", "content": "旧问题：直录比例是多少？"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "t1", "type": "function",
                             "function": {"name": "web_search",
                                          "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": big},
            {"role": "user", "content": "现在继续：浦东片区的名额编号是多少？"},
        ]}
        proxy.transform(body, "t")
        roles = [m.get("role") for m in body["messages"]]
        check("超预算时工具结果被整条丢弃", "tool" not in roles, str(roles))
        check("最后一条 user 保住", roles[-1] == "user", str(roles))
        cks = list(d.glob("*/checkpoint.json"))
        facts = []
        if cks:
            st = proxy.checkpoint.load(cks[0])
            facts = [f["t"] for f in st.get("facts") or []
                     if f.get("src") == "tool"]
        check("被丢工具结果的证据折进检查点", bool(facts), str(facts)[:200])
        check("日志喊出折入证据",
              "工具证据" in (d / "proxy.log").read_text(encoding="utf-8"))
    finally:
        (proxy.SESS_DIR, proxy.LOG_PATH, proxy.THRESHOLD,
         proxy.DROP_ONLY) = old
        shutil.rmtree(d, ignore_errors=True)


def test_snap_restore_336():
    """3.36 缓存回滚：每轮快照、选时刻恢复、一次性、指向别会话不动。"""
    import json
    import os
    import shutil
    import tempfile
    import time
    old = (proxy.SESS_DIR, proxy.LOG_PATH, proxy.PREFIX_DIR)
    d = Path(tempfile.mkdtemp(prefix="rb336-"))
    proxy.SESS_DIR, proxy.LOG_PATH = d, d / "proxy.log"
    proxy.PREFIX_DIR = d / "prefix"
    conv = "rb-336"
    try:
        ck = d / conv / "checkpoint.json"
        ck.parent.mkdir(parents=True, exist_ok=True)
        ck.write_text(json.dumps({"goal": "9:30 的干净目标"},
                                 ensure_ascii=False), encoding="utf-8")
        proxy.snap_take(conv)
        ck.write_text(json.dumps({"goal": "被污染的坏目标"},
                                 ensure_ascii=False), encoding="utf-8")
        pp = proxy._prefix_path(conv)
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text(json.dumps({"keys": ["k"], "xform": {}, "cfg": {}}),
                      encoding="utf-8")
        proxy._prefix_mem[conv] = {"keys": ["k"], "xform": {}, "cfg": {}}
        times = proxy.snap_list(conv)
        check("快照落盘且可列", len(times) == 1 and bool(times[0]["t"]),
              str(times))
        got = proxy.snap_restore(conv, "23:59:59")
        check("恢复到当天最近快照", got == times[0]["t"], str(got))
        st = json.loads(ck.read_text(encoding="utf-8"))
        check("检查点换回干净那份", "9:30" in str(st.get("goal")), str(st))
        check("存储前缀作废（内存态清掉）", conv not in proxy._prefix_mem)
        check("当天没有更早快照返回空",
              proxy.snap_restore(conv, "00:00:00") == "")
        cmd = proxy.cache_cmd_path()
        now = time.time()
        cmd.write_text(json.dumps({"conv": conv, "as_of": "23:59:59",
                                   "status": "pending"}, ensure_ascii=False),
                       encoding="utf-8")
        # mtime 缓存按时间tick判重：写入钉成互不相同的 mtime，防同tick读旧内容
        os.utime(cmd, (now + 2, now + 2))
        proxy.cache_cmd_apply(conv, "t")
        done = json.loads(cmd.read_text(encoding="utf-8"))
        check("命令消费成 done 并记恢复时刻",
              done.get("status") == "done" and bool(done.get("restored")),
              str(done))
        proxy.cache_cmd_apply(conv, "t")
        check("一次性：下一轮不再恢复",
              json.loads(cmd.read_text(encoding="utf-8")).get("status")
              == "done")
        cmd.write_text(json.dumps({"conv": "other-conv", "as_of": "23:59:59",
                                   "status": "pending"}, ensure_ascii=False),
                       encoding="utf-8")
        os.utime(cmd, (now + 4, now + 4))
        proxy.cache_cmd_apply(conv, "t")
        check("指向别的会话不动本会话",
              json.loads(cmd.read_text(encoding="utf-8")).get("status")
              == "pending")
        check("stats 带最近命令结果（GUI 状态字读它）",
              "cache_cmd" in proxy._stats_snapshot())
    finally:
        proxy.SESS_DIR, proxy.LOG_PATH, proxy.PREFIX_DIR = old
        proxy._prefix_mem.pop(conv, None)
        shutil.rmtree(d, ignore_errors=True)


def test_gui_cache_button_336():
    """3.36 GUI：回滚行控件 + 命令文件写法 + 状态字走 i18n。"""
    mod, p = _gui_main_mod()
    check("GUI 模块加载", mod is not None, str(p))
    msrc = p.read_text(encoding="utf-8")
    check("回滚行控件在实况表下", all(x in msrc for x in
          ("self.cb_rb_time", "self.btn_rb", "self.rb_status",
           "_on_cache_restore")))
    check("按钮写命令文件（文件控制面，不开 HTTP 口）",
          "cache_cmd.json" in msrc and '"status": "pending"' in msrc)
    check("快照时刻只列当天", 's.get("day") == day' in msrc)
    check("状态字走 i18n", "已下发，下一轮对话生效" in
          (p.parent / "i18n.py").read_text(encoding="utf-8"))
    check("按钮旁备注一次性语义", "只对下一次对话生效" in msrc
          and "后续对话的缓存不会回到选定的时间点" in msrc)
    check("时刻可手填（下拉非只读）",
          'state="readonly"' not in msrc.split("self.cb_rb_time")[1][:200])
    check("时刻规整函数在", "def rb_norm_time" in msrc)
    check("HH:MM 规整成 HH:MM:SS", mod.rb_norm_time("9:30") == "09:30:00")
    check("非法时刻返回空",
          mod.rb_norm_time("abc") == "" and mod.rb_norm_time("25:00") == "")
    check("备注走 i18n", "只对下一次对话生效" in
          (p.parent / "i18n.py").read_text(encoding="utf-8"))
    check("APP_VERSION=5.0.9", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_ratio_clamp_510():
    """5.1.0：动态压缩比格子限幅 0.50~0.75，区间塌缩时提示说实话。

    背景：填 0.90 时 proxy.py 的 DENSITY_MIN(0.80) > DENSITY_MAX(0.65) 触发
    防反置塌缩成固定 0.90——自适应失效、压缩几乎不压缩，而旧提示还显示
    反着的「实际区间 0.80~0.65」。有牙核验：改前备份 spinbox 仍 0.05~0.9。
    """
    mod, p = _gui_main_mod()
    check("GUI 模块加载", mod is not None, str(p))
    msrc = p.read_text(encoding="utf-8")
    check("限幅常量 0.50/0.75",
          getattr(mod, "RATIO_MIN", 0) == 0.50
          and getattr(mod, "RATIO_MAX", 0) == 0.75)
    check("格子边界用限幅常量", "from_=RATIO_MIN, to=RATIO_MAX" in msrc)
    check("旧 0.05~0.9 边界不再残留",
          "from_=0.05" not in msrc and "min(0.9," not in msrc)
    lo, hi = mod.ratio_band(0.50)
    check("0.50 区间 = 0.40~0.60（与默认口径一致）",
          (lo, hi) == (0.40, 0.60), f"{lo}~{hi}")
    lo, hi = mod.ratio_band(0.75)
    check("0.75 区间塌缩（lo==hi=0.65）", lo == hi == 0.65, f"{lo}~{hi}")
    lo, hi = mod.ratio_band(0.90)
    check("越界 0.90 判为反置（lo>hi）", lo > hi, f"{lo}~{hi}")
    check("提示在塌缩时说实话", "区间塌缩为固定值" in msrc)
    check("build_env/读取/建议三处夹子都用限幅常量",
          msrc.count("max(RATIO_MIN, min(RATIO_MAX,") >= 3)
    check("APP_VERSION=5.1.0", 'APP_VERSION = "5.1.0"' in msrc, "missing")


def test_cache_no_outbound_334():
    """3.34 命门判据：本版只读不写——源码里不存在打标动作。

    断点（cache_control）是下一版的事。如果读数版偷偷往 body 里塞了标记，
    前缀字节就变了：命中率必为 0，还白花写入溢价。所以把「代码里没有
    cache_control」钉成判据——它同时挡住误加和复制粘贴带进来。
    """
    psrc = (HERE / "proxy" / "proxy.py").read_text(encoding="utf-8")
    check("proxy.py 里没有 cache_control（本版不打标）",
          "cache_control" not in psrc)
    check("出站 body 的构造路径没动（无 apply_cache_marks）",
          "apply_cache_marks" not in psrc)
    check("读数三件套都在", all(x in psrc for x in
                                ("_CACHED_RE", "_CACHE_CREATE_RE",
                                 "scan_cache_chunk", "tools_fingerprint",
                                 "cache_turn_set")))
    check("kpi 的 ch/cw/hr 读本轮实测那份（不是继承的实况值）",
          '"ch": ct.get("hit")' in psrc and '"hr": ct.get("rate")' in psrc
          and '"cw": ct.get("made")' in psrc)
    check("PROXY_VERSION=3.34（代理本次动了）",
          'PROXY_VERSION = "3.36"' in psrc, "missing")


def test_gui_cache_columns_334():
    """3.34 GUI：历史实况表两列（命中 tok／命中率）+ 0 与 — 的区分 + 帮助文字。"""
    mod, p = _gui_main_mod()
    check("GUI 模块加载", mod is not None, str(p))
    msrc = p.read_text(encoding="utf-8")
    check("实况表列元组含 hit/rate", '"usage", "hit", "rate", "anchors"' in msrc)
    check("状态条挂缓存命中", "\u7f13\u5b58\u547d\u4e2d {hit}" in msrc)
    check("帮助文字讲清与绿线不是一回事",
          "\u547d\u4e2d tok\uff0f\u547d\u4e2d\u7387" in msrc
          and "\u4e0d\u662f\u4e00\u56de\u4e8b" in msrc)
    check("APP_VERSION=5.0.8", 'APP_VERSION = "5.1.0"' in msrc, "missing")
    if mod is None:
        return
    base = {"key": "abcdef1234567890", "body": 1000, "tools": 10, "total": 1010,
            "raw_total": 5000, "usage_prompt": 150000, "cache_hit": 149000,
            "cache_rate": 0.9933, "ts": "12:00:00"}
    rows = mod.ctx_rows({"conversations": [base]})
    check("ctx_rows 出行", len(rows) == 1, str(rows))
    if rows:
        check("命中 tok 进表（第 7 列）", rows[0][6] == "149,000", str(rows[0][6]))
        check("命中率进表（第 8 列，一位小数）", rows[0][7] == "99.3%",
              str(rows[0][7]))
    rows0 = mod.ctx_rows({"conversations": [dict(base, cache_hit=0,
                                                 cache_rate=0.0)]})
    check("命中 0 画 0 不画 —", rows0 and rows0[0][6] == "0"
          and rows0[0][7] == "0.0%", str(rows0[0][6:8]) if rows0 else "")
    rowsN = mod.ctx_rows({"conversations": [dict(base, cache_hit=None,
                                                 cache_rate=None)]})
    check("上游没报画 —", rowsN and rowsN[0][6] == "\u2014"
          and rowsN[0][7] == "\u2014", str(rowsN[0][6:8]) if rowsN else "")
    txt = mod.ctx_live_text({"actual": base})
    check("状态条显示命中率", "149,000" in txt and "99.3%" in txt, txt)
    txtN = mod.ctx_live_text({"actual": dict(base, cache_hit=None,
                                             cache_rate=None)})
    check("没报就不画（不装 0）", "\u7f13\u5b58\u547d\u4e2d" not in txtN, txtN)


if __name__ == "__main__":
    print("v2.0 单元测试")
    for fn in (test_extract_facts, test_backfill_full, test_backfill_budget,
               test_density_cap, test_dedupe_lines, test_fold_text,
               test_trim_keeps_facts, test_overlap, test_slice_block_extractive,
               test_query_alloc, test_structured_detect, test_fact_window,
               test_window_no_midword_cut, test_repair_cut_tokens,
               test_fact_coverage_metric, test_repeat_dedupe,
               test_toolfmt_pytest, test_toolfmt_passthrough, test_toolfmt_json,
               test_toolfmt_logs, test_prefix_replay, test_transform_prefix_replay,
               test_prefix_emergency_unfreeze, test_truncate_tools_uses_toolfmt,
               test_prefix_freeze_dedupe, test_evidence_identifiers,
               test_evidence_constraints, test_evidence_covered,
               test_evidence_budget, test_query_alloc_no_starve,
               test_digest_cap_evidence_density, test_tool_path_prefix,
               test_checkpoint_state, test_checkpoint_render_budget,
               test_checkpoint_inject, test_checkpoint_inject_agent_tail,
               test_checkpoint_inject_no_position_drift,
               test_checkpoint_reset,
               test_checkpoint_kb_sources, test_checkpoint_noise_filter, test_checkpoint_role_scoped, test_checkpoint_cursor_replace,
               test_checkpoint_source_fallback, test_anchor_noise_filter,
        test_anchor_noise_filter_off, test_anchor_echo_after_filter,
        test_harness_goal_sane, test_ck_skill_doc_filter,
        test_ck_block_excision, test_ck_doc_filter_off,
        test_anchor_remind_ttl, test_anchor_remind_ttl_off,
        test_reseed_goal_on_reset, test_calib_factor,
        test_calib_gate_effect, test_judge_every,
        test_canary_probe, test_throttle_backoff,
        test_digest_coverage_retry, test_provenance_turn,
        test_corpus_purge, test_kpi_write,
        test_parse_anchors_typed, test_data_evict_aging,
        test_drop_data_keeps_rules, test_reset_uses_passed_k,
        test_reset_counts_tool_blocks, test_record_sent_keeps_usage,
        test_model_catalog_routing, test_upstream_retry_gate,
        test_reset_watchdog, test_reset_noop_keeps_prefix,
        test_clamp_calibrated_thinking,
        test_hyg_persist_roundtrip, test_atomic_write,
        test_drop_only_policy,
        test_cold_probe_register, test_cold_probe_reanchor_lost,
        test_cold_probe_off, test_cold_qmark_question,
        test_cold_qmark_inject_score, test_cold_qmark_off_and_wiring,
        test_cold_qmark_gate, test_cold_deep_record,
        test_kpi_time_axis,
        test_kpi_line_styles,
        test_kpi_entry_layout,
        test_kpi_scale_p95,
        test_kpi_popup_edge,
        test_baseline_degenerate,
        test_baseline_bg_collect,
        test_kpi_range_and_model,
        test_baseline_update_model,
        test_kpi_export_md,
        test_task_mode_monitor,
        test_task_mode_record,
        test_task_mode_text_visible,
        test_kpi_model_baseline,
        test_kpi_green_selfview, test_baseline_pick_on_switch,
        test_kpi_axis_cleanup, test_kpi_clear_and_gate, test_kpi_axis_draw_324,
        test_kpi_chart_snapshots_325, test_kpi_buttons_visible_326,
        test_old_bucket_rename_327,
        test_auto_work_window,
        test_slot_overwrite_rule, test_slot_overwrite_decision,
        test_slot_overwrite_off,
        test_goal_watch_topic_swap, test_goal_watch_rescan_idempotent,
        test_goal_watch_off,
               test_transform_checkpoint_inject,
               test_thinking_override, test_gui_thinking_control,
               test_rule_hedge_quoted_332, test_correction_sync_332,
               test_drop_fold_corrections_332, test_kpi_drp_gate_332,
               test_routing_takeover_333, test_send_policy_333,
               test_strip_thinking_333, test_routing_archive_333,
               test_gui_routing_panel_333,
               test_cache_watch_334, test_cache_record_334,
               test_tools_fp_334, test_cache_no_outbound_334,
               test_gui_cache_columns_334,
               test_probe_boundary_335, test_drop_evidence_335,
               test_drop_fold_wiring_335,
               test_snap_restore_336, test_gui_cache_button_336,
               test_ratio_clamp_510):
        print(f"\n[{fn.__name__}]")
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL  exception: {type(exc).__name__}: {exc}")
    print(f"\n结果：{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
