from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from statistics import median

from schemer.core.diagnostics import record_draft_issue
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.geometry.envelopes import Envelope, union
from schemer.kicad.items import Junction, SchematicLine, Vector2
from schemer.native.routing_model import (
    CROSSING_ENDPOINT_CLEARANCE,
    PARALLEL_WIRE_CLEARANCE,
    PIN_STUB_MM,
    PlacedEndpoint,
)
from schemer.native.routing_paths import (
    clear_orthogonal_path,
    path_cost,
    segment_hits_box,
    wire_contact,
)


def shared_vertical_trunk(endpoints: list[PlacedEndpoint]) -> int:
    """Prefer a perpendicular branch's axis within the horizontal pin exits."""

    vertical = [p for p in endpoints if p.side in {"top", "bottom"}]
    x = (vertical[0].position.x if len(vertical) == 1
         else round(median(p.position.x for p in endpoints)))
    right = [p.position.x for p in endpoints if p.side == "right"]
    left = [p.position.x for p in endpoints if p.side == "left"]
    lower = max(right, default=min([x, *left]))
    upper = min(left, default=max([x, *right]))
    # A median behind an output pin makes its escape double back around the
    # painted pin stroke. Stay within the common outward corridor when one
    # exists, even when all the endpoints are horizontal.
    return (max(lower, min(x, upper)) if lower <= upper
            else round(median(p.position.x for p in endpoints)))


def preview_clear_route(
    pins: list[PlacedEndpoint], obstacles: list[Envelope],
    *, allow_detours: bool = False,
) -> list[SchematicLine]:
    """Evaluate label candidates against fixed geometry without draft findings."""
    try:
        return [w for w in route_group(pins, obstacles=obstacles, report_failure=False,
                                      allow_detours=allow_detours)
                if isinstance(w, SchematicLine)]
    except KiCadSchematicError:
        return []


def _pin_inward_obstacle(pin: PlacedEndpoint) -> Envelope | None:
    if pin.stroke is None or pin.side is None:
        return None
    b, p = pin.stroke, pin.position
    box = Envelope(max(b.min_x, p.x + 1) if pin.side == "left" else b.min_x,
                    max(b.min_y, p.y + 1) if pin.side == "top" else b.min_y,
                    min(b.max_x, p.x - 1) if pin.side == "right" else b.max_x,
                    min(b.max_y, p.y - 1) if pin.side == "bottom" else b.max_y)
    return box if box.min_x <= box.max_x and box.min_y <= box.max_y else None


