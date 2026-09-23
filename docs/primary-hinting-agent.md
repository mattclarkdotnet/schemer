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
> 3. Correct a wrong generic symbol in the copied component package when a
>    suitable functional KiCad symbol exists. Preserve the electrical pin
>    contract and validate the package after the change. Never add a
>    part-specific symbol substitution to the layout engine.
> 4. Add only durable intent that changes how the circuit should be
>    interpreted. Put a contextual `function` on a module instantiation. Put
>    static component `role`, owner, pin, group, attachment, and order fields
>    on the component call that implements them. Use `pin_layout` only when
>    the authored symbol cannot preserve a required datasheet grouping. Do not
>    add explicit spatial placement hints such as `right-of`: the procedural
>    baseline must choose block positions without them.
> 5. Validate every proposed role against evaluated connectivity and datasheet
>    pin names. Do not infer intent from a reference designator, value, net
>    name, package, or geometric seed alone. If evidence permits more than one
>    interpretation, record the ambiguity in the run review and stop; do not
>    guess.
> 6. Build the modified entrypoint and compare component inventory and
>    connectivity with the input. The only permitted electrical difference is
>    an explicitly reported correction to an erroneous copied source package.
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
2. component inventory and connectivity are preserved, except for an explicit
   correction to erroneous copied source;
3. every active device appears in the review worklist;
4. every added field is durable domain intent consumed by layout;
5. the source contains no review receipts or explicit spatial placement hints; and
6. no unresolved ownership or circuit-function ambiguity remains.

New role or hint kinds remain experimental throughout that run and are
presented for human review with the resulting schematic. Repetition of an
existing role is expected; repeated need for the same relationship should be
considered later as evidence for a generic procedural rule.

## Coordinator handoff

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
