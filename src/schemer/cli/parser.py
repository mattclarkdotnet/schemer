from __future__ import annotations

import argparse
from pathlib import Path

from schemer.integration.kicad_cli import DEFAULT_KICAD_CLI
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER


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

    sheets = subparsers.add_parser("plan-sheets", help="inspect source-driven sheet boundaries")
    sheets.add_argument("entrypoint", type=Path, help="Zener .zen entrypoint")
    sheets.add_argument("--json", action="store_true", help="emit the complete boundary plan")
    sheets.add_argument("--compiler", type=Path, default=DEFAULT_PCB_COMPILER)

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
    for command in (layout_kicad, project):
        command.add_argument("--preparation-review", type=Path,
                             help="validated first-run source-intent review")

    return parser
