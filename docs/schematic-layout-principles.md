# Principles of good schematic layout

A schematic explains a circuit; it is not an inventory of pin-to-net
assignments. Its structure should reveal signal flow, power relationships,
functional groups and useful local topology to a reader unfamiliar with the
design.

These principles apply to any design. Board-specific acceptance notes,
component references, example renders, implementation constants and tool
limitations belong in separate documents. Evidence behind the drawing
guidance is recorded in [layout-sources.md](layout-sources.md); the
implementation process is in [placement-process.md](placement-process.md).

## 1. Preserve electrical meaning

1. Preserve component identity, pin identity and connectivity. Layout must
   neither invent a connection nor imply a short, bypass or open circuit.
2. Show every electrically meaningful component in the selected view exactly
   once. Mechanical and service-only objects may be omitted from an
   explanatory electrical view without changing the underlying design.
3. Make unused pins explicit rather than leaving apparent dangling wires.
   Trim unused wire tails after joining branches; a junction must not leave
   an unexplained open-ended spur.
4. Keep crossing wires distinguishable from connected junctions. Prefer
   ordinary T junctions; avoid ambiguous four-way crossings, coincident tees
   and out-and-back hooks.
5. A wire may not cross a component body or unrelated annotation. Text,
   component bodies and unrelated local groups must not overlap.
   This applies to every symbol graphic, including rail symbols, and to
   wires on the symbol's own net. Meet only intended connection points;
   electrical continuity does not permit a wire through a graphic.
6. A local circuit must be understandable without knowing its intended
   function beforehand. Distinct components, rail symbols and their named
   nodes must not read as a composite or malformed symbol merely because
   their individual graphics do not overlap.

## 2. Explain functional structure

1. Choose a primary reading direction from the circuit's function. Signal
   paths usually read left to right; power relationships usually read top to
   bottom. Mixed-direction circuits can use different directions within
   clearly distinguished branches.
2. Arrange conversion, isolation, buffering, protection and switching stages
   in their electrical order. Peer branches should look like peers, not like
   an invented serial path.
3. Give each major device a coherent wiring surface with meaningful pin names
   and a sensible authored pin order. Preserve useful symbols instead of
   reducing every device to an undifferentiated numbered box.
4. Group each active device with the circuitry that serves it. A local
   passive must have an evident owner or role, not appear as an unexplained
   island.
5. Use hierarchy only where it helps explain the circuit. Expand useful
   internal circuitry together; thin wrappers need not create extra visual
   boundaries. Place completed groups as units rather than scattering their
   descendants. Source modules are not automatic sheet boundaries: combine
   small related sections onto shared sheets. Terminate cross-sheet signals
   on the circuit itself, not in detached banks repeating the same net names.
   Overview sheet boxes provide navigation, not another copy of each net name.
6. Make electrical domains and isolation boundaries evident. Do not
   interleave support circuitry belonging to opposite sides of a barrier.

## 3. Follow conventional circuit grammar

1. Series elements lie along the signal or supply path. Shunts, bias networks
   and decoupling branch from the node they serve.
   By default, a single two-terminal component attached to a device pin
   follows that pin's axis, with a straight connection. This is a geometric
   default, not a role-specific hint. Explicit layout intent or a genuine
   collision may require a different arrangement. Apply it in every pin
   direction, not only to particular component types or functions. Try
   clearance along the pin axis before introducing a bend.
   This preference weakens when several passives serve the same pin. Place
   an authored functional network together: its members' relationship and
   clear shared wiring take precedence over aligning either member with
   the device pin. Do not stretch a network apart to satisfy a single-part
   placement default.
2. Supply symbols point north and ground symbols point south. This convention
   takes precedence over keeping a wire perfectly straight when the
   connection needs no more than one right-angle turn.
   It constrains the rail symbol, not the attached component's orientation.
   A horizontal component can terminate in a south-facing ground symbol.
   On a continuing wire, separate a rail marker clearly from an adjacent
   component and its caption. Use a short branch when an inline marker
   obscures the component or which side of it the named node belongs to.
