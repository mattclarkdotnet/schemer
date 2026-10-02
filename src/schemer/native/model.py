from __future__ import annotations

from dataclasses import dataclass

from schemer.analysis.inventory import StructuralInventory
from schemer.core.layout import Position
from schemer.kicad.items import Vector2
from schemer.native.routing_model import PlacedEndpoint


@dataclass(frozen=True)
class NativeLayoutReport:
    component_count: int
    hidden_component_count: int
    power_symbol_count: int
    label_count: int
    wire_count: int
    junction_count: int
    no_connect_count: int
    page_size: str
    structural_inventory: StructuralInventory


@dataclass(frozen=True)
class NetSymbolTarget:
    net_name: str
    display_name: str
    position: Vector2
    rotation: float
    rail: bool
    ground: bool
    text_alignment: str = "center"
    owner: str | None = None
    group: str | None = None
    members: tuple[PlacedEndpoint, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class PositionedNetSymbol:
    net_name: str
    symbol_id: str
    position: Position
    group: str | None
