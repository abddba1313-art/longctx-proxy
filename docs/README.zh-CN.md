<p align="center"><sub><a href="../README.md">English</a> · <b>简体中文</b></sub></p>

# longctx-proxy

**缓存感知的上下文压缩代理，面向 OpenAI 兼容客户端。**

它放在你的聊天客户端（Cherry Studio，或任何 OpenAI 兼容应用）和任意 LLM API 之间，把长对话压缩到模型窗口装得下的长度——**同时不打碎前缀 / KV 缓存**，并且**持续测量模型还记得多少**。

GUI 5.1.0 · 代理 3.36 · Python 3.10+ · 以 Windows 为主 · Apache-2.0

---

## 为什么还要再做一个压缩代理？

多数上下文压缩工具忽略了两个失效模式：

1. **压缩打碎缓存。** 改写历史就改变了请求前缀，于是本地引擎的 KV 缓存（或云端的隐式前缀缓存）失配，你得重新付一遍全价 prefill。省下的 token 钱又吐了回去——有时反而更贵。
2. **压缩吃掉记忆。** 目标、约束、数字、路径、决定被摘要掉，模型悄悄不再遵守。你往往三轮之后才发现。

longctx-proxy 就是围绕这两点造的：

- **前缀稳定压缩**——老消息只压一次、摘要按内容哈希缓存、出站前缀保持字节稳定，还有一个工具定义指纹看门狗，改一个字节就提醒你缓存要掉。
- **记忆闭环**——逐锚点回声测量 → 沉默遵守判定 → 下一轮把被忽视的锚点回注。据我们检索，没有别的开源项目把这条完整闭环做全（见 [related-work.md](related-work.md)）。

## 功能

**压缩**
- 检查点状态引擎：从历史里抽取目标 / 规则 / 事实 / 待办，压成一个紧凑状态块；过滤 harness 与系统注入的噪声；识别用户纠正（"不是 A，是 B"）与目标漂移。
- 动态压缩比，密度自适应区间（限幅 0.50–0.75；区间塌缩成固定值时界面如实说明）。
- 热层保护：最近的消息与被钉住的事实绝不压缩。
- 确定性保真回填：URL、文件路径、版本号、IP、邮箱、编号、数字用正则从原文抽出、回填进摘要（借鉴 LLMLingua-2 的 force-reserve 思路）；token 边界切割修复。
- 证据折叠：工具结果被丢弃前，先把它的证据行折进检查点。
- 任务模式（自动 / 表格 / 法律工程 / 关闭）：代码块、表格、条款在需要时整块原样保留。
- 并行 map-reduce 摘要，按提供方控并发；已压历史再次变长时可重压。

**缓存感知**
- 前缀稳定变换，仅在必要时才作废存储前缀。
- 实时缓存命中遥测：本地引擎的 `reused` token、云端的隐式 / 显式命中 token，逐轮展示。
- 工具指纹看门狗：工具定义一变就报警（一个字节就能把命中率清零）。
- **快照与回滚**：每轮拍一张状态快照，一键把对话状态拨回选定时刻（一次性生效，之后照常顺延）。

**记忆可观测**
- 逐锚点回声跟踪：检查点里哪些锚点模型真的复述回来了。
- 金丝雀探针 + 冷深锚问题：主动召回检查，不只是被动回声。
- 可选的小模型"沉默遵守"判定：没提到 ≠ 没照做。
- 仪表盘线图：绿线＝回声重心（记忆）、紫线＝探针存活、灰线＝基线；连续 3 轮压在基线下 → 提示"降一档压缩"。低置信池画成点、病态轮次断开——图会告诉你什么时候别信它。
- 离线确定性召回审计（`audit_recall.py`）：标识与约束召回率、丢失清单，不调模型。

**路由与控制**
- 多上游配置：压缩模型与任务模型分开；本地引擎 + 云端 API 混搭。
- 采样接管（temperature/top_p/top_k/seed）、思考档位覆盖、max_tokens 限幅。
- 按模型分时段计价、成本顾问、KPI 历史。
- 国际化：中 / 英界面。

**工程**
- 839 个离线确定性单元测试（`test_v2_units.py`）；构建闸门红灯拒绝出包。
- 320k / 600k token 窗口压测。
- 429/5xx 退避与节流处理；逐请求 dump 对供审计。

## 架构

```
Cherry Studio / 任意 OpenAI 兼容客户端
        │   API 地址：http://127.0.0.1:8082/v1
        ▼
┌──────────────────────────────────────┐
│  proxy/proxy.py                      │  压缩 · 检查点 · 路由 · 测量
│    ├ checkpoint.py   状态引擎        │
│    ├ evidence.py     证据折叠        │
│    └ engine/src/longctx_mcp          │  map-reduce 摘要管线
└──────────────────────────────────────┘
        │                         │
        ▼                         ▼
  本地引擎                    云端任务模型
  127.0.0.1:8080             DeepSeek / Qwen / 任意 OpenAI 兼容
        ▲
  GUI（app-src/main.py，tkinter）：启停 · 配置 · KPI 线图 · 回滚
```

GUI 是启动器 + 仪表盘：它用一个本地 Python 解释器跑 `proxy/proxy.py`，通过文件和一个 stats 端点与代理通信。代理本体是标准库 `http.server` + `httpx`。

## 记忆闭环

| 段 | 能力 | 位置 |
|---|---|---|
| G1 | 只压一次、保持前缀稳定 | 内容哈希摘要缓存、`[longctx]` 标记 |
| G2 | 测实测、不测估计 | 旁路解析上游 `usage` / 缓存字段 |
| G3 | 逐锚点回声测量 | `parse_anchors` + `anchor_echo` |
| G4 | 沉默遵守判定（没提 ≠ 没照做） | `judge_followed`（可选小模型） |
| G5 | 下轮回注被忽视的锚点 | `remind_block`，封顶 |

