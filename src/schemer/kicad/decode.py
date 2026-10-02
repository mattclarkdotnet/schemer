from __future__ import annotations

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.records import (
    KiCadField,
    KiCadJunction,
    KiCadLabel,
    KiCadNoConnect,
    KiCadSymbol,
    KiCadWire,
)
from schemer.kicad.syntax import ListExpr, descendant, sexpr_at, sexpr_atom, sexpr_number, uuid


def _field(expression: ListExpr) -> KiCadField:
    name = sexpr_atom(expression, 1, "property name").value
    value = sexpr_atom(expression, 2, f"property {name}").value
    x, y, rotation = sexpr_at(expression, f"property {name}")
    size = descendant(expression, ("effects", "font", "size"))
    text_size = None
    if size is not None:
        text_size = (
            sexpr_number(sexpr_atom(size, 1, f"property {name} size"), f"property {name} size"),
            sexpr_number(sexpr_atom(size, 2, f"property {name} size"), f"property {name} size"),
        )
    hidden = expression.first_list("hide")
    return KiCadField(
        name,
        value,
        (x, y),
        rotation,
        text_size,
        expression,
        hidden is not None and sexpr_atom(hidden, 1, f"property {name} hide").value == "yes",
    )


def decode_symbol(expression: ListExpr) -> KiCadSymbol:
    fields = tuple(_field(field) for field in expression.lists("property"))
    by_name = {field.name: field for field in fields}
    for required in ("Reference", "Value"):
        if required not in by_name:
            raise KiCadSchematicError(f"top-level symbol is missing {required!r}")
    library_id = expression.first_list("lib_id")
    if library_id is None:
        raise KiCadSchematicError("top-level symbol is missing 'lib_id'")
    unit = expression.first_list("unit")
    path = by_name.get("Path")
    mirror = expression.first_list("mirror")
    mirror_axis = sexpr_atom(mirror, 1, "mirror").value if mirror is not None else None
    if mirror_axis not in {None, "x", "y"}:
        raise KiCadSchematicError(f"unsupported symbol mirror axis {mirror_axis!r}")
    identity = path.value if path is not None else by_name["Reference"].value
    x, y, rotation = sexpr_at(expression, f"symbol {identity}")
    return KiCadSymbol(
        library_id=sexpr_atom(library_id, 1, "lib_id").value,
        path=path.value if path is not None else None,
        reference=by_name["Reference"].value,
        value=by_name["Value"].value,
        uuid=uuid(expression),
        unit=int(sexpr_number(sexpr_atom(unit, 1, "unit"), "unit")) if unit is not None else 1,
        position=(x, y),
        rotation=rotation,
        mirror_x=mirror_axis == "x",
        mirror_y=mirror_axis == "y",
        fields=fields,
        expression=expression,
    )


def decode_wire(expression: ListExpr) -> KiCadWire:
    points = expression.first_list("pts")
    if points is None:
        raise KiCadSchematicError("wire has no pts expression")
    coordinates = []
    for point in points.lists("xy"):
        coordinates.append(
            (
                sexpr_number(sexpr_atom(point, 1, "wire point"), "wire point"),
                sexpr_number(sexpr_atom(point, 2, "wire point"), "wire point"),
            )
        )
    if len(coordinates) < 2:
        raise KiCadSchematicError("wire has fewer than two points")
    return KiCadWire(tuple(coordinates), uuid(expression), expression)


def decode_label(expression: ListExpr) -> KiCadLabel:
    text = sexpr_atom(expression, 1, f"{expression.tag} text").value
    x, y, rotation = sexpr_at(expression, f"{expression.tag} {text}")
    size = descendant(expression, ("effects", "font", "size"))
    text_size = None
    if size is not None:
        text_size = (
            sexpr_number(sexpr_atom(size, 1, f"{expression.tag} size"), f"{expression.tag} size"),
            sexpr_number(sexpr_atom(size, 2, f"{expression.tag} size"), f"{expression.tag} size"),
        )
    return KiCadLabel(
        expression.tag or "",
        text,
        (x, y),
        rotation,
        text_size,
        uuid(expression),
        expression,
    )


def decode_no_connect(expression: ListExpr) -> KiCadNoConnect:
    x, y, _ = sexpr_at(expression, "no_connect")
    return KiCadNoConnect((x, y), uuid(expression), expression)


def decode_junction(expression: ListExpr) -> KiCadJunction:
    x, y, _ = sexpr_at(expression, "junction")
    return KiCadJunction((x, y), uuid(expression), expression)
