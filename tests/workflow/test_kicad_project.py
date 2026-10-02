from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from schemer.analysis.hierarchy import plan_sheets
from schemer.core.errors import KiCadSchematicError
from schemer.integration.kicad_cli import DEFAULT_KICAD_CLI, verify_native_project_connectivity
from schemer.kicad.editor import FileSchematic
from schemer.kicad.items import GlobalLabel, Vector2
from schemer.native.annotations.signal_topology import localize_signal_labels
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.seed import net_symbol_targets
from schemer.workflow.native_project import _seed_sheet, layout_native_project, sheet_view
from tests.support.hierarchy import LIBRARY, _part


def _fixture(*, root_part=False):
    symbols = []
    instances = {"root": {"kind": "Module", "children": {"A": "root.A", "B": "root.B"}}}
    lib = LIBRARY.removeprefix("(lib_symbols").rstrip()[:-1].strip()
    for group in ("A", "B"):
        instances[f"root.{group}"] = {"kind": "Module", "children": {}, "attributes": {
            "schematic_properties": {"Json": {"sheet": group}},
        }}
    paths = ["A.R1", "A.R2", "B.R3", "B.R4"]
    if root_part:
        paths.append("R5")
        instances["root"]["children"]["R5"] = "root.R5"
    for index, path in enumerate(paths, 1):
        ref = "root." + path
        instances[ref] = {
            "kind": "Component", "reference_designator": f"R{index}",
            "children": {"1": ref + ".1", "2": ref + ".2"},
            "attributes": {"type": {"String": "resistor"}, "value": {"String": "1k"},
                           "__symbol_value": {"String": lib}},
        }
        symbols.append(_part(f"R{index}", "/unused").replace(
            '(property "Value"', f'(property "Path" "{path}" (at 0 0 0) '
            '(effects (font (size 1.27 1.27)) hide)) (property "Value"',
        ))
    nets = {}
    for index, (name, ports) in enumerate((
        ("A.IN", ("A.R1.1",)), ("B.IN", ("B.R3.1",)),
        ("LINK", ("A.R1.2", "A.R2.1", "B.R3.2")),
        ("NEXT", ("A.R2.2", "B.R4.1")), ("END", ("B.R4.2",)),
    )):
        nets[str(index)] = {"id": index, "name": name, "kind": "Net",
                            "ports": ["root." + port for port in ports]}
    intent = {"root_ref": "root", "instances": instances, "nets": nets}
    if root_part:
        nets["2"]["ports"].append("root.R5.1")
        nets["4"]["ports"].append("root.R5.2")
    seed = FileSchematic.from_text(
        f'(kicad_sch (version 20250114) (generator "test") (uuid "{uuid4()}") '
        '(paper "A4") ' + LIBRARY + "\n" + "\n".join(symbols) + ")",
    )
    return intent, seed


def test_single_sheet_overview_keeps_completed_framing():
    from schemer.kicad.items import PageSettings
    from schemer.workflow.native_project import _overview

    _, editor = _fixture(root_part=True)
    editor.set_page_settings(PageSettings("A2", "landscape"))
    before = [(s.uuid, s.position, s.rotation) for s in editor.document.symbols]
    result = _overview(editor, (), {}, {}, "root-id", "Project")
    assert result.get_page_settings() == PageSettings("A2", "landscape")
    assert [(s.uuid, s.position, s.rotation) for s in result.document.symbols] == before
    assert result.document.root.first_list("sheet_instances") is not None


