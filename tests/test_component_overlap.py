from __future__ import annotations

from schemer.quality import component_body_overlap_findings

_ROOT = "fixture.zen:<root>"
_SYMBOL = """
    (symbol "generic"
      (rectangle (start -1 -1) (end 1 1))
      (pin passive line (at 0 -3 90) (length 2) (name "A") (number "1"))
      (pin passive line (at 0 3 270) (length 2) (name "B") (number "2")))
"""


def _schematic(second_x: float) -> dict[str, object]:
    component = {
        "kind": "Component",
        "attributes": {
            "type": {"String": "resistor"},
            "__symbol_value": {"String": _SYMBOL},
        },
    }
    return {
        "root_ref": _ROOT,
        "instances": {
            _ROOT: {
                "kind": "Module",
                "symbol_positions": {
                    "comp:A": {"x": 0, "y": 0},
                    "comp:B": {"x": second_x, "y": 0},
                },
            },
            f"{_ROOT}.A": component,
            f"{_ROOT}.B": component,
        },
    }


def _disjoint_multi_unit_schematic() -> dict[str, object]:
    schematic = _schematic(second_x=50)
    instances = schematic["instances"]
    root = instances[_ROOT]
    component = instances.pop(f"{_ROOT}.A")
    instances.pop(f"{_ROOT}.B")
    root["symbol_positions"] = {
        "comp:U@A": {"x": 0, "y": 0},
        "comp:U@B": {"x": 100, "y": 0},
        "comp:R": {"x": 50, "y": 0},
    }
    instances[f"{_ROOT}.U"] = component
    instances[f"{_ROOT}.R"] = component
    return schematic


def test_zero_area_body_edge_contact_is_not_body_overlap() -> None:
    assert not component_body_overlap_findings(_schematic(second_x=22))


def test_any_positive_area_component_body_overlap_is_reported() -> None:
    findings = component_body_overlap_findings(_schematic(second_x=21))

    assert len(findings) == 1
    assert findings[0].code == "component-body-overlap"
    assert findings[0].measured == 1
    assert findings[0].threshold == 0


def test_gap_between_disjoint_multi_unit_bodies_is_not_filled_for_overlap() -> None:
    assert not component_body_overlap_findings(_disjoint_multi_unit_schematic())
