<p align="center"><sub><b>English</b> · <a href="docs/README.zh-CN.md">简体中文</a></sub></p>

# longctx-proxy

**A cache-aware context-compression proxy for OpenAI-compatible clients.**

It sits between your chat client (Cherry Studio, or any OpenAI-compatible app) and any LLM API, compresses long conversations so they fit the model's window — **without breaking the prefix/KV cache**, and with **continuous measurement of what the model still remembers**.

GUI 5.1.0 · proxy 3.36 · Python 3.10+ · Windows-first · Apache-2.0

---

## Why another compression proxy?

Most context-compression tools ignore two failure modes:

1. **Compression breaks the cache.** Rewriting history changes the request prefix, so the local engine's KV cache (or the cloud's implicit prefix cache) misses and you pay full prefill again. The token savings evaporate — sometimes you pay *more*.
2. **Compression eats memory.** Goals, constraints, numbers, paths and decisions get summarized away, and the model silently stops honoring them. You find out three turns later.

longctx-proxy is built around both:

- **Prefix-stable compression** — old messages are compressed once, summaries are content-hash cached, the outgoing prefix stays byte-stable, and a tool-definition fingerprint watchdog warns you when a single changed byte kills the cache.
- **A memory closed loop** — per-anchor echo measurement → silent-compliance judging → re-injection of ignored anchors on the next turn. In our survey, no other open project closes this full loop (see [docs/related-work.md](docs/related-work.md)).

## Features

