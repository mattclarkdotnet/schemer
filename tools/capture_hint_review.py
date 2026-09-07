"""Capture an already-generated proposal for a qualitative hint review.

Run with uv run python tools/capture_hint_review.py ENTRYPOINT OUTPUT --module NAME.
This measures defects without claiming that a diagnostic render is acceptable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from schemer.hints import parse_hints
from schemer.quality import all_quality_findings
from schemer.render_metrics import rendered_overview_metrics
from schemer.toolchain import ToolchainError, connectivity_digest, evaluate_zener, resolve_toolchain
from schemer.view_policy import electrical_view, focus_module
from schemer.viewer import render_schematic


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entrypoint", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--module", help="Omit to capture the complete electrical sheet")
    parser.add_argument("--width", type=int, default=3200)
    parser.add_argument("--height", type=int, default=2400)
    args = parser.parse_args()
    toolchain = resolve_toolchain()
    schematic = evaluate_zener(args.entrypoint, toolchain.compiler)
    module_ref = schematic["root_ref"]
    focused = electrical_view(schematic)
    if args.module:
        module_ref += "." + args.module
        focused = focus_module(focused, module_ref)
    args.output.mkdir(parents=True, exist_ok=True)
    capture = render_schematic(
        focused, toolchain, args.output / "detail.png", width=args.width, height=args.height
    )
    prefix = module_ref + "."
    module_source = Path(schematic["instances"][module_ref]["type_ref"]["source_path"])
    manifest = {
        "status": "diagnostic-only; visual review required",
        "entrypoint": str(args.entrypoint.resolve()),
        "module": args.module or "<root>",
        "full_connectivity_digest": connectivity_digest(schematic),
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        "source_hints": [hint.as_dict() for hint in parse_hints(module_source.read_text())],
        "rendered_metrics": rendered_overview_metrics(capture).as_dict(),
        # Focusing changes the apparent hierarchy. Measure the actual sheet,
        # otherwise local parts would be mistaken for top-level module groups.
        "quality_findings": [
            finding.as_dict()
            for finding in all_quality_findings(electrical_view(schematic))
            if not args.module or finding.module_ref == module_ref
        ],
        "components": {
            ref.removeprefix(prefix): instance.get("reference_designator")
            for ref, instance in focused["instances"].items()
            if instance.get("kind") == "Component"
        },
    }
    valid_content = bool(manifest["rendered_metrics"]["annotation_glyph_count"])
    if not valid_content:
        manifest["status"] = "invalid: no schematic annotation content detected"
    (args.output / "evidence.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if not valid_content:
        raise ToolchainError("invalid review capture: no schematic annotation content detected")
    print(capture)


if __name__ == "__main__":
    main()
