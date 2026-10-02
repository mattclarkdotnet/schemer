from __future__ import annotations

SYMBOL = '(symbol "R" (symbol "R_1_1" (pin passive line (at -2.54 0 0) '


SYMBOL += '(length 2.54) (name "1") (number "1")) (pin passive line '


SYMBOL += '(at 2.54 0 180) (length 2.54) (name "2") (number "2"))))'


def fixture():
    instances = {"root.NET": {"kind": "Module", "attributes": {
        "schematic_properties": {"Json": {"representation": "independent-blocks"}},
    }}}
    instances["root"] = {"kind": "Module", "children": {"NET": "root.NET"}}
    instances["root.NET"]["children"] = {}
    groups, nets = {}, {}
    for suffix, owner, child, value in (("A", "U98", "R9", "470k"),
                                       ("B", "U2", "R42", "39k")):
        for name, reference in (("IC", owner), ("PART", child)):
            ref = f"root.NET.{suffix}.{name}"
            instances[ref] = {"reference_designator": reference, "attributes": {
                "__symbol_value": {"String": SYMBOL}, "value": {"String": value},
            }}
            groups[ref] = suffix
        instances[f"root.NET.{suffix}.PART"]["attributes"]["schematic_properties"] = {"Json": {
            "role": "gain-setting", "owner": owner, "pin": "1", "group": suffix,
        }}
        for index, ports in enumerate((("IC.1", "PART.2"), ("IC.2",), ("PART.1",))):
            name = suffix + str(index)
            nets[name] = {"name": name, "kind": "Net",
                          "ports": [f"root.NET.{suffix}.{p}" for p in ports]}
    return {"root_ref": "root", "instances": instances, "nets": nets}, groups
