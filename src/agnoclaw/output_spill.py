"""Lossless model-facing previews for artifact-backed capability output."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from .capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
)
from .runtime.artifacts import ArtifactReference
from .runtime.errors import HarnessError
from .runtime.operations import EffectClass

READ_SPILLED_OUTPUT = "read_spilled_output"
_SCHEMA_VERSION = "1.0"


def render_output(value: Any) -> str:
    """Render the same finite JSON-like values accepted by ArtifactStore."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            code="OUTPUT_SPILL_SERIALIZATION_INVALID",
            category="artifact",
            message="Capability output cannot be rendered for bounded model access.",
            retryable=False,
        ) from exc


def model_output(
    value: Any,
    reference: ArtifactReference,
    *,
    maximum_inline_chars: int,
) -> tuple[Any, int | None]:
    """Return the original small value or a bounded lossless artifact envelope."""
    rendered = render_output(value)
    if len(rendered) <= maximum_inline_chars:
        return value, None
    preview_chars = max(128, min(2_000, maximum_inline_chars // 2))
    head = preview_chars // 2
    tail = preview_chars - head
    omitted = len(rendered) - preview_chars
    preview = f"{rendered[:head]}\n… [{omitted} chars stored losslessly] …\n{rendered[-tail:]}"
    return (
        {
            "type": "agnoclaw.spilled_output",
            "id": reference.artifact_id,
            "schema_version": _SCHEMA_VERSION,
            "artifact": {
                "artifact_id": reference.artifact_id,
                "checksum": reference.checksum,
                "media_type": reference.media_type,
                "size_bytes": reference.size_bytes,
            },
            "rendered_chars": len(rendered),
            "preview": preview,
            "read": {
                "tool": READ_SPILLED_OUTPUT,
                "artifact_id": reference.artifact_id,
                "offset": 0,
            },
        },
        len(rendered),
    )


def output_page(
    value: Any,
    reference: ArtifactReference,
    *,
    offset: int,
    limit: int | None,
    maximum_page_chars: int,
) -> dict[str, Any]:
    """Render one deterministic character page from a verified artifact value."""
    if maximum_page_chars <= 0 or offset < 0 or limit is not None and limit <= 0:
        raise _range_error(reference)
    rendered = render_output(value)
    if offset > len(rendered):
        raise _range_error(reference)
    page_limit = maximum_page_chars if limit is None else min(limit, maximum_page_chars)
    end = min(len(rendered), offset + page_limit)
    return {
        "type": "agnoclaw.spilled_output_page",
        "artifact_id": reference.artifact_id,
        "checksum": reference.checksum,
        "offset": offset,
        "content": rendered[offset:end],
        "next_offset": None if end >= len(rendered) else end,
        "complete": end >= len(rendered),
        "total_chars": len(rendered),
    }


def read_capability(factory: Callable[[], Any]) -> CapabilitySpec:
    """Build the one internal, governed paging capability used by spill envelopes."""
    digest = hashlib.sha256(b"agnoclaw:read-spilled-output:v1").hexdigest()
    return CapabilitySpec(
        name=READ_SPILLED_OUTPUT,
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.READ_ONLY,
        trust=CapabilityTrust.BUILTIN,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECREATABLE,
        implementation_digest=f"sha256:{digest}",
        description=(
            "Read the next bounded text page from an agnoclaw.spilled_output artifact. "
            "Continue with next_offset until complete."
        ),
        tags=("artifact", "context", "output"),
        input_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1, "maxLength": 512},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        factory=factory,
    )


def _range_error(reference: ArtifactReference) -> HarnessError:
    return HarnessError(
        code="OUTPUT_SPILL_RANGE_INVALID",
        category="artifact",
        message="Spilled output requires a valid bounded character range.",
        retryable=False,
        details={"artifact_id": reference.artifact_id},
    )
