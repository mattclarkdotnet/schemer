"""Command-line interface for Schemer's toolchain experiments."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from schemer.block_generation import generate_functional_ic_blocks
from schemer.generic_plan import generic_layout_plan
from schemer.hints import parse_hints
from schemer.kicad_api import FileSchematic
from schemer.kicad_bridge import associate_components
from schemer.kicad_connectivity import DEFAULT_KICAD_CLI, verify_native_connectivity
from schemer.kicad_layout import layout_kicad_from_zener
from schemer.kicad_schematic import KiCadSchematicDocument, KiCadSchematicError
from schemer.layout import source_diff
from schemer.layout_metrics import sheet_legibility_metrics
from schemer.review import direct_child_review_targets, render_review_bundle
from schemer.shadow import materialize_proposal_shadow
from schemer.signal_terminations import signal_termination_sources
from schemer.spacing import (
    pack_top_level_groups,
    remove_redundant_root_rails,
    separate_primary_neighbour_overlaps,
    spread_parallel_rail_labels,
    spread_repeated_active_channels,
)
from schemer.symbol_geometry import refine_symbol_geometry
from schemer.toolchain import (
    DEFAULT_CHROME,
    DEFAULT_PCB_COMPILER,
    Toolchain,
    ToolchainError,
    connectivity_digest,
    evaluate_zener,
    inspect_schematic,
    resolve_toolchain,
)
from schemer.view_policy import electrical_view
from schemer.viewer import render_schematic


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

    doctor = subparsers.add_parser("doctor", help="verify the compiler and viewer toolchain")
    doctor.add_argument("entrypoint", type=Path, help="Zener .zen entrypoint")
    doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _add_toolchain_arguments(doctor)

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

    return parser


def _toolchain_for(args: argparse.Namespace) -> Toolchain:
    return resolve_toolchain(
        compiler=args.compiler,
        extension=args.extension,
        chrome=args.chrome,
    )


def _doctor(args: argparse.Namespace) -> int:
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


def _render(args: argparse.Namespace) -> int:
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


def _inspect_kicad(args: argparse.Namespace) -> int:
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


def _layout_kicad(args: argparse.Namespace) -> int:
    schematic = evaluate_zener(args.entrypoint, args.compiler)
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


def _validate_layout_file_overrides(overrides: dict[Path, str]) -> None:
    if any(path.suffix == ".kicad_sym" and path.exists() for path in overrides):
        raise ToolchainError(
            "layout proposals may not override source-defined component symbols"
        )


def _layout(args: argparse.Namespace) -> int:
    if args.experimental_hints and args.proposal_dir is None:
        raise ToolchainError("--experimental-hints requires --proposal-dir for a buildable result")
    toolchain = _toolchain_for(args)
    schematic = evaluate_zener(args.entrypoint, toolchain.compiler)
    plan = generic_layout_plan(args.entrypoint, schematic, toolchain)
    # Component symbols in the evaluated Zener source are authoritative.
    # Primary hinting may correct a copied package before this point, but the
    # layout pass must never substitute presentation geometry at runtime.
    termination_overrides: dict[Path, str] = {}
    presentation_schematic = schematic
    # Complete local component groups before asking the top-level packer to
    # measure them. Output framing and bitmap size never participate here.
    refined_plan = refine_symbol_geometry(presentation_schematic, plan)
    channel_plan = spread_repeated_active_channels(
        presentation_schematic,
        refined_plan,
    )
    block_composition = generate_functional_ic_blocks(
        presentation_schematic,
        refine_symbol_geometry(presentation_schematic, channel_plan),
        allow_experimental_hints=args.experimental_hints,
    )
    if (
        block_composition.applied_hints or block_composition.sheet_hints
    ) and args.proposal_dir is None:
        raise ToolchainError("layout hints require --proposal-dir for a buildable result")
    local_plan = separate_primary_neighbour_overlaps(
        presentation_schematic,
        block_composition.plan,
    )
    # Text is the last local concern. Its allowance is part of the completed
    # block envelope measured by the top-level packer.
    labelled_plan = spread_parallel_rail_labels(presentation_schematic, local_plan)
    labelled_plan = block_composition.preserve_completed_modules(labelled_plan)
    labelled_plan = remove_redundant_root_rails(
        labelled_plan.apply_to_schematic(presentation_schematic),
        labelled_plan,
        {module_ref for module_ref, _ in block_composition.module_blocks},
    )
    labelled_plan = block_composition.preserve_completed_modules(labelled_plan)
    locally_complete = labelled_plan.apply_to_schematic(presentation_schematic)
    final_plan = pack_top_level_groups(
        electrical_view(locally_complete),
        labelled_plan,
        right_of=tuple((hint.blocks[0], hint.blocks[1]) for hint in block_composition.sheet_hints),
    )
    final_plan = block_composition.preserve_completed_modules(final_plan)
    # Root-copy ownership depends on final block geometry. Packing can make
    # a previously unclassified seed copy local to a regenerated child.
    final_plan = remove_redundant_root_rails(
        final_plan.apply_to_schematic(presentation_schematic),
        final_plan,
        {module_ref for module_ref, _ in block_composition.module_blocks},
    )
    block_composition = replace(
        block_composition,
        plan=final_plan,
        applied_hints=block_composition.applied_hints
        + tuple((schematic["root_ref"], hint.id) for hint in block_composition.sheet_hints),
    )
    proposed_plan = final_plan if args.proposal_dir is not None else plan
    updates = proposed_plan.proposed_sources()
    if args.proposal_dir is not None:
        termination_overrides.update(
            signal_termination_sources(
                presentation_schematic,
                proposed_plan,
                updates,
            )
        )
    _validate_layout_file_overrides(termination_overrides)
    changed = {
        path: (path.read_text(), proposed)
        for path, proposed in updates.items()
        if path.read_text() != proposed
    }

    if args.diff:
        for path, (before, after) in changed.items():
            print(source_diff(path, before, after), end="")

    exact_proposed_schematic = None
    if args.proposal_dir is not None:
        proposal = materialize_proposal_shadow(
            args.entrypoint,
            updates,
            args.proposal_dir,
            file_overrides=termination_overrides,
        )
        exact_proposed_schematic = evaluate_zener(proposal.entrypoint, toolchain.compiler)
        if connectivity_digest(exact_proposed_schematic) != connectivity_digest(schematic):
            raise ToolchainError("persisted proposal changed the evaluated connectivity")
        electrical_proposal = electrical_view(exact_proposed_schematic)
        output_legibility = sheet_legibility_metrics(
            electrical_proposal,
            viewport_width=args.width,
            viewport_height=args.height,
        )
        print(f"wrote buildable proposal workspace under {proposal.workspace}")
        print(f"proposal entrypoint: {proposal.entrypoint}")
        print(f"block-composed modules: {len(block_composition.module_blocks)}")
        for module_ref, hint_id in block_composition.applied_hints:
            print(f"layout hint applied: {module_ref}: {hint_id}")
        print(
            "fitted nominal text: "
            f"{output_legibility.nominal_text_pixels:.1f}px at "
            f"{args.width}x{args.height}"
        )

    if args.write:
        for path, proposed in updates.items():
            path.write_text(proposed)
        print(f"updated {len(changed)} Zener position blocks")

    if args.render is not None or args.review_dir is not None:
        proposed_schematic = exact_proposed_schematic or proposed_plan.apply_to_schematic(schematic)
        if connectivity_digest(proposed_schematic) != connectivity_digest(schematic):
            raise ToolchainError("layout proposal changed the evaluated connectivity")
        review_schematic = proposed_schematic
        if not args.include_service_items:
            review_schematic = electrical_view(review_schematic)

    if args.render is not None:
        output = render_schematic(
            review_schematic,
            toolchain,
            args.render,
            width=args.width,
            height=args.height,
            timeout_seconds=args.timeout,
            zoom_factor=args.zoom,
        )
        print(output)

    if args.review_dir is not None:
        source_root = schematic["root_ref"]
        child_names = [
            module.instance_ref.removeprefix(source_root + ".")
            for module in plan.modules
            if module.instance_ref != source_root
        ]
        targets = direct_child_review_targets(review_schematic, child_names)
        captures = render_review_bundle(
            review_schematic,
            toolchain,
            args.review_dir,
            targets,
            width=args.width,
            height=args.height,
            timeout_seconds=args.timeout,
            block_composition=block_composition.as_manifest(),
        )
        print(f"wrote {len(captures) - 1} review captures under {args.review_dir}")

    if args.check:
        if changed:
            print(f"Schemer layout differs in {len(changed)} source files", file=sys.stderr)
            return 1
        print("Schemer layout is current")
        return 0

    if (
        not args.write
        and args.proposal_dir is None
        and args.render is None
        and args.review_dir is None
        and not args.diff
    ):
        print(f"Schemer would update {len(changed)} of {len(updates)} position blocks")
        print("use --diff, --proposal-dir, or explicit --write")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "render":
            return _render(args)
        if args.command == "layout":
            return _layout(args)
        if args.command == "inspect-kicad":
            return _inspect_kicad(args)
        if args.command == "layout-kicad":
            return _layout_kicad(args)
    except (KiCadSchematicError, ToolchainError) as error:
        print(f"schemer: {error}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
