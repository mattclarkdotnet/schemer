import pytest

from schemer.active_projection import pin_name_restates_number
from schemer.projection_view import select_kicad_symbol
from schemer.toolchain import ToolchainError

LIBRARY = """(kicad_symbol_lib (version 20220914)
  (symbol "USED"
    (symbol "USED_1_1"
      (rectangle (start -1 -1) (end 1 1))))
  (symbol "UNUSED"
    (symbol "UNUSED_1_1"
      (rectangle (start -100 -100) (end 100 100)))))
"""


def test_projection_feedback_selects_one_outer_library_symbol() -> None:
    selected = select_kicad_symbol(LIBRARY, "USED")

    assert selected.startswith('(symbol "USED"')
    assert "USED_1_1" in selected
    assert "UNUSED" not in selected
    assert "100" not in selected


def test_projection_feedback_rejects_absent_symbol() -> None:
    with pytest.raises(ToolchainError, match="no entry 'MISSING'"):
        select_kicad_symbol(LIBRARY, "MISSING")


@pytest.mark.parametrize("name", ["4", "P4", "Pin_4", "pin 4"])
def test_redundant_pin_name_detection(name: str) -> None:
    assert pin_name_restates_number(name, "4")


def test_semantic_pin_name_is_not_redundant() -> None:
    assert not pin_name_restates_number("VBUS", "4")
