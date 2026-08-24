"""Lossless, bounded model access to governed capability output artifacts."""

from __future__ import annotations

import pytest

from agnoclaw.output_spill import (
    READ_SPILLED_OUTPUT,
    model_output,
    output_page,
    read_capability,
    render_output,
)
from agnoclaw.runtime import ArtifactScope, HarnessError, LocalArtifactStore


@pytest.mark.asyncio
async def test_large_output_becomes_bounded_lossless_envelope(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    value = "A" * 5_000
    reference = await store.stage_json(
        value,
        scope=ArtifactScope(run_id="run-1", tenant_id="tenant-1", user_id="user-1"),
        purpose="operation_result",
        metadata={"kind": "capability"},
    )

    bounded, spilled_chars = model_output(value, reference, maximum_inline_chars=1_024)

    assert spilled_chars == 5_000
    assert bounded["type"] == "agnoclaw.spilled_output"
    assert bounded["id"] == reference.artifact_id
    assert bounded["artifact"]["checksum"] == reference.checksum
    assert bounded["artifact"]["size_bytes"] == reference.size_bytes
    assert bounded["rendered_chars"] == 5_000
    assert bounded["read"] == {
        "tool": READ_SPILLED_OUTPUT,
        "artifact_id": reference.artifact_id,
        "offset": 0,
    }
    assert value not in str(bounded)
    assert "stored losslessly" in bounded["preview"]
    assert await store.load_json(reference) == value


@pytest.mark.asyncio
async def test_small_and_structured_outputs_preserve_deterministic_semantics(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    value = {"z": [2, 1], "a": "é"}
    reference = await store.stage_json(
        value,
        scope=ArtifactScope(run_id="run-2"),
        purpose="operation_result",
    )

    inline, spilled_chars = model_output(value, reference, maximum_inline_chars=1_024)

    assert inline is value
    assert spilled_chars is None
    assert render_output(value) == '{"a":"é","z":[2,1]}'


@pytest.mark.asyncio
async def test_pages_reconstruct_exact_rendered_output_and_clamp_limits(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    value = {"body": "0123456789" * 90}
    reference = await store.stage_json(
        value,
        scope=ArtifactScope(run_id="run-3"),
        purpose="operation_result",
    )
    expected = render_output(value)
    offset = 0
    pages: list[str] = []

    while True:
        page = output_page(
            value,
            reference,
            offset=offset,
            limit=10_000,
            maximum_page_chars=128,
        )
        pages.append(page["content"])
        assert len(page["content"]) <= 128
        if page["complete"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]

    assert "".join(pages) == expected
    assert page["total_chars"] == len(expected)


@pytest.mark.asyncio
async def test_invalid_page_ranges_fail_with_stable_typed_error(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    reference = await store.stage_json(
        "small",
        scope=ArtifactScope(run_id="run-4"),
        purpose="operation_result",
    )

    for offset, limit, maximum in ((-1, None, 128), (0, 0, 128), (6, None, 128), (0, 1, 0)):
        with pytest.raises(HarnessError) as caught:
            output_page(
                "small",
                reference,
                offset=offset,
                limit=limit,
                maximum_page_chars=maximum,
            )
        assert caught.value.code == "OUTPUT_SPILL_RANGE_INVALID"


def test_reader_is_a_bounded_read_only_run_capability() -> None:
    reader = read_capability(lambda: object())
    manifest = reader.manifest()

    assert reader.name == READ_SPILLED_OUTPUT
    assert manifest["effect_class"] == "read_only"
    assert manifest["lifetime"] == "run"
    assert manifest["concurrency"] == "isolated"
    assert manifest["recovery"] == "recreatable"
    assert manifest["input_schema"]["required"] == ["artifact_id"]


def test_non_json_output_fails_before_entering_model_context() -> None:
    with pytest.raises(HarnessError) as caught:
        render_output({"unsupported": object()})

    assert caught.value.code == "OUTPUT_SPILL_SERIALIZATION_INVALID"
