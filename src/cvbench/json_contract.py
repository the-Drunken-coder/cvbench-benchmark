"""Shared compact JSON serialization used for callback byte accounting."""

from __future__ import annotations

import json
from typing import Any


def serialized_json_bytes(value: Any) -> bytes:
    """Serialize JSON exactly as the trusted runner transmits it over HTTP."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
