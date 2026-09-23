"""Project accepted Zener presentation positions into a KiCad schematic."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from math import cos, hypot, radians, sin, sqrt
from statistics import median
from typing import Any

from schemer.kicad_api import (
    BaseLabel,
    FileSchematic,
    Junction,
    LocalLabel,
    NoConnectMarker,
    PageSettings,
    SchematicLine,
    SchematicSymbolInstance,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.kicad_bridge import KiCadComponentAssociation, associate_components
from schemer.kicad_connectivity import expected_pin_nets
from schemer.kicad_geometry import (
    _library_definition,
    _pins_for_unit,
    placed_pin_positions,
    placed_pin_segments,
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad_schematic import KiCadSchematicError, KiCadSymbol, _atom, _descendant
from schemer.layout import Position
from schemer.layout_metrics import top_level_root_symbol_groups
from schemer.roles import RoleSource, component_roles, schematic_properties
from schemer.symbol_geometry import (
    net_symbol_pin_position,
    net_with_default_signal_symbol,
    placed_symbol_origin,
    symbol_pin_electrical_types,
    symbol_pin_number_groups,
)
from schemer.view_policy import clean_schematic_labels, electrical_view

_VIEWER_UNITS_PER_MM = 10.0
_MARGIN_MM = 10.0
_PIN_STUB_MM = 2.54
_BLOCK_CLEARANCE_MM = 20.32
_LOCAL_RAIL_CLUSTER_MM = 25.4
_LOCAL_RAIL_OFFSET_MM = 2.54
_RAIL_CORRIDOR_PITCH_MM = 2.54
_MULTI_UNIT_PITCH_MM = 15.24
_LANDSCAPE_RATIO = sqrt(2)
_LOG = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _PlacedEndpoint:
    position: Vector2
    side: str | None = None
    owner: str | None = None
    group: str | None = None
    members: tuple[_PlacedEndpoint, ...] = ()
    rail_class: str | None = None
    stroke: _Envelope | None = None
    bank: tuple[str, str] | None = None


@dataclass(frozen=True)
class _NetSymbolTarget:
    net_name: str
    display_name: str
    position: Vector2
    rotation: float
    rail: bool
    ground: bool
    text_alignment: str = "center"
    owner: str | None = None
    group: str | None = None
    members: tuple[_PlacedEndpoint, ...] = ()


@dataclass(frozen=True)
class _PositionedNetSymbol:
    net_name: str
    symbol_id: str
    position: Position
    group: str | None


@dataclass(frozen=True)
class _Envelope:
    min_x: int
    min_y: int
    max_x: int
    max_y: int

    @property
    def width(self) -> int:
        return self.max_x - self.min_x

    @property
    def height(self) -> int:
        return self.max_y - self.min_y

    @property
    def center_x(self) -> int:
        return round((self.min_x + self.max_x) / 2)

    @property
    def center_y(self) -> int:
        return round((self.min_y + self.max_y) / 2)

    def translated(self, delta: Vector2) -> _Envelope:
        return _Envelope(
            self.min_x + delta.x,
            self.min_y + delta.y,
            self.max_x + delta.x,
            self.max_y + delta.y,
        )


def _ordered_unit_position(base_x: float, base_y: float, index: int) -> tuple[float, float]:
    """Place one physical component's units on a shared vertical axis."""

    return base_x, base_y + index * _MULTI_UNIT_PITCH_MM * _VIEWER_UNITS_PER_MM


def layout_kicad_from_zener(
    schematic: dict[str, Any],
    editor: FileSchematic,
    *,
    right_of: tuple[tuple[str, str], ...] = (),
) -> NativeLayoutReport:
    """Apply accepted procedural positions and rebuild presentation wiring."""

    visible = electrical_view(schematic)
    _validate_owned_networks(visible)
    associations = associate_components(visible, editor.document, allow_unexpected=True)
    layout_groups, connector_groups = _component_layout_groups(visible)
    # Electrical-view filtering owns the decision to suppress service-only
    # components and their nearby root symbols. Read both component and
    # net-symbol positions from that view: service parts are intentionally
    # absent from the accepted electrical layout and are hidden below.
    physical_positions, net_positions = _flatten_positions(visible)
    visible_refs = {
        ref
        for ref, instance in visible["instances"].items()
        if isinstance(instance, dict) and instance.get("reference_designator")
    }

    component_targets: dict[str, tuple[float, float, float]] = {}
    for association in associations:
        position = physical_positions.get(association.instance_ref)
        instance = schematic["instances"].get(association.instance_ref)
        if position is None or not isinstance(instance, dict):
            raise KiCadSchematicError(
                f"accepted layout has no component position for {association.path!r}"
            )
        origin_x, origin_y = placed_symbol_origin(instance, position)
        component_targets[association.path] = (origin_x, origin_y, position.rotation)

    visible_targets = [
        component_targets[association.path]
        for association in associations
        if association.instance_ref in visible_refs
    ]
    translation = _drawing_translation(visible_targets)

    typed_by_id = {item.id: item for item in editor.get_symbols()}
    component_updates: list[SchematicSymbolInstance] = []
    component_groups: dict[str, str] = {}
    visible_paths = {association.path for association in associations}
    hidden_ids: list[str] = [
        symbol.uuid
        for symbol in editor.document.symbols
        if symbol.path is not None and symbol.path not in visible_paths
    ]
    for association in associations:
        assert association.instance_ref in visible_refs
        base_x, base_y, rotation = component_targets[association.path]
        ordered = sorted(association.symbols, key=lambda symbol: symbol.unit)
        for index, raw_symbol in enumerate(ordered):
            item = typed_by_id[raw_symbol.uuid]
            if len(ordered) == 1:
                # Viewer rotations are clockwise; KiCad stores counterclockwise.
                unit_x, unit_y, unit_rotation = base_x, base_y, (-rotation) % 360
            else:
                # KiCad retains separately drawn units for one physical
                # component.  Preserve that package identity visually: units
                # share one axis and follow their native unit order, as they
                # do in a conventional datasheet package diagram.  A grid of
                # independent-looking gates loses that relationship and lets
                # adjacent rows' captions and wires collide.
                unit_x, unit_y = _ordered_unit_position(base_x, base_y, index)
                unit_rotation = 0.0
            target = Vector2.from_xy_mm(
                (unit_x + translation[0]) / _VIEWER_UNITS_PER_MM,
                (unit_y + translation[1]) / _VIEWER_UNITS_PER_MM,
            )
            _translate_symbol(item, target, unit_rotation)
            component_updates.append(item)
            component_groups[item.id] = layout_groups[association.instance_ref]

    editor.update_items(component_updates)
    _align_single_pin_attachments(visible, editor)
    _compact_inline_connections(visible, editor)
    bank_ids = _align_authored_inline_banks(visible, editor)
    _place_vertical_pin_bias_branches(visible, editor)
    _place_top_pin_bypasses(visible, editor)
    _align_perpendicular_branches(visible, editor)
    _align_same_face_terminal_exits(visible, editor)
    _place_owned_shunt_banks(visible, editor)
    _place_owned_pin_networks(visible, editor)
    component_updates = [
        item for item in editor.get_symbols() if item.id in component_groups
    ]
    primary_group = _primary_group(editor, associations, layout_groups)
    display = clean_schematic_labels(visible)
    _apply_component_display_values(display, associations, component_updates)
    editor.update_items(component_updates)
    component_updates = _position_component_fields(editor, component_updates)
    _position_bank_fields(editor, component_updates, bank_ids)
    _consolidate_multi_unit_fields(editor, associations, component_updates)
    editor.update_items(component_updates)

    component_endpoints = _component_net_endpoints(visible, editor)
    display_by_net = _display_names(schematic)
    targets = _net_symbol_targets(
        schematic,
        net_positions,
        display_by_net,
        translation,
        {},
        Vector2(0, 0),
        component_endpoints,
    )
    templates = targets
    if _space_labeled_pin_connections(visible, editor, templates, component_endpoints):
        _align_authored_inline_banks(visible, editor)
        component_endpoints = _component_net_endpoints(visible, editor)
        component_updates = [s for s in editor.get_symbols() if s.id in component_groups]
    glyph_bounds = _rail_glyph_bounds(editor)
    caption_obstacles = _component_annotation_obstacles(editor, component_groups)
    targets = _localize_net_targets(
        templates, component_endpoints, glyph_bounds, caption_obstacles,
        _component_body_obstacles(editor, component_groups),
    )
    if _clear_rail_attachment_exits(visible, editor, targets, component_endpoints):
        component_endpoints = _component_net_endpoints(visible, editor)
        caption_obstacles = _component_annotation_obstacles(editor, component_groups)
        targets = _localize_net_targets(
            templates, component_endpoints, glyph_bounds, caption_obstacles,
            _component_body_obstacles(editor, component_groups),
        )
    targets = _prune_unused_targets(targets, component_endpoints)
    (
        power_updates,
        label_creates,
        label_net_names,
        label_anchors,
        target_endpoints,
        used_power_ids,
        target_item_groups,
    ) = (
        _place_net_symbols(
            editor,
            targets,
        )
    )
    editor.update_items(power_updates)

    obsolete_ids = [
        *[item.id for item in editor.get_lines()],
        *[item.id for item in editor.get_labels()],
        *[item.id for item in editor.get_junctions()],
        *[item.id for item in editor.get_no_connects()],
        *hidden_ids,
        *[
            item.id
            for item in editor.get_symbols()
            if item.zener_path is None and item.id not in used_power_ids
        ],
    ]
    editor.remove_items_by_id(obsolete_ids)
    created_labels = editor.create_items(label_creates)
    for label, net_name, anchor in zip(
        created_labels, label_net_names, label_anchors, strict=True
    ):
        assert isinstance(label, BaseLabel)
        target_endpoints[net_name].append(replace(anchor, position=label.position))
        if anchor.group is not None:
            target_item_groups[label.id] = anchor.group

    routing_items: list[SchematicLine | Junction | NoConnectMarker] = []
    routing_groups: list[str | None] = []
    body_obstacles = _component_body_obstacles(editor, component_groups)
    for net in visible["nets"].values():
        if not isinstance(net, dict) or not isinstance(net.get("name"), str):
            continue
        net_name = net["name"]
        endpoints = component_endpoints.get(net_name, [])
        if net.get("kind") == "NotConnected":
            for endpoint in endpoints:
                routing_items.append(NoConnectMarker(id="", position=endpoint.position))
                routing_groups.append(endpoint.group)
            continue
        symbols = target_endpoints.get(net_name, [])
        obstacles = {group: list(boxes) for group, boxes in body_obstacles.items()}
        foreign_wires: dict[str | None, list[SchematicLine]] = defaultdict(list)
        for item, group in zip(routing_items, routing_groups, strict=True):
            if isinstance(item, SchematicLine):
                foreign_wires[group].append(item)
        for name, pins in component_endpoints.items():
            if name != net_name:
                for p in pins:
                    obstacles.setdefault(p.group, []).append(p.stroke or _Envelope(
                        p.position.x - 250_000, p.position.y - 250_000,
                        p.position.x + 250_000, p.position.y + 250_000,
                    ))
        if symbols:
            groups: dict[int, list[_PlacedEndpoint]] = defaultdict(list)
            for endpoint in endpoints:
                nearest = _nearest_local_target(endpoint, symbols)
                groups[nearest].append(endpoint)
            for index, symbol in enumerate(symbols):
                created = _route_group([*groups[index], symbol],
                                       obstacles=obstacles.get(symbol.group, []),
                                       foreign_wires=foreign_wires[symbol.group])
                routing_items.extend(created)
                routing_groups.extend([symbol.group] * len(created))
        else:
            endpoint_groups = {endpoint.group for endpoint in endpoints}
            group = endpoint_groups.pop() if len(endpoint_groups) == 1 else None
            created = _route_group(endpoints, obstacles=obstacles.get(group, []),
                                   foreign_wires=foreign_wires[group])
            routing_items.extend(created)
            routing_groups.extend([group] * len(created))

    created_routing = editor.create_items(routing_items)
    item_groups = {**component_groups, **target_item_groups}
    for item, group in zip(created_routing, routing_groups, strict=True):
        if group is not None:
            item_groups[item.id] = group
    _clear_final_label_wires(editor, item_groups)
    field_updates = _resolve_symbol_field_overlaps(editor, component_updates)
    editor.update_items(field_updates)
    _pack_completed_groups(
        editor,
        item_groups,
        primary_group,
        connector_groups=connector_groups,
        right_of=right_of,
        stage_order=_power_stage_order(visible, layout_groups),
    )
    page_settings = _page_settings_for_content(editor)
    editor.set_page_settings(page_settings)
    return NativeLayoutReport(
        component_count=len(component_updates),
        hidden_component_count=len(hidden_ids),
        power_symbol_count=len(power_updates),
        label_count=len(created_labels),
        wire_count=sum(isinstance(item, SchematicLine) for item in created_routing),
        junction_count=sum(isinstance(item, Junction) for item in created_routing),
        no_connect_count=sum(isinstance(item, NoConnectMarker) for item in created_routing),
        page_size=page_settings.page_size,
    )


def _clear_final_label_wires(
    editor: FileSchematic, groups: dict[str, str] | None = None,
) -> None:
    """Fit dangling labels against actual routes, extending their own straight stub.

    Predicted routes cannot establish final annotation clearance. This last
    text pass may slide a terminal label along its incident wire, but cannot
    change a junction, a component, or the electrical attachment.
    """

    groups = groups or {}
    bodies = [(groups.get(symbol.uuid), _envelope_from_points(points))
              for symbol in editor.document.symbols
              if (points := placed_symbol_body_positions(editor.document, symbol))]
    for label in editor.get_labels():
        group = groups.get(label.id)
        local_bodies = [box for g, box in bodies if g == group]
        wires = [w for w in editor.get_lines() if groups.get(w.id) == group]
        box = _envelope_from_points(_label_envelope(label.text))
        if not any(_segment_hits_box(w.start, w.end, box) for w in wires):
            continue
        incident = [w for w in wires if label.position in (w.start, w.end)]
        if len(incident) != 1:
            raise KiCadSchematicError(f"label crosses final wiring: {label.text.value}")
        wire = incident[0]
        other = wire.end if wire.start == label.position else wire.start
        dx = (label.position.x > other.x) - (label.position.x < other.x)
        dy = (label.position.y > other.y) - (label.position.y < other.y)
        other_labels = [_envelope_from_points(_label_envelope(item.text))
                        for item in editor.get_labels()
                        if item.id != label.id and groups.get(item.id) == group]
        foreign = [w for w in wires if w.id != wire.id]
        for step in range(1, 33):
            point = Vector2(label.position.x + dx * step * 635_000,
                            label.position.y + dy * step * 635_000)
            candidate = replace(label.text, position=point)
            box = _envelope_from_points(_label_envelope(candidate))
            if (any(_segment_hits_box(w.start, w.end, box) for w in foreign)
                    or any(not _envelopes_do_not_overlap(box, b, 250_000)
                           for b in [*local_bodies, *other_labels])
                    or any(_segment_hits_box(label.position, point, b)
                           for b in [*local_bodies, *other_labels])
                    or any(_wires_intersect(SchematicLine(
                        id="", start=label.position, end=point), w) for w in foreign)):
                continue
            if wire.start == label.position:
                wire.start = point
            else:
                wire.end = point
            label.position = point
            label.text = candidate
            editor.update_items([wire, label])
            break
        else:
            raise KiCadSchematicError(f"no clear final label position: {label.text.value}")


def _align_single_pin_attachments(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Default an exclusive two-terminal attachment to its owner's pin axis."""

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    layout_groups, _ = _component_layout_groups(schematic)
    groups = {ref: layout_groups.get(schematic["root_ref"] + "." + s.zener_path)
              for ref, s in symbols.items()}
    raw = {s.uuid: s for s in editor.document.symbols}
    pins = {ref: placed_pin_positions(editor.document, raw[s.id]) for ref, s in symbols.items()}
    sides = {ref: placed_pin_sides(editor.document, raw[s.id]) for ref, s in symbols.items()}
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    inline = {ref for ref in symbols if len(pins[ref]) == 2
              and set(sides[ref].values()) in ({"left", "right"}, {"top", "bottom"})}
    by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for endpoint, net in expected_pin_nets(schematic).items():
        if endpoint[0] in symbols:
            by_net[net].append(endpoint)
    attachments: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    connected_devices: dict[str, set[str]] = defaultdict(set)
    rails = {net["name"] for net in schematic["nets"].values()
             if net.get("kind") in {"Power", "Ground"}}
    for net, members in by_net.items():
        for child, _ in members:
            if child in inline and net not in rails:
                connected_devices[child].update(ref for ref, _ in members if ref not in inline)
        if len(members) != 2:
            continue
        for (owner, number), (child, terminal) in (members, members[::-1]):
            if (owner != child and owner not in inline and child in inline
                    and groups[owner] == groups[child]):
                attachments[child].append((owner, number, terminal))

    # An authored attachment is unambiguous even when the far terminal also
    # connects to a device. Resolve its actual pin, not the nearest seed row.
    authored = set()
    net_by_pin = expected_pin_nets(schematic)
    for child in sorted(inline):
        props = _component_properties(schematic, schematic["root_ref"] + "."
                                      + symbols[child].zener_path)
        owner = props.get("owner")
        if owner not in symbols or groups[owner] != groups[child] or not props.get("pin"):
            continue
        instance = schematic["instances"][schematic["root_ref"] + "."
                                           + symbols[owner].zener_path]
        numbers = symbol_pin_number_groups(instance).get(props["pin"], ())
        number = next((n for n in numbers if n in pins[owner]), None)
        if number is None:
            continue
        net = net_by_pin.get((owner, number))
        if len(by_net.get(net, [])) != 2 and not (
                props.get("role") == "bypass" and net in rails):
            continue  # A shared node is a branch, not an exclusive inline attachment.
        terminals = [n for n in pins[child] if net is not None
                     and net_by_pin.get((child, n)) == net]
        if len(terminals) == 1:
            attachments[child] = [(owner, number, terminals[0])]
            authored.add(child)

    bounds = {}
    for ref, symbol in symbols.items():
        points = placed_symbol_body_positions(editor.document, raw[symbol.id])
        if points:
            bounds[ref] = _Envelope(min(p.x for p in points), min(p.y for p in points),
                                    max(p.x for p in points), max(p.y for p in points))
    clearance = round(2 * _PIN_STUB_MM * 1_000_000)
    for child, owners in attachments.items():
        # A bridge between two devices has no unique local attachment owner.
        if len(owners) != 1 or (child not in authored and len(connected_devices[child]) > 1):
            continue
        owner, number, terminal = owners[0]
        symbol = symbols[child]
        ref = schematic["root_ref"] + "." + symbol.zener_path
        props = _component_properties(schematic, ref)
        if (props.get("role") == "divider"
                or props.get("role") == "series" and not props.get("owner")
                or props.get("owner", owner) != owner):
            continue
        body = raw[symbol.id]
        side = sides[owner][number]
        rotation = next(
            angle for angle in (0, 90, 180, 270)
            if placed_pin_sides(editor.document, replace(body, rotation=angle))[terminal]
            == opposite[side]
        )
        rotated = replace(body, rotation=rotation)
        old_pin = placed_pin_positions(editor.document, rotated)[terminal]
        points = placed_symbol_body_positions(editor.document, rotated)
        if not points:
            continue
        box = _Envelope(min(p.x for p in points), min(p.y for p in points),
                        max(p.x for p in points), max(p.y for p in points))
        dx, dy = {"left": (-1, 0), "right": (1, 0),
                  "top": (0, -1), "bottom": (0, 1)}[side]
        anchor = pins[owner][number]
        for step in range(32):
            span = clearance + step * clearance // 2
            delta = Vector2(anchor.x + dx * span - old_pin.x,
                            anchor.y + dy * span - old_pin.y)
            candidate = box.translated(delta)
            if any(not _envelopes_do_not_overlap(candidate, other, clearance // 10)
                   for ref, other in bounds.items()
                   if ref != child and groups[ref] == groups[child]):
                continue
            _translate_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                              symbol.position.y + delta.y), rotation)
            editor.update_items(symbol)
            bounds[child] = candidate
            break


