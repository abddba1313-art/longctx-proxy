"""运行配置：全部来自环境变量，便于在 Cherry Studio / Claude Desktop 里配置。"""

import os
from pathlib import Path

DEFAULT_MAX_TOKENS = 32_000
DEFAULT_TOKENIZER = "o200k_base"
DEFAULT_CONCURRENCY = 4


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def data_dir() -> Path:
    """会话与分块落盘目录。"""
    raw = env("LONGCTX_DATA_DIR")
    if raw:
        path = Path(raw).expanduser()
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
        path = Path(base) / "longctx-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def llm_settings(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[str, str, str]:
    base = base_url or env("LONGCTX_BASE_URL", "https://api.openai.com/v1")
    key = api_key or env("LONGCTX_API_KEY", "")
    name = model or env("LONGCTX_MODEL", "")
    if not name:
        raise ValueError(
            "缺少模型名：请给 tool 传 model，或设置环境变量 LONGCTX_MODEL（例如 deepseek-chat / qwen-plus / gpt-4o-mini）"
        )
    if not key:
        raise ValueError(
            "缺少 API Key：请给 tool 传 api_key，或设置环境变量 LONGCTX_API_KEY"
        )
    return base.rstrip("/"), key, name
