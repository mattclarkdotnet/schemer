from __future__ import annotations

from statistics import median

from schemer.analysis.drawing_model import Envelope
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.circuits.chains import chain_owner, passive_chains
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    PASSIVE_CHAIN_GAP,
    PASSIVE_CHAIN_OWNER_GAP,
)
from schemer.placement.circuits.queries import component_drawing_envelope, drawings_overlap
from schemer.symbols.geometry import pin_outward_side, pin_positions
from schemer.symbols.model import Point


def place_tapped_passive_chains(members, positions) -> set[str]:
    """Place each IC-tapped resistor path as one continuous vertical ladder."""

    placed_refs: set[str] = set()
    for chain in passive_chains(members):
        owner = chain_owner(chain, members)
        if owner is None:
            continue
        chain_refs = {entry[0].ref for entry in chain.entries}
        previous_sink: Point | None = None
        temporary: dict[str, Position] = {}
        for index, (component, source_terminal, sink_terminal) in enumerate(chain.entries):
            base = oriented_terminal_vector(
                component, source_terminal, sink_terminal, axis="y", direction=1,
            )
            source = median_point(pin_positions(component.instance, base, source_terminal))
            source_y = (
                0.0 if previous_sink is None else previous_sink.y + PASSIVE_CHAIN_GAP
            )
            placed = translate_pin_to(base, source, Point(0.0, source_y))
            temporary[component.symbol_id] = placed
            previous_sink = median_point(pin_positions(component.instance, placed, sink_terminal))

        chain_drawings = [
            component_drawing_envelope(component, temporary[component.symbol_id])
            for component, _, _ in chain.entries
        ]
        chain_bounds = Envelope(
            min(item.min_x for item in chain_drawings),
            min(item.min_y for item in chain_drawings),
            max(item.max_x for item in chain_drawings),
            max(item.max_y for item in chain_drawings),
        )
        owner_position = positions[owner.symbol_id]
        owner_bounds = component_drawing_envelope(owner, owner_position)
        side_counts = {"left": 0, "right": 0}
        for terminal, (net_ref, _) in owner.terminals.items():
            if net_ref not in chain.net_refs:
                continue
            try:
                side = pin_outward_side(owner.instance, owner_position, terminal)
            except ToolchainError:
                continue
            if side in side_counts:
                side_counts[side] += 1
        side = max(side_counts, key=lambda candidate: (side_counts[candidate], candidate == "left"))
        target_x = (
            owner_bounds.min_x - PASSIVE_CHAIN_OWNER_GAP - chain_bounds.max_x
            if side == "left"
            else owner_bounds.max_x + PASSIVE_CHAIN_OWNER_GAP - chain_bounds.min_x
        )
        tap_offsets = []
        for index, net_ref in enumerate(chain.net_refs[1:-1], start=1):
            owner_terminals = [
                terminal for terminal, (candidate, _) in owner.terminals.items()
                if candidate == net_ref
            ]
            if len(owner_terminals) != 1:
                continue
            upstream, _, upstream_terminal = chain.entries[index - 1]
            downstream, downstream_terminal, _ = chain.entries[index]
            upstream_point = median_point(pin_positions(
                upstream.instance, temporary[upstream.symbol_id], upstream_terminal,
            ))
            downstream_point = median_point(pin_positions(
                downstream.instance, temporary[downstream.symbol_id], downstream_terminal,
            ))
            junction_y = (upstream_point.y + downstream_point.y) / 2
            owner_y = median_point(pin_positions(
                owner.instance, owner_position, owner_terminals[0],
            )).y
            tap_offsets.append(owner_y - junction_y)
        target_y = (
            float(median(tap_offsets))
            if tap_offsets
            else owner_bounds.min_y + (
                (owner_bounds.max_y - owner_bounds.min_y)
                - (chain_bounds.max_y - chain_bounds.min_y)
            ) / 2 - chain_bounds.min_y
        )
        other_drawings = [
            component_drawing_envelope(component, positions[component.symbol_id])
            for component in members
            if component.ref not in chain_refs
        ]
        direction = -1 if side == "left" else 1
        for step in range(1000):
            dx = target_x + direction * step * PASSIVE_CHAIN_GAP
            moved = Envelope(
                chain_bounds.min_x + dx, chain_bounds.min_y + target_y,
                chain_bounds.max_x + dx, chain_bounds.max_y + target_y,
            )
            if not any(drawings_overlap(moved, other) for other in other_drawings):
                break
        else:
            raise ToolchainError("cannot clear tapped passive chain from its owner block")
        for symbol_id, position in temporary.items():
            positions[symbol_id] = Position(
                position.x + dx, position.y + target_y, position.rotation, position.mirror,
            )
        placed_refs.update(chain_refs)
    return placed_refs
