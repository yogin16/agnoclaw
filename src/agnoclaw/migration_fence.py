"""Shared writer-fence primitives for persisted-data migrations."""

from __future__ import annotations

from pathlib import Path

from .runtime.errors import HarnessError


def migration_writer_fence_path(path: str | Path) -> Path:
    """Return the sidecar marker that fences a local persisted store."""
    source = Path(path).expanduser().resolve()
    return source.with_name(f"{source.name}.agnoclaw-fence.json")


def assert_migration_store_writable(
    path: str | Path,
    *,
    code: str,
    category: str,
    store_name: str,
) -> None:
    """Fail closed when a migration owns the local store writer boundary."""
    marker = migration_writer_fence_path(path)
    if marker.exists():
        raise HarnessError(
            code=code,
            category=category,
            message=f"The legacy {store_name} store is fenced for migration.",
            retryable=False,
            details={
                "store_path": str(Path(path).expanduser().resolve()),
                "fence_path": str(marker),
            },
        )


__all__ = ["assert_migration_store_writable", "migration_writer_fence_path"]
