# 上下文闭环监测 · 同类项目对照表

> 记录「检查点锚点回声 → 沉默遵守判定 → 未回声回注」这条闭环在
> GitHub / 社区里的同类工作，标明**各家做到了第几段、缺哪一段**。
>
> 置信度约定：每行带可点开的来源；读过原文标「已核」，只依据搜索摘要标「据检索」，
> 凭背景知识标「背景·未核」——三种置信度不混写。能力段编号见第 1 节。
>
> 最后更新：2026-11-11

---

## 1. 闭环的五个能力段（本项目的实现位置）

| 段 | 能力 | longctx 实现 | 开关 |
|----|------|--------------|------|
| G1 | 压缩 + 前缀稳定（老消息只压一次、缓存不打架） | proxy.py 摘要按内容 sha256 缓存、`[longctx]` 标记跳过 | 全程 |
| G2 | 实测对账（引擎 usage.prompt_tokens 旁观解析） | `_forward` 旁听 SSE/JSON，挂进实况 | 全程 |
| G3 | 锚点级回声测量（检查点每条锚逐一核实出现与否） | `parse_anchors` + `anchor_echo`（逐字 or 实体命中率≥50%） | `PROXY_ANCHOR_RATIO/MAX` |
| G4 | 沉默遵守判定（没提及≠没照做，问一次小模型） | `judge_followed` 批量 Y/N | `PROXY_ANCHOR_JUDGE=0` 关 |
| G5 | 未回声回注（下轮把"它忽视了哪几条"写回检查点尾部） | `remind_block`，封顶 3 条，插在 end 标记前 | `PROXY_ANCHOR_REMIND_MAX` |

## 2. 对照表

| 项目 / 来源 | 形态 | 覆盖段 | 与完整闭环的差距 | 证据 | 置信度 |
|---|---|---|---|---|---|
| **context-canary**（JuliusBrussee/skills） | Agent 技能 | ≈G3 | canary 是整体失效探针（串没出现→上下文坏了），不逐条测量、不判定遵守、不回注 | github.com/JuliusBrussee/skills/blob/main/skills/context-canary/SKILL.md | 据检索 |
| **pydantic-ai-harness Issue #83**「System Reminders / Mid-Session Behavioral Steering」 | 讨论 | ≈G5 | 讨论"中途注入提醒转向行为"，无回声测量、无遵守判定；停在需求层 | github.com/pydantic/pydantic-ai-harness/issues/83 | 据检索 |
| **context-rot-detection**（milos-product-maker） | 小项目 | 探测 | 长上下文退化检测，非逐锚点、非闭环 | github.com/milos-product-maker/context-rot-detection | 据检索 |
| **classifier-context-rot**（samm393） | 小项目 | 探测 | 用分类器识别上下文腐化，同上 | github.com/samm393/classifier-context-rot | 据检索 |
| **agentverify**（simukappu） | 框架 | 核验（通用） | 对 agent 输出做验证/评估，对象是回答质量，不针对注入状态 | github.com/simukappu/agentverify | 据检索 |
| **agent-audit**（USTechAutomations）/ **auditable-agent**（Fangyuan025）/ **AgentShield**（AdityaBelhekar） | 审计/安全 | 核验（通用） | 事后审计与安全护栏方向，无轮内闭环 | 见各自仓库 | 据检索 |
| **Letta（MemGPT 系）**：agent-memory 博客、letta-code、v1 agent loop 复盘 | 框架 + 文章 | G1 近邻 | 管"记忆存取与分层"，不核实"模型这轮读没读注入块" | letta.com/blog/agent-memory/、letta.com/blog/letta-v1-agent/ | 据检索 |
| **VerifiedState × Letta 集成页** | 服务 | 记忆验证 | "外部已验证记忆"口号接近 G2+G3，但验证对象是记忆写入而非回答回声；页面偏营销待拆 | verifiedstate.ai/integrations/letta | 据检索 |
| **Mark Hendrickson**《your agent harness owns context, not truth》 | 博文 | 理念 | 论证 harness（代理层）该管上下文真相——正是 longctx 的架构立场，无实现 | markmhendrickson.com/posts/your-agent-harness-owns-context-not-truth/ | 据检索 |
| **Tim Kellogg**《Layers of Memory, Layers of Compression》 | 博文 | G1 理念 | 压缩分层讨论；其"摘要的摘要"缺口正是本代理已知边界（压后历史仍以 0.34×新增爬长） | timkellogg.me/blog/2025/06/15/compression | 据检索 |
| **Lost in the Middle**（Stanford 等, 2023 论文） | 论文 | 动机 | 中段注意力衰减的实证——G4/G5 存在的理由 | arXiv 2307.03172 | 背景·未核 |
| **Chroma《context rot》报告**（2025） | 报告 | 动机 | 长上下文性能随长度衰减的社区研究，Reddit/HN 讨论多（本轮归档接口超时，帖子链接待补） | trychroma.com 研究页 | 背景·未核 |
| **Agent Patterns Catalog** · Letta 条目 | 目录站 | 索引 | 可用作后续同类检索入口 | agentpatternscatalog.org/compositions/letta/ | 据检索 |