同类项目检索（context-canary、Letta/MemGPT、context-rot 检测器、tokenfold / copium / LLMLingua 等压缩代理）：各做一段，没人做全 G3→G4→G5。细节与来源见 [related-work.md](related-work.md)。

## 快速上手（Windows）

1. 装 **Python 3.10+**（勾选 "Add Python to PATH"）。
2. 装依赖：
   ```cmd
   pip install -r requirements.txt
   ```
   （`httpx` 必装；`tiktoken` 可选——没有它 token 计数退回字符估算。）
3. 从源码跑 GUI：
   ```cmd
   python app-src\main.py
   ```
   或先打包 exe（见下）。首次运行在 GUI 同目录生成 `profiles.json` / `targets.json` / `pricing.json`，**API Key 全空**。
4. 选一个压缩配置，填 API Key，保存。
5. 两种跑法：
   - **本地引擎**：本机有一个 OpenAI 兼容服务监听 `127.0.0.1:8080`；选"本地"那条，不用填 Key。
   - **全云端**：左侧加一个云端压缩配置，右侧加一个任务模型并启用。**两边都要设**——否则请求仍发去 `127.0.0.1:8080`。
6. 把客户端的 API 地址指向：
   ```
   http://127.0.0.1:8082/v1
   ```
   客户端里填的 Key 随便写，代理用的是你配置的那个。聊天前先在 GUI 里启动代理。

### 打包 GUI exe

```cmd
pip install pyinstaller
pyinstaller longctx-proxy-gui.spec
```

把产物 exe 放进**与 `proxy/`、`engine/` 并排**的目录（例如 `app/`），并在 exe 旁建 `python_path.txt`，一行写装了 `httpx`/`tiktoken` 的解释器完整路径；或设环境变量 `LONGCTX_PY`。然后双击 exe。

`build.py` 是带闸门的构建：先跑全套测试，任何失败都拒绝出包。

> 未签名 exe 会触发 Windows SmartScreen——"更多信息 → 仍要运行"是正常路径。

## 界面怎么看

- **绿线＝回声重心**：旧检查点内容还有多少被模型复述回来（近 20 轮滚动）。测的是**记忆**，**不是缓存**。
- **紫线＝探针存活率**：主动金丝雀 / 冷探针召回。
- **灰线＝基线**；建议只在同口径之间比较。
- **命中 tok / 命中率列**＝引擎或云端自报的缓存复用。命中率掉 0＝前缀字节被改或会话被踢出缓存（那轮首包会明显变慢）。这才是**缓存**信号——跟绿线是两回事。
- **回滚行**：选一个快照时刻点一次，下一轮把状态拨回那一刻，之后照常顺延。

## 离线召回审计

```cmd
set PROXY_DUMP=1            :: 启动代理前设置
python audit_recall.py --latest
python audit_recall.py --json recall-report.json
```

纯确定性匹配（精确 / 归一化 / 字符二元组），不调模型：token 保留率、标识召回（数字、版本、路径、URL、错误码、哈希）、约束召回、检查点覆盖，以及一份明确的丢失清单。

## 可选：MCP 长文档引擎

`engine/src/longctx_mcp` 也能作为独立 MCP stdio 服务运行（32k 精确分段 → 逐段调模型 → 树状汇总）：

```cmd
pip install mcp
python -m longctx_mcp.server
```

代理的摘要管线导入同一个包——`engine/src` 必须与 `proxy/` 并排保留。

## 测试

```cmd
python test_v2_units.py
```

839 个离线测试，不调模型，确定性。构建闸门（`build.py`）要求全绿。

## 配置项（节选）

| 变量 | 含义 |
|---|---|
| `LONGCTX_BASE_URL` / `LONGCTX_API_KEY` / `LONGCTX_MODEL` | 压缩模型端点（env 回退） |
| `LONGCTX_PY` | 跑代理用的解释器（冻结 GUI） |
| `LONGCTX_PROXY_DIR` / `LONGCTX_PROXY_PATH` | 覆盖代理位置 |
| `LONGCTX_MCP_JSON` | MCP 风格 LLM 配置 JSON 的路径 |
| `PROXY_DUMP`、`PROXY_DUMP_DIR`、`PROXY_DUMP_KEEP`、`PROXY_DUMP_MAX_MB` | 供审计的请求 dump 对 |
| `PROXY_HOT_TOKENS` | 热层大小（最近消息不压缩） |
| `PROXY_ANCHOR_RATIO` / `PROXY_ANCHOR_MAX` / `PROXY_ANCHOR_JUDGE` / `PROXY_ANCHOR_REMIND_MAX` | 锚点回声闭环调参 |
| `PROXY_SNAP_KEEP` | 回滚保留的快照数（默认 48） |

日常大多数设置在 GUI 里（配置、压缩比、任务模式、窗口、路由、计价）。

## 诚实的边界

- **回声 ≠ 召回。** 模型可能记得但不复述；绿线是便宜的被动代理信号——探针和离线审计就是来补它盲区的。
- 压缩设计上就有损。保真回填与证据折叠是缓解，不是保证。
- 压后历史仍逐轮变长；超长会话请开重压。
- 以 Windows 为主：单实例锁用 `msvcrt`，GUI 是 tkinter。代理核心基本平台中立，但未在别处测试。
- GUI exe 未签名（SmartScreen 提示）。

## 许可证

Apache-2.0。见 [LICENSE](../LICENSE)。
