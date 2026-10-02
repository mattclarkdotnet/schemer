from __future__ import annotations

import os
from pathlib import Path

from tests.paths import TESTS

FIXTURE = TESTS / "fixtures" / "package_projection" / "active_connector" / "IsolationBlocks.zen"


ABX_WORKSPACE = Path(
    os.environ.get(
        "SCHEMER_ABX_WORKSPACE",
        str(TESTS / "fixtures/sample-board"),
    )
)


DIGITAL_ABX = ABX_WORKSPACE / "boards" / "sample-board" / "SampleBoard.zen"


SPDIF_INTERFACE = DIGITAL_ABX.with_name("SpdifInterfaces.zen")


PARALLEL_TRANSFORMER = (
    TESTS / "fixtures" / "interface_blocks" / "parallel_transformer" / "ParallelTransformer.zen"
)


PARALLEL_TRANSFORMER_EXPECTED = PARALLEL_TRANSFORMER.with_name("expected-layout.json")


def _position_geometry(schematic: dict[str, object], module_ref: str) -> list[tuple[object, ...]]:
    instances = schematic["instances"]
    assert isinstance(instances, dict)
    module = instances[module_ref]
    assert isinstance(module, dict)
    positions = module["symbol_positions"]
    assert isinstance(positions, dict)
    return sorted(
        (
            float(position["x"]),
            float(position["y"]),
            float(position.get("rotation", 0.0)),
            position.get("mirror"),
        )
        for position in positions.values()
        if isinstance(position, dict)
    )
