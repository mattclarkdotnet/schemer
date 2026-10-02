from __future__ import annotations

import json

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.item_codec import (
    RawItem,
    format_float,
    label_horizontal_alignment,
    text_vertical_alignment,
)
from schemer.kicad.items import (
    BaseLabel,
    EditableItem,
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    LocalLabel,
    NoConnectMarker,
    SchematicLine,
    SchematicSymbolInstance,
    Vector2,
)
from schemer.kicad.records import (
    KiCadJunction,
    KiCadLabel,
    KiCadNoConnect,
    KiCadSymbol,
    KiCadWire,
)
from schemer.kicad.syntax import Atom, Edit, ListExpr, descendant, sexpr_atom


def item_edits(raw: RawItem, item: EditableItem) -> list[Edit]:
    if isinstance(raw, KiCadSymbol) and isinstance(item, SchematicSymbolInstance):
        return _symbol_edits(raw, item)
    if isinstance(raw, KiCadWire) and isinstance(item, SchematicLine):
        return _line_edits(raw, item)
    if isinstance(raw, KiCadLabel) and isinstance(item, BaseLabel):
        return _label_edits(raw, item)
    if isinstance(raw, KiCadJunction) and isinstance(item, Junction):
        return _position_edits(raw.expression, item.position, None, "junction")
    if isinstance(raw, KiCadNoConnect) and isinstance(item, NoConnectMarker):
        return _position_edits(raw.expression, item.position, None, "no-connect")
    raise KiCadSchematicError(
        f"schematic item {item.id!r} changed type from {type(raw).__name__} "
        f"to {type(item).__name__}"
    )


def _symbol_edits(raw: KiCadSymbol, item: SchematicSymbolInstance) -> list[Edit]:
    if (item.transform.mirror_x, item.transform.mirror_y) != (raw.mirror_x, raw.mirror_y):
        raise KiCadSchematicError(
            "symbol mirror updates are not implemented by the KiCad 10 backend"
        )
    edits = _position_edits(
        raw.expression,
        item.position,
        item.transform.orientation,
        f"symbol {item.id}",
    )
    raw_fields = {field.name: field for field in raw.fields}
    item_fields = {field.name: field for field in item.fields}
    if set(raw_fields) != set(item_fields):
        raise KiCadSchematicError(
            f"symbol {item.id!r} fields changed; creating or removing fields is not supported"
        )
    for name, field in item_fields.items():
        raw_field = raw_fields[name]
        if raw_field.hidden and field.visible:
            raise KiCadSchematicError(f"showing hidden property {name!r} is not implemented")
        if not raw_field.hidden and not field.visible:
            effects = raw_field.expression.first_list("effects")
            insertion = effects.start if effects is not None else raw_field.expression.end - 1
            edits.append(Edit(insertion, insertion, "\n\t\t\t(hide yes)\n\t\t\t"))
        edits.append(
            Edit(
                sexpr_atom(raw_field.expression, 2, f"property {name}").start,
                sexpr_atom(raw_field.expression, 2, f"property {name}").end,
                json.dumps(field.text.value),
            )
        )
        edits.extend(
            _position_edits(
                raw_field.expression,
                field.text.position,
                field.text.attributes.angle,
                f"property {name}",
            )
        )
        edits.extend(
            _text_size_edits(
                raw_field.expression,
                field.text.attributes.size,
                f"property {name}",
            )
        )
        edits.extend(
            _text_alignment_edits(
                raw_field.expression,
                field.text.attributes.horizontal_alignment,
                f"property {name}",
                vertical_alignment=field.text.attributes.vertical_alignment,
            )
        )
    return edits


def _line_edits(raw: KiCadWire, item: SchematicLine) -> list[Edit]:
    if item.type != "wire":
        raise KiCadSchematicError(f"unsupported schematic line type {item.type!r}")
    points = raw.expression.first_list("pts")
    if points is None:
        raise KiCadSchematicError(f"wire {item.id!r} has no points")
    coordinates = points.lists("xy")
    if len(coordinates) != 2:
        raise KiCadSchematicError(f"wire {item.id!r} does not have exactly two endpoints")
    edits = []
    for expression, position in zip(coordinates, (item.start, item.end), strict=True):
        edits.extend(_xy_edits(expression, position, f"wire {item.id}"))
    return edits


