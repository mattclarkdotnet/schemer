from __future__ import annotations

from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    is_rail_net,
    is_return_net,
)
from schemer.analysis.roles import component_roles
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.circuits.model import Component, NetSymbolAttachment
from schemer.placement.circuits.net_symbols import terminal_net_symbol_attachment
from schemer.placement.circuits.orientation import (
    connector_net_point,
    median_point,
    oriented_terminal_vector,
    passive_orientation,
    pin_side_near_bounds,
    translate_pin_to,
)
from schemer.placement.circuits.policy import LOCAL_RAIL_STUB, PIN_EXIT_STUB
from schemer.placement.circuits.queries import net_components
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point


def place_shunts(
    instances: dict[str, Any],
    components: tuple[Component, ...],
    connectors: tuple[Component, ...],
    connector_positions: dict[str, Position],
    positions: dict[str, Position],
) -> tuple[set[str], tuple[NetSymbolAttachment, ...]]:
    component_refs = tuple(component.ref for component in components)
    pulldown_roles = {
        role.component_ref: role
        for role in component_roles(instances, components)
        if role.kind == "pulldown"
    }
    used: set[str] = set()
    attachments: list[NetSymbolAttachment] = []
    for connector in connectors:
        branches: list[tuple[Component, str, str, float]] = []
        connector_position = connector_positions[connector.ref]
        for passive in components:
            if passive.component_type not in NON_OWNER_TYPES or len(passive.terminals) != 2:
                continue
            signal = [
                (terminal, net_ref, net)
                for terminal, (net_ref, net) in passive.terminals.items()
                if not is_rail_net(net_ref, net)
            ]
            returns = [
                (terminal, net_ref, net)
                for terminal, (net_ref, net) in passive.terminals.items()
                if is_rail_net(net_ref, net)
            ]
            if len(signal) != 1 or len(returns) != 1:
                continue
            neighbours = net_components(signal[0][2], component_refs) - {passive.ref}
            if neighbours != {connector.ref}:
                continue
            source_y = connector_net_point(connector, connector_position, signal[0][1]).y
            branches.append((passive, signal[0][0], returns[0][0], source_y))
        if not branches:
            continue
        bounds = placed_symbol_bounds(connector.instance, connector_position)
        ordered = sorted(branches, key=lambda branch: (branch[3], branch[0].ref))
        source_sides = {
            passive.ref: pin_side_near_bounds(
                connector_net_point(
                    connector,
                    connector_position,
                    passive.terminals[signal_terminal][0],
                ),
                bounds,
            )
            for passive, signal_terminal, _, _ in ordered
        }
        return_groups: dict[tuple[str, str], list[tuple[Component, Position, str]]] = {}
        authored = [entry for entry in ordered if entry[0].ref in pulldown_roles]
        authored_row_y = (
            max(entry[3] for entry in authored) + 100.0 if authored else 0.0
        )
        authored_index = {entry[0].ref: index for index, entry in enumerate(authored)}
        connector_designator = str(connector.instance.get("reference_designator", ""))
        for passive, signal_terminal, return_terminal, source_y in ordered:
            source_side = source_sides[passive.ref]
            role = pulldown_roles.get(passive.ref)
            if role is not None:
                if (
                    role.owner != connector_designator
                    or role.pin not in connector.terminals
                    or connector.terminals[role.pin][0]
                    != passive.terminals[signal_terminal][0]
                ):
                    raise ToolchainError(
                        f"{passive.ref}: pulldown role does not name its connector pin"
                    )
                outward = -1.0 if source_side == "left" else 1.0
                base = oriented_terminal_vector(
                    passive,
                    signal_terminal,
                    return_terminal,
                    axis="y",
                    direction=1,
                )
                target_x = (
                    bounds.min_x if source_side == "left" else bounds.max_x
                ) + outward * (160.0 + authored_index[passive.ref] * 100.0)
                target_y = authored_row_y
            elif source_side == "left":
                base = passive_orientation(passive, return_terminal, signal_terminal)
                target_x = bounds.min_x - LOCAL_RAIL_STUB
                target_y = source_y
            else:
                base = passive_orientation(passive, signal_terminal, return_terminal)
                target_x = bounds.max_x + LOCAL_RAIL_STUB
                target_y = source_y
            signal_pin = median_point(pin_positions(passive.instance, base, signal_terminal))
            target = Point(target_x, target_y)
            placed = translate_pin_to(base, signal_pin, target)
            positions[passive.symbol_id] = placed
            return_net_ref = passive.terminals[return_terminal][0]
            return_groups.setdefault((return_net_ref, source_side), []).append(
                (passive, placed, return_terminal)
            )
            used.add(passive.ref)
        for (_, source_side), group in sorted(return_groups.items()):
            if len(group) == 1:
                passive, placed, return_terminal = group[0]
                attachments.append(
                    terminal_net_symbol_attachment(
                        passive,
                        placed,
                        return_terminal,
                        clearance=LOCAL_RAIL_STUB,
                    )
                )
                continue
            return_points = tuple(
                median_point(pin_positions(passive.instance, placed, return_terminal))
                for passive, placed, return_terminal in group
            )
            trunk_x = (
                min(point.x for point in return_points) - LOCAL_RAIL_STUB
                if source_side == "left"
                else max(point.x for point in return_points) + LOCAL_RAIL_STUB
            )
            passive, _, return_terminal = group[0]
            net_ref, net = passive.terminals[return_terminal]
            target = Point(
                trunk_x,
                # End the bank just beyond its last branch. An unrelated
                # full-length net stub can run into the following pin bank.
                max(point.y for point in return_points) + PIN_EXIT_STUB
                if is_return_net(net_ref, net)
                else min(point.y for point in return_points) - PIN_EXIT_STUB,
            )
            attachments.append(NetSymbolAttachment(net_ref, net, target))
    return used, tuple(attachments)
