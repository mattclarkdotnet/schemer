from __future__ import annotations

from collections import defaultdict
from typing import Any

from schemer.analysis.circuits import (
    component_layout_groups,
    component_properties,
)
from schemer.analysis.connectivity import expected_pin_nets
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelopes_do_not_overlap,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.native.routing_model import PIN_STUB_MM


def compact_inline_connections(
    schematic: dict[str, Any], editor: FileSchematic, *, excluded: frozenset[str] = frozenset(),
) -> None:
    """Shorten existing straight attachments without choosing new owners or roles."""

    symbols = list(editor.get_symbols())
    raw = {symbol.uuid: symbol for symbol in editor.document.symbols}
    pin_nets = expected_pin_nets(schematic)
    layout_groups, _ = component_layout_groups(schematic)
    pins = {s.id: placed_pin_positions(editor.document, raw[s.id]) for s in symbols
            if s.zener_path is not None}
    sides = {s.id: placed_pin_sides(editor.document, raw[s.id]) for s in symbols
             if s.id in pins}
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    for symbol in symbols:
        if symbol.id in excluded or symbol.id not in pins or len(pins[symbol.id]) != 2:
            continue
        source = schematic["root_ref"] + "." + symbol.zener_path
        props = component_properties(schematic, source)
        if props.get("role") in {"series", "divider"}:
            continue
        candidates = []
        for owner in symbols:
            if (owner.id not in pins or owner.id == symbol.id
                    or (len(pins[owner.id]) <= 2 and owner.reference != props.get("owner"))):
                continue
            owner_group = layout_groups.get(schematic["root_ref"] + "." + owner.zener_path)
            part_group = layout_groups.get(source)
            if owner_group != part_group:
                continue
            for number, a in pins[owner.id].items():
                net = pin_nets.get((owner.reference, number))
                if net is None:
                    continue
                side = sides[owner.id][number]
                for terminal, b in pins[symbol.id].items():
                    if (pin_nets.get((symbol.reference, terminal)) != net
                            or sides[symbol.id][terminal] != opposite[side]):
                        continue
                    horizontal = side in {"left", "right"}
                    if (a.y != b.y if horizontal else a.x != b.x):
                        continue
                    direction = -1 if side in {"left", "top"} else 1
                    span = direction * (b.x - a.x if horizontal else b.y - a.y)
                    if span > 0:
                        candidates.append((span, owner, a, b, side))
        if not candidates:
            continue
        span, owner, a, b, side = min(candidates, key=lambda item: item[0])
        gap = round(2 * PIN_STUB_MM * 1_000_000)
        if span <= gap:
            continue
        direction = -1 if side in {"left", "top"} else 1
        horizontal = side in {"left", "right"}
        delta = Vector2(direction * (gap - span) if horizontal else 0,
                        0 if horizontal else direction * (gap - span))
        # Check the destination, not the swept path of an imaginary drag.
        # A symbol can move past an obstacle into clear space on its existing
        # wire axis. Captions and the shortened wires are rebuilt afterwards.
        points = placed_symbol_body_positions(editor.document, raw[symbol.id])
        if not points:
            continue
        clearance = round(0.5 * 1_000_000)
        candidate = Envelope(
            min(p.x for p in points) + delta.x - clearance,
            min(p.y for p in points) + delta.y - clearance,
            max(p.x for p in points) + delta.x + clearance,
            max(p.y for p in points) + delta.y + clearance,
        )
        blocked = False
        for other in symbols:
            if other.id in {symbol.id, owner.id} or other.zener_path is None:
                continue
            points = placed_symbol_body_positions(editor.document, raw[other.id])
            if points and not envelopes_do_not_overlap(candidate, Envelope(
                min(p.x for p in points), min(p.y for p in points),
                max(p.x for p in points), max(p.y for p in points),
            ), 0):
                blocked = True
                break
        if not blocked:
            place_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                              symbol.position.y + delta.y),
                              symbol.transform.orientation)
            editor.update_items(symbol)
            raw[symbol.id] = next(
                item for item in editor.document.symbols if item.uuid == symbol.id
            )
            pins[symbol.id] = placed_pin_positions(editor.document, raw[symbol.id])


def align_authored_inline_banks(
    schematic: dict[str, Any], editor: FileSchematic, *, excluded: frozenset[str] = frozenset(),
) -> dict[str, tuple[str, str, str, str]]:
    """Align repeated inline roles on one owner face, preserving pin rows."""

    symbols = list(editor.get_symbols())
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    banks: dict[tuple[str, str, str, str], list[SchematicSymbolInstance]] = defaultdict(list)
    for symbol in symbols:
        if symbol.zener_path is None or symbol.id in excluded:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        properties = component_properties(schematic, ref)
        if properties.get("role") not in {"series-termination", "current-limit",
                                          "ac-coupling", "source-impedance", "gain-setting",
                                          "pullup", "pulldown"}:
            continue
        owner = owner_symbol_for_pin(schematic, editor, properties.get("owner"),
                                      properties.get("pin"))
        if owner is None:
            continue
        sides = set(placed_pin_sides(editor.document, raw_by_id[symbol.id]).values())
        if sides != {"left", "right"}:
            continue
        side = "left" if symbol.position.x < owner.position.x else "right"
        role = ("bias" if properties["role"] in {"pullup", "pulldown"}
                else properties["role"])
        banks[(owner.id, role, properties["group"], side)].append(symbol)
    bank_ids = {}
    for key, members in banks.items():
        if len(members) < 2:
            continue
        x = (min if key[3] == "left" else max)(member.position.x for member in members)
        for member in members:
            place_symbol(member, Vector2(x, member.position.y), member.transform.orientation)
            bank_ids[member.id] = key
        editor.update_items(members)
    return bank_ids
