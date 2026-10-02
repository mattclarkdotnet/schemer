from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
    point_distance,
)
from schemer.kicad.geometry.text import label_envelope
from schemer.kicad.items import (
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.annotations.signal_topology import inline_label_span, nearest_local_target
from schemer.native.annotations.symbols import label_approach_side
from schemer.native.contacts import wires_intersect
from schemer.native.model import NetSymbolTarget
from schemer.native.routing import (
    pin_contact_obstacle,
    preview_clear_route,
    route_group,
    stub_endpoint,
)
from schemer.native.routing_model import PARALLEL_WIRE_CLEARANCE, PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box


def align_close_global_label_tips(
    targets: list[NetSymbolTarget],
    annotation_obstacles: dict[str, list[Envelope]] | None = None,
    body_obstacles: dict[str, list[Envelope]] | None = None,
) -> list[NetSymbolTarget]:
    """Use one tip column for dense labels on a device face, without bending exits.

    Pin rows are fixed; movable branch labels instead get breathing room when
    localized. Never align unrelated blocks, opposite faces, or rotated text.
    """

    def bounds(target: NetSymbolTarget) -> Envelope:
        return envelope_from_points(label_envelope(Text(
            target.display_name, target.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0,
                           target.text_alignment, "center"),
        )))

    faces: dict[tuple[str | None, str, str, str], list[int]] = defaultdict(list)
    for index, target in enumerate(targets):
        if target.rail or not target.required or target.rotation or len(target.members) != 1:
            continue
        pin = target.members[0]
        if (pin.owner and pin.side in {"left", "right"}
                and pin.position.y == target.position.y):
            faces[(target.group, pin.owner, pin.side, target.text_alignment)].append(index)
    result = list(targets)
    for (group, _, side, _), indices in faces.items():
        rows: list[list[int]] = []
        for index in sorted(indices, key=lambda i: targets[i].position.y):
            if not rows or (targets[index].position.y
                            - targets[rows[-1][-1]].position.y > 3_810_000):
                rows.append([])
            rows[-1].append(index)
        for row in rows:
            if len(row) < 2:
                continue
            x = (min if side == "left" else max)(result[i].position.x for i in row)
            candidates = [replace(result[i], position=Vector2(x, result[i].position.y))
                          for i in row]
            if any(not envelopes_do_not_overlap(bounds(a), bounds(b), 250_000)
                   for i, a in enumerate(candidates) for b in candidates[i + 1:]):
                continue
            obstacles = [*(annotation_obstacles or {}).get(group, []),
                         *(body_obstacles or {}).get(group, [])]
            obstacles.extend(bounds(t) for i, t in enumerate(result)
                             if i not in row and not t.rail and t.group == group)
            # Alignment is subordinate to clearance: do not stretch a stub
            # across another net or push its label into a symbol or caption.
            foreign_stubs = [(p.position, t.position) for i, t in enumerate(result)
                             if i not in row and t.group == group for p in t.members]
            if any(not envelopes_do_not_overlap(bounds(t), box, 635_000)
                   or segment_hits_box(t.members[0].position, t.position, box)
                   for t in candidates for box in obstacles):
                continue
            if any(segment_hits_box(a, b, bounds(t)) or wires_intersect(
                SchematicLine(id="", start=t.members[0].position, end=t.position),
                SchematicLine(id="", start=a, end=b),
            ) for t in candidates for a, b in foreign_stubs if a.x == b.x or a.y == b.y):
                continue
            for index, candidate in zip(row, candidates, strict=True):
                result[index] = candidate
    return result


