"""In-memory schematic geometry matching shadow-only symbol projections."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from schemer.shadow import find_workspace_root
from schemer.toolchain import ToolchainError


def _attribute_string(instance: dict[str, Any], name: str) -> str | None:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("String"), str):
        return value["String"]
    return None


def select_kicad_symbol(source: str, symbol_name: str) -> str:
    """Extract one top-level symbol from a KiCad symbol-library source.

    Evaluated ``__symbol_value`` contains only the selected outer symbol, not
    its whole library. Projection feedback must preserve that distinction or
    unused sibling symbols incorrectly inflate bounds and duplicate pins.
    """

    marker = "(symbol "
    search_from = 0
    while (start := source.find(marker, search_from)) >= 0:
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(source)):
            character = source[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == '"':
                quoted = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    block = source[start : index + 1]
                    name_match = re.match(r'\(symbol\s+"([^"\\]*(?:\\.[^"\\]*)*)"', block)
                    if name_match is not None and name_match.group(1) == symbol_name:
                        return block
                    search_from = index + 1
                    break
        else:
            break
    raise ToolchainError(f"projected symbol library has no entry {symbol_name!r}")


def schematic_with_symbol_overrides(
    schematic: dict[str, Any],
    entrypoint: Path,
    file_overrides: dict[Path, str],
) -> dict[str, Any]:
    """Project shadow symbol sources into evaluated component geometry.

    This lets every later pin-registration and sheet-metrics pass see the same
    symbols that the exact materialised proposal will contain. Selection is by
    resolved package path, never by reference designator or board identity.
    """

    projected = deepcopy(schematic)
    instances = projected.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances must be an object")
    workspace = find_workspace_root(entrypoint)
    normalized_overrides = {path.resolve(): source for path, source in file_overrides.items()}
    used: set[Path] = set()
    for instance in instances.values():
        if not isinstance(instance, dict) or instance.get("kind") != "Component":
            continue
        symbol_path = _attribute_string(instance, "symbol_path")
        if symbol_path is None or not symbol_path.startswith("package://"):
            continue
        relative = Path(symbol_path.removeprefix("package://"))
        if relative.parts and relative.parts[0] == "workspace":
            relative = Path(*relative.parts[1:])
            source_path = (workspace / relative).resolve()
        elif relative.parts and relative.parts[0] == "stdlib":
            source_path = (workspace / ".pcb" / relative).resolve()
        else:
            source_path = (workspace / relative).resolve()
        source = normalized_overrides.get(source_path)
        if source is None:
            continue
        symbol_name = _attribute_string(instance, "symbol_name")
        if symbol_name is None:
            raise ToolchainError("projected component has no symbol name")
        attributes = instance.get("attributes")
        if not isinstance(attributes, dict):
            raise ToolchainError("projected component attributes must be an object")
        attributes["__symbol_value"] = {"String": select_kicad_symbol(source, symbol_name)}
        used.add(source_path)
    unused = set(normalized_overrides) - used
    if unused:
        raise ToolchainError(
            "symbol projection override has no evaluated component user: "
            + ", ".join(str(path) for path in sorted(unused))
        )
    return projected
