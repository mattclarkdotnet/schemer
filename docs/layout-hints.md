# Qualitative review and semantic comment hints

Status: `local-return` and `pin-exit` approved by Matt after the first run,
2026-09-07. Newly implemented types still require end-of-run review.

The current procedural baseline uses circuit roles and ownership, not explicit
spatial placement hints. The earlier whole-sheet `right-of` hints have been
removed from the working board. The format below documents existing support,
not a requirement or a workaround for generator placement defects.

Before the procedural baseline is generated, a dedicated primary-hinting agent
completes the source-intent pass defined in
[`primary-hinting-agent.md`](primary-hinting-agent.md). An independent reviewer then reads
the actual Zener viewer image against `schematic-layout-principles.md` and its
own schematic knowledge. It returns ordinary engineering observations with
component/pin references and an explanation. The coordinator translates
selected observations into semantic comment records, regenerates the layout,
and sends the actual new image back for comparison.

The review remains prose. Each hint's `reason` preserves the finding that
caused it; its other fields are the narrower, executable interpretation.
Consuming a hint does not prove that the visible defect disappeared. That
distinction mattered in this run: a geometry test passed while a ground hook
remained, until pin direction was read from the symbol stroke.

## Required visual review before a verdict

Review the whole sheet and every functional block at a readable detail scale,
not only the changed hint targets. An unchanged block is not evidence of a
correct block. Check these independently; do not average failures into a pass:

1. Local signal and power chains are continuous wires, not adjacent duplicated
   labels. Trace the actual endpoint pins, including series supply parts.
2. Each bypass joins its actual consumer supply pin through a visible local
   branch. Verify terminal identities; similar parts need not share pin numbers.
3. Shared trunks have clean T junctions: no hooks, doubled junctions, wire loops
   or overlapping nudged segments. Aligned coordinates alone do not pass.
   Inspect each pin-to-trunk exit, including tiny steps. A clean central
   junction does not excuse doglegs on its branches. Straight unobstructed
   exits are a generator invariant, never something to repair with hints.
   Trace each bank member from pin tip to the first common-trunk intersection:
   absent an evidenced obstacle, that branch must be one segment collinear
   with the pin stroke. Judge branches and trunk separately, at detail scale.
4. Annotations clear symbols, wires, other text and neighbouring blocks. Remove
   redundant local labels instead of letting them occupy routing corridors.
5. Generic active-device rectangles show reference, short evidenced function
   and MPN; catalogue and package detail remains hidden.
6. Basic symbols and text are readable relative to large IC bodies at the same
   scale. More export pixels do not fix an incorrect relative size.
7. Preserve component coverage and connectivity, and inspect the actual full
   render after local changes and packing. Programmatic tests establish only
   the invariants they measure, not visual acceptance.
8. Inspect connector breakouts separately from ICs. Trace every lane through
   nearby pulls and series parts; report avoidable crossings even when the
   exported netlist is correct. A connected circuit can still be hard to read.
9. Inspect where each shunt joins a shared trunk. A tiny sideways step before
   the junction counts as an avoidable jog, not a clean connection.
10. After checking defects, make a separate compactness pass. Identify specific
    stubs and branches that can be shortened without losing pin-exit direction,
    annotation clearance or useful topology. Consider rotating a bypass and
    aligning its supply symbol with the owner pin before accepting extra turns.
    Distance remains a tie-breaker, not a reason to crowd or bend a clear wire.

Renderer-originated defects remain failures or explicitly unresolved findings.
They may require a renderer change; they are not grounds to waive a rule.
The round-02 "reasonable enough" verdict was withdrawn after Matt identified
missed local wires, junction defects, text overlap and relative-size problems.

## Comment format

One JSON object per `# schemer:hint` line, immediately before the final
contiguous `# pcb:sch` position block:

```text
# schemer:hint {"version":1,"id":"primary-return","kind":"pin-exit","endpoints":[{"component":"TRANSFORMER","pin":"PRI_RET"}],"reason":"Keep this ground termination on the outward pin axis."}
# pcb:sch TRANSFORMER x=... y=... rot=...
```

Component paths are relative to the owning evaluated module. Pins are logical
terminal names, which may differ from the physical numbers in the image.
No workspace path, screenshot location, transient viewer ID, distance, offset,
or text pixel size belongs in a hint. Only documented fields are allowed.
Source configurations that change instance names require updating references;
the prototype reports stale references instead of guessing.

The hint preamble survives generated-position replacement and compilation.
Zener treats these as ordinary comments; electrical connectivity is unchanged.

## Component roles and ownership

Component roles preserve authored intent when connectivity permits more than
one reasonable interpretation. A role states what a component instance does
and, when relevant, which net, component, or pin it serves. It never states
where to draw it.

