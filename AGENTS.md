## Domain intent and process evidence

- Begin new schematic experiments with `schemer prepare`, review and annotate
  the copied source, then run `schemer check-preparation`. Supply that review
  to layout. An unannotated input is a preparation task, not permission to
  generate an unprepared first draft. Keep the worklist in the run artifacts.

- Do not duplicate an existing authoritative source, URL, identifier, or other provenance merely to prove that a process step occurred.
- Store only durable domain intent in source metadata when that information changes how the artifact is interpreted or generated.
- Keep transient evidence such as review coverage, citations, reasoning, and completion records in the run review or other process artifact.
- Enforce required review steps through the documented workflow and its review worklist, not by adding receipt-like fields to domain objects.
- Before adding persistent metadata, ask whether a downstream consumer needs the value itself. If it only proves that work happened, it does not belong in the domain source.

## Direct code audit before comparisons

- Investigate implementation behaviour by reading the relevant code first. Trace assignments, defaults, transformations and their callers rather than inferring behaviour from before/after output comparisons.
- Before/after comparisons are expensive and unreliable as a primary diagnostic: matching outputs can miss unexamined properties or code paths. Use them only to answer a specific remaining question or verify a code-audit finding.
- Limit conclusions to what the audit establishes. A narrow comparison or passing test does not establish that no other behaviour or override exists.

## Architecture boundaries

- Follow the responsibility map in `docs/architecture-refactor.md`. Keep pure analysis and geometry separate from mutable placement, file editing and external tools.
- Dependency direction and package cycles are enforced by the Import Linter contracts in `pyproject.toml`; run `uv run lint-imports --no-cache`, the tests and Ruff when restructuring code. Do not weaken contracts or add forwarding facades to accommodate a misplaced responsibility.
- Give shared services explicit public names and import them from their owning module. Extract coherent phases and state ownership before a pipeline becomes another collection of unrelated helpers.

## Procedural layout baseline

- Evaluate the generator without explicit spatial placement hints such as `right-of`. Keep circuit functions, component roles, ownership and source-defined symbols; let the generator determine positions.
- Do not add placement hints to compensate for packing, clearance or routing defects. Fix the generic generator instead.
- Keep local geometry reuse separate from electrical representation. Repeated circuits may share an arrangement without being split into independent labelled blocks; reuse must preserve the existing connectivity and group boundaries.
- Default to direct wiring within authored owner/support circuits and named connections between those circuits. Follow ownership transitively, including through transistors and other supporting devices. Pin count and repetition do not establish semantic boundaries; unassigned parts retain their source grouping rather than acquiring invented owners.
- When a circuit has materially different valid schematic representations and no accepted choice, ask the user before layout. Persist the chosen representation at group or module level, separately from electrical roles, and reuse it on subsequent runs. Routine spacing and routing remain generator decisions.
- Before layout, audit every active device's source symbol. A missing library entry is a source-preparation blocker, not permission to leave an IC as a connector placeholder. Correct shared packages and migrate their pin-based intent before regenerating the compiler seed.
- Multi-sheet schematics have one root and one level of circuit sheets. Group related sheets by name, not additional nesting. Preserve the source module hierarchy independently.
- Source modules are circuit blocks, not automatic page boundaries. Group small related modules onto shared sheets; choose the split for readability. Cross-sheet labels belong on circuit connections. Root sheet boxes provide compact navigation, without electrical pins or duplicate-name banks.
- Deliver schematic previews as SVG or native KiCad files, not generated PNG previews. Inspect native/vector drawings at readable detail scale.
- Default to CLI-generated SVG exports for visual review. Do not drive KiCad's GUI merely to open, navigate or inspect a schematic; reserve computer use for an explicitly requested interaction or a specific problem that cannot be assessed from the exported drawing.