def _compact_inline_connections(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Shorten existing straight attachments without choosing new owners or roles."""

    symbols = list(editor.get_symbols())
    raw = {symbol.uuid: symbol for symbol in editor.document.symbols}
    pin_nets = expected_pin_nets(schematic)
    layout_groups, _ = _component_layout_groups(schematic)
    pins = {s.id: placed_pin_positions(editor.document, raw[s.id]) for s in symbols
            if s.zener_path is not None}
    sides = {s.id: placed_pin_sides(editor.document, raw[s.id]) for s in symbols
             if s.id in pins}
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    for symbol in symbols:
        if symbol.id not in pins or len(pins[symbol.id]) != 2:
            continue
        source = schematic["root_ref"] + "." + symbol.zener_path
        props = _component_properties(schematic, source)
        if props.get("role") in {"series", "divider"}:
            continue
        candidates = []
        for owner in symbols:
            if (owner.id not in pins or owner.id == symbol.id
                    or (len(pins[owner.id]) <= 2 and owner.reference != props.get("owner"))):
                continue
            owner_group = layout_groups.get(schematic["root_ref"] + "." + owner.zener_path)
            part_group = layout_groups.get(source)
            if owner_group != part_group:
                continue
            for number, a in pins[owner.id].items():
                net = pin_nets.get((owner.reference, number))
                if net is None:
                    continue
                side = sides[owner.id][number]
                for terminal, b in pins[symbol.id].items():
                    if (pin_nets.get((symbol.reference, terminal)) != net
                            or sides[symbol.id][terminal] != opposite[side]):
                        continue
                    horizontal = side in {"left", "right"}
                    if (a.y != b.y if horizontal else a.x != b.x):
                        continue
                    direction = -1 if side in {"left", "top"} else 1
                    span = direction * (b.x - a.x if horizontal else b.y - a.y)
                    if span > 0:
                        candidates.append((span, owner, a, b, side))
        if not candidates:
            continue
        span, owner, a, b, side = min(candidates, key=lambda item: item[0])
        gap = round(2 * _PIN_STUB_MM * 1_000_000)
        if span <= gap:
            continue
        direction = -1 if side in {"left", "top"} else 1
        horizontal = side in {"left", "right"}
        delta = Vector2(direction * (gap - span) if horizontal else 0,
                        0 if horizontal else direction * (gap - span))
        # Check the destination, not the swept path of an imaginary drag.
        # A symbol can move past an obstacle into clear space on its existing
        # wire axis. Captions and the shortened wires are rebuilt afterwards.
        points = placed_symbol_body_positions(editor.document, raw[symbol.id])
        if not points:
            continue
        clearance = round(0.5 * 1_000_000)
        candidate = _Envelope(
            min(p.x for p in points) + delta.x - clearance,
            min(p.y for p in points) + delta.y - clearance,
            max(p.x for p in points) + delta.x + clearance,
            max(p.y for p in points) + delta.y + clearance,
        )
        blocked = False
        for other in symbols:
            if other.id in {symbol.id, owner.id} or other.zener_path is None:
                continue
            points = placed_symbol_body_positions(editor.document, raw[other.id])
            if points and not _envelopes_do_not_overlap(candidate, _Envelope(
                min(p.x for p in points), min(p.y for p in points),
                max(p.x for p in points), max(p.y for p in points),
            ), 0):
                blocked = True
                break
        if not blocked:
            _translate_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                              symbol.position.y + delta.y),
                              symbol.transform.orientation)
            editor.update_items(symbol)
            raw[symbol.id] = next(
                item for item in editor.document.symbols if item.uuid == symbol.id
            )
            pins[symbol.id] = placed_pin_positions(editor.document, raw[symbol.id])


def _align_authored_inline_banks(
    schematic: dict[str, Any], editor: FileSchematic,
) -> set[str]:
    """Align repeated inline roles on one owner face, preserving pin rows."""

    symbols = list(editor.get_symbols())
    by_ref = {symbol.reference: symbol for symbol in symbols}
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    banks: dict[tuple[str, str, str, str], list[SchematicSymbolInstance]] = defaultdict(list)
    for symbol in symbols:
        if symbol.zener_path is None:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        properties = next((
            props for source in (ref, ref.rsplit(".", 1)[0])
            if (props := schematic_properties(schematic["instances"].get(source, {})))
            and "role" in props
        ), {})
        if properties.get("role") not in {"series-termination", "current-limit",
                                          "pullup", "pulldown"}:
            continue
        owner = by_ref.get(properties.get("owner"))
        if owner is None:
            continue
        sides = set(placed_pin_sides(editor.document, raw_by_id[symbol.id]).values())
        if sides != {"left", "right"}:
            continue
        side = "left" if symbol.position.x < owner.position.x else "right"
        role = ("bias" if properties["role"] in {"pullup", "pulldown"}
                else properties["role"])
        banks[(owner.id, role, properties["group"], side)].append(symbol)
    bank_ids = set()
    for (_, _, _, side), members in banks.items():
        if len(members) < 2:
            continue
        x = (min if side == "left" else max)(member.position.x for member in members)
        for member in members:
            _translate_symbol(member, Vector2(x, member.position.y), member.transform.orientation)
            bank_ids.add(member.id)
        editor.update_items(members)
    return bank_ids


def _place_vertical_pin_bias_branches(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """A pull resistor on a vertical device pin branches beside the device."""

    symbols = list(editor.get_symbols())
    by_ref = {symbol.reference: symbol for symbol in symbols}
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    pin_nets = expected_pin_nets(schematic)
    for symbol in symbols:
        if symbol.zener_path is None:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        props = next((
            props for source in (ref, ref.rsplit(".", 1)[0])
            if (props := schematic_properties(schematic["instances"].get(source, {})))
            and "role" in props
        ), {})
        if props.get("role") not in {"pullup", "pulldown"}:
            continue
        owner = by_ref.get(props.get("owner"))
        if owner is None or owner.zener_path is None:
            continue
        owner_ref = schematic["root_ref"] + "." + owner.zener_path
        number = symbol_pin_number_groups(schematic["instances"][owner_ref])[props["pin"]][0]
        owner_raw = raw_by_id[owner.id]
        side = placed_pin_sides(editor.document, owner_raw)[number]
        if side not in {"top", "bottom"}:
            continue
        pins = placed_pin_positions(editor.document, raw_by_id[symbol.id])
        if len(pins) != 2:
            continue
        net = pin_nets[(owner.reference, number)]
        signal = next(pin for pin in pins if pin_nets[(symbol.reference, pin)] == net)
        wanted_side = "top" if props["role"] == "pulldown" else "bottom"
        raw = raw_by_id[symbol.id]
        rotation = next(angle for angle in (0, 90, 180, 270)
                        if placed_pin_sides(editor.document, replace(raw, rotation=angle))[signal]
                        == wanted_side)
        owner_pins = placed_pin_positions(editor.document, owner_raw)
        anchor = _stub_endpoint(_PlacedEndpoint(owner_pins[number], side)).position
        branch_x = min(pin.x for pin in owner_pins.values()) - round(7.62 * 1_000_000)
        old_pin = placed_pin_positions(editor.document, replace(raw, rotation=rotation))[signal]
        _translate_symbol(symbol, Vector2(
            symbol.position.x + branch_x - old_pin.x,
            symbol.position.y + anchor.y - old_pin.y,
        ), rotation)
        editor.update_items(symbol)


def _place_top_pin_bypasses(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Keep bypass branches above top pins or crowded side-pin exits."""

    symbols = list(editor.get_symbols())
    by_ref = {symbol.reference: symbol for symbol in symbols}
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    pin_nets = expected_pin_nets(schematic)
    for symbol in symbols:
        if symbol.zener_path is None:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        props = next((
            props for source in (ref, ref.rsplit(".", 1)[0])
            if (props := schematic_properties(schematic["instances"].get(source, {})))
            and "role" in props
        ), {})
        if props.get("role") != "bypass":
            continue
        owner = by_ref.get(props.get("owner"))
        if owner is None or owner.zener_path is None:
            continue
        owner_ref = schematic["root_ref"] + "." + owner.zener_path
        numbers = symbol_pin_number_groups(schematic["instances"][owner_ref])[props["pin"]]
        owner_raw = raw_by_id[owner.id]
        owner_sides = placed_pin_sides(editor.document, owner_raw)
        owner_pins = placed_pin_positions(editor.document, owner_raw)
        number = next((n for n in numbers if owner_sides.get(n) == "top"), None)
        if number is None:
            # A bypass/return on a side supply pin must not occupy the rail
            # glyph space of a lower neighbouring pin. Raise that branch;
            # text can move later, but the electrical symbols cannot overlap.
            types = symbol_library_pins(editor.document, owner_raw)
            number = next((n for n in numbers if owner_sides.get(n) in {"left", "right"}
                           and any(owner_sides[m] == owner_sides[n]
                                   and 0 < p.y - owner_pins[n].y <= 5_080_000
                                   and (pin_nets.get((owner.reference, m))
                                        != pin_nets.get((owner.reference, n))
                                        or types[m].electrical_type in {"input", "bidirectional"})
                                   for m, p in owner_pins.items())), None)
        if number is None:
            continue
        raw = raw_by_id[symbol.id]
        pins = placed_pin_positions(editor.document, raw)
        if len(pins) != 2:
            continue
        net = pin_nets[(owner.reference, number)]
        supply = next(pin for pin in pins if pin_nets[(symbol.reference, pin)] == net)
        direction = -1 if owner_sides[number] == "left" else 1
        rotation = next(angle for angle in (0, 90, 180, 270)
                        if placed_pin_sides(editor.document, replace(raw, rotation=angle))[supply]
                        == ("left" if direction == 1 else "right"))
        anchor = owner_pins[number]
        # Two normal pin exits leave a tee for the supply arrow. The return
        # can then turn south once; it does not dictate the capacitor axis.
        clearance = round(2 * _PIN_STUB_MM * 1_000_000)
        old_pin = placed_pin_positions(editor.document, replace(raw, rotation=rotation))[supply]
        _translate_symbol(symbol, Vector2(
            symbol.position.x + anchor.x + direction * clearance - old_pin.x,
            symbol.position.y + anchor.y - clearance - old_pin.y,
        ), rotation)
        editor.update_items(symbol)


def _shared_vertical_trunk(endpoints: list[_PlacedEndpoint]) -> int:
    """Prefer a perpendicular branch's axis within the horizontal pin exits."""

    vertical = [p for p in endpoints if p.side in {"top", "bottom"}]
    if len(vertical) == 1:
        x = vertical[0].position.x
        right = [p.position.x for p in endpoints if p.side == "right"]
        left = [p.position.x for p in endpoints if p.side == "left"]
        lower = max(right, default=min([x, *left]))
        upper = min(left, default=max([x, *right]))
        if lower <= upper:
            return max(lower, min(x, upper))
    return round(median(p.position.x for p in endpoints))


def _align_perpendicular_branches(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Align an attached two-terminal branch with the actual shared wire trunk."""

    by_owner = {
        schematic["root_ref"] + "." + item.zener_path: item
        for item in editor.get_symbols() if item.zener_path is not None
    }
    raw = {item.uuid: item for item in editor.document.symbols}
    bounds = {}
    for item_id, item in raw.items():
        points = placed_symbol_body_positions(editor.document, item)
        if points:
            bounds[item_id] = _Envelope(min(p.x for p in points), min(p.y for p in points),
                                        max(p.x for p in points), max(p.y for p in points))
    for endpoints in _component_net_endpoints(schematic, editor).values():
        vertical = [p for p in endpoints if p.side in {"top", "bottom"}]
        horizontal = [p for p in endpoints if p.side in {"left", "right"}]
        if len(vertical) != 1 or len(horizontal) < 2:
            continue
        if len({p.group for p in endpoints}) != 1:
            continue
        branch = vertical[0]
        item = by_owner.get(branch.owner)
        if item is None or len(placed_pin_positions(editor.document, raw[item.id])) != 2:
            continue
        props = _component_properties(schematic, branch.owner)
        if props.get("role") in {"series", "divider"}:
            continue
        x = _shared_vertical_trunk([_stub_endpoint(p) for p in endpoints])
        delta = x - branch.position.x
        if not delta:
            continue
        candidate = bounds.get(item.id)
        if candidate is None:
            continue
        candidate = candidate.translated(Vector2(delta, 0))
        if any(
            not _envelopes_do_not_overlap(candidate, other, 0)
            for other_id, other in bounds.items() if other_id != item.id
        ):
            continue
        _translate_symbol(item, Vector2(item.position.x + delta, item.position.y),
                          item.transform.orientation)
        editor.update_items(item)
        bounds[item.id] = candidate


def _align_same_face_terminal_exits(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Leave outward space between facing pins of folded two-terminal symbols."""

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    groups, _ = _component_layout_groups(schematic)
    by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    net_by_pin = expected_pin_nets(schematic)
    for terminal, net in net_by_pin.items():
        if terminal[0] in symbols:
            by_net[net].append(terminal)
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    for ref, symbol in symbols.items():
        source = raw[symbol.id]
        pins = placed_pin_positions(editor.document, source)
        sides = placed_pin_sides(editor.document, source)
        if len(pins) != 2 or len(set(sides.values())) != 1:
            continue
        side = next(iter(sides.values()))
        vertical = side in {"top", "bottom"}
        sign = 1 if side in {"right", "bottom"} else -1
        group = groups[schematic["root_ref"] + "." + symbol.zener_path]
        limits = []
        for number, point in pins.items():
            for other_ref, other_number in by_net[net_by_pin[(ref, number)]]:
                other = symbols[other_ref]
                if other_ref == ref or groups[schematic["root_ref"] + "."
                                             + other.zener_path] != group:
                    continue
                other_raw = raw[other.id]
                if placed_pin_sides(editor.document, other_raw)[other_number] != opposite[side]:
                    continue
                target = placed_pin_positions(editor.document, other_raw)[other_number]
                span = sign * (target.y - point.y if vertical else target.x - point.x)
                limits.append(span - round(2 * _PIN_STUB_MM * 1_000_000))
        shift = min([0, *limits])
        if shift == 0:
            continue
        delta = Vector2(0, sign * shift) if vertical else Vector2(sign * shift, 0)
        body = _envelope_from_points(placed_symbol_body_positions(editor.document, source))
        if any(not _envelopes_do_not_overlap(body.translated(delta), _envelope_from_points(
                placed_symbol_body_positions(editor.document, raw[other.id])), 500_000)
               for other in symbols.values() if other.id != symbol.id):
            continue
        _translate_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                          symbol.position.y + delta.y),
                          symbol.transform.orientation)
        editor.update_items(symbol)
        raw = {s.uuid: s for s in editor.document.symbols}


