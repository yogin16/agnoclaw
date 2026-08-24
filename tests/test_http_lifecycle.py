"""End-to-end contracts for the authenticated HTTP lifecycle boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Request

from agnoclaw import DeclaredChildTemplate, RemoteHarnessClient, RemoteHarnessError
from agnoclaw.commands import Steer
from agnoclaw.runtime import (
    ChildResultSet,
    ChildRunOutcome,
    ExecutionContext,
    RunNotFoundError,
    RunOutputSegment,
    RunSnapshot,
    RunState,
)
from agnoclaw.runtime.http_lifecycle import build_lifecycle_router
from agnoclaw.runtime.store import RuntimeEvent, decode_event_cursor, encode_event_cursor


class FakeRun:
    def __init__(
        self,
        *,
        run_id: str = "run-1",
        state: RunState = RunState.COMPLETED,
        user_id: str | None = "alice",
    ) -> None:
        self.snapshot = RunSnapshot(
            run_id=run_id,
            state=state,
            revision=2,
            tenant_id="tenant-1",
            user_id=user_id,
            session_id="session-1",
            steering_open=state
            not in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.FAILED_WITH_UNKNOWN_EFFECTS,
                RunState.CANCELLED,
                RunState.EXPIRED,
            },
        )
        self.commands = []
        self.cancel_calls = 0
        self.child_calls = []
        self.child_runs: list[FakeRun] = []
        self._events = [
            RuntimeEvent(
                run_id=run_id,
                sequence=1,
                event_type="run.created",
                payload={"safe": True},
            ),
            RuntimeEvent(
                run_id=run_id,
                sequence=2,
                event_type="run.transitioned",
                payload={"after": state.value},
            ),
        ]
        self._output = [
            RunOutputSegment(
                run_id=run_id,
                segment_sequence=1,
                event_sequence=3,
                cursor=encode_event_cursor(run_id=run_id, sequence=3),
                content="durable ",
                delta_count=2,
                artifact_id="artifact-1",
                occurred_at="2026-08-11T00:00:00+00:00",
            ),
            RunOutputSegment(
                run_id=run_id,
                segment_sequence=2,
                event_sequence=5,
                cursor=encode_event_cursor(run_id=run_id, sequence=5),
                content="output",
                delta_count=1,
                artifact_id="artifact-2",
                occurred_at="2026-08-11T00:00:01+00:00",
            ),
        ]

    async def status(self) -> RunSnapshot:
        return self.snapshot

    async def wait(self) -> Any:
        return {"content": "done"}

    async def events(self, *, after=None, follow=None):
        del follow
        sequence = decode_event_cursor(after, run_id=self.snapshot.run_id) if after else 0
        for event in self._events:
            if event.sequence > sequence:
                yield event

    async def output(self, *, after=None, follow=None):
        del follow
        sequence = decode_event_cursor(after, run_id=self.snapshot.run_id) if after else 0
        for segment in self._output:
            if segment.event_sequence > sequence:
                yield segment

    async def cancel(self) -> RunSnapshot:
        self.cancel_calls += 1
        self.snapshot = RunSnapshot(
            **{
                **self.snapshot.__dict__,
                "state": RunState.CANCELLED,
                "revision": self.snapshot.revision + 1,
                "steering_open": False,
            }
        )
        return self.snapshot

    async def command(self, command) -> None:
        self.commands.append(command)

    async def child(self, child_harness, message, **kwargs):
        self.child_calls.append((child_harness, message, kwargs))
        child = FakeRun(
            run_id=f"child-{len(self.child_runs) + 1}",
            state=RunState.COMPLETED,
            user_id=self.snapshot.user_id,
        )
        child.snapshot = RunSnapshot(
            **{
                **child.snapshot.__dict__,
                "tenant_id": self.snapshot.tenant_id,
                "parent_run_id": self.snapshot.run_id,
                "root_run_id": self.snapshot.root_run_id or self.snapshot.run_id,
                "child_depth": self.snapshot.child_depth + 1,
            }
        )
        self.child_runs.append(child)
        return child

    async def children(self, *, limit=64):
        return tuple(item.snapshot for item in self.child_runs[:limit])

    async def child_results(self, *, limit=64, artifact_limit=16):
        del artifact_limit
        return ChildResultSet(
            parent_run_id=self.snapshot.run_id,
            outcomes=tuple(
                ChildRunOutcome(
                    child_run_id=item.snapshot.run_id,
                    delegation_id=self.child_calls[index][2]["delegation_id"],
                    purpose_code=self.child_calls[index][2]["purpose_code"],
                    state=RunState.COMPLETED,
                    result={"content": "checked"},
                )
                for index, item in enumerate(self.child_runs[:limit])
            ),
        )


class FakeHarness:
    def __init__(self, tmp_path, *, run: FakeRun | None = None) -> None:
        self.workspace = SimpleNamespace(path=tmp_path)
        self._tenant_id = "tenant-1"
        self.user_id = "host-user"
        self.run = run or FakeRun()
        self.starts = []
        self.get_contexts: list[ExecutionContext] = []
        self.raise_on_get: Exception | None = None

    async def start(self, message: str, **kwargs):
        self.starts.append((message, kwargs))
        return self.run

    def get_run(self, run_id: str, *, context: ExecutionContext):
        self.get_contexts.append(context)
        if self.raise_on_get is not None:
            raise self.raise_on_get
        if run_id != self.run.snapshot.run_id or (
            self.run.snapshot.user_id is not None and context.user_id != self.run.snapshot.user_id
        ):
            raise RunNotFoundError(run_id)
        return self.run


class FakeChildHarness:
    _spec = SimpleNamespace(settings_digest="sha256:fake-child-harness")

    async def start(self, _message, **_kwargs):  # pragma: no cover - fake parent owns dispatch
        raise AssertionError


def _app(harness: FakeHarness, *, settings=None, child_templates=None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        profile = request.headers.get("x-test-identity", "anonymous")
        if profile == "root":
            request.state.authenticated = True
            request.state.authorization_enabled = False
        elif profile == "configured-unauthenticated":
            request.state.authorization_enabled = True
        elif profile != "anonymous":
            request.state.authenticated = True
            request.state.authorization_enabled = True
            request.state.user_id = "alice"
            request.state.claims = {"sub": "alice", "tenant_id": "tenant-1"}
            request.state.scopes = (
                ["agents:read"] if profile == "read" else ["agents:read", "agents:run"]
            )
        return await call_next(request)

    app.include_router(
        build_lifecycle_router(
            {"agnoclaw": harness},
            settings=settings,
            child_templates=child_templates,
        )
    )
    return app


def _transport_client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(
        transport=transport,
        base_url="https://agentos.test",
        headers={"x-test-identity": "full"},
    )
    return http_client, RemoteHarnessClient("https://agentos.test", client=http_client)


@pytest.mark.asyncio
async def test_http_lifecycle_round_trips_remote_start_result_events_and_reattach(tmp_path):
    harness = FakeHarness(tmp_path)
    http_client, client = _transport_client(_app(harness))
    async with http_client:
        run = await client.start(
            "work",
            idempotency_key="request-1",
            session_id="session-1",
            user_id="alice",
            metadata={"purpose": "contract"},
            learning_consent=True,
        )
        assert await run.wait() == {"content": "done"}
        events = [event async for event in run.events()]
        output = [segment async for segment in run.output(page_size=1)]
        reattached = await client.get_run("run-1", user_id="alice")
        assert (await reattached.status()).state is RunState.COMPLETED

    assert [event.sequence for event in events] == [1, 2]
    assert [segment.content for segment in output] == ["durable ", "output"]
    assert [segment.segment_sequence for segment in output] == [1, 2]
    message, kwargs = harness.starts[0]
    assert message == "work"
    assert kwargs["idempotency_key"] == "request-1"
    assert kwargs["learning_consent"] is True
    assert kwargs["context"].user_id == "alice"
    assert kwargs["context"].tenant_id == "tenant-1"
    assert kwargs["context"].identity_source.value == "authenticated_claims"
    assert kwargs["metadata"] == {"purpose": "contract"}
    assert kwargs["persist_output"] is True


@pytest.mark.asyncio
async def test_http_lifecycle_starts_and_collects_only_registered_child_templates(tmp_path):
    harness = FakeHarness(tmp_path, run=FakeRun(state=RunState.RUNNING))
    child_harness = FakeChildHarness()
    template = DeclaredChildTemplate(
        name="research",
        child_harness=child_harness,
        purpose_code="research",
        max_inline_result_chars=256,
    )
    app = _app(
        harness,
        child_templates={"agnoclaw": {"research": template}},
    )
    http_client, client = _transport_client(app)
    async with http_client:
        parent = await client.get_run("run-1", user_id="alice")
        child = await parent.child(
            "research",
            "inspect evidence",
            delegation_id="research-1",
        )
        children = await parent.children()
        results = await parent.child_results(max_inline_result_chars=256)

    assert child.run_id == "child-1"
    assert children[0].parent_run_id == "run-1"
    assert children[0].child_depth == 1
    assert results.parent_run_id == "run-1"
    assert results.outcomes[0].delegation_id == "research-1"
    assert results.outcomes[0].result == {"content": "checked"}
    _child_harness, task, declaration = harness.run.child_calls[0]
    assert task == "inspect evidence"
    assert declaration["budget"] == template.budget
    assert declaration["learning_allowed"] is False


@pytest.mark.asyncio
async def test_http_lifecycle_hides_unregistered_child_templates(tmp_path):
    harness = FakeHarness(tmp_path, run=FakeRun(state=RunState.RUNNING))
    http_client, client = _transport_client(_app(harness))
    async with http_client:
        parent = await client.get_run("run-1", user_id="alice")
        with pytest.raises(RemoteHarnessError) as hidden:
            await parent.child("unknown", "work", delegation_id="work-1")
    assert getattr(hidden.value, "code", None) == "CHILD_TEMPLATE_NOT_FOUND"
    assert harness.run.child_calls == []


@pytest.mark.asyncio
async def test_http_lifecycle_enforces_template_specific_scope_before_dispatch(tmp_path):
    harness = FakeHarness(tmp_path, run=FakeRun(state=RunState.RUNNING))
    template = DeclaredChildTemplate(
        name="privileged_research",
        child_harness=FakeChildHarness(),
        purpose_code="research",
        required_scopes=("research:delegate",),
    )
    app = _app(
        harness,
        child_templates={"agnoclaw": {template.name: template}},
    )
    http_client, client = _transport_client(app)
    async with http_client:
        parent = await client.get_run("run-1", user_id="alice")
        with pytest.raises(RemoteHarnessError) as denied:
            await parent.child(template.name, "work", delegation_id="work-1")
    assert denied.value.code == "LIFECYCLE_SCOPE_REQUIRED"
    assert harness.run.child_calls == []


@pytest.mark.asyncio
async def test_http_lifecycle_fails_closed_and_enforces_read_run_scopes(tmp_path):
    harness = FakeHarness(tmp_path)
    app = _app(harness)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agentos.test"
    ) as client:
        anonymous = await client.get("/agnoclaw/v1/harnesses/agnoclaw/runs/run-1")
        read_only_start = await client.post(
            "/agnoclaw/v1/harnesses/agnoclaw/runs",
            headers={"x-test-identity": "read"},
            json={"protocol_version": "1.0", "message": "work"},
        )
        readable = await client.get(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1",
            headers={"x-test-identity": "read"},
        )
        configured_but_unauthenticated = await client.get(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1",
            headers={"x-test-identity": "configured-unauthenticated"},
        )

    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "LIFECYCLE_AUTHENTICATION_REQUIRED"
    assert read_only_start.status_code == 403
    assert read_only_start.json()["error"]["code"] == "LIFECYCLE_SCOPE_REQUIRED"
    assert readable.status_code == 200
    assert configured_but_unauthenticated.status_code == 401
    assert (
        configured_but_unauthenticated.json()["error"]["code"]
        == "LIFECYCLE_AUTHENTICATION_REQUIRED"
    )
    assert harness.starts == []


@pytest.mark.asyncio
async def test_http_lifecycle_claims_override_body_identity_and_hide_wrong_owner(tmp_path):
    harness = FakeHarness(tmp_path, run=FakeRun(user_id="bob"))
    app = _app(harness)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://agentos.test",
        headers={"x-test-identity": "full"},
    ) as client:
        conflict = await client.post(
            "/agnoclaw/v1/harnesses/agnoclaw/runs",
            json={"protocol_version": "1.0", "message": "work", "user_id": "bob"},
        )
        hidden = await client.get("/agnoclaw/v1/harnesses/agnoclaw/runs/run-1")

    assert conflict.status_code == 403
    assert conflict.json()["error"]["code"] == "IDENTITY_CLAIM_CONFLICT"
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert harness.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"protocol_version": "2.0", "message": "work"},
        {"protocol_version": "1.0", "message": "work", "unknown": True},
        {
            "protocol_version": "1.0",
            "message": "work",
            "metadata": {"claims": {"sub": "mallory"}},
        },
        {
            "protocol_version": "1.0",
            "message": "work",
            "options": {"context": {"user_id": "mallory"}},
        },
        {"protocol_version": "1.0", "message": "work", "learning_consent": "yes"},
        {"protocol_version": "1.0", "message": "work", "persist_output": "yes"},
    ],
)
async def test_http_lifecycle_rejects_unknown_protected_and_malformed_start_fields(tmp_path, body):
    harness = FakeHarness(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(harness)),
        base_url="https://agentos.test",
        headers={"x-test-identity": "full"},
    ) as client:
        response = await client.post("/agnoclaw/v1/harnesses/agnoclaw/runs", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "LIFECYCLE_REQUEST_INVALID"
    assert harness.starts == []


@pytest.mark.asyncio
async def test_http_lifecycle_rejects_oversize_and_invalid_json_before_dispatch(tmp_path):
    harness = FakeHarness(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(harness)),
        base_url="https://agentos.test",
        headers={"x-test-identity": "full"},
    ) as client:
        oversize = await client.post(
            "/agnoclaw/v1/harnesses/agnoclaw/runs",
            content=json.dumps({"protocol_version": "1.0", "message": "x"}),
            headers={"content-length": "1048577"},
        )
        invalid = await client.post(
            "/agnoclaw/v1/harnesses/agnoclaw/runs",
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )

    assert oversize.status_code == 400
    assert invalid.status_code == 400
    assert harness.starts == []


@pytest.mark.asyncio
async def test_http_lifecycle_wraps_agentos_auth_and_query_validation_in_protocol(tmp_path):
    from agno.os.settings import AgnoAPISettings

    harness = FakeHarness(tmp_path)
    app = _app(harness, settings=AgnoAPISettings(os_security_key="secret"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agentos.test"
    ) as client:
        missing = await client.get("/agnoclaw/v1/harnesses/agnoclaw/runs/run-1")
        invalid = await client.get(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1",
            headers={"authorization": "Bearer wrong"},
        )
        malformed_limit = await client.get(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1/events?limit=not-an-int",
            headers={"authorization": "Bearer secret"},
        )
        oversized_output_page = await client.get(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1/output?limit=51",
            headers={"authorization": "Bearer secret"},
        )
        valid = await client.post(
            "/agnoclaw/v1/harnesses/agnoclaw/runs",
            headers={"authorization": "Bearer secret"},
            json={"protocol_version": "1.0", "message": "work"},
        )

    for response in (missing, invalid):
        assert response.status_code == 401
        assert response.json()["kind"] == "error"
        assert response.json()["error"]["code"] == "LIFECYCLE_AUTHENTICATION_REQUIRED"
    assert malformed_limit.status_code == 400
    assert malformed_limit.json()["error"]["code"] == "LIFECYCLE_REQUEST_INVALID"
    assert oversized_output_page.status_code == 400
    assert oversized_output_page.json()["error"]["code"] == "LIFECYCLE_REQUEST_INVALID"
    assert valid.status_code == 202


@pytest.mark.asyncio
async def test_http_lifecycle_commands_cancel_root_identity_and_safe_failures(tmp_path):
    harness = FakeHarness(tmp_path)
    app = _app(harness)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://agentos.test",
        headers={"x-test-identity": "full"},
    ) as client:
        command = await client.post(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1/commands",
            json={
                "schema_version": "1.0",
                "command_type": "steer",
                "command_id": "command-1",
                "instruction": "focus",
            },
        )
        cancelled = await client.post("/agnoclaw/v1/harnesses/agnoclaw/runs/run-1/cancel")
        harness.run.snapshot = RunSnapshot(
            run_id="run-1",
            state=RunState.COMPLETED,
            user_id="host-user",
        )
        root = await client.get(
            "/agnoclaw/v1/harnesses/agnoclaw/runs/run-1",
            headers={"x-test-identity": "root"},
        )
        harness.raise_on_get = RuntimeError("private database connection detail")
        failed = await client.get("/agnoclaw/v1/harnesses/agnoclaw/runs/run-1")

    assert command.status_code == 200
    assert harness.run.commands == [Steer("focus", command_id="command-1")]
    assert cancelled.status_code == 200
    assert harness.run.cancel_calls == 1
    assert root.status_code == 200
    assert harness.get_contexts[-2].identity_source.value == "trusted_host"
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "LIFECYCLE_INTERNAL_ERROR"
    assert "private" not in failed.text
