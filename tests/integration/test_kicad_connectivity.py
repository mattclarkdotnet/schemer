from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from schemer.analysis.connectivity import expected_pin_nets, validate_exported_netlist
from schemer.core.errors import KiCadSchematicError


def _circuit():
    return {
        "root_ref": "root",
        "instances": {
            "root": {"children": {}},
            "root.U": {
                "reference_designator": "U1",
                "attributes": {"__symbol_value": {"String": '''(symbol "Part"
                    (pin power_in line (name "GND") (number "1"))
                    (pin power_in line (name "GND") (number "2"))
                    (pin input line (name "IN") (number "3")))'''}},
            },
            "root.R": {"reference_designator": "R1"},
        },
        "nets": {
            "ground": {"name": "RETURN", "kind": "Ground", "ports": ["root.U.GND"]},
            "signal": {"name": "INPUT", "ports": ["root.U.IN", "root.R.1"]},
            "other": {"name": "OTHER", "ports": ["root.R.2"]},
        },
    }


def _netlist(groups):
    root = ET.Element("export")
    nets = ET.SubElement(root, "nets")
    for index, pins in enumerate(groups):
        net = ET.SubElement(nets, "net", code=str(index), name=f"arbitrary_{index}")
        for ref, number in pins:
            ET.SubElement(net, "node", ref=ref, pin=number)
    return ET.tostring(root, encoding="unicode")


def test_all_physical_pins_of_one_terminal_are_required():
    pins = expected_pin_nets(_circuit())
    assert pins[("U1", "1")] == pins[("U1", "2")] == "RETURN"
    assert len(pins) == 5


def test_exported_pin_membership_is_independent_of_display_net_names():
    xml = _netlist([
        [("U1", "1"), ("U1", "2")],
        [("U1", "3"), ("R1", "1")],
        [("R1", "2")],
    ])
    assert validate_exported_netlist(_circuit(), xml) == 5


@pytest.mark.parametrize("groups, error", [
    ([[("U1", "1")], [("U1", "2")], [("U1", "3"), ("R1", "1")], [("R1", "2")]],
     "opens=.*RETURN"),
    ([[("U1", "1"), ("U1", "2"), ("U1", "3"), ("R1", "1")], [("R1", "2")]],
     "shorts=.*INPUT.*RETURN"),
    ([[("U1", "1")], [("U1", "3"), ("R1", "1")], [("R1", "2")]], "missing pins"),
])
def test_export_rejects_open_short_or_missing_physical_pin(groups, error):
    with pytest.raises(KiCadSchematicError, match=error):
        validate_exported_netlist(_circuit(), _netlist(groups))


def test_export_rejects_duplicated_physical_pin_instead_of_overwriting_it():
    xml = _netlist([
        [("U1", "1"), ("U1", "2")],
        [("U1", "3"), ("R1", "1")],
        [("R1", "2")],
        [("R1", "1")],
    ])
    with pytest.raises(KiCadSchematicError, match="repeats a physical pin"):
        validate_exported_netlist(_circuit(), xml)
