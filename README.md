# Schemer

Schemer generates schematics from [Zener](https://github.com/diodeinc/pcb) projects.
It places each IC with its local wiring and support components, measures the
group, then arranges the groups into a complete drawing. Positions are saved as
`# pcb:sch` comments and rendered using the Zener VS Code extension.

The included sample board is the main example.
[View its schematic](docs/images/sample-board.png).
Text and small-component sizing still need work; the
[renderer-sizing note](docs/renderer-sizing.md) describes the controls we'd like
from the renderer.

## Try it

You need [uv](https://docs.astral.sh/uv/), the Zener compiler and VS Code
extension, and Chrome. Tested on macOS with `pcbc 0.4.30` and Zener extension
`2.1.41`. VS Code can stay closed.

```sh
git clone https://github.com/mattclarkdotnet/schemer.git
cd schemer
uv run schemer layout tests/fixtures/sample-board/boards/sample-board/SampleBoard.zen \
  --experimental-hints --proposal-dir artifacts/sample-board-proposal \
  --render artifacts/sample-board.png --width 8000 --height 6000 --zoom 1
```

The PNG is written to `artifacts/sample-board.png`; the generated Zener files go
into `artifacts/sample-board-proposal/`. The board's local dependencies are
included in the fixture.

Schemer finds `pcbc` or `pcb` on `PATH`, the standard VS Code extensions
directory, and macOS Chrome. For other locations, use `--compiler`,
`--extension`, and `--chrome`, or set `SCHEMER_COMPILER`,
`SCHEMER_EXTENSION_ROOT`, and `SCHEMER_CHROME`.

For your own Zener workspace:

```sh
uv run schemer layout /path/to/project/Board.zen \
  --proposal-dir artifacts/proposal --render artifacts/proposal.png --zoom 1
```

The proposal is a source copy, leaving your project unchanged.
`uv run schemer layout --help` lists the options.

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
