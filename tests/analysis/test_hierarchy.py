from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import pytest

from schemer.analysis.hierarchy import plan_sheets
from schemer.core.errors import ToolchainError


def _circuit():
    instances = {
        "root": {"kind": "Module", "children": {"A": "root.A", "B": "root.B"}},
        "root.A": {"kind": "Module", "children": {}},
        "root.A.WRAPPER": {"kind": "Module", "children": {}},
        "root.B": {"kind": "Module", "children": {}},
        "root.B.CHILD": {"kind": "Module", "children": {}},
    }
    for ref, name in (("root.A", "A"), ("root.B", "B"), ("root.B.CHILD", "B / CHILD")):
        instances[ref]["attributes"] = {"schematic_properties": {"Json": {"sheet": name}}}
    for path, designator in (
        ("A.WRAPPER.U", "U1"), ("A.R", "R1"),
        ("B.U", "U2"), ("B.CHILD.R", "R2"), ("B.CHILD.C", "C2"),
    ):
        instances["root." + path] = {
            "kind": "Component", "reference_designator": designator,
        }
    return {"root_ref": "root", "instances": instances, "nets": {
        "signal": {"name": "SIGNAL", "kind": "Net", "ports": [
            "root.A.WRAPPER.U.OUT", "root.B.U.IN", "root.B.CHILD.R.1",
        ]},
        "return": {"name": "RETURN", "kind": "Ground", "ports": [
            "root.A.R.2", "root.B.CHILD.C.2",
        ]},
        "internal": {"name": "INTERNAL", "kind": "Net", "ports": [
            "root.B.U.OUT", "root.B.CHILD.C.1",
        ]},
        "unused": {"name": "NC", "kind": "NotConnected", "ports": [
            "root.A.WRAPPER.U.NC", "root.B.U.NC",
        ]},
    }}


def test_module_sheets_partition_parts_once_and_keep_wrappers_inline():
    source = _circuit()
    before = deepcopy(source)
    plan = plan_sheets(source)
    sheets = {sheet.module_ref: sheet for sheet in plan.sheets}
    assert set(sheets) == {"root", "root.A", "root.B", "root.B.CHILD"}
    assert sheets["root"].component_refs == ()
    assert sheets["root"].child_refs == ("root.A", "root.B", "root.B.CHILD")
    assert all(sheet.parent_ref == "root" and sheet.child_refs == ()
               for ref, sheet in sheets.items() if ref != "root")
    assert sheets["root.B.CHILD"].name == "B / CHILD"
    parts = [part for sheet in plan.sheets for part in sheet.component_refs]
    assert len(parts) == len(set(parts)) == 5
    assert source == before


def test_boundary_membership_uses_physical_pins_not_module_port_aliases():
    source = _circuit()
    source["nets"]["internal"]["ports"].append("root.A.ALIAS")
    sheets = {sheet.module_ref: sheet for sheet in plan_sheets(source).sheets}
    assert {port.net for port in sheets["root.A"].ports} == {"SIGNAL", "RETURN"}
    assert {port.net for port in sheets["root.B"].ports} == {"SIGNAL", "INTERNAL"}
    assert {port.net for port in sheets["root.B.CHILD"].ports} == {
        "SIGNAL", "RETURN", "INTERNAL",
    }
    assert sheets["root"].ports == ()


def test_empty_containers_are_names_not_additional_sheets():
    source = {"root_ref": "root", "nets": {}, "instances": {
        "root": {"kind": "Module", "children": {"BANK": "root.BANK"}},
        "root.BANK": {"kind": "Module", "children": {}},
    }}
    for index, path in enumerate(("BANK.FIRST", "BANK.SECOND", "OTHER.FIRST")):
        source["instances"]["root." + path] = {"kind": "Module", "children": {}}
        source["instances"]["root." + path]["attributes"] = {
            "schematic_properties": {"Json": {"sheet": path.replace(".", " / ")}},
        }
        for pin in (1, 2):
            source["instances"][f"root.{path}.R{pin}"] = {
                "kind": "Component", "reference_designator": f"R{index * 2 + pin}",
            }
    plan = plan_sheets(source)
    sheets = {sheet.module_ref: sheet for sheet in plan.sheets}
    assert "root.BANK" not in sheets
    assert [sheet.name for sheet in plan.sheets] == [
        "Overview", "BANK / FIRST", "BANK / SECOND", "OTHER / FIRST",
    ]
    assert all(sheet.parent_ref == "root" for sheet in plan.sheets[1:])
    assert sum(len(sheet.component_refs) for sheet in plan.sheets) == 6


def test_nested_source_parts_expose_their_connection_on_both_sibling_sheets():
    sheets = {sheet.module_ref: sheet for sheet in plan_sheets(_circuit()).sheets}
    parent_port = next(port for port in sheets["root.B"].ports if port.net == "INTERNAL")
    child_port = next(port for port in sheets["root.B.CHILD"].ports if port.net == "INTERNAL")
    assert parent_port.inside_components == ("root.B.U",)
    assert child_port.inside_components == ("root.B.CHILD.C",)
    assert parent_port.inside_components == child_port.outside_components
    assert child_port.inside_components == parent_port.outside_components


