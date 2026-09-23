# Reference schematic corpus

Status: reviewed visual and semantic evidence for Schemer's quality gate.

This corpus is a set of guides, not templates to copy. A reference enters the
corpus only when its provenance and useful lesson are explicit. An official
schematic may be authoritative about circuit intent while still being a poor
page-layout exemplar. Schemer records those two judgements separately.

The Raspberry Pi schematics are the primary aspirational visual benchmark for
the first implementation. Other sources may add circuit-specific evidence,
but they do not average down that target.

The general standards and drawing guidance behind the project are listed in
[`layout-sources.md`](layout-sources.md). This document is narrower: it records
real schematics inspected while developing the generator, the visual grammar
that recurs across them, and the generic checks that grammar should eventually
support.

## Evidence classes

- **Visual exemplar**: use its grouping, flow, spacing, or local circuit
  presentation as positive layout evidence.
- **Semantic corroboration**: use it to understand which parts form a circuit
  or domain, but do not copy its page geometry automatically.
- **Negative lesson**: preserve the engineering fact while explicitly avoiding
  a presentation defect visible in the reference.

Every promoted rule must be generic. Component references, manufacturer part
numbers, board names, and instance paths may appear in a review as evidence,
but never as selectors in the layout implementation.

## Reviewed references

### Raspberry Pi Pico Rev3 powerchain

