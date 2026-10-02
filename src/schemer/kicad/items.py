from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from schemer.core.errors import KiCadSchematicError

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
    # KiCad 11 API's hierarchical SheetPath. The project writer manages those
    # paths while preserving this original source identity across sheets.
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


def place_symbol(
    item: SchematicSymbolInstance,
    target: Vector2,
    rotation: float,
) -> None:
    """Set a symbol pose; translate its fields without rotating their offsets."""
    delta_x = target.x - item.position.x
    delta_y = target.y - item.position.y
    item.position = target
    item.transform.orientation = rotation
    for field in item.fields:
        field.text.position = Vector2(
            field.text.position.x + delta_x,
            field.text.position.y + delta_y,
        )


def translate_item(item: EditableItem, delta: Vector2) -> None:
    """Move a complete drawing item without changing its orientation or style."""
    def shift(point: Vector2) -> Vector2:
        return Vector2(point.x + delta.x, point.y + delta.y)

    if isinstance(item, SchematicSymbolInstance):
        place_symbol(item, shift(item.position), item.transform.orientation)
    elif isinstance(item, SchematicLine):
        item.start, item.end = shift(item.start), shift(item.end)
    else:
        item.position = shift(item.position)
        if isinstance(item, BaseLabel):
            item.text.position = item.position
