from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from schemer.analysis.circuits import component_properties
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    envelope_from_points,
    envelopes_do_not_overlap,
    point_distance,
)
from schemer.kicad.geometry.library import (
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad.geometry.text import text_envelope
from schemer.kicad.items import (
    SchematicLine,
    Vector2,
    place_symbol,
)
from schemer.native.contacts import wires_intersect
from schemer.native.model import NetSymbolTarget
from schemer.native.policy import LOCAL_RAIL_CLUSTER_MM
from schemer.native.routing import route_group, stub_endpoint
from schemer.native.routing_model import PIN_STUB_MM, PlacedEndpoint


def inline_label_span(pins: list[PlacedEndpoint]) -> tuple[Vector2, Vector2] | None:
    """Usable span between facing pins, on either axis."""
    if len(pins) != 2:
        return None
    for axis, sides in (("x", ("right", "left")), ("y", ("bottom", "top"))):
        first, second = sorted(pins, key=lambda p: getattr(p.position, axis))
        other = "y" if axis == "x" else "x"
        if ((first.side, second.side) == sides
                and getattr(first.position, other) == getattr(second.position, other)):
            return stub_endpoint(first).position, stub_endpoint(second).position
    return None


def space_labeled_pin_connections(
    schematic: dict[str, Any], editor: FileSchematic,
    targets: list[NetSymbolTarget], endpoints: dict[str, list[PlacedEndpoint]],
) -> bool:
    """Reserve a named local wire's text span between a support part and IC."""

    by_owner = {schematic["root_ref"] + "." + s.zener_path: s
                for s in editor.get_symbols() if s.zener_path}
    raw = {s.uuid: s for s in editor.document.symbols}
    counts = {owner: len(symbol_library_pins(editor.document, raw[s.id]))
              for owner, s in by_owner.items()}
    boxes = {s.id: envelope_from_points(placed_symbol_body_positions(editor.document, raw[s.id]))
             for s in by_owner.values()}
    shifts: dict[str, Vector2] = {}
    for target in targets:
        if target.rail:
            continue
        grouped: dict[str | None, list[PlacedEndpoint]] = defaultdict(list)
        for pin in endpoints.get(target.net_name, []):
            grouped[pin.group].append(pin)
        if len(grouped) < 2:
            continue
        for pins in grouped.values():
            if inline_label_span(pins) is None:
                continue
            part = next((p for p in pins if counts.get(p.owner) == 2), None)
            owner = next((p for p in pins if counts.get(p.owner, 0) > 2), None)
            if part is None or owner is None:
                continue
            if component_properties(schematic, part.owner).get("role") in {"series", "divider"}:
                continue
            symbol = by_owner[part.owner]
            vertical = part.side in {"top", "bottom"}
            axis, other_axis = ("y", "x") if vertical else ("x", "y")
            direction = 1 if part.side in {"right", "bottom"} else -1
            # A bank caption may extend along the same wire lane. Include
            # its real span as well as the net name, without shrinking text.
            reach = 2_540_000
            for field in symbol.fields:
                if not field.visible or field.name not in {"Reference", "Value"}:
                    continue
                box = envelope_from_points(
                    text_envelope(field.text, symbol.transform.orientation),
                )
                if (getattr(box, "min_" + other_axis) - 885_000
                        <= getattr(part.position, other_axis)
                        <= getattr(box, "max_" + other_axis) + 885_000):
                    reach = max(reach, (getattr(box, "max_" + axis) - getattr(part.position, axis)
                        if direction == 1 else getattr(part.position, axis)
                        - getattr(box, "min_" + axis)) + 635_000)
            width = len(target.display_name) * 1_270_000
            required = reach + width + 2_540_000
            extra = required - abs(getattr(owner.position, axis) - getattr(part.position, axis))
            if extra > 0:
                delta = -direction * extra
                if abs(delta) > abs(getattr(shifts.get(symbol.id, Vector2(0, 0)), axis)):
                    shifts[symbol.id] = Vector2(0, delta) if vertical else Vector2(delta, 0)
    moved = False
    for symbol in by_owner.values():
        delta = shifts.get(symbol.id, 0)
        if not delta:
            continue
        destination = boxes[symbol.id].translated(delta)
        if any(not envelopes_do_not_overlap(destination, box, 500_000)
               for other, box in boxes.items() if other != symbol.id):
            continue
        place_symbol(symbol, Vector2(symbol.position.x + delta.x, symbol.position.y + delta.y),
                          symbol.transform.orientation)
        editor.update_items(symbol)
        boxes[symbol.id] = destination
        moved = True
    return moved


def localize_signal_labels(
    targets: list[NetSymbolTarget],
    component_endpoints: dict[str, list[PlacedEndpoint]],
    *, direct_groups: frozenset[str] = frozenset(),
) -> list[NetSymbolTarget]:
    """Put named signal endpoints on outward stubs instead of shared wire trees."""

    targets_by_net: dict[str, list[NetSymbolTarget]] = defaultdict(list)
    for target in targets:
        targets_by_net[target.net_name].append(target)
    result: list[NetSymbolTarget] = []
    for net_name, net_targets in targets_by_net.items():
        endpoints = component_endpoints.get(net_name, [])
        template = net_targets[0]
        if template.rail or not endpoints:
            result.extend(net_targets)
            continue
        endpoint_groups: dict[str, list[PlacedEndpoint]] = defaultdict(list)
        for index, endpoint in enumerate(endpoints):
            key = endpoint.group or endpoint.owner or f"endpoint-{index}"
            endpoint_groups[key].append(endpoint)
        if (not template.required and len(endpoints) > 1 and len(endpoint_groups) == 1
                and endpoints[0].group in direct_groups):
            # An accepted representation outranks generated seed labels and
            # the default wraparound heuristic. Rails and real sheet ports
            # still retain their termination semantics.
            continue
        local_clusters = [cluster for pins in endpoint_groups.values()
                          for cluster in ([pins] if pins[0].group in direct_groups else
                                          _split_wraparound_connections(
                              pins, [items for name, items in component_endpoints.items()
                                     if name != net_name],
                          ))]
        split_wraparound = len(local_clusters) > len(endpoint_groups)
        if (not template.required and len(endpoints) > 1 and len(endpoint_groups) == 1
                and not split_wraparound
                and (len(net_targets) == 1 or inline_label_span(endpoints) is not None)):
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
                nearest = min(range(len(net_targets)), key=lambda index: point_distance(
                    endpoint.position, net_targets[index].position,
                ))
                key = endpoint.group or endpoint.owner or ""
                endpoint_groups[key if endpoint.group in direct_groups
                                else f"{key}:{nearest}"].append(endpoint)
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
            position = stub_endpoint(endpoint).position
            alignment = "right" if endpoint.side == "left" else "left"
            rotation = 0.0
            if len(group_endpoints) > 1:
                # A label on a compound node must sit outside its wireset,
                # not centered on a branch or junction inside it.
                stubs = [stub_endpoint(p).position for p in group_endpoints]
                extent = [*stubs, *(p.position for p in group_endpoints)]
                step = round(PIN_STUB_MM * 1_000_000)
                inline = inline_label_span(group_endpoints)
                if inline is not None:
                    # Boxed cross-sheet labels need a branch off a through-wire;
                    # unlike plain labels, their text occupies the wire's axis.
                    vertical = inline[0].x == inline[1].x
                    position = inline[0]
                    if template.required:
                        position = Vector2(position.x - 2 * step, position.y) if vertical else (
                            Vector2(position.x, position.y - 2 * step))
                    else:
                        rotation = 90.0 if vertical else 0.0
                    alignment = "right" if vertical and not template.required else "left"
                elif endpoint.side == "right":
                    position = Vector2(max(p.x for p in extent) + step, position.y)
                    alignment = "left"
                else:
                    position = Vector2(min(p.x for p in extent) - step, position.y)
                    alignment = "right"
                # Outside the wireset, a boxed label can terminate a straight
                # extension. Do not add a perpendicular branch pre-emptively.
            key = endpoint.group, position.x, position.y
            if key not in positions:
                result.append(
                    replace(
                        template,
                        position=position,
                        rotation=rotation,
                        text_alignment=alignment,
                        owner=endpoint.owner,
                        group=endpoint.group,
                        members=tuple(group_endpoints),
                    )
                )
                positions.add(key)
    return result


def _split_wraparound_connections(
    pins: list[PlacedEndpoint], foreign_nets: list[list[PlacedEndpoint]],
) -> list[list[PlacedEndpoint]]:
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
                     for wire in route_group(local, topology_only=True)
                     if isinstance(wire, SchematicLine)]

    def compatible(a: PlacedEndpoint, b: PlacedEndpoint) -> bool:
        if a.owner == b.owner and a.side != b.side:
            return False
        if point_distance(a.position, b.position) > LOCAL_RAIL_CLUSTER_MM * 1_000_000:
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
        if any(wires_intersect(wire, other)
               for wire in route_group([a, b], topology_only=True)
               if isinstance(wire, SchematicLine) for other in foreign_wires):
            return False
        return True

    clusters: list[list[PlacedEndpoint]] = []
    for pin in sorted(pins, key=lambda p: (p.owner not in wrap_owners,
                                           p.position.y, p.position.x)):
        candidates = [cluster for cluster in clusters
                      if all(compatible(pin, member) for member in cluster)]
        if not candidates:
            clusters.append([pin])
        else:
            min(candidates, key=lambda cluster: min(
                point_distance(pin.position, member.position) for member in cluster
            )).append(pin)
    return clusters


