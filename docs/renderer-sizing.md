# Renderer sizing: upstream discussion

## Problem

Text and basic passive symbols need to remain legible relative to larger IC
symbols. Increasing PNG dimensions or viewport zoom enlarges everything together;
it cannot change those proportions. This matters when generating a complete
schematic rather than viewing a small hand-edited detail.

Schemer uses the installed Zener VS Code extension's viewer, not a replacement
renderer. The public code shows the caller boundary in
[`src/schemer/viewer.py`](../src/schemer/viewer.py) and the geometry assumptions
in [`src/schemer/symbol_geometry.py`](../src/schemer/symbol_geometry.py).

## Observations from Zener 2.1.41 (7 September 2026)

1. Changing KiCad Reference/Value property font sizes or the extension theme's
   `fontSize` did not change the generated captions in our probes.
2. Inspection of the installed WASM identified separate generated-caption and
   dynamic wire-label size paths. A disposable, version-locked patch to both
   measurement and drawing constants demonstrated larger generated text.
3. The local trial paired larger text with larger generic R/C geometry. It
   improved relative readability, but exposed collisions and local routing
   problems; we rejected and removed the workaround rather than reshape the
   generator around a patched renderer.
4. We did not locate a supported public sizing API or the current Rust renderer
   source. Binary source paths referenced `projects/editor/crates/schematic`.
   Historical TypeScript viewer code is present in the public PCB repository at
   [this revision](https://github.com/diodeinc/pcb/tree/9798fb08f391907e6aff8c6fafee3049c8cb6340/vscode/preview/src).
   Failure to locate the current source is not proof that it is private.

The extension installation was never changed. This repository contains neither
the patch nor the binary, and does not claim a working sizing workaround.

## Requested interface

Could the renderer expose supported settings for:

1. Generated reference/value/net-caption sizes, with pin names and numbers
   separately controllable if practical?
2. Basic component-symbol scale relative to larger IC symbols?

Those settings should be reflected consistently in drawing, text measurement,
symbol bounds and pin anchors used by placement/routing. A drawing-only scale
would leave the caller working with incompatible geometry.

We would welcome guidance on an existing API if we missed one. The immediate
need is a supported way to tune proportions; it is not a request for a new
automatic layout engine.

## Small example

With the dependencies configured as in the README:

```sh
uv run python tools/render_generated_fixture.py \
  tests/fixtures/package_projection/active_connector/IsolationBlocks.zen \
  artifacts/generic-local-block.png
```

This fixture provides connectors, an active block, series resistors, shunts and
a bypass capacitor, without needing the original board project. It illustrates
the relative sizing in the unmodified renderer; it is not a claim that every
layout defect in the example has been fixed.

For the complete sample board schematic, use the clone-and-generate command in
the repository README. Its Zener sources and local symbols are included under
`tests/fixtures/sample-board/`.
