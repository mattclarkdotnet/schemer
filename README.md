# Schemer

Schemer generates schematics from [Zener](https://github.com/diodeinc/pcb) projects.
It places each IC with its local wiring and support components, measures the
group, then arranges the groups into a native KiCad schematic. Authored symbols,
fonts and component roles are preserved.

The included sample board is the main example.
[View its schematic](docs/images/sample-board.png).

## Try it

You need [uv](https://docs.astral.sh/uv/), the Zener compiler and VS Code
extension, Chrome, and KiCad 10. VS Code can stay closed.

```sh
git clone https://github.com/mattclarkdotnet/schemer.git
cd schemer
mkdir -p artifacts
pcb apply schematic tests/fixtures/sample-board/boards/sample-board/SampleBoard.zen --no-open
uv run schemer layout-kicad tests/fixtures/sample-board/boards/sample-board/SampleBoard.zen \
  tests/fixtures/sample-board/boards/sample-board/layout/SampleBoard/SampleBoard.kicad_sch \
  --output artifacts/SampleBoard.kicad_sch
```

Open `artifacts/SampleBoard.kicad_sch` in KiCad. The command checks every physical
pin against the Zener netlist before saving. The fixture includes the board's
local dependencies; no other project checkout is needed.

Schemer finds `pcb` on `PATH`, the standard VS Code extensions
directory, and macOS Chrome. For other locations, use `--compiler`,
`--extension`, and `--chrome`, or set `SCHEMER_COMPILER`,
`SCHEMER_EXTENSION_ROOT`, and `SCHEMER_CHROME`.

KiCad's `kicad-cli` must be on PATH or in the standard macOS installation.
Use `--kicad-cli` for another location. For your own board, run `pcb apply
schematic` first, then pass its source and generated schematic to `layout-kicad`.

## Legacy renderer

The original Zener viewer path remains available:

```sh
uv run schemer layout tests/fixtures/sample-board/boards/sample-board/SampleBoard.zen \
  --proposal-dir artifacts/sample-board-proposal --render artifacts/sample-board.png --zoom 1
```

The proposal is a source copy, leaving your project unchanged.
`uv run schemer layout --help` lists the options.

The [renderer-sizing note](docs/renderer-sizing.md) records limitations of that viewer.

## Development

```sh
uv run pytest -q
uv run ruff check .
```

Tests cover generic circuit fixtures and the sample board. Renderer tests are enabled
with `SCHEMER_VIEWER_TESTS=1`; see [testing](docs/testing-strategy.md).

The design is described in the [layout principles](docs/schematic-layout-principles.md),
[placement process](docs/placement-process.md), and [semantic hints](docs/layout-hints.md).

No project licence yet. Existing third-party licences are listed in
[THIRD_PARTY.md](THIRD_PARTY.md).
