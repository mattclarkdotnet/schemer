from __future__ import annotations

from schemer.analysis.connections import is_return_net
from schemer.analysis.drawing_model import Envelope
from schemer.analysis.measurements import with_annotation_envelope
from schemer.core.attributes import attribute_string
from schemer.placement.builders.primary.model import PrimaryPlacement
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    anchor_net_symbol_attachments,
    attach_net_symbols,
    clear_rail_signal_lanes,
    net_symbol_drawing_envelope,
    shunt_drawing_envelope,
    single_ended_net_symbol_attachments,
)
from schemer.placement.circuits.orientation import (
    active_orientation_toward,
    median_point,
    oriented_terminal_vector,
    pin_side_near_bounds,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    BRANCH_STUB,
    DEVICE_STUB,
    LOCAL_RAIL_STUB,
    PIN_EXIT_STUB,
)
from schemer.placement.circuits.queries import component_pin_for_net
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point


def prepare_primary_clearance(state: PrimaryPlacement) -> list[Envelope]:
    # Complete the primary's own rail terminations before measuring space for
    # a neighbouring active circuit. These are not subordinate-owned symbols.
    primary_attachments = list(anchor_net_symbol_attachments(
        state.recipe.primary, state.primary_position, skipped_terminals=state.skipped_primary_rails,
    ))
    signal_lanes = []
    for component, terminal, _, remote_terminal in state.recipe.series:
        pin = median_point(
            pin_positions(state.recipe.primary.instance, state.primary_position, terminal)
        )
        bounds = placed_symbol_bounds(component.instance, state.positions[component.symbol_id])
        side = pin_side_near_bounds(pin, state.primary_bounds)
        remote = median_point(pin_positions(
            component.instance, state.positions[component.symbol_id], remote_terminal,
        ))
        net_ref, net = component.terminals[remote_terminal]
        caption = str(net.get("name", net_ref)).rsplit(".", 1)[-1]
        caption_width = with_annotation_envelope(Envelope(0, 0, 0, 0), (caption,)).width
        outer = remote.x + (-caption_width if side == "left" else caption_width)
        signal_lanes.append((side, Envelope(
            min(pin.x, bounds.min_x, outer), min(pin.y, bounds.min_y) - PIN_EXIT_STUB,
            max(pin.x, bounds.max_x, outer), max(pin.y, bounds.max_y) + PIN_EXIT_STUB,
        )))
    for attachment in single_ended_net_symbol_attachments(
        state.recipe.primary, state.primary_position, state.recipe.component_refs,
    ):
        if attachment.net_ref in state.labelled_nets:
            continue
        if attachment.outward_side not in {"left", "right"}:
            continue
        state.attachments.append(attachment)
        state.labelled_nets.add(attachment.net_ref)
        signal_lanes.append((
            attachment.outward_side,
            net_symbol_drawing_envelope(attachment),
        ))
    state.attachments.extend(clear_rail_signal_lanes(primary_attachments, signal_lanes))
    preview_symbols = NetSymbols()
    attach_net_symbols({}, tuple(state.attachments), symbols=preview_symbols)
    primary_obstacles = list(preview_symbols.rail_envelopes)
    # Primary-only bias branches occupy real space before a neighbour is
    # placed, including when their return nets alternate supply and ground.
    for branch, signal_terminal, rail_terminal, signal_net_ref, _ in state.recipe.shunts:
        if branch.ref in state.recipe.primary_bias_roles:
            continue
        if state.recipe.authored_owner_refs[branch.ref] != state.recipe.primary.ref:
            continue
        pin = component_pin_for_net(state.recipe.primary, state.primary_position, signal_net_ref)
        if pin is None:
            continue
        owner_pin = pin[1]
        side = pin_side_near_bounds(owner_pin, state.primary_bounds)
        direction_y = 1.0 if is_return_net(*branch.terminals[rail_terminal]) else -1.0
        base = oriented_terminal_vector(
            branch, signal_terminal, rail_terminal, axis="y", direction=direction_y,
        )
        target = (
            Point(owner_pin.x + (-1 if side == "left" else 1) * BRANCH_STUB,
                  owner_pin.y + direction_y * LOCAL_RAIL_STUB)
            if side in {"left", "right"}
            else Point(owner_pin.x, owner_pin.y + direction_y * BRANCH_STUB)
        )
        placed_branch = translate_pin_to(
            base, median_point(pin_positions(branch.instance, base, signal_terminal)), target,
        )
        primary_obstacles.append(shunt_drawing_envelope(branch, placed_branch, rail_terminal))
    for local_component in state.recipe.components:
        if (
            local_component == state.recipe.primary
            or local_component.symbol_id not in state.positions
        ):
            continue
        bounds = placed_symbol_bounds(
            local_component.instance, state.positions[local_component.symbol_id],
        )
        primary_obstacles.append(with_annotation_envelope(
            Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
            (str(local_component.instance.get("reference_designator", "")),
             attribute_string(local_component.instance, "value") or ""),
        ))

    return primary_obstacles


