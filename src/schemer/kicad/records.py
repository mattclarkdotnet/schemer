from __future__ import annotations

from dataclasses import dataclass

from schemer.kicad.syntax import ListExpr


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
