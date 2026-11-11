"""longctx-mcp：32k 精确分段 + 逐段调用模型 + 树状汇总（MCP stdio）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # mcp 2.x 把 FastMCP 改名为 MCPServer；同时兼容 1.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import DEFAULT_CONCURRENCY, DEFAULT_MAX_TOKENS, DEFAULT_TOKENIZER, env, llm_settings
from .llm import LLMClient
from .pipeline import aggregate as aggregate_impl
from .pipeline import run_map
from .splitter import split_text as split_text_impl
from .store import SessionMeta, SessionStore

mcp = FastMCP("longctx-mcp")
store = SessionStore()
_PID = os.getpid()
_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _manifest(meta: SessionMeta, include_chunks: bool = True) -> dict:
    payload = {
        "session_id": meta.session_id,
        "source": meta.source,
        "created_at": meta.created_at,
        "tokenizer": meta.tokenizer,
        "estimated": meta.estimated,
        "total_tokens": meta.total_tokens,
        "total_chars": meta.total_chars,
        "max_tokens": meta.max_tokens,
        "overlap_tokens": meta.overlap_tokens,
        "chunk_count": len(meta.chunks),
        "notes": meta.notes,
    }
    if include_chunks:
        payload["chunks"] = [
            {
                "index": c.index,
                "tokens": c.token_count,
                "chars": c.char_count,
                "preview": c.preview,
            }
            for c in meta.chunks
        ]
    return payload


def _read_source(text: str | None, file_path: str | None, encoding: str) -> tuple[str, str]:
    if text and file_path:
        raise ValueError("text 和 file_path 只能二选一")
    if file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ValueError(f"文件不存在：{path}")
        raw = path.read_bytes()
        content = raw.decode("utf-8-sig" if encoding.lower() == "utf-8" else encoding, errors="replace")
        return content, str(path)
    if text:
        return text, "<inline>"
    raise ValueError("请提供 text 或 file_path")


def _client(
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    timeout: float,
) -> LLMClient:
    base, key, name = llm_settings(base_url=base_url, api_key=api_key, model=model)
    return LLMClient(base_url=base, api_key=key, model=name, timeout=timeout)


@mcp.tool(name="split_text")
def split_text(
    text: str | None = None,
    file_path: str | None = None,
    session_id: str | None = None,
    max_tokens: int = 0,
    overlap_tokens: int = 0,
    tokenizer: str = DEFAULT_TOKENIZER,
    prefer_boundary: bool = True,
    encoding: str = "utf-8",
) -> str:
    """把长文本按 token 精确切成若干段并落盘，返回会话清单（不含正文）。

    max_tokens 为每段上限，默认取环境变量 LONGCTX_MAX_TOKENS（否则 32000）。
    text 与 file_path 二选一。切完后用 read_chunk 取正文，或用 run_chunks 逐段调用模型。
    """
    limit = max_tokens or int(env("LONGCTX_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
    content, source = _read_source(text, file_path, encoding)
    result = split_text_impl(
        content,
        max_tokens=limit,
        overlap_tokens=overlap_tokens,
        tokenizer=tokenizer,
        prefer_boundary=prefer_boundary,
    )
    meta = store.create(result, source=source, max_tokens=limit, overlap_tokens=overlap_tokens, session_id=session_id)
    return _j(_manifest(meta))


@mcp.tool()
def list_chunks(session_id: str) -> str:
    """查看某个会话的分段清单。"""
    return _j(_manifest(store.load(session_id)))


@mcp.tool()
def read_chunk(session_id: str, chunk_index: int, max_chars: int = 0) -> str:
    """读取某一段的正文。max_chars>0 时截断返回。"""
    text = store.read_chunk(session_id, chunk_index)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n...[已截断，完整长度 {len(text)} 字符]"
    return text


@mcp.tool()
async def run_chunks(
    session_id: str,
    prompt: str,
    chunk_indices: list[int] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int | None = None,
    concurrency: int = 0,
    skip_existing: bool = False,
    timeout: float = 600.0,
) -> str:
    """对每一段调用一次模型，结果落盘，返回成功/失败统计。

    prompt 中用 {chunk} 代表分段正文（不写则自动追加），还可用 {index}/{total}/{source}。
    模型与接口：参数优先，其次读 LONGCTX_MODEL / LONGCTX_BASE_URL / LONGCTX_API_KEY。
    """
    meta = store.load(session_id)
    indices = chunk_indices if chunk_indices else [c.index for c in meta.chunks]
    for idx in indices:
        if not (0 <= idx < len(meta.chunks)):
            raise ValueError(f"chunk 索引越界：{idx}")
    client = _client(model, base_url, api_key, timeout)
    summary = await run_map(
        store,
        meta,
        indices,
        prompt,
        client,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        concurrency=concurrency or int(env("LONGCTX_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
        skip_existing=skip_existing,
    )
    return _j(summary)


@mcp.tool()
def get_results(session_id: str, chunk_indices: list[int] | None = None, max_chars: int = 4000) -> str:
    """取逐段结果。默认每段最多返回 4000 字符，max_chars=0 表示不截断。"""
    meta = store.load(session_id)
    indices = chunk_indices if chunk_indices else store.result_indices(session_id)
    out = []
    for idx in indices:
        record = store.read_result(session_id, idx)
        if record is None:
            out.append({"index": idx, "found": False})
            continue
        item = {
            "index": idx,
            "ok": record.get("ok"),
            "error": record.get("error"),
            "content": record.get("content", ""),
            "completion_tokens": record.get("completion_tokens"),
        }
        if max_chars and len(item["content"]) > max_chars:
            item["content"] = item["content"][:max_chars]
            item["truncated"] = True
        out.append(item)
    return _j({"session_id": session_id, "count": len(out), "results": out})


@mcp.tool()
async def aggregate(
    session_id: str,
    reduce_prompt: str,
    chunk_indices: list[int] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    budget_tokens: int = 0,
    temperature: float = 0.2,
    max_output_tokens: int | None = None,
    from_results: bool = True,
    timeout: float = 600.0,
) -> str:
    """把逐段结果合并成最终答案；超出预算时自动分层归并（树状 reduce）。

    from_results=false 时直接对分段原文汇总（跳过 map 阶段）。
    budget_tokens 为单次汇总输入的 token 上限，默认等于切分时的 max_tokens。
    """
    meta = store.load(session_id)
    indices = chunk_indices if chunk_indices else store.result_indices(session_id)
    if not indices and from_results:
        raise ValueError("会话尚无逐段结果，请先 run_chunks，或传 from_results=false")
    client = _client(model, base_url, api_key, timeout)
    result = await aggregate_impl(
        store,
        meta,
        indices,
        reduce_prompt,
        client,
        budget_tokens=budget_tokens or None,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        from_results=from_results,
    )
    return _j(result)


@mcp.tool()
async def process(
    map_prompt: str,
    reduce_prompt: str,
    text: str | None = None,
    file_path: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 0,
    overlap_tokens: int = 0,
    tokenizer: str = DEFAULT_TOKENIZER,
    max_output_tokens: int | None = None,
    concurrency: int = 0,
    timeout: float = 600.0,
    encoding: str = "utf-8",
) -> str:
    """一步到位：切分 → 逐段调用 → 汇总，返回最终答案与会话号。"""
    limit = max_tokens or int(env("LONGCTX_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
    content, source = _read_source(text, file_path, encoding)
    result = split_text_impl(content, max_tokens=limit, overlap_tokens=overlap_tokens, tokenizer=tokenizer)
    meta = store.create(result, source=source, max_tokens=limit, overlap_tokens=overlap_tokens)
    client = _client(model, base_url, api_key, timeout)
    summary = await run_map(
        store,
        meta,
        [c.index for c in meta.chunks],
        map_prompt,
        client,
        temperature=0.2,
        max_output_tokens=max_output_tokens,
        concurrency=concurrency or int(env("LONGCTX_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
    )
    if summary["ok"] == 0:
        raise ValueError(f"所有分段调用均失败：{summary['results'][:3]}")
    merged = await aggregate_impl(
        store,
        meta,
        [c.index for c in meta.chunks],
        reduce_prompt,
        client,
        budget_tokens=limit,
        max_output_tokens=max_output_tokens,
        from_results=True,
    )
    return _j(
        {
            "session_id": meta.session_id,
            "chunk_count": len(meta.chunks),
            "total_tokens": meta.total_tokens,
            "map_ok": summary["ok"],
            "map_failed": summary["failed"],
            "final": merged["final"],
            "rounds": merged["rounds"],
            "calls": merged["calls"],
        }
    )


@mcp.tool()
def diagnose() -> str:
    """诊断当前进程实际读到的配置（key 只显示尾 4 位），用于确认重启后环境变量是否生效。"""
    key = env("LONGCTX_API_KEY", "") or ""
    return _j(
        {
            "version": __version__,
            "pid": _PID,
            "started_at": _STARTED_AT,
            "model": env("LONGCTX_MODEL", ""),
            "base_url": env("LONGCTX_BASE_URL", ""),
            "max_tokens_default": env("LONGCTX_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)),
            "concurrency_default": env("LONGCTX_CONCURRENCY", str(DEFAULT_CONCURRENCY)),
            "api_key_present": bool(key),
            "api_key_length": len(key),
            "api_key_tail": key[-4:],
            "data_dir": str(store.root),
        }
    )


@mcp.tool()
def status(session_id: str) -> str:
    """查看会话状态：分段数、已完成结果数、是否已生成最终答案。"""
    meta = store.load(session_id)
    results = store.result_indices(session_id)
    ok = 0
    failed = 0
    for idx in results:
        record = store.read_result(session_id, idx)
        if record and record.get("ok"):
            ok += 1
        else:
            failed += 1
    final_path = store.root / session_id / "final.md"
    return _j(
        {
            **_manifest(meta, include_chunks=False),
            "results_done": ok,
            "results_failed": failed,
            "results_missing": len(meta.chunks) - len(results),
            "has_final": final_path.is_file(),
            "data_dir": str(store.root / session_id),
        }
    )


@mcp.tool()
def list_sessions(limit: int = 20) -> str:
    """列出最近的会话。"""
    return _j({"data_dir": str(store.root), "sessions": store.list_sessions(limit)})


@mcp.tool()
def clear_session(session_id: str) -> str:
    """删除会话（分段、逐段结果、最终答案一并删除）。"""
    store.clear(session_id)
    return _j({"session_id": session_id, "deleted": True})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
