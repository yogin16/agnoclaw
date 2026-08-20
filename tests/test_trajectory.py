"""Content-minimized authoritative trajectory projection contracts."""

from __future__ import annotations

import json

from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.events import build_event
from agnoclaw.runtime.security import IdentitySource
from agnoclaw.runtime.trajectory import project_harness_event


def _context() -> ExecutionContext:
    return ExecutionContext.create(
        tenant_id="tenant-secret",
        user_id="user-secret",
        session_id="session-secret",
        workspace_id="workspace-secret",
        roles=("operator-secret",),
        scopes=("runs:write",),
        request_id="request-123",
        trace_id="trace-456",
        metadata={"access_token": "metadata-secret"},
        identity_source=IdentitySource.AUTHENTICATED_CLAIMS,
    )


def test_projection_retains_operational_shape_without_raw_content() -> None:
    event = build_event(
        event_type="model.request.failed",
        run_id="run-1",
        context=_context(),
        payload={
            "content": "raw-model-content",
            "error": "provider leaked secret-token",
            "code": "PROVIDER_TIMEOUT",
            "tool_name": "inventory.lookup",
            "duration_ms": 125.5,
            "metadata": {"authorization": "Bearer private"},
        },
    )

    proposed = project_harness_event(event, attempt_id="run-1:attempt:1")
    serialized = json.dumps(proposed.semantic_value(), sort_keys=True)

    assert proposed.event_id == event.event_id
    assert proposed.event_type == "trajectory.model.request.failed"
    assert proposed.attempt_id == "run-1:attempt:1"
    assert proposed.payload["request_id"] == "request-123"
    assert proposed.payload["trace_id"] == "trace-456"
    assert proposed.payload["data"]["code"] == "PROVIDER_TIMEOUT"
    assert proposed.payload["data"]["tool_name"] == "inventory.lookup"
    assert proposed.payload["data"]["duration_ms"] == 125.5
    for secret in (
        "raw-model-content",
        "secret-token",
        "Bearer private",
        "metadata-secret",
        "tenant-secret",
        "user-secret",
        "session-secret",
        "workspace-secret",
        "operator-secret",
    ):
        assert secret not in serialized


def test_projection_is_deterministic_bounded_and_describes_opaque_values() -> None:
    payload = {
        "content": "x" * 100_000,
        "items": [{f"unsafe field {index}": object()} for index in range(500)],
    }
    event = build_event(
        event_type="response_chunk",
        run_id="run-1",
        context=_context(),
        payload=payload,
    )

    first = project_harness_event(event, attempt_id="run-1:attempt:1")
    second = project_harness_event(event, attempt_id="run-1:attempt:1")
    encoded = json.dumps(first.semantic_value(), sort_keys=True).encode()

    assert first == second
    assert len(encoded) < 32_768
    assert first.payload["omitted_items"] > 0
    assert "x" * 1_000 not in encoded.decode()
    assert "unsafe field" not in encoded.decode()


def test_unsafe_trace_identifiers_are_digested() -> None:
    context = ExecutionContext.create(
        tenant_id="tenant",
        user_id="user",
        session_id="session",
        workspace_id="workspace",
        request_id="request id contains private text",
        trace_id="trace\nsecret",
    )
    event = build_event(
        event_type="run.started",
        run_id="run-1",
        context=context,
    )

    proposed = project_harness_event(event, attempt_id="run-1:attempt:1")

    assert proposed.payload["request_id"]["$type"] == "text"
    assert proposed.payload["trace_id"]["$type"] == "text"
