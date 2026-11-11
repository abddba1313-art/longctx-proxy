"""逐段调用 + 汇总。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .llm import LLMClient
from .splitter import TokenCodec, estimate_tokens, split_text
from .store import SessionMeta, SessionStore

INTERMEDIATE_PREFIX = (
    "以下是同一份长文档若干分段的分析结果。请把它们压缩合并成一段笔记，"
    "保留所有关键事实、数字、结论和分段编号线索，不要遗漏相互矛盾之处：\n\n"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def render_map_prompt(template: str, index: int, total: int, source: str, chunk: str) -> str:
    if "{chunk}" not in template:
        template = template.rstrip() + "\n\n{chunk}"
    return (
        template.replace("{index}", str(index))
        .replace("{total}", str(total))
        .replace("{source}", source)
        .replace("{chunk}", chunk)
    )


def codec_for(meta: SessionMeta) -> TokenCodec:
    if meta.estimated or meta.tokenizer.startswith("estimate"):
        codec = TokenCodec(meta.tokenizer if meta.tokenizer in ("o200k_base", "cl100k_base") else "o200k_base")
        return codec
    return TokenCodec(meta.tokenizer)


def count_tokens(codec: TokenCodec, text: str) -> int:
    if codec.estimated:
        return estimate_tokens(text)
    return codec.count(text)


async def run_map(
    store: SessionStore,
    meta: SessionMeta,
    indices: list[int],
    prompt_template: str,
    client: LLMClient,
    temperature: float = 0.2,
    max_output_tokens: int | dict[int, int] | None = None,
    concurrency: int = 4,
    skip_existing: bool = False,
) -> dict:
    total = len(meta.chunks)
    sem = asyncio.Semaphore(max(1, concurrency))
    records: list[dict] = []

    def _cap(idx: int) -> int | None:
        """支持按段给不同预算（查询感知分配）；整数时所有段共用。"""
        if isinstance(max_output_tokens, dict):
            return max_output_tokens.get(idx)
        return max_output_tokens

    async def one(idx: int) -> None:
        if skip_existing and store.read_result(meta.session_id, idx):
            records.append({"index": idx, "ok": True, "skipped": True})
            return
        chunk = store.read_chunk(meta.session_id, idx)
        prompt = render_map_prompt(prompt_template, idx, total, meta.source, chunk)
        async with sem:
            try:
                completion = await client.complete(
                    [{"role": "user", "content": prompt}],
                    temperature=temperature,
                    max_tokens=_cap(idx),
                )
                payload = {
                    "index": idx,
                    "ok": True,
                    "content": completion.text,
                    "model": completion.model,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "finished_at": _now(),
                }
                store.save_result(meta.session_id, idx, payload)
                records.append({"index": idx, "ok": True, "chars": len(completion.text)})
            except Exception as exc:  # 单块失败不拖垮整批
                payload = {"index": idx, "ok": False, "error": str(exc), "finished_at": _now()}
                store.save_result(meta.session_id, idx, payload)
                records.append({"index": idx, "ok": False, "error": str(exc)})

    await asyncio.gather(*(one(i) for i in indices))
    records.sort(key=lambda r: r["index"])
    ok = [r for r in records if r.get("ok")]
    return {
        "session_id": meta.session_id,
        "requested": len(indices),
        "ok": len(ok),
        "failed": len(records) - len(ok),
        "skipped": sum(1 for r in records if r.get("skipped")),
        "results": records,
    }


def _collect_items(
    store: SessionStore,
    meta: SessionMeta,
    indices: list[int],
    from_results: bool,
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for idx in indices:
        if from_results:
            record = store.read_result(meta.session_id, idx)
            if not record or not record.get("ok"):
                continue
            items.append((f"分段 {idx}", record.get("content", "")))
        else:
            items.append((f"分段 {idx}", store.read_chunk(meta.session_id, idx)))
    if not items:
        raise ValueError("没有可用于汇总的内容：请先调用 run_chunks 生成逐段结果，或改用 from_results=false")
    return items


def _format_items(items: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"<<<{label}>>>\n{content}" for label, content in items)


def _batch_by_tokens(
    codec: TokenCodec,
    items: list[tuple[str, str]],
    budget: int,
) -> list[list[tuple[str, str]]]:
    """贪心装箱；单个结果超预算时先按 token 切开。"""
    flat: list[tuple[str, str]] = []
    for label, content in items:
        if count_tokens(codec, content) <= budget:
            flat.append((label, content))
            continue
        inner = split_text(content, max_tokens=budget, overlap_tokens=0, prefer_boundary=True)
        for part in inner.chunks:
            flat.append((f"{label}（续）", part.text))

    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_tokens = 0
    for item in flat:
        cost = count_tokens(codec, item[1]) + 16
        if current and current_tokens + cost > budget:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += cost
    if current:
        batches.append(current)
    return batches


async def aggregate(
    store: SessionStore,
    meta: SessionMeta,
    indices: list[int],
    reduce_prompt: str,
    client: LLMClient,
    budget_tokens: int | None = None,
    temperature: float = 0.2,
    max_output_tokens: int | None = None,
    from_results: bool = True,
    max_rounds: int = 8,
) -> dict:
    codec = codec_for(meta)
    budget = budget_tokens or max(1000, meta.max_tokens)
    items = _collect_items(store, meta, indices, from_results)

    rounds = 0
    calls = 0
    while True:
        combined = _format_items(items)
        if count_tokens(codec, combined) + count_tokens(codec, reduce_prompt) <= budget:
            prompt = reduce_prompt.rstrip() + "\n\n" + combined
            completion = await client.complete(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
            calls += 1
            final = completion.text
            store.save_final(
                meta.session_id,
                final,
                {
                    "model": completion.model,
                    "rounds": rounds,
                    "calls": calls,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "from_results": from_results,
                    "finished_at": _now(),
                },
            )
            return {
                "session_id": meta.session_id,
                "final": final,
                "rounds": rounds,
                "calls": calls,
                "output_file": str(store.root / meta.session_id / "final.md"),
            }

        if rounds >= max_rounds:
            raise ValueError(f"汇总层数超过 {max_rounds} 层仍未收敛，请调大 budget_tokens 或减少分段")

        batches = _batch_by_tokens(codec, items, budget)
        merged: list[tuple[str, str]] = []
        for batch in batches:
            prompt = INTERMEDIATE_PREFIX + _format_items(batch)
            completion = await client.complete(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
            calls += 1
            merged.append((f"合并笔记 {len(merged)}", completion.text))
        items = merged
        rounds += 1
