from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.core.errors import ToolchainError
from schemer.core.layout import (
    ModuleLayout,
    Position,
)


def translate_root_groups(
    root_layout: ModuleLayout,
    assignments: dict[str, str],
    deltas: dict[str, tuple[float, float]],
) -> dict[str, Position]:
    """Translate complete root groups without separating their local branches."""

    updated = dict(root_layout.positions)
    moved_anchors: set[str] = set()
    for symbol_id, position in root_layout.positions.items():
        group_name = assignments.get(symbol_id)
        delta = deltas.get(group_name) if group_name is not None else None
        if delta is None:
            continue
        delta_x, delta_y = delta
        updated[symbol_id] = replace(
            position,
            x=position.x + delta_x,
            y=position.y + delta_y,
        )
        if symbol_id.startswith("comp:"):
            moved_anchors.add(group_name)
    missing = set(deltas) - moved_anchors
    if missing:
        raise ToolchainError(
            "top-level functional groups have no movable root anchors: "
            + ", ".join(sorted(missing))
        )
    return updated


def module_net_kind(
    schematic: dict[str, Any],
    module_ref: str,
    source_symbol_id: str,
) -> str:
    root_ref = schematic.get("root_ref")
    nets = schematic.get("nets")
    if not isinstance(root_ref, str) or not isinstance(nets, dict):
        raise ToolchainError("schematic root or nets are invalid")
    raw_name = source_symbol_id.removeprefix("sym:").rsplit("#", 1)[0]
    module_path = "" if module_ref == root_ref else module_ref.removeprefix(root_ref + ".")
    scoped_name = f"{module_path}.{raw_name}" if module_path else raw_name
    for candidate in (raw_name, scoped_name):
        net = nets.get(candidate)
        if isinstance(net, dict):
            return str(net.get("kind", ""))
    for net in nets.values():
        if isinstance(net, dict) and net.get("name") in {raw_name, scoped_name}:
            return str(net.get("kind", ""))
    return ""
