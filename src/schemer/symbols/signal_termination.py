from __future__ import annotations

SYMBOL_NAME = "SignalTermination"


# Both bounds must be finite: a zero-width line makes the installed router
# detour around an otherwise collinear endpoint. The tiny end cap preserves
# a neutral wire-end appearance without a supply/ground arrow.
SYMBOL = '''(symbol "SignalTermination"
  (power global)
  (pin_numbers hide) (pin_names (offset 0) hide)
  (property "Reference" "#NET" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (symbol "SignalTermination_0_1"
    (polyline (pts (xy 0 0) (xy 0 1.27) (xy -0.127 1.27) (xy 0.127 1.27))
      (stroke (width 0) (type default))
      (fill (type none))))
  (symbol "SignalTermination_1_1"
    (pin power_in line (at 0 0 90) (length 0)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27)))))))'''
LIBRARY = f'(kicad_symbol_lib (version 20251024) (generator "schemer")\n{SYMBOL}\n)\n'
FILENAME = "SchemerSignalTermination.kicad_sym"
