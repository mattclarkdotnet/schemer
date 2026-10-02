# Native toolchain

Schemer evaluates annotated Zener source and writes native KiCad schematics.
No browser, VS Code extension, WASM viewer or GUI automation is involved.
Follow the [README workflow](../README.md#recommended-workflow) for commands.

## Dependencies

Use the uv-managed Python environment, a compatible Zener `pcb` compiler and
KiCad's `kicad-cli`. Compiler discovery uses `SCHEMER_COMPILER`, then `pcb` or
`pcbc` on PATH, then `~/.local/bin/pcb`; `--compiler` overrides it. Native
commands accept `--kicad-cli`; its default uses PATH or the macOS app location.

## Source preparation and evaluation

`prepare` copies the dependency closure into a new run and creates a complete
component-review worklist. Review and annotate the copy, then seal it with
`check-preparation`. Both layout commands enforce that review.

The compiler adapter invokes `build --netlist`, validates its JSON and binds
physical pin numbers to logical terminals. Preparation compares topology
without depending on source locations or compiler-local numeric net IDs.
Source/symbol digests prevent a stale review from authorizing changed intent.

## Native generation and validation

Generate a fresh compiler seed with `pcb apply schematic --no-open`.
`layout-project` builds circuits and a flat hierarchy from reviewed source;
`layout-kicad` is the lower-level path for already positioned source. Neither
uses a browser autoplacer or writes placement comments back into the source.

Verify native identity with `inspect-kicad --zener`. During layout, KiCad exports
a netlist from the complete generated hierarchy; Schemer compares physical pin
membership with the evaluated Zener design. Draft mode records connectivity
failures without claiming a pass. Strict mode refuses failed output.

## Visual review

Export SVG with `kicad-cli sch export svg --exclude-drawing-sheet`, then inspect
every sheet and circuit at readable detail scale. Automated connectivity and
geometry checks do not establish drawing quality. Keep source-defined symbol
and text sizes; choose the standard sheet after measuring completed content.

Generated candidates are disposable views. Hand finishing is possible, but
manual edits are not preserved through regeneration. Do not use a finished
candidate as the next compiler seed.

The old `layout`, `render` and `doctor` commands, browser discovery, raster
review helpers and Playwright/Pillow dependencies have been removed. Historical
experiments and publication records describe their original environment; they
are not instructions for running the current generator.