3. A different rail-symbol orientation is permitted only when conventional
   orientation would require more than one right-angle turn, or when an
   explicit exception has been approved. Neither dense packing nor text
   placement alone is such an exception.
4. Decoupling is local to the actual supply pin or supply region it serves.
   Feedback and bias networks remain compact and legible around their active
   device.
   Rail-symbol orientation does not prescribe capacitor orientation. A bypass
   on a north-facing supply pin can branch horizontally, with the supply
   symbol directly above the pin and a south-facing return at the other end.
5. Differential pairs and bus members stay adjacent, consistently ordered
   and visibly related.
6. Comparable support components on successive pins should align neatly.
   Membership in the same visual bank follows role and pin sequence, not
   merely net identity: alternating pull-ups and pull-downs can still form
   an aligned bank. Stagger only when a real conflict requires it.
7. Repeated channels share orientation, component alignment and spacing.
   Do not make identical roles look unrelated through arbitrary offsets.

## 4. Prefer useful local wires

1. Draw a local connection continuously when doing so exposes useful
   topology. Do not replace it with adjacent duplicate net labels merely
   because the groups need more space.
2. Increase separation along an established wire lane when this makes room
   for intervening branches or neighbouring attachments. Keep the direct
   connection straight where possible; a longer clear local wire is better
   than a short crowded one or a pair of floating labels.
3. Use named ports for connections leaving the view or linking distinct
   functional areas where a continuous wire would obscure the circuit.
4. A pin terminated by a net assignment should have a visible outward wire
   stub before its label. A name placed immediately against the pin is less
   clear about what is a pin name and what is a net connection.
5. Repeated same-net rail pins serving the same function on one component
   face should share a local termination. Keep supply/return connections and
   their bypasses separate from signal pins tied high or low, even on the
   same net, including hard pull-ups and pull-downs with no intervening
   component. Preserve their visual separation as well as their separate
   wiresets; the space that makes these functions distinct is useful, not
   wasted wire. Do not compact one branch into the other's supply region or
   run a supply trunk through signal rows just to combine them. The same rail
   on another face may terminate separately: do not loop a wire around the
   device merely to join the two drawings.
6. Shared return wiring should be visibly shared when the branches form a
   local bank. Keep unrelated owners' return groups local even when they
   ultimately have the same electrical net.
   End the return bank before the next unrelated signal bank; its symbol is
   an obstacle with real dimensions, not an invisible connection point.
   Include the entire glyph and caption: a ground-symbol tip touching a
   signal wire implies a connection even without a junction dot.
   Merge nearby same-net terminations only when their shared route is clear;
   proximity alone does not justify crossing another terminal or component.
7. Respect the outward exit direction of every pin. Derive branch junctions
   and shared trunks from those exits before attaching other symbols.
   Align electrical connection points, not stored drawing anchors.
   Keep wires and junctions clear of every component body; approach only
   through its pins with a visible outward segment. Recheck this after
   moving a shared trunk or compacting its attached components.
8. An attached shunt must leave room for the approach to its pin. Do not put
   a vertical pin directly on a horizontal trunk if its required approach
   forces the router into a double bend.
9. Minimize unnecessary bends and loops, subject to electrical clarity,
   conventional rail orientation and genuine obstacles. A bend with one of
   those purposes is not an avoidable dogleg.
   First consider whether moving or rotating the attached component removes
   the crossing or bend. Routing around a poor placement is not a solution.
10. Directly join independently laid-out major device or connector blocks
    only when at least three distinct one-to-one connections can run straight
    between them. Otherwise use named interfaces with visible local stubs.
    This does not sever useful connections within a device's local support
    circuit, such as a bias or switching branch.
