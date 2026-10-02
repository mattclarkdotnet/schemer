from __future__ import annotations

from dataclasses import replace

from schemer.analysis.connections import (
    is_rail_net,
    is_return_net,
)
from schemer.analysis.drawing_model import Envelope
from schemer.analysis.measurements import with_annotation_envelope
from schemer.core.layout import Position
from schemer.placement.blocks.model import Rect
from schemer.placement.circuits.model import Component, NetSymbolAttachment
from schemer.placement.circuits.net_symbols import (
    rail_drawing_envelope,
    terminal_net_symbol_attachment,
)
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    passive_orientation,
    pin_side_near_bounds,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    LOCAL_GAP,
    LOCAL_RAIL_STUB,
    NET_SYMBOL_STUB,
    PIN_CLUSTER_GAP,
)
from schemer.placement.circuits.queries import pin_clusters
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
)
from schemer.symbols.library import symbol_pin_electrical_types
from schemer.symbols.model import Point


def place_bypasses(
    components: tuple[Component, ...],
    central: Component,
    central_position: Position,
    positions: dict[str, Position],
) -> tuple[set[str], set[str], tuple[NetSymbolAttachment, ...]]:
    used: set[str] = set()
    owner_terminals_used: set[str] = set()
    attachments: list[NetSymbolAttachment] = []
    central_bounds = placed_symbol_bounds(central.instance, central_position)
    central_nets = {net_ref for net_ref, _ in central.terminals.values()}
    for passive in components:
        if passive.component_type != "capacitor" or len(passive.terminals) != 2:
            continue
        supply = [
            (terminal, net_ref, net)
            for terminal, (net_ref, net) in passive.terminals.items()
            if is_rail_net(net_ref, net) and not is_return_net(net_ref, net)
        ]
        returns = [
            (terminal, net_ref, net)
            for terminal, (net_ref, net) in passive.terminals.items()
            if is_return_net(net_ref, net)
        ]
        if len(supply) != 1 or len(returns) != 1 or {supply[0][1], returns[0][1]} - central_nets:
            continue
        electrical_types = symbol_pin_electrical_types(central.instance)
        owner_terminals = [
            terminal
            for terminal, (net_ref, _) in central.terminals.items()
            if net_ref == supply[0][1] and electrical_types.get(terminal) == "power_in"
        ]
        if not owner_terminals:
            owner_terminals = [
                terminal
                for terminal, (net_ref, _) in central.terminals.items()
                if net_ref == supply[0][1]
            ]
        owner_points = tuple(
            point
            for terminal, (net_ref, _) in central.terminals.items()
            if terminal in owner_terminals and net_ref == supply[0][1]
            for point in pin_positions(central.instance, central_position, terminal)
        )
        owner_point = median_point(owner_points)
        owner_side = pin_side_near_bounds(owner_point, central_bounds)
        side = "left" if owner_side == "left" else "right"
        clustered_owner_terminals = set(owner_terminals)
        # A rail can serve separated banks on the same face. Put its local
        # bypass at the bottom bank (ground leaves downward), and terminate
        # the other bank independently instead of drawing a trunk across
        # intervening signal pins. Never average across those banks.
        same_face = {
            terminal: median_point(pin_positions(central.instance, central_position, terminal))
            for terminal, (net_ref, _) in central.terminals.items()
            if net_ref == supply[0][1]
            and pin_side_near_bounds(median_point(pin_positions(
                central.instance, central_position, terminal,
            )), central_bounds) == owner_side
        }
        face_clusters = pin_clusters(tuple(same_face.values()), side)
        separate_banks = owner_side in {"left", "right"} and len(face_clusters) > 1
        if separate_banks:
            cluster = face_clusters[-1]
            owner_point = max(cluster, key=lambda point: point.y)
            clustered_owner_terminals = {
                terminal for terminal, point in same_face.items() if point in cluster
            }
        owner_along = owner_point.y if side in {"left", "right"} else owner_point.x
        for terminal, (net_ref, _) in central.terminals.items():
            if net_ref != supply[0][1]:
                continue
            terminal_point = median_point(
                pin_positions(central.instance, central_position, terminal)
            )
            if pin_side_near_bounds(terminal_point, central_bounds) != owner_side:
                continue
            terminal_along = terminal_point.y if side in {"left", "right"} else terminal_point.x
            if abs(terminal_along - owner_along) <= PIN_CLUSTER_GAP:
                clustered_owner_terminals.add(terminal)
        if side == "left":
            base = passive_orientation(passive, returns[0][0], supply[0][0])
            target_x = central_bounds.min_x - LOCAL_GAP
        else:
            base = passive_orientation(passive, supply[0][0], returns[0][0])
            target_x = central_bounds.max_x + LOCAL_GAP
        supply_pin = median_point(pin_positions(passive.instance, base, supply[0][0]))
        placed = translate_pin_to(
            base,
            supply_pin,
            Point(target_x, owner_point.y),
        )
        # A bypass is a complete local branch, not just a capacitor body.
        # Move the whole branch outward if its return glyph/caption would
        # encroach on a neighbouring named signal endpoint on this face.
        return_attachment = terminal_net_symbol_attachment(
            passive, placed, returns[0][0], clearance=LOCAL_RAIL_STUB,
        )
        branch_return = rail_drawing_envelope(
            return_attachment.net, return_attachment.position(),
        )
        delta_x = 0.0
        for terminal, (net_ref, net) in central.terminals.items():
            if is_rail_net(net_ref, net) or net.get("kind") == "NotConnected":
                continue
            for point in pin_positions(central.instance, central_position, terminal):
                if pin_side_near_bounds(point, central_bounds) != side:
                    continue
                endpoint_x = point.x + (-1 if side == "left" else 1) * NET_SYMBOL_STUB
                endpoint = with_annotation_envelope(
                    Envelope(endpoint_x, point.y, endpoint_x, point.y), (terminal,),
                )
                if branch_return.min_y < endpoint.max_y and endpoint.min_y < branch_return.max_y:
                    if side == "right":
                        delta_x = max(delta_x, endpoint.max_x + LOCAL_GAP - branch_return.min_x)
                    else:
                        delta_x = min(delta_x, endpoint.min_x - LOCAL_GAP - branch_return.max_x)
        placed = replace(placed, x=placed.x + delta_x)
        if separate_banks:
            # Reserve an outer branch lane beyond the short signal stubs.
            # Its local supply tee must not sit on a signal endpoint row.
            current_pin = median_point(pin_positions(passive.instance, placed, supply[0][0]))
            direction = -1 if side == "left" else 1
            reach = NET_SYMBOL_STUB + 3 * LOCAL_RAIL_STUB
            target_x = direction * max(direction * current_pin.x,
                                       direction * owner_point.x + reach)
            placed = replace(placed, x=placed.x + target_x - current_pin.x)
            attachments.append(NetSymbolAttachment(
                supply[0][1], supply[0][2],
                Point(target_x - direction * LOCAL_RAIL_STUB,
                      owner_point.y - LOCAL_RAIL_STUB),
                outward_side=side,
            ))
        positions[passive.symbol_id] = placed
        attachments.append(
            terminal_net_symbol_attachment(
                passive,
                placed,
                returns[0][0],
                clearance=LOCAL_RAIL_STUB,
            )
        )
        used.add(passive.ref)
        owner_terminals_used.update(clustered_owner_terminals)
    return used, owner_terminals_used, tuple(attachments)


