from __future__ import annotations

from typing import Any

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import pin_stroke_envelopes
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
from schemer.kicad.geometry.text import (
    caption_bank_axis,
    oriented_field_text,
    text_envelope,
    text_width,
)
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.association import KiCadComponentAssociation
from schemer.native.obstacles import component_body_obstacles
from schemer.native.routing import pin_contact_obstacle
from schemer.native.routing_model import PIN_STUB_MM, PlacedEndpoint


def position_bank_fields(
    editor: FileSchematic, components: list[SchematicSymbolInstance],
    bank_ids: dict[str, tuple[str, str, str, str]],
    caption_widths: dict[str, int] | None = None,
    *, component_groups: dict[str, str] | None = None,
) -> None:
    """Use a compact caption row beside each aligned inline passive."""

    raw = {symbol.uuid: symbol for symbol in editor.document.symbols}
    vertical = [s for s in components
                if len(symbol_library_pins(editor.document, raw[s.id])) == 2
                and set(placed_pin_sides(editor.document, raw[s.id]).values())
                == {"top", "bottom"}]
    for part in vertical:
        fields = [f for f in (part.reference_field, part.value_field) if f.visible]
        if not fields:
            continue
        # Narrow columns use rotated captions; electrical rows stay fixed.
        width = max(text_width(f.text) for f in fields) + 2_540_000
        peers = [s for s in vertical if s.id != part.id
                 and (component_groups or {}).get(s.id) == (component_groups or {}).get(part.id)
                 and abs(s.position.y - part.position.y) < 500_000
                 and 0 < abs(s.position.x - part.position.x) < width]
        if not peers:
            continue
        body = envelope_from_points(placed_symbol_body_positions(editor.document, raw[part.id]))
        x, y = body.max_x + 1_270_000, body.max_y
        for field in fields:
            field.text.position = Vector2(x, y)
            field.text = oriented_field_text(field.text, part.transform.orientation, 90, "left")
            y -= text_width(field.text) + field.text.attributes.size.x
    horizontal = {
        component.id: component
        for component in components
        if len(symbol_library_pins(editor.document, raw[component.id])) == 2
        and set(placed_pin_sides(editor.document, raw[component.id]).values())
        == {"left", "right"}
    }
    # A close stack has the same caption grammar regardless of whether its
    # members were placed by an authored bank or by the pin-axis default.
    stacked_ids = {
        component.id for component in horizontal.values()
        if any(
            other.id != component.id
            and abs(other.position.x - component.position.x) < 500_000
            and 0 < abs(other.position.y - component.position.y) <= 3_810_000
            for other in horizontal.values()
        )
    }
    caption_ids = set(bank_ids) | stacked_ids
    # Reserve the actual caption row before the native routes are constructed.
    # Closely aligned members move together along their existing pin axes.
    pending = [component for component in components if component.id in caption_ids]
    while pending:
        first = pending.pop(0)
        bank = [first]
        nearby = sorted(pending[:], key=lambda item: abs(item.position.y - first.position.y))
        for component in nearby:
            same_bank = first.id in bank_ids and bank_ids.get(component.id) == bank_ids[first.id]
            neighbouring = (first.id not in bank_ids and component.id not in bank_ids
                            and any(abs(component.position.y - member.position.y) <= 3_810_000
                                    for member in bank))
            if (abs(component.position.x - first.position.x) < 500_000
                    and (same_bank or neighbouring)):
                bank.append(component)
                pending.remove(component)
        shift = 0
        for component in bank:
            body = placed_symbol_body_positions(editor.document, raw[component.id])
            reference, value = component.reference_field.text, component.value_field.text
            right = max(p.x for p in body) + 1_270_000
            right += (caption_widths or {}).get(
                component.id,
                text_width(reference) + reference.attributes.size.x + text_width(value),
            )
            for other in editor.document.symbols:
                if other.uuid in caption_ids:
                    continue
                sides = placed_pin_sides(editor.document, other)
                for number, pin in placed_pin_positions(editor.document, other).items():
                    if (sides[number] == "left" and pin.x > component.position.x
                            and abs(pin.y - component.position.y) < 100_000):
                        shift = max(shift, right + 635_000 - pin.x)
        if shift > 0:
            for component in bank:
                place_symbol(
                    component,
                    Vector2(component.position.x - shift, component.position.y),
                    component.transform.orientation,
                )
            editor.update_items(bank)
            raw = {symbol.uuid: symbol for symbol in editor.document.symbols}
    for component in components:
        if component.id not in caption_ids:
            continue
        body = placed_symbol_body_positions(editor.document, raw[component.id])
        x = max(point.x for point in body) + round(1.27 * 1_000_000)
        y = component.position.y - round(1.27 * 1_000_000)
        if component.id in bank_ids and bank_ids[component.id][1] == "snake":
            # The normal pin stub is the turn lane; labels start beyond it,
            # on the component centreline, not over the connecting wire.
            members = [s for s in components if bank_ids.get(s.id) == bank_ids[component.id]]
            x = max(p.x for s in members for p in placed_pin_positions(
                editor.document, raw[s.id]).values()) + round((PIN_STUB_MM + 1.27) * 1e6)
            y = component.position.y
        for field in (component.reference_field, component.value_field):
            field.text.position = Vector2(x, y)
            field.text = oriented_field_text(field.text, component.transform.orientation, 0, "left")
            x += text_width(field.text) + field.text.attributes.size.x
    _clear_bank_caption_pin_exits(editor, components, component_groups or {})


