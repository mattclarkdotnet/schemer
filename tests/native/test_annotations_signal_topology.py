from __future__ import annotations

import pytest

from schemer.kicad.geometry.library import (
    placed_pin_positions,
    symbol_library_pins,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.annotations.signal_topology import (
    localize_signal_labels,
    nearest_local_target,
    prune_unused_targets,
    space_labeled_pin_connections,
)
from schemer.native.model import NetSymbolTarget
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.seed import net_symbol_targets
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("vertical", [False, True])
def test_named_local_wire_reserves_space_without_changing_its_pin_row(
        direction, vertical, monkeypatch):
    schematic, editor = _support_fixture(None)
    parts = editor.get_symbols()
    for s, x in zip(parts, (40, 40 + direction * 10), strict=True):
        s.transform.orientation = 90 if vertical else 0
        s.position = Vector2.from_xy_mm(30.48, x) if vertical else Vector2.from_xy_mm(x, 30.48)
        s.reference_field.visible = s.value_field.visible = False
    editor.update_items(parts)
    geometry = symbol_library_pins
    monkeypatch.setattr(
        "schemer.native.annotations.signal_topology.symbol_library_pins",
        lambda doc, s: (
            {"1": None, "2": None, "3": None} if s.reference == "R1" else geometry(doc, s)
        ),
    )
    pins = {s.reference: placed_pin_positions(editor.document, s) for s in editor.document.symbols}
    number = "1" if (direction == 1) == vertical else "2"
    other = "2" if number == "1" else "1"
    owner_side = ("bottom" if direction == 1 else "top") if vertical else (
        "right" if direction == 1 else "left")
    part_side = {"bottom": "top", "top": "bottom", "right": "left", "left": "right"}[owner_side]
    endpoints = {"SIGNAL_NAME": [
        PlacedEndpoint(pins["R1"][number], owner_side, "root.OWNER", "IC"),
        PlacedEndpoint(pins["R2"][other], part_side, "root.BIAS", "IC"),
        PlacedEndpoint(Vector2(0, 0), "left", "root.PORT", "PORT"),
    ]}
    target = NetSymbolTarget("SIGNAL_NAME", "SIGNAL_NAME", Vector2(0, 0), 0, False, False)
    before = {s.id: (s.position.x, s.position.y) for s in parts}
    assert space_labeled_pin_connections(schematic, editor, [target], endpoints)
    owner, part = editor.get_symbols()
    assert owner.position == parts[0].position
    assert ((part.position.x if vertical else part.position.y)
            == before[part.id][0 if vertical else 1])
    assert direction * ((part.position.y if vertical else part.position.x)
                        - before[part.id][1 if vertical else 0]) > 0


def test_cross_block_interfaces_get_targets_without_legacy_position_comments():
    schematic = {"nets": {
        "external": {"id": 1, "name": "EXTERNAL", "kind": "Net"},
        "local": {"id": 2, "name": "LOCAL", "kind": "Net"},
    }}
    a = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "a", "FIRST")
    b = PlacedEndpoint(Vector2.from_xy_mm(40, 10), "left", "b", "SECOND")
    endpoints = {"EXTERNAL": [a, b], "LOCAL": [a, a]}
    templates = net_symbol_targets(
        schematic, [], {"1": "EXT"}, (0, 0), {}, Vector2(0, 0), endpoints,
    )
    assert [target.net_name for target in templates] == ["EXTERNAL"]
    labels = localize_signal_labels(templates, endpoints)
    assert {target.group for target in labels} == {"FIRST", "SECOND"}
    assert {target.display_name for target in labels} == {"EXT"}


def test_legacy_label_proximity_cannot_merge_independently_packed_blocks():
    targets = [
        NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(x, 10), 0, False, False)
        for x in (0, 100)
    ]
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(x, 10), "left", group=group)
        for x, group in ((10, "panel.j1"), (20, "panel.j2"), (90, "processor"))
    ]
    labels = localize_signal_labels(targets, {"DATA": endpoints})
    assert {label.group for label in labels} == {endpoint.group for endpoint in endpoints}
    assert len(labels) == 3


