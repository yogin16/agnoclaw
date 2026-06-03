"""RemoteHarnessClient tests."""

from __future__ import annotations

import httpx
import pytest

from agnoclaw.remote import RemoteHarnessClient, _parse_sse_line


@pytest.mark.asyncio
async def test_remote_harness_client_posts_agentos_run():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"content": "ok", "run_id": "run-1"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://agents.example.com",
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        run = await client.arun(
            "hello",
            session_id="sess-1",
            user_id="user-1",
            metadata={"tenant": "t1"},
        )

    assert run.result == {"content": "ok", "run_id": "run-1"}
    assert captured["url"] == "https://agents.example.com/agents/agnoclaw/runs"
    assert "message=hello" in captured["body"]
    assert "session_id=sess-1" in captured["body"]
    assert "user_id=user-1" in captured["body"]


@pytest.mark.asyncio
async def test_remote_harness_client_streams_sse_events():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='data: {"event":"RunContent","content":"hi"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://agents.example.com",
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        run = await client.arun("hello", stream=True)
        events = [event async for event in run.events()]

    assert events == [{"event": "RunContent", "content": "hi"}]


def test_parse_sse_line_handles_non_json_data():
    assert _parse_sse_line("data: hello") == {"content": "hello"}
