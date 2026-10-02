from __future__ import annotations

from dataclasses import replace
from math import inf
from typing import Any

from schemer.analysis.connections import (
    component_symbol_groups,
    internal_signal_symbol_ids,
    module_boundary_net_names,
    resolved_to_source_ids,
)
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, position_from_viewer
from schemer.placement.refinement.connectors import (
    align_terminal_connectors_to_series_banks,
    compact_terminal_connector_gaps,
    orient_terminal_connectors_toward_series_banks,
)
from schemer.placement.refinement.decoupling import cluster_local_decoupling
from schemer.placement.refinement.lanes import separate_orthogonal_branch_lanes
from schemer.placement.refinement.orientation import orient_two_terminal_components
from schemer.placement.refinement.series import (
    align_series_to_device_pins,
    hang_leaf_series_from_device_pins,
)
from schemer.placement.refinement.terminals import (
    align_horizontal_series_terminals,
    align_leaf_series_endpoints,
)


def suppress_internal_signal_net_symbols(
    schematic: dict[str, Any],
    plan: LayoutPlan,
) -> LayoutPlan:
    """Remove label anchors that split an entirely local signal connection.

    A non-rail net joining two or more physical components inside one module
    should be drawn as their wire. A net symbol is retained when the net reaches
    that module's boundary, because it then represents a real external port.
    """

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    modules: list[ModuleLayout] = []
    for module in plan.modules:
        module_instance = instances.get(module.instance_ref)
        raw_positions = (
            module_instance.get("symbol_positions") if isinstance(module_instance, dict) else None
        )
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
        resolved_to_source = resolved_to_source_ids(proposed, module)
        suppressed = {
            resolved_to_source[symbol_id]
            for symbol_id in internal_signal_symbol_ids(
                resolved_positions,
                component_groups,
                nets,
                module_boundary_net_names(module_instance),
            )
        }

        modules.append(
            replace(
                module,
                positions={
                    symbol_id: position
                    for symbol_id, position in module.positions.items()
                    if symbol_id not in suppressed
                },
            )
        )
    return replace(plan, modules=tuple(modules))


def refine_symbol_geometry(schematic: dict[str, Any], plan: LayoutPlan) -> LayoutPlan:
    """Apply the generic pin-aware refinement sequence to a semantic plan."""

    direct_wires = suppress_internal_signal_net_symbols(schematic, plan)
    clustered = cluster_local_decoupling(schematic, direct_wires)
    oriented = orient_two_terminal_components(schematic, clustered)
    connectors_oriented = orient_terminal_connectors_toward_series_banks(schematic, oriented)
    connectors_aligned = align_terminal_connectors_to_series_banks(schematic, connectors_oriented)
    pins_aligned = align_series_to_device_pins(
        schematic, connectors_aligned, maximum_adjustment=inf
    )
    leaves_aligned = align_leaf_series_endpoints(schematic, pins_aligned, maximum_adjustment=inf)
    leaves_hung = hang_leaf_series_from_device_pins(schematic, leaves_aligned)
    aligned = align_horizontal_series_terminals(
        schematic,
        leaves_hung,
        maximum_endpoint_spread=inf,
        maximum_adjustment=inf,
    )
    separated = separate_orthogonal_branch_lanes(schematic, aligned)
    compacted = compact_terminal_connector_gaps(schematic, separated)
    return cluster_local_decoupling(schematic, compacted)
