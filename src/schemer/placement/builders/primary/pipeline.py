from __future__ import annotations

from typing import Any

from schemer.analysis.connections import is_rail_net
from schemer.core.layout import ModuleLayout
from schemer.placement.blocks.composition import block_from_positions
from schemer.placement.blocks.model import Rect
from schemer.placement.blocks.plan import BlockPlan
from schemer.placement.builders.primary.active import (
    place_subordinate_devices,
    prepare_primary_clearance,
)
from schemer.placement.builders.primary.branches import (
    place_primary_bias,
    place_primary_series,
    place_primary_shunts,
)
from schemer.placement.builders.primary.model import PrimaryPlacement
from schemer.placement.builders.primary.topology import analyze_primary_circuit
from schemer.placement.circuits.net_symbols import (
    anchor_net_symbol_attachments,
    attach_net_symbols,
    terminal_net_symbol_attachment,
)
from schemer.placement.circuits.queries import (
    is_boundary_net,
    net_components,
)
from schemer.symbols.geometry import placed_symbol_body_bounds


def primary_ic_block_from_zero(
    schematic: dict[str, Any], module: ModuleLayout, *, padding: float = 40.0,
) -> BlockPlan | None:
    """Place a validated primary circuit, or let another builder handle its topology."""
    recipe = analyze_primary_circuit(schematic, module)
    if recipe is None:
        return None
    state = PrimaryPlacement(recipe)
    state.positions[recipe.primary.symbol_id] = state.primary_position
    place_primary_bias(state)
    if not place_primary_series(state):
        return None
    obstacles = prepare_primary_clearance(state)
    if not place_subordinate_devices(state, obstacles) or not place_primary_shunts(state):
        return None
    return complete_primary_block(state, padding=padding)

def complete_primary_block(state: PrimaryPlacement, *, padding: float) -> BlockPlan:
    for component, position in state.placed_active.values():
        if component != state.recipe.primary:
            state.attachments.extend(anchor_net_symbol_attachments(component, position))
        for terminal, (net_ref, net) in component.terminals.items():
            if (
                net_ref in state.labelled_nets
                or is_rail_net(net_ref, net)
                or str(net.get("name", net_ref)).rsplit(".", 1)[-1].startswith("NC_")
                or not is_boundary_net(net_ref, net, state.recipe.boundary_net_names)
            ):
                continue
            if net_components(net, state.recipe.component_refs) != {component.ref}:
                continue
            state.attachments.append(terminal_net_symbol_attachment(component, position, terminal))
            state.labelled_nets.add(net_ref)

    attach_net_symbols(state.positions, tuple(state.attachments))
    occupied: dict[str, Rect] = {}
    for component in state.recipe.components:
        position = state.positions[component.symbol_id]
        bounds = placed_symbol_body_bounds(component.instance, position)
        occupied[component.symbol_id] = Rect(
            bounds.min_x,
            bounds.min_y,
            bounds.max_x - bounds.min_x,
            bounds.max_y - bounds.min_y,
        )
    block = block_from_positions(
        "primary-ic-local",
        state.positions,
        occupied=occupied,
        padding=padding,
    )
    result = BlockPlan(block)
    result.validate()
    return result
