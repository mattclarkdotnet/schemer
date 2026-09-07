"""Scale-independent layout measurements and optional render diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from math import inf
from typing import Any

from schemer.layout import Position
from schemer.symbol_geometry import (
    placed_symbol_body_bounds,
    rotated_offset,
    symbol_local_bounds,
    symbol_pin_offsets,
)
from schemer.toolchain import ToolchainError

_VIEWER_UNITS_PER_MM = 10.0
_ANNOTATION_CHARACTER_WIDTH = 7.0
_ANNOTATION_HORIZONTAL_MARGIN = 16.0
_ANNOTATION_VERTICAL_MARGIN = 32.0
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


def _attribute_string(instance: dict[str, Any], name: str) -> str:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    value = attributes.get(name)
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, dict) and isinstance(value.get("String"), str):
        return value["String"].casefold()
    return ""


def _position(raw: dict[str, Any]) -> Position:
    return Position(
        x=float(raw["x"]),
        y=float(raw["y"]),
        rotation=float(raw.get("rotation", 0)),
        mirror=raw.get("mirror"),
    )


def _symbol_envelope(instance: dict[str, Any], position: Position) -> Envelope:
    if position.mirror is not None:
        raise ToolchainError("sheet-scale metrics do not yet support mirrored symbols")
    bounds = symbol_local_bounds(instance)
    origin_x = position.x - bounds.min_x * _VIEWER_UNITS_PER_MM
    origin_y = position.y + bounds.max_y * _VIEWER_UNITS_PER_MM
    corners = (
        (bounds.min_x, bounds.min_y),
        (bounds.min_x, bounds.max_y),
        (bounds.max_x, bounds.min_y),
        (bounds.max_x, bounds.max_y),
    )
    transformed = [
        (
            origin_x + rotated_offset(corner, position.rotation)[0] * _VIEWER_UNITS_PER_MM,
            origin_y + rotated_offset(corner, position.rotation)[1] * _VIEWER_UNITS_PER_MM,
        )
        for corner in corners
    ]
    return Envelope(
        min(point[0] for point in transformed),
        min(point[1] for point in transformed),
        max(point[0] for point in transformed),
        max(point[1] for point in transformed),
    )


def _component_ref(module_ref: str, symbol_id: str) -> str:
    local_ref = symbol_id.removeprefix("comp:").split("@", 1)[0]
    return f"{module_ref}.{local_ref}"


def _module_offsets(schematic: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Resolve placed child-module origins from their nearest placed ancestor."""

    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")

    modules = {
        ref
        for ref, instance in instances.items()
        if isinstance(ref, str) and isinstance(instance, dict) and instance.get("kind") == "Module"
    }
    offsets: dict[str, tuple[float, float]] = {root_ref: (0.0, 0.0)}
    for module_ref in sorted(modules - {root_ref}, key=lambda ref: ref.count(".")):
        ancestors = [ancestor for ancestor in offsets if module_ref.startswith(ancestor + ".")]
        if not ancestors:
            continue
        ancestor_ref = max(ancestors, key=len)
        ancestor = instances[ancestor_ref]
        positions = ancestor.get("symbol_positions")
        if not isinstance(positions, dict):
            continue
        local_path = module_ref.removeprefix(ancestor_ref + ".")
        raw = positions.get(f"comp:{local_path}")
        if not isinstance(raw, dict):
            continue
        parent_x, parent_y = offsets[ancestor_ref]
        offsets[module_ref] = (parent_x + float(raw["x"]), parent_y + float(raw["y"]))
    return offsets