def test_navigation_uses_the_shared_drawing_margin():
    from schemer.native.packing import DRAWING_MARGIN_MM
    from schemer.workflow.native_project import _overview

    intent, editor = _fixture(root_part=True)
    plan = plan_sheets(intent)
    files = {s.module_ref: f"{i}.kicad_sch" for i, s in enumerate(plan.sheets)}
    ids = {s.module_ref: str(uuid4()) for s in plan.sheets}
    result = _overview(editor, plan.sheets[1:], files, ids, ids[plan.root_ref], "Project")
    boxes = [e for e in result.document.root.children if getattr(e, "tag", None) == "sheet"]
    assert boxes
    assert all(float(e.first_list("at").children[axis].value) >= DRAWING_MARGIN_MM
               for e in boxes for axis in (1, 2))


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
@pytest.mark.parametrize("desired", [0, 90])
def test_generated_field_direction_matches_native_svg_across_symbol_rotations(tmp_path, desired):
    """Check KiCad's renderer, not just the generator's own angle model."""
    import re
    import subprocess
    import xml.etree.ElementTree as ET

    from schemer.kicad.geometry.text import oriented_field_text

    _, editor = _fixture()
    symbols = editor.get_symbols()
    for symbol, rotation in zip(symbols, (0, 90, 180, 270), strict=True):
        symbol.position = Vector2.from_xy_mm(40 + rotation / 2, 80)
        symbol.transform.orientation = rotation
        symbol.reference_field.visible = False
        symbol.value_field.text.value = "FIELD"
        symbol.value_field.text.position = Vector2.from_xy_mm(40 + rotation / 2, 60)
        symbol.value_field.text = oriented_field_text(
            symbol.value_field.text, rotation, desired, "left")
    editor.update_items(symbols)
    path = tmp_path / "fields.kicad_sch"
    editor.save_as(path)
    subprocess.run([str(DEFAULT_KICAD_CLI), "sch", "export", "svg",
                    "--exclude-drawing-sheet", "--output", str(tmp_path), str(path)],
                   check=True, capture_output=True)
    svg = ET.parse(tmp_path / "fields.svg")
    ns = {"s": "http://www.w3.org/2000/svg"}
    strokes = [g for g in svg.findall('.//s:g[@class="stroked-text"]', ns)
               if g.findtext("s:desc", namespaces=ns) == "FIELD"]
    assert len(strokes) == 4
    # Normalize painted paths by the common left/bottom anchor. Opposite
    # symbol rotations must not reverse glyphs or their text direction.
    drawings = []
    for group in strokes:
        numbers = [float(n) for p in group.findall("s:path", ns)
                   for n in re.findall(r"-?\d+\.\d+", p.attrib["d"])]
        points = list(zip(numbers[::2], numbers[1::2], strict=True))
        anchor_x = round(min(x for x, _ in points) / 5) * 5
        drawings.append([(x - anchor_x, y) for x, y in points])
    for drawing in drawings[1:]:
        for actual, expected in zip(drawing, drawings[0], strict=True):
            assert actual == pytest.approx(expected, abs=.0002)
    text_nodes = [t for t in svg.findall('.//s:text', ns) if t.text == "FIELD"]
    parents = {child: parent for parent in svg.iter() for child in parent}
    for text in text_nodes:
        transform = parents[text].attrib.get("transform", "")
        assert ("rotate(-90.000000" in transform) == (desired == 90)


def test_sheet_view_preserves_identity_and_restricts_pin_membership():
    intent, _ = _fixture()
    before = deepcopy(intent)
    sheets = plan_sheets(intent).sheets
    view = sheet_view(intent, sheets[1])
    assert {item["reference_designator"] for item in view["instances"].values()
            if item.get("reference_designator")} == {"R1", "R2"}
    assert view["nets"]["2"]["ports"] == ["root.A.R1.2", "root.A.R2.1"]
    assert intent == before


@pytest.mark.parametrize("owned_branch", [False, True])
def test_authored_channel_is_seeded_locally_without_a_repeated_peer(tmp_path, owned_branch):
    intent, _ = _fixture(root_part=owned_branch)
    for order, ref in enumerate(("root.A.R1", "root.A.R2")):
        intent["instances"][ref]["attributes"]["schematic_properties"] = {"Json": {
            "role": "series", "group": "conditioning", "order": order,
        }}
    if owned_branch:
        intent["instances"]["root.R5"]["attributes"]["schematic_properties"] = {"Json": {
            "role": "source-impedance", "group": "measurement", "owner": "R1", "pin": "2",
        }}
    before = deepcopy(intent)
    entry = tmp_path / "circuit.zen"
    entry.write_text("")
    seeded = _seed_sheet(intent, entry)
    positions = seeded["instances"]["root"]["symbol_positions"]
    a, b = positions["comp:A.R1"], positions["comp:A.R2"]
    assert a["y"] == b["y"]
    assert 0 < b["x"] - a["x"] < 1000
    # An unrelated circuit can change size without changing this circuit's
    # internal geometry. The native wiring partition governs the seed too.
    unrelated = deepcopy(intent)
    unrelated["instances"]["root.B.R3"]["attributes"]["value"] = {"String": "long caption " * 30}
    alternate = _seed_sheet(unrelated, entry)["instances"]["root"]["symbol_positions"]
    assert (alternate["comp:A.R2"]["x"] - alternate["comp:A.R1"]["x"],
            alternate["comp:A.R2"]["y"] - alternate["comp:A.R1"]["y"]) == (
                b["x"] - a["x"], b["y"] - a["y"])
    if owned_branch:
        from schemer.analysis.circuits import component_layout_groups

        groups, _ = component_layout_groups(intent)
        assert groups["root.R5"] == groups["root.A.R1"] == groups["root.A.R2"]
        branch = positions["comp:R5"]
        assert abs(branch["x"] - a["x"]) < 1000
        assert abs(branch["y"] - a["y"]) < 1000
    assert intent == before


