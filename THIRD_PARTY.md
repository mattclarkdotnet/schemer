# Third-party material

Schemer's own code and design files are published without a licence grant for
now. This does not replace or remove licences already attached to third-party
material.

## KiCad library assets

The sample board fixture contains KiCad-derived symbol and footprint assets under
`tests/fixtures/sample-board/components/`, including connector, logic, discrete,
USB-interface and standard-package library data. These retain attribution to
the KiCad library contributors and the CC-BY-SA 4.0 licence with the KiCad
design exception. See [the upstream notice](licenses/KiCad-Libraries.md).

Sources: [KiCad symbols](https://github.com/KiCad/kicad-symbols) and
[KiCad footprints](https://github.com/KiCad/kicad-footprints).
The fixture carries previously selected/adapted assets from the source design.
For this publication, multi-symbol libraries were reduced to the referenced
symbol plus any inherited parents; retained symbol geometry was not redrawn.
Custom board-specific assets and Zener modules are not represented as upstream
KiCad originals. The fixture is for layout testing, not fabrication approval.

## Runtime dependencies

Zener/pcb, its standard library, the Zener extension, Chrome/Chromium, Python and
the packages in `uv.lock` are separate dependencies under their own licences.
No compiler, standard-library bundle, extension WASM, browser binary, or patched
copy is distributed in this repository.
