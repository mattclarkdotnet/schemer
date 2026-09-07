import ast
from copy import deepcopy
from pathlib import Path

import pytest

from schemer.block_generation import generate_functional_ic_blocks
from schemer.heuristic_block import (
    _Component,
    _has_straight_bundle,
    _SeriesLink,
    _SeriesWire,
)
from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.shadow import materialize_proposal_shadow
from schemer.signal_terminations import (
    annotate_net_calls,
    signal_termination_sources,
    with_signal_termination_symbols,
)
from schemer.symbol_geometry import _attribute_string, _terminal_nets, pin_positions
from schemer.toolchain import (
    DEFAULT_PCB_COMPILER,
    ToolchainError,
    connectivity_digest,
    evaluate_zener,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_symbol_projection_preserves_arguments_comments_and_layout():
    source = '''# α Unicode before the constructor.
DATA = Net(
    "DATA", impedance="90ohm",  # preserve this comment
)
OTHER = Net()
# pcb:sch DATA.0 x=1 y=2 rot=90
'''
    result = annotate_net_calls(source, {"DATA"})
    ast.parse(result)
    assert '"DATA", impedance="90ohm",  # preserve this comment' in result
    assert result.endswith('# pcb:sch DATA.0 x=1 y=2 rot=90\n')
    assert "OTHER = Net()" in result
    assert annotate_net_calls(result, {"DATA"}) == result


@pytest.mark.parametrize("source", ["DATA = io(Net)\n", "OTHER = Net()\n"])
def test_unsupported_bindings_fail_instead_of_guessing(source):
    with pytest.raises(ToolchainError):
        annotate_net_calls(source, {"DATA"})


@pytest.mark.e2e
def test_named_interface_is_compiled_presentation_not_disconnected_nets(tmp_path):
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local compiler unavailable")
    source = FIXTURES / "package_projection/active_connector/OpposedChannels.zen"
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(
        schematic, LayoutPlan((ModuleLayout(root, source, {}),)),
    )
    preview = result.plan.apply_to_schematic(
        with_signal_termination_symbols(schematic, result.plan),
    )
    positions = result.plan.modules[0].positions
    for name in ("IC_INPUT", "IC_A", "IC_B"):
        assert sum(key.startswith(f"sym:{name}#") for key in positions) == 2
        assert preview["nets"][name]["properties"]["symbol_name"] == "SignalTermination"
    for ref, terminal in (("R_A.R", "AUX_A"), ("R_B.R", "SIGNAL")):
        passive = pin_positions(schematic["instances"][root + "." + ref],
                                positions["comp:" + ref], "2")[0]
        connector = pin_positions(schematic["instances"][root + ".SINK"],
                                  positions["comp:SINK"], terminal)[0]
        assert passive.y == pytest.approx(connector.y)
    updates = result.plan.proposed_sources()
    shadow = materialize_proposal_shadow(source, updates, tmp_path / "proposal",
        file_overrides=signal_termination_sources(schematic, result.plan, updates))
    compiled = evaluate_zener(shadow.entrypoint, DEFAULT_PCB_COMPILER)
    assert connectivity_digest(compiled) == connectivity_digest(schematic)
    for name in ("IC_INPUT", "IC_A", "IC_B"):
        assert _attribute_string(
            {"attributes": compiled["nets"][name]["properties"]}, "symbol_name",
        ) == "SignalTermination"
        assert len(compiled["nets"][name]["ports"]) == len(schematic["nets"][name]["ports"])


@pytest.mark.e2e
def test_straight_bundle_needs_three_distinct_one_to_one_rows():
    source = FIXTURES / "interface_blocks/parallel_transformer/ParallelTransformer.zen"
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local compiler unavailable")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    ref = schematic["root_ref"] + ".DRIVER"
    instance = schematic["instances"][ref]
    original = deepcopy(instance)
    connector = _Component(ref, "comp:DRIVER", instance, "connector",
                           _terminal_nets(ref, instance, schematic["nets"]))
    wires = []
    for terminal in ("OUT_A", "OUT_B", "OUT_C"):
        point = pin_positions(instance, Position(0, 0), terminal)[0]
        net_ref = connector.terminals[terminal][0]
        link = _SeriesLink(connector, connector, terminal, net_ref, terminal, "1", "2")
        wires.append(_SeriesWire(link, "right", point, point))
    assert not _has_straight_bundle(connector, Position(0, 0), tuple(wires[:1]))
    assert not _has_straight_bundle(connector, Position(0, 0), tuple(wires[:2]))
    assert _has_straight_bundle(connector, Position(0, 0), tuple(wires))
    assert not _has_straight_bundle(connector, Position(0, 25), tuple(wires))
    assert not _has_straight_bundle(connector, Position(0, 0), (wires[0],) * 3)
    assert instance == original
