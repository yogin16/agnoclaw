"""Pure safety and statistics contracts for the learning archive benchmark."""

from __future__ import annotations

import argparse

import pytest
from scripts.benchmark_learning_evaluation_archive import (
    _percentiles,
    _validate_arguments,
    _validate_target,
)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@db.example.com/agnoclaw_test",
        "postgresql://postgres@127.0.0.1/production",
        "https://127.0.0.1/agnoclaw_test",
    ],
)
def test_learning_archive_benchmark_refuses_unsafe_targets(dsn: str) -> None:
    with pytest.raises(ValueError):
        _validate_target(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@127.0.0.1/agnoclaw_test",
        "postgres://postgres@localhost/service_test",
        "postgresql://postgres@[::1]/learning_test",
    ],
)
def test_learning_archive_benchmark_accepts_only_loopback_test_databases(
    dsn: str,
) -> None:
    _validate_target(dsn)


def test_learning_archive_benchmark_percentiles_use_nearest_rank() -> None:
    result = _percentiles([float(value) for value in range(1, 101)])

    assert result == {
        "p50_ms": 50.5,
        "p95_ms": 95.0,
        "p99_ms": 99.0,
        "max_ms": 100.0,
    }


def test_learning_archive_benchmark_arguments_are_bounded() -> None:
    values = argparse.Namespace(
        dsn="postgresql://postgres@127.0.0.1/agnoclaw_test",
        history_size=1_000,
        samples=50,
        pool_size=2,
        hot_workers=1,
        page_limit=50,
        max_p99_ms=100.0,
        max_p95_slowdown_ratio=5.0,
    )
    _validate_arguments(values)
    values.hot_workers = 2
    with pytest.raises(ValueError, match="lower than"):
        _validate_arguments(values)
    values.hot_workers = 1
    values.page_limit = 126
    with pytest.raises(ValueError, match="too large"):
        _validate_arguments(values)
