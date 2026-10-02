from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    """A point in the viewer's schematic coordinate system."""

    x: float
    y: float


@dataclass(frozen=True)
class SymbolBounds:
    """Unrotated KiCad symbol bounds in millimetres, before viewer expansion."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(frozen=True)
class PlacedBounds:
    """Axis-aligned symbol bounds in the viewer coordinate system."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def center_x(self) -> float:
        return (self.min_x + self.max_x) / 2

    @property
    def center_y(self) -> float:
        return (self.min_y + self.max_y) / 2

    def union(self, other: PlacedBounds) -> PlacedBounds:
        return PlacedBounds(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )

    def translated(self, delta_x: float, delta_y: float) -> PlacedBounds:
        return PlacedBounds(
            self.min_x + delta_x,
            self.min_y + delta_y,
            self.max_x + delta_x,
            self.max_y + delta_y,
        )


NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
VIEWER_UNITS_PER_MM = 10.0
VIEWER_BOUNDS_EXPANSION_MM = 0.1
