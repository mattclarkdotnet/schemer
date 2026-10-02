from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.native.endpoints import component_net_endpoints
from tests.support.schematic import _support_fixture


def test_component_endpoints_carry_the_full_pin_stroke_to_routing():
    schematic, editor = _support_fixture(None)
    endpoints = component_net_endpoints(schematic, editor)
    for pins in endpoints.values():
        for pin in pins:
            assert pin.stroke is not None
            assert max(pin.stroke.width, pin.stroke.height) == 1_770_000


@pytest.mark.parametrize("role,expected", [
    ("bypass", ("supply", "supply")),
    ("shunt", ("signal-tie", "signal-support")),
    ("series", ("signal-tie", "signal-support")),
    ("divider", ("signal-tie", "signal-support")),
    ("pin-bridge", ("signal-tie", "signal-support")),
    (None, ("signal-tie", "signal-tie")),
])
def test_passive_pin_rail_role_uses_authored_support_not_just_electrical_pin_type(role, expected):
    schematic, editor = _support_fixture(role)
    schematic["nets"]["sig"]["kind"] = "Power"
    schematic["nets"]["gnd"]["kind"] = "Ground"
    endpoints = component_net_endpoints(schematic, editor)
    assert tuple(p.rail_class for p in endpoints["SIG"]) == expected
    # A bypass on pin 2 does not reclassify the owner's other passive pin.
    assert endpoints["GND"][0].rail_class == "signal-tie"


@pytest.mark.parametrize("role,expected", [("shunt", "supply"), ("divider", "supply"),
                                           ("pin-bridge", "supply"),
                                           ("pullup", "signal-tie"), ("pulldown", "signal-tie")])
def test_supply_support_shares_its_owner_node_but_logic_bias_does_not(role, expected):
    schematic, editor = _support_fixture(role)
    # Model an actual native power output, not a passive contact merely
    # connected to a net whose name happens to sound like a rail.
    editor = FileSchematic.from_text(editor.get_as_string().replace(
        '(pin passive line (at 2.54 0 180)', '(pin power_out line (at 2.54 0 180)', 1))
    schematic["nets"]["sig"]["kind"] = "Power"
    pins = component_net_endpoints(schematic, editor)["SIG"]
    assert pins[0].rail_class == "supply"
    assert pins[1].rail_class == expected


@pytest.mark.parametrize("role", ["shunt", "divider", "pin-bridge"])
def test_signal_support_return_does_not_join_its_owners_supply_return(role):
    schematic, editor = _support_fixture(role)
    editor = FileSchematic.from_text(editor.get_as_string().replace(
        '(pin passive line (at -2.54 0 0)', '(pin power_in line (at -2.54 0 0)', 1))
    schematic["nets"]["gnd"]["kind"] = "Ground"
    pins = component_net_endpoints(schematic, editor)["GND"]
    assert pins[0].rail_class == "supply"
    assert pins[1].rail_class == "signal-support"


@pytest.mark.parametrize("types,units,expected", [
    (["power_in", "power_in", "passive"], 3, True),
    (["power_in", "power_in", "passive"], 1, False),
    (["power_in", "input", "passive"], 3, False),
    (["passive", "passive"], 2, False),
])
def test_passive_supply_contacts_require_a_dedicated_library_power_unit(types, units, expected):
    from schemer.native.endpoints import _is_dedicated_power_unit
    assert _is_dedicated_power_unit(types, units) is expected
