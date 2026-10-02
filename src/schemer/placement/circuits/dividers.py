from __future__ import annotations

from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.circuits.chains import authored_divider_chains
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    DIVIDER_COMPONENT_GAP,
    DIVIDER_OWNER_GAP,
    DIVIDER_TAP_STUB,
)
from schemer.placement.circuits.queries import component_drawing_envelope, drawings_overlap
from schemer.symbols.geometry import pin_outward_side, pin_positions
from schemer.symbols.model import Point


def place_authored_dividers(instances, members, positions) -> set[str]:
    """Fold owner-attached dividers around direct, horizontal IC tap wires."""

    placed_refs: set[str] = set()
    for chain, owner in authored_divider_chains(instances, members):
        owner_position = positions[owner.symbol_id]
        owner_bounds = component_drawing_envelope(owner, owner_position)
        taps = []
        for boundary, net_ref in enumerate(chain.net_refs[1:-1], start=1):
            terminals = [
                terminal for terminal, (candidate, _) in owner.terminals.items()
                if candidate == net_ref
            ]
            if len(terminals) != 1:
                continue
            terminal = terminals[0]
            side = pin_outward_side(owner.instance, owner_position, terminal)
            if side not in {"left", "right"}:
                raise ToolchainError(
                    f"{owner.ref}.{terminal}: divider tap must be on a side face"
                )
            taps.append((boundary, terminal, side, median_point(
                pin_positions(owner.instance, owner_position, terminal)
            )))
        if not taps:
            raise ToolchainError(f"{owner.ref}: divider has no owner-pin taps")
        if len({tap[2] for tap in taps}) != 1:
            raise ToolchainError(f"{owner.ref}: divider taps must share one IC face")
        if [tap[0] for tap in taps] != sorted(tap[0] for tap in taps):
            raise ToolchainError(f"{owner.ref}: divider taps do not follow chain order")
        tap_ys = [tap[3].y for tap in taps]
        if tap_ys != sorted(tap_ys) or len(set(tap_ys)) != len(tap_ys):
            raise ToolchainError(f"{owner.ref}: divider tap pins must follow chain order")

        side = taps[0][2]
        outward = -1.0 if side == "left" else 1.0
        backbone_x = (
            owner_bounds.min_x - DIVIDER_OWNER_GAP
            if side == "left"
            else owner_bounds.max_x + DIVIDER_OWNER_GAP
        )
        temporary: dict[str, Position] = {}

        first_boundary, _, _, first_tap = taps[0]
        cursor_y = first_tap.y
        for component, source_terminal, sink_terminal in reversed(
            chain.entries[:first_boundary]
        ):
            base = oriented_terminal_vector(
                component, source_terminal, sink_terminal, axis="y", direction=1,
            )
            sink = median_point(pin_positions(component.instance, base, sink_terminal))
            placed = translate_pin_to(base, sink, Point(backbone_x, cursor_y))
            temporary[component.symbol_id] = placed
            source = median_point(pin_positions(component.instance, placed, source_terminal))
            cursor_y = source.y - DIVIDER_COMPONENT_GAP

        boundaries = [tap[0] for tap in taps]
        for start, end, upper_y, lower_y in zip(
            boundaries, boundaries[1:], tap_ys, tap_ys[1:]
        ):
            segment = chain.entries[start:end]
            top_count = (len(segment) + 1) // 2
            top = segment[:top_count]
            bottom = segment[top_count:]
            cursor_x = backbone_x + outward * DIVIDER_TAP_STUB
            top_end: Point | None = None
            for component, source_terminal, sink_terminal in top:
                base = oriented_terminal_vector(
                    component, source_terminal, sink_terminal,
                    axis="x", direction=outward,
                )
                source = median_point(pin_positions(component.instance, base, source_terminal))
                placed = translate_pin_to(base, source, Point(cursor_x, upper_y))
                temporary[component.symbol_id] = placed
                top_end = median_point(
                    pin_positions(component.instance, placed, sink_terminal)
                )
                cursor_x = top_end.x + outward * DIVIDER_COMPONENT_GAP
            if bottom:
                if top_end is None:
                    raise ToolchainError("divider middle segment lost its upper endpoint")
                cursor_x = top_end.x
                for component, source_terminal, sink_terminal in bottom:
                    base = oriented_terminal_vector(
                        component, source_terminal, sink_terminal,
                        axis="x", direction=-outward,
                    )
                    source = median_point(
                        pin_positions(component.instance, base, source_terminal)
                    )
                    placed = translate_pin_to(base, source, Point(cursor_x, lower_y))
                    temporary[component.symbol_id] = placed
                    bottom_end = median_point(
                        pin_positions(component.instance, placed, sink_terminal)
                    )
                    cursor_x = bottom_end.x - outward * DIVIDER_COMPONENT_GAP

        last_boundary, _, _, last_tap = taps[-1]
        cursor_y = last_tap.y
        for component, source_terminal, sink_terminal in chain.entries[last_boundary:]:
            base = oriented_terminal_vector(
                component, source_terminal, sink_terminal, axis="y", direction=1,
            )
            source = median_point(pin_positions(component.instance, base, source_terminal))
            placed = translate_pin_to(base, source, Point(backbone_x, cursor_y))
            temporary[component.symbol_id] = placed
            sink = median_point(pin_positions(component.instance, placed, sink_terminal))
            cursor_y = sink.y + DIVIDER_COMPONENT_GAP

        if len(temporary) != len(chain.entries):
            raise ToolchainError(f"{owner.ref}: divider placement did not consume every member")
        chain_refs = {entry[0].ref for entry in chain.entries}
        drawings = [
            component_drawing_envelope(component, temporary[component.symbol_id])
            for component, _, _ in chain.entries
        ]
        # A semantic role does not lock coordinates. Move this whole network
        # outward until its completed envelope clears the other support.
        # Keeping every Y coordinate preserves straight functional tap wires.
        occupied = [component_drawing_envelope(other, positions[other.symbol_id])
                    for other in members if other.ref not in chain_refs]
        for step in range(1000):
            dx = outward * step * DIVIDER_COMPONENT_GAP
            if not any(drawings_overlap(drawing.translated(dx, 0), obstacle)
                       for drawing in drawings for obstacle in occupied):
                temporary = {key: Position(p.x + dx, p.y, p.rotation, p.mirror)
                             for key, p in temporary.items()}
                break
        else:
            raise ToolchainError(f"cannot clear authored divider for {owner.ref}")
        for symbol_id, position in temporary.items():
            positions[symbol_id] = position
        placed_refs.update(chain_refs)
    return placed_refs
