from __future__ import annotations

from schemer.analysis.connections import is_rail_net, is_return_net
from schemer.analysis.roles import component_roles
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    BYPASS_CLEARANCE_STEP,
    BYPASS_OWNER_GAP,
    INLINE_KINDS,
    INLINE_ROLE_STUB,
    POWER_FEED_STUB,
)
from schemer.placement.circuits.queries import component_drawing_envelope, drawings_overlap
from schemer.symbols.geometry import pin_outward_side, pin_positions
from schemer.symbols.model import Point


def place_authored_bypasses(instances, members, positions) -> set[str]:
    """Place datasheet-owned bypass capacitors outside their owner's pin corridors."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind == "bypass"
    )
    if not roles:
        return set()
    by_ref = {component.ref: component for component in members}
    owners = {
        str(component.instance.get("reference_designator", "")): component
        for component in members
    }
    placed_refs: set[str] = set()
    for role in sorted(roles, key=lambda item: item.component_ref):
        component = by_ref[role.component_ref]
        owner = owners.get(role.owner or "")
        if (
            owner is None
            or role.pin not in owner.terminals
            or component.component_type != "capacitor"
            or len(component.terminals) != 2
        ):
            raise ToolchainError(
                f"{component.ref}: bypass owner and pin must name a local capacitor branch"
            )
        supply_ref = owner.terminals[role.pin][0]
        supply_terminals = [
            terminal for terminal, (net_ref, _) in component.terminals.items()
            if net_ref == supply_ref
        ]
        if len(supply_terminals) != 1:
            raise ToolchainError(
                f"{component.ref}: bypass supply does not connect to "
                f"{role.owner}.{role.pin}"
            )
        supply_terminal = supply_terminals[0]
        return_terminal = next(
            terminal for terminal in component.terminals
            if terminal != supply_terminal
        )
        return_ref, return_net = component.terminals[return_terminal]
        if not is_return_net(return_ref, return_net):
            raise ToolchainError(f"{component.ref}: bypass remote terminal must be a return net")

        base = oriented_terminal_vector(
            component, supply_terminal, return_terminal, axis="y", direction=1,
        )
        owner_bounds = component_drawing_envelope(owner, positions[owner.symbol_id])
        base_bounds = component_drawing_envelope(component, base)
        target_y = owner_bounds.min_y - BYPASS_OWNER_GAP - base_bounds.max_y
        owner_center_x = (owner_bounds.min_x + owner_bounds.max_x) / 2
        base_center_x = (base_bounds.min_x + base_bounds.max_x) / 2
        other_drawings = [
            component_drawing_envelope(other, positions[other.symbol_id])
            for other in members
            if other.ref not in {component.ref, owner.ref}
        ]
        for step in range(1000):
            lane = (step + 1) // 2
            direction = -1 if step % 2 else 1
            dx = owner_center_x - base_center_x + direction * lane * BYPASS_CLEARANCE_STEP
            candidate = Position(base.x + dx, base.y + target_y, base.rotation, base.mirror)
            if not any(drawings_overlap(component_drawing_envelope(component, candidate), other)
                       for other in other_drawings):
                break
        else:
            raise ToolchainError(f"cannot clear authored bypass for {component.ref}")
        positions[component.symbol_id] = candidate
        placed_refs.add(component.ref)
    return placed_refs


def place_authored_power_feeds(instances, members, positions):
    """Place declared supply feeds beside the exact owner pin they supply."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind == "power-feed"
    )
    if not roles:
        return set(), {}
    by_ref = {component.ref: component for component in members}
    owners = {
        str(component.instance.get("reference_designator", "")): component
        for component in members
    }
    placed_refs: set[str] = set()
    skipped: dict[str, set[str]] = {}
    for role in sorted(roles, key=lambda item: item.component_ref):
        component = by_ref[role.component_ref]
        owner = owners.get(role.owner or "")
        if (
            owner is None
            or role.pin not in owner.terminals
            or len(component.terminals) != 2
        ):
            raise ToolchainError(
                f"{component.ref}: power-feed owner and pin must name a local "
                "two-terminal supply branch"
            )
        output_ref, output_net = owner.terminals[role.pin]
        output_terminals = [
            terminal for terminal, (net_ref, _) in component.terminals.items()
            if net_ref == output_ref
        ]
        if len(output_terminals) != 1 or not is_rail_net(output_ref, output_net):
            raise ToolchainError(
                f"{component.ref}: power-feed output does not connect to "
                f"{role.owner}.{role.pin}"
            )
        output_terminal = output_terminals[0]
        input_terminal = next(
            terminal for terminal in component.terminals if terminal != output_terminal
        )
        input_ref, input_net = component.terminals[input_terminal]
        if (
            is_return_net(output_ref, output_net)
            or not is_rail_net(input_ref, input_net)
            or is_return_net(input_ref, input_net)
        ):
            raise ToolchainError(f"{component.ref}: power-feed endpoints must be supply rails")

        owner_position = positions[owner.symbol_id]
        owner_pin = median_point(pin_positions(owner.instance, owner_position, role.pin))
        side = pin_outward_side(owner.instance, owner_position, role.pin)
        horizontal = side in {"left", "right"}
        outward = -1.0 if side in {"left", "top"} else 1.0
        base = oriented_terminal_vector(
            component, output_terminal, input_terminal,
            axis="x" if horizontal else "y", direction=outward,
        )
        output_pin = median_point(pin_positions(component.instance, base, output_terminal))
        placed = translate_pin_to(
            base,
            output_pin,
            Point(
                owner_pin.x + (outward * POWER_FEED_STUB if horizontal else 0),
                owner_pin.y + (0 if horizontal else outward * POWER_FEED_STUB),
            ),
        )
        positions[component.symbol_id] = placed
        placed_refs.add(component.ref)
        skipped.setdefault(owner.ref, set()).add(role.pin)
        skipped.setdefault(component.ref, set()).add(output_terminal)
    return placed_refs, skipped


