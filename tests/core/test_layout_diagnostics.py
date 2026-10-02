from __future__ import annotations

from types import SimpleNamespace

import pytest

from schemer.core.diagnostics import capture_layout_issues
from schemer.core.errors import KiCadSchematicError, ToolchainError
from schemer.core.layout import Position
from schemer.kicad.geometry.envelopes import Envelope, envelope_from_points
from schemer.kicad.geometry.text import label_envelope
from schemer.kicad.items import LocalLabel, SchematicLine, Text, TextAttributes, Vector2
from schemer.native.routing import route_group
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.validation import clear_final_label_wires
from schemer.placement.blocks.model import BlockItem, LayoutBlock, Rect
from schemer.placement.blocks.plan import BlockPlan


def test_draft_retains_overlapping_parts_but_default_remains_strict():
    items = tuple(BlockItem(name, Position(10, 10), Rect(10, 10, 20, 20))
                  for name in ("comp:R1", "comp:R2"))
    plan = BlockPlan(LayoutBlock("block", 100, 100, items=items))
    with capture_layout_issues(True) as issues:
        assert len(plan.positions()) == 2
        plan.positions()  # repeated validation does not duplicate the finding
    assert len(issues) == 1
    assert issues[0]["code"] == "item-body-overlap"
    with pytest.raises(ToolchainError, match="item-body-overlap"):
        plan.positions()


def test_draft_does_not_suppress_invalid_ownership_or_structure():
    item = BlockItem("comp:R1", Position(10, 10), Rect(10, 10, 20, 20))
    plan = BlockPlan(LayoutBlock("block", 100, 100, items=(item, item)))
    with capture_layout_issues(True), pytest.raises(ToolchainError, match="duplicate-symbol-owner"):
        plan.positions()


def test_draft_omits_unsafe_route_and_reports_blocked_escape():
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right"),
                 PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    obstacles = [Envelope(11_000_000, 9_000_000, 12_000_000, 11_000_000)]
    with capture_layout_issues(True) as issues:
        result = route_group(endpoints, obstacles=obstacles, net_name="SIGNAL")
    assert not any(isinstance(item, SchematicLine) for item in result)
    assert issues[0]["code"] == "route-clearance-failed"
    assert issues[0]["subjects"] == ["SIGNAL"]
    with pytest.raises(KiCadSchematicError, match="routing net SIGNAL: blocked pin exit"):
        route_group(endpoints, obstacles=obstacles, net_name="SIGNAL")


@pytest.mark.parametrize("multiple_incident_wires", [False, True])
def test_draft_retains_unresolved_labels(multiple_incident_wires):
    point = Vector2.from_xy_mm(10, 10)
    text = Text("SIGNAL", point, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                               horizontal_alignment="right"))
    label = LocalLabel(id="label", position=point, text=text)
    box = envelope_from_points(label_envelope(text))
    wires = [SchematicLine(id="stub", start=Vector2.from_xy_mm(0, 10), end=point),
             SchematicLine(id="foreign", start=Vector2(-100_000_000, box.center_y),
                           end=Vector2(100_000_000, box.center_y))]
    if multiple_incident_wires:
        wires.append(SchematicLine(id="branch", start=point, end=Vector2.from_xy_mm(10, 20)))
    editor = SimpleNamespace(document=SimpleNamespace(symbols=[]),
                             get_labels=lambda: [label], get_lines=lambda: wires)
    with capture_layout_issues(True) as issues:
        clear_final_label_wires(editor)
    assert label.position == point
    assert issues[0]["code"] == "label-wire-overlap"
    with pytest.raises(KiCadSchematicError, match="label"):
        clear_final_label_wires(editor)