### 2.1 压缩代理类（2026-11-11 补充，GitHub 仓库检索）

| 项目 | 星数 | 做什么 | 与 longctx 的差距 | 置信度 |
|---|---|---|---|---|
| snchimata/tokenfold | 101 | 可逆压缩 schema/日志，provider-neutral | 不处理前缀缓存命中，无回声/遵守闭环 | 据检索 |
| iKislay/copium | 19 | drop-in 压缩代理，宣称 65–90% token 节省 | 同上，无记忆测量 | 据检索 |
| attnstack/tokencut | 2 | 本地 prompt 收缩代理，保 JSON 合法 | 同上 | 据检索 |
| diegosouzapw/OmniGlyph | 169 | 把上下文渲染成 PNG 图片省 token | 路线不同（图像化），不保 KV 缓存语义 | 据检索 |

## 3. 社区讨论（待补）

- Reddit 检索本轮**未取到数据**（Arctic Shift 免密归档对 r/LocalLLaMA、r/LLMDevs 连续超时；
  关键词 `context rot`、`reminder ignored`、`compaction loses details`）。
  → 待办：带凭据或换端点重试，把 "context rot 讨论串 / 长上下文遗忘求助串" 补成链接行。
- HN / V2EX 未搜。

## 4. 待细读（搜到但没看内容，只进了候选池）

| 仓库 | 为什么值得看 |
|---|---|
| ppradyoth/AgentInjectionBench | 注入类基准，可能含"提醒是否生效"的测法 |
| miggy-code/Panoptes | 名字暗示全景观测，功能未知 |
| mnemom/aip、emanalshazly/monna-adip | 记忆/代理方向，未辨 |
| buecking/incontext `docs/reference/failure-modes.md` | failure-modes 清单可能已给 G3/G4 术语 |
| muratcankoylan/Agent-Skills-for-Context-Engineering `degradation_detector.py` | 有现成检测脚本可拆 |

## 5. 结论快照（截至本轮）

**没有任何检索到的项目同时做 G3+G4+G5。** 各家集中在：canary 式单点探针（≈G3 弱化版）、
通用输出审计、记忆存取验证、以及"中途转向"的讨论（G5 需求层）。
把**逐锚点回声测量 → 小模型判沉默遵守 → 未回声下轮回注**缝成轮内闭环的，
本轮检索范围内是首例。压缩代理类（2.1 节）同样无人做记忆闭环与前缀缓存保护。

## 6. 更新记录

| 日期 | 改动 | 来源 |
|---|---|---|
| 2026-11-06 | 建表：5 段划分 + 14 行对照 + Reddit 超时备注 + 5 行待细读 | 三路 web 检索 |
| 2026-11-11 | 补 2.1 节压缩代理类 4 行（tokenfold/copium/tokencut/OmniGlyph） | GitHub 仓库搜索 API |