def route_group(
    endpoints: list[PlacedEndpoint],
    *, obstacles: list[Envelope] | None = None,
    foreign_wires: list[SchematicLine] | None = None,
    net_name: str | None = None,
    report_failure: bool = True,
    topology_only: bool = False,
    allow_detours: bool = False,
) -> list[SchematicLine | Junction]:
    unique_by_geometry = {
        (endpoint.position.x, endpoint.position.y, endpoint.side): endpoint
        for endpoint in endpoints
    }
    unique = list(unique_by_geometry.values())
    # Opposing pins already meeting at a node are connected. Extending each
    # one's mandatory free-end stub would run into the opposite component.
    # Route from their common node, retaining a dot when a third branch joins.
    by_position: dict[tuple[int, int], list[PlacedEndpoint]] = defaultdict(list)
    for endpoint in unique:
        by_position[(endpoint.position.x, endpoint.position.y)].append(endpoint)
    joined_pins = set()
    for point, coincident in by_position.items():
        sides = {endpoint.side for endpoint in coincident}
        if {"top", "bottom"} <= sides or {"left", "right"} <= sides:
            joined_pins.add(point)
            unique = [p for p in unique if (p.position.x, p.position.y) != point]
            unique.append(replace(coincident[0], side=None))
    if len(unique) < 2:
        return []
    # A pin's painted inward stroke is not wireable, even by its own net.
    # Otherwise a projected trunk can reverse the escape and pruning leaves
    # a junction on the body instead of beyond the actual electrical anchor.
    # Clustering deliberately examines the projected topology, before any
    # detours. A detour must not make a wraparound connection look local and
    # thereby merge rail terminations that should remain separate.
    if not topology_only:
        obstacles = [*(obstacles or []), *[box for pin in unique
                                          if (box := _pin_inward_obstacle(pin)) is not None]]
    segments: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    routed: list[PlacedEndpoint] = []
    for endpoint in unique:
        stub = stub_endpoint(endpoint)
        if stub.position != endpoint.position:
            _add_segment(segments, endpoint.position, stub.position)
        routed.append(stub)

    if len(routed) == 2:
        _connect_pair(segments, routed[0], routed[1])
    else:
        horizontal = sum(endpoint.side in {"left", "right"} for endpoint in routed)
        if horizontal >= len(routed) - horizontal:
            symbol_x = [endpoint.position.x for endpoint in routed if endpoint.side is None]
            trunk_x = symbol_x[0] if symbol_x else shared_vertical_trunk(routed)
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
            _add_segment(
                segments,
                Vector2(min(point.x for point in joins), trunk_y),
                Vector2(max(point.x for point in joins), trunk_y),
            )

    if (any(segment_hits_box(Vector2(*a), Vector2(*b), box)
            for a, b in segments for box in obstacles or [])
            or any(wire_contact(Vector2(*a), Vector2(*b), wire)
                   for a, b in segments for wire in foreign_wires or [])):
        try:
            segments = _route_clear_tree(unique, obstacles or [], foreign_wires or [],
                                         allow_detours=allow_detours)
        except KiCadSchematicError as error:
            message = f"routing net {net_name}: {error}" if net_name else str(error)
            _route_failure(message, net_name, report_failure)
            # A failed draft route must remain visibly incomplete, never a
            # plausible-looking wire through a body or shorting another net.
            segments = set()
    segments = _trim_wire_tails(segments, {(p.position.x, p.position.y) for p in unique})
    # Pruning an extended escape at a nearer label/branch can expose a new
    # endpoint beside a crossing. Check the completed geometry so label
    # backtracking can relocate that anchor rather than accept an unsafe bend.
    if any(wire_contact(Vector2(*a), Vector2(*b), wire)
           for a, b in segments for wire in foreign_wires or []):
        message = f"routing net {net_name}: completed route lacks crossing clearance"
        _route_failure(message, net_name, report_failure)
        segments = set()
    items: list[SchematicLine | Junction] = [
        SchematicLine(id="", start=Vector2(*start), end=Vector2(*end))
        for start, end in sorted(segments)
    ]
    items.extend(Junction(id="", position=Vector2(*point)) for point in sorted(joined_pins))
    if len(unique) > 2:
        degree: dict[tuple[int, int], int] = defaultdict(int)
        for start, end in segments:
            degree[start] += 1
            degree[end] += 1
        items.extend(Junction(id="", position=Vector2(*point))
                     for point, count in sorted(degree.items())
                     if count >= 3 and point not in joined_pins)
    return items


def _route_failure(message: str, net_name: str | None, report: bool) -> None:
    if not report or not record_draft_issue(
            "route-clearance-failed", message, (net_name,) if net_name else ()):
        raise KiCadSchematicError(message)


def pin_contact_obstacle(pin: PlacedEndpoint) -> Envelope:
    """Protect an unrouted terminal's approach, not an arbitrary full stub.

    A route laid first must obey the same endpoint clearance as a route laid
    last. Otherwise it can cross just outside a future pin and make that
    pin's escape impossible. Crossings beyond the contact clearance remain
    legal; the artificial end of a normal stub is not a fixed obstacle.
    """
    dx, dy = {"left": (-1, 0), "right": (1, 0), "top": (0, -1),
              "bottom": (0, 1), None: (0, 0)}[pin.side]
    p = pin.position
    end = Vector2(p.x + dx * CROSSING_ENDPOINT_CLEARANCE,
                  p.y + dy * CROSSING_ENDPOINT_CLEARANCE)
    # Apply the same transverse spacing as wire_contact will require when
    # this pin is routed later. A 0.25 mm contact-only box allowed an earlier
    # parallel wire to consume the future exit's 1.27 mm routing lane.
    across = PARALLEL_WIRE_CLEARANCE - 1
    pad_x = across if dy else 250_000
    pad_y = across if dx else 250_000
    approach = Envelope(min(p.x, end.x) - pad_x, min(p.y, end.y) - pad_y,
                         max(p.x, end.x) + pad_x, max(p.y, end.y) + pad_y)
    return union(approach, pin.stroke) if pin.stroke is not None else approach


