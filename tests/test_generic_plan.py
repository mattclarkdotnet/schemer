from copy import deepcopy
from pathlib import Path

from schemer.generic_plan import generic_layout_plan
from schemer.layout import Position
from schemer.toolchain import Toolchain, ToolchainError


def _toolchain() -> Toolchain:
    placeholder = Path("/not-used")
    return Toolchain(compiler=placeholder, extension=placeholder, chrome=placeholder)


def test_generic_plan_resets_stored_positions_and_expands_only_opaque_children(
    tmp_path: Path,
) -> None:
    entrypoint = tmp_path / "Circuit.zen"
    child_source = tmp_path / "Function.zen"
    wrapper_source = tmp_path / "Port.zen"
    root_ref = str(entrypoint) + ":<root>"
    child_ref = root_ref + ".FUNCTION"
    wrapper_ref = root_ref + ".PORT"
    schematic = {
        "root_ref": root_ref,
        "instances": {
            root_ref: {
                "kind": "Module",
                "children": {"FUNCTION": child_ref, "PORT": wrapper_ref},
                "symbol_positions": {"comp:OLD": {"x": 999, "y": 999, "rotation": 0}},
            },
            child_ref: {
                "kind": "Module",
                "children": {"IC": child_ref + ".IC"},
                "symbol_positions": {"comp:OLD": {"x": -999, "y": -999, "rotation": 0}},
                "type_ref": {"source_path": str(child_source)},
            },
            child_ref + ".IC": {
                "kind": "Component",
                "children": {},
                "reference_designator": "X1",
                "attributes": {"type": {"String": "integrated_circuit"}},
            },
            wrapper_ref: {
                "kind": "Module",
                "children": {"PHYSICAL": wrapper_ref + ".PHYSICAL"},
                "type_ref": {"source_path": str(wrapper_source)},
            },
            wrapper_ref + ".PHYSICAL": {
                "kind": "Component",
                "children": {},
                "reference_designator": "X2",
                "attributes": {"type": {"String": "connector"}},
            },
        },
        "nets": {},
        "symbols": {},
    }
    calls: list[dict] = []

    def fake_auto_place(focused: dict, _toolchain: Toolchain) -> dict[str, Position]:
        calls.append(deepcopy(focused))
        assert focused["instances"][focused["root_ref"]]["symbol_positions"] == {}
        if focused["root_ref"] == root_ref:
            return {
                "comp:FUNCTION": Position(10, 20),
                "comp:PORT.PHYSICAL": Position(-10, 20),
            }
        return {"comp:IC": Position(0, 0)}

    def unavailable_evaluator(_entrypoint: Path, _compiler: Path) -> dict:
        raise ToolchainError("fixture uses the focused fallback")

    plan = generic_layout_plan(
        entrypoint,
        schematic,
        _toolchain(),
        auto_placer=fake_auto_place,
        evaluator=unavailable_evaluator,
    )

    assert [module.instance_ref for module in plan.modules] == [root_ref, child_ref]
    assert [module.source_path for module in plan.modules] == [entrypoint, child_source]
    assert [call["root_ref"] for call in calls] == [root_ref, child_ref]
    assert schematic["instances"][root_ref]["symbol_positions"] == {
        "comp:OLD": {"x": 999, "y": 999, "rotation": 0}
    }


def test_generic_plan_generates_shared_module_source_once(tmp_path: Path) -> None:
    entrypoint = tmp_path / "Circuit.zen"
    shared_source = tmp_path / "Repeated.zen"
    root_ref = str(entrypoint) + ":<root>"
    first_ref = root_ref + ".FIRST"
    second_ref = root_ref + ".SECOND"
    schematic = {
        "root_ref": root_ref,
        "instances": {
            root_ref: {
                "children": {"FIRST": first_ref, "SECOND": second_ref},
                "symbol_positions": {},
            },
            first_ref: {
                "children": {},
                "symbol_positions": {},
                "type_ref": {"source_path": str(shared_source)},
            },
            second_ref: {
                "children": {},
                "symbol_positions": {},
                "type_ref": {"source_path": str(shared_source)},
            },
        },
        "nets": {},
        "symbols": {},
    }

    def fake_auto_place(focused: dict, _toolchain: Toolchain) -> dict[str, Position]:
        if focused["root_ref"] == root_ref:
            return {"comp:FIRST": Position(0, 0), "comp:SECOND": Position(100, 0)}
        return {"comp:PART": Position(0, 0)}

    def unavailable_evaluator(_entrypoint: Path, _compiler: Path) -> dict:
        raise ToolchainError("fixture uses the focused fallback")

    plan = generic_layout_plan(
        entrypoint,
        schematic,
        _toolchain(),
        auto_placer=fake_auto_place,
        evaluator=unavailable_evaluator,
    )

    assert [module.source_path for module in plan.modules] == [entrypoint, shared_source]


def test_generic_plan_maps_standalone_designators_to_configured_instance(tmp_path: Path) -> None:
    entrypoint = tmp_path / "Circuit.zen"
    child_source = tmp_path / "Function.zen"
    part_source = tmp_path / "Part.zen"
    root_ref = str(entrypoint) + ":<root>"
    child_ref = root_ref + ".FUNCTION"
    standalone_root = str(child_source) + ":<root>"
    component_type = {"source_path": str(part_source), "module_name": "PART"}
    schematic = {
        "root_ref": root_ref,
        "instances": {
            root_ref: {"children": {"FUNCTION": child_ref}, "symbol_positions": {}},
            child_ref: {
                "children": {"R101": child_ref + ".R101"},
                "symbol_positions": {},
                "type_ref": {"source_path": str(child_source)},
            },
            child_ref + ".R101.PART": {
                "kind": "Component",
                "reference_designator": "R101",
                "type_ref": component_type,
            },
        },
        "nets": {},
        "symbols": {},
    }
    standalone = {
        "root_ref": standalone_root,
        "instances": {
            standalone_root: {"children": {}, "symbol_positions": {}},
            standalone_root + ".R1.PART": {
                "kind": "Component",
                "reference_designator": "R1",
                "type_ref": component_type,
            },
        },
        "nets": {},
        "symbols": {},
    }

    def fake_evaluator(path: Path, _compiler: Path) -> dict:
        assert path == child_source
        return deepcopy(standalone)

    def fake_auto_place(focused: dict, _toolchain: Toolchain) -> dict[str, Position]:
        if focused["root_ref"] == root_ref:
            return {"comp:FUNCTION": Position(0, 0)}
        return {
            "comp:R1.PART": Position(50, 60),
            "sym:SIGNAL#0": Position(10, 60),
        }

    plan = generic_layout_plan(
        entrypoint,
        schematic,
        _toolchain(),
        auto_placer=fake_auto_place,
        evaluator=fake_evaluator,
    )

    assert plan.modules[1].positions == {
        "comp:R101.PART": Position(50, 60),
        "sym:SIGNAL#0": Position(10, 60),
    }
