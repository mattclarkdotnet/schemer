from __future__ import annotations

import pytest

from schemer.analysis.roles import (
    component_roles,
    module_functions,
    module_representations,
    module_sheets,
)
from schemer.core.errors import ToolchainError
from schemer.placement.circuits.model import Component


def read_role(payload):
    instance = {"attributes": {"schematic_properties": {"Json": payload}}}
    return component_roles({"root.PART": instance}, (
        Component("root.PART", "comp:PART", instance, "", {}),
    ))[0]


@pytest.mark.parametrize("payload", [
    {"role": "series", "owner": "U1", "pin": "OUT", "order": 0},
    {"role": "shunt", "owner": "U1", "pin": "SENSE", "return_pin": "REF"},
    {"role": "shunt", "owner": "U1", "at": "FILTERED", "return_pin": "REF"},
    {"role": "pin-bridge", "owner": "U1", "pin": "BOOST", "other_pin": "SW"},
    {"role": "gain-setting", "owner": "U1", "pin": "S1"},
])
def test_owned_network_role_schema_preserves_durable_attachments(payload):
    role = read_role({"group": "network", **payload})
    assert role.owner == "U1"
    for name, value in payload.items():
        assert getattr(role, "kind" if name == "role" else name) == value


@pytest.mark.parametrize("payload", [
    {"role": "series", "owner": "U1", "order": 0},
    {"role": "shunt", "owner": "U1", "pin": "IN", "at": "NODE"},
    {"role": "shunt", "at": "NODE", "return_pin": "REF"},
    {"role": "shunt", "owner": 3, "pin": "IN"},
    {"role": "pin-bridge", "owner": "U1", "pin": "BOOST"},
    {"role": "bypass", "owner": "U1", "pin": "VDD", "other_pin": "SW"},
    {"role": "gain-setting", "owner": "U1", "pin": "S1", "order": 0},
])
def test_owned_network_role_schema_rejects_ambiguous_attachments(payload):
    with pytest.raises(ToolchainError):
        read_role({"group": "network", **payload})


@pytest.mark.parametrize("choice", ["independent-blocks", "connected-circuit"])
def test_module_representation_is_separate_from_function_and_sheet(choice):
    instances = {"root.NETWORK": {"kind": "Module", "attributes": {
        "schematic_properties": {"Json": {
            "function": "switched-network", "sheet": "Network",
            "representation": choice,
        }},
    }}}
    assert module_functions(instances)[0].function == "switched-network"
    assert module_sheets(instances) == {"root.NETWORK": "Network"}
    assert module_representations(instances) == {"root.NETWORK": choice}


@pytest.mark.parametrize("choice", [None, 3, {}, "ladder", "right-of"])
def test_module_representation_rejects_unsupported_choices(choice):
    instances = {"root.NETWORK": {"kind": "Module", "attributes": {
        "schematic_properties": {"Json": {"representation": choice}},
    }}}
    with pytest.raises(ToolchainError, match="representation"):
        module_representations(instances)