def _label_edits(raw: KiCadLabel, item: BaseLabel) -> list[Edit]:
    expected_type = {
        "label": LocalLabel,
        "global_label": GlobalLabel,
        "hierarchical_label": HierarchicalLabel,
    }[raw.kind]
    if not isinstance(item, expected_type):
        raise KiCadSchematicError(f"label {item.id!r} changed kind")
    text_atom = sexpr_atom(raw.expression, 1, f"label {item.id}")
    edits = [Edit(text_atom.start, text_atom.end, json.dumps(item.text.value))]
    edits.extend(
        _position_edits(
            raw.expression,
            item.position,
            item.text.attributes.angle,
            f"label {item.id}",
        )
    )
    edits.extend(_text_size_edits(raw.expression, item.text.attributes.size, f"label {item.id}"))
    edits.extend(_text_alignment_edits(
        raw.expression, item.text.attributes.horizontal_alignment, f"label {item.id}",
        vertical_alignment=item.text.attributes.vertical_alignment,
    ))
    return edits


def _position_edits(
    expression: ListExpr,
    position: Vector2,
    rotation: float | None,
    context: str,
) -> list[Edit]:
    at = expression.first_list("at")
    if at is None:
        raise KiCadSchematicError(f"{context} has no position")
    edits = _xy_edits(at, position, context)
    if rotation is not None:
        if len(at.children) < 4:
            raise KiCadSchematicError(f"{context} has no rotation atom")
        atom = sexpr_atom(at, 3, context)
        edits.append(Edit(atom.start, atom.end, format_float(rotation)))
    return edits


def _xy_edits(expression: ListExpr, position: Vector2, context: str) -> list[Edit]:
    x = sexpr_atom(expression, 1, context)
    y = sexpr_atom(expression, 2, context)
    x_mm, y_mm = position.as_mm()
    return [
        Edit(x.start, x.end, format_float(x_mm)),
        Edit(y.start, y.end, format_float(y_mm)),
    ]


def _text_size_edits(expression: ListExpr, size: Vector2, context: str) -> list[Edit]:
    raw_size = descendant(expression, ("effects", "font", "size"))
    if raw_size is None:
        if size.x == 0 and size.y == 0:
            return []
        raise KiCadSchematicError(f"{context} has no text-size expression")
    if size.x <= 0 or size.y <= 0:
        raise KiCadSchematicError(f"{context} text size must be positive")
    return _xy_edits(raw_size, size, f"{context} text size")


def _text_alignment_edits(
    expression: ListExpr,
    alignment: str,
    context: str,
    *,
    vertical_alignment: str | None = None,
) -> list[Edit]:
    if alignment not in {"center", "left", "right"}:
        raise KiCadSchematicError(f"unsupported {context} alignment {alignment!r}")
    current = label_horizontal_alignment(expression)
    if vertical_alignment not in {None, "center", "top", "bottom"}:
        raise KiCadSchematicError(
            f"unsupported {context} vertical alignment {vertical_alignment!r}",
        )
    if (current == alignment
            and (vertical_alignment is None
                 or text_vertical_alignment(expression) == vertical_alignment)):
        return []
    effects = expression.first_list("effects")
    if effects is None:
        raise KiCadSchematicError(f"{context} has no effects expression")
    justify = effects.first_list("justify")
    values = [] if justify is None else [
        child.value
        for child in justify.children[1:]
        if isinstance(child, Atom) and child.value not in {"left", "right"}
    ]
    if vertical_alignment is not None:
        values = [value for value in values if value not in {"top", "bottom"}]
        if vertical_alignment != "center":
            values.append(vertical_alignment)
    if alignment != "center":
        values.insert(0, alignment)
    replacement = f"(justify {' '.join(values)})" if values else ""
    if justify is not None:
        return [Edit(justify.start, justify.end, replacement)]
    insertion = effects.end - 1
    return [Edit(insertion, insertion, f"\n\t\t\t{replacement}" if replacement else "")]
