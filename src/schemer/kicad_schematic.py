"""Narrow, source-preserving access to persistent KiCad schematics.

The layout engine does not own KiCad's file format.  This module parses enough
S-expression structure to identify the objects Schemer needs while retaining
the original source text.  Writes replace individual atom spans; unrelated
syntax, ordering and formatting remain byte-for-byte unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class KiCadSchematicError(ValueError):
    """The schematic is malformed or lacks an object required by an edit."""


@dataclass(frozen=True)
class Atom:
    value: str
    start: int
    end: int
    quoted: bool = False


@dataclass(frozen=True)
class ListExpr:
    children: tuple[Expr, ...]
    start: int
    end: int

    @property
    def tag(self) -> str | None:
        if not self.children or not isinstance(self.children[0], Atom):
            return None
        return self.children[0].value

    def lists(self, tag: str) -> tuple[ListExpr, ...]:
        return tuple(
            child for child in self.children[1:] if isinstance(child, ListExpr) and child.tag == tag
        )

    def first_list(self, tag: str) -> ListExpr | None:
        return next(iter(self.lists(tag)), None)


type Expr = Atom | ListExpr


@dataclass(frozen=True)
class KiCadField:
    name: str
    value: str
    position: tuple[float, float]
    rotation: float
    text_size: tuple[float, float] | None
    expression: ListExpr
    hidden: bool = False


@dataclass(frozen=True)
class KiCadSymbol:
    library_id: str
    path: str | None
    reference: str
    value: str
    uuid: str
    unit: int
    position: tuple[float, float]
    rotation: float
    mirror_x: bool
    mirror_y: bool
    fields: tuple[KiCadField, ...]
    expression: ListExpr

    def field(self, name: str) -> KiCadField | None:
        return next((field for field in self.fields if field.name == name), None)


@dataclass(frozen=True)
class KiCadWire:
    points: tuple[tuple[float, float], ...]
    uuid: str
    expression: ListExpr


@dataclass(frozen=True)
class KiCadLabel:
    kind: str
    text: str
    position: tuple[float, float]
    rotation: float
    text_size: tuple[float, float] | None
    uuid: str
    expression: ListExpr


@dataclass(frozen=True)
class KiCadNoConnect:
    position: tuple[float, float]
    uuid: str
    expression: ListExpr


@dataclass(frozen=True)
class KiCadJunction:
    position: tuple[float, float]
    uuid: str
    expression: ListExpr


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    replacement: str


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.index = 0

    def parse(self) -> ListExpr:
        self._skip_space()
        expression = self._expression()
        self._skip_space()
        if self.index != len(self.source):
            raise KiCadSchematicError(f"unexpected trailing input at offset {self.index}")
        if not isinstance(expression, ListExpr):
            raise KiCadSchematicError("expected a parenthesized KiCad document")
        return expression

    def _skip_space(self) -> None:
        while self.index < len(self.source) and self.source[self.index].isspace():
            self.index += 1

    def _expression(self) -> Expr:
        self._skip_space()
        if self.index >= len(self.source):
            raise KiCadSchematicError("unexpected end of input")
        if self.source[self.index] == "(":
            return self._list()
        if self.source[self.index] == '"':
            return self._string()
        return self._atom()

    def _list(self) -> ListExpr:
        start = self.index
        self.index += 1
        children: list[Expr] = []
        while True:
            self._skip_space()
            if self.index >= len(self.source):
                raise KiCadSchematicError(f"unterminated list at offset {start}")
            if self.source[self.index] == ")":
                self.index += 1
                return ListExpr(tuple(children), start, self.index)
            children.append(self._expression())

    def _string(self) -> Atom:
        start = self.index
        self.index += 1
        escaped = False
        while self.index < len(self.source):
            character = self.source[self.index]
            self.index += 1
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                token = self.source[start : self.index]
                try:
                    value = json.loads(token)
                except json.JSONDecodeError as error:
                    raise KiCadSchematicError(
                        f"invalid quoted string at offset {start}: {error.msg}"
                    ) from error
                return Atom(value, start, self.index, quoted=True)
        raise KiCadSchematicError(f"unterminated string at offset {start}")

    def _atom(self) -> Atom:
        start = self.index
        while self.index < len(self.source):
            character = self.source[self.index]
            if character.isspace() or character in "()":
                break
            self.index += 1
        if start == self.index:
            raise KiCadSchematicError(f"unexpected token at offset {start}")
        return Atom(self.source[start : self.index], start, self.index)


def _atom(expression: ListExpr, index: int, context: str) -> Atom:
    try:
        item = expression.children[index]
    except IndexError as error:
        raise KiCadSchematicError(f"{context} is missing atom {index}") from error
    if not isinstance(item, Atom):
        raise KiCadSchematicError(f"{context} atom {index} is a list")
    return item


def _number(atom: Atom, context: str) -> float:
    try:
        return float(atom.value)
    except ValueError as error:
        raise KiCadSchematicError(f"{context} is not numeric: {atom.value!r}") from error


def _at(expression: ListExpr, context: str) -> tuple[float, float, float]:
    at = expression.first_list("at")
    if at is None:
        raise KiCadSchematicError(f"{context} has no at expression")
    x = _number(_atom(at, 1, context), context)
    y = _number(_atom(at, 2, context), context)
    rotation = _number(_atom(at, 3, context), context) if len(at.children) > 3 else 0.0
    return x, y, rotation


def _uuid(expression: ListExpr) -> str:
    uuid = expression.first_list("uuid")
    return _atom(uuid, 1, "uuid").value if uuid is not None else ""


def _descendant(expression: ListExpr, path: tuple[str, ...]) -> ListExpr | None:
    current = expression
    for tag in path:
        child = current.first_list(tag)
        if child is None:
            return None
        current = child
    return current


def _field(expression: ListExpr) -> KiCadField:
    name = _atom(expression, 1, "property name").value
    value = _atom(expression, 2, f"property {name}").value
    x, y, rotation = _at(expression, f"property {name}")
    size = _descendant(expression, ("effects", "font", "size"))
    text_size = None
    if size is not None:
        text_size = (
            _number(_atom(size, 1, f"property {name} size"), f"property {name} size"),
            _number(_atom(size, 2, f"property {name} size"), f"property {name} size"),
        )
    hidden = expression.first_list("hide")
    return KiCadField(
        name,
        value,
        (x, y),
        rotation,
        text_size,
        expression,
        hidden is not None and _atom(hidden, 1, f"property {name} hide").value == "yes",
    )


def _symbol(expression: ListExpr) -> KiCadSymbol:
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
    mirror_axis = _atom(mirror, 1, "mirror").value if mirror is not None else None
    if mirror_axis not in {None, "x", "y"}:
        raise KiCadSchematicError(f"unsupported symbol mirror axis {mirror_axis!r}")
    identity = path.value if path is not None else by_name["Reference"].value
    x, y, rotation = _at(expression, f"symbol {identity}")
    return KiCadSymbol(
        library_id=_atom(library_id, 1, "lib_id").value,
        path=path.value if path is not None else None,
        reference=by_name["Reference"].value,
        value=by_name["Value"].value,
        uuid=_uuid(expression),
        unit=int(_number(_atom(unit, 1, "unit"), "unit")) if unit is not None else 1,
        position=(x, y),
        rotation=rotation,
        mirror_x=mirror_axis == "x",
        mirror_y=mirror_axis == "y",
        fields=fields,
        expression=expression,
    )


def _wire(expression: ListExpr) -> KiCadWire:
    points = expression.first_list("pts")
    if points is None:
        raise KiCadSchematicError("wire has no pts expression")
    coordinates = []
    for point in points.lists("xy"):
        coordinates.append(
            (
                _number(_atom(point, 1, "wire point"), "wire point"),
                _number(_atom(point, 2, "wire point"), "wire point"),
            )
        )
    if len(coordinates) < 2:
        raise KiCadSchematicError("wire has fewer than two points")
    return KiCadWire(tuple(coordinates), _uuid(expression), expression)


def _label(expression: ListExpr) -> KiCadLabel:
    text = _atom(expression, 1, f"{expression.tag} text").value
    x, y, rotation = _at(expression, f"{expression.tag} {text}")
    size = _descendant(expression, ("effects", "font", "size"))
    text_size = None
    if size is not None:
        text_size = (
            _number(_atom(size, 1, f"{expression.tag} size"), f"{expression.tag} size"),
            _number(_atom(size, 2, f"{expression.tag} size"), f"{expression.tag} size"),
        )
    return KiCadLabel(
        expression.tag or "",
        text,
        (x, y),
        rotation,
        text_size,
        _uuid(expression),
        expression,
    )


def _no_connect(expression: ListExpr) -> KiCadNoConnect:
    x, y, _ = _at(expression, "no_connect")
    return KiCadNoConnect((x, y), _uuid(expression), expression)


def _junction(expression: ListExpr) -> KiCadJunction:
    x, y, _ = _at(expression, "junction")
    return KiCadJunction((x, y), _uuid(expression), expression)


@dataclass(frozen=True)
class KiCadSchematicDocument:
    source: str
    root: ListExpr
    version: str
    symbols: tuple[KiCadSymbol, ...]
    wires: tuple[KiCadWire, ...]
    labels: tuple[KiCadLabel, ...]
    junctions: tuple[KiCadJunction, ...]
    no_connects: tuple[KiCadNoConnect, ...]

    @classmethod
    def from_text(cls, source: str) -> KiCadSchematicDocument:
        root = _Parser(source).parse()
        if root.tag != "kicad_sch":
            raise KiCadSchematicError(f"expected kicad_sch root, found {root.tag!r}")
        version = root.first_list("version")
        if version is None:
            raise KiCadSchematicError("kicad_sch document has no version")
        top_level = tuple(item for item in root.children[1:] if isinstance(item, ListExpr))
        return cls(
            source=source,
            root=root,
            version=_atom(version, 1, "version").value,
            symbols=tuple(_symbol(item) for item in top_level if item.tag == "symbol"),
            wires=tuple(_wire(item) for item in top_level if item.tag == "wire"),
            labels=tuple(
                _label(item)
                for item in top_level
                if item.tag in {"label", "global_label", "hierarchical_label"}
            ),
            junctions=tuple(_junction(item) for item in top_level if item.tag == "junction"),
            no_connects=tuple(_no_connect(item) for item in top_level if item.tag == "no_connect"),
        )

    @classmethod
    def from_file(cls, path: Path) -> KiCadSchematicDocument:
        return cls.from_text(path.read_text())

    def symbols_by_path(self, path: str) -> tuple[KiCadSymbol, ...]:
        return tuple(symbol for symbol in self.symbols if symbol.path == path)

    def with_field_size(
        self,
        *,
        path: str,
        field_names: tuple[str, ...],
        size_mm: float,
    ) -> str:
        """Return source with selected fields resized on every unit at ``path``."""

        symbols = self.symbols_by_path(path)
        if not symbols:
            raise KiCadSchematicError(f"no symbol has Path {path!r}")
        edits: list[_Edit] = []
        edited_fields: set[str] = set()
        replacement = _format_number(size_mm)
        for symbol in symbols:
            for field_name in field_names:
                field = symbol.field(field_name)
                if field is None:
                    continue
                size = _descendant(field.expression, ("effects", "font", "size"))
                if size is None:
                    raise KiCadSchematicError(
                        f"symbol {path!r} field {field_name!r} has no font size"
                    )
                x = _atom(size, 1, f"symbol {path} field {field_name} size")
                y = _atom(size, 2, f"symbol {path} field {field_name} size")
                edits.extend(
                    (_Edit(x.start, x.end, replacement), _Edit(y.start, y.end, replacement))
                )
                edited_fields.add(field_name)
        missing = set(field_names) - edited_fields
        if missing:
            raise KiCadSchematicError(
                f"symbol {path!r} is missing requested fields: {', '.join(sorted(missing))}"
            )
        return _apply_edits(self.source, edits)

    def summary(self) -> dict[str, object]:
        component_symbols = tuple(symbol for symbol in self.symbols if symbol.path is not None)
        return {
            "version": self.version,
            "symbol_count": len(self.symbols),
            "component_symbol_count": len(component_symbols),
            "component_path_count": len({symbol.path for symbol in component_symbols}),
            "power_symbol_count": len(self.symbols) - len(component_symbols),
            "wire_count": len(self.wires),
            "label_count": len(self.labels),
            "junction_count": len(self.junctions),
            "no_connect_count": len(self.no_connects),
            "symbols": [
                {
                    "path": symbol.path,
                    "reference": symbol.reference,
                    "value": symbol.value,
                    "library_id": symbol.library_id,
                    "unit": symbol.unit,
                    "uuid": symbol.uuid,
                    "position": list(symbol.position),
                    "rotation": symbol.rotation,
                    "field_sizes": {
                        field.name: list(field.text_size) if field.text_size is not None else None
                        for field in symbol.fields
                    },
                }
                for symbol in self.symbols
            ],
        }


def _format_number(value: float) -> str:
    if value <= 0:
        raise KiCadSchematicError("text size must be positive")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _apply_edits(source: str, edits: list[_Edit]) -> str:
    ordered = sorted(edits, key=lambda edit: edit.start, reverse=True)
    previous_start = len(source) + 1
    result = source
    for edit in ordered:
        if edit.end > previous_start:
            raise KiCadSchematicError("overlapping KiCad source edits")
        result = result[: edit.start] + edit.replacement + result[edit.end :]
        previous_start = edit.start
    return result
