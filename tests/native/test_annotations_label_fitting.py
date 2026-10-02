from __future__ import annotations

import pytest

from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
)
from schemer.kicad.geometry.text import label_envelope, text_envelope
from schemer.kicad.items import (
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.annotations.label_fitting import (
    _fit_dense_label_banks,
    align_close_global_label_tips,
    fit_signal_labels_on_pin_exits,
)
from schemer.native.annotations.signal_topology import (
    localize_signal_labels,
)
from schemer.native.model import NetSymbolTarget
from schemer.native.routing import (
    pin_contact_obstacle,
    preview_clear_route,
    route_group,
)
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box


@pytest.mark.parametrize("quick_probe_succeeds", [False, True])
def test_label_fit_only_uses_deep_search_when_quick_candidates_fail(
    monkeypatch, quick_probe_succeeds,
):
    import schemer.native.annotations.label_fitting as layout

    original = preview_clear_route
    attempts = []

    def probe(pins, obstacles, *, allow_detours=False):
        attempts.append(allow_detours)
        if not quick_probe_succeeds and not allow_detours:
            return []
        return original(pins, obstacles, allow_detours=allow_detours)

    monkeypatch.setattr(layout, "preview_clear_route", probe)
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, 10), side, group="block")
            for x, side in ((10, "right"), (30, "left"))]
    target = NetSymbolTarget("LINK", "LINK", Vector2.from_xy_mm(5, 10), 0,
                              False, False, group="block", members=tuple(pins), required=True)
    result, = fit_signal_labels_on_pin_exits(
        [target], {"LINK": pins}, {}, {},
        {"block": [Envelope(0, 8_000_000, 6_000_000, 12_000_000)]},
    )
    assert result.position != target.position
    assert any(attempts) == (not quick_probe_succeeds)


def test_label_fit_includes_neighbouring_single_pin_wire_exits():
    pins = {name: [PlacedEndpoint(Vector2.from_xy_mm(20, y), "right", "IC", "block")]
            for name, y in (("FIRST", 10), ("SECOND", 12.54), ("CLOCK", 15.08))}
    targets = [NetSymbolTarget(name, name, Vector2.from_xy_mm(x, pins[name][0].position.y / 1e6),
                                0, False, False, "left", group="block", members=tuple(pins[name]))
               for name, x in (("FIRST", 25.08), ("SECOND", 35.24), ("CLOCK", 25.08))]
    fitted = fit_signal_labels_on_pin_exits(targets, pins, {})
    assert all(label.rotation == 0 for label in fitted)
    for label in fitted:
        box = envelope_from_points(label_envelope(Text(
            label.display_name, label.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27), label.rotation,
                           label.text_alignment, "bottom"),
        )))
        assert all(not segment_hits_box(pins[other.net_name][0].position, other.position, box)
                   for other in fitted if other.net_name != label.net_name)


@pytest.mark.parametrize("side,dx,dy", [("left", -1, 0), ("right", 1, 0),
                                      ("top", 0, -1), ("bottom", 0, 1)])
def test_label_can_extend_its_own_stub_to_clear_a_component_caption(side, dx, dy):
    pin = PlacedEndpoint(Vector2(0, 0), side, group="IC")
    label = NetSymbolTarget(
        "DATA", "DATA", Vector2(dx * 2_540_000, dy * 2_540_000),
        0, False, False, text_alignment="right" if dx < 0 else "left", group="IC",
    )
    obstruction = Envelope(-5_000_000, -5_000_000, 5_000_000, 5_000_000)
    fitted = fit_signal_labels_on_pin_exits(
        [label], {"DATA": [pin]}, {}, {"IC": [obstruction]},
    )[0]
    assert fitted.position.x * dy == fitted.position.y * dx
    assert dx * fitted.position.x + dy * fitted.position.y > 2_540_000
    assert fitted.net_name == label.net_name
    assert fitted.rotation == 0


def test_label_ignores_captions_in_a_different_block():
    label = NetSymbolTarget("DATA", "DATA", Vector2(2_540_000, 0),
                             0, False, False, text_alignment="left", group="IC")
    endpoints = {"DATA": [PlacedEndpoint(Vector2(0, 0), "right", group="IC")]}
    assert fit_signal_labels_on_pin_exits(
        [label], endpoints, {}, {"OTHER": [Envelope(0, -5_000_000, 20_000_000, 5_000_000)]},
    ) == [label]


