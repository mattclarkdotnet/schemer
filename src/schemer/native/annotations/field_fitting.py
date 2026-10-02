from __future__ import annotations

from schemer.core.diagnostics import record_draft_issue
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import (
    dnp_caption_obstacle,
    pin_name_envelopes,
    pin_number_envelopes,
    pin_stroke_envelopes,
)
from schemer.kicad.geometry.envelopes import (
    Envelope,
    box_distance,
    envelope_from_points,
    envelopes_do_not_overlap,
    union_all,
)
from schemer.kicad.geometry.library import placed_symbol_body_positions
from schemer.kicad.geometry.text import (
    caption_bank_axis,
    label_envelope,
    oriented_field_text,
    text_envelope,
)
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
)
from schemer.kicad.syntax import sexpr_atom
from schemer.native.policy import LOG


def _rail_caption_offsets(
    original: Envelope, body: Envelope, gap: int,
    obstacles: tuple[tuple[Envelope, int], ...] = (),
) -> list[Vector2]:
    """Offer positions tied to the glyph, never a free-floating text search."""

    candidates = []
    if box_distance(original, body) <= gap:
        candidates.append(Vector2(0, 0))
    for y in (body.center_y, body.max_y, body.min_y):
        candidates.extend([
            Vector2(body.max_x + gap - original.min_x, y - original.center_y),
            Vector2(body.min_x - gap - original.max_x, y - original.center_y),
        ])
    for y in (body.min_y - gap // 2 - original.max_y,
              body.max_y + gap // 2 - original.min_y):
        for dx in (0, gap // 2, -gap // 2, gap, -gap):
            candidates.append(Vector2(body.center_x + dx - original.center_x, y))
    # Try the actual edges of nearby obstacles, not just three fixed glyph
    # heights. Keep the caption adjacent to its own glyph.
    adjusted = []
    for candidate in candidates:
        box = original.translated(candidate)
        for obstacle, clearance in obstacles:
            if envelopes_do_not_overlap(box, obstacle, clearance):
                continue
            for shift in (
                Vector2(obstacle.min_x - clearance - box.max_x, 0),
                Vector2(obstacle.max_x + clearance - box.min_x, 0),
                Vector2(0, obstacle.min_y - clearance - box.max_y),
                Vector2(0, obstacle.max_y + clearance - box.min_y),
            ):
                moved = box.translated(shift)
                if box_distance(moved, body) <= 3 * gap:
                    adjusted.append(Vector2(candidate.x + shift.x, candidate.y + shift.y))
    unique = {(p.x, p.y): p for p in adjusted}
    candidates.extend(sorted(unique.values(), key=lambda p: (
        box_distance(original.translated(p), body), abs(p.x) + abs(p.y), p.x, p.y,
    )))
    local = []
    for base in candidates[:]:
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                moved = Vector2(base.x + dx * gap, base.y + dy * gap)
                box = original.translated(moved)
                if box_distance(box, body) <= 3 * gap:
                    local.append(moved)
    candidates.extend(sorted({(p.x, p.y): p for p in local}.values(), key=lambda p: (
        box_distance(original.translated(p), body), abs(p.x) + abs(p.y))))
    return candidates


def resolve_symbol_field_overlaps(
    editor: FileSchematic,
    components: list[SchematicSymbolInstance],
    *,
    grid_mm: float = 1.27,
    clearance_mm: float = 0.25,
) -> list[SchematicSymbolInstance]:
    """Move all symbol captions last, without disturbing electrical geometry."""

    symbols, failed = _fit_symbol_fields(editor, components, grid_mm, clearance_mm)
    priority = frozenset()
    while failed and not {s.id for s in failed} <= priority:
        priority = priority | frozenset(s.id for s in failed)
        retried, unresolved = _fit_symbol_fields(
            editor, components, grid_mm, clearance_mm, priority,
        )
        if len(unresolved) >= len(failed):
            break
        symbols, failed = retried, unresolved
    for component in failed:
        LOG.warning("No clear caption position for %s (%s)",
                     component.reference, component.value)
        record_draft_issue("caption-overlap", "No clear caption position",
                           (component.reference, component.value))
    return symbols


def _fit_symbol_fields(
    editor: FileSchematic, components: list[SchematicSymbolInstance],
    grid_mm: float, clearance_mm: float, priority_ids: frozenset[str] = frozenset(),
) -> tuple[list[SchematicSymbolInstance], list[SchematicSymbolInstance]]:
    """Allocate captions speculatively; only the chosen allocation is reported.

    A later constrained name can be stranded by an earlier movable caption.
    The caller may retry with those names first, using the same geometry and
    ownership constraints instead of changing the circuit to fit greedy text.
    """

    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    component_ids = {component.id for component in components}
    body_envelopes: dict[str, Envelope] = {}
    fixed: list[Envelope] = []
    symbols = list(editor.get_symbols())
    failed = []

    for symbol in symbols:
        raw = raw_by_id[symbol.id]
        dnp = dnp_caption_obstacle(editor, raw)
        if dnp is not None:
            fixed.append(dnp)
        points = placed_symbol_body_positions(editor.document, raw)
        if points:
            envelope = envelope_from_points(points)
            fixed.append(envelope)
            body_envelopes[symbol.id] = envelope
            # Reserve each pin stroke separately from its number, whose font
            # can extend well beyond a fixed one-millimetre stroke margin.
            fixed.extend(pin_stroke_envelopes(editor, raw).values())
            fixed.extend(pin_number_envelopes(editor, raw))
            fixed.extend(pin_name_envelopes(editor, raw))
    for marker in editor.get_no_connects():
        half_size = 635_000
        fixed.append(Envelope(
            marker.position.x - half_size, marker.position.y - half_size,
            marker.position.x + half_size, marker.position.y + half_size,
        ))
    for junction in editor.document.junctions:
        diameter = junction.expression.first_list("diameter")
        size_mm = (
            0 if diameter is None else float(sexpr_atom(diameter, 1, "junction diameter").value)
        )
        # A zero diameter selects KiCad's default 0.9144 mm painted dot.
        radius = round((size_mm if size_mm > 0 else 0.9144) * 500_000)
        position = Vector2.from_xy_mm(*junction.position)
        fixed.append(Envelope(
            position.x - radius, position.y - radius,
            position.x + radius, position.y + radius,
        ))
    for label in editor.get_labels():
        fixed.append(envelope_from_points(label_envelope(label.text)))
    wire_half_width = round(0.15 * 1_000_000)
    wire_envelopes = []
    for line in editor.get_lines():
        wire_envelopes.append(Envelope(
            min(line.start.x, line.end.x) - wire_half_width,
            min(line.start.y, line.end.y) - wire_half_width,
            max(line.start.x, line.end.x) + wire_half_width,
            max(line.start.y, line.end.y) + wire_half_width,
        ))
    fixed.extend(wire_envelopes)

    grid = round(grid_mm * 1_000_000)
    clearance = round(clearance_mm * 1_000_000)
    offsets = sorted(
        (
            Vector2(dx * grid, dy * grid)
            for radius in range(9)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if max(abs(dx), abs(dy)) == radius
        ),
        key=lambda offset: (abs(offset.x) + abs(offset.y), abs(offset.y), offset.x, offset.y),
    )
    placed_fields: list[Envelope] = []
    # Give the most constrained rail glyph first choice. Otherwise an open
    # neighbour can occupy its only nearby caption slot and strand its label.
    rail_choices = {}
    for symbol in symbols:
        if symbol.id in component_ids or symbol.id not in body_envelopes:
            continue
        boxes = [envelope_from_points(text_envelope(field.text, symbol.transform.orientation))
                 for field in symbol.fields
                 if field.visible and field.name in {"Reference", "Value"}]
        if not boxes:
            continue
        original = union_all(boxes)
        wire_gap = max(clearance, round(0.75 * original.height))
        rail_choices[symbol.id] = sum(
            all(envelopes_do_not_overlap(box.translated(offset), obstacle, clearance)
                for box in boxes for obstacle in fixed)
            and all(envelopes_do_not_overlap(box.translated(offset), wire, wire_gap)
                    for box in boxes for wire in wire_envelopes)
            for offset in _rail_caption_offsets(
                original, body_envelopes[symbol.id], grid,
                tuple((box, clearance) for box in fixed)
                + tuple((box, wire_gap) for box in wire_envelopes),
            )
        )
    ordered = sorted(
        symbols,
        key=lambda component: (
            component.id not in priority_ids,
            # Bank captions have one axis of freedom; other captions yield.
            not (component.id in component_ids and caption_bank_axis(component)),
            # Component identifiers stay near their bodies before placing rail names.
            component.id not in component_ids,
            rail_choices.get(component.id, 0),
            -body_envelopes.get(
                component.id,
                Envelope(
                    component.position.x,
                    component.position.y,
                    component.position.x,
                    component.position.y,
                ),
            ).width,
            component.reference,
        ),
    )
    for component in ordered:
        fields = [
            field
            for field in component.fields
            if field.visible and field.name in {"Reference", "Value"}
        ]
        if not fields:
            continue
        field_boxes = [
            envelope_from_points(
                text_envelope(field.text, component.transform.orientation)
            )
            for field in fields
        ]
        original = union_all(field_boxes)
        reference_box = next((box for field, box in zip(fields, field_boxes, strict=True)
                              if field.name == "Reference"), original)
        obstacles = [*fixed, *placed_fields]
        body = body_envelopes.get(component.id, original)
        caption_clearance = max(clearance, round(0.75 * max(
            field.text.attributes.size.y for field in fields
        )))
        candidates = list(offsets)
        edge_positions: list[Vector2] = []
        gap = round(1.27 * 1_000_000)
        if component.id in component_ids:
            # Four ordinary title positions, each tied to a body edge. Keep
            # a long device title beside its own body instead of searching
            # outward from an already displaced caption.
            candidates.extend([
                Vector2(body.max_x + gap - original.min_x, body.min_y + gap - original.min_y),
                Vector2(body.min_x - gap - original.max_x, body.min_y + gap - original.min_y),
                Vector2(body.max_x + gap - original.min_x, body.center_y - original.center_y),
                Vector2(body.min_x - gap - original.max_x, body.center_y - original.center_y),
                Vector2(body.center_x - original.center_x, body.max_y + gap - original.min_y),
            ])
            # Slide a title along the actual body edge before detaching it at
            # a corner. Side candidates align with the body, not its pin box.
            for x in (body.min_x, body.max_x - original.width, body.center_x - original.width // 2):
                candidates.append(Vector2(x - original.min_x, body.min_y - gap - original.max_y))
            edge_positions = candidates[-8:]
            # A title moved to another body edge may still need to slide
            # along that edge. Searching x-only or y-only from its original
            # side misses this ordinary two-axis move in a dense circuit.
            for candidate in edge_positions:
                shifted = original.translated(candidate)
                for obstacle in obstacles:
                    if envelopes_do_not_overlap(shifted, obstacle, caption_clearance):
                        continue
                    for correction in (
                        Vector2(0, obstacle.min_y - caption_clearance - shifted.max_y),
                        Vector2(0, obstacle.max_y + caption_clearance - shifted.min_y),
                        Vector2(obstacle.min_x - caption_clearance - shifted.max_x, 0),
                        Vector2(obstacle.max_x + caption_clearance - shifted.min_x, 0),
                    ):
                        moved = Vector2(candidate.x + correction.x, candidate.y + correction.y)
                        if box_distance(original.translated(moved), body) <= max(
                                3 * gap, original.height + gap):
                            candidates.append(moved)
            # Nearby wires may fill the ordinary caption band for any part,
            # including a two-pin passive. Try their actual edges as well as
            # grid offsets before retaining an unresolved overlap.
            reach = 20 * grid
            for obstacle in obstacles:
                if box_distance(obstacle, body) > reach:
                    continue
                candidates.extend([
                    Vector2(0, obstacle.min_y - caption_clearance - original.max_y),
                    Vector2(0, obstacle.max_y + caption_clearance - original.min_y),
                    Vector2(obstacle.min_x - caption_clearance - original.max_x, 0),
                    Vector2(obstacle.max_x + caption_clearance - original.min_x, 0),
                ])
            candidates = [p for p in candidates if abs(p.x) + abs(p.y) <= 2 * reach]
            if caption_bank_axis(component):
                # A bank caption belongs to this exact row; a free NC row
                # above or below is not a valid substitute for ownership.
                horizontal_bank = caption_bank_axis(component) == "x"
                candidates = [candidate for candidate in candidates
                              if (candidate.y if horizontal_bank else candidate.x) == 0]
                # Text need not follow the electrical grid. A full grid step
                # can hit the body when a small sideways slide clears a mark.
                for box in field_boxes:
                    for obstacle, separation in (
                        [(box, clearance) for box in fixed]
                        + [(box, caption_clearance) for box in placed_fields]
                    ):
                        if envelopes_do_not_overlap(box, obstacle, separation):
                            continue
                        candidates.extend([
                            Vector2(obstacle.min_x - separation - box.max_x, 0),
                            Vector2(obstacle.max_x + separation - box.min_x, 0),
                        ] if horizontal_bank else [
                            Vector2(0, obstacle.min_y - separation - box.max_y),
                            Vector2(0, obstacle.max_y + separation - box.min_y),
                        ])
            candidates.sort(key=lambda offset: (
                sum(box_distance(reference_box.translated(offset), other) + clearance
                    < box_distance(reference_box.translated(offset), body)
                    for key, other in body_envelopes.items()
                    if key in component_ids and key != component.id),
                offset != Vector2(0, 0),
                box_distance(reference_box.translated(offset), body),
                box_distance(original.translated(offset), body),
                abs(offset.x) + abs(offset.y),
            ))
        else:
            candidates = _rail_caption_offsets(
                original, body, gap,
                tuple((box, clearance) for box in fixed)
                + tuple((box, caption_clearance) for box in [*placed_fields, *wire_envelopes]),
            )

        def caption_positions():
            yield from candidates
            if caption_bank_axis(component):
                return  # An inline bank title still belongs to its own row.
            # If edge-only moves fail, search locally around those body edges,
            # not around a caption that may have started on the wrong side.
            for base in edge_positions:
                for delta in offsets:
                    moved = Vector2(base.x + delta.x, base.y + delta.y)
                    if box_distance(original.translated(moved), body) <= max(
                            3 * gap, original.height + 2 * gap):
                        yield moved

        def clear_caption(candidate: Vector2, boxes: list[Envelope]) -> bool:
            return all(
                envelopes_do_not_overlap(box.translated(candidate), obstacle, clearance)
                for box in boxes for obstacle in fixed
            ) and all(
                envelopes_do_not_overlap(
                    box.translated(candidate), obstacle, caption_clearance
                )
                for box in boxes for obstacle in placed_fields
            ) and (component.id in component_ids or all(
                envelopes_do_not_overlap(
                    box.translated(candidate), wire, caption_clearance
                )
                for box in boxes for wire in wire_envelopes
            ))

        offset = next((p for p in caption_positions() if clear_caption(p, field_boxes)), None)
        if offset is None and component.id not in component_ids:
            # A rail name is movable text, not fixed geometry. Offer readable
            # vertical text beside its glyph before accepting an overlap or
            # moving the already clear circuit to accommodate a wide name.
            turned = [oriented_field_text(field.text, component.transform.orientation, 90)
                      for field in fields]
            boxes = [envelope_from_points(text_envelope(text, component.transform.orientation))
                     for text in turned]
            turned_offsets = _rail_caption_offsets(
                union_all(boxes), body, gap,
                tuple((box, clearance) for box in fixed)
                + tuple((box, caption_clearance) for box in [*placed_fields, *wire_envelopes]))
            offset = next((p for p in turned_offsets if clear_caption(p, boxes)), None)
            if offset is not None:
                for field, text in zip(fields, turned, strict=True):
                    field.text = text
                field_boxes = boxes
        if offset is None:
            failed.append(component)
            offset = Vector2(0, 0)
        for field in fields:
            field.text.position = Vector2(
                field.text.position.x + offset.x,
                field.text.position.y + offset.y,
            )
        placed_fields.extend(box.translated(offset) for box in field_boxes)
    return symbols, failed
