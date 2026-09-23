"""KiCad 11-shaped schematic editing over a KiCad 10 file.

The public method names and item model follow the official ``kipy`` schematic
API closely enough that layout policy need not know whether it is talking to
this source-preserving file backend or, later, a live KiCad 11 backend.  Only
the item types Schemer needs are implemented.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from schemer.kicad_schematic import (
    Atom,
    KiCadField,
    KiCadJunction,
    KiCadLabel,
    KiCadNoConnect,
    KiCadSchematicDocument,
    KiCadSchematicError,
    KiCadSymbol,
    KiCadWire,
    ListExpr,
    _apply_edits,
    _atom,
    _descendant,
    _Edit,
)

NM_PER_MM = 1_000_000


@dataclass
class Vector2:
    """Integer nanometre coordinate, matching ``kipy.geometry.Vector2``."""

    x: int
    y: int

    @classmethod
    def from_xy(cls, x_nm: int, y_nm: int) -> Vector2:
        return cls(x_nm, y_nm)

    @classmethod
    def from_xy_mm(cls, x_mm: float, y_mm: float) -> Vector2:
        return cls(round(x_mm * NM_PER_MM), round(y_mm * NM_PER_MM))

    def as_mm(self) -> tuple[float, float]:
        return self.x / NM_PER_MM, self.y / NM_PER_MM


@dataclass
class TextAttributes:
    size: Vector2
    angle: float = 0.0
    horizontal_alignment: str = "center"
    vertical_alignment: str = "center"


@dataclass
class Text:
    value: str
    position: Vector2
    attributes: TextAttributes


@dataclass
class SchematicField:
    name: str
    text: Text
    visible: bool = True


@dataclass(kw_only=True)
class SchematicItem:
    id: str


@dataclass
class SchematicSymbolTransform:
    orientation: float = 0.0
    mirror_x: bool = False
    mirror_y: bool = False


@dataclass(kw_only=True)
class SchematicSymbolInstance(SchematicItem):
    # This is the Zener identity stored in KiCad's custom Path field, not the
    # KiCad 11 API's hierarchical SheetPath.  Hierarchy is outside this
    # single-sheet backend's current contract.
    zener_path: str | None
    library_id: str
    unit: int
    position: Vector2
    transform: SchematicSymbolTransform
    fields: list[SchematicField]

    def field(self, name: str) -> SchematicField | None:
        return next((field for field in self.fields if field.name == name), None)

    @property
    def reference_field(self) -> SchematicField:
        return self._required_field("Reference")

    @property
    def value_field(self) -> SchematicField:
        return self._required_field("Value")

    @property
    def reference(self) -> str:
        """Shortcut matching KiCad 11's ``reference_field.text.value``."""

        return self.reference_field.text.value

    @reference.setter
    def reference(self, value: str) -> None:
        self.reference_field.text.value = value

    @property
    def value(self) -> str:
        """Shortcut matching KiCad 11's ``value_field.text.value``."""

        return self.value_field.text.value

    @value.setter
    def value(self, value: str) -> None:
        self.value_field.text.value = value

    def _required_field(self, name: str) -> SchematicField:
        field = self.field(name)
        if field is None:
            raise KiCadSchematicError(f"symbol {self.id!r} has no {name!r} field")
        return field


@dataclass(kw_only=True)
class SchematicLine(SchematicItem):
    start: Vector2
    end: Vector2
    type: str = "wire"


@dataclass(kw_only=True)
class BaseLabel(SchematicItem):
    position: Vector2
    text: Text


@dataclass(kw_only=True)
class LocalLabel(BaseLabel):
    pass


@dataclass(kw_only=True)
class GlobalLabel(BaseLabel):
    pass


@dataclass(kw_only=True)
class HierarchicalLabel(BaseLabel):
    pass


@dataclass(kw_only=True)
class Junction(SchematicItem):
    position: Vector2


@dataclass(kw_only=True)
class NoConnectMarker(SchematicItem):
    position: Vector2


@dataclass(frozen=True)
class Commit:
    id: str


@dataclass(frozen=True)
class PageSettings:
    """Physical sheet framing, shaped like KiCad 11's page settings value."""

    page_size: str
    orientation: str = "landscape"


