from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.analysis.connections import (
    is_rail_net,
    is_return_net,
)
from schemer.analysis.drawing_model import Envelope
from schemer.analysis.measurements import with_annotation_envelope
from schemer.core.attributes import attribute_string
from schemer.core.layout import Position
from schemer.placement.circuits.model import Component, NetSymbolAttachment
from schemer.placement.circuits.orientation import (
    fan_in_continuation_coordinate,
    median_point,
    pin_side_near_bounds,
)
from schemer.placement.circuits.policy import LOCAL_GAP, LOCAL_RAIL_STUB, NET_SYMBOL_STUB
from schemer.placement.circuits.queries import net_components
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point
from schemer.symbols.signal_termination import SYMBOL as SIGNAL_TERMINATION_SYMBOL


class NetSymbols:
    def __init__(self, *, qualified: bool = True) -> None:
        self._next_suffix: dict[str, int] = {}
        self.rail_envelopes: list[Envelope] = []
        self._qualified = qualified

    def add(
        self,
        positions: dict[str, Position],
        net_ref: str,
        net: dict[str, Any],
        position: Position,
        outward_side: str | None = None,
    ) -> None:
        if is_rail_net(net_ref, net):
            envelope = rail_drawing_envelope(net, position)
            properties = net.get("properties", {})
            if "__symbol_value" in properties:
                bounds = placed_symbol_bounds({"attributes": properties}, position)
                # Routing lanes clear glyph bodies; conservative whole-block
                # text allowances must not push attached wires away from pins.
                envelope = Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
            # Keep north/south glyphs on separate lanes when their bodies
            # would meet. Moving a lateral termination outward retains
            # exactly one right-angle approach; moving it vertically would not.
            if outward_side in {"left", "right"}:
                for previous in sorted(
                    self.rail_envelopes,
                    key=lambda item: item.min_x,
                    reverse=outward_side == "left",
                ):
                    if (
                        envelope.min_y < previous.max_y + LOCAL_GAP
                        and previous.min_y < envelope.max_y + LOCAL_GAP
                        and envelope.min_x < previous.max_x + LOCAL_GAP
                        and previous.min_x < envelope.max_x + LOCAL_GAP
                    ):
                        delta = (
                            previous.min_x - LOCAL_GAP - envelope.max_x
                            if outward_side == "left"
                            else previous.max_x + LOCAL_GAP - envelope.min_x
                        )
                        position = replace(position, x=position.x + delta)
                        envelope = envelope.translated(delta, 0)
            self.rail_envelopes.append(envelope)
        full_name = str(net.get("name", net_ref))
        raw_name = full_name if self._qualified else full_name.rsplit(".", 1)[-1]
        suffix = self._next_suffix.get(raw_name, 0)
        self._next_suffix[raw_name] = suffix + 1
        positions[f"sym:{raw_name}#{suffix}"] = position


def attach_net_symbols(
    positions: dict[str, Position],
    attachments: tuple[NetSymbolAttachment, ...],
    *,
    symbols: NetSymbols | None = None,
) -> None:
    """Attach drawings only after all electrical endpoints have been chosen."""

    symbols = symbols or NetSymbols()
    for attachment in sorted(attachments, key=attachment_order):
        symbols.add(
            positions,
            attachment.net_ref,
            attachment.net,
            attachment.position(),
            attachment.outward_side,
        )


def attachment_order(attachment: NetSymbolAttachment) -> tuple:
    return (
        attachment.outward_side in {"left", "right"},
        attachment.target.y, attachment.target.x, attachment.net_ref,
    )


