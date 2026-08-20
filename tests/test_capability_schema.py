"""Bounded fail-closed capability input-schema contracts."""

from __future__ import annotations

import pytest

from agnoclaw import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
)
from agnoclaw.capability_schema import (
    MAX_CAPABILITY_ARGUMENT_DEPTH,
    MAX_CAPABILITY_SCHEMA_BYTES,
    preflight_capability_arguments,
    validate_capability_arguments,
)
from agnoclaw.runtime import HarnessError


def _spec(schema) -> CapabilitySpec:
    return CapabilitySpec(
        name="inventory.lookup",
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.READ_ONLY,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECREATABLE,
        implementation_digest="sha256:inventory-lookup-v1",
        input_schema=schema,
    )


def test_schema_rejects_unknown_assertions_external_refs_and_bad_roots():
    with pytest.raises(HarnessError) as keyword:
        _spec({"type": "object", "patternProperties": {".*": {"type": "string"}}})
    assert keyword.value.code == "CAPABILITY_INPUT_SCHEMA_UNSUPPORTED"
    assert keyword.value.details["keyword"] == "patternProperties"

    with pytest.raises(HarnessError) as reference:
        _spec({"type": "object", "$ref": "https://example.test/schema"})
    assert reference.value.code == "CAPABILITY_INPUT_SCHEMA_UNSUPPORTED"

    with pytest.raises(HarnessError) as root:
        _spec({"type": "array", "items": {"type": "string"}})
    assert root.value.code == "CAPABILITY_INPUT_SCHEMA_INVALID"


def test_schema_budget_and_shape_errors_fail_at_registration():
    with pytest.raises(HarnessError) as size:
        _spec({"type": "object", "description": "x" * MAX_CAPABILITY_SCHEMA_BYTES})
    assert size.value.code == "CAPABILITY_INPUT_SCHEMA_BUDGET_EXCEEDED"

    with pytest.raises(HarnessError) as malformed:
        _spec({"type": "object", "required": ["sku", "sku"]})
    assert malformed.value.code == "CAPABILITY_INPUT_SCHEMA_INVALID"

    with pytest.raises(HarnessError) as reference:
        _spec({"type": "object", "$ref": "#/$defs/missing"})
    assert reference.value.code == "CAPABILITY_INPUT_SCHEMA_INVALID"

    cyclic: dict[str, object] = {"type": "object"}
    cyclic["properties"] = {"self": cyclic}
    with pytest.raises(HarnessError) as cycle:
        _spec(cyclic)
    assert cycle.value.code == "CAPABILITY_INPUT_SCHEMA_INVALID"


def test_object_array_string_number_and_dependency_assertions():
    schema = {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "minLength": 2, "maxLength": 8},
            "quantity": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "multipleOf": 1,
            },
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ["hot", "cold"]},
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
            },
            "reason": {"type": "string"},
        },
        "required": ["sku", "quantity", "tags"],
        "dependentRequired": {"reason": ["quantity"]},
        "additionalProperties": False,
    }
    _spec(schema)
    validate_capability_arguments(
        schema,
        {"sku": "A-1", "quantity": 2, "tags": ["hot"]},
        capability="inventory.lookup",
    )

    invalid = (
        ({"quantity": 2, "tags": ["hot"]}, "required"),
        ({"sku": "A", "quantity": 2, "tags": ["hot"]}, "minLength"),
        ({"sku": "A-1", "quantity": 0, "tags": ["hot"]}, "minimum"),
        ({"sku": "A-1", "quantity": 2, "tags": ["hot", "hot"]}, "uniqueItems"),
        ({"sku": "A-1", "quantity": 2, "tags": ["warm"]}, "enum"),
        (
            {"sku": "A-1", "quantity": 2, "tags": ["hot"], "secret": "value"},
            "additionalProperties",
        ),
    )
    for arguments, keyword in invalid:
        with pytest.raises(HarnessError) as failure:
            validate_capability_arguments(
                schema,
                arguments,
                capability="inventory.lookup",
            )
        assert failure.value.code == "CAPABILITY_ARGUMENT_SCHEMA_INVALID"
        assert failure.value.details["keyword"] == keyword
        assert "value" not in str(failure.value.details)


def test_local_refs_combinators_conditionals_and_contains_are_enforced():
    schema = {
        "type": "object",
        "$defs": {
            "identifier": {
                "type": "string",
                "minLength": 3,
            }
        },
        "properties": {
            "id": {"$ref": "#/$defs/identifier"},
            "mode": {"enum": ["read", "write"]},
            "token": {"type": "string"},
            "values": {
                "type": "array",
                "contains": {"type": "integer", "minimum": 10},
                "minContains": 1,
                "maxContains": 1,
            },
            "choice": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ]
            },
        },
        "required": ["id", "mode", "values", "choice"],
        "if": {"properties": {"mode": {"const": "write"}}},
        "then": {"required": ["token"]},
        "additionalProperties": False,
    }
    _spec(schema)
    validate_capability_arguments(
        schema,
        {
            "id": "abc",
            "mode": "write",
            "token": "opaque",
            "values": [1, 10],
            "choice": 7,
        },
        capability="inventory.lookup",
    )

    with pytest.raises(HarnessError) as missing:
        validate_capability_arguments(
            schema,
            {
                "id": "abc",
                "mode": "write",
                "values": [10, 11],
                "choice": "seven",
            },
            capability="inventory.lookup",
        )
    assert missing.value.details["keyword"] in {"required", "maxContains"}

    pointer_schema = {
        "type": "object",
        "properties": {
            "template": {
                "type": "array",
                "prefixItems": [{"type": "string", "minLength": 2}],
            },
            "value": {"$ref": "#/properties/template/prefixItems/0"},
        },
    }
    _spec(pointer_schema)
    with pytest.raises(HarnessError) as pointer:
        validate_capability_arguments(
            pointer_schema,
            {"value": "x"},
            capability="inventory.lookup",
        )
    assert pointer.value.details["keyword"] == "minLength"


def test_boolean_property_schemas_and_numeric_json_equality():
    schema = {
        "type": "object",
        "properties": {
            "allowed": True,
            "blocked": False,
            "values": {"type": "array", "uniqueItems": True},
        },
    }
    _spec(schema)

    with pytest.raises(HarnessError) as blocked:
        validate_capability_arguments(
            schema,
            {"blocked": "anything"},
            capability="inventory.lookup",
        )
    assert blocked.value.details["keyword"] == "false"

    with pytest.raises(HarnessError) as duplicate:
        validate_capability_arguments(
            schema,
            {"values": [1, 1.0]},
            capability="inventory.lookup",
        )
    assert duplicate.value.details["keyword"] == "uniqueItems"


def test_argument_preflight_rejects_cycles_opaque_values_and_excessive_depth():
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(HarnessError) as cycle:
        preflight_capability_arguments(cyclic, capability="inventory.lookup")
    assert cycle.value.code == "CAPABILITY_ARGUMENT_NOT_JSON"

    with pytest.raises(HarnessError) as opaque:
        preflight_capability_arguments(
            {"value": object()},
            capability="inventory.lookup",
        )
    assert opaque.value.code == "CAPABILITY_ARGUMENT_NOT_JSON"

    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(MAX_CAPABILITY_ARGUMENT_DEPTH + 1):
        child: dict[str, object] = {}
        cursor["next"] = child
        cursor = child
    with pytest.raises(HarnessError) as depth:
        preflight_capability_arguments(nested, capability="inventory.lookup")
    assert depth.value.code == "CAPABILITY_ARGUMENT_BUDGET_EXCEEDED"