def _clear_bank_caption_pin_exits(
    editor: FileSchematic, components: list[SchematicSymbolInstance], groups: dict[str, str],
) -> None:
    """A provisional bank caption must not reserve another terminal's escape.

    Keep the caption pair on its bank axis and move text, never the circuit.
    Final annotation fitting still handles routed wires and other captions.
    """
    obstacles = component_body_obstacles(editor, groups)
    for raw in editor.document.symbols:
        group = groups.get(raw.uuid)
        if group is None:
            continue
        sides = placed_pin_sides(editor.document, raw)
        strokes = pin_stroke_envelopes(editor, raw)
        for number, point in placed_pin_positions(editor.document, raw).items():
            obstacles.setdefault(group, []).append(pin_contact_obstacle(
                PlacedEndpoint(point, sides[number], stroke=strokes.get(number))))
    for component in components:
        fields = [f for f in (component.reference_field, component.value_field) if f.visible]
        axis = caption_bank_axis(component)
        if axis is None:
            continue
        boxes = [envelope_from_points(text_envelope(f.text, component.transform.orientation))
                 for f in fields]
        fixed = obstacles.get(groups.get(component.id), [])
        gap = 250_000

        def clear(offset):
            return all(envelopes_do_not_overlap(box.translated(offset), obstacle, gap)
                       for box in boxes for obstacle in fixed)

        if clear(Vector2(0, 0)):
            continue
        offsets = []
        for box in boxes:
            for obstacle in fixed:
                if (axis == "x" and box.min_y - gap <= obstacle.max_y
                        and obstacle.min_y <= box.max_y + gap):
                    offsets.extend((Vector2(obstacle.min_x - gap - box.max_x, 0),
                                    Vector2(obstacle.max_x + gap - box.min_x, 0)))
                elif (axis == "y" and box.min_x - gap <= obstacle.max_x
                        and obstacle.min_x <= box.max_x + gap):
                    offsets.extend((Vector2(0, obstacle.min_y - gap - box.max_y),
                                    Vector2(0, obstacle.max_y + gap - box.min_y)))
        offset = next((p for p in sorted(offsets, key=lambda p: (abs(p.x) + abs(p.y), p.x, p.y))
                       if clear(p)), None)
        if offset is not None:
            for field in fields:
                field.text.position = Vector2(field.text.position.x + offset.x,
                                              field.text.position.y + offset.y)


def position_component_fields(
    editor: FileSchematic,
    components: list[SchematicSymbolInstance],
) -> list[SchematicSymbolInstance]:
    """Place visible fields from resolved pin geometry, never inherited offsets."""

    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    typed_by_id = {symbol.id: symbol for symbol in editor.get_symbols()}
    result = []
    for previous in components:
        component = typed_by_id[previous.id]
        raw = raw_by_id[previous.id]
        body = placed_symbol_body_positions(editor.document, raw)
        if body:
            center_x = round((min(point.x for point in body) + max(point.x for point in body)) / 2)
            top_y = min(point.y for point in body)
        else:
            center_x = component.position.x
            top_y = component.position.y
        reference = component.field("Reference")
        value = component.field("Value")
        fields = [field for field in (reference, value) if field is not None]
        line_height = max(
            (field.text.attributes.size.y for field in fields),
            default=round(1.27 * 1_000_000),
        )
        body_width = max((point.x for point in body), default=center_x) - min(
            (point.x for point in body), default=center_x
        )
        body_height = max((point.y for point in body), default=top_y) - min(
            (point.y for point in body), default=top_y
        )
        compact_two_terminal = (
            len(symbol_library_pins(editor.document, raw)) == 2
            and max(body_width, body_height) <= round(6.0 * 1_000_000)
        )
        if compact_two_terminal:
            _position_compact_two_terminal_fields(
                component,
                body,
                placed_pin_sides(editor.document, raw),
                line_height,
            )
            result.append(component)
            continue
        # A one-pin symbol with a north-facing terminal keeps its captions
        # below its body, leaving the straight supply approach unobstructed.
        below = (len(symbol_library_pins(editor.document, raw)) == 1
                 and set(placed_pin_sides(editor.document, raw).values()) == {"top"})
        baseline = (top_y + body_height + round(1.25 * line_height) if below
                    else top_y - round(1.25 * line_height))
        if value is not None:
            value.text.position = Vector2(
                center_x, baseline + 2 * line_height if below else baseline)
            value.text = oriented_field_text(value.text, component.transform.orientation)
        if reference is not None:
            reference.text.position = Vector2(
                center_x, baseline if below else baseline - 2 * line_height)
            reference.text = oriented_field_text(reference.text, component.transform.orientation)
        result.append(component)
    return result


