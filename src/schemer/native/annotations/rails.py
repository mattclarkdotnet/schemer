from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from statistics import median

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
    point_distance,
    rotated_envelope,
)
from schemer.kicad.geometry.library import placed_symbol_body_positions
from schemer.kicad.items import (
    SchematicLine,
    Vector2,
)
from schemer.native.annotations.signal_topology import nearest_local_target
from schemer.native.model import NetSymbolTarget
from schemer.native.policy import (
    LOCAL_RAIL_CLUSTER_MM,
    LOCAL_RAIL_OFFSET_MM,
    RAIL_CORRIDOR_PITCH_MM,
)
from schemer.native.routing import route_group, stub_endpoint
from schemer.native.routing_model import PlacedEndpoint


def _rail_cluster_position(
    cluster: list[PlacedEndpoint], *, ground: bool, offset: int,
    foreign_pins: list[PlacedEndpoint] | None = None,
) -> Vector2:
    positions = [stub_endpoint(endpoint).position for endpoint in cluster]
    if {p.side for p in cluster} == {"top"}:
        return Vector2(round(median(p.x for p in positions)), min(p.y for p in positions))
    if {p.side for p in cluster} == {"bottom"}:
        return Vector2(round(median(p.x for p in positions)), max(p.y for p in positions))
    # A marker on a continuing vertical conductor reads as part of the
    # component above/below it. Give that named node its own short branch.
    if ({endpoint.side for endpoint in cluster} == {"top", "bottom"}
            and len({endpoint.position.x for endpoint in cluster}) == 1):
        y = round(median(endpoint.position.y for endpoint in cluster))
        candidates = [Vector2(positions[0].x + direction * 2 * offset, y)
                      for direction in (1, -1)]
        neighbours = [pin for pin in foreign_pins or [] if pin.group == cluster[0].group]
        return max(candidates, key=lambda point: min(
            (point_distance(point, pin.position) for pin in neighbours), default=float("inf"),
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
    cluster: list[PlacedEndpoint], foreign_pins: list[PlacedEndpoint],
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
    wires = [item for item in route_group([*cluster, PlacedEndpoint(position)],
                                          topology_only=True)
             if isinstance(item, SchematicLine)]
    return not any(
        min(wire.start.x, wire.end.x) <= pin.position.x <= max(wire.start.x, wire.end.x)
        and min(wire.start.y, wire.end.y) <= pin.position.y <= max(wire.start.y, wire.end.y)
        for pin in foreign_pins if pin.group == cluster[0].group
        for wire in wires
    )


def localize_rail_symbols(
    targets: list[NetSymbolTarget],
    component_endpoints: dict[str, list[PlacedEndpoint]],
    *,
    cluster_mm: float = LOCAL_RAIL_CLUSTER_MM,
    offset_mm: float = LOCAL_RAIL_OFFSET_MM,
) -> list[NetSymbolTarget]:
    """Derive north/south rail symbols from nearby connected pin clusters."""

    targets_by_net: dict[str, list[NetSymbolTarget]] = defaultdict(list)
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
        clusters: list[list[PlacedEndpoint]] = []
        for endpoint in sorted(
            endpoints,
            key=lambda item: (item.position.y, item.position.x, item.owner or ""),
        ):
            stub = stub_endpoint(endpoint)
            cluster = next(
                (
                    current
                    for current in clusters
                    if all(member.group == endpoint.group for member in current)
                    and compatible_rail_members([*current, endpoint])
                    and (any(
                         endpoint.bank is not None and (member.bank == endpoint.bank
                             or not template.ground and member.owner == endpoint.bank[0])
                         or not template.ground and member.bank is not None
                         and endpoint.owner == member.bank[0]
                         for member in current)
                         or all(point_distance(stub.position, stub_endpoint(member).position)
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
                    rotation=(180.0 if {p.side for p in cluster} ==
                              ({"top"} if template.ground else {"bottom"}) else 0.0),
                    owner=owners.pop() if len(owners) == 1 else None,
                    group=groups.pop() if len(groups) == 1 else None,
                    members=tuple(cluster),
                )
            )
    return result


def compatible_rail_members(members: list[PlacedEndpoint]) -> bool:
    """Supply connections and signal ties use separate same-net wiresets."""

    return len({pin.rail_class for pin in members if pin.rail_class is not None}) <= 1


def rail_approach_side(target: NetSymbolTarget) -> str:
    """Meet a rail's anchor from outside its graphic, even on its own net."""
    sides = ("top", "left", "bottom", "right")
    return sides[(int(target.rotation // 90) + (0 if target.ground else 2)) % 4]


def separate_same_face_rail_corridors(
    targets: list[NetSymbolTarget],
    component_endpoints: dict[str, list[PlacedEndpoint]],
    *,
    pitch_mm: float = RAIL_CORRIDOR_PITCH_MM,
) -> list[NetSymbolTarget]:
    """Give distinct rail nets separate outward lanes on one component face.

    Independently localized rails can otherwise land on the same axis. Even
    when their wire segments do not quite touch, a one-pin-pitch gap reads as
    a continuous wire between different nets. Keep the nearest rail in its
    natural lane and move subsequent rails farther outward.
    """

    endpoint_indexes_by_target: dict[int, list[PlacedEndpoint]] = defaultdict(list)
    indexes_by_group_and_net: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, target in enumerate(targets):
        if target.rail and target.group is not None:
            indexes_by_group_and_net[(target.group, target.net_name)].append(index)
    for (group, net_name), indexes in indexes_by_group_and_net.items():
        for endpoint in component_endpoints.get(net_name, []):
            if endpoint.group != group:
                continue
            nearest = indexes[nearest_local_target(endpoint, [targets[i] for i in indexes])]
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


def rail_glyph_bounds(editor: FileSchematic) -> dict[bool, Envelope]:
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
            bounds[shape == "GND"] = envelope_from_points(points)
    return bounds


def merge_touching_rail_symbols(
    targets: list[NetSymbolTarget], glyph_bounds: dict[bool, Envelope],
) -> list[NetSymbolTarget]:
    """Clearance can bring same-net glyphs together; share that termination."""

    result: list[NetSymbolTarget] = []
    for target in targets:
        glyph = glyph_bounds.get(target.ground)
        if not target.rail or glyph is None:
            result.append(target)
            continue
        for index, other in enumerate(result):
            if (other.rail and other.net_name == target.net_name and other.group == target.group
                    and other.rotation == target.rotation
                    and compatible_rail_members([*other.members, *target.members])
                    and not envelopes_do_not_overlap(
                        rotated_envelope(glyph, other.rotation).translated(other.position),
                        rotated_envelope(glyph, target.rotation).translated(target.position),
                        635_000)):
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