def test_label_on_an_inline_local_connection_stays_between_its_pins():
    target = NetSymbolTarget("BIAS", "BIAS", Vector2(0, 0), 0, False, False)
    pins = [PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="IC"),
            PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left", group="IC"),
            PlacedEndpoint(Vector2.from_xy_mm(50, 20), "left", group="PORT")]
    labels = localize_signal_labels([target], {"BIAS": pins})
    local = next(t for t in labels if t.group == "IC")
    assert local.position == Vector2.from_xy_mm(12.54, 20)
    assert local.text_alignment == "left"
    fitted = fit_signal_labels_on_pin_exits(
        labels, {"BIAS": pins}, {},
        {"IC": [Envelope(10_000_000, 18_000_000, 17_000_000, 21_000_000)]},
    )
    local = next(t for t in fitted if t.group == "IC")
    assert local.position == Vector2.from_xy_mm(27.46, 20)
    assert local.text_alignment == "right"


def test_boxed_label_at_parallel_branches_stays_on_their_shared_row():
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, 10), "bottom", owner, "circuit")
            for x, owner in ((20, "clamp"), (30, "bypass"))]
    template = NetSymbolTarget("CONTROL", "CONTROL", Vector2(0, 0), 0, False, False,
                                required=True)
    target, = localize_signal_labels([template], {"CONTROL": pins})
    fitted, = fit_signal_labels_on_pin_exits([target], {"CONTROL": pins}, {}, {}, {})
    assert fitted.position == Vector2.from_xy_mm(17.46, 12.54)
    assert fitted.text_alignment == "right"
    wires = [w for w in route_group([*pins, PlacedEndpoint(fitted.position)])
             if isinstance(w, SchematicLine)]
    assert {w.start.y for w in wires if w.start.y == w.end.y} == {12_540_000}


def test_plain_compound_label_uses_the_existing_trunk_instead_of_pulling_it_north():
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, y), side, group="path")
            for x, y, side in ((20, 10, "right"), (35, 10, "left"),
                              (40, 25, "top"), (60, 25, "top"))]
    target = NetSymbolTarget("BIAS", "BIAS", Vector2.from_xy_mm(65, 10),
                             0, False, False, group="path", members=tuple(pins))
    before = [w for w in route_group(pins) if isinstance(w, SchematicLine)]
    fitted, = fit_signal_labels_on_pin_exits([target], {"BIAS": pins}, {}, {}, {})
    assert any(min(w.start.x, w.end.x) <= fitted.position.x <= max(w.start.x, w.end.x)
               and min(w.start.y, w.end.y) <= fitted.position.y <= max(w.start.y, w.end.y)
               for w in before)
    after = [w for w in route_group([*pins, PlacedEndpoint(fitted.position)])
             if isinstance(w, SchematicLine)]
    # No new wire row is introduced just to carry the caption.
    assert {w.start.y for w in after if w.start.y == w.end.y} <= {
        w.start.y for w in before if w.start.y == w.end.y}


def test_vertical_inline_labels_rotate_between_their_pins_without_side_branches():
    nets = {name: [PlacedEndpoint(Vector2.from_xy_mm(x, y), side, group="owner")
                   for y, side in ((10, "bottom"), (50, "top"))]
            for name, x in (("OUTPUT_LEFT", 10), ("OUTPUT_RIGHT", 15.08))}
    # Required interface with another circuit, while both local attachments
    # remain connected directly. Only the local cluster is needed here.
    localized = [
        NetSymbolTarget(name, name, Vector2(pins[0].position.x, 12_540_000),
                         90, False, False, text_alignment="right", group="owner",
                         members=tuple(pins))
        for name, pins in nets.items()
    ]
    fitted = fit_signal_labels_on_pin_exits(localized, nets, {})
    for t in fitted:
        assert t.rotation == 90
        box = envelope_from_points(label_envelope(Text(
            t.display_name, t.position, TextAttributes(
                Vector2.from_xy_mm(1.27, 1.27), t.rotation, t.text_alignment, "bottom"))))
        assert 10_000_000 < box.min_y < box.max_y < 50_000_000
        assert t.position.x == nets[t.net_name][0].position.x
        wires = route_group([*nets[t.net_name], PlacedEndpoint(t.position)])
        assert all(w.start.x == w.end.x for w in wires if isinstance(w, SchematicLine))


@pytest.mark.parametrize("side", ["left", "right"])
def test_dense_global_labels_align_tips_without_bending_pin_exits(side):
    direction = -1 if side == "left" else 1
    labels = [NetSymbolTarget(
        str(i), "SHORT" if i else "LONGER_NAME",
        Vector2.from_xy_mm(direction * x, y), 0, False, False,
        text_alignment="right" if side == "left" else "left", group="block", required=True,
        members=(PlacedEndpoint(Vector2.from_xy_mm(0, y), side, "IC", "block"),),
    ) for i, (x, y) in enumerate(((2.54, 10), (5.08, 13.175), (2.54, 20)))]
    aligned = align_close_global_label_tips(labels)
    assert [t.position.x for t in aligned] == [direction * x for x in
                                             (5_080_000, 5_080_000, 2_540_000)]
    assert [t.position.y for t in aligned] == [t.position.y for t in labels]
    assert [t.members for t in aligned] == [t.members for t in labels]


