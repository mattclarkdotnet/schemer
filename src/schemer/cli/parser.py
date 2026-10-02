from __future__ import annotations

import argparse
from pathlib import Path

from schemer.integration.kicad_cli import DEFAULT_KICAD_CLI
from schemer.integration.toolchain import (
    DEFAULT_CHROME,
    DEFAULT_PCB_COMPILER,
)


def _add_toolchain_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compiler",
        type=Path,
        default=DEFAULT_PCB_COMPILER,
        help="path to the local pcbc compiler",
    )
    parser.add_argument(
        "--extension",
        type=Path,
        help="path to an installed diode-inc.zener extension (newest is automatic)",
    )
    parser.add_argument(
        "--chrome",
        type=Path,
        default=DEFAULT_CHROME,
        help="path to a Chrome executable used for headless rendering",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemer",
        description="Toolchain for readable Zener schematic layout experiments.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="copy and inventory source for intent review")
    prepare.add_argument("entrypoint", type=Path)
    prepare.add_argument("--output", type=Path, required=True, help="new preparation directory")
    prepare.add_argument("--compiler", type=Path, default=DEFAULT_PCB_COMPILER)
    check = subparsers.add_parser(
        "check-preparation", help="validate the completed intent worklist",
    )
    check.add_argument("review", type=Path)
    check.add_argument("--compiler", type=Path, default=DEFAULT_PCB_COMPILER)

    doctor = subparsers.add_parser("doctor", help="verify the compiler and viewer toolchain")
    doctor.add_argument("entrypoint", type=Path, help="Zener .zen entrypoint")
    doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _add_toolchain_arguments(doctor)

    sheets = subparsers.add_parser("plan-sheets", help="inspect source-driven sheet boundaries")
    sheets.add_argument("entrypoint", type=Path, help="Zener .zen entrypoint")
    sheets.add_argument("--json", action="store_true", help="emit the complete boundary plan")
    sheets.add_argument("--compiler", type=Path, default=DEFAULT_PCB_COMPILER)

    render = subparsers.add_parser(
        "render", help="render the current schematic without changing positions"
    )
    render.add_argument("entrypoint", type=Path, help="Zener .zen entrypoint")
    render.add_argument("--output", type=Path, required=True, help="output PNG path")
    render.add_argument("--width", type=int, default=6400, help="viewport width")
    render.add_argument("--height", type=int, default=4266, help="viewport height")
    render.add_argument(
        "--zoom",
        type=float,
        default=1.0,
        help="nominal center zoom for a detail capture (1 keeps the fitted overview)",
    )
    render.add_argument(
        "--timeout", type=float, default=45.0, help="viewer startup timeout in seconds"
    )
    render.add_argument(
        "--auto-place",
        action="store_true",
        help="ask the installed viewer to auto-place before capture",
    )
    render.add_argument(
        "--messages",
        type=Path,
        help="write viewer host messages to JSON (diagnostic)",
    )
    _add_toolchain_arguments(render)

    layout = subparsers.add_parser("layout", help="propose readable schematic positions")
    layout.add_argument("entrypoint", type=Path, help="Zener .zen entrypoint")
    action = layout.add_mutually_exclusive_group()
    action.add_argument(
        "--check",
        action="store_true",
        help="exit nonzero when source position blocks differ from the proposal",
    )
    action.add_argument(
        "--write",
        action="store_true",
        help="update the source position blocks (never implied)",
    )
    layout.add_argument("--diff", action="store_true", help="print the proposed source diff")
    layout.add_argument(
        "--proposal-dir",
        type=Path,
        help="write proposed .zen copies to this Schemer-owned directory",
    )
    layout.add_argument("--render", type=Path, help="render the proposed in-memory layout")
    layout.add_argument(
        "--experimental-hints",
        action="store_true",
        help="interpret experimental semantic comment hints for review in this run",
    )
    layout.add_argument(
        "--review-dir",
        type=Path,
        help="render a fitted overview and current detail for every placed child module",
    )
    layout.add_argument(
        "--include-service-items",
        action="store_true",
        help="include mounting holes and root test points in the render",
    )
    layout.add_argument("--width", type=int, default=6400, help="render viewport width")
    layout.add_argument("--height", type=int, default=4266, help="render viewport height")
    layout.add_argument(
        "--zoom",
        type=float,
        default=2.0,
        help="nominal center zoom for proposal review (default: 2)",
    )
    layout.add_argument(
        "--timeout", type=float, default=45.0, help="viewer startup timeout in seconds"
    )
    _add_toolchain_arguments(layout)

    inspect_kicad = subparsers.add_parser(
        "inspect-kicad",
        help="inspect the native objects in a persistent KiCad schematic",
    )
    inspect_kicad.add_argument("schematic", type=Path, help="KiCad .kicad_sch file")
    inspect_kicad.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    inspect_kicad.add_argument(
        "--zener",
        type=Path,
        help="evaluated Zener entrypoint whose components must match KiCad Path fields",
    )
    inspect_kicad.add_argument(
        "--compiler",
        type=Path,
        default=DEFAULT_PCB_COMPILER,
        help="path to the current pcb compiler",
    )

    layout_kicad = subparsers.add_parser(
        "layout-kicad",
        help="apply accepted Zener positions as native KiCad schematic layout",
    )
    layout_kicad.add_argument("entrypoint", type=Path, help="positioned Zener .zen entrypoint")
    layout_kicad.add_argument("schematic", type=Path, help="persistent KiCad .kicad_sch file")
    layout_kicad.add_argument(
        "--output",
        type=Path,
        required=True,
        help="destination .kicad_sch file",
    )
    layout_kicad.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output file",
    )
    layout_kicad.add_argument(
        "--compiler",
        type=Path,
        default=DEFAULT_PCB_COMPILER,
        help="path to the current pcb compiler",
    )
    layout_kicad.add_argument("--kicad-cli", type=Path, default=DEFAULT_KICAD_CLI)

    project = subparsers.add_parser(
        "layout-project", help="lay out a flat native KiCad schematic hierarchy",
    )
    project.add_argument("entrypoint", type=Path, help="intent-prepared Zener entrypoint")
    project.add_argument("schematic", type=Path, help="fresh pcb apply schematic output")
    project.add_argument("--output", type=Path, required=True, help="new project directory")
    project.add_argument("--compiler", type=Path, default=DEFAULT_PCB_COMPILER)
    project.add_argument("--kicad-cli", type=Path, default=DEFAULT_KICAD_CLI)
    project.add_argument(
        "--draft", action="store_true",
        help="render all sheets for review, retaining and reporting layout defects",
    )
    for command in (layout, layout_kicad, project):
        command.add_argument("--preparation-review", type=Path,
                             help="validated first-run source-intent review")

    return parser
