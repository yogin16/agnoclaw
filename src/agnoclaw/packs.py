"""Python-native agnoclaw pack manifests and loader."""

from __future__ import annotations

import importlib
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
    tools: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    context_providers: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    @property
    def code_entries(self) -> list[str]:
        entries: list[str] = []
        for values in (
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
    default: str = "local"
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
    tools: list[Any] = field(default_factory=list)
    skills_dirs: list[Path] = field(default_factory=list)
    pre_run_hooks: list[Callable] = field(default_factory=list)
    post_run_hooks: list[Callable] = field(default_factory=list)
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
    return PackManifest(
        name=name,
        version=str(data.get("version", "0.0.0")),
        description=str(data.get("description", "")),
        root=root_dir,
        provides=PackProvides(
            skills=_string_list(provides_data.get("skills")),
            tools=_string_list(provides_data.get("tools")),
            hooks=_string_list(provides_data.get("hooks")),
            context_providers=_string_list(provides_data.get("context_providers")),
            policies=_string_list(provides_data.get("policies")),
            commands=_string_list(provides_data.get("commands")),
        ),
        trust=PackTrust(
            default=str(trust_data.get("default", "local")),
            requires_code_execution=bool(trust_data.get("requires_code_execution", False)),
        ),
    )


def load_pack(path: str | Path, *, trusted: bool = False) -> LoadedPack:
    """Load a pack manifest and, when trusted, execute registered Python providers."""
    manifest = inspect_pack(path)
    loaded = LoadedPack(manifest=manifest)
    loaded.skills_dirs.extend(_resolve_pack_paths(manifest, manifest.provides.skills))

    if manifest.provides.code_entries:
        if manifest.trust.requires_code_execution and not trusted:
            raise PackTrustError(
                f"Pack {manifest.name!r} requires trust before executing Python registrations"
            )
        with _pack_import_path(manifest.root):
            loaded.tools.extend(_load_registered_items(manifest.provides.tools))
            hook_items = _load_registered_items(manifest.provides.hooks)
            for item in hook_items:
                if isinstance(item, dict):
                    loaded.pre_run_hooks.extend(item.get("pre_run_hooks", []) or [])
                    loaded.post_run_hooks.extend(item.get("post_run_hooks", []) or [])
                elif isinstance(item, (list, tuple)):
                    loaded.pre_run_hooks.extend(item)
                else:
                    loaded.pre_run_hooks.append(item)
            loaded.context_providers.extend(
                _load_registered_items(manifest.provides.context_providers)
            )
            loaded.policies.extend(_load_registered_items(manifest.provides.policies))
    return loaded


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise PackError(f"Expected string or list, got {type(value).__name__}")


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
