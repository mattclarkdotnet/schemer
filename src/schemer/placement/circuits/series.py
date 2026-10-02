from __future__ import annotations

from dataclasses import replace

from schemer.analysis.drawing_model import Envelope
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.circuits.model import Component, SeriesLink, SeriesWire
from schemer.placement.circuits.orientation import (
    connector_net_point,
    connector_orientation,
    median_point,
    passive_orientation,
    translate_pin_to,
)
from schemer.placement.circuits.policy import (
    CONNECTOR_STUB,
    LOCAL_GAP,
    SERIES_PITCH,
    SIGNAL_STUB,
    STAGGER,
)
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point


def series_wires_from_ic(
    central: Component,
    central_position: Position,
    links: tuple[SeriesLink, ...],
    *,
    side: str,
) -> tuple[SeriesWire, ...]:
    """Derive the final electrical lanes from IC pins before placing symbols."""

    direction = -1.0 if side == "left" else 1.0
    ordered = sorted(
        links,
        key=lambda link: (
            median_point(
                pin_positions(central.instance, central_position, link.central_terminal)
            ).y,
            link.passive.ref,
        ),
    )
    result: list[SeriesWire] = []
    for link in ordered:
        central_pin = median_point(
            pin_positions(central.instance, central_position, link.central_terminal)
        )
        result.append(
            SeriesWire(
                link,
                side,
                central_pin,
                Point(central_pin.x + direction * SIGNAL_STUB, central_pin.y),
            )
        )
    return tuple(result)


def place_series_symbols(wires: tuple[SeriesWire, ...]) -> dict[str, Position]:
    """Attach each series component to its already selected IC wire lane."""

    result: dict[str, Position] = {}
    for wire in wires:
        link = wire.link
        if wire.side == "left":
            first_terminal = link.passive_connector_terminal
            second_terminal = link.passive_central_terminal
        else:
            first_terminal = link.passive_central_terminal
            second_terminal = link.passive_connector_terminal
        passive_position = passive_orientation(
            link.passive,
            first_terminal,
            second_terminal,
        )
        central_pin = median_point(
            pin_positions(link.passive.instance, passive_position, link.passive_central_terminal)
        )
        passive_position = translate_pin_to(
            passive_position,
            central_pin,
            wire.passive_pin_target,
        )
        result[link.passive.symbol_id] = passive_position
    return result


def place_connector(
    connector: Component,
    wires: tuple[SeriesWire, ...],
    passive_positions: dict[str, Position],
    *,
    side: str,
) -> Position:
    links = tuple(wire.link for wire in wires)
    position = connector_orientation(connector, links, side=side)
    bounds = placed_symbol_bounds(connector.instance, position)
    passive_pins = [
        median_point(
            pin_positions(
                link.passive.instance,
                passive_positions[link.passive.symbol_id],
                link.passive_connector_terminal,
            )
        )
        for link in links
    ]
    if side == "left":
        target_edge = min(point.x for point in passive_pins) - CONNECTOR_STUB
        delta_x = target_edge - bounds.max_x
    else:
        target_edge = max(point.x for point in passive_pins) + CONNECTOR_STUB
        delta_x = target_edge - bounds.min_x
    # Register the first signal lane, not an average between incompatible
    # connector/IC pin orders. Averaging leaves every connection misaligned.
    first_wire = min(wires, key=lambda wire: wire.owner_pin.y)
    delta_y = (
        first_wire.owner_pin.y
        - connector_net_point(connector, position, first_wire.link.connector_net).y
    )
    return replace(position, x=position.x + delta_x, y=position.y + delta_y)


def apply_series_annotation_clearance(
    wires: tuple[SeriesWire, ...],
    passive_positions: dict[str, Position],
    *,
    side: str,
    obstacles: tuple[Envelope, ...] = (),
) -> dict[str, Position]:
    """Resolve text crowding last by moving symbols only along their wire lanes.

    When the endpoint orders oppose one another, one bend per link is
    unavoidable. The owner endpoint remains straight and crowded parts use
    staggered x distances; annotation clearance cannot change a wire's y.
    """

    targets = [(wire.link, wire.owner_pin.y) for wire in wires]
    crowded = any(
        abs(first[1] - second[1]) < SERIES_PITCH
        for index, first in enumerate(targets)
        for second in targets[index + 1 :]
    )
    ordered = sorted(targets, key=lambda item: (item[1], item[0].passive.ref))
    result = dict(passive_positions)
    for index, (link, target_y) in enumerate(ordered):
        position = result[link.passive.symbol_id]
        central_pin = median_point(
            pin_positions(
                link.passive.instance,
                position,
                link.passive_central_terminal,
            )
        )
        stagger = (index - (len(ordered) - 1) / 2) * STAGGER if crowded else 0.0
        if side == "left":
            stagger = -stagger
        result[link.passive.symbol_id] = replace(
            position,
            x=position.x + stagger,
            y=position.y,
        )
        # The connector's local return bank is already complete. A series
        # body may slide along its wire, but must not occupy that
        # bank's ground glyph or label. Preserve the electrical row.
        for obstacle in sorted(obstacles, key=lambda item: item.min_x, reverse=side == "right"):
            placed = result[link.passive.symbol_id]
            bounds = placed_symbol_bounds(link.passive.instance, placed)
            envelope = Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
            if (envelope.min_y < obstacle.max_y + LOCAL_GAP
                    and obstacle.min_y < envelope.max_y + LOCAL_GAP
                    and envelope.min_x < obstacle.max_x + LOCAL_GAP
                    and obstacle.min_x < envelope.max_x + LOCAL_GAP):
                delta = (obstacle.max_x + LOCAL_GAP - envelope.min_x if side == "left"
                         else obstacle.min_x - LOCAL_GAP - envelope.max_x)
                result[link.passive.symbol_id] = replace(placed, x=placed.x + delta)
        if abs(central_pin.y - target_y) > 1e-6:
            raise ToolchainError("annotation clearance cannot move a series symbol off its wire")
    return result
