from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Any

from schemer.analysis.connections import is_rail_net
from schemer.analysis.measurements import top_level_root_symbol_groups
from schemer.core.errors import ToolchainError
from schemer.core.layout import (
    LayoutPlan,
    ModuleLayout,
    resolve_module_position_ids,
)
from schemer.placement.spacing.shared import module_net_kind


def spread_parallel_rail_labels(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    row_tolerance: float = 30.0,
    minimum_pitch: float = 160.0,
    average_character_width: float = 6.5,
    label_padding: float = 25.0,
) -> LayoutPlan:
    """Separate crowded power/ground symbols that occupy one label row.

    The viewer renders rail names horizontally above their symbols. Several
    distinct rails on nearby pins therefore need more pitch than their pin
    origins provide. This pass preserves order and y lanes, moving only rail
    symbols and leaving every component anchor untouched.
    """

    if row_tolerance < 0 or minimum_pitch <= 0 or average_character_width <= 0:
        raise ToolchainError("rail-label spacing parameters are invalid")
    modules: list[ModuleLayout] = []
    for module in plan.modules:
        candidates = [
            (symbol_id, position)
            for symbol_id, position in module.positions.items()
            if symbol_id.startswith("sym:")
            and module_net_kind(schematic, module.instance_ref, symbol_id) in {"Power", "Ground"}
        ]
        rows: list[list[tuple[str, Any]]] = []
        for candidate in sorted(candidates, key=lambda item: (item[1].y, item[1].x, item[0])):
            row = next(
                (
                    current
                    for current in rows
                    if abs(candidate[1].y - median(item[1].y for item in current)) <= row_tolerance
                ),
                None,
            )
            if row is None:
                rows.append([candidate])
            else:
                row.append(candidate)

        updated = dict(module.positions)
        for row in rows:
            if len(row) < 2:
                continue
            ordered = sorted(row, key=lambda item: (item[1].x, item[0]))
            targets = [item[1].x for item in ordered]
            for index in range(1, len(ordered)):
                left_name = ordered[index - 1][0].removeprefix("sym:").rsplit("#", 1)[0]
                right_name = ordered[index][0].removeprefix("sym:").rsplit("#", 1)[0]
                text_pitch = (
                    len(left_name) + len(right_name)
                ) * average_character_width / 2 + label_padding
                pitch = max(minimum_pitch, text_pitch)
                targets[index] = max(targets[index], targets[index - 1] + pitch)
            centering = float(median(item[1].x for item in ordered) - median(targets))
            for (symbol_id, position), target_x in zip(ordered, targets, strict=True):
                updated[symbol_id] = replace(position, x=target_x + centering)
        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def remove_redundant_root_rails(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    regenerated_modules: set[str],
) -> LayoutPlan:
    """Discard seed rail copies superseded by a completed child's local rails.

    Ownership is per displayed copy, not per entire net: a connector elsewhere
    on the same rail keeps its own root symbol. Ordinary signal labels remain.
    """
    root_ref = schematic["root_ref"]
    root = next(module for module in plan.modules if module.instance_ref == root_ref)
    assignments = top_level_root_symbol_groups(schematic)
    local_rails = {}
    for module in plan.modules:
        if module.instance_ref == root_ref or module.instance_ref not in regenerated_modules:
            continue
        local_rails[module.instance_ref] = {
            symbol_id.removeprefix("sym:").rsplit("#", 1)[0]
            for symbol_id in resolve_module_position_ids(module, schematic)
            if symbol_id.startswith("sym:")
        }
    updated = dict(root.positions)
    for symbol_id in root.positions:
        if not symbol_id.startswith("sym:"):
            continue
        net_name = symbol_id.removeprefix("sym:").rsplit("#", 1)[0]
        net = schematic["nets"].get(net_name)
        owner = assignments.get(symbol_id)
        if (
            isinstance(net, dict)
            and is_rail_net(net_name, net)
            and owner is not None
            and net_name in local_rails.get(f"{root_ref}.{owner}", set())
        ):
            del updated[symbol_id]
    return replace(
        plan,
        modules=tuple(
            replace(module, positions=updated) if module is root else module
            for module in plan.modules
        ),
    )