EditableItem = SchematicSymbolInstance | SchematicLine | BaseLabel | Junction | NoConnectMarker


class SchematicEditor(Protocol):
    """Layout-facing subset shared with KiCad 11's ``Schematic`` API."""

    def get_symbols(self) -> Sequence[SchematicSymbolInstance]: ...

    def get_lines(self) -> Sequence[SchematicLine]: ...

    def get_labels(self) -> Sequence[BaseLabel]: ...

    def get_junctions(self) -> Sequence[Junction]: ...

    def get_no_connects(self) -> Sequence[NoConnectMarker]: ...

    def get_page_settings(self) -> PageSettings: ...

    def set_page_settings(self, settings: PageSettings) -> None: ...

    def begin_commit(self) -> Commit: ...

    def push_commit(self, commit: Commit, message: str = "") -> None: ...

    def drop_commit(self, commit: Commit) -> None: ...

    def update_items(self, items: EditableItem | Sequence[EditableItem]) -> list[EditableItem]: ...

    def create_items(self, items: EditableItem | Iterable[EditableItem]) -> list[EditableItem]: ...

    def remove_items(self, items: EditableItem | Sequence[EditableItem]) -> None: ...

    def remove_items_by_id(self, ids: str | Sequence[str]) -> None: ...

    def get_as_string(self) -> str: ...


