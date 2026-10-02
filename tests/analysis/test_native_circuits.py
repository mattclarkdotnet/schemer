from __future__ import annotations

import pytest

from schemer.analysis.circuits import (
    component_layout_groups,
    power_flow_edges,
    power_stage_order,
    validate_owned_networks,
)
from schemer.core.errors import KiCadSchematicError
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("role,extra", [
    ("shunt", {"return_pin": "1"}),
    ("pin-bridge", {"other_pin": "1"}),
    ("series", {"order": 0}),
])
def test_owned_network_intent_validates_exact_owner_terminal_nets(role, extra):
    schematic, _ = _support_fixture(role)
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"]["String"] = (
        '(symbol "P" (pin (name "SIG") (number "2")) (pin (name "1") (number "1")))'
    )
    props = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
    props.update(extra)
    validate_owned_networks(schematic)
    props["pin"] = "MISSING"
    with pytest.raises(KiCadSchematicError, match="invalid owner pin"):
        validate_owned_networks(schematic)


def test_each_connector_is_a_local_block_even_inside_a_mixed_module():
    def component(reference, kind):
        return {"reference_designator": reference, "attributes": {"type": {"String": kind}}}

    schematic = {"root_ref": "root", "instances": {
        "root.POWER.U1.IC": component("U1", "ic"),
        "root.POWER.J1.PORT": component("J1", "connector"),
        "root.POWER.J2.PORT": component("J2", "connector"),
        "root.POWER.R1.R": component("R1", "resistor"),
        "root.POWER.R2.R": component("R2", "resistor"),
        "root.POWER.R1": {"attributes": {"schematic_properties": {"Json": {
            "role": "pulldown", "owner": "J1", "pin": "CC", "group": "bias",
        }}}},
    }}
    groups, connectors = component_layout_groups(schematic)
    assert connectors == {"POWER.J1.PORT", "POWER.J2.PORT"}
    assert groups["root.POWER.R1.R"] == groups["root.POWER.J1.PORT"]
    assert groups["root.POWER.R2.R"] == groups["root.POWER.U1.IC"] == "POWER"


def test_power_stages_follow_pin_directions_and_preserve_authored_support_ownership():
    # Deliberately use reverse reference/path order and arbitrary net names.
    physical = {}
    for name, designator in (("Z", "U9"), ("M", "U3"), ("A", "U1")):
        physical[f"root.BLOCK.{name}"] = {"reference_designator": designator, "attributes": {
            "__symbol_value": {"String": '(symbol "P" '
                '(pin power_in line (name "IN") (number "1")) '
                '(pin power_out line (name "OUT") (number "2")))'},
        }}
    physical["root.BLOCK.CAP"] = {"reference_designator": "C1", "attributes": {
        "schematic_properties": {"Json": {"role": "bypass", "owner": "U3",
                                             "pin": "IN", "group": "input"}},
    }}
    schematic = {"root_ref": "root", "instances": physical, "nets": {
        "x": {"name": "X", "kind": "Power", "ports": ["root.BLOCK.Z.OUT", "root.BLOCK.M.IN"]},
        "y": {"name": "Y", "kind": "Power", "ports": ["root.BLOCK.M.OUT", "root.BLOCK.A.IN"]},
    }}
    groups, connectors = component_layout_groups(schematic)
    assert not connectors
    assert groups["root.BLOCK.CAP"] == "BLOCK.M"
    assert power_stage_order(schematic, groups) == {"BLOCK": ("BLOCK.Z", "BLOCK.M", "BLOCK.A")}
    assert power_flow_edges(schematic) == {
        ("root.BLOCK.Z", "root.BLOCK.M"), ("root.BLOCK.M", "root.BLOCK.A"),
    }


def test_single_connector_module_keeps_its_authored_block_name():
    schematic = {"root_ref": "root", "instances": {
        "root.PANEL.J1.PORT": {"reference_designator": "J1", "attributes": {
            "type": {"String": "connector"},
        }},
    }}
    groups, connectors = component_layout_groups(schematic)
    assert groups == {"root.PANEL.J1.PORT": "PANEL"}
    assert connectors == {"PANEL"}