Keep a human explanation beside the component call and put the machine-readable
fields in one namespaced hash inside that instance's `properties`:

```python
# Second shunt element of the reconstruction filter, at FILTER_2.
Capacitor(
    name="FILTER_CAP_2",
    P1=FILTER_2,
    P2=RETURN,
    properties={
        "schematic_properties": {
            "role": "shunt",
            "group": "signal-path",
            "at": "FILTER_2",
        },
    },
)
```

Within `schematic_properties`, `group` identifies the local circuit. A `series`
instance has a zero-based `order`; a `shunt` instance has an `at` net.
The generator checks every claim against evaluated connectivity before it
places anything. Missing members, discontinuous series neighbours, an unknown
attachment net, or a non-return shunt endpoint are errors.

The current interpreter accepts one fully annotated series group per local
module. It draws the ordered series path left-to-right, hangs each declared
shunt below its named node, and uses the source's return nets. If no component
has role metadata, this interpreter does nothing. If only part of the local
circuit is annotated, it fails instead of inventing the missing roles.

The `pullup` role records `owner` and `pin`. Schemer checks that the resistor
joins that exact owner pin to a non-return supply rail.
Pull-ups on the same owner face and supply rail are drawn as uniform horizontal
branches from one shared vertical rail; pull-ups on another face form a
separate local bank rather than wiring around the IC.

The corresponding `pulldown` role records the same `owner`, `pin`, and `group`
fields and validates the remote endpoint as a return rail. Pull-downs on one
owner face are aligned as a bank and use one south-facing return wireset.

The `bypass` role records `owner` and a datasheet `pin`. Schemer checks that the
capacitor joins that exact pin net to a return and places it outside the IC's
side-pin corridors. This prevents a supply bypass from displacing direct
functional wires.

The `power-feed` role records `owner` and a datasheet `pin` for a two-terminal
part that feeds a device supply input. Schemer checks both sides are supply
rails and that the output joins the named owner pin. It draws the part as a
short inline local power branch beside that pin, with the source symbol pointing
north after at most one turn. The internal owner net is not terminated by
duplicated labels. This intent comes from the device datasheet; topology alone
does not distinguish a supply feed from an arbitrary rail-to-rail part.

The `divider` role records `owner`, `group`, and zero-based `order` on every
resistor in the complete path. Schemer validates neighbour continuity, finds
the intermediate nets connected to the owner's functional pins, and aligns
those taps directly with the pin rows. It does not infer divider purpose from
values or reference names.

The inline roles `series-termination`, `current-limit`, `ac-coupling`, and
`source-impedance` each record `owner`, `pin`, and `group`. They distinguish
different electrical purposes that share the same two-terminal topology. The
generator validates attachment to the exact named owner pin, places the part
outward on that pin's natural axis, and suppresses a redundant caption on the
short internal net. The role name describes the part's function; a broad
interface name is not a substitute for that intent.

Further role kinds should be added only when each has one clear, documented
layout consequence. Roles may encode membership, order, ownership, and
electrical attachment. They may not encode coordinates, distances, bitmap
sizes, or renderer-specific repairs.

### Experimental: owned local networks

The native KiCad experiment accepts these extensions for human review:

- `series` may declare `owner` and `pin`, anchoring its network to that exact
  terminal. The current native implementation supports a directly attached
  element, not an arbitrary multi-element owned chain.
- `shunt` may declare `owner` and either `pin` or `at`. `return_pin` identifies
  its other connection without assuming that the reference is Ground.
- `pin-bridge` declares `owner`, `pin`, and `other_pin` for a floating component
  between two owner terminals, such as a bootstrap capacitor.

The native consumer validates terminal nets, keeps support with its declared
owner, and places parallel shunts at their attachment. It does not infer
ownership from a shared rail. These extensions are not implemented by the
older whole-module series interpreter described above.
An owned series/shunt pair sharing the same declared group and pin is placed
as one local network, rather than forcing the series member onto the device's
pin axis. No spatial hint is needed.

Power-stage order comes from source symbols' `power_out` → `power_in`
connections within a module, not a placement hint or a function-name lookup.

Roles are authored source intent. The generator must never create a role,
infer an owner, or promote a topology classification into semantic metadata.
It may only validate an authored role against connectivity and either apply
the documented layout consequence or reject the inconsistent or missing
intent.

An IC or module component may declare `pin_layout.perimeter` when its package
pin numbers carry physical schematic order that a generic box cannot recover.
The list follows the package perimeter: down the left face, then up the right.
Optional `pin_layout.bottom` entries identify auxiliary access pins that do not
belong on that perimeter. Schemer requires every component terminal exactly
once. This is datasheet pinout intent, not a set of coordinates.