- Source: [Raspberry Pi Pico datasheet, Figure 15, PDF page 21 / printed page
  20](https://datasheets.raspberrypi.com/pico/pico-datasheet.pdf)
- Provenance: official board datasheet.
- Evidence class: strong visual exemplar and semantic corroboration.
- Confidence: high.

Useful recurring grammar:

1. The energy path is one uninterrupted left-to-right spine:
   connector, input rail, series diode, system rail, regulator, and output
   rail.
2. Monitoring dividers, enable controls, and capacitors leave the spine as
   short vertical branches. They do not compete with it for horizontal space.
3. The regulator is a large anchor. Its inductor, feedback connection, input
   capacitor, output capacitor, and ground returns remain local to its body.
4. Rail names sit at meaningful nodes rather than replacing every short local
   wire.
5. Junctions occur at branch roots; conductors do not wrap around an inline
   component and create a false bypass.

Checkable implications:

- a recognized power chain has a monotonic primary-flow ordering;
- series elements lie between the correct upstream and downstream terminals;
- support branches leave the spine substantially perpendicular to it;
- the active power device owns a compact local support envelope; and
- bends and junctions on the main spine carry a reason, not merely a packing
  convenience.

### Current Raspberry Pi RP2040 minimal design

- Source: [official `Minimal-KiCAD.zip` design
  package](https://datasheets.raspberrypi.com/rp2040/Minimal-KiCAD.zip),
  inspected as schematic revision S1 dated 2026-07-09.
- Companion explanation: [Hardware design with
  RP2040](https://datasheets.raspberrypi.com/rp2040/hardware-design-with-rp2040.pdf).
- Provenance: official editable KiCad source and supplied PDF.
- Evidence class: strong visual exemplar.
- Confidence: high.

Useful recurring grammar:

1. The major IC is a large central anchor with enough perimeter for natural
   chains to leave the relevant pin sides.
2. Power, flash, crystal, and IO regions are spatially distinct. Their
   whitespace is structured: intra-region distances are visibly smaller than
   inter-region gaps.
3. Each peripheral circuit is locally complete. The flash and crystal sections
   can be understood without tracing across the sheet.
4. Numerous processor decouplers form an aligned bank on their supply rails;
   exceptional pin-specific capacitors remain nearer the corresponding pins.
5. Long-distance logical connections use names, while short relationships
   inside a circuit use continuous wires.
6. Mechanicals occupy a quiet margin and do not enlarge or fragment the main
   electrical story.

This reference also prevents a bad metric: total whitespace is not itself a
defect. The relevant measures are ownership, local completeness, block
separation, page balance, and readable scale.

The companion guide's Figure 5 independently explains that local decoupling
belongs close to the relevant power pins. Its figures are useful, but the guide
itself warns that embedded schematic images may lag the downloadable KiCad
source; the source package therefore takes precedence when they differ.

Checkable implications:

- major-anchor clearance scales with pin count and attached natural chains;
- every support component has an owner and is closer to that owner's local
  region than to unrelated blocks;
- intra-cluster spacing is smaller than inter-cluster spacing;
- repeated decouplers and channels use stable rows, columns, and pitch; and
- the electrical content bounding box is compact enough to meet the delivered
  readability target without collapsing useful block gaps.

### ADuM4160/ADuM3160 evaluation board

- Source: [Analog Devices UG-043, Figure 2, PDF page
  5](https://www.analog.com/media/en/technical-documentation/user-guides/EVAL-ADuM4160EBZ-UG-043.pdf)
- Provenance: official evaluation-board guide for the same device family used
  by DigitalAbx.
- Evidence class: semantic corroboration; limited visual exemplar.
- Confidence: high for domain structure, medium for reusable page geometry.

Useful recurring grammar:

1. The isolator is the domain boundary and the principal anchor.
2. Upstream connector, data conditioning, power, decoupling, and ground remain
   on one side; the downstream equivalents remain on the other.
3. The two data members stay paired through the boundary.
4. Each side owns its supply and return network; domain grounds are not
   interleaved.

Negative lessons:

- the published full-board schematic is rotated in the guide and is dense
  enough that it is not a good scale or page-balance target;
- several option networks compete visually with the primary USB path.

Checkable implications:

- every domain-owned component remains on the correct side of an isolation
  boundary;
- differential members retain adjacency, order, and comparable path shape;
- local decoupling attaches to the supply pins in its own domain; and
- optional support circuitry is visually subordinate to the primary path.

### Cirrus Logic CS8406 S/PDIF transmitter

- Source: [CDB4272 reference design, Figure 11, PDF page
  21](https://statics.cirrus.com/pubs/rdDatasheet/CDB4272-2.pdf)
- Provenance: official evaluation-board reference design.
- Evidence class: semantic corroboration with selected local visual evidence.
- Confidence: high for circuit grouping, medium for page geometry.

Useful recurring grammar:

1. The transmitter IC is the anchor; serial audio/control enter on one side and
   physical S/PDIF outputs leave on the other.
2. Series resistors remain inline with their individual signal paths.
3. Transformer/coax and optical-output stages form distinct terminal chains
   rather than a miscellaneous passive cloud.
4. Supply bypassing stays local to the transmitter and output devices.

Negative lessons:

- the figure is rotated and uses a large amount of unstructured page area;
- its full-page orientation is not a model for delivered review images.

Checkable implications:

- coax and optical output chains each have an explicit source-to-connector
  order;
- repeated drivers align at their actual terminals, not merely their stored
  component origins; and
- convergence, shunt, transformer, coupling, and connector roles remain
  visually distinguishable.

## Promoted visual grammar

The following patterns recur strongly enough to guide Schemer now:

1. **Primary spine**: give each functional branch one dominant flow line.
2. **Large anchor**: reserve perimeter around a major IC according to its pin
   field and attached chains, not a global symbol pitch.
3. **Owned support**: every passive and minor device belongs to a consumer,
   branch, or common power structure before coordinates are assigned.
4. **Perpendicular support branches**: shunts, monitoring, bias, and local
   power support normally leave the main flow vertically.
5. **Local functional loops**: feedback, switching, reset, crystal, and
   protection loops stay next to their active device.
6. **Aligned repetition**: repeated branches share row/column structure,
   terminal alignment, pitch, and direction.
7. **Domain sidedness**: isolation-domain content remains on its own side of a
   visible boundary.
8. **Structured whitespace**: gaps express ownership and hierarchy; unused
   area alone is neither good nor bad.
9. **Local wires, distant names**: continuous wires explain local topology;
   labels bridge genuinely distant or global relationships.
10. **Quiet service region**: unused units, test points, and mechanical items
    do not interrupt the engineering narrative.

## Mandatory autonomous review gate

Before a generated board render is presented for design feedback, Schemer's
review process must:

1. render one fitted overview and current readable details for every
   functional cluster;
2. run all implemented hard geometry and connectivity checks;
3. compare the drawing with the promoted grammar above;
4. record each finding with severity, confidence, visual evidence, violated
   principle, and the earliest placement stage that can fix it;
5. distinguish a hard false-topology defect from a quality warning and a
   stylistic preference;
6. withhold the candidate while a known hard defect or missing mandatory
   review capture remains; and
7. turn recurring objective defects into generic fixtures or checks before
   relying on manual coordinate adjustment.

This gate does not claim that visual quality is fully automatable. It does make
the generator responsible for finding obvious violations itself and leaves the
user to judge the harder questions of engineering emphasis and taste.
