from __future__ import annotations

from schemer.kicad.geometry.envelopes import Envelope
from schemer.native.annotations.corridors import clear_signal_stub_corridors
from schemer.native.annotations.label_fitting import (
    align_close_global_label_tips,
    fit_signal_labels_on_pin_exits,
)
from schemer.native.annotations.rails import (
    localize_rail_symbols,
    merge_touching_rail_symbols,
    separate_same_face_rail_corridors,
)
from schemer.native.annotations.signal_topology import localize_signal_labels
from schemer.native.model import NetSymbolTarget
from schemer.native.routing_model import PlacedEndpoint


def localize_net_targets(
    templates: list[NetSymbolTarget],
    endpoints: dict[str, list[PlacedEndpoint]],
    glyph_bounds: dict[bool, Envelope],
    annotation_obstacles: dict[str, list[Envelope]] | None = None,
    body_obstacles: dict[str, list[Envelope]] | None = None,
    *, direct_groups: frozenset[str] = frozenset(),
) -> list[NetSymbolTarget]:
    targets = localize_signal_labels(templates, endpoints, direct_groups=direct_groups)
    targets = localize_rail_symbols(targets, endpoints)
    targets = separate_same_face_rail_corridors(targets, endpoints)
    targets = fit_signal_labels_on_pin_exits(
        targets, endpoints, glyph_bounds, annotation_obstacles, body_obstacles,
    )
    targets = clear_signal_stub_corridors(targets, endpoints, glyph_bounds, body_obstacles,
                                          annotation_obstacles)
    targets = align_close_global_label_tips(targets, annotation_obstacles, body_obstacles)
    return merge_touching_rail_symbols(targets, glyph_bounds)