def place_subordinate_devices(state: PrimaryPlacement, primary_obstacles: list[Envelope]) -> bool:
    state.placed_active = {
        state.recipe.primary.ref: (state.recipe.primary, state.primary_position)
    }
    for component in state.recipe.subordinate:
        net_ref, primary_terminal, subordinate_terminal = state.recipe.subordinate_links[
            component.ref
        ]
        primary_pin = median_point(
            pin_positions(state.recipe.primary.instance, state.primary_position, primary_terminal)
        )
        primary_side = pin_side_near_bounds(primary_pin, state.primary_bounds)
        if primary_side not in {"left", "right"}:
            return False
        direction = -1.0 if primary_side == "left" else 1.0
        base = active_orientation_toward(
            component,
            subordinate_terminal,
            outward_direction=direction,
        )
        component_pin = median_point(pin_positions(component.instance, base, subordinate_terminal))
        distance = DEVICE_STUB
        for branch, signal_terminal, rail_terminal, signal_net_ref, _ in state.recipe.shunts:
            if signal_net_ref != net_ref:
                continue
            if state.recipe.authored_owner_refs[branch.ref] != component.ref:
                continue
            direction_y = 1.0 if is_return_net(*branch.terminals[rail_terminal]) else -1.0
            branch_base = oriented_terminal_vector(
                branch, signal_terminal, rail_terminal, axis="y", direction=direction_y,
            )
            branch_pin = median_point(pin_positions(branch.instance, branch_base, signal_terminal))
            branch_position = translate_pin_to(branch_base, branch_pin, Point(
                0, primary_pin.y + direction_y * LOCAL_RAIL_STUB,
            ))
            envelope = shunt_drawing_envelope(branch, branch_position, rail_terminal)
            for obstacle in sorted(primary_obstacles, key=lambda item: item.min_x,
                                   reverse=direction < 0):
                if envelope.min_y >= obstacle.max_y or obstacle.min_y >= envelope.max_y:
                    continue
                current = envelope.translated(primary_pin.x + direction * distance / 2, 0)
                if (current.max_x + LOCAL_RAIL_STUB <= obstacle.min_x
                        or obstacle.max_x + LOCAL_RAIL_STUB <= current.min_x):
                    continue
                midpoint = (
                    obstacle.max_x + LOCAL_RAIL_STUB - envelope.min_x
                    if direction > 0
                    else obstacle.min_x - LOCAL_RAIL_STUB - envelope.max_x
                )
                distance = max(distance, 2 * direction * (midpoint - primary_pin.x))
        placed = translate_pin_to(
            base,
            component_pin,
            Point(primary_pin.x + direction * distance, primary_pin.y),
        )
        state.positions[component.symbol_id] = placed
        state.placed_active[component.ref] = (component, placed)
        state.labelled_nets.discard(net_ref)


    return True
