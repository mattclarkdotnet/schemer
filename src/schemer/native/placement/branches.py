from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from schemer.analysis.circuits import (
    component_layout_groups,
    component_properties,
)
from schemer.analysis.connectivity import expected_pin_nets
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
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
    Vector2,
    place_symbol,
)
from schemer.native.endpoints import component_net_endpoints
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.native.routing import shared_vertical_trunk, stub_endpoint
from schemer.native.routing_model import PIN_STUB_MM, PlacedEndpoint
from schemer.symbols.library import symbol_pin_number_groups


def place_vertical_pin_bias_branches(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """A pull resistor on a vertical device pin branches beside the device."""

    symbols = list(editor.get_symbols())
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    pin_nets = expected_pin_nets(schematic)
    for symbol in symbols:
        if symbol.zener_path is None:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        props = component_properties(schematic, ref)
        if props.get("role") not in {"pullup", "pulldown"}:
            continue
        owner = owner_symbol_for_pin(schematic, editor, props.get("owner"), props.get("pin"))
        if owner is None or owner.zener_path is None:
            continue
        owner_ref = schematic["root_ref"] + "." + owner.zener_path
        number = symbol_pin_number_groups(schematic["instances"][owner_ref])[props["pin"]][0]
        owner_raw = raw_by_id[owner.id]
        side = placed_pin_sides(editor.document, owner_raw)[number]
        if side not in {"top", "bottom"}:
            continue
        pins = placed_pin_positions(editor.document, raw_by_id[symbol.id])
        if len(pins) != 2:
            continue
        net = pin_nets[(owner.reference, number)]
        signal = next(pin for pin in pins if pin_nets[(symbol.reference, pin)] == net)
        wanted_side = "top" if props["role"] == "pulldown" else "bottom"
        raw = raw_by_id[symbol.id]
        rotation = next(angle for angle in (0, 90, 180, 270)
                        if placed_pin_sides(editor.document, replace(raw, rotation=angle))[signal]
                        == wanted_side)
        owner_pins = placed_pin_positions(editor.document, owner_raw)
        anchor = stub_endpoint(PlacedEndpoint(owner_pins[number], side)).position
        branch_x = min(pin.x for pin in owner_pins.values()) - round(7.62 * 1_000_000)
        old_pin = placed_pin_positions(editor.document, replace(raw, rotation=rotation))[signal]
        place_symbol(symbol, Vector2(
            symbol.position.x + branch_x - old_pin.x,
            symbol.position.y + anchor.y - old_pin.y,
        ), rotation)
        editor.update_items(symbol)


def place_top_pin_bypasses(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Keep bypass branches above top pins or crowded side-pin exits."""

    symbols = list(editor.get_symbols())
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    pin_nets = expected_pin_nets(schematic)
    for symbol in symbols:
        if symbol.zener_path is None:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        props = component_properties(schematic, ref)
        if props.get("role") != "bypass":
            continue
        owner = owner_symbol_for_pin(schematic, editor, props.get("owner"), props.get("pin"))
        if owner is None or owner.zener_path is None:
            continue
        owner_ref = schematic["root_ref"] + "." + owner.zener_path
        numbers = symbol_pin_number_groups(schematic["instances"][owner_ref])[props["pin"]]
        owner_raw = raw_by_id[owner.id]
        owner_sides = placed_pin_sides(editor.document, owner_raw)
        owner_pins = placed_pin_positions(editor.document, owner_raw)
        number = next((n for n in numbers if owner_sides.get(n) == "top"), None)
        if number is None:
            # A bypass/return on a side supply pin must not occupy the rail
            # glyph space of a lower neighbouring pin. Raise that branch;
            # text can move later, but the electrical symbols cannot overlap.
            types = symbol_library_pins(editor.document, owner_raw)
            number = next((n for n in numbers if owner_sides.get(n) in {"left", "right"}
                           and any(owner_sides[m] == owner_sides[n]
                                   and 0 < p.y - owner_pins[n].y <= 5_080_000
                                   and (pin_nets.get((owner.reference, m))
                                        != pin_nets.get((owner.reference, n))
                                        or types[m].electrical_type in {"input", "bidirectional"})
                                   for m, p in owner_pins.items())), None)
        if number is None:
            continue
        raw = raw_by_id[symbol.id]
        pins = placed_pin_positions(editor.document, raw)
        if len(pins) != 2:
            continue
        net = pin_nets[(owner.reference, number)]
        supply = next(pin for pin in pins if pin_nets[(symbol.reference, pin)] == net)
        direction = -1 if owner_sides[number] == "left" else 1
        if owner_sides[number] == "top":
            # Fan out from the body rather than sending every branch right
            # through neighbouring top-pin exits. Use rendered geometry, not
            # pin names or supply polarity, to select the open side.
            top_xs = {owner_pins[n].x for n, side in owner_sides.items() if side == "top"}
            if len(top_xs) > 1 and owner_pins[number].x < (min(top_xs) + max(top_xs)) / 2:
                direction = -1
        rotation = next(angle for angle in (0, 90, 180, 270)
                        if placed_pin_sides(editor.document, replace(raw, rotation=angle))[supply]
                        == ("left" if direction == 1 else "right"))
        anchor = owner_pins[number]
        # Two normal pin exits leave a tee for the supply arrow. The return
        # can then turn south once; it does not dictate the capacitor axis.
        clearance = round(2 * PIN_STUB_MM * 1_000_000)
        old_pin = placed_pin_positions(editor.document, replace(raw, rotation=rotation))[supply]
        branch_y = anchor.y - clearance
        if owner_sides[number] in {"left", "right"}:
            # A fixed one-row lift can put the bypass directly across another
            # signal exit. Clear the entire same-face pin bank, not just the
            # supply pin's lower neighbour.
            branch_y = min(p.y for n, p in owner_pins.items()
                           if owner_sides[n] == owner_sides[number]) - clearance
        place_symbol(symbol, Vector2(
            symbol.position.x + anchor.x + direction * clearance - old_pin.x,
            symbol.position.y + branch_y - old_pin.y,
        ), rotation)
        editor.update_items(symbol)


def align_perpendicular_branches(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Compact branches onto their trunk without reversing path-node order.

    Symbol bodies constrain this pass; movable captions are fitted afterwards.
    A standalone shunt may approach its node, but not cross a neighbouring
    shunt's axis and thereby undo the circuit placer's branch-bank ordering.
    """

    by_owner = {
        schematic["root_ref"] + "." + item.zener_path: item
        for item in editor.get_symbols() if item.zener_path is not None
    }
    raw = {item.uuid: item for item in editor.document.symbols}
    bounds = {}
    for item_id, item in raw.items():
        points = placed_symbol_body_positions(editor.document, item)
        if points:
            bounds[item_id] = Envelope(min(p.x for p in points), min(p.y for p in points),
                                        max(p.x for p in points), max(p.y for p in points))
    net_endpoints = component_net_endpoints(schematic, editor)
    standalone = {
        owner for owner in by_owner
        if (props := component_properties(schematic, owner)).get("role") == "shunt"
        and not props.get("owner")
    }
    branch_axes = {p.owner: (p.group, p.position.x)
                   for endpoints in net_endpoints.values() for p in endpoints
                   if p.owner in standalone and p.side in {"top", "bottom"}}
    local_nodes: dict[tuple[str, str | None], list[PlacedEndpoint]] = defaultdict(list)
    for net, endpoints in net_endpoints.items():
        for endpoint in endpoints:
            local_nodes[net, endpoint.group].append(endpoint)
    for endpoints in local_nodes.values():
        vertical = [p for p in endpoints if p.side in {"top", "bottom"}]
        horizontal = [p for p in endpoints if p.side in {"left", "right"}]
        if not vertical or not horizontal:
            continue
        for branch in vertical:
            item = by_owner.get(branch.owner)
            if item is None or len(placed_pin_positions(editor.document, raw[item.id])) != 2:
                continue
            props = component_properties(schematic, branch.owner)
            if props.get("role") in {"series", "divider"}:
                continue
            independent = branch.owner in standalone
            if not independent and (len(vertical) != 1 or len(horizontal) < 2):
                continue
            x, y = branch.position.x, branch.position.y
            if len(vertical) == 1:
                x = shared_vertical_trunk([stub_endpoint(p) for p in endpoints])
            if independent:
                # Keep each node's branches in order even when its neighbour
                # has a wider body or a different number of attachments.
                if any(group == branch.group and owner != branch.owner
                       and (branch.position.x - other_x) * (x - other_x) <= 0
                       for owner, (group, other_x) in branch_axes.items()):
                    x = branch.position.x
                rows = {p.position.y for p in horizontal}
                if len(rows) == 1:
                    row = next(iter(rows))
                    sign = 1 if branch.side == "top" else -1
                    # Shorten only: never shift a branch across its through-wire.
                    if sign * (y - row) > round(PIN_STUB_MM * 1_000_000):
                        y = row + sign * round(PIN_STUB_MM * 1_000_000)
            delta = Vector2(x - branch.position.x, y - branch.position.y)
            candidate = bounds.get(item.id)
            if candidate is None or delta == Vector2(0, 0):
                continue
            candidate = candidate.translated(delta)
            if any(
                not envelopes_do_not_overlap(candidate, other, 0)
                for other_id, other in bounds.items() if other_id != item.id
            ):
                continue
            place_symbol(item, Vector2(item.position.x + delta.x, item.position.y + delta.y),
                              item.transform.orientation)
            editor.update_items(item)
            bounds[item.id] = candidate
            if independent:
                branch_axes[branch.owner] = branch.group, x


def align_same_face_terminal_exits(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Leave outward space between facing pins of folded two-terminal symbols."""

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    groups, _ = component_layout_groups(schematic)
    by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    net_by_pin = expected_pin_nets(schematic)
    for terminal, net in net_by_pin.items():
        if terminal[0] in symbols:
            by_net[net].append(terminal)
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    for ref, symbol in symbols.items():
        source = raw[symbol.id]
        pins = placed_pin_positions(editor.document, source)
        sides = placed_pin_sides(editor.document, source)
        if len(pins) != 2 or len(set(sides.values())) != 1:
            continue
        side = next(iter(sides.values()))
        vertical = side in {"top", "bottom"}
        sign = 1 if side in {"right", "bottom"} else -1
        group = groups[schematic["root_ref"] + "." + symbol.zener_path]
        limits = []
        for number, point in pins.items():
            for other_ref, other_number in by_net[net_by_pin[(ref, number)]]:
                other = symbols[other_ref]
                if other_ref == ref or groups[schematic["root_ref"] + "."
                                             + other.zener_path] != group:
                    continue
                other_raw = raw[other.id]
                if placed_pin_sides(editor.document, other_raw)[other_number] != opposite[side]:
                    continue
                target = placed_pin_positions(editor.document, other_raw)[other_number]
                span = sign * (target.y - point.y if vertical else target.x - point.x)
                limits.append(span - round(2 * PIN_STUB_MM * 1_000_000))
        shift = min([0, *limits])
        if shift == 0:
            continue
        delta = Vector2(0, sign * shift) if vertical else Vector2(sign * shift, 0)
        body = envelope_from_points(placed_symbol_body_positions(editor.document, source))
        if any(not envelopes_do_not_overlap(body.translated(delta), envelope_from_points(
                placed_symbol_body_positions(editor.document, raw[other.id])), 500_000)
               for other in symbols.values() if other.id != symbol.id):
            continue
        place_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                          symbol.position.y + delta.y),
                          symbol.transform.orientation)
        editor.update_items(symbol)
        raw = {s.uuid: s for s in editor.document.symbols}
