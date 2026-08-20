"""Pure safety and statistics contracts for the live PostgreSQL benchmark."""

from __future__ import annotations

import argparse

import pytest
from scripts.benchmark_postgres_runtime import (
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
def test_postgres_benchmark_refuses_unsafe_targets(dsn: str) -> None:
    with pytest.raises(ValueError):
        _validate_target(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@127.0.0.1/agnoclaw_test",
        "postgres://postgres@localhost/service_test",
        "postgresql://postgres@[::1]/runtime_test",
    ],
)
def test_postgres_benchmark_accepts_only_loopback_test_databases(dsn: str) -> None:
    _validate_target(dsn)


def test_postgres_benchmark_percentiles_use_nearest_rank() -> None:
    result = _percentiles([float(value) for value in range(1, 101)])

    assert result == {
        "p50_ms": 50.5,
        "p95_ms": 95.0,
        "p99_ms": 99.0,
        "max_ms": 100.0,
    }


def test_postgres_benchmark_arguments_are_bounded() -> None:
    values = argparse.Namespace(
        dsn="postgresql://postgres@127.0.0.1/agnoclaw_test",
        history_size=1,
        samples=50,
        pool_size=2,
        hot_workers=1,
        pool_timeout_seconds=0.1,
        max_p99_ms=25.0,
        max_p95_slowdown_ratio=4.0,
    )
    _validate_arguments(values)
    values.hot_workers = 2
    with pytest.raises(ValueError, match="lower than"):
        _validate_arguments(values)
