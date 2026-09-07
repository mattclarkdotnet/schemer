import os
from pathlib import Path
from statistics import median

import pytest
from PIL import Image

from schemer.heuristic_block import _PIN_EXIT_STUB
from schemer.layout import Position
from schemer.symbol_geometry import pin_positions
from schemer.toolchain import ToolchainError
from schemer.viewer import positions_from_host_messages


def test_positions_from_host_messages_uses_latest_complete_commit() -> None:
    messages = [
        {
            "type": "positions_committed",
            "positions": [{"symbol_id": "comp:OLD", "x": 1, "y": 2, "rotation": 0, "mirror": None}],
        },
        {"type": "viewer_ready"},
        {
            "type": "positions_committed",
            "positions": [
                {"symbol_id": "comp:BLOCK", "x": 10.5, "y": -2, "rotation": 90},
                {"symbol_id": "sym:RAIL#0", "x": 12, "y": 3, "rotation": 0},
            ],
        },
    ]

    assert positions_from_host_messages(messages) == {
        "comp:BLOCK": Position(10.5, -2, 90),
        "sym:RAIL#0": Position(12, 3, 0),
    }


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"type": "positions_committed", "positions": []}],
        [
            {
                "type": "positions_committed",
                "positions": [{"symbol_id": "not-a-symbol", "x": 0, "y": 0, "rotation": 0}],
            }
        ],
    ],
)
def test_positions_from_host_messages_rejects_unusable_commits(messages: object) -> None:
    with pytest.raises(ToolchainError):
        positions_from_host_messages(messages)


@pytest.mark.viewer
@pytest.mark.skipif(
    os.environ.get("SCHEMER_VIEWER_TESTS") != "1",
    reason="opt-in actual-WASM route regression; ordinary geometry tests stay browser-free",
)
@pytest.mark.parametrize("offset,straight", [(0.0, False), (_PIN_EXIT_STUB, True)])
def test_outward_exit_trunk_has_no_lower_dogleg(tmp_path: Path, offset: float, straight: bool):
    from schemer.toolchain import evaluate_zener, resolve_toolchain
    from schemer.viewer import render_schematic

    source = Path(__file__).parent / "fixtures/package_projection/active_connector/PinExit.zen"
    toolchain = resolve_toolchain()
    schematic = evaluate_zener(source, toolchain.compiler)
    module = schematic["instances"][schematic["root_ref"]]
    module["symbol_positions"]["comp:SHUNT.R"]["x"] += offset
    output = render_schematic(schematic, toolchain, tmp_path / "route.png", width=1200, height=1200)

    # This fixture has exactly one green wire. Ignore its required initial
    # horizontal exit, then check the column of every remaining wire row.
    # The uncorrected control MUST fail the straightness check, proving that
    # this is inspecting actual WASM routing, not merely endpoint coordinates.
    with Image.open(output).convert("RGB") as image:
        rows = []
        for y in range(image.height):
            xs = [x for x in range(image.width)
                  if (pixel := image.getpixel((x, y)))[1] > 70
                  and pixel[1] > pixel[0] * 1.5 and pixel[1] > pixel[2] * 1.5]
            if xs:
                rows.append((y, median(xs)))
    assert len(rows) > 100, "render is missing the expected connecting wire"
    first_y = rows[0][0] + (rows[-1][0] - rows[0][0]) * 0.1
    columns = [x for y, x in rows if y > first_y]
    assert (max(columns) - min(columns) <= 2) is straight


