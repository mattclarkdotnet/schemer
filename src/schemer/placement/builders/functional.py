from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    is_rail_net,
)
from schemer.analysis.drawing_model import Envelope
from schemer.analysis.measurements import with_annotation_envelope
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import ModuleLayout, Position
from schemer.placement.blocks.composition import block_from_positions
from schemer.placement.blocks.model import LayoutBlock, PlacedBlock, Rect
from schemer.placement.blocks.plan import BlockPlan
from schemer.placement.circuits.bypasses import place_bypasses
from schemer.placement.circuits.model import NetSymbolAttachment
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    anchor_net_symbol_attachments,
    attach_net_symbols,
    attachment_order,
    named_signal_attachment,
    rail_drawing_envelope,
    single_ended_net_symbol_attachments,
)
from schemer.placement.circuits.orientation import central_orientation, median_point
from schemer.placement.circuits.queries import (
    collect_components,
    connector_signal_points,
    has_straight_bundle,
    series_links,
)
from schemer.placement.circuits.series import (
    apply_series_annotation_clearance,
    place_connector,
    place_series_symbols,
    series_wires_from_ic,
)
from schemer.placement.circuits.shunts import place_shunts
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point


def functional_ic_block_from_zero(
    schematic: dict[str, Any],
    module: ModuleLayout,
    *,
    padding: float = 40.0,
) -> BlockPlan | None:
    """Generate one simple connector/series/IC block without input coordinates.

    The deliberately narrow eligibility rule is structural: exactly one active
    non-connector anchor, two connectors, a series signal path from each
    connector to the anchor, and only local shunts or bypass capacitors beyond
    those paths. Unsupported modules are left to their existing layout.
    """

    instances, _, components = collect_components(schematic, module)
    connectors = tuple(
        component for component in components if component.component_type == "connector"
    )
    active = tuple(
        component
        for component in components
        if component.component_type not in NON_OWNER_TYPES
        and component.component_type != "connector"
    )
    if len(connectors) != 2 or len(active) != 1:
        return None
    central = active[0]
    links = series_links(components, central, connectors)
    links_by_connector = {
        connector.ref: tuple(link for link in links if link.connector.ref == connector.ref)
        for connector in connectors
    }
    if any(not connector_links for connector_links in links_by_connector.values()):
        return None

    central_position = central_orientation(central, connectors, links)
    connector_signal_x = {
        connector.ref: float(
            median(
                median_point(
                    pin_positions(central.instance, central_position, link.central_terminal)
                ).x
                for link in links_by_connector[connector.ref]
            )
        )
        for connector in connectors
    }
    left, right = sorted(connectors, key=lambda connector: connector_signal_x[connector.ref])

    # Phase 1: establish the IC anchor. No support symbol or annotation may
    # influence its orientation or origin.
    positions: dict[str, Position] = {central.symbol_id: central_position}
    owners = {central.symbol_id: central.ref}

    # Phase 2: derive immutable electrical lanes from the IC's real pins.
    wire_groups = {
        connector.ref: series_wires_from_ic(
            central,
            central_position,
            links_by_connector[connector.ref],
            side=side,
        )
        for connector, side in ((left, "left"), (right, "right"))
    }

    # Phase 3: attach component bodies to those lanes, then attach connectors,
    # branches, and one-pin net symbols to the resulting wire endpoints.
    connector_positions: dict[str, Position] = {}
    direct_bundles: dict[str, bool] = {}
    series_refs = {link.passive.ref for link in links}
    for connector, side in ((left, "left"), (right, "right")):
        wires = wire_groups[connector.ref]
        passive_positions = place_series_symbols(wires)
        positions.update(passive_positions)
        owners.update({key: connector.ref for key in passive_positions})
        connector_position = place_connector(
            connector,
            wires,
            passive_positions,
            side=side,
        )
        positions[connector.symbol_id] = connector_position
        owners[connector.symbol_id] = connector.ref
        connector_positions[connector.ref] = connector_position
        direct_bundles[connector.ref] = has_straight_bundle(connector, connector_position, wires)
        if not direct_bundles[connector.ref]:
            # The resistor belongs to the connector breakout. Choose that
            # physical pin's row before placing it; do not inherit an IC row
            # across a connection that will terminate by name.
            local_wires = []
            for wire in wires:
                point = connector_signal_points(
                    connector, connector_position, wire.link.connector_net,
                )[0]
                local_wires.append(replace(
                    wire, owner_pin=point,
                    passive_pin_target=Point(wire.passive_pin_target.x, point.y),
                ))
            wire_groups[connector.ref] = tuple(local_wires)
            positions.update(place_series_symbols(tuple(local_wires)))

    attachments: list[tuple[str, NetSymbolAttachment]] = []
    shunt_refs: set[str] = set()
    for connector in (left, right):
        used, local_attachments = place_shunts(
            instances, components, (connector,), connector_positions, positions,
        )
        shunt_refs.update(used)
        owners.update({item.symbol_id: connector.ref for item in components if item.ref in used})
        attachments.extend((connector.ref, item) for item in local_attachments)
    bypass_refs, bypass_owner_terminals, bypass_attachments = place_bypasses(
        components,
        central,
        central_position,
        positions,
    )
    owners.update({item.symbol_id: central.ref for item in components if item.ref in bypass_refs})
    attachments.extend((central.ref, item) for item in bypass_attachments)
    classified = {central.ref, *(connector.ref for connector in connectors)}
    classified.update(series_refs)
    classified.update(shunt_refs)
    classified.update(bypass_refs)
    if classified != {component.ref for component in components}:
        return None

    for component in (central, *connectors):
        position = positions[component.symbol_id]
        local_attachments = list(
            anchor_net_symbol_attachments(
                component,
                position,
                skipped_terminals=(bypass_owner_terminals if component == central else None),
            )
        )
        local_attachments.extend(
            single_ended_net_symbol_attachments(
                component,
                position,
                tuple(item.ref for item in components),
            )
        )
        attachments.extend((component.ref, item) for item in local_attachments)
    # Finish passive annotation clearance before fixing named endpoints.
    for connector, side in ((left, "left"), (right, "right")):
        positions.update(apply_series_annotation_clearance(
            wire_groups[connector.ref], positions, side=side,
            obstacles=tuple(
                rail_drawing_envelope(item.net, item.position())
                for owner, item in attachments if owner == connector.ref
                and is_rail_net(item.net_ref, item.net)
            ),
        ))
        if not direct_bundles[connector.ref]:
            for wire in wire_groups[connector.ref]:
                link = wire.link
                attachments.extend((
                    (connector.ref, named_signal_attachment(
                        link.passive, positions[link.passive.symbol_id],
                        link.passive_central_terminal, "right" if side == "left" else "left",
                    )),
                    (central.ref, named_signal_attachment(
                        central, central_position, link.central_terminal, side,
                    )),
                ))
    net_symbols = NetSymbols()
    net_by_symbol: dict[str, dict[str, Any]] = {}
    owner_rail_envelopes: dict[str, list[Envelope]] = {}
    for owner, attachment in sorted(
        attachments, key=lambda item: (item[0], attachment_order(item[1])),
    ):
        # These drawings are still in independent owner-local coordinates.
        # Only same-owner rails can collide before the blocks are packed.
        net_symbols.rail_envelopes = owner_rail_envelopes.setdefault(owner, [])
        before = set(positions)
        attach_net_symbols(positions, (attachment,), symbols=net_symbols)
        symbol_id, = set(positions) - before
        owners[symbol_id] = owner
        net_by_symbol[symbol_id] = attachment.net

    occupied = {}
    visual: dict[str, Envelope] = {}
    for component in components:
        position = positions[component.symbol_id]
        bounds = placed_symbol_body_bounds(component.instance, position)
        occupied[component.symbol_id] = Rect(
            bounds.min_x,
            bounds.min_y,
            bounds.max_x - bounds.min_x,
            bounds.max_y - bounds.min_y,
        )
        full = placed_symbol_bounds(component.instance, position)
        labels = (
            str(component.instance.get("reference_designator", "")),
            attribute_string(component.instance, "value") or "",
        )
        visual[component.symbol_id] = with_annotation_envelope(
            Envelope(full.min_x, full.min_y, full.max_x, full.max_y), labels,
        )
    for symbol_id, net in net_by_symbol.items():
        position = positions[symbol_id]
        properties = net.get("properties", {})
        if "__symbol_value" in properties:
            full = placed_symbol_bounds({"attributes": properties}, position)
            envelope = Envelope(full.min_x, full.min_y, full.max_x, full.max_y)
        else:
            envelope = Envelope(position.x, position.y, position.x, position.y)
        visual[symbol_id] = with_annotation_envelope(
            envelope, (str(net.get("name", "")).rsplit(".", 1)[-1],),
        )
    if set(owners) != set(positions) or set(visual) != set(positions):
        raise ToolchainError("every local drawing must have an owner and a visual envelope")
    for key, position in positions.items():
        # Rotated symbols can have their stored anchor outside the painted
        # bounds. Use the same origin for measuring and normalizing a child.
        visual[key] = visual[key].union(
            Envelope(position.x, position.y, position.x, position.y),
        )

    # Pack completed local drawings, not bare connector/IC bodies. Inline
    # parts travel with their connector breakout; connections to the IC span
    # the gap. Preserve every electrical row during the rigid translation.
    owner_blocks = []
    top = min(envelope.min_y for envelope in visual.values()) - padding
    cursor_x = 0.0
    # Four millimetres of whitespace outside the caption allowances. The
    # diagram is scale independent; this is not tied to bitmap/page size.
    gap = 40.0
    for owner in (left, central, right):
        members = {key: position for key, position in positions.items() if owners[key] == owner.ref}
        envelope = visual[next(iter(members))]
        for key in members:
            envelope = envelope.union(visual[key])
        child = block_from_positions(
            owner.symbol_id.removeprefix("comp:"), members,
            occupied={key: occupied[key] for key in members if key in occupied},
            content_bounds=Rect(envelope.min_x, envelope.min_y, envelope.width, envelope.height),
            padding=0.0,
        )
        y = envelope.min_y - top
        owner_blocks.append(PlacedBlock(child, cursor_x, y))
        cursor_x += child.width + gap
    block = LayoutBlock(
        "functional-ic", cursor_x - gap,
        max(child.y + child.block.height for child in owner_blocks),
        children=tuple(owner_blocks), minimum_child_spacing=gap,
    )
    result = BlockPlan(block)
    result.validate()
    return result
