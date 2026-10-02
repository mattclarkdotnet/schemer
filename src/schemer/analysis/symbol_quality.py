from __future__ import annotations

from math import hypot
from statistics import median
from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    component_group_bounds,
    component_symbol_groups,
    internal_signal_symbol_ids,
    is_named_interface_net,
    is_rail_net,
    is_return_net,
    module_boundary_net_names,
    opposing_anchor_axis,
    port_component_ref,
    single_neighbour_pin_anchor,
    terminal_net_map,
    two_terminal_axis,
)
from schemer.analysis.findings import QualityFinding
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position, position_from_viewer
from schemer.symbols.geometry import pin_position, pin_positions
from schemer.symbols.model import Point


def schematic_quality_findings(
    schematic: dict[str, Any],
    *,
    maximum_series_dogleg: float = 1.0,
    maximum_local_passive_gap: float = 40.0,
    maximum_connected_component_gap: float = 400.0,
    minimum_bank_pitch: float = 90.0,
    lane_tolerance: float = 40.0,
    bank_group_span: float = 400.0,
) -> tuple[QualityFinding, ...]:
    """Measure detached components, series doglegs, and compressed banks.

    Findings are review evidence rather than hard electrical errors. The same
    ambiguity rules as alignment apply, so shared fan-in/fan-out topology is
    not mislabeled as a correctable two-endpoint dogleg.
    """

    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    findings: list[QualityFinding] = []
    for module_ref, module_instance in instances.items():
        if not isinstance(module_ref, str) or not isinstance(module_instance, dict):
            continue
        raw_positions = module_instance.get("symbol_positions")
        if not isinstance(raw_positions, dict) or not raw_positions:
            continue
        positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module_ref, positions)
        net_symbol_groups: dict[str, list[Position]] = {}
        for symbol_id, position in positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbol_groups.setdefault(net_name, []).append(position)

        for symbol_id in sorted(
            internal_signal_symbol_ids(
                positions,
                component_groups,
                nets,
                module_boundary_net_names(module_instance),
            )
        ):
            findings.append(
                QualityFinding(
                    code="internal-signal-net-symbol",
                    module_ref=module_ref,
                    symbol_ids=(symbol_id,),
                    measured=1.0,
                    threshold=0.0,
                    message="an entirely local signal net is split by a label instead of a wire",
                )
            )

        component_refs = tuple(component_groups)
        for component_ref, group in sorted(component_groups.items()):
            if len(group) != 1:
                continue
            symbol_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            connection_gaps: list[float] = []
            terminals_by_net: dict[str, tuple[dict[str, Any], list[str]]] = {}
            for terminal, (net_ref, net) in terminal_net_map(
                component_ref, component, nets
            ).items():
                terminals_by_net.setdefault(net_ref, (net, []))[1].append(terminal)
            for net_ref, (net, terminals) in sorted(terminals_by_net.items()):
                own_points = tuple(
                    point
                    for terminal in terminals
                    for point in pin_positions(component, position, terminal)
                )
                peers = {
                    peer_ref
                    for port_ref in net.get("ports", ())
                    if isinstance(port_ref, str)
                    if (peer_ref := port_component_ref(port_ref, component_refs)) is not None
                    if peer_ref != component_ref
                }
                terminal_gaps: list[float] = []
                for peer_ref in peers:
                    peer_group = component_groups.get(peer_ref)
                    peer = instances.get(peer_ref)
                    if not peer_group or len(peer_group) != 1 or not isinstance(peer, dict):
                        continue
                    _, peer_position = peer_group[0]
                    prefix = peer_ref + "."
                    peer_terminals = {
                        port_ref.removeprefix(prefix)
                        for port_ref in net.get("ports", ())
                        if isinstance(port_ref, str) and port_ref.startswith(prefix)
                    }
                    peer_points = tuple(
                        point
                        for peer_terminal in peer_terminals
                        if "." not in peer_terminal
                        for point in pin_positions(peer, peer_position, peer_terminal)
                    )
                    terminal_gaps.extend(
                        hypot(own.x - other.x, own.y - other.y)
                        for own in own_points
                        for other in peer_points
                    )
                connection_gaps.extend(terminal_gaps)
                if (
                    terminal_gaps
                    and not is_rail_net(net_ref, net)
                    and not (
                        is_named_interface_net(net)
                        and len(net_symbol_groups.get(net_ref, [])) >= 2
                    )
                    and min(terminal_gaps) > maximum_connected_component_gap
                ):
                    findings.append(
                        QualityFinding(
                            code="detached-local-signal",
                            module_ref=module_ref,
                            symbol_ids=(symbol_id, f"net:{net_ref}"),
                            measured=min(terminal_gaps),
                            threshold=maximum_connected_component_gap,
                            message=("non-rail terminal has no nearby component peer on its net"),
                        )
                    )
            if connection_gaps and min(connection_gaps) > maximum_connected_component_gap:
                findings.append(
                    QualityFinding(
                        code="detached-connected-component",
                        module_ref=module_ref,
                        symbol_ids=(symbol_id,),
                        measured=min(connection_gaps),
                        threshold=maximum_connected_component_gap,
                        message="connected component has no nearby visual peer",
                    )
                )

        for capacitor_ref, capacitor_group in sorted(component_groups.items()):
            capacitor = instances.get(capacitor_ref)
            if (
                not isinstance(capacitor, dict)
                or capacitor.get("kind") != "Component"
                or attribute_string(capacitor, "type") != "capacitor"
                or len(capacitor_group) != 1
            ):
                continue
            terminals = terminal_net_map(capacitor_ref, capacitor, nets)
            if len(terminals) != 2:
                continue
            return_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if is_return_net(net_ref, net)
            ]
            supply_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if is_rail_net(net_ref, net) and not is_return_net(net_ref, net)
            ]
            if len(return_entries) != 1 or len(supply_entries) != 1:
                continue

            capacitor_bounds = component_group_bounds(capacitor, capacitor_group)
            capacitor_nets = {return_entries[0][0], supply_entries[0][0]}
            owner_gaps: list[float] = []
            for owner_ref, owner_group in component_groups.items():
                if owner_ref == capacitor_ref:
                    continue
                owner = instances.get(owner_ref)
                if not isinstance(owner, dict) or owner.get("kind") != "Component":
                    continue
                if (attribute_string(owner, "type") or "").casefold() in NON_OWNER_TYPES:
                    continue
                owner_nets = {
                    net_ref for net_ref, _ in terminal_net_map(owner_ref, owner, nets).values()
                }
                if not capacitor_nets <= owner_nets:
                    continue
                owner_bounds = component_group_bounds(owner, owner_group)
                delta_x = max(
                    owner_bounds.min_x - capacitor_bounds.max_x,
                    capacitor_bounds.min_x - owner_bounds.max_x,
                    0.0,
                )
                delta_y = max(
                    owner_bounds.min_y - capacitor_bounds.max_y,
                    capacitor_bounds.min_y - owner_bounds.max_y,
                    0.0,
                )
                owner_gaps.append(hypot(delta_x, delta_y))

            if not owner_gaps:
                continue
            owner_gap = min(owner_gaps)
            if owner_gap > maximum_local_passive_gap:
                findings.append(
                    QualityFinding(
                        code="local-passive-owner-gap",
                        module_ref=module_ref,
                        symbol_ids=(capacitor_group[0][0],),
                        measured=owner_gap,
                        threshold=maximum_local_passive_gap,
                        message="local rail passive is visually detached from its owner",
                    )
                )

        series: list[tuple[str, Point]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            symbol_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if two_terminal_axis(component, component_position, terminals) != "x":
                continue
            if any(is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue
            terminal_points = [
                pin_position(component, component_position, terminal) for terminal in terminals
            ]
            terminal_center = Point(
                x=float(median(point.x for point in terminal_points)),
                y=float(median(point.y for point in terminal_points)),
            )
            series.append((symbol_id, terminal_center))

            first_anchor = single_neighbour_pin_anchor(
                terminal_nets[terminals[0]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            second_anchor = single_neighbour_pin_anchor(
                terminal_nets[terminals[1]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            if first_anchor is None or second_anchor is None:
                continue
            if opposing_anchor_axis(first_anchor, second_anchor, terminal_center) != "x":
                continue
            dogleg = max(
                abs(terminal_points[0].y - first_anchor.y),
                abs(terminal_points[1].y - second_anchor.y),
            )
            if dogleg > maximum_series_dogleg:
                findings.append(
                    QualityFinding(
                        code="series-terminal-dogleg",
                        module_ref=module_ref,
                        symbol_ids=(symbol_id,),
                        measured=dogleg,
                        threshold=maximum_series_dogleg,
                        message="simple series terminals exceed the vertical dogleg allowance",
                    )
                )

        lanes: list[list[tuple[str, Point]]] = []
        for candidate in sorted(series, key=lambda item: (item[1].x, item[1].y, item[0])):
            matching_lane = next(
                (
                    lane
                    for lane in lanes
                    if abs(candidate[1].x - median(item[1].x for item in lane)) <= lane_tolerance
                ),
                None,
            )
            if matching_lane is None:
                lanes.append([candidate])
            else:
                matching_lane.append(candidate)
        for lane in lanes:
            ordered = sorted(lane, key=lambda item: (item[1].y, item[0]))
            for first, second in zip(ordered, ordered[1:], strict=False):
                pitch = second[1].y - first[1].y
                if pitch > bank_group_span or pitch + 1e-6 >= minimum_bank_pitch:
                    continue
                findings.append(
                    QualityFinding(
                        code="series-bank-pitch",
                        module_ref=module_ref,
                        symbol_ids=(first[0], second[0]),
                        measured=pitch,
                        threshold=minimum_bank_pitch,
                        message="repeated series members are too close for annotation clearance",
                    )
                )

    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.module_ref,
                finding.code,
                finding.symbol_ids,
            ),
        )
    )
