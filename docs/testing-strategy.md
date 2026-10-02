# Semantic layout testing strategy

## Test organization and architecture checks

Tests are grouped by responsibility under `tests/{core,symbols,analysis,placement,
source,kicad,native,integration,workflow,architecture}`. Older entries below use
the original test filenames; the relevant package now owns those tests. Shared
synthetic schematics are in `tests/support`, while `tests/paths.py` provides stable
fixture paths independent of test nesting. Production fixtures remain generic.

Run `uv run pytest` and `uv run ruff check src tests tools`. The architecture suite
runs the Import Linter contracts in `pyproject.toml`, imports every package module,
and rejects private cross-module services and self-imports. Run those dependency
contracts directly with `uv run lint-imports --no-cache`.

Distribution verification is separate: build with `uv build`, install the wheel
in an isolated uv environment outside the checkout, import the package and exercise
CLI help. A passing source-checkout suite cannot establish packaging correctness.

## Behavioural fixtures

`ParallelCapacitors.zen` checks a three-capacitor supply bank with reversed
terminal order on one capacitor, plus a different-return-domain control.
Assertions cover aligned bus pins, supply-up orientation, one termination
per bank rail, domain separation and unchanged connectivity. Inspect native
SVG output for actual continuous supply and return buses.

`GeneralBlocks.zen` covers two equal-sized ICs with bypass/load parts and an
unanchored RC chain, outside the specialised motif recognisers. It checks
complete, exclusive component coverage, local ownership, valid block bounds,
seed independence and connectivity. `NestedGeneralBlocks.zen` checks that a
parent retains and measures its child placement without duplicating parts.
Net-label tests ensure nested metadata is written to its defining source.
The former test expecting an unsupported circuit to pass through unchanged
now requires an explicit error when symbol geometry is missing.

`PassiveStructures.zen` covers a five-resistor tapped ladder, a two-resistor
feedback divider and one functional supply-to-return capacitor. It requires
each resistor path to remain one ordered vertical structure, aligns the
feedback tap with its owner pin, and requires the single capacitor to point
from supply above to return below. The fixture contains no board, net or
reference identities used by the production rule.

`LocalBranches.zen` uses an existing generic eight-pin body and generic
resistors to exercise compound rail-feed spacing and a ground termination
beside a series-signal lane. Its regression checks the wider feed span,
midpoint supply tee, straight series connection, outward ground clearance,
same-face ground-bank alignment, conventional orientation and unchanged
connectivity. The primary-device integration render remains the check against
the renderer silently choosing a different same-net termination.

`SupplyBanks.zen` is a generic compiler-backed fixture for one supply feeding
separated pin banks on either IC face. It checks lower-bank bypass alignment,
separate upper/lower supply terminations and conventional symbol orientation.
The generic connector fixture also checks return glyphs against all passive
bodies, not just component-to-component overlaps.

Named-interface regressions in `test_signal_terminations.py` use generic
fixtures to check connector-owned series rows, the three-distinct-straight-lane
threshold, source-comment preservation, idempotence and unchanged compiled net
membership. Native routing and connectivity tests check that labelled endpoints
retain the intended local wire connections. Bypass clearance may retain the
existing `local-passive-owner-gap` advisory: integration tests bound the
outward reach and preserve the supply row rather than weakening the production
distance check. No overlap checks are relaxed.

`LocalControl.zen` covers an aligned mixed pull-up/pull-down bank, a shared
primary/subordinate connection with branch clearance, and an orthogonal shunt
approach. The rail-orientation matrix checks all four exit sides for supply
and ground. These are generic, compiler-backed cases; the production rules
contain no fixture or board identities. Native routing tests check orthogonal
approaches, pin exits and minimum-turn paths without browser pixel inspection.
The principles document has its own corpus-identifier regression guard.

The experimental semantic-comment interpreter is protected by
`tests/source/test_hints.py`. It checks strict non-geometric records, duplicate and
unsupported requests, rejection of unapproved types, preamble preservation,
and idempotent metadata installation. The generic parallel-transformer fixture
then checks changed relative geometry, unchanged component coverage and
connectivity, source/position recompilation, and independence from seed
coordinates. Cardinal corner-pin cases ensure stroke direction wins over
ambiguous bounding-box edges. These tests do not claim to prove viewer wire
shape; the independent qualitative rerender review remains necessary.