def _place_owned_shunt_banks(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Place authored parallel support at its owner, not at a shared rail bank.

    Direct-pin banks branch outside that pin face. A network's downstream
    shunts follow its authored series member. Other same-net parts are not
    candidates for ownership or attachment.
    """

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    nets = expected_pin_nets(schematic)
    rails = {net["name"] for net in schematic["nets"].values()
             if net.get("kind") in {"Power", "Ground"}}
    props = {ref: _component_properties(schematic, schematic["root_ref"] + "." + s.zener_path)
             for ref, s in symbols.items()}
    banks: dict[tuple[str, str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for ref, attributes in props.items():
        owner = attributes.get("owner")
        if attributes.get("role") not in {"bypass", "shunt"} or owner not in symbols:
            continue
        owner_ref = schematic["root_ref"] + "." + symbols[owner].zener_path
        owner_source = schematic["instances"][owner_ref]
        pins = symbol_pin_number_groups(owner_source).get(attributes.get("pin"), ())
        attached = next((nets.get((owner, n)) for n in pins if (owner, n) in nets), None)
        if attached is None and attributes.get("at"):
            attached = next((net for (part, _), net in nets.items() if part == ref
                             and net.rsplit(".", 1)[-1] == attributes["at"]), None)
        terminals = [(n, net) for (part, n), net in nets.items() if part == ref]
        connected = [n for n, net in terminals if net == attached]
        returned = [net for _, net in terminals if net != attached]
        if len(connected) == len(returned) == 1:
            banks[(owner, attributes["group"], attached, returned[0])].append((ref, connected[0]))
    for (owner, group, attached, _), members in banks.items():
        # A lone direct attachment is already placed by the pin-axis pass.
        if (len(members) == 1 and props[members[0][0]].get("pin")
                and (attached not in rails or props[members[0][0]]["role"] == "bypass")):
            continue
        anchor_symbol = symbols[owner]
        anchor_numbers = [n for (part, n), net in nets.items() if part == owner and net == attached]
        if not anchor_numbers:
            peers = [ref for ref, attributes in props.items()
                     if attributes.get("owner") == owner and attributes.get("group") == group
                     and attributes.get("role") == "series"]
            candidates = [(ref, n) for (ref, n), net in nets.items()
                          if ref in peers and net == attached]
            if len(candidates) != 1:
                raise KiCadSchematicError(
                    f"{owner}/{group}: shunt node lacks a unique series attachment"
                )
            source_ref, number = candidates[0]
            anchor_symbol = symbols[source_ref]
            anchor_numbers = [number]
        anchor_raw = raw[anchor_symbol.id]
        # Authored pin wins over another same-net control pin on this owner.
        declared = props[members[0][0]].get("pin")
        if declared and anchor_symbol.reference == owner:
            instance = schematic["instances"][
                schematic["root_ref"] + "." + anchor_symbol.zener_path
            ]
            anchor_numbers = list(symbol_pin_number_groups(instance)[declared])
        point = placed_pin_positions(editor.document, anchor_raw)[anchor_numbers[0]]
        side = placed_pin_sides(editor.document, anchor_raw)[anchor_numbers[0]]
        direction = -1 if side == "left" else 1
        member_ids = {symbols[ref].id for ref, _ in members}
        obstacles = [_envelope_from_points(placed_symbol_body_positions(editor.document, s))
                     for s in raw.values() if s.uuid not in member_ids and s.path is not None]
        cursor = point.x + direction * 7_620_000
        for ref, terminal in sorted(members):
            symbol = symbols[ref]
            original = raw[symbol.id]
            rotation = next(angle for angle in (0, 90, 180, 270)
                            if placed_pin_sides(editor.document, replace(
                                original, rotation=angle))[terminal] == "top")
            rotated = replace(original, rotation=rotation)
            pin = placed_pin_positions(editor.document, rotated)[terminal]
            body = _envelope_from_points(placed_symbol_body_positions(editor.document, rotated))
            # Reserve actual caption width alongside each vertical member.
            width = max((len(f.text.value) * f.text.attributes.size.x
                         for f in symbol.fields if f.name in {"Reference", "Value"}), default=0)
            for attempt in range(32):
                x = cursor + direction * attempt * 5_080_000
                y = point.y if side in {"left", "right"} else point.y + 5_080_000
                delta = Vector2(x - pin.x, y - pin.y)
                candidate = body.translated(delta)
                if any(not _envelopes_do_not_overlap(candidate, other, 2_540_000)
                       for other in obstacles):
                    continue
                _translate_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                                  symbol.position.y + delta.y), rotation)
                editor.update_items(symbol)
                obstacles.append(candidate)
                cursor = x + direction * max(10_160_000, width + 5_080_000)
                break
        raw = {s.uuid: s for s in editor.document.symbols}


def _place_owned_pin_networks(schematic: dict[str, Any], editor: FileSchematic) -> None:
    """Keep an authored series/shunt network together at its shared pin node.

    Neither member is an exclusive pin attachment. Retain the shunt's local
    branch and place the series member perpendicular to it, with outward
    wire space at all three terminals. Ownership and roles come from source.
    """

    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    nets = expected_pin_nets(schematic)
    groups, _ = _component_layout_groups(schematic)
    source = {ref: schematic["root_ref"] + "." + s.zener_path for ref, s in symbols.items()}
    networks: dict[tuple[str, str, str], dict[str, list[str]]] = {}
    for ref in symbols:
        props = _component_properties(schematic, source[ref])
        role = props.get("role")
        if (role not in {"series", "shunt"} or props.get("owner") not in symbols
                or not props.get("pin")):
            continue
        key = (props["owner"], props["group"], props["pin"])
        networks.setdefault(key, defaultdict(list))[role].append(ref)
    directions = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
    stub = round(_PIN_STUB_MM * 1_000_000)
    for (owner, _, name), members in networks.items():
        if len(members.get("series", ())) != 1 or len(members.get("shunt", ())) != 1:
            continue
        series, shunt = members["series"][0], members["shunt"][0]
        number = symbol_pin_number_groups(schematic["instances"][source[owner]])[name][0]
        net = nets[(owner, number)]
        terminals = {ref: [n for (part, n), value in nets.items() if part == ref and value == net]
                     for ref in (series, shunt)}
        if any(len(numbers) != 1 for numbers in terminals.values()):
            continue
        series_pin, shunt_pin = terminals[series][0], terminals[shunt][0]
        shunt_raw, original = raw[symbols[shunt].id], raw[symbols[series].id]
        if any(len(placed_pin_positions(editor.document, s)) != 2
               for s in (shunt_raw, original)):
            continue
        anchor = placed_pin_positions(editor.document, shunt_raw)[shunt_pin]
        owner_pin = placed_pin_positions(editor.document, raw[symbols[owner].id])[number]
        side = placed_pin_sides(editor.document, shunt_raw)[shunt_pin]
        dx, dy = directions[side]
        node = Vector2(anchor.x + dx * stub, anchor.y + dy * stub)
        # Prefer the open side away from the device, then try the other side.
        axes = sorted(((dy, -dx), (-dy, dx)), key=lambda v: -(
            v[0] * (anchor.x - owner_pin.x) + v[1] * (anchor.y - owner_pin.y)))
        obstacles = [_envelope_from_points(placed_symbol_body_positions(editor.document, s))
                     for ref, symbol in symbols.items()
                     if ref != series and groups.get(source[ref]) == groups[source[series]]
                     for s in (raw[symbol.id],)]
        foreign_strokes = [segment for ref, symbol in symbols.items() if ref != series
                           and groups.get(source[ref]) == groups[source[series]]
                           for n, segment in placed_pin_segments(
                               editor.document, raw[symbol.id]).items()
                           if nets.get((ref, n)) != net]
        candidates = []
        for preference, (sx, sy) in enumerate(axes):
            facing = next(s for s, direction in directions.items() if direction == (-sx, -sy))
            rotation = next(angle for angle in (0, 90, 180, 270)
                            if placed_pin_sides(editor.document, replace(
                                original, rotation=angle))[series_pin] == facing)
            rotated = replace(original, rotation=rotation)
            pin = placed_pin_positions(editor.document, rotated)[series_pin]
            body = _envelope_from_points(placed_symbol_body_positions(editor.document, rotated))
            for step in range(8):
                target = Vector2(node.x + sx * stub * (step + 1),
                                 node.y + sy * stub * (step + 1))
                delta = Vector2(target.x - pin.x, target.y - pin.y)
                candidate = body.translated(delta)
                if (any(not _envelopes_do_not_overlap(candidate, box, 635_000)
                        or _segment_hits_box(node, target, box) for box in obstacles)
                        or any(_segment_hits_box(a, b, candidate) for a, b in foreign_strokes)):
                    continue
                candidates.append((step, preference, delta, rotation))
                break
        if not candidates:
            raise KiCadSchematicError(f"{owner}/{name}: no clear series/shunt placement")
        _, _, delta, rotation = min(candidates, key=lambda item: (item[1], item[0]))
        symbol = symbols[series]
        _translate_symbol(symbol, Vector2(symbol.position.x + delta.x,
                                          symbol.position.y + delta.y), rotation)
        editor.update_items(symbol)
        raw = {s.uuid: s for s in editor.document.symbols}


def _position_bank_fields(
    editor: FileSchematic, components: list[SchematicSymbolInstance], bank_ids: set[str],
) -> None:
    """Use a compact caption row beside each aligned inline passive."""

    raw = {symbol.uuid: symbol for symbol in editor.document.symbols}
    horizontal = {
        component.id: component
        for component in components
        if len(symbol_library_pins(editor.document, raw[component.id])) == 2
        and set(placed_pin_sides(editor.document, raw[component.id]).values())
        == {"left", "right"}
    }
    # A close stack has the same caption grammar regardless of whether its
    # members were placed by an authored bank or by the pin-axis default.
    stacked_ids = {
        component.id for component in horizontal.values()
        if any(
            other.id != component.id
            and abs(other.position.x - component.position.x) < 500_000
            and 0 < abs(other.position.y - component.position.y) <= 3_810_000
            for other in horizontal.values()
        )
    }
    caption_ids = bank_ids | stacked_ids
    # Reserve the actual caption row before the native routes are constructed.
    # Closely aligned members move together along their existing pin axes.
    pending = [component for component in components if component.id in caption_ids]
    while pending:
        first = pending.pop(0)
        bank = [first]
        nearby = sorted(pending[:], key=lambda item: abs(item.position.y - first.position.y))
        for component in nearby:
            if (abs(component.position.x - first.position.x) < 500_000
                    and any(abs(component.position.y - member.position.y) <= 3_810_000
                            for member in bank)):
                bank.append(component)
                pending.remove(component)
        shift = 0
        for component in bank:
            body = placed_symbol_body_positions(editor.document, raw[component.id])
            reference, value = component.reference_field.text, component.value_field.text
            right = max(p.x for p in body) + 1_270_000
            right += _text_width(reference) + reference.attributes.size.x
            right += _text_width(value)
            for other in editor.document.symbols:
                if other.uuid in caption_ids:
                    continue
                sides = placed_pin_sides(editor.document, other)
                for number, pin in placed_pin_positions(editor.document, other).items():
                    if (sides[number] == "left" and pin.x > component.position.x
                            and abs(pin.y - component.position.y) < 100_000):
                        shift = max(shift, right + 635_000 - pin.x)
        if shift > 0:
            for component in bank:
                _translate_symbol(
                    component,
                    Vector2(component.position.x - shift, component.position.y),
                    component.transform.orientation,
                )
            editor.update_items(bank)
            raw = {symbol.uuid: symbol for symbol in editor.document.symbols}
    for component in components:
        if component.id not in caption_ids:
            continue
        body = placed_symbol_body_positions(editor.document, raw[component.id])
        x = max(point.x for point in body) + round(1.27 * 1_000_000)
        y = component.position.y - round(1.27 * 1_000_000)
        for field in (component.reference_field, component.value_field):
            field.text.position = Vector2(x, y)
            field.text.attributes.angle = (-component.transform.orientation) % 360
            field.text.attributes.horizontal_alignment = "left"
            x += _text_width(field.text) + field.text.attributes.size.x


def _translate_symbol(
    item: SchematicSymbolInstance,
    target: Vector2,
    rotation: float,
) -> None:
    delta_x = target.x - item.position.x
    delta_y = target.y - item.position.y
    item.position = target
    item.transform.orientation = rotation
    for field in item.fields:
        field.text.position = Vector2(
            field.text.position.x + delta_x,
            field.text.position.y + delta_y,
        )


def _position_component_fields(
    editor: FileSchematic,
    components: list[SchematicSymbolInstance],
) -> list[SchematicSymbolInstance]:
    """Place visible fields from resolved pin geometry, never inherited offsets."""

    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    typed_by_id = {symbol.id: symbol for symbol in editor.get_symbols()}
    result = []
    for previous in components:
        component = typed_by_id[previous.id]
        raw = raw_by_id[previous.id]
        body = placed_symbol_body_positions(editor.document, raw)
        if body:
            center_x = round((min(point.x for point in body) + max(point.x for point in body)) / 2)
            top_y = min(point.y for point in body)
        else:
            center_x = component.position.x
            top_y = component.position.y
        reference = component.field("Reference")
        value = component.field("Value")
        fields = [field for field in (reference, value) if field is not None]
        line_height = max(
            (field.text.attributes.size.y for field in fields),
            default=round(1.27 * 1_000_000),
        )
        body_width = max((point.x for point in body), default=center_x) - min(
            (point.x for point in body), default=center_x
        )
        body_height = max((point.y for point in body), default=top_y) - min(
            (point.y for point in body), default=top_y
        )
        compact_two_terminal = (
            len(symbol_library_pins(editor.document, raw)) == 2
            and max(body_width, body_height) <= round(6.0 * 1_000_000)
        )
        if compact_two_terminal:
            _position_compact_two_terminal_fields(
                component,
                body,
                placed_pin_sides(editor.document, raw),
                line_height,
            )
            result.append(component)
            continue
        baseline = top_y - round(1.25 * line_height)
        if value is not None:
            value.text.position = Vector2(center_x, baseline)
            value.text.attributes.angle = component.transform.orientation
            value.text.attributes.horizontal_alignment = "center"
        if reference is not None:
            reference.text.position = Vector2(center_x, baseline - 2 * line_height)
            reference.text.attributes.angle = component.transform.orientation
            reference.text.attributes.horizontal_alignment = "center"
        result.append(component)
    return result


def _position_compact_two_terminal_fields(
    component: SchematicSymbolInstance,
    body: list[Vector2],
    pin_sides: dict[str, str],
    line_height: int,
) -> None:
    """Keep captions off the occupied terminal axis of a small two-pin part."""

    reference = component.field("Reference")
    value = component.field("Value")
    min_x = min((point.x for point in body), default=component.position.x)
    max_x = max((point.x for point in body), default=component.position.x)
    min_y = min((point.y for point in body), default=component.position.y)
    max_y = max((point.y for point in body), default=component.position.y)
    vertical = set(pin_sides.values()) == {"top", "bottom"}
    if vertical:
        x = max_x + round(0.8 * line_height)
        center_y = round((min_y + max_y) / 2)
        for field, y in (
            (reference, center_y - line_height),
            (value, center_y + line_height),
        ):
            if field is None:
                continue
            field.text.position = Vector2(x, y)
            # Property angles are stored relative to the symbol transform.
            # Counter-rotate the field so the rendered caption is horizontal.
            field.text.attributes.angle = (-component.transform.orientation) % 360
            field.text.attributes.horizontal_alignment = "left"
        return

    center_x = round((min_x + max_x) / 2)
    if reference is not None:
        reference.text.position = Vector2(center_x, min_y - round(2.8 * line_height))
        reference.text.attributes.angle = component.transform.orientation
        reference.text.attributes.horizontal_alignment = "center"
    if value is not None:
        value.text.position = Vector2(center_x, min_y - round(0.8 * line_height))
        value.text.attributes.angle = component.transform.orientation
        value.text.attributes.horizontal_alignment = "center"


def _primary_group(
    editor: FileSchematic,
    associations: tuple[KiCadComponentAssociation, ...],
    groups: dict[str, str],
) -> str:
    """Return the group containing the physical component with most pins."""

    primary = max(
        associations,
        key=lambda association: (
            sum(
                len(symbol_library_pins(editor.document, symbol)) for symbol in association.symbols
            ),
            association.path,
        ),
    )
    return groups[primary.instance_ref]


def _component_properties(schematic: dict[str, Any], ref: str) -> dict[str, Any]:
    return next((props for source in (ref, ref.rsplit(".", 1)[0])
                 if (props := schematic_properties(schematic["instances"].get(source, {})))
                 and "role" in props), {})


def _power_flow_edges(schematic: dict[str, Any]) -> set[tuple[str, str]]:
    """Read source-to-consumer edges from authoritative electrical pin types."""

    physical = {ref: item for ref, item in schematic["instances"].items()
                if item.get("reference_designator")}
    types = {ref: symbol_pin_electrical_types(item) for ref, item in physical.items()}
    result = set()
    for net in schematic.get("nets", {}).values():
        if net.get("kind") != "Power":
            continue
        pins = [(ref, port[len(ref) + 1:]) for port in net.get("ports", [])
                for ref in physical if port.startswith(ref + ".")]
        outputs = {ref for ref, pin in pins if types[ref].get(pin) == "power_out"}
        inputs = {ref for ref, pin in pins if types[ref].get(pin) == "power_in"}
        for source in outputs:
            for sink in inputs - {source}:
                # Only compose stages within their existing module boundary.
                prefix = schematic["root_ref"] + "."
                if (source.removeprefix(prefix).split(".")[0]
                        == sink.removeprefix(prefix).split(".")[0]):
                    result.add((source, sink))
    return result


def _power_stage_order(
    schematic: dict[str, Any], groups: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    edges = _power_flow_edges(schematic)
    stages = {ref for edge in edges for ref in edge}
    ordered = []
    pending = set(stages)
    while pending:
        ready = sorted(ref for ref in pending
                       if not any(sink == ref and source in pending for source, sink in edges))
        if not ready:
            raise KiCadSchematicError("power stage dependencies form a cycle")
        ordered.extend(ready)
        pending.difference_update(ready)
    result: dict[str, list[str]] = defaultdict(list)
    for ref in ordered:
        result[groups[ref].split(".", 1)[0]].append(groups[ref])
    return {parent: tuple(dict.fromkeys(children)) for parent, children in result.items()}


def _validate_owned_networks(schematic: dict[str, Any]) -> None:
    """Check experimental local-network intent against exact terminal nets."""

    instances = schematic["instances"]
    physical = {item["reference_designator"]: (ref, item) for ref, item in instances.items()
                if item.get("reference_designator")}
    component_roles(instances, tuple(RoleSource(ref, item) for ref, item in physical.values()))
    nets = expected_pin_nets(schematic)
    for designator, (ref, _) in physical.items():
        props = _component_properties(schematic, ref)
        if props.get("role") not in {"series", "shunt", "pin-bridge"} or not props.get("owner"):
            continue
        owner = props["owner"]
        if owner not in physical:
            raise KiCadSchematicError(f"{designator}: unknown authored owner {owner}")
        aliases = symbol_pin_number_groups(physical[owner][1])

        def owner_net(key: str) -> str:
            numbers = aliases.get(props.get(key), ())
            values = {nets[(owner, n)] for n in numbers if (owner, n) in nets}
            if len(values) != 1:
                raise KiCadSchematicError(f"{designator}: invalid owner {key} {props.get(key)!r}")
            return values.pop()

        terminals = {net for (part, _), net in nets.items() if part == designator}
        if len(terminals) != 2:
            raise KiCadSchematicError(f"{designator}: owned network member needs two terminal nets")
        if props.get("pin"):
            attached = owner_net("pin")
        else:
            matches = {net for net in terminals if net == props.get("at")
                       or net.rsplit(".", 1)[-1] == props.get("at")}
            if len(matches) != 1:
                raise KiCadSchematicError(
                    f"{designator}: invalid owned attachment {props.get('at')!r}"
                )
            attached = matches.pop()
        if attached not in terminals:
            raise KiCadSchematicError(f"{designator}: authored attachment is not connected")
        for key in ("return_pin", "other_pin"):
            if props.get(key) and terminals != {attached, owner_net(key)}:
                raise KiCadSchematicError(f"{designator}: {key} does not match the other terminal")


def _component_layout_groups(schematic: dict[str, Any]) -> tuple[dict[str, str], frozenset[str]]:
    """Keep each external interface local, including its authored support."""

    instances = schematic.get("instances", {})
    physical = {ref: item for ref, item in instances.items()
                if isinstance(item, dict) and item.get("reference_designator")}
    paths = {ref: ref.removeprefix(schematic["root_ref"] + ".") for ref in physical}
    groups = {ref: path.split(".", 1)[0] if "." in path else ""
              for ref, path in paths.items()}
    connectors = {
        ref for ref, item in physical.items()
        if item.get("attributes", {}).get("type", {}).get("String")
        in {"connector", "optical_receiver", "optical_transmitter"}
    }
    by_designator = {item["reference_designator"]: ref for ref, item in physical.items()}
    top_members: dict[str, list[str]] = defaultdict(list)
    for ref, group in groups.items():
        top_members[group].append(ref)
    for ref in connectors:
        # A one-connector module already is a local block; preserve its name
        # for authored inter-block relationships.
        if len(top_members[groups[ref]]) > 1:
            groups[ref] = paths[ref]
    stages = {ref for edge in _power_flow_edges(schematic) for ref in edge}
    for ref in stages:
        groups[ref] = paths[ref]
    for ref in physical:
        owner = by_designator.get(_component_properties(schematic, ref).get("owner"))
        if owner in connectors or owner in stages:
            groups[ref] = groups[owner]
    return groups, frozenset(groups[ref] for ref in connectors)


def _pack_completed_groups(
    editor: FileSchematic,
    groups: dict[str, str],
    primary_group: str,
    *,
    connector_groups: frozenset[str] = frozenset(),
    right_of: tuple[tuple[str, str], ...] = (),
    stage_order: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Measure and pack frozen blocks after all local drawing is complete."""

    envelopes = _completed_group_envelopes(editor, groups)

    packed_envelopes = dict(envelopes)
    inner_deltas = {}
    parents = {}
    for parent, children in (stage_order or {}).items():
        children = tuple(child for child in children if child in envelopes)
        if not children:
            continue
        gap = round(_BLOCK_CLEARANCE_MM * 1_000_000)
        inner = _pack_block_rows(envelopes, list(children),
                                 sum(envelopes[c].width + gap for c in children), gap)
        # Keep residual module-owned objects as their own measured block.
        if parent in packed_envelopes:
            continue
        packed_envelopes[parent] = _union_all([envelopes[c].translated(inner[c]) for c in children])
        for child in children:
            del packed_envelopes[child]
            parents[child] = parent
            inner_deltas[child] = inner[child]
    outer = _packed_group_deltas(
        packed_envelopes,
        parents.get(primary_group, primary_group),
        connector_groups=connector_groups,
        right_of=right_of,
    )
    deltas = {}
    for group in envelopes:
        delta = outer[parents.get(group, group)]
        inner = inner_deltas.get(group, Vector2(0, 0))
        deltas[group] = Vector2(delta.x + inner.x, delta.y + inner.y)
    packed = {
        group: envelope.translated(deltas.get(group, Vector2(0, 0)))
        for group, envelope in envelopes.items()
    }
    margin = Vector2.from_xy_mm(_MARGIN_MM, _MARGIN_MM)
    global_delta = Vector2(
        margin.x - min(envelope.min_x for envelope in packed.values()),
        margin.y - min(envelope.min_y for envelope in packed.values()),
    )
    final_deltas = {
        # Text bounds may introduce sub-micrometre offsets. Keep electrical
        # blocks on a common grid: independently rounded symbol-pin sums and
        # wire coordinates otherwise disagree inside KiCad's netlist reader.
        group: Vector2(round((delta.x + global_delta.x) / 1000) * 1000,
                       round((delta.y + global_delta.y) / 1000) * 1000)
        for group, delta in deltas.items()
    }
    updates = []
    for item in editor.get_items():
        group = groups.get(item.id)
        if group is None:
            continue
        delta = final_deltas[group]
        if isinstance(item, SchematicSymbolInstance):
            _translate_symbol(
                item,
                Vector2(item.position.x + delta.x, item.position.y + delta.y),
                item.transform.orientation,
            )
        elif isinstance(item, SchematicLine):
            item.start = Vector2(item.start.x + delta.x, item.start.y + delta.y)
            item.end = Vector2(item.end.x + delta.x, item.end.y + delta.y)
        elif isinstance(item, BaseLabel):
            item.position = Vector2(item.position.x + delta.x, item.position.y + delta.y)
            item.text.position = item.position
        elif isinstance(item, (Junction, NoConnectMarker)):
            item.position = Vector2(item.position.x + delta.x, item.position.y + delta.y)
        updates.append(item)
    editor.update_items(updates)


def _completed_group_envelopes(
    editor: FileSchematic,
    groups: dict[str, str],
) -> dict[str, _Envelope]:
    """Measure bodies, annotations and local wiring owned by each block."""

    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    envelopes: dict[str, _Envelope] = {}
    for item in editor.get_items():
        group = groups.get(item.id)
        if group is None:
            continue
        points: list[Vector2] = []
        if isinstance(item, SchematicSymbolInstance):
            raw = raw_by_id[item.id]
            points.extend(placed_symbol_body_positions(editor.document, raw))
            points.extend(placed_pin_positions(editor.document, raw).values())
            for field in item.fields:
                if field.visible and field.name in {"Reference", "Value"}:
                    points.extend(
                        _text_envelope(field.text, item.transform.orientation)
                    )
            if not points:
                points.append(item.position)
        elif isinstance(item, SchematicLine):
            points.extend((item.start, item.end))
        elif isinstance(item, BaseLabel):
            points.extend(_label_envelope(item.text))
        elif isinstance(item, (Junction, NoConnectMarker)):
            points.append(item.position)
        if not points:
            continue
        envelope = _Envelope(
            min(point.x for point in points),
            min(point.y for point in points),
            max(point.x for point in points),
            max(point.y for point in points),
        )
        previous = envelopes.get(group)
        envelopes[group] = envelope if previous is None else _union(previous, envelope)
    return envelopes


def _pin_stroke_envelopes(
    editor: FileSchematic, symbol: KiCadSymbol,
) -> dict[str, _Envelope]:
    """Reserve the painted pin stroke and clearance on every side."""

    margin = 250_000
    return {
        number: _Envelope(
            min(a.x, b.x) - margin, min(a.y, b.y) - margin,
            max(a.x, b.x) + margin, max(a.y, b.y) + margin,
        )
        for number, (a, b) in placed_pin_segments(editor.document, symbol).items()
    }


def _pin_number_envelopes(
    editor: FileSchematic, symbol: KiCadSymbol,
) -> list[_Envelope]:
    """Reserve rendered number space using each embedded pin's font and length."""

    definition = _library_definition(editor.document, symbol)
    visibility = definition.first_list("pin_numbers")
    if visibility is not None:
        hide = visibility.first_list("hide")
        if (any(getattr(child, "value", None) == "hide" for child in visibility.children)
                or hide is not None and _atom(hide, 1, "pin number visibility").value == "yes"):
            return []
    positions = placed_pin_positions(editor.document, symbol)
    sides = placed_pin_sides(editor.document, symbol)
    result = []
    for pin in _pins_for_unit(definition, symbol.unit):
        number = pin.first_list("number")
        if number is None:
            continue
        text = _atom(number, 1, "pin number").value
        if text not in positions:
            continue
        size = _descendant(number, ("effects", "font", "size"))
        font = 1_270_000 if size is None else round(
            float(_atom(size, 1, "pin number font").value) * 1_000_000
        )
        if font <= 0:
            continue
        anchor = positions[text]
        side = sides[text]
        length = pin.first_list("length")
        span = 0 if length is None else round(
            float(_atom(length, 1, "pin length").value) * 1_000_000
        )
        width = round(len(text) * 0.9 * font + 254_000)
        # KiCad numbers sit beside the stroke, not centred on it. The stroke
        # font's painted height and baseline offset exceed the nominal size.
        depth = round(1.25 * font + 500_000)
        if side in {"top", "bottom"}:
            center = anchor.y + (span // 2 if side == "top" else -span // 2)
            result.append(_Envelope(
                anchor.x - depth, center - width // 2,
                anchor.x - 200_000, center + width // 2,
            ))
        else:
            center = anchor.x + (span // 2 if side == "left" else -span // 2)
            result.append(_Envelope(
                center - width // 2, anchor.y - depth,
                center + width // 2, anchor.y - 200_000,
            ))
    return result


def _rail_caption_offsets(
    original: _Envelope, body: _Envelope, gap: int,
    obstacles: tuple[tuple[_Envelope, int], ...] = (),
) -> list[Vector2]:
    """Offer positions tied to the glyph, never a free-floating text search."""

    candidates = []
    if _box_distance(original, body) <= gap:
        candidates.append(Vector2(0, 0))
    for y in (body.center_y, body.max_y, body.min_y):
        candidates.extend([
            Vector2(body.max_x + gap - original.min_x, y - original.center_y),
            Vector2(body.min_x - gap - original.max_x, y - original.center_y),
        ])
    for y in (body.min_y - gap // 2 - original.max_y,
              body.max_y + gap // 2 - original.min_y):
        for dx in (0, gap // 2, -gap // 2, gap, -gap):
            candidates.append(Vector2(body.center_x + dx - original.center_x, y))
    # Try the actual edges of nearby obstacles, not just three fixed glyph
    # heights. Keep the caption adjacent to its own glyph.
    adjusted = []
    for candidate in candidates:
        box = original.translated(candidate)
        for obstacle, clearance in obstacles:
            if _envelopes_do_not_overlap(box, obstacle, clearance):
                continue
            for shift in (
                Vector2(obstacle.min_x - clearance - box.max_x, 0),
                Vector2(obstacle.max_x + clearance - box.min_x, 0),
                Vector2(0, obstacle.min_y - clearance - box.max_y),
                Vector2(0, obstacle.max_y + clearance - box.min_y),
            ):
                moved = box.translated(shift)
                if (_box_distance(moved, body) <= 3 * gap
                        and body.min_y - gap <= moved.center_y <= body.max_y + gap):
                    adjusted.append(Vector2(candidate.x + shift.x, candidate.y + shift.y))
    unique = {(p.x, p.y): p for p in adjusted}
    candidates.extend(sorted(unique.values(), key=lambda p: (
        _box_distance(original.translated(p), body), abs(p.x) + abs(p.y), p.x, p.y,
    )))
    return candidates


def _resolve_symbol_field_overlaps(
    editor: FileSchematic,
    components: list[SchematicSymbolInstance],
    *,
    grid_mm: float = 1.27,
    clearance_mm: float = 0.25,
) -> list[SchematicSymbolInstance]:
    """Move all symbol captions last, without disturbing electrical geometry."""

    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    component_ids = {component.id for component in components}
    body_envelopes: dict[str, _Envelope] = {}
    fixed: list[_Envelope] = []
    symbols = list(editor.get_symbols())
    for symbol in symbols:
        raw = raw_by_id[symbol.id]
        points = placed_symbol_body_positions(editor.document, raw)
        if points:
            envelope = _Envelope(
                min(point.x for point in points),
                min(point.y for point in points),
                max(point.x for point in points),
                max(point.y for point in points),
            )
            fixed.append(envelope)
            body_envelopes[symbol.id] = envelope
            # Reserve each pin stroke separately from its number, whose font
            # can extend well beyond a fixed one-millimetre stroke margin.
            fixed.extend(_pin_stroke_envelopes(editor, raw).values())
            fixed.extend(_pin_number_envelopes(editor, raw))
    for marker in editor.get_no_connects():
        half_size = 635_000
        fixed.append(_Envelope(
            marker.position.x - half_size, marker.position.y - half_size,
            marker.position.x + half_size, marker.position.y + half_size,
        ))
    for junction in editor.document.junctions:
        diameter = junction.expression.first_list("diameter")
        size_mm = 0 if diameter is None else float(_atom(diameter, 1, "junction diameter").value)
        # A zero diameter selects KiCad's default 0.9144 mm painted dot.
        radius = round((size_mm if size_mm > 0 else 0.9144) * 500_000)
        position = Vector2.from_xy_mm(*junction.position)
        fixed.append(_Envelope(
            position.x - radius, position.y - radius,
            position.x + radius, position.y + radius,
        ))
    for label in editor.get_labels():
        fixed.append(_envelope_from_points(_label_envelope(label.text)))
    wire_half_width = round(0.15 * 1_000_000)
    wire_envelopes = []
    for line in editor.get_lines():
        wire_envelopes.append(_Envelope(
            min(line.start.x, line.end.x) - wire_half_width,
            min(line.start.y, line.end.y) - wire_half_width,
            max(line.start.x, line.end.x) + wire_half_width,
            max(line.start.y, line.end.y) + wire_half_width,
        ))
    fixed.extend(wire_envelopes)

    grid = round(grid_mm * 1_000_000)
    clearance = round(clearance_mm * 1_000_000)
    offsets = sorted(
        (
            Vector2(dx * grid, dy * grid)
            for radius in range(9)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if max(abs(dx), abs(dy)) == radius
        ),
        key=lambda offset: (abs(offset.x) + abs(offset.y), abs(offset.y), offset.x, offset.y),
    )
    placed_fields: list[_Envelope] = []
    # Give the most constrained rail glyph first choice. Otherwise an open
    # neighbour can occupy its only nearby caption slot and strand its label.
    rail_choices = {}
    for symbol in symbols:
        if symbol.id in component_ids or symbol.id not in body_envelopes:
            continue
        boxes = [_envelope_from_points(_text_envelope(field.text, symbol.transform.orientation))
                 for field in symbol.fields
                 if field.visible and field.name in {"Reference", "Value"}]
        if not boxes:
            continue
        original = _union_all(boxes)
        wire_gap = max(clearance, round(0.75 * original.height))
        rail_choices[symbol.id] = sum(
            all(_envelopes_do_not_overlap(box.translated(offset), obstacle, clearance)
                for box in boxes for obstacle in fixed)
            and all(_envelopes_do_not_overlap(box.translated(offset), wire, wire_gap)
                    for box in boxes for wire in wire_envelopes)
            for offset in _rail_caption_offsets(
                original, body_envelopes[symbol.id], grid,
                tuple((box, clearance) for box in fixed)
                + tuple((box, wire_gap) for box in wire_envelopes),
            )
        )
    ordered = sorted(
        symbols,
        key=lambda component: (
            component.id in component_ids,
            rail_choices.get(component.id, 0),
            -body_envelopes.get(
                component.id,
                _Envelope(
                    component.position.x,
                    component.position.y,
                    component.position.x,
                    component.position.y,
                ),
            ).width,
            component.reference,
        ),
    )
    for component in ordered:
        fields = [
            field
            for field in component.fields
            if field.visible and field.name in {"Reference", "Value"}
        ]
        if not fields:
            continue
        field_boxes = [
            _envelope_from_points(
                _text_envelope(field.text, component.transform.orientation)
            )
            for field in fields
        ]
        original = _union_all(field_boxes)
        obstacles = [*fixed, *placed_fields]
        body = body_envelopes.get(component.id, original)
        caption_clearance = max(clearance, round(0.75 * max(
            field.text.attributes.size.y for field in fields
        )))
        candidates = list(offsets)
        gap = round(1.27 * 1_000_000)
        if component.id in component_ids:
            # Four ordinary title positions, each tied to a body edge. Keep
            # a long device title beside its own body instead of searching
            # outward from an already displaced caption.
            candidates.extend([
                Vector2(body.max_x + gap - original.min_x, body.min_y + gap - original.min_y),
                Vector2(body.min_x - gap - original.max_x, body.min_y + gap - original.min_y),
                Vector2(body.max_x + gap - original.min_x, body.center_y - original.center_y),
                Vector2(body.min_x - gap - original.max_x, body.center_y - original.center_y),
                Vector2(body.center_x - original.center_x, body.max_y + gap - original.min_y),
            ])
            # Slide a title along the actual body edge before detaching it at
            # a corner. Side candidates align with the body, not its pin box.
            for x in (body.min_x, body.max_x - original.width, body.center_x - original.width // 2):
                candidates.append(Vector2(x - original.min_x, body.min_y - gap - original.max_y))
            # Nearby wires may fill the ordinary caption band for any part,
            # including a two-pin passive. Try their actual edges as well as
            # grid offsets before retaining an unresolved overlap.
            reach = 20 * grid
            for obstacle in obstacles:
                if _box_distance(obstacle, body) > reach:
                    continue
                candidates.extend([
                    Vector2(0, obstacle.min_y - caption_clearance - original.max_y),
                    Vector2(0, obstacle.max_y + caption_clearance - original.min_y),
                    Vector2(obstacle.min_x - caption_clearance - original.max_x, 0),
                    Vector2(obstacle.max_x + caption_clearance - original.min_x, 0),
                ])
            candidates = [p for p in candidates if abs(p.x) + abs(p.y) <= 2 * reach]
            if len(fields) == 2 and fields[0].text.position.y == fields[1].text.position.y:
                # A bank caption belongs to this exact row; a free NC row
                # above or below is not a valid substitute for ownership.
                candidates = [candidate for candidate in candidates if candidate.y == 0]
                # Text need not follow the electrical grid. A full grid step
                # can hit the body when a small sideways slide clears a mark.
                for box in field_boxes:
                    for obstacle, separation in (
                        [(box, clearance) for box in fixed]
                        + [(box, caption_clearance) for box in placed_fields]
                    ):
                        if _envelopes_do_not_overlap(box, obstacle, separation):
                            continue
                        candidates.extend([
                            Vector2(obstacle.min_x - separation - box.max_x, 0),
                            Vector2(obstacle.max_x + separation - box.min_x, 0),
                        ])
            candidates.sort(key=lambda offset: (
                offset != Vector2(0, 0),
                _box_distance(original.translated(offset), body),
                abs(offset.x) + abs(offset.y),
            ))
        else:
            candidates = _rail_caption_offsets(
                original, body, gap,
                tuple((box, clearance) for box in fixed)
                + tuple((box, caption_clearance) for box in [*placed_fields, *wire_envelopes]),
            )
        offset = next(
            (candidate for candidate in candidates if all(
                _envelopes_do_not_overlap(box.translated(candidate), obstacle, clearance)
                for box in field_boxes for obstacle in fixed
            ) and all(
                _envelopes_do_not_overlap(
                    box.translated(candidate), obstacle, caption_clearance
                )
                for box in field_boxes for obstacle in placed_fields
            ) and (component.id in component_ids or all(
                _envelopes_do_not_overlap(
                    box.translated(candidate), wire, caption_clearance
                )
                for box in field_boxes for wire in wire_envelopes
            )
            )),
            None,
        )
        if offset is None:
            _LOG.warning("No clear caption position for %s (%s)",
                         component.reference, component.value)
            offset = Vector2(0, 0)
        for field in fields:
            field.text.position = Vector2(
                field.text.position.x + offset.x,
                field.text.position.y + offset.y,
            )
        placed_fields.extend(box.translated(offset) for box in field_boxes)
    return symbols


def _box_distance(first: _Envelope, second: _Envelope) -> int:
    return max(0, first.min_x - second.max_x, second.min_x - first.max_x) + max(
        0, first.min_y - second.max_y, second.min_y - first.max_y,
    )


def _envelope_from_points(points: tuple[Vector2, ...]) -> _Envelope:
    return _Envelope(min(p.x for p in points), min(p.y for p in points),
                     max(p.x for p in points), max(p.y for p in points))


def _union_all(envelopes: list[_Envelope]) -> _Envelope:
    result = envelopes[0]
    for envelope in envelopes[1:]:
        result = _union(result, envelope)
    return result


def _envelopes_do_not_overlap(
    first: _Envelope,
    second: _Envelope,
    clearance: int,
) -> bool:
    return (
        first.max_x + clearance <= second.min_x
        or second.max_x + clearance <= first.min_x
        or first.max_y + clearance <= second.min_y
        or second.max_y + clearance <= first.min_y
    )


def _text_width(text: Text) -> int:
    """Reserve a full nominal character pitch for caption/label placement."""

    return len(text.value) * text.attributes.size.x


def _text_envelope(text: Text, parent_angle: float = 0.0) -> tuple[Vector2, Vector2]:
    """Return a conservative painted envelope for one rendered text item."""

    # KiCad's stroke font varies by glyph and plotting backend.  Use a
    # deliberately conservative advance here: a false overlap costs a small
    # amount of whitespace, while an underestimated caption stays unreadable.
    width = _text_width(text)
    height = text.attributes.size.y
    if text.attributes.horizontal_alignment == "left":
        min_x, max_x = 0, width
    elif text.attributes.horizontal_alignment == "right":
        min_x, max_x = -width, 0
    else:
        min_x, max_x = -round(width / 2), round(width / 2)
    half_height = round(height / 2)
    min_y, max_y = -half_height, half_height
    if text.attributes.vertical_alignment == "bottom":
        min_y, max_y = -height, 0
    elif text.attributes.vertical_alignment == "top":
        min_y, max_y = 0, height
    angle = radians((text.attributes.angle + parent_angle) % 360)
    points = [Vector2(text.position.x + round(x * cos(angle) + y * sin(angle)),
                      text.position.y + round(-x * sin(angle) + y * cos(angle)))
              for x in (min_x, max_x) for y in (min_y, max_y)]
    return (
        Vector2(min(p.x for p in points), min(p.y for p in points)),
        Vector2(max(p.x for p in points), max(p.y for p in points)),
    )


def _label_envelope(text: Text) -> tuple[Vector2, Vector2]:
    """Reserve the native label-to-wire inset and painted stroke as well as glyphs.

    A local label's electrical anchor is not its glyph baseline. The native
    plot shifts text away from the wire; this offset rotates with the label.
    Half a text height conservatively covers that inset at the native font.
    """

    angle = radians(text.attributes.angle)
    inset = text.attributes.size.y / 2
    advance = text.attributes.size.x / 4 * {
        "left": 1, "right": -1, "center": 0,
    }[text.attributes.horizontal_alignment]
    shifted = replace(text, position=Vector2(
        text.position.x + round(advance * cos(angle) - inset * sin(angle)),
        text.position.y - round(advance * sin(angle) + inset * cos(angle)),
    ))
    low, high = _text_envelope(shifted)
    stroke = round(text.attributes.size.y * 0.08)
    return (Vector2(low.x - stroke, low.y - stroke),
            Vector2(high.x + stroke, high.y + stroke))


def _packed_group_deltas(
    envelopes: dict[str, _Envelope],
    primary_group: str,
    *,
    clearance_mm: float = _BLOCK_CLEARANCE_MM,
    connector_groups: frozenset[str] = frozenset(),
    right_of: tuple[tuple[str, str], ...] = (),
) -> dict[str, Vector2]:
    """Pack intact blocks into a standard landscape frame, connectors last."""

    clearance = Vector2.from_xy_mm(clearance_mm, clearance_mm).x
    mentioned = {group for pair in right_of for group in pair}
    connector_names = sorted(
        group for group in connector_groups
        if group in envelopes and group not in mentioned and group != primary_group
    )
    functional = {group: box for group, box in envelopes.items()
                  if group not in connector_names}
    if right_of:
        result = _relative_group_deltas(functional, right_of, clearance)
        bounds = _union_all([box.translated(result[group]) for group, box in functional.items()])
        if connector_names:
            width = max(bounds.width, max(envelopes[g].width for g in connector_names))
            bank = _pack_block_rows(envelopes, connector_names, width, clearance)
            bank_bounds = _union_all([envelopes[g].translated(bank[g]) for g in connector_names])
            for group in connector_names:
                delta = bank[group]
                result[group] = Vector2(
                    delta.x + bounds.center_x - bank_bounds.center_x,
                    delta.y + bounds.max_y + clearance,
                )
        return result

    order = [primary_group, *sorted(set(functional) - {primary_group})]
    all_names = order + connector_names
    minimum_width = max(box.width for box in envelopes.values())
    # Row composition changes only at sums of consecutive block widths.
    widths = {minimum_width}
    for start in range(len(all_names)):
        width = -clearance
        for group in all_names[start:]:
            width += envelopes[group].width + clearance
            widths.add(max(minimum_width, width))
    candidates = []
    for width in sorted(widths):
        result = _pack_block_rows(envelopes, order, width, clearance)
        bounds = _union_all([envelopes[g].translated(result[g]) for g in order])
        if connector_names:
            bank = _pack_block_rows(envelopes, connector_names, width, clearance)
            bank_bounds = _union_all([envelopes[g].translated(bank[g]) for g in connector_names])
            for group in connector_names:
                delta = bank[group]
                result[group] = Vector2(
                    delta.x + bounds.center_x - bank_bounds.center_x,
                    delta.y + bounds.max_y + clearance,
                )
        complete = _union_all([box.translated(result[g]) for g, box in envelopes.items()])
        frame_width = max(complete.width, complete.height * _LANDSCAPE_RATIO)
        candidates.append((
            frame_width ** 2 / _LANDSCAPE_RATIO,
            abs(complete.width / complete.height - _LANDSCAPE_RATIO),
            width,
            result,
        ))
    return min(candidates, key=lambda item: item[:3])[3]


def _pack_block_rows(
    envelopes: dict[str, _Envelope], order: list[str], width: int, gap: int,
) -> dict[str, Vector2]:
    """Place a stable sequence of measured blocks in rows without resizing."""

    x = y = row_height = 0
    result = {}
    for group in order:
        box = envelopes[group]
        if x and x + box.width > width:
            x = 0
            y += row_height + gap
            row_height = 0
        result[group] = Vector2(x - box.min_x, y - box.min_y)
        x += box.width + gap
        row_height = max(row_height, box.height)
    return result


def _relative_group_deltas(
    envelopes: dict[str, _Envelope],
    right_of: tuple[tuple[str, str], ...],
    clearance: int,
) -> dict[str, Vector2]:
    """Pack functional groups from authored relative stages."""

    names = set(envelopes)
    mentioned = {name for pair in right_of for name in pair} & names
    if mentioned != names:
        raise KiCadSchematicError(
            "right-of relations do not cover all native functional groups: "
            + ", ".join(sorted(names - mentioned))
        )
    predecessors = {name: set() for name in names}
    for subject, predecessor in right_of:
        if subject in names and predecessor in names:
            predecessors[subject].add(predecessor)
    levels: dict[str, int] = {}
    while len(levels) < len(names):
        ready = sorted(
            name for name in names - levels.keys() if predecessors[name] <= levels.keys()
        )
        if not ready:
            raise KiCadSchematicError("right-of relations form a cycle")
        for name in ready:
            levels[name] = max(
                (levels[previous] + 1 for previous in predecessors[name]),
                default=0,
            )
    result: dict[str, Vector2] = {}
    cursor_x = 0
    for level in range(max(levels.values()) + 1):
        peers = sorted(name for name in names if levels[name] == level)
        total_height = sum(envelopes[name].height for name in peers)
        total_height += clearance * (len(peers) - 1)
        cursor_y = -round(total_height / 2)
        for name in peers:
            envelope = envelopes[name]
            result[name] = Vector2(cursor_x - envelope.min_x, cursor_y - envelope.min_y)
            cursor_y += envelope.height + clearance
        cursor_x += max(envelopes[name].width for name in peers) + clearance
    return result


def _union(first: _Envelope, second: _Envelope) -> _Envelope:
    return _Envelope(
        min(first.min_x, second.min_x),
        min(first.min_y, second.min_y),
        max(first.max_x, second.max_x),
        max(first.max_y, second.max_y),
    )


def _page_settings_for_content(editor: FileSchematic) -> PageSettings:
    """Choose the smallest standard landscape sheet containing the drawing."""

    return _standard_page_for_bounds(_content_envelope(editor))


def _standard_page_for_bounds(envelope: _Envelope) -> PageSettings:
    """Choose output framing after scale-independent layout is complete."""

    margin = Vector2.from_xy_mm(_MARGIN_MM, _MARGIN_MM).x
    required_width = envelope.max_x + margin
    required_height = envelope.max_y + margin
    for name, width_mm, height_mm in (
        ("A4", 297.0, 210.0),
        ("A3", 420.0, 297.0),
        ("A2", 594.0, 420.0),
        ("A1", 841.0, 594.0),
        ("A0", 1189.0, 841.0),
    ):
        size = Vector2.from_xy_mm(width_mm, height_mm)
        if required_width <= size.x and required_height <= size.y:
            return PageSettings(name, "landscape")
    raise KiCadSchematicError("native layout exceeds an A0 landscape sheet")


def _content_envelope(editor: FileSchematic) -> _Envelope:
    """Use the same completed-drawing bounds for packing and sheet selection."""

    envelopes = _completed_group_envelopes(
        editor, {item.id: "drawing" for item in editor.get_items()},
    )
    if not envelopes:
        raise KiCadSchematicError("native layout contains no visible items")
    return envelopes["drawing"]


def _apply_component_display_values(
    schematic: dict[str, Any],
    associations: tuple[KiCadComponentAssociation, ...],
    components: list[SchematicSymbolInstance],
) -> None:
    by_id = {component.id: component for component in components}
    instances = schematic.get("instances", {})
    for association in associations:
        instance = instances.get(association.instance_ref)
        if not isinstance(instance, dict):
            continue
        attributes = instance.get("attributes")
        if not isinstance(attributes, dict):
            continue
        value = attributes.get("value", attributes.get("Value"))
        if isinstance(value, dict):
            value = value.get("String")
        if not isinstance(value, str) or not value:
            continue
        for raw_symbol in association.symbols:
            component = by_id.get(raw_symbol.uuid)
            if component is not None:
                component.value = value


def _consolidate_multi_unit_fields(
    editor: FileSchematic,
    associations: tuple[KiCadComponentAssociation, ...],
    components: list[SchematicSymbolInstance],
) -> None:
    """Use one package caption instead of repeating it on every drawn unit."""

    by_id = {component.id: component for component in components}
    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    for association in associations:
        if len(association.symbols) < 2:
            continue
        ordered = sorted(association.symbols, key=lambda symbol: symbol.unit)
        visible = by_id.get(ordered[0].uuid)
        if visible is None:
            continue
        body = [
            point
            for raw in ordered
            if raw.uuid in by_id
            for point in placed_symbol_body_positions(editor.document, raw_by_id[raw.uuid])
        ]
        if body:
            center_x = round((min(point.x for point in body) + max(point.x for point in body)) / 2)
            top_y = min(point.y for point in body)
            fields = [
                field
                for field in (visible.field("Reference"), visible.field("Value"))
                if field is not None
            ]
            line_height = max(
                (field.text.attributes.size.y for field in fields),
                default=round(1.27 * 1_000_000),
            )
            baseline = top_y - round(1.25 * line_height)
            value = visible.field("Value")
            reference = visible.field("Reference")
            if value is not None:
                value.text.position = Vector2(center_x, baseline)
            if reference is not None:
                reference.text.position = Vector2(
                    center_x,
                    baseline - 2 * line_height,
                )
        for raw in ordered[1:]:
            component = by_id.get(raw.uuid)
            if component is None:
                continue
            for name in ("Reference", "Value"):
                field = component.field(name)
                if field is not None:
                    field.visible = False


def _flatten_positions(
    schematic: dict[str, Any],
) -> tuple[dict[str, Position], list[_PositionedNetSymbol]]:
    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if (
        not isinstance(root_ref, str)
        or not isinstance(instances, dict)
        or not isinstance(nets, dict)
    ):
        raise KiCadSchematicError("evaluated schematic lacks root, instances, or nets")

    modules = {
        ref: instance
        for ref, instance in instances.items()
        if isinstance(ref, str)
        and isinstance(instance, dict)
        and isinstance(instance.get("symbol_positions"), dict)
        and instance["symbol_positions"]
    }
    offsets: dict[str, tuple[float, float]] = {root_ref: (0.0, 0.0)}
    unresolved = set(modules) - {root_ref}
    while unresolved:
        progress = False
        for module_ref in sorted(unresolved, key=len):
            module_path = module_ref.removeprefix(root_ref + ".")
            for parent_ref, parent in modules.items():
                if parent_ref not in offsets or parent_ref == module_ref:
                    continue
                parent_path = (
                    "" if parent_ref == root_ref else parent_ref.removeprefix(root_ref + ".")
                )
                if parent_path and not module_path.startswith(parent_path + "."):
                    continue
                local_path = (
                    module_path if not parent_path else module_path.removeprefix(parent_path + ".")
                )
                raw = parent["symbol_positions"].get(f"comp:{local_path}")
                if isinstance(raw, dict):
                    position = _position(raw)
                    offsets[module_ref] = (
                        offsets[parent_ref][0] + position.x,
                        offsets[parent_ref][1] + position.y,
                    )
                    unresolved.remove(module_ref)
                    progress = True
                    break
        if not progress:
            break

    physical_refs = {
        ref
        for ref, instance in instances.items()
        if isinstance(ref, str)
        and isinstance(instance, dict)
        and instance.get("reference_designator")
    }
    component_candidates: dict[str, list[tuple[int, Position]]] = defaultdict(list)
    root_symbol_groups = top_level_root_symbol_groups(schematic)
    net_positions: list[_PositionedNetSymbol] = []
    net_names = {
        net.get("name")
        for net in nets.values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }
    for module_ref, module in modules.items():
        if module_ref not in offsets:
            continue
        module_path = "" if module_ref == root_ref else module_ref.removeprefix(root_ref + ".")
        for symbol_id, raw in module["symbol_positions"].items():
            if not isinstance(symbol_id, str) or not isinstance(raw, dict):
                continue
            position = _position(raw)
            absolute = Position(
                position.x + offsets[module_ref][0],
                position.y + offsets[module_ref][1],
                position.rotation,
                position.mirror,
            )
            if symbol_id.startswith("comp:"):
                local = symbol_id.removeprefix("comp:").split("@", 1)[0]
                path = local if not module_path else f"{module_path}.{local}"
                full_ref = f"{root_ref}.{path}"
                if full_ref in physical_refs:
                    depth = 0 if not module_path else len(module_path.split("."))
                    component_candidates[full_ref].append((depth, absolute))
            elif symbol_id.startswith("sym:"):
                value = symbol_id.removeprefix("sym:")
                local_name, separator, index = value.rpartition("#")
                if not separator or not index.isdigit():
                    continue
                scoped = f"{module_path}.{local_name}" if module_path else local_name
                actual_name = local_name if local_name in net_names else scoped
                if actual_name in net_names:
                    group = (
                        root_symbol_groups.get(symbol_id)
                        if module_ref == root_ref
                        else module_path.split(".", 1)[0]
                    )
                    net_positions.append(
                        _PositionedNetSymbol(actual_name, symbol_id, absolute, group)
                    )

    missing = physical_refs - set(component_candidates)
    if missing:
        raise KiCadSchematicError(
            "accepted layout lacks physical components: "
            + ", ".join(sorted(ref.removeprefix(root_ref + ".") for ref in missing))
        )
    components = {
        ref: min(candidates, key=lambda candidate: candidate[0])[1]
        for ref, candidates in component_candidates.items()
    }
    return components, net_positions


def _position(raw: dict[str, Any]) -> Position:
    return Position(
        float(raw["x"]),
        float(raw["y"]),
        float(raw.get("rotation", 0.0)),
        raw.get("mirror") if isinstance(raw.get("mirror"), str) else None,
    )


def _drawing_translation(
    visible_targets: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """Move only visible electrical geometry to the drawing margin."""

    min_x = min((target[0] for target in visible_targets), default=0.0)
    min_y = min((target[1] for target in visible_targets), default=0.0)
    margin = _MARGIN_MM * _VIEWER_UNITS_PER_MM
    return margin - min_x, margin - min_y


def _display_names(schematic: dict[str, Any]) -> dict[str, str]:
    cleaned = clean_schematic_labels(schematic)
    return {
        str(net["id"]): net["name"]
        for net in cleaned["nets"].values()
        if isinstance(net, dict) and "id" in net and isinstance(net.get("name"), str)
    }


def _net_symbol_targets(
    schematic: dict[str, Any],
    positions: list[_PositionedNetSymbol],
    display_by_net: dict[str, str],
    translation: tuple[float, float],
    group_deltas: dict[str, Vector2],
    global_delta: Vector2,
    component_endpoints: dict[str, list[_PlacedEndpoint]] | None = None,
) -> list[_NetSymbolTarget]:
    nets_by_name = {
        net["name"]: net
        for net in schematic["nets"].values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }
    targets = []
    for placed in positions:
        net_name = placed.net_name
        position = placed.position
        net = nets_by_name[net_name]
        point = net_symbol_pin_position(net_with_default_signal_symbol(net), position)
        unshifted = Vector2.from_xy_mm(
            (point.x + translation[0]) / _VIEWER_UNITS_PER_MM,
            (point.y + translation[1]) / _VIEWER_UNITS_PER_MM,
        )
        delta = group_deltas.get(placed.group, global_delta)
        target = Vector2(unshifted.x + delta.x, unshifted.y + delta.y)
        targets.append(
            _NetSymbolTarget(
                net_name=net_name,
                display_name=display_by_net.get(str(net.get("id")), net_name.split(".")[-1]),
                position=target,
                rotation=position.rotation,
                rail=net.get("kind") in {"Power", "Ground"},
                ground=net.get("kind") == "Ground",
                group=placed.group,
            )
        )
    represented = {target.net_name for target in targets}
    for net_name, endpoints in (component_endpoints or {}).items():
        net = nets_by_name[net_name]
        if (net_name in represented or net.get("kind") == "NotConnected"
                or len({endpoint.group for endpoint in endpoints}) < 2):
            continue
        # Independently packed blocks cannot retain an unowned cross-block
        # wire. An interface needs local terminations even if the earlier
        # renderer did not emit any explicit symbol-position comments.
        targets.append(_NetSymbolTarget(
            net_name=net_name,
            display_name=display_by_net.get(str(net.get("id")), net_name.split(".")[-1]),
            position=endpoints[0].position,
            rotation=0,
            rail=net.get("kind") in {"Power", "Ground"},
            ground=net.get("kind") == "Ground",
            group=None,
        ))
    return targets


def _place_net_symbols(
    editor: FileSchematic,
    targets: list[_NetSymbolTarget],
) -> tuple[
    list[SchematicSymbolInstance],
    list[LocalLabel],
    list[str],
    list[_PlacedEndpoint],
    dict[str, list[_PlacedEndpoint]],
    set[str],
    dict[str, str],
]:
    power_pool: dict[str, list[SchematicSymbolInstance]] = defaultdict(list)
    for symbol in editor.get_symbols():
        if symbol.zener_path is None:
            power_pool[symbol.value].append(symbol)
    updates = []
    labels = []
    label_net_names = []
    label_anchors = []
    endpoints: dict[str, list[_PlacedEndpoint]] = defaultdict(list)
    used: set[str] = set()
    item_groups: dict[str, str] = {}
    for target in targets:
        candidates = power_pool.get(target.display_name, []) if target.rail else []
        symbol = next((candidate for candidate in candidates if candidate.id not in used), None)
        if symbol is None and target.rail:
            expected_shape = "GND" if target.ground else "VCC"
            symbol = next(
                (
                    candidate
                    for pool in power_pool.values()
                    for candidate in pool
                    if candidate.id not in used
                    and candidate.library_id.rsplit(":", 1)[-1] == expected_shape
                ),
                None,
            )
        if symbol is not None:
            _translate_symbol(symbol, target.position, target.rotation)
            symbol.value = target.display_name
            updates.append(symbol)
            used.add(symbol.id)
            endpoints[target.net_name].append(
                _PlacedEndpoint(target.position, owner=target.owner, group=target.group,
                                members=target.members)
            )
            if target.group is not None:
                item_groups[symbol.id] = target.group
        else:
            text = Text(
                target.display_name,
                target.position,
                TextAttributes(
                    Vector2.from_xy_mm(1.27, 1.27),
                    target.rotation,
                    target.text_alignment,
                    vertical_alignment="bottom",
                ),
            )
            labels.append(LocalLabel(id="", position=target.position, text=text))
            label_net_names.append(target.net_name)
            label_anchors.append(_PlacedEndpoint(target.position, owner=target.owner,
                                                  group=target.group, members=target.members))
    return (
        updates,
        labels,
        label_net_names,
        label_anchors,
        endpoints,
        used,
        item_groups,
    )


def _inline_label_span(pins: list[_PlacedEndpoint]) -> tuple[int, int] | None:
    if len(pins) != 2 or pins[0].position.y != pins[1].position.y:
        return None
    left, right = sorted(pins, key=lambda p: p.position.x)
    if left.side == "right" and right.side == "left":
        return _stub_endpoint(left).position.x, _stub_endpoint(right).position.x
    return None


def _space_labeled_pin_connections(
    schematic: dict[str, Any], editor: FileSchematic,
    targets: list[_NetSymbolTarget], endpoints: dict[str, list[_PlacedEndpoint]],
) -> bool:
    """Reserve a named local wire's text span between a support part and IC."""

    by_owner = {schematic["root_ref"] + "." + s.zener_path: s
                for s in editor.get_symbols() if s.zener_path}
    raw = {s.uuid: s for s in editor.document.symbols}
    counts = {owner: len(symbol_library_pins(editor.document, raw[s.id]))
              for owner, s in by_owner.items()}
    boxes = {s.id: _envelope_from_points(placed_symbol_body_positions(editor.document, raw[s.id]))
             for s in by_owner.values()}
    shifts: dict[str, int] = {}
    for target in targets:
        if target.rail:
            continue
        grouped: dict[str | None, list[_PlacedEndpoint]] = defaultdict(list)
        for pin in endpoints.get(target.net_name, []):
            grouped[pin.group].append(pin)
        if len(grouped) < 2:
            continue
        for pins in grouped.values():
            if _inline_label_span(pins) is None:
                continue
            part = next((p for p in pins if counts.get(p.owner) == 2), None)
            owner = next((p for p in pins if counts.get(p.owner, 0) > 2), None)
            if part is None or owner is None:
                continue
            if _component_properties(schematic, part.owner).get("role") in {"series", "divider"}:
                continue
            symbol = by_owner[part.owner]
            direction = 1 if part.side == "right" else -1
            # A bank caption may extend along the same wire lane. Include
            # its real span as well as the net name, without shrinking text.
            reach = 2_540_000
            for field in symbol.fields:
                if not field.visible or field.name not in {"Reference", "Value"}:
                    continue
                box = _envelope_from_points(
                    _text_envelope(field.text, symbol.transform.orientation),
                )
                if box.min_y - 885_000 <= part.position.y <= box.max_y + 885_000:
                    reach = max(reach, (box.max_x - part.position.x if direction == 1
                                       else part.position.x - box.min_x) + 635_000)
            width = len(target.display_name) * 1_270_000
            required = reach + width + 2_540_000
            extra = required - abs(owner.position.x - part.position.x)
            if extra > 0:
                delta = -direction * extra
                if abs(delta) > abs(shifts.get(symbol.id, 0)):
                    shifts[symbol.id] = delta
    moved = False
    for symbol in by_owner.values():
        delta = shifts.get(symbol.id, 0)
        if not delta:
            continue
        destination = boxes[symbol.id].translated(Vector2(delta, 0))
        if any(not _envelopes_do_not_overlap(destination, box, 500_000)
               for other, box in boxes.items() if other != symbol.id):
            continue
        _translate_symbol(symbol, Vector2(symbol.position.x + delta, symbol.position.y),
                          symbol.transform.orientation)
        editor.update_items(symbol)
        boxes[symbol.id] = destination
        moved = True
    return moved


def _localize_signal_labels(
    targets: list[_NetSymbolTarget],
    component_endpoints: dict[str, list[_PlacedEndpoint]],
) -> list[_NetSymbolTarget]:
    """Put named signal endpoints on outward stubs instead of shared wire trees."""

    targets_by_net: dict[str, list[_NetSymbolTarget]] = defaultdict(list)
    for target in targets:
        targets_by_net[target.net_name].append(target)
    result: list[_NetSymbolTarget] = []
    for net_name, net_targets in targets_by_net.items():
        endpoints = component_endpoints.get(net_name, [])
        template = net_targets[0]
        if template.rail or not endpoints:
            result.extend(net_targets)
            continue
        endpoint_groups: dict[str, list[_PlacedEndpoint]] = defaultdict(list)
        for index, endpoint in enumerate(endpoints):
            key = endpoint.group or endpoint.owner or f"endpoint-{index}"
            endpoint_groups[key].append(endpoint)
        local_clusters = [cluster for pins in endpoint_groups.values()
                          for cluster in _split_wraparound_connections(
                              pins, [items for name, items in component_endpoints.items()
                                     if name != net_name],
                          )]
        split_wraparound = len(local_clusters) > len(endpoint_groups)
        if (len(endpoints) > 1 and len(endpoint_groups) == 1
                and not split_wraparound
                and (len(net_targets) == 1 or _inline_label_span(endpoints) is not None)):
            # This net is wholly local to one functional group. Draw its real
            # wires instead of manufacturing duplicate same-name labels.
            continue
        if split_wraparound:
            endpoint_groups = {str(index): pins for index, pins in enumerate(local_clusters)}
        elif len(net_targets) > 1:
            # Retain the authored interface terminations between local blocks,
            # even when those blocks live within one top-level module.
            endpoint_groups = defaultdict(list)
            for endpoint in endpoints:
                nearest = min(range(len(net_targets)), key=lambda index: _distance(
                    endpoint.position, net_targets[index].position,
                ))
                key = endpoint.group or endpoint.owner or ""
                endpoint_groups[f"{key}:{nearest}"].append(endpoint)
        positions: set[tuple[str | None, int, int]] = set()
        for group_endpoints in endpoint_groups.values():
            endpoint = min(
                group_endpoints,
                key=lambda item: (
                    item.position.y,
                    item.position.x,
                    item.owner or "",
                ),
            )
            position = _stub_endpoint(endpoint).position
            alignment = "right" if endpoint.side == "left" else "left"
            if len(group_endpoints) > 1:
                # A label on a compound node must sit outside its wireset,
                # not centered on a branch or junction inside it.
                stubs = [_stub_endpoint(p).position for p in group_endpoints]
                step = round(_PIN_STUB_MM * 1_000_000)
                inline = _inline_label_span(group_endpoints)
                if inline is not None:
                    position = Vector2(inline[0], position.y)
                    alignment = "left"
                elif endpoint.side == "right":
                    position = Vector2(max(p.x for p in stubs) + step, position.y)
                    alignment = "left"
                else:
                    position = Vector2(min(p.x for p in stubs) - step, position.y)
                    alignment = "right"
            key = endpoint.group, position.x, position.y
            if key not in positions:
                result.append(
                    replace(
                        template,
                        position=position,
                        rotation=0.0,
                        text_alignment=alignment,
                        owner=endpoint.owner,
                        group=endpoint.group,
                        members=tuple(group_endpoints),
                    )
                )
                positions.add(key)
    return result


def _wires_intersect(a: SchematicLine, b: SchematicLine) -> bool:
    if _wire_contact(a.start, a.end, b):
        return True
    vertical, horizontal = (a, b) if a.start.x == a.end.x else (b, a)
    return (vertical.start.x == vertical.end.x and horizontal.start.y == horizontal.end.y
            and min(horizontal.start.x, horizontal.end.x) < vertical.start.x
            < max(horizontal.start.x, horizontal.end.x)
            and min(vertical.start.y, vertical.end.y) < horizontal.start.y
            < max(vertical.start.y, vertical.end.y))


def _split_wraparound_connections(
    pins: list[_PlacedEndpoint], foreign_nets: list[list[_PlacedEndpoint]],
) -> list[list[_PlacedEndpoint]]:
    """Keep local branches, but don't wire a named net around its own device.

    Opposite faces are separate presentation regions. A foreign pin row is
    also a boundary: joining past it creates a crossing of its outward exit.
    This is geometry, independent of net type or component function.
    """

    faces: dict[str, set[str]] = defaultdict(set)
    for pin in pins:
        if pin.owner and pin.side:
            faces[pin.owner].add(pin.side)
    wrap_owners = {owner for owner, sides in faces.items() if len(sides) > 1}
    if not wrap_owners:
        return [pins]
    foreign_pins = [p for items in foreign_nets for p in items]
    foreign_wires = [wire for items in foreign_nets
                     if len(local := [p for p in items if p.group == pins[0].group]) > 1
                     for wire in _route_group(local) if isinstance(wire, SchematicLine)]

    def compatible(a: _PlacedEndpoint, b: _PlacedEndpoint) -> bool:
        if a.owner == b.owner and a.side != b.side:
            return False
        if _distance(a.position, b.position) > _LOCAL_RAIL_CLUSTER_MM * 1_000_000:
            return False
        for source, other in ((a, b), (b, a)):
            if source.owner not in wrap_owners:
                continue
            horizontal = source.side in {"left", "right"}
            low, high = sorted((source.position.y, other.position.y) if horizontal
                               else (source.position.x, other.position.x))
            if any(p.owner == source.owner and p.side == source.side
                   and low < (p.position.y if horizontal else p.position.x) < high
                   for p in foreign_pins):
                return False
        if any(_wires_intersect(wire, other)
               for wire in _route_group([a, b])
               if isinstance(wire, SchematicLine) for other in foreign_wires):
            return False
        return True

    clusters: list[list[_PlacedEndpoint]] = []
    for pin in sorted(pins, key=lambda p: (p.owner not in wrap_owners,
                                           p.position.y, p.position.x)):
        candidates = [cluster for cluster in clusters
                      if all(compatible(pin, member) for member in cluster)]
        if not candidates:
            clusters.append([pin])
        else:
            min(candidates, key=lambda cluster: min(
                _distance(pin.position, member.position) for member in cluster
            )).append(pin)
    return clusters


def _prune_unused_targets(
    targets: list[_NetSymbolTarget],
    component_endpoints: dict[str, list[_PlacedEndpoint]],
) -> list[_NetSymbolTarget]:
    """Discard presentation symbols that no electrical endpoint selects."""

    targets_by_net: dict[str, list[tuple[int, _NetSymbolTarget]]] = defaultdict(list)
    for index, target in enumerate(targets):
        targets_by_net[target.net_name].append((index, target))
    used: set[int] = set()
    for net_name, candidates in targets_by_net.items():
        for endpoint in component_endpoints.get(net_name, []):
            index, _ = candidates[_nearest_local_target(endpoint, [t for _, t in candidates])]
            used.add(index)
    return [target for index, target in enumerate(targets) if index in used]


def _nearest_local_target(
    endpoint: _PlacedEndpoint, targets: list[_NetSymbolTarget] | list[_PlacedEndpoint],
) -> int:
    """An independently packed block must never borrow another block's terminal."""

    local = [index for index, target in enumerate(targets) if target.group == endpoint.group]
    if not local:
        raise KiCadSchematicError(f"no local net termination for block {endpoint.group!r}")
    assigned = [index for index in local if endpoint in targets[index].members]
    if assigned:
        return assigned[0]
    return min(local, key=lambda index: _distance(endpoint.position, targets[index].position))


def _rail_cluster_position(
    cluster: list[_PlacedEndpoint], *, ground: bool, offset: int,
    foreign_pins: list[_PlacedEndpoint] | None = None,
) -> Vector2:
    positions = [_stub_endpoint(endpoint).position for endpoint in cluster]
    # A marker on a continuing vertical conductor reads as part of the
    # component above/below it. Give that named node its own short branch.
    if ({endpoint.side for endpoint in cluster} == {"top", "bottom"}
            and len({endpoint.position.x for endpoint in cluster}) == 1):
        y = round(median(endpoint.position.y for endpoint in cluster))
        candidates = [Vector2(positions[0].x + direction * 2 * offset, y)
                      for direction in (1, -1)]
        neighbours = [pin for pin in foreign_pins or [] if pin.group == cluster[0].group]
        return max(candidates, key=lambda point: min(
            (_distance(point, pin.position) for pin in neighbours), default=float("inf"),
        ))
    outward = "bottom" if ground else "top"
    aligned = [endpoint for endpoint in cluster if endpoint.side == outward]
    x = round(median(endpoint.position.x for endpoint in aligned)) if aligned else round(
        median(position.x for position in positions)
    )
    # Horizontal pin exits already provide a clear attachment point for a
    # north/south glyph. An extra vertical wire can push that glyph into the
    # next signal row for no electrical purpose.
    horizontal = all(endpoint.side in {"left", "right"} for endpoint in cluster)
    extra = 0 if aligned or horizontal else offset
    y = max(p.y for p in positions) + extra if ground else min(p.y for p in positions) - extra
    return Vector2(x, y)


def _rail_cluster_clears_other_pins(
    cluster: list[_PlacedEndpoint], foreign_pins: list[_PlacedEndpoint],
    *, ground: bool, offset: int,
) -> bool:
    # Do not join pin rows across a different signal on the same device face.
    # Checking only exact terminal contacts misses the signal's outward wire.
    for a in cluster:
        for b in cluster:
            if a.owner is None or a.owner != b.owner or a.side != b.side:
                continue
            horizontal = a.side in {"left", "right"}
            low, high = sorted((a.position.y, b.position.y) if horizontal
                               else (a.position.x, b.position.x))
            if any(p.owner == a.owner and p.side == a.side
                   and low < (p.position.y if horizontal else p.position.x) < high
                   for p in foreign_pins):
                return False
    position = _rail_cluster_position(cluster, ground=ground, offset=offset,
                                      foreign_pins=foreign_pins)
    wires = [item for item in _route_group([*cluster, _PlacedEndpoint(position)])
             if isinstance(item, SchematicLine)]
    return not any(
        min(wire.start.x, wire.end.x) <= pin.position.x <= max(wire.start.x, wire.end.x)
        and min(wire.start.y, wire.end.y) <= pin.position.y <= max(wire.start.y, wire.end.y)
        for pin in foreign_pins if pin.group == cluster[0].group
        for wire in wires
    )


def _localize_rail_symbols(
    targets: list[_NetSymbolTarget],
    component_endpoints: dict[str, list[_PlacedEndpoint]],
    *,
    cluster_mm: float = _LOCAL_RAIL_CLUSTER_MM,
    offset_mm: float = _LOCAL_RAIL_OFFSET_MM,
) -> list[_NetSymbolTarget]:
    """Derive north/south rail symbols from nearby connected pin clusters."""

    targets_by_net: dict[str, list[_NetSymbolTarget]] = defaultdict(list)
    for target in targets:
        targets_by_net[target.net_name].append(target)
    maximum_distance = Vector2.from_xy_mm(cluster_mm, cluster_mm).x
    offset = Vector2.from_xy_mm(offset_mm, offset_mm).x
    result = []
    for net_name, net_targets in targets_by_net.items():
        template = net_targets[0]
        if not template.rail:
            result.extend(net_targets)
            continue
        endpoints = component_endpoints.get(net_name, [])
        foreign_pins = [pin for name, pins in component_endpoints.items()
                        if name != net_name for pin in pins]
        clusters: list[list[_PlacedEndpoint]] = []
        for endpoint in sorted(
            endpoints,
            key=lambda item: (item.position.y, item.position.x, item.owner or ""),
        ):
            stub = _stub_endpoint(endpoint)
            cluster = next(
                (
                    current
                    for current in clusters
                    if all(member.group == endpoint.group for member in current)
                    and _compatible_rail_members([*current, endpoint])
                    and (any(
                         endpoint.bank is not None and (member.bank == endpoint.bank
                                                       or member.owner == endpoint.bank[0])
                         or member.bank is not None and endpoint.owner == member.bank[0]
                         for member in current)
                         or all(_distance(stub.position, _stub_endpoint(member).position)
                                <= maximum_distance for member in current))
                    and _rail_cluster_clears_other_pins(
                        [*current, endpoint], foreign_pins,
                        ground=template.ground, offset=offset,
                    )
                ),
                None,
            )
            if cluster is None:
                clusters.append([endpoint])
            else:
                cluster.append(endpoint)
        for cluster in clusters:
            owners = {endpoint.owner for endpoint in cluster}
            groups = {endpoint.group for endpoint in cluster}
            result.append(
                replace(
                    template,
                    position=_rail_cluster_position(
                        cluster, ground=template.ground, offset=offset, foreign_pins=foreign_pins,
                    ),
                    rotation=0.0,
                    owner=owners.pop() if len(owners) == 1 else None,
                    group=groups.pop() if len(groups) == 1 else None,
                    members=tuple(cluster),
                )
            )
    return result


def _compatible_rail_members(members: list[_PlacedEndpoint]) -> bool:
    """Supply connections and signal ties use separate same-net wiresets."""

    return len({pin.rail_class for pin in members if pin.rail_class is not None}) <= 1


def _separate_same_face_rail_corridors(
    targets: list[_NetSymbolTarget],
    component_endpoints: dict[str, list[_PlacedEndpoint]],
    *,
    pitch_mm: float = _RAIL_CORRIDOR_PITCH_MM,
) -> list[_NetSymbolTarget]:
    """Give distinct rail nets separate outward lanes on one component face.

    Independently localized rails can otherwise land on the same axis. Even
    when their wire segments do not quite touch, a one-pin-pitch gap reads as
    a continuous wire between different nets. Keep the nearest rail in its
    natural lane and move subsequent rails farther outward.
    """

    endpoint_indexes_by_target: dict[int, list[_PlacedEndpoint]] = defaultdict(list)
    indexes_by_group_and_net: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, target in enumerate(targets):
        if target.rail and target.group is not None:
            indexes_by_group_and_net[(target.group, target.net_name)].append(index)
    for (group, net_name), indexes in indexes_by_group_and_net.items():
        for endpoint in component_endpoints.get(net_name, []):
            if endpoint.group != group:
                continue
            nearest = indexes[_nearest_local_target(endpoint, [targets[i] for i in indexes])]
            endpoint_indexes_by_target[nearest].append(endpoint)

    corridors: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    along_by_target: dict[int, int] = {}
    spans: dict[int, tuple[int, int]] = {}
    for index, endpoints in endpoint_indexes_by_target.items():
        target = targets[index]
        # Rail symbols point north or south, so their attached corridor is
        # vertical regardless of which component face supplied the endpoint.
        axis = "vertical"
        coordinate = target.position.x
        along = round(median(endpoint.position.y for endpoint in endpoints))
        corridors[(target.group or "", axis, coordinate)].append(index)
        along_by_target[index] = along
        ys = [target.position.y, *(endpoint.position.y for endpoint in endpoints)]
        spans[index] = min(ys), max(ys)

    pitch = round(pitch_mm * 1_000_000)
    result = list(targets)
    for (_, axis, _), indexes in corridors.items():
        if len({targets[index].net_name for index in indexes}) < 2:
            continue
        lanes: dict[int, list[int]] = defaultdict(list)
        for index in sorted(indexes, key=along_by_target.get):
            target = targets[index]
            low, high = spans[index]
            lane = 0
            while any(
                targets[other].net_name != target.net_name
                and low <= spans[other][1] + pitch and spans[other][0] <= high + pitch
                for other in lanes[lane]
            ):
                lane += 1
            lanes[lane].append(index)
            delta = lane * pitch
            position = Vector2(target.position.x + delta, target.position.y)
            result[index] = replace(target, position=position)
    return result


def _rail_glyph_bounds(editor: FileSchematic) -> dict[bool, _Envelope]:
    """Measure the actual north/south graphics relative to their wire anchor."""

    bounds = {}
    for symbol in editor.document.symbols:
        shape = symbol.library_id.rsplit(":", 1)[-1]
        if symbol.path is not None or shape not in {"GND", "VCC"}:
            continue
        points = placed_symbol_body_positions(
            editor.document, replace(symbol, position=(0, 0), rotation=0),
        )
        if points:
            bounds[shape == "GND"] = _envelope_from_points(points)
    return bounds


def _component_body_obstacles(
    editor: FileSchematic, groups: dict[str, str],
) -> dict[str, list[_Envelope]]:
    """Measure only fixed bodies; captions must yield to electrical geometry."""

    result: dict[str, list[_Envelope]] = defaultdict(list)
    for symbol in editor.document.symbols:
        group = groups.get(symbol.uuid)
        if group is None:
            continue
        points = placed_symbol_body_positions(editor.document, symbol)
        if points:
            result[group].append(_envelope_from_points(points))
    return result


def _component_annotation_obstacles(
    editor: FileSchematic, groups: dict[str, str],
) -> dict[str, list[_Envelope]]:
    """Give movable labels the actual local bodies and component captions."""

    result = _component_body_obstacles(editor, groups)
    for symbol in editor.get_symbols():
        group = groups.get(symbol.id)
        if group is None:
            continue
        for field in symbol.fields:
            if field.visible and field.name in {"Reference", "Value"}:
                result[group].append(_envelope_from_points(
                    _text_envelope(field.text, symbol.transform.orientation),
                ))
    return result


def _localize_net_targets(
    templates: list[_NetSymbolTarget],
    endpoints: dict[str, list[_PlacedEndpoint]],
    glyph_bounds: dict[bool, _Envelope],
    annotation_obstacles: dict[str, list[_Envelope]] | None = None,
    body_obstacles: dict[str, list[_Envelope]] | None = None,
) -> list[_NetSymbolTarget]:
    targets = _localize_signal_labels(templates, endpoints)
    targets = _localize_rail_symbols(targets, endpoints)
    targets = _separate_same_face_rail_corridors(targets, endpoints)
    targets = _fit_signal_labels_on_pin_exits(
        targets, endpoints, glyph_bounds, annotation_obstacles, body_obstacles,
    )
    targets = _clear_signal_stub_corridors(targets, endpoints, glyph_bounds, body_obstacles)
    return _merge_touching_rail_symbols(targets, glyph_bounds)


def _merge_touching_rail_symbols(
    targets: list[_NetSymbolTarget], glyph_bounds: dict[bool, _Envelope],
) -> list[_NetSymbolTarget]:
    """Clearance can bring same-net glyphs together; share that termination."""

    result: list[_NetSymbolTarget] = []
    for target in targets:
        glyph = glyph_bounds.get(target.ground)
        if not target.rail or glyph is None:
            result.append(target)
            continue
        for index, other in enumerate(result):
            if (other.rail and other.net_name == target.net_name and other.group == target.group
                    and _compatible_rail_members([*other.members, *target.members])
                    and not _envelopes_do_not_overlap(glyph.translated(other.position),
                                                     glyph.translated(target.position), 635_000)):
                members = list(other.members)
                members.extend(p for p in target.members if p not in members)
                y = (max if target.ground else min)(other.position.y, target.position.y)
                result[index] = replace(other, position=Vector2(other.position.x, y),
                                        members=tuple(members),
                                        owner=other.owner if other.owner == target.owner else None)
                break
        else:
            result.append(target)
    return result


def _fit_signal_labels_on_pin_exits(
    targets: list[_NetSymbolTarget],
    endpoints: dict[str, list[_PlacedEndpoint]],
    glyph_bounds: dict[bool, _Envelope],
    annotation_obstacles: dict[str, list[_Envelope]] | None = None,
    body_obstacles: dict[str, list[_Envelope]] | None = None,
) -> list[_NetSymbolTarget]:
    """Let text yield before moving an otherwise clear rail corridor.

    A local label must remain electrically attached and recognizably owned:
    slide it along its own pin exit, retaining a visible wire, or rotate it
    there. Do not move it to an unrelated empty row.
    """

    def caption(target: _NetSymbolTarget) -> _Envelope:
        return _envelope_from_points(_label_envelope(Text(
            target.display_name, target.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                           target.rotation, target.text_alignment, "bottom"),
        )))

    result = list(targets)
    for index, target in enumerate(targets):
        if target.rail:
            continue
        pins = list(target.members) or [p for p in endpoints.get(target.net_name, [])
                                       if p.group == target.group]
        inline = _inline_label_span(pins)
        if inline is not None:
            obstacles = (annotation_obstacles or {}).get(target.group, [])
            candidates = [replace(target, position=Vector2(x, pins[0].position.y),
                                  text_alignment=alignment)
                          for x, alignment in zip(inline, ("left", "right"), strict=True)]
            clear = [candidate for candidate in candidates
                     if caption(candidate).min_x >= min(p.position.x for p in pins) + 635_000
                     and caption(candidate).max_x <= max(p.position.x for p in pins) - 635_000
                     and all(_envelopes_do_not_overlap(caption(candidate), box, 250_000)
                             for box in obstacles)]
            if clear:
                result[index] = clear[0]
            continue
        if len(pins) != 1 or pins[0].side is None:
            continue
        pin = pins[0]
        foreign_wires = []
        for name, items in endpoints.items():
            if name == target.net_name:
                continue
            local = [p for p in items if p.group == target.group]
            terminals = [t for t in result if t.net_name == name and t.group == target.group]
            routes = [[*[p for p in local if _nearest_local_target(p, terminals) == i],
                       _PlacedEndpoint(t.position)] for i, t in enumerate(terminals)]
            for route in routes or [local]:
                foreign_wires.extend(w for w in _route_group(route)
                                     if isinstance(w, SchematicLine))
        wire_boxes = [_envelope_from_points((w.start, w.end)) for w in foreign_wires]
        # Only already fitted labels are obstacles. A later label's provisional
        # position must not force this wire to grow; that label gets its turn
        # to move around the completed placement.
        obstacles = [caption(t) for i, t in enumerate(result)
                     if i < index and not t.rail and t.group == target.group]
        captions_start = len(obstacles)
        obstacles.extend((annotation_obstacles or {}).get(target.group, []))
        captions_end = len(obstacles)
        rails_start = len(obstacles)
        for rail in targets:
            if not rail.rail or rail.group != target.group:
                continue
            alternatives = [t for t in targets if t.rail and t.group == rail.group
                            and t.net_name == rail.net_name]
            members = [p for p in endpoints.get(rail.net_name, []) if p.group == rail.group
                       and alternatives[_nearest_local_target(p, alternatives)] is rail]
            ys = [rail.position.y, *(p.position.y for p in members)]
            obstacles.append(_Envelope(rail.position.x - 150_000, min(ys),
                                       rail.position.x + 150_000, max(ys)))
            if rail.ground in glyph_bounds:
                obstacles.append(glyph_bounds[rail.ground].translated(rail.position))
        rails_end = len(obstacles)
        for name, members in endpoints.items():
            if name == target.net_name:
                continue
            for p in members:
                if p.group == target.group:
                    stub = _stub_endpoint(p).position
                    obstacles.append(_Envelope(
                        min(p.position.x, stub.x) - 250_000,
                        min(p.position.y, stub.y) - 250_000,
                        max(p.position.x, stub.x) + 250_000,
                        max(p.position.y, stub.y) + 250_000,
                    ))
        wire_boxes.extend(obstacles[rails_end:])
        dx, dy = {"left": (-1, 0), "right": (1, 0),
                  "top": (0, -1), "bottom": (0, 1)}[pin.side]
        span = dx * (target.position.x - pin.position.x) + dy * (
            target.position.y - pin.position.y
        )
        candidates = [target]
        alignments = [target.text_alignment]
        if dy:
            # A vertical pin exit has no preferred text side. Change text
            # justification before asking an adjacent rail to bend around it.
            alignments.append("right" if target.text_alignment == "left" else "left")
        for rotation in (0, 90):
            for length in [*range(span, 634_999, -635_000), 254_000]:
                for alignment in alignments:
                    candidates.append(replace(
                        target, rotation=rotation, text_alignment=alignment,
                        position=Vector2(pin.position.x + dx * length,
                                         pin.position.y + dy * length),
                    ))
        # A longer straight stub can make room for neighbouring captions.
        # Derive that length from the obstacle edge, not a larger fixed gap.
        for candidate in candidates[:]:
            box = caption(candidate)
            for obstacle in [*obstacles, *wire_boxes]:
                if _envelopes_do_not_overlap(box, obstacle, 635_000):
                    continue
                distance = (
                    obstacle.max_x + 635_000 - box.min_x if dx == 1 else
                    box.max_x - obstacle.min_x + 635_000 if dx == -1 else
                    obstacle.max_y + 635_000 - box.min_y if dy == 1 else
                    box.max_y - obstacle.min_y + 635_000
                )
                candidates.append(replace(candidate, position=Vector2(
                    candidate.position.x + dx * distance,
                    candidate.position.y + dy * distance,
                )))
        fitted = []
        constrained_exit = any(_wires_intersect(
            SchematicLine(id="", start=pin.position, end=target.position), wire,
        ) for wire in foreign_wires)
        for candidate in candidates:
            box = caption(candidate)
            if not _envelopes_do_not_overlap(box, _Envelope(
                pin.position.x - 500_000, pin.position.y - 500_000,
                pin.position.x + 500_000, pin.position.y + 500_000,
            ), 0):
                continue
            hard = obstacles[:rails_start]
            if body_obstacles is not None:
                # Component fields are moved after labels. They may yield to
                # a short clear exit instead of forcing it across a wire.
                hard = [*obstacles[:captions_start], *body_obstacles.get(target.group, [])]
            if not all(_envelopes_do_not_overlap(box, obstacle, 635_000) for obstacle in hard):
                continue
            if not all(_envelopes_do_not_overlap(box, obstacle, 250_000)
                       for obstacle in wire_boxes):
                continue
            if any(_segment_hits_box(pin.position, candidate.position, obstacle)
                   for obstacle in obstacles[:captions_start]):
                continue
            # If text alone cannot clear a rail, retain the candidate needing
            # least further clearance, rather than restoring a wide label
            # that pushes the entire rail beyond its full original width.
            overlap = sum(
                max(0, min(box.max_x, obstacle.max_x + 635_000)
                    - max(box.min_x, obstacle.min_x - 635_000))
                * max(0, min(box.max_y, obstacle.max_y + 635_000)
                    - max(box.min_y, obstacle.min_y - 635_000))
                for obstacle in obstacles[rails_start:rails_end]
            )
            stub = SchematicLine(id="", start=pin.position, end=candidate.position)
            crossings = sum(_wires_intersect(stub, wire) for wire in foreign_wires)
            caption_conflicts = sum(not _envelopes_do_not_overlap(box, obstacle, 635_000)
                                    for obstacle in obstacles[captions_start:captions_end])
            fitted.append((crossings, overlap, caption_conflicts,
                           _distance(pin.position, candidate.position) < 1_270_000,
                           _distance(pin.position, candidate.position) if constrained_exit else 0,
                           candidate.rotation != 0,
                           candidate))
        if fitted:
            result[index] = min(fitted, key=lambda item: item[:6])[6]
    return result


def _clear_rail_attachment_exits(
    schematic: dict[str, Any],
    editor: FileSchematic,
    targets: list[_NetSymbolTarget],
    endpoints: dict[str, list[_PlacedEndpoint]],
) -> bool:
    """Keep a visible pin exit when a shared trunk moves past an attachment.

    Small attached symbols can be compacted before a trunk is moved for its
    other connections. Move those attachments outward on their existing axes
    rather than doubling their wire back through the symbol body.
    """

    by_owner = {schematic["root_ref"] + "." + s.zener_path: s
                for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    bounds = {s.id: _envelope_from_points(placed_symbol_body_positions(
        editor.document, raw[s.id],
    )) for s in by_owner.values()}
    shifts: dict[str, int] = {}
    for name, pins in endpoints.items():
        rails = [t for t in targets if t.rail and t.net_name == name]
        for pin in pins:
            if pin.side not in {"left", "right"} or pin.owner not in by_owner:
                continue
            local = [t for t in rails if t.group == pin.group]
            if not local:
                continue
            target = local[_nearest_local_target(pin, local)]
            if target.owner == pin.owner:
                continue  # A lone termination has no shared trunk to clear.
            symbol = by_owner[pin.owner]
            if len(placed_pin_positions(editor.document, raw[symbol.id])) != 2:
                continue
            delta = target.position.x - _stub_endpoint(pin).position.x
            if (pin.side == "right" and delta < 0) or (pin.side == "left" and delta > 0):
                if abs(delta) > abs(shifts.get(symbol.id, 0)):
                    shifts[symbol.id] = delta
    moved = False
    for symbol in by_owner.values():
        delta = shifts.get(symbol.id, 0)
        if not delta:
            continue
        candidate = bounds[symbol.id].translated(Vector2(delta, 0))
        if any(not _envelopes_do_not_overlap(candidate, box, 500_000)
               for other, box in bounds.items() if other != symbol.id):
            continue
        _translate_symbol(symbol, Vector2(symbol.position.x + delta, symbol.position.y),
                          symbol.transform.orientation)
        editor.update_items(symbol)
        bounds[symbol.id] = candidate
        moved = True
    return moved


def _clear_signal_stub_corridors(
    targets: list[_NetSymbolTarget],
    endpoints: dict[str, list[_PlacedEndpoint]],
    glyph_bounds: dict[bool, _Envelope] | None = None,
    body_obstacles: dict[str, list[_Envelope]] | None = None,
) -> list[_NetSymbolTarget]:
    """Place rail trunks beyond the full width of unrelated signal labels."""

    step = round(_PIN_STUB_MM * 1_000_000)
    result = list(targets)
    # Process the row a glyph points towards first. Later terminations then
    # clear its final wire length rather than its superseded short stub.
    order = sorted(range(len(targets)), key=lambda i: (
        targets[i].ground,
        -targets[i].position.y if targets[i].ground else targets[i].position.y,
    ))
    for index in order:
        target = result[index]
        if not target.rail:
            continue
        local_targets = [other for other in result if other.rail
                         and other.net_name == target.net_name and other.group == target.group]
        members = [p for p in endpoints.get(target.net_name, []) if p.group == target.group
                   and local_targets[_nearest_local_target(p, local_targets)] is target]
        ys = [target.position.y, *(p.position.y for p in members)]
        x = target.position.x
        glyph = (glyph_bounds or {}).get(target.ground, _Envelope(0, 0, 0, 0))
        boxes = [
            (other, _envelope_from_points(_label_envelope(Text(
                other.display_name, other.position,
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                               other.rotation, other.text_alignment, "bottom"),
            )))) for other in result if not other.rail
            and other.net_name != target.net_name and other.group == target.group
        ]
        # The complete glyph must clear conductors too, including a label's
        # extended stub. Text clearance alone misses tips touching the next row.
        side = members[0].side if members else "right"
        boxes.extend((replace(target, text_alignment=(
            "right" if side == "left" else "left"
        )), box) for box in (body_obstacles or {}).get(target.group, []))
        for name, pins in endpoints.items():
            if name == target.net_name:
                continue
            for pin in pins:
                if pin.group != target.group:
                    continue
                end = _stub_endpoint(pin).position
                labels = [t for t in result if not t.rail and t.net_name == name
                          and t.group == pin.group and _target_is_outward(pin, t.position)]
                if labels:
                    end = labels[_nearest_local_target(pin, labels)].position
                box = _Envelope(min(pin.position.x, end.x) - 250_000,
                                min(pin.position.y, end.y) - 250_000,
                                max(pin.position.x, end.x) + 250_000,
                                max(pin.position.y, end.y) + 250_000)
                boxes.append((replace(target, text_alignment=(
                    "right" if side == "left" else "left"
                )), box))
        # Shared local nets can extend far beyond their pin stubs. Include
        # those planned conductors, not only the short terminal approaches.
        for name, pins in endpoints.items():
            local_pins = [p for p in pins if p.group == target.group]
            foreign_targets = [t for t in result if t.net_name == name
                               and t.group == target.group]
            routes: list[list[_PlacedEndpoint]] = []
            if foreign_targets:
                for foreign_index, other in enumerate(foreign_targets):
                    if name == target.net_name and (other is target or
                            _compatible_rail_members([*target.members, *other.members])):
                        continue
                    selected = [p for p in local_pins
                                if _nearest_local_target(p, foreign_targets) == foreign_index]
                    routes.append([*selected, _PlacedEndpoint(other.position)])
            else:
                routes.append(local_pins)
            for route in routes:
                for wire in _route_group(route):
                    if isinstance(wire, SchematicLine):
                        boxes.append((replace(target, text_alignment=(
                            "right" if side == "left" else "left"
                        )), _envelope_from_points((wire.start, wire.end))))
        for _ in range(len(boxes) + 1):
            conflict = next((
                (other, box) for other, box in boxes
                if (box.min_x - step // 4 <= x <= box.max_x + step // 4
                    and min(ys) <= box.max_y and box.min_y <= max(ys))
                or not _envelopes_do_not_overlap(
                    glyph.translated(Vector2(x, target.position.y)), box, step // 4,
                )
            ), None)
            if conflict is None:
                break
            other, box = conflict
            x = (box.min_x - step - max(0, glyph.max_x)
                 if side == "left" or (side != "right" and other.text_alignment == "right")
                 else box.max_x + step - min(0, glyph.min_x))
        result[index] = replace(target, position=Vector2(x, target.position.y))
    return result


def _split_rails_across_component_faces(
    targets: list[_NetSymbolTarget],
    component_endpoints: dict[str, list[_PlacedEndpoint]],
) -> list[_NetSymbolTarget]:
    """Give rail pins a local termination when the nearest target is behind them."""

    indexes_by_net: dict[str, list[int]] = defaultdict(list)
    for index, target in enumerate(targets):
        if target.rail:
            indexes_by_net[target.net_name].append(index)
    result = list(targets)
    offset = round(5.08 * 1_000_000)
    for net_name, indexes in indexes_by_net.items():
        unsafe: dict[tuple[str | None, str | None], list[_PlacedEndpoint]] = defaultdict(list)
        for endpoint in component_endpoints.get(net_name, []):
            nearest = min(
                indexes,
                key=lambda index: _distance(endpoint.position, targets[index].position),
            )
            if not _target_is_outward(endpoint, targets[nearest].position):
                unsafe[(endpoint.owner, endpoint.side)].append(endpoint)
        for endpoints in unsafe.values():
            template = targets[indexes[0]]
            stubs = [_stub_endpoint(endpoint).position for endpoint in endpoints]
            x = round(median(point.x for point in stubs))
            side = endpoints[0].side
            if side == "top":
                y = min(point.y for point in stubs) - offset
            elif side == "bottom":
                y = max(point.y for point in stubs) + offset
            elif template.ground:
                y = max(point.y for point in stubs) + offset
            else:
                y = min(point.y for point in stubs) - offset
            result.append(replace(template, position=Vector2(x, y), rotation=0.0))
    return result


def _target_is_outward(endpoint: _PlacedEndpoint, target: Vector2) -> bool:
    if endpoint.side == "left":
        return target.x < endpoint.position.x
    if endpoint.side == "right":
        return target.x > endpoint.position.x
    if endpoint.side == "top":
        return target.y < endpoint.position.y
    if endpoint.side == "bottom":
        return target.y > endpoint.position.y
    return True


def _component_net_endpoints(
    schematic: dict[str, Any],
    editor: FileSchematic,
) -> dict[str, list[_PlacedEndpoint]]:
    root_ref = schematic["root_ref"]
    instances = schematic["instances"]
    layout_groups, _ = _component_layout_groups(schematic)
    physical_refs = sorted(
        (
            ref
            for ref, instance in instances.items()
            if isinstance(ref, str)
            and isinstance(instance, dict)
            and instance.get("reference_designator")
        ),
        key=len,
        reverse=True,
    )
    by_designator = {instances[ref]["reference_designator"]: ref for ref in physical_refs}
    raw_by_path: dict[str, list[Any]] = defaultdict(list)
    for symbol in editor.document.symbols:
        if symbol.path is not None:
            raw_by_path[symbol.path].append(symbol)
    pin_geometry: dict[str, tuple[Vector2, str, str, _Envelope]] = {}
    for component_ref in physical_refs:
        path = component_ref.removeprefix(root_ref + ".")
        instance = instances[component_ref]
        number_by_name = symbol_pin_number_groups(instance)
        for symbol in raw_by_path.get(path, []):
            positions = placed_pin_positions(editor.document, symbol)
            sides = placed_pin_sides(editor.document, symbol)
            library_pins = symbol_library_pins(editor.document, symbol)
            strokes = _pin_stroke_envelopes(editor, symbol)
            for number, position in positions.items():
                pin_geometry[f"{component_ref}:{number}"] = (
                    position, sides[number], library_pins[number].electrical_type, strokes[number],
                )
        instance["__schemer_pin_numbers"] = number_by_name

    result: dict[str, list[_PlacedEndpoint]] = defaultdict(list)
    for net in schematic["nets"].values():
        if not isinstance(net, dict) or not isinstance(net.get("name"), str):
            continue
        for port in net.get("ports", []):
            if not isinstance(port, str):
                continue
            component_ref = next(
                (ref for ref in physical_refs if port.startswith(ref + ".")),
                None,
            )
            if component_ref is None:
                continue
            terminal = port.removeprefix(component_ref + ".").split(".", 1)[0]
            instance = instances[component_ref]
            number_by_name = instance["__schemer_pin_numbers"]
            numbers = number_by_name.get(terminal)
            if numbers is None and terminal.startswith("Pin_"):
                numbers = (terminal.removeprefix("Pin_"),)
            if numbers is None and terminal.isdigit():
                numbers = (terminal,)
            for number in numbers or (None,):
                geometry = pin_geometry.get(f"{component_ref}:{number}")
                if geometry is None:
                    raise KiCadSchematicError(
                        f"cannot resolve KiCad pin for {port.removeprefix(root_ref + '.')}"
                    )
                position, side, electrical_type, stroke = geometry
                group = layout_groups[component_ref]
                properties = _component_properties(schematic, component_ref)
                bank = ((by_designator.get(properties["owner"], properties["owner"]),
                         properties["group"])
                        if properties.get("owner") and properties.get("group")
                        and properties.get("role") in {"bypass", "shunt"} else None)
                rail_class = None
                if net.get("kind") in {"Power", "Ground"}:
                    role = properties.get("role")
                    if electrical_type in {"power_in", "power_out"} or role == "bypass":
                        rail_class = "supply"
                    elif electrical_type in {"input", "bidirectional"} or role in {
                        "pullup", "pulldown",
                    }:
                        rail_class = "signal-tie"
                endpoint = _PlacedEndpoint(position, side, component_ref, group,
                                           rail_class=rail_class, stroke=stroke, bank=bank)
                if endpoint not in result[net["name"]]:
                    result[net["name"]].append(endpoint)
    return result


def _route_group(
    endpoints: list[_PlacedEndpoint],
    *, obstacles: list[_Envelope] | None = None,
    foreign_wires: list[SchematicLine] | None = None,
) -> list[SchematicLine | Junction]:
    unique_by_geometry = {
        (endpoint.position.x, endpoint.position.y, endpoint.side): endpoint
        for endpoint in endpoints
    }
    unique = list(unique_by_geometry.values())
    if len(unique) < 2:
        return []
    segments: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    routed: list[_PlacedEndpoint] = []
    for endpoint in unique:
        stub = _stub_endpoint(endpoint)
        if stub.position != endpoint.position:
            _add_segment(segments, endpoint.position, stub.position)
        routed.append(stub)

    if len(routed) == 2:
        _connect_pair(segments, routed[0], routed[1])
    else:
        junctions: set[tuple[int, int]] = set()
        horizontal = sum(endpoint.side in {"left", "right"} for endpoint in routed)
        if horizontal >= len(routed) - horizontal:
            symbol_x = [endpoint.position.x for endpoint in routed if endpoint.side is None]
            trunk_x = symbol_x[0] if symbol_x else _shared_vertical_trunk(routed)
            anchor_ys = [
                endpoint.position.y
                for endpoint in routed
                if endpoint.side in {"left", "right", None}
            ]
            joins = []
            for endpoint in routed:
                if endpoint.side in {"left", "right", None}:
                    join_y = endpoint.position.y
                    _add_segment(segments, endpoint.position, Vector2(trunk_x, join_y))
                else:
                    join_y = min(anchor_ys, key=lambda y: abs(y - endpoint.position.y))
                    elbow = Vector2(endpoint.position.x, join_y)
                    _add_segment(segments, endpoint.position, elbow)
                    _add_segment(segments, elbow, Vector2(trunk_x, join_y))
                join = Vector2(trunk_x, join_y)
                joins.append(join)
                junctions.add((join.x, join.y))
            _add_segment(
                segments,
                Vector2(trunk_x, min(point.y for point in joins)),
                Vector2(trunk_x, max(point.y for point in joins)),
            )
        else:
            symbol_y = [endpoint.position.y for endpoint in routed if endpoint.side is None]
            trunk_y = symbol_y[0] if symbol_y else int(median(p.position.y for p in routed))
            anchor_xs = [
                endpoint.position.x
                for endpoint in routed
                if endpoint.side in {"top", "bottom", None}
            ]
            joins = []
            for endpoint in routed:
                if endpoint.side in {"top", "bottom", None}:
                    join_x = endpoint.position.x
                    _add_segment(segments, endpoint.position, Vector2(join_x, trunk_y))
                else:
                    join_x = min(anchor_xs, key=lambda x: abs(x - endpoint.position.x))
                    elbow = Vector2(join_x, endpoint.position.y)
                    _add_segment(segments, endpoint.position, elbow)
                    _add_segment(segments, elbow, Vector2(join_x, trunk_y))
                join = Vector2(join_x, trunk_y)
                joins.append(join)
                junctions.add((join.x, join.y))
            _add_segment(
                segments,
                Vector2(min(point.x for point in joins), trunk_y),
                Vector2(max(point.x for point in joins), trunk_y),
            )

    if (any(_segment_hits_box(Vector2(*a), Vector2(*b), box)
            for a, b in segments for box in obstacles or [])
            or any(_wire_contact(Vector2(*a), Vector2(*b), wire)
                   for a, b in segments for wire in foreign_wires or [])):
        segments = _route_clear_tree(unique, obstacles or [], foreign_wires or [])
    segments = _trim_wire_tails(segments, {(p.position.x, p.position.y) for p in unique})
    items: list[SchematicLine | Junction] = [
        SchematicLine(id="", start=Vector2(*start), end=Vector2(*end))
        for start, end in sorted(segments)
    ]
    if len(unique) > 2:
        degree: dict[tuple[int, int], int] = defaultdict(int)
        for start, end in segments:
            degree[start] += 1
            degree[end] += 1
        items.extend(Junction(id="", position=Vector2(*point))
                     for point, count in sorted(degree.items()) if count >= 3)
    return items


def _segment_hits_box(a: Vector2, b: Vector2, box: _Envelope) -> bool:
    return (min(a.x, b.x) <= box.max_x and max(a.x, b.x) >= box.min_x
            and min(a.y, b.y) <= box.max_y and max(a.y, b.y) >= box.min_y)


def _clear_orthogonal_path(
    a: Vector2, b: Vector2, obstacles: list[_Envelope],
    foreign_wires: list[SchematicLine],
) -> list[Vector2] | None:
    """Try straight, elbow and outside-lane routes, with bends before length."""

    candidates = [[a, b]] if a.x == b.x or a.y == b.y else []
    candidates.extend(([a, Vector2(a.x, b.y), b], [a, Vector2(b.x, a.y), b]))
    gap = 635_000
    xs = {box.min_x - gap for box in obstacles} | {box.max_x + gap for box in obstacles}
    ys = {box.min_y - gap for box in obstacles} | {box.max_y + gap for box in obstacles}
    for wire in foreign_wires:
        for p in (wire.start, wire.end):
            xs.update((p.x - gap, p.x + gap))
            ys.update((p.y - gap, p.y + gap))
    candidates.extend([a, Vector2(x, a.y), Vector2(x, b.y), b] for x in sorted(xs))
    candidates.extend([a, Vector2(a.x, y), Vector2(b.x, y), b] for y in sorted(ys))
    clear = []
    for points in candidates:
        path = [points[0]]
        for point in points[1:]:
            if point != path[-1]:
                path.append(point)
        if all(not _segment_hits_box(start, end, box)
               for start, end in zip(path, path[1:]) for box in obstacles) and all(
                   not _wire_contact(start, end, wire)
                   for start, end in zip(path, path[1:]) for wire in foreign_wires):
            clear.append(path)
    return min(clear, key=_path_cost) if clear else None


def _path_cost(path: list[Vector2]) -> tuple[int, int]:
    return (len(path) - 2,
            sum(abs(a.x - b.x) + abs(a.y - b.y) for a, b in zip(path, path[1:])))


def _wire_contact(a: Vector2, b: Vector2, wire: SchematicLine) -> bool:
    """Allow plain interior crossings, never overlaps or false T junctions."""

    c, d = wire.start, wire.end
    if not _segment_hits_box(a, b, _envelope_from_points((c, d))):
        return False
    horizontal = a.y == b.y
    other_horizontal = c.y == d.y
    if horizontal == other_horizontal:
        return True
    point = Vector2(c.x, a.y) if horizontal else Vector2(a.x, c.y)
    return point in (a, b, c, d)


def _route_clear_tree(
    endpoints: list[_PlacedEndpoint], obstacles: list[_Envelope],
    foreign_wires: list[SchematicLine],
) -> set[tuple[tuple[int, int], tuple[int, int]]]:
    """Keep legal pin escapes and join branches only through clear space."""

    stubs = [_stub_endpoint(p).position for p in endpoints]
    segments: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for pin, stub in zip(endpoints, stubs, strict=True):
        if any(_segment_hits_box(pin.position, stub, box) for box in obstacles):
            raise KiCadSchematicError(f"blocked pin exit at {pin.position.as_mm()}")
        _add_segment(segments, pin.position, stub)
    tree: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    joined = [stubs[0]]
    pending = list(stubs[1:])
    while pending:
        choices = []
        for index, start in enumerate(pending):
            anchors = {(p.x, p.y) for p in joined}
            for a, b in tree:
                anchors.add((max(a[0], min(start.x, b[0])),
                             max(a[1], min(start.y, b[1]))))
            for x, y in sorted(anchors):
                path = _clear_orthogonal_path(start, Vector2(x, y), obstacles, foreign_wires)
                if path is not None:
                    choices.append((_path_cost(path), index, path))
        if not choices:
            raise KiCadSchematicError("no clear orthogonal route between local pin exits")
        _, index, path = min(choices, key=lambda item: item[0])
        pending.pop(index)
        for start, end in zip(path, path[1:]):
            _add_segment(tree, start, end)
        joined.extend(path)
    return segments | tree


def _trim_wire_tails(
    segments: set[tuple[tuple[int, int], tuple[int, int]]],
    terminals: set[tuple[int, int]],
) -> set[tuple[tuple[int, int], tuple[int, int]]]:
    """Prune leftover pin-escape tails beyond a completed same-net junction."""

    points = {point for segment in segments for point in segment} | terminals
    edges = set()
    for start, end in segments:
        on_line = sorted(point for point in points
                         if min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
                         and min(start[1], end[1]) <= point[1] <= max(start[1], end[1]))
        edges.update(zip(on_line, on_line[1:]))
    neighbours: dict[tuple[int, int], set[tuple[int, int]]] = defaultdict(set)
    for start, end in edges:
        neighbours[start].add(end)
        neighbours[end].add(start)
    pending = [point for point, connected in neighbours.items()
               if len(connected) == 1 and point not in terminals]
    while pending:
        point = pending.pop()
        if len(neighbours[point]) != 1 or point in terminals:
            continue
        other = neighbours[point].pop()
        neighbours[other].remove(point)
        edges.discard(tuple(sorted((point, other))))
        if len(neighbours[other]) == 1 and other not in terminals:
            pending.append(other)
    return edges


def _stub_endpoint(endpoint: _PlacedEndpoint) -> _PlacedEndpoint:
    step = round(_PIN_STUB_MM * 1_000_000)
    delta = {
        "left": (-step, 0),
        "right": (step, 0),
        "top": (0, -step),
        "bottom": (0, step),
        None: (0, 0),
    }[endpoint.side]
    return _PlacedEndpoint(
        Vector2(endpoint.position.x + delta[0], endpoint.position.y + delta[1]),
        endpoint.side,
        endpoint.owner,
        endpoint.group,
    )


def _connect_pair(
    segments: set[tuple[tuple[int, int], tuple[int, int]]],
    first: _PlacedEndpoint,
    second: _PlacedEndpoint,
) -> None:
    a = first.position
    b = second.position
    if a.x == b.x or a.y == b.y:
        _add_segment(segments, a, b)
        return
    if first.side in {"left", "right"}:
        bend = Vector2(b.x, a.y)
    elif first.side in {"top", "bottom"}:
        bend = Vector2(a.x, b.y)
    elif second.side in {"left", "right"}:
        bend = Vector2(a.x, b.y)
    else:
        bend = Vector2(b.x, a.y)
    _add_segment(segments, a, bend)
    _add_segment(segments, bend, b)


def _add_segment(
    segments: set[tuple[tuple[int, int], tuple[int, int]]],
    start: Vector2,
    end: Vector2,
) -> None:
    if start == end:
        return
    pair = ((start.x, start.y), (end.x, end.y))
    segments.add(tuple(sorted(pair)))


def _distance(first: Vector2, second: Vector2) -> float:
    return hypot(first.x - second.x, first.y - second.y)