def test_target_selection_and_pruning_stay_within_the_block():
    targets = [
        NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(x, 10), 0, False, False,
                         group=group)
        for x, group in ((10, "first"), (20, "second"))
    ]
    # Both endpoints are closer to the other block's label before packing.
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(x, 10), "left", group=group)
        for x, group in ((21, "first"), (11, "second"))
    ]
    assert [nearest_local_target(p, targets) for p in endpoints] == [0, 1]
    assert prune_unused_targets(targets, {"DATA": endpoints}) == targets


def test_rail_membership_survives_termination_movement():
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "IC", "block")
    assigned = NetSymbolTarget("GND", "GND", Vector2.from_xy_mm(40, 10),
                                0, True, True, group="block", members=(pin,))
    nearer = NetSymbolTarget("GND", "GND", Vector2.from_xy_mm(13, 10),
                              0, True, True, group="block")
    assert nearest_local_target(pin, [nearer, assigned]) == 1
    assert prune_unused_targets([nearer, assigned], {"GND": [pin]}) == [assigned]


def test_coincident_labels_in_different_blocks_survive_until_packing():
    target = NetSymbolTarget("DATA", "DATA", Vector2(0, 0), 0, False, False)
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(x, 10), side, group=group)
        for x, side, group in ((10, "right", "a"), (15.08, "left", "b"))
    ]
    labels = localize_signal_labels([target], {"DATA": endpoints})
    assert len(labels) == 2
    assert labels[0].position == labels[1].position
    assert {label.group for label in labels} == {"a", "b"}


def test_vertical_pin_label_is_beside_its_wire_not_centered_across_it():
    target = NetSymbolTarget("SHIELD", "SHIELD", Vector2(0, 0), 0, False, False)
    endpoint = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "bottom")
    label = localize_signal_labels([target], {"SHIELD": [endpoint]})[0]
    assert label.text_alignment == "left"
    assert label.position == Vector2.from_xy_mm(10, 12.54)


def test_named_signal_endpoints_receive_separate_outward_labels() -> None:
    target = NetSymbolTarget(
        net_name="CONTROL",
        display_name="CONTROL",
        position=Vector2.from_xy_mm(30, 20),
        rotation=0,
        rail=False,
        ground=False,
    )
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 20), "left", group="input"),
        PlacedEndpoint(Vector2.from_xy_mm(40, 30), "right", group="output"),
    ]

    adjusted = localize_signal_labels(
        [target],
        {"CONTROL": endpoints},
    )

    assert [item.position for item in adjusted] == [
        Vector2.from_xy_mm(7.46, 20),
        Vector2.from_xy_mm(42.54, 30),
    ]
    assert [item.text_alignment for item in adjusted] == ["right", "left"]


def test_boxed_compound_node_label_extends_its_through_wire_without_a_dogleg():
    target = NetSymbolTarget("OUTPUT", "OUTPUT", Vector2(0, 0), 0, False, False,
                             required=True)
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, y), side, owner, "channel")
            for x, y, side, owner in ((20, 10, "right", "IC"),
                                     (30, 10, "left", "series"),
                                     (20, 20, "right", "feedback"))]
    label, = localize_signal_labels([target], {"OUTPUT": pins})
    assert label.position.y == pins[0].position.y
    assert label.position.x > max(p.position.x for p in pins)
    assert label.text_alignment == "left"
    assert all(pin in label.members for pin in pins) and len(label.members) == len(pins)


@pytest.mark.parametrize("seed_count", [1, 2])
def test_one_functional_group_uses_real_wires_instead_of_duplicate_labels(seed_count) -> None:
    target = NetSymbolTarget(
        net_name="LOCAL_CONTROL",
        display_name="LOCAL_CONTROL",
        position=Vector2.from_xy_mm(30, 20),
        rotation=0,
        rail=False,
        ground=False,
    )
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="controller"),
        PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left", group="controller"),
    ]

    assert localize_signal_labels([target] * seed_count, {"LOCAL_CONTROL": endpoints}) == []


