# Sample board

Entrypoint: `boards/sample-board/SampleBoard.zen`.

This is the digital module of an ABX project, presented on one schematic sheet:
input connector → controller → S/PDIF and USB branches, with local power wiring.
The fixture includes its 19 required Zener modules, package manifests, symbols
and footprints. Standard-library modules are supplied by the installed Zener
toolchain. No other project checkout is required.

Local components and interfaces are preserved from the layout-development input.
Unused library symbols were removed, keeping referenced symbols and inherited
parents. Circuit topology and component pin mappings were not changed. Existing
placement comments are input metadata, not golden output coordinates: the
generator recomputes positions. Semantic hints capture the intended grouping.

Some original sourcing/datasheet metadata refers to documents not included here.
These are descriptive strings, not build dependencies. Physical layout, STEP
models and manufacturing validation are outside this fixture's purpose. See
`THIRD_PARTY.md` at the repository root for inherited asset licences.