def prune_unused_targets(
    targets: list[NetSymbolTarget],
    component_endpoints: dict[str, list[PlacedEndpoint]],
) -> list[NetSymbolTarget]:
    """Discard presentation symbols that no electrical endpoint selects."""

    targets_by_net: dict[str, list[tuple[int, NetSymbolTarget]]] = defaultdict(list)
    for index, target in enumerate(targets):
        targets_by_net[target.net_name].append((index, target))
    used: set[int] = set()
    for net_name, candidates in targets_by_net.items():
        for endpoint in component_endpoints.get(net_name, []):
            index, _ = candidates[nearest_local_target(endpoint, [t for _, t in candidates])]
            used.add(index)
    return [target for index, target in enumerate(targets) if index in used]


def nearest_local_target(
    endpoint: PlacedEndpoint, targets: list[NetSymbolTarget] | list[PlacedEndpoint],
) -> int:
    """An independently packed block must never borrow another block's terminal."""

    local = [index for index, target in enumerate(targets) if target.group == endpoint.group]
    if not local:
        raise KiCadSchematicError(f"no local net termination for block {endpoint.group!r}")
    assigned = [index for index in local if endpoint in targets[index].members]
    if assigned:
        return assigned[0]
    return min(local, key=lambda index: point_distance(endpoint.position, targets[index].position))
