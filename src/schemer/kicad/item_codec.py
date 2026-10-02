from __future__ import annotations

import json

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.items import (
    BaseLabel,
    EditableItem,
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    LocalLabel,
    NoConnectMarker,
    SchematicField,
    SchematicLine,
    SchematicSymbolInstance,
    SchematicSymbolTransform,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.kicad.records import (
    KiCadField,
    KiCadJunction,
    KiCadLabel,
    KiCadNoConnect,
    KiCadSymbol,
    KiCadWire,
)
from schemer.kicad.syntax import Atom, ListExpr, descendant


def symbol_item(symbol: KiCadSymbol) -> SchematicSymbolInstance:
    return SchematicSymbolInstance(
        id=symbol.uuid,
        zener_path=symbol.path,
        library_id=symbol.library_id,
        unit=symbol.unit,
        position=Vector2.from_xy_mm(*symbol.position),
        transform=SchematicSymbolTransform(symbol.rotation, symbol.mirror_x, symbol.mirror_y),
        fields=[_field_item(field) for field in symbol.fields],
    )


def _field_item(field: KiCadField) -> SchematicField:
    size = field.text_size or (0.0, 0.0)
    return SchematicField(
        name=field.name,
        text=Text(
            value=field.value,
            position=Vector2.from_xy_mm(*field.position),
            attributes=TextAttributes(
                Vector2.from_xy_mm(*size),
                field.rotation,
                label_horizontal_alignment(field.expression),
                text_vertical_alignment(field.expression),
            ),
        ),
        visible=not field.hidden,
    )


def line_item(wire: KiCadWire) -> SchematicLine:
    if len(wire.points) != 2:
        raise KiCadSchematicError(
            f"wire {wire.uuid!r} has {len(wire.points)} points; KiCad API lines require two"
        )
    return SchematicLine(
        id=wire.uuid,
        start=Vector2.from_xy_mm(*wire.points[0]),
        end=Vector2.from_xy_mm(*wire.points[1]),
    )


def label_item(label: KiCadLabel) -> BaseLabel:
    label_type = {
        "label": LocalLabel,
        "global_label": GlobalLabel,
        "hierarchical_label": HierarchicalLabel,
    }.get(label.kind)
    if label_type is None:
        raise KiCadSchematicError(f"unsupported label kind {label.kind!r}")
    size = label.text_size or (0.0, 0.0)
    return label_type(
        id=label.uuid,
        position=Vector2.from_xy_mm(*label.position),
        text=Text(
            value=label.text,
            position=Vector2.from_xy_mm(*label.position),
            attributes=TextAttributes(
                Vector2.from_xy_mm(*size),
                label.rotation,
                label_horizontal_alignment(label.expression),
                text_vertical_alignment(label.expression),
            ),
        ),
    )


def junction_item(junction: KiCadJunction) -> Junction:
    return Junction(id=junction.uuid, position=Vector2.from_xy_mm(*junction.position))


def no_connect_item(marker: KiCadNoConnect) -> NoConnectMarker:
    return NoConnectMarker(id=marker.uuid, position=Vector2.from_xy_mm(*marker.position))


def text_vertical_alignment(expression: ListExpr) -> str:
    justify = descendant(expression, ("effects", "justify"))
    if justify is not None:
        for child in justify.children[1:]:
            if isinstance(child, Atom) and child.value in {"top", "bottom"}:
                return child.value
    return "center"


def label_horizontal_alignment(expression: ListExpr) -> str:
    justify = descendant(expression, ("effects", "justify"))
    if justify is None:
        return "center"
    values = {
        child.value
        for child in justify.children[1:]
        if isinstance(child, Atom)
    }
    for value in ("left", "right"):
        if value in values:
            return value
    return "center"


RawItem = KiCadSymbol | KiCadWire | KiCadLabel | KiCadJunction | KiCadNoConnect


def format_float(value: float) -> str:
    if abs(value) < 0.0000005:
        value = 0.0
    return f"{value:.6f}".rstrip("0").rstrip(".")


def serialize_item(item: EditableItem) -> str:
    if isinstance(item, SchematicLine):
        if item.type != "wire":
            raise KiCadSchematicError(f"unsupported schematic line type {item.type!r}")
        start_x, start_y = item.start.as_mm()
        end_x, end_y = item.end.as_mm()
        return (
            "(wire\n"
            f"\t\t(pts (xy {format_float(start_x)} {format_float(start_y)}) "
            f"(xy {format_float(end_x)} {format_float(end_y)}))\n"
            "\t\t(stroke (width 0) (type default))\n"
            f'\t\t(uuid "{item.id}"))'
        )
    if isinstance(item, BaseLabel):
        tag = {
            LocalLabel: "label",
            GlobalLabel: "global_label",
            HierarchicalLabel: "hierarchical_label",
        }.get(type(item))
        if tag is None:
            raise KiCadSchematicError(f"unsupported label type {type(item).__name__}")
        x, y = item.position.as_mm()
        size_x, size_y = item.text.attributes.size.as_mm()
        if size_x <= 0 or size_y <= 0:
            raise KiCadSchematicError("label text size must be positive")
        alignment = item.text.attributes.horizontal_alignment
        if alignment not in {"center", "left", "right"}:
            raise KiCadSchematicError(f"unsupported label alignment {alignment!r}")
        justify = "" if alignment == "center" else f" (justify {alignment})"
        vertical = item.text.attributes.vertical_alignment
        if vertical not in {"center", "top", "bottom"}:
            raise KiCadSchematicError(f"unsupported label vertical alignment {vertical!r}")
        if vertical != "center":
            values = [alignment, vertical] if alignment != "center" else [vertical]
            justify = f" (justify {' '.join(values)})"
        shape = " (shape input)" if tag != "label" else ""
        return (
            f"({tag} {json.dumps(item.text.value)}{shape} "
            f"(at {format_float(x)} {format_float(y)} "
            f"{format_float(item.text.attributes.angle)})\n"
            f"\t\t(effects (font (size {format_float(size_x)} {format_float(size_y)}))"
            f"{justify})\n"
            f'\t\t(uuid "{item.id}"))'
        )
    if isinstance(item, Junction):
        x, y = item.position.as_mm()
        return (
            f"(junction (at {format_float(x)} {format_float(y)})\n"
            "\t\t(diameter 0)\n"
            "\t\t(color 0 0 0 0)\n"
            f'\t\t(uuid "{item.id}"))'
        )
    if isinstance(item, NoConnectMarker):
        x, y = item.position.as_mm()
        return f'(no_connect (at {format_float(x)} {format_float(y)}) (uuid "{item.id}"))'
    raise KiCadSchematicError(f"creating {type(item).__name__} is not supported")
