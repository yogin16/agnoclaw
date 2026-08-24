"""Admission, materialization, and settlement contracts for capability execution."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest

from agnoclaw import (
    CapabilityConcurrency,
    CapabilityExecutor,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilityRegistry,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
)
from agnoclaw.runtime import (
    AdmissionEnvelope,
    HarnessError,
    IdentityAssertion,
    IdentitySource,
    LocalArtifactStore,
    OperationGateway,
    OperationNotFoundError,
    OperationState,
    RunSnapshot,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.store import OperationIdempotencyConflictError

_OPEN_STORES: list[SQLiteRuntimeStore] = []


@pytest.fixture(autouse=True)
def _close_runtime_stores():
    """Keep executor contracts from leaking SQLite handles into later tests."""
    start = len(_OPEN_STORES)
    yield
    for store in _OPEN_STORES[start:]:
        store.close()
    del _OPEN_STORES[start:]


def _admission(
    *,
    tenant_id: str = "tenant-1",
    user_id: str = "user-1",
    session_id: str = "session-1",
    scopes: tuple[str, ...] = ("inventory:read",),
    request_id: str | None = None,
    client_metadata: dict[str, object] | None = None,
) -> AdmissionEnvelope:
    return AdmissionEnvelope.resolve(
        IdentityAssertion(
            source=IdentitySource.TRUSTED_HOST,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            scopes=scopes,
        ),
        request_id=request_id,
        client_metadata=client_metadata,
    )


def _spec(
    *,
    name: str = "inventory.lookup",
    factory=lambda: lambda **arguments: arguments,
    effect: EffectClass = EffectClass.READ_ONLY,
    lifetime: CapabilityLifetime = CapabilityLifetime.RUN,
    concurrency: CapabilityConcurrency = CapabilityConcurrency.ISOLATED,
    trust: CapabilityTrust = CapabilityTrust.VERIFIED,
    recovery: CapabilityRecovery = CapabilityRecovery.RECREATABLE,
    required_scopes: tuple[str, ...] = ("inventory:read",),
    input_schema: dict | None = None,
) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=effect,
        trust=trust,
        lifetime=lifetime,
        concurrency=concurrency,
        recovery=recovery,
        implementation_digest=f"sha256:{name}",
        input_schema=input_schema or {"type": "object"},
        required_scopes=required_scopes,
        supports_idempotency_key=effect is EffectClass.IDEMPOTENT,
        factory=factory,
    )


def _executor(tmp_path, *specs: CapabilitySpec, artifact_results: bool = False):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _OPEN_STORES.append(store)
    store.create_run(
        RunSnapshot(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
        )
    )
    registry = CapabilityRegistry()
    for spec in specs:
        registry.register(spec)
    artifacts = LocalArtifactStore(tmp_path / "artifacts") if artifact_results else None
    gateway = OperationGateway(
        store,
        worker_id="worker-1",
        artifact_store=artifacts,
        result_cache_size=0 if artifacts is not None else 128,
    )
    return CapabilityExecutor(registry, gateway), store, artifacts


@pytest.mark.asyncio
async def test_durable_execution_requires_admission_before_intent_or_factory(tmp_path):
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return lambda: "unexpected"

    executor, store, _ = _executor(tmp_path, _spec(factory=factory))

    with pytest.raises(HarnessError) as failure:
        await executor.execute(
            "inventory.lookup",
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            profile="durable",
        )

    assert failure.value.code == "CAPABILITY_ADMISSION_REQUIRED"
    assert calls == 0
    with pytest.raises(OperationNotFoundError):
        store.get_operation("operation-1")
    with pytest.raises(HarnessError) as hidden:
        await executor.execute(
            "private.unknown",
            operation_id="operation-2",
            run_id="run-1",
            attempt_id="attempt-1",
            profile="service",
        )
    assert hidden.value.code == "CAPABILITY_ADMISSION_REQUIRED"


@pytest.mark.asyncio
async def test_scope_and_exact_run_owner_are_reauthorized_before_intent(tmp_path):
    executor, store, _ = _executor(tmp_path, _spec())

    with pytest.raises(HarnessError) as missing:
        await executor.execute(
            "inventory.lookup",
            operation_id="missing-scope",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(scopes=()),
        )
    assert missing.value.code == "CAPABILITY_SCOPE_REQUIRED"

    with pytest.raises(HarnessError) as hidden:
        await executor.execute(
            "inventory.lookup",
            operation_id="wrong-owner",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(tenant_id="tenant-2"),
        )
    assert hidden.value.code == "RUN_NOT_FOUND"
    with pytest.raises(OperationNotFoundError):
        store.get_operation("wrong-owner")
    with pytest.raises(HarnessError) as hidden_capability:
        await executor.execute(
            "private.unknown",
            operation_id="wrong-owner-unknown",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(tenant_id="tenant-2"),
        )
    assert hidden_capability.value.code == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_arguments_are_digest_bound_but_never_persisted(tmp_path):
    executor, store, _ = _executor(tmp_path, _spec())
    admission = _admission()

    completed = await executor.execute(
        "inventory.lookup",
        operation_id="operation-1",
        run_id="run-1",
        attempt_id="attempt-1",
        arguments={"sku": "secret-sku-value"},
        admission=admission,
        safe_metadata={"request_class": "lookup"},
    )

    assert completed.value == {"sku": "secret-sku-value"}
    record = store.get_operation("operation-1")
    persisted = str(record.to_dict())
    assert "secret-sku-value" not in persisted
    assert record.intent.metadata["authority_digest"] == admission.authority_digest
    assert record.intent.metadata["capability_digest"] == completed.spec.digest
    assert record.intent.metadata["profile"] == "durable"

    with pytest.raises(HarnessError) as reserved:
        await executor.execute(
            "inventory.lookup",
            operation_id="operation-2",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=admission,
            safe_metadata={"profile": "legacy"},
        )
    assert reserved.value.code == "CAPABILITY_METADATA_RESERVED"


@pytest.mark.asyncio
async def test_argument_schema_fails_before_intent_or_factory(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    executor, store, _ = _executor(
        tmp_path,
        _spec(
            factory=factory,
            input_schema={
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
                "additionalProperties": False,
            },
        ),
    )

    with pytest.raises(HarnessError) as failure:
        await executor.execute(
            "inventory.lookup",
            operation_id="schema-invalid",
            run_id="run-1",
            attempt_id="attempt-1",
            arguments={"sku": 42},
            admission=_admission(),
        )

    assert failure.value.code == "CAPABILITY_ARGUMENT_SCHEMA_INVALID"
    assert factories == 0
    with pytest.raises(OperationNotFoundError):
        store.get_operation("schema-invalid")


@pytest.mark.asyncio
async def test_pre_dispatch_and_result_transform_run_inside_settlement_boundary(tmp_path):
    executor, store, _ = _executor(tmp_path, _spec())
    order: list[str] = []

    async def pre_dispatch():
        assert store.get_operation("operation-transform").state is OperationState.DISPATCHING
        order.append("authority")

    async def transform(value):
        order.append("transform")
        return {"governed": value}

    completed = await executor.execute(
        "inventory.lookup",
        operation_id="operation-transform",
        run_id="run-1",
        attempt_id="attempt-1",
        arguments={"sku": "one"},
        admission=_admission(),
        pre_dispatch=pre_dispatch,
        result_transformer=transform,
    )

    assert order == ["authority", "transform"]
    assert completed.value == {"governed": {"sku": "one"}}
    assert completed.operation.record.state is OperationState.SUCCEEDED


@pytest.mark.asyncio
async def test_operation_identity_cannot_be_reused_with_different_arguments(tmp_path):
    executor, _store, _ = _executor(tmp_path, _spec())
    admission = _admission()
    await executor.execute(
        "inventory.lookup",
        operation_id="same-operation",
        run_id="run-1",
        attempt_id="attempt-1",
        arguments={"sku": "one"},
        admission=admission,
    )
    replayed = await executor.execute(
        "inventory.lookup",
        operation_id="same-operation",
        run_id="run-1",
        attempt_id="attempt-1",
        arguments={"sku": "one"},
        admission=_admission(
            request_id="retry-request",
            client_metadata={"trace_attempt": 2},
        ),
    )
    assert replayed.replayed

    with pytest.raises(OperationIdempotencyConflictError):
        await executor.execute(
            "inventory.lookup",
            operation_id="same-operation",
            run_id="run-1",
            attempt_id="attempt-1",
            arguments={"sku": "two"},
            admission=admission,
        )


@pytest.mark.asyncio
async def test_durable_replay_loads_result_without_factory_or_redispatch(tmp_path):
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return lambda **arguments: {"answer": arguments["value"]}

    spec = _spec(factory=factory)
    executor, store, artifacts = _executor(tmp_path, spec, artifact_results=True)
    admission = _admission()
    first = await executor.execute(
        "inventory.lookup",
        operation_id="operation-1",
        run_id="run-1",
        attempt_id="attempt-1",
        arguments={"value": 42},
        admission=admission,
    )
    restarted_registry = CapabilityRegistry()
    restarted_registry.register(
        _spec(factory=lambda: (_ for _ in ()).throw(AssertionError("must not materialize")))
    )
    restarted = CapabilityExecutor(
        restarted_registry,
        OperationGateway(
            store,
            worker_id="worker-2",
            artifact_store=artifacts,
            result_cache_size=0,
        ),
    )
    replayed = await restarted.execute(
        "inventory.lookup",
        operation_id="operation-1",
        run_id="run-1",
        attempt_id="attempt-1",
        arguments={"value": 42},
        admission=admission,
    )

    assert first.value == replayed.value == {"answer": 42}
    assert replayed.replayed
    assert factory_calls == 1


@dataclass
class _Resource:
    value: str
    closed: list[str]

    def __call__(self, **_arguments):
        return self.value

    def close(self) -> None:
        self.closed.append(self.value)


@pytest.mark.asyncio
async def test_run_session_and_process_lifetimes_are_materialized_and_closed_exactly(tmp_path):
    created: dict[str, int] = {"run": 0, "session": 0, "process": 0}
    closed: list[str] = []

    def factory(kind: str):
        def make():
            created[kind] += 1
            return _Resource(kind, closed)

        return make

    specs = (
        _spec(name="tool.run", factory=factory("run"), required_scopes=()),
        _spec(
            name="tool.session",
            factory=factory("session"),
            lifetime=CapabilityLifetime.SESSION,
            concurrency=CapabilityConcurrency.IMMUTABLE_SHARED,
            required_scopes=(),
        ),
        _spec(
            name="tool.process",
            factory=factory("process"),
            lifetime=CapabilityLifetime.PROCESS_POOL,
            concurrency=CapabilityConcurrency.IMMUTABLE_SHARED,
            required_scopes=(),
        ),
    )
    executor, _store, _ = _executor(tmp_path, *specs)
    admission = _admission(scopes=())

    for ordinal in (1, 2):
        for name in ("tool.run", "tool.session", "tool.process"):
            await executor.execute(
                name,
                operation_id=f"{name}:{ordinal}",
                run_id="run-1",
                attempt_id=f"attempt-{ordinal}",
                admission=admission,
            )

    assert created == {"run": 2, "session": 1, "process": 1}
    assert closed == ["run", "run"]
    assert await executor.release_session("session-1") == 1
    assert closed == ["run", "run", "session"]
    await executor.aclose()
    assert closed == ["run", "run", "session", "process"]
    await executor.aclose()


@pytest.mark.asyncio
async def test_serialized_shared_capability_never_overlaps(tmp_path):
    active = 0
    maximum = 0
    entered = asyncio.Event()

    async def capability(**_arguments):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        await asyncio.sleep(0.03)
        active -= 1
        return "done"

    executor, _store, _ = _executor(
        tmp_path,
        _spec(
            factory=lambda: capability,
            lifetime=CapabilityLifetime.SESSION,
            concurrency=CapabilityConcurrency.SERIALIZED,
        ),
    )
    admission = _admission()

    first = asyncio.create_task(
        executor.execute(
            "inventory.lookup",
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=admission,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        executor.execute(
            "inventory.lookup",
            operation_id="operation-2",
            run_id="run-1",
            attempt_id="attempt-2",
            admission=admission,
        )
    )
    await asyncio.gather(first, second)

    assert maximum == 1


@pytest.mark.asyncio
async def test_cancelling_sync_dispatch_does_not_release_shared_resource_early(tmp_path):
    first_entered = threading.Event()
    allow_first_to_finish = threading.Event()
    second_entered = threading.Event()
    active = 0
    maximum = 0
    call_lock = threading.Lock()

    def capability(*, ordinal: int):
        nonlocal active, maximum
        with call_lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if ordinal == 1:
                first_entered.set()
                allow_first_to_finish.wait(timeout=2)
            else:
                second_entered.set()
            return ordinal
        finally:
            with call_lock:
                active -= 1

    executor, _store, _ = _executor(
        tmp_path,
        _spec(
            factory=lambda: capability,
            lifetime=CapabilityLifetime.SESSION,
            concurrency=CapabilityConcurrency.SERIALIZED,
        ),
    )
    admission = _admission()
    first = asyncio.create_task(
        executor.execute(
            "inventory.lookup",
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            arguments={"ordinal": 1},
            admission=admission,
        )
    )
    assert await asyncio.to_thread(first_entered.wait, 1)
    first.cancel()
    second = asyncio.create_task(
        executor.execute(
            "inventory.lookup",
            operation_id="operation-2",
            run_id="run-1",
            attempt_id="attempt-2",
            arguments={"ordinal": 2},
            admission=admission,
        )
    )
    await asyncio.sleep(0.03)
    assert not second_entered.is_set()

    allow_first_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).value == 2
    assert maximum == 1

    with pytest.raises(HarnessError) as hidden:
        await executor.recover_interrupted(
            "operation-1",
            recovery_id="recover-hidden",
            admission=_admission(tenant_id="tenant-2"),
        )
    assert hidden.value.code == "OPERATION_NOT_FOUND"
    recovered = await executor.recover_interrupted(
        "operation-1",
        recovery_id="recover-1",
        admission=admission,
    )
    assert recovered.state is OperationState.PLANNED


@pytest.mark.asyncio
async def test_busy_session_resources_fail_closed_instead_of_closing_in_use(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def capability(**_arguments):
        entered.set()
        await release.wait()

    executor, _store, _ = _executor(
        tmp_path,
        _spec(
            factory=lambda: capability,
            lifetime=CapabilityLifetime.SESSION,
            concurrency=CapabilityConcurrency.IMMUTABLE_SHARED,
        ),
    )
    task = asyncio.create_task(
        executor.execute(
            "inventory.lookup",
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(),
        )
    )
    await entered.wait()

    with pytest.raises(HarnessError) as failure:
        await executor.release_session("session-1")
    assert failure.value.code == "CAPABILITY_SESSION_BUSY"
    release.set()
    await task

    run_entered = asyncio.Event()
    run_release = asyncio.Event()

    async def run_capability(**_arguments):
        run_entered.set()
        await run_release.wait()

    executor.registry.register(_spec(name="inventory.run_wait", factory=lambda: run_capability))
    run_task = asyncio.create_task(
        executor.execute(
            "inventory.run_wait",
            operation_id="operation-2",
            run_id="run-1",
            attempt_id="attempt-2",
            admission=_admission(),
        )
    )
    await run_entered.wait()
    with pytest.raises(HarnessError) as closing:
        await executor.aclose()
    assert closing.value.code == "CAPABILITY_EXECUTOR_BUSY"
    run_release.set()
    await run_task
    await executor.aclose()


@pytest.mark.asyncio
async def test_opaque_and_live_only_capabilities_never_reach_durable_dispatch(tmp_path):
    executor, store, _ = _executor(
        tmp_path,
        _spec(
            trust=CapabilityTrust.OPAQUE_LEGACY,
            recovery=CapabilityRecovery.LIVE_ONLY,
        ),
    )

    with pytest.raises(HarnessError) as failure:
        await executor.execute(
            "inventory.lookup",
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(),
        )
    assert failure.value.code == "CAPABILITY_NOT_DURABLE"
    with pytest.raises(OperationNotFoundError):
        store.get_operation("operation-1")


@pytest.mark.asyncio
async def test_idempotent_capability_requires_and_persists_provider_key(tmp_path):
    executor, store, _ = _executor(
        tmp_path,
        _spec(effect=EffectClass.IDEMPOTENT),
    )

    with pytest.raises(HarnessError) as missing:
        await executor.execute(
            "inventory.lookup",
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(),
        )
    assert missing.value.code == "CAPABILITY_IDEMPOTENCY_KEY_REQUIRED"

    await executor.execute(
        "inventory.lookup",
        operation_id="operation-2",
        run_id="run-1",
        attempt_id="attempt-1",
        admission=_admission(),
        idempotency_key="provider-key-1",
    )
    assert store.get_operation("operation-2").intent.idempotency_key == "provider-key-1"


@pytest.mark.asyncio
async def test_argument_and_metadata_budgets_fail_before_persistence(tmp_path):
    executor, store, _ = _executor(tmp_path, _spec())
    executor = CapabilityExecutor(
        executor.registry,
        executor.gateway,
        max_argument_bytes=8,
        max_metadata_bytes=8,
    )

    with pytest.raises(HarnessError) as arguments:
        await executor.execute(
            "inventory.lookup",
            operation_id="argument-budget",
            run_id="run-1",
            attempt_id="attempt-1",
            arguments={"large": "value"},
            admission=_admission(),
        )
    assert arguments.value.code == "CAPABILITY_ARGUMENT_BUDGET_EXCEEDED"

    with pytest.raises(HarnessError) as metadata:
        await executor.execute(
            "inventory.lookup",
            operation_id="metadata-budget",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=_admission(),
            safe_metadata={"large": "value"},
        )
    assert metadata.value.code == "CAPABILITY_METADATA_BUDGET_EXCEEDED"
    with pytest.raises(OperationNotFoundError):
        store.get_operation("argument-budget")
    with pytest.raises(OperationNotFoundError):
        store.get_operation("metadata-budget")


def test_capability_operation_metadata_cannot_override_authoritative_fields():
    with pytest.raises(HarnessError) as failure:
        _spec().operation_intent(
            operation_id="operation-1",
            run_id="run-1",
            attempt_id="attempt-1",
            request_digest="sha256:request",
            profile="durable",
            metadata={"capability_digest": "sha256:forged"},
        )
    assert failure.value.code == "CAPABILITY_METADATA_RESERVED"
