from __future__ import annotations

from typing import Any

from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.symbols.geometry import pin_positions
from schemer.symbols.library import symbol_pin_offset_groups
from schemer.symbols.model import Point
from schemer.symbols.signal_termination import SYMBOL as SIGNAL_TERMINATION_SYMBOL


def net_symbol_pin_position(net: dict[str, Any], position: Position) -> Point:
    """Resolve the sole electrical pin of a placed one-pin net symbol."""

    properties = net.get("properties")
    if not isinstance(properties, dict):
        raise ToolchainError("evaluated net has no symbol properties")
    instance = {"attributes": properties}
    offsets = symbol_pin_offset_groups(instance)
    if not offsets:
        raise ToolchainError("evaluated net symbol has no pin geometry")
    points = {
        (
            round(point.x, 9),
            round(point.y, 9),
        )
        for pin_name in offsets
        for point in pin_positions(instance, position, pin_name)
    }
    if len(points) != 1:
        raise ToolchainError("evaluated net symbol must have exactly one electrical pin")
    x, y = next(iter(points))
    return Point(x, y)


def net_with_default_signal_symbol(net: dict[str, Any]) -> dict[str, Any]:
    properties = net.get("properties")
    if isinstance(properties, dict) and "__symbol_value" in properties:
        return net
    return {
        **net,
        "properties": {
            **(properties if isinstance(properties, dict) else {}),
            "__symbol_value": SIGNAL_TERMINATION_SYMBOL,
        },
    }


def position_net_symbol_pin(
    net: dict[str, Any],
    target: Point,
    *,
    rotation: float = 0.0,
) -> Position:
    """Return a stored anchor whose net-symbol pin lands exactly on ``target``."""

    origin = Position(0.0, 0.0, rotation=rotation)
    pin = net_symbol_pin_position(net, origin)
    return Position(target.x - pin.x, target.y - pin.y, rotation=rotation)
