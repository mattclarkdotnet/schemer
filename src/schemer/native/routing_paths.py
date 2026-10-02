from __future__ import annotations

from bisect import bisect_left
from heapq import heappop, heappush

from schemer.kicad.geometry.envelopes import Envelope, envelope_from_points, point_distance
from schemer.kicad.items import SchematicLine, Vector2
from schemer.native.routing_model import CROSSING_ENDPOINT_CLEARANCE, PARALLEL_WIRE_CLEARANCE


def segment_hits_box(a: Vector2, b: Vector2, box: Envelope) -> bool:
    return (min(a.x, b.x) <= box.max_x and max(a.x, b.x) >= box.min_x
            and min(a.y, b.y) <= box.max_y and max(a.y, b.y) >= box.min_y)


def clear_orthogonal_path(
    a: Vector2, b: Vector2, obstacles: list[Envelope],
    foreign_wires: list[SchematicLine],
    *, allow_detours: bool = False,
) -> list[Vector2] | None:
    """Try straight, elbow and outside-lane routes, with bends before length."""

    candidates = [[a, b]] if a.x == b.x or a.y == b.y else []
    candidates.extend(([a, Vector2(a.x, b.y), b], [a, Vector2(b.x, a.y), b]))
    gap = 635_000
    xs = {box.min_x - gap for box in obstacles} | {box.max_x + gap for box in obstacles}
    ys = {box.min_y - gap for box in obstacles} | {box.max_y + gap for box in obstacles}
    for wire in foreign_wires:
        for p in (wire.start, wire.end):
            xs.update((p.x - CROSSING_ENDPOINT_CLEARANCE, p.x + CROSSING_ENDPOINT_CLEARANCE))
            ys.update((p.y - CROSSING_ENDPOINT_CLEARANCE, p.y + CROSSING_ENDPOINT_CLEARANCE))
    candidates.extend([a, Vector2(x, a.y), Vector2(x, b.y), b] for x in sorted(xs))
    candidates.extend([a, Vector2(a.x, y), Vector2(b.x, y), b] for y in sorted(ys))
    clear = []
    for points in candidates:
        path = [points[0]]
        for point in points[1:]:
            if point != path[-1]:
                path.append(point)
        if all(not segment_hits_box(start, end, box)
               for start, end in zip(path, path[1:]) for box in obstacles) and all(
                   not wire_contact(start, end, wire)
                   for start, end in zip(path, path[1:]) for wire in foreign_wires):
            clear.append(path)
    if clear:
        return min(clear, key=path_cost)
    if allow_detours:
        return _orthogonal_visibility_path(a, b, obstacles, foreign_wires, xs, ys)
    return None


def _orthogonal_visibility_path(
    a: Vector2, b: Vector2, obstacles: list[Envelope],
    foreign_wires: list[SchematicLine], xs: set[int], ys: set[int],
) -> list[Vector2] | None:
    """Search clear rectilinear lanes when a local route needs more than two bends.

    The normal straight/elbow search remains first. This bounded fallback is
    used for actual routes, not thousands of speculative annotation previews.
    It uses the same body and painted-stroke clearance predicates.
    """
    columns, rows = sorted(xs | {a.x, b.x}), sorted(ys | {a.y, b.y})
    start = (bisect_left(columns, a.x), bisect_left(rows, a.y), -1)
    goal = (bisect_left(columns, b.x), bisect_left(rows, b.y))
    best = {start: (0, 0)}
    parent = {}
    queue = [(0, point_distance(a, b), 0, start)]
    edges = {}
    visited = 0

    def contact(p: Vector2, q: Vector2, wire: SchematicLine) -> bool:
        if (p.y == q.y) == (wire.start.y == wire.end.y):
            return wire_contact(p, q, wire)
        if not segment_hits_box(p, q, envelope_from_points((wire.start, wire.end))):
            return False
        crossing = Vector2(wire.start.x, p.y) if p.y == q.y else Vector2(p.x, wire.start.y)
        # Grid vertices are not route corners. A straight path may cross at
        # one; preserve clearance from physical endpoints, and prohibit turns
        # near conductors separately below.
        return any(point_distance(crossing, end) < CROSSING_ENDPOINT_CLEARANCE
                   for end in (a, b, wire.start, wire.end))

    while queue and visited < 50_000:
        bends, _, length, current = heappop(queue)
        if best.get(current) != (bends, length):
            continue
        visited += 1
        ix, iy, direction = current
        if (ix, iy) == goal:
            path = []
            node = current
            while True:
                path.append(Vector2(columns[node[0]], rows[node[1]]))
                if node == start:
                    break
                node = parent[node]
            path.reverse()
            compact = []
            for point in path:
                if len(compact) > 1 and (compact[-2].x == compact[-1].x == point.x
                                        or compact[-2].y == compact[-1].y == point.y):
                    compact[-1] = point
                else:
                    compact.append(point)
            return compact
        p = Vector2(columns[ix], rows[iy])
        for nx, ny, axis in ((ix-1, iy, 0), (ix+1, iy, 0), (ix, iy-1, 1), (ix, iy+1, 1)):
            if not (0 <= nx < len(columns) and 0 <= ny < len(rows)):
                continue
            if direction != -1 and direction != axis and any(
                    max(min(w.start.x, w.end.x) - p.x, 0, p.x - max(w.start.x, w.end.x))
                    + max(min(w.start.y, w.end.y) - p.y, 0, p.y - max(w.start.y, w.end.y))
                    < CROSSING_ENDPOINT_CLEARANCE for w in foreign_wires):
                continue
            q = Vector2(columns[nx], rows[ny])
            edge = tuple(sorted(((ix, iy), (nx, ny))))
            if edge not in edges:
                edges[edge] = (all(not segment_hits_box(p, q, box) for box in obstacles)
                               and all(not contact(p, q, wire) for wire in foreign_wires))
            if not edges[edge]:
                continue
            cost = (bends + (direction != -1 and direction != axis), length + point_distance(p, q))
            nxt = (nx, ny, axis)
            if cost < best.get(nxt, (float("inf"), float("inf"))):
                best[nxt], parent[nxt] = cost, current
                heappush(queue, (cost[0], cost[1] + point_distance(q, b), cost[1], nxt))
    return None


def path_cost(path: list[Vector2]) -> tuple[int, int]:
    return (len(path) - 2,
            sum(abs(a.x - b.x) + abs(a.y - b.y) for a, b in zip(path, path[1:])))


def wire_contact(a: Vector2, b: Vector2, wire: SchematicLine) -> bool:
    """Allow interior crossings clear of junctions, corners and terminals."""

    c, d = wire.start, wire.end
    horizontal = a.y == b.y
    other_horizontal = c.y == d.y
    if horizontal == other_horizontal:
        # Reserve a readable routing lane, not merely non-touching strokes.
        along = (max(min(a.x, b.x), min(c.x, d.x)) <= min(max(a.x, b.x), max(c.x, d.x))
                 if horizontal else
                 max(min(a.y, b.y), min(c.y, d.y)) <= min(max(a.y, b.y), max(c.y, d.y)))
        return along and abs(a.y - c.y if horizontal else a.x - c.x) < PARALLEL_WIRE_CLEARANCE
    if not segment_hits_box(a, b, envelope_from_points((c, d))):
        return False
    point = Vector2(c.x, a.y) if horizontal else Vector2(a.x, c.y)
    return any(abs(point.x - p.x) + abs(point.y - p.y) < CROSSING_ENDPOINT_CLEARANCE
               for p in (a, b, c, d))
