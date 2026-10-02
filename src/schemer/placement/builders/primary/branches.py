from __future__ import annotations

from dataclasses import replace
from statistics import median

from schemer.analysis.connections import is_return_net
from schemer.core.errors import ToolchainError
from schemer.placement.builders.primary.model import PrimaryPlacement
from schemer.placement.circuits.model import Component, NetSymbolAttachment
from schemer.placement.circuits.net_symbols import (
    net_symbol_attachment,
    terminal_net_symbol_attachment,
)
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    pin_side_near_bounds,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    BRANCH_STUB,
    DEVICE_STUB,
    LOCAL_BRANCH_SPAN,
    LOCAL_GAP,
    LOCAL_RAIL_STUB,
    NET_SYMBOL_STUB,
    SERIES_ANNOTATION_PITCH,
    SERIES_LANE_OFFSET,
)
from schemer.placement.circuits.queries import (
    component_pin_for_net,
    is_boundary_net,
    net_components,
)
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point


def place_primary_bias(state: PrimaryPlacement) -> None:
    bias_groups: dict[tuple[str, str], list[tuple[Component, str, str, Point]]] = {}
    for component_ref, role in state.recipe.primary_bias_roles.items():
        component, signal_terminal, rail_terminal, _, rail_net_ref = state.recipe.shunts_by_ref[
            component_ref
        ]
        owner_pin = median_point(
            pin_positions(state.recipe.primary.instance, state.primary_position, role.pin or "")
        )
        side = pin_side_near_bounds(owner_pin, state.primary_bounds)
        if side not in {"left", "right"}:
            raise ToolchainError(f"{component_ref}: pullup owner pin must be on a side face")
        bias_groups.setdefault((side, rail_net_ref), []).append(
            (component, signal_terminal, rail_terminal, owner_pin)
        )
    for (side, rail_net_ref), entries in sorted(bias_groups.items()):
        outward = -1.0 if side == "left" else 1.0
        rail_points = []
        rail_net = entries[0][0].terminals[entries[0][2]][1]
        subordinate_net_refs = {
            net_ref for net_ref, _, _ in state.recipe.subordinate_links.values()
        }
        for component, signal_terminal, rail_terminal, owner_pin in sorted(
            entries, key=lambda entry: (entry[3].y, entry[0].ref)
        ):
            signal_net_ref = component.terminals[signal_terminal][0]
            if signal_net_ref in subordinate_net_refs:
                # This owner pin continues directly to a subordinate active
                # device. Keep that signal corridor straight and hang the
                # pull-up vertically from its midpoint.
                base = oriented_terminal_vector(
                    component,
                    signal_terminal,
                    rail_terminal,
                    axis="y",
                    direction=-1.0,
                )
                target = Point(
                    owner_pin.x + outward * (DEVICE_STUB / 2),
                    owner_pin.y,
                )
            else:
                base = oriented_terminal_vector(
                    component,
                    rail_terminal,
                    signal_terminal,
                    axis="x",
                    direction=-outward,
                )
                target = Point(
                    owner_pin.x + outward * DEVICE_STUB,
                    owner_pin.y,
                )
            signal_pin = median_point(
                pin_positions(component.instance, base, signal_terminal)
            )
            placed = translate_pin_to(
                base,
                signal_pin,
                target,
            )
            state.positions[component.symbol_id] = placed
            rail_points.extend(pin_positions(component.instance, placed, rail_terminal))
        bus_x = float(median(point.x for point in rail_points))
        state.attachments.append(NetSymbolAttachment(
            rail_net_ref,
            rail_net,
            Point(
                bus_x,
                (
                    max(point.y for point in rail_points) + NET_SYMBOL_STUB
                    if is_return_net(rail_net_ref, rail_net)
                    else min(point.y for point in rail_points) - NET_SYMBOL_STUB
                ),
            ),
            rotation=180.0 if is_return_net(rail_net_ref, rail_net) else 0.0,
            outward_side=(
                "bottom" if is_return_net(rail_net_ref, rail_net) else "top"
            ),
        ))


