from __future__ import annotations

from dataclasses import dataclass

_NON_IC_TYPES = {
    "capacitor",
    "connector",
    "diode",
    "ferrite_bead",
    "inductor",
    "mechanical",
    "mounting_hole",
    "resistor",
    "test_point",
    "transformer",
}


@dataclass(frozen=True)
class Envelope:
    """Axis-aligned bounds in the viewer coordinate system."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center_x(self) -> float:
        return (self.min_x + self.max_x) / 2

    @property
    def center_y(self) -> float:
        return (self.min_y + self.max_y) / 2

    def translated(self, x: float, y: float) -> Envelope:
        return Envelope(
            self.min_x + x,
            self.min_y + y,
            self.max_x + x,
            self.max_y + y,
        )

    def union(self, other: Envelope) -> Envelope:
        return Envelope(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )


@dataclass(frozen=True)
class PlacedComponent:
    """One physical component and the union of all its visible units."""

    instance_ref: str
    component_type: str
    pin_geometry_count: int
    envelope: Envelope

    @property
    def is_ic(self) -> bool:
        return self.pin_geometry_count >= 6 and self.component_type not in _NON_IC_TYPES


@dataclass(frozen=True)
class PrimaryAnchorMetrics:
    """Post-layout diagnostics for primary position within one output framing."""

    primary_ref: str
    primary_pin_geometry_count: int
    primary_height_ratio: float
    fitted_height_ratio: float
    horizontal_center_offset_ratio: float
    vertical_center_offset_ratio: float
    page: Envelope
    primary: Envelope

    def as_dict(self) -> dict[str, object]:
        return {
            "horizontal_center_offset_ratio": round(self.horizontal_center_offset_ratio, 4),
            "fitted_height_ratio": round(self.fitted_height_ratio, 4),
            "page": {
                "height": round(self.page.height, 4),
                "width": round(self.page.width, 4),
            },
            "primary_height_ratio": round(self.primary_height_ratio, 4),
            "primary_pin_geometry_count": self.primary_pin_geometry_count,
            "primary_ref": self.primary_ref,
            "vertical_center_offset_ratio": round(self.vertical_center_offset_ratio, 4),
        }


@dataclass(frozen=True)
class SheetLegibilityMetrics:
    """Fitted overview scale expressed as the size of ordinary schematic text."""

    viewport_width: int
    viewport_height: int
    nominal_text_height_mm: float
    fitted_pixels_per_viewer_unit: float
    nominal_text_pixels: float
    page: Envelope

    def as_dict(self) -> dict[str, object]:
        return {
            "fitted_pixels_per_viewer_unit": round(self.fitted_pixels_per_viewer_unit, 4),
            "nominal_text_height_mm": self.nominal_text_height_mm,
            "nominal_text_pixels": round(self.nominal_text_pixels, 2),
            "page": {
                "height": round(self.page.height, 4),
                "width": round(self.page.width, 4),
            },
            "viewport_height": self.viewport_height,
            "viewport_width": self.viewport_width,
        }
