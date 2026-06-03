"""Remote client helpers for AgentOS-exported agnoclaw harnesses."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class RemoteHarnessRun:
    """Result wrapper returned by RemoteHarnessClient."""

    result: dict[str, Any] | None = None
    _events: AsyncIterator[dict[str, Any]] | None = None

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if self._events is None:
            return
        async for event in self._events:
            yield event


class RemoteHarnessClient:
    """Small agnoclaw-shaped client for AgentOS agent run endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        agent_id: str = "agnoclaw",
        api_key: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._owns_client = client is None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    async def arun(
        self,
        message: str,
        *,
        stream: bool = False,
        agent_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RemoteHarnessRun:
        target_agent = agent_id or self.agent_id
        path = f"/agents/{target_agent}/runs"
        data: dict[str, Any] = {
            "message": message,
            "stream": stream,
        }
        if session_id is not None:
            data["session_id"] = session_id
        if user_id is not None:
            data["user_id"] = user_id
        if metadata is not None:
            data["metadata"] = json.dumps(metadata)
        for key, value in kwargs.items():
            if value is not None:
                data[key] = json.dumps(value) if isinstance(value, (dict, list)) else value

        if stream:
            return RemoteHarnessRun(_events=self._stream_events(path, data))

        response = await self._client.post(path, data=data)
        response.raise_for_status()
        return RemoteHarnessRun(result=response.json())

    async def _stream_events(
        self,
        path: str,
        data: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream("POST", path, data=data) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                event = _parse_sse_line(line)
                if event is not None:
                    yield event

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> RemoteHarnessClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text or not text.startswith("data:"):
        return None
    payload = text.removeprefix("data:").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {"content": payload}
    return parsed if isinstance(parsed, dict) else {"data": parsed}
