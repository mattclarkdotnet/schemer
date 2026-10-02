from __future__ import annotations

from math import inf
from typing import Any

from schemer.analysis.drawing_model import (
    Envelope,
    PlacedComponent,
    PrimaryAnchorMetrics,
    SheetLegibilityMetrics,
)
from schemer.analysis.measurements import placed_components
from schemer.core.errors import ToolchainError

_VIEWER_UNITS_PER_MM = 10.0


def _page_envelope(components: tuple[PlacedComponent, ...]) -> Envelope:
    if not components:
        raise ToolchainError("schematic has no placed physical components")
    page = Envelope(inf, inf, -inf, -inf)
    for component in components:
        page = page.union(component.envelope)
    return page


def primary_component(schematic: dict[str, Any]) -> PlacedComponent:
    """Select the structurally largest placed IC without reference to a page."""

    components = placed_components(schematic)
    if not components:
        raise ToolchainError("schematic has no placed physical components")
    ic_candidates = [component for component in components if component.is_ic]
    if not ic_candidates:
        raise ToolchainError("schematic has no placed IC candidate")
    return max(
        ic_candidates,
        key=lambda component: (
            component.pin_geometry_count,
            component.envelope.height,
            component.envelope.width,
            component.instance_ref,
        ),
    )


def primary_anchor_metrics(
    schematic: dict[str, Any], *, viewport_aspect_ratio: float = 1.5
) -> PrimaryAnchorMetrics:
    """Report legacy fitted-page diagnostics for a selected primary IC.

    These values describe a particular output framing. They are not layout
    inputs or acceptance requirements.
    """

    components = placed_components(schematic)
    primary = primary_component(schematic)
    page = _page_envelope(components)
    fitted_scale = min(viewport_aspect_ratio / page.width, 1.0 / page.height)
    return PrimaryAnchorMetrics(
        primary_ref=primary.instance_ref,
        primary_pin_geometry_count=primary.pin_geometry_count,
        primary_height_ratio=primary.envelope.height / page.height,
        fitted_height_ratio=primary.envelope.height * fitted_scale,
        horizontal_center_offset_ratio=abs(primary.envelope.center_x - page.center_x) / page.width,
        vertical_center_offset_ratio=abs(primary.envelope.center_y - page.center_y) / page.height,
        page=page,
        primary=primary.envelope,
    )


def sheet_legibility_metrics(
    schematic: dict[str, Any],
    *,
    viewport_width: int = 2400,
    viewport_height: int = 1600,
    nominal_text_height_mm: float = 1.27,
) -> SheetLegibilityMetrics:
    """Measure ordinary text after the whole sheet is fitted to the viewport."""

    if viewport_width <= 0 or viewport_height <= 0:
        raise ToolchainError("legibility viewport dimensions must be positive")
    if nominal_text_height_mm <= 0:
        raise ToolchainError("nominal schematic text height must be positive")
    page = _page_envelope(placed_components(schematic))
    fitted_scale = min(viewport_width / page.width, viewport_height / page.height)
    nominal_pixels = nominal_text_height_mm * _VIEWER_UNITS_PER_MM * fitted_scale
    return SheetLegibilityMetrics(
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        nominal_text_height_mm=nominal_text_height_mm,
        fitted_pixels_per_viewer_unit=fitted_scale,
        nominal_text_pixels=nominal_pixels,
        page=page,
    )
