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
    union_all,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.geometry.text import text_envelope
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.native.routing_model import PIN_STUB_MM
from schemer.symbols.library import symbol_pin_number_groups


def pack_supported_units(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Separate completed unit/support envelopes, retaining one package axis."""
    symbols = list(editor.get_symbols())
    units: dict[str, list[SchematicSymbolInstance]] = defaultdict(list)
    for symbol in symbols:
        if symbol.zener_path is not None:
            units[symbol.reference].append(symbol)
    for reference, members in units.items():
        if len(members) < 2:
            continue
        attached = {s.id: [s] for s in members}
        by_reference = {s.reference: s for s in symbols if s.zener_path is not None}
        for part in symbols:
            if part.zener_path is None or part.reference == reference:
                continue
            ancestor = part
            seen = set()
            while ancestor.reference not in seen:
                seen.add(ancestor.reference)
                props = component_properties(
                    schematic, schematic["root_ref"] + "." + ancestor.zener_path)
                if props.get("owner") == reference and props.get("pin"):
                    owner = owner_symbol_for_pin(schematic, editor, reference, props["pin"])
                    if owner is not None:
                        attached[owner.id].append(part)
                    break
                ancestor = by_reference.get(props.get("owner"))
                if ancestor is None:
                    break
        bottom = None
        for member in sorted(members, key=lambda s: s.unit):
            raw = {s.uuid: s for s in editor.document.symbols}
            boxes = []
            for part in attached[member.id]:
                points = [*placed_symbol_body_positions(editor.document, raw[part.id]),
                          *placed_pin_positions(editor.document, raw[part.id]).values()]
                if points:
                    boxes.append(envelope_from_points(points))
                boxes.extend(envelope_from_points(text_envelope(
                    f.text, part.transform.orientation,
                )) for f in part.fields if f.visible and f.name in {"Reference", "Value"})
            box = union_all(boxes)
            shift = max(0, bottom + 7_620_000 - box.min_y) if bottom is not None else 0
            if shift:
                for part in attached[member.id]:
                    place_symbol(part, Vector2(part.position.x, part.position.y + shift),
                                      part.transform.orientation)
                editor.update_items(attached[member.id])
            bottom = box.max_y + shift


def align_single_pin_attachments(
    schematic: dict[str, Any], editor: FileSchematic, *, excluded: frozenset[str] = frozenset(),
) -> None:
    """Default an exclusive two-terminal attachment to its owner's pin axis."""

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    layout_groups, _ = component_layout_groups(schematic)
    groups = {ref: layout_groups.get(schematic["root_ref"] + "." + s.zener_path)
              for ref, s in symbols.items()}
    raw = {s.uuid: s for s in editor.document.symbols}
    pins: dict[str, dict[str, Vector2]] = defaultdict(dict)
    sides: dict[str, dict[str, str]] = defaultdict(dict)
    for unit in editor.document.symbols:
        if unit.path is not None:
            pins[unit.reference].update(placed_pin_positions(editor.document, unit))
            sides[unit.reference].update(placed_pin_sides(editor.document, unit))
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    inline = {ref for ref in symbols if len(pins[ref]) == 2
              and set(sides[ref].values()) in ({"left", "right"}, {"top", "bottom"})}
    by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for endpoint, net in expected_pin_nets(schematic).items():
        if endpoint[0] in symbols:
            by_net[net].append(endpoint)
    attachments: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    connected_devices: dict[str, set[str]] = defaultdict(set)
    rails = {net["name"] for net in schematic["nets"].values()
             if net.get("kind") in {"Power", "Ground"}}
    for net, members in by_net.items():
        for child, _ in members:
            if child in inline and net not in rails:
                connected_devices[child].update(ref for ref, _ in members if ref not in inline)
        if len(members) != 2:
            continue
        for (owner, number), (child, terminal) in (members, members[::-1]):
            if (owner != child and owner not in inline and child in inline
                    and groups[owner] == groups[child]):
                attachments[child].append((owner, number, terminal))

    # An authored attachment is unambiguous even when the far terminal also
    # connects to a device. Resolve its actual pin, not the nearest seed row.
    authored = set()
    net_by_pin = expected_pin_nets(schematic)
    for child in sorted(inline):
        props = component_properties(schematic, schematic["root_ref"] + "."
                                      + symbols[child].zener_path)
        owner = props.get("owner")
        if owner not in symbols or groups[owner] != groups[child] or not props.get("pin"):
            continue
        instance = schematic["instances"][schematic["root_ref"] + "."
                                           + symbols[owner].zener_path]
        numbers = symbol_pin_number_groups(instance).get(props["pin"], ())
        number = next((n for n in numbers if n in pins[owner]), None)
        if number is None:
            continue
        net = net_by_pin.get((owner, number))
        local_members = [(part, terminal) for part, terminal in by_net.get(net, [])
                         if groups[part] == groups[owner]]
        bridge_only = all(
            (part in {owner, child} or (
                (peer := component_properties(schematic, schematic["root_ref"] + "."
                                                + symbols[part].zener_path)).get("role")
                == "pin-bridge" and peer.get("owner") == owner))
            for part, _ in local_members
        )
        if len(local_members) != 2 and not (
                props.get("role") == "bypass" and net in rails
                or bridge_only and props.get("role") in {
                    "source-impedance", "series-termination", "current-limit", "ac-coupling"}):
            continue  # A shared node is a branch, not an exclusive inline attachment.
        terminals = [n for n in pins[child] if net is not None
                     and net_by_pin.get((child, n)) == net]
        if len(terminals) == 1:
            attachments[child] = [(owner, number, terminals[0])]
            authored.add(child)

    bounds = {}
    for ref, symbol in symbols.items():
        points = placed_symbol_body_positions(editor.document, raw[symbol.id])
        if points:
            bounds[ref] = Envelope(min(p.x for p in points), min(p.y for p in points),
                                    max(p.x for p in points), max(p.y for p in points))
    clearance = round(2 * PIN_STUB_MM * 1_000_000)
    for child, owners in attachments.items():
        if symbols[child].id in excluded:
            continue
        # A bridge between two devices has no unique local attachment owner.
        if len(owners) != 1 or (child not in authored and len(connected_devices[child]) > 1):
            continue
        owner, number, terminal = owners[0]
        symbol = symbols[child]
        ref = schematic["root_ref"] + "." + symbol.zener_path
        props = component_properties(schematic, ref)
        if (props.get("role") in {"divider", "pin-bridge"}
                or props.get("role") == "series" and not props.get("owner")
                or props.get("owner", owner) != owner):
            continue
        body = raw[symbol.id]
        side = sides[owner][number]
        rotation = next(
            angle for angle in (0, 90, 180, 270)
            if placed_pin_sides(editor.document, replace(body, rotation=angle))[terminal]
            == opposite[side]
        )
        rotated = replace(body, rotation=rotation)
        old_pin = placed_pin_positions(editor.document, rotated)[terminal]
        points = placed_symbol_body_positions(editor.document, rotated)
        if not points:
            continue
        box = Envelope(min(p.x for p in points), min(p.y for p in points),
                        max(p.x for p in points), max(p.y for p in points))
        dx, dy = {"left": (-1, 0), "right": (1, 0),
                  "top": (0, -1), "bottom": (0, 1)}[side]
        anchor = pins[owner][number]
        for step in range(32):
            span = clearance + step * clearance // 2
            delta = Vector2(anchor.x + dx * span - old_pin.x,
                            anchor.y + dy * span - old_pin.y)
            candidate = box.translated(delta)
            if any(not envelopes_do_not_overlap(candidate, other, clearance // 10)
                   for ref, other in bounds.items()
                   if ref != child and groups[ref] == groups[child]):
                continue
            place_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                              symbol.position.y + delta.y), rotation)
            editor.update_items(symbol)
            bounds[child] = candidate
            break
