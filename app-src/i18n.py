# -*- coding: utf-8 -*-
"""界面语言：中文 ↔ English 切换。

设计要点
  * 中文是源语言，代码里写的都是中文串；t("中文") 在 zh 模式下原样返回。
    所以不切换时行为与之前完全一致，测试断言不受影响。
  * 切换靠遍历控件树登记「静态文本」，不逐个改构造点 —— 改动面小，
    不会漏掉后加的控件（下次加按钮自动被登记）。
  * 动态文本（状态栏、价目表标题、缓存模式下拉）由调用处过一遍 t()。
  * 计算结果报告与运行日志不在本表范围内：那是中文数字混排的 f-string，
    翻译要重构 pricing.py 的报告生成，风险远大于收益。
"""

from __future__ import annotations

ZH = "zh"
EN = "en"

# 中文原文 -> 英文。中文串是唯一 key，因此写错的 key 只会退化成中文，不会崩。
TR: dict[str, str] = {
    # ---- 四个区域 / 两张列表
    "压缩模型": "Compressor",
    "任务模型": "Task model",
    "成本建议": "Cost advice",
    "综合参数计算": "Parameter calculator",

    # ---- 任务模式（下拉框与说明）
    "任务模式": "Task mode",
    "自动": "Auto",
    "表格": "Table",
    "法律或工程规范": "Legal / standards",
    "关闭": "Off",
    "日常对话 / 编码 / 混合内容：按结构占比判断，代码块与表格占比高才整块保留":
        "Chat / coding / mixed: keep code and table blocks verbatim when they dominate.",
    "财报、对账、报表、数据分析：连续 3 行以上的表格整块原样保留":
        "Financial reports, reconciliation, data analysis: keep tables of 3+ rows verbatim.",
    "合同、条款、合规、制度文件，以及 GB/API/ASME 等工程规范："
    "4 条以上编号条款整块原样保留，不改措辞":
        "Contracts, clauses, compliance and engineering standards (GB/API/ASME): "
        "keep 4+ numbered clause lines verbatim, no rewording.",
    "短对话 / 纯聊天：不做结构直通，全部按普通文本压缩":
        "Short chats: no structure passthrough; compress everything as plain text.",
    "任务模式切换为「{name}」——已停止代理，请重新点击「启动代理」使新档生效。":
        "Task mode switched to \"{name}\" — proxy stopped. "
        "Click Start again to apply the new mode.",

    # ---- 老内容重压开关
    "更早的聊天记录太占地方时，再压一次":
        "Compact earlier chat history when it gets too large",
    "涨到": "at",
    "% 时": "%",
    ("勾上：更早的聊天记录涨到总长度的 {pct}% 时，把它们再压一遍，压到 {tgt}% 左右。\n"
     "压完会停顿几秒，之后又能接着聊很久。"):
        ("On: once earlier chat history reaches {pct}% of the total length, "
         "compact it again down to about {tgt}%.\n"
         "You'll wait a few seconds, then you can keep chatting for a long while."),
    ("不勾：聊天记录只增不减。超过模型能装下的长度后，请求会直接失败。\n"
     "除非你有别的办法控制长度，否则别关。"):
        ("Off: chat history only grows. Once it passes what the model can hold, "
         "requests fail outright.\n"
         "Unless you have another way to keep it short, leave this on."),

    # ---- 懒压缩开关
    "平时不压缩，攒够了再压":
        "Don't compress every turn — wait until it builds up",
    ("勾上：平时一个字都不改，聊天内容原样发给模型。\n"
     "等聊天记录涨到总长度的 {pct}% 时，才整体压一次，压到 {tgt}% 左右，然后继续原样发。\n"
     "好处是前面的细节不会被提前总结掉；代价是压缩那一下会停顿几秒。\n"
     "注意：上面那个开关也要勾上。只勾这个的话，到了 {pct}% 之后每轮都会重复压，"
     "钱花了体积也不降。"):
        ("On: nothing is rewritten — the conversation goes to the model as-is.\n"
         "Only when it reaches {pct}% of the total length is it compacted once, down to "
         "about {tgt}%, then it goes back to as-is.\n"
         "Upside: earlier detail isn't summarized away. Cost: that one turn "
         "pauses for a few seconds.\n"
         "Also turn on the switch above. With only this one on, every turn past "
         "{pct}% re-compacts again — you pay and the size never drops."),
    ("不勾（默认）：每轮都把老对话压到 1.4 万 token 左右。\n"
     "上下文小、出字快；但老内容很快被总结掉，细节会丢。"):
        ("Off (default): every turn compresses old chat down to about 14k tokens.\n"
         "Small context, fast first token; but old content gets summarized early "
         "and detail is lost."),
    "模型上下文": "Model context",
    "≈ {n:,} token（任务模型窗口 {ctx:,}）":
        "≈ {n:,} tokens (cloud task model window {ctx:,})",
    "≈ {n:,} token（窗口 {ctx:,}）": "≈ {n:,} tokens (window {ctx:,})",
    "≈ {n:,} token（本地引擎窗口 {ctx:,}）":
        "≈ {n:,} tokens (local engine window {ctx:,})",
    "≈ {n:,} token（按窗口 {ctx:,} 估）":
        "≈ {n:,} tokens (assuming a {ctx:,} window)",
    "上下文能力": "Context window",
    "token（按服务商填；填了才能换算压缩百分比）":
        "tokens (per provider; needed to convert the compression percentages)",
    "token（0 = 自动读本地引擎，读不到按 12.8 万算）":
        "tokens (0 = auto-read from the local engine; 128k if unavailable)",
    ("本地模型建议：上面填 50、下面填 70。\n"
     "本地窗口留给大文件的余量不多，压得太晚，一次大文件输入就可能直接失败。"):
        ("Local model recommendation: 50 above, 70 below.\n"
         "A local window leaves little headroom for a big file — compress too "
         "late and one large input can fail outright."),
    "压缩模型列表": "Compressor list",
    "任务模型列表": "Task model list",
    "运行日志": "Log",

    # ---- 列表表头
    "名称": "Name",
    "并发": "Lanes",
    "阈值": "Threshold",
    "API 链接": "API URL",
    "启用": "On",
    "模型": "Model",

    # ---- 通用按钮
    "新增": "Add",
    "复制": "Duplicate",
    "删除": "Delete",
    "添加": "Insert",
    "保存配置": "Save config",
    "保存列表": "Save list",
    "保存价目表": "Save prices",
    "设为默认": "Set default",
    "启动代理": "Start proxy",
    "停止": "Stop",
    # ---- 5.0.5 思考级别统一闸
    "思考级别": "Thinking level",
    "跟随客户端": "As client sends",
    "当前：跟随客户端（代理不改写思考档位）":
        "Now: as the client sends (the proxy leaves thinking levels alone)",
    "当前：{level}（代理删客户端档位、统一换成它；即时生效）":
        "Now: {level} (the proxy strips the client's level and forces this one; live immediately)",
    "获取价目": "Fetch prices",
    "测试连接 ▾": "Test ▾",
    "测试目标连接": "Test target",
    "Ping（1 token，几乎免费）": "Ping (1 token, nearly free)",
    "测预填充 / 生成速度（消耗额度）": "Benchmark prefill / decode (billable)",
    "计算建议": "Compute advice",
    "计算本地账": "Compute local time",
    "应用到启动参数": "Apply to launch args",
    "从代理读取": "Read from proxy",

    # ---- 压缩模型表单
    "API Key": "API Key",
    "显示": "Show",
    "价目链接": "Price page URL",
    "留空 = 按 API 链接自动识别（DeepSeek 官方 / 百炼）":
        "Empty = detect from API URL (DeepSeek / Bailian)",
    "并发路数": "Concurrency",
    "  1-64；百炼按量付费与 DeepSeek 实测 16 路为拐点":
        "  1-64; measured knee at 16 lanes (Bailian PAYG & DeepSeek)",
    "额外参数": "Extra params",
    "本地模型（127.0.0.1:8080，并发固定 1 路）":
        "Local model (127.0.0.1:8080, forced to 1 lane)",

    # ---- 任务模型表单
    "启用云端完成任务模型（压缩完转给它答题）":
        "Use a cloud model for the task (forward the digest to it)",
    "左侧「任务模型列表」独立选择/保存；启用后代理才把压缩结果转给它。":
        "Selected/saved independently in the list above; only when enabled "
        "does the proxy forward digests to it.",
    ("Key 留空 = 透传 Cherry Studio 里填的 Key。\n"
     "地址支持 https://host[:port][/path]，例如\n"
     "https://api.deepseek.com 或 .../compatible-mode/v1。\n"
     "模型留空 = 透传客户端请求里的 model。"):
        ("Empty key = pass through the key from Cherry Studio.\n"
         "Accepts https://host[:port][/path], e.g.\n"
         "https://api.deepseek.com or .../compatible-mode/v1.\n"
         "Empty model = pass through the client's requested model."),

    # ---- 价目表
    "完成任务模型价目（每百万 token）": "Task model prices (per 1M tokens)",
    "压缩模型价目（每百万 token）": "Compressor prices (per 1M tokens)",
    "时段": "Window",
    "输入": "Input",
    "输出": "Output",
    "缓存命中": "Cache hit",
    "缓存写入": "Cache write",
    "显式命中": "Explicit hit",
    "起": "From",
    "止": "To",
    "命中": "Hit",
    "写入": "Write",
    "显式": "Expl.",
    "缓存模式": "Cache mode",
    "自动缓存不计写入费；显式缓存另算一次创建费":
        "Auto cache: no write fee. Explicit cache: one build fee.",
    "无缓存": "No cache",
    "自动缓存": "Auto cache",
    "显式缓存": "Explicit cache",
    ("五个价格字段对着账单填：输入 / 输出 / 缓存命中 / 显式命中 / 缓存写入。\n"
     "「输入（缓存命中）」填命中列；「显式缓存命中」填显式列（不填沿用命中价）；\n"
     "「显式缓存创建」填写入列，只在选显式缓存时计费。\n"
     "时段支持跨零点（如 16:30-00:30），左闭右开。\n"
     "压缩比越小越省钱，下限由你的质量底线决定，价格算不出来。"):
        ("Fill the five fields from your bill: Input / Output / Cache hit /\n"
         "Explicit hit / Cache write.\n"
         "\"Input (cache hit)\" -> Hit column; \"Explicit cache hit\" -> Expl.\n"
         "column (falls back to Hit if blank); \"Explicit cache creation\" ->\n"
         "Write column, billed only in explicit mode.\n"
         "Windows may cross midnight (e.g. 16:30-00:30), half-open on the left.\n"
         "Lower ratio always costs less; the floor is your quality bar, not price."),

    # ---- 建议输入
    "每轮新增 token": "New tokens / turn",
    "压缩比": "Ratio",
    "  0.34 = 保留 34% 正文": "  0.34 = keep 34% of the body",
    "当前历史 token": "History tokens",
    "每轮输出 token": "Output tokens / turn",
    "  含思考；只影响整轮花销，不影响净收益":
        "  incl. reasoning; affects total cost only, not net saving",
    "最少每轮净省": "Min net saving / turn",
    "代理按实际请求统计": "measured by the proxy",
    "填好价目表后点「计算建议」。": "Fill the price tables, then click Compute advice.",

    # ---- 本地任务模型
    "本地预填充": "Local prefill",
    "本地生成": "Local decode",
    "压缩预填充": "Comp. prefill",
    "压缩生成": "Comp. decode",
    "压缩速度按单路实测填；测速自动填单路与合计。":
        "Fill compressor speed per single lane; benchmark fills both lanes & aggregate.",
    "并发实测合计": "Measured aggregate",
    "预填 /": "prefill /",
    "生成 tok/s": "decode tok/s",
    "路": "lanes",
    "0 = 未测；合计只用于首压 / 多段。":
        "0 = not measured; aggregate applies to first / multi-chunk only.",
    "压缩固定延迟": "Fixed latency",
    "秒": "s",
    "压缩并发": "Concurrency",
    "分段长度": "Chunk",
    "token": "token",
    "时间价值": "Time value",
    "元/时": "CNY/h",
    "每轮新增 / 压缩比 / 当前历史 / 每轮输出与「成本建议」页共用。":
        "New/turn, ratio, history and output/turn are shared with Cost advice.",
    "当前时段": "Current window",
    "计算口径": "Model",
    "建议压缩阈值": "Suggested threshold",
    "首压 / 多段": "First / multi-chunk",
    "当前配置阈值": "Current config",
    "时段切换时自动更新配置（代理需重启生效）":
        "Auto-update config on window change (restart proxy to apply)",
    ("本地模型不收 token 费，压缩在钱上是纯支出，买到的是时间。\n"
     "压缩模型的预填充/生成速度要按实际 API 填，它决定结论。"):
        ("A local model charges no tokens, so compression is pure cost that buys\n"
         "time. The compressor's prefill/decode speed must match your real API —\n"
         "it decides the verdict."),
    "填好速度后点「计算本地账」。": "Fill the speeds, then click Compute local time.",
    "还没有统计数据（代理没跑过长对话）":
        "No stats yet (the proxy hasn't served a long conversation)",

    # ---- 窗口标题
    "longctx-proxy 启动器": "longctx-proxy launcher",

    # ---- 状态栏 / 弹窗标题与提示
    "服务地址": "Service address",
    "空闲": "idle",
    "占用中": "in use",
    "运行中…": "running…",
    "已停止": "stopped",
    "地址格式不对": "bad address",
    "就绪": "Ready",
    "未选中配置": "no config selected",
    # 带占位符的实时状态（用 tf 翻译并填充）
    "运行中 · {host}:{port} → 8080 · PID {pid}":
        "running · {host}:{port} → 8080 · PID {pid}",
    "启动中… PID {pid}": "starting… PID {pid}",
    # 任务模型历史实况（状态条 + 会话表）
    "尚未观测到请求": "no requests observed yet",
    "任务模型实况 {total} tok · 正文 {body} + 工具 {tools} · 压缩前 {raw}":
        "task-model live {total} tok · body {body} + tools {tools} · was {raw}",
    "任务模型实况 {total} tok · 正文 {body} + 工具 {tools} · 压缩前 {raw} · 引擎实测 {usage}":
        "task-model live {total} tok · body {body} + tools {tools} "
        "· was {raw} · engine usage {usage}",
    "任务模型历史实况": "Task-model history",
    "每轮实际发给任务模型的历史大小（压缩后正文 + 工具定义）；「引擎实测」取响应 usage.prompt_tokens，未回传时显示 —；「命中」取响应 cached_tokens：本地是 conversation cache 复用的 reused，云端是隐式/显式缓存命中，0 是真 0（前缀断了或该模型不支持隐式缓存）、— 是上游没报；代理停止后显示最后记录。":
        "Real size actually sent to the task model each turn "
        "(compressed body + tool defs); 'engine usage' comes from the "
        "response's usage.prompt_tokens (— when absent); 'hit' comes "
        "from cached_tokens (local: conversation-cache reused; cloud: "
        "implicit/explicit cache hit) where 0 is a real 0 and — means the "
        "upstream did not report it; keeps the last record after the proxy "
        "stops.",
    "会话编号": "Session",
    "正文 tok": "Body tok",
    "工具 tok": "Tools tok",
    "合计 tok": "Total tok",
    "压缩前 tok": "Pre tok",
    "引擎实测 tok": "Engine usage tok",
    # 3.34 缓存命中读数（引擎/云端报的，不是代理测的）
    "命中 tok": "Cache-hit tok",
    "回滚会话：": "Rollback conv:",
    "恢复到：": "Restore to:",
    "恢复缓存到该时刻": "Restore cache to this time",
    "最近一次：{st} 恢复至 {t}": "last: {st} -> {t}",
    "时刻格式：HH:MM 或 HH:MM:SS": "time format: HH:MM or HH:MM:SS",
    "尚无快照：本版启动后每轮对话拍一张，也可手填时刻":
        "no snapshots yet: one is taken per turn since this build; "
        "you can also type a time",
    "写命令失败：{e}": "cmd write failed: {e}",
    "已下发，下一轮对话生效": "armed; takes effect on the next turn",
    "备注：选定时间后按按钮确定，只对下一次对话生效；"
    "后续对话的缓存不会回到选定的时间点。":
        "note: the restore applies to the NEXT turn only; later turns keep "
        "rolling forward and do not jump back to the picked time.",
    "命中率": "Hit rate",
    " · 缓存命中 {hit}（{rate}）": " · cache hit {hit} ({rate})",
    # 2.9.5 锚点回声
    " · 引擎实测 {usage}": " · engine usage {usage}",
    " · 锚点回声 {echoed}/{total}": " · anchor echo {echoed}/{total}",
    "（待提醒 {missed}）": " ({missed} pending remind)",
    "锚点回声": "Anchors",
    "更新时间": "Updated",
    # 弹窗标题
    "启动": "Start",
    "启动失败": "Start failed",
    "已在运行": "Already running",
    "端口被占用": "Port in use",
    "保存": "Save",
    "价目表": "Price table",
    "测速": "Benchmark",
    "测速并发": "Benchmark lanes",
    "测速会占用本机算力": "Benchmark will use local compute",
    "上一轮测速还没结束，请稍等。": "The previous benchmark is still running; please wait.",
    "本地引擎没开": "Local engine is not running",
    "127.0.0.1:8080 没有监听，测速请求会被拒绝。\n\n"
    "先把本地引擎启动（默认 127.0.0.1:8080）；如果要测云端压缩模型，"
    "先在左侧列表选中那条云端配置，再点「测试连接 → 测速」。":
        "Nothing is listening on 127.0.0.1:8080, so benchmark requests will be refused.\n\n"
        "Start the local engine first (default 127.0.0.1:8080). To benchmark a cloud "
        "compressor, select that config in the left list, then use "
        "\"Test connection -> Benchmark\".",
    "测速会消耗预充值额度": "Benchmark will spend prepaid credit",
    "并发路数（1-64）：\n每路都会发一条约 {n} token 的请求（取分段长度，最多 32,000）。":
        "Lanes (1-64):\nEach lane sends one request of about {n} tokens "
        "(chunk size, max 32,000).",
    "本地模型不计 token 费，但会占用本机算力。":
        "The local model has no token fee, but it uses local compute.",
    "按当前时段压缩价估算约 {yuan} 元（输入 {i} / 输出上限 {o} token）。":
        "Estimated at the current compressor price: about CNY {yuan} "
        "(input {i} / output up to {o} tokens).",
    "价目表当前时段的数字填得不对，金额未知。":
        "The price table has invalid numbers for this window; amount unknown.",
    "价目表当前时段没填压缩价，金额未知。":
        "No compressor price for this window; amount unknown.",
    "这会真实调用本地引擎，占用本机算力，不产生 token 费。":
        "This really calls the local engine, uses local compute, and incurs no token fee.",
    "这会真实调用 API，从预充值余额扣费；实际金额以服务商账单为准。":
        "This really calls the API and deducts from your prepaid balance; "
        "the provider's bill is authoritative.",
    "目标配置：{name}\n{url}\n\n将按 {lanes} 路并发，各发一条约 {n} token 的请求"
    "（取分段长度，最多 32,000），并要求生成最多 {maxout} token。\n\n"
    "{cost}\n\n{charge}\n结果受网络、排队、限流影响，只作初步参考。\n\n继续？":
        "Target config: {name}\n{url}\n\n"
        "Will run {lanes} lanes, each sending about {n} tokens "
        "(chunk size, max 32,000), and request up to {maxout} generated tokens.\n\n"
        "{cost}\n\n{charge}\n"
        "Results vary with network, queueing and rate limits; treat them as a rough guide.\n\n"
        "Continue?",
    "测试": "Test",
    "提示": "Notice",
    "错误": "Error",
    "这条配置缺少 API 链接或 API Key。":
        "This config is missing an API URL or API Key.",
    "启用了任务模型，但没填它的 API 链接。":
        "The cloud task model is enabled but has no API URL.",
    "代理已经在运行，先停止再启动。":
        "The proxy is already running. Stop it first.",
    "填写有误：": "Invalid input: ",
    "先在「压缩模型列表」里选一条。":
        "Select a row in the compressor list first.",
    "先在「任务模型列表」里选一条。":
        "Select a row in the task model list first.",
}