11. An external interface represented by named stubs is not an IC
    alignment anchor. Complete the functional device blocks first, then pack
    these interfaces together along the bottom, wrapping within the sheet
    ratio. Include optical interfaces and their owned support circuitry;
    classification comes from their function, not reference prefixes.
    Do not widen or distort the functional layout merely to preserve an
    interface's seed position.

## 5. Complete local layout before packing groups

Terminology: a **simple termination** is a pin stub and one net symbol or
label. A **local branch** is a connected arrangement of wires, components and
net symbols belonging to a device. An **IC-local block** contains the device
and its completed branches and terminations.

1. Place the main device, establish its wire lanes, add its support
   components and rail terminations, then place annotations.
   A local branch needs more room than a simple termination: reserve space
   for its junctions and attached symbols before choosing its span.
2. Measure the completed local drawing, including pins, attached components,
   wires, labels and symbols. Bare component-body bounds are not a sufficient
   packing envelope.
3. Separate completed groups by a clear minimum gap. Support parts and
   annotations must travel with their owner during whole-group movement.
4. Preserve electrical rows and useful direct connections while allocating
   space. Text must not silently move a component perpendicular to its wire
   lane or create new junctions.
   Labels are the easiest items to move. Try translating or rotating them
   to fit a coherent component-and-wire layout before changing that layout.
   Keep electrical attachment and visual ownership unambiguous; a caption
   belongs beside its own symbol, not in an unrelated empty row.
   When a rail termination competes with a neighbouring signal branch, let
   the termination move outward if text placement alone cannot resolve the
   conflict, instead of bending the signal wire. Preserve
   its local ownership and account for the whole branch, including its label.
   Choose semantic grouping, endpoint orientation, bend count, alignment and
   clearance in that order. Use connection distance only to choose between
   candidates that are otherwise equivalent. A slightly longer straight or
   consistently vertical branch is preferable to a shorter dogleg.
5. Use consistent spacing increments and reserve enough room around dense
   pin fields, branch junctions and domain boundaries.
6. Related items should be closer to one another than to unrelated groups.
   Compactness matters only after local topology and annotations are clear.
   Shorten clearly excessive wire only when the alternative preserves that
   clarity, functional separation, pin exits, alignment and bend count.
   A modest straight span is preferable to a shorter route with doglegs;
   length alone is not a defect. An unobstructed escape beyond neighbouring
   annotations can justify a longer stub. Move text where it gives a real
   improvement, but do not force every termination to the minimum length.
   A simple termination should not inherit unrelated branch clearance merely
   because it shares the same net. This applies to all connection types.
   Apply this throughout a chain and to repeated branches; arbitrary fixed
   gaps must not leave one local group stretched out relative to the rest.
7. Keep layout scale independent. Choose the output framing after the
   circuit is laid out; do not size a device as an arbitrary fraction of
   a page. Pack completed groups within a standard aspect ratio, wrapping
   groups and connector banks as needed. Use the same ratio for whole-sheet
   previews; do not turn the drawing into a wide panorama by cropping it.

## 6. Make annotations serve the reader

1. Show a component's reference and useful electrical value or part number.
   Generic active-device shapes also need a short, evidenced functional
   description.
2. Keep assembly instructions, package and footprint names, catalogue prose
   and sourcing metadata out of the explanatory schematic.
3. Use datasheet functional pin names, with physical pin numbers in their
   separate field. Do not expose compound internal identifiers that repeat
   both pieces of information.
4. Prefer short, unambiguous local net names. A unique functional endpoint
   name can also name its wire; retain enough qualification to distinguish
   electrically different nets.
5. Caption simplification must never merge net identities or change a
   connection.
6. Text and basic component bodies must be comfortably readable relative
   to the major symbols at the same scale. Increasing bitmap resolution
   alone cannot repair the wrong relative sizes.
7. Keep each reference/value pair close to its own component. In a stacked
   bank, captions must identify their corresponding rows, not form detached
   columns above the bank. Reposition captions after moving or rotating parts.
