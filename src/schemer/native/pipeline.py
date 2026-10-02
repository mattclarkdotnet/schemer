from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from itertools import chain
from typing import Any

from schemer.analysis.circuits import (
    component_layout_groups,
    direct_circuit_groups,
    power_stage_order,
    validate_owned_networks,
)
from schemer.analysis.inventory import StructuralInventory, structural_inventory
from schemer.analysis.visibility import clean_schematic_labels, electrical_view
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
)
from schemer.kicad.geometry.text import label_envelope
from schemer.kicad.items import (
    BaseLabel,
    Junction,
    NoConnectMarker,
    SchematicLine,
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.annotations.field_fitting import resolve_symbol_field_overlaps
from schemer.native.annotations.fields import (
    apply_component_display_values,
    consolidate_multi_unit_fields,
    position_bank_fields,
    position_component_fields,
)
from schemer.native.annotations.pipeline import localize_net_targets
from schemer.native.annotations.rails import rail_glyph_bounds
from schemer.native.annotations.signal_topology import (
    nearest_local_target,
    prune_unused_targets,
    space_labeled_pin_connections,
)
from schemer.native.annotations.symbols import place_net_symbols
from schemer.native.association import associate_components
from schemer.native.endpoints import component_net_endpoints
from schemer.native.model import NativeLayoutReport
from schemer.native.obstacles import component_annotation_obstacles, label_layout_obstacles
from schemer.native.packing import pack_completed_groups
from schemer.native.placement.attachments import (
    align_single_pin_attachments,
    pack_supported_units,
)
from schemer.native.placement.banks import place_owned_shunt_banks, place_shared_rail_banks
from schemer.native.placement.branches import (
    align_perpendicular_branches,
    align_same_face_terminal_exits,
    place_top_pin_bypasses,
    place_vertical_pin_bias_branches,
)
from schemer.native.placement.bridges import place_pin_bridges
from schemer.native.placement.chains import stack_owned_passive_runs
from schemer.native.placement.inline import (
    align_authored_inline_banks,
    compact_inline_connections,
)
from schemer.native.placement.networks import place_owned_pin_networks
from schemer.native.placement.orientation import (
    orient_shunts_away_from_signal_routes,
    orient_single_terminal_rails,
)
from schemer.native.placement.queries import local_link_priority
from schemer.native.policy import VIEWER_UNITS_PER_MM
from schemer.native.reuse import (
    compact_connected_circuits,
    reuse_repeated_block_geometry,
    select_primary_group,
)
from schemer.native.route_backtracking import route_with_label_backtracking
from schemer.native.routing import pin_contact_obstacle, route_group
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.seed import (
    display_names,
    drawing_translation,
    flatten_positions,
    net_symbol_targets,
    ordered_unit_position,
    power_unit_rotation,
)
from schemer.native.validation import clear_final_label_wires, validate_fixed_geometry
from schemer.symbols.geometry import placed_symbol_origin


def layout_kicad_from_zener(
    schematic: dict[str, Any],
    editor: FileSchematic,
    *,
    right_of: tuple[tuple[str, str], ...] = (),
    net_display_names: dict[str, str] | None = None,
    boundary_nets: frozenset[str] = frozenset(),
    global_nets: frozenset[str] = frozenset(),
    inventory: StructuralInventory | None = None,
) -> NativeLayoutReport:
    """Apply accepted procedural positions and rebuild presentation wiring."""

    visible = electrical_view(schematic)
    inventory = inventory if inventory is not None else structural_inventory(visible)
    validate_owned_networks(visible)
    associations = associate_components(visible, editor.document, allow_unexpected=True)
    layout_groups, connector_groups = component_layout_groups(visible)
    direct_groups = direct_circuit_groups(visible)
    # Components and net symbols must use the same service-filtered view.
    physical_positions, net_positions = flatten_positions(visible)
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
    translation = drawing_translation(visible_targets)

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
                # Keep separately drawn units on one axis to show package identity.
                unit_x, unit_y = ordered_unit_position(base_x, base_y, index)
                unit_rotation = power_unit_rotation(editor, raw_symbol)
            target = Vector2.from_xy_mm(
                (unit_x + translation[0]) / VIEWER_UNITS_PER_MM,
                (unit_y + translation[1]) / VIEWER_UNITS_PER_MM,
            )
            place_symbol(item, target, unit_rotation)
            component_updates.append(item)
            component_groups[item.id] = layout_groups[association.instance_ref]

    editor.update_items(component_updates)
    orient_single_terminal_rails(visible, editor)
    # Choose the run's shape during initial native placement, before the
    # individual-attachment passes can string it out along a pin axis.
    bank_ids = stack_owned_passive_runs(visible, editor)
    folded = frozenset(bank_ids)
    align_single_pin_attachments(visible, editor, excluded=folded)
    compact_inline_connections(visible, editor, excluded=folded)
    bank_ids.update(align_authored_inline_banks(visible, editor, excluded=folded))
    place_vertical_pin_bias_branches(visible, editor)
    place_top_pin_bypasses(visible, editor)
    align_perpendicular_branches(visible, editor)
    align_same_face_terminal_exits(visible, editor)
    place_owned_shunt_banks(visible, editor)
    place_owned_pin_networks(visible, editor)
    place_pin_bridges(visible, editor)
    orient_shunts_away_from_signal_routes(visible, editor)
    component_updates = [
        item for item in editor.get_symbols() if item.id in component_groups
    ]
    primary_group = select_primary_group(editor, associations, layout_groups)
    display = clean_schematic_labels(visible)
    apply_component_display_values(display, associations, component_updates)
    editor.update_items(component_updates)
    repeated_widths = reuse_repeated_block_geometry(visible, editor, layout_groups, inventory)
    component_updates = [s for s in editor.get_symbols() if s.id in component_groups]
    component_updates = position_component_fields(editor, component_updates)
    position_bank_fields(editor, component_updates, bank_ids, repeated_widths,
                          component_groups=component_groups)
    consolidate_multi_unit_fields(editor, associations, component_updates)
    editor.update_items(component_updates)
    pack_supported_units(visible, editor)
    compact_connected_circuits(visible, editor, layout_groups, direct_groups)
    place_shared_rail_banks(visible, editor)
    component_updates = [s for s in editor.get_symbols() if s.id in component_groups]

    component_endpoints = component_net_endpoints(visible, editor)
    display_by_net = (net_display_names if net_display_names is not None
                      else display_names(schematic))
    templates = net_symbol_targets(
        schematic,
        net_positions,
        display_by_net,
        translation,
        {},
        Vector2(0, 0),
        component_endpoints,
        required_nets=boundary_nets | global_nets,
    )
    if space_labeled_pin_connections(visible, editor, templates, component_endpoints):
        align_authored_inline_banks(visible, editor, excluded=folded)
        component_endpoints = component_net_endpoints(visible, editor)
        component_updates = [s for s in editor.get_symbols() if s.id in component_groups]
    glyph_bounds = rail_glyph_bounds(editor)
    # A rail without a native glyph falls back to a label, so reserve label
    # geometry rather than positioning it as an invisible power symbol.
    templates = [replace(t, rail=False) if t.rail and t.ground not in glyph_bounds else t
                 for t in templates]
    caption_obstacles = component_annotation_obstacles(editor, component_groups)
    targets = localize_net_targets(
        templates, component_endpoints, glyph_bounds, caption_obstacles,
        label_layout_obstacles(editor, component_groups),
        direct_groups=direct_groups,
    )
    # Component geometry is complete before termination placement. A moved
    # rail must find a legal approach; it must not drag its attachment through
    # an already allocated label bank and restart placement with new conflicts.
    targets = prune_unused_targets(targets, component_endpoints)
    (
        power_updates,
        label_creates,
        label_net_names,
        label_anchors,
        target_endpoints,
        used_power_ids,
        target_item_groups,
    ) = place_net_symbols(editor, targets, global_nets=global_nets)
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
    routing_net_names: list[str] = []
    # Routing must preserve the same constrained caption slots as label fitting.
    body_obstacles = label_layout_obstacles(editor, {**component_groups, **target_item_groups})
    # Reserve short, unlabelled local links before long branching trees can
    # occupy their turn lanes. Source declaration order is not routing intent.
    routing_nets = sorted(visible["nets"].values(), key=lambda net: local_link_priority(
        component_endpoints.get(net.get("name"), []),
        target_endpoints.get(net.get("name"), [])))
    for net in routing_nets:
        if not isinstance(net, dict) or not isinstance(net.get("name"), str):
            continue
        net_name = net["name"]
        first_item = len(routing_items)
        endpoints = component_endpoints.get(net_name, [])
        if net.get("kind") == "NotConnected":
            for endpoint in endpoints:
                routing_items.append(NoConnectMarker(id="", position=endpoint.position))
                routing_groups.append(endpoint.group)
                routing_net_names.append(net_name)
            continue
        symbols = target_endpoints.get(net_name, [])
        obstacles = {group: list(boxes) for group, boxes in body_obstacles.items()}
        # Fitted labels reserve both painted bounds and electrical anchors.
        for label, label_net, anchor in zip(
                created_labels, label_net_names, label_anchors, strict=True):
            if label_net != net_name:
                box = envelope_from_points(label_envelope(label.text))
                obstacles.setdefault(anchor.group, []).append(
                    Envelope(box.min_x - 250_000, box.min_y - 250_000,
                              box.max_x + 250_000, box.max_y + 250_000))
                # Even a local label has an electrical anchor below its
                # text. A foreign wire touching it would silently join nets.
                obstacles[anchor.group].append(pin_contact_obstacle(
                    replace(anchor, position=label.position)))
        foreign_wires: dict[str | None, list[SchematicLine]] = defaultdict(list)
        for item, group in zip(routing_items, routing_groups, strict=True):
            if isinstance(item, SchematicLine):
                foreign_wires[group].append(item)
        for name, pins in chain(component_endpoints.items(), target_endpoints.items()):
            if name != net_name:
                for p in pins:
                    obstacles.setdefault(p.group, []).append(pin_contact_obstacle(p))
        if symbols:
            groups: dict[int, list[PlacedEndpoint]] = defaultdict(list)
            for endpoint in endpoints:
                nearest = nearest_local_target(endpoint, symbols)
                groups[nearest].append(endpoint)
            for index, symbol in enumerate(symbols):
                branch = [*groups[index], symbol]
                label_index = next((i for i, (label, name) in enumerate(zip(
                    created_labels, label_net_names, strict=True))
                    if name == net_name and label.position == symbol.position
                    and label_anchors[i].group == symbol.group), None)
                label = created_labels[label_index] if label_index is not None else None
                created, moved = route_with_label_backtracking(
                    branch, label, obstacles.get(symbol.group, []),
                    foreign_wires[symbol.group], net_name,
                    label_layout_obstacles(editor, component_groups).get(symbol.group, []))
                if moved is not None:
                    editor.update_items(moved)
                    created_labels[label_index] = moved
                    symbols[index] = replace(symbol, position=moved.position)
                routing_items.extend(created)
                routing_groups.extend([symbol.group] * len(created))
        else:
            endpoint_groups = {endpoint.group for endpoint in endpoints}
            group = endpoint_groups.pop() if len(endpoint_groups) == 1 else None
            created = route_group(endpoints, obstacles=obstacles.get(group, []),
                                  foreign_wires=foreign_wires[group], net_name=net_name,
                                  allow_detours=True)
            routing_items.extend(created)
            routing_groups.extend([group] * len(created))
        routing_net_names.extend([net_name] * (len(routing_items) - first_item))

    created_routing = editor.create_items(routing_items)
    item_groups = {**component_groups, **target_item_groups}
    for item, group in zip(created_routing, routing_groups, strict=True):
        if group is not None:
            item_groups[item.id] = group
    clear_final_label_wires(editor, item_groups)
    field_updates = resolve_symbol_field_overlaps(editor, component_updates)
    editor.update_items(field_updates)
    validate_fixed_geometry(editor, item_groups, wire_nets={
        item.id: name for item, name in zip(created_routing, routing_net_names, strict=True)
        if isinstance(item, SchematicLine)})
    page_settings = pack_completed_groups(
        editor,
        item_groups,
        primary_group,
        connector_groups=connector_groups,
        right_of=right_of,
        stage_order=power_stage_order(visible, layout_groups),
    )
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
        structural_inventory=inventory,
    )