def test_boundary_label_survives_local_compound_node_simplification():
    a = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "a", "GROUP")
    b = PlacedEndpoint(Vector2.from_xy_mm(20, 20), "left", "b", "GROUP")
    schematic = {"nets": {"n": {"id": 1, "name": "LINK", "kind": "Net"}}}
    targets = net_symbol_targets(
        schematic, [], {"1": "LINK"}, (0, 0), {}, Vector2(0, 0), {"LINK": [a, b]},
        required_nets=frozenset({"LINK"}),
    )
    assert len(localize_signal_labels(targets, {"LINK": [a, b]})) == 1


def test_single_series_part_and_shunt_form_a_complete_local_circuit(tmp_path):
    from schemer.core.layout import ModuleLayout, Position
    from schemer.placement.circuits.queries import collect_components
    from schemer.symbols.geometry import pin_positions

    intent, _ = _fixture()
    intent["instances"]["root.A.R1"]["attributes"]["schematic_properties"] = {"Json": {
        "role": "series", "group": "filter", "order": 0,
    }}
    intent["instances"]["root.A.R2"]["attributes"]["schematic_properties"] = {"Json": {
        "role": "shunt", "group": "filter", "at": "LINK",
    }}
    intent["nets"]["3"]["kind"] = "Ground"
    entry = tmp_path / "circuit.zen"
    entry.write_text("")
    seeded = _seed_sheet(intent, entry)
    raw = seeded["instances"]["root"]["symbol_positions"]
    _, _, components = collect_components(intent, ModuleLayout("root", entry, {}))
    points = {}
    for component in components:
        if component.ref.startswith("root.A."):
            p = Position(**raw[component.symbol_id])
            points[component.ref] = [pin_positions(component.instance, p, t)[0]
                                     for t in component.terminals]
    a, b = points["root.A.R1"], points["root.A.R2"]
    assert a[0].y == a[1].y
    assert b[0].x == b[1].x
    assert max(p.x for p in [*a, *b]) - min(p.x for p in [*a, *b]) < 1000


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
@pytest.mark.parametrize("root_part", [False, True])
def test_project_writer_exports_real_flat_hierarchy_without_renumbering(tmp_path, root_part):
    intent, seed = _fixture(root_part=root_part)
    before = deepcopy(intent)
    entry = tmp_path / "Circuit.zen"
    entry.write_text("# Intents already supplied in this test.\n")
    output = tmp_path / "output"
    report = layout_native_project(intent, seed, entry, output)
    assert report.checked_pins == 8 + 2 * root_part
    assert len(report.sheets) == 3
    for sheet in report.sheets:
        assert "structural_inventory" in sheet
        assert (output / sheet["structural_review"]).is_file()
    assert intent == before
    root = FileSchematic.from_file(report.root_file)
    assert len(root.document.root.lists("sheet")) == 2
    assert all(not sheet.lists("pin") for sheet in root.document.root.lists("sheet"))
    # No electrical items exist on an overview without actual root components.
    if not root_part:
        assert not root.get_labels() and not root.get_lines()
    else:
        assert {label.text for label in root.document.labels
                if label.kind == "global_label"} >= {"LINK", "END"}
    for file in output.glob("0*.kicad_sch"):
        child = FileSchematic.from_file(file)
        assert not child.document.root.lists("sheet")
        assert {s.path for s in child.document.symbols} <= {"A.R1", "A.R2", "B.R3", "B.R4"}
        # Boundary names terminate actual circuit wires, with no local-label bridge bank.
        boundary = [label for label in child.document.labels if label.kind == "global_label"]
        assert boundary
        assert all(label.text.attributes.vertical_alignment == "center"
                   for label in child.get_labels() if isinstance(label, GlobalLabel))
        assert not any(label.kind == "hierarchical_label" for label in child.document.labels)
        local_names = {label.text for label in child.document.labels if label.kind == "label"}
        assert not ({label.text for label in boundary} & local_names)
        assert local_names  # Non-boundary nets stay sheet-local.
    assert verify_native_project_connectivity(intent, report.root_file) == report.checked_pins
    with pytest.raises(KiCadSchematicError, match="already exists"):
        layout_native_project(intent, seed, entry, output)


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
def test_cross_sheet_labels_keep_same_leaf_names_electrically_distinct(tmp_path):
    intent, seed = _fixture(root_part=True)
    intent["nets"]["2"]["name"] = "A.SHARED"
    intent["nets"]["3"]["name"] = "B.SHARED"
    entry = tmp_path / "Circuit.zen"
    entry.write_text("")
    report = layout_native_project(intent, seed, entry, tmp_path / "output")
    assert report.checked_pins == 10
    labels = {label.text for sheet in report.sheets
              for label in FileSchematic.from_file(
                  report.root_file.parent / sheet["file"]).document.labels
              if label.kind == "global_label"}
    assert {"A.SHARED", "B.SHARED"} <= labels
    assert "SHARED" not in labels
    assert verify_native_project_connectivity(intent, report.root_file) == 10