## Module function

A module instantiation may describe the function it serves in its parent:

```python
Filter(
    name="OUTPUT_FILTER",
    properties={
        "schematic_properties": {
            "function": "audio-reconstruction-filter",
        },
    },
)
```

The function belongs to that use of the module rather than to its reusable
implementation. Component roles are static implementation details and remain
on the component calls inside the module. A module `function` currently has no
geometric meaning. Component role fields and module function fields may not be
mixed in one hash. Datasheet coverage and citations are process evidence in the
run review, not instance properties.

## Approved hint types

| Type | Meaning | Current supported interpretation |
| --- | --- | --- |
| `local-return` | Two endpoints form the return of one local outgoing/return circuit. | The recognized transformer–coupling-passive–connector chain uses ordinary short branch stubs on the secondary path. Its return stays within a local routing span; the actual viewer must confirm a continuous wire. |
| `pin-exit` | A rail terminal receives a short stub in its authored outward direction. | The rail glyph still follows supply-north / ground-south when reachable with at most one turn. This hint does not authorize a sideways glyph. Ambiguous and mirrored pin geometry is rejected. |

These are initial interpreters for a generic circuit structure, not arbitrary
topology. All component and net choices are metadata; production code contains
no identities from the experiment. Unsupported targets, unknown kinds,
duplicate requests, and malformed records fail explicitly. A request must be
consumed exactly once.

## Whole-sheet relationship

`right-of` names two complete top-level blocks in the root source:

```text
# schemer:hint {"version":1,"id":"flow","kind":"right-of","blocks":["CORE","INPUT"],"reason":"Read the input before the controller."}
```

The first block is the subject, the second its predecessor. Local components,
wires, symbols and text are completed and measured before these relationships
move whole blocks. Same-stage peers stack vertically, in source-name order;
they do not become a serial electrical chain. Clearance is procedural, not a
hint coordinate. Internal placements and electrical connectivity are preserved.

The interpreter requires relationships covering all visible top-level
blocks; unknown names, missing blocks and cycles fail rather than being guessed.
It is root-only. Do not author these relations during baseline primary hinting.
No shared-ground-bank, parallel-driver or text-size type has been added.

## Repeated hints and general improvements

Many instances may legitimately need the same hint type. Record that repetition
in the run review and consider whether it exposes a missing procedural rule.
Type approval permits using the relationship; it does not make the relationship
an automatic default everywhere.

Before generalising, identify the shared topology or symbol geometry that makes
the hint appropriate, and examples where it would be wrong. If those facts can
reliably select the intended cases, implement the rule in the procedural
generator and protect both matching and nonmatching cases with generic
fixtures. Confirm the change in the actual viewer. Explicit design intent that
cannot be inferred from those facts remains metadata.

Each run's summary should note recurring hint types, any proposed general rule,
and whether the evidence supports implementing it yet. Repetition prompts this
review; it is not an automatic threshold for changing global behavior.

The earlier whole-sheet experiment used three `right-of` relations. They were
removed on 2026-09-23 so that the baseline exercises generic block packing.

## Running an experiment

Work in a Schemer-owned source copy. Install a comments-only hint file with:

```console
uv run python tools/apply_layout_hints.py SOURCE.zen HINTS.zen
uv run schemer layout ENTRYPOINT.zen \
  --proposal-dir artifacts/run/proposal --render artifacts/run/overview.png --zoom 1
uv run python tools/capture_hint_review.py \
  artifacts/run/proposal/boards/BOARD/ENTRYPOINT.zen artifacts/run \
  --module MODULE --width 8000 --height 6000
```

Approved types work without an experimental flag. Add `--experimental-hints`
when testing newly implemented types awaiting review; the flag does not approve
them. Hinted generation requires a buildable proposal directory. Unknown kinds
still fail explicitly, even with the flag. No-hint generation retains its
previous behavior.

The capture helper records source hints, connectivity digest, component
inventory, image hash, text measurements, and existing geometry findings.
Its output is diagnostic. Normal `--review-dir` acceptance gates are unchanged.
Geometry is measured in the actual sheet hierarchy, before focusing an image.
Omit `--module` from the capture command to measure and capture the full sheet.

The coordinator currently runs this sequence with an independent Sol/high
reviewer. There is no autonomous scheduler, numerical scoring contract, or
automatic hint-type approval. A run ends with before/after evidence,
unresolved findings, and new types for Matt to review.

First-run metadata:
[`../experiments/hint-review-01/spdif-hints.zen`](../experiments/hint-review-01/spdif-hints.zen).
First-run findings:
[`reviews/semantic-hints-20260907.md`](reviews/semantic-hints-20260907.md).