Schemer develops layout behavior through small semantic scenarios before it is
trusted on a complete design. A fixture is not a miniature screenshot test and
not merely a graph shape. It states one piece of conventional schematic grammar
that the generator must preserve, such as a signal chain, a shunt branch, a
fan-out, or decoupling local to an IC.

## Fixture contract

Each end-to-end fixture contains:

- one stable `.zen` input using only Zener standard-library generic symbols;
- a package manifest sufficient for the local compiler;
- `expected-layout.json`, containing meaningful relative constraints rather
  than a complete table of pixel coordinates.

Symbol-authoring fixtures may include a tiny private generic symbol and
footprint. They test helpers used during primary source correction; their
output is never applied by the runtime layout pass.

Every expected file names a `scenario`. This name is the engineering meaning
under test, not an algorithm selector. The generator must infer the applicable
role from component types, rail types, connectivity, and directed IO names;
board-specific intent may later refine that inference explicitly.

Current constraint vocabulary includes:

- `left_to_right`, `right_of`, `same_x`, `different_x`, and `same_y` for flow
  and alignment;
- `below` and `x_between` for conventional shunt placement;
- `different_y` for branch separation;
- `near` for local support relationships such as IC decoupling; and
- `closer_to` for ordered proximity, such as a 100 nF bypass closer to its IC
  than a 10 µF bulk capacitor; and
- `rotations` where symbol orientation is part of circuit grammar; and
- `terminal_flows` for named, directed terminals whose physical left-to-right
  order must agree with semantic upstream-to-downstream flow.

The geometry audit covers terminal collinearity for straight local chains. It
compares viewer-equivalent connected-pin coordinates, not equality of
component `x` or `y` values, and reports a series bend above one viewer unit.
This catches avoidable doglegs caused by differing symbol, rotation, field, or
multi-unit placement anchors. A constrained bend remains a recorded finding
rather than disappearing beneath a broad tolerance.

Implemented geometry regressions now include rejecting an unowned rail
passive, clearing an owned decoupler's `local-passive-owner-gap`, hanging leaf
series branches from the real side of an owner pin, and preserving authored
symbol geometry throughout layout. The compiler-backed generic
isolation fixture also gives one logical ground terminal two physical pins on
one component face and a third occurrence on the opposite face. It requires
the parser to retain all three pin positions and the block planner to emit
exactly one local ground wireset per face. This is the
minimal regression for the rule that prevents an IC's repeated rail pins from
producing around-body loops.

Fixtures may also assert the semantic plan before geometry:

- `roles` proves why each named component is in the drawing;
- `stages` proves major devices enter before power and signal support; and
- `owners` proves local support was attached to the intended consumer.

The generator emits a deterministic trace with a role, stage, owner where
applicable, and reason for every visible symbol. Position coverage and trace
coverage must be identical. A component may not be assigned coordinates from a
fallback “miscellaneous” bucket.