def test_failed_verification_does_not_publish_project(tmp_path, monkeypatch):
    intent, seed = _fixture()
    entry = tmp_path / "Circuit.zen"
    entry.write_text("")

    def reject(*args):
        raise KiCadSchematicError("test verification failure")

    monkeypatch.setattr(
        "schemer.workflow.native_project.verify_native_project_connectivity", reject
    )
    with pytest.raises(KiCadSchematicError, match="test verification failure"):
        layout_native_project(intent, seed, entry, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_explicit_draft_publishes_all_sheets_with_failed_check_recorded(tmp_path, monkeypatch):
    intent, seed = _fixture()
    entry = tmp_path / "Circuit.zen"
    entry.write_text("")

    def reject(*args):
        raise KiCadSchematicError("test connectivity mismatch")

    monkeypatch.setattr(
        "schemer.workflow.native_project.verify_native_project_connectivity", reject
    )
    output = tmp_path / "draft"
    report = layout_native_project(intent, seed, entry, output, draft=True)
    assert report.draft and report.checked_pins is None
    assert report.connectivity_error == "test connectivity mismatch"
    saved = json.loads((output / "layout-report.json").read_text())
    assert saved["draft"] and saved["connectivity_error"] == report.connectivity_error
    assert len(saved["sheets"]) == 3
    for sheet in saved["sheets"]:
        assert "DRAFT -" in (output / sheet["file"]).read_text()
    assert len(FileSchematic.from_file(report.root_file).document.root.lists("sheet")) == 2


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
def test_project_rails_cross_sheets_without_duplicate_hierarchy_ports(tmp_path):
    intent, seed = _fixture(root_part=True)
    intent["nets"]["2"]["kind"] = "Power"
    intent["nets"]["3"]["kind"] = "Ground"
    entry = tmp_path / "Circuit.zen"
    entry.write_text("")
    report = layout_native_project(intent, seed, entry, tmp_path / "output")
    assert report.checked_pins == 10
    for sheet in report.sheets:
        assert not ({"LINK", "NEXT"} & set(sheet["ports"]))
        editor = FileSchematic.from_file(report.root_file.parent / sheet["file"])
        assert not any(label.kind == "hierarchical_label" and label.text in {"LINK", "NEXT"}
                       for label in editor.document.labels)
    assert verify_native_project_connectivity(intent, report.root_file) == 10