8. Keep complete text bounds clear of final wires, pin strokes and numbers,
   junctions and symbol graphics. A clear text anchor is not enough. Put a
   label beside a continuing wire or on a clear stub, never across the wire.
   Move text to resolve an annotation collision rather than bending an
   otherwise sensible connection.
9. Give boxed cross-sheet labels breathing room wherever their branches can
   move. When close stacking is unavoidable at device pins, align the labels'
   connection tips on a common line, retaining straight exits and clearance.
   Do not stagger their tips merely because the names have different lengths.

## 7. Validate the drawing, not just its coordinates

1. Missing or duplicated meaningful components, any body overlap, annotation
   collisions, apparent electrical errors and ambiguous terminations are
   hard failures. A favourable aggregate score cannot excuse them.
2. After those gates pass, prefer clearer local topology, conventional
   orientation, aligned branches, fewer crossings and avoidable bends, and
   balanced use of space.
3. Check the actual renderer output. Aligned input coordinates do not prove
   that the resulting wire is straight, continuous or unambiguous.
4. Preserve source semantics and unrelated source text. Repeated generation
   from identical input and configuration should be deterministic and
   idempotent.
5. Use small generic fixtures for repeatable geometry rules and compare
   rendered examples for qualitative matters. Keep implementation bugs
   separate from semantic hints: a hint must not be required to repair
   a coordinate mismatch, accidental overlap or unexplained routing loop.
6. Record limitations honestly. A geometry check is not evidence of
   electrical-design correctness, and a clean local detail is not acceptance
   of the entire schematic.
7. Check that a clearance correction preserves useful local wires. Moving a
   component until an overlap disappears is not sufficient if its connection
   silently becomes a pair of labels.

### Review pass

A traceable circuit is not necessarily a clear layout. Report avoidable
crossings and wraparound routes even when every junction is unambiguous.
Consider whether component placement or local named terminations would
simplify the wiring while preserving useful local topology, following §4.
Assess these structural issues before minor caption polish. Passing the hard
electrical-clarity and collision gates alone is not overall layout acceptance.

Use the structural inventory as a review worklist, not as a verdict. Compare
the listed corresponding components and branches for consistent presentation.
Same-symbol cohorts alone do not prove circuit equivalence; actual values,
external connections and authored intent can justify differences. Report
missed repetitions as well as problems in listed families. An inventory does
not authorize a different representation or replace whole-sheet inspection.

Inspect the whole sheet and readable details of every local group. Check:

1. Wire crossings, component-body crossings, symbol-to-wire contacts and
   dangling tails. Distinguish the actual netlist from what the drawing
   appears to connect.
   Include same-net wire/graphic overlaps and near-parallel strokes that
   visually merge despite distinct centreline coordinates.
   Read each local circuit as an unfamiliar reader: can its components,
   polarity and named nodes be distinguished without reconstructing intent?
   Geometrically separate symbols can still form an unintelligible drawing.
2. Pin-axis alignment, avoidable doglegs and loops. Look at component
   placement and orientation before recommending a routing change.
3. Complete rail-symbol and text clearance, especially where a return bank
   ends beside another signal bank or a rail trunk passes signal labels.
4. Caption association as well as collision: references, values and device
   titles must clearly belong to their bodies after any layout change.
5. Clearly excessive stub and connection lengths once topology and clearance
   are sound. Identify a feasible shorter arrangement before reporting one:
   it must preserve functional separation, pin exits, alignment and bend
   count, including the complete neighbouring glyphs and text. An apparently
   empty pin row alone does not establish a clear escape. Do not flag modest
   spans whose shortening would add doglegs or crowd distinct supply and
   bias branches. Apply this to all connections, not just particular nets.

Report concrete locations and qualitative corrections, distinguishing defects
from style suggestions. Judge orientation, caption placement and rail
clearance separately: a correct orientation is not invalidated by a nearby
text defect. Passing connectivity tests do not establish visual acceptance.
After fixing a collision, reread the whole local circuit before marking it
resolved; moving text out of a wire does not prove the arrangement is clear.
