from __future__ import annotations

from dataclasses import replace

from schemer.analysis.connections import is_rail_net, is_return_net
from schemer.analysis.drawing_model import Envelope
from schemer.analysis.roles import component_roles
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.blocks.composition import block_from_positions
from schemer.placement.blocks.model import Rect
from schemer.placement.blocks.plan import BlockPlan
from schemer.placement.circuits.bypasses import component_envelopes
from schemer.placement.circuits.model import Component
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    attach_net_symbols,
    named_signal_attachment,
    net_symbol_attachment,
    rail_drawing_envelope,
)
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    INLINE_KINDS,
    PIN_EXIT_STUB,
    ROLE_SERIES_GAP,
    ROLE_SHUNT_STUB,
)
from schemer.placement.circuits.queries import (
    component_drawing_envelope,
    drawings_overlap,
    role_terminal,
)
from schemer.placement.circuits.support import place_authored_inline_parts
from schemer.symbols.geometry import pin_outward_side, pin_positions
from schemer.symbols.model import Point


def role_directed_series_block(
    instances: dict, components: tuple[Component, ...], *, padding: float,
) -> BlockPlan | None:
    """Lay out a series path and its shunts only when the source says what they are."""

    all_roles = component_roles(instances, components)
    # An owned series/shunt network is support inside a larger circuit, not
    # a claim that the entire sheet is one standalone passive signal path.
    support = tuple(role for role in all_roles if role.owner is not None)
    roles = tuple(role for role in all_roles if role.owner is None
                  and role.kind in {"series", "shunt"})
    if support:
        # Explicitly owned branches do not turn a passive circuit into an IC
        # support network. Preserve the authored path and place its branches
        # afterwards, but only when every owner chain ends inside that path.
        if (any(role.kind not in INLINE_KINDS for role in support)
                or len(roles) + len(support) != len(components)):
            return None
        owners = {str(c.instance.get("reference_designator", "")): c.ref for c in components}
        rooted = {role.component_ref for role in roles}
        pending = list(support)
        while pending:
            ready = [role for role in pending if owners.get(role.owner) in rooted]
            if not ready:
                return None
            rooted.update(role.component_ref for role in ready)
            pending = [role for role in pending if role not in ready]
    if not roles:
        return None
    if len(roles) + len(support) != len(components):
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
    if not series_roles:
        return None
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

    first_input = next(iter(series[0].terminals)) if len(series) == 1 else next(
        terminal for terminal, (net_ref, _) in series[0].terminals.items()
        if net_ref != joins[0]
    )
    last_output = next(
        t for t in series[0].terminals if t != first_input
    ) if len(series) == 1 else next(
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
        base = oriented_terminal_vector(
            component, incoming, outgoing, axis="x", direction=1,
        )
        input_pin = median_point(pin_positions(component.instance, base, incoming))
        target = Point(0.0, 0.0) if previous_output is None else Point(
            previous_output.x + ROLE_SERIES_GAP, previous_output.y,
        )
        placed = translate_pin_to(base, input_pin, target)
        positions[component.symbol_id] = placed
        actual_input = median_point(pin_positions(component.instance, placed, incoming))
        actual_output = median_point(pin_positions(component.instance, placed, outgoing))
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
            "left": (-PIN_EXIT_STUB, 0.0),
            "right": (PIN_EXIT_STUB, 0.0),
            "top": (0.0, -PIN_EXIT_STUB),
            "bottom": (0.0, PIN_EXIT_STUB),
        }[side]
        branch_points[net_ref] = Point(point.x + dx, point.y + dy)
    attachments = []
    occupied = [
        component_drawing_envelope(component, positions[component.symbol_id])
        for component in series
    ]
    # Preserve the order of attachment nodes along the signal path. A
    # collision-driven shuffle across a neighbouring node introduces a wire
    # crossing that no amount of downstream routing can make informative.
    shunt_roles = sorted(shunt_roles, key=lambda role: (
        path_points[by_ref[role.component_ref].terminals[
            role_terminal(by_ref[role.component_ref], role.at)][0]].x,
        role.component_ref,
    ))
    previous_branch_x = None
    for role in shunt_roles:
        component = by_ref[role.component_ref]
        if len(component.terminals) != 2 or role.at is None:
            raise ToolchainError(f"{component.ref}: shunt role requires a two-terminal part")
        attached = role_terminal(component, role.at)
        attached_ref, _ = component.terminals[attached]
        if attached_ref not in path_net_refs:
            raise ToolchainError(
                f"{component.ref}: shunt attachment {role.at!r} is not on its series path"
            )
        returned = next(terminal for terminal in component.terminals if terminal != attached)
        return_ref, return_net = component.terminals[returned]
        if not is_return_net(return_ref, return_net):
            raise ToolchainError(f"{component.ref}: shunt remote terminal must be a return net")
        base = oriented_terminal_vector(
            component, attached, returned, axis="y", direction=1,
        )
        pin = median_point(pin_positions(component.instance, base, attached))
        # An endpoint branch joins after the viewer's mandatory outward pin
        # exit. Internal path nodes already have a straight continuation, so
        # their physical shared terminal remains the natural junction.
        junction = branch_points[attached_ref]
        placed = translate_pin_to(
            base, pin, Point(junction.x, junction.y + ROLE_SHUNT_STUB),
        )
        # Several authored shunts can share a node. Give each a clear
        # parallel branch instead of stacking their bodies on that node's x.
        box = component_drawing_envelope(component, placed)
        pitch = max(ROLE_SERIES_GAP, box.max_x - box.min_x + padding)
        for step in range(1000):
            lane = (step + 1) // 2 * (1 if step % 2 else -1)
            candidate = replace(placed, x=placed.x + lane * pitch)
            if previous_branch_x is not None and candidate.x <= previous_branch_x:
                continue
            box = component_drawing_envelope(component, candidate)
            if not any(drawings_overlap(box, other) for other in occupied):
                placed = candidate
                occupied.append(box)
                previous_branch_x = candidate.x
                break
        else:
            raise ToolchainError(f"cannot clear authored shunt branch for {component.ref}")
        positions[component.symbol_id] = placed

    # Complete each node's branch bank before the next series stage. Use
    # actual fitted terminal positions: caption clearance can have moved a
    # branch beyond the initial gap. Shift the remaining path as one suffix,
    # retaining node order and all already-cleared branch spacing.
    node_order = {net: i for i, net in enumerate(path_points)}
    for index, node in enumerate(joins, 1):
        branches = [by_ref[r.component_ref] for r in shunt_roles
                    if by_ref[r.component_ref].terminals[
                        role_terminal(by_ref[r.component_ref], r.at)][0] == node]
        if not branches:
            continue
        input_pin = median_point(pin_positions(
            series[index].instance, positions[series[index].symbol_id], terminal_pairs[index][0]))
        last_branch = max(median_point(pin_positions(c.instance, positions[c.symbol_id],
            next(t for t, (net, _) in c.terminals.items() if net == node))).x for c in branches)
        shift = max(0.0, last_branch + 2 * PIN_EXIT_STUB - input_pin.x)
        downstream = [*series[index:], *(by_ref[r.component_ref] for r in shunt_roles
            if node_order[by_ref[r.component_ref].terminals[
                role_terminal(by_ref[r.component_ref], r.at)][0]] > node_order[node])]
        for component in downstream:
            p = positions[component.symbol_id]
            positions[component.symbol_id] = replace(p, x=p.x + shift)

    for role in shunt_roles:
        component = by_ref[role.component_ref]
        attached = role_terminal(component, role.at)
        returned = next(t for t in component.terminals if t != attached)
        return_ref, return_net = component.terminals[returned]
        attachments.append(net_symbol_attachment(
            return_ref,
            return_net,
            pin_positions(component.instance, positions[component.symbol_id], returned),
            "bottom",
        ))

    if support:
        place_authored_inline_parts(instances, components, positions)
        owners = {str(c.instance.get("reference_designator", "")): c for c in components}
        for role in support:
            component = by_ref[role.component_ref]
            owner_net = owners[role.owner].terminals[role.pin][0]
            for terminal, (net_ref, net) in component.terminals.items():
                if net_ref == owner_net:
                    continue
                position = positions[component.symbol_id]
                side = pin_outward_side(component.instance, position, terminal)
                attachments.append(net_symbol_attachment(
                    net_ref, net, pin_positions(component.instance, position, terminal), side,
                ))

    first_ref, first_net = first_component.terminals[first_input]
    last_ref, last_net = last_component.terminals[last_output]
    attachments.append(
        net_symbol_attachment(
            first_ref, first_net,
            pin_positions(
                first_component.instance, positions[first_component.symbol_id], first_input,
            ),
            "left",
        )
        if is_rail_net(first_ref, first_net)
        else named_signal_attachment(
            first_component, positions[first_component.symbol_id], first_input, "left",
        )
    )
    attachments.append(
        net_symbol_attachment(
            last_ref, last_net,
            pin_positions(
                last_component.instance, positions[last_component.symbol_id], last_output,
            ),
            "right",
        )
        if is_rail_net(last_ref, last_net)
        else named_signal_attachment(
            last_component, positions[last_component.symbol_id], last_output, "right",
        )
    )

    symbols = NetSymbols()
    attach_net_symbols(positions, tuple(attachments), symbols=symbols)
    envelopes = [
        component_drawing_envelope(component, positions[component.symbol_id])
        for component in components
    ]
    envelopes.extend(rail_drawing_envelope(item.net, item.position()) for item in attachments)
    envelopes.extend(symbols.rail_envelopes)
    bounds = Envelope(
        min(item.min_x for item in envelopes), min(item.min_y for item in envelopes),
        max(item.max_x for item in envelopes), max(item.max_y for item in envelopes),
    )
    leaf = block_from_positions(
        "authored-series-group",
        positions,
        occupied=component_envelopes(components, positions),
        content_bounds=Rect(bounds.min_x, bounds.min_y, bounds.width, bounds.height),
        padding=padding,
    )
    result = BlockPlan(leaf)
    result.validate()
    return result
