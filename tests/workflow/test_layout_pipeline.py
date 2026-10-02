from __future__ import annotations

import pytest

from schemer.analysis.measurements import top_level_root_symbol_groups
from schemer.analysis.topology import connectivity_digest
from schemer.cli.main import main
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener
from tests.paths import TESTS


@pytest.mark.e2e
def test_final_packing_does_not_leave_root_rail_copies_in_completed_children(tmp_path, monkeypatch):
    workspace = (TESTS / "fixtures/sample-board")
    source = workspace / "boards/sample-board/SampleBoard.zen"
    if not DEFAULT_PCB_COMPILER.is_file() or not source.is_file():
        pytest.skip("frozen integration corpus unavailable")
    destination = tmp_path / "proposal"
    # This frozen geometry regression does not attest source-review coverage.
    # CLI review enforcement is tested separately in test_preparation.py.
    monkeypatch.setattr("schemer.workflow.proposal.require_preparation", lambda *_: None)
    assert main(["layout", str(source), "--proposal-dir", str(destination)]) == 0
    compiled = evaluate_zener(destination / source.relative_to(workspace), DEFAULT_PCB_COMPILER)
    root = compiled["root_ref"]
    owners = top_level_root_symbol_groups(compiled)
    for symbol_id in compiled["instances"][root]["symbol_positions"]:
        if not symbol_id.startswith("sym:"):
            continue
        owner = owners.get(symbol_id)
        child = compiled["instances"].get(f"{root}.{owner}", {})
        child_positions = child.get("symbol_positions", {})
        if not child_positions:
            continue
        prefix = symbol_id.rsplit("#", 1)[0] + "#"
        assert not any(key.startswith(prefix) for key in child_positions), symbol_id
    # Top-level connector supplies remain; cleanup is not wholesale deletion.
    assert any(key.startswith("sym:") for key in compiled["instances"][root]["symbol_positions"])
    assert connectivity_digest(compiled) == connectivity_digest(
        evaluate_zener(source, DEFAULT_PCB_COMPILER),
    )
