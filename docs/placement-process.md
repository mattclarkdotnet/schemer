# Placement process

The generator works from connectivity, component roles and symbol pin geometry,
not fixed coordinates for a particular board. Layout is independent of page
size: output framing comes after placement.

1. Evaluate the source and identify meaningful electrical components. Omit
   mechanical-only items from the explanatory view without changing the netlist.
2. Clear previous coordinates in memory and obtain initial positions from the
   installed viewer. Generic projections can combine physical IC units and
   provide a coherent pin perimeter when an authored symbol contains only
   sequential generic pin names. These transformations are proposal-only.
3. Establish the major device or connector for each local block. Derive lanes
   from actual connection points and required outward pin exits, not symbol
   bounding-box anchors.
4. Lay out series paths, shunts, local supply/bypass branches, and rail
   terminations. Keep support parts with their electrical owner, preserve
   isolation domains, and prefer shared local return wiring where appropriate.
5. Account for annotations after the electrical rows are established. Text
   clearance must not silently displace components across their wire lanes.
6. Measure completed local blocks, then translate whole blocks apart with a
   minimum gap. Local geometry must survive translation unchanged.
7. Materialise placement comments in a proposal workspace and re-evaluate it.
   Compare connectivity digests; check body overlap and block clearance; render
   the result with the real installed viewer.

Relevant implementation entry points are `src/schemer/cli.py` (orchestration),
`heuristic_block.py` (local circuit placement), `blocks.py` (block geometry and
validation), `spacing.py` (packing), and `symbol_geometry.py` (pin coordinates).

## Quality boundary

Any positive-area component-body overlap is a failure. Unexplained doglegs,
text collisions, ambiguous junctions and visually detached local connections
also need review, even when source connectivity is unchanged. Renderer-generated
wire paths and text placements cannot all be predicted exactly from source
anchors. Existing checks do not constitute electrical design verification.

The [principles](schematic-layout-principles.md) are the target, not a claim that
all outputs satisfy them. Improve generic geometry bugs in the generator;
do not use board-specific hints to conceal those bugs.
