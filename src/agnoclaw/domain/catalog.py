"""Resolve domain definitions once; inspect, diff and evaluate immutable manifests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, TypeVar, cast

from agnoclaw.capabilities import CapabilityRegistry, CapabilitySpec
from agnoclaw.runtime.errors import HarnessError
from agnoclaw.runtime.security import canonical_json_digest, thaw_data

from ._contracts import Record, digest_string, fail, frozen_json, identifier
from .authoring import Check, Domain, Procedure
from .behavior import EvaluationSuite, SkillBinding
from .components import DomainComponent, component_digest
from .decisions import DecisionTable
from .dispatch import Action
from .evaluation import EvaluationSummary
from .extensions import Extensions, Hook, Listener
from .identity import ContractRef
from .outputs import OutputSpec
from .verification import CheckStatus

ComponentT = TypeVar("ComponentT")


@dataclass(frozen=True)
class Diagnostic(Record):
    code: str
    path: str
    message: str


class DomainValidationError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "Domain validation failed:\n"
            + "\n".join(f"{d.path}: [{d.code}] {d.message}" for d in diagnostics)
        )


@dataclass(frozen=True)
class Definition(Record):
    kind: str
    reference: str
    fingerprint: str
    dependencies: Mapping[str, str]
    details: Mapping[str, Any]
    origins: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.kind, "kind")
        if "/" in self.kind or "@" in self.kind or any(c.isspace() for c in self.kind):
            raise ValueError("definition kind cannot contain delimiters or whitespace")
        ContractRef.parse(self.reference)
        digest_string(self.fingerprint)
        for dependency in self.dependencies.values():
            digest_string(dependency)
        object.__setattr__(self, "dependencies", frozen_json(self.dependencies))
        object.__setattr__(self, "details", frozen_json(self.details))
        object.__setattr__(self, "origins", tuple(self.origins))

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.reference}"


@dataclass(frozen=True)
class Override:
    replacement: object
    expected_digest: str
    reason: str

    def __post_init__(self) -> None:
        digest_string(self.expected_digest)
        identifier(self.reason, "reason")


@dataclass(frozen=True)
class DomainDiff(Record):
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    affected: tuple[str, ...]
    aliases_changed: tuple[str, ...] = ()
    pack_changed: bool = False


@dataclass(frozen=True)
class PackEvaluation(Record):
    manifest_digest: str
    suites: Mapping[str, EvaluationSummary]
    workspace_digest: str | None = None

    def __post_init__(self) -> None:
        digest_string(self.manifest_digest)
        if self.workspace_digest is not None:
            digest_string(self.workspace_digest)
        object.__setattr__(self, "suites", MappingProxyType(dict(self.suites)))

    @property
    def passed(self) -> bool:
        return bool(self.suites) and all(
            result.status is CheckStatus.PASS
            for suite in self.suites.values()
            for result in suite.results
        )

    def require_current(
        self, domain: ResolvedDomain, *, workspace_digest: str | None = None
    ) -> None:
        if self.manifest_digest != domain.digest or self.workspace_digest != workspace_digest:
            fail("EVALUATION_STALE", "Evaluation describes different pack or workspace content.")


@dataclass(frozen=True)
class ValidationReport(Record):
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not self.diagnostics

    def require_valid(self) -> None:
        if self.diagnostics:
            raise DomainValidationError(self.diagnostics)


@dataclass(frozen=True)
class ResolvedDomain:
    name: str
    version: str
    definitions: Mapping[str, Definition]
    aliases: Mapping[str, str]
    packages: Mapping[str, str]
    _implementations: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("definitions", "aliases", "packages", "_implementations"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def manifest(self) -> dict[str, Any]:
        return {
            "format": 1,
            "name": self.name,
            "version": self.version,
            "definitions": [value.to_dict() for _, value in sorted(self.definitions.items())],
            "aliases": dict(sorted(self.aliases.items())),
            "packages": dict(sorted(self.packages.items())),
        }

    @property
    def digest(self) -> str:
        return canonical_json_digest(self.manifest())

    def definition(self, kind: str, reference: str | ContractRef) -> Definition:
        reference = str(reference)
        if kind == "schema":
            reference = self.aliases.get(reference, reference)
        matches = [
            item
            for item in self.definitions.values()
            if item.kind == kind
            and (
                item.reference == reference
                or ("@" not in reference and ContractRef.parse(item.reference).name == reference)
            )
        ]
        if len(matches) != 1:
            code = "UNRESOLVED_REFERENCE" if not matches else "AMBIGUOUS_REFERENCE"
            raise DomainValidationError(
                (
                    Diagnostic(
                        code, f"{kind}/{reference}", "Select one registered definition/version."
                    ),
                )
            )
        return matches[0]

    def get(self, kind: str, reference: str | ContractRef) -> object:
        definition = self.definition(kind, reference)
        value = self._implementations[definition.key]
        if component_digest(value) != definition.fingerprint:
            fail("DEFINITION_DRIFT", "Definition changed after compilation; compile a new version.")
        return value

    def schema(self, reference: str | ContractRef) -> OutputSpec:
        return cast(OutputSpec, self.get("schema", reference))

    def use(self, definition: ComponentT) -> ComponentT:
        """Resolve a direct definition while retaining its static Python type."""
        kind, reference, _, _ = _info(definition)
        value = self.get(kind, reference)
        if component_digest(value) != component_digest(definition):
            fail("DEFINITION_DRIFT", "Requested definition differs from the compiled pack.")
        return cast(ComponentT, value)

    def procedure(self, reference: str | ContractRef) -> Procedure:
        return cast(Procedure, self.get("procedure", reference))

    def capabilities(self) -> tuple[CapabilitySpec, ...]:
        return tuple(
            cast(CapabilitySpec, self.get(d.kind, d.reference))
            for d in self.definitions.values()
            if d.kind == "capability"
        )

    def extensions(self, *, tenant_id: str) -> Extensions:
        scope = Extensions(tenant_id=tenant_id)
        for definition in self.definitions.values():
            if definition.kind in {"hook", "listener"}:
                scope.register(
                    cast(Hook | Listener, self.get(definition.kind, definition.reference))
                )
        return scope

    def validate_host(self, registry: CapabilityRegistry) -> ValidationReport:
        """Check registry bindings without materializing any capability factories."""
        diagnostics = []
        for expected in self.capabilities():
            reference = f"{expected.name}@{expected.version}"
            try:
                actual = registry.resolve(reference)
            except HarnessError:
                diagnostics.append(
                    Diagnostic(
                        "HOST_BINDING_MISSING",
                        reference,
                        "Register this capability in the host registry.",
                    )
                )
                continue
            if actual.digest != expected.digest:
                diagnostics.append(
                    Diagnostic(
                        "HOST_BINDING_CHANGED",
                        reference,
                        "Host implementation differs from the pinned definition.",
                    )
                )
            if actual.factory is None:
                diagnostics.append(
                    Diagnostic(
                        "HOST_FACTORY_MISSING",
                        reference,
                        "Host capability has no executable factory.",
                    )
                )
        return ValidationReport(tuple(diagnostics))

    async def evaluate(
        self, suite: str | None = None, *, workspace_digest: str | None = None
    ) -> PackEvaluation:
        selected = (
            [self.definition("evaluation", suite)]
            if suite
            else [d for d in self.definitions.values() if d.kind == "evaluation"]
        )
        if not selected:
            raise ValueError("domain has no evaluation suites")
        results = {}
        for definition in selected:
            runner = cast(EvaluationSuite, self.get("evaluation", definition.reference))
            results[definition.reference] = await runner.evaluate()
        return PackEvaluation(self.digest, results, workspace_digest)

    def diff(self, previous: ResolvedDomain) -> DomainDiff:
        old, new = previous.definitions, self.definitions
        changed = {
            key for key in old.keys() & new.keys() if old[key].fingerprint != new[key].fingerprint
        }
        added, removed = set(new) - set(old), set(old) - set(new)
        affected = changed | added | removed
        aliases_changed = {
            alias
            for alias in self.aliases.keys() | previous.aliases.keys()
            if self.aliases.get(alias) != previous.aliases.get(alias)
        }
        for alias in aliases_changed:
            for aliases in (self.aliases, previous.aliases):
                if alias in aliases:
                    affected.add(f"schema/{aliases[alias]}")
        while True:
            dependents = {
                key
                for key, definition in {**old, **new}.items()
                if set(definition.dependencies) & affected
            }
            if dependents <= affected:
                break
            affected |= dependents
        return DomainDiff(
            tuple(sorted(added)),
            tuple(sorted(removed)),
            tuple(sorted(changed)),
            tuple(sorted(affected)),
            tuple(sorted(aliases_changed)),
            (self.name, self.version) != (previous.name, previous.version)
            or self.packages != previous.packages,
        )


def inspect_domain_manifest(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach an exported manifest without importing executable code."""
    result = thaw_data(frozen_json(data))
    if (
        not isinstance(result, dict)
        or set(result) != {"format", "name", "version", "definitions", "aliases", "packages"}
        or type(result["format"]) is not int
        or result["format"] != 1
    ):
        raise ValueError("unsupported domain manifest format")
    if (
        not isinstance(result["definitions"], list)
        or not all(isinstance(item, dict) for item in result["definitions"])
        or not isinstance(result["aliases"], dict)
        or not isinstance(result["packages"], dict)
    ):
        raise ValueError("invalid domain manifest containers")
    ContractRef(result["name"], result["version"])
    for reference, fingerprint in result["packages"].items():
        ContractRef.parse(reference)
        digest_string(fingerprint)
    if str(ContractRef(result["name"], result["version"])) not in result["packages"]:
        raise ValueError("manifest must pin its root package")
    if len(result["definitions"]) > 1024:
        raise ValueError("manifest exceeds 1024 definitions")
    definitions = [Definition(**item) for item in result["definitions"]]
    by_key = {item.key: item for item in definitions}
    if len(by_key) != len(definitions):
        raise ValueError("manifest contains duplicate definitions")
    for item in definitions:
        for key, fingerprint in item.dependencies.items():
            if key not in by_key or by_key[key].fingerprint != fingerprint:
                raise ValueError(f"manifest has unresolved or changed dependency: {key}")
    for alias, target in result["aliases"].items():
        identifier(alias, "alias")
        if f"schema/{target}" not in by_key:
            raise ValueError(f"unresolved schema alias: {alias}")
    unresolved = set(by_key)
    while unresolved:
        leaves = {key for key in unresolved if not (set(by_key[key].dependencies) & unresolved)}
        if not leaves:
            raise ValueError("manifest contains a dependency cycle")
        unresolved -= leaves
    return result