def clear_rail_signal_lanes(
    attachments: list[NetSymbolAttachment],
    lanes: list[tuple[str, Envelope]],
    endpoint_lanes: list[tuple[str, Point]] | None = None,
) -> list[NetSymbolAttachment]:
    """Let lateral rail glyphs drift outward; keep useful signal lanes fixed."""
    endpoint_lanes = endpoint_lanes or []
    result = []
    for attachment in attachments:
        side = attachment.outward_side
        if side in {"left", "right"} and is_rail_net(attachment.net_ref, attachment.net):
            same_side_lanes = [
                lane for lane_side, lane in lanes
                if lane_side == side
            ]
            if len(attachment.origins) > 1:
                # A repeated rail becomes one vertical face-local trunk. Its
                # topology is only simple when the trunk is beyond every
                # established signal endpoint on that face; putting it at the
                # shortest x-coordinate makes the router detour each signal
                # around the trunk. Deliberately accept the longer rail stubs.
                min_y = min(
                    attachment.target.y,
                    min(point.y for point in attachment.origins),
                )
                max_y = max(
                    attachment.target.y,
                    max(point.y for point in attachment.origins),
                )
                endpoints = [
                    point for lane_side, point in endpoint_lanes
                    if lane_side == side and min_y <= point.y <= max_y
                ]
                overlapping = [
                    lane for lane in same_side_lanes
                    if min_y < lane.max_y and lane.min_y < max_y
                ]
                if endpoints:
                    target_x = (
                        min(point.x for point in endpoints) - LOCAL_GAP
                        if side == "left"
                        else max(point.x for point in endpoints) + LOCAL_GAP
                    )
                elif overlapping:
                    target_x = (
                        min(lane.min_x for lane in overlapping) - LOCAL_GAP
                        if side == "left"
                        else max(lane.max_x for lane in overlapping) + LOCAL_GAP
                    )
                else:
                    target_x = attachment.target.x
                if endpoints or overlapping:
                    if (side == "left" and target_x < attachment.target.x) or (
                        side == "right" and target_x > attachment.target.x
                    ):
                        attachment = replace(
                            attachment,
                            target=Point(target_x, attachment.target.y),
                        )
            for lane_side, lane in sorted(lanes, key=lambda item: item[1].min_x,
                                          reverse=side == "left"):
                if lane_side != side:
                    continue
                bounds = rail_drawing_envelope(attachment.net, attachment.position())
                if (bounds.min_y < lane.max_y and lane.min_y < bounds.max_y
                        and bounds.min_x < lane.max_x and lane.min_x < bounds.max_x):
                    dx = (lane.min_x - LOCAL_GAP - bounds.max_x if side == "left"
                          else lane.max_x + LOCAL_GAP - bounds.min_x)
                    attachment = replace(attachment, target=Point(
                        attachment.target.x + dx, attachment.target.y,
                    ))
        result.append(attachment)
    # Keep repeated terminations on this owner's face on one rail lane.
    # Moving just one past its neighbours can make the renderer assign its
    # pin to a different same-net glyph and draw an around-the-pins loop.
    for side in ("left", "right"):
        for net_ref in {item.net_ref for item in result if item.outward_side == side}:
            indices = [i for i, item in enumerate(result)
                       if item.outward_side == side and item.net_ref == net_ref]
            if not indices:
                continue
            coordinate = (min if side == "left" else max)(result[i].target.x for i in indices)
            for i in indices:
                result[i] = replace(result[i], target=Point(coordinate, result[i].target.y))
    return result


def rail_drawing_envelope(net: dict[str, Any], position: Position) -> Envelope:
    properties = net.get("properties", {})
    if "__symbol_value" in properties:
        bounds = placed_symbol_bounds({"attributes": properties}, position)
        envelope = Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
    else:
        envelope = Envelope(position.x, position.y, position.x, position.y)
    return with_annotation_envelope(envelope, (str(net.get("name", "")).rsplit(".", 1)[-1],))


def net_symbol_drawing_envelope(attachment: NetSymbolAttachment) -> Envelope:
    """Measure a named endpoint including its projected drawing and caption."""

    properties = attachment.net.get("properties", {})
    if "__symbol_value" not in properties:
        properties = {**properties, "__symbol_value": SIGNAL_TERMINATION_SYMBOL}
    position = attachment.position()
    bounds = placed_symbol_bounds({"attributes": properties}, position)
    return with_annotation_envelope(
        Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
        (str(attachment.net.get("name", attachment.net_ref)).rsplit(".", 1)[-1],),
    )


def shunt_drawing_envelope(
    component: Component, position: Position, rail_terminal: str,
) -> Envelope:
    bounds = placed_symbol_bounds(component.instance, position)
    envelope = with_annotation_envelope(
        Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
        (str(component.instance.get("reference_designator", "")),
         attribute_string(component.instance, "value") or ""),
    )
    rail = terminal_net_symbol_attachment(
        component, position, rail_terminal, clearance=LOCAL_RAIL_STUB,
    )
    return envelope.union(rail_drawing_envelope(rail.net, rail.position()))


def net_symbol_attachment(
    net_ref: str,
    net: dict[str, Any],
    points: tuple[Point, ...],
    side: str,
    *,
    clearance: float = NET_SYMBOL_STUB,
) -> NetSymbolAttachment:
    point = median_point(points)
    if len(points) >= 3:
        if side in {"left", "right"}:
            point = Point(point.x, fan_in_continuation_coordinate(tuple(p.y for p in points)))
        else:
            point = Point(fan_in_continuation_coordinate(tuple(p.x for p in points)), point.y)
    if side == "left":
        target = Point(point.x - clearance, point.y)
    elif side == "right":
        target = Point(point.x + clearance, point.y)
    elif side == "top":
        target = Point(point.x, point.y - clearance)
    else:
        target = Point(point.x, point.y + clearance)
    if is_return_net(net_ref, net):
        if side in {"left", "right"}:
            target = Point(target.x, target.y + LOCAL_RAIL_STUB)
        # A lateral exit then a downward approach takes only one turn.
        # An upward-facing owner pin needs two turns to reach a south-facing
        # ground, so it is the geometric exception to conventional orientation.
        rotation = 180.0 if side == "top" else 0.0
    elif is_rail_net(net_ref, net):
        if side in {"left", "right"}:
            target = Point(target.x, target.y - LOCAL_RAIL_STUB)
        rotation = 180.0 if side == "bottom" else 0.0
    else:
        # The viewer routes into a one-pin symbol according to that pin's
        # orientation, even when the pin length is zero.  A neutral endpoint
        # left at its default vertical orientation therefore turns an
        # otherwise horizontal side-face stub into a small rectangular
        # dogleg.  Orient the endpoint from the already chosen wire direction;
        # distance is not allowed to compensate for a topology mismatch.
        rotation = {
            "left": 270.0,
            "right": 90.0,
            "top": 180.0,
            "bottom": 0.0,
        }[side]
    return NetSymbolAttachment(net_ref, net, target, rotation, side, points)


