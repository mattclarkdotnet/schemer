# Sources for schematic-layout principles

Status: source audit for the working layout specification.

This file records the sources behind the layout principles. Internal iteration
reports and board-specific review notes are not part of this public snapshot.

Schemer distinguishes normative documentation standards, official EDA
semantics, and practical drawing guidance. They answer different questions:
standards define what an electrical document must communicate, EDA manuals
define what graphical constructs mean, and drawing guides supply the visual
grammar that makes a correct diagram easy to read.

## Normative framework

### IEC 61082-1

IEC TC 3 describes IEC 61082-1 as the general and diagram-specific rules for
electrotechnical documents. Its public explanation says the modern rules focus
on how the result looks, particularly legibility and clear understanding:

- <https://tc3.iec.ch/tc-activity/rules-for-documents-preparation/>
- <https://webstore.iec.ch/en/publication/4469>

This supports Schemer's top-level objective: optimize the reader's
understanding, not merely geometric compactness. The full standard is not
publicly available, so Schemer does not yet claim conformance to its detailed
clauses.

### IPC-2612

IPC-2612 is specifically concerned with electronic diagramming documentation.
The public table of contents includes legibility, terminating resistors,
bypass capacitors, connecting lines, junctions, power and ground, signal paths,
and net naming:

- <https://www.ipc.org/TOC/IPC-2612.pdf>
- <https://www.ipc.org/ipc-board-design-standards>

Those topics map closely to Schemer's intended relationship recognizers and
hard rendering checks. The public material establishes scope but does not
expose the rules themselves; exact IPC requirements must not be inferred from
the headings alone.

### IEC 60617

IEC 60617 is the graphical-symbol reference and directs readers to IEC 61082
for application and diagram-preparation rules:

- <https://webstore.iec.ch/en/iec_catalog/product/preview/?id=L3B1Yi9wZGYvcHJldmlldy9pbmZvX2llYzYwNjE3e2VkMS4wfWIucGRm>

The Zener viewer and component libraries, rather than Schemer, currently own
symbol artwork. This source therefore constrains what Schemer should preserve,
not what its first placement pass can generate.

## Official tool semantics

### KiCad Schematic Editor manual

KiCad's official manual distinguishes wires from graphical lines; defines the
scope of local, global, and hierarchical labels; explains junction and
no-connect meaning; and recommends a 50 mil / 1.27 mm electrical grid:

- <https://docs.kicad.org/10.0/en/eeschema/eeschema.html>

Although Schemer targets the Zener viewer, its inputs use KiCad symbols and the
same electrical drawing vocabulary. These semantics support hard gates around
ambiguous junctions, false graphical connections, explicit unused pins, label
scope, and grid-aligned placement.

## Practical drawing guidance

### TU Delft IP-1 course manual

TU Delft's electrical-engineering course manual gives concrete schematic
drawing guidance: primary flow left-to-right, positive supplies above and
ground below, functional grouping, minimal and unambiguous crossings,
orthogonal grid alignment, consistent component orientation, whitespace, and
strategic use of named global connectors:

- <https://eee.ewi.tudelft.nl/ip-1-manual/parts/appendices/schematics/>

These recommendations directly support Schemer's first ordering, clustering,
spacing, orientation, and crossing objectives.

### Altium schematic-capture guidance

Altium's official overview independently states the common left-to-right
signal-flow and top-to-bottom power convention, and emphasizes consistent net
naming:

- <https://resources.altium.com/p/what-schematic-capture>

It is useful corroboration for the broad visual grammar, but it is product
guidance rather than a normative standard.

## Applying the guidance

The sources establish general conventions; they do not determine a particular
design's engineering narrative. That comes from the design itself and its
author. Functional ordering should follow the circuit's purpose rather than
being inferred from component reference numbers or a fixed page template.

Power remains electrically complete but visually local to each functional
cluster. Shared supply and ground nets use repeated local power/ground symbols
or short local stubs instead of long cross-sheet wires. This is an application
of the general clarity guidance, not a rule quoted from a standard.
