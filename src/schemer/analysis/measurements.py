from __future__ import annotations

from typing import Any

from schemer.analysis.drawing_model import Envelope, PlacedComponent
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position, position_from_viewer
from schemer.symbols.geometry import placed_symbol_body_bounds, placed_symbol_bounds
from schemer.symbols.library import symbol_pin_offsets

_ANNOTATION_CHARACTER_WIDTH = 7.0
_ANNOTATION_HORIZONTAL_MARGIN = 16.0
_ANNOTATION_VERTICAL_MARGIN = 32.0


def _symbol_envelope(instance: dict[str, Any], position: Position) -> Envelope:
    if position.mirror is not None:
        raise ToolchainError("sheet-scale metrics do not yet support mirrored symbols")
    bounds = placed_symbol_bounds(instance, position)
    return Envelope(
        bounds.min_x,
        bounds.min_y,
        bounds.max_x,
        bounds.max_y,
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
                position = position_from_viewer(raw)
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
                    component_type=(attribute_string(component, "type") or "").casefold(),
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


def with_annotation_envelope(envelope: Envelope, labels: tuple[str, ...]) -> Envelope:
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
            attribute_string(instance, "value") or attribute_string(instance, "Value") or "",
        )
        visible = with_annotation_envelope(component.envelope, labels)
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
        position = position_from_viewer(raw)
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
            position = position_from_viewer(raw)
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
            visible = with_annotation_envelope(visible, (net_name,))
            envelopes[group_name] = envelopes[group_name].union(visible)

    return envelopes