def test_owned_network_cannot_be_cut_by_a_sheet_boundary():
    source = _circuit()
    source["instances"]["root.B.CHILD.C"]["attributes"] = {
        "schematic_properties": {"Json": {
            "role": "bypass", "owner": "U2", "pin": "VDD", "group": "supply",
        }},
    }
    sheets = {sheet.module_ref: sheet for sheet in plan_sheets(source).sheets}
    assert set(sheets) == {"root", "root.A", "root.B", "root.B.CHILD"}
    assert "root.B.CHILD.C" in sheets["root.B"].component_refs
    assert "root.B.CHILD.C" not in sheets["root.B.CHILD"].component_refs
    assert {port.net for port in sheets["root.B.CHILD"].ports} == {"SIGNAL"}


def test_owned_support_can_follow_an_owner_in_a_sibling_module():
    source = _circuit()
    source["instances"]["root.A.R"]["attributes"] = {
        "schematic_properties": {"Json": {
            "role": "bypass", "owner": "U2", "pin": "VDD", "group": "supply",
        }},
    }
    sheets = {sheet.module_ref: sheet for sheet in plan_sheets(source).sheets}
    assert "root.A.R" in sheets["root.B"].component_refs
    assert "root.A.WRAPPER.U" in sheets["root.A"].component_refs
    assert {port.net for port in sheets["root.A"].ports} == {"SIGNAL"}


def test_cyclic_authored_ownership_is_rejected():
    source = _circuit()
    for ref, owner in (("root.A.R", "U2"), ("root.B.U", "R1")):
        source["instances"][ref]["attributes"] = {
            "schematic_properties": {"Json": {
                "role": "bypass", "owner": owner, "pin": "VDD", "group": "supply",
            }},
        }
    with pytest.raises(ToolchainError, match="cyclic authored ownership"):
        plan_sheets(source)


def test_transparent_multi_part_wrapper_does_not_add_a_sheet():
    source = _circuit()
    source["instances"]["root.A.R"]["reference_designator"] = None
    source["instances"]["root.A.WRAPPER.R"] = {
        "kind": "Component", "reference_designator": "R1",
    }
    sheets = {sheet.module_ref: sheet for sheet in plan_sheets(source).sheets}
    assert "root.A" in sheets and "root.A.WRAPPER" not in sheets


def test_plan_does_not_depend_on_dict_order_or_positions():
    source = _circuit()
    expected = asdict(plan_sheets(source))
    source["instances"] = dict(reversed(list(source["instances"].items())))
    source["nets"] = dict(reversed(list(source["nets"].items())))
    source["instances"]["root.A"]["symbol_positions"] = {"comp:R": {"x": 999, "y": -99}}
    assert asdict(plan_sheets(source)) == expected


def test_authored_function_is_a_title_not_a_new_role():
    source = _circuit()
    source["instances"]["root.A"]["attributes"] = {
        "schematic_properties": {"Json": {"function": "Signal conditioning", "sheet": "A"}},
    }
    sheet = next(sheet for sheet in plan_sheets(source).sheets if sheet.module_ref == "root.A")
    assert sheet.function == "Signal conditioning"


def test_missing_owner_is_an_error_not_permission_to_guess():
    source = _circuit()
    source["instances"]["root.A.R"]["attributes"] = {
        "schematic_properties": {"Json": {
            "role": "bypass", "owner": "ABSENT", "pin": "VDD", "group": "supply",
        }},
    }
    with pytest.raises(ToolchainError, match="missing authored owner"):
        plan_sheets(source)


def test_sheet_plan_cli_reports_without_changing_sources(monkeypatch, capsys):
    import json

    from schemer import cli
    from schemer.cli import commands

    source = _circuit()
    before = deepcopy(source)
    monkeypatch.setattr(commands, "evaluate_zener", lambda *_: source)
    assert cli.main(["plan-sheets", "example.zen", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["root_ref"] == "root"
    assert len(report["sheets"]) == 4
    assert report["sheets"][0]["name"] == "Overview"
    assert all(sheet["parent_ref"] == "root" and not sheet["child_refs"]
               for sheet in report["sheets"][1:])
    assert source == before


def test_unassigned_modules_stay_on_root_regardless_of_size_or_nesting():
    source = _circuit()
    for instance in source["instances"].values():
        instance.pop("attributes", None)
    plan = plan_sheets(source)
    assert len(plan.sheets) == 1
    assert len(plan.sheets[0].component_refs) == 5


def test_related_modules_share_sheet_and_descendants_inherit_membership():
    source = _circuit()
    for ref in ("root.A", "root.B"):
        source["instances"][ref]["attributes"]["schematic_properties"]["Json"]["sheet"] = "Shared"
    source["instances"]["root.B.CHILD"].pop("attributes")
    plan = plan_sheets(source)
    assert [sheet.name for sheet in plan.sheets] == ["Overview", "Shared"]
    assert len(plan.sheets[1].component_refs) == 5
    assert plan.sheets[1].ports == ()


def test_nested_module_can_join_root_without_adding_an_overview_child():
    source = _circuit()
    source["instances"]["root.B.CHILD"]["attributes"] = {
        "schematic_properties": {"Json": {"sheet": "Overview"}},
    }
    plan = plan_sheets(source)
    assert [sheet.name for sheet in plan.sheets] == ["Overview", "A", "B"]
    assert len(plan.sheets[0].component_refs) == 2


@pytest.mark.parametrize("name", ["", " ", None, 4])
def test_invalid_sheet_name_is_rejected(name):
    source = _circuit()
    source["instances"]["root.A"]["attributes"]["schematic_properties"]["Json"]["sheet"] = name
    with pytest.raises(ToolchainError, match="sheet must be a non-empty module sheet name"):
        plan_sheets(source)
