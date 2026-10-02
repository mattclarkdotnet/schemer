from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.analysis.connections import component_symbol_groups, net_anchor, opposing_anchor_axis
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position, position_from_viewer
from schemer.symbols.geometry import pin_position
from schemer.symbols.library import symbol_pin_offsets


def _module_orientations(
    schematic: dict[str, Any], module: ModuleLayout, resolved_positions: dict[str, Position]
) -> ModuleLayout:
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
    net_symbol_groups: dict[str, list[Position]] = {}
    for symbol_id, position in resolved_positions.items():
        if not symbol_id.startswith("sym:"):
            continue
        net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
        if separator and suffix.isdigit():
            net_symbol_groups.setdefault(net_name, []).append(position)

    updated = dict(module.positions)
    for component_ref, group in component_groups.items():
        if len(group) != 1:
            continue
        symbol_id, component_position = group[0]
        instance = instances.get(component_ref)
        if not isinstance(instance, dict) or instance.get("kind") != "Component":
            continue

        pin_offsets = symbol_pin_offsets(instance)
        terminal_nets: dict[str, dict[str, Any]] = {}
        prefix = component_ref + "."
        for net in nets.values():
            if not isinstance(net, dict):
                continue
            for port_ref in net.get("ports", []):
                if not isinstance(port_ref, str) or not port_ref.startswith(prefix):
                    continue
                terminal_name = port_ref.removeprefix(prefix)
                if "." not in terminal_name and terminal_name in pin_offsets:
                    terminal_nets[terminal_name] = net
        if len(terminal_nets) != 2 or len({id(net) for net in terminal_nets.values()}) != 2:
            continue

        terminals = sorted(terminal_nets)
        first_terminal, second_terminal = terminals
        first_anchor = net_anchor(
            terminal_nets[first_terminal],
            subject_ref=component_ref,
            component_groups=component_groups,
            net_symbol_groups=net_symbol_groups,
        )
        second_anchor = net_anchor(
            terminal_nets[second_terminal],
            subject_ref=component_ref,
            component_groups=component_groups,
            net_symbol_groups=net_symbol_groups,
        )
        if first_anchor is None or second_anchor is None:
            continue
        if opposing_anchor_axis(first_anchor, second_anchor, component_position) is None:
            continue

        first_pin = pin_position(instance, component_position, first_terminal)
        second_pin = pin_position(instance, component_position, second_terminal)
        anchor_vector = (
            second_anchor.x - first_anchor.x,
            second_anchor.y - first_anchor.y,
        )
        pin_vector = (second_pin.x - first_pin.x, second_pin.y - first_pin.y)
        if anchor_vector[0] * pin_vector[0] + anchor_vector[1] * pin_vector[1] >= 0:
            continue

        local_symbol_id = symbol_id
        source_position = module.positions[local_symbol_id]
        updated[local_symbol_id] = replace(
            source_position,
            rotation=(source_position.rotation + 180) % 360,
        )

    return replace(module, positions=updated)


def orient_two_terminal_components(schematic: dict[str, Any], plan: LayoutPlan) -> LayoutPlan:
    """Orient every evidenced two-terminal branch toward its own neighbouring nets.

    This pass is board-agnostic. It considers every placed two-terminal
    component, uses evaluated connectivity to find the geometric centre of the
    components and net symbols on each terminal's net, and flips the symbol by
    180 degrees when its real pin order opposes those anchors. Components whose
    two nets do not have clear, opposing anchors retain their semantic
    orientation; no reference-designator or component-type exceptions exist.
    """

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances were not an object")
    modules = []
    for module in plan.modules:
        instance = instances.get(module.instance_ref)
        if not isinstance(instance, dict):
            raise ToolchainError(f"layout module is absent: {module.instance_ref}")
        raw_positions = instance.get("symbol_positions")
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        modules.append(_module_orientations(proposed, module, resolved_positions))
    return replace(plan, modules=tuple(modules))
