"""Typed remote client for compatibility calls and the durable lifecycle protocol."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from .commands import RunCommand, command_to_dict
from .runtime.child_results import ChildResultSet
from .runtime.errors import HarnessError
from .runtime.lifecycle import RunSnapshot, RunState
from .runtime.lifecycle_protocol import (
    LIFECYCLE_PROTOCOL_VERSION,
    MAX_LIFECYCLE_EVENT_PAGE_SIZE,
    MAX_LIFECYCLE_OUTPUT_PAGE_SIZE,
    LifecycleProtocolError,
    event_from_wire,
    output_segment_from_wire,
    require_lifecycle_envelope,
    snapshot_from_wire,
)
from .runtime.run_handle import (
    RunControlUnavailableError,
    RunHeartbeat,
    RunReconciliationRequiredError,
    RunWaitError,
)
from .runtime.store import decode_event_cursor


class RemoteHarnessError(HarnessError):
    """A safe, typed error returned by the remote lifecycle service."""


@dataclass
class RemoteHarnessRun:
    """The existing remote run wrapper, evolved with the HarnessRun lifecycle grammar."""

    result: Any = None
    run_id: str | None = None
    session_id: str | None = None
    _events: AsyncIterator[dict[str, Any]] | None = None
    _client: RemoteHarnessClient | None = None
    _harness_id: str | None = None
    _user_id: str | None = None

    @property
    def id(self) -> str | None:
        return self.run_id

    def _require_lifecycle(self, operation: str) -> tuple[RemoteHarnessClient, str]:
        if self._client is None or self.run_id is None:
            raise RunControlUnavailableError(
                run_id=self.run_id or "compatibility-run",
                operation=operation,
            )
        return self._client, self.run_id

    async def status(self) -> RunSnapshot:
        client, run_id = self._require_lifecycle("status")
        snapshot = await client._get_run_snapshot(
            run_id,
            agent_id=self._harness_id,
            user_id=self._user_id,
        )
        self.session_id = snapshot.session_id
        return snapshot

    async def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.05,
    ) -> Any:
        if self._client is None:
            if self._events is not None and self.result is None:
                async for _event in self.events():
                    pass
            return self.result
        client, run_id = self._require_lifecycle("wait")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        started = monotonic()
        while True:
            payload = await client._get_result(
                run_id,
                agent_id=self._harness_id,
                user_id=self._user_id,
            )
            snapshot = snapshot_from_wire(payload.get("run"))
            _require_run_identity(snapshot, run_id)
            self.session_id = snapshot.session_id
            ready = payload.get("ready")
            blocked = payload.get("blocked")
            if not isinstance(ready, bool) or not isinstance(blocked, bool):
                raise LifecycleProtocolError("The remote result readiness state is invalid.")
            if blocked:
                if (
                    ready
                    or snapshot.state is not RunState.WAITING_FOR_RECONCILIATION
                    or not isinstance(payload.get("error"), Mapping)
                    or payload.get("result") is not None
                ):
                    raise LifecycleProtocolError("The remote blocked result is invalid.")
                raise RunReconciliationRequiredError(snapshot)
            if ready:
                if not snapshot.terminal:
                    raise LifecycleProtocolError("The remote terminal result is invalid.")
                error = payload.get("error")
                if error is not None:
                    if not isinstance(error, Mapping) or payload.get("result") is not None:
                        raise LifecycleProtocolError("The remote terminal error is invalid.")
                    code = error.get("code")
                    message = error.get("message")
                    if not isinstance(code, str) or not code:
                        raise LifecycleProtocolError("The remote terminal error is invalid.")
                    if not isinstance(message, str) or not message:
                        raise LifecycleProtocolError("The remote terminal error is invalid.")
                    raise RunWaitError(
                        snapshot=snapshot,
                        code=code,
                        message=message,
                        error=error.get("safe_error"),
                    )
                self.result = payload.get("result")
                return self.result
            if (
                snapshot.terminal
                or payload.get("error") is not None
                or payload.get("result") is not None
            ):
                raise LifecycleProtocolError("The remote result readiness state is invalid.")
            elapsed = monotonic() - started
            if timeout is not None and elapsed >= timeout:
                raise TimeoutError
            delay = poll_interval if timeout is None else min(poll_interval, timeout - elapsed)
            await asyncio.sleep(max(0, delay))

    async def events(
        self,
        *,
        after: str | None = None,
        follow: bool | None = None,
        timeout: float | None = None,
        poll_interval: float = 0.05,
        heartbeat_interval: float = 15.0,
        page_size: int = MAX_LIFECYCLE_EVENT_PAGE_SIZE,
    ) -> AsyncIterator[Any]:
        if self._client is None:
            if self._events is None:
                return
            async for event in self._events:
                yield event
            return
        client, run_id = self._require_lifecycle("events")
        async for event in client._lifecycle_events(
            run_id,
            agent_id=self._harness_id,
            user_id=self._user_id,
            after=after,
            follow=follow,
            timeout=timeout,
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
            page_size=page_size,
        ):
            yield event

    async def cancel(self) -> RunSnapshot:
        client, run_id = self._require_lifecycle("cancel")
        return await client._cancel_run(
            run_id,
            agent_id=self._harness_id,
            user_id=self._user_id,
        )

    async def output(
        self,
        *,
        after: str | None = None,
        follow: bool | None = None,
        timeout: float | None = None,
        poll_interval: float = 0.05,
        heartbeat_interval: float = 15.0,
        page_size: int = MAX_LIFECYCLE_OUTPUT_PAGE_SIZE,
    ) -> AsyncIterator[Any]:
        """Replay authenticated, artifact-verified provider output segments."""
        client, run_id = self._require_lifecycle("output")
        async for segment in client._lifecycle_output(
            run_id,
            agent_id=self._harness_id,
            user_id=self._user_id,
            after=after,
            follow=follow,
            timeout=timeout,
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
            page_size=page_size,
        ):
            yield segment

    async def command(self, command: RunCommand) -> RunSnapshot:
        client, run_id = self._require_lifecycle("command")
        return await client._command_run(
            run_id,
            agent_id=self._harness_id,
            command=command,
            user_id=self._user_id,
        )

    async def child(
        self,
        template: str,
        task: str,
        *,
        delegation_id: str,
    ) -> RemoteHarnessRun:
        """Start one server-declared child template without remote policy choices."""
        client, run_id = self._require_lifecycle("child")
        return await client._start_child(
            run_id,
            agent_id=self._harness_id,
            template=template,
            task=task,
            delegation_id=delegation_id,
            user_id=self._user_id,
        )

    async def children(self, *, limit: int = 64) -> tuple[RunSnapshot, ...]:
        """List authoritative direct children through the authenticated parent."""
        client, run_id = self._require_lifecycle("children")
        return await client._list_children(
            run_id,
            agent_id=self._harness_id,
            user_id=self._user_id,
            limit=limit,
        )

    async def child_results(
        self,
        *,
        limit: int = 64,
        artifact_limit: int = 16,
        max_inline_result_chars: int = 8_000,
    ) -> ChildResultSet:
        """Collect typed child outcomes with bounded inline results or lossless pointers."""
        client, run_id = self._require_lifecycle("child_results")
        return await client._get_child_results(
            run_id,
            agent_id=self._harness_id,
            user_id=self._user_id,
            limit=limit,
            artifact_limit=artifact_limit,
            max_inline_result_chars=max_inline_result_chars,
        )


class RemoteHarnessClient:
    """Small remote edge for legacy AgentOS calls and versioned agnoclaw runs."""

    base_url: str
    agent_id: str
    _owns_client: bool
    _headers: dict[str, str] | None
    _client: httpx.AsyncClient

    def __init__(
        self,
        base_url: str,
        *,
        agent_id: str = "agnoclaw",
        api_key: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or bool(parsed.username)
            or bool(parsed.password)
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an http(s) origin without credentials or extra data")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        self.base_url = str(parsed).rstrip("/")
        self.agent_id = agent_id.strip()
        self._owns_client = client is None
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=False,
        )

    async def start(
        self,
        message: str,
        *,
        idempotency_key: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        learning_consent: bool = False,
        persist_output: bool = True,
        **options: Any,
    ) -> RemoteHarnessRun:
        target = agent_id or self.agent_id
        payload = await self._request_json(
            "POST",
            self._lifecycle_path(target, "runs"),
            json_body={
                "protocol_version": LIFECYCLE_PROTOCOL_VERSION,
                "message": message,
                "idempotency_key": idempotency_key,
                "session_id": session_id,
                "user_id": user_id,
                "metadata": metadata or {},
                "learning_consent": learning_consent,
                "persist_output": persist_output,
                "options": options,
            },
            expected_kind="run",
        )
        snapshot = snapshot_from_wire(payload.get("run"))
        return RemoteHarnessRun(
            run_id=snapshot.run_id,
            session_id=snapshot.session_id,
            _client=self,
            _harness_id=target,
            _user_id=user_id,
        )

    async def get_run(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> RemoteHarnessRun:
        target = agent_id or self.agent_id
        snapshot = await self._get_run_snapshot(run_id, agent_id=target, user_id=user_id)
        return RemoteHarnessRun(
            run_id=snapshot.run_id,
            session_id=snapshot.session_id,
            _client=self,
            _harness_id=target,
            _user_id=user_id,
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
        """Compatibility call through AgentOS's native completed/raw-stream route."""
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

        response = await self._client.post(path, data=data, headers=self._headers)
        response.raise_for_status()
        result = response.json()
        run_id = result.get("run_id") if isinstance(result, Mapping) else None
        session = result.get("session_id") if isinstance(result, Mapping) else None
        return RemoteHarnessRun(result=result, run_id=run_id, session_id=session)

    async def _get_run_snapshot(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        user_id: str | None,
    ) -> RunSnapshot:
        payload = await self._request_json(
            "GET",
            self._lifecycle_path(agent_id or self.agent_id, "runs", run_id),
            params=_identity_params(user_id),
            expected_kind="run",
        )
        snapshot = snapshot_from_wire(payload.get("run"))
        _require_run_identity(snapshot, run_id)
        return snapshot

    async def _start_child(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        template: str,
        task: str,
        delegation_id: str,
        user_id: str | None,
    ) -> RemoteHarnessRun:
        target = agent_id or self.agent_id
        payload = await self._request_json(
            "POST",
            self._lifecycle_path(target, "runs", run_id, "children"),
            json_body={
                "protocol_version": LIFECYCLE_PROTOCOL_VERSION,
                "template": template,
                "task": task,
                "delegation_id": delegation_id,
                "user_id": user_id,
            },
            expected_kind="child",
        )
        parent = snapshot_from_wire(payload.get("parent"))
        child = snapshot_from_wire(payload.get("child"))
        _require_run_identity(parent, run_id)
        if child.parent_run_id != run_id or child.child_depth != parent.child_depth + 1:
            raise LifecycleProtocolError("The remote child lineage is invalid.")
        template_name = payload.get("template")
        template_digest = payload.get("template_digest")
        if (
            template_name != template
            or not isinstance(template_digest, str)
            or not template_digest.startswith("sha256:")
        ):
            raise LifecycleProtocolError("The remote child template evidence is invalid.")
        return RemoteHarnessRun(
            run_id=child.run_id,
            session_id=child.session_id,
            _client=self,
            _harness_id=target,
            _user_id=user_id,
        )

    async def _list_children(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        user_id: str | None,
        limit: int,
    ) -> tuple[RunSnapshot, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 64:
            raise ValueError("limit must be between 1 and 64")
        payload = await self._request_json(
            "GET",
            self._lifecycle_path(agent_id or self.agent_id, "runs", run_id, "children"),
            params={"limit": limit, **_identity_params(user_id)},
            expected_kind="children",
        )
        parent = snapshot_from_wire(payload.get("parent"))
        _require_run_identity(parent, run_id)
        raw_children = payload.get("children")
        if not isinstance(raw_children, list) or len(raw_children) > limit:
            raise LifecycleProtocolError("The remote child page is invalid.")
        children = tuple(snapshot_from_wire(item) for item in raw_children)
        if any(item.parent_run_id != run_id for item in children):
            raise LifecycleProtocolError("The remote child lineage is invalid.")
        return children

    async def _get_child_results(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        user_id: str | None,
        limit: int,
        artifact_limit: int,
        max_inline_result_chars: int,
    ) -> ChildResultSet:
        if isinstance(limit, bool) or not 1 <= limit <= 64:
            raise ValueError("limit must be between 1 and 64")
        if isinstance(artifact_limit, bool) or not 0 <= artifact_limit <= 100:
            raise ValueError("artifact_limit must be between 0 and 100")
        if (
            isinstance(max_inline_result_chars, bool)
            or not 256 <= max_inline_result_chars <= 1_000_000
        ):
            raise ValueError("max_inline_result_chars must be between 256 and 1000000")
        payload = await self._request_json(
            "GET",
            self._lifecycle_path(
                agent_id or self.agent_id,
                "runs",
                run_id,
                "child-results",
            ),
            params={
                "limit": limit,
                "artifact_limit": artifact_limit,
                "max_inline_result_chars": max_inline_result_chars,
                **_identity_params(user_id),
            },
            expected_kind="child_results",
        )
        parent = snapshot_from_wire(payload.get("parent"))
        _require_run_identity(parent, run_id)
        try:
            results = ChildResultSet.from_dict(payload.get("results"))
        except (TypeError, ValueError) as exc:
            raise LifecycleProtocolError("The remote child result set is invalid.") from exc
        if results.parent_run_id != run_id or len(results.outcomes) > limit:
            raise LifecycleProtocolError("The remote child result identity is invalid.")
        return results

    async def _get_result(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        user_id: str | None,
    ) -> Mapping[str, Any]:
        return await self._request_json(
            "GET",
            self._lifecycle_path(agent_id or self.agent_id, "runs", run_id, "result"),
            params=_identity_params(user_id),
            expected_kind="result",
        )

    async def _cancel_run(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        user_id: str | None,
    ) -> RunSnapshot:
        payload = await self._request_json(
            "POST",
            self._lifecycle_path(agent_id or self.agent_id, "runs", run_id, "cancel"),
            params=_identity_params(user_id),
            expected_kind="run",
        )
        snapshot = snapshot_from_wire(payload.get("run"))
        _require_run_identity(snapshot, run_id)
        return snapshot

    async def _command_run(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        command: RunCommand,
        user_id: str | None,
    ) -> RunSnapshot:
        payload = await self._request_json(
            "POST",
            self._lifecycle_path(agent_id or self.agent_id, "runs", run_id, "commands"),
            params=_identity_params(user_id),
            json_body=command_to_dict(command),
            expected_kind="run",
        )
        snapshot = snapshot_from_wire(payload.get("run"))
        _require_run_identity(snapshot, run_id)
        return snapshot

    async def _lifecycle_events(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        user_id: str | None,
        after: str | None,
        follow: bool | None,
        timeout: float | None,
        poll_interval: float,
        heartbeat_interval: float,
        page_size: int,
    ) -> AsyncIterator[Any]:
        if not 1 <= page_size <= MAX_LIFECYCLE_EVENT_PAGE_SIZE:
            raise ValueError("page_size must be between 1 and 100")
        if poll_interval <= 0 or heartbeat_interval <= 0:
            raise ValueError("poll and heartbeat intervals must be positive")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        sequence = decode_event_cursor(after, run_id=run_id) if after else 0
        cursor = after
        started = monotonic()
        last_activity = started
        while True:
            params: dict[str, Any] = {"limit": page_size, **_identity_params(user_id)}
            if cursor is not None:
                params["after"] = cursor
            payload = await self._request_json(
                "GET",
                self._lifecycle_path(agent_id or self.agent_id, "runs", run_id, "events"),
                params=params,
                expected_kind="events",
            )
            snapshot = snapshot_from_wire(payload.get("run"))
            _require_run_identity(snapshot, run_id)
            raw_events = payload.get("events")
            if not isinstance(raw_events, list) or len(raw_events) > page_size:
                raise LifecycleProtocolError("The remote event page is invalid.")
            for raw_event in raw_events:
                event = event_from_wire(raw_event, run_id=run_id)
                if event.sequence != sequence + 1:
                    raise LifecycleProtocolError("The remote event sequence is not gap-free.")
                sequence = event.sequence
                last_activity = monotonic()
                yield event
            next_cursor = payload.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise LifecycleProtocolError("The remote event cursor is invalid.")
            if raw_events:
                if next_cursor is None or next_cursor == cursor:
                    raise LifecycleProtocolError("The remote event cursor did not advance.")
                if _remote_cursor_sequence(next_cursor, run_id=run_id) != sequence:
                    raise LifecycleProtocolError("The remote event cursor is inconsistent.")
                cursor = next_cursor
                continue
            if next_cursor != cursor:
                raise LifecycleProtocolError("The remote empty event page advanced its cursor.")
            if follow is False or (snapshot.terminal and follow is not True):
                return
            now = monotonic()
            if timeout is not None and now - started >= timeout:
                return
            if follow is True and now - last_activity >= heartbeat_interval:
                last_activity = now
                yield RunHeartbeat(run_id=run_id, after_sequence=sequence)
            delay = poll_interval
            if timeout is not None:
                delay = min(delay, max(0, timeout - (now - started)))
            await asyncio.sleep(delay)

    async def _lifecycle_output(
        self,
        run_id: str,
        *,
        agent_id: str | None,
        user_id: str | None,
        after: str | None,
        follow: bool | None,
        timeout: float | None,
        poll_interval: float,
        heartbeat_interval: float,
        page_size: int,
    ) -> AsyncIterator[Any]:
        if not 1 <= page_size <= MAX_LIFECYCLE_OUTPUT_PAGE_SIZE:
            raise ValueError("page_size must be between 1 and 50")
        if poll_interval <= 0 or heartbeat_interval <= 0:
            raise ValueError("poll and heartbeat intervals must be positive")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        sequence = decode_event_cursor(after, run_id=run_id) if after else 0
        segment_sequence: int | None = None
        cursor = after
        started = monotonic()
        last_activity = started
        while True:
            params: dict[str, Any] = {"limit": page_size, **_identity_params(user_id)}
            if cursor is not None:
                params["after"] = cursor
            payload = await self._request_json(
                "GET",
                self._lifecycle_path(agent_id or self.agent_id, "runs", run_id, "output"),
                params=params,
                expected_kind="output",
            )
            snapshot = snapshot_from_wire(payload.get("run"))
            _require_run_identity(snapshot, run_id)
            raw_segments = payload.get("segments")
            if not isinstance(raw_segments, list) or len(raw_segments) > page_size:
                raise LifecycleProtocolError("The remote output page is invalid.")
            for raw_segment in raw_segments:
                segment = output_segment_from_wire(raw_segment, run_id=run_id)
                if segment.event_sequence <= sequence:
                    raise LifecycleProtocolError("The remote output sequence did not advance.")
                if (
                    segment_sequence is not None
                    and segment.segment_sequence != segment_sequence + 1
                ):
                    raise LifecycleProtocolError("The remote output sequence is not gap-free.")
                sequence = segment.event_sequence
                segment_sequence = segment.segment_sequence
                last_activity = monotonic()
                yield segment
            next_cursor = payload.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise LifecycleProtocolError("The remote output cursor is invalid.")
            if raw_segments:
                if next_cursor != raw_segments[-1].get("cursor") or next_cursor == cursor:
                    raise LifecycleProtocolError("The remote output cursor is inconsistent.")
                if _remote_cursor_sequence(next_cursor, run_id=run_id) != sequence:
                    raise LifecycleProtocolError("The remote output cursor is inconsistent.")
                cursor = next_cursor
                if len(raw_segments) == page_size:
                    continue
            elif next_cursor != cursor:
                raise LifecycleProtocolError("The remote empty output page advanced its cursor.")
            if follow is False or (snapshot.terminal and follow is not True):
                return
            now = monotonic()
            if timeout is not None and now - started >= timeout:
                return
            if follow is True and now - last_activity >= heartbeat_interval:
                last_activity = now
                yield RunHeartbeat(run_id=run_id, after_sequence=sequence)
            delay = poll_interval
            if timeout is not None:
                delay = min(delay, max(0, timeout - (now - started)))
            await asyncio.sleep(delay)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected_kind: str,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Mapping[str, Any]:
        response = await self._client.request(
            method,
            path,
            params=params,
            json=json_body,
            headers=self._headers,
        )
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LifecycleProtocolError("The remote lifecycle response is not JSON.") from exc
        if response.is_error:
            raise _remote_error(response.status_code, payload)
        return require_lifecycle_envelope(payload, kind=expected_kind)

    async def _stream_events(
        self,
        path: str,
        data: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream("POST", path, data=data, headers=self._headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                event = _parse_sse_line(line)
                if event is not None:
                    yield event

    @staticmethod
    def _lifecycle_path(harness_id: str, *parts: str) -> str:
        if not _is_safe_path_segment(harness_id):
            raise ValueError("agent_id must be a non-empty path-segment string")
        if any(not _is_safe_path_segment(part) for part in parts):
            raise ValueError("lifecycle path identifiers must be non-empty path segments")
        suffix = "/".join(parts)
        return f"/agnoclaw/v1/harnesses/{harness_id}/{suffix}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> RemoteHarnessClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


def _identity_params(user_id: str | None) -> dict[str, str]:
    return {"user_id": user_id} if user_id is not None else {}


_SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]{1,512}\Z")


def _is_safe_path_segment(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_PATH_SEGMENT.fullmatch(value) is not None


def _require_run_identity(snapshot: RunSnapshot, run_id: str) -> None:
    if snapshot.run_id != run_id:
        raise LifecycleProtocolError("The remote run identity changed in transit.")


def _remote_cursor_sequence(cursor: str, *, run_id: str) -> int:
    try:
        return decode_event_cursor(cursor, run_id=run_id)
    except HarnessError as exc:
        raise LifecycleProtocolError("The remote event cursor is invalid.") from exc


def _remote_error(status_code: int, payload: Any) -> RemoteHarnessError:
    try:
        envelope = require_lifecycle_envelope(payload, kind="error")
        error = envelope["error"]
        if not isinstance(error, Mapping):
            raise TypeError
        details = error.get("details")
        if details is not None and not isinstance(details, Mapping):
            raise TypeError
        code = error["code"]
        category = error["category"]
        message = error["message"]
        retryable = error["retryable"]
        if not isinstance(code, str) or not code:
            raise TypeError
        if not isinstance(category, str) or not category:
            raise TypeError
        if not isinstance(message, str) or not message:
            raise TypeError
        if not isinstance(retryable, bool):
            raise TypeError
        return RemoteHarnessError(
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            details=dict(details) if details is not None else {"http_status": status_code},
        )
    except (KeyError, TypeError, LifecycleProtocolError):
        return RemoteHarnessError(
            code="REMOTE_LIFECYCLE_HTTP_ERROR",
            category="transport",
            message="The remote lifecycle request failed without a valid error envelope.",
            retryable=status_code >= 500,
            details={"http_status": status_code},
        )


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


__all__ = [
    "RemoteHarnessClient",
    "RemoteHarnessError",
    "RemoteHarnessRun",
]