class FileSchematic:
    """Source-preserving file backend with KiCad 11-style editing methods."""

    def __init__(self, source: str, path: Path | None = None):
        self._source = source
        self._path = path
        self._open_commit: Commit | None = None
        self._commit_source: str | None = None
        self._parse()

    @classmethod
    def from_text(cls, source: str) -> FileSchematic:
        return cls(source)

    @classmethod
    def from_file(cls, path: Path) -> FileSchematic:
        resolved = path.expanduser().resolve()
        return cls(resolved.read_text(), resolved)

    @property
    def name(self) -> str:
        return self._path.name if self._path is not None else "<memory>.kicad_sch"

    @property
    def version(self) -> str:
        return self._document.version

    @property
    def document(self) -> KiCadSchematicDocument:
        """Current parsed file state for geometry operations in the file backend."""

        return self._document

    def get_as_string(self) -> str:
        return self._source

    def get_items(self) -> Sequence[EditableItem]:
        return (
            *self.get_symbols(),
            *self.get_lines(),
            *self.get_labels(),
            *self.get_junctions(),
            *self.get_no_connects(),
        )

    def get_items_by_id(self, ids: str | Sequence[str]) -> Sequence[EditableItem]:
        requested = [ids] if isinstance(ids, str) else list(ids)
        by_id = {item.id: item for item in self.get_items()}
        missing = set(requested) - set(by_id)
        if missing:
            raise KiCadSchematicError("unknown schematic item IDs: " + ", ".join(sorted(missing)))
        return [by_id[item_id] for item_id in requested]

    def get_symbols(self) -> Sequence[SchematicSymbolInstance]:
        return [_symbol_item(symbol) for symbol in self._document.symbols]

    def get_lines(self) -> Sequence[SchematicLine]:
        return [_line_item(wire) for wire in self._document.wires]

    def get_labels(self) -> Sequence[BaseLabel]:
        return [_label_item(label) for label in self._document.labels]

    def get_junctions(self) -> Sequence[Junction]:
        return [_junction_item(junction) for junction in self._document.junctions]

    def get_no_connects(self) -> Sequence[NoConnectMarker]:
        return [_no_connect_item(marker) for marker in self._document.no_connects]

    def get_page_settings(self) -> PageSettings:
        paper = self._document.root.first_list("paper")
        if paper is None:
            raise KiCadSchematicError("schematic has no paper settings")
        page_size = _atom(paper, 1, "paper size").value
        orientation = "landscape"
        if len(paper.children) > 2:
            raw_orientation = paper.children[2]
            if not isinstance(raw_orientation, Atom):
                raise KiCadSchematicError("paper orientation is not an atom")
            orientation = raw_orientation.value
        return PageSettings(page_size, orientation)

    def set_page_settings(self, settings: PageSettings) -> None:
        if settings.orientation not in {"landscape", "portrait"}:
            raise KiCadSchematicError(f"unsupported paper orientation: {settings.orientation!r}")
        paper = self._document.root.first_list("paper")
        if paper is None:
            raise KiCadSchematicError("schematic has no paper settings")
        orientation = " portrait" if settings.orientation == "portrait" else ""
        replacement = f"(paper {json.dumps(settings.page_size)}{orientation})"
        self._source = _apply_edits(
            self._source,
            [_Edit(paper.start, paper.end, replacement)],
        )
        self._parse()

    def begin_commit(self) -> Commit:
        if self._open_commit is not None:
            raise KiCadSchematicError("a schematic commit is already open")
        commit = Commit(str(uuid4()))
        self._open_commit = commit
        self._commit_source = self._source
        return commit

    def push_commit(self, commit: Commit, message: str = "") -> None:
        del message
        self._require_commit(commit)
        self._open_commit = None
        self._commit_source = None

    def drop_commit(self, commit: Commit) -> None:
        self._require_commit(commit)
        assert self._commit_source is not None
        self._source = self._commit_source
        self._open_commit = None
        self._commit_source = None
        self._parse()

    def update_items(self, items: EditableItem | Sequence[EditableItem]) -> list[EditableItem]:
        requested = [items] if isinstance(items, SchematicItem) else list(items)
        if not requested:
            return []
        duplicate_ids = _duplicates(item.id for item in requested)
        if duplicate_ids:
            raise KiCadSchematicError(
                "duplicate schematic item IDs in update: " + ", ".join(sorted(duplicate_ids))
            )

        raw_by_id = self._raw_items_by_id()
        edits: list[_Edit] = []
        for item in requested:
            raw = raw_by_id.get(item.id)
            if raw is None:
                raise KiCadSchematicError(f"cannot update unknown schematic item {item.id!r}")
            edits.extend(_item_edits(raw, item))

        self._source = _apply_edits(self._source, edits)
        self._parse()
        return list(self.get_items_by_id([item.id for item in requested]))

    def create_items(self, items: EditableItem | Iterable[EditableItem]) -> list[EditableItem]:
        requested = [items] if isinstance(items, SchematicItem) else list(items)
        if not requested:
            return []
        existing_ids = {item.id for item in self.get_items()}
        created_ids: list[str] = []
        expressions: list[str] = []
        for item in requested:
            item.id = item.id or str(uuid4())
            if item.id in existing_ids or item.id in created_ids:
                raise KiCadSchematicError(f"duplicate schematic item UUID: {item.id}")
            expressions.append(_serialize_item(item))
            created_ids.append(item.id)
        insertion = "".join(f"\n\t{expression}" for expression in expressions)
        self._source = _apply_edits(
            self._source,
            [_Edit(self._document.root.end - 1, self._document.root.end - 1, insertion)],
        )
        self._parse()
        return list(self.get_items_by_id(created_ids))

    def remove_items(self, items: EditableItem | Sequence[EditableItem]) -> None:
        requested = [items] if isinstance(items, SchematicItem) else list(items)
        self.remove_items_by_id([item.id for item in requested])

    def remove_items_by_id(self, ids: str | Sequence[str]) -> None:
        requested = [ids] if isinstance(ids, str) else list(ids)
        if not requested:
            return
        duplicate_ids = _duplicates(requested)
        if duplicate_ids:
            raise KiCadSchematicError(
                "duplicate schematic item IDs in removal: " + ", ".join(sorted(duplicate_ids))
            )
        raw_by_id = self._raw_items_by_id()
        missing = set(requested) - set(raw_by_id)
        if missing:
            raise KiCadSchematicError(
                "cannot remove unknown schematic item IDs: " + ", ".join(sorted(missing))
            )
        edits = [
            _Edit(
                _top_level_item_start(self._source, raw_by_id[item_id].expression.start),
                raw_by_id[item_id].expression.end,
                "",
            )
            for item_id in requested
        ]
        self._source = _apply_edits(self._source, edits)
        self._parse()

    def save(self) -> None:
        if self._path is None:
            raise KiCadSchematicError("an in-memory schematic has no file path; use save_as")
        if self._open_commit is not None:
            raise KiCadSchematicError("cannot save while a schematic commit is open")
        self._path.write_text(self._source)

    def save_as(
        self,
        filename: str | Path,
        overwrite: bool = False,
        include_project: bool = True,
    ) -> None:
        # The file backend has no live KiCad project to copy.  Retaining this
        # parameter keeps call sites compatible with EditorCommandsHandler.
        del include_project
        if self._open_commit is not None:
            raise KiCadSchematicError("cannot save while a schematic commit is open")
        destination = Path(filename).expanduser().resolve()
        if destination.exists() and not overwrite:
            raise KiCadSchematicError(f"schematic already exists: {destination}")
        destination.write_text(self._source)

    def revert(self) -> None:
        if self._path is None:
            raise KiCadSchematicError("an in-memory schematic cannot be reverted")
        if self._open_commit is not None:
            raise KiCadSchematicError("cannot revert while a schematic commit is open")
        self._source = self._path.read_text()
        self._parse()

    def _parse(self) -> None:
        self._document = KiCadSchematicDocument.from_text(self._source)
        ids = [item.id for item in self.get_items()]
        missing = [type(item).__name__ for item in self.get_items() if not item.id]
        if missing:
            raise KiCadSchematicError(
                "editable schematic items lack UUIDs: " + ", ".join(sorted(set(missing)))
            )
        duplicate_ids = _duplicates(ids)
        if duplicate_ids:
            raise KiCadSchematicError(
                "duplicate schematic item UUIDs: " + ", ".join(sorted(duplicate_ids))
            )

    def _raw_items_by_id(
        self,
    ) -> dict[str, KiCadSymbol | KiCadWire | KiCadLabel | KiCadJunction | KiCadNoConnect]:
        items = (
            *self._document.symbols,
            *self._document.wires,
            *self._document.labels,
            *self._document.junctions,
            *self._document.no_connects,
        )
        return {item.uuid: item for item in items}

    def _require_commit(self, commit: Commit) -> None:
        if self._open_commit != commit:
            raise KiCadSchematicError("schematic commit is not open")


