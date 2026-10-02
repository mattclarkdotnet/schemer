from __future__ import annotations

from collections import defaultdict
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
)
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.symbols.library import symbol_pin_number_groups


def place_pin_bridges(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Draw authored bridges around their unit, with parallel members in rows."""
    nets = expected_pin_nets(schematic)
    banks: dict[tuple[str, str, str], list[SchematicSymbolInstance]] = defaultdict(list)
    for part in editor.get_symbols():
        if part.zener_path is None:
            continue
        props = component_properties(schematic, schematic["root_ref"] + "." + part.zener_path)
        if props.get("role") == "pin-bridge":
            a, b = sorted((props["pin"], props["other_pin"]))
            banks[props["owner"], a, b].append(part)
    for (reference, a, b), parts in banks.items():
        owner = owner_symbol_for_pin(schematic, editor, reference, a)
        other = owner_symbol_for_pin(schematic, editor, reference, b)
        if owner is None or other is None or owner.id != other.id:
            continue
        raw = {s.uuid: s for s in editor.document.symbols}
        unit = raw[owner.id]
        aliases = symbol_pin_number_groups(schematic["instances"][
            schematic["root_ref"] + "." + owner.zener_path])
        positions, sides = placed_pin_positions(editor.document, unit), placed_pin_sides(
            editor.document, unit)
        numbers = [next(n for n in aliases[name] if n in positions) for name in (a, b)]
        face_pair = {sides[n] for n in numbers}
        same_face = len(face_pair) == 1
        if not same_face and face_pair not in ({"left", "right"}, {"top", "bottom"}):
            continue
        horizontal = (bool(face_pair & {"top", "bottom"}) if same_face
                      else face_pair == {"left", "right"})
        axis = "x" if horizontal else "y"
        first = (min(numbers, key=lambda n: getattr(positions[n], axis)) if same_face else
                 next(n for n in numbers if sides[n] == ("left" if horizontal else "top")))
        body = envelope_from_points(placed_symbol_body_positions(editor.document, unit))
        coordinate = positions[first].y if horizontal else positions[first].x
        center = body.center_y if horizontal else body.center_x
        direction = 1 if coordinate >= center else -1
        edge = (body.max_y if direction == 1 else body.min_y) if horizontal else (
            body.max_x if direction == 1 else body.min_x)
        if same_face:
            side = sides[first]
            direction = -1 if side in {"left", "top"} else 1
            edge = positions[first].y if horizontal else positions[first].x
            low, high = sorted(getattr(positions[n], axis) for n in numbers)
            if any(sides[n] == side and low < getattr(p, axis) < high
                   for n, p in positions.items() if n not in numbers):
                continue
        occupied = [envelope_from_points(placed_symbol_body_positions(editor.document, s))
                    for s in raw.values() if s.path is not None
                    and s.uuid not in {p.id for p in parts}]
        for row, part in enumerate(sorted(parts, key=lambda s: s.reference)):
            original = raw[part.id]
            terminal = next(n for n in placed_pin_positions(editor.document, original)
                            if nets[(part.reference, n)] == nets[(reference, first)])
            facing = "left" if horizontal else "top"
            rotation = next(angle for angle in (0, 90, 180, 270)
                            if placed_pin_sides(editor.document, replace(
                                original, rotation=angle))[terminal] == facing)
            rotated = replace(original, rotation=rotation)
            points = list(placed_pin_positions(editor.document, rotated).values())
            center = Vector2(round(sum(p.x for p in points) / len(points)),
                             round(sum(p.y for p in points) / len(points)))
            lane = edge + direction * (7_620_000 + row * 7_620_000)
            target = Vector2(body.center_x, lane) if horizontal else Vector2(lane, body.center_y)
            if same_face:
                if max(getattr(p, axis) for p in points) - min(
                        getattr(p, axis) for p in points) > high - low:
                    continue
                box = envelope_from_points(placed_symbol_body_positions(editor.document, rotated))
                for attempt in range(32):
                    lane = edge + direction * (5_080_000 + (row + attempt) * 7_620_000)
                    target = (Vector2((low + high) // 2, lane) if horizontal
                              else Vector2(lane, (low + high) // 2))
                    candidate = box.translated(Vector2(target.x - center.x, target.y - center.y))
                    if all(envelopes_do_not_overlap(candidate, obstacle, 635_000)
                           for obstacle in occupied):
                        occupied.append(candidate)
                        break
                else:
                    continue
            place_symbol(part, Vector2(part.position.x + target.x - center.x,
                                           part.position.y + target.y - center.y), rotation)
            editor.update_items(part)