def local_bypasses(components: tuple[Component, ...]) -> tuple[Component, ...]:
    result: list[Component] = []
    for component in components:
        if component.component_type != "capacitor" or len(component.terminals) != 2:
            continue
        rails = tuple(
            (net_ref, net)
            for net_ref, net in component.terminals.values()
            if is_rail_net(net_ref, net)
        )
        if len(rails) == 2 and sum(is_return_net(net_ref, net) for net_ref, net in rails) == 1:
            result.append(component)
    return tuple(sorted(result, key=lambda component: component.ref))


def assign_local_bypasses(
    active: tuple[Component, ...],
    bypasses: tuple[Component, ...],
) -> dict[str, Component] | None:
    """Match indistinguishable rail shunts to compatible consumers stably.

    Connectivity cannot distinguish two equal decouplers on the same rails.
    Within each rail pair, source-stable component order is therefore used as
    the only tie-break. The rule applies only when consumers and capacitors
    have equal cardinality, so no passive is silently left unowned.
    """

    by_rails: dict[frozenset[str], list[Component]] = {}
    for bypass in bypasses:
        by_rails.setdefault(
            frozenset(net_ref for net_ref, _ in bypass.terminals.values()), []
        ).append(bypass)

    assigned: dict[str, Component] = {}
    claimed_active: set[str] = set()
    for rail_pair, candidates in sorted(by_rails.items(), key=lambda item: tuple(sorted(item[0]))):
        consumers = sorted(
            (
                component
                for component in active
                if rail_pair <= {net_ref for net_ref, _ in component.terminals.values()}
                and component.ref not in claimed_active
            ),
            key=lambda component: component.ref,
        )
        candidates.sort(key=lambda component: component.ref)
        if len(consumers) != len(candidates):
            return None
        for consumer, bypass in zip(consumers, candidates, strict=True):
            assigned[consumer.ref] = bypass
            claimed_active.add(consumer.ref)
    if set(assigned) != {component.ref for component in active}:
        return None
    return assigned


def component_envelopes(
    components: tuple[Component, ...],
    positions: dict[str, Position],
) -> dict[str, Rect]:
    occupied: dict[str, Rect] = {}
    for component in components:
        bounds = placed_symbol_body_bounds(component.instance, positions[component.symbol_id])
        occupied[component.symbol_id] = Rect(
            bounds.min_x,
            bounds.min_y,
            bounds.max_x - bounds.min_x,
            bounds.max_y - bounds.min_y,
        )
    return occupied


def place_local_bypass(
    owner: Component,
    owner_position: Position,
    bypass: Component,
    positions: dict[str, Position],
) -> tuple[NetSymbolAttachment, ...]:
    supply = next(
        terminal
        for terminal, (net_ref, net) in bypass.terminals.items()
        if is_rail_net(net_ref, net) and not is_return_net(net_ref, net)
    )
    return_terminal = next(
        terminal
        for terminal, (net_ref, net) in bypass.terminals.items()
        if is_return_net(net_ref, net)
    )
    base = oriented_terminal_vector(
        bypass,
        supply,
        return_terminal,
        axis="y",
        direction=1.0,
    )
    supply_net_ref = bypass.terminals[supply][0]
    owner_supply_points = tuple(
        point
        for terminal, (net_ref, _) in owner.terminals.items()
        if net_ref == supply_net_ref
        for point in pin_positions(owner.instance, owner_position, terminal)
    )
    owner_supply = median_point(owner_supply_points)
    owner_bounds = placed_symbol_bounds(owner.instance, owner_position)
    supply_pin = median_point(pin_positions(bypass.instance, base, supply))
    placed = translate_pin_to(
        base,
        supply_pin,
        Point(owner_bounds.max_x + LOCAL_RAIL_STUB, owner_supply.y - LOCAL_RAIL_STUB),
    )
    positions[bypass.symbol_id] = placed
    return (
        terminal_net_symbol_attachment(
            bypass, placed, return_terminal, side="bottom", clearance=LOCAL_RAIL_STUB
        ),
    )
