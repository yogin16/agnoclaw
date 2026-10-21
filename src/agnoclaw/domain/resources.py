"""Explicit provider reads and immutable, scoped workspace snapshots."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar, Protocol

from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import fail, identifier
from .components import DomainComponent
from .identity import ContractRef


def resource_path(value: str) -> str:
    identifier(value, "resource path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("resource path must be a normalized relative POSIX path")
    return value


class ResourceProvider(Protocol):
    """Hosts authenticate remote reads; returned bytes are hashed by the library."""

    def read(self, path: str) -> bytes: ...


@dataclass(frozen=True)
class LocalResources:
    root: Path

    def read(self, path: str) -> bytes:
        relative = resource_path(path)
        root = Path(self.root).resolve(strict=True)
        selected = root / relative
        for item in (selected, *selected.parents):
            if item == root:
                break
            if item.is_symlink():
                raise ValueError("workspace resources cannot use symbolic links")
        selected.resolve(strict=True).relative_to(root)
        return selected.read_bytes()


@dataclass(frozen=True)
class Resources(DomainComponent):
    name: str
    paths: tuple[str, ...]
    version: str = field(default="1", kw_only=True)
    max_bytes: int = field(default=5_000_000, kw_only=True)
    kind: ClassVar[str] = "resources"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        object.__setattr__(self, "paths", tuple(resource_path(p) for p in self.paths))
        if not self.paths or len(set(self.paths)) != len(self.paths):
            raise ValueError("resources require unique non-empty paths")
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")

    def manifest(self) -> dict[str, Any]:
        return {"paths": list(self.paths), "max_bytes": self.max_bytes}

    def capture(self, provider: ResourceProvider, *, tenant_id: str) -> WorkspaceSnapshot:
        files: dict[str, bytes] = {}
        size = 0
        for path in self.paths:
            value = provider.read(path)
            if not isinstance(value, bytes):
                raise TypeError("resource providers must return bytes")
            size += len(value)
            if size > self.max_bytes:
                raise ValueError("resource snapshot exceeds max_bytes")
            files[path] = value
        return WorkspaceSnapshot(tenant_id, self.digest, files)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    tenant_id: str
    definition_digest: str
    files: Mapping[str, bytes] = field(repr=False)
    base_digest: str | None = None

    def __post_init__(self) -> None:
        from ._contracts import digest_string

        identifier(self.tenant_id, "tenant_id")
        digest_string(self.definition_digest)
        if self.base_digest is not None:
            digest_string(self.base_digest)
        copied = {}
        for path, content in self.files.items():
            resource_path(path)
            if not isinstance(content, bytes):
                raise TypeError("snapshot contents must be bytes")
            copied[path] = content
        paths = set(copied)
        if any(str(parent) in paths for path in paths for parent in PurePosixPath(path).parents):
            raise ValueError("a resource cannot be both a file and directory")
        object.__setattr__(self, "files", MappingProxyType(copied))

    def manifest(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "definition_digest": self.definition_digest,
            "base_digest": self.base_digest,
            "files": {
                p: "sha256:" + hashlib.sha256(b).hexdigest() for p, b in sorted(self.files.items())
            },
        }

    @property
    def digest(self) -> str:
        return canonical_json_digest(self.manifest())

    def overlay(
        self, files: Mapping[str, bytes], *, expected: Mapping[str, str]
    ) -> WorkspaceSnapshot:
        """Replacement requires the exact old file hash; additions need no expected key."""
        hashes = self.manifest()["files"]
        replaced = set(files) & set(self.files)
        if set(expected) != replaced or any(expected[p] != hashes[p] for p in replaced):
            fail("RESOURCE_CONFLICT", "Overlay must name the exact hashes of replaced files.")
        return WorkspaceSnapshot(
            self.tenant_id, self.definition_digest, {**self.files, **files}, self.digest
        )

    def materialize(self, destination: Path, *, tenant_id: str, expected_digest: str) -> Path:
        """Stage then rename into a new directory; never overwrite an active snapshot.

        The parent is host-owned. Hosts atomically switch their own active pointer and
        retain old snapshots for pinned runs; this API does not manage that pointer.
        """
        if tenant_id != self.tenant_id:
            fail("SCOPE_MISMATCH", "Workspace snapshot belongs to another tenant.")
        if expected_digest != self.digest:
            fail("WORKSPACE_CHANGED", "Workspace snapshot differs from the pinned content.")
        destination = Path(destination).absolute()
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            raise ValueError("snapshot destination cannot traverse symbolic links")
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".domain-stage-", dir=destination.parent))
        try:
            for path, content in self.files.items():
                target = stage / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            # Reserving the destination prevents replacement of an existing directory.
            destination.mkdir()
            try:
                os.rename(stage, destination)
            except BaseException:
                destination.rmdir()
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return destination