def test_owned_support_in_other_source_module_uses_its_actual_owners_group():
    schematic = {"root_ref": "root", "instances": {
        "root.LOGIC.U.IC": {"reference_designator": "U8"},
        "root.SUPPLY.C.C": {"reference_designator": "C3", "attributes": {
            "schematic_properties": {"Json": {
                "role": "bypass", "owner": "U8", "pin": "VDD", "group": "supply",
            }},
        }},
    }, "nets": {}}
    groups, _ = component_layout_groups(schematic)
    assert groups["root.SUPPLY.C.C"] == groups["root.LOGIC.U.IC"] == "LOGIC"


def test_authored_circuits_are_local_without_repetition_or_pin_count_rules():
    from copy import deepcopy

    def component(ref, kind, owner=None):
        attributes = {"type": {"String": kind}}
        if owner:
            attributes["schematic_properties"] = {"Json": {
                "role": "support", "group": "local-function", "owner": owner,
            }}
        return {"reference_designator": ref, "attributes": attributes}

    schematic = {"root_ref": "root", "instances": {
        "root.CIRCUIT.A": component("U1", "ic"),
        "root.CIRCUIT.B": component("K1", "relay"),
        "root.CIRCUIT.Q": component("Q1", "transistor", "U1"),
        "root.CIRCUIT.R": component("R1", "resistor", "Q1"),
        "root.ELSEWHERE.C": component("C1", "capacitor", "K1"),
        "root.CIRCUIT.UNASSIGNED": component("Q2", "transistor"),
    }, "nets": {}}
    original = deepcopy(schematic)
    groups, _ = component_layout_groups(schematic)
    assert groups["root.CIRCUIT.A"] == groups["root.CIRCUIT.Q"] == groups["root.CIRCUIT.R"]
    assert groups["root.CIRCUIT.B"] == groups["root.ELSEWHERE.C"]
    assert groups["root.CIRCUIT.A"] != groups["root.CIRCUIT.B"]
    assert groups["root.CIRCUIT.UNASSIGNED"] == "CIRCUIT"
    assert schematic == original


def test_authored_groups_keep_discretes_together_and_are_scoped_to_their_circuit():
    instances = {}
    for module in ("A", "B"):
        for name, kind in (("Q", "transistor"), ("R", "resistor")):
            instances[f"root.{module}.{name}"] = {"attributes": {
                "schematic_properties": {"Json": {"role": "support", "group": "mute"}},
            }}
            instances[f"root.{module}.{name}.PART"] = {
                "reference_designator": f"{name}{module}",
                "attributes": {"type": {"String": kind}},
            }
    schematic = {"root_ref": "root", "instances": instances, "nets": {}}
    groups, _ = component_layout_groups(schematic)
    assert groups["root.A.Q.PART"] == groups["root.A.R.PART"]
    assert groups["root.B.Q.PART"] == groups["root.B.R.PART"]
    assert groups["root.A.Q.PART"] != groups["root.B.Q.PART"]


def test_independent_representation_separates_owners_but_keeps_their_support():
    from schemer.analysis.circuits import direct_circuit_groups

    schematic = {"root_ref": "root", "instances": {
        "root.NETWORK": {"kind": "Module", "attributes": {
            "schematic_properties": {"Json": {"representation": "independent-blocks"}},
        }},
        "root.NETWORK.A.IC": {"reference_designator": "U8"},
        "root.NETWORK.B.IC": {"reference_designator": "U9"},
        "root.ELSEWHERE.C.C": {"reference_designator": "C3", "attributes": {
            "schematic_properties": {"Json": {
                "role": "bypass", "owner": "U8", "pin": "VDD", "group": "supply",
            }},
        }},
        "root.UNRELATED.X": {"reference_designator": "U20"},
        "root.UNRELATED.Y": {"reference_designator": "U21"},
    }, "nets": {}}
    groups, _ = component_layout_groups(schematic)
    assert groups["root.NETWORK.A.IC"] == groups["root.ELSEWHERE.C.C"] == "NETWORK.A.IC"
    assert groups["root.NETWORK.B.IC"] == "NETWORK.B.IC"
    assert groups["root.UNRELATED.X"] == groups["root.UNRELATED.Y"] == "UNRELATED"
    assert direct_circuit_groups(schematic) == {groups["root.NETWORK.A.IC"],
                                                 groups["root.NETWORK.B.IC"]}