def _placed_component_units(
    schematic: dict[str, Any],
    *,
    body_only: bool = False,
) -> tuple[PlacedComponent, ...]:
    """Collect individual visible units before physical-component grouping."""

    instances = schematic.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances must be an object")
    offsets = _module_offsets(schematic)
    result: list[PlacedComponent] = []
    for module_ref, (offset_x, offset_y) in offsets.items():
        module = instances.get(module_ref)
        positions = module.get("symbol_positions") if isinstance(module, dict) else None
        if not isinstance(positions, dict):
            continue
        for symbol_id, raw in positions.items():
            if (
                not isinstance(symbol_id, str)
                or not symbol_id.startswith("comp:")
                or not isinstance(raw, dict)
            ):
                continue
            component_ref = _component_ref(module_ref, symbol_id)
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            try:
                position = _position(raw)
                if body_only:
                    bounds = placed_symbol_body_bounds(component, position)
                    envelope = Envelope(
                        bounds.min_x,
                        bounds.min_y,
                        bounds.max_x,
                        bounds.max_y,
                    )
                else:
                    envelope = _symbol_envelope(component, position)
                envelope = envelope.translated(offset_x, offset_y)
            except ToolchainError:
                continue
            result.append(
                PlacedComponent(
                    instance_ref=component_ref,
                    component_type=_attribute_string(component, "type"),
                    pin_geometry_count=len(symbol_pin_offsets(component)),
                    envelope=envelope,
                )
            )

    return tuple(
        sorted(
            result,
            key=lambda component: (
                component.instance_ref,
                component.envelope.min_x,
                component.envelope.min_y,
            ),
        )
    )


def placed_components(schematic: dict[str, Any]) -> tuple[PlacedComponent, ...]:
    """Collect the union envelope of each visible physical component."""

    grouped: dict[str, PlacedComponent] = {}
    for unit in _placed_component_units(schematic):
        previous = grouped.get(unit.instance_ref)
        if previous is None:
            grouped[unit.instance_ref] = unit
            continue
        grouped[unit.instance_ref] = PlacedComponent(
            instance_ref=unit.instance_ref,
            component_type=unit.component_type,
            pin_geometry_count=max(previous.pin_geometry_count, unit.pin_geometry_count),
            envelope=previous.envelope.union(unit.envelope),
        )

    return tuple(sorted(grouped.values(), key=lambda component: component.instance_ref))


def placed_component_bodies(schematic: dict[str, Any]) -> tuple[PlacedComponent, ...]:
    """Collect each actual painted body without filling gaps between package units."""

    return _placed_component_units(schematic, body_only=True)


def _display_attribute(instance: dict[str, Any], name: str) -> str:
    """Return one viewer-facing string without changing its case."""

    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    value = attributes.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("String"), str):
        return value["String"]
    return ""


def _with_annotation_envelope(envelope: Envelope, labels: tuple[str, ...]) -> Envelope:
    """Conservatively include the visible reference/value or net caption."""

    longest = max((len(label) for label in labels if label), default=0)
    half_width = longest * _ANNOTATION_CHARACTER_WIDTH / 2
    horizontal = half_width + _ANNOTATION_HORIZONTAL_MARGIN
    return Envelope(
        envelope.min_x - horizontal,
        envelope.min_y - _ANNOTATION_VERTICAL_MARGIN,
        envelope.max_x + horizontal,
        envelope.max_y + _ANNOTATION_VERTICAL_MARGIN,
    )


def _point_distance_to_envelope(x: float, y: float, envelope: Envelope) -> float:
    delta_x = max(envelope.min_x - x, 0.0, x - envelope.max_x)
    delta_y = max(envelope.min_y - y, 0.0, y - envelope.max_y)
    return delta_x * delta_x + delta_y * delta_y


def _component_top_level_group_envelopes(
    schematic: dict[str, Any],
) -> dict[str, Envelope]:
    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")

    envelopes: dict[str, Envelope] = {}
    for component in placed_components(schematic):
        if not component.instance_ref.startswith(root_ref + "."):
            continue
        local_ref = component.instance_ref.removeprefix(root_ref + ".")
        group_name = local_ref.split(".", 1)[0]
        instance = instances.get(component.instance_ref)
        if not isinstance(instance, dict):
            continue
        reference = instance.get("reference_designator")
        labels = (
            reference if isinstance(reference, str) else "",
            _display_attribute(instance, "value") or _display_attribute(instance, "Value"),
        )
        visible = _with_annotation_envelope(component.envelope, labels)
        previous = envelopes.get(group_name)
        envelopes[group_name] = visible if previous is None else previous.union(visible)
    return envelopes


