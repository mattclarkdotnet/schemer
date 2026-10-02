from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

from schemer.analysis.sheet_metrics import sheet_legibility_metrics
from schemer.analysis.topology import connectivity_digest
from schemer.analysis.visibility import electrical_view
from schemer.core.errors import ToolchainError
from schemer.integration.toolchain import (
    evaluate_zener,
    resolve_toolchain,
)
from schemer.integration.viewer import render_schematic
from schemer.placement.pipeline import generate_functional_ic_blocks
from schemer.placement.refinement.pipeline import refine_symbol_geometry
from schemer.placement.spacing.channels import (
    separate_primary_neighbour_overlaps,
    spread_repeated_active_channels,
)
from schemer.placement.spacing.groups import pack_top_level_groups
from schemer.placement.spacing.rails import remove_redundant_root_rails, spread_parallel_rail_labels
from schemer.source.positions import proposed_sources, source_diff
from schemer.source.shadow import materialize_proposal_shadow
from schemer.source.signal_terminations import signal_termination_sources
from schemer.workflow.generic_plan import generic_layout_plan
from schemer.workflow.preparation import require_preparation
from schemer.workflow.review import direct_child_review_targets, render_review_bundle


def _validate_layout_file_overrides(overrides: dict[Path, str]) -> None:
    if any(path.suffix == ".kicad_sym" and path.exists() for path in overrides):
        raise ToolchainError(
            "layout proposals may not override source-defined component symbols"
        )


@dataclass(frozen=True)
class LayoutRequest:
    entrypoint: Path
    compiler: Path
    extension: Path | None
    chrome: Path
    preparation_review: Path | None
    proposal_dir: Path | None
    experimental_hints: bool
    width: int
    height: int
    timeout: float
    zoom: float
    diff: bool
    write: bool
    check: bool
    render: Path | None
    review_dir: Path | None
    include_service_items: bool


def layout_source(args: LayoutRequest) -> int:
    if args.experimental_hints and args.proposal_dir is None:
        raise ToolchainError("--experimental-hints requires --proposal-dir for a buildable result")
    toolchain = resolve_toolchain(
        compiler=args.compiler, extension=args.extension, chrome=args.chrome
    )
    schematic = evaluate_zener(args.entrypoint, toolchain.compiler)
    require_preparation(args, schematic)
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
    updates = proposed_sources(proposed_plan)
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
