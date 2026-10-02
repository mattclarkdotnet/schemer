from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from schemer.analysis.circuits import (
    component_layout_groups,
    component_properties,
)
from schemer.analysis.connectivity import expected_pin_nets
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_segments,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.items import (
    Vector2,
    place_symbol,
)
from schemer.kicad.records import KiCadSymbol
from schemer.native.placement.chains import compose_owned_series_trees
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.native.routing import stub_endpoint
from schemer.native.routing_model import PIN_STUB_MM, PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box
from schemer.symbols.library import symbol_pin_number_groups


def place_owned_pin_networks(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Keep an authored series/shunt network together at its shared pin node.

    Retain the shunt bank's local branch and place the series member beside
    it. Move its authored attachment tree with it, using the displacement of
    each declared owner pin (not the owner's origin when it rotates).
    Ownership and roles come from source; proximity never creates members.
    """

    composed = compose_owned_series_trees(schematic, editor)
    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    nets = expected_pin_nets(schematic)
    groups, _ = component_layout_groups(schematic)
    source = {ref: schematic["root_ref"] + "." + s.zener_path for ref, s in symbols.items()}
    properties = {ref: component_properties(schematic, path) for ref, path in source.items()}
    children: dict[str, list[str]] = defaultdict(list)
    for ref, props in properties.items():
        if props.get("owner") in symbols:
            children[props["owner"]].append(ref)

    def descendants(ref: str) -> list[str]:
        return [child for direct in sorted(children[ref])
                for child in (direct, *descendants(direct))]

    def depth(ref: str) -> int:
        owner = properties[ref].get("owner")
        return 1 + depth(owner) if owner in symbols else 0

    def attached_move(ref: str, candidate: KiCadSymbol) -> dict[str, KiCadSymbol]:
        moved = {ref: candidate}
        for child in descendants(ref):
            props = properties[child]
            owner = props["owner"]
            old, new = raw[symbols[owner].id], moved[owner]
            if props.get("pin"):
                number = symbol_pin_number_groups(schematic["instances"][source[owner]])[
                    props["pin"]][0]
                before = placed_pin_positions(editor.document, old)[number]
                after = placed_pin_positions(editor.document, new)[number]
                dx, dy = (after.x - before.x) / 1e6, (after.y - before.y) / 1e6
            else:
                dx, dy = new.position[0] - old.position[0], new.position[1] - old.position[1]
            original = raw[symbols[child].id]
            moved[child] = replace(original, position=(original.position[0] + dx,
                                                       original.position[1] + dy))
        return moved

    def clear_exits(moved: dict[str, KiCadSymbol], obstacles: list[Envelope]) -> bool:
        local = {ref: moved.get(ref, raw[symbol.id]) for ref, symbol in symbols.items()
                 if groups[source[ref]] == groups[source[next(iter(moved))]]}
        bodies = {ref: envelope_from_points(placed_symbol_body_positions(editor.document, item))
                  for ref, item in moved.items()}
        strokes = [(ref, n, Envelope(min(a.x, b.x) - 250_000, min(a.y, b.y) - 250_000,
                                     max(a.x, b.x) + 250_000, max(a.y, b.y) + 250_000))
                   for ref, item in local.items()
                   for n, (a, b) in placed_pin_segments(editor.document, item).items()]
        for ref, item in local.items():
            sides = placed_pin_sides(editor.document, item)
            for n, pin in placed_pin_positions(editor.document, item).items():
                end = stub_endpoint(PlacedEndpoint(pin, sides[n])).position
                if (ref in moved and any(segment_hits_box(pin, end, box) for box in obstacles)
                        or any(segment_hits_box(pin, end, box) for peer, box in bodies.items()
                               if peer != ref)
                        or any(segment_hits_box(pin, end, box) for peer, number, box in strokes
                               if peer != ref and (peer in moved or ref in moved)
                               and nets.get((peer, number)) != nets.get((ref, n)))):
                    return False
        return True

    networks: dict[tuple[str, str, str], dict[str, list[str]]] = {}
    for ref in symbols:
        props = properties[ref]
        role = props.get("role")
        if (role not in {"series", "shunt"} or props.get("owner") not in symbols
                or not props.get("pin")):
            continue
        key = (props["owner"], props["group"], props["pin"])
        networks.setdefault(key, defaultdict(list))[role].append(ref)
    directions = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
    stub = round(PIN_STUB_MM * 1_000_000)
    for (owner, _, name), members in sorted(networks.items(), key=lambda item: (
            depth(item[0][0]), item[0])):
        if owner in composed or any(r in composed for refs in members.values() for r in refs):
            continue
        if len(members.get("series", ())) != 1 or len(members.get("shunt", ())) != 1:
            continue
        series, shunt = members["series"][0], members["shunt"][0]
        number = symbol_pin_number_groups(schematic["instances"][source[owner]])[name][0]
        net = nets[(owner, number)]
        owner_symbol = owner_symbol_for_pin(schematic, editor, owner, name)
        if owner_symbol is None:
            raise KiCadSchematicError(f"{owner}/{name}: missing drawn owner pin")
        owner_pin = placed_pin_positions(editor.document, raw[owner_symbol.id])[number]
        terminals = {ref: [n for (part, n), value in nets.items() if part == ref and value == net]
                     for ref in (series, shunt)}
        if any(len(numbers) != 1 for numbers in terminals.values()):
            continue
        series_pin, shunt_pin = terminals[series][0], terminals[shunt][0]
        shunt_raw, original = raw[symbols[shunt].id], raw[symbols[series].id]
        if any(len(placed_pin_positions(editor.document, s)) != 2
               for s in (shunt_raw, original)):
            continue
        anchor = placed_pin_positions(editor.document, shunt_raw)[shunt_pin]
        side = placed_pin_sides(editor.document, shunt_raw)[shunt_pin]
        dx, dy = directions[side]
        node = Vector2(anchor.x + dx * stub, anchor.y + dy * stub)
        # Prefer the open side away from the device, then try the other side.
        axes = sorted(((dy, -dx), (-dy, dx)), key=lambda v: -(
            v[0] * (anchor.x - owner_pin.x) + v[1] * (anchor.y - owner_pin.y)))
        moving_refs = {series, *descendants(series)}
        obstacles = [envelope_from_points(placed_symbol_body_positions(editor.document, s))
                     for ref, symbol in symbols.items()
                     if ref not in moving_refs and groups.get(source[ref]) == groups[source[series]]
                     for s in (raw[symbol.id],)]
        foreign_strokes = [segment for ref, symbol in symbols.items() if ref not in moving_refs
                           and groups.get(source[ref]) == groups[source[series]]
                           for n, segment in placed_pin_segments(
                               editor.document, raw[symbol.id]).items()
                           if nets.get((ref, n)) != net]
        candidates = []
        for preference, (sx, sy) in enumerate(axes):
            facing = next(s for s, direction in directions.items() if direction == (-sx, -sy))
            rotation = next(angle for angle in (0, 90, 180, 270)
                            if placed_pin_sides(editor.document, replace(
                                original, rotation=angle))[series_pin] == facing)
            rotated = replace(original, rotation=rotation)
            pin = placed_pin_positions(editor.document, rotated)[series_pin]
            body = envelope_from_points(placed_symbol_body_positions(editor.document, rotated))
            for step in range(8):
                target = Vector2(node.x + sx * stub * (step + 1),
                                 node.y + sy * stub * (step + 1))
                delta = Vector2(target.x - pin.x, target.y - pin.y)
                candidate = body.translated(delta)
                moved = attached_move(series, replace(rotated, position=(
                    rotated.position[0] + delta.x / 1e6,
                    rotated.position[1] + delta.y / 1e6)))
                boxes = {ref: envelope_from_points(placed_symbol_body_positions(
                    editor.document, item)) for ref, item in moved.items()}
                if (any(not envelopes_do_not_overlap(candidate, box, 635_000)
                        or segment_hits_box(node, target, box) for box in obstacles)
                        or any(not envelopes_do_not_overlap(box, obstacle, 635_000)
                               for box in boxes.values() for obstacle in obstacles)
                        or any(not envelopes_do_not_overlap(box, other, 635_000)
                               for ref, box in boxes.items() for peer, other in boxes.items()
                               if ref < peer)
                        or not clear_exits(moved, obstacles)
                        or any(segment_hits_box(a, b, box) for box in boxes.values()
                               for a, b in foreign_strokes)):
                    continue
                candidates.append((step, preference, moved))
                break
        if not candidates:
            raise KiCadSchematicError(f"{owner}/{name}: no clear series/shunt placement")
        _, _, moved = min(candidates, key=lambda item: (item[1], item[0]))
        for ref, candidate in moved.items():
            place_symbol(symbols[ref], Vector2.from_xy_mm(*candidate.position),
                              candidate.rotation)
        editor.update_items([symbols[ref] for ref in moved])
        raw = {s.uuid: s for s in editor.document.symbols}
