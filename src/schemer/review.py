"""Programmatic evidence bundles for generated schematic review."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schemer.layout_metrics import primary_anchor_metrics, sheet_legibility_metrics
from schemer.quality import (
    all_quality_findings,
    require_no_component_body_overlaps,
    require_no_detached_connected_components,
    require_no_internal_signal_net_symbols,
    require_no_top_level_block_overlaps,
    require_top_level_block_clearance,
    top_level_block_envelopes,
)
from schemer.render_metrics import require_rendered_overview_quality
from schemer.toolchain import Toolchain, ToolchainError, connectivity_digest
from schemer.view_policy import focus_module
from schemer.viewer import render_schematic


@dataclass(frozen=True)
class ReviewTarget:
    """One module subtree that needs a readable current detail capture."""

    name: str
    instance_ref: str

    @property
    def slug(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        if not slug:
            raise ToolchainError(f"review target has no usable filename: {self.name!r}")
        return slug


def direct_child_review_targets(
    schematic: dict[str, Any], child_names: list[str]
) -> tuple[ReviewTarget, ...]:
    """Resolve selected placed child modules without board-specific viewer IDs."""

    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")
    root = instances.get(root_ref)
    children = root.get("children") if isinstance(root, dict) else None
    if not isinstance(children, dict):
        raise ToolchainError("schematic root children are invalid")

    targets: list[ReviewTarget] = []
    seen_slugs: set[str] = set()
    for child_name in child_names:
        child_ref = children.get(child_name)
        if not isinstance(child_ref, str):
            raise ToolchainError(f"review child is absent from schematic root: {child_name}")
        target = ReviewTarget(child_name, child_ref)
        if target.slug in seen_slugs:
            raise ToolchainError(f"review target filenames collide at {target.slug!r}")
        seen_slugs.add(target.slug)
        targets.append(target)
    return tuple(targets)


def render_review_bundle(
    schematic: dict[str, Any],
    toolchain: Toolchain,
    output_dir: Path,
    targets: tuple[ReviewTarget, ...],
    *,
    width: int = 6400,
    height: int = 4266,
    timeout_seconds: float = 45.0,
    block_composition: dict[str, object] | None = None,
) -> dict[str, Path]:
    """Render a quality-gated overview and fitted detail for every selected module."""

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    captures: dict[str, Path] = {}
    require_no_component_body_overlaps(schematic)
    require_no_detached_connected_components(schematic)
    require_no_internal_signal_net_symbols(schematic)
    require_no_top_level_block_overlaps(schematic)
    require_top_level_block_clearance(schematic)

    overview = render_schematic(
        schematic,
        toolchain,
        output_dir / "overview.png",
        width=width,
        height=height,
        timeout_seconds=timeout_seconds,
    )
    captures["overview"] = overview

    target_manifest: list[dict[str, str]] = []
    for target in targets:
        detail = render_schematic(
            focus_module(schematic, target.instance_ref),
            toolchain,
            output_dir / f"{target.slug}.png",
            width=width,
            height=height,
            timeout_seconds=timeout_seconds,
        )
        captures[target.slug] = detail
        target_manifest.append(
            {
                "instance_ref": target.instance_ref,
                "name": target.name,
                "path": detail.name,
            }
        )

    anchor_metrics = primary_anchor_metrics(schematic)
    legibility_metrics = sheet_legibility_metrics(
        schematic,
        viewport_width=width,
        viewport_height=height,
    )
    rendered_metrics = require_rendered_overview_quality(overview)
    group_envelopes = top_level_block_envelopes(schematic)
    manifest = {
        "captures": {name: path.name for name, path in captures.items()},
        "connectivity_digest": connectivity_digest(schematic),
        "height": height,
        "primary_anchor": anchor_metrics.as_dict(),
        "sheet_legibility": legibility_metrics.as_dict(),
        "quality_findings": [finding.as_dict() for finding in all_quality_findings(schematic)],
        "quality_enforced": True,
        "rendered_overview": rendered_metrics.as_dict(),
        "targets": target_manifest,
        "top_level_groups": {
            name: {
                "height": envelope.height,
                "max_x": envelope.max_x,
                "max_y": envelope.max_y,
                "min_x": envelope.min_x,
                "min_y": envelope.min_y,
                "width": envelope.width,
            }
            for name, envelope in sorted(group_envelopes.items())
        },
        "width": width,
    }
    if block_composition is not None:
        manifest["block_composition"] = block_composition
    manifest_path = output_dir / "review-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    captures["manifest"] = manifest_path
    return captures
