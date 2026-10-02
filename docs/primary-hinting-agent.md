# Primary hinting agent

Primary hinting is the first task in a new schematic experiment. It runs once
against a Schemer-owned source copy, before the procedural generator assigns
positions. Its job is to make durable circuit intent explicit in the copied
Zener source. It does not lay out or review a drawing.

Run it again only when the source, connectivity, symbol contract, or relevant
datasheet changes. Coordinate-only layout iterations reuse the accepted intent.

## Task prompt

Supply the entrypoint, source-copy root, and run-review path when dispatching
the task. Give the agent this prompt in full:

> You are the primary-hinting agent for a schematic-layout experiment.
>
> Work only in the supplied Schemer-owned Zener source copy. Do not edit the
> original project. Read `docs/schematic-layout-principles.md`,
> `docs/placement-process.md`, and `docs/layout-hints.md` before changing the
> copy.
>
> Entrypoint: `<entrypoint>`
>
> Source-copy root: `<source-copy-root>`
>
> Run review: `<run-review>`
>
> Establish the circuit intent needed by layout before any coordinates are
> generated:
>
> 1. Evaluate the entrypoint with the real Zener compiler. Inventory every
>    instantiated module, physical component, component pin, net, and module
>    boundary. Work through the complete hierarchy, not just the visually
>    dominant block.
> 2. Review the datasheet for every datasheet-backed IC and active module.
>    Check the authored symbol, functional pin names, pin grouping, supply and
>    return pins, and the purpose of its external support circuit. Use the
>    source's existing part identity and datasheet field; do not add another
>    provenance field to prove that the review happened.
> 3. Reject connector placeholders and numbered boxes used for ICs. Audit
>    every instantiated active device, not just examples from user feedback.
>    Correct the shared source package with a suitable functional symbol;
>    if none is available, curate one against the datasheet before layout.
>    An unavailable library symbol is not permission to pass the gate with
>    a placeholder. Preserve the electrical pin
>    contract and validate the package after the change. Never add a
>    part-specific symbol substitution to the layout engine.
> 4. Add only durable intent that changes how the circuit should be
>    interpreted. Put a contextual `function` on a module instantiation. Put
>    static component `role`, owner, pin, group, attachment, and order fields
>    on the component call that implements them. Use `pin_layout` only when
>    the authored symbol cannot preserve a required datasheet grouping. Do not
>    add explicit spatial placement hints such as `right-of`: the procedural
>    baseline must choose block positions without them.
>    Do not represent parallel branches as separate singleton series groups.
>    Record their function and owning pin or channel; shared nets remain
>    authoritative in the circuit connectivity.
>    Measurement tap resistors belong to the circuit they sample, not isolated
>    one-part groups. If a tapped net joins otherwise independently labelled
>    device blocks, attach the tap to the receiving circuit; do not introduce
>    shared wiring between those blocks just to accommodate the tap.
>    For a multi-sheet drawing, propose a small set of useful circuit sheets.
>    Related modules may share a `sheet` name; do not allocate a page to each
>    source module. This records membership only, not positions or page sizes.
>    Check the split against the rendered layout in the coordinator review.
>    If materially different schematic representations are reasonable and no
>    accepted choice exists, present the alternatives to the coordinator for
>    a user decision before layout. This is a presentation choice, not an
>    ambiguity in electrical function. Record the selected representation as
>    durable group or module intent, separately from component roles; do not
>    change roles to force a drawing style. Reuse the choice on later runs.
>    Ordinary spacing and routing choices do not require user decisions.
> 5. Validate every proposed role against evaluated connectivity and datasheet
>    pin names. Do not infer intent from a reference designator, value, net
>    name, package, or geometric seed alone. If evidence permits more than one
>    interpretation, record the ambiguity in the run review and stop; do not
>    guess.
> 6. Build the modified entrypoint and check component inventory and
>    connectivity against the input. If the circuit or a package's electrical
>    contract needs correction, report it and request separate user approval.
>    Do not silently change the baseline or bypass the preparation check.
> 7. In the run review, list the files changed, intent added, datasheets and
>    application circuits examined, unresolved ambiguities, and any proposed
>    new hint or role kind. Keep citations and review coverage in this process
>    artifact, not in Zener instance properties.
>
> Do not run the layout generator, render a schematic, edit `# pcb:sch`
> positions, or encode coordinates, distances, page dimensions, bitmap sizes,
> font sizes, router instructions, or viewer-specific corrections. Do not add
> receipt fields such as `datasheet_review`. Do not modify generator code to
> recognize a board, reference, part number, source path, or net name.
>
> Finish only when every active device has been reviewed and every support
> component is either assigned durable intent or named as a blocking ambiguity.
> A new hint or role kind is a proposal for end-of-run human review, not an
> accepted schema extension.

## Completion gate

The coordinator checks the agent's diff and run review before invoking the
procedural generator. The gate passes only when:

1. the copied entrypoint builds;
2. component inventory and connectivity are preserved;
3. every active device appears in the review worklist and uses a functional
   symbol with checked pin names, numbers and grouping; placeholders block layout;
4. every added field is durable domain intent consumed by layout;
5. the source contains no review receipts or explicit spatial placement hints; and
6. no unresolved ownership or circuit-function ambiguity remains; and
7. any material representation alternatives have an accepted user choice.

An approved electrical correction requires a fresh preparation baseline and
review. New role or hint kinds are proposals, not accepted source annotations:
review and implement their generic schema/consumer support before using them.
Repetition of an existing role is expected; repeated need for the same
relationship may be evidence for a generic procedural rule.

## Coordinator handoff

Start with `schemer prepare ENTRYPOINT --output RUN`. This copies the dependency
closure and writes `source-facts.json`, `preparation-review.json` and a worklist
guide. Review every physical component, including DNP options and electrical
links excluded from assembly files. Record exceptions only for items that truly
need no domain annotation, not for unsupported or unresolved circuit intent.

After annotation and review, run `schemer check-preparation RUN/preparation-review.json`.
It checks worklist completeness, role/representation schema, unchanged evaluated
inventory/connectivity, and source freshness. Pass `--preparation-review` to
`layout-kicad` or `layout-project`; draft mode does not bypass preparation.
This is an enforceable workflow handoff, not automated proof of datasheet
understanding or visual quality. A changed source requires a renewed review.

After the source-intent gate passes, the coordinator runs Schemer's role and
hint consumers with empty coordinate seeds, followed by the relevant
integration tests. This is the first procedural step; it is deliberately not
part of the independent agent task.

A consumer failure does not make evidenced source intent wrong. Record what
the machine actually measured against the first affected block, but do not
treat a heuristic quality threshold as a layout verdict. Programmatic gates
remain authoritative for connectivity, component coverage, role validity and
objective geometry such as component-body overlap. The rendered schematic is
presented to the human reviewer, who decides whether its layout is acceptable.