def top_level_root_symbol_groups(schematic: dict[str, Any]) -> dict[str, str]:
    """Assign root positions to the electrically relevant group that owns them.

    A net-symbol position is a separate record in Zener's persisted layout, but
    it is not an independent layout object.  It terminates a local wire branch
    belonging to one of the top-level groups connected to that net.  Geometry
    is used only to distinguish multiple displayed copies of the same shared
    net; it must never attach a symbol to an electrically unrelated group.
    """

    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if (
        not isinstance(root_ref, str)
        or not isinstance(instances, dict)
        or not isinstance(nets, dict)
    ):
        raise ToolchainError("schematic root, instances, or nets are invalid")
    root = instances.get(root_ref)
    positions = root.get("symbol_positions") if isinstance(root, dict) else None
    if not isinstance(positions, dict):
        raise ToolchainError("schematic root positions are invalid")
    component_envelopes = _component_top_level_group_envelopes(schematic)
    assignments: dict[str, str] = {}
    for symbol_id, raw in positions.items():
        if not isinstance(symbol_id, str) or not isinstance(raw, dict):
            continue
        if symbol_id.startswith("comp:"):
            group_name = symbol_id.removeprefix("comp:").split(".", 1)[0]
            if group_name in component_envelopes:
                assignments[symbol_id] = group_name
            continue
        if not symbol_id.startswith("sym:") or not component_envelopes:
            continue
        net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
        if not separator or not suffix.isdigit():
            continue
        net = nets.get(net_name)
        if not isinstance(net, dict):
            matches = [
                candidate
                for candidate in nets.values()
                if isinstance(candidate, dict) and candidate.get("name") == net_name
            ]
            if len(matches) != 1:
                continue
            net = matches[0]
        ports = net.get("ports")
        if not isinstance(ports, (list, tuple)):
            continue
        connected_groups = {
            port.removeprefix(root_ref + ".").split(".", 1)[0]
            for port in ports
            if isinstance(port, str) and port.startswith(root_ref + ".")
        } & component_envelopes.keys()
        if not connected_groups:
            continue
        position = _position(raw)
        assignments[symbol_id] = min(
            connected_groups,
            key=lambda name: (
                _point_distance_to_envelope(position.x, position.y, component_envelopes[name]),
                name,
            ),
        )
    return assignments


def top_level_group_envelopes(schematic: dict[str, Any]) -> dict[str, Envelope]:
    """Measure complete visible geometry for each first-level functional group.

    Component geometry includes pins plus conservative reference/value text.
    Net-symbol drawings and captions are included as well. Viewer-created wires
    join endpoints already inside those bounds, so a non-looping Manhattan wire
    cannot escape the resulting envelope.
    """

    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")
    if not isinstance(nets, dict):
        raise ToolchainError("schematic nets are invalid")

    envelopes = _component_top_level_group_envelopes(schematic)
    root_assignments = top_level_root_symbol_groups(schematic)

    offsets = _module_offsets(schematic)
    nets_by_name = {
        net.get("name"): net
        for net in nets.values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }
    for module_ref, (offset_x, offset_y) in offsets.items():
        module = instances.get(module_ref)
        positions = module.get("symbol_positions") if isinstance(module, dict) else None
        if not isinstance(positions, dict):
            continue
        for symbol_id, raw in positions.items():
            if (
                not isinstance(symbol_id, str)
                or not symbol_id.startswith("sym:")
                or not isinstance(raw, dict)
            ):
                continue
            net_name, separator, index = symbol_id.removeprefix("sym:").rpartition("#")
            if not separator or not index.isdigit():
                continue
            position = _position(raw)
            anchor_x = offset_x + position.x
            anchor_y = offset_y + position.y
            if module_ref == root_ref:
                group_name = root_assignments.get(symbol_id)
                if group_name is None:
                    continue
            else:
                group_name = module_ref.removeprefix(root_ref + ".").split(".", 1)[0]
                if group_name not in envelopes:
                    continue

            net = nets_by_name.get(net_name)
            properties = net.get("properties") if isinstance(net, dict) else None
            if isinstance(properties, dict):
                try:
                    local = _symbol_envelope({"attributes": properties}, position)
                    visible = local.translated(offset_x, offset_y)
                except ToolchainError:
                    visible = Envelope(anchor_x, anchor_y, anchor_x, anchor_y)
            else:
                visible = Envelope(anchor_x, anchor_y, anchor_x, anchor_y)
            visible = _with_annotation_envelope(visible, (net_name,))
            envelopes[group_name] = envelopes[group_name].union(visible)

    return envelopes


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