def _symbol_item(symbol: KiCadSymbol) -> SchematicSymbolInstance:
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
                _label_horizontal_alignment(field.expression),
                _text_vertical_alignment(field.expression),
            ),
        ),
        visible=not field.hidden,
    )


def _line_item(wire: KiCadWire) -> SchematicLine:
    if len(wire.points) != 2:
        raise KiCadSchematicError(
            f"wire {wire.uuid!r} has {len(wire.points)} points; KiCad API lines require two"
        )
    return SchematicLine(
        id=wire.uuid,
        start=Vector2.from_xy_mm(*wire.points[0]),
        end=Vector2.from_xy_mm(*wire.points[1]),
    )


def _label_item(label: KiCadLabel) -> BaseLabel:
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
                _label_horizontal_alignment(label.expression),
                _text_vertical_alignment(label.expression),
            ),
        ),
    )


def _junction_item(junction: KiCadJunction) -> Junction:
    return Junction(id=junction.uuid, position=Vector2.from_xy_mm(*junction.position))


def _no_connect_item(marker: KiCadNoConnect) -> NoConnectMarker:
    return NoConnectMarker(id=marker.uuid, position=Vector2.from_xy_mm(*marker.position))


def _text_vertical_alignment(expression: ListExpr) -> str:
    justify = _descendant(expression, ("effects", "justify"))
    if justify is not None:
        for child in justify.children[1:]:
            if isinstance(child, Atom) and child.value in {"top", "bottom"}:
                return child.value
    return "center"


def _label_horizontal_alignment(expression: ListExpr) -> str:
    justify = _descendant(expression, ("effects", "justify"))
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


def _item_edits(raw: RawItem, item: EditableItem) -> list[_Edit]:
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


