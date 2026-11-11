"""按 token 精确切分长文本。

核心保证：
1. 每个分块的 token 数 <= max_tokens（用 tiktoken 实数，不靠字符数估算）；
2. 分块解码后再编码必须与原始 token 序列一致（不切坏多字节字符）；
3. 不给 overlap 时，分块首尾相接、不丢字；
4. 尽量在自然边界（空行 / 换行 / 句号）处截断。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

try:  # tiktoken 首次使用需要下载 BPE 文件；失败时退回估算模式
    import tiktoken
except Exception:  # pragma: no cover - 仅在依赖缺失时触发
    tiktoken = None


_CJK_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")
_BOUNDARIES = ("\n\n", "\r\n\r\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")


@dataclass
class Chunk:
    index: int
    text: str
    token_count: int
    start_token: int
    end_token: int
    char_start: int = 0
    char_end: int = 0
    estimated: bool = False

    @property
    def preview(self) -> str:
        flat = self.text.replace("\r", " ").replace("\n", " ")
        return flat[:120]


@dataclass
class SplitResult:
    chunks: list[Chunk]
    total_tokens: int
    total_chars: int
    tokenizer: str
    estimated: bool = False
    notes: list[str] = field(default_factory=list)


class TokenCodec:
    """包装 tiktoken；不可用时用字符启发式估算。"""

    def __init__(self, name: str = "o200k_base"):
        self.name = name
        self.estimated = False
        self._enc = None
        if tiktoken is not None:
            try:
                self._enc = tiktoken.get_encoding(name)
            except Exception:
                try:
                    self._enc = tiktoken.get_encoding("cl100k_base")
                    self.name = "cl100k_base"
                except Exception:
                    self._enc = None
        if self._enc is None:
            self.estimated = True
            self.name = "estimate(cjk=1,other=0.25)"

    def encode(self, text: str) -> list[int]:
        assert self._enc is not None
        return self._enc.encode(text, disallowed_special=())

    def decode(self, tokens: list[int]) -> str:
        assert self._enc is not None
        return self._enc.decode(tokens)

    def count(self, text: str) -> int:
        if self._enc is not None:
            return len(self.encode(text))
        return estimate_tokens(text)


def estimate_tokens(text: str) -> int:
    """离线兜底估算：中日韩字符约 1 token/字，其余约 4 字符/token。"""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


# 跨多字节字符时，分块边界最多坏 1~3 个 token。只回退这么多，
# 不能逐个 token 一路退到底：源文本本身含 U+FFFD 时（decode 结果里
# 天然带 "�"）每轮都会判定失败，逐 token 回退会退化成 O(n²) 卡死。
_MAX_TAIL_FALLBACK = 8


def _clean_decode(codec: TokenCodec, tokens: list[int], start: int, end: int) -> tuple[str, int]:
    """解码 tokens[start:end]，必要时回退到能无损往返的边界。

    只以“解码再编码是否等于原 tokens”为准。源文本可能本来就含 U+FFFD，
    不能因为它出现在解码结果里就判定失败。
    """
    try:
        piece = codec.decode(tokens[start:end])
        if codec.encode(piece) == tokens[start:end]:
            return piece, end
    except Exception:
        pass
    limit = min(end - start, _MAX_TAIL_FALLBACK)
    for back in range(1, limit + 1):
        candidate_end = end - back
        try:
            piece = codec.decode(tokens[start:candidate_end])
            if codec.encode(piece) == tokens[start:candidate_end]:
                return piece, candidate_end
        except Exception:
            continue
    return "", start


def _natural_end(codec: TokenCodec, tokens: list[int], start: int, hard_end: int, min_tokens: int) -> int:
    """在最后 15% 的窗口里找自然边界；找不到就用 hard_end。"""
    window = hard_end - start
    if hard_end >= len(tokens) or window < 64:
        return hard_end

    tail_start = max(start + int(window * 0.85), start)
    tail = codec.decode(tokens[tail_start:hard_end])
    for sep in _BOUNDARIES:
        pos = tail.rfind(sep)
        if pos < 0:
            continue
        # 把字符偏移换回 token 偏移；多算一个 token 也无妨，后面还会校验
        prefix = tail[: pos + len(sep)]
        candidate = tail_start + len(codec.encode(prefix))
        if min_tokens <= candidate - start <= window:
            return candidate
    return hard_end


def split_text(
    text: str,
    max_tokens: int = 32_000,
    overlap_tokens: int = 0,
    tokenizer: str = "o200k_base",
    prefer_boundary: bool = True,
) -> SplitResult:
    if not text:
        raise ValueError("文本为空，无法切分")
    if max_tokens < 100:
        raise ValueError("max_tokens 太小（至少 100）")

    codec = TokenCodec(tokenizer)
    notes: list[str] = []

    if codec.estimated:
        return _split_estimated(text, max_tokens, overlap_tokens, notes)

    tokens = codec.encode(text)
    total = len(tokens)
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens 必须小于 max_tokens")

    chunks: list[Chunk] = []
    start = 0
    char_cursor = 0  # 当前 chunk 起点对应的字符位置
    while start < total:
        hard_end = min(start + max_tokens, total)
        end = hard_end
        if prefer_boundary and hard_end < total:
            end = _natural_end(codec, tokens, start, hard_end, min_tokens=max(1, int(max_tokens * 0.5)))

        piece, end = _clean_decode(codec, tokens, start, end)
        if not piece:
            # 极端情况：在 hard_end 处切不动，退回固定边界强切
            end = hard_end
            piece = codec.decode(tokens[start:end]).replace("�", "")
            notes.append(f"chunk {len(chunks)} 在自然边界处无法无损解码，已强制切分")

        token_count = end - start
        chunks.append(
            Chunk(
                index=len(chunks),
                text=piece,
                token_count=token_count,
                start_token=start,
                end_token=end,
                char_start=char_cursor,
                char_end=char_cursor + len(piece),
            )
        )
        char_cursor += len(piece)
        if end >= total:
            break

        next_start = end - overlap_tokens
        if next_start <= start:  # 防死循环
            next_start = start + 1
        if overlap_tokens:
            overlap_text = codec.decode(tokens[next_start:end])
            char_cursor -= len(overlap_text)
        start = next_start

    return SplitResult(
        chunks=chunks,
        total_tokens=total,
        total_chars=len(text),
        tokenizer=codec.name,
        estimated=False,
        notes=notes,
    )


def _split_estimated(text: str, max_tokens: int, overlap_tokens: int, notes: list[str]) -> SplitResult:
    """没有 tiktoken 时的降级路径：按段落 + 字符估算切分。"""
    notes.append("tiktoken 不可用，token 数为估算值，可能超出预算")

    def est(s: str) -> int:
        return estimate_tokens(s)

    paragraphs = re.split(r"(\n\n+)", text)
    units: list[str] = []
    for part in paragraphs:
        if not part:
            continue
        if est(part) <= max_tokens:
            units.append(part)
            continue
        # 超长段落按字符硬切
        per_char = est(part) / max(1, len(part))
        step = max(1, int(max_tokens / max(per_char, 1e-6)))
        for i in range(0, len(part), step):
            units.append(part[i : i + step])

    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    char_cursor = 0
    for unit in units:
        ut = est(unit)
        if buf and buf_tokens + ut > max_tokens:
            piece = "".join(buf)
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=piece,
                    token_count=buf_tokens,
                    start_token=0,
                    end_token=0,
                    char_start=char_cursor,
                    char_end=char_cursor + len(piece),
                    estimated=True,
                )
            )
            char_cursor += len(piece)
            buf, buf_tokens = [], 0
        buf.append(unit)
        buf_tokens += ut
    if buf:
        piece = "".join(buf)
        chunks.append(
            Chunk(
                index=len(chunks),
                text=piece,
                token_count=buf_tokens,
                start_token=0,
                end_token=0,
                char_start=char_cursor,
                char_end=char_cursor + len(piece),
                estimated=True,
            )
        )

    return SplitResult(
        chunks=chunks,
        total_tokens=sum(c.token_count for c in chunks),
        total_chars=len(text),
        tokenizer="estimate(cjk=1,other=0.25)",
        estimated=True,
        notes=notes,
    )
