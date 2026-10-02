from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.analysis.circuits import component_properties
from schemer.analysis.connectivity import expected_pin_nets
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    envelope_from_points,
    envelopes_do_not_overlap,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad.items import (
    SchematicLine,
    Vector2,
    place_symbol,
)
from schemer.native.endpoints import component_net_endpoints
from schemer.native.routing import route_group, stub_endpoint
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box


def orient_shunts_away_from_signal_routes(
    schematic: dict[str, Any], editor: FileSchematic,
) -> None:
    """Choose the clear side of a shunt's node before routing its rail return.

    Compare both orientations about the *connected pin*, not the body centre.
    Projected local signal trees supply crossing costs; captions remain movable.
    Keep the existing orientation on ties and never move dependent circuitry.
    """
    symbols = [s for s in editor.get_symbols() if s.zener_path is not None]
    props = {s.reference: component_properties(
        schematic, schematic["root_ref"] + "." + s.zener_path) for s in symbols}
    rails = {n["name"] for n in schematic["nets"].values()
             if n.get("kind") in {"Power", "Ground"}}
    nets = expected_pin_nets(schematic)
    for symbol in symbols:
        attributes = props[symbol.reference]
        if attributes.get("role") != "shunt" or not attributes.get("owner"):
            continue
        if any(p.get("owner") == symbol.reference for p in props.values()):
            continue
        raw = {s.uuid: s for s in editor.document.symbols}
        original = raw[symbol.id]
        pins = placed_pin_positions(editor.document, original)
        if len(pins) != 2:
            continue
        attached = [n for n in pins if nets.get((symbol.reference, n)) not in rails]
        if len(attached) != 1:
            continue
        terminal = attached[0]
        returned = next(n for n in pins if n != terminal)
        if nets.get((symbol.reference, returned)) not in rails:
            continue
        endpoints = component_net_endpoints(schematic, editor)
        path = schematic["root_ref"] + "." + symbol.zener_path
        group = next(p.group for ps in endpoints.values() for p in ps if p.owner == path)
        wires = [w for net, ps in endpoints.items()
                 if net not in rails and net != nets[symbol.reference, terminal]
                 for w in route_group([p for p in ps if p.group == group], topology_only=True)
                 if isinstance(w, SchematicLine)]
        obstacles = [envelope_from_points(placed_symbol_body_positions(editor.document, s))
                     for s in raw.values() if s.path is not None and s.uuid != symbol.id]

        def score(item, delta):
            body = envelope_from_points(placed_symbol_body_positions(editor.document, item))
            if any(not envelopes_do_not_overlap(body.translated(delta), b, 635_000)
                   for b in obstacles):
                return float("inf")
            point = placed_pin_positions(editor.document, item)[returned]
            side = placed_pin_sides(editor.document, item)[returned]
            end = stub_endpoint(PlacedEndpoint(
                Vector2(point.x + delta.x, point.y + delta.y), side)).position
            box = envelope_from_points((pins[terminal], end))
            return sum(segment_hits_box(w.start, w.end, box) for w in wires)

        rotation = (original.rotation + 180) % 360
        flipped = replace(original, rotation=rotation)
        pin = placed_pin_positions(editor.document, flipped)[terminal]
        delta = Vector2(pins[terminal].x - pin.x, pins[terminal].y - pin.y)
        if score(flipped, delta) < score(original, Vector2(0, 0)):
            place_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                              symbol.position.y + delta.y), rotation)
            editor.update_items(symbol)


def orient_single_terminal_rails(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Point passive one-terminal rail access outward on the natural rail axis."""
    nets = expected_pin_nets(schematic)
    rail_sides = {net["name"]: "bottom" if net.get("kind") == "Ground" else "top"
                  for net in schematic["nets"].values()
                  if net.get("kind") in {"Power", "Ground"}}
    typed = {s.id: s for s in editor.get_symbols() if s.zener_path is not None}
    for raw in editor.document.symbols:
        if raw.uuid not in typed:
            continue
        pins = symbol_library_pins(editor.document, raw)
        if len(pins) != 1:
            continue
        number, pin = next(iter(pins.items()))
        side = rail_sides.get(nets.get((raw.reference, number)))
        if side is None or pin.electrical_type != "passive":
            continue
        rotation = next(angle for angle in (0, 90, 180, 270)
                        if placed_pin_sides(editor.document, replace(
                            raw, rotation=angle))[number] == side)
        symbol = typed[raw.uuid]
        place_symbol(symbol, symbol.position, rotation)
        editor.update_items(symbol)