def _symbol_edits(raw: KiCadSymbol, item: SchematicSymbolInstance) -> list[_Edit]:
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
            edits.append(_Edit(insertion, insertion, "\n\t\t\t(hide yes)\n\t\t\t"))
        edits.append(
            _Edit(
                _atom(raw_field.expression, 2, f"property {name}").start,
                _atom(raw_field.expression, 2, f"property {name}").end,
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


def _line_edits(raw: KiCadWire, item: SchematicLine) -> list[_Edit]:
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


def _label_edits(raw: KiCadLabel, item: BaseLabel) -> list[_Edit]:
    expected_type = {
        "label": LocalLabel,
        "global_label": GlobalLabel,
        "hierarchical_label": HierarchicalLabel,
    }[raw.kind]
    if not isinstance(item, expected_type):
        raise KiCadSchematicError(f"label {item.id!r} changed kind")
    text_atom = _atom(raw.expression, 1, f"label {item.id}")
    edits = [_Edit(text_atom.start, text_atom.end, json.dumps(item.text.value))]
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
) -> list[_Edit]:
    at = expression.first_list("at")
    if at is None:
        raise KiCadSchematicError(f"{context} has no position")
    edits = _xy_edits(at, position, context)
    if rotation is not None:
        if len(at.children) < 4:
            raise KiCadSchematicError(f"{context} has no rotation atom")
        atom = _atom(at, 3, context)
        edits.append(_Edit(atom.start, atom.end, _format_float(rotation)))
    return edits


def _xy_edits(expression: ListExpr, position: Vector2, context: str) -> list[_Edit]:
    x = _atom(expression, 1, context)
    y = _atom(expression, 2, context)
    x_mm, y_mm = position.as_mm()
    return [
        _Edit(x.start, x.end, _format_float(x_mm)),
        _Edit(y.start, y.end, _format_float(y_mm)),
    ]


def _text_size_edits(expression: ListExpr, size: Vector2, context: str) -> list[_Edit]:
    raw_size = _descendant(expression, ("effects", "font", "size"))
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
) -> list[_Edit]:
    if alignment not in {"center", "left", "right"}:
        raise KiCadSchematicError(f"unsupported {context} alignment {alignment!r}")
    current = _label_horizontal_alignment(expression)
    if vertical_alignment not in {None, "center", "top", "bottom"}:
        raise KiCadSchematicError(
            f"unsupported {context} vertical alignment {vertical_alignment!r}",
        )
    if (current == alignment
            and (vertical_alignment is None
                 or _text_vertical_alignment(expression) == vertical_alignment)):
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
        return [_Edit(justify.start, justify.end, replacement)]
    insertion = effects.end - 1
    return [_Edit(insertion, insertion, f"\n\t\t\t{replacement}" if replacement else "")]


def _format_float(value: float) -> str:
    if abs(value) < 0.0000005:
        value = 0.0
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _top_level_item_start(source: str, expression_start: int) -> int:
    """Include a top-level item's indentation and leading newline in deletion."""

    newline = source.rfind("\n", 0, expression_start)
    line_start = newline + 1
    if source[line_start:expression_start].strip():
        return expression_start
    return newline if newline >= 0 else 0


def _serialize_item(item: EditableItem) -> str:
    if isinstance(item, SchematicLine):
        if item.type != "wire":
            raise KiCadSchematicError(f"unsupported schematic line type {item.type!r}")
        start_x, start_y = item.start.as_mm()
        end_x, end_y = item.end.as_mm()
        return (
            "(wire\n"
            f"\t\t(pts (xy {_format_float(start_x)} {_format_float(start_y)}) "
            f"(xy {_format_float(end_x)} {_format_float(end_y)}))\n"
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
            f"(at {_format_float(x)} {_format_float(y)} "
            f"{_format_float(item.text.attributes.angle)})\n"
            f"\t\t(effects (font (size {_format_float(size_x)} {_format_float(size_y)}))"
            f"{justify})\n"
            f'\t\t(uuid "{item.id}"))'
        )
    if isinstance(item, Junction):
        x, y = item.position.as_mm()
        return (
            f"(junction (at {_format_float(x)} {_format_float(y)})\n"
            "\t\t(diameter 0)\n"
            "\t\t(color 0 0 0 0)\n"
            f'\t\t(uuid "{item.id}"))'
        )
    if isinstance(item, NoConnectMarker):
        x, y = item.position.as_mm()
        return f'(no_connect (at {_format_float(x)} {_format_float(y)}) (uuid "{item.id}"))'
    raise KiCadSchematicError(f"creating {type(item).__name__} is not supported")
