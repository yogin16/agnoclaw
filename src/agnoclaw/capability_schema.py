"""Bounded fail-closed JSON Schema validation for capability arguments.

The core deliberately implements a documented interoperable subset instead of
pretending provider-side schema hints are an authorization boundary. Unknown
assertion keywords fail at capability construction; annotations remain annotations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from .runtime.errors import HarnessError

MAX_CAPABILITY_SCHEMA_BYTES = 65_536
MAX_CAPABILITY_SCHEMA_DEPTH = 32
MAX_CAPABILITY_SCHEMA_NODES = 4_096
MAX_CAPABILITY_ARGUMENT_DEPTH = 32
MAX_CAPABILITY_ARGUMENT_NODES = 10_000

_ANNOTATIONS = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "format",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_ASSERTIONS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "dependentRequired",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "if",
        "items",
        "maxContains",
        "maximum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "minContains",
        "minimum",
        "minItems",
        "minLength",
        "minProperties",
        "multipleOf",
        "not",
        "oneOf",
        "prefixItems",
        "properties",
        "required",
        "then",
        "type",
        "uniqueItems",
    }
)
_SUPPORTED_KEYWORDS = _ANNOTATIONS | _ASSERTIONS
_JSON_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})


@dataclass(frozen=True)
class _Mismatch(Exception):
    keyword: str
    schema_path: tuple[str | int, ...]


def _schema_error(
    *,
    code: str,
    message: str,
    capability: str | None,
    keyword: str | None = None,
    schema_path: Sequence[str | int] = (),
) -> HarnessError:
    details: dict[str, Any] = {"schema_path": tuple(schema_path)}
    if capability is not None:
        details["capability"] = capability
    if keyword is not None:
        details["keyword"] = keyword
    return HarnessError(
        code=code,
        category="capability",
        message=message,
        retryable=False,
        details=details,
    )


def _canonical_size(value: Any, *, capability: str | None) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability input schema must contain JSON-compatible values.",
            capability=capability,
        ) from exc
    return len(encoded)


def _require_nonnegative_integer(value: Any, *, path: tuple[Any, ...]) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability input schema has an invalid non-negative integer.",
            capability=None,
            schema_path=path,
        )


def _require_number(value: Any, *, path: tuple[Any, ...], positive: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not isfinite(value))
        or (positive and value <= 0)
    ):
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability input schema has an invalid numeric assertion.",
            capability=None,
            schema_path=path,
        )


def _check_schema(
    schema: Any,
    *,
    path: tuple[str | int, ...],
    depth: int,
    counter: list[int],
    seen: set[int],
) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability schema nodes must be objects or booleans.",
            capability=None,
            schema_path=path,
        )
    if depth > MAX_CAPABILITY_SCHEMA_DEPTH:
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_BUDGET_EXCEEDED",
            message="Capability input schema exceeds the nesting-depth budget.",
            capability=None,
            schema_path=path,
        )
    identity = id(schema)
    if identity in seen:
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability input schema contains a live object cycle.",
            capability=None,
            schema_path=path,
        )
    seen.add(identity)
    counter[0] += 1
    if counter[0] > MAX_CAPABILITY_SCHEMA_NODES:
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_BUDGET_EXCEEDED",
            message="Capability input schema exceeds the node budget.",
            capability=None,
            schema_path=path,
        )
    try:
        unknown = tuple(sorted(set(schema) - _SUPPORTED_KEYWORDS))
        if unknown:
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_UNSUPPORTED",
                message="Capability input schema uses unsupported keywords.",
                capability=None,
                keyword=unknown[0],
                schema_path=path + (unknown[0],),
            )
        raw_type = schema.get("type")
        if raw_type is not None:
            types = [raw_type] if isinstance(raw_type, str) else raw_type
            if (
                not isinstance(types, list)
                or not types
                or any(not isinstance(item, str) or item not in _JSON_TYPES for item in types)
                or len(set(types)) != len(types)
            ):
                raise _schema_error(
                    code="CAPABILITY_INPUT_SCHEMA_INVALID",
                    message="Capability input schema has an invalid type assertion.",
                    capability=None,
                    schema_path=path + ("type",),
                )
        if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability enum must be a non-empty array.",
                capability=None,
                schema_path=path + ("enum",),
            )
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping) or any(
            not isinstance(key, str) for key in properties
        ):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability properties must be an object with string keys.",
                capability=None,
                schema_path=path + ("properties",),
            )
        for key, subschema in properties.items():
            _check_schema(
                subschema,
                path=path + ("properties", key),
                depth=depth + 1,
                counter=counter,
                seen=seen,
            )
        definitions = schema.get("$defs", {})
        if not isinstance(definitions, Mapping) or any(
            not isinstance(key, str) for key in definitions
        ):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability $defs must be an object with string keys.",
                capability=None,
                schema_path=path + ("$defs",),
            )
        for key, subschema in definitions.items():
            _check_schema(
                subschema,
                path=path + ("$defs", key),
                depth=depth + 1,
                counter=counter,
                seen=seen,
            )
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
        ):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability required must contain unique string names.",
                capability=None,
                schema_path=path + ("required",),
            )
        dependencies = schema.get("dependentRequired", {})
        if not isinstance(dependencies, Mapping):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability dependentRequired must be an object.",
                capability=None,
                schema_path=path + ("dependentRequired",),
            )
        for key, names in dependencies.items():
            if (
                not isinstance(key, str)
                or not isinstance(names, list)
                or any(not isinstance(item, str) for item in names)
                or len(set(names)) != len(names)
            ):
                raise _schema_error(
                    code="CAPABILITY_INPUT_SCHEMA_INVALID",
                    message="Capability dependentRequired has an invalid entry.",
                    capability=None,
                    schema_path=path + ("dependentRequired", str(key)),
                )
        for keyword in ("additionalProperties", "items", "contains", "not", "if", "then", "else"):
            if keyword in schema:
                _check_schema(
                    schema[keyword],
                    path=path + (keyword,),
                    depth=depth + 1,
                    counter=counter,
                    seen=seen,
                )
        prefix = schema.get("prefixItems", [])
        if not isinstance(prefix, list):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability prefixItems must be an array.",
                capability=None,
                schema_path=path + ("prefixItems",),
            )
        for index, subschema in enumerate(prefix):
            _check_schema(
                subschema,
                path=path + ("prefixItems", index),
                depth=depth + 1,
                counter=counter,
                seen=seen,
            )
        for keyword in ("allOf", "anyOf", "oneOf"):
            if keyword not in schema:
                continue
            branches = schema[keyword]
            if not isinstance(branches, list) or not branches:
                raise _schema_error(
                    code="CAPABILITY_INPUT_SCHEMA_INVALID",
                    message=f"Capability {keyword} must be a non-empty array.",
                    capability=None,
                    schema_path=path + (keyword,),
                )
            for index, subschema in enumerate(branches):
                _check_schema(
                    subschema,
                    path=path + (keyword, index),
                    depth=depth + 1,
                    counter=counter,
                    seen=seen,
                )
        for keyword in (
            "maxContains",
            "maxItems",
            "maxLength",
            "maxProperties",
            "minContains",
            "minItems",
            "minLength",
            "minProperties",
        ):
            if keyword in schema:
                _require_nonnegative_integer(schema[keyword], path=path + (keyword,))
        for minimum, maximum in (
            ("minContains", "maxContains"),
            ("minItems", "maxItems"),
            ("minLength", "maxLength"),
            ("minProperties", "maxProperties"),
        ):
            if minimum in schema and maximum in schema and schema[minimum] > schema[maximum]:
                raise _schema_error(
                    code="CAPABILITY_INPUT_SCHEMA_INVALID",
                    message=f"Capability {minimum} cannot exceed {maximum}.",
                    capability=None,
                    schema_path=path,
                )
        for keyword in ("exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum"):
            if keyword in schema:
                _require_number(schema[keyword], path=path + (keyword,))
        if "multipleOf" in schema:
            _require_number(
                schema["multipleOf"],
                path=path + ("multipleOf",),
                positive=True,
            )
        if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability uniqueItems must be a boolean.",
                capability=None,
                schema_path=path + ("uniqueItems",),
            )
        if "$ref" in schema and (
            not isinstance(schema["$ref"], str) or not schema["$ref"].startswith("#")
        ):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_UNSUPPORTED",
                message="Only local capability schema references are supported.",
                capability=None,
                keyword="$ref",
                schema_path=path + ("$ref",),
            )
    finally:
        seen.remove(identity)


def _resolve_reference(root: Any, reference: str) -> Any:
    if reference == "#":
        return root
    if not reference.startswith("#/"):
        raise KeyError(reference)
    target: Any = root
    for raw in reference[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping):
            if token not in target:
                raise KeyError(reference)
            target = target[token]
        elif isinstance(target, list) and token.isdigit():
            index = int(token)
            if index >= len(target):
                raise KeyError(reference)
            target = target[index]
        else:
            raise KeyError(reference)
    return target


def _verify_references(
    schema: Any,
    *,
    root: Mapping[str, Any],
    path: tuple[str | int, ...],
    seen: set[int],
) -> None:
    if isinstance(schema, bool) or id(schema) in seen:
        return
    seen.add(id(schema))
    reference = schema.get("$ref")
    if reference is not None:
        try:
            target = _resolve_reference(root, reference)
        except KeyError:
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability input schema has an unresolved local reference.",
                capability=None,
                keyword="$ref",
                schema_path=path + ("$ref",),
            ) from None
        if not isinstance(target, (Mapping, bool)):
            raise _schema_error(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                message="Capability local reference does not target a schema node.",
                capability=None,
                keyword="$ref",
                schema_path=path + ("$ref",),
            )
        _verify_references(
            target,
            root=root,
            path=path + ("$ref",),
            seen=seen,
        )
    mappings = ("$defs", "properties")
    for keyword in mappings:
        for key, child in schema.get(keyword, {}).items():
            _verify_references(
                child,
                root=root,
                path=path + (keyword, key),
                seen=seen,
            )
    for keyword in (
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "then",
    ):
        if keyword in schema:
            _verify_references(
                schema[keyword],
                root=root,
                path=path + (keyword,),
                seen=seen,
            )
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for index, child in enumerate(schema.get(keyword, [])):
            _verify_references(
                child,
                root=root,
                path=path + (keyword, index),
                seen=seen,
            )


def validate_capability_input_schema(schema: Any, *, capability: str | None = None) -> None:
    """Validate one bounded object-input schema at capability construction."""
    if not isinstance(schema, Mapping):
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability input schema must be an object schema.",
            capability=capability,
        )
    raw_type = schema.get("type", "object")
    if raw_type != "object":
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_INVALID",
            message="Capability input schema root must have type 'object'.",
            capability=capability,
            keyword="type",
            schema_path=("type",),
        )
    size = _canonical_size(schema, capability=capability)
    if size > MAX_CAPABILITY_SCHEMA_BYTES:
        raise _schema_error(
            code="CAPABILITY_INPUT_SCHEMA_BUDGET_EXCEEDED",
            message="Capability input schema exceeds the byte budget.",
            capability=capability,
        )
    try:
        _check_schema(schema, path=(), depth=0, counter=[0], seen=set())
        _verify_references(schema, root=schema, path=(), seen=set())
    except HarnessError as exc:
        if capability is not None and exc.details is not None:
            exc.details.setdefault("capability", capability)
        raise


def _json_identity(value: Any) -> Any:
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", Decimal(str(value)).normalize())
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_json_identity(item) for item in value))
    if isinstance(value, Mapping):
        return (
            "object",
            tuple(sorted((key, _json_identity(item)) for key, item in value.items())),
        )
    return (type(value).__qualname__, repr(value))


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    return isinstance(value, Mapping)


def _decimal(value: int | float) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - JSON freezer rejects this
        raise _Mismatch("number", ()) from exc


def _matches(
    schema: Any,
    value: Any,
    *,
    root: Mapping[str, Any],
    path: tuple[str | int, ...],
    active: set[tuple[int, int]],
) -> None:
    if schema is True:
        return
    if schema is False:
        raise _Mismatch("false", path)
    pair = (id(schema), id(value))
    if pair in active:
        return
    active.add(pair)
    try:
        if "$ref" in schema:
            try:
                target = _resolve_reference(root, schema["$ref"])
            except KeyError:
                raise _Mismatch("$ref", path + ("$ref",)) from None
            _matches(target, value, root=root, path=path + ("$ref",), active=active)
        if "type" in schema:
            raw = schema["type"]
            expected = [raw] if isinstance(raw, str) else raw
            if not any(_matches_type(value, item) for item in expected):
                raise _Mismatch("type", path + ("type",))
        if "const" in schema and _json_identity(value) != _json_identity(schema["const"]):
            raise _Mismatch("const", path + ("const",))
        if "enum" in schema and not any(
            _json_identity(value) == _json_identity(item) for item in schema["enum"]
        ):
            raise _Mismatch("enum", path + ("enum",))
        for keyword in ("allOf",):
            for index, branch in enumerate(schema.get(keyword, [])):
                _matches(
                    branch,
                    value,
                    root=root,
                    path=path + (keyword, index),
                    active=active,
                )
        for keyword, exact in (("anyOf", False), ("oneOf", True)):
            if keyword not in schema:
                continue
            successes = 0
            for index, branch in enumerate(schema[keyword]):
                try:
                    _matches(
                        branch,
                        value,
                        root=root,
                        path=path + (keyword, index),
                        active=set(active),
                    )
                except _Mismatch:
                    continue
                successes += 1
            if successes == 0 or (exact and successes != 1):
                raise _Mismatch(keyword, path + (keyword,))
        if "not" in schema:
            try:
                _matches(
                    schema["not"],
                    value,
                    root=root,
                    path=path + ("not",),
                    active=set(active),
                )
            except _Mismatch:
                pass
            else:
                raise _Mismatch("not", path + ("not",))
        if "if" in schema:
            try:
                _matches(
                    schema["if"],
                    value,
                    root=root,
                    path=path + ("if",),
                    active=set(active),
                )
            except _Mismatch:
                selected = schema.get("else")
                selected_name = "else"
            else:
                selected = schema.get("then")
                selected_name = "then"
            if selected is not None:
                _matches(
                    selected,
                    value,
                    root=root,
                    path=path + (selected_name,),
                    active=active,
                )
        if isinstance(value, Mapping):
            count = len(value)
            if count < schema.get("minProperties", 0):
                raise _Mismatch("minProperties", path + ("minProperties",))
            maximum = schema.get("maxProperties")
            if maximum is not None and count > maximum:
                raise _Mismatch("maxProperties", path + ("maxProperties",))
            for name in schema.get("required", []):
                if name not in value:
                    raise _Mismatch("required", path + ("required",))
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", {})
            for name, item in value.items():
                if name in properties:
                    _matches(
                        properties[name],
                        item,
                        root=root,
                        path=path + ("properties", name),
                        active=active,
                    )
                elif additional is False:
                    raise _Mismatch("additionalProperties", path + ("additionalProperties",))
                elif additional is not True and additional != {}:
                    _matches(
                        additional,
                        item,
                        root=root,
                        path=path + ("additionalProperties",),
                        active=active,
                    )
            for name, dependencies in schema.get("dependentRequired", {}).items():
                if name in value and any(item not in value for item in dependencies):
                    raise _Mismatch("dependentRequired", path + ("dependentRequired", name))
        if isinstance(value, list):
            count = len(value)
            if count < schema.get("minItems", 0):
                raise _Mismatch("minItems", path + ("minItems",))
            maximum = schema.get("maxItems")
            if maximum is not None and count > maximum:
                raise _Mismatch("maxItems", path + ("maxItems",))
            if schema.get("uniqueItems") and len({_json_identity(item) for item in value}) != count:
                raise _Mismatch("uniqueItems", path + ("uniqueItems",))
            prefix = schema.get("prefixItems", [])
            for index, subschema in enumerate(prefix[:count]):
                _matches(
                    subschema,
                    value[index],
                    root=root,
                    path=path + ("prefixItems", index),
                    active=active,
                )
            items = schema.get("items", {})
            for index in range(len(prefix), count):
                _matches(
                    items,
                    value[index],
                    root=root,
                    path=path + ("items",),
                    active=active,
                )
            if "contains" in schema:
                matches = 0
                for item in value:
                    try:
                        _matches(
                            schema["contains"],
                            item,
                            root=root,
                            path=path + ("contains",),
                            active=set(active),
                        )
                    except _Mismatch:
                        continue
                    matches += 1
                if matches < schema.get("minContains", 1):
                    raise _Mismatch("minContains", path + ("minContains",))
                if "maxContains" in schema and matches > schema["maxContains"]:
                    raise _Mismatch("maxContains", path + ("maxContains",))
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise _Mismatch("minLength", path + ("minLength",))
            maximum = schema.get("maxLength")
            if maximum is not None and len(value) > maximum:
                raise _Mismatch("maxLength", path + ("maxLength",))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = _decimal(value)
            comparisons = (
                ("minimum", lambda bound: number >= bound),
                ("maximum", lambda bound: number <= bound),
                ("exclusiveMinimum", lambda bound: number > bound),
                ("exclusiveMaximum", lambda bound: number < bound),
            )
            for keyword, predicate in comparisons:
                if keyword in schema and not predicate(_decimal(schema[keyword])):
                    raise _Mismatch(keyword, path + (keyword,))
            if "multipleOf" in schema and number % _decimal(schema["multipleOf"]) != 0:
                raise _Mismatch("multipleOf", path + ("multipleOf",))
    finally:
        active.remove(pair)


def validate_capability_arguments(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
    *,
    capability: str,
) -> None:
    """Reject arguments outside the registered schema without echoing their values."""
    try:
        _matches(schema, arguments, root=schema, path=(), active=set())
    except _Mismatch as exc:
        raise _schema_error(
            code="CAPABILITY_ARGUMENT_SCHEMA_INVALID",
            message=f"Arguments do not satisfy capability '{capability}' input schema.",
            capability=capability,
            keyword=exc.keyword,
            schema_path=exc.schema_path,
        ) from None


def preflight_capability_arguments(value: Any, *, capability: str) -> None:
    """Bound an argument graph before deep freezing, hashing, or schema traversal."""
    counter = [0]
    active: set[int] = set()

    def visit(item: Any, *, depth: int) -> None:
        counter[0] += 1
        if counter[0] > MAX_CAPABILITY_ARGUMENT_NODES:
            raise _schema_error(
                code="CAPABILITY_ARGUMENT_BUDGET_EXCEEDED",
                message="Capability arguments exceed the node budget.",
                capability=capability,
            )
        if depth > MAX_CAPABILITY_ARGUMENT_DEPTH:
            raise _schema_error(
                code="CAPABILITY_ARGUMENT_BUDGET_EXCEEDED",
                message="Capability arguments exceed the nesting-depth budget.",
                capability=capability,
            )
        if item is None or isinstance(item, (bool, int, str)):
            return
        if isinstance(item, float):
            if isfinite(item):
                return
            raise _schema_error(
                code="CAPABILITY_ARGUMENT_NOT_JSON",
                message="Capability arguments contain a non-finite number.",
                capability=capability,
            )
        if not isinstance(item, (Mapping, list, tuple)):
            raise _schema_error(
                code="CAPABILITY_ARGUMENT_NOT_JSON",
                message="Capability arguments must contain only JSON-compatible values.",
                capability=capability,
            )
        identity = id(item)
        if identity in active:
            raise _schema_error(
                code="CAPABILITY_ARGUMENT_NOT_JSON",
                message="Capability arguments contain a live object cycle.",
                capability=capability,
            )
        active.add(identity)
        try:
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise _schema_error(
                        code="CAPABILITY_ARGUMENT_NOT_JSON",
                        message="Capability argument object keys must be strings.",
                        capability=capability,
                    )
                for child in item.values():
                    visit(child, depth=depth + 1)
            else:
                for child in item:
                    visit(child, depth=depth + 1)
        finally:
            active.remove(identity)

    visit(value, depth=0)


__all__ = [
    "MAX_CAPABILITY_ARGUMENT_DEPTH",
    "MAX_CAPABILITY_ARGUMENT_NODES",
    "MAX_CAPABILITY_SCHEMA_BYTES",
    "MAX_CAPABILITY_SCHEMA_DEPTH",
    "MAX_CAPABILITY_SCHEMA_NODES",
    "preflight_capability_arguments",
    "validate_capability_arguments",
    "validate_capability_input_schema",
]
