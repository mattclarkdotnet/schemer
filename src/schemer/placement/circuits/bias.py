from __future__ import annotations

from statistics import median

from schemer.analysis.connections import is_rail_net, is_return_net
from schemer.analysis.roles import component_roles
from schemer.core.errors import ToolchainError
from schemer.placement.circuits.model import NetSymbolAttachment
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    PULLDOWN_LANE_GAP,
    PULLDOWN_OUTWARD_STUB,
    PULLDOWN_VERTICAL_STUB,
    PULLUP_RAIL_STUB,
    PULLUP_STUB,
)
from schemer.symbols.geometry import pin_outward_side, pin_positions
from schemer.symbols.model import Point


def place_authored_pullup_banks(instances, members, positions):
    """Place source-declared pull-ups as uniform lanes on a shared supply bus."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind == "pullup"
    )
    if not roles:
        return set(), []
    by_ref = {component.ref: component for component in members}
    owners = {
        str(component.instance.get("reference_designator", "")): component
        for component in members
    }
    groups = {}
    for role in roles:
        component = by_ref[role.component_ref]
        owner = owners.get(role.owner or "")
        if owner is None or role.pin not in owner.terminals or len(component.terminals) != 2:
            raise ToolchainError(
                f"{component.ref}: pullup owner and pin must name a local two-terminal branch"
            )
        signal_ref = owner.terminals[role.pin][0]
        signal_terminals = [
            terminal for terminal, (net_ref, _) in component.terminals.items()
            if net_ref == signal_ref
        ]
        if len(signal_terminals) != 1:
            raise ToolchainError(
                f"{component.ref}: pullup signal does not connect to {role.owner}.{role.pin}"
            )
        signal_terminal = signal_terminals[0]
        rail_terminal = next(
            terminal for terminal in component.terminals if terminal != signal_terminal
        )
        rail_ref, rail_net = component.terminals[rail_terminal]
        if not is_rail_net(rail_ref, rail_net) or is_return_net(rail_ref, rail_net):
            raise ToolchainError(f"{component.ref}: pullup remote terminal must be a supply rail")
        owner_position = positions[owner.symbol_id]
        owner_pin = median_point(pin_positions(owner.instance, owner_position, role.pin))
        side = pin_outward_side(owner.instance, owner_position, role.pin)
        if side not in {"left", "right"}:
            raise ToolchainError(f"{component.ref}: pullup owner pin must be on a side face")
        groups.setdefault((owner.ref, side, rail_ref), []).append(
            (component, signal_terminal, rail_terminal, owner_pin, rail_net)
        )

    attachments = []
    placed_refs = set()
    for (_, side, rail_ref), entries in sorted(groups.items()):
        outward = -1.0 if side == "left" else 1.0
        rail_points = []
        for component, signal_terminal, rail_terminal, owner_pin, _ in sorted(
            entries, key=lambda entry: (entry[3].y, entry[0].ref)
        ):
            base = oriented_terminal_vector(
                component,
                rail_terminal,
                signal_terminal,
                axis="x",
                direction=-outward,
            )
            signal_pin = median_point(
                pin_positions(component.instance, base, signal_terminal)
            )
            placed = translate_pin_to(
                base,
                signal_pin,
                Point(owner_pin.x + outward * PULLUP_STUB, owner_pin.y),
            )
            positions[component.symbol_id] = placed
            rail_points.extend(pin_positions(component.instance, placed, rail_terminal))
            placed_refs.add(component.ref)
        attachments.append(NetSymbolAttachment(
            rail_ref,
            entries[0][4],
            Point(
                float(median(point.x for point in rail_points)),
                min(point.y for point in rail_points) - PULLUP_RAIL_STUB,
            ),
            rotation=0.0,
            outward_side="top",
        ))
    return placed_refs, attachments


def place_authored_pulldown_banks(instances, members, positions):
    """Place declared pull-downs as aligned vertical branches to one return."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind == "pulldown"
    )
    if not roles:
        return set(), []
    by_ref = {component.ref: component for component in members}
    owners = {
        str(component.instance.get("reference_designator", "")): component
        for component in members
    }
    groups = {}
    for role in roles:
        component = by_ref[role.component_ref]
        owner = owners.get(role.owner or "")
        if owner is None or role.pin not in owner.terminals or len(component.terminals) != 2:
            raise ToolchainError(
                f"{component.ref}: pulldown owner and pin must name a local "
                "two-terminal branch"
            )
        signal_ref = owner.terminals[role.pin][0]
        signal_terminals = [
            terminal for terminal, (net_ref, _) in component.terminals.items()
            if net_ref == signal_ref
        ]
        if len(signal_terminals) != 1:
            raise ToolchainError(
                f"{component.ref}: pulldown signal does not connect to "
                f"{role.owner}.{role.pin}"
            )
        signal_terminal = signal_terminals[0]
        rail_terminal = next(
            terminal for terminal in component.terminals if terminal != signal_terminal
        )
        rail_ref, rail_net = component.terminals[rail_terminal]
        if not is_return_net(rail_ref, rail_net):
            raise ToolchainError(
                f"{component.ref}: pulldown remote terminal must be a return rail"
            )
        owner_position = positions[owner.symbol_id]
        owner_pin = median_point(pin_positions(owner.instance, owner_position, role.pin))
        side = pin_outward_side(owner.instance, owner_position, role.pin)
        if side not in {"left", "right"}:
            raise ToolchainError(f"{component.ref}: pulldown owner pin must be on a side face")
        groups.setdefault((owner.ref, side, rail_ref), []).append(
            (component, signal_terminal, rail_terminal, owner_pin, rail_net)
        )

    attachments = []
    placed_refs = set()
    for (_, side, rail_ref), entries in sorted(groups.items()):
        outward = -1.0 if side == "left" else 1.0
        ordered = sorted(entries, key=lambda entry: (entry[3].y, entry[0].ref))
        row_y = max(entry[3].y for entry in ordered) + PULLDOWN_VERTICAL_STUB
        rail_points = []
        for index, (component, signal_terminal, rail_terminal, owner_pin, _) in enumerate(
            ordered
        ):
            base = oriented_terminal_vector(
                component,
                signal_terminal,
                rail_terminal,
                axis="y",
                direction=1,
            )
            signal_pin = median_point(
                pin_positions(component.instance, base, signal_terminal)
            )
            lane_x = owner_pin.x + outward * (
                PULLDOWN_OUTWARD_STUB + index * PULLDOWN_LANE_GAP
            )
            placed = translate_pin_to(base, signal_pin, Point(lane_x, row_y))
            positions[component.symbol_id] = placed
            rail_points.extend(pin_positions(component.instance, placed, rail_terminal))
            placed_refs.add(component.ref)
        attachments.append(NetSymbolAttachment(
            rail_ref,
            ordered[0][4],
            Point(
                float(median(point.x for point in rail_points)),
                max(point.y for point in rail_points) + PULLUP_RAIL_STUB,
            ),
            rotation=180.0,
            outward_side="bottom",
        ))
    return placed_refs, attachments
