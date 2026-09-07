"""Position records, source edits, and layout-plan application."""

from __future__ import annotations

import difflib
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schemer.toolchain import ToolchainError

_POSITION_PREFIX = "# pcb:sch "


@dataclass(frozen=True)
class Position:
    """A viewer position persisted by pcb-sch."""

    x: float
    y: float
    rotation: float = 0
    mirror: str | None = None

    def as_viewer_dict(self) -> dict[str, float | str]:
        result: dict[str, float | str] = {
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
        }
        if self.mirror is not None:
            result["mirror"] = self.mirror
        return result


@dataclass(frozen=True)
class ModuleLayout:
    """Positions owned by one evaluated module and one Zener source file."""

    instance_ref: str
    source_path: Path
    positions: dict[str, Position]


@dataclass(frozen=True)
class LayoutPlan:
    """A complete multi-module layout proposal for one evaluated entrypoint."""

    modules: tuple[ModuleLayout, ...]

    def apply_to_schematic(self, schematic: dict[str, Any]) -> dict[str, Any]:
        proposed = deepcopy(schematic)
        instances = proposed.get("instances")
        if not isinstance(instances, dict):
            raise ToolchainError("schematic instances were not an object")

        for module in self.modules:
            instance = instances.get(module.instance_ref)
            if not isinstance(instance, dict):
                raise ToolchainError(
                    f"layout module is absent from evaluation: {module.instance_ref}"
                )
            positions = resolve_module_position_ids(module, proposed)
            instance["symbol_positions"] = {
                symbol_id: position.as_viewer_dict() for symbol_id, position in positions.items()
            }
        return proposed

    def proposed_sources(self) -> dict[Path, str]:
        updates: dict[Path, str] = {}
        for module in self.modules:
            content = module.source_path.read_text()
            updates[module.source_path] = replace_position_block(content, module.positions)
        return updates


def resolve_module_position_ids(
    module: ModuleLayout, schematic: dict[str, Any]
) -> dict[str, Position]:
    """Resolve source-local net symbols to evaluated, instance-scoped viewer IDs."""

    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic has no root reference")
    if module.instance_ref == root_ref:
        module_path = ""
    elif module.instance_ref.startswith(root_ref + "."):
        module_path = module.instance_ref.removeprefix(root_ref + ".")
    else:
        raise ToolchainError(f"layout module is outside the schematic root: {module.instance_ref}")

    raw_nets = schematic.get("nets")
    if not isinstance(raw_nets, dict):
        raise ToolchainError("schematic nets were not an object")
    net_names = {
        net.get("name")
        for net in raw_nets.values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }

    resolved: dict[str, Position] = {}
    for symbol_id, position in module.positions.items():
        if symbol_id.startswith("comp:"):
            resolved_id = symbol_id
        elif symbol_id.startswith("sym:"):
            net_symbol = symbol_id.removeprefix("sym:")
            net_name, separator, suffix = net_symbol.rpartition("#")
            if not separator or not suffix.isdigit():
                raise ToolchainError(f"invalid schematic net-symbol ID: {symbol_id}")
            scoped_name = f"{module_path}.{net_name}" if module_path else net_name
            if net_name in net_names:
                actual_name = net_name
            elif scoped_name in net_names:
                actual_name = scoped_name
            else:
                raise ToolchainError(
                    f"layout net symbol does not resolve in {module.instance_ref}: {symbol_id}"
                )
            resolved_id = f"sym:{actual_name}#{suffix}"
        else:
            raise ToolchainError(f"invalid schematic symbol ID: {symbol_id}")

        if resolved_id in resolved:
            raise ToolchainError(
                f"multiple source positions resolve to the same viewer ID: {resolved_id}"
            )
        resolved[resolved_id] = position
    return resolved


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)
    )


def symbol_id_to_comment_key(symbol_id: str) -> str:
    """Match pcb-sch's conversion from stable viewer ID to comment key."""

    if symbol_id.startswith("comp:"):
        return symbol_id.removeprefix("comp:")
    if symbol_id.startswith("sym:"):
        return symbol_id.removeprefix("sym:").replace("#", ".")
    raise ToolchainError(f"invalid schematic symbol ID: {symbol_id}")


def position_block_start(content: str) -> int:
    """Find the final contiguous pcb:sch/blank block using pcb-sch semantics."""

    block_start = len(content)
    for line in reversed(content.splitlines(keepends=True)):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith(_POSITION_PREFIX):
            block_start -= len(line)
            continue
        break
    return block_start


def format_position_block(positions: dict[str, Position]) -> str:
    """Format stable viewer IDs as a naturally ordered pcb:sch block."""

    by_comment_key = {
        symbol_id_to_comment_key(symbol_id): position for symbol_id, position in positions.items()
    }
    lines: list[str] = []
    for element_id in sorted(by_comment_key, key=_natural_key):
        position = by_comment_key[element_id]
        mirror = f" mirror={position.mirror}" if position.mirror is not None else ""
        lines.append(
            f"{_POSITION_PREFIX}{element_id} "
            f"x={position.x:.4f} y={position.y:.4f} rot={position.rotation:.0f}{mirror}\n"
        )
    return "".join(lines)


def replace_position_block(content: str, positions: dict[str, Position]) -> str:
    """Replace only the final persisted layout block."""

    prefix = content[: position_block_start(content)]
    block = format_position_block(positions)
    if not block:
        return prefix
    if not prefix:
        return block
    if prefix.endswith("\n\n"):
        separator = ""
    elif prefix.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return prefix + separator + block


def source_diff(path: Path, before: str, after: str) -> str:
    """Return a unified diff for one proposed source edit."""

    if before == after:
        return ""
    label = str(path)
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=label,
            tofile=label,
        )
    )
