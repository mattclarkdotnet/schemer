# Architecture restructuring

## Goal and constraints

Replace the flat, increasingly interdependent collection of modules with explicit
responsibility boundaries. Preserve schematic behaviour, source intent, public CLI
commands, and file formats. This is not a new placement algorithm or a rewrite.
Do not retain old modules as forwarding facades merely to hide incomplete migration.

## Audit

The initial static inventory covers all 42 production modules (23,581 lines),
including function-local imports and relationships between definitions. The baseline
is 894 passing tests and seven optional integration skips. No import cycle existed.
Detailed code inspection follows the responsibilities being moved, not just file size.

1. `symbol_geometry` mixes symbol parsing, coordinate transforms, circuit queries,
   quality checks, and mutable placement repairs (2,836 lines).
2. `kicad_layout` owns nearly the entire native pipeline: input translation, component
   placement, captions, labels, rails, routing and validation (4,826 lines).
3. `heuristic_block` combines shared circuit models, connectivity queries, orientation
   rules, support placement and several builders. `general_blocks` imports its private
   implementation details (3,008 and 1,547 lines respectively).
4. Analysis depends on the KiCad connectivity adapter just to obtain source pin nets;
   almost every layer depends on the compiler adapter just to obtain an exception.
5. Position records and source-file editing share a module; the plan object reads files.
6. Parsing, document editing and mutable KiCad item representations need distinct owners.
7. Several attribute/position parsers repeat. Native nanometre and viewer-coordinate
   geometry, however, have different semantics and must remain distinct.
8. Tests mostly follow the old files, so large test modules obscure responsibility too.
   Current boundary tests cover only a handful of direct imports.

## Target responsibilities

| Package | Owns | Must not own |
| --- | --- | --- |
| `core` | Errors, position/plan records, shared attribute access | Files, compilers, layout policy |
| `symbols` | Source symbol parsing, viewer-coordinate geometry, net glyphs | Placement passes |
| `analysis` | Circuit roles, connectivity, hierarchy, drawing measurements, quality | Tool execution, native editing |
| `placement` | Block composition, circuit builders, orientation and refinement | Native file serialization |
| `kicad` | Native syntax, document/item model, file editor, native geometry | Circuit placement policy |
| `native` | Native layout stages, routing, labels/rails, packing | CLI or project orchestration |
| `source` | Position comments, copied workspaces, symbol projection | Native layout orchestration |
| `integration` | Compiler and native export subprocess boundaries | Domain ownership decisions |
| `workflow` | Preparation and native project assembly | Low-level geometry implementations |
| `cli` | Argument parsing and dispatch | Placement algorithms |

Shared services must be extracted from their consumers before splitting orchestration.
Sibling services do not import their pipeline. Cross-module helpers have deliberate
names; package initializers are not broad re-export registries. Preserve the `src`
package layout and verify a built wheel independently of the checkout.

## Execution plan

1. Separate foundational values/errors and source-only connectivity from adapters.
   Move plan-to-source I/O out of the plan record. Run the suite.
2. Mechanically relocate definitions into cohesive packages, preserving function
   bodies and accounting for every original definition. Split symbol geometry,
   circuit builders, native placement/annotation/routing and native file handling.
   Update all production, test and maintained-tool imports, including patch targets.
3. Simplify inside the new boundaries: consolidate equivalent helpers, extract
   phases from oversized orchestration/builders, remove forwarding helpers and
   redundant comments. Keep behavioural changes separate from mechanical moves.
4. Organize tests around the new responsibilities and enforce dependency directions
   and acyclic services with Import Linter. Add tests for newly explicit contracts.
5. Run the complete test/lint suite, build and exercise an installed wheel, and
   perform a bounded real-schematic cross-check. Record what was and was not verified.

## Guidance used