def test_connected_representation_joins_circuits_without_changing_ownership():
    from copy import deepcopy

    schematic = {"root_ref": "root", "instances": {
        "root.NETWORK": {"kind": "Module", "attributes": {
            "schematic_properties": {"Json": {"representation": "connected-circuit"}},
        }},
        "root.NETWORK.A.IC": {"reference_designator": "U8"},
        "root.NETWORK.B.IC": {"reference_designator": "U9"},
        "root.ELSEWHERE.C.C": {"reference_designator": "C3", "attributes": {
            "schematic_properties": {"Json": {
                "role": "bypass", "owner": "U8", "pin": "VDD", "group": "supply",
            }},
        }},
        "root.NETWORK.PORT": {"reference_designator": "J1", "attributes": {
            "type": {"String": "connector"},
        }},
        "root.UNRELATED.X": {"reference_designator": "U20"},
    }, "nets": {}}
    original = deepcopy(schematic)
    groups, connectors = component_layout_groups(schematic)
    joined = groups["root.NETWORK.A.IC"]
    assert groups["root.NETWORK.B.IC"] == groups["root.ELSEWHERE.C.C"] == joined
    assert groups["root.NETWORK.PORT"] != joined
    assert connectors == {groups["root.NETWORK.PORT"]}
    assert groups["root.UNRELATED.X"] != joined
    assert schematic == original
    schematic["instances"]["root.NETWORK.B"] = {"kind": "Module", "attributes": {
        "schematic_properties": {"Json": {"representation": "independent-blocks"}},
    }}
    groups, _ = component_layout_groups(schematic)
    assert groups["root.NETWORK.B.IC"] != groups["root.NETWORK.A.IC"]


@pytest.mark.parametrize("kind", ["connector", "optical_receiver", "optical_transmitter",
                                  "test_point"])
def test_external_interface_group_uses_type_not_reference_prefix(kind):
    schematic = {"root_ref": "root", "instances": {
        "root.FUNCTION.PORT": {"reference_designator": "U9", "attributes": {
            "type": {"String": kind},
        }},
        "root.FUNCTION.CAP": {"reference_designator": "C8", "attributes": {
            "schematic_properties": {"Json": {
                "role": "bypass", "owner": "U9", "pin": "VDD", "group": "supply",
            }},
        }},
        "root.FUNCTION.LOGIC": {"reference_designator": "J7", "attributes": {
            "type": {"String": "ic"},
        }},
    }}
    groups, interfaces = component_layout_groups(schematic)
    assert interfaces == {"FUNCTION.PORT"}
    assert groups["root.FUNCTION.CAP"] == groups["root.FUNCTION.PORT"]
    assert groups["root.FUNCTION.LOGIC"] == "FUNCTION"


def test_owned_testpoint_stays_with_its_circuit_not_the_external_interface_row():
    schematic = {"root_ref": "root", "instances": {
        "root.A.IC": {"reference_designator": "U8"},
        "root.A.ACCESS": {"reference_designator": "X1", "attributes": {
            "type": {"String": "test_point"},
            "schematic_properties": {"Json": {
                "role": "support", "group": "probe", "owner": "U8"}}}},
        "root.B.ACCESS": {"reference_designator": "X2", "attributes": {
            "type": {"String": "test_point"}}},
    }, "nets": {}}
    groups, interfaces = component_layout_groups(schematic)
    assert groups["root.A.ACCESS"] == groups["root.A.IC"]
    assert groups["root.A.IC"] not in interfaces
    assert groups["root.B.ACCESS"] in interfaces


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("pin_count,kind,excluded,expected", [
    (1, "passive", True, True), (2, "passive", True, False),
    (1, "input", True, False), (1, "passive", False, False),
])
def test_untyped_access_pad_is_identified_by_its_electrical_assembly_contract(
    owned, pin_count, kind, excluded, expected,
):
    from schemer.analysis.circuits import is_external_interface

    symbol = '(symbol "Anything" ' + " ".join(
        f'(pin {kind} line (name "P{i}") (number "{i}"))'
        for i in range(pin_count)) + ')'
    attributes = {"__symbol_value": {"String": symbol},
                  "skip_bom": {"Boolean": excluded}, "skip_pos": {"Boolean": excluded}}
    if owned:
        attributes["schematic_properties"] = {"Json": {
            "role": "support", "owner": "U1", "group": "probe"}}
    schematic = {"root_ref": "root", "instances": {
        "root.ACCESS": {"reference_designator": "X3", "attributes": attributes}}}
    assert is_external_interface(schematic, "root.ACCESS") == (expected and not owned)