def _route_clear_tree(
    endpoints: list[PlacedEndpoint], obstacles: list[Envelope],
    foreign_wires: list[SchematicLine],
    *, allow_detours: bool = False,
) -> set[tuple[tuple[int, int], tuple[int, int]]]:
    """Keep legal pin escapes and join branches only through clear space."""

    stubs = [_clear_pin_escape(p, obstacles, foreign_wires) for p in endpoints]
    segments: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for pin, stub in zip(endpoints, stubs, strict=True):
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
                path = clear_orthogonal_path(start, Vector2(x, y), obstacles, foreign_wires,
                                              allow_detours=allow_detours)
                if path is not None:
                    choices.append((path_cost(path), index, path))
        if not choices:
            raise KiCadSchematicError("no clear orthogonal route between local pin exits")
        _, index, path = min(choices, key=lambda item: item[0])
        pending.pop(index)
        for start, end in zip(path, path[1:]):
            _add_segment(tree, start, end)
        joined.extend(path)
    return segments | tree


def _clear_pin_escape(
    pin: PlacedEndpoint, obstacles: list[Envelope], foreign_wires: list[SchematicLine],
) -> Vector2:
    """Keep artificial pin-stub ends visibly clear of crossing foreign wires.

    The physical pin remains fixed. A slightly longer outward escape makes
    the crossing an interior intersection, clear of an apparent T junction.
    Body collisions and collinear wire contacts remain hard failures.
    """
    stub = stub_endpoint(pin).position
    candidates = [stub]
    if pin.side is not None:
        dx = (stub.x > pin.position.x) - (stub.x < pin.position.x)
        dy = (stub.y > pin.position.y) - (stub.y < pin.position.y)
        gap = CROSSING_ENDPOINT_CLEARANCE
        for wire in foreign_wires:
            if dx and wire.start.x == wire.end.x:
                x = wire.start.x + dx * gap
                if dx * (x - stub.x) > 0:
                    candidates.append(Vector2(x, stub.y))
            elif dy and wire.start.y == wire.end.y:
                y = wire.start.y + dy * gap
                if dy * (y - stub.y) > 0:
                    candidates.append(Vector2(stub.x, y))
    candidates = sorted(candidates, key=lambda p: (
        abs(p.x - pin.position.x) + abs(p.y - pin.position.y)
    ))
    if pin.side is not None:
        # A nominal stub is not a mandatory wire length. A legal shorter
        # exit can turn before a nearby wire whose endpoint prevents a
        # clean interior crossing of the longer stub.
        candidates.append(Vector2(pin.position.x + dx * CROSSING_ENDPOINT_CLEARANCE,
                                  pin.position.y + dy * CROSSING_ENDPOINT_CLEARANCE))
    for candidate in candidates:
        if (not any(segment_hits_box(pin.position, candidate, box) for box in obstacles)
                and not any(wire_contact(pin.position, candidate, wire)
                            for wire in foreign_wires)):
            return candidate
    raise KiCadSchematicError(f"blocked pin exit at {pin.position.as_mm()}")


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
    # A collinear degree-two join is not a corner or a junction. Leaving
    # artificial stub ends in the route makes later nets falsely reserve
    # crossing clearance around invisible points in a straight conductor.
    for point in list(neighbours):
        adjacent = neighbours[point]
        if point in terminals or len(adjacent) != 2:
            continue
        a, b = adjacent
        if not (a[0] == point[0] == b[0] or a[1] == point[1] == b[1]):
            continue
        edges.discard(tuple(sorted((a, point))))
        edges.discard(tuple(sorted((b, point))))
        edges.add(tuple(sorted((a, b))))
        neighbours[a].remove(point)
        neighbours[b].remove(point)
        neighbours[a].add(b)
        neighbours[b].add(a)
        neighbours[point].clear()
    return edges


def stub_endpoint(endpoint: PlacedEndpoint) -> PlacedEndpoint:
    step = (endpoint.escape_length if endpoint.escape_length is not None
            else round(PIN_STUB_MM * 1_000_000))
    delta = {
        "left": (-step, 0),
        "right": (step, 0),
        "top": (0, -step),
        "bottom": (0, step),
        None: (0, 0),
    }[endpoint.side]
    return PlacedEndpoint(
        Vector2(endpoint.position.x + delta[0], endpoint.position.y + delta[1]),
        endpoint.side,
        endpoint.owner,
        endpoint.group,
    )


def _connect_pair(
    segments: set[tuple[tuple[int, int], tuple[int, int]]],
    first: PlacedEndpoint,
    second: PlacedEndpoint,
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