def place_primary_series(state: PrimaryPlacement) -> bool:
    series_lanes: dict[str, int] = {}
    for side in ("left", "right"):
        candidates = []
        for component, primary_terminal, _, _ in state.recipe.series:
            primary_pin = median_point(
                pin_positions(
                    state.recipe.primary.instance, state.primary_position, primary_terminal
                )
            )
            if pin_side_near_bounds(primary_pin, state.primary_bounds) == side:
                candidates.append((primary_pin.y, component.ref))
        lane_ys: list[float] = []
        for pin_y, component_ref in sorted(candidates):
            lane = next(
                (
                    lane_index
                    for lane_index, previous_y in enumerate(lane_ys)
                    if pin_y - previous_y >= SERIES_ANNOTATION_PITCH
                ),
                len(lane_ys),
            )
            if lane == len(lane_ys):
                lane_ys.append(pin_y)
            else:
                lane_ys[lane] = pin_y
            series_lanes[component_ref] = lane

    for component, primary_terminal, attached_terminal, remote_terminal in (
        *state.recipe.series,
        *state.recipe.rail_feeds,
    ):
        primary_pin = median_point(
            pin_positions(state.recipe.primary.instance, state.primary_position, primary_terminal)
        )
        side = pin_side_near_bounds(primary_pin, state.primary_bounds)
        is_rail_feed = any(component.ref == item[0].ref for item in state.recipe.rail_feeds)
        power_feed = component.ref in state.recipe.power_feed_roles
        if side in {"left", "right"}:
            axis = "x"
            direction = -1.0 if side == "left" else 1.0
            target = Point(
                primary_pin.x
                + direction
                * (
                    DEVICE_STUB
                    if power_feed
                    else (
                        LOCAL_BRANCH_SPAN if is_rail_feed else DEVICE_STUB
                    )
                    + series_lanes.get(component.ref, 0) * SERIES_LANE_OFFSET
                ),
                primary_pin.y,
            )
        elif side in {"top", "bottom"} and is_rail_feed:
            axis = "y"
            direction = -1.0 if side == "top" else 1.0
            target = Point(
                primary_pin.x,
                primary_pin.y
                + direction * (DEVICE_STUB if power_feed else LOCAL_BRANCH_SPAN),
            )
        else:
            return False
        base = oriented_terminal_vector(
            component,
            attached_terminal,
            remote_terminal,
            axis=axis,
            direction=direction,
        )
        attached_pin = median_point(pin_positions(component.instance, base, attached_terminal))
        placed = translate_pin_to(
            base,
            attached_pin,
            target,
        )
        state.positions[component.symbol_id] = placed
        state.attachments.append(
            terminal_net_symbol_attachment(
                component,
                placed,
                remote_terminal,
                side=side,
            )
        )
        remote_net_ref = component.terminals[remote_terminal][0]
        state.labelled_nets.add(remote_net_ref)
        if is_rail_feed:
            state.skipped_primary_rails.add(primary_terminal)
            local_net_ref, local_net = component.terminals[attached_terminal]
            if net_components(local_net, state.recipe.component_refs) - {
                component.ref,
                state.recipe.primary.ref,
            }:
                # One local supply termination on the visible feed/consumer
                # chain, another at each remote consumer; do not force their
                # support wiring to join across the primary's pin field.
                feed_pin = median_point(
                    pin_positions(component.instance, placed, attached_terminal)
                )
                midpoint = (
                    Point((primary_pin.x + feed_pin.x) / 2, primary_pin.y)
                    if axis == "x"
                    else Point(primary_pin.x, (primary_pin.y + feed_pin.y) / 2)
                )
                state.attachments.append(net_symbol_attachment(
                    local_net_ref, local_net,
                    (midpoint,),
                    "bottom" if is_return_net(local_net_ref, local_net) else "top",
                    clearance=LOCAL_RAIL_STUB,
                ))


    return True


def place_primary_shunts(state: PrimaryPlacement) -> bool:
    for component, signal_terminal, rail_terminal, signal_net_ref, _ in state.recipe.shunts:
        if component.ref in state.recipe.primary_bias_roles:
            continue
        signal_net = component.terminals[signal_terminal][1]
        owner = state.placed_active.get(state.recipe.authored_owner_refs[component.ref])
        if owner is None:
            return False
        owner_component, owner_position = owner
        pin = component_pin_for_net(owner_component, owner_position, signal_net_ref)
        if pin is None:
            return False
        owner_pin = pin[1]
        direction_y = 1.0 if is_return_net(*component.terminals[rail_terminal]) else -1.0
        base = oriented_terminal_vector(
            component,
            signal_terminal,
            rail_terminal,
            axis="y",
            direction=direction_y,
        )
        signal_pin = median_point(pin_positions(component.instance, base, signal_terminal))
        owner_bounds = placed_symbol_bounds(owner_component.instance, owner_position)
        owner_side = pin_side_near_bounds(owner_pin, owner_bounds)
        if owner_side in {"left", "right"}:
            direction_x = -1.0 if owner_side == "left" else 1.0
            target = Point(owner_pin.x + direction_x * BRANCH_STUB, owner_pin.y)
            label_origin = target
            target = Point(target.x, target.y + direction_y * LOCAL_RAIL_STUB)
        else:
            target = Point(owner_pin.x, owner_pin.y + direction_y * BRANCH_STUB)
            label_origin = target
        placed = translate_pin_to(base, signal_pin, target)
        branch_bounds = placed_symbol_body_bounds(component.instance, placed)
        if owner_side == "left" and branch_bounds.max_x > owner_bounds.min_x - LOCAL_GAP:
            placed = replace(
                placed,
                x=placed.x + owner_bounds.min_x - LOCAL_GAP - branch_bounds.max_x,
            )
        elif owner_side == "right" and branch_bounds.min_x < owner_bounds.max_x + LOCAL_GAP:
            placed = replace(
                placed,
                x=placed.x + owner_bounds.max_x + LOCAL_GAP - branch_bounds.min_x,
            )
        elif owner_side == "top" and branch_bounds.max_y > owner_bounds.min_y - LOCAL_GAP:
            placed = replace(
                placed,
                y=placed.y + owner_bounds.min_y - LOCAL_GAP - branch_bounds.max_y,
            )
        elif owner_side == "bottom" and branch_bounds.min_y < owner_bounds.max_y + LOCAL_GAP:
            placed = replace(
                placed,
                y=placed.y + owner_bounds.max_y + LOCAL_GAP - branch_bounds.min_y,
            )
        state.positions[component.symbol_id] = placed
        rail_side = "bottom" if direction_y > 0 else "top"
        state.attachments.append(
            terminal_net_symbol_attachment(
                component,
                placed,
                rail_terminal,
                side=rail_side,
                clearance=LOCAL_RAIL_STUB,
            )
        )
        if signal_net_ref not in state.labelled_nets and is_boundary_net(
            signal_net_ref, signal_net, state.recipe.boundary_net_names
        ):
            direction_x = -1.0 if label_origin.x < state.primary_bounds.center_x else 1.0
            state.attachments.append(
                NetSymbolAttachment(
                    signal_net_ref,
                    signal_net,
                    Point(
                        label_origin.x + direction_x * NET_SYMBOL_STUB,
                        label_origin.y,
                    ),
                )
            )
            state.labelled_nets.add(signal_net_ref)


    return True