Coordinates use the viewer convention in which increasing `y` moves downward.
Generic endpoint tests cover all four faces and require the one-pin neutral
symbol to inherit the outgoing wire direction. This catches the coordinate-
correct but visually bent route produced when a horizontal stub terminates at
a vertically oriented zero-length pin.
`SharedFaceRail.zen` puts two return pins and two named signals on the same
right face of a generic IC. It requires one south-facing return wireset whose
vertical trunk lies beyond both signal endpoints, proving that shortest wire
length cannot place a shared trunk across established signal lanes.
Persisted positions are 0.1 mm units at the rotated symbol bounding-box
top-left, not the KiCad symbol origin. Pin-aware checks rotate the complete
symbol bounds first, reconstruct the KiCad origin from that rotated box, and
convert KiCad y-up pin coordinates into viewer y-down coordinates. An
asymmetric quarter-turn unit test protects this transform because its sign,
scale and rotation order affect every alignment rule.
The same transform applies to one-pin power and ground symbols: a focused test
places an asymmetric generic GND drawing by its sole electrical pin, and the
DigitalAbx integration check requires the nearest J301 ground symbol pin—not
its stored anchor—to share the connector ground pin's x coordinate.
The generic isolation-chain fixture contains an isolated bypass capacitor and
two grounded signal shunts owned by the same connector. The bypass ground pin
must be exactly one short stub beyond the passive return pin on the same row,
and its ground drawing must face along that stub. The two shunts must instead
share one semantic return endpoint. Their upper branch is placed farther
outward than their lower branch so its return drop cannot cross the lower
signal lane.
Those fixtures also assert the branch-side pin rows: connector shunts match
their connector pins, bypass capacitors match their owning IC supply pins, and
series resistors match the adjacent central-IC pins. This protects the process
rule that annotation spacing may stagger horizontal distance but cannot move a
part off its electrical lane.
The DigitalAbx integration fixture also runs the final presentation cleanup and
compares the complete multiset of symbol coordinates before and after it. Net
symbol identifiers may change when captions are shortened, but text cleanup
must not change any `x`, `y`, rotation, or mirror value.
The DigitalAbx integration assertions apply the same generic rules to real
symbols: C301 has a straight outward horizontal ground termination with the
ground drawing rotated along it, while R301 and R302 form one shared
`USB_HOST_GND` bank. R301 is the upper and outer branch, R302 the lower and
inner branch, and their one common ground endpoint lies below the outer return
trunk and one ordinary stub beyond its outermost pin. This catches redundant
local doglegs, the viewer's zero-length-segment loop, and a return wire crossing
the CC2 signal.
The same integration fixture requires J301's right-facing VBUS pin and its
nearest `USB_HOST_VBUS` rail symbol to share an exact row, with the symbol one
ordinary stub to the right and rotated to face outward with its pin toward the
connector. This prevents either the upright default or an inward-facing supply
symbol from restoring an otherwise purposeless dogleg.
The geometry audit reports a series-terminal dogleg above one viewer unit, so
automated review cannot silently omit the smaller but still visible bends that
the former 60-unit threshold missed.
For standard small passives, rotations 90 and 270 are both horizontal, but they
exchange terminal sides: with the generic symbols' normal pin order, rotation
270 faces pin 1 left and pin 2 right. Rotation 0 leaves a shunt passive
vertical. Tests assert terminal order as well as horizontal-versus-vertical
grammar, because the latter alone cannot detect a false bypass.

The expected file intentionally does not freeze spacing constants. A safe
change to grid pitch should not require rewriting every test. Exact generated
positions are still required to be deterministic for identical input.

Block-composition tests run below the compiler-backed scenario layer. They
construct generic leaf and composite blocks and require deterministic
flattening, exclusive symbol ownership, boundary-constrained ports, child
containment and spacing, child-port link resolution and net agreement, and
local rejection of component/corridor,
annotation/corridor, and unrelated-wire-corridor overlap. These tests isolate
composition failures before a full schematic or viewer render is involved.

Semantic end-to-end fixtures will migrate to blocks incrementally. A fixture
must first prove the local block (for example one decoupled IC, series chain,
or differential pair), then prove composition with its immediate neighbours,
and only then participate in a full-board sheet. A full-board visual failure
should be reproducible at the smallest block boundary that owns it.

## End-to-end checks

For every fixture, the test:

1. copies the stable source into a temporary package;
2. evaluates it with the real local `pcbc build --netlist` compiler path;
3. generates the semantic trace and positions twice and requires both to be
   identical;
4. checks declared semantic roles, stages, owners, and relative relationships;
5. replaces only the final `# pcb:sch` comment block;
6. recompiles the modified copy;
7. proves the normalized connectivity digest is unchanged; and
8. verifies that every generated viewer ID and position was accepted by the
   compiler.

For nested modules, source comments deliberately keep local net names. The
evaluated viewer IDs use the compiler's semantics: public IO resolves to the
bound net name, while an internal net is scoped as
`<module-instance>.<local-net>`. Source-format tests resolve those IDs in memory
and require the compiler to accept the persisted positions in an isolated copy.
This tests the source adapter, not a browser-rendering workflow.

This catches source-format, identifier, placement-comment, determinism, and
connectivity regressions without depending on mocked compiler output. The
KiCad-interpreted netlist and exported SVG cover connectivity and visual
defects that the source compiler cannot detect.

Presentation-policy regressions separately use small generic in-memory
schematics. They require unique net captions to collapse to local suffixes,
ambiguous suffixes to retain only enough hierarchy to distinguish them, and
all corresponding net-symbol position IDs to be remapped without coordinate
changes. They also require component descriptions to be absent from the viewer
payload, both supported spellings of the component package attribute to be
absent, MPN-prefixed prose to collapse to the MPN, ordinary electrical values
to remain intact, repeated compound pin labels to separate into a shared
functional name plus unchanged physical numbers, and the canonical input
object and component-port mapping to remain unchanged. A single coincidental
`_<pin-number>` suffix is retained unless another pin proves that the symbol
uses the suffix as a repeated-name disambiguator. Generic placeholder families
such as `Pin_1`/`Pin_2` are also retained rather than being misrepresented as a
datasheet name. A separate pair of cases requires a net with one informative
active-device endpoint name to inherit that name while two electrically
distinct nets proposing the same functional caption both fall back to their
unambiguous net names.

Geometry tests record advisory findings for series-terminal doglegs and
compressed repeated banks using the shared source-coordinate pin transform.
They do not replace SVG review of native labels and routed wires.

A local-wire invariant fixture contains both an entirely internal signal and a
true module-boundary signal. Generation must remove the internal signal's label
anchor so the viewer wires its component pins directly, while retaining the
boundary label. The same condition is a hard review finding if it survives into
a completed proposal.

A companion attachment-radius regression places two connected generic bodies
first far apart and then locally. The distant form must fail strict review and
the local form must pass. The DigitalAbx DSP integration check supplies empty
and deliberately absurd seed maps and requires identical from-zero block
output, complete component coverage, unchanged connectivity, no body overlap,
and no remaining local-geometry finding.

`interface_blocks/parallel_transformer` is the minimal compiler-backed fixture
for the multi-active interface grammar. It uses only private generic endpoint,
driver, transformer and connector drawings plus standard-library passives. It
requires three independently bounded endpoint leaves, one parallel-driver leaf,
and a measured parent composition. Empty and deliberately absurd seed maps must
produce identical positions. The test also requires every driver resistor to
remain on its real output-pin row, the shared input net to use one symbol, every
component to be owned, no body overlap, no local-passive or dogleg finding, and
an unchanged connectivity digest. It invokes no renderer or reasoning model.

A separate DigitalAbx integration check applies that generic grammar to the
symbols authored in its copied component packages. It protects the buffer
body, the three optical channels, and the transformer/coupling/connector order
without freezing absolute coordinates. This test evaluates Zener and runs
deterministic geometry only; native SVG review remains necessary when code or
presentation changes.

The integration corpus uses the pinned ABX snapshot under `.schemer/sources`
by default. Runs may set `SCHEMER_ABX_WORKSPACE` to another checkout
explicitly. The default must not follow the live ABX workspace: integration
expectations describe one stable source revision, not whatever happens to be
under active development.

Presentation-name regressions also prove that rails retain their net names and
that a one-letter device pin such as `G`, `D`, or `S` cannot replace a useful
signal caption. Pin-aware shortening is reserved for descriptive, unambiguous
functional names.

The DSP integration set also checks the topology-derived three-terminal
presentation: the owner-connected terminal is left-facing, the control
terminal is right-facing, and the return terminal is bottom-facing. Selection
uses terminal connectivity and pin count rather than a part number or
reference designator.

Browser-renderer and Pillow pixel tests have been removed with the legacy
commands. Native annotation tests use actual field, label and symbol geometry;
visual review uses readable vector drawings, not pixel-height thresholds.

Generic regressions also prove that excessive signal-connected block gaps
compact toward the primary without moving it, vertical expansion preserves
branch order and x flow, crowded rail labels gain readable pitch without
moving components, and projection feedback selects only the component's outer
KiCad symbol rather than every sibling in its library. An adversarial ownership
case places a VDD drawing directly over an electrically unrelated resistor and
requires it to remain owned by the IC that is actually connected to VDD. This
proves proximity is only a tie-break between valid net participants.

The final group-packing regression uses a generic IC between upstream and
downstream groups, gives one peer a deliberately long visible value, and
requires exactly 100 viewer units between the resulting complete envelopes.
It then repacks the result and requires byte-for-byte identical positions. The
DigitalAbx integration case additionally proves that J1's root-positioned but
connector-owned supply and ground branches retain their complete relative
coordinates through vertical spreading, connected-gap compaction, and final
packing. All child-module coordinates remain frozen and no pair of final
complete group envelopes overlaps. Review manifests record those measured
envelopes.

The inverse block-spacing fixture starts with a generic downstream block too
close to the primary and requires the configured minimum corridor while the
primary and upstream block remain fixed. The
`repeated_active_channels_with_local_series_support` projection fixture starts
three anonymous powered devices only 150 units apart, requires 180 units of
body clearance, and proves each generic series resistor keeps its relative
channel position. A third generic regression overlaps a primary and neighbour,
then requires the primary to remain fixed while the neighbour moves clear.

Additional regressions require sequential decouplers to choose distinct owner
slots, distinguish any painted-body overlap from zero-area body-edge contact, move a
misplaced series bank between its two endpoints, and recognize redundant
number-only pin names without hiding semantic names.

## Scenario ladder

The suite grows from one semantic rule at a time toward the first complete
board:

1. `series_chain`: successive two-pin components preserve left-to-right flow;
2. `series_with_shunt`: a signal shunt is vertical, below, and attached between
   its neighbouring series stages;
3. `fanout`: downstream branches occupy one column but separate rows;
4. `decoupled_ic`: a supply-to-return capacitor stays beside its IC rather than
   entering the main signal path;
5. `ordered_decoupling`: an IC is a major anchor, both capacitors are owned by
   it, and the 100 nF part is closer than the 10 µF part despite adverse
   reference ordering;
6. `rc_low_pass`: combined series and capacitive-shunt grammar with an explicit
   input/output transfer direction;
7. common-power branches: shared filtering and bulk storage are separated from
   consumer-local bypass parts;
8. `anchor_orientation`: real pin geometry selects the major-device rotation
   that faces semantic upstream and downstream endpoints;
9. `series_rectifier`: a generic Schottky rectifier is rotated from its real
   A/K geometry so its terminal order follows the directed signal flow;
10. `repeated_series_bank`: three generic parallel branches preserve alignment
    while every real pin-1/pin-2 order faces the shared downstream stage;
11. orthogonal branch clearance: a vertical rail-to-signal branch and its rail
    symbol leave a colliding horizontal series lane as one unit, but remain
    between the signal net's connected endpoints so the repair cannot create a
    large rectangular detour;
12. terminal-chain compaction: a generic connector wired directly to a
    parallel series bank rotates its real pins toward that bank and is brought
    within a bounded endpoint gap; deliberately displaced series members also
    move toward their unambiguous endpoint-pin lines while preserving order,
    and movable leaf net symbols follow the real component pins;
13. resistor ladders: repeated taps, stable order, and equal pitch;
14. repeated active channels: stable vertical order, a 180-unit body/annotation
    corridor, local support travelling with its channel, and obstacle-aware
    lane centring; then differential pairs and isolation-domain boundaries;
15. `multi_unit_ic`: four separately placed units of a stable generic logic
    symbol compile into one package body, preserving connectivity, physical pin
    numbers, left-side inputs, and right-side outputs;
16. primary-anchor selection: the largest generic IC is selected without an
    identifier and initially moved to the sheet centre using rendered symbol
    bounds rather than stored-anchor coordinates; connected-flow compaction may
    then leave it within the accepted central field;
17. `active_connector`: a powered one-sided generic active symbol becomes a
    functional block with supply above, return below, and signal pins at the
    side, while retaining every physical pin and net;
18. `IsolationBlocks`: a connector-to-active-IC-to-connector chain is generated
    from an empty position set and from deliberately unrelated seed positions;
    both results must be identical, complete, overlap-free, and free of local
    passive, series-dogleg, and repeated-bank findings;
19. `ordered_perimeter`: a generic sequential-pin module is projected from an
    odd/even connector body to pins `1..N/2` down the left and `N/2+1..N` up the
    right, then compiled again with identical connectivity;
20. `parallel_transformer`: three generic active endpoints compose beside a
    parallel driver, common shunt, transformer, coupling part and connector;
    every leaf is generated from zero and validated before composition;
21. the DigitalAbx one-sheet integration corpus, including scale-invariant
    local-block completion before measured top-level packing and readable
    output generated only after layout; and
22. later boards only after their new semantic structures have minimal
    fixtures.

RC filters and resistor ladders are semantic scenarios even when their raw
connectivity resembles simpler chains. Their tests must name and enforce the
role that makes the drawing interpretable; they must not pass solely because a
generic graph layout happens to produce tidy coordinates.

`AuthoredRoles.zen` is the first source-role fixture. It declares series order,
shunt nodes, group membership, and function on component instances using only
generic resistor and capacitor parts. The positive test checks the resulting
relative geometry and connectivity. Its control removes the role attributes
from the otherwise identical evaluated circuit and requires the role-directed
layout to remain inactive. This prevents a future pass from replacing authored
intent with another implicit topology classifier.

Module-source persistence tests cover both scoped internal nets and ports that
are renamed by their parent. The generated source must use the module-local
names even though in-memory placement continues to use evaluated net identity.
This protects real power symbols: a stale parent-level comment is ignored by
Zener and leaves only the component pin's implicit, often vertical, net label.

`PullupBank.zen` uses a generic IC rectangle and three component-instance roles
that name distinct pins. Its test requires all three resistors to occupy their
owner pin rows, share one aligned supply bus, and retain connectivity. This is
role execution, not recognition from resistor values, signal names, or the
shape of the surrounding graph.

The package-projection tests also cover an authored, non-contiguous pin
perimeter with separate auxiliary pins. They require the main sequence to run
down the left and up the right while the auxiliary group remains below the
body. This protects datasheet pin ordering without naming a real device.

`PowerFeed.zen` uses a generic four-pin IC rectangle and a generic two-terminal
part. Its source role names one supply pin. The regression requires an inline
feed on that pin row, a north-facing source symbol, no duplicate termination on
the internal rail, the general-block path, and unchanged connectivity.

## Development rule

Add or change a layout heuristic only with the smallest fixture that explains
the intended engineering meaning. Once that primitive passes, exercise it in
the integration corpus and inspect native SVG output. Board-specific
profiles and corrective coordinate tables are forbidden; corpus intent belongs
in acceptance documentation and tests, not production behavior.

`test_genericity.py` enforces the architectural boundary by rejecting known
corpus tokens, module-filename branches, and stored production position tables.
Native project tests check authored group boundaries, seed-independent placement,
flat hierarchy assembly and physical connectivity. CLI contract tests reject the
removed commands and prove that native help imports without browser or raster
dependencies. Ordinary tests do not spend model time inspecting unchanged output.

The implementation order follows
[`placement-process.md`](placement-process.md). A fixture for a later geometry
stage cannot compensate for an absent role or ownership decision in an earlier
semantic stage.

Reference review findings follow the same rule. A recurring objective defect
found through the corpus review becomes a generic fixture or rendered check at
the earliest responsible stage. Reference designators, part numbers, board
names, and source paths may identify evidence in a review, but they may never
select the implementation behavior. Qualitative judgements that cannot yet be
made reliable remain recorded review warnings rather than brittle pseudo-tests.

Rendered integration defects that falsely change the perceived topology are
promoted to programmatic gates. The orientation pass enumerates every placed
two-terminal component on any board, obtains both terminal nets from the real
evaluation, estimates each net's anchor from its placed endpoints, and flips
inverted pin order without consulting component type, reference designator, or
board name. `series_rectifier` protects polarity-sensitive A/K orientation;
`repeated_series_bank` protects the parallel passive pattern that exposed the
90-degree viewer-coordinate error.

Full-board integration tests also preserve anchor-specific envelopes. Dense
major devices such as the Pico and ADuM3160 require substantially more support
clearance than optical modules or passive branches; a future global compaction
change must not collapse those natural-chain regions.

The local-bypass regression in `parallel_transformer` now requires one supply
termination per consumer (not another copy per capacitor), supply-side branch
placement, and ground/body separation. Odd/even fan-in tests protect placement
of rail terminations between branch rows to avoid a coincident four-way node.
These are geometry/ownership tests, not wire-routing acceptance: the actual
viewer still needs to show continuous local wires and clean junctions.

Generated multi-unit symbols must use grid-compatible pin rows. The generic
package round-trip fixture checks pin offsets on a 1.27 mm grid and a 10.16 mm
row pitch: an arbitrary 9 mm pitch previously passed endpoint-alignment tests
but produced tiny pin-exit doglegs in the real viewer. The transformer fixture
also checks that its outgoing continuation leaves from the bank's end row,
not an interior branch that creates a four-way junction. These protect the
generator causes; detail renders still check the actual routed branches.

`OpposedChannels.zen` protects connector alignment when two channel orders
disagree: one actual lane is registered instead of placing both endpoints
off-lane by averaging.

## Native KiCad adapter regressions

The native backend keeps fast deterministic tests below the compiler and
renderer boundary. Its stable schematic fixture covers UUID-based symbol and
field updates, field visibility, page settings, transactions, wire/label/
junction/no-connect creation and removal, and source preservation. Pure layout
tests cover aligned two-pin routing, the minimum required right-angle route,
dominant-face trunk joins, orphan net-symbol removal, local rail clustering,
separate outward corridors for distinct rail nets on one component face,
post-routing caption displacement around fixed electrical geometry, completed
top-level block packing, and sheet-size selection from final content bounds.
Completed-block packing measures bodies, pins, final captions, local labels,
net symbols and wires; a label extending toward a neighbouring block is a
regression even when the two component bodies remain separate. Sheet-size
selection proves containment only; visual review uses an SVG exported without
the drawing sheet and framed to the visible content.

Native output must also pass KiCad's own netlist interpretation before it is
saved. Compare pin memberships with the evaluated source, allowing display net
names to differ. This catches errors that self-consistent geometry tests miss.
Regressions cover KiCad's counterclockwise rotations, every physical pin of a
shared logical terminal, open/short/missing-pin rejection, signal-label
endpoints on supply trunks, and separate caption boxes around a component.

Single-attachment regressions cover all four pin directions without authored
roles, repeated-row alignment, idempotence and preservation of components
bridging two devices, including repeated physical pins on the far net. Rail
clustering tests reject shared routes through another net's terminal. The
parallel-output fixture checks compact spans throughout the local chain.
Straight-span compaction is tested on all four pin axes without requiring a
role. Rail regressions distinguish local endpoint ownership from distant
same-net pins, and keep markers off continuing vertical component connections.
Further regressions cover labels yielding on their own wires, whole rail
glyphs clearing adjacent label rows, mirrored attachment exits after trunk
movement, caption ownership and explicit/default junction-dot sizes.
Caption fitting also covers sliding beside a glyph's full height when its
own approach wire blocks the centred slot. Vertical-pin label tests mirror
both pin directions and both text sides, preserving straight adjacent rails.

For visual acceptance, use the review pass in
[`schematic-layout-principles.md`](schematic-layout-principles.md#review-pass).
In particular, test results do not settle glyph-to-wire clearance, caption
association after rotation, or whether a wire length or bend is justified.

DigitalAbx is the integration check. Generate from a fresh `pcb apply
schematic` result and require that proposal materialisation copied only the
`.kicad_pro` project configuration, not a previous `.kicad_sch`. Reconcile a
disposable copy with `pcb apply schematic --no-open --offline` and require a
zero exit with no topology-equivalence error. Export the untouched Schemer
result through `kicad-cli sch export svg --exclude-drawing-sheet`. Retain an
uncropped export for clipping checks, then inspect a content-framed overview
and readable detail crops. Reapplying is validation only; the reconciled file
is not the presentation artifact. Preservation of manual edits is deferred and
is not an integration requirement.

The earlier fixed-offset transformer-shunt rejection was confounded by later
sheet packing dismantling the fixture's local geometry. The corrected test
now preserves the block and checks the normal outward pin exit plus alignment
to the resulting trunk. See the two-fix follow-up (development run record; not included in this snapshot).

The September 7 user review invalidated an earlier whole-sheet pass despite
passing automated tests. In particular, `local-passive-owner-gap` and body-only
overlap checks cannot prove good annotation placement or local wire continuity.
Use the explicit visual checklist in `layout-hints.md`; renderer limitations
remain unresolved findings rather than test exemptions or acceptance excuses.

`PinExit.zen` is a two-resistor coordinate/port-direction reproducer. Its
physical connection points are collinear, but the horizontal resistor requires
an outward exit before turning toward the vertical shunt. The fixture test
asserts both alignment and outward directions; it is not a straight-wire pass.
Matched actual-viewer diagnostic copies (unchanged painted leads/endpoints,
different routing-port direction) isolate that constraint. The generator must
place the shunt relative to the shared trunk after the exits, not assume that
collinear physical pins imply the desired route. Keep normal pin definitions.

Completed-owner tests additionally require three independent isolation-chain
children, a minimum gap outside caption allowances, and growth when a local
caption grows. Wire shape is checked by native routing tests and SVG review;
the former optional WASM pixel tests are retired.
