"""Function-first authoring over the explicit domain contracts."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import wraps
from typing import Any, Generic, ParamSpec, Protocol, TypeVar, cast, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, create_model

from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.security import thaw_data

from ._contracts import Record, fail, frozen_json, identifier
from .authoring import Check, CheckOutcome, Domain, OutputSchema, PreparedWork, Procedure
from .behavior import EvaluationSuite
from .catalog import PackEvaluation, ResolvedDomain
from .components import DomainComponent, resolve, timeout
from .evaluation import EvaluationCase
from .evidence import EvidenceRef
from .extensions import Extensions, Hook
from .identity import ContractRef
from .objects import ObjectRef, ObjectSnapshot
from .outputs import WorkProduct
from .verification import CheckResult, CheckStatus

P = ParamSpec("P")
R = TypeVar("R", bound=BaseModel)
C = TypeVar("C", bound=bool | CheckOutcome | Awaitable[bool | CheckOutcome])


class Model(BaseModel):
    """Pydantic model with strict domain defaults; normal Field/validators work."""

    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


@dataclass(frozen=True)
class _Value(Record):
    schema_digest: str
    content: Mapping[str, Any]


_active_run: ContextVar[PackRun | None] = ContextVar("agnoclaw_domain_pack_run", default=None)


def _require_checks(name: str, results: Sequence[CheckResult]) -> None:
    for result in results:
        if result.status is not CheckStatus.PASS:
            fail(
                "VERIFICATION_REQUIRED",
                f"{name}: check '{result.check_id}' {result.status.value}: {result.reason}",
            )


class ProcedureFunction(Protocol[P, R]):
    """A real async Python function with attached authoring helpers."""

    inputs: OutputSchema
    output: OutputSchema
    definition: Procedure
    function: Callable[P, R | Awaitable[R]]

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    def check(self, function: Callable[[R], C]) -> Callable[[R], C]: ...

    def example(self, *args: Any, expected: R | Mapping[str, Any], **kwargs: Any) -> None: ...


class TypedProcedure(DomainComponent, Generic[P, R]):
    """An async callable retaining its Python parameters and typed return value."""

    kind = "operation"

    def __init__(
        self,
        pack: Pack,
        function: Callable[P, R | Awaitable[R]],
        *,
        name: str,
        version: str,
        timeout_seconds: float,
    ) -> None:
        self.pack, self.function = pack, function
        self.name, self.version, self.timeout_seconds = name, version, timeout_seconds
        ContractRef(name, version)
        timeout(timeout_seconds)
        self.signature = inspect.signature(function)
        try:
            hints = get_type_hints(function, include_extras=True)
        except (NameError, TypeError) as exc:
            raise TypeError(
                f"{function.__name__}: resolve annotations before registration"
            ) from exc
        output = hints.get("return")
        if not isinstance(output, type) or not issubclass(output, BaseModel):
            raise TypeError(f"{function.__name__}: annotate its return with a domain Model")
        self.model = cast(type[R], output)
        self.output = pack._schema(output)
        fields: dict[str, Any] = {}
        for parameter in self.signature.parameters.values():
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                raise TypeError(
                    f"{function.__name__}: use named, typed parameters instead of *args/**kwargs"
                )
            if parameter.name not in hints:
                raise TypeError(f"{function.__name__}: annotate parameter '{parameter.name}'")
            default = ... if parameter.default is parameter.empty else parameter.default
            fields[parameter.name] = (hints[parameter.name], default)
        self._inputs = create_model(f"{function.__name__}Inputs", __base__=Model, **fields)
        self.inputs = OutputSchema(f"{name}.input", self._inputs, version=version)
        self._checks: list[Check[WorkProduct]] = []
        self._value_checks: list[Check[_Value]] = []
        self._cases: list[EvaluationCase] = []
        self._definition: Procedure | None = None

        @wraps(function, updated=())
        async def call(*args: P.args, **kwargs: P.kwargs) -> R:
            return await self(*args, **kwargs)

        # Real functions integrate with Agno/Pydantic introspection. Resolve postponed
        # annotations in the author's module before handing off to those frameworks.
        call.__annotations__ = hints
        self.call = cast(ProcedureFunction[P, R], call)
        self.call.inputs, self.call.output = self.inputs, self.output
        self.call.definition, self.call.function = self.definition, function
        call.__dict__.update(check=self.check, example=self.example)

    def manifest(self) -> Mapping[str, Any]:
        return {
            "handler": f"{self.function.__module__}:{self.function.__qualname__}",
            "timeout_seconds": self.timeout_seconds,
        }

    def dependencies(self) -> tuple[object, ...]:
        return (self.inputs, self.definition)

    def _arguments(self, *args: Any, **kwargs: Any) -> inspect.BoundArguments:
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        validated = self._inputs.model_validate(bound.arguments, strict=True)
        # Check the pinned JSON contract too, including nested BaseModel instances
        # whose own configuration may skip instance revalidation.
        self.inputs.validate_content(validated.model_dump(mode="json", by_alias=True))
        bound.arguments.update({name: getattr(validated, name) for name in bound.arguments})
        return bound

    def _model(self, content: Mapping[str, Any]) -> R:
        return self.model.model_validate_json(json.dumps(thaw_data(content)), strict=True)

    @property
    def definition(self) -> Procedure:
        if self._definition is None:

            def valid_output(work: WorkProduct) -> bool:
                self.output.validate_content(work.content)
                return True

            self._definition = Procedure(
                self.name,
                self.output,
                version=self.version,
                checks=(
                    Check(f"{self.name}.output", valid_output, version=self.version),
                    *self._checks,
                ),
            )
        return self._definition

    def check(self, function: Callable[[R], C]) -> Callable[[R], C]:
        """Attach a check receiving the typed output, preserving the check function."""
        self.pack._require_editable()
        name = f"{self.name}.{function.__name__}"
        if name == f"{self.name}.output" or any(c.name == name for c in self._checks):
            raise ValueError(f"{self.name}: duplicate/reserved check name '{function.__name__}'")

        def check_work(work: WorkProduct) -> Any:
            return function(self._model(work.content))

        def check_value(value: _Value) -> Any:
            return function(self._model(value.content))

        self._checks.append(Check(name, check_work, version=self.version))
        self._value_checks.append(Check(name, check_value, version=self.version))
        self._definition = None
        self.call.definition = self.definition
        return function

    def example(self, *args: Any, expected: R | Mapping[str, Any], **kwargs: Any) -> None:
        """Register a normal call and expected output for ``await pack.test()``."""
        self.pack._require_editable()
        bound = self._arguments(*args, **kwargs)
        inputs = self._inputs.model_validate(bound.arguments).model_dump(mode="json")
        output = (
            expected.model_dump(mode="json", by_alias=True)
            if isinstance(expected, BaseModel)
            else expected
        )
        self._cases.append(
            EvaluationCase(
                f"{self.name}:example-{len(self._cases) + 1}",
                inputs,
                self.output.validate_content(output),
            )
        )

    async def _case(self, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        # Fixtures are independent function calls, never attached to a live host run.
        token = _active_run.set(None)
        try:
            parsed = self._inputs.model_validate_json(json.dumps(thaw_data(inputs)), strict=True)
            bound = self.signature.bind_partial()
            bound.arguments.update(
                {name: getattr(parsed, name) for name in self.signature.parameters}
            )
            result = await self(*bound.args, **bound.kwargs)
            return result.model_dump(mode="json", by_alias=True)
        finally:
            _active_run.reset(token)

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        run = _active_run.get()
        compiled = run.compiled if run is not None else self.pack.compile()
        if compiled.use(self) is not self:
            fail("DEFINITION_DRIFT", "Call the procedure registered in this pack.")
        if run is not None:
            run._require_open()
        elif any(
            d.kind == "hook"
            and cast(Hook, compiled.get(d.kind, d.reference)).schema.reference
            == self.output.reference
            for d in compiled.definitions.values()
        ):
            raise ValueError(f"{self.name}: context hooks require 'async with pack.run(...)'")
        bound = self._arguments(*args, **kwargs)
        async with asyncio.timeout(self.timeout_seconds):
            result = await resolve(self.function(*bound.args, **bound.kwargs))
        raw = (
            result.model_dump(mode="json", by_alias=True)
            if isinstance(result, BaseModel)
            else result
        )
        content = self.output.validate_content(raw)
        if run is not None:
            run._require_open()
            content = dict(await run.extensions.apply(self.output, content, context=run.context))
            prepared = await self.definition.prepare(
                content,
                target=run.target,
                run_id=run.run_id,
                context=run.context,
                evidence=run.evidence,
            )
            run._require_open()
            run._results.append(prepared)
            _require_checks(self.name, prepared.report.results)
            prepared.require_ready()
        else:
            value = _Value(self.output.schema_digest, frozen_json(content))
            _require_checks(self.name, [await check.run(value) for check in self._value_checks])
        return self._model(content)


class Pack:
    """Declare typed functions; compile once automatically on first use.

    Register definitions, checks and examples before calling or embedding the pack.
    Compilation starts no models or workers. Functions execute normally, with no
    implicit durable replay or authorization of side effects.
    """

    def __init__(
        self, name: str, *, version: str = "1", includes: Sequence[Domain | Pack] = ()
    ) -> None:
        ContractRef(name, version)
        self.name, self.version = name, version
        self._includes = tuple(includes)
        if any(not isinstance(p, (Domain, Pack)) for p in self._includes):
            raise TypeError("includes must contain Pack or Domain definitions")
        self._procedures: dict[str, TypedProcedure[Any, Any]] = {}
        self._schemas: dict[type[BaseModel], OutputSchema] = {}
        self._components: list[object] = []
        self._aliases: dict[str, OutputSchema] = {}
        self._compiled: ResolvedDomain | None = None
        self._declaration: Domain | None = None

    def _require_editable(self) -> None:
        if self._compiled is not None:
            raise ValueError(
                f"Pack '{self.name}' is active; declare definitions/checks/examples "
                "before its first call"
            )

    def _schema(self, model: type[BaseModel]) -> OutputSchema:
        if model not in self._schemas:
            self._require_editable()
            self._schemas[model] = OutputSchema(
                f"{self.name}.{model.__name__}", model, version=self.version
            )
        return self._schemas[model]

    @overload
    def procedure(
        self,
        function: Callable[P, R | Awaitable[R]],
        *,
        name: str | None = None,
        version: str | None = None,
        timeout_seconds: float = 60,
    ) -> ProcedureFunction[P, R]: ...

    @overload
    def procedure(
        self,
        function: None = None,
        *,
        name: str | None = None,
        version: str | None = None,
        timeout_seconds: float = 60,
    ) -> Callable[[Callable[P, R | Awaitable[R]]], ProcedureFunction[P, R]]: ...

    def procedure(
        self,
        function: Any = None,
        *,
        name: str | None = None,
        version: str | None = None,
        timeout_seconds: float = 60,
    ) -> Any:
        def register(callback: Any) -> ProcedureFunction[Any, Any]:
            self._require_editable()
            selected = name or f"{self.name}.{callback.__name__}"
            if selected in self._procedures:
                raise ValueError(
                    f"Pack '{self.name}': procedure '{selected}' is already registered"
                )
            operation = TypedProcedure(
                self,
                callback,
                name=selected,
                version=self.version if version is None else version,
                timeout_seconds=timeout_seconds,
            )
            self._procedures[selected] = operation
            return operation.call

        return register(function) if function is not None else register

    def add(self, *definitions: object) -> None:
        """Use existing Entity/Relation/Hook/etc. declarations when needed."""
        self._require_editable()
        self._components.extend(definitions)

    def alias(self, name: str, model: type[BaseModel]) -> None:
        self._require_editable()
        selected = self._schema(model)
        if name in self._aliases and self._aliases[name] is not selected:
            raise ValueError(f"Alias '{name}' already names a different model")
        self._aliases[name] = selected

    def _domain(self) -> Domain:
        if self._declaration is not None:
            return self._declaration
        suites = [
            EvaluationSuite(
                f"{p.name}.examples",
                tuple(p._cases),
                p._case,
                implementation_id=f"{p.function.__module__}:{p.function.__qualname__}:{p.version}",
                subjects=(p,),
                version=p.version,
                timeout_seconds=p.timeout_seconds,
            )
            for p in self._procedures.values()
            if p._cases
        ]
        return Domain(
            self.name,
            version=self.version,
            components=(*self._procedures.values(), *self._components, *suites),
            aliases=self._aliases,
            includes=tuple(p._domain() if isinstance(p, Pack) else p for p in self._includes),
        )

    def compile(self) -> ResolvedDomain:
        if self._compiled is None:
            for included in self._includes:
                if isinstance(included, Pack):
                    included.compile()
            declaration = self._domain()
            self._compiled = declaration.compile()
            self._declaration = declaration
        return self._compiled

    async def test(self) -> PackEvaluation:
        compiled = self.compile()
        if not any(d.kind == "evaluation" for d in compiled.definitions.values()):
            raise ValueError(
                f"Pack '{self.name}' has no examples; declare "
                "procedure.example(..., expected=...) before its first call"
            )
        report = await compiled.evaluate()
        if not report.passed:
            failures = [
                f"{r.case_id}: {r.status.value}"
                for suite in report.suites.values()
                for r in suite.results
                if r.status is not CheckStatus.PASS
            ]
            raise AssertionError("Domain examples failed:\n" + "\n".join(failures))
        return report

    def run(
        self,
        target: ObjectRef | ObjectSnapshot,
        *,
        context: ExecutionContext | None = None,
        run_id: str | None = None,
        evidence: Sequence[EvidenceRef] = (),
    ) -> PackRun:
        """Bind host identity once; an active governed tool can supply context/run ID."""
        from agnoclaw.agent import get_current_tool_runtime

        runtime = get_current_tool_runtime()
        if runtime is not None:
            active_context, active_run_id = runtime.get("context"), runtime.get("parent_run_id")
            if not isinstance(active_context, ExecutionContext) or not isinstance(
                active_run_id, str
            ):
                raise ValueError("The active tool has no authoritative context/run identity")
            if (context is not None and context != active_context) or (
                run_id is not None and run_id != active_run_id
            ):
                fail("RUN_CONTEXT_MISMATCH", "Explicit identity differs from the active tool run.")
            context, run_id = active_context, active_run_id
        elif context is None or run_id is None:
            raise ValueError("Outside a governed tool, pack.run needs context= and run_id=")
        return PackRun(
            self.compile(),
            target.reference if isinstance(target, ObjectSnapshot) else target,
            context,
            run_id,
            tuple(evidence),
        )


class PackRun:
    """Request-scoped receipts and hooks; no global registry or new worker."""

    def __init__(
        self,
        compiled: ResolvedDomain,
        target: ObjectRef,
        context: ExecutionContext,
        run_id: str,
        evidence: tuple[EvidenceRef, ...],
    ) -> None:
        target.require_scope(context)
        identifier(run_id, "run_id")
        if any(e.source.tenant_id != target.tenant_id for e in evidence):
            fail("SCOPE_MISMATCH", "Run evidence belongs to another tenant.")
        self.compiled, self.target, self.context, self.run_id, self.evidence = (
            compiled,
            target,
            context,
            run_id,
            evidence,
        )
        self.extensions: Extensions = compiled.extensions(tenant_id=target.tenant_id)
        self._results: list[PreparedWork] = []
        self._token: Token[PackRun | None] | None = None
        self._closed = False

    @property
    def results(self) -> tuple[PreparedWork, ...]:
        return tuple(self._results)

    @property
    def last(self) -> PreparedWork:
        if not self._results:
            raise ValueError("No procedure has produced a result in this run")
        return self._results[-1]

    def _require_open(self) -> None:
        if self._closed or self._token is None:
            raise RuntimeError("Domain run is not active")

    async def __aenter__(self) -> PackRun:
        if self._closed or self._token is not None:
            raise RuntimeError("Domain run scopes cannot be reused")
        self._token = _active_run.set(self)
        return self

    async def __aexit__(self, *args: object) -> None:
        self._require_open()
        token = self._token
        assert token is not None
        _active_run.reset(token)
        self._closed = True
        await self.extensions.aclose(cancel=True)