def _info(value: object) -> tuple[str, str, Mapping[str, Any], tuple[object, ...]]:
    if isinstance(value, OutputSpec):
        return (
            "schema",
            value.reference,
            {"json_schema": value.json_schema(), "metadata": value.metadata},
            (),
        )
    if isinstance(value, Procedure):
        skill = (value.skill,) if isinstance(value.skill, SkillBinding) else ()
        return (
            "procedure",
            str(value.ref),
            value.spec.manifest(),
            (value.output, *value.checks, *skill),
        )
    if isinstance(value, Check):
        return (
            "check",
            str(value.ref),
            {
                "implementation_id": value.implementation_id,
                "timeout_seconds": value.timeout_seconds,
            },
            (),
        )
    if isinstance(value, Action):
        return (
            "action",
            value.spec.reference,
            {"review_required": value.review_required},
            (value.capability, *value.checks),
        )
    if isinstance(value, CapabilitySpec):
        return "capability", f"{value.name}@{value.version}", value.manifest(), ()
    if isinstance(value, DecisionTable):
        return (
            "decision",
            f"{value.name}@{value.version}",
            {
                "implementation_digest": value.implementation_digest,
                "rules": [{"id": r.rule_id, "outcome": r.outcome} for r in value.rules],
            },
            (),
        )
    if isinstance(value, DomainComponent):
        return value.kind, str(value.ref), value.manifest(), value.dependencies()
    raise TypeError(f"unsupported domain component {type(value).__name__}")


