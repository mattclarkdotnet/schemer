from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from schemer.analysis.hierarchy import plan_sheets
from schemer.analysis.topology import inspect_schematic
from schemer.integration.kicad_cli import verify_native_connectivity
from schemer.integration.toolchain import (
    Toolchain,
    evaluate_zener,
    resolve_toolchain,
)
from schemer.integration.viewer import render_schematic
from schemer.kicad.document import KiCadSchematicDocument
from schemer.kicad.editor import FileSchematic
from schemer.native.association import associate_components
from schemer.native.pipeline import layout_kicad_from_zener
from schemer.source.hints import parse_hints
from schemer.workflow.preparation import require_preparation


def _toolchain_for(args: argparse.Namespace) -> Toolchain:
    return resolve_toolchain(
        compiler=args.compiler,
        extension=args.extension,
        chrome=args.chrome,
    )


def doctor(args: argparse.Namespace) -> int:
    toolchain = _toolchain_for(args)
    schematic = evaluate_zener(args.entrypoint, toolchain.compiler)
    report = {
        "compiler": str(toolchain.compiler),
        "extension": str(toolchain.extension),
        "chrome": str(toolchain.chrome),
        "entrypoint": str(args.entrypoint.expanduser().resolve()),
        "schematic": inspect_schematic(schematic),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print("Schemer toolchain is ready")
    print(f"  compiler:  {report['compiler']}")
    print(f"  extension: {report['extension']}")
    print(f"  chrome:    {report['chrome']}")
    facts = report["schematic"]
    print(
        "  schematic: "
        f"{facts['instance_count']} evaluated instances, "
        f"{facts['physical_component_count']} physical components, "
        f"{facts['net_count']} nets"
    )
    print(f"  root children: {', '.join(facts['root_children'])}")
    print(f"  existing root positions: {facts['root_position_count']} (informational only)")
    print(f"  connectivity digest: {facts['connectivity_digest']}")
    return 0


def render(args: argparse.Namespace) -> int:
    toolchain = _toolchain_for(args)
    schematic = evaluate_zener(args.entrypoint, toolchain.compiler)
    output = render_schematic(
        schematic,
        toolchain,
        args.output,
        width=args.width,
        height=args.height,
        timeout_seconds=args.timeout,
        auto_place_on_load=args.auto_place,
        messages_output=args.messages,
        zoom_factor=args.zoom,
    )
    print(output)
    return 0


def report_sheet_plan(args: argparse.Namespace) -> int:
    plan = plan_sheets(evaluate_zener(args.entrypoint, args.compiler))
    if args.json:
        print(json.dumps(asdict(plan), indent=2))
    else:
        print(f"{len(plan.sheets)} sheets; "
              f"{sum(len(sheet.component_refs) for sheet in plan.sheets)} visible components")
        for sheet in plan.sheets:
            print(f"  {sheet.name}: {len(sheet.component_refs)} parts, "
                  f"{len(sheet.ports)} boundary nets")
    return 0


def inspect_kicad(args: argparse.Namespace) -> int:
    schematic_path = args.schematic.expanduser().resolve()
    document = KiCadSchematicDocument.from_file(schematic_path)
    summary = document.summary()
    summary["schematic"] = str(schematic_path)
    if args.zener is not None:
        zener = evaluate_zener(args.zener, args.compiler)
        associations = associate_components(zener, document)
        summary["zener"] = str(args.zener.expanduser().resolve())
        summary["associated_component_count"] = len(associations)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print("KiCad schematic inventory")
    print(f"  schematic:   {schematic_path}")
    print(f"  version:     {document.version}")
    print(f"  symbols:     {len(document.symbols)}")
    print(f"  components:  {summary['component_path_count']}")
    print(f"  wires:       {len(document.wires)}")
    print(f"  labels:      {len(document.labels)}")
    print(f"  no-connects: {len(document.no_connects)}")
    if args.zener is not None:
        print(f"  Zener match: {summary['associated_component_count']} components")
    return 0


def layout_kicad(args: argparse.Namespace) -> int:
    schematic = evaluate_zener(args.entrypoint, args.compiler)
    require_preparation(args, schematic)
    right_of = tuple(
        (hint.blocks[0], hint.blocks[1])
        for hint in parse_hints(args.entrypoint.read_text())
        if hint.kind == "right-of"
    )
    editor = FileSchematic.from_file(args.schematic)
    commit = editor.begin_commit()
    try:
        report = layout_kicad_from_zener(schematic, editor, right_of=right_of)
    except Exception:
        editor.drop_commit(commit)
        raise
    editor.push_commit(commit, "Apply Schemer native layout")
    checked_pins = verify_native_connectivity(schematic, editor, args.kicad_cli)
    editor.save_as(args.output, overwrite=args.overwrite)
    print(f"wrote native KiCad schematic: {args.output.expanduser().resolve()}")
    print(f"KiCad connectivity matches source: {checked_pins} physical pins")
    print(
        "layout objects: "
        f"{report.component_count} components, "
        f"{report.power_symbol_count} power symbols, "
        f"{report.label_count} labels, "
        f"{report.wire_count} wires"
    )
    print(
        "suppressed service symbols: "
        f"{report.hidden_component_count}; "
        f"no-connects: {report.no_connect_count}; "
        f"sheet: {report.page_size} landscape"
    )
    return 0