@pytest.mark.viewer
@pytest.mark.skipif(
    os.environ.get("SCHEMER_VIEWER_TESTS") != "1",
    reason="opt-in actual-WASM route regression",
)
@pytest.mark.parametrize("approach,one_turn", [(-40.0, False), (40.0, True)])
def test_horizontal_to_vertical_branch_has_one_turn(tmp_path, approach, one_turn):
    from schemer.toolchain import evaluate_zener, resolve_toolchain
    from schemer.viewer import render_schematic

    source = Path(__file__).parent / "fixtures/package_projection/active_connector/PinExit.zen"
    toolchain = resolve_toolchain()
    schematic = evaluate_zener(source, toolchain.compiler)
    root = schematic["root_ref"]
    positions = schematic["instances"][root]["symbol_positions"]
    out = pin_positions(schematic["instances"][root + ".OUT.R"],
                        Position(**positions["comp:OUT.R"]), "2")[0]
    shunt = pin_positions(schematic["instances"][root + ".SHUNT.R"],
                          Position(**positions["comp:SHUNT.R"]), "1")[0]
    positions["comp:SHUNT.R"]["x"] += out.x + 80 - shunt.x
    positions["comp:SHUNT.R"]["y"] += out.y + approach - shunt.y
    output = render_schematic(schematic, toolchain, tmp_path / "branch.png",
                              width=1200, height=1200)
    with Image.open(output).convert("RGB") as image:
        wide_rows = []
        for y in range(image.height):
            xs = [x for x in range(image.width)
                  if (p := image.getpixel((x, y)))[1] > 70
                  and p[1] > p[0] * 1.5 and p[1] > p[2] * 1.5]
            if xs and max(xs) - min(xs) > 15:
                wide_rows.append(y)
    # A genuine L has one horizontal band. A wrong-side approach control
    # must have more than one, proving we inspected renderer routing.
    bands = sum(i == 0 or y - wide_rows[i - 1] > 2 for i, y in enumerate(wide_rows))
    assert bands > 0, "connecting wire missing"
    assert (bands == 1) is one_turn


@pytest.mark.viewer
@pytest.mark.skipif(os.environ.get("SCHEMER_VIEWER_TESTS") != "1", reason="opt-in WASM check")
@pytest.mark.parametrize("named,degenerate", [(False, False), (True, False), (True, True)])
def test_explicit_signal_endpoints_split_wire_without_losing_local_stubs(
    tmp_path, named, degenerate,
):
    from schemer.signal_terminations import SYMBOL, SYMBOL_NAME
    from schemer.symbol_geometry import Point, position_net_symbol_pin
    from schemer.toolchain import evaluate_zener, resolve_toolchain
    from schemer.viewer import render_schematic

    source = Path(__file__).parent / "fixtures/package_projection/active_connector/PinExit.zen"
    toolchain = resolve_toolchain()
    schematic = evaluate_zener(source, toolchain.compiler)
    root = schematic["root_ref"]
    positions = schematic["instances"][root]["symbol_positions"]
    positions["comp:SHUNT.R"] = Position(280, 0, 270).as_viewer_dict()
    if named:
        glyph = SYMBOL
        if degenerate:
            glyph = glyph.replace(
                '(xy 0 0) (xy 0 1.27) (xy -0.127 1.27) (xy 0.127 1.27)',
                '(xy 0 0) (xy 0 1.27)',
            )
        net = schematic["nets"]["COMMON"]
        net["properties"] = {
            "__symbol_value": {"String": glyph}, "symbol_name": {"String": SYMBOL_NAME},
        }
        for i, (ref, terminal, direction, rotation) in enumerate((
            ("OUT.R", "2", 1, 90), ("SHUNT.R", "1", -1, 270),
        )):
            point = pin_positions(schematic["instances"][root + "." + ref],
                                  Position(**positions["comp:" + ref]), terminal)[0]
            positions[f"sym:COMMON#{i}"] = position_net_symbol_pin(
                net, Point(point.x + direction * 60, point.y), rotation=rotation,
            ).as_viewer_dict()
    output = render_schematic(schematic, toolchain, tmp_path / "named.png", width=1600, height=800)
    with Image.open(output).convert("RGB") as image:
        columns = []
        wire_rows = set()
        for x in range(image.width):
            rows = [y for y in range(image.height)
                    if (p := image.getpixel((x, y)))[1] > 70
                    and p[1] > p[0] * 1.5 and p[1] > p[2] * 1.5]
            if rows:
                wire_rows.add(median(rows))
            count = len(rows)
            if count:
                columns.append(x)
    assert len(columns) > 50, "local connecting stubs are missing"
    largest_gap = max(b - a for a, b in zip(columns, columns[1:]))
    # One continuous central wire in the control; two visible, disconnected
    # local stubs carrying the same net name in the named-interface case.
    assert (largest_gap > 50) is named
    # The old zero-width endpoint still leaves both stubs present, but forces
    # a rectangular detour. Pin alignment alone did not detect this bug.
    assert (max(wire_rows) - min(wire_rows) <= 2) is (not degenerate)
