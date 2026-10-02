from __future__ import annotations

from dataclasses import replace

from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
    rotated_envelope,
)
from schemer.kicad.geometry.text import (
    label_envelope,
    text_envelope,
)
from schemer.kicad.items import (
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.annotations.rails import compatible_rail_members, rail_approach_side
from schemer.native.annotations.signal_topology import nearest_local_target
from schemer.native.annotations.symbols import label_approach_side
from schemer.native.model import NetSymbolTarget
from schemer.native.routing import preview_clear_route, stub_endpoint
from schemer.native.routing_model import PIN_STUB_MM, PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box


def clear_signal_stub_corridors(
    targets: list[NetSymbolTarget],
    endpoints: dict[str, list[PlacedEndpoint]],
    glyph_bounds: dict[bool, Envelope] | None = None,
    body_obstacles: dict[str, list[Envelope]] | None = None,
    annotation_obstacles: dict[str, list[Envelope]] | None = None,
) -> list[NetSymbolTarget]:
    """Place rail trunks beyond the full width of unrelated signal labels."""

    step = round(PIN_STUB_MM * 1_000_000)
    result = list(targets)
    # Process the row a glyph points towards first. Later terminations then
    # clear its final wire length rather than its superseded short stub.
    order = sorted(range(len(targets)), key=lambda i: (
        targets[i].ground,
        -targets[i].position.y if targets[i].ground else targets[i].position.y,
    ))
    for index in order:
        target = result[index]
        if not target.rail:
            continue
        local_targets = [other for other in result if other.rail
                         and other.net_name == target.net_name and other.group == target.group]
        members = [p for p in endpoints.get(target.net_name, []) if p.group == target.group
                   and local_targets[nearest_local_target(p, local_targets)] is target]
        # The shared corridor starts outside each component's pin escape.
        # Its own terminal/body is not an obstacle across that access ray.
        ys = [target.position.y, *(stub_endpoint(p).position.y for p in members)]
        x = target.position.x
        glyph = (glyph_bounds or {}).get(target.ground, Envelope(0, 0, 0, 0))
        boxes = [
            (other, envelope_from_points(label_envelope(Text(
                other.display_name, other.position,
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                               other.rotation, other.text_alignment,
                               "center" if other.required else "bottom"),
            )))) for other in result if not other.rail
            and other.net_name != target.net_name and other.group == target.group
        ]
        boxes.extend((other, rotated_envelope((glyph_bounds or {}).get(
            other.ground, Envelope(0, 0, 0, 0)), other.rotation).translated(other.position))
            for other_index, other in enumerate(result)
            if other_index != index and other.rail and other.group == target.group)
        # The complete glyph must clear conductors too, including a label's
        # extended stub. Text clearance alone misses tips touching the next row.
        side = members[0].side if members else "right"
        boxes.extend((replace(target, text_alignment=(
            "right" if side == "left" else "left"
        )), box) for box in (body_obstacles or {}).get(target.group, []))
        # A shared rail can point back into one of its own pin strokes, just
        # as a lone termination can. Electrical sameness does not permit the
        # glyph to cover that painted pin or block its outward escape.
        boxes.extend((target, p.stroke) for p in members if p.stroke is not None)
        for name, pins in endpoints.items():
            if name == target.net_name:
                continue
            for pin in pins:
                if pin.group != target.group:
                    continue
                end = stub_endpoint(pin).position
                # Reserve the real pin exit, not the rectangular area between
                # a pin and an off-row flag. Actual orthogonal routes below
                # reserve their strokes without blocking that whole empty area.
                box = Envelope(min(pin.position.x, end.x) - 250_000,
                                min(pin.position.y, end.y) - 250_000,
                                max(pin.position.x, end.x) + 250_000,
                                max(pin.position.y, end.y) + 250_000)
                boxes.append((replace(target, text_alignment=(
                    "right" if side == "left" else "left"
                )), box))
        # Shared local nets can extend far beyond their pin stubs. Include
        # those planned conductors, not only the short terminal approaches.
        for name, pins in endpoints.items():
            local_pins = [p for p in pins if p.group == target.group]
            foreign_targets = [t for t in result if t.net_name == name
                               and t.group == target.group]
            routes: list[list[PlacedEndpoint]] = []
            if foreign_targets:
                for foreign_index, other in enumerate(foreign_targets):
                    if name == target.net_name and (other is target or
                            compatible_rail_members([*target.members, *other.members])):
                        continue
                    selected = [p for p in local_pins
                                if nearest_local_target(p, foreign_targets) == foreign_index]
                    routes.append([*selected, PlacedEndpoint(other.position,
                        rail_approach_side(other) if other.rail else
                        label_approach_side(other) if other.required else None,
                        escape_length=635_000 if other.rail or other.required else None)])
            else:
                routes.append(local_pins)
            for route in routes:
                local_obstacles = (body_obstacles or {}).get(target.group, [])
                for wire in preview_clear_route(route, local_obstacles):
                    if isinstance(wire, SchematicLine):
                        boxes.append((replace(target, text_alignment=(
                            "right" if side == "left" else "left"
                        )), envelope_from_points((wire.start, wire.end))))
        # Match final routing's clearance around foreign labels and strokes.
        # A clear glyph is insufficient if its mandatory approach is blocked.
        boxes = [(other, Envelope(box.min_x - 250_000, box.min_y - 250_000,
                                  box.max_x + 250_000, box.max_y + 250_000))
                 for other, box in boxes]
        # A short horizontal rail glyph can fit between adjacent pin rows
        # without moving the node or lengthening either wire. Try that before
        # pushing its corridor beyond a whole bank of labels.
        def caption_fits(candidate: NetSymbolTarget, own_wires=()) -> bool:
            body = rotated_envelope(glyph, candidate.rotation).translated(candidate.position)
            occupied = [body, *(b for _, b in boxes),
                        *(annotation_obstacles or {}).get(candidate.group, []),
                        *(envelope_from_points((w.start, w.end)) for w in own_wires
                          if isinstance(w, SchematicLine))]
            for angle in (0, 90):
                text = Text(candidate.display_name, Vector2(0, 0),
                            TextAttributes(Vector2.from_xy_mm(1.27, 1.27), angle,
                                           "center", "center"))
                original = envelope_from_points(text_envelope(text))
                for x, y in ((body.min_x - 1_270_000 - original.width // 2, body.center_y),
                             (body.max_x + 1_270_000 + original.width // 2, body.center_y),
                             (body.center_x, body.min_y - 1_270_000 - original.height // 2),
                             (body.center_x, body.max_y + 1_270_000 + original.height // 2)):
                    proposed = original.translated(Vector2(x, y))
                    if all(envelopes_do_not_overlap(proposed, b, 635_000) for b in occupied):
                        return True
            return False

        if (glyph_bounds and target.ground in glyph_bounds and len(members) == 1
                and (any(not envelopes_do_not_overlap(
                    glyph.translated(target.position), box, 250_000) for _, box in boxes)
                     or any(segment_hits_box(target.position, stub_endpoint(PlacedEndpoint(
                         target.position, rail_approach_side(target),
                         escape_length=635_000)).position,
                         box) for _, box in boxes)
                     or any(p.stroke and not envelopes_do_not_overlap(
                         glyph.translated(target.position), p.stroke, 0) for p in members)
                     or not caption_fits(target))):
            pin = members[0]
            dx, dy = {"left": (-1, 0), "right": (1, 0),
                      "top": (0, -1), "bottom": (0, 1)}[side]
            choices = []
            for length in (1_270_000, 2_540_000, 3_810_000, 5_080_000, 7_620_000,
                           10_160_000, 15_240_000, 20_320_000, 30_480_000):
                point = Vector2(pin.position.x + dx * length, pin.position.y + dy * length)
                alternatives = [(point, angle) for angle in (0, 90, 270)]
                if dx:
                    alternatives.insert(0, (Vector2(point.x, point.y + (
                        step if target.ground else -step)), 0))
                for point, angle in alternatives:
                    candidate = replace(target, position=point, rotation=angle)
                    box = rotated_envelope(glyph, angle).translated(point)
                    if any(not envelopes_do_not_overlap(box, other, 250_000)
                           for _, other in boxes) or (pin.stroke is not None and
                           not envelopes_do_not_overlap(box, pin.stroke, 250_000)):
                        continue
                    own_body = Envelope(
                        box.min_x + (1 if box.min_x == point.x else 0),
                        box.min_y + (1 if box.min_y == point.y else 0),
                        box.max_x - (1 if box.max_x == point.x else 0),
                        box.max_y - (1 if box.max_y == point.y else 0))
                    route = preview_clear_route([pin, PlacedEndpoint(point,
                        rail_approach_side(candidate), escape_length=635_000)],
                        [own_body, *(body_obstacles or {}).get(target.group, [])])
                    if not route or any(segment_hits_box(w.start, w.end, other)
                           for w in route if isinstance(w, SchematicLine) for _, other in boxes):
                        continue
                    if not caption_fits(candidate, route):
                        continue
                    choices.append((angle != 0, len(route), point != target.position, length,
                                    candidate))
            if choices:
                result[index] = min(choices, key=lambda c: c[:4])[4]
                continue
        rotation = target.rotation
        if (side in {"left", "right"} and any(not envelopes_do_not_overlap(
                glyph.translated(target.position), box, step // 4) for _, box in boxes)):
            angle = (90 if side == "left" else 270) if not target.ground else (
                270 if side == "left" else 90)
            turned = rotated_envelope(glyph, angle)
            if all(envelopes_do_not_overlap(turned.translated(target.position), box, step // 4)
                   for _, box in boxes):
                rotation, glyph = angle, turned
        for _ in range(len(boxes) + 1):
            conflict = next((
                (other, box) for other, box in boxes
                if (box.min_x - step // 4 <= x <= box.max_x + step // 4
                    and min(ys) <= box.max_y and box.min_y <= max(ys))
                or not envelopes_do_not_overlap(
                    glyph.translated(Vector2(x, target.position.y)), box, step // 4,
                )
            ), None)
            if conflict is None:
                break
            other, box = conflict
            x = (box.min_x - step - max(0, glyph.max_x)
                 if side == "left" or (side != "right" and other.text_alignment == "right")
                 else box.max_x + step - min(0, glyph.min_x))
        result[index] = replace(target, position=Vector2(x, target.position.y), rotation=rotation)
    return result