def anchor_net_symbol_attachments(
    component: Component,
    position: Position,
    *,
    skipped_terminals: set[str] | None = None,
) -> tuple[NetSymbolAttachment, ...]:
    skipped_terminals = skipped_terminals or set()
    bounds = placed_symbol_bounds(component.instance, position)
    by_net_side: dict[tuple[str, str], list[Point]] = {}
    nets_by_ref: dict[str, dict[str, Any]] = {}
    for terminal, (net_ref, net) in component.terminals.items():
        if terminal in skipped_terminals or not is_rail_net(net_ref, net):
            continue
        prefer = "bottom" if is_return_net(net_ref, net) else "top"
        for point in pin_positions(component.instance, position, terminal):
            side = pin_side_near_bounds(point, bounds, prefer=prefer)
            by_net_side.setdefault((net_ref, side), []).append(point)
            nets_by_ref[net_ref] = net
    result: list[NetSymbolAttachment] = []
    for (net_ref, side), points in sorted(by_net_side.items()):
        net = nets_by_ref[net_ref]
        # One rail on one device face is one visible wireset. Splitting it by
        # pin distance produces a row of duplicated L-shaped terminations even
        # though the reader sees one supply or return function. Different
        # faces remain separate so the router never loops around the body.
        attachment = net_symbol_attachment(net_ref, net, tuple(points), side)
        if len(points) > 1 and side in {"left", "right"}:
            terminal_y = (
                bounds.max_y + LOCAL_RAIL_STUB
                if is_return_net(net_ref, net)
                else bounds.min_y - LOCAL_RAIL_STUB
            )
            attachment = replace(
                attachment,
                target=Point(attachment.target.x, terminal_y),
            )
        result.append(attachment)
    return tuple(result)


def terminal_net_symbol_attachment(
    component: Component,
    position: Position,
    terminal: str,
    *,
    side: str | None = None,
    clearance: float = NET_SYMBOL_STUB,
) -> NetSymbolAttachment:
    net_ref, net = component.terminals[terminal]
    points = pin_positions(component.instance, position, terminal)
    bounds = placed_symbol_bounds(component.instance, position)
    if side is None:
        side = pin_side_near_bounds(median_point(points), bounds)
    return net_symbol_attachment(
        net_ref,
        net,
        points,
        side,
        clearance=clearance,
    )


def named_signal_attachment(
    component: Component, position: Position, terminal: str, side: str,
) -> NetSymbolAttachment:
    net_ref, net = component.terminals[terminal]
    presentation_net = {
        **net,
        "properties": {**net.get("properties", {}), "__symbol_value": SIGNAL_TERMINATION_SYMBOL},
    }
    attachment = net_symbol_attachment(
        net_ref, presentation_net, pin_positions(component.instance, position, terminal), side,
    )
    # The neutral symbol uses a zero-length vertical pin, just like a rail
    # attachment, but its rotation is purely the signal's outward direction.
    return replace(attachment, rotation=90 if side == "right" else 270)


def single_ended_net_symbol_attachments(
    component: Component,
    position: Position,
    component_refs: tuple[str, ...],
) -> tuple[NetSymbolAttachment, ...]:
    result: list[NetSymbolAttachment] = []
    for terminal, (net_ref, net) in component.terminals.items():
        name = str(net.get("name", net_ref)).rsplit(".", 1)[-1]
        if is_rail_net(net_ref, net) or name.startswith("NC_"):
            continue
        if net_components(net, component_refs) == {component.ref}:
            result.append(terminal_net_symbol_attachment(component, position, terminal))
    return tuple(result)


def is_not_connected_net(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    local_name = str(net.get("name", net_ref)).rsplit(".", 1)[-1].upper()
    return kind == "notconnected" or local_name.startswith("NC_")


def consolidated_rail_attachments(
    component: Component,
    position: Position,
) -> tuple[NetSymbolAttachment, ...]:
    """Terminate each same-net, same-face pin group with one local symbol."""

    bounds = placed_symbol_bounds(component.instance, position)
    grouped: dict[tuple[str, str], list[Point]] = {}
    nets: dict[str, dict[str, Any]] = {}
    for terminal, (net_ref, net) in component.terminals.items():
        if not is_rail_net(net_ref, net):
            continue
        preferred = "bottom" if is_return_net(net_ref, net) else "top"
        for point in pin_positions(component.instance, position, terminal):
            side = pin_side_near_bounds(point, bounds, prefer=preferred)
            grouped.setdefault((net_ref, side), []).append(point)
            nets[net_ref] = net
    return tuple(
        net_symbol_attachment(net_ref, nets[net_ref], tuple(points), side)
        for (net_ref, side), points in sorted(grouped.items())
    )
