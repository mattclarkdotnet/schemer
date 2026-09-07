from pathlib import Path

import pytest

from schemer.heuristic_block import (
    _anchor_net_symbol_attachments,
    _components,
    _place_bypasses,
)
from schemer.layout import ModuleLayout, Position
from schemer.symbol_geometry import pin_positions
from schemer.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener


@pytest.mark.e2e
def test_separated_supply_banks_have_local_bypass_and_separate_terminations():
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("compiler unavailable")
    source = Path(__file__).parent / "fixtures/package_projection/active_connector/SupplyBanks.zen"
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    _, _, components = _components(schematic, ModuleLayout(root, source, {}))
    central = next(component for component in components if component.symbol_id == "comp:IC")
    positions = {central.symbol_id: Position(0, 0)}
    used, skipped, attachments = _place_bypasses(components, central, positions[central.symbol_id],
                                                positions)
    assert len(used) == 2
    assert skipped == {"MODE_A", "ENABLE_A", "MODE_B"}
    rail_attachments = _anchor_net_symbol_attachments(
        central, positions[central.symbol_id], skipped_terminals=skipped,
    )
    for ref, terminal, rail, sign in (
        ("C_A.C", "ENABLE_A", "RAIL_A", -1), ("C_B.C", "MODE_B", "RAIL_B", 1),
    ):
        capacitor = schematic["instances"][root + "." + ref]
        cap_pin = pin_positions(capacitor, positions["comp:" + ref], "1")[0]
        owner_pin = pin_positions(central.instance, positions[central.symbol_id], terminal)[0]
        assert cap_pin.y == pytest.approx(owner_pin.y)
        assert (cap_pin.x - owner_pin.x) * sign == pytest.approx(200)
        local_supply, = [item for item in attachments if item.net_ref == rail]
        distant_supply, = [item for item in rail_attachments if item.net_ref == rail]
        assert local_supply.target.y > distant_supply.target.y
        assert local_supply.rotation == distant_supply.rotation == 0