def _position_compact_two_terminal_fields(
    component: SchematicSymbolInstance,
    body: list[Vector2],
    pin_sides: dict[str, str],
    line_height: int,
) -> None:
    """Keep captions off the occupied terminal axis of a small two-pin part."""

    reference = component.field("Reference")
    value = component.field("Value")
    min_x = min((point.x for point in body), default=component.position.x)
    max_x = max((point.x for point in body), default=component.position.x)
    min_y = min((point.y for point in body), default=component.position.y)
    max_y = max((point.y for point in body), default=component.position.y)
    vertical = set(pin_sides.values()) == {"top", "bottom"}
    if vertical:
        x = max_x + round(0.8 * line_height)
        center_y = round((min_y + max_y) / 2)
        for field, y in (
            (reference, center_y - line_height),
            (value, center_y + line_height),
        ):
            if field is None:
                continue
            field.text.position = Vector2(x, y)
            field.text = oriented_field_text(field.text, component.transform.orientation, 0, "left")
        return

    center_x = round((min_x + max_x) / 2)
    if reference is not None:
        reference.text.position = Vector2(center_x, min_y - round(2.8 * line_height))
        reference.text = oriented_field_text(reference.text, component.transform.orientation)
    if value is not None:
        value.text.position = Vector2(center_x, min_y - round(0.8 * line_height))
        value.text = oriented_field_text(value.text, component.transform.orientation)


def apply_component_display_values(
    schematic: dict[str, Any],
    associations: tuple[KiCadComponentAssociation, ...],
    components: list[SchematicSymbolInstance],
) -> None:
    by_id = {component.id: component for component in components}
    instances = schematic.get("instances", {})
    for association in associations:
        instance = instances.get(association.instance_ref)
        if not isinstance(instance, dict):
            continue
        attributes = instance.get("attributes")
        if not isinstance(attributes, dict):
            continue
        value = attributes.get("value", attributes.get("Value"))
        if isinstance(value, dict):
            value = value.get("String")
        if not isinstance(value, str) or not value:
            continue
        for raw_symbol in association.symbols:
            component = by_id.get(raw_symbol.uuid)
            if component is not None:
                component.value = value


def consolidate_multi_unit_fields(
    editor: FileSchematic,
    associations: tuple[KiCadComponentAssociation, ...],
    components: list[SchematicSymbolInstance],
) -> None:
    """Keep every unit identified, but print the long package value only once."""

    by_id = {component.id: component for component in components}
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    for association in associations:
        if len(association.symbols) < 2:
            continue
        ordered = sorted(association.symbols, key=lambda symbol: symbol.unit)
        visible = by_id.get(ordered[0].uuid)
        if visible is None:
            continue
        body = placed_symbol_body_positions(editor.document, raw_by_id[visible.id])
        if body:
            center_x = round((min(point.x for point in body) + max(point.x for point in body)) / 2)
            top_y = min(point.y for point in body)
            fields = [
                field
                for field in (visible.field("Reference"), visible.field("Value"))
                if field is not None
            ]
            line_height = max(
                (field.text.attributes.size.y for field in fields),
                default=round(1.27 * 1_000_000),
            )
            baseline = top_y - round(1.25 * line_height)
            value = visible.field("Value")
            reference = visible.field("Reference")
            if value is not None:
                value.text.position = Vector2(center_x, baseline)
            if reference is not None:
                reference.text.position = Vector2(
                    center_x,
                    baseline - 2 * line_height,
                )
        for raw in ordered[1:]:
            component = by_id.get(raw.uuid)
            if component is None:
                continue
            reference = component.field("Reference")
            if reference is not None:
                reference.visible = True
            value = component.field("Value")
            if value is not None:
                value.visible = False
