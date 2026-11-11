"""会话存储：分块、逐段结果、最终答案全部落盘，重启服务后可继续使用。"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir
from .splitter import SplitResult


@dataclass
class ChunkMeta:
    index: int
    token_count: int
    char_count: int
    start_token: int
    end_token: int
    char_start: int
    char_end: int
    preview: str
    estimated: bool = False


@dataclass
class SessionMeta:
    session_id: str
    created_at: str
    source: str
    tokenizer: str
    max_tokens: int
    overlap_tokens: int
    total_tokens: int
    total_chars: int
    estimated: bool
    notes: list[str] = field(default_factory=list)
    chunks: list[ChunkMeta] = field(default_factory=list)


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SessionStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or data_dir()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 会话 ----------

    def create(
        self,
        result: SplitResult,
        source: str,
        max_tokens: int,
        overlap_tokens: int,
        session_id: str | None = None,
    ) -> SessionMeta:
        sid = session_id or self._new_id()
        if not _SESSION_ID_RE.match(sid):
            raise ValueError("session_id 只允许字母、数字、下划线和短横线（最长 64）")
        path = self.root / sid
        if path.exists():
            raise ValueError(f"session_id 已存在：{sid}")
        path.mkdir(parents=True)

        metas = [
            ChunkMeta(
                index=c.index,
                token_count=c.token_count,
                char_count=len(c.text),
                start_token=c.start_token,
                end_token=c.end_token,
                char_start=c.char_start,
                char_end=c.char_end,
                preview=c.preview,
                estimated=c.estimated,
            )
            for c in result.chunks
        ]
        meta = SessionMeta(
            session_id=sid,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source=source,
            tokenizer=result.tokenizer,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            total_tokens=result.total_tokens,
            total_chars=result.total_chars,
            estimated=result.estimated,
            notes=result.notes,
            chunks=metas,
        )
        self._write_json(path / "meta.json", asdict(meta))
        for chunk in result.chunks:
            (path / f"chunk_{chunk.index:04d}.txt").write_text(chunk.text, encoding="utf-8")
        return meta

    def load(self, session_id: str) -> SessionMeta:
        path = self._session_path(session_id)
        data = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        data["chunks"] = [ChunkMeta(**c) for c in data.get("chunks", [])]
        return SessionMeta(**data)

    def list_sessions(self, limit: int = 50) -> list[dict]:
        sessions = []
        for path in self.root.iterdir():
            meta_file = path / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            sessions.append(
                {
                    "session_id": meta.get("session_id", path.name),
                    "created_at": meta.get("created_at", ""),
                    "source": meta.get("source", ""),
                    "chunks": len(meta.get("chunks", [])),
                    "total_tokens": meta.get("total_tokens", 0),
                }
            )
        sessions.sort(key=lambda s: s["created_at"], reverse=True)
        return sessions[:limit]

    def clear(self, session_id: str) -> None:
        path = self._session_path(session_id)
        shutil.rmtree(path)

    # ---------- 分块 ----------

    def read_chunk(self, session_id: str, index: int) -> str:
        meta = self.load(session_id)
        if not (0 <= index < len(meta.chunks)):
            raise ValueError(f"chunk 索引越界：{index}（共 {len(meta.chunks)} 块）")
        return (self._session_path(session_id) / f"chunk_{index:04d}.txt").read_text(encoding="utf-8")

    # ---------- 逐段结果 ----------

    def save_result(self, session_id: str, index: int, payload: dict) -> None:
        path = self._session_path(session_id) / f"result_{index:04d}.json"
        self._write_json(path, payload)

    def read_result(self, session_id: str, index: int) -> dict | None:
        path = self._session_path(session_id) / f"result_{index:04d}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def result_indices(self, session_id: str) -> list[int]:
        out = []
        for path in self._session_path(session_id).glob("result_*.json"):
            try:
                out.append(int(path.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        return sorted(out)

    def save_final(self, session_id: str, text: str, meta: dict | None = None) -> Path:
        path = self._session_path(session_id) / "final.md"
        path.write_text(text, encoding="utf-8")
        if meta is not None:
            self._write_json(self._session_path(session_id) / "final_meta.json", meta)
        return path

    # ---------- 内部 ----------

    def _session_path(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id or ""):
            raise ValueError("非法 session_id")
        path = self.root / session_id
        if not (path / "meta.json").is_file():
            raise ValueError(f"会话不存在：{session_id}")
        return path

    def _new_id(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