The migration follows responsibility/state ownership rather than an arbitrary line
limit, and uses small, tested, behaviour-preserving steps. See Clare Sudbery's
[large-class refactoring walkthrough](https://martinfowler.com/articles/class-too-large.html)
and Fowler's [refactoring overview](https://www.martinfowler.com/books/refactoring.html).
The same reasoning applies to these procedural modules: split coherent responsibilities,
not consecutive chunks of text.

[Import Linter layers](https://import-linter.readthedocs.io/en/stable/contract_types/layers/)
and [acyclic-sibling contracts](https://import-linter.readthedocs.io/en/stable/contract_types/acyclic_siblings/)
provide executable restrictions on dependency direction and package cycles, including
nested packages. Independent layers prohibit peer coupling. Avoid inventing a second
custom architecture-checking framework.

[PyPA's src-layout guidance](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
motivates retaining the current package layout and testing the distribution outside
the source checkout.

## Implemented structure

The measurements and preserved-command statements below record the refactor
checkpoint. Subsequently, the browser-backed `layout`, `render` and `doctor`
commands were removed, including proposal/autoplacement, raster review tools,
browser discovery and dependencies. Native generation retains the shared source
geometry and preparation services. See the README for the current CLI contract.

All five stages above have been executed. The original 42-module flat package is
now ten responsibility packages, containing 130 implementation/entrypoint modules
and 20 package initializers. The largest production module is 481 lines; this is
an outcome of the boundaries, not a new arbitrary file-size rule. Total source
length increases from 23,581 to 25,241 lines, primarily because explicit imports
and module boundaries replace same-file dependencies. This is not a claim of a
smaller algorithmic codebase.

Important changes beyond moving files:

1. `LayoutPlan` retains in-memory transformations; `source.positions` owns reading
   source files and producing replacement text. Shared errors live in `core`, not
   in compiler or editor adapters.
2. Source connectivity and exported-netlist validation are pure analysis. Exporting
   a netlist is a separate `integration.kicad_cli` operation.
3. Source-symbol parsing and transforms are separate from placement repairs. Viewer
   coordinates and native nanometre geometry remain distinct; their superficially
   similar records are not interchangeable.
4. The primary-device builder now analyzes topology once into `PrimaryCircuit`,
   then runs named placement phases with `PrimaryPlacement` as the explicit mutable
   state. Its former 676-line function becomes a short coordinator. An unreachable
   multiple-owner branch was removed after tracing the single-owner input contract.
5. Native placement, annotations, routing and packing own their services. Their
   orchestrator coordinates them rather than also defining their implementations.
   Native syntax, parsed records, mutable items, codecs and editing are separate.
6. Duplicate attribute and position readers and forwarding geometry/net-symbol
   helpers were consolidated. Shared helpers have public names; imports no longer
   reach into sibling modules' private implementations. Redundant generated headers
   and migration whitespace were removed without stripping explanatory comments.
7. Proposal orchestration moved out of the CLI. A typed `LayoutRequest` separates
   the workflow input from argument parsing. CLI commands and file formats remain.
8. Tests now follow responsibilities, with shared drawings/setup under `tests/support`.
   All 455 existing test functions and their parameterization were retained. The
   genericity guard recursively scans the new packages, not just the package root.

Three Import Linter contracts enforce exhaustive package layers, recursive sibling
acyclicity, and restricted access to the native orchestrator. Pytest also checks
module imports, private service imports and self-imports. These run with the ordinary
suite; `AGENTS.md` records the maintenance requirement.

## Internal import migration

There are deliberately no legacy forwarding modules. Maintained tools and tests
use the new imports. Historical experiment scripts remain run records and may need
their imports updated before being reused; they are not a supported Python API.

| Previous location | Current owner |
| --- | --- |
| `schemer.layout.Position`, `ModuleLayout`, `LayoutPlan` | `schemer.core.layout` |
| `LayoutPlan.proposed_sources()` | `schemer.source.positions.proposed_sources(plan)` |
| `schemer.symbol_geometry` parsing/geometry | `schemer.symbols.library`, `.geometry`, `.net_symbols` |
| `schemer.symbol_geometry` placement repairs | `schemer.placement.refinement` |
| `schemer.heuristic_block`, `general_blocks` | `schemer.placement.circuits`, `.builders` |
| `schemer.kicad_schematic.KiCadSchematicDocument` | `schemer.kicad.document` |
| `schemer.kicad_api.FileSchematic` | `schemer.kicad.editor` |
| `schemer.kicad_layout.layout_kicad_from_zener` | `schemer.native.pipeline` |
| `schemer.kicad_project.layout_native_project` | `schemer.workflow.native_project` |
| `schemer.toolchain` | `schemer.integration.toolchain`; pure inspection in `schemer.analysis.topology` |

`schemer.cli:main`, `schemer.cli.build_parser`, and `python -m schemer.cli` remain
available. The CLI package's two explicit exports are entrypoints, not an umbrella
re-export of internal implementation.

## Verification scope

The final regression suite passes **908 tests**, with the same seven optional viewer
skips as the baseline. Ruff passes, all three dependency contracts pass, and the
wheel builds successfully. An isolated Python 3.12 installation outside the checkout
matches all 150 source-file hashes, imports all 148 importable submodules and passes
11 command/module help checks.

The Calibration regeneration passes the native hierarchy's 826-pin connectivity
check and reports no layout issues for that sheet. Its exported SVG is identical
to the accepted drawing after removing only the export timestamp. The other sheets
in that hierarchy were copied as context, not regenerated in this cross-check.

Composite also passes its 124-pin connectivity check, reports no generator layout
issues, and has an identical SVG apart from its export timestamp. These regenerations
ran during the staged migration; the final test and wheel checks include the later
formatting and unreachable-branch cleanup. A duplicate Composite launch was rejected
at atomic publication because the first run had already completed; the verified
first run was retained unchanged.

These are bounded regression checks following the code audit, not proof that every
possible layout is unchanged or that existing visual defects have been fixed. The
optional browser-renderer tests were not enabled, and no PNG previews were generated.
Audit inventories, migration accounting, test XML, wheel and native cross-check
artifacts are in `artifacts/architecture-refactor-20261001/`.
`verification.json` records the final source hashes, test results and the precise
scope of both SVG comparisons.
