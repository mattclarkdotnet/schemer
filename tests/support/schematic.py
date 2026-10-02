from __future__ import annotations

from schemer.kicad.editor import FileSchematic

SCHEMATIC = """(kicad_sch
  (version 20260306)
  (generator "eeschema")
  (paper "A1")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "R_1_1"
        (rectangle (start -1.27 -1.27) (end 1.27 1.27)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (pin passive line (at -2.54 0 0) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 2.54 0 180) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))))))
  (symbol
    (lib_id "Device:R")
    (at 20.32 30.48 90)
    (unit 1)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Path" "FILTER.R_INPUT.R" (at 20.32 30.48 0) (hide yes)
      (effects (font (size 1.27 1.27))))
    (property "Reference" "R1" (at 21.59 29.21 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 21.59 31.75 0)
      (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "11111111-1111-1111-1111-111111111101"))
    (pin "2" (uuid "11111111-1111-1111-1111-111111111102")))
  (symbol
    (lib_id "74xx:74HC14")
    (at 40.64 30.48 0)
    (unit 2)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Path" "BUFFER.U1" (at 40.64 30.48 0) (hide yes)
      (effects (font (size 1.27 1.27))))
    (property "Reference" "U1" (at 40.64 27.94 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "74HC14" (at 40.64 33.02 0)
      (effects (font (size 1.27 1.27)))))
  (symbol
    (lib_id "power:VCC")
    (at 40.64 20.32 0)
    (unit 1)
    (uuid "77777777-7777-7777-7777-777777777777")
    (property "Reference" "#PWR01" (at 40.64 24.13 0) (hide yes)
      (effects (font (size 1.27 1.27))))
    (property "Value" "VCC" (at 40.64 22.86 0)
      (effects (font (size 1.27 1.27)))))
  (wire
    (pts (xy 20.32 30.48) (xy 40.64 30.48))
    (stroke (width 0) (type default))
    (uuid "33333333-3333-3333-3333-333333333333"))
  (label "FILTER_OUT" (at 30.48 30.48 0)
    (effects (font (size 1.27 1.27)))
    (uuid "44444444-4444-4444-4444-444444444444"))
  (global_label "VCC" (shape input) (at 40.64 20.32 90)
    (effects (font (size 1.27 1.27)))
    (uuid "55555555-5555-5555-5555-555555555555"))
  (junction (at 40.64 30.48)
    (diameter 0)
    (color 0 0 0 0)
    (uuid "88888888-8888-8888-8888-888888888888"))
  (no_connect (at 45.72 30.48) (uuid "66666666-6666-6666-6666-666666666666")))
"""


def _support_fixture(role):
    editor = FileSchematic.from_text(SCHEMATIC)
    owner = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != owner.id])
    owner.field("Path").text.value = "OWNER"
    editor.update_items(owner)
    raw = editor.document.symbols[0].expression
    text = editor.get_as_string()
    copy = text[raw.start:raw.end].replace("11111111", "aaaaaaaa")
    copy = copy.replace('"OWNER"', '"BIAS"').replace('"R1"', '"R2"')
    copy = copy.replace("(at 20.32 30.48 90)", "(at 20.32 20.32 0)")
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    schematic = {
        "root_ref": "root",
        "instances": {
            "root": {"children": {}},
            "root.OWNER": {"reference_designator": "R1", "attributes": {
                "__symbol_value": {"String": '(symbol "P" (pin (name "SIG") (number "2")))'},
            }},
            "root.BIAS": {"reference_designator": "R2", "attributes": {
                "schematic_properties": {"Json": {
                    "role": role, "group": "bias", "owner": "R1", "pin": "SIG",
                }},
            }},
        },
        "nets": {
            "sig": {"name": "SIG", "ports": ["root.OWNER.SIG", "root.BIAS.1"]},
            "gnd": {"name": "GND", "ports": ["root.OWNER.1", "root.BIAS.2"]},
        },
    }

    return schematic, editor
