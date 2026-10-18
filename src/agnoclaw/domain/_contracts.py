"""Small shared contracts for the opt-in domain SDK preview."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import fields
from datetime import datetime
from enum import Enum
from typing import Any, NoReturn

from agnoclaw.runtime.errors import HarnessError
from agnoclaw.runtime.security import canonical_json_digest, freeze_data, thaw_data


def fail(code: str, message: str) -> NoReturn:
    raise HarnessError(code=f"DOMAIN_{code}", category="domain", message=message, retryable=False)


def identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{field} must be a non-empty string of at most 512 characters")


def digest_string(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("expected a canonical sha256 digest")


def aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")


def frozen_json(value: Any, *, max_bytes: int = 1_000_000) -> Any:
    frozen = freeze_data(value)
    if len(json.dumps(thaw_data(frozen), ensure_ascii=False).encode("utf-8")) > max_bytes:
        raise ValueError("domain JSON exceeds its byte budget")
    return frozen


def wire(value: Any) -> Any:
    if isinstance(value, Record):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(item) for item in value]
    return value


class Record:
    """JSON export and content identity; not an authorization or storage layer."""

    def to_dict(self) -> dict[str, Any]:
        return {field.name: wire(getattr(self, field.name)) for field in fields(self)}  # type: ignore[arg-type]

    @property
    def digest(self) -> str:
        return canonical_json_digest({"contract": type(self).__name__, "v": 1, **self.to_dict()})
