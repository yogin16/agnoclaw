"""RemoteHarnessClient tests."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agnoclaw import LIFECYCLE_PROTOCOL_VERSION, RemoteHarnessError
from agnoclaw.commands import Steer
from agnoclaw.remote import RemoteHarnessClient, RemoteHarnessRun, _parse_sse_line
from agnoclaw.runtime import (
    LifecycleProtocolError,
    RunReconciliationRequiredError,
    RunState,
)
from agnoclaw.runtime.run_handle import RunWaitError
from agnoclaw.runtime.store import encode_event_cursor


def _snapshot(run_id: str = "run-1", *, state: str = "running", revision: int = 1):
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "state": state,
        "revision": revision,
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "created_at": "2026-08-10T00:00:00+00:00",
        "updated_at": "2026-08-10T00:00:01+00:00",
        "steering_open": state not in {"completed", "failed", "cancelled", "expired"},
        "pending_request_id": None,
        "last_transition_id": "transition-1",
        "last_reason_code": None,
        "metadata": {"safe": True},
    }


def _envelope(kind: str, **payload):
    return {
        "protocol_version": LIFECYCLE_PROTOCOL_VERSION,
        "kind": kind,
        **payload,
    }


def _event(sequence: int, *, run_id: str = "run-1"):
    return {
        "schema_version": "1.0",
        "event_id": f"event-{sequence}",
        "run_id": run_id,
        "sequence": sequence,
        "event_type": "run.transitioned",
        "occurred_at": "2026-08-10T00:00:01+00:00",
        "attempt_id": None,
        "payload": {"sequence": sequence},
    }


def _segment(segment_sequence: int, event_sequence: int, *, run_id: str = "run-1"):
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "segment_sequence": segment_sequence,
        "event_sequence": event_sequence,
        "cursor": encode_event_cursor(run_id=run_id, sequence=event_sequence),
        "content": f"segment-{segment_sequence}",
        "delta_count": 1,
        "artifact_id": f"artifact-{segment_sequence}",
        "occurred_at": "2026-08-10T00:00:01+00:00",
    }


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


@pytest.mark.asyncio
async def test_lifecycle_start_uses_versioned_authenticated_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(202, json=_envelope("run", run=_snapshot()))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://agents.example.com",
    ) as http_client:
        client = RemoteHarnessClient(
            "https://agents.example.com",
            api_key="secret",
            client=http_client,
        )
        run = await client.start(
            "do the work",
            idempotency_key="request-1",
            session_id="session-1",
            user_id="user-1",
            metadata={"purpose": "test"},
            learning_consent=True,
            stream_events=True,
        )

    assert run.id == "run-1"
    assert run.session_id == "session-1"
    assert captured == {
        "path": "/agnoclaw/v1/harnesses/agnoclaw/runs",
        "authorization": "Bearer secret",
        "payload": {
            "protocol_version": "1.0",
            "message": "do the work",
            "idempotency_key": "request-1",
            "session_id": "session-1",
            "user_id": "user-1",
            "metadata": {"purpose": "test"},
            "learning_consent": True,
            "persist_output": True,
            "options": {"stream_events": True},
        },
    }


@pytest.mark.asyncio
async def test_remote_run_reattaches_polls_result_and_preserves_identity():
    result_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_calls
        assert request.url.params.get("user_id") == "user-1"
        if request.url.path.endswith("/result"):
            result_calls += 1
            if result_calls == 1:
                return httpx.Response(
                    200,
                    json=_envelope(
                        "result",
                        run=_snapshot(),
                        ready=False,
                        blocked=False,
                        result=None,
                        error=None,
                    ),
                )
            return httpx.Response(
                200,
                json=_envelope(
                    "result",
                    run=_snapshot(state="completed", revision=2),
                    ready=True,
                    blocked=False,
                    result={"content": "done"},
                    error=None,
                ),
            )
        return httpx.Response(200, json=_envelope("run", run=_snapshot()))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        run = await client.get_run("run-1", user_id="user-1")
        assert (await run.status()).state is RunState.RUNNING
        assert await run.wait(poll_interval=0.001) == {"content": "done"}

    assert run.result == {"content": "done"}
    assert result_calls == 2


@pytest.mark.asyncio
async def test_remote_wait_timeout_and_caller_cancellation_never_cancel_run():
    methods_and_paths = []
    first_poll = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        methods_and_paths.append((request.method, request.url.path))
        first_poll.set()
        return httpx.Response(
            200,
            json=_envelope(
                "result",
                run=_snapshot(),
                ready=False,
                blocked=False,
                result=None,
                error=None,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="agnoclaw")
        with pytest.raises(TimeoutError):
            await handle.wait(timeout=0)
        waiter = asyncio.create_task(handle.wait(poll_interval=1))
        await first_poll.wait()
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    assert not any(path.endswith("/cancel") for _, path in methods_and_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_type", "code"),
    [
        (
            _envelope(
                "result",
                run=_snapshot(state="waiting_for_reconciliation"),
                ready=False,
                blocked=True,
                result=None,
                error={"code": "RUN_RECONCILIATION_REQUIRED"},
            ),
            RunReconciliationRequiredError,
            "RUN_RECONCILIATION_REQUIRED",
        ),
        (
            _envelope(
                "result",
                run=_snapshot(state="failed"),
                ready=True,
                blocked=False,
                result=None,
                error={
                    "code": "RUN_FAILED",
                    "message": "Run failed safely.",
                    "safe_error": {"type": "model"},
                },
            ),
            RunWaitError,
            "RUN_FAILED",
        ),
    ],
)
async def test_remote_wait_maps_reconciliation_and_terminal_failures(payload, error_type, code):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="agnoclaw")
        with pytest.raises(error_type) as exc:
            await handle.wait()

    assert exc.value.code == code


@pytest.mark.asyncio
async def test_remote_events_are_gap_free_paged_and_cursor_resumable():
    cursor_2 = encode_event_cursor(run_id="run-1", sequence=2)
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        pages += 1
        assert request.url.params.get("limit") == "2"
        if pages == 1:
            assert request.url.params.get("after") is None
            return httpx.Response(
                200,
                json=_envelope(
                    "events",
                    run=_snapshot(),
                    events=[_event(1), _event(2)],
                    next_cursor=cursor_2,
                ),
            )
        assert request.url.params.get("after") == cursor_2
        return httpx.Response(
            200,
            json=_envelope(
                "events",
                run=_snapshot(state="completed"),
                events=[],
                next_cursor=cursor_2,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="agnoclaw")
        events = [event async for event in handle.events(page_size=2)]

    assert [event.sequence for event in events] == [1, 2]
    assert pages == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _envelope(
            "events",
            run=_snapshot(),
            events=[_event(2)],
            next_cursor=encode_event_cursor(run_id="run-1", sequence=2),
        ),
        _envelope(
            "events",
            run=_snapshot(),
            events=[_event(1)],
            next_cursor=encode_event_cursor(run_id="run-1", sequence=2),
        ),
        _envelope(
            "events",
            run=_snapshot(),
            events=[],
            next_cursor=encode_event_cursor(run_id="run-1", sequence=0),
        ),
    ],
)
async def test_remote_events_reject_gaps_and_inconsistent_cursors(response):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="agnoclaw")
        with pytest.raises(LifecycleProtocolError):
            _ = [event async for event in handle.events(follow=False)]


@pytest.mark.asyncio
async def test_remote_output_is_bounded_paged_and_cursor_resumable():
    cursor_3 = encode_event_cursor(run_id="run-1", sequence=3)
    cursor_5 = encode_event_cursor(run_id="run-1", sequence=5)
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        pages += 1
        assert request.url.path.endswith("/runs/run-1/output")
        assert request.url.params.get("limit") == "1"
        if pages == 1:
            assert request.url.params.get("after") is None
            return httpx.Response(
                200,
                json=_envelope(
                    "output",
                    run=_snapshot(),
                    segments=[_segment(1, 3)],
                    next_cursor=cursor_3,
                ),
            )
        if pages == 2:
            assert request.url.params.get("after") == cursor_3
            return httpx.Response(
                200,
                json=_envelope(
                    "output",
                    run=_snapshot(),
                    segments=[_segment(2, 5)],
                    next_cursor=cursor_5,
                ),
            )
        assert request.url.params.get("after") == cursor_5
        return httpx.Response(
            200,
            json=_envelope(
                "output",
                run=_snapshot(state="completed"),
                segments=[],
                next_cursor=cursor_5,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="agnoclaw")
        segments = [segment async for segment in handle.output(page_size=1)]

    assert [segment.content for segment in segments] == ["segment-1", "segment-2"]
    assert pages == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments,next_cursor",
    [
        (
            [{**_segment(1, 3), "cursor": encode_event_cursor(run_id="run-1", sequence=4)}],
            encode_event_cursor(run_id="run-1", sequence=4),
        ),
        (
            [_segment(1, 3), _segment(3, 5)],
            encode_event_cursor(run_id="run-1", sequence=5),
        ),
        (
            [{**_segment(1, 3), "content": "x" * 8_193}],
            encode_event_cursor(run_id="run-1", sequence=3),
        ),
    ],
)
async def test_remote_output_rejects_cursor_gap_and_content_bound_drift(
    segments,
    next_cursor,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_envelope(
                "output",
                run=_snapshot(state="completed"),
                segments=segments,
                next_cursor=next_cursor,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="agnoclaw")
        with pytest.raises(LifecycleProtocolError):
            _ = [segment async for segment in handle.output(follow=False)]


@pytest.mark.asyncio
async def test_remote_cancel_and_command_use_canonical_routes_and_verify_identity():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (request.url.path, json.loads(request.content) if request.content else None)
        )
        return httpx.Response(200, json=_envelope("run", run=_snapshot()))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        handle = RemoteHarnessRun(run_id="run-1", _client=client, _harness_id="custom")
        await handle.cancel()
        await handle.command(Steer("focus", command_id="command-1"))

    assert requests == [
        ("/agnoclaw/v1/harnesses/custom/runs/run-1/cancel", None),
        (
            "/agnoclaw/v1/harnesses/custom/runs/run-1/commands",
            {
                "schema_version": "1.0",
                "command_type": "steer",
                "command_id": "command-1",
                "instruction": "focus",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_remote_rejects_identity_change_malformed_errors_and_non_json():
    responses = iter(
        [
            httpx.Response(200, json=_envelope("run", run=_snapshot("other-run"))),
            httpx.Response(
                403,
                json=_envelope(
                    "error",
                    error={
                        "code": "DENIED",
                        "category": "authorization",
                        "message": "Denied.",
                        "retryable": "false",
                        "details": None,
                    },
                ),
            ),
            httpx.Response(200, text="not-json"),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://agents.example.com"
    ) as http_client:
        client = RemoteHarnessClient("https://agents.example.com", client=http_client)
        with pytest.raises(LifecycleProtocolError, match="identity"):
            await client.get_run("run-1")
        with pytest.raises(RemoteHarnessError) as malformed:
            await client.get_run("run-1")
        assert malformed.value.code == "REMOTE_LIFECYCLE_HTTP_ERROR"
        with pytest.raises(LifecycleProtocolError, match="not JSON"):
            await client.get_run("run-1")


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://agents.example.com",
        "https://user:secret@agents.example.com",
        "https://agents.example.com/api",
        "https://agents.example.com?tenant=one",
    ],
)
def test_remote_rejects_ambiguous_base_urls(base_url):
    with pytest.raises(ValueError, match="origin"):
        RemoteHarnessClient(base_url)


@pytest.mark.parametrize("identifier", ["", "two/parts", "has space", "query?value", "#fragment"])
def test_remote_rejects_unsafe_lifecycle_path_identifiers(identifier):
    with pytest.raises(ValueError, match="path-segment"):
        RemoteHarnessClient._lifecycle_path(identifier, "runs")
    with pytest.raises(ValueError, match="path segments"):
        RemoteHarnessClient._lifecycle_path("agnoclaw", "runs", identifier)