def _same_implementation(a: object, b: object) -> bool:
    if a is b:
        return True
    for attribute in (
        "content_model",
        "evaluate",
        "run",
        "callback",
        "handle",
        "compare",
        "factory",
        "detect",
        "function",
    ):
        if attribute in vars(a) and getattr(a, attribute) is not getattr(b, attribute, None):
            return False
    if isinstance(a, DecisionTable) and isinstance(b, DecisionTable):
        return all(x.predicate is y.predicate for x, y in zip(a.rules, b.rules, strict=True))
    return True


def resolve_domain(
    domain: Domain, *, raise_errors: bool = True
) -> ResolvedDomain | ValidationReport:
    entries: dict[str, Definition] = {}
    implementations: dict[str, object] = {}
    errors: list[Diagnostic] = []
    aliases: dict[str, str] = {}
    package_pins: dict[str, str] = {}
    visiting: set[int] = set()

    def error(code: str, path: str, message: str) -> None:
        errors.append(Diagnostic(code, path, message))

    def visit(value: object, path: str, override: Override | None = None) -> str | None:
        if id(value) in visiting:
            error("DEPENDENCY_CYCLE", path, "Definition dependency cycle.")
            return None
        if len(entries) >= 1024:
            error("CATALOG_LIMIT", path, "Domain exceeds 1024 definitions.")
            return None
        visiting.add(id(value))
        try:
            kind, reference, details, children = _info(value)
            dependencies = {}
            for index, child in enumerate(children):
                key = visit(child, f"{path}.dependencies[{index}]")
                if key is not None:
                    dependencies[key] = component_digest(child)
            item = Definition(
                kind, reference, component_digest(value), dependencies, details, (path,)
            )
            old = entries.get(item.key)
            if override:
                if old is None or old.fingerprint != override.expected_digest:
                    error(
                        "OVERRIDE_STALE",
                        path,
                        "Override requires the exact existing definition digest.",
                    )
                    return item.key
                item = Definition(
                    kind,
                    reference,
                    item.fingerprint,
                    dependencies,
                    {**details, "override_reason": override.reason},
                    (*old.origins, path),
                )
            elif old:
                if old.fingerprint != item.fingerprint or not _same_implementation(
                    implementations[item.key], value
                ):
                    error(
                        "DEFINITION_CONFLICT",
                        path,
                        f"Conflicting {item.key}; first defined at {old.origins[0]}.",
                    )
                    return item.key
                item = Definition(
                    kind,
                    reference,
                    item.fingerprint,
                    dependencies,
                    details,
                    tuple(dict.fromkeys((*old.origins, path))),
                )
            entries[item.key], implementations[item.key] = item, value
            return item.key
        except (ValueError, TypeError, RecursionError) as exc:
            error("INVALID_DEFINITION", path, str(exc))
            return None
        finally:
            visiting.remove(id(value))

    packs: set[int] = set()

    def collect(pack: Domain, path: str) -> str | None:
        if id(pack) in packs:
            error("PACK_CYCLE", path, "Domain includes form a cycle.")
            return None
        packs.add(id(pack))
        includes = []
        for index, included in enumerate(pack.includes):
            includes.append(collect(included, f"{path}.includes[{index}]"))
        local = []
        for index, value in enumerate((*pack.procedures, *pack.components)):
            key = visit(value, f"{path}.definitions[{index}]")
            if key is not None and key in entries:
                local.append((key, entries[key].fingerprint))
        replacements = []
        for index, override in enumerate(pack.overrides):
            key = visit(override.replacement, f"{path}.overrides[{index}]", override)
            if key is not None and key in entries:
                replacements.append(
                    (key, entries[key].fingerprint, override.expected_digest, override.reason)
                )
        for alias, schema in pack.aliases.items():
            visit(schema, f"{path}.aliases[{alias}]")
            if alias in aliases and aliases[alias] != schema.reference:
                error("ALIAS_CONFLICT", path, f"Alias {alias} names different schema versions.")
            aliases[alias] = schema.reference
        packs.remove(id(pack))
        reference = str(ContractRef(pack.name, pack.version))
        fingerprint = canonical_json_digest(
            [
                reference,
                includes,
                local,
                replacements,
                {name: schema.digest for name, schema in pack.aliases.items()},
            ]
        )
        if reference in package_pins and package_pins[reference] != fingerprint:
            error("PACK_CONFLICT", path, f"Package {reference} has conflicting definitions.")
        package_pins[reference] = fingerprint
        return fingerprint

    collect(domain, domain.name)
    from .resources import Resources

    resources = {
        path
        for item in implementations.values()
        if isinstance(item, Resources)
        for path in item.paths
    }
    for key, entry in entries.items():
        for dependency, fingerprint in entry.dependencies.items():
            if dependency not in entries or entries[dependency].fingerprint != fingerprint:
                error(
                    "DEPENDENCY_CHANGED",
                    entry.origins[0],
                    f"Rebind changed dependency {dependency}.",
                )
        value = implementations[key]
        if isinstance(value, SkillBinding) and value.resource and value.resource not in resources:
            error(
                "RESOURCE_UNDECLARED", entry.origins[0], f"Declare skill resource {value.resource}."
            )
        if isinstance(value, Procedure) and value.skill:
            skill_name = value.skill if isinstance(value.skill, str) else str(value.skill.ref)
            matches = [
                d
                for d in entries.values()
                if d.kind == "skill"
                and (d.reference == skill_name or ContractRef.parse(d.reference).name == skill_name)
            ]
            if len(matches) != 1:
                error(
                    "SKILL_UNRESOLVED",
                    entry.origins[0],
                    f"Bind exactly one skill for {skill_name}.",
                )
            elif (
                cast(SkillBinding, implementations[matches[0].key]).output.digest
                != value.output.digest
            ):
                error(
                    "SKILL_OUTPUT_MISMATCH", entry.origins[0], "Skill and procedure outputs differ."
                )
            else:
                selected_skill = matches[0]
                entries[key] = replace(
                    entry,
                    dependencies={
                        **entry.dependencies,
                        selected_skill.key: selected_skill.fingerprint,
                    },
                )
    report = ValidationReport(tuple(errors))
    if not raise_errors:
        return report
    report.require_valid()
    return ResolvedDomain(
        domain.name, domain.version, entries, aliases, package_pins, implementations
    )
