---
name: schemer
description: Prepare and annotate Zener sources, generate native KiCad schematics, and review or fix Schemer candidates. Use for Schemer workflows, not general PCB routing.
---

# Schemer

Use the maintained repository guidance for the requested pass. The README owns
the workflow, prompts and annotation syntax; this skill only selects what to read.

## Resolve the run

This skill lives inside the Schemer checkout. Resolve the links below relative
to this file, not the circuit project's working directory. Read the applicable
[repository instructions](../../../AGENTS.md). Identify the circuit entrypoint,
run directory and exact candidate/review paths needed for the requested pass.

## Select the pass

Read the linked sections and their required references before acting. Load only
the modes needed for the user's request.

- **Prepare or annotate source:** follow the [annotation prompt](../../../README.md#annotation-agent-prompt),
  [annotation reference](../../../README.md#complete-hint-and-annotation-reference)
  and [source-review procedure](../../../docs/primary-hinting-agent.md).
  New, unannotated inputs begin with preparation, not a first layout.
- **Generate a candidate:** follow the [recommended workflow](../../../README.md#recommended-workflow).
  Use validated preparation and a fresh compiler seed. For multiple sheets,
  also read the [hierarchy contract](../../../docs/schematic-hierarchy.md).
- **Review a candidate:** follow the [independent review prompt](../../../README.md#independent-review-agent-prompt)
  and [layout principles](../../../docs/schematic-layout-principles.md).
  Inspect the actual SVG/native drawings; this pass reports findings without editing.
- **Fix accepted findings:** follow the [fix-and-regenerate prompt](../../../README.md#fix-and-regenerate-agent-prompt).
  For generator changes, also read the [architecture responsibility map](../../../docs/architecture-refactor.md).
  Keep corrections generic rather than adding per-board placement hints.

Follow the user's requested scope, round count and stopping point. An annotation
request stops at preparation validation; a review request stops at its report.
Do not automatically launch every pass, delegate work, replace originals, or
commit and publish changes merely because this skill was invoked.
