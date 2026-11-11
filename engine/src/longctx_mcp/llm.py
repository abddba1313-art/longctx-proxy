"""最小 OpenAI 兼容客户端：/chat/completions，带重试与并发控制。"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 600.0,
        max_retries: int = 3,
        extra_body: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra_body = extra_body or {}

    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> Completion:
        url = f"{self.base_url}/chat/completions"
        payload: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if self.extra_body:
            payload.update(self.extra_body)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = ""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                        await self._sleep(attempt)
                        continue
                    if resp.status_code >= 400:
                        body = resp.text[:500]
                        raise LLMError(f"模型接口返回 {resp.status_code}: {body}")
                    data = resp.json()
                    return self._parse(data)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self.max_retries:
                        await self._sleep(attempt)
                        continue
                except LLMError:
                    raise
        raise LLMError(f"模型调用失败（已重试 {self.max_retries} 次）：{last_error}")

    def _parse(self, data: dict) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"模型返回缺少 choices：{str(data)[:300]}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not text and message.get("reasoning_content"):
            text = message["reasoning_content"]
        if isinstance(text, list):  # 部分服务返回 content 数组
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    @staticmethod
    async def _sleep(attempt: int) -> None:
        await asyncio.sleep((2**attempt) + random.uniform(0, 0.5))


def make_semaphore(limit: int) -> asyncio.Semaphore:
    return asyncio.Semaphore(max(1, limit))