**Compression**
- Checkpoint state engine: extracts goals / rules / facts / TODOs from history into a compact state block; filters harness & system-injected noise; detects user corrections ("not A, it's B") and goal drift.
- Dynamic compression ratio with density-adaptive band (clamped 0.50–0.75, honest UI when the band collapses to a fixed value).
- Hot-layer protection: recent messages and pinned facts are never compressed.
- Deterministic fidelity backfill: URLs, file paths, versions, IPs, emails, IDs and numbers are regex-extracted from the source and backfilled into summaries (inspired by LLMLingua-2's force-reserve idea); token-boundary cut repair.
- Evidence folding: before a tool result is dropped, its evidence lines are folded into the checkpoint.
- Task modes (auto / table / legal-engineering / off): code blocks, tables and clauses kept verbatim when they matter.
- Parallel map-reduce digest with per-provider concurrency control; re-compaction of already-compressed history when it grows again.

**Cache awareness**
- Prefix-stable transforms + stored-prefix invalidation only when necessary.
- Live cache-hit telemetry: `reused` tokens from local engines, implicit/explicit hit tokens from cloud APIs, shown per turn.
- Tools fingerprint watch: alerts when tool definitions change (a one-byte change can zero out the hit rate).
- **Snapshot & rollback**: every turn takes a state snapshot; one click restores the conversation state to a chosen moment (one-shot, then normal operation resumes).

**Memory observability**
- Per-anchor echo tracking: which checkpoint anchors the model actually repeats back.
- Canary probes + cold deep-anchor questions: active recall checks, not just passive echo.
- Optional small-model "silent compliance" judge: not mentioned ≠ not followed.
- Dashboard chart: green = echo centroid (memory), purple = probe survival, gray = baseline; three consecutive turns under baseline → "lower the compression" advice. Low-confidence pools drawn as dots, pathological turns broken out — the chart tells you when not to trust it.
- Offline deterministic recall audit (`audit_recall.py`): identifier & constraint recall rates, loss lists, no model calls.

**Routing & control**
- Multi-upstream profiles: separate compression model and task model; local engine + cloud API mixing.
- Sampling takeover (temperature/top_p/top_k/seed), thinking-level override, max_tokens clamping.
- Time-window pricing per model, cost advisor, KPI history.
- i18n: Chinese / English UI.

**Engineering**
- 839 offline, deterministic unit tests (`test_v2_units.py`); build gate refuses to package on red.
- Stress-tested at 320k / 600k token windows.
- Backoff & throttle handling for 429/5xx; per-request dump pairs for auditing.

## Architecture

```
Cherry Studio / any OpenAI-compatible client
        │   API base: http://127.0.0.1:8082/v1
        ▼
┌──────────────────────────────────────┐
│  proxy/proxy.py                      │  compress · checkpoint · route · measure
│    ├ checkpoint.py   state engine    │
│    ├ evidence.py     evidence fold   │
│    └ engine/src/longctx_mcp          │  map-reduce digest pipeline
└──────────────────────────────────────┘
        │                         │
        ▼                         ▼
  local engine               cloud task model
  127.0.0.1:8080             DeepSeek / Qwen / any OpenAI-compatible
        ▲
  GUI (app-src/main.py, tkinter): start/stop · profiles · KPI chart · rollback
```

The GUI is a launcher + dashboard: it runs `proxy/proxy.py` with a local Python interpreter and talks to it through files and a stats endpoint. The proxy itself is stdlib `http.server` + `httpx`.

## The memory closed loop

| Stage | Capability | Where |
|---|---|---|
| G1 | Compress once, keep the prefix stable | content-hash summary cache, `[longctx]` marks |
| G2 | Measure reality, not estimates | side-channel parse of upstream `usage` / cache fields |
| G3 | Per-anchor echo measurement | `parse_anchors` + `anchor_echo` |
| G4 | Silent-compliance judging (not mentioned ≠ not followed) | `judge_followed` (optional small model) |
| G5 | Re-inject what was ignored next turn | `remind_block`, capped |

Survey of related projects (context-canary, Letta/MemGPT, context-rot detectors, compression proxies such as tokenfold / copium / LLMLingua): each covers pieces; none closes G3→G4→G5. Details and sources: [docs/related-work.md](docs/related-work.md).

## Quick start (Windows)

1. Install **Python 3.10+** (tick "Add Python to PATH").
2. Install dependencies:
   ```cmd
   pip install -r requirements.txt
   ```
   (`httpx` is required; `tiktoken` is optional — without it token counts fall back to character estimates.)
3. Run the GUI from source:
   ```cmd
   python app-src\main.py
   ```
   Or build the exe first (below). First run creates `profiles.json` / `targets.json` / `pricing.json` next to the GUI with **empty API keys**.
4. Pick a compression profile, fill in the API key, save.
5. Two ways to run:
   - **Local engine**: a local OpenAI-compatible server listening on `127.0.0.1:8080`; choose the "local" profile, no key needed.
   - **Cloud-only**: add a cloud profile on the left (compression) **and** a task model on the right (answers), enable it. Both sides must be set — otherwise requests still go to `127.0.0.1:8080`.
6. Point your client's API base at:
   ```
   http://127.0.0.1:8082/v1
   ```
   The key you type in the client can be anything; the proxy uses the keys you configured. Start the proxy from the GUI before chatting.

### Build the GUI exe

```cmd
pip install pyinstaller
pyinstaller longctx-proxy-gui.spec
```

Put the produced exe in a folder **next to `proxy/` and `engine/`** (e.g. `app/`), and next to the exe create `python_path.txt` containing one line: the full path of a Python interpreter that has `httpx`/`tiktoken` installed. Alternatively set the `LONGCTX_PY` environment variable. Then double-click the exe.

`build.py` is the gated build: it runs the full test suite first and refuses to package on any failure.

> Unsigned executables trigger Windows SmartScreen — "More info → Run anyway" is the normal path.

## Reading the dashboard

- **Green line = echo centroid**: how much of the old checkpoint content the model still repeats back (20-turn rolling). It measures *memory*, **not cache**.
- **Purple line = probe survival**: active canary/cold-probe recall.
- **Gray line = baseline**; advice only compares like-for-like metrics.
- **Hit tok / hit rate column** = the engine's or cloud's own reported cache reuse. Hit rate → 0 means the prefix bytes changed or the session was evicted (expect a slow first token that turn). This is the *cache* signal — a different thing from the green line.
- **Rollback row**: pick a snapshot time, click once; the next turn restores state to that moment, then normal operation resumes.

## Offline recall audit

```cmd
set PROXY_DUMP=1            :: before starting the proxy
python audit_recall.py --latest
python audit_recall.py --json recall-report.json
```

Deterministic matching only (exact / normalized / char-bigram), no model calls: token retention, identifier recall (numbers, versions, paths, URLs, error codes, hashes), constraint recall, checkpoint coverage, and an explicit loss list.

## Optional: MCP long-document engine

`engine/src/longctx_mcp` also runs standalone as an MCP stdio server (32k precise splitting → per-chunk model calls → tree aggregation):

```cmd
pip install mcp
python -m longctx_mcp.server
```

The proxy imports the same package for its digest pipeline — keep `engine/src` next to `proxy/`.

## Tests

```cmd
python test_v2_units.py
```

839 offline tests, no model calls, deterministic. The build gate (`build.py`) requires all green.

## Configuration knobs (excerpt)

| Variable | Meaning |
|---|---|
| `LONGCTX_BASE_URL` / `LONGCTX_API_KEY` / `LONGCTX_MODEL` | compression-model endpoint (env fallback) |
| `LONGCTX_PY` | interpreter used to run the proxy (frozen GUI) |
| `LONGCTX_PROXY_DIR` / `LONGCTX_PROXY_PATH` | override proxy location |
| `LONGCTX_MCP_JSON` | path to MCP-style config JSON for LLM settings |
| `PROXY_DUMP`, `PROXY_DUMP_DIR`, `PROXY_DUMP_KEEP`, `PROXY_DUMP_MAX_MB` | request dump pairs for auditing |
| `PROXY_HOT_TOKENS` | hot-layer size (recent messages never compressed) |
| `PROXY_ANCHOR_RATIO` / `PROXY_ANCHOR_MAX` / `PROXY_ANCHOR_JUDGE` / `PROXY_ANCHOR_REMIND_MAX` | anchor echo loop tuning |
| `PROXY_SNAP_KEEP` | snapshots kept for rollback (default 48) |

Most day-to-day settings live in the GUI (profiles, ratio, task mode, window, routing, pricing).

## Limitations (honest)

- **Echo ≠ recall.** A model can remember without repeating; the green line is a cheap passive proxy — probes and the offline audit exist to cover its blind spots.
- Compression is lossy by design. Fidelity backfill and evidence folding mitigate, not guarantee.
- Compressed history still grows turn over turn; enable re-compaction for very long sessions.
- Windows-first: the single-instance lock uses `msvcrt`; the GUI is tkinter. The proxy core is largely platform-neutral but not tested elsewhere.
- The GUI exe is unsigned (SmartScreen warning).

## License

Apache-2.0. See [LICENSE](LICENSE).
