# Testing

Public snapshot validation: 163 passed, 7 opt-in viewer tests skipped, using
pcbc 0.4.30, Zener extension 2.1.41 and macOS Chrome. The documented full-board
generation command was also run independently and produced the included PNG.
The exported sample board fixture retains the baseline connectivity digest
`02ad9528f03034147820727474776387c013ea82d466257f8933bc8d83e937a8`.

Run commands with the project's uv environment and shared cache.

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
```

1. Unit tests cover placement-comment parsing, transformations, naming,
   block envelopes, overlap rejection and connectivity digests.
2. Compiler-backed `e2e` tests use stable `.zen` fixtures with generic symbols.
   They assert relationships such as alignment, ownership, ordering, spacing,
   orientation and unchanged connectivity, rather than one huge pixel golden.
   Set `SCHEMER_COMPILER` to the compatible compiler executable. They skip when
   it is absent; skipped tests are not evidence of working compiler integration.
3. `viewer` tests exercise actual WASM routing. Enable them explicitly with
   `SCHEMER_VIEWER_TESTS=1 uv run pytest -m viewer -q`. These require the installed
   Zener extension and Chrome as well as the compiler. Positive/negative controls
   distinguish real straight routing from plausible input coordinates.
4. Sample-board integration tests use `tests/fixtures/sample-board`, included with
   all its local dependencies. `SCHEMER_SAMPLE_WORKSPACE` can override the corpus
   for tests which support it. The complete pipeline test uses the included
   fixture and also requires the extension and browser.

Generic fixtures cover series chains, bypass capacitors, common supplies,
fanout, filters, rectifiers, multi-unit IC projection, pin exits, local control
branches, isolation blocks, and repeated channels. Start with small semantic
scenarios; do not repair a failing test using a board-specific placement rule.

The compiler netlist cannot detect every visual disconnection introduced by
the viewer's routing decisions. Geometry checks are therefore necessary but
not sufficient. Inspect representative renders when changing layout or renderer
integration; ordinary unchanged test outputs need no model-based review.
