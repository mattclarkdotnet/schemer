# Schematic hierarchy

Large boards need a navigable schematic, not smaller symbols on a larger
single page. Use one root and one level of circuit sheets. Source modules
provide circuit boundaries and naming groups, not extra schematic levels.

## Boundaries

`schemer plan-sheets <entrypoint>` reports the proposed hierarchy; `--json`
includes physical component membership and each boundary's nets.
This is a planning command, not a multi-sheet layout exporter.

Source modules are circuit blocks, not pages. They stay inline by default.
The intent pass groups related blocks into a few readable sheets, using
`schematic_properties={"sheet": "Audio", "function": "input-conditioning"}`
on module calls. Descendants inherit the nearest sheet assignment unless
they declare another. Modules naming the same sheet share it, even across
source files. Unassigned modules remain on the root (`Overview` by default).
An assignment to `Overview` explicitly returns a nested block to the root.

Use sheet boundaries where they help explain substantial circuit sections;
combine small related sections. Do not give a module its own page just because
it contains multiple parts. Assess the split against the resulting layout.
Every circuit sheet is a direct child of the root, regardless of source
nesting. Names may group related sheets without introducing another level.

Authored component ownership takes precedence over the source-file boundary:
support parts travel with their owner. Their source identity and electrical
connectivity do not change. A remote signal connection alone does not imply
ownership. Missing owners and ownership cycles are errors, not invitations to
infer new roles.

Every visible physical component belongs to exactly one sheet, including all
units of a multi-unit component. Service-item filtering is the same as the
single-sheet view. Boundary nets come from actual physical pin membership,
not counts of module port aliases. A connection between sheets is exposed on
both sheets with matching global labels, including when one source module contains
the other. Sheet boundaries use only the parts actually assigned to each
sheet, not all descendants of its source module. A no-connect is not a sheet
port.

The plan contains no coordinates, page dimensions or part-specific rules.
Primary hinting establishes intent before layout. If a circuit module remains
too large, review its functional decomposition; do not split an arbitrary
number of parts off merely to fill another page.

## Native export contract

Run `schemer layout-project <entrypoint> <fresh-seed.kicad_sch> --preparation-review <review.json> --output <new-directory>`
after the [prepared-source workflow](../README.md#recommended-workflow) and
`pcb apply schematic`. The writer uses
the existing procedural blocks on each sheet. It writes native KiCad sheets,
with circuit-local global labels and a root overview. It does not edit
the Zener source or merge changes into an existing project.

Cross-sheet signal labels replace local labels on the actual circuit wires;
there are no detached interface banks. Root sheet boxes are compact navigation
entries, with no electrical pins or label bridges. Power and ground retain native power
symbols and project-wide scope, without redundant sheet pins. When the seed
has no available power glyph, the existing label fallback is global in a
multi-sheet project. Caption shortening remains unique across the project.

Use `--draft` to generate every sheet for visual feedback before layout defects
are fixed. Drafts retain overlapping parts and the initial orthogonal wire
candidate where clearance routing fails. Each sheet's defects and the full
project connectivity result are recorded in `layout-report.json`; every sheet
title is marked DRAFT. Connectivity errors do not prevent draft export.
Invalid source ownership or missing component identity still stop generation.
The default export remains strict.

Normal exports enforce these invariants:

1. Emit a real KiCad hierarchy with one root and one level of child sheets,
   correct instance paths and references, and global labels for cross-sheet nets.
2. Keep source net identity separate from caption shortening. Identical local
   names in different sheets must not silently create a global connection.
3. Lay out each sheet's completed local blocks and interfaces before selecting
   a standard paper size. Retain native symbol and font sizes.
4. Stage and verify the entire project from its root before publishing any
   sheet. Independent per-sheet checks are insufficient.

`verify_native_project_connectivity` asks KiCad to export the complete root
hierarchy and compares every visible physical pin's net membership with Zener.
Real KiCad regression tests cover an intact two-sheet circuit, a mismatched
child port and a parent-side short. Duplicate physical pins are also rejected.

The file representation follows KiCad's
[schematic format](https://dev-docs.kicad.org/en/file-formats/sexpr-schematic/),
including hierarchical sheet instances and global labels.
