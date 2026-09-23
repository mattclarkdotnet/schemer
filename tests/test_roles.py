import pytest

from schemer.heuristic_block import _Component
from schemer.roles import component_roles
from schemer.toolchain import ToolchainError


def read_role(payload):
    instance = {"attributes": {"schematic_properties": {"Json": payload}}}
    return component_roles({"root.PART": instance}, (
        _Component("root.PART", "comp:PART", instance, "", {}),
    ))[0]


@pytest.mark.parametrize("payload", [
    {"role": "series", "owner": "U1", "pin": "OUT", "order": 0},
    {"role": "shunt", "owner": "U1", "pin": "SENSE", "return_pin": "REF"},
    {"role": "shunt", "owner": "U1", "at": "FILTERED", "return_pin": "REF"},
    {"role": "pin-bridge", "owner": "U1", "pin": "BOOST", "other_pin": "SW"},
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
])
def test_owned_network_role_schema_rejects_ambiguous_attachments(payload):
    with pytest.raises(ToolchainError):
        read_role({"group": "network", **payload})