def place_authored_inline_parts(instances, members, positions):
    """Place a source-declared inline part directly outward from its owner pin."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind in INLINE_KINDS
    )
    if not roles:
        return set(), {}
    by_ref = {component.ref: component for component in members}
    owners = {
        str(component.instance.get("reference_designator", "")): component
        for component in members
    }
    placed_refs: set[str] = set()
    skipped: dict[str, set[str]] = {}
    pending = sorted(roles, key=lambda item: item.component_ref)
    ordered = []
    while pending:
        pending_refs = {role.component_ref for role in pending}
        ready = [role for role in pending
                 if role.owner not in owners or owners[role.owner].ref not in pending_refs]
        if not ready:
            raise ToolchainError("cyclic authored inline ownership")
        ordered.extend(ready)
        pending = [role for role in pending if role not in ready]
    for role in ordered:
        component = by_ref[role.component_ref]
        owner = owners.get(role.owner or "")
        if (
            owner is None
            or role.pin not in owner.terminals
            or len(component.terminals) != 2
        ):
            raise ToolchainError(
                f"{component.ref}: {role.kind} owner and pin must name a local "
                "two-terminal inline part"
            )
        owner_net_ref = owner.terminals[role.pin][0]
        attached = [
            terminal for terminal, (net_ref, _) in component.terminals.items()
            if net_ref == owner_net_ref
        ]
        if len(attached) != 1:
            raise ToolchainError(
                f"{component.ref}: {role.kind} does not connect to "
                f"{role.owner}.{role.pin}"
            )
        attached_terminal = attached[0]
        remote_terminal = next(
            terminal for terminal in component.terminals
            if terminal != attached_terminal
        )
        owner_position = positions[owner.symbol_id]
        owner_pin = median_point(pin_positions(owner.instance, owner_position, role.pin))
        side = pin_outward_side(owner.instance, owner_position, role.pin)
        dx, dy = {
            "left": (-1.0, 0.0),
            "right": (1.0, 0.0),
            "top": (0.0, -1.0),
            "bottom": (0.0, 1.0),
        }[side]
        base = oriented_terminal_vector(
            component,
            attached_terminal,
            remote_terminal,
            axis="x" if dx else "y",
            direction=dx or dy,
        )
        component_pin = median_point(
            pin_positions(component.instance, base, attached_terminal)
        )
        placed = translate_pin_to(
            base,
            component_pin,
            Point(
                owner_pin.x + dx * INLINE_ROLE_STUB,
                owner_pin.y + dy * INLINE_ROLE_STUB,
            ),
        )
        occupied = [component_drawing_envelope(other, positions[other.symbol_id])
                    for other in members if other.ref != component.ref
                    and other.symbol_id in positions]
        for step in range(1000):
            candidate = Position(placed.x + dx * step * INLINE_ROLE_STUB,
                                 placed.y + dy * step * INLINE_ROLE_STUB,
                                 placed.rotation, placed.mirror)
            if not any(
                drawings_overlap(component_drawing_envelope(component, candidate), box)
                for box in occupied
            ):
                placed = candidate
                break
        else:
            raise ToolchainError(f"cannot clear authored inline part {component.ref}")
        positions[component.symbol_id] = placed
        placed_refs.add(component.ref)
        skipped.setdefault(owner.ref, set()).add(role.pin)
        skipped.setdefault(component.ref, set()).add(attached_terminal)
    return placed_refs, skipped


def place_single_rail_shunts(members, positions, *, skipped: set[str]) -> set[str]:
    """Orient individual supply-to-return capacitors vertically."""

    placed: set[str] = set()
    for component in members:
        if component.ref in skipped or component.component_type != "capacitor":
            continue
        supplies = [
            terminal for terminal, (net_ref, net) in component.terminals.items()
            if is_rail_net(net_ref, net) and not is_return_net(net_ref, net)
        ]
        returns = [
            terminal for terminal, (net_ref, net) in component.terminals.items()
            if is_return_net(net_ref, net)
        ]
        if len(supplies) != 1 or len(returns) != 1:
            continue
        old = positions[component.symbol_id]
        old_supply = median_point(pin_positions(component.instance, old, supplies[0]))
        base = oriented_terminal_vector(
            component, supplies[0], returns[0], axis="y", direction=1,
        )
        source = median_point(pin_positions(component.instance, base, supplies[0]))
        positions[component.symbol_id] = translate_pin_to(base, source, old_supply)
        placed.add(component.ref)
    return placed