def test_dense_global_tip_alignment_yields_to_obstacles_and_separate_devices():
    from dataclasses import replace

    labels = [NetSymbolTarget(
        str(i), "SIGNAL", Vector2.from_xy_mm(x, y), 0, False, False,
        group="block", required=True,
        members=(PlacedEndpoint(Vector2.from_xy_mm(0, y), "right", "IC", "block"),),
    ) for i, (x, y) in enumerate(((2.54, 10), (5.08, 12.54)))]
    box = Envelope(4_000_000, 9_500_000, 4_500_000, 10_500_000)
    assert align_close_global_label_tips(labels, body_obstacles={"block": [box]}) == labels
    labels[1] = replace(labels[1], members=(replace(labels[1].members[0], owner="OTHER"),))
    assert align_close_global_label_tips(labels) == labels


def test_pin_row_is_reconsidered_after_provisional_label_fanout():
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "left", "IC", "block")
    target = NetSymbolTarget("SIGNAL", "SIGNAL", Vector2.from_xy_mm(5, 15),
                              0, False, False, text_alignment="right", group="block",
                              required=True, members=(pin,))
    fitted = fit_signal_labels_on_pin_exits([target], {"SIGNAL": [pin]}, {})[0]
    assert fitted.position.y == pin.position.y


def test_straight_label_exit_beats_a_shorter_off_row_caption():
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "IC", "block")
    target = NetSymbolTarget("SIGNAL", "SIGNAL", Vector2.from_xy_mm(15, 15),
                              0, False, False, group="block", members=(pin,))
    # The caption can clear a nearby support part by continuing straight.
    # Its provisional empty row is shorter, but must not win on distance.
    box = Envelope(12_000_000, 7_000_000, 20_000_000, 9_900_000)
    fitted, = fit_signal_labels_on_pin_exits([target], {"SIGNAL": [pin]}, {},
                                              body_obstacles={"block": [box]})
    assert fitted.position.y == pin.position.y
    assert fitted.position.x > box.max_x


def test_boxed_compound_label_uses_the_circuit_backbone_before_a_new_branch():
    pins = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 0), "right", "IC", "block",
                         stroke=Envelope(7_000_000, -250_000, 10_250_000, 250_000)),
        PlacedEndpoint(Vector2.from_xy_mm(15.08, 0), "left", "SERIES", "block"),
        PlacedEndpoint(Vector2.from_xy_mm(5, 10), "right", "C", "block"),
        PlacedEndpoint(Vector2.from_xy_mm(5, 20), "right", "R", "block"),
    ]
    target = NetSymbolTarget("OUT", "OUTPUT", Vector2.from_xy_mm(8, 5),
                              0, False, False, text_alignment="left", group="block",
                              required=True, members=tuple(pins))
    fitted, = fit_signal_labels_on_pin_exits([target], {"OUT": pins}, {},
                                              body_obstacles={"block": []})
    assert fitted.position.x == 12_540_000
    assert 0 < fitted.position.y <= 20_000_000


@pytest.mark.parametrize("side", ["left", "right"])
def test_mixed_plain_and_boxed_port_bank_allocates_all_rows_together(side):
    sign = -1 if side == "left" else 1
    targets = [NetSymbolTarget(
        str(i), f"PORT_{i}", Vector2.from_xy_mm(sign * 2.54, i * 2.54),
        0, False, False, text_alignment="right" if sign == -1 else "left",
        group="bank", required=i % 2 == 0,
        members=(PlacedEndpoint(Vector2.from_xy_mm(0, i * 2.54), side, "IC", "bank"),),
    ) for i in range(6)]
    spaced, _ = _fit_dense_label_banks(targets, {}, {})
    boxes = [envelope_from_points(label_envelope(Text(
        t.display_name, t.position, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
            0, t.text_alignment, "center" if t.required else "bottom"),
    ))) for t in spaced]
    assert len({t.position.x for t in spaced}) == 1
    assert all(envelopes_do_not_overlap(a, b, 635_000)
               for index, a in enumerate(boxes) for b in boxes[index+1:])
    assert [t.members for t in spaced] == [t.members for t in targets]
    assert sum(t.position.y for t in spaced) == sum(t.position.y for t in targets)


