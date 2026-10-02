from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from schemer.analysis.roles import ModuleFunction, RoleSource, module_functions
from schemer.analysis.topology import connectivity_digest
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener
from schemer.placement.builders.authored import role_directed_series_block
from schemer.placement.builders.general import general_local_blocks
from schemer.placement.circuits.model import NetSymbolAttachment
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    net_symbol_drawing_envelope,
    rail_drawing_envelope,
)
from schemer.placement.circuits.queries import (
    collect_components,
    component_drawing_envelope,
    drawings_overlap,
)
from schemer.placement.circuits.support import (
    place_authored_inline_parts,
    place_authored_power_feeds,
)
from schemer.placement.pipeline import generate_functional_ic_blocks
from schemer.symbols.geometry import pin_outward_side, pin_positions, placed_symbol_bounds
from schemer.symbols.net_symbols import net_symbol_pin_position
from schemer.symbols.signal_termination import SYMBOL as SIGNAL_TERMINATION_SYMBOL
from tests.paths import TESTS

FIXTURE = TESTS / "fixtures/interface_blocks/parallel_transformer/GeneralBlocks.zen"


@pytest.mark.e2e
def test_functional_pads_and_links_survive_both_visibility_and_seed_placement():
    from schemer.analysis.visibility import electrical_view
    from schemer.workflow.native_project import _seed_sheet

    source = FIXTURE.with_name("FunctionalAccess.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    visible = electrical_view(schematic)
    seeded = _seed_sheet(visible, source)
    positions = seeded["instances"][seeded["root_ref"]]["symbol_positions"]
    assert "comp:INPUT_PAD.TP" in positions
    assert "comp:LINK.SJ" in positions
    assert "comp:PROBE.TP" not in positions


def test_generated_net_position_ids_keep_scope_even_when_captions_can_be_shortened():
    symbols = NetSymbols()
    positions = {}
    for name in ("FIRST.SIGNAL", "SECOND.SIGNAL"):
        symbols.add(positions, name, {"name": name, "kind": "Net"}, Position(0, 0))
    assert set(positions) == {"sym:FIRST.SIGNAL#0", "sym:SECOND.SIGNAL#0"}


@pytest.mark.e2e
@pytest.mark.parametrize("shared_owner", [False, True])
def test_shared_supply_does_not_override_explicit_bypass_owners(shared_owner):
    schematic = evaluate_zener(FIXTURE, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    nets = schematic["nets"]
    first = next(n for n in nets.values() if n["name"] == "VDD_A")
    second_key = next(key for key, n in nets.items() if n["name"] == "VDD_B")
    first["ports"].extend(nets.pop(second_key)["ports"])
    for name in ("FIRST", "SECOND"):
        owner_name = "FIRST" if shared_owner else name
        owner = schematic["instances"][root + "." + owner_name]["reference_designator"]
        schematic["instances"][root + "." + name + "_BYPASS"]["attributes"][
            "schematic_properties"
        ] = {"Json": {"role": "bypass", "group": "supply", "owner": owner, "pin": "PRI"}}
    before = deepcopy(schematic)
    block = general_local_blocks(schematic, ModuleLayout(root, FIXTURE, {}))
    assert block is not None
    assert len([name for name in block.positions() if name.startswith("comp:")]) == 9
    assert schematic == before


def test_owned_series_support_is_not_mistaken_for_a_standalone_passive_sheet():
    support = RoleSource("root.R", {"attributes": {"schematic_properties": {"Json": {
        "role": "series", "group": "output-filter", "owner": "U1", "pin": "SW", "order": 0,
    }}}})
    owner = RoleSource("root.U", {"reference_designator": "U1"})
    assert role_directed_series_block({}, (support, owner), padding=40) is None


@pytest.mark.e2e
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("blocked", [False, True])
def test_gain_setting_branch_uses_its_authored_switch_pin(rotation, blocked):
    source = FIXTURE.with_name("PowerFeed.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    module = ModuleLayout(schematic["root_ref"], source, {})
    instances, _, components = collect_components(schematic, module)
    owner = next(c for c in components if c.instance.get("reference_designator") == "U1")
    branch = next(c for c in components if c.instance.get("reference_designator") == "R1")
    branch.instance["attributes"]["schematic_properties"] = {"Json": {
        "role": "gain-setting", "group": "conductance-bank", "owner": "U1", "pin": "SEC",
    }}
    before = deepcopy(schematic)
    positions = {c.symbol_id: Position(0, 0) for c in components}
    positions[owner.symbol_id] = Position(100, 200, rotation)
    placed, _ = place_authored_inline_parts(instances, components, positions)
    if blocked:
        instance = deepcopy(branch.instance)
        instance["reference_designator"] = "R99"
        instance["attributes"].pop("schematic_properties", None)
        obstacle = replace(branch, ref=branch.ref + "_OBSTACLE", symbol_id="comp:obstacle",
                           instance=instance)
        components = (*components, obstacle)
        positions[obstacle.symbol_id] = Position(0, 0)
        target = component_drawing_envelope(branch, positions[branch.symbol_id])
        box = component_drawing_envelope(obstacle, positions[obstacle.symbol_id])
        old = positions[obstacle.symbol_id]
        positions[obstacle.symbol_id] = Position(
            old.x + (target.min_x + target.max_x - box.min_x - box.max_x) / 2,
            old.y + (target.min_y + target.max_y - box.min_y - box.max_y) / 2,
            old.rotation, old.mirror,
        )
        place_authored_inline_parts(instances, components, positions)
        assert not drawings_overlap(component_drawing_envelope(branch, positions[branch.symbol_id]),
                            component_drawing_envelope(obstacle, positions[obstacle.symbol_id]))
    terminal = next(t for t, (net, _) in branch.terminals.items()
                    if net == owner.terminals["SEC"][0])
    a = pin_positions(owner.instance, positions[owner.symbol_id], "SEC")[0]
    b = pin_positions(branch.instance, positions[branch.symbol_id], terminal)[0]
    side = pin_outward_side(owner.instance, positions[owner.symbol_id], "SEC")
    if side in {"left", "right"}:
        assert b.y == pytest.approx(a.y)
        assert (b.x - a.x) * (-1 if side == "left" else 1) > 0
    else:
        assert b.x == pytest.approx(a.x)
        assert (b.y - a.y) * (-1 if side == "top" else 1) > 0
    assert placed == {branch.ref}
    assert schematic == before


@pytest.mark.e2e
def test_module_function_is_declared_on_the_instantiation_call():
    source = FIXTURE.with_name("ModuleProperties.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    assert module_functions(schematic["instances"]) == (
        ModuleFunction(root + ".FILTER", "signal-filter"),
    )


@pytest.mark.e2e
def test_parallel_capacitors_share_rail_symbols_and_aligned_bus_pins():
    source = FIXTURE.with_name("ParallelCapacitors.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(schematic, LayoutPlan((ModuleLayout(root, source, {}),)))
    positions = result.plan.modules[0].positions
    supply, returned = [], []
    for name, supply_terminal, return_terminal in (("SMALL", "1", "2"),
                                                  ("MEDIUM", "2", "1"),
                                                  ("LARGE", "1", "2")):
        component = schematic["instances"][root + f".{name}.C"]
        position = positions[f"comp:{name}.C"]
        supply.append(pin_positions(component, position, supply_terminal)[0])
        returned.append(pin_positions(component, position, return_terminal)[0])
    assert len({round(p.y, 6) for p in supply}) == 1
    assert len({round(p.y, 6) for p in returned}) == 1
    assert all(top.y < bottom.y and top.x == pytest.approx(bottom.x)
               for top, bottom in zip(supply, returned))
    assert len({round(p.x, 6) for p in supply}) == 3
    assert len([key for key in positions if key.startswith("sym:GND#")]) == 1
    assert len([key for key in positions if key.startswith("sym:VDD#")]) == 2
    assert len([key for key in positions if key.startswith("sym:OTHER_GND#")]) == 1
    assert not result.module_blocks[0][1].findings()
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_authored_divider_taps_connect_directly_to_owner_pin_rows():
    source = FIXTURE.with_name("PassiveStructures.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(
        schematic, LayoutPlan((ModuleLayout(root, source, {}),))
    )
    positions = result.plan.modules[0].positions
    _, _, components = collect_components(schematic, ModuleLayout(root, source, {}))
    by_name = {component.symbol_id.removeprefix("comp:").split(".")[0]: component
               for component in components}

    def terminal_point(name, net_name):
        component = by_name[name]
        terminal = next(
            terminal for terminal, (_, net) in component.terminals.items()
            if str(net.get("name", "")).rsplit(".", 1)[-1] == net_name
        )
        return pin_positions(component.instance, positions[component.symbol_id], terminal)[0]

    owner = by_name["PROTECTION"]
    for tap, terminal, upstream, downstream in (
        ("SENSE_A", "SEC", "LADDER_2", "LADDER_3"),
        ("SENSE_B", "SEC_RET", "LADDER_4", "LADDER_5"),
    ):
        owner_point = pin_positions(
            owner.instance, positions[owner.symbol_id], terminal,
        )[0]
        points = (
            terminal_point(upstream, tap),
            terminal_point(downstream, tap),
            owner_point,
        )
        assert len({round(point.y, 6) for point in points}) == 1
        if pin_outward_side(owner.instance, positions[owner.symbol_id], terminal) == "left":
            assert max(point.x for point in points[:-1]) < owner_point.x
        else:
            assert min(point.x for point in points[:-1]) > owner_point.x

    bypass = by_name["BYPASS"]
    bypass_position = positions[bypass.symbol_id]
    bypass_supply = terminal_point("BYPASS", "VDD_A")
    bypass_return = terminal_point("BYPASS", "GND_A")
    owner_bounds = placed_symbol_bounds(owner.instance, positions[owner.symbol_id])
    bypass_bounds = placed_symbol_bounds(bypass.instance, bypass_position)
    assert bypass_supply.x == pytest.approx(bypass_return.x)
    assert bypass_supply.y < bypass_return.y
    assert bypass_bounds.max_y < owner_bounds.min_y
    # One divider supply termination plus one local bypass termination. The
    # owner must not add a third overlapping copy of the same rail label.
    assert sum(symbol_id.startswith("sym:VDD_A#") for symbol_id in positions) == 2

    feedback_top = terminal_point("FB_TOP", "FEEDBACK")
    feedback_bottom = terminal_point("FB_BOTTOM", "FEEDBACK")
    assert feedback_top.x == pytest.approx(feedback_bottom.x)
    assert feedback_top.y < feedback_bottom.y
    assert (feedback_top.y + feedback_bottom.y) / 2 == pytest.approx(
        terminal_point("REGULATOR", "FEEDBACK").y
    )

    supply = terminal_point("OUTPUT", "VDD_B")
    returned = terminal_point("OUTPUT", "GND_B")
    assert supply.x == pytest.approx(returned.x)
    assert supply.y < returned.y
    assert not result.module_blocks[0][1].findings()
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_authored_component_roles_define_series_order_and_shunt_nodes():
    source = FIXTURE.with_name("AuthoredRoles.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    module = ModuleLayout(root, source, {})
    result = generate_functional_ic_blocks(schematic, LayoutPlan((module,)))
    block = result.module_blocks[0][1]
    assert block.root.block_id == "authored-series-group"
    positions = result.plan.modules[0].positions
    _, _, components = collect_components(schematic, module)
    by_name = {
        component.symbol_id.removeprefix("comp:").split(".")[0]: component
        for component in components
    }

    def terminal_point(name, net_name):
        component = by_name[name]
        terminal = next(
            terminal for terminal, (_, net) in component.terminals.items()
            if str(net.get("name", "")).rsplit(".", 1)[-1] == net_name
        )
        return pin_positions(component.instance, positions[component.symbol_id], terminal)[0]

    series = ("SERIES_1", "SERIES_2", "SERIES_3")
    centres = [
        sum(point.x for terminal in by_name[name].terminals
            for point in pin_positions(
                by_name[name].instance, positions[by_name[name].symbol_id], terminal,
            )) / 2
        for name in series
    ]
    assert centres == sorted(centres)
    assert terminal_point("SERIES_1", "FILTER_1").y == pytest.approx(
        terminal_point("SERIES_2", "FILTER_1").y
    )
    assert terminal_point("SERIES_2", "FILTER_2").y == pytest.approx(
        terminal_point("SERIES_3", "FILTER_2").y
    )
    for shunt, node in (("SHUNT_1", "FILTER_1"), ("SHUNT_2", "FILTER_2"),
                        ("BIAS", "OUTPUT")):
        attached = terminal_point(shunt, node)
        other = next(
            terminal for terminal, (_, net) in by_name[shunt].terminals.items()
            if str(net.get("name", "")).rsplit(".", 1)[-1] != node
        )
        returned = pin_positions(
            by_name[shunt].instance, positions[by_name[shunt].symbol_id], other,
        )[0]
        assert attached.x == pytest.approx(returned.x)
        assert attached.y < returned.y
    final_series = by_name["SERIES_3"]
    final_output = terminal_point("SERIES_3", "OUTPUT")
    output_side = pin_outward_side(
        final_series.instance,
        positions[final_series.symbol_id],
        next(
            terminal
            for terminal, (_, net) in final_series.terminals.items()
            if str(net.get("name", "")).rsplit(".", 1)[-1] == "OUTPUT"
        ),
    )
    bias_input = terminal_point("BIAS", "OUTPUT")
    assert (terminal_point("SHUNT_1", "FILTER_1").x
            < terminal_point("SHUNT_2", "FILTER_2").x < bias_input.x)
    assert output_side == "right"
    assert bias_input.x - final_output.x == pytest.approx(12.7)
    assert not block.findings()
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_multiple_authored_shunts_at_one_node_get_distinct_clear_branches():
    source = FIXTURE.with_name("AuthoredRoles.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    _, _, components = collect_components(schematic, ModuleLayout(root, source, {}))
    original = next(c for c in components if c.symbol_id.startswith("comp:SHUNT_1."))
    instance = deepcopy(original.instance)
    instance["attributes"]["schematic_properties"] = deepcopy(
        schematic["instances"][original.ref.rsplit(".", 1)[0]]["attributes"][
            "schematic_properties"])
    duplicate = replace(original, ref=original.ref + "_parallel",
                        symbol_id=original.symbol_id + "_parallel", instance=instance)
    block = role_directed_series_block(
        schematic["instances"], (*components, duplicate), padding=40)
    positions = block.positions()
    assert not drawings_overlap(component_drawing_envelope(original, positions[original.symbol_id]),
                        component_drawing_envelope(duplicate, positions[duplicate.symbol_id]))
    next_stage = next(c for c in components if c.symbol_id.startswith("comp:SERIES_2."))
    node = next(net for net, info in original.terminals.values() if info["kind"] != "Ground")
    next_input = next(t for t, (net, _) in next_stage.terminals.items() if net == node)
    limit = pin_positions(next_stage.instance, positions[next_stage.symbol_id], next_input)[0].x
    for branch in (original, duplicate):
        terminal = next(t for t, (net, _) in branch.terminals.items() if net == node)
        assert pin_positions(branch.instance, positions[branch.symbol_id], terminal)[0].x < limit
    assert not block.findings()


@pytest.mark.e2e
def test_unannotated_equivalent_is_not_silently_given_authored_structure():
    source = FIXTURE.with_name("AuthoredRoles.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    unannotated = deepcopy(schematic)
    for instance in unannotated["instances"].values():
        attributes = instance.get("attributes")
        if isinstance(attributes, dict):
            for name in tuple(attributes):
                if name.startswith("schematic_"):
                    del attributes[name]
    root = unannotated["root_ref"]
    result = generate_functional_ic_blocks(
        unannotated, LayoutPlan((ModuleLayout(root, source, {}),))
    )
    assert result.module_blocks[0][1].root.block_id == "general-local-blocks"


@pytest.mark.e2e
def test_authored_shunt_node_must_exist_on_that_component_and_series_path():
    source = FIXTURE.with_name("AuthoredRoles.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    wrapper = schematic["instances"][root + ".SHUNT_1"]
    wrapper["attributes"]["schematic_properties"] = {
        "String": (
            '{"role":"shunt","group":"signal-path",'
            '"at":"NOT_A_REAL_NODE"}'
        )
    }
    with pytest.raises(ToolchainError, match="must name exactly one terminal"):
        generate_functional_ic_blocks(
            schematic, LayoutPlan((ModuleLayout(root, source, {}),))
        )


@pytest.mark.e2e
def test_authored_pullups_share_one_supply_bus_and_match_their_owner_pin_lanes():
    source = FIXTURE.with_name("PullupBank.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    module = ModuleLayout(root, source, {})
    result = generate_functional_ic_blocks(schematic, LayoutPlan((module,)))
    block = result.module_blocks[0][1]
    assert block.root.block_id == "primary-ic-local"
    positions = result.plan.modules[0].positions
    _, _, components = collect_components(schematic, module)
    owner = next(component for component in components
                 if component.instance.get("reference_designator") == "U1")
    pullups = {
        component.symbol_id.removeprefix("comp:").split(".")[0]: component
        for component in components
        if component.instance.get("reference_designator", "").startswith("R")
    }
    rail_xs = []
    for name, owner_pin in (("PULL_A", "IN_A"), ("PULL_B", "IN_B"),
                            ("PULL_C", "IN_C")):
        component = pullups[name]
        position = positions[component.symbol_id]
        signal_terminal = next(
            terminal for terminal, (_, net) in component.terminals.items()
            if str(net.get("name", "")).rsplit(".", 1)[-1] == f"SIGNAL_{name[-1]}"
        )
        rail_terminal = next(terminal for terminal in component.terminals
                             if terminal != signal_terminal)
        signal = pin_positions(component.instance, position, signal_terminal)[0]
        rail = pin_positions(component.instance, position, rail_terminal)[0]
        owner_point = pin_positions(
            owner.instance, positions[owner.symbol_id], owner_pin,
        )[0]
        assert signal.y == pytest.approx(owner_point.y)
        assert rail.y == pytest.approx(signal.y)
        assert rail.x < signal.x < owner_point.x
        rail_xs.append(rail.x)
    assert len({round(x, 6) for x in rail_xs}) == 1
    # One symbol feeds the pull-up bus and one terminates the IC's own PWR pin.
    assert len([key for key in positions if key.startswith("sym:VDD#")]) == 2
    assert not block.findings()
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_authored_power_feed_forms_an_inline_supply_branch_at_its_owner_pin():
    source = FIXTURE.with_name("PowerFeed.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    module = ModuleLayout(root, source, {})
    result = generate_functional_ic_blocks(schematic, LayoutPlan((module,)))
    positions = result.plan.modules[0].positions
    _, _, components = collect_components(schematic, module)
    owner = next(component for component in components
                 if component.instance.get("reference_designator") == "U1")
    feed = next(component for component in components
                if component.instance.get("reference_designator") == "R1")
    assert result.module_blocks[0][1].root.block_id == "general-local-blocks"
    owner_pin = pin_positions(owner.instance, positions[owner.symbol_id], "SEC")[0]
    feed_supply = next(
        terminal for terminal, (_, net) in feed.terminals.items()
        if str(net.get("name", "")).rsplit(".", 1)[-1] == "SOURCE"
    )
    feed_output = next(terminal for terminal in feed.terminals if terminal != feed_supply)
    supply_pin = pin_positions(
        feed.instance, positions[feed.symbol_id], feed_supply,
    )[0]
    output_pin = pin_positions(
        feed.instance, positions[feed.symbol_id], feed_output,
    )[0]
    assert supply_pin.y == pytest.approx(output_pin.y)
    assert supply_pin.x > output_pin.x
    assert output_pin.y == pytest.approx(owner_pin.y)
    assert output_pin.x > owner_pin.x
    supply_symbol = next(
        position for key, position in positions.items() if key.startswith("sym:SOURCE#")
    )
    assert supply_symbol.rotation == pytest.approx(0.0)
    assert supply_symbol.y < supply_pin.y
    assert len([key for key in positions if key.startswith("sym:VSYS#")]) == 0
    assert not result.module_blocks[0][1].findings()
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_authored_power_feed_follows_every_owner_pin_direction(rotation):
    source = FIXTURE.with_name("PowerFeed.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    module = ModuleLayout(schematic["root_ref"], source, {})
    instances, _, components = collect_components(schematic, module)
    owner = next(c for c in components if c.instance.get("reference_designator") == "U1")
    feed = next(c for c in components if c.instance.get("reference_designator") == "R1")
    positions = {c.symbol_id: Position(0, 0) for c in components}
    positions[owner.symbol_id] = Position(100, 200, rotation)
    placed, skipped = place_authored_power_feeds(instances, components, positions)
    terminal = next(t for t, (net, _) in feed.terminals.items()
                    if net == owner.terminals["SEC"][0])
    a = pin_positions(owner.instance, positions[owner.symbol_id], "SEC")[0]
    b = pin_positions(feed.instance, positions[feed.symbol_id], terminal)[0]
    side = pin_outward_side(owner.instance, positions[owner.symbol_id], "SEC")
    if side in {"left", "right"}:
        assert b.y == pytest.approx(a.y)
        assert (b.x - a.x) * (-1 if side == "left" else 1) > 0
    else:
        assert b.x == pytest.approx(a.x)
        assert (b.y - a.y) * (-1 if side == "top" else 1) > 0
    assert placed == {feed.ref}
    assert skipped[owner.ref] == {"SEC"}


@pytest.mark.e2e
def test_general_modules_have_complete_seed_independent_local_blocks():
    schematic = evaluate_zener(FIXTURE, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    module = ModuleLayout(root, FIXTURE, {})
    result = generate_functional_ic_blocks(schematic, LayoutPlan((module,)))
    assert len(result.module_blocks) == 1
    block = result.module_blocks[0][1]
    assert block.root.block_id == "general-local-blocks"
    assert not block.findings()
    positions = block.positions()
    expected = {"comp:" + ref.removeprefix(root + ".")
                for ref, instance in schematic["instances"].items()
                if instance.get("kind") == "Component"}
    assert {key for key in positions if key.startswith("comp:")} == expected
    assert len(expected) == 9
    leaves = [child.block for row in block.root.children for child in row.block.children]
    owned = [{item.symbol_id for item in leaf.items} for leaf in leaves]
    for name in ("FIRST", "SECOND"):
        assert any({f"comp:{name}", f"comp:{name}_BYPASS.C", f"comp:{name}_LOAD.R"}
                   <= group for group in owned)
    assert any({"comp:SERIES_A.R", "comp:SERIES_B.R", "comp:SHUNT.C"} <= group for group in owned)
    moved = replace(module, positions={key: Position(9000, -5000, 90) for key in expected})
    assert generate_functional_ic_blocks(schematic, LayoutPlan((moved,))) == result
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_general_blocks_clear_side_rail_captions_from_adjacent_signal_captions():
    source = (
        FIXTURE.parents[2]
        / "package_projection/ordered_perimeter/GeneralCaptionClearance.zen"
    )
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(
        schematic, LayoutPlan((ModuleLayout(root, source, {}),))
    )
    assert result.module_blocks[0][1].root.block_id == "general-local-blocks"
    positions = result.plan.modules[0].positions
    return_envelopes = [
        rail_drawing_envelope(schematic["nets"]["RETURN"], position)
        for key, position in positions.items()
        if key.startswith("sym:RETURN#")
    ]
    assert len(return_envelopes) == 2
    for name in ("LONG_STATUS_A", "LONG_STATUS_B"):
        position = next(
            value for key, value in positions.items() if key.startswith(f"sym:{name}#")
        )
        net = schematic["nets"][name]
        projected = {
            **net,
            "properties": {
                **net.get("properties", {}),
                "__symbol_value": SIGNAL_TERMINATION_SYMBOL,
            },
        }
        target = net_symbol_pin_position(projected, position)
        signal = net_symbol_drawing_envelope(NetSymbolAttachment(
            name, net, target, rotation=position.rotation, outward_side="left",
        ))
        assert all(
            rail.max_x <= signal.min_x
            or signal.max_x <= rail.min_x
            or rail.max_y <= signal.min_y
            or signal.max_y <= rail.min_y
            for rail in return_envelopes
        )


@pytest.mark.e2e
def test_shared_face_rail_trunk_sits_beyond_signal_endpoints():
    source = (
        FIXTURE.parents[2]
        / "package_projection/ordered_perimeter/SharedFaceRail.zen"
    )
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(
        schematic, LayoutPlan((ModuleLayout(root, source, {}),))
    )
    positions = result.plan.modules[0].positions
    ground_positions = [
        position for key, position in positions.items()
        if key.startswith("sym:RETURN#")
    ]
    assert len(ground_positions) == 1
    ground = net_symbol_pin_position(schematic["nets"]["RETURN"], ground_positions[0])

    signal_points = []
    for name in ("SIGNAL_A", "SIGNAL_B"):
        position = next(
            value for key, value in positions.items()
            if key.startswith(f"sym:{name}#")
        )
        projected = {
            **schematic["nets"][name],
            "properties": {"__symbol_value": SIGNAL_TERMINATION_SYMBOL},
        }
        signal_points.append(net_symbol_pin_position(projected, position))
        assert position.rotation == pytest.approx(90.0)

    assert ground.x > max(point.x for point in signal_points)
    assert ground.y > max(
        pin_positions(schematic["instances"][root + ".IC"], positions["comp:IC"], terminal)[0].y
        for terminal in ("Pin_2", "Pin_8")
    )
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_parent_packs_completed_child_without_duplicating_its_parts():
    source = FIXTURE.with_name("NestedGeneralBlocks.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    plan = LayoutPlan((ModuleLayout(root, source, {"comp:SUB": Position(9999, -9999)}),
                       ModuleLayout(root + ".SUB", FIXTURE, {})))
    result = generate_functional_ic_blocks(schematic, plan)
    assert len(result.module_blocks) == 2
    parent, child = result.plan.modules
    assert set(parent.positions) == {"comp:SUB"}
    assert len([key for key in child.positions if key.startswith("comp:")]) == 9
    blocks = dict(result.module_blocks)
    assert blocks[root].root.width >= blocks[root + ".SUB"].root.width
    assert blocks[root].root.height >= blocks[root + ".SUB"].root.height
    assert connectivity_digest(result.plan.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )
