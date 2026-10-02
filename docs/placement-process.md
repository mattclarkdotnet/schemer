# Staged schematic placement process

This document defines how Schemer gets from evaluated Zener connectivity to a
readable drawing. The outcome principles in
[`schematic-layout-principles.md`](schematic-layout-principles.md) remain the
acceptance contract; this process is the ordered method used to satisfy it.

The central rule is that **semantic placement is completed before coordinate
placement begins**. Schemer must first decide what every visible item does,
which circuit owns it, and how it relates to the engineering narrative. Only a
complete, auditable semantic plan may be converted to `x`, `y`, rotation, and
mirror values. Coordinate refinement is the last step, not a substitute for
understanding the circuit.

The preparation agent supplies semantic intent; the generator consumes it and
must not invent roles. Ordinary geometric defaults, such as aligning a single
attached component with its pin, do not need role metadata or new hints.
Evaluate the procedural baseline without explicit spatial placement hints.
Source intent describes the circuit, not which block sits beside another;
the generator chooses positions and packs the completed groups.

## First task: primary hinting

Every new board or module experiment begins with a dedicated primary-hinting
agent. The agent works in the Schemer-owned source copy, reviews every active
device and its datasheet, and writes durable module functions, component roles,
ownership and symbol corrections before the procedural generator runs. It does
not place or render the schematic.

The CLI handoff is `prepare` → reviewed source annotations → `check-preparation`
→ layout with `--preparation-review`. An unannotated input must complete this
first, even for a draft. Worklist evidence belongs in the run directory, not
source receipt fields. See the primary-hinting guide for the completion gate.

The complete task prompt and completion gate are defined in
[`primary-hinting-agent.md`](primary-hinting-agent.md). A layout run may not
start while that task has an unresolved ownership or circuit-function
ambiguity, or a material representation choice awaiting the user.
Subsequent coordinate-only iterations reuse the accepted intent;
they do not pay for another datasheet review unless relevant source changes.
The coordinator then runs the role consumers and integration suite before the
first render. Connectivity, coverage, role validity and objective overlap
checks remain hard invariants. Heuristic spacing and proximity findings are
reported with the render; the human reviewer decides whether the layout is
acceptable.

When a circuit admits materially different schematic representations, the
coordinator asks the user to choose before generating coordinates. Store the
choice as group or module presentation intent, distinct from electrical roles
and functions. It selects how the circuit is shown, not exact positions or
distances. Reuse the accepted choice; do not ask again on each pass or silently
switch representations to work around a generator defect. Routine geometric
decisions remain procedural.

By default, authored owner/support assemblies form local circuit boundaries:
wire within each assembly and label connections between assemblies. Follow
ownership transitively, including through supporting transistors; neither pin
count nor repetition establishes a circuit boundary. Unowned parts with an
authored role group form a local circuit within that group's declaring scope;
the same group name in another source module does not join the circuits. A
source module containing several assemblies is not a command to wire them all
together. Unassigned parts retain their module grouping; unresolved membership
is a source-preparation question, not permission to infer ownership from adjacent
nets. Resolve this partition before placing anything, and use it for both local
placement and native wiring. Lay out every circuit independently, including a
single series component with its shunts and owned branches. Do not place a
whole sheet by connectivity and then repair selected groups afterwards.

Local placement, label fitting, routing and final validation share the same
component-body and full-label envelopes. Boxed labels include their flag and
connection tip, not only their text. Speculative routes can rank label choices
but are not immutable obstacles; the final router must respect the selected
labels. Completed drawings are checked for label/body, label/label and
wire/body intersections before packing. A failed draft route is omitted and
reported as incomplete, never retained as a body-crossing fallback wire.

An accepted `independent-blocks` representation additionally separates otherwise
unowned components. Structurally equivalent
blocks share relative component positions and orientations, with caption space
reserved for the widest corresponding instance. Match source symbols, roles
and pin-level connectivity, not reference numbering or component values.
Text and wire clearance still require validation for each instance. Preserve
authored bank membership through annotation placement; do not reconstruct it
from an assumed pin pitch.

Conversely, a user-selected `connected-circuit` representation retains direct
connections across device assemblies within that module, such as a composite
feedback loop. It does not change ownership, roles or component positions.
Connector assemblies remain separate; a more specific child representation
takes precedence.

Before coordinates are generated, build a procedural structural inventory from
source symbols, authored ownership/groups and pin-level connectivity. Pass the
same inventory to native layout and include it in the review report with a
readable correspondence worklist. Exact owner-block and authored-group matches
are distinct from weaker same-symbol cohorts. Values and net names are not
matching criteria; ambiguous correspondences are reported rather than guessed.
Detection does not assign roles, split blocks or choose a representation.
Local geometry reuse does not require an independent-block representation.
Use exact correspondences to share local arrangements while retaining the
existing electrical groups and connections. For repeated authored series
groups, construct a compact pin-aligned channel and reuse it as aligned rows;
do not copy a scattered seed merely to make its mistakes consistent.
Representation choices govern wiring between blocks, not permission to reuse
component geometry. Each instance still needs clearance and routing checks.
The judge compares corresponding instances for consistent presentation and
missed reuse opportunities, but still inspects every local group and the whole
sheet: this bounded inventory is neither exhaustive nor a visual acceptance gate.

For a multi-sheet experiment, inspect the source-driven
[hierarchy plan](schematic-hierarchy.md) after primary hinting and before
coordinate placement. Preserve complete owned networks and verify connectivity
across the entire generated hierarchy, not merely within each sheet. Use one
root and one level of circuit sheets; group related sheets by name rather
than reproducing the source's nesting depth.

## Mandatory datasheet preparation

Before semantic placement, review every datasheet-backed IC or active module in
the evaluated design. This is a process requirement, not special treatment for
whichever device currently looks wrong. The pass emits only useful intent:
contextual module functions and static support-component roles.

Use the review to assign static roles to support-component calls in the owning
Zener module. Read the pin descriptions and reference or typical application;
do not reconstruct these roles from values, reference designators, or topology
on every layout run. Validate roles against evaluated connectivity and
datasheet pin names. A review may find no external support role. Source
citations and coverage belong in the run review, not in each Zener instance.

This metadata expresses electrical intent: function, ownership, group,
attachment pin, and constrained circuit order. It must not contain coordinates,
router schedules, bitmap dimensions, or viewer-specific corrections. Other
consumers may derive their own ordering from the same intent without putting
routing commands into the Zener source.

If a reusable module's generic symbol does not preserve its datasheet pin
perimeter, record that perimeter on the component definition. Main package
pins follow the physical edge order; auxiliary or underside access pins form a
separate group. This ordering is established before any local power or signal
part is placed, because those parts must attach to the correct face and region.

## Process artifacts

Each run produces three conceptually separate artifacts:

1. **Circuit facts** extracted from the evaluated design: hierarchy, physical
   components, symbol and pin geometry, component attributes and values, nets,
   ports, directions, power domains, and repeated structures.
2. **Semantic placement plan**: one role, placement stage, owner where
   applicable, branch or flow position, relative constraints, and a written
   reason for every visible symbol. This artifact contains no viewer
   coordinates.
3. **Geometry realization**: positions and orientations derived from the
   complete semantic plan, followed by bounded coordinate refinement.

A visible symbol without a role or rationale is an error. This makes the
“loose components scattered around the page” failure directly diagnosable
rather than a subjective surprise at render time.

Later stages may refine decisions made by earlier stages only by explicitly
backtracking to the earliest affected stage. A compaction pass cannot silently
move a decoupler away from its consumer, merge separate power branches, reverse
the signal narrative, or split a functional cluster.

## Block composition model

Failure to compose ordinary circuitry is a generator coverage bug, not an
aesthetic score. Specialised motif rules may improve a baseline, but failure
to match one must not silently retain auto-placement.

The general baseline assigns ICs and connectors as anchors, attaches
two-terminal parts through signal nets before supply nets, and starts a local
chain for passive-only networks. Sharing only a return net does not establish
ownership. Every electrical part is assigned once. Pin geometry determines
attachment direction; component drawings and annotation allowances determine
the completed block bounds before packing. This baseline does not yet infer
full regulator or filter semantics.

Where connectivity does not express circuit intent, read component-instance
roles from the Zener source before selecting a layout. Roles decide membership,
ownership, series order, and named shunt attachment; topology only validates
that those authored claims are electrically possible. Geometry then applies
the conventional drawing for that role. The generator must not reconstruct
the same intent from names, values, reference designators, or an ever-growing
set of topology recognisers on every pass.

The first role-directed layout accepts a fully annotated local series group.
It lays the declared series instances left-to-right and places each declared
shunt below its named path node. An absent role set leaves this layout inactive;
a partial or impossible role set is an error rather than permission to guess.

For an authored pull-up, validate the named owner, pin, signal net, and supply
rail. Group pull-ups by owner face and supply identity. Place every resistor on
its owner's pin row with the signal terminal toward the IC, align the supply
terminals on one vertical bus, and put one north-facing supply symbol above
that bus. Do not join banks across opposite IC faces.

Apply the same rule southward for authored pull-downs: validate the exact
owner pin and return endpoint, align one face's resistors, and share a local
return wireset. For authored inline roles (`series-termination`,
`current-limit`, `ac-coupling`, and `source-impedance`), place the part on the
named pin's outward axis before considering wire length or surrounding text.
These names are intentionally functional: two identical series topologies may
need different presentation because one limits current while another sets
line impedance or blocks DC.

Parallel supply capacitors within one owner block form a bank, keyed by both
the supply and return net identities. Orient them supply-up/return-down,
align their supply pins, and attach one shared symbol on each rail. Do not
create per-capacitor rail copies: those cause the renderer to split a bank
into separate local connections. Different return domains and different owner
blocks remain separate. Measure the completed bank before packing its owner.

An authored `divider` role records the complete resistor order and owning IC.
Preserve the path, but treat connections to functional owner pins as semantic
boundaries: align those taps with their datasheet pin rows and wire them
directly. Several physical resistors may form one logical divider leg; fold
that leg between adjacent tap rows rather than detaching the function as a
remote ladder. An unannotated resistor path remains a provisional baseline.
A shared rail is a path boundary, not evidence that unrelated resistors belong
to one chain. A shared return with several local consumers is likewise a
boundary. Standalone supply-to-return capacitors normally stand vertically,
with supply above return. An owned bypass follows its consumer's pin geometry:
it may lie horizontally beside a north-facing supply pin. Parallel parts on
the same two rails continue to use the shared bank rule above.

Discover opaque module paths from the viewer's placement inventory, including
those below transparent wrappers. Complete children before parents, and pack
their measured bounds without also positioning their internals in the parent.
Keep qualified net identities until display-label formatting. Net-symbol
metadata belongs in the source that defines the net, not necessarily the
source that owns its drawing position.

Persist each layout comment in the namespace of the source file receiving it.
An internal evaluated name such as `INSTANCE.LOCAL_RAIL` becomes
`LOCAL_RAIL` in that module source. When a parent binds a child port to a
differently named net, translate the evaluated parent name back to the child
port name. Otherwise Zener ignores the unmatched comment and falls back to an
implicit pin label instead of drawing the requested power or ground symbol.

Before sheet packing, reconcile seed symbols with completed child ownership.
A root auto-placement rail copy is redundant only when its electrically
constrained owner is a regenerated child that now supplies a local termination
on that same evaluated rail. Remove that copy; retain symbols for connectors,
unregenerated blocks and rails without replacements. This check precedes final
envelope measurement. Always inspect the complete sheet too: focused child
renders cannot reveal leftover root symbols inside component bodies.

Geometry realization is bottom-up. The generator does not place a global set
of component columns and repair the result afterward. It first creates small,
locally meaningful layout blocks, validates each block, and then composes those
blocks into larger functional blocks and finally the sheet.

A block is deliberately flexible. It may be an IC or connector body, one side
of an IC with its pin breakouts, a power branch, a series or shunt network, a
repeated channel bank, or a composite of smaller blocks. Every block declares:

- its width and height in viewer units;
- the symbols it exclusively owns and their block-local positions;
- occupied component-body envelopes;
- named ports on its four boundaries;
- rectangular corridors reserved for wires and annotations;
- child-block offsets;
- parent-owned links between named child ports; and
- the minimum spacing between its children.

Composition uses local coordinate systems. Moving a block translates every
owned symbol and child together; no later sheet-level pass may scatter its
contents. Row and column composition are merely deterministic primitives, not
the semantic model: the hierarchy can combine them in any arrangement and a
block's meaning is never inferred from its identifier.

The first implemented from-zero block is deliberately narrow: one active IC,
two connectors, direct series signal paths on both sides, connector-owned
shunts, and IC-owned bypass capacitors. Eligibility comes only from evaluated
component type and connectivity. Existing component and net-symbol coordinates
are ignored.

Its geometry realization has four fixed phases:

1. **IC anchors:** orient and place the active IC body without allowing a
   passive, connector, net symbol, or annotation to influence it.
2. **Electrical skeleton:** derive immutable wire rows directly from the IC's
   real pins. Decide which endpoints share local power or return branches
   before choosing any rail-symbol drawing anchor; exact junction coordinates
   follow from the attached component pins. A non-rail net whose component
   endpoints are wholly inside this module is a direct local wire, not a pair
   of labels; its auto-placed net-symbol anchors are discarded.
3. **Attached symbols:** snap series, shunt, bypass, connector, power, and
   ground drawings onto those established wires. A one-pin net symbol is
   positioned by inverting its real electrical-pin offset, never by treating
   its drawing-box anchor as a wire endpoint.
4. **Text last:** simplify annotation content and resolve clearance only after
   the electrical drawing exists. Component and net-symbol captions are
   movable annotation blocks; component bodies, pins, wires, junctions and
   net-label anchors are fixed obstacles. Move the complete reference/value
   pair to the nearest clear grid position without changing electrical
   geometry. A clearance adjustment may translate an attached series symbol
   along its existing horizontal wire lane, but it may not change the lane's
   `y`, alter a junction, or add a bend.

Small two-terminal captions respect the terminal axis: horizontal parts keep
reference and value together above the body, while vertical parts stack both
captions horizontally beside the body so supply and return wiring stays clear.
In a repeated inline bank, keep the bodies aligned and use one compact
reference/value row beside each body. Measure each field separately: the space
between a reference above a part and its value below is occupied by the part,
not by text when that split arrangement is retained. Power captions receive
nearby space before component titles;
device titles choose positions adjacent to their own body edges.
Recompute these placements after any component rotation or translation;
preserving old caption offsets can detach them from their new rows. Reserve
the final pin strokes, pin numbers, wires, junctions and complete rail-symbol
graphics when testing text clearance, not just their anchors.

Within this order, authored pin functions outrank provisional support-part
positions. A provisional part may be moved out of a direct functional pin
corridor; it may not push an authored divider away until the viewer replaces a
wire with duplicated labels.

The implementation represents phase-two signal runs as `_SeriesWire` values
and phase-three wire endpoints as `_NetSymbolAttachment` values. This is a
small ordering device, not a general graph router. Connectors terminate the
signal skeleton at its boundary. Shared power and return nets first acquire a
semantic branch membership, then a junction derived from their attached pins,
and only afterward a rail drawing.

The generated coordinates are final inside this block. The former second
coordinate-refinement and recomposition pass has been removed. Before sheet
packing, every top-level group receives one frozen visible envelope containing
component bodies and pins, the final reference/value positions, net-symbol
drawings and captions, local labels, and the endpoints that bound its internal
Manhattan wires. A later pass moves only the complete group and never its
contents. Body-only envelopes are not sufficient for sheet packing.

The second from-zero grammar handles an interface module made from repeated
active endpoints plus one parallel driver and transformer output. Recognition
is deliberately narrow and structural. Each endpoint must have exactly one
non-rail signal through one inline passive to a module boundary. The unique
higher-pin-count active must have one repeated boundary input, two or more
inline outputs converging on one net, a return shunt on that net, then a
transformer, one coupling passive, and a two-pin connector. Every remaining
passive must be a supply-to-return bypass. If any component is unowned, the
whole grammar declines the module rather than preserving a stray seed.

Geometry follows the same ordered process at two levels. Each endpoint becomes
an independently measured leaf containing the active body, its straight
signal lane, boundary caption, bypass and local rails. Those leaves are stacked
as a repeated-channel bank. The parallel-driver leaf starts with the IC, aligns
each output passive to its real output pin, joins their remote pins on one
vertical bus, attaches the shunt to that bus, and continues horizontally
through transformer, coupling part and connector. The channel bank and
transformer leaf are then composed with declared clearance; sheet packing can
only translate their completed parent block.

When several indistinguishable bypass capacitors and consumers share exactly
the same supply and return nets, connectivity alone cannot prove which physical
capacitor belongs to which consumer. The grammar accepts that case only when
the counts match, then pairs both source-stable sequences. This tie-break is
generic, deterministic and explicit; an unequal count remains an ownership
failure.

Validation also proceeds bottom-up. Before a block can be composed, Schemer
requires unique symbol ownership, containment of every item and corridor,
ports on their declared boundaries, no positive-area component-body overlap,
no annotation/corridor overlap, no crossing wire corridors for distinct nets,
resolved child-port links with compatible nets, and the declared clearance
between siblings. A child port may participate in only one parent link.
Same-net wire regions may join inside a block to represent an intentional
junction. Parent corridors may touch a child boundary but may not intrude into
the child's local area.

The Zener viewer still owns final wire routing. Reserved wire rectangles are
therefore a placement contract and an early rejection mechanism, not a second
schematic renderer. After flattening a validated block plan into a
`LayoutPlan`, the real viewer remains the oracle for the exact routed result.
Any recurring mismatch between a reserved corridor and viewer output becomes
a small block fixture at the earliest responsible composition level.

Distance from a bad seed is never a reason to skip an unambiguous semantic
attachment. Once topology identifies a series part and its leaf symbol as an
IC-owned branch, they move to the IC's real pin row regardless of their initial
auto-placement distance.

An IC-local block is complete only when every classified component has been
placed from the anchor's real pin geometry. For the currently supported simple
grammar, the order is: unique largest IC, direct boundary series paths,
directly controlled smaller active devices, rail shunts and two-terminal power
feeds, then rail and signal captions. The builder starts from an empty position
map and aborts the whole transformation if even one component is unclassified.
It never preserves a distant viewer seed as a fallback.

Large two-row modules use an expanded intrinsic pin pitch. This is part of
their local attachment geometry, not a page-height target: nearby reference,
value, and net text must fit between adjacent branches before any bitmap size
is chosen.

A three-terminal control device whose one signal joins a larger local IC, whose
other signal drives a support branch, and whose third terminal is return is
drawn as a small functional block. The owner-facing signal is on the left, the
control signal is on the right, and return is on the bottom. This keeps the
datasheet terminal names while preventing an authored transistor glyph from
forcing a control caption onto a vertical wire.

## Stage 0: extract circuit facts

Evaluate the entrypoint with the real Zener compiler and record, without
placing anything:

- the root and its direct-child functional modules;
- every physical component and its stable viewer identity;
- component type, value, package, and relevant semantic attributes;
- every component pin, its symbol-side geometry, and its attached net;
- net kind, name, public direction, domain, and endpoints;
- repeated channels, differential pairs, buses, and isolation boundaries;
- mechanical and service-only items eligible for the explanatory-view policy.

The result is validated for complete component and endpoint coverage. Names
may provide hints, but component type, pin connectivity, and hierarchy take
precedence over source order or reference-designator order.

## Stage 1: classify roles and ownership

Classify every visible component before any coordinates exist. Initial roles
are:

- **major anchor**: connector, IC, transformer, regulator, isolator, or another
  device that establishes a functional block;
- **local decoupling**: supply-to-return capacitor owned by a particular
  consumer and supply domain;
- **common power**: conversion, protection, filtering, bulk storage, or
  distribution shared by a block rather than one pin;
- **signal path**: an inline signal component;
- **signal shunt**: pull-up, pull-down, termination, bias, filter, or feedback
  element branching from a signal path;
- **service**: test or debug access; and
- **mechanical**: mounting or other non-electrical board structure.

Ownership is explicit. A support component cannot be placed until Schemer has
identified its consumer or its common branch. If inference is ambiguous, the
board profile must supply intent or the run must report the ambiguity; the item
must not fall through to a miscellaneous placement bucket.

The same rule applies to one-pin net symbols. Zener persists a net-symbol
anchor as a separate `sym:<net>#<index>` position, but that is only a storage
detail. Semantically the symbol terminates a wire belonging to a connected
component-pin branch. Schemer first limits possible owners to top-level groups
that actually have a port on the symbol's net. Geometry may distinguish
multiple displayed copies of one shared net only after that connectivity
filter; it may never attach a symbol to an unrelated nearby component. Once
selected, the component or module anchor, its local wire endpoint, and its net
symbol form one group for every sheet-level translation.

A supply-to-return capacitor is a hard ownership case. It must either share
both rails with one placed non-passive consumer or belong to an evidenced
shared power branch. Otherwise generation fails as an unowned local passive.
The rendered review separately reports a local passive whose symbol envelope
is more than 4 mm from its inferred owner. “Lone passive” is therefore a
machine-checkable red flag, not something deferred to visual scoring.

This stage also rejects unexplained leftovers. Complete classification is the
hard boundary before semantic positioning.

## Stage 2: place major anchors and functional blocks semantically

Establish the circuit narrative using only relative relationships and
topological slots:

1. enumerate physical IC packages, rank them first by evidenced pin count and
   then by actual symbol envelope, and select the largest as the primary anchor;
2. establish each IC or connector body as the anchor of a local block, without
   assigning that completed block a sheet position yet;
3. order top-level functional blocks from sources to transformations to sinks;
4. assign connectors and other major ICs to the appropriate narrative stage;
5. identify peer fan-out branches instead of forcing a false serial chain;
6. reserve a semantic envelope and wire corridor around each anchor; and
7. assign initial pin-flow expectations, such as inputs on the left and
   outputs on the right.

“Largest” is an evaluated circuit fact, not a board-profile reference. The
current tie-breaks deliberately make a dense controller or processor outrank a
small interface IC, while connector, passive, transformer, mechanical, and
service types are not IC candidates. Top-level packing holds the selected IC's
completed functional block fixed while placing peer blocks around it; its local
support has already been included in the measured block envelope.

Primary selection establishes the main narrative anchor; it does not establish
a page scale. The primary's eventual fraction of an image naturally depends on
the number and size of its peer blocks. No viewport dimension, target page
height, bitmap resolution, or zoom may participate in semantic placement.

No small passive is allowed to determine the location of a major device. A
major anchor may move later only if the semantic plan is explicitly revisited.

## Stage 3: add power structure

Power placement is performed after anchors exist and before ordinary signal
support:

1. assign each local bypass capacitor to the exact consumer, supply domain,
   and preferably supply pin it serves;
2. expand every logical supply or return terminal into all of its physical
   symbol pins, then group those pins by net and component-body face;
3. give each net-and-face group exactly one local wireset and rail termination;
   repeated pins on that face share it, while occurrences of the same net on
   different faces receive separate local terminations instead of an
   around-body wire;
4. identify shunts with the same owner, owner face, and return net as one
   semantic return bank, then give the bank one common return bus and one
   ground symbol; isolated shunts and bypasses retain a nearby local
   termination;
5. order capacitors nearest to farthest by their high-frequency role—initially
   lower nominal capacitance first, with smaller package as a tiebreaker;
6. keep the nearest bypass inside the consumer's local pin region;
7. collect shared conversion, protection, filtering, and bulk capacitance into
   a separate common-power branch above the supplied block; and
8. represent distant or global rails with local power symbols instead of
   page-spanning wires.

When a datasheet identifies an external two-terminal supply feed, record its
owning device and supply pin as source intent before layout. The generator then
keeps the feed in the owner's power region, with a north-facing source and a
short inline connection to the named pin, rather than treating it as an
ordinary rail-to-rail neighbour.

Rail termination count is derived from these semantic face groups, not from
the number or positions of pre-existing `# pcb:sch` symbols. Logical pin names
are not unique physical coordinates: a package may paint one terminal on
several pins. Collapsing those occurrences before grouping by face is invalid
because it forces the router to join unrelated sides of an IC with large loops.
The return bank is decided before coordinates are refined; geometric proximity
alone never merges grounds from different owners or component faces. Comparable
bias components on the same face use one aligned bank, even when their rail
nets alternate. Shared trunks sit outside the component endpoints, retaining
the renderer's required outward exit. Grounds point south and supplies north;
on a lateral pin this normally means an outward stub followed by one turn.
Only a geometry requiring more than one turn, or an explicitly approved
exception, permits another orientation. Pin direction controls the first wire
segment, not automatically the rail glyph's orientation.

Capacitance ordering must be based on value and ownership, never on reference
designator. The `ordered_decoupling` fixture deliberately names the 10 µF bulk
part before the 100 nF local part to enforce this.

The current generic implementation handles owned supply-to-return decouplers
and a first deliberately narrow common branch: an upstream bulk capacitor and
ferrite feed are separated from a consumer-local bypass. Exact supply-pin
ownership and more complex regulator, protection, and multi-consumer trees
remain explicit later scenarios rather than being approximated with arbitrary
coordinates.

## Stage 4: add signal networks

Place smaller signal structures around the already-established anchors:

- put series parts between their actual endpoint pins;
- draw shunts, bias, pull, and termination branches vertically;
- keep feedback components in a compact loop around their active device;
- maintain stable adjacent ordering for differential and bus members; and
- align repeated branches and channels as semantic copies.

Signal passives may refine a reserved local envelope but may not displace a
major anchor into another functional block.

## Stage 5: choose semantic orientation

First apply circuit-grammar constraints: inline passives are horizontal,
shunts are vertical, and isolation barriers and repeated structures retain
consistent orientation. Then enumerate permitted rotations and mirrors for
major symbols using their real pin geometry.

Orientation candidates are compared by:

1. whether required input/output and supply/return pin sides are preserved;
2. estimated local wire-loop area;
3. pin-to-owner and pin-to-next-stage path length;
4. required bends and crossings; and
5. label and symbol-envelope conflicts.

Two-terminal elements have an additional hard constraint: each physical
terminal must face the placed endpoints on its own net. Schemer derives both
terminal nets from the evaluated connectivity and derives their geometric
anchors from every placed component and net symbol on those nets. When the
anchors lie on opposite sides of the part, real pin order must agree with
anchor order. Otherwise the viewer can cross the two feeds and draw an
enclosing loop that makes the component appear shorted or bypassed. That is a
false schematic, not a cosmetic crossing, and it is corrected before
coordinate refinement.

This is still semantic placement: the output is an allowed orientation and pin
side relationship, not final coordinate tweaking. If no orientation satisfies
the hard relationships, Schemer backtracks to the affected anchor or branch.

The implementation parses actual named and numbered pin offsets from the
evaluated symbol data using the viewer's y-down coordinate convention. It
applies the same topology-derived pass to every evidenced two-terminal
component, independent of board, reference designator, and component type. A
180-degree flip preserves the chosen horizontal or vertical circuit grammar
while exchanging terminal sides. Components without two clear opposing net
anchors retain their earlier semantic orientation rather than receiving an
identifier-based exception. Mirroring, exact multi-unit pin-to-unit anchors,
and label envelopes will extend the same candidate process.

## Stage 6: realize coordinates

Only after every visible symbol has a role, owner where applicable, relative
slot, and allowed orientation may Schemer assign viewer coordinates:

The primary-hinting pass owns those semantic decisions. The procedural
generator never creates roles or ownership from connectivity. Connectivity is
used only to validate authored intent; a specialized placement pass must fail
clearly when required intent is absent instead of inventing it.

1. place the IC or functional device body that anchors each local block;
2. establish the wires leaving its actual pins, including shared local
   branches and junctions;
3. attach passives, rail symbols, and other endpoint symbols to those wires;
4. place and simplify text after the electrical geometry is stable;
5. measure the completed local block from its bodies, pins, wires, symbols,
   and annotation allowances;
6. pack those measured blocks into the sheet using their semantic order and
   minimum spacing;
7. place connector-only stub blocks together in remaining sheet space, without
   using them to align or widen the functional blocks; and
8. flatten the validated block tree into deterministic viewer coordinates.

Component and net-symbol coordinates use the same persisted bounding-box
anchor convention, but that does not make their stored `x` and `y` electrical
connection points. Local VCC, GND, and other one-pin symbols are first assigned
a desired wire endpoint, then their evaluated KiCad pin offset is inverted to
derive the stored anchor. Comparing or copying the component pin coordinate
directly into the net-symbol position creates a small unexplained dogleg.
The first segment follows the terminal's real outward axis. The rail endpoint
then follows conventional supply-north / ground-south orientation when possible
with at most one turn. This deliberate L is not an unnecessary dogleg.
The same wire-first order applies to series and shunt banks. First place each
passive on one real owner-pin row. If adjacent annotations then compete, assign
different horizontal distances or enlarge the block; never spread the rows
away from their electrical pins. When opposite endpoint pin orders make one
bend per series link unavoidable, keep one endpoint exact and record the pin
order constraint rather than moving the passive between both rows and creating
two bends.

For an orthogonal shunt, reserve an actual branch junction and a nonzero
approach to the shunt pin. A vertical pin cannot sit directly on a horizontal
wire row without forcing the viewer to leave and return to that row. For a
directly connected subordinate device, measure neighbouring primary-owned
branches and rails first, then widen the shared horizontal connection enough
to clear them. Keep the shared bias branch between its two owners; do not
replace that connection with duplicate labels.

Rail-body clearance is local to an owner. Independent blocks must not be
compared in their temporary local coordinates. Do not subsequently move rail
symbols with a text-spacing pass: complete the wiring, then measure caption
allowances for block packing. A plain net-label position is currently not a
reliable wire endpoint in the installed viewer; see the toolchain limitation.

This pass should create a valid first drawing without relying on optimization.

Symbol coherence precedes coordinate placement, but symbol selection is source
authoring rather than layout. The `Symbol(...)` selected by the evaluated
component is authoritative and the layout pass may not replace it. When review
finds a generic connector, numbered box, or unsuitable multi-unit drawing,
correct the copied component package first, validate every physical pin and
rebuild. A symbol-generation helper may prepare that source edit, but its
output must be persisted and reviewed before the coordinate pass begins.

## Stage 7: coordinate refinement

Coordinate tweaking belongs here. It may compact local whitespace, align repeated
items, shorten wires, and reduce bends and crossings.
Candidate moves and rotations are scored through the real viewer whenever
possible.

Top-level packing moves completed groups, not their contents. In native output,
compare row arrangements by the area of their enclosing standard landscape
frame (√2:1). Preserve authored relative ordering; otherwise start with the
primary group and use a stable order. Pack locally labelled connectors together
after the functional groups, wrapping the bank rather than widening the sheet.
Measure complete envelopes and retain the minimum gap between them. Every
component, wire and annotation receives its group's translation. Seed positions
do not prescribe columns or sheet shape.

The packer rejects any positive-area intersection between the translated group
envelopes before producing a plan. The review gate recomputes those same
complete envelopes; it does not substitute component-body bounds or omit text
and net symbols.

Repeated active devices aligned in one vertical lane are then spaced from real
projected body bounds. Three or more bodies receive at least 180 viewer units
of body-to-body corridor. A directly connected non-rail passive moves with its
channel; an all-rail support part is assigned by shared rails and proximity;
net symbols follow the nearest connected component. The complete lane is
centred between unrelated obstacles with an 80-unit boundary corridor, so
clearing a transformer or connector chain does not destroy the lane pitch or
move one passive in isolation.

If local composition reveals a body collision, the anchor remains fixed, the
neighbour moves outward along its existing dominant relative axis, and directly
connected passive support farther along that axis travels with it. This repair
must complete before the local block is measured and packed.

Parallel power and ground symbols receive a final text-width-aware lane pass.
Symbols sharing a row retain their order and y lane, but gain enough horizontal
pitch for the viewer's rail names. Components and ordinary signal symbols do
not move in this pass.

Sequential support placement updates the collision model immediately after
every move. A later decoupler therefore sees slots already claimed by earlier
decouplers instead of choosing the same apparently free owner position.

Simple horizontal series parts with two unambiguous endpoints are centred
between those endpoints on both axes, even when a poor seed placed both
endpoints on the same side. This prevents a series body from sitting on its
transformer, connector, or other neighbour.

The refinement review flags avoidable doglegs: short orthogonal jogs between
two components intended to form one straight local chain. Equal component `x`
or `y` coordinates are not evidence of pin alignment, because different
symbols, rotations, and multi-unit bodies may use different rendered placement
anchors. Schemer should compare actual transformed connected-terminal
coordinates and, where circuit grammar and clearance allow, shift a component
perpendicular to the flow until those terminals are collinear. Doglegs required
by fan-in, pin pitch, branch separation, or obstacle avoidance remain valid but
should have an identifiable reason.

The first implementation applies that check to a simple horizontal
two-terminal element with exactly one placed neighbour (or a net-symbol
anchor) on each side. It uses viewer-equivalent pin coordinates and shifts the
series element toward the midpoint of the two endpoint pins. Shared fan-in or
fan-out nets are skipped: their offsets can be semantic and require a later
grammar-aware rule. Repeated elements in one lane are refined as a bank: their
endpoint-derived order and electrical pin rows are retained. When device pin
pitch is too tight for adjacent annotations, the elements move into separate
horizontal-distance lanes; annotation clearance is not allowed to create a
residual vertical dogleg.

If one side of such a series element is a real component pin and the other is
only a local net symbol, that net symbol is a movable leaf rather than a fixed
anchor. The leaf symbol and series element align to the real pin together;
close parallel leaves alternate between horizontal-distance lanes while all
retain the real pin rows. This is the generic mechanism for hanging natural
chains from a major IC perimeter.

The leaf pass also uses the actual side of the connected device pin. Branches
on opposite sides of a large IC are never collapsed into one bank merely
because their pin rows share a `y` coordinate. Banks are grouped by physical
owner and pin side, receive horizontal annotation clearance independently, and
move their leaf symbols with them.

Refinement also reserves distinct lanes for incompatible circuit grammar. A
vertical rail-to-signal support branch may not occupy the same clearance lane
as a nearby horizontal series chain. When topology and real terminal geometry
identify that conflict, the support branch and its attached local rail symbol
move together to the midpoint of the connected signal-net span. That midpoint
is the shared-node trunk between upstream and downstream components. Moving a
shunt beyond all connected components is forbidden because it creates the
large rectangular “loop-the-loop” detour seen in early DigitalAbx renders.
This rule is independent of component type, reference designator, board, and
package.

Terminal endpoints receive a complementary compaction pass only when they are
part of a deliberately direct local circuit. When a connector is wired
directly to one or more horizontal series elements and the endpoint gap is
excessive, the connector first rotates so its real connected pins face that
bank, then moves toward it with sufficient symbol-and-label clearance.
Nearby one-rail support branches and local net symbols on the connector's own
nets move with it, preserving the endpoint cluster rather than leaving its
support network behind. Connector classification comes from evaluated
component metadata; adjacency comes from connectivity, never a board-specific
reference.

Before midpoint alignment, a terminal connector is translated vertically by
the median delta between its connected bank pins and the opposite device pins.
This removes whole-bank offsets while preserving the connector as one body.
If the two endpoint devices expose opposite D+/D- orders, a residual symmetric
dogleg is retained and reported rather than hidden by overlapping the pair.

Refinement is bounded by the semantic plan. It must not alter ownership,
relative stage ordering, circuit grammar, branch identity, domain separation,
or local-versus-common power placement. A proposed move that breaks one of
those constraints is rejected even if it reduces aggregate wire length.

## Stage 8: place or suppress service and mechanical items

Service and mechanical elements are processed only after the electrical
narrative is stable. The explanatory electrical view may suppress mounting
holes and redundant rail test points. If the raw extension view must display
them, they occupy a quiet aligned margin and never expand or fragment the main
electrical layout.

## Stage 9: render, inspect, and round-trip

Every complete candidate is:

1. applied to a Schemer-owned copy or in-memory netlist;
2. re-evaluated to prove unchanged connectivity;
3. rendered using the installed Zener extension viewer;
4. checked against hard geometry and semantic gates;
5. captured as both a full-sheet overview and readable detail views; and
6. regenerated to prove deterministic, idempotent source comments.

The rendered overview is measured at pixel level after capture. Schemer finds
the actual teal schematic-annotation glyphs emitted by the installed viewer and
requires a typical height of at least 28 pixels. A coordinate-space estimate
is retained as diagnostic evidence but cannot pass legibility by itself. The
current viewer ignores both source-property-only font scaling and VS Code theme
font-size changes for these annotations, so accepted review evidence is
captured at 6400×4266. A capture below 28-pixel typical annotation height is
diagnostic evidence only, not an acceptable readable schematic.

The same pixel analysis records the visible content bounding box and occupancy
as output-framing diagnostics. They do not feed back into layout or impose a
page-relative target on the primary IC.

For native KiCad output, use a standard landscape aspect ratio (√2:1) for both
packing and whole-sheet previews. After packing, use the same completed-drawing
bounds to choose the smallest standard sheet. Keep the drawing at least 20 mm
from the paper edge (inside KiCad's default frame and coordinate band), clear
the title block, and centre the packed drawing in the usable sheet area. Test
the actual block envelopes against the title block rather than reserving an
empty strip across the whole page. Paper size does not set component scale.
Inspect a frame-inclusive export as well as borderless review SVGs.
A tighter preview frame must retain the standard ratio and contain the complete drawing.
Do not stretch the drawing or crop it into a panorama.

Preserve the authored KiCad symbol geometry and font settings. Layout may move
and rotate symbols and their fields; it must not resize symbols, change fonts,
or shrink text to make the drawing fit. Whole-sheet preview reduction is not a
reason to alter the underlying schematic scale.
For stacked reference/value captions, separate their baselines by two text
heights; do not compress the lines to fit. Move the caption pair together when
it needs clearance.

Derive nearby caption alternatives from obstacle edges as well as the usual
positions around a glyph. Text is not constrained to the electrical grid:
bank captions can slide by the required clearance along their existing row
without moving the component or its wires. A failed caption search must report
the unresolved item, not silently treat its original overlapping position as
acceptable.
Signal labels must account for local bodies and component captions. They may
move outward on their existing pin axis when that clears an obstruction.
On a straight connection between opposing pins, keep the label between those
pins, on either axis; rotate the text for vertical connections. Reserve its
measured width, including adjacent component captions, before
routing; do not place it beyond the far pin inside the connected device.
Preserve net-label vertical justification in the adapter and collision bounds.
An inline local label sits above its wire with bottom alignment, not centred
on the conductor.
On compound nodes, prefer a label position on the existing clear connection
over introducing a new branch or moving the trunk. Check both the text and its
electrical anchor against actual pin strokes and bodies. Text movement comes
before wire detours.

An authored series path must leave room for all shunts at a node before the
next series stage. Preserve that ordered geometry through native alignment;
do not independently pull a shunt across neighbouring nodes. Exclusive pin
attachments are assessed within their authored circuit, not against remote
connector endpoints that will be connected by labels.

Native local placement uses actual KiCad pin endpoints. An authored bypass on
a north-facing supply pin branches horizontally; put the supply arrow on the
owner's pin axis and let the remote return turn south once. A simple outward
rail termination uses the normal pin stub without an additional branch offset.
Raise a side-pin bypass branch when the next lower pin's separate termination
needs that glyph space. Keep the branch attached to its authored supply pin;
do not route through signal rows or combine supply and logic-tie wiresets.
Separate rail corridors only when their vertical spans conflict, not merely
because distant channels share an x-coordinate. Align a perpendicular shunt
with the shared trunk after accounting for the other pins' outward exits.
Shorten existing clear, collinear component-to-device connections before
placing annotations; this needs no role and does not choose a new attachment
axis. Preserve spans required by bodies or measured caption widths.
Test the proposed destination, not the swept area of dragging the component
there. Geometric refinement must stay within the same completed block and must
not pull a member out of an authored series path or divider.
Check each rail corridor against only the pins served by that local symbol,
not every same-net pin in the block. Put rail markers on a short side branch
when an inline marker would merge visually with a component on a continuing
vertical connection.
Retain rail-cluster membership through clearance moves, pruning and routing.
Do not assign the pins again by proximity after moving their termination.
Use the symbol's electrical pin types to keep supply/return pins separate
from signal pins tied to those rails. Authored bypasses join the supply
wireset; pull-ups and pull-downs join bias wiresets. Do not join same-face
rail pins across intervening signal exits, or undo this separation when
merging nearby glyphs.
Check complete rail glyphs against neighbouring conductors as well as text,
using the adjusted routes rather than only the initial pin stubs. Share a
termination when clearance brings same-net glyphs together.
Route within each completed block's geometry. Preserve outward pin escapes;
reject routes through component bodies or other nets' terminals. Try clear
orthogonal alternatives with fewer bends before shorter distance. Ordinary
interior wire crossings are distinct from collinear overlaps and false tees.
Fail explicitly if no clear route is available.
Try net-label translation along its own pin exit, then rotation, before
displacing a rail corridor. Include the native rail glyph and junction-dot
bounds in clearance checks. If a shared trunk must move, preserve the outward
pin exits of its attached symbols rather than doubling wires through bodies.
Procedural parallel output chains use local pin-exit clearances, not fixed
wide routing gaps; reserve space for independent return branches. Before
merging nearby rail terminations, reject a shared route that passes through
another net's terminal.
Default a single attached two-terminal component to the device pin's axis,
without requiring a role. Derive the connection from the physical pin nets,
rotate the attached pin toward its owner and use a short straight span.
Move outward along that axis only when a component body blocks the position.
The first member of an authored series path is not an exclusive attachment
when other passives share that pin. Lay out its declared network together;
for a series/shunt pair, keep the shared junction clear and allow either
member to turn away from the device's pin axis.
Do not relocate a bridging component attached to two devices as though it had
one local owner; repeated same-net pins still count toward that distinction.
Apply authored branch arrangements after this default, then place rail
symbols and text. A south-facing ground does not require a vertical resistor.

Top-level child envelopes are also measured from compiled symbol geometry.
Any physical overlap between functional blocks is a recorded hard finding.
Signal-connected side-by-side blocks must additionally retain the 100-unit
component-envelope corridor.
Distinct physical component bodies also have a hard any-overlap gate. The
comparison uses painted body primitives without pin strokes or endpoints, so
pin collisions remain a separate wire-path defect while any positive-area body
collision is rejected.

The inspection is a recorded self-review, not an invitation for the user to
find the first obvious defect. It compares the overview and every current
cluster detail with the promoted visual grammar in
[`reference-schematic-corpus.md`](reference-schematic-corpus.md). Each finding
records severity, confidence, visual evidence, the violated principle, and the
earliest stage that can correct it. A candidate with a known hard defect or a
missing mandatory current detail capture is withheld.

Official references are not copied blindly. The review distinguishes visual
exemplars from schematics that are useful only for circuit semantics and from
negative examples whose presentation should not be reproduced.

The hard geometry gate rejects any apparent short, apparent bypass,
self-crossing series branch, ambiguous junction, or wire crossing through a
symbol body before a full-board candidate is presented. The generic
two-terminal pass compares named terminal sides with the placed endpoints on
their respective nets and requires both orders to match. The repair is made at
semantic orientation or branch placement; later coordinate polishing is not
allowed to conceal the fault.

Rendered review also reports avoidable doglegs as a quality warning. This
check operates on rendered or viewer-equivalent terminal coordinates, not just
stored component origins. A repeated bank is suspect when corresponding
branches contain identical unexplained jogs even though straight connections
are available.

Review detail images must be shown at no less than twice the effective scale of
the rejected early DigitalAbx renders. Symbols and labels must be comfortably
readable at the delivered viewing scale. A full-sheet image may provide
orientation, but it cannot be the only evidence of legibility.

## Generic application order

Every entrypoint starts from the same process; a board cannot replace or amend
it with a coordinate profile:

1. discard the selected module's stored `# pcb:sch` positions in memory;
2. ask the installed Zener viewer to auto-place the root from the evaluated
   hierarchy and connectivity;
3. treat a direct child as an opaque one-level block exactly when the root
   viewer returns a `comp:<child>` position for it; transparent wrappers remain
   on the parent sheet;
4. evaluate each opaque child source and ask the same viewer to place its
   complete module boundary and contents;
5. map source-local component IDs to the configured parent instance using
   evaluated component type, reference family, and stable order; if standalone
   evaluation is unavailable, use the focused parent evaluation;
6. classify service-only direct children from semantic metadata such as
   `skip_bom` plus `skip_pos`, never from their names or designators;
7. normalize symbols, build each IC/connector-local electrical block from its
   body through wires and attached symbols, then place text;
8. measure and pack the completed blocks without a page-size target; and
9. fit the resulting scale-invariant schematic to overview and detail outputs
   through the real viewer before scoring the candidate.

DigitalAbx remains the first full-board integration corpus. Its intended story
is an acceptance criterion for the generic stages, not executable placement
data. A missing primitive must first receive a minimal semantic fixture and an
auditable placement trace.

For independent connector/IC blocks, first test whether at least three
distinct one-to-one signal lanes align. Keep that bundle directly wired;
otherwise terminate the interface locally by name. Support parts belong on
their authored owner's actual pin rows, not another device's rows or the
average of duplicate pin rows. A device at the far terminal does not make
an explicit owner ambiguous. Preserve shared-node branches rather than
forcing them inline. Attach their named endpoints
after horizontal annotation clearance, so the endpoints cannot be left behind.
Shortening series-part stagger is not automatically an improvement: check
annotation clearance as well as the actual rendered wires.

After functional blocks are complete, pack external-interface stub blocks as a
separate bottom bank, wrapping within the sheet ratio. Include optical
receivers and transmitters by component type, not reference prefix.
Their original coordinates and apparent
alignment with an IC carry no meaning and must not increase the functional
drawing's width.
Each external interface is a separate local block even when several share a
source module or live inside an IC's module. Explicitly owned support parts
travel with that interface. Local label selection, wiring and final packing
must use the same block membership; proximity never joins separate blocks.

Named interfaces use a small presentation-only net glyph persisted in the
proposal shadow. They retain the original net and its complete membership;
they are not disconnected replacement nets or distance-based routing tricks.
The source projection currently requires a local, top-level `Net(...)`
assignment and rejects unsupported bindings rather than guessing a rewrite.

When a bypass return symbol competes with the adjacent signal endpoint bank,
move the complete capacitor/return branch outward, keeping the supply-pin row
fixed. Include that extent when measuring the finished block. This can trigger
the existing passive-owner-distance advisory; do not silently relax its
threshold to hide the tradeoff.

Recheck redundant root rail symbols after final packing and preservation of
completed children. Geometry-derived ownership can change during packing;
an early cleanup alone cannot prove the saved proposal is free of duplicates.

For a supply shared by separated pin banks on one IC face, do not connect
those banks through a trunk crossing intervening signal pins. Each separated
bank gets its own local supply termination. Place the bypass at the bottom
bank's last pin so its return can leave downward; keep the upper supply bank
locally terminated. The cap, return and supply tee form one complete branch
outside the short signal-stub region. This is a drawing rule for an existing
net, not a change to capacitor values or electrical connectivity.

Distinguish a compound rail-feed branch from a simple pin termination before
placement. The primary-IC generator reserves three ordinary net-stub lengths
between the owner pin and the attached feed pin; a shared supply tee lies at
the midpoint. Previously it used one short rail stub for that entire span,
leaving the tee almost against the IC. This rule follows the branch topology,
not any component identity.

Primary rail terminations yield to established series-signal lanes. Measure
the lane from the IC through the series body and its outward boundary label,
then move a conflicting lateral rail glyph outward without changing its row
or north/south orientation. Repeated same-net terminations on that owner's
face share the resulting outward lane. Moving just one too far can otherwise
make the renderer reassign its pin to a nearer, unintended same-net symbol.
This alignment is owner-local: it does not join rails across IC faces or
between unrelated devices.
Neutral named endpoints inherit the direction of their wire lane. The viewer
routes into a symbol pin according to its orientation even when that pin has
zero length, so leaving every endpoint vertical creates a renderer dogleg on
horizontal stubs. Endpoint orientation is fixed before distance is considered.
When allocating room for a subordinate branch, test its actual candidate
envelope against obstacles in outward order. An obstacle at the same height
but beyond the candidate does not require moving the branch beyond it.

Connector attachments follow the same pin-axis default as other local parts.
A shared return trunk uses the required pin exit rather than an oversized
label stub, but its complete glyph and caption must clear the next signal
lane and its components. Check the proposed shared route before merging rail
terminations; reject a merge that crosses another terminal or body. Preserve
established signal rows while finding clearance. Then inspect the renderer:
shorten unnecessary spans without replacing useful wires with labels or
leaving unused tails at junctions.

Completed local drawings are now immutable inputs to the sheet packer. In the
isolation-chain generator, connector breakouts and the IC support network are
separate children measured after parts, net glyphs and caption allowances are
present. Packing preserves all local rows; later legacy passes cannot move
individual items out of those children. Full-pin and annotation extents size
the child; component-body rectangles remain a separate collision check.

For a parallel bank feeding a vertical shunt, align the shunt with the shared
trunk after the renderer's required outward pin escapes, not with the bank's
physical endpoint column. The two-fix record (development run record; not included in this snapshot)
documents the calibrated viewer distance, regressions and rendered evidence.
