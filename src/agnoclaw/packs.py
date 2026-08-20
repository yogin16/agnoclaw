"""Python-native agnoclaw pack manifests and loader."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PackError(ValueError):
    """Base pack loading error."""


class PackTrustError(PackError):
    """Raised when a pack requires trust before code execution."""


@dataclass(frozen=True)
class PackProvides:
    skills: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    context_providers: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    @property
    def code_entries(self) -> list[str]:
        entries: list[str] = []
        for values in (
            self.capabilities,
            self.tools,
            self.hooks,
            self.context_providers,
            self.policies,
            self.commands,
        ):
            entries.extend(values)
        return entries


@dataclass(frozen=True)
class PackTrust:
    default: str = "community"
    requires_code_execution: bool = False


@dataclass(frozen=True)
class PackManifest:
    name: str
    version: str = "0.0.0"
    description: str = ""
    root: Path = field(default_factory=Path)
    provides: PackProvides = field(default_factory=PackProvides)
    trust: PackTrust = field(default_factory=PackTrust)


@dataclass
class LoadedPack:
    manifest: PackManifest
    trusted: bool = False
    capabilities: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    skills_dirs: list[Path] = field(default_factory=list)
    pre_run_hooks: list[Callable] = field(default_factory=list)
    post_run_hooks: list[Callable] = field(default_factory=list)
    lifecycle_hooks: dict[str, list[Callable]] = field(default_factory=dict)
    context_providers: list[Any] = field(default_factory=list)
    policies: list[Any] = field(default_factory=list)


def inspect_pack(path: str | Path) -> PackManifest:
    """Parse an `agnoclaw-pack.toml` manifest without executing pack code."""
    root = Path(path).expanduser().resolve()
    manifest_path = root if root.name == "agnoclaw-pack.toml" else root / "agnoclaw-pack.toml"
    if not manifest_path.exists():
        raise PackError(f"Pack manifest not found: {manifest_path}")
    with manifest_path.open("rb") as handle:
        data = tomllib.load(handle)
    name = str(data.get("name", "")).strip()
    if not name:
        raise PackError("Pack manifest requires a non-empty `name`")
    root_dir = manifest_path.parent
    provides_data = data.get("provides") or {}
    trust_data = data.get("trust") or {}
    skill_trust = str(trust_data.get("default", "community"))
    if skill_trust not in {"community", "local"}:
        raise PackError("Pack trust.default must be 'community' or 'local'")
    return PackManifest(
        name=name,
        version=str(data.get("version", "0.0.0")),
        description=str(data.get("description", "")),
        root=root_dir,
        provides=PackProvides(
            skills=_string_list(provides_data.get("skills")),
            capabilities=_string_list(provides_data.get("capabilities")),
            tools=_string_list(provides_data.get("tools")),
            hooks=_string_list(provides_data.get("hooks")),
            context_providers=_string_list(provides_data.get("context_providers")),
            policies=_string_list(provides_data.get("policies")),
            commands=_string_list(provides_data.get("commands")),
        ),
        trust=PackTrust(
            default=skill_trust,
            requires_code_execution=bool(trust_data.get("requires_code_execution", False)),
        ),
    )


def pack_store_dir(root: str | Path | None = None) -> Path:
    """Return the local installed-pack store."""
    if root is not None:
        return Path(root).expanduser().resolve()
    return Path.home().joinpath(".agnoclaw", "packs").resolve()


def _pack_trust_record_path(name: str, *, root: str | Path | None = None) -> Path:
    return pack_store_dir(root) / ".trust" / f"{_slugify(name)}.json"


def _remove_pack_trust_record(name: str, *, root: str | Path | None = None) -> None:
    _pack_trust_record_path(name, root=root).unlink(missing_ok=True)


def _pack_digest(root: Path) -> str:
    """Hash regular pack bytes and relative paths; reject link-based ambiguity."""
    resolved_root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(resolved_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise PackError(f"Pack trust cannot cover symbolic links: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(resolved_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def list_installed_packs(*, root: str | Path | None = None) -> list[PackManifest]:
    """List installed pack manifests from the local pack store."""
    store = pack_store_dir(root)
    if not store.exists():
        return []
    manifests: list[PackManifest] = []
    for child in sorted(store.iterdir()):
        if not child.is_dir():
            continue
        try:
            manifests.append(inspect_pack(child))
        except PackError:
            continue
    return manifests


def install_pack(
    source: str | Path,
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
) -> PackManifest:
    """Install a local or git+ pack into the local pack store."""
    store = pack_store_dir(root)
    store.mkdir(parents=True, exist_ok=True)
    source_text = str(source)
    temp_dir: Path | None = None
    if source_text.startswith("git+"):
        temp_dir = store / f".tmp-{_slugify(source_text)}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", source_text.removeprefix("git+"), str(temp_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise PackError(f"Failed to clone pack source: {detail}") from exc
        src = temp_dir
    else:
        src = Path(source).expanduser().resolve()
    try:
        manifest = inspect_pack(src)
        dest = store / _slugify(manifest.name)
        if dest.exists():
            if not overwrite:
                raise PackError(f"Pack already installed: {manifest.name}")
            shutil.rmtree(dest)
            _remove_pack_trust_record(manifest.name, root=store)
        shutil.copytree(src, dest)
        # Legacy in-payload markers were forgeable. Never retain one while
        # installing into the host-owned pack store.
        (dest / ".agnoclaw-trust.json").unlink(missing_ok=True)
        return inspect_pack(dest)
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def remove_pack(name: str, *, root: str | Path | None = None) -> bool:
    """Remove an installed pack by name."""
    dest = pack_store_dir(root) / _slugify(name)
    if not dest.exists():
        return False
    shutil.rmtree(dest)
    _remove_pack_trust_record(name, root=pack_store_dir(root))
    return True


def trust_pack(name: str, *, root: str | Path | None = None) -> PackManifest:
    """Trust one exact installed-pack digest in a host-owned sidecar store."""
    store = pack_store_dir(root)
    manifest = _installed_manifest(name, root=store)
    marker = _pack_trust_record_path(manifest.name, root=store)
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": manifest.name,
                "digest": _pack_digest(manifest.root),
                "trusted": True,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o600)
    return manifest


def is_pack_trusted(path_or_name: str | Path, *, root: str | Path | None = None) -> bool:
    """Return whether a host-owned record trusts this exact installed-pack digest."""
    store = pack_store_dir(root)
    try:
        manifest = inspect_pack(path_or_name)
    except PackError:
        try:
            manifest = _installed_manifest(str(path_or_name), root=store)
        except PackError:
            return False
    try:
        if manifest.root.parent.resolve() != store.resolve():
            return False
    except (OSError, ValueError):
        return False
    marker = _pack_trust_record_path(manifest.name, root=store)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        digest = _pack_digest(manifest.root)
    except (json.JSONDecodeError, OSError, PackError):
        return False
    return bool(
        data.get("trusted")
        and data.get("name") == manifest.name
        and data.get("digest") == digest
    )


def load_pack(
    path: str | Path,
    *,
    trusted: bool = False,
    trust_root: str | Path | None = None,
) -> LoadedPack:
    """Load a pack manifest and, when trusted, execute registered Python providers."""
    manifest = inspect_pack(path)
    trusted = trusted or is_pack_trusted(manifest.root, root=trust_root)
    loaded = LoadedPack(manifest=manifest, trusted=trusted)
    loaded.skills_dirs.extend(_resolve_pack_paths(manifest, manifest.provides.skills))

    if manifest.provides.code_entries:
        if not trusted:
            raise PackTrustError(
                f"Pack {manifest.name!r} contains Python registrations and requires host trust"
            )
        with _pack_import_path(manifest.root):
            loaded.capabilities.extend(_load_registered_items(manifest.provides.capabilities))
            loaded.tools.extend(_load_registered_items(manifest.provides.tools))
            hook_items = _load_registered_items(manifest.provides.hooks)
            for item in hook_items:
                if isinstance(item, dict):
                    loaded.pre_run_hooks.extend(item.get("pre_run_hooks", []) or [])
                    loaded.post_run_hooks.extend(item.get("post_run_hooks", []) or [])
                    for event_type, hooks in (item.get("lifecycle_hooks") or {}).items():
                        loaded.lifecycle_hooks.setdefault(str(event_type), []).extend(
                            _callable_list(hooks)
                        )
                    for key, event_type in (
                        ("session_start_hooks", "session.created"),
                        ("session_end_hooks", "session.end.completed"),
                        ("compaction_hooks", "session.compaction.completed"),
                        ("message_hooks", "message.received"),
                    ):
                        hooks = item.get(key)
                        if hooks:
                            loaded.lifecycle_hooks.setdefault(event_type, []).extend(
                                _callable_list(hooks)
                            )
                elif isinstance(item, (list, tuple)):
                    loaded.pre_run_hooks.extend(item)
                else:
                    loaded.pre_run_hooks.append(item)
            loaded.context_providers.extend(
                _load_registered_items(manifest.provides.context_providers)
            )
            loaded.policies.extend(_load_registered_items(manifest.provides.policies))
    return loaded


def _installed_manifest(name: str, *, root: str | Path | None = None) -> PackManifest:
    dest = pack_store_dir(root) / _slugify(name)
    if not dest.exists():
        raise PackError(f"Pack is not installed: {name}")
    return inspect_pack(dest)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-").lower()
    return slug or "pack"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise PackError(f"Expected string or list, got {type(value).__name__}")


def _callable_list(value: Any) -> list[Callable]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        hooks = list(value)
        if not all(callable(item) for item in hooks):
            raise PackError("Expected callable or list of callables")
        return hooks
    if callable(value):
        return [value]
    raise PackError(f"Expected callable or list of callables, got {type(value).__name__}")


def _resolve_pack_paths(manifest: PackManifest, values: list[str]) -> list[Path]:
    return [(manifest.root / value).resolve() for value in values]


def _load_registered_items(entries: list[str]) -> list[Any]:
    items: list[Any] = []
    for entry in entries:
        factory = _import_entry(entry)
        value = factory()
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            items.extend(value)
        else:
            items.append(value)
    return items


def _import_entry(entry: str) -> Callable[[], Any]:
    module_name, sep, attr = entry.partition(":")
    if not sep or not module_name or not attr:
        raise PackError(f"Invalid pack registration entry: {entry!r}")
    module = importlib.import_module(module_name)
    value = module
    for part in attr.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise PackError(f"Pack registration is not callable: {entry!r}")
    return value


@contextmanager
def _pack_import_path(root: Path) -> Iterator[None]:
    text = str(root)
    inserted = False
    if text not in sys.path:
        sys.path.insert(0, text)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(text)
            except ValueError:
                pass
