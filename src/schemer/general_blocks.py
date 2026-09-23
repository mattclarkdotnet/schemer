"""Baseline local blocks for circuits outside the specialised motif rules."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt
from statistics import median

from schemer.blocks import BlockPlan, Rect, block_from_positions, compose_column, compose_row
from schemer.heuristic_block import (
    _PIN_EXIT_STUB,
    _anchor_net_symbol_attachments,
    _attach_net_symbols,
    _clear_rail_signal_lanes,
    _Component,
    _component_envelopes,
    _components,
    _is_not_connected_net,
    _mean_point,
    _named_signal_attachment,
    _net_symbol_attachment,
    _net_symbol_drawing_envelope,
    _NetSymbolAttachment,
    _NetSymbols,
    _oriented_terminal_vector,
    _rail_drawing_envelope,
    _single_ended_net_symbol_attachments,
    _translated,
)
from schemer.layout import ModuleLayout, Position
from schemer.layout_metrics import Envelope, _with_annotation_envelope
from schemer.roles import component_roles
from schemer.symbol_geometry import (
    _NON_OWNER_TYPES,
    Point,
    _attribute_string,
    _is_rail_net,
    _is_return_net,
    pin_outward_side,
    pin_positions,
    placed_symbol_bounds,
)
from schemer.toolchain import ToolchainError
from schemer.view_policy import _is_non_explanatory_component

_SERVICE_TYPES = {"mechanical", "mounting_hole", "test_point", "fiducial"}
_PASSIVE_CHAIN_GAP = 40.0
_PASSIVE_CHAIN_OWNER_GAP = 160.0
_DIVIDER_COMPONENT_GAP = 40.0
_DIVIDER_OWNER_GAP = 100.0
_DIVIDER_TAP_STUB = 60.0
_BYPASS_OWNER_GAP = 80.0
_BYPASS_CLEARANCE_STEP = 80.0
_ROLE_SERIES_GAP = 100.0
_ROLE_SHUNT_STUB = 120.0
_PULLUP_STUB = 320.0
_PULLUP_RAIL_STUB = 80.0
_PULLDOWN_OUTWARD_STUB = 160.0
_PULLDOWN_LANE_GAP = 100.0
_PULLDOWN_VERTICAL_STUB = 100.0
_POWER_FEED_STUB = 180.0
_INLINE_ROLE_STUB = 160.0


@dataclass(frozen=True)
class _PassiveChain:
    """An ordered resistor path whose intermediate nets must stay visible."""

    entries: tuple[tuple[_Component, str, str], ...]
    net_refs: tuple[str, ...]


def _role_terminal(component: _Component, local_net_name: str) -> str:
    matches = [
        terminal
        for terminal, (net_ref, net) in component.terminals.items()
        if str(net.get("name", net_ref)).rsplit(".", 1)[-1] == local_net_name
    ]
    if len(matches) != 1:
        raise ToolchainError(
            f"{component.ref}: role net {local_net_name!r} must name exactly one terminal"
        )
    return matches[0]


def _role_directed_series_block(
    instances: dict, components: tuple[_Component, ...], *, padding: float,
) -> BlockPlan | None:
    """Lay out a series path and its shunts only when the source says what they are."""

    all_roles = component_roles(instances, components)
    roles = tuple(role for role in all_roles if role.kind in {"series", "shunt"})
    if not roles:
        return None
    if len(roles) != len(components):
        missing = sorted({component.ref for component in components}
                         - {role.component_ref for role in roles})
        raise ToolchainError(
            "a role-directed local block cannot guess roles for: " + ", ".join(missing)
        )
    groups = {role.group for role in roles}
    if len(groups) != 1:
        raise ToolchainError(
            "one local role-directed block currently requires exactly one role group"
        )
    by_ref = {component.ref: component for component in components}
    series_roles = sorted(
        (role for role in roles if role.kind == "series"),
        key=lambda role: role.order if role.order is not None else -1,
    )
    shunt_roles = tuple(role for role in roles if role.kind == "shunt")
    if len(series_roles) < 2:
        raise ToolchainError("a role-directed series path requires at least two series parts")
    if [role.order for role in series_roles] != list(range(len(series_roles))):
        raise ToolchainError("series role order must be unique and contiguous from zero")

    series = [by_ref[role.component_ref] for role in series_roles]
    if any(len(component.terminals) != 2 for component in series):
        raise ToolchainError("series roles require two-terminal components")
    joins: list[str] = []
    for first, second in zip(series, series[1:]):
        shared = (
            {net_ref for net_ref, _ in first.terminals.values()}
            & {net_ref for net_ref, _ in second.terminals.values()}
        )
        if len(shared) != 1:
            raise ToolchainError(
                f"authored series neighbours {first.ref} and {second.ref} "
                "must share exactly one net"
            )
        joins.append(shared.pop())

    first_input = next(
        terminal for terminal, (net_ref, _) in series[0].terminals.items()
        if net_ref != joins[0]
    )
    last_output = next(
        terminal for terminal, (net_ref, _) in series[-1].terminals.items()
        if net_ref != joins[-1]
    )
    terminal_pairs: list[tuple[str, str]] = []
    for index, component in enumerate(series):
        incoming = (
            first_input if index == 0 else
            next(terminal for terminal, (net_ref, _) in component.terminals.items()
                 if net_ref == joins[index - 1])
        )
        outgoing = (
            last_output if index == len(series) - 1 else
            next(terminal for terminal, (net_ref, _) in component.terminals.items()
                 if net_ref == joins[index])
        )
        terminal_pairs.append((incoming, outgoing))

    positions: dict[str, Position] = {}
    path_points: dict[str, Point] = {}
    previous_output: Point | None = None
    for component, (incoming, outgoing) in zip(series, terminal_pairs):
        base = _oriented_terminal_vector(
            component, incoming, outgoing, axis="x", direction=1,
        )
        input_pin = _mean_point(pin_positions(component.instance, base, incoming))
        target = Point(0.0, 0.0) if previous_output is None else Point(
            previous_output.x + _ROLE_SERIES_GAP, previous_output.y,
        )
        placed = _translated(base, input_pin, target)
        positions[component.symbol_id] = placed
        actual_input = _mean_point(pin_positions(component.instance, placed, incoming))
        actual_output = _mean_point(pin_positions(component.instance, placed, outgoing))
        path_points.setdefault(component.terminals[incoming][0], actual_input)
        path_points[component.terminals[outgoing][0]] = actual_output
        previous_output = actual_output

    path_net_refs = set(path_points)
    branch_points = dict(path_points)
    first_component, last_component = series[0], series[-1]
    for component, terminal in (
        (first_component, first_input),
        (last_component, last_output),
    ):
        net_ref = component.terminals[terminal][0]
        point = path_points[net_ref]
        side = pin_outward_side(component.instance, positions[component.symbol_id], terminal)
        dx, dy = {
            "left": (-_PIN_EXIT_STUB, 0.0),
            "right": (_PIN_EXIT_STUB, 0.0),
            "top": (0.0, -_PIN_EXIT_STUB),
            "bottom": (0.0, _PIN_EXIT_STUB),
        }[side]
        branch_points[net_ref] = Point(point.x + dx, point.y + dy)
    attachments = []
    for role in shunt_roles:
        component = by_ref[role.component_ref]
        if len(component.terminals) != 2 or role.at is None:
            raise ToolchainError(f"{component.ref}: shunt role requires a two-terminal part")
        attached = _role_terminal(component, role.at)
        attached_ref, _ = component.terminals[attached]
        if attached_ref not in path_net_refs:
            raise ToolchainError(
                f"{component.ref}: shunt attachment {role.at!r} is not on its series path"
            )
        returned = next(terminal for terminal in component.terminals if terminal != attached)
        return_ref, return_net = component.terminals[returned]
        if not _is_return_net(return_ref, return_net):
            raise ToolchainError(f"{component.ref}: shunt remote terminal must be a return net")
        base = _oriented_terminal_vector(
            component, attached, returned, axis="y", direction=1,
        )
        pin = _mean_point(pin_positions(component.instance, base, attached))
        # An endpoint branch joins after the viewer's mandatory outward pin
        # exit. Internal path nodes already have a straight continuation, so
        # their physical shared terminal remains the natural junction.
        junction = branch_points[attached_ref]
        placed = _translated(
            base, pin, Point(junction.x, junction.y + _ROLE_SHUNT_STUB),
        )
        positions[component.symbol_id] = placed
        attachments.append(_net_symbol_attachment(
            return_ref,
            return_net,
            pin_positions(component.instance, placed, returned),
            "bottom",
        ))

    first_ref, first_net = first_component.terminals[first_input]
    last_ref, last_net = last_component.terminals[last_output]
    attachments.append(
        _net_symbol_attachment(
            first_ref, first_net,
            pin_positions(
                first_component.instance, positions[first_component.symbol_id], first_input,
            ),
            "left",
        )
        if _is_rail_net(first_ref, first_net)
        else _named_signal_attachment(
            first_component, positions[first_component.symbol_id], first_input, "left",
        )
    )
    attachments.append(
        _net_symbol_attachment(
            last_ref, last_net,
            pin_positions(
                last_component.instance, positions[last_component.symbol_id], last_output,
            ),
            "right",
        )
        if _is_rail_net(last_ref, last_net)
        else _named_signal_attachment(
            last_component, positions[last_component.symbol_id], last_output, "right",
        )
    )

    symbols = _NetSymbols()
    _attach_net_symbols(positions, tuple(attachments), symbols=symbols)
    envelopes = [_drawing(component, positions[component.symbol_id]) for component in components]
    envelopes.extend(_rail_drawing_envelope(item.net, item.position()) for item in attachments)
    envelopes.extend(symbols.rail_envelopes)
    bounds = Envelope(
        min(item.min_x for item in envelopes), min(item.min_y for item in envelopes),
        max(item.max_x for item in envelopes), max(item.max_y for item in envelopes),
    )
    leaf = block_from_positions(
        "authored-series-group",
        positions,
        occupied=_component_envelopes(components, positions),
        content_bounds=Rect(bounds.min_x, bounds.min_y, bounds.width, bounds.height),
        padding=padding,
    )
    result = BlockPlan(leaf)
    result.validate()
    return result


def _place_authored_pullup_banks(instances, members, positions):
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
        if not _is_rail_net(rail_ref, rail_net) or _is_return_net(rail_ref, rail_net):
            raise ToolchainError(f"{component.ref}: pullup remote terminal must be a supply rail")
        owner_position = positions[owner.symbol_id]
        owner_pin = _mean_point(pin_positions(owner.instance, owner_position, role.pin))
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
            base = _oriented_terminal_vector(
                component,
                rail_terminal,
                signal_terminal,
                axis="x",
                direction=-outward,
            )
            signal_pin = _mean_point(
                pin_positions(component.instance, base, signal_terminal)
            )
            placed = _translated(
                base,
                signal_pin,
                Point(owner_pin.x + outward * _PULLUP_STUB, owner_pin.y),
            )
            positions[component.symbol_id] = placed
            rail_points.extend(pin_positions(component.instance, placed, rail_terminal))
            placed_refs.add(component.ref)
        attachments.append(_NetSymbolAttachment(
            rail_ref,
            entries[0][4],
            Point(
                float(median(point.x for point in rail_points)),
                min(point.y for point in rail_points) - _PULLUP_RAIL_STUB,
            ),
            rotation=0.0,
            outward_side="top",
        ))
    return placed_refs, attachments


def _place_authored_pulldown_banks(instances, members, positions):
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
        if not _is_return_net(rail_ref, rail_net):
            raise ToolchainError(
                f"{component.ref}: pulldown remote terminal must be a return rail"
            )
        owner_position = positions[owner.symbol_id]
        owner_pin = _mean_point(pin_positions(owner.instance, owner_position, role.pin))
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
        row_y = max(entry[3].y for entry in ordered) + _PULLDOWN_VERTICAL_STUB
        rail_points = []
        for index, (component, signal_terminal, rail_terminal, owner_pin, _) in enumerate(
            ordered
        ):
            base = _oriented_terminal_vector(
                component,
                signal_terminal,
                rail_terminal,
                axis="y",
                direction=1,
            )
            signal_pin = _mean_point(
                pin_positions(component.instance, base, signal_terminal)
            )
            lane_x = owner_pin.x + outward * (
                _PULLDOWN_OUTWARD_STUB + index * _PULLDOWN_LANE_GAP
            )
            placed = _translated(base, signal_pin, Point(lane_x, row_y))
            positions[component.symbol_id] = placed
            rail_points.extend(pin_positions(component.instance, placed, rail_terminal))
            placed_refs.add(component.ref)
        attachments.append(_NetSymbolAttachment(
            rail_ref,
            ordered[0][4],
            Point(
                float(median(point.x for point in rail_points)),
                max(point.y for point in rail_points) + _PULLUP_RAIL_STUB,
            ),
            rotation=180.0,
            outward_side="bottom",
        ))
    return placed_refs, attachments


def _place_authored_bypasses(instances, members, positions) -> set[str]:
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
        if not _is_return_net(return_ref, return_net):
            raise ToolchainError(f"{component.ref}: bypass remote terminal must be a return net")

        base = _oriented_terminal_vector(
            component, supply_terminal, return_terminal, axis="y", direction=1,
        )
        owner_bounds = _drawing(owner, positions[owner.symbol_id])
        base_bounds = _drawing(component, base)
        target_y = owner_bounds.min_y - _BYPASS_OWNER_GAP - base_bounds.max_y
        owner_center_x = (owner_bounds.min_x + owner_bounds.max_x) / 2
        base_center_x = (base_bounds.min_x + base_bounds.max_x) / 2
        other_drawings = [
            _drawing(other, positions[other.symbol_id])
            for other in members
            if other.ref not in {component.ref, owner.ref}
            and other.ref not in placed_refs
        ]
        for step in range(1000):
            lane = (step + 1) // 2
            direction = -1 if step % 2 else 1
            dx = owner_center_x - base_center_x + direction * lane * _BYPASS_CLEARANCE_STEP
            candidate = Position(base.x + dx, base.y + target_y, base.rotation, base.mirror)
            if not any(_overlap(_drawing(component, candidate), other)
                       for other in other_drawings):
                break
        else:
            raise ToolchainError(f"cannot clear authored bypass for {component.ref}")
        positions[component.symbol_id] = candidate
        placed_refs.add(component.ref)
    return placed_refs


def _place_authored_power_feeds(instances, members, positions):
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
        if len(output_terminals) != 1 or not _is_rail_net(output_ref, output_net):
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
            _is_return_net(output_ref, output_net)
            or not _is_rail_net(input_ref, input_net)
            or _is_return_net(input_ref, input_net)
        ):
            raise ToolchainError(f"{component.ref}: power-feed endpoints must be supply rails")

        owner_position = positions[owner.symbol_id]
        owner_pin = _mean_point(pin_positions(owner.instance, owner_position, role.pin))
        side = pin_outward_side(owner.instance, owner_position, role.pin)
        horizontal = side in {"left", "right"}
        outward = -1.0 if side in {"left", "top"} else 1.0
        base = _oriented_terminal_vector(
            component, output_terminal, input_terminal,
            axis="x" if horizontal else "y", direction=outward,
        )
        output_pin = _mean_point(pin_positions(component.instance, base, output_terminal))
        placed = _translated(
            base,
            output_pin,
            Point(
                owner_pin.x + (outward * _POWER_FEED_STUB if horizontal else 0),
                owner_pin.y + (0 if horizontal else outward * _POWER_FEED_STUB),
            ),
        )
        positions[component.symbol_id] = placed
        placed_refs.add(component.ref)
        skipped.setdefault(owner.ref, set()).add(role.pin)
        skipped.setdefault(component.ref, set()).add(output_terminal)
    return placed_refs, skipped


def _place_authored_inline_parts(instances, members, positions):
    """Place a source-declared inline part directly outward from its owner pin."""

    inline_kinds = {
        "series-termination",
        "current-limit",
        "ac-coupling",
        "source-impedance",
    }
    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind in inline_kinds
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
        owner_pin = _mean_point(pin_positions(owner.instance, owner_position, role.pin))
        side = pin_outward_side(owner.instance, owner_position, role.pin)
        dx, dy = {
            "left": (-1.0, 0.0),
            "right": (1.0, 0.0),
            "top": (0.0, -1.0),
            "bottom": (0.0, 1.0),
        }[side]
        base = _oriented_terminal_vector(
            component,
            attached_terminal,
            remote_terminal,
            axis="x" if dx else "y",
            direction=dx or dy,
        )
        component_pin = _mean_point(
            pin_positions(component.instance, base, attached_terminal)
        )
        placed = _translated(
            base,
            component_pin,
            Point(
                owner_pin.x + dx * _INLINE_ROLE_STUB,
                owner_pin.y + dy * _INLINE_ROLE_STUB,
            ),
        )
        positions[component.symbol_id] = placed
        placed_refs.add(component.ref)
        skipped.setdefault(owner.ref, set()).add(role.pin)
        skipped.setdefault(component.ref, set()).add(attached_terminal)
    return placed_refs, skipped


def _passive_chains(
    members: tuple[_Component, ...] | list[_Component],
) -> tuple[_PassiveChain, ...]:
    """Find unbranched resistor paths without relying on names or references.

    A net joins two path edges when it connects exactly two resistors and at
    most one other local component. Rails are boundaries, never chain joins.
    The latter condition keeps a shared return bus from absorbing unrelated
    shunts while still allowing an IC sense pin to tap a divider midpoint.
    """

    resistors = {
        component.ref: component
        for component in members
        if component.component_type == "resistor"
        and len(component.terminals) == 2
        and len({net_ref for net_ref, _ in component.terminals.values()}) == 2
    }
    if len(resistors) < 2:
        return ()
    refs_by_net: dict[str, set[str]] = {}
    nets_by_ref: dict[str, dict] = {}
    for component in members:
        for net_ref, net in component.terminals.values():
            refs_by_net.setdefault(net_ref, set()).add(component.ref)
            nets_by_ref[net_ref] = net
    resistor_ends: dict[str, list[str]] = {}
    for component in resistors.values():
        for net_ref, _ in component.terminals.values():
            resistor_ends.setdefault(net_ref, []).append(component.ref)

    adjacency: dict[str, list[tuple[str, str]]] = {ref: [] for ref in resistors}
    for net_ref, endpoint_refs in resistor_ends.items():
        if len(endpoint_refs) != 2 or _is_rail_net(net_ref, nets_by_ref[net_ref]):
            continue
        external = refs_by_net[net_ref] - set(endpoint_refs)
        if len(external) > 1:
            continue
        first, second = endpoint_refs
        adjacency[first].append((second, net_ref))
        adjacency[second].append((first, net_ref))

    result: list[_PassiveChain] = []
    unseen = set(resistors)
    while unseen:
        seed = min(unseen)
        stack = [seed]
        connected: set[str] = set()
        while stack:
            ref = stack.pop()
            if ref in connected:
                continue
            connected.add(ref)
            stack.extend(other for other, _ in adjacency[ref])
        unseen -= connected
        if len(connected) < 2:
            continue
        if any(len(adjacency[ref]) > 2 for ref in connected):
            continue
        edge_count = sum(len(adjacency[ref]) for ref in connected) // 2
        endpoints = sorted(ref for ref in connected if len(adjacency[ref]) == 1)
        if edge_count != len(connected) - 1 or len(endpoints) != 2:
            continue

        ordered_refs: list[str] = []
        previous: str | None = None
        current = endpoints[0]
        while True:
            ordered_refs.append(current)
            following = sorted(other for other, _ in adjacency[current] if other != previous)
            if not following:
                break
            previous, current = current, following[0]
        shared_nets = []
        for first, second in zip(ordered_refs, ordered_refs[1:]):
            shared = {net for other, net in adjacency[first] if other == second}
            if len(shared) != 1:
                break
            shared_nets.append(shared.pop())
        else:
            def terminal_for(component: _Component, net_ref: str) -> str:
                return next(
                    terminal for terminal, (candidate, _) in component.terminals.items()
                    if candidate == net_ref
                )

            first_component = resistors[ordered_refs[0]]
            last_component = resistors[ordered_refs[-1]]
            first_net = next(
                net_ref for net_ref, _ in first_component.terminals.values()
                if net_ref != shared_nets[0]
            )
            last_net = next(
                net_ref for net_ref, _ in last_component.terminals.values()
                if net_ref != shared_nets[-1]
            )
            net_order = [first_net, *shared_nets, last_net]

            def orientation_score(nets: list[str]) -> tuple[int, int]:
                first_ref, last_ref = nets[0], nets[-1]
                first, last = nets_by_ref[first_ref], nets_by_ref[last_ref]
                first_supply = (
                    _is_rail_net(first_ref, first) and not _is_return_net(first_ref, first)
                )
                last_return = _is_return_net(last_ref, last)
                first_return = _is_return_net(first_ref, first)
                last_supply = _is_rail_net(last_ref, last) and not _is_return_net(last_ref, last)
                return (int(first_supply) + int(last_return),
                        -int(first_return) - int(last_supply))

            if orientation_score(list(reversed(net_order))) > orientation_score(net_order):
                ordered_refs.reverse()
                net_order.reverse()
            entries = tuple(
                (
                    resistors[ref],
                    terminal_for(resistors[ref], net_order[index]),
                    terminal_for(resistors[ref], net_order[index + 1]),
                )
                for index, ref in enumerate(ordered_refs)
            )
            result.append(_PassiveChain(entries, tuple(net_order)))
    return tuple(sorted(result, key=lambda chain: tuple(entry[0].ref for entry in chain.entries)))


def _chain_owner(chain: _PassiveChain, members) -> _Component | None:
    chain_refs = {entry[0].ref for entry in chain.entries}
    candidates = []
    for component in members:
        if component.ref in chain_refs or component.component_type in _NON_OWNER_TYPES:
            continue
        shared = {net_ref for net_ref, _ in component.terminals.values()} & set(chain.net_refs)
        candidates.append((len(shared), len(component.terminals), component.ref, component))
    if not candidates:
        return None
    count, _, _, owner = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    return owner if count >= 2 else None


def _authored_divider_chains(instances, members):
    """Resolve declared divider order and owner without part-specific knowledge."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind == "divider"
    )
    if not roles:
        return ()
    by_ref = {component.ref: component for component in members}
    grouped = {}
    for role in roles:
        grouped.setdefault((role.group, role.owner), []).append(role)
    result = []
    for (group, owner_name), group_roles in sorted(grouped.items()):
        ordered = sorted(group_roles, key=lambda role: role.order or 0)
        if [role.order for role in ordered] != list(range(len(ordered))):
            raise ToolchainError(
                f"divider group {group!r} order must be unique and contiguous from zero"
            )
        components = [by_ref[role.component_ref] for role in ordered]
        if len(components) < 2 or any(
            component.component_type != "resistor" or len(component.terminals) != 2
            for component in components
        ):
            raise ToolchainError(
                f"divider group {group!r} requires at least two two-terminal resistors"
            )
        shared_nets = []
        for first, second in zip(components, components[1:]):
            shared = (
                {net_ref for net_ref, _ in first.terminals.values()}
                & {net_ref for net_ref, _ in second.terminals.values()}
            )
            if len(shared) != 1:
                raise ToolchainError(
                    f"divider group {group!r} has discontinuous neighbours "
                    f"{first.ref} and {second.ref}"
                )
            shared_nets.append(shared.pop())

        first_net = next(
            net_ref for net_ref, _ in components[0].terminals.values()
            if net_ref != shared_nets[0]
        )
        last_net = next(
            net_ref for net_ref, _ in components[-1].terminals.values()
            if net_ref != shared_nets[-1]
        )
        net_order = (first_net, *shared_nets, last_net)
        entries = tuple(
            (
                component,
                next(terminal for terminal, (net_ref, _) in component.terminals.items()
                     if net_ref == net_order[index]),
                next(terminal for terminal, (net_ref, _) in component.terminals.items()
                     if net_ref == net_order[index + 1]),
            )
            for index, component in enumerate(components)
        )
        owner = next(
            (
                component for component in members
                if str(component.instance.get("reference_designator", "")) == owner_name
            ),
            None,
        )
        if owner is None:
            raise ToolchainError(
                f"divider group {group!r} owner {owner_name!r} is not a local component"
            )
        result.append((_PassiveChain(entries, net_order), owner))
    return tuple(result)


