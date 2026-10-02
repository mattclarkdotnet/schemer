"""Exercise real KiCad sheet connectivity, not a flattened mock netlist."""

from __future__ import annotations

from uuid import uuid4

LIBRARY = '''(lib_symbols
  (symbol "Device:R" (pin_names (offset 0)) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "R_1_1"
      (rectangle (start -1.27 -1.27) (end 1.27 1.27)
        (stroke (width 0) (type default)) (fill (type none)))
      (pin passive line (at -2.54 0 0) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 2.54 0 180) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))))))'''


def _part(reference, sheet_path):
    return f'''(symbol (lib_id "Device:R") (at 20 20 0) (unit 1)
      (in_bom yes) (on_board yes) (uuid "{uuid4()}")
      (property "Reference" "{reference}" (at 20 16 0)
        (effects (font (size 1.27 1.27))))
      (property "Value" "1k" (at 20 23 0) (effects (font (size 1.27 1.27))))
      (instances (project "Circuit" (path "{sheet_path}"
        (reference "{reference}") (unit 1)))))'''


def _label(name, x, y, *, hierarchical=False):
    tag = "hierarchical_label" if hierarchical else "label"
    shape = "(shape passive)" if hierarchical else ""
    return f'''({tag} "{name}" {shape} (at {x} {y} 0)
      (effects (font (size 1.27 1.27))) (uuid "{uuid4()}"))'''


def _project(tmp_path, *, child_port="LINK", short=False, child_output="OUTPUT"):
    root_id, child_id = str(uuid4()), str(uuid4())
    child = f'''(kicad_sch (version 20250114) (generator "schemer")
      (uuid "{uuid4()}") (paper "A4") {LIBRARY}
      {_part("R2", f"/{root_id}/{child_id}")}
      {_label(child_port, 17.46, 20, hierarchical=True)}
      {_label(child_output, 22.54, 20)})'''
    root = f'''(kicad_sch (version 20250114) (generator "schemer")
      (uuid "{root_id}") (paper "A4") {LIBRARY}
      {_part("R1", f"/{root_id}")}
      {_label("INPUT", 17.46, 20)} {_label("LINK", 22.54, 20)}
      (sheet (at 50 15) (size 30 20)
        (stroke (width 0) (type default)) (fill (color 0 0 0 0)) (uuid "{child_id}")
        (property "Sheetname" "Stage" (at 50 14 0)
          (effects (font (size 1.27 1.27)) (justify left)))
        (property "Sheetfile" "Stage.kicad_sch" (at 50 36 0)
          (effects (font (size 1.27 1.27)) (justify left)))
        (pin "LINK" passive (at 50 20 180)
          (effects (font (size 1.27 1.27))) (uuid "{uuid4()}"))
        (instances (project "Circuit" (path "/{root_id}" (page "2")))))
      {_label("LINK", 50, 20)}
      {_label("INPUT", 50, 20) if short else ""}
      (sheet_instances (path "/" (page "1"))))'''
    path = tmp_path / "Circuit.kicad_sch"
    path.write_text(root)
    (tmp_path / "Stage.kicad_sch").write_text(child)
    return path


def _intent():
    return {"root_ref": "root", "instances": {
        "root": {"kind": "Module", "children": {}},
        "root.A": {"kind": "Component", "reference_designator": "R1"},
        "root.B": {"kind": "Component", "reference_designator": "R2"},
    }, "nets": {
        "input": {"name": "INPUT", "ports": ["root.A.1"]},
        "link": {"name": "LINK", "ports": ["root.A.2", "root.B.1"]},
        "output": {"name": "OUTPUT", "ports": ["root.B.2"]},
    }}
