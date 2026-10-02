from __future__ import annotations

from typing import Any


def attribute_string(instance: dict[str, Any], name: str) -> str | None:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        string_value = value.get("String")
        if isinstance(string_value, str):
            return string_value
    return None
