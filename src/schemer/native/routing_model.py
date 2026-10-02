from __future__ import annotations

from dataclasses import dataclass

from schemer.kicad.geometry.envelopes import Envelope
from schemer.kicad.items import Vector2

PIN_STUB_MM = 2.54
PARALLEL_WIRE_CLEARANCE = 1_270_000
CROSSING_ENDPOINT_CLEARANCE = 1_270_000


@dataclass(frozen=True)
class PlacedEndpoint:
    position: Vector2
    side: str | None = None
    owner: str | None = None
    group: str | None = None
    members: tuple[PlacedEndpoint, ...] = ()
    rail_class: str | None = None
    stroke: Envelope | None = None
    bank: tuple[str, str] | None = None
    escape_length: int | None = None
