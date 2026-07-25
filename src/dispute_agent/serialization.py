from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def summarize(value: Any, *, max_string: int = 500, max_items: int = 25) -> Any:
    value = jsonable(value)
    if isinstance(value, str):
        return value if len(value) <= max_string else f"{value[:max_string]}…"
    if isinstance(value, list):
        result = [summarize(item, max_string=max_string, max_items=max_items) for item in value[:max_items]]
        if len(value) > max_items:
            result.append({"truncated_items": len(value) - max_items})
        return result
    if isinstance(value, dict):
        items = list(value.items())
        result = {
            str(key): summarize(item, max_string=max_string, max_items=max_items)
            for key, item in items[:max_items]
        }
        if len(items) > max_items:
            result["_truncated_fields"] = len(items) - max_items
        return result
    return value
