from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Any

from schemer.analysis.connections import (
    component_symbol_groups,
    is_rail_net,
    port_component_ref,
    resolved_to_source_ids,
    single_neighbour_pin_anchor,
    terminal_net_map,
    two_terminal_axis,
)
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position, position_from_viewer
from schemer.symbols.geometry import pin_position


def align_terminal_connectors_to_series_banks(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_adjustment: float = 500.0,
) -> LayoutPlan:
    """Translate a terminal connector so its bank pins meet opposite device pins.

    Connector and device pin pitches often match while their initial vertical
    origins do not. The median paired-pin delta moves the connector as a body;
    the later series alignment pass can then produce straight horizontal runs.
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
        positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, positions)
        component_refs = tuple(component_groups)
        resolved_to_source = resolved_to_source_ids(proposed, module)
        updated = dict(module.positions)

        series: list[tuple[str, dict[str, tuple[str, dict[str, Any]]]]] = []
        connectors: list[tuple[str, str, Position]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            symbol_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminals = terminal_net_map(component_ref, component, nets)
            if (attribute_string(component, "type") or "").casefold() == "connector":
                connectors.append((component_ref, symbol_id, position))
                continue
            if len(terminals) != 2:
                continue
            terminal_names = tuple(sorted(terminals))
            if two_terminal_axis(component, position, terminal_names) != "x":
                continue
            if any(is_rail_net(net_ref, net) for net_ref, net in terminals.values()):
                continue
            series.append((component_ref, terminals))

        for connector_ref, connector_id, connector_position in sorted(connectors):
            connector = instances[connector_ref]
            connector_terminals = terminal_net_map(connector_ref, connector, nets)
            deltas: list[float] = []
            for series_ref, series_terminals in series:
                shared = [
                    (terminal, net_ref, net)
                    for terminal, (net_ref, net) in series_terminals.items()
                    if net_ref
                    in {candidate_ref for candidate_ref, _ in connector_terminals.values()}
                ]
                if len(shared) != 1:
                    continue
                _, shared_net_ref, shared_net = shared[0]
                connector_pin_names = [
                    terminal
                    for terminal, (net_ref, _) in connector_terminals.items()
                    if net_ref == shared_net_ref
                ]
                if len(connector_pin_names) != 1:
                    continue
                connector_pin = pin_position(connector, connector_position, connector_pin_names[0])
                opposite_nets = [
                    net for net_ref, net in series_terminals.values() if net_ref != shared_net_ref
                ]
                if len(opposite_nets) != 1:
                    continue
                opposite_net = opposite_nets[0]
                neighbours = {
                    neighbour_ref
                    for port_ref in opposite_net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := port_component_ref(port_ref, component_refs)) is not None
                    if neighbour_ref not in {series_ref, connector_ref}
                }
                if len(neighbours) != 1:
                    continue
                opposite_pin = single_neighbour_pin_anchor(
                    opposite_net,
                    subject_ref=series_ref,
                    component_groups=component_groups,
                    instances=instances,
                    net_symbol_groups={},
                )
                if opposite_pin is None:
                    continue
                # The shared net object is deliberately resolved above: it
                # proves this is a direct connector-to-series branch.
                if not isinstance(shared_net, dict):
                    continue
                deltas.append(opposite_pin.y - connector_pin.y)

            if not deltas:
                continue
            delta_y = float(median(deltas))
            if abs(delta_y) > maximum_adjustment:
                continue
            source_id = resolved_to_source[connector_id]
            updated[source_id] = replace(updated[source_id], y=updated[source_id].y + delta_y)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def orient_terminal_connectors_toward_series_banks(
    schematic: dict[str, Any], plan: LayoutPlan
) -> LayoutPlan:
    """Rotate terminal connectors so their connected pins face a series bank.

    Candidate rotations are scored from real evaluated pin geometry to the
    centres of directly connected horizontal two-terminal components. The
    current rotation wins exact ties, avoiding gratuitous changes. Connector
    classification and adjacency are both generic evaluated semantics.
    """

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    modules: list[ModuleLayout] = []
    for module in plan.modules:
        instance = instances.get(module.instance_ref)
        raw_positions = instance.get("symbol_positions") if isinstance(instance, dict) else None
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
        resolved_to_source = resolved_to_source_ids(proposed, module)

        series_by_net: dict[str, list[Position]] = {}
        connectors: list[tuple[str, str, Position, dict[str, tuple[str, dict[str, Any]]]]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(component_ref, component, nets)
            if attribute_string(component, "type") == "connector":
                connectors.append((component_ref, resolved_id, position, terminal_nets))
                continue
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if two_terminal_axis(component, position, terminals) != "x":
                continue
            if any(is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue
            for net_ref, _ in terminal_nets.values():
                series_by_net.setdefault(net_ref, []).append(position)

        updated = dict(module.positions)
        for connector_ref, connector_id, connector_position, terminal_nets in connectors:
            component = instances[connector_ref]
            evidenced_terminals = {
                terminal: series_by_net[net_ref]
                for terminal, (net_ref, _) in terminal_nets.items()
                if net_ref in series_by_net
            }
            if not evidenced_terminals:
                continue

            current_rotation = int(connector_position.rotation) % 360

            def rotation_score(rotation: int) -> tuple[float, int]:
                candidate_position = replace(connector_position, rotation=rotation)
                distance = 0.0
                for terminal, series_positions in evidenced_terminals.items():
                    terminal_position = pin_position(component, candidate_position, terminal)
                    distance += min(
                        abs(terminal_position.x - series_position.x)
                        + abs(terminal_position.y - series_position.y)
                        for series_position in series_positions
                    )
                return distance, 0 if rotation == current_rotation else 1

            best_rotation = min((0, 90, 180, 270), key=rotation_score)
            if best_rotation == current_rotation:
                continue
            connector_source_id = resolved_to_source[connector_id]
            updated[connector_source_id] = replace(
                updated[connector_source_id], rotation=best_rotation
            )

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def compact_terminal_connector_gaps(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_gap: float = 450.0,
    target_gap: float = 400.0,
    support_radius: float = 750.0,
    attached_symbol_radius: float = 800.0,
) -> LayoutPlan:
    """Pull remote terminal connectors toward an adjacent horizontal series bank.

    A connector at the end of one or more series branches should read as the
    endpoint of those branches, not as an apparently unrelated island. This
    pass finds connector components from evaluated type metadata, identifies
    directly connected horizontal two-terminal branches, and closes only
    excessive terminal gaps. Nearby one-rail support branches and local net
    symbols on the connector's own nets move with it so the endpoint cluster
    remains coherent.

    Selection is entirely semantic and geometric: there are no board names,
    reference designators, package names, or component-specific exceptions.
    """

    if target_gap < 0 or maximum_gap <= target_gap:
        raise ValueError("terminal connector gap limits must satisfy 0 <= target < maximum")

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    modules: list[ModuleLayout] = []
    for module in plan.modules:
        instance = instances.get(module.instance_ref)
        raw_positions = instance.get("symbol_positions") if isinstance(instance, dict) else None
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
        resolved_to_source = resolved_to_source_ids(proposed, module)

        component_terminals: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        horizontal_series: dict[str, tuple[Position, frozenset[str]]] = {}
        connectors: list[tuple[str, str, Position, frozenset[str]]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(component_ref, component, nets)
            component_terminals[component_ref] = terminal_nets
            net_refs = frozenset(net_ref for net_ref, _ in terminal_nets.values())
            if attribute_string(component, "type") == "connector":
                connectors.append((component_ref, resolved_id, position, net_refs))
                continue
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if two_terminal_axis(component, position, terminals) != "x":
                continue
            if any(is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue
            horizontal_series[component_ref] = (position, net_refs)

        updated = dict(module.positions)
        moved_resolved_positions = dict(resolved_positions)
        claimed_components: set[str] = set()
        claimed_symbols: set[str] = set()
        for connector_ref, connector_id, connector_position, connector_nets in sorted(connectors):
            adjacent = [
                position
                for position, net_refs in horizontal_series.values()
                if connector_nets & net_refs
            ]
            if not adjacent:
                continue
            bank_x = float(median(position.x for position in adjacent))
            if (
                min(position.x for position in adjacent)
                < connector_position.x
                < max(position.x for position in adjacent)
            ):
                continue
            direction = 1.0 if bank_x > connector_position.x else -1.0
            gap = abs(bank_x - connector_position.x)
            if gap <= maximum_gap:
                continue
            target_x = bank_x - direction * target_gap
            delta_x = target_x - connector_position.x

            connector_source_id = resolved_to_source[connector_id]
            updated[connector_source_id] = replace(
                updated[connector_source_id], x=updated[connector_source_id].x + delta_x
            )
            moved_resolved_positions[connector_id] = replace(connector_position, x=target_x)
            claimed_components.add(connector_ref)

            for support_ref, terminal_nets in component_terminals.items():
                if support_ref in claimed_components or len(terminal_nets) != 2:
                    continue
                support_group = component_groups[support_ref]
                support_id, support_position = support_group[0]
                distance = abs(support_position.x - connector_position.x) + abs(
                    support_position.y - connector_position.y
                )
                if distance > support_radius:
                    continue
                support_net_refs = {net_ref for net_ref, _ in terminal_nets.values()}
                rail_count = sum(
                    is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()
                )
                if rail_count != 1 or not connector_nets & support_net_refs:
                    continue
                support_source_id = resolved_to_source[support_id]
                updated[support_source_id] = replace(
                    updated[support_source_id],
                    x=updated[support_source_id].x + delta_x,
                )
                moved_resolved_positions[support_id] = replace(
                    support_position, x=support_position.x + delta_x
                )
                claimed_components.add(support_ref)

            connector_net_names = {
                str(net.get("name", net_ref))
                for net_ref, net in component_terminals[connector_ref].values()
            }
            for symbol_id, symbol_position in tuple(moved_resolved_positions.items()):
                if not symbol_id.startswith("sym:") or symbol_id in claimed_symbols:
                    continue
                net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
                if not separator or not suffix.isdigit() or net_name not in connector_net_names:
                    continue
                distance = abs(symbol_position.x - connector_position.x) + abs(
                    symbol_position.y - connector_position.y
                )
                if distance > attached_symbol_radius:
                    continue
                symbol_source_id = resolved_to_source[symbol_id]
                updated[symbol_source_id] = replace(
                    updated[symbol_source_id], x=updated[symbol_source_id].x + delta_x
                )
                moved_resolved_positions[symbol_id] = replace(
                    symbol_position, x=symbol_position.x + delta_x
                )
                claimed_symbols.add(symbol_id)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))