@pytest.mark.parametrize("required,rail,other_group", [
    (False, False, False), (True, False, False), (False, True, False),
    (False, False, True),
])
def test_explicit_direct_circuit_overrides_seed_splits_but_not_boundaries(
    required, rail, other_group,
):
    target = NetSymbolTarget("LOOP", "LOOP", Vector2(0, 0), 0, rail, False,
                              required=required)
    pins = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 30), "left", "core", "circuit"),
        PlacedEndpoint(Vector2.from_xy_mm(30, 10), "right", "core",
                        "elsewhere" if other_group else "circuit"),
    ]
    barrier = PlacedEndpoint(Vector2.from_xy_mm(30, 15), "right", "core", "circuit")
    labels = localize_signal_labels([target, target], {"LOOP": pins, "OTHER": [barrier]},
                                    direct_groups=frozenset({"circuit"}))
    assert bool(labels) == (required or rail or other_group)


def test_named_boundary_does_not_split_an_explicit_blocks_internal_wire_tree():
    target = NetSymbolTarget("LOOP", "LOOP", Vector2(0, 0), 0, False, False)
    pins = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 30), "left", "core", "circuit"),
        PlacedEndpoint(Vector2.from_xy_mm(30, 10), "right", "core", "circuit"),
        PlacedEndpoint(Vector2.from_xy_mm(80, 10), "left", "port", "external"),
    ]
    barrier = PlacedEndpoint(Vector2.from_xy_mm(30, 15), "right", "core", "circuit")
    labels = localize_signal_labels([target, target], {"LOOP": pins, "OTHER": [barrier]},
                                    direct_groups=frozenset({"circuit"}))
    assert len(labels) == 2
    assert next(t for t in labels if t.group == "circuit").members == tuple(pins[:2])
    assert next(t for t in labels if t.group == "external").members == (pins[2],)


@pytest.mark.parametrize("series_x", [30, 34.92])
def test_named_reference_does_not_cross_a_neighbouring_local_branch(series_x):
    target = NetSymbolTarget("REF", "REF", Vector2(0, 0), 0, False, False)
    left = PlacedEndpoint(Vector2.from_xy_mm(40, 20), "left", "core", "block")
    right = PlacedEndpoint(Vector2.from_xy_mm(60, 0), "right", "core", "block")
    shunt = PlacedEndpoint(Vector2.from_xy_mm(20, 17), "bottom", "shunt", "block")
    reference = PlacedEndpoint(Vector2.from_xy_mm(10, 30), "left", "cap", "block")
    signal = [
        PlacedEndpoint(Vector2.from_xy_mm(40, 17), "left", "core", "block"),
        PlacedEndpoint(Vector2.from_xy_mm(series_x, 17), "right", "series", "block"),
        PlacedEndpoint(Vector2.from_xy_mm(15, 30), "right", "cap", "block"),
    ]
    labels = localize_signal_labels([target], {"REF": [left, right, shunt, reference],
                                                "CONTROL": signal})
    assert any(label.members == (left,) for label in labels)


def test_shared_signal_gets_one_label_per_functional_group() -> None:
    target = NetSymbolTarget(
        net_name="RESET",
        display_name="RESET",
        position=Vector2.from_xy_mm(30, 20),
        rotation=0,
        rail=False,
        ground=False,
    )
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="controller"),
        PlacedEndpoint(Vector2.from_xy_mm(15, 30), "top", group="controller"),
        PlacedEndpoint(Vector2.from_xy_mm(50, 20), "left", group="panel"),
    ]

    adjusted = localize_signal_labels([target], {"RESET": endpoints})

    assert len(adjusted) == 2
    assert {item.owner for item in adjusted} == {None}
    assert {(item.position.x, item.position.y) for item in adjusted} == {
        (Vector2.from_xy_mm(17.54, 20).x, Vector2.from_xy_mm(17.54, 20).y),
        (Vector2.from_xy_mm(47.46, 20).x, Vector2.from_xy_mm(47.46, 20).y),
    }


def test_unselected_duplicate_net_symbol_is_removed() -> None:
    near = NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(10, 10),
        0,
        True,
        True,
    )
    orphan = NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(100, 100),
        0,
        True,
        True,
    )

    retained = prune_unused_targets(
        [near, orphan],
        {"GND": [PlacedEndpoint(Vector2.from_xy_mm(12, 10), "left", "root.U1")]},
    )

    assert retained == [near]