@pytest.mark.parametrize("side", ["left", "right"])
def test_greedy_label_fitting_preserves_a_crowded_native_bank(side):
    sign = -1 if side == "left" else 1
    pins = {str(i): [PlacedEndpoint(Vector2.from_xy_mm(0, 2.54 * i),
                                    side, "device", "local")] for i in range(6)}
    targets = [NetSymbolTarget(name, "LONG_INTERFACE_" + name,
        Vector2.from_xy_mm(sign * 2.54, ps[0].position.y / 1e6), 0, False, False,
        text_alignment="right" if sign < 0 else "left", group="local",
        members=tuple(ps), required=i % 2 == 0)
        for i, (name, ps) in enumerate(pins.items())]
    spaced, _ = _fit_dense_label_banks(targets, {}, {})
    fitted = fit_signal_labels_on_pin_exits(spaced, pins, {})
    assert {t.rotation for t in fitted} == {0}
    assert len({t.position.x for t in fitted}) == 1
    assert [t.members for t in fitted] == [t.members for t in targets]
    boxes = [envelope_from_points(label_envelope(Text(t.display_name, t.position,
        TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0, t.text_alignment,
                       "center" if t.required else "bottom")))) for t in fitted]
    assert all(envelopes_do_not_overlap(a, b, 635_000)
               for i, a in enumerate(boxes) for b in boxes[i+1:])
    # A neighbouring unlabeled terminal is fixed geometry too: the bank
    # must move its tips, not cover the terminal's only outward approach.
    foreign = PlacedEndpoint(Vector2.from_xy_mm(sign * 12, 2.54), side, "support", "local")
    changed = fit_signal_labels_on_pin_exits(targets, {**pins, "SUPPLY": [foreign]}, {})
    for t in changed:
        box = envelope_from_points(label_envelope(Text(t.display_name, t.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0, t.text_alignment,
                           "center" if t.required else "bottom"))))
        assert envelopes_do_not_overlap(box, pin_contact_obstacle(foreign), 635_000)


def test_named_net_keeps_local_branches_without_wrapping_around_device():
    target = NetSymbolTarget("REF", "REF", Vector2(0, 0), 0, False, False)
    left = PlacedEndpoint(Vector2.from_xy_mm(10, 30), "left", "core", "block")
    right = PlacedEndpoint(Vector2.from_xy_mm(30, 10), "right", "core", "block")
    local = PlacedEndpoint(Vector2.from_xy_mm(5, 30), "bottom", "shunt", "block")
    below = PlacedEndpoint(Vector2.from_xy_mm(40, 25), "right", "part", "block")
    barrier = PlacedEndpoint(Vector2.from_xy_mm(30, 15), "right", "core", "block")

    labels = localize_signal_labels([target], {"REF": [left, right, local, below],
                                                "OUTPUT": [barrier]})

    assert len(labels) == 3
    assert any(label.members == (left, local) for label in labels)
    assert any(label.members == (right,) for label in labels)
    assert any(label.members == (below,) for label in labels)
    # Caption fitting must not silently rejoin the three regions.
    fitted = fit_signal_labels_on_pin_exits(labels,
                                             {"REF": [left, right, local, below]}, {})
    assert [label.members for label in fitted] == [label.members for label in labels]


def test_label_fitting_does_not_extend_a_separated_stub_across_local_wiring():
    pin = PlacedEndpoint(Vector2.from_xy_mm(40, 20), "left", "core", "block")
    label = NetSymbolTarget("REFERENCE", "LONG_REFERENCE_NET", Vector2.from_xy_mm(37.46, 20),
                             0, False, False, "right", group="block", members=(pin,))
    control = [
        PlacedEndpoint(Vector2.from_xy_mm(40, 17.46), "left", "core", "block"),
        PlacedEndpoint(Vector2.from_xy_mm(34.92, 17.46), "right", "series", "block"),
        PlacedEndpoint(Vector2.from_xy_mm(15, 30), "right", "cap", "block"),
    ]
    fitted = fit_signal_labels_on_pin_exits(
        [label], {"REFERENCE": [pin], "CONTROL": control}, {},
        {"block": [Envelope(20_000_000, 16_000_000, 36_000_000, 21_000_000)]},
    )[0]
    assert fitted.position.x > 37_460_000
    assert fitted.rotation in (90, 270)
    box = envelope_from_points(text_envelope(Text(
        fitted.display_name, fitted.position,
        TextAttributes(Vector2.from_xy_mm(1.27, 1.27), fitted.rotation,
                       fitted.text_alignment, "bottom"),
    )))
    assert all(not segment_hits_box(w.start, w.end, box)
               for w in route_group(control) if isinstance(w, SchematicLine))
