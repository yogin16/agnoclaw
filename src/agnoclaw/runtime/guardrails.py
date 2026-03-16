"""Runtime tool guardrails for path and network controls."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .hooks import ToolCallRequest

_PATH_ARG_KEYS = frozenset({"path", "base_dir", "working_dir", "directory", "dir", "cwd"})
_NETWORK_TOOL_NAMES = frozenset({"web_search", "web_fetch"})
_BROWSER_TOOL_NAMES = frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_screenshot",
        "browser_snapshot",
        "browser_scroll",
        "browser_fill_form",
    }
)
_BASH_NETWORK_COMMAND_RE = re.compile(
    r"\b(curl|wget|http|https|ftp|scp|ssh|sftp|nc|ncat|telnet|dig|nslookup)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'`]+", re.IGNORECASE)


@dataclass(frozen=True)
class GuardrailViolation:
    """A single guardrail violation."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class RuntimeGuardrails:
    """Path and network guardrail evaluator for tool calls."""

    def __init__(
        self,
        *,
        workspace_dir: str | Path,
        enabled: bool = True,
        path_enabled: bool = True,
        path_allowed_roots: Iterable[str] | None = None,
        path_blocked_roots: Iterable[str] | None = None,
        network_enabled: bool = True,
        network_enforce_https: bool = True,
        network_allowed_hosts: Iterable[str] | None = None,
        network_blocked_hosts: Iterable[str] | None = None,
        network_block_private_hosts: bool = True,
        network_block_in_bash: bool = True,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser().resolve(strict=False)
        self.enabled = enabled
        self.path_enabled = path_enabled
        self.network_enabled = network_enabled
        self.network_enforce_https = network_enforce_https
        self.network_block_private_hosts = network_block_private_hosts
        self.network_block_in_bash = network_block_in_bash

        explicit_allowed = tuple(path_allowed_roots or ())
        self.path_allowed_roots = (
            self._normalize_roots(explicit_allowed)
            if explicit_allowed
            else (self.workspace_dir,)
        )
        self.path_blocked_roots = self._normalize_roots(path_blocked_roots or ())

        self.network_allowed_hosts = tuple(self._normalize_hosts(network_allowed_hosts or ()))
        self.network_blocked_hosts = tuple(self._normalize_hosts(network_blocked_hosts or ()))

    def check(self, request: ToolCallRequest) -> tuple[GuardrailViolation, ...]:
        """Evaluate guardrails for a tool call request."""
        if not self.enabled:
            return ()

        violations: list[GuardrailViolation] = []
        if self.path_enabled:
            violations.extend(self._check_path_constraints(request))
        violations.extend(self._check_network_constraints(request))
        return tuple(violations)

    def _check_path_constraints(self, request: ToolCallRequest) -> list[GuardrailViolation]:
        violations: list[GuardrailViolation] = []
        for arg_key, raw_path in self._extract_path_candidates(request.arguments):
            resolved = self._resolve_path(raw_path)

            blocked_root = self._first_matching_root(resolved, self.path_blocked_roots)
            if blocked_root is not None:
                violations.append(
                    GuardrailViolation(
                        code="PATH_BLOCKED_ROOT",
                        message=f"Tool '{request.tool_name}' path is under blocked root: {blocked_root}",
                        details={
                            "tool_name": request.tool_name,
                            "arg_key": arg_key,
                            "path": str(resolved),
                            "blocked_root": str(blocked_root),
                        },
                    )
                )
                continue

            if self.path_allowed_roots and self._first_matching_root(resolved, self.path_allowed_roots) is None:
                violations.append(
                    GuardrailViolation(
                        code="PATH_OUTSIDE_ALLOWED_ROOTS",
                        message=f"Tool '{request.tool_name}' path is outside allowed roots",
                        details={
                            "tool_name": request.tool_name,
                            "arg_key": arg_key,
                            "path": str(resolved),
                            "allowed_roots": [str(root) for root in self.path_allowed_roots],
                        },
                    )
                )
        return violations

    def _check_network_constraints(self, request: ToolCallRequest) -> list[GuardrailViolation]:
        violations: list[GuardrailViolation] = []
        tool_name = request.tool_name
        arguments = request.arguments

        if (tool_name in _NETWORK_TOOL_NAMES or tool_name in _BROWSER_TOOL_NAMES) and not self.network_enabled:
            violations.append(
                GuardrailViolation(
                    code="NETWORK_DISABLED",
                    message=f"Tool '{tool_name}' is blocked because network is disabled",
                    details={"tool_name": tool_name},
                )
            )

        if tool_name == "web_fetch":
            url = arguments.get("url")
            if isinstance(url, str):
                violations.extend(self._validate_url(url=url, tool_name=tool_name, arg_key="url"))

        if tool_name == "browser_navigate":
            url = arguments.get("url")
            if isinstance(url, str):
                violations.extend(self._validate_url(url=url, tool_name=tool_name, arg_key="url"))

        if tool_name in {"bash", "bash_start"} and self.network_block_in_bash:
            command = arguments.get("command")
            if isinstance(command, str):
                command_has_network = bool(_BASH_NETWORK_COMMAND_RE.search(command) or _URL_RE.search(command))
                if command_has_network and not self.network_enabled:
                    violations.append(
                        GuardrailViolation(
                            code="NETWORK_DISABLED_BASH",
                            message="bash command appears to perform network access while network is disabled",
                            details={"tool_name": "bash", "command": command[:200]},
                        )
                    )
                for url in _URL_RE.findall(command):
                    violations.extend(self._validate_url(url=url, tool_name=tool_name, arg_key="command"))
        return violations

    def _validate_url(self, *, url: str, tool_name: str, arg_key: str) -> list[GuardrailViolation]:
        violations: list[GuardrailViolation] = []
        parsed = urlparse(url.strip())
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()

        if self.network_enforce_https and scheme and scheme != "https":
            violations.append(
                GuardrailViolation(
                    code="NETWORK_HTTPS_REQUIRED",
                    message=f"Only https URLs are allowed, got '{scheme}'",
                    details={"tool_name": tool_name, "arg_key": arg_key, "url": url},
                )
            )

        if not host:
            violations.append(
                GuardrailViolation(
                    code="NETWORK_INVALID_URL",
                    message="URL host is missing or invalid",
                    details={"tool_name": tool_name, "arg_key": arg_key, "url": url},
                )
            )
            return violations

        if self.network_blocked_hosts and self._host_in_set(host, self.network_blocked_hosts):
            violations.append(
                GuardrailViolation(
                    code="NETWORK_HOST_BLOCKED",
                    message=f"Host '{host}' is blocked by policy",
                    details={"tool_name": tool_name, "arg_key": arg_key, "url": url, "host": host},
                )
            )

        if self.network_allowed_hosts and not self._host_in_set(host, self.network_allowed_hosts):
            violations.append(
                GuardrailViolation(
                    code="NETWORK_HOST_NOT_ALLOWED",
                    message=f"Host '{host}' is not in the allowed host list",
                    details={"tool_name": tool_name, "arg_key": arg_key, "url": url, "host": host},
                )
            )

        if self.network_block_private_hosts and self._is_private_host(host):
            violations.append(
                GuardrailViolation(
                    code="NETWORK_PRIVATE_HOST_BLOCKED",
                    message=f"Private/loopback host '{host}' is blocked",
                    details={"tool_name": tool_name, "arg_key": arg_key, "url": url, "host": host},
                )
            )
        return violations

    def _extract_path_candidates(self, arguments: dict[str, Any]) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        for key, value in arguments.items():
            key_l = key.lower()
            if key_l not in _PATH_ARG_KEYS:
                continue
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if stripped:
                candidates.append((key, stripped))
        return candidates

    def _resolve_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = (self.workspace_dir / candidate).resolve(strict=False)
        else:
            candidate = candidate.resolve(strict=False)
        return candidate

    @staticmethod
    def _normalize_roots(roots: Iterable[str]) -> tuple[Path, ...]:
        return tuple(Path(root).expanduser().resolve(strict=False) for root in roots if str(root).strip())

    @staticmethod
    def _normalize_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for host in hosts:
            value = str(host).strip().lower()
            if value:
                normalized.append(value)
        return tuple(normalized)

    @staticmethod
    def _first_matching_root(path: Path, roots: Iterable[Path]) -> Path | None:
        for root in roots:
            if path.is_relative_to(root):
                return root
        return None

    @staticmethod
    def _host_in_set(host: str, patterns: Iterable[str]) -> bool:
        host_l = host.lower().strip(".")
        for pattern in patterns:
            p = pattern.lower().strip()
            if not p:
                continue
            if p.startswith("*."):
                suffix = p[2:]
                if host_l == suffix or host_l.endswith(f".{suffix}"):
                    return True
            elif host_l == p:
                return True
        return False

    @staticmethod
    def _is_private_host(host: str) -> bool:
        normalized = host.lower().strip(".")
        if normalized in {"localhost"} or normalized.endswith(".local"):
            return True
        try:
            ip = ip_address(normalized)
        except ValueError:
            return False
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
