from __future__ import annotations

from dataclasses import replace

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
)
from schemer.kicad.geometry.text import label_envelope
from schemer.kicad.items import (
    BaseLabel,
    GlobalLabel,
    Junction,
    SchematicLine,
    Vector2,
)
from schemer.native.contacts import wire_hits_label
from schemer.native.routing import route_group
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box


def route_with_label_backtracking(
    endpoints: list[PlacedEndpoint], label: BaseLabel | None,
    obstacles: list[Envelope], foreign_wires: list[SchematicLine], net_name: str,
    caption_obstacles: list[Envelope],
) -> tuple[list[SchematicLine | Junction], BaseLabel | None]:
    """Backtrack a movable label when final routing disproves its draft fit.

    Components, net membership and other completed routes stay fixed. Never
    accept a disconnected flag merely because its earlier preview fitted.
    """
    def route_obstacles(candidate: BaseLabel | None) -> list[Envelope]:
        if not isinstance(candidate, GlobalLabel):
            return obstacles
        box = envelope_from_points(label_envelope(candidate.text))
        p = candidate.position
        # The connection tip is the sole legal entry into a flag graphic.
        # Clip its outward edge just behind that anchor, as for rail glyphs.
        side = endpoints[-1].side
        box = Envelope(p.x + 1 if side == "left" else box.min_x,
                        p.y + 1 if side == "top" else box.min_y,
                        p.x - 1 if side == "right" else box.max_x,
                        p.y - 1 if side == "bottom" else box.max_y)
        return [*obstacles, box]

    def try_label(candidate: BaseLabel | None) -> list[SchematicLine | Junction] | None:
        branch = (endpoints if candidate is None else
                  [*endpoints[:-1], replace(endpoints[-1], position=candidate.position)])
        try:
            route = route_group(branch, obstacles=route_obstacles(candidate),
                                foreign_wires=foreign_wires, net_name=net_name,
                                report_failure=False, allow_detours=True)
        except KiCadSchematicError:
            return None
        if candidate is not None and any(wire_hits_label(w, candidate) for w in route
                                         if isinstance(w, SchematicLine)):
            return None
        return route

    if (route := try_label(label)) is not None:
        return route, None
    if label is not None:
        offsets = sorted((Vector2(dx * 1_270_000, dy * 1_270_000)
                          for dx in range(-6, 7) for dy in range(-6, 7)
                          if dx or dy), key=lambda p: (abs(p.x) + abs(p.y), abs(p.y), p.x))
        for offset in offsets:
            point = Vector2(label.position.x + offset.x, label.position.y + offset.y)
            text = replace(label.text, position=point)
            box = envelope_from_points(label_envelope(text))
            if (any(not envelopes_do_not_overlap(box, b, 250_000)
                    for b in [*obstacles, *caption_obstacles])
                    or any(segment_hits_box(w.start, w.end, box) for w in foreign_wires)):
                continue
            candidate = replace(label, position=point, text=text)
            if (route := try_label(candidate)) is not None:
                return route, candidate
    # Preserve the ordinary strict/draft failure contract after bounded
    # backtracking is exhausted; no route-through-body fallback is allowed.
    return route_group(endpoints, obstacles=route_obstacles(label), foreign_wires=foreign_wires,
                       net_name=net_name, allow_detours=True), None