def _place_authored_dividers(instances, members, positions) -> set[str]:
    """Fold owner-attached dividers around direct, horizontal IC tap wires."""

    placed_refs: set[str] = set()
    for chain, owner in _authored_divider_chains(instances, members):
        owner_position = positions[owner.symbol_id]
        owner_bounds = _drawing(owner, owner_position)
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
            taps.append((boundary, terminal, side, _mean_point(
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
            owner_bounds.min_x - _DIVIDER_OWNER_GAP
            if side == "left"
            else owner_bounds.max_x + _DIVIDER_OWNER_GAP
        )
        temporary: dict[str, Position] = {}

        first_boundary, _, _, first_tap = taps[0]
        cursor_y = first_tap.y
        for component, source_terminal, sink_terminal in reversed(
            chain.entries[:first_boundary]
        ):
            base = _oriented_terminal_vector(
                component, source_terminal, sink_terminal, axis="y", direction=1,
            )
            sink = _mean_point(pin_positions(component.instance, base, sink_terminal))
            placed = _translated(base, sink, Point(backbone_x, cursor_y))
            temporary[component.symbol_id] = placed
            source = _mean_point(pin_positions(component.instance, placed, source_terminal))
            cursor_y = source.y - _DIVIDER_COMPONENT_GAP

        boundaries = [tap[0] for tap in taps]
        for start, end, upper_y, lower_y in zip(
            boundaries, boundaries[1:], tap_ys, tap_ys[1:]
        ):
            segment = chain.entries[start:end]
            top_count = (len(segment) + 1) // 2
            top = segment[:top_count]
            bottom = segment[top_count:]
            cursor_x = backbone_x + outward * _DIVIDER_TAP_STUB
            top_end: Point | None = None
            for component, source_terminal, sink_terminal in top:
                base = _oriented_terminal_vector(
                    component, source_terminal, sink_terminal,
                    axis="x", direction=outward,
                )
                source = _mean_point(pin_positions(component.instance, base, source_terminal))
                placed = _translated(base, source, Point(cursor_x, upper_y))
                temporary[component.symbol_id] = placed
                top_end = _mean_point(
                    pin_positions(component.instance, placed, sink_terminal)
                )
                cursor_x = top_end.x + outward * _DIVIDER_COMPONENT_GAP
            if bottom:
                if top_end is None:
                    raise ToolchainError("divider middle segment lost its upper endpoint")
                cursor_x = top_end.x
                for component, source_terminal, sink_terminal in bottom:
                    base = _oriented_terminal_vector(
                        component, source_terminal, sink_terminal,
                        axis="x", direction=-outward,
                    )
                    source = _mean_point(
                        pin_positions(component.instance, base, source_terminal)
                    )
                    placed = _translated(base, source, Point(cursor_x, lower_y))
                    temporary[component.symbol_id] = placed
                    bottom_end = _mean_point(
                        pin_positions(component.instance, placed, sink_terminal)
                    )
                    cursor_x = bottom_end.x - outward * _DIVIDER_COMPONENT_GAP

        last_boundary, _, _, last_tap = taps[-1]
        cursor_y = last_tap.y
        for component, source_terminal, sink_terminal in chain.entries[last_boundary:]:
            base = _oriented_terminal_vector(
                component, source_terminal, sink_terminal, axis="y", direction=1,
            )
            source = _mean_point(pin_positions(component.instance, base, source_terminal))
            placed = _translated(base, source, Point(backbone_x, cursor_y))
            temporary[component.symbol_id] = placed
            sink = _mean_point(pin_positions(component.instance, placed, sink_terminal))
            cursor_y = sink.y + _DIVIDER_COMPONENT_GAP

        if len(temporary) != len(chain.entries):
            raise ToolchainError(f"{owner.ref}: divider placement did not consume every member")
        chain_refs = {entry[0].ref for entry in chain.entries}
        drawings = [
            _drawing(component, temporary[component.symbol_id])
            for component, _, _ in chain.entries
        ]
        authored_refs = {
            role.component_ref for role in component_roles(instances, tuple(members))
        }
        # Direct functional tap wires are part of the authored divider. They
        # take precedence over the provisional positions of unreviewed nearby
        # support parts. Move those parts rather than silently stretching the
        # divider until the renderer replaces the wire with paired labels.
        for other in sorted(members, key=lambda component: component.ref):
            if other.ref in chain_refs or other.ref == owner.ref:
                continue
            if not any(_overlap(_drawing(other, positions[other.symbol_id]), drawing)
                       for drawing in drawings):
                continue
            if other.ref in authored_refs:
                raise ToolchainError(
                    f"authored divider for {owner.ref} overlaps authored part {other.ref}"
                )
            original = positions[other.symbol_id]
            for step in range(1, 1000):
                lane = (step + 1) // 2
                direction = 1 if step % 2 else -1
                candidate = Position(
                    original.x,
                    original.y + direction * lane * _DIVIDER_COMPONENT_GAP,
                    original.rotation,
                    original.mirror,
                )
                candidate_drawing = _drawing(other, candidate)
                occupied = drawings + [
                    _drawing(peer, positions[peer.symbol_id])
                    for peer in members
                    if peer.ref not in chain_refs
                    and peer.ref not in {owner.ref, other.ref}
                ] + [owner_bounds]
                if not any(_overlap(candidate_drawing, item) for item in occupied):
                    positions[other.symbol_id] = candidate
                    break
            else:
                raise ToolchainError(
                    f"cannot clear provisional part {other.ref} from authored divider"
                )
        for symbol_id, position in temporary.items():
            positions[symbol_id] = position
        placed_refs.update(chain_refs)
    return placed_refs


def _place_tapped_passive_chains(members, positions) -> set[str]:
    """Place each IC-tapped resistor path as one continuous vertical ladder."""

    placed_refs: set[str] = set()
    for chain in _passive_chains(members):
        owner = _chain_owner(chain, members)
        if owner is None:
            continue
        chain_refs = {entry[0].ref for entry in chain.entries}
        previous_sink: Point | None = None
        temporary: dict[str, Position] = {}
        for index, (component, source_terminal, sink_terminal) in enumerate(chain.entries):
            base = _oriented_terminal_vector(
                component, source_terminal, sink_terminal, axis="y", direction=1,
            )
            source = _mean_point(pin_positions(component.instance, base, source_terminal))
            source_y = (
                0.0 if previous_sink is None else previous_sink.y + _PASSIVE_CHAIN_GAP
            )
            placed = _translated(base, source, Point(0.0, source_y))
            temporary[component.symbol_id] = placed
            previous_sink = _mean_point(pin_positions(component.instance, placed, sink_terminal))

        chain_drawings = [
            _drawing(component, temporary[component.symbol_id])
            for component, _, _ in chain.entries
        ]
        chain_bounds = Envelope(
            min(item.min_x for item in chain_drawings),
            min(item.min_y for item in chain_drawings),
            max(item.max_x for item in chain_drawings),
            max(item.max_y for item in chain_drawings),
        )
        owner_position = positions[owner.symbol_id]
        owner_bounds = _drawing(owner, owner_position)
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
            owner_bounds.min_x - _PASSIVE_CHAIN_OWNER_GAP - chain_bounds.max_x
            if side == "left"
            else owner_bounds.max_x + _PASSIVE_CHAIN_OWNER_GAP - chain_bounds.min_x
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
            upstream_point = _mean_point(pin_positions(
                upstream.instance, temporary[upstream.symbol_id], upstream_terminal,
            ))
            downstream_point = _mean_point(pin_positions(
                downstream.instance, temporary[downstream.symbol_id], downstream_terminal,
            ))
            junction_y = (upstream_point.y + downstream_point.y) / 2
            owner_y = _mean_point(pin_positions(
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
            _drawing(component, positions[component.symbol_id])
            for component in members
            if component.ref not in chain_refs
        ]
        direction = -1 if side == "left" else 1
        for step in range(1000):
            dx = target_x + direction * step * _PASSIVE_CHAIN_GAP
            moved = Envelope(
                chain_bounds.min_x + dx, chain_bounds.min_y + target_y,
                chain_bounds.max_x + dx, chain_bounds.max_y + target_y,
            )
            if not any(_overlap(moved, other) for other in other_drawings):
                break
        else:
            raise ToolchainError("cannot clear tapped passive chain from its owner block")
        for symbol_id, position in temporary.items():
            positions[symbol_id] = Position(
                position.x + dx, position.y + target_y, position.rotation, position.mirror,
            )
        placed_refs.update(chain_refs)
    return placed_refs


def _place_single_rail_shunts(members, positions, *, skipped: set[str]) -> set[str]:
    """Orient individual supply-to-return capacitors vertically."""

    placed: set[str] = set()
    for component in members:
        if component.ref in skipped or component.component_type != "capacitor":
            continue
        supplies = [
            terminal for terminal, (net_ref, net) in component.terminals.items()
            if _is_rail_net(net_ref, net) and not _is_return_net(net_ref, net)
        ]
        returns = [
            terminal for terminal, (net_ref, net) in component.terminals.items()
            if _is_return_net(net_ref, net)
        ]
        if len(supplies) != 1 or len(returns) != 1:
            continue
        old = positions[component.symbol_id]
        old_supply = _mean_point(pin_positions(component.instance, old, supplies[0]))
        base = _oriented_terminal_vector(
            component, supplies[0], returns[0], axis="y", direction=1,
        )
        source = _mean_point(pin_positions(component.instance, base, supplies[0]))
        positions[component.symbol_id] = _translated(base, source, old_supply)
        placed.add(component.ref)
    return placed


def _drawing(component: _Component, position: Position) -> Envelope:
    bounds = placed_symbol_bounds(component.instance, position)
    return _with_annotation_envelope(
        Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
        (str(component.instance.get("reference_designator", "")),
         _attribute_string(component.instance, "value") or ""),
    )


def _overlap(a: Envelope, b: Envelope) -> bool:
    return (a.min_x < b.max_x + 30 and b.min_x < a.max_x + 30
            and a.min_y < b.max_y + 30 and b.min_y < a.max_y + 30)


def _parallel_capacitor_banks(members, positions):
    """Lay out same-owner parallel capacitors as one two-rail drawing."""
    candidates = {}
    for component in members:
        if component.component_type != "capacitor" or len(component.terminals) != 2:
            continue
        supply = [t for t, (ref, net) in component.terminals.items()
                  if _is_rail_net(ref, net) and not _is_return_net(ref, net)]
        returns = [t for t, (ref, net) in component.terminals.items() if _is_return_net(ref, net)]
        if len(supply) == len(returns) == 1:
            key = (component.terminals[supply[0]][0], component.terminals[returns[0]][0])
            candidates.setdefault(key, []).append((component, supply[0], returns[0]))
    attachments = []
    bank_refs = set()
    for bank in candidates.values():
        if len(bank) < 2:
            continue
        refs = {component.ref for component, _, _ in bank}
        other_drawings = [_drawing(c, positions[c.symbol_id]) for c in members if c.ref not in refs]
        first, supply, _ = bank[0]
        supply_y = _mean_point(pin_positions(first.instance, positions[first.symbol_id], supply)).y
        cursor = max((e.max_x for e in other_drawings), default=0) + 160
        # The bank is placed as a whole after its owner's other attachments.
        # Each capacitor points from the upper supply bus to the lower return bus.
        for component, supply, returned in bank:
            base = _oriented_terminal_vector(component, supply, returned, axis="y", direction=1)
            source_pin = _mean_point(pin_positions(component.instance, base, supply))
            drawing = _drawing(component, base)
            placed = _translated(base, source_pin, Point(cursor + source_pin.x - drawing.min_x,
                                                        supply_y))
            positions[component.symbol_id] = placed
            cursor = _drawing(component, placed).max_x + 80
        for terminal_index, side in ((1, "top"), (2, "bottom")):
            component = bank[0][0]
            net_ref, net = component.terminals[bank[0][terminal_index]]
            points = tuple(_mean_point(pin_positions(c.instance, positions[c.symbol_id], item))
                           for c, item in ((entry[0], entry[terminal_index]) for entry in bank))
            attachments.append(_net_symbol_attachment(net_ref, net, points, side))
        bank_refs.update(refs)
    return bank_refs, attachments


def general_local_blocks(
    schematic: dict, module: ModuleLayout, *, padding: float = 40,
    excluded_modules: tuple[str, ...] = (),
    child_blocks: dict[str, BlockPlan] | None = None,
) -> BlockPlan | None:
    """Assign every electrical component once, then measure and pack local blocks.

    Anchors are ICs, connectors and non-two-terminal devices. Passives attach
    along signal nets first, supply nets second; a shared return alone does
    not establish ownership. Passive-only circuits start their own chain.
    Child modules with separate layouts are never swallowed by their parent.
    """
    instances, _, all_components = _components(schematic, module)
    components = tuple(c for c in all_components
                       if c.component_type not in _SERVICE_TYPES
                       and not _is_non_explanatory_component(c.instance)
                       and not any(c.ref.startswith(ref + ".") for ref in excluded_modules))
    child_blocks = child_blocks or {}
    if not components and not child_blocks:
        return None
    if components and not child_blocks:
        role_block = _role_directed_series_block(instances, components, padding=padding)
        if role_block is not None:
            return role_block
    anchors = sorted(
        (c for c in components if c.component_type not in _NON_OWNER_TYPES
         or c.component_type == "connector" or len(c.terminals) != 2),
        key=lambda c: (-len(c.terminals), c.ref),
    )
    groups = [[anchor] for anchor in anchors]
    owned = {anchor.ref: (index, 0) for index, anchor in enumerate(anchors)}
    pending = {c.ref: c for c in components if c.ref not in owned}
    links = {}
    while pending:
        candidates = []
        for child in pending.values():
            for parent in components:
                if parent.ref not in owned:
                    continue
                group, depth = owned[parent.ref]
                for child_terminal, (net_ref, net) in child.terminals.items():
                    if _is_return_net(net_ref, net) or _is_not_connected_net(net_ref, net):
                        continue
                    for parent_terminal, (other_ref, _) in parent.terminals.items():
                        if other_ref == net_ref:
                            candidates.append((int(_is_rail_net(net_ref, net)), depth, group,
                                               child.ref, parent.ref,
                                               child_terminal, parent_terminal))
        if not candidates:
            anchor = min(pending.values(), key=lambda c: c.ref)
            owned[anchor.ref] = (len(groups), 0)
            groups.append([anchor])
            del pending[anchor.ref]
            continue
        _, depth, group, child_ref, parent_ref, child_terminal, parent_terminal = min(candidates)
        child = pending.pop(child_ref)
        groups[group].append(child)
        owned[child_ref] = (group, depth + 1)
        links[child_ref] = (parent_ref, child_terminal, parent_terminal)

    symbols = _NetSymbols(qualified=True)
    blocks = []
    by_ref = {c.ref: c for c in components}
    for index, members in enumerate(groups):
        anchor = members[0]
        positions = {anchor.symbol_id: Position(0, 0)}
        envelopes = [_drawing(anchor, positions[anchor.symbol_id])]
        for child in members[1:]:
            parent_ref, attached, parent_terminal = links[child.ref]
            parent = by_ref[parent_ref]
            parent_position = positions[parent.symbol_id]
            origin = _mean_point(pin_positions(parent.instance, parent_position, parent_terminal))
            side = pin_outward_side(parent.instance, parent_position, parent_terminal)
            dx, dy = {"left": (-1, 0), "right": (1, 0),
                      "top": (0, -1), "bottom": (0, 1)}[side]
            remote = next(t for t in child.terminals if t != attached)
            base = _oriented_terminal_vector(child, attached, remote,
                                            axis="x" if dx else "y", direction=dx or dy)
            point = _mean_point(pin_positions(child.instance, base, attached))
            # Preserve the chosen pin lane. Increase outward clearance only
            # when the completed component drawing would collide with a peer.
            for step in range(1000):
                distance = 160 + step * 80
                position = _translated(base, point,
                                       Point(origin.x + dx * distance, origin.y + dy * distance))
                envelope = _drawing(child, position)
                if not any(_overlap(envelope, other) for other in envelopes):
                    break
            else:
                raise ToolchainError(f"cannot clear local attachment lane for {child.ref}")
            positions[child.symbol_id] = position
            envelopes.append(envelope)

        bank_refs, attachments = _parallel_capacitor_banks(members, positions)
        power_feed_refs, power_feed_skips = _place_authored_power_feeds(
            instances, members, positions,
        )
        inline_refs, inline_skips = _place_authored_inline_parts(
            instances, members, positions,
        )
        bypass_refs = _place_authored_bypasses(instances, members, positions)
        divider_refs = _place_authored_dividers(instances, members, positions)
        _place_tapped_passive_chains(
            [component for component in members
             if component.ref not in divider_refs | power_feed_refs | inline_refs],
            positions,
        )
        _place_single_rail_shunts(
            members,
            positions,
            skipped=bank_refs | bypass_refs | power_feed_refs | inline_refs,
        )
        pullup_refs, pullup_attachments = _place_authored_pullup_banks(
            instances, members, positions,
        )
        pulldown_refs, pulldown_attachments = _place_authored_pulldown_banks(
            instances, members, positions,
        )
        attachments.extend(pullup_attachments)
        attachments.extend(pulldown_attachments)
        divider_owner_skips: dict[str, set[str]] = {}
        for chain, owner in _authored_divider_chains(instances, members):
            endpoint_nets = {chain.net_refs[0], chain.net_refs[-1]}
            divider_owner_skips.setdefault(owner.ref, set()).update(
                terminal
                for terminal, (net_ref, net) in owner.terminals.items()
                if net_ref in endpoint_nets and _is_rail_net(net_ref, net)
            )
        attachment_skips = {
            component_ref: set(terminals)
            for component_ref, terminals in power_feed_skips.items()
        }
        for component_ref, terminals in inline_skips.items():
            attachment_skips.setdefault(component_ref, set()).update(terminals)
        for owner_ref, terminals in divider_owner_skips.items():
            attachment_skips.setdefault(owner_ref, set()).update(terminals)
        envelopes = [_drawing(c, positions[c.symbol_id]) for c in members]
        member_refs = tuple(c.ref for c in members)
        for component in members:
            position = positions[component.symbol_id]
            if component.ref not in bank_refs | pullup_refs | pulldown_refs:
                attachments.extend(_anchor_net_symbol_attachments(
                    component,
                    position,
                    skipped_terminals=attachment_skips.get(component.ref),
                ))
            attachments.extend(_single_ended_net_symbol_attachments(
                component, position, member_refs,
            ))
        signal_lanes = [
            (attachment.outward_side, _net_symbol_drawing_envelope(attachment))
            for attachment in attachments
            if attachment.outward_side in {"left", "right"}
            and not _is_rail_net(attachment.net_ref, attachment.net)
        ]
        signal_endpoints = [
            (attachment.outward_side, attachment.target)
            for attachment in attachments
            if attachment.outward_side in {"left", "right"}
            and not _is_rail_net(attachment.net_ref, attachment.net)
        ]
        attachments = _clear_rail_signal_lanes(
            attachments, signal_lanes, signal_endpoints,
        )
        _attach_net_symbols(positions, tuple(attachments), symbols=symbols)
        envelopes.extend(_rail_drawing_envelope(a.net, a.position()) for a in attachments)
        # Include actual displaced symbol drawings too, not just planned endpoints.
        envelopes.extend(symbols.rail_envelopes)
        symbols.rail_envelopes.clear()
        min_x = min(e.min_x for e in envelopes)
        min_y = min(e.min_y for e in envelopes)
        max_x = max(e.max_x for e in envelopes)
        max_y = max(e.max_y for e in envelopes)
        blocks.append(block_from_positions(
            f"local-{index}", positions,
            occupied=_component_envelopes(tuple(members), positions),
            content_bounds=Rect(min_x, min_y, max_x - min_x, max_y - min_y), padding=padding,
        ))
    for ref, child in child_blocks.items():
        key = "comp:" + ref.removeprefix(module.instance_ref + ".")
        blocks.append(block_from_positions(
            f"module-{len(blocks)}", {key: Position(0, 0)},
            content_bounds=Rect(0, 0, child.root.width, child.root.height), padding=0,
        ))
    columns = ceil(sqrt(len(blocks)))
    rows = tuple(compose_row(f"row-{i}", tuple(blocks[i:i + columns]), gap=180)
                 for i in range(0, len(blocks), columns))
    result = BlockPlan(compose_column("general-local-blocks", rows, gap=180))
    result.validate()
    return result