def fit_signal_labels_on_pin_exits(
    targets: list[NetSymbolTarget],
    endpoints: dict[str, list[PlacedEndpoint]],
    glyph_bounds: dict[bool, Envelope],
    annotation_obstacles: dict[str, list[Envelope]] | None = None,
    body_obstacles: dict[str, list[Envelope]] | None = None,
) -> list[NetSymbolTarget]:
    """Let text yield before moving an otherwise clear rail corridor.

    A local label must remain electrically attached and recognizably owned:
    slide it along its own pin exit, retaining a visible wire, or rotate it
    there. Do not move it to an unrelated empty row.
    """

    def caption(target: NetSymbolTarget) -> Envelope:
        return envelope_from_points(label_envelope(Text(
            target.display_name, target.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                           target.rotation, target.text_alignment,
                           "center" if target.required else "bottom"),
        )))

    result, bank_indices = _fit_dense_label_banks(targets, endpoints, body_obstacles or {})
    # On a row of vertical pins, fit the outer label first. Otherwise an
    # earlier flag can cover its neighbour's only straight escape corridor.
    order = sorted(range(len(targets)), key=lambda i: (
        targets[i].group or "", len(targets[i].members) == 1, targets[i].position.y,
        -targets[i].position.x if targets[i].text_alignment == "left" else targets[i].position.x,
    ))
    fitted_indices = set(bank_indices)
    for index in order:
        target = result[index]
        if target.rail or index in bank_indices:
            continue
        pins = list(target.members) or [p for p in endpoints.get(target.net_name, [])
                                       if p.group == target.group]
        inline = inline_label_span(pins)
        if inline is not None and not target.required:
            vertical = inline[0].x == inline[1].x
            axis = "y" if vertical else "x"
            obstacles = [*(annotation_obstacles or {}).get(target.group, []),
                         *(body_obstacles or {}).get(target.group, []),
                         *(caption(t) for i, t in enumerate(result) if i in fitted_indices
                           and not t.rail and t.group == target.group)]
            candidates = [replace(target, position=point, rotation=90 if vertical else 0,
                                  text_alignment=alignment)
                          for point, alignment in zip(inline,
                              ("right", "left") if vertical else ("left", "right"), strict=True)]
            clear = [candidate for candidate in candidates
                     if getattr(caption(candidate), "min_" + axis) >= min(
                         getattr(p.position, axis) for p in pins) + 635_000
                     and getattr(caption(candidate), "max_" + axis) <= max(
                         getattr(p.position, axis) for p in pins) - 635_000
                     and all(envelopes_do_not_overlap(caption(candidate), box, 250_000)
                             for box in obstacles)]
            if clear:
                result[index] = clear[0]
                fitted_indices.add(index)
                continue
        if not pins:
            continue
        pin = min(pins, key=lambda p: point_distance(p.position, target.position))
        if pin.side is None:
            continue
        foreign_wires = []
        for name, items in endpoints.items():
            if name == target.net_name:
                continue
            local = [p for p in items if p.group == target.group]
            terminals = [t for t in result if t.net_name == name and t.group == target.group]
            routes = [[*[p for p in local if nearest_local_target(p, terminals) == i],
                       PlacedEndpoint(t.position)] for i, t in enumerate(terminals)]
            for route in routes or [local]:
                foreign_wires.extend(w for w in route_group(route, topology_only=True)
                                     if isinstance(w, SchematicLine))
        wire_boxes = [envelope_from_points((w.start, w.end)) for w in foreign_wires]
        # Only already fitted labels are obstacles. A later label's provisional
        # position must not force this wire to grow; that label gets its turn
        # to move around the completed placement.
        obstacles = [caption(t) for i, t in enumerate(result)
                     if i in fitted_indices and not t.rail and t.group == target.group]
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
                       and alternatives[nearest_local_target(p, alternatives)] is rail]
            ys = [rail.position.y, *(p.position.y for p in members)]
            obstacles.append(Envelope(rail.position.x - 150_000, min(ys),
                                       rail.position.x + 150_000, max(ys)))
            if rail.ground in glyph_bounds:
                obstacles.append(glyph_bounds[rail.ground].translated(rail.position))
        rails_end = len(obstacles)
        for name, members in endpoints.items():
            if name == target.net_name:
                continue
            for p in members:
                if p.group == target.group:
                    stub = stub_endpoint(p).position
                    obstacles.append(Envelope(
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
        fixed = [*(body_obstacles or {}).get(target.group, []), *obstacles[rails_end:]]
        through_wires = preview_clear_route(pins, fixed)
        if len(pins) > 1 and target.required:
            # A flag can branch directly off the completed circuit backbone.
            # Offer its existing junctions/segments before a nearby free-space
            # caption pulls that backbone back toward an IC's inward stroke.
            for wire in through_wires:
                for point in (wire.start, wire.end, Vector2(
                    (wire.start.x + wire.end.x) // 2,
                    (wire.start.y + wire.end.y) // 2)):
                    for alignment in ("left", "right"):
                        candidates.append(replace(target, position=point, rotation=0,
                                                  text_alignment=alignment))
        if len(pins) > 1 and not target.required:
            # Plain labels can slide along an existing connection. Offer
            # those positions before creating a branch for the caption;
            # the name must not choose a new row for the electrical trunk.
            for wire in through_wires:
                horizontal = wire.start.y == wire.end.y
                for point in (wire.start, wire.end, Vector2(
                    (wire.start.x + wire.end.x) // 2,
                    (wire.start.y + wire.end.y) // 2)):
                    for alignment in ("left", "right"):
                        candidates.append(replace(target, position=point,
                            rotation=0 if horizontal else 90, text_alignment=alignment))
        alignments = [target.text_alignment]
        if dy:
            # A vertical pin exit has no preferred text side. Change text
            # justification before asking an adjacent rail to bend around it.
            alignments.append("right" if target.text_alignment == "left" else "left")
        for rotation in (0, 90):
            for length in [*range(max(span, 2_540_000), 634_999, -635_000), 254_000]:
                for alignment in alignments:
                    # Dense-bank row allocation is provisional. Always offer
                    # the real pin row too; otherwise every subsequent search
                    # preserves an avoidable bend introduced by that estimate.
                    rows = {pin.position.y + dy * length}
                    if dx and len(pins) == 1:
                        rows.add(target.position.y)
                    for y in sorted(rows):
                        candidates.append(replace(
                            target, rotation=rotation, text_alignment=alignment,
                            position=Vector2(pin.position.x + dx * length, y),
                        ))
        # A longer straight stub can make room for neighbouring captions.
        # Derive that length from the obstacle edge, not a larger fixed gap.
        for candidate in candidates[:]:
            box = caption(candidate)
            for obstacle in [*obstacles, *wire_boxes,
                             *(body_obstacles or {}).get(target.group, [])]:
                if envelopes_do_not_overlap(box, obstacle, 635_000):
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
                anchor_distance = (
                    obstacle.max_x + 635_000 - candidate.position.x if dx == 1 else
                    candidate.position.x - obstacle.min_x + 635_000 if dx == -1 else
                    obstacle.max_y + 635_000 - candidate.position.y if dy == 1 else
                    candidate.position.y - obstacle.min_y + 635_000
                )
                candidates.append(replace(candidate, position=Vector2(
                    candidate.position.x + dx * max(distance, anchor_distance),
                    candidate.position.y + dy * max(distance, anchor_distance))))
        if len(pins) > 1:
            # A compound node is not an exclusive pin attachment. When its
            # through-wire cannot hold the caption, give the name a short
            # orthogonal branch from that node instead of forcing it through
            # the support component at the other end of the wire.
            center = Vector2(round(sum(p.position.x for p in pins) / len(pins)),
                             round(sum(p.position.y for p in pins) / len(pins)))
            for axis_x, axis_y in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                for step in range(2, 17, 2):
                    for alignment in ("left", "right"):
                        candidates.append(replace(target, rotation=0, text_alignment=alignment,
                            position=Vector2(center.x + axis_x * step * 1_270_000,
                                             center.y + axis_y * step * 1_270_000)))
        fitted = []
        deferred = []
        allow_detours = False
        constrained_exit = any(wires_intersect(
            SchematicLine(id="", start=pin.position, end=target.position), wire,
        ) for wire in foreign_wires)

        def candidate_positions():
            nonlocal allow_detours
            yield from candidates
            # A fixed-row search cannot clear an already occupied port row.
            # Only after straight exits fail, move the label into a nearby
            # row, keeping it on the same face and leaving the symbol fixed.
            if not fitted and len(pins) == 1:
                for along in (2_540_000, 5_080_000, 10_160_000, 20_320_000, 30_480_000):
                    for across in (3_810_000, 7_620_000, 11_430_000, 15_240_000):
                        for sign in (-1, 1):
                            yield replace(target, rotation=0, position=Vector2(
                                pin.position.x + dx * along + dy * sign * across,
                                pin.position.y + dy * along + dx * sign * across,
                            ))
            # Failure of the cheap two-bend probe does not establish that a
            # geometrically clear label is unroutable. Search those survivors
            # only after ordinary fitting fails, never every speculative slot.
            if not fitted:
                allow_detours = True
                yield from deferred

        for candidate in candidate_positions():
            box = caption(candidate)
            anchor_box = Envelope(candidate.position.x - 250_000, candidate.position.y - 250_000,
                                   candidate.position.x + 250_000, candidate.position.y + 250_000)
            if any(p.stroke and not envelopes_do_not_overlap(anchor_box, p.stroke, 0)
                   for p in pins):
                continue
            if any(not envelopes_do_not_overlap(box, p.stroke or Envelope(
                p.position.x - 500_000, p.position.y - 500_000,
                p.position.x + 500_000, p.position.y + 500_000,
            ), 0) for p in pins):
                continue
            hard = obstacles[:rails_start]
            if body_obstacles is not None:
                # Component fields are moved after labels. They may yield to
                # a short clear exit instead of forcing it across a wire.
                hard = [*obstacles[:captions_start], *body_obstacles.get(target.group, [])]
            if not all(envelopes_do_not_overlap(box, obstacle, 635_000) for obstacle in hard):
                continue
            if not all(envelopes_do_not_overlap(anchor_box, obstacle, 0)
                       for obstacle in [*hard, *obstacles[rails_end:]]):
                continue
            # Real pin exits are fixed; a speculative whole-net route is not.
            # Prefer clearing that preview, but let the actual router use the
            # chosen label envelope instead of making an imaginary wire a
            # hard barrier that can leave a label inside a component.
            if not all(envelopes_do_not_overlap(box, obstacle, 250_000)
                       for obstacle in obstacles[rails_end:]):
                continue
            own_route = preview_clear_route([*pins, PlacedEndpoint(candidate.position,
                label_approach_side(candidate) if candidate.required else None,
                escape_length=635_000 if candidate.required else None)], fixed,
                allow_detours=allow_detours)
            if not own_route:
                if not allow_detours:
                    deferred.append(candidate)
                continue
            if any(segment_hits_box(w.start, w.end, obstacle)
                   for w in own_route if isinstance(w, SchematicLine)
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
            crossings = sum(wires_intersect(w, wire) for w in own_route
                            if isinstance(w, SchematicLine) for wire in foreign_wires)
            caption_conflicts = sum(not envelopes_do_not_overlap(box, obstacle, 635_000)
                                    for obstacle in obstacles[captions_start:captions_end])
            preview_conflicts = sum(segment_hits_box(w.start, w.end, box) for w in foreign_wires)
            leaves_through_wire = (len(pins) > 1 and not any(
                segment_hits_box(w.start, w.end, Envelope(
                    candidate.position.x, candidate.position.y,
                    candidate.position.x, candidate.position.y))
                or (target.required and w.start.y == w.end.y == candidate.position.y)
                for w in through_wires))
            distance = point_distance(pin.position, candidate.position)
            length_cost = (max(2_540_000, distance) + 2_540_000 * (candidate.rotation != 0)
                           if len(pins) == 1 else distance if constrained_exit else 0)
            if len(pins) == 1:
                # Hypothetical foreign routes must not lengthen an otherwise
                # clear terminal. The router will respect the fitted label;
                # only fixed bodies and real pin exits constrained it above.
                off_axis = ((candidate.position.y != pin.position.y) if dx
                            else (candidate.position.x != pin.position.x))
                score = ((off_axis, length_cost), preview_conflicts, overlap, crossings,
                         distance < 1_270_000, candidate.rotation != 0, caption_conflicts, 0)
            else:
                score = (preview_conflicts, crossings, overlap, leaves_through_wire,
                         caption_conflicts, distance < 1_270_000, length_cost,
                         candidate.rotation != 0)
            fitted.append((*score, candidate))
        if fitted:
            result[index] = min(fitted, key=lambda item: item[:8])[8]
        fitted_indices.add(index)
    return result


def _fit_dense_label_banks(
    targets: list[NetSymbolTarget], endpoints: dict[str, list[PlacedEndpoint]],
    body_obstacles: dict[str, list[Envelope]],
) -> tuple[list[NetSymbolTarget], set[int]]:
    """Allocate a crowded device face together, before fitting lone labels.

    A greedy label can occupy the only exit of the next pin. Keep the native
    label sizes, pin order and a common tip column; fan out only when painted
    bounds cannot fit on the original rows. Leave room for distinct turn lanes.
    """
    def bounds(t):
        return envelope_from_points(label_envelope(Text(t.display_name, t.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0, t.text_alignment,
                           "center" if t.required else "bottom"))))

    faces: dict[tuple[str | None, str, str], list[int]] = defaultdict(list)
    for i, t in enumerate(targets):
        if not t.rail and len(t.members) == 1:
            p = t.members[0]
            if p.owner and p.side in {"left", "right"}:
                faces[t.group, p.owner, p.side].append(i)
    result, fitted = list(targets), set()
    for (group, _, side), indices in faces.items():
        indices.sort(key=lambda i: targets[i].members[0].position.y)
        if len(indices) < 2 or not any(targets[i].required for i in indices):
            continue
        on_rows = [replace(targets[i], rotation=0,
                           position=targets[i].members[0].position) for i in indices]
        if all(envelopes_do_not_overlap(bounds(a), bounds(b), 635_000)
               for a, b in zip(on_rows, on_rows[1:])):
            continue
        # Ordered row expansion cannot permute channels or introduce crossings.
        ys = [t.position.y for t in on_rows]
        allocated = [ys[0]]
        local = [bounds(replace(t, position=Vector2(0, 0))) for t in on_rows]
        for offset in range(1, len(indices)):
            allocated.append(max(ys[offset], allocated[-1] + local[offset-1].max_y
                                 - local[offset].min_y + 635_000))
        shift = round(sum(a - b for a, b in zip(ys, allocated)) / len(ys))
        allocated = [y + shift for y in allocated]
        sign = -1 if side == "left" else 1
        pin_x = (min if sign < 0 else max)(t.position.x for t in on_rows)
        # One lane per displaced row, plus the real pin and label approaches.
        span = 2_540_000 + 635_000 + PARALLEL_WIRE_CLEARANCE * sum(
            a != b for a, b in zip(ys, allocated))
        fixed = list(body_obstacles.get(group, []))
        fixed.extend(bounds(result[i]) for i in fitted if result[i].group == group)
        bank_nets = {targets[i].net_name for i in indices}
        fixed.extend(pin_contact_obstacle(p) for name, pins in endpoints.items()
                     if name not in bank_nets for p in pins if p.group == group)
        candidate = []
        for _ in range(len(fixed) + 2):
            x = pin_x + sign * span
            candidate = [replace(targets[i], rotation=0,
                                 text_alignment="right" if sign < 0 else "left",
                                 position=Vector2(x, y)) for i, y in zip(indices, allocated)]
            hits = [(bounds(t), b) for t in candidate for b in fixed
                    if not envelopes_do_not_overlap(bounds(t), b, 635_000)]
            if not hits:
                break
            span += max((a.max_x - b.min_x if sign < 0 else b.max_x - a.min_x)
                        + 635_000 for a, b in hits)
        else:
            continue
        if any(not envelopes_do_not_overlap(bounds(t), b, 635_000)
               for t in candidate for b in fixed):
            continue
        for i, t in zip(indices, candidate):
            result[i] = t
            fitted.add(i)
    return result, fitted
