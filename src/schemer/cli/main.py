from __future__ import annotations

import sys
from collections.abc import Sequence

from schemer.cli.commands import inspect_kicad, layout_kicad, report_sheet_plan
from schemer.cli.parser import build_parser
from schemer.core.errors import KiCadSchematicError, ToolchainError
from schemer.integration.toolchain import evaluate_zener
from schemer.kicad.editor import FileSchematic
from schemer.workflow.native_project import layout_native_project
from schemer.workflow.preparation import check_preparation, prepare_source, require_preparation


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            review = prepare_source(args.entrypoint, args.output, args.compiler)
            print(f"source copied; complete intent review before layout: {review}")
            return 0
        if args.command == "check-preparation":
            print(f"prepared source validated: {check_preparation(args.review, args.compiler)}")
            return 0
        if args.command == "plan-sheets":
            return report_sheet_plan(args)
        if args.command == "inspect-kicad":
            return inspect_kicad(args)
        if args.command == "layout-kicad":
            return layout_kicad(args)
        if args.command == "layout-project":
            schematic = evaluate_zener(args.entrypoint, args.compiler)
            require_preparation(args, schematic)
            report = layout_native_project(
                schematic,
                FileSchematic.from_file(args.schematic), args.entrypoint, args.output,
                executable=args.kicad_cli, draft=args.draft,
            )
            kind = "DRAFT native KiCad project" if report.draft else "native KiCad project"
            print(f"wrote {kind}: {report.root_file}")
            if report.connectivity_error:
                print(f"DRAFT connectivity check failed: {report.connectivity_error}")
            else:
                print(f"connectivity matches source: {report.checked_pins} physical pins")
            for sheet in report.sheets:
                print(f"{sheet['name']}: {sheet['parts']} parts, {sheet['page_size']}, "
                      f"{len(sheet['issues'])} layout defects")
            return 0
    except (KiCadSchematicError, ToolchainError) as error:
        print(f"schemer: {error}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2
