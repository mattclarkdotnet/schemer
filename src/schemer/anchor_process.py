"""Stage-2 placement of the automatically selected primary IC."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.layout import LayoutPlan
from schemer.layout_metrics import primary_anchor_metrics, top_level_root_symbol_groups
from schemer.toolchain import ToolchainError


def center_primary_ic(
    schematic: dict[str, Any],
    plan: LayoutPlan,
) -> LayoutPlan:
    """Move the primary IC's functional block to the current layout centre.

    The primary is selected from evaluated physical ICs, not a reference or
    board profile. If it belongs to a placed child module, the child anchor is
    moved as one unit so already-owned local support follows the IC.

    This scale-invariant helper is not part of the production pipeline; final
    block packing establishes relative placement after local composition.
    """

    proposed = plan.apply_to_schematic(schematic)
    metrics = primary_anchor_metrics(proposed)
    root_ref = proposed.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    root_assignments = top_level_root_symbol_groups(proposed)
    target_x = metrics.page.center_x
    target_y = metrics.page.center_y
    delta_x = target_x - metrics.primary.center_x
    delta_y = target_y - metrics.primary.center_y

    owners = [
        module
        for module in plan.modules
        if metrics.primary_ref.startswith(module.instance_ref + ".")
    ]
    if not owners:
        raise ToolchainError(f"primary IC is outside every layout module: {metrics.primary_ref}")
    owner = max(owners, key=lambda module: len(module.instance_ref))

    modules = list(plan.modules)
    parent_candidates = [
        module
        for module in plan.modules
        if owner.instance_ref.startswith(module.instance_ref + ".")
    ]
    moved = False
    for parent in sorted(
        parent_candidates,
        key=lambda module: len(module.instance_ref),
        reverse=True,
    ):
        local_path = owner.instance_ref.removeprefix(parent.instance_ref + ".")
        anchor_id = f"comp:{local_path}"
        if anchor_id not in parent.positions:
            continue
        updated = dict(parent.positions)
        if parent.instance_ref == root_ref:
            group_name = local_path.split(".", 1)[0]
            matching = [
                symbol_id
                for symbol_id in parent.positions
                if root_assignments.get(symbol_id) == group_name
            ]
        else:
            matching = [anchor_id]
        if not matching:
            continue
        for symbol_id in matching:
            position = updated[symbol_id]
            updated[symbol_id] = replace(
                position,
                x=position.x + delta_x,
                y=position.y + delta_y,
            )
        modules[modules.index(parent)] = replace(parent, positions=updated)
        moved = True
        break

    if not moved:
        if owner.instance_ref == root_ref:
            group_name = metrics.primary_ref.removeprefix(root_ref + ".").split(".", 1)[0]
            matching = [
                symbol_id
                for symbol_id in owner.positions
                if root_assignments.get(symbol_id) == group_name
            ]
        else:
            local_ref = metrics.primary_ref.removeprefix(owner.instance_ref + ".")
            prefix = f"comp:{local_ref}"
            matching = [
                symbol_id
                for symbol_id in owner.positions
                if symbol_id == prefix or symbol_id.startswith(prefix + "@")
            ]
        if not matching:
            raise ToolchainError(f"primary IC has no movable layout anchor: {metrics.primary_ref}")
        updated = dict(owner.positions)
        for symbol_id in matching:
            position = updated[symbol_id]
            updated[symbol_id] = replace(
                position,
                x=position.x + delta_x,
                y=position.y + delta_y,
            )
        modules[modules.index(owner)] = replace(owner, positions=updated)

    return replace(plan, modules=tuple(modules))
