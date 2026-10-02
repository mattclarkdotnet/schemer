# Schemer

Schemer generates readable, editable KiCad schematics from
[Zener](https://github.com/diodeinc/pcb) projects. It preserves source-defined
symbols and sizes, places devices with their supporting circuits, routes local
connections, and packs completed blocks onto standard sheets. Larger designs can
use a flat hierarchy. Zener remains the electrical source of truth.

This is a draft-generation tool, not a substitute for engineering review. Valid
connectivity does not establish a good drawing. The recommended workflow is
**prepare → review and annotate intent → validate preparation → generate → review**.
Schemer does not run an LLM reviewer or invent the annotations for you.

## Installation

```sh
git clone https://github.com/mattclarkdotnet/schemer.git
cd schemer
uv sync
uv run schemer --help
```

Use Python 3.12 or later through [uv](https://docs.astral.sh/uv/), a compatible
Zener `pcb` compiler with `apply schematic` support, and KiCad's `kicad-cli`.
Native exports have been exercised with KiCad 10. Schemer does not install these
external tools. The Zener entrypoint needs a linked schematic, for example through
`Board(..., schematic=True)` or `Project(...)`.

Schemer uses native KiCad output only. It needs neither Chrome, a Zener VS Code
extension nor an open application window. Export previews with KiCad's CLI.

| Dependency | Discovery / override |
| --- | --- |
| Compiler | `SCHEMER_COMPILER`, otherwise `pcb`/`pcbc` on PATH, then `~/.local/bin/pcb`; `--compiler PATH` overrides it. |
| KiCad CLI | `kicad-cli` on PATH, otherwise the standard macOS KiCad app path; native commands accept `--kicad-cli PATH`. |

The public snapshot includes a self-contained sample at
`tests/fixtures/sample-board/boards/sample-board/SampleBoard.zen`. Use it as the
input below, or use your own project. Development checkouts may keep their
integration corpus separately; the generator does not depend on this sample.

## Recommended workflow

### 1. Prepare a source copy

```sh
uv run schemer prepare /path/to/Board.zen --output artifacts/first-run
```

Use a new or empty output directory. Preparation copies the source dependency
closure and writes `PREPARATION.md`, `source-facts.json` and
`preparation-review.json`. It creates a worklist of **every physical component**;
it does not annotate the circuit, place components or produce a first draft.
Subsequent commands use the copied entrypoint recorded in the review, not the
original project.

### 2. Review symbols and record intent

A human or engineering agent works through the entire worklist:

1. Read the source, connectivity and relevant datasheets. Check every active
   device's symbol, pin names/numbers, units, supplies and support network.
   Fix incorrect or placeholder symbols in the copied shared package before layout.
2. Add the semantic annotations below: function, role, owner, attachment, group
   and order where they convey real intent. Do not add coordinates, distances or
   placement relationships to repair routing.
3. Ask the user when materially different representations are reasonable, such
   as a directly wired circuit versus independently labelled device blocks.
   Persist the accepted choice in `representation`.
4. For large designs, choose a few substantial circuit sheets using `sheet`.
   Related modules can share a sheet. Use one root plus one level of children,
   not a sheet per source module or a deep hierarchy.
5. Complete each review row's `intent` and `symbol_review`, then set `reviewed`
   to `true`. If no supported annotation is needed, explain why in
   `annotation_not_needed`. Resolve every top-level `unresolved` entry.

A worklist row looks like this; retain the generated `path` and `reference`:

```json
{
  "path": "FRONT_END.FILTER_CAP.C",
  "reference": "C1",
  "reviewed": true,
  "intent": "Shunts the filter node to its local return; annotated in the source copy.",
  "symbol_review": "Checked the capacitor symbol and its two-terminal pin mapping.",
  "annotation_not_needed": ""
}
```

Keep citations, reasoning, coverage and completion evidence in this worklist or
its companion review. Source properties contain only durable circuit intent,
not receipts proving review occurred. Do not duplicate existing symbol datasheet
or part-identity fields. See the [source-review procedure](docs/primary-hinting-agent.md).
For a copyable agent task, use the [annotation prompt](#annotation-agent-prompt) below.

### 3. Validate preparation and inspect sheet membership

```sh
uv run schemer check-preparation artifacts/first-run/preparation-review.json
# Set this to the copied entrypoint reported by check-preparation:
SCHEMER_ENTRY=/absolute/path/to/artifacts/first-run/source/path/to/Board.zen
uv run schemer plan-sheets "$SCHEMER_ENTRY" --json
```

The check requires complete coverage, unchanged inventory/connectivity, valid
annotations and no unresolved questions, then seals the reviewed sources and
resolved symbols. It cannot judge whether the datasheet interpretation is correct.
Both layout commands require `--preparation-review`, including draft runs.

Coordinate-only iterations reuse the review. Source, dependency or symbol changes
require renewed preparation and review of affected intent. Do not clear digest
fields to bypass stale-source detection: prepare a fresh run from the revised
copy and reconcile its worklist.

### 4. Generate a fresh native seed and candidate

```sh
pcb apply schematic --no-open "$SCHEMER_ENTRY"
# Use the .kicad_sch path printed by pcb apply, under the copied project's layout:
SCHEMER_SEED=/absolute/path/to/copied/layout/Board.kicad_sch
uv run schemer layout-project "$SCHEMER_ENTRY" "$SCHEMER_SEED" \
  --preparation-review artifacts/first-run/preparation-review.json \
  --output artifacts/first-run/candidate-1 --draft
```

`layout-project` generates positions from intent, for one sheet or a hierarchy.
The output directory must not exist. It writes a root `.kicad_sch`, any circuit
sheets, `layout-report.json`, and per-sheet structural-review worklists describing
repeated structures and circuit membership. Source modules remain unchanged.

Draft mode retains recorded geometry/connectivity defects and marks titles DRAFT.
Read the report: a successful draft command does **not** mean connectivity passed.
Invalid identity or ownership can still stop generation. Omit `--draft` for a
strict candidate; the default rejects failures.

### 5. Review the SVG/native drawing and iterate

```sh
# Substitute the root filename written by layout-project:
kicad-cli sch export svg --exclude-drawing-sheet \
  --output artifacts/first-run/candidate-1/svg \
  artifacts/first-run/candidate-1/Board.kicad_sch
```

Inspect every sheet and block at a readable detail scale, in SVG or KiCad. Use the
structural worklists to check repetition, ownership and grouping; they are review
aids, not automatic verdicts. Trace local connections, power branches, crossings,
labels and symbol clearance. Do not use tiny PNG previews as acceptance evidence.
See the [layout principles](docs/schematic-layout-principles.md).
The [review prompt](#independent-review-agent-prompt) below makes the inspection
scope and expected findings explicit.

Correct missing **intent** in the source copy. Correct generic spacing, packing
or routing defects in the generator, not with per-board placement hints. Regenerate
the compiler seed, use a new candidate directory, then review the new drawing.
Connectivity verification asks KiCad to interpret the complete root hierarchy and
compares physical pin membership with Zener; independent page checks are insufficient.

Candidates are disposable generated views. Manual finishing in KiCad is reasonable
after handoff, but Schemer does not preserve those edits through regeneration.
Never use a hand-edited candidate as the next compiler seed.

## Using agents for annotation and review

The CLI enforces preparation and checks generated connectivity; **you orchestrate
the agents**. The prompts below are tasks to paste into your coding/engineering
agent, not additional Schemer commands. The annotation agent needs source,
compiler and datasheet access. The reviewer also needs to inspect the actual
SVG/native drawings at readable detail scale. If it cannot do that, its report
must say visual review is incomplete; reading SVG XML or a layout report is not
a substitute for seeing the drawing.

Use the same coordinator for the run, but preferably a fresh reviewer context
for each candidate. Give that reviewer the accepted circuit intent and artifacts,
not the generating agent's explanation of why its layout is good. On later
rounds, supply prior finding IDs for resolution tracking, while still requiring
fresh inspection of the whole requested scope.

Replace every `<PLACEHOLDER>` with an actual path or decision. Keep candidate
directories immutable and review files outside them. A useful handoff includes:

| Pass | Inputs | Deliverable / stopping point |
| --- | --- | --- |
| Annotation | Prepared source, full component worklist, datasheets, accepted representation choices | Annotated copy, completed preparation review, unresolved questions or successful validation; no drawing yet |
| Generation | Validated preparation and fresh compiler seed | New native candidate, layout report, structural worklists and SVG exports |
| Independent review | Exact candidate, prepared source, reports, drawing guidance | Numbered findings, coverage and limitations; no source or layout edits |
| Fix round | Human-approved findings and exact candidate/review paths | Generic corrections, tests and a new candidate for another independent review |

### Annotation agent prompt

Use this after step 1 has created the preparation directory. If asking an agent
to perform step 1 too, supply the original entrypoint and a new output directory;
tell it to run `schemer prepare` before editing anything.

```text
Prepare circuit intent for Schemer; do not generate a layout yet.

Schemer checkout: <SCHEMER_REPO>
Preparation directory: <RUN>
Accepted representation choices / user constraints: <DECISIONS_OR_NONE>

Read the repository instructions, README annotation reference,
docs/primary-hinting-agent.md and docs/schematic-layout-principles.md.
Read <RUN>/PREPARATION.md, source-facts.json and preparation-review.json.
Resolve the copied entrypoint and workspace from that review file. Edit only
that source copy and this run's review artifacts, never the original project.

Work through every physical component in the worklist, including DNP parts.
Evaluate the copied circuit and inspect the relevant datasheets. Audit every
active device's actual source symbol, logical-to-physical pin mapping, units,
supplies and supporting circuitry. Correct missing or placeholder symbols in
the copied shared package, preserving electrical connectivity and pin identity.
Do not substitute symbols in the generator or infer function from names alone.

Add only evidenced, supported semantic intent: function, role, owner, pin,
group, order, attachment, representation and sheet membership where useful.
Follow transitive ownership through supporting devices. Leave routine geometry
to the generator: no coordinates, right-of hints, font changes or routing hacks.
Ask me before choosing between materially different valid representations.
Reuse accepted choices. For a large design, propose a small number of useful
circuit sheets with one hierarchy level, not one sheet per source module.

Complete each worklist row's intent and symbol_review. Set reviewed=true only
after doing that review. If no supported annotation is needed, explain why in
annotation_not_needed; unsupported or ambiguous intent is not an exemption.
Keep evidence and citations in <RUN>/annotation-notes.md, not receipt fields in
source properties. Preserve generated paths, references and baseline digests.

If evidence, a suitable symbol or an ownership decision is missing, record it
in unresolved and ask me. Do not invent new schema fields or silently alter the
circuit. An electrical correction needs separate approval and a new baseline.

When all questions are resolved, build the copied entrypoint and run:
uv run schemer check-preparation <RUN>/preparation-review.json
Do not bypass failures or mark unexamined items complete to make it pass.
Report changed files, intent/representation decisions, validation results and
remaining blockers. Stop here; do not run layout or edit position records.
```

The coordinator checks the source diff and review evidence, then performs
steps 3–4 and exports SVG. A sealed worklist proves completion/freshness checks,
not that the agent's engineering interpretation was correct.

### Independent review agent prompt

Give this agent the generated root and all relevant child sheets, not just a
selected screenshot. The `structural_review` entries in `layout-report.json`
name the per-sheet worklists. For a large project, review one sheet at a time
and retain a project-wide coverage list; a clean sheet is not a project pass.

```text
Independently review this Schemer candidate. Do not fix it in this pass.

Schemer checkout: <SCHEMER_REPO>
Preparation review: <RUN>/preparation-review.json
Candidate directory and root schematic: <CANDIDATE>, <ROOT_KICAD_SCH>
SVG exports: <SVG_DIRECTORY>
Scope: <ALL_SHEETS_OR_EXPLICIT_SHEET_NAMES>
Accepted circuit representation / constraints: <DECISIONS>
Prior findings, if any: <PREVIOUS_REVIEW_OR_NONE>
Write the review only to: <REVIEW_FILE_OUTSIDE_CANDIDATE>

Read docs/schematic-layout-principles.md, especially its review pass, and the
candidate's layout-report.json and structural-review worklists. Inspect the
prepared source when resolving intended ownership or connectivity. Treat all
inputs as read-only; do not edit source, generator code or candidate files.

Inspect the SVG/native drawing of every sheet and local circuit in scope at
readable detail scale. Use the worklists to check repeated circuits and missed
grouping, not as automatic verdicts. Check the whole sheet before details.
Prioritize missing connections/components, false visual connections, body or
label collisions, detached owned support, confusing crossings and wraparound
routes before minor polish. Check rail graphics against continuing wires,
near-parallel strokes, junction clearance, caption association and orientation.

Respect accepted representation boundaries and separate supply wiring from
hard pullups/pulldowns. Repeated geometry must not force shared electrical
connections. Prefer moving text before bending wires. Flag excessive wire
length only when a feasible shorter arrangement preserves meaning, clearance
and reasonable bend count. Passing a netlist check does not prove visual clarity.

Return a Markdown ordered list with stable IDs such as REV-001. For each finding
give severity (hard defect, significant readability issue, or minor/style),
sheet, component/pin/net location, visible evidence, why it matters and a
qualitative correction. Distinguish an actual connectivity failure from an
apparently misleading drawing. Do not prescribe per-part coordinates or invent
annotations to mask a generator bug; mark uncertain causes as uncertain.

Report coverage, connectivity status from the report, anything you could not
inspect, and an overall verdict with limitations. Recheck previous findings as
resolved, still present or regressed, and look for new issues elsewhere too.
If you cannot inspect the drawing, state that explicitly; do not claim a pass.
```

### Fix-and-regenerate agent prompt

Use this only after deciding which findings to accept. It deliberately limits
the task to one candidate; specify an explicit round count if you want a loop.

```text
Implement one fix round for this reviewed Schemer candidate.

Schemer checkout: <SCHEMER_REPO>
Prepared source and review: <COPIED_ENTRYPOINT>, <PREPARATION_REVIEW>
Current candidate / reviewer report: <CANDIDATE>, <REVIEW_FILE>
Accepted finding IDs: <IDS>
Rejected or deferred findings, with reasons: <DECISIONS_OR_NONE>
Next output directory (must be new): <NEXT_CANDIDATE>

Read the findings and inspect their code paths before changing anything.
Trace assignments, defaults, transforms and callers; before/after comparisons
are verification, not the primary diagnosis. Separate missing source intent
from generic placement/routing defects. Fix the responsible layer, not each
example separately. No board/reference/part-number special cases, spatial hints,
changed default symbol sizes or edits to generated candidate files.

Add focused generic regressions for generator fixes and run the relevant tests,
Ruff and architecture checks. If source intent or a symbol must change, follow
the preparation renewal procedure; do not rewrite a seal or baseline to pass.
Ask before changing electrical design or an accepted representation choice.

Generate a fresh compiler seed, create the next native candidate using its
validated preparation review, and export SVG through kicad-cli. Read the layout
report and report any connectivity failure, including in draft mode. Preserve
the previous candidate and review. Summarize changes by finding ID, tests run,
remaining issues and artifact paths. Stop after this candidate; do not approve
your own visual result or start another round without instruction.
```

Send that candidate back to the independent reviewer. If you use one agent for
all passes, keep the same explicit handoffs and distinguish its self-check from
independent review. Human acceptance remains the final decision.

## Complete hint and annotation reference

There are three distinct forms: **source intent** in `schematic_properties`,
limited **comment hints**, and generated **position records**. All names are
case-sensitive. A parser-valid annotation still needs a compatible layout consumer
and valid electrical connectivity.

### Source intent: placement in Zener

Pass a nested dictionary through the existing component/module call's `properties`
argument. This keyword fragment describes a part attached to evaluated device
`U1`'s logical `IN` terminal:

```python
properties={"schematic_properties": {
    "role": "series-termination",
    "group": "input",
    "owner": "U1",
    "pin": "IN",
}}
```

Role lookup checks the physical `Component()` first, then its immediate wrapper
module. Put the role at one of these locations, not an arbitrary ancestor.
Module-use function and representation belong on the module invocation. Do not
mix component-role fields with module-function fields in one dictionary. Unknown
fields are rejected by the relevant consumer.

### Component fields and every supported role

| Field | Meaning |
| --- | --- |
| `role` | Required name from the role table. |
| `group` | Required nonempty local circuit/group name, not a global tag or placement instruction. |
| `owner` | Evaluated **reference designator** of the owning physical component, e.g. `U1`; not its source `name` or instance path. Revisit it if annotation changes the designator. |
| `pin` | Logical owner terminal, resolved through the compiled pin mapping; it need not equal a physical pin number. |
| `order` | Zero-based non-negative integer for `series`/`divider`. Digit strings are accepted for compatibility; prefer integers. Complete paths need unique contiguous orders. |
| `at` | Shunt attachment net. Consumers resolve connected net identities/names; use an unambiguous name, not a location. |
| `return_pin` | Optional second **owner** terminal for `shunt`, identifying its other connection without assuming Ground. |
| `other_pin` | Second **owner** terminal for `pin-bridge`. |

Every role requires `role` and `group`. The table lists its additional fields;
omit fields that do not belong to that role.

| Role | Additional fields | Intent and interpretation |
| --- | --- | --- |
| `series` | `order`; optionally **both** `owner` and `pin` | Ordered two-terminal path element. No `at`. Owned form anchors the network to the owner's terminal. |
| `shunt` | Exactly one of `at` or `pin`; `owner` required with `pin`, optional with `at`; optional `return_pin` with `owner` | Branch from a named node/owner pin. No `order`. Unowned series-path consumer requires its remote endpoint to be a return net. |
| `pin-bridge` | `owner`, `pin`, `other_pin` | Floating component between two owner terminals, e.g. feedback/bootstrap support. No `order` or `at`. |
| `pullup` | `owner`, `pin` | Resistor joining that exact pin to a non-return supply. |
| `pulldown` | `owner`, `pin` | Resistor joining that exact pin to a return rail. |
| `bypass` | `owner`, `pin` | Local decoupling at the named consumer supply pin. |
| `power-feed` | `owner`, `pin` | Two-terminal supply feed: output serves the exact owner supply pin, input is a non-return supply. |
| `divider` | `owner`, `order` | Complete resistor divider with taps at owner terminals. No `pin` or `at`. |
| `series-termination` | `owner`, `pin` | Inline signal termination/damping. |
| `current-limit` | `owner`, `pin` | Inline current-limiting element. |
| `ac-coupling` | `owner`, `pin` | Inline DC-blocking/coupling element. |
| `source-impedance` | `owner`, `pin` | Inline source-impedance element. |
| `gain-setting` | `owner`, `pin` | Experimental inline gain-setting support; use only when this describes the circuit. |

The owner/pin roles do not accept `order` or `at`; `return_pin` is shunt-only and
`other_pin` is pin-bridge-only. Ownership follows transitively through supporting
devices, including transistors, and takes precedence over source-file/sheet
boundaries. Unknown owners and cycles are errors. Sharing a supply or having many
pins does not establish ownership. Unassigned parts keep their source grouping.

Each line below is a separate instance's `schematic_properties` payload:

```json
{"role":"series","group":"filter","order":0}
{"role":"shunt","group":"filter","at":"FILTER_NODE"}
{"role":"shunt","group":"feedback","owner":"U1","pin":"IN_N","return_pin":"OUT"}
{"role":"pin-bridge","group":"feedback","owner":"U1","pin":"OUT","other_pin":"IN_N"}
{"role":"divider","group":"threshold","owner":"U1","order":0}
{"role":"bypass","group":"supply","owner":"U1","pin":"VDD"}
```

The unowned role-directed series interpreter supports one complete ordered group
per local block; it does not guess missing members. Owned `series`, owned `shunt`
and `pin-bridge` are native local-network extensions, not universal support for
arbitrary feedback graphs. The owned series consumer anchors directly attached
elements, not arbitrary multi-element chains. Parallel branches must not be
disguised as singleton series groups. Repeated circuits may share geometry without
changing their electrical representation.

### Module fields

```python
properties={"schematic_properties": {
    "function": "input-conditioning",
    "sheet": "Audio — inputs",
    "representation": "independent-blocks",
}}
```

| Field | Accepted values and effect |
| --- | --- |
| `function` | Nonempty contextual purpose string. No fixed vocabulary; it does not prescribe geometry. |
| `sheet` | Nonempty sheet name. Descendants inherit the nearest assignment; modules with the same name share a sheet. Unassigned blocks stay on root `Overview`; assigning `Overview` returns a nested block to it. |
| `representation` | `independent-blocks` or `connected-circuit`, chosen with the user when there is a meaningful alternative. |

These fields can be used independently or together. `independent-blocks` draws
each device and owned support locally, with labels between blocks; explicit unowned
role groups remain intact. `connected-circuit` retains direct wiring within the
chosen multi-device circuit, useful for a feedback loop. Connector/interface
assemblies retain local labelled breakouts. The most specific applicable module
representation wins.

At an entrypoint, Zener's module property API avoids adding a wrapper just for intent:

```python
builtin.add_property("schematic_properties", {
    "function": "composite-amplifier",
    "representation": "connected-circuit",
})
```

Sheet membership prescribes neither dimensions nor placement. The generator picks
a standard sheet after layout; it does not scale symbols or text to fit. Cross-sheet
labels belong on circuit wires. Root boxes are navigation, not duplicate-name port
banks. See [hierarchy](docs/schematic-hierarchy.md).

### Source-authoring pin layout (not a runtime override)

The `source.primary_projection` helper reads this **physical component's**
`schematic_properties` payload when authoring a generic package symbol:

```json
{"pin_layout":{"perimeter":["Pin_1","Pin_2","Pin_3","Pin_4","Pin_5","Pin_6","Pin_7","Pin_8"],"bottom":["PAD"]}}
```

`perimeter` lists logical terminals down the left face, then up the right;
`bottom` is optional auxiliary access. Together they must name every component
terminal exactly once. The perimeter needs at least eight entries and an even
length; terminals must map to numeric physical pins. No other `pin_layout` keys
are accepted. Do not combine it with a component-role payload.

This is a source-preparation API, not a layout CLI switch. The generator does not
replace component symbols at runtime. Curate the actual source symbol, validate
its electrical pin contract and re-prepare before generating a candidate.

### Comment hints: exact syntax and limited consumers

One JSON object per ordinary source-comment line:

```text
# schemer:hint {"version":1,"id":"return-pair","kind":"local-return","endpoints":[{"component":"TRANSFORMER","pin":"SEC_RET"},{"component":"CONNECTOR","pin":"RETURN"}],"reason":"These endpoints form one local circuit's return."}
# schemer:hint {"version":1,"id":"primary-return","kind":"pin-exit","endpoints":[{"component":"TRANSFORMER","pin":"PRI_RET"}],"reason":"Keep the rail exit on its natural outward axis."}
```

Place them before the final contiguous `# pcb:sch` block, if present. `version`
must be integer `1`; `id`, `kind` and `reason` are nonempty strings. Each endpoint
has **exactly** `component` and `pin`: a source-local component path relative to
the owning module, and a logical terminal. Unlike a role's `owner`, `component`
is **not** a reference designator. Wrapper parts may need `TERMINATION.R`.
No additional keys are accepted.

| `kind` | Members | Current consumer |
| --- | --- | --- |
| `local-return` | Exactly two distinct `endpoints` | Recognized transformer/coupling-passive/connector circuit's local secondary return; not arbitrary ground pins. |
| `pin-exit` | Exactly one entry in `endpoints` | Recognized transformer's primary return exit; not a general routing override. Rail conventions still apply. |
| `right-of` | Exactly two distinct names in `blocks`, replacing `endpoints` | Historical whole-block ordering; **prohibited by preparation and native hierarchy layout**. |

For completeness, the historical spatial form is:

```text
# schemer:hint {"version":1,"id":"flow","kind":"right-of","blocks":["CORE","INPUT"],"reason":"Read the input before the controller."}
```

`CORE` is the subject, `INPUT` its predecessor. The old interpreter is root-only,
requires coverage of all visible top-level blocks and rejects cycles. Parser
support is **not** permission to use it in prepared runs. Do not use spatial
hints to compensate for generator defects.

Duplicate IDs, repeated requests (endpoint order is irrelevant), unknown kinds,
extra fields and unsupported/unconsumed targets fail. Each hint must be consumed
exactly once. Recognition depends on topology; JSON validity does not establish
applicability to every sheet or backend.

To replace a copied source's comment-hint preamble, before sealing preparation:

```sh
uv run python tools/apply_layout_hints.py COPIED_MODULE.zen HINTS.zen
```

The hints file contains comments only. The helper preserves electrical source and
position records. All three existing types are recognized by the parser, but
`right-of` is rejected by preparation. There is no command-line bypass for
unapproved hint types. Native seeding consumes comment hints only where its
applicable local builder can resolve them; prefer semantic roles.

### Generated positions are not intent hints

The legacy coordinate format is:

```text
# pcb:sch PART x=120.0000 y=40.0000 rot=90
# pcb:sch RAIL.0 x=100.0000 y=0.0000 rot=0 mirror=x
```

These store viewer-unit positions, rotation in degrees and optional `x`/`y` mirror.
Component keys omit `comp:`; net-symbol keys omit `sym:` and use `.N` instead of
`#N`. They are output/state, not semantic annotation. The procedural baseline starts
without explicit coordinate constraints. Do not hand-tune them to conceal defects.

## Other commands and limits

`inspect-kicad FILE --zener ENTRYPOINT` checks native/source identity.
`layout-kicad` is the lower-level single-file path for **already positioned** source,
not the recommended first run. It also requires preparation and a fresh compiler
seed; replacing an existing output requires `--overwrite`. Connectivity is checked
before saving.

The supported commands are `prepare`, `check-preparation`, `plan-sheets`,
`inspect-kicad`, `layout-kicad` and `layout-project`. Use each command's `--help`
for exact flags. The former `layout`, `render` and `doctor` commands have been
removed, along with their browser renderer and raster review tools.

## Development

```sh
uv run pytest
uv run ruff check src tests tools
uv run lint-imports --no-cache
uv build
```

Tests cover generic circuits, native editing/routing, preparation and architecture.
Compiler/KiCad checks need those tools installed. Public tests use the included
sample; development tests can use `SCHEMER_ABX_WORKSPACE` for a separate integration
corpus.

See [architecture](docs/architecture-refactor.md), [testing](docs/testing-strategy.md),
[placement](docs/placement-process.md), [detailed hints](docs/layout-hints.md) and
[reference-schematic review](docs/reference-schematic-corpus.md). Source symbols
and intent stay authoritative. Production rules must not branch on board names,
component references or part numbers.

No project licence has been granted for Schemer. Existing third-party notices,
including those for the public sample's inherited assets, remain applicable; see
the public snapshot's [THIRD_PARTY.md](https://github.com/mattclarkdotnet/schemer/blob/main/THIRD_PARTY.md).
