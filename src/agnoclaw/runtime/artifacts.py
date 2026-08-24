"""Scoped content-addressed artifact storage for durable runtime results.

Artifact bytes are staged before the runtime ledger authorizes a reference.  The
``RuntimeStore`` remains the authority for ownership and liveness; this module owns
immutable bytes, checksums, bounded reads, and the host encryption seam.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import stat
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import HarnessError
from .security import KeyProvider, KeyPurpose, KeyReference, SealedContent, freeze_data, thaw_data

ARTIFACT_SCHEMA_VERSION = "1.0"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact:v1:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$")
_STORAGE_KEY_RE = re.compile(r"^v1/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{64}/[0-9a-f]{64}\.blob$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _artifact_descriptor_digest(
    *,
    scope: ArtifactScope,
    purpose: str,
    media_type: str,
    encoding: str,
    checksum: str,
    stored_checksum: str,
    protection: ArtifactProtection | None,
) -> str:
    canonical = json.dumps(
        {
            "scope": scope.to_dict(),
            "purpose": purpose,
            "media_type": media_type,
            "encoding": encoding,
            "checksum": checksum,
            "stored_checksum": stored_checksum,
            "protection": protection.to_dict() if protection else None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_optional_identifier(value: str | None, *, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when supplied")
    if len(value) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")


@dataclass(frozen=True)
class ArtifactScope:
    """Exact ledger ownership namespace for one artifact."""

    run_id: str
    tenant_id: str | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("run_id", "tenant_id", "user_id"):
            _require_optional_identifier(getattr(self, field_name), field_name=field_name)
        if not self.run_id:
            raise ValueError("run_id must be a non-empty string")

    @property
    def digest(self) -> str:
        framed = b"".join(
            len((value or "").encode("utf-8")).to_bytes(8, "big") + (value or "").encode("utf-8")
            for value in (self.tenant_id, self.user_id, self.run_id)
        )
        return hashlib.sha256(framed).hexdigest()

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class ArtifactProtection:
    """Serializable envelope metadata; ciphertext remains in the ArtifactStore."""

    key: KeyReference
    algorithm: str
    nonce_b64: str
    aad_digest: str

    def __post_init__(self) -> None:
        for field_name in ("algorithm", "nonce_b64", "aad_digest"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"artifact protection requires {field_name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": {
                "key_id": self.key.key_id,
                "version": self.key.version,
                "tenant_id": self.key.tenant_id,
                "purpose": self.key.purpose.value,
            },
            "algorithm": self.algorithm,
            "nonce_b64": self.nonce_b64,
            "aad_digest": self.aad_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ArtifactProtection:
        key = dict(value["key"])
        return cls(
            key=KeyReference(
                key_id=key["key_id"],
                version=key["version"],
                tenant_id=key["tenant_id"],
                purpose=KeyPurpose(key["purpose"]),
            ),
            algorithm=value["algorithm"],
            nonce_b64=value["nonce_b64"],
            aad_digest=value["aad_digest"],
        )


@dataclass(frozen=True)
class ArtifactReference:
    """Immutable staged-byte descriptor committed into the runtime ledger."""

    artifact_id: str
    scope: ArtifactScope
    purpose: str
    media_type: str
    encoding: str
    checksum: str
    stored_checksum: str
    size_bytes: int
    stored_size_bytes: int
    storage_key: str
    protection: ArtifactProtection | None = None
    metadata: Any = field(default_factory=dict)
    staged_at: str = field(default_factory=_now)
    schema_version: str = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported artifact schema '{self.schema_version}'")
        if not _ARTIFACT_ID_RE.fullmatch(self.artifact_id):
            raise ValueError("artifact_id is not a canonical v1 content address")
        if not _DIGEST_RE.fullmatch(self.checksum):
            raise ValueError("checksum must be a canonical sha256 digest")
        if not _DIGEST_RE.fullmatch(self.stored_checksum):
            raise ValueError("stored_checksum must be a canonical sha256 digest")
        if not _STORAGE_KEY_RE.fullmatch(self.storage_key):
            raise ValueError("storage_key is not a canonical artifact object key")
        for field_name in ("purpose", "media_type", "encoding", "staged_at"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"artifact reference requires {field_name}")
            if len(value) > 512:
                raise ValueError(f"{field_name} cannot exceed 512 characters")
        if self.size_bytes < 0 or self.stored_size_bytes < 0:
            raise ValueError("artifact sizes cannot be negative")
        address = self.artifact_id.split(":")
        if address[2] != self.scope.digest:
            raise ValueError("artifact address does not match its scope")
        if address[3] != self.checksum.removeprefix("sha256:"):
            raise ValueError("artifact address does not match its content checksum")
        descriptor_digest = _artifact_descriptor_digest(
            scope=self.scope,
            purpose=self.purpose,
            media_type=self.media_type,
            encoding=self.encoding,
            checksum=self.checksum,
            stored_checksum=self.stored_checksum,
            protection=self.protection,
        )
        if address[4] != descriptor_digest:
            raise ValueError("artifact address does not match its immutable descriptor")
        if self.protection is not None:
            if self.scope.tenant_id is None:
                raise ValueError("protected artifacts require a tenant scope")
            if self.protection.key.tenant_id != self.scope.tenant_id:
                raise ValueError("artifact key tenant does not match artifact scope")
            if self.protection.key.purpose is not KeyPurpose.ARTIFACT:
                raise ValueError("artifact protection requires an artifact-purpose key")
        object.__setattr__(self, "metadata", freeze_data(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "scope": self.scope.to_dict(),
            "purpose": self.purpose,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "checksum": self.checksum,
            "stored_checksum": self.stored_checksum,
            "size_bytes": self.size_bytes,
            "stored_size_bytes": self.stored_size_bytes,
            "storage_key": self.storage_key,
            "protection": self.protection.to_dict() if self.protection else None,
            "metadata": thaw_data(self.metadata),
            "staged_at": self.staged_at,
        }

    @property
    def storage_identity_digest(self) -> str:
        """Digest immutable byte/decryption identity, excluding reference evidence."""
        payload = self.to_dict()
        payload.pop("metadata")
        payload.pop("staged_at")
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return _sha256(canonical)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ArtifactReference:
        payload = dict(value)
        payload["scope"] = ArtifactScope(**payload["scope"])
        if payload.get("protection") is not None:
            payload["protection"] = ArtifactProtection.from_dict(payload["protection"])
        return cls(**payload)


@dataclass(frozen=True)
class ArtifactChunk:
    artifact_id: str
    offset: int
    data: bytes
    next_offset: int | None
    complete: bool
    total_size_bytes: int


@dataclass(frozen=True)
class ArtifactGarbageCollection:
    examined: int
    deleted: int
    reclaimed_bytes: int
    limited: bool


class ArtifactTooLargeError(HarnessError):
    def __init__(self, *, size_bytes: int, maximum_bytes: int):
        super().__init__(
            code="ARTIFACT_TOO_LARGE",
            category="artifact",
            message="Artifact content exceeds the configured size boundary.",
            retryable=False,
            details={"size_bytes": size_bytes, "maximum_bytes": maximum_bytes},
        )


class ArtifactNotFoundError(HarnessError):
    def __init__(self, artifact_id: str):
        super().__init__(
            code="ARTIFACT_NOT_FOUND",
            category="artifact",
            message="The artifact is absent or not visible to this owner.",
            retryable=False,
            details={"artifact_id": artifact_id},
        )


class ArtifactCorruptError(HarnessError):
    def __init__(self, artifact_id: str, *, reason: str):
        super().__init__(
            code="ARTIFACT_CORRUPT",
            category="artifact",
            message="Committed artifact bytes failed integrity verification.",
            retryable=False,
            details={"artifact_id": artifact_id, "reason": reason},
        )


class ArtifactTenantRequiredError(HarnessError):
    def __init__(self, run_id: str):
        super().__init__(
            code="ARTIFACT_TENANT_REQUIRED",
            category="artifact",
            message="Encrypted artifact storage requires an authoritative tenant scope.",
            retryable=False,
            details={"run_id": run_id},
        )


@runtime_checkable
class ArtifactStore(Protocol):
    """One async-first external-byte boundary; ledger references remain authoritative."""

    async def stage_json(
        self,
        value: Any,
        *,
        scope: ArtifactScope,
        purpose: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference: ...

    async def load_json(self, reference: ArtifactReference) -> Any: ...

    async def read(
        self,
        reference: ArtifactReference,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> ArtifactChunk: ...


class LocalArtifactStore:
    """Atomic local-filesystem content store for the single-node durable profile."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_artifact_bytes: int = 16 * 1024 * 1024,
        max_stored_bytes: int | None = None,
        max_page_bytes: int = 1024 * 1024,
        key_provider: KeyProvider | None = None,
    ) -> None:
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        if max_page_bytes <= 0 or max_page_bytes > max_artifact_bytes:
            raise ValueError("max_page_bytes must be positive and no larger than artifact size")
        resolved_stored_max = max_stored_bytes or (
            max_artifact_bytes + max(64 * 1024, max_artifact_bytes // 16)
        )
        if resolved_stored_max < max_artifact_bytes:
            raise ValueError("max_stored_bytes cannot be smaller than max_artifact_bytes")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._objects = self.root / "objects"
        self._staging = self.root / "staging"
        self._objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_artifact_bytes = max_artifact_bytes
        self.max_stored_bytes = resolved_stored_max
        self.max_page_bytes = max_page_bytes
        self.key_provider = key_provider

    @staticmethod
    async def _resolve(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _aad(
        *,
        scope: ArtifactScope,
        checksum: str,
        purpose: str,
        media_type: str,
        encoding: str,
    ) -> bytes:
        return json.dumps(
            {
                "scope": scope.to_dict(),
                "checksum": checksum,
                "purpose": purpose,
                "media_type": media_type,
                "encoding": encoding,
                "schema_version": ARTIFACT_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _object_path(self, storage_key: str) -> Path:
        if not _STORAGE_KEY_RE.fullmatch(storage_key):
            raise ValueError("invalid artifact storage key")
        path = (self._objects / storage_key).resolve(strict=False)
        if not path.is_relative_to(self._objects):
            raise ValueError("artifact storage key escapes the object root")
        return path

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - defensive OS contract
                raise OSError("artifact write made no progress")
            view = view[written:]

    def _stage_file(self, storage_key: str, payload: bytes, artifact_id: str) -> None:
        target = self._object_path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix="artifact-", suffix=".stage", dir=self._staging)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            self._write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                # Content addressing makes an existing object reusable. Every read
                # verifies again, but staging also refuses to authorize corruption.
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    existing_fd = os.open(target, flags)
                except OSError as exc:
                    raise ArtifactCorruptError(
                        artifact_id, reason="existing_object_unreadable"
                    ) from exc
                try:
                    info = os.fstat(existing_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size != len(payload):
                        raise ArtifactCorruptError(artifact_id, reason="existing_object_mismatch")
                    chunks: list[bytes] = []
                    remaining = len(payload)
                    while remaining:
                        chunk = os.read(existing_fd, min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    if b"".join(chunks) != payload:
                        raise ArtifactCorruptError(artifact_id, reason="existing_object_mismatch")
                finally:
                    os.close(existing_fd)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _read_stored(self, reference: ArtifactReference) -> bytes:
        if reference.size_bytes > self.max_artifact_bytes:
            raise ArtifactTooLargeError(
                size_bytes=reference.size_bytes,
                maximum_bytes=self.max_artifact_bytes,
            )
        if reference.stored_size_bytes > self.max_stored_bytes:
            raise ArtifactTooLargeError(
                size_bytes=reference.stored_size_bytes,
                maximum_bytes=self.max_stored_bytes,
            )
        path = self._object_path(reference.storage_key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except (FileNotFoundError, OSError) as exc:
            raise ArtifactCorruptError(reference.artifact_id, reason="missing_bytes") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactCorruptError(reference.artifact_id, reason="not_regular_file")
            if info.st_size != reference.stored_size_bytes:
                raise ArtifactCorruptError(reference.artifact_id, reason="stored_size_mismatch")
            chunks: list[bytes] = []
            remaining = reference.stored_size_bytes
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise ArtifactCorruptError(reference.artifact_id, reason="unexpected_eof")
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(fd)
        if _sha256(payload) != reference.stored_checksum:
            raise ArtifactCorruptError(reference.artifact_id, reason="stored_checksum_mismatch")
        return payload

    async def stage_json(
        self,
        value: Any,
        *,
        scope: ArtifactScope,
        purpose: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference:
        try:
            payload = json.dumps(
                thaw_data(freeze_data(value)),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                code="ARTIFACT_JSON_INVALID",
                category="artifact",
                message="Artifact JSON must contain finite JSON-like values.",
                retryable=False,
                details={"run_id": scope.run_id},
            ) from exc
        if len(payload) > self.max_artifact_bytes:
            raise ArtifactTooLargeError(
                size_bytes=len(payload), maximum_bytes=self.max_artifact_bytes
            )
        checksum = _sha256(payload)
        media_type = "application/json"
        encoding = "utf-8"
        aad = self._aad(
            scope=scope,
            checksum=checksum,
            purpose=purpose,
            media_type=media_type,
            encoding=encoding,
        )
        stored = payload
        protection: ArtifactProtection | None = None
        if self.key_provider is not None:
            if scope.tenant_id is None:
                raise ArtifactTenantRequiredError(scope.run_id)
            sealed = await self._resolve(
                self.key_provider.seal(
                    payload,
                    tenant_id=scope.tenant_id,
                    purpose=KeyPurpose.ARTIFACT,
                    aad=aad,
                )
            )
            if not isinstance(sealed, SealedContent):
                raise TypeError("KeyProvider.seal() must return SealedContent")
            try:
                stored = base64.b64decode(sealed.ciphertext_b64, validate=True)
            except ValueError as exc:
                raise ValueError("KeyProvider returned invalid base64 ciphertext") from exc
            protection = ArtifactProtection(
                key=sealed.key,
                algorithm=sealed.algorithm,
                nonce_b64=sealed.nonce_b64,
                aad_digest=sealed.aad_digest,
            )
        if len(stored) > self.max_stored_bytes:
            raise ArtifactTooLargeError(size_bytes=len(stored), maximum_bytes=self.max_stored_bytes)
        stored_checksum = _sha256(stored)
        content_hex = checksum.removeprefix("sha256:")
        descriptor_hex = _artifact_descriptor_digest(
            scope=scope,
            purpose=purpose,
            media_type=media_type,
            encoding=encoding,
            checksum=checksum,
            stored_checksum=stored_checksum,
            protection=protection,
        )
        artifact_id = f"artifact:v1:{scope.digest}:{content_hex}:{descriptor_hex}"
        storage_key = f"v1/{scope.digest[:2]}/{scope.digest}/{content_hex}/{descriptor_hex}.blob"
        await asyncio.to_thread(self._stage_file, storage_key, stored, artifact_id)
        return ArtifactReference(
            artifact_id=artifact_id,
            scope=scope,
            purpose=purpose,
            media_type=media_type,
            encoding=encoding,
            checksum=checksum,
            stored_checksum=stored_checksum,
            size_bytes=len(payload),
            stored_size_bytes=len(stored),
            storage_key=storage_key,
            protection=protection,
            metadata=metadata or {},
        )

    async def _read_plaintext(self, reference: ArtifactReference) -> bytes:
        stored = await asyncio.to_thread(self._read_stored, reference)
        payload = stored
        if reference.protection is not None:
            if self.key_provider is None:
                raise ArtifactCorruptError(reference.artifact_id, reason="key_unavailable")
            sealed = SealedContent(
                key=reference.protection.key,
                algorithm=reference.protection.algorithm,
                nonce_b64=reference.protection.nonce_b64,
                ciphertext_b64=base64.b64encode(stored).decode("ascii"),
                aad_digest=reference.protection.aad_digest,
            )
            aad = self._aad(
                scope=reference.scope,
                checksum=reference.checksum,
                purpose=reference.purpose,
                media_type=reference.media_type,
                encoding=reference.encoding,
            )
            try:
                payload = await self._resolve(self.key_provider.unseal(sealed, aad=aad))
            except Exception as exc:
                raise ArtifactCorruptError(reference.artifact_id, reason="unseal_failed") from exc
            if not isinstance(payload, bytes):
                raise ArtifactCorruptError(
                    reference.artifact_id, reason="unseal_returned_non_bytes"
                )
        if len(payload) != reference.size_bytes:
            raise ArtifactCorruptError(reference.artifact_id, reason="content_size_mismatch")
        if _sha256(payload) != reference.checksum:
            raise ArtifactCorruptError(reference.artifact_id, reason="content_checksum_mismatch")
        return payload

    async def read(
        self,
        reference: ArtifactReference,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> ArtifactChunk:
        page_limit = self.max_page_bytes if limit is None else limit
        if offset < 0 or page_limit <= 0 or page_limit > self.max_page_bytes:
            raise HarnessError(
                code="ARTIFACT_RANGE_INVALID",
                category="artifact",
                message="Artifact reads require a valid bounded byte range.",
                retryable=False,
                details={"artifact_id": reference.artifact_id},
            )
        payload = await self._read_plaintext(reference)
        if offset > len(payload):
            raise HarnessError(
                code="ARTIFACT_RANGE_INVALID",
                category="artifact",
                message="Artifact read offset is beyond the end of the content.",
                retryable=False,
                details={"artifact_id": reference.artifact_id},
            )
        data = payload[offset : offset + page_limit]
        end = offset + len(data)
        complete = end >= len(payload)
        return ArtifactChunk(
            artifact_id=reference.artifact_id,
            offset=offset,
            data=data,
            next_offset=None if complete else end,
            complete=complete,
            total_size_bytes=len(payload),
        )

    async def load_json(self, reference: ArtifactReference) -> Any:
        if reference.media_type != "application/json" or reference.encoding != "utf-8":
            raise HarnessError(
                code="ARTIFACT_FORMAT_UNSUPPORTED",
                category="artifact",
                message="This artifact is not a supported JSON result artifact.",
                retryable=False,
                details={"artifact_id": reference.artifact_id},
            )
        payload = await self._read_plaintext(reference)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactCorruptError(reference.artifact_id, reason="invalid_json") from exc

    def _garbage_collect_sync(
        self,
        referenced_storage_keys: Collection[str],
        *,
        grace_seconds: float,
        limit: int,
    ) -> ArtifactGarbageCollection:
        if grace_seconds < 0:
            raise ValueError("grace_seconds cannot be negative")
        if limit <= 0 or limit > 100_000:
            raise ValueError("limit must be between 1 and 100000")
        live = set(referenced_storage_keys)
        if any(not _STORAGE_KEY_RE.fullmatch(key) for key in live):
            raise ValueError("referenced_storage_keys contains an invalid key")
        cutoff = datetime.now(UTC).timestamp() - grace_seconds
        examined = deleted = reclaimed = 0
        limited = False
        for path in self._objects.rglob("*.blob"):
            if examined >= limit:
                limited = True
                break
            examined += 1
            try:
                relative = path.relative_to(self._objects).as_posix()
                info = path.lstat()
            except FileNotFoundError:
                continue
            if relative in live or info.st_mtime > cutoff or not stat.S_ISREG(info.st_mode):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            deleted += 1
            reclaimed += info.st_size
        if not limited:
            for path in self._staging.glob("*.stage"):
                if examined >= limit:
                    limited = True
                    break
                examined += 1
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if info.st_mtime > cutoff or not stat.S_ISREG(info.st_mode):
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                deleted += 1
                reclaimed += info.st_size
        return ArtifactGarbageCollection(examined, deleted, reclaimed, limited)

    async def garbage_collect(
        self,
        referenced_storage_keys: Collection[str],
        *,
        grace_seconds: float = 3600,
        limit: int = 1000,
    ) -> ArtifactGarbageCollection:
        """Delete only old staged objects absent from an authoritative live-key set."""
        return await asyncio.to_thread(
            self._garbage_collect_sync,
            referenced_storage_keys,
            grace_seconds=grace_seconds,
            limit=limit,
        )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactChunk",
    "ArtifactCorruptError",
    "ArtifactGarbageCollection",
    "ArtifactNotFoundError",
    "ArtifactProtection",
    "ArtifactReference",
    "ArtifactScope",
    "ArtifactStore",
    "ArtifactTenantRequiredError",
    "ArtifactTooLargeError",
    "LocalArtifactStore",
]