_lang = ZH


def get_lang() -> str:
    return _lang


def set_lang(lang: str) -> str:
    global _lang
    _lang = lang if lang in (ZH, EN) else ZH
    return _lang


def t(text: str) -> str:
    """按当前语言返回文本。zh 模式下恒等，因此默认行为与未做国际化时一致。"""
    if _lang == EN and isinstance(text, str):
        return TR.get(text, text)
    return text


def tf(template: str, **kw) -> str:
    """先按语言翻译模板，再填充占位符。"""
    return t(template).format(**kw)


def other(text: str) -> str | None:
    """反查：给英文找中文（用于把动态拼接串的当前值换回 key）。"""
    if _lang != EN:
        return None
    for zh, en in TR.items():
        if en == text:
            return zh
    return None


def collect(root) -> list[tuple]:
    """遍历控件树，登记所有静态文本，返回可重放的登记表。

    每项：(widget, kind, key, zh)
      kind="text"    -> widget.configure(text=...)
      kind="heading" -> widget.heading(key, text=...)   key 是列名
      kind="menu"    -> widget.entryconfigure(key, label=...)
    """
    reg: list[tuple] = []

    def note(widget, kind, key, value):
        if isinstance(value, str) and value.strip() and value in TR:
            reg.append((widget, kind, key, value))

    def walk(widget):
        cls = widget.winfo_class()
        if cls in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton",
                   "TMenubutton", "TLabelframe", "Label", "Button",
                   "Checkbutton", "Radiobutton", "Menubutton", "LabelFrame"):
            # 动态文本（状态栏、带模型名的价目表标题）由各自的 setter 过 t()，
            # 登记了反而会在切换时把实时值冲掉，故跳过。
            if not getattr(widget, "_i18n_skip", False):
                try:
                    # 绑了 textvariable 的标签由变量自己维护，登记了反而会
                    # 把 text= 写死、断开绑定（值恰好等于某个 key 时才会踩到）。
                    # LabelFrame / Button 没有这个选项，取不到就当空。
                    try:
                        bound = str(widget.cget("textvariable") or "")
                    except Exception:
                        bound = ""
                    if not bound:
                        note(widget, "text", None, widget.cget("text"))
                except Exception:
                    pass
        cls_name = type(widget).__name__
        if cls_name == "Treeview":
            for col in (("#0",) + tuple(widget["columns"])):
                try:
                    note(widget, "heading", col, widget.heading(col).get("text"))
                except Exception:
                    pass
        if cls_name == "Menu" or cls == "Menu":
            try:
                # Tk 的 index("end") 返回的是**最后一项的下标**，不是项数，
                # 直接用 range() 会漏掉最后一条菜单项。
                last = widget.index("end")
                if isinstance(last, str):
                    last = -1
                for i in range(int(last) + 1):
                    note(widget, "menu", i, widget.entrycget(i, "label"))
            except Exception:
                pass
        for child in widget.winfo_children():
            walk(child)

    walk(root)
    return reg


def apply_lang(root, registry: list[tuple]) -> None:
    """按当前语言重放登记表。"""
    en = _lang == EN
    for widget, kind, key, zh in registry:
        value = TR[zh] if en else zh
        try:
            if kind == "text":
                widget.configure(text=value)
            elif kind == "heading":
                widget.heading(key, text=value)
            elif kind == "menu":
                widget.entryconfigure(key, label=value)
        except Exception:
            pass  # 控件已销毁：忽略，下次重建时会重新登记
