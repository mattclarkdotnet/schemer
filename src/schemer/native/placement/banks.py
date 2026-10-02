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
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad.geometry.text import text_envelope
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.annotations.fields import position_component_fields
from schemer.native.placement.queries import bypass_capacitance, owner_symbol_for_pin
from schemer.symbols.library import symbol_pin_number_groups


def place_shared_rail_banks(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Compose an authored unowned shunt bank around its common rail.

    Group membership supplies intent; shared connectivity only chooses the
    orientation and ordering within that group. Owned bypasses stay at their IC.
    """
    groups, _ = component_layout_groups(schematic)
    nets = expected_pin_nets(schematic)
    rails = {n["name"] for n in schematic["nets"].values()
             if n.get("kind") in {"Power", "Ground"}}
    grounds = {n["name"] for n in schematic["nets"].values() if n.get("kind") == "Ground"}
    raw = {s.uuid: s for s in editor.document.symbols}
    members: dict[str, list[SchematicSymbolInstance]] = defaultdict(list)
    for s in editor.get_symbols():
        if s.zener_path is not None:
            members[groups[schematic["root_ref"] + "." + s.zener_path]].append(s)
    for symbols in members.values():
        props = [component_properties(schematic, schematic["root_ref"] + "." + s.zener_path)
                 for s in symbols]
        if len(symbols) < 2 or any(p.get("owner") or not p.get("group")
                                   or p.get("role") not in {"shunt", "bypass"} for p in props):
            continue
        terminals = {s.id: {pin: nets.get((s.reference, pin))
                            for pin in symbol_library_pins(editor.document, raw[s.id])}
                     for s in symbols}
        if any(len(pins) != 2 or None in pins.values() for pins in terminals.values()):
            continue
        common = rails.intersection(*(set(pins.values()) for pins in terminals.values()))
        if not common:
            continue
        rail = min(common, key=lambda name: (name not in grounds, name))
        side = "bottom" if rail in grounds else "top"
        x = min(s.position.x for s in symbols)
        y = min(s.position.y for s in symbols)
        for s in sorted(symbols, key=lambda s: (
                sorted(n for n in terminals[s.id].values() if n != rail), s.reference)):
            pin = next(n for n, net in terminals[s.id].items() if net == rail)
            rotation = next(a for a in (0, 90, 180, 270) if placed_pin_sides(
                editor.document, replace(raw[s.id], rotation=a))[pin] == side)
            p = placed_pin_positions(editor.document, replace(raw[s.id], rotation=rotation))[pin]
            place_symbol(s, Vector2(s.position.x + x - p.x, s.position.y + y - p.y), rotation)
            editor.update_items(s)
            s = position_component_fields(editor, [s])[0]
            editor.update_items(s)
            right = max((envelope_from_points(text_envelope(
                f.text, rotation)).max_x for f in s.fields
                         if f.visible and f.name in {"Reference", "Value"}), default=x)
            x = max(x + 10_160_000, right + 5_080_000)


def _stack_parallel_signal_bank(
    editor: FileSchematic, members: list[tuple[SchematicSymbolInstance, str]],
    anchor: Vector2, side: str, obstacles: list[Envelope],
) -> bool:
    """Keep both common nodes beside a stack, outside neighbouring series paths."""
    facing = {"left": "right", "right": "left"}[side]
    direction = -1 if side == "left" else 1
    raw = {s.uuid: s for s in editor.document.symbols}
    rotated = []
    for symbol, terminal in members:
        original = raw[symbol.id]
        angle = next(a for a in (0, 90, 180, 270) if placed_pin_sides(
            editor.document, replace(original, rotation=a))[terminal] == facing)
        item = replace(original, rotation=angle)
        pins = placed_pin_positions(editor.document, item)
        span = envelope_from_points([
            *placed_symbol_body_positions(editor.document, item), *pins.values()])
        rotated.append((symbol, angle, pins[terminal], span))
    pitch = max(7_620_000, max(box.max_y - box.min_y for _, _, _, box in rotated)
                + 2_540_000)
    for attempt in range(32):
        x = anchor.x + direction * (7_620_000 + attempt * 5_080_000)
        plan = []
        for row, (symbol, angle, pin, span) in enumerate(rotated):
            delta = Vector2(x - pin.x, anchor.y + 5_080_000 + row * pitch - pin.y)
            plan.append((symbol, angle, delta, span.translated(delta)))
        # Test the complete two-trunk corridor, not just the individual bodies.
        corridor = envelope_from_points([
            p for _, _, _, box in plan for p in (
                Vector2(box.min_x, box.min_y), Vector2(box.max_x, box.max_y))])
        if any(not envelopes_do_not_overlap(corridor, box, 2_540_000)
               for box in obstacles):
            continue
        for symbol, angle, delta, _ in plan:
            place_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                        symbol.position.y + delta.y), angle)
        editor.update_items([s for s, _, _, _ in plan])
        return True
    return False


def place_owned_shunt_banks(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Place authored parallel support at its owner, not at a shared rail bank.

    Direct-pin banks branch outside that pin face. A network's downstream
    shunts follow its authored series member. Other same-net parts are not
    candidates for ownership or attachment.
    """

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    nets = expected_pin_nets(schematic)
    rails = {net["name"] for net in schematic["nets"].values()
             if net.get("kind") in {"Power", "Ground"}}
    props = {ref: component_properties(schematic, schematic["root_ref"] + "." + s.zener_path)
             for ref, s in symbols.items()}
    banks: dict[tuple[str, str, str, str, str | None], list[tuple[str, str]]] = defaultdict(list)
    for ref, attributes in props.items():
        owner = attributes.get("owner")
        if attributes.get("role") not in {"bypass", "shunt"} or owner not in symbols:
            continue
        owner_ref = schematic["root_ref"] + "." + symbols[owner].zener_path
        owner_source = schematic["instances"][owner_ref]
        pins = symbol_pin_number_groups(owner_source).get(attributes.get("pin"), ())
        attached = next((nets.get((owner, n)) for n in pins if (owner, n) in nets), None)
        if attached is None and attributes.get("at"):
            attached = next((net for (part, _), net in nets.items() if part == ref
                             and net.rsplit(".", 1)[-1] == attributes["at"]), None)
        terminals = [(n, net) for (part, n), net in nets.items() if part == ref]
        connected = [n for n, net in terminals if net == attached]
        returned = [net for _, net in terminals if net != attached]
        if len(connected) == len(returned) == 1:
            banks[(owner, attributes["group"], attached, returned[0],
                   attributes.get("pin"))].append((ref, connected[0]))
    for (owner, group, attached, returned, declared), members in banks.items():
        # Single signal-to-rail shunts still need ownership-based placement:
        # the exclusive pin-axis pass skips their shared signal nodes. Keep
        # bypasses and signal-to-signal supports with their existing composers.
        lone_rail_shunt = (len(members) == 1 and returned in rails
                           and attached not in rails
                           and props[members[0][0]]["role"] == "shunt"
                           and not any(p.get("owner") == members[0][0]
                                       for p in props.values()))
        if (len(members) == 1 and props[members[0][0]].get("pin")
                and not lone_rail_shunt
                and (attached not in rails or props[members[0][0]]["role"] == "bypass")):
            continue
        anchor_symbol = symbols[owner]
        anchor_numbers = [n for (part, n), net in nets.items() if part == owner and net == attached]
        if not anchor_numbers:
            peers = [ref for ref, attributes in props.items()
                     if attributes.get("owner") == owner and attributes.get("group") == group
                     and attributes.get("role") == "series"]
            candidates = [(ref, n) for (ref, n), net in nets.items()
                          if ref in peers and net == attached]
            if len(candidates) != 1:
                raise KiCadSchematicError(
                    f"{owner}/{group}: shunt node lacks a unique series attachment"
                )
            source_ref, number = candidates[0]
            anchor_symbol = symbols[source_ref]
            anchor_numbers = [number]
        # Authored pin wins over another same-net control pin on this owner.
        if declared and anchor_symbol.reference == owner:
            anchor_symbol = owner_symbol_for_pin(schematic, editor, owner, declared)
            if anchor_symbol is None:
                raise KiCadSchematicError(f"{owner}/{declared}: missing drawn owner pin")
            instance = schematic["instances"][
                schematic["root_ref"] + "." + anchor_symbol.zener_path
            ]
            anchor_numbers = list(symbol_pin_number_groups(instance)[declared])
        anchor_raw = raw[anchor_symbol.id]
        point = placed_pin_positions(editor.document, anchor_raw)[anchor_numbers[0]]
        side = placed_pin_sides(editor.document, anchor_raw)[anchor_numbers[0]]
        if lone_rail_shunt:
            ref, terminal = members[0]
            existing = placed_pin_positions(editor.document, raw[symbols[ref].id])[terminal]
            outward = {"left": existing.x < point.x, "right": existing.x > point.x,
                       "top": existing.y < point.y, "bottom": existing.y > point.y}[side]
            if outward:
                # It may already sit with another member of the input network.
                # Correct the wrong face, not every existing local arrangement.
                continue
        direction = -1 if side == "left" else 1
        member_ids = {symbols[ref].id for ref, _ in members}
        obstacles = [envelope_from_points(placed_symbol_body_positions(editor.document, s))
                     for s in raw.values() if s.uuid not in member_ids and s.path is not None]
        if (len(members) > 1 and side in {"left", "right"}
                and attached not in rails and returned not in rails
                and all(props[ref]["role"] == "shunt" for ref, _ in members)
                and _stack_parallel_signal_bank(
                    editor, [(symbols[ref], pin) for ref, pin in sorted(members)],
                    point, side, obstacles)):
            raw = {s.uuid: s for s in editor.document.symbols}
            continue
        cursor = point.x + direction * 7_620_000
        for ref, terminal in sorted(members, key=lambda item: (
                bypass_capacitance(schematic["instances"][schematic["root_ref"] + "."
                    + symbols[item[0]].zener_path])
                if props[item[0]].get("role") == "bypass" else float("inf"), item[0])):
            symbol = symbols[ref]
            original = raw[symbol.id]
            facing = ({"left": "right", "right": "left"}[side]
                      if lone_rail_shunt and side in {"left", "right"} else "top")
            rotation = next(angle for angle in (0, 90, 180, 270)
                            if placed_pin_sides(editor.document, replace(
                                original, rotation=angle))[terminal] == facing)
            rotated = replace(original, rotation=rotation)
            pin = placed_pin_positions(editor.document, rotated)[terminal]
            body = envelope_from_points(placed_symbol_body_positions(editor.document, rotated))
            positions = placed_pin_positions(editor.document, rotated).values()
            pin_span = max(p.y for p in positions) - min(p.y for p in positions)
            # Reserve actual caption width alongside each vertical member.
            width = max((len(f.text.value) * f.text.attributes.size.x
                         for f in symbol.fields if f.name in {"Reference", "Value"}), default=0)
            for attempt in range(32):
                x = cursor + direction * attempt * 5_080_000
                y = point.y
                if side == "top":
                    # The complete bank, including its return, belongs above
                    # a north-facing supply. A positive fixed offset undid
                    # the earlier bypass placement and occupied signal rows.
                    y -= pin_span + 5_080_000
                elif side == "bottom":
                    y += 5_080_000
                delta = Vector2(x - pin.x, y - pin.y)
                candidate = body.translated(delta)
                if any(not envelopes_do_not_overlap(candidate, other, 2_540_000)
                       for other in obstacles):
                    continue
                place_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                                  symbol.position.y + delta.y), rotation)
                editor.update_items(symbol)
                obstacles.append(candidate)
                cursor = x + direction * max(10_160_000, width + 5_080_000)
                break
        raw = {s.uuid: s for s in editor.document.symbols}
