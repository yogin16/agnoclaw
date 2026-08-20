"""Truthful scoped CRUD and forget contracts for direct learning stores."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agno.db.sqlite import SqliteDb
from agno.learn import LearningMachine
from agno.learn.config import SessionContextConfig, UserMemoryConfig, UserProfileConfig

from agnoclaw import (
    AgentHarness,
    HarnessConfig,
    HarnessError,
    LearningAdminBackendError,
    LearningAdminGateway,
    LearningDataStore,
    LearningMutationAction,
    LearningProfile,
    LearningScope,
    LearningVerificationLevel,
)
from agnoclaw.runtime import ExecutionContext


@dataclass
class _Profile:
    user_id: str
    name: str | None = None

    @classmethod
    def from_dict(cls, value):
        if not value.get("user_id"):
            return None
        return cls(user_id=value["user_id"], name=value.get("name"))

    def to_dict(self):
        return asdict(self)


@dataclass
class _Memories:
    user_id: str
    memories: list[dict]

    @classmethod
    def from_dict(cls, value):
        if not value.get("user_id") or not isinstance(value.get("memories"), list):
            return None
        return cls(user_id=value["user_id"], memories=list(value["memories"]))

    def to_dict(self):
        return asdict(self)


@dataclass
class _Session:
    session_id: str
    user_id: str | None = None
    summary: str | None = None
    goal: str | None = None
    plan: list[str] | None = None
    progress: list[str] | None = None

    @classmethod
    def from_dict(cls, value):
        if not value.get("session_id"):
            return None
        return cls(
            session_id=value["session_id"],
            user_id=value.get("user_id"),
            summary=value.get("summary"),
            goal=value.get("goal"),
            plan=value.get("plan"),
            progress=value.get("progress"),
        )

    def to_dict(self):
        return asdict(self)


class _Db:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.raise_reads = False
        self.keep_on_delete = False

    @staticmethod
    def _record_id(*, learning_type, user_id=None, session_id=None, **kwargs):
        del kwargs
        if learning_type == "user_profile":
            return f"user_profile_{user_id}"
        if learning_type == "user_memory":
            return f"memories_{user_id}"
        return f"session_context_{session_id}"

    def get_learning(self, **kwargs):
        if self.raise_reads:
            raise RuntimeError("secret backend detail")
        return self.rows.get(self._record_id(**kwargs))

    def upsert_learning(self, *, id, content, **kwargs):
        del kwargs
        self.rows[id] = {"content": dict(content)}

    def delete_learning(self, *, id):
        if self.keep_on_delete:
            return True
        return self.rows.pop(id, None) is not None


class _AsyncDb(_Db):
    async def get_learning(self, **kwargs):
        return super().get_learning(**kwargs)

    async def upsert_learning(self, *, id, content, **kwargs):
        super().upsert_learning(id=id, content=content, **kwargs)

    async def delete_learning(self, *, id):
        return super().delete_learning(id=id)


def _policy():
    return LearningProfile.personal_and_session(
        consent_required=False,
        tenant_required=True,
    )


def _context(*, tenant: str = "acme", user: str = "user-1"):
    return ExecutionContext.create(
        tenant_id=tenant,
        user_id=user,
        session_id="session-1",
        workspace_id="workspace-1",
    )


def _gateway(db=None, *, policy=None, context=None):
    policy = policy or _policy()
    scope = LearningScope.resolve(
        policy,
        context or _context(),
        agent_id="agent-1",
        consented=True,
    )
    db = db or _Db()
    machine = SimpleNamespace(
        user_profile_store=SimpleNamespace(db=db, schema=_Profile),
        user_memory_store=SimpleNamespace(db=db, schema=_Memories),
        session_context_store=SimpleNamespace(db=db, schema=_Session),
    )
    return (
        LearningAdminGateway(
            machine,
            policy=policy,
            scope=scope,
            agent_id="agent-1",
        ),
        db,
        scope,
        machine,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "content", "expected_prefix"),
    [
        (LearningDataStore.USER_PROFILE, {"name": "Ada"}, "user_profile_"),
        (
            LearningDataStore.USER_MEMORY,
            {"memories": [{"id": "m1", "content": "Prefers terse output"}]},
            "memories_",
        ),
        (
            LearningDataStore.SESSION_CONTEXT,
            {"summary": "Investigating retries"},
            "session_context_",
        ),
    ],
)
async def test_replace_read_and_forget_are_post_verified(
    store,
    content,
    expected_prefix,
) -> None:
    gateway, db, scope, _ = _gateway()
    absent = await gateway.read(store)
    assert absent.present is False

    replaced = await gateway.replace(store, content, operation_id=f"replace:{store}")
    assert replaced.schema_version == "1.0"
    assert replaced.action is LearningMutationAction.REPLACE
    assert replaced.existed_before is False
    assert replaced.content_digest_after is not None
    assert replaced.backend_delete_confirmed is None
    assert replaced.verified is True
    assert replaced.verification_level is LearningVerificationLevel.POINT_IN_TIME
    assert replaced.digest.startswith("sha256:")

    record = await gateway.read(store)
    assert record.schema_version == "1.0"
    assert record.present is True
    assert record.content_digest == replaced.content_digest_after
    assert list(db.rows)[0].startswith(expected_prefix)
    assert scope.storage_user_id not in content.values()

    forgotten = await gateway.forget(store, operation_id=f"forget:{store}")
    assert forgotten.action is LearningMutationAction.FORGET
    assert forgotten.existed_before is True
    assert forgotten.backend_delete_confirmed is True
    assert forgotten.content_digest_after is None
    assert (await gateway.read(store)).present is False

    replay = await gateway.forget(store, operation_id=f"forget:{store}:again")
    assert replay.existed_before is False
    assert replay.backend_delete_confirmed is False
    assert replay.verified is True


@pytest.mark.asyncio
async def test_real_agno_sqlite_round_trip_for_all_direct_stores(tmp_path) -> None:
    db = SqliteDb(db_file=str(tmp_path / "agno-learning.db"))
    machine = LearningMachine(
        db=db,
        user_profile=UserProfileConfig(db=db),
        user_memory=UserMemoryConfig(db=db),
        session_context=SessionContextConfig(db=db, enable_planning=True),
    )
    policy = _policy()
    scope = LearningScope.resolve(
        policy,
        _context(),
        agent_id="agent-1",
        consented=True,
    )
    gateway = LearningAdminGateway(
        machine,
        policy=policy,
        scope=scope,
        agent_id="agent-1",
    )
    payloads = {
        LearningDataStore.USER_PROFILE: {
            "name": "Ada Lovelace",
            "preferred_name": "Ada",
        },
        LearningDataStore.USER_MEMORY: {
            "memories": [{"id": "m1", "content": "Prefers terse output"}],
        },
        LearningDataStore.SESSION_CONTEXT: {
            "summary": "Investigating retries",
            "goal": "Ship safely",
            "plan": ["test the exact upstream contract"],
        },
    }

    for store, payload in payloads.items():
        replaced = await gateway.replace(
            store,
            payload,
            operation_id=f"real-agno:{store}:replace",
        )
        assert replaced.verified is True
        assert (await gateway.read(store)).content_digest == replaced.content_digest_after

        forgotten = await gateway.forget(
            store,
            operation_id=f"real-agno:{store}:forget",
        )
        assert forgotten.verified is True
        assert (await gateway.read(store)).present is False


@pytest.mark.asyncio
async def test_scope_uses_opaque_identity_and_cannot_read_another_owner() -> None:
    db = _Db()
    first, _, first_scope, _ = _gateway(db, context=_context(user="user-1"))
    second, _, second_scope, _ = _gateway(db, context=_context(user="user-2"))

    await first.replace(
        LearningDataStore.USER_PROFILE,
        {"name": "First"},
        operation_id="replace:first",
    )

    assert first_scope.storage_user_id != second_scope.storage_user_id
    assert (await second.read(LearningDataStore.USER_PROFILE)).present is False
    assert first_scope.storage_user_id in next(iter(db.rows))
    assert "user-1" not in next(iter(db.rows))


@pytest.mark.asyncio
async def test_backend_failure_is_not_reported_as_absence() -> None:
    gateway, db, _, _ = _gateway()
    db.raise_reads = True
    with pytest.raises(LearningAdminBackendError) as error:
        await gateway.read(LearningDataStore.USER_PROFILE)
    assert error.value.code == "LEARNING_ADMIN_BACKEND_FAILED"
    assert error.value.retryable is True
    assert "secret backend detail" not in str(error.value)


@pytest.mark.asyncio
async def test_forget_fails_when_post_delete_read_still_finds_data() -> None:
    gateway, db, _, _ = _gateway()
    await gateway.replace(
        LearningDataStore.USER_PROFILE,
        {"name": "Ada"},
        operation_id="replace:before-stuck-delete",
    )
    db.keep_on_delete = True
    with pytest.raises(HarnessError) as error:
        await gateway.forget(
            LearningDataStore.USER_PROFILE,
            operation_id="forget:stuck",
        )
    assert error.value.code == "LEARNING_ADMIN_FORGET_NOT_VERIFIED"


@pytest.mark.asyncio
async def test_content_schema_identity_and_unknown_fields_fail_closed() -> None:
    gateway, _, _, _ = _gateway()
    with pytest.raises(HarnessError) as identity_error:
        await gateway.replace(
            LearningDataStore.USER_PROFILE,
            {"user_id": "attacker", "name": "Ada"},
            operation_id="replace:identity",
        )
    assert identity_error.value.code == "LEARNING_ADMIN_CONTENT_IDENTITY_FORBIDDEN"

    with pytest.raises(HarnessError) as unknown_error:
        await gateway.replace(
            LearningDataStore.USER_PROFILE,
            {"name": "Ada", "unknown": "silently dropped upstream"},
            operation_id="replace:unknown",
        )
    assert unknown_error.value.code == "LEARNING_ADMIN_CONTENT_FIELDS_UNSUPPORTED"

    with pytest.raises(HarnessError) as invalid_error:
        await gateway.replace(
            LearningDataStore.USER_MEMORY,
            {"memories": "not-a-list"},
            operation_id="replace:invalid",
        )
    assert invalid_error.value.code == "LEARNING_ADMIN_CONTENT_INVALID"

    for store, invalid_content in (
        (LearningDataStore.USER_PROFILE, {"name": ["not", "a", "name"]}),
        (
            LearningDataStore.USER_MEMORY,
            {
                "memories": [
                    {"id": "duplicate", "content": "first"},
                    {"id": "duplicate", "content": "second"},
                ]
            },
        ),
        (LearningDataStore.SESSION_CONTEXT, {"plan": "not-a-list"}),
    ):
        with pytest.raises(HarnessError) as shape_error:
            await gateway.replace(
                store,
                invalid_content,
                operation_id=f"replace:shape:{store}",
            )
        assert shape_error.value.code == "LEARNING_ADMIN_CONTENT_INVALID"


@pytest.mark.asyncio
async def test_disabled_store_and_async_database_are_supported() -> None:
    policy = LearningProfile.personal(
        user_profile="always",
        user_memory=None,
        consent_required=False,
        tenant_required=True,
    )
    gateway, _, _, _ = _gateway(_AsyncDb(), policy=policy)
    with pytest.raises(HarnessError) as disabled_error:
        await gateway.read(LearningDataStore.USER_MEMORY)
    assert disabled_error.value.code == "LEARNING_ADMIN_STORE_DISABLED"

    receipt = await gateway.replace(
        LearningDataStore.USER_PROFILE,
        {"name": "Async"},
        operation_id="replace:async",
    )
    assert receipt.verified is True


@pytest.mark.asyncio
async def test_agent_admin_api_resolves_trusted_scope_and_serializes_mutations(
    tmp_path,
) -> None:
    policy = _policy()
    gateway, _, _, machine = _gateway(policy=policy)
    del gateway
    with (
        patch("agnoclaw.agent.Agent", return_value=MagicMock()),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path / "workspace",
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
        )

    with patch("agnoclaw.memory.build_learning_machine", return_value=machine):
        receipt = await harness.replace_learning_data(
            LearningDataStore.SESSION_CONTEXT,
            {"summary": "Durable plan"},
            context=_context(),
            operation_id="replace:agent-session",
        )
        assert receipt.verified is True
        assert (
            await harness.read_learning_data(
                LearningDataStore.SESSION_CONTEXT,
                context=_context(),
            )
        ).present is True
        forgotten = await harness.forget_learning_data(
            LearningDataStore.SESSION_CONTEXT,
            context=_context(),
            operation_id="forget:agent-session",
        )
        assert forgotten.verified is True

    await harness.aclose()
