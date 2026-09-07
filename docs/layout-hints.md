# Semantic layout hints

The procedural generator can consume optional semantic metadata with
`--experimental-hints`. Hints describe relationships rather than exact positions:

```text
# schemer:hint {"version":1,"id":"flow","kind":"right-of","blocks":["CORE","INPUT"],"reason":"Read the input before the controller."}
```

Place each JSON object on its own `# schemer:hint` line before the final
`# pcb:sch` position block. Component paths are relative to the owning module;
pin names are logical terminal names, not necessarily physical pin numbers.

The current interpreters are:

1. `right-of`: order complete top-level blocks. The `blocks` array names the
   subject and its predecessor. Relationships must cover the visible top-level
   blocks without cycles. The packer chooses spacing after local blocks are
   complete.
2. `local-return`: identify two endpoints belonging to the return of a local
   circuit. Its current interpreter recognises the supported transformer,
   coupling-passive and connector topology; it is not arbitrary wire routing.
3. `pin-exit`: request a local rail termination on a pin's outward axis while
   preserving the supply-north/ground-south convention when reachable in one turn.

Endpoint records use `{"component":"TRANSFORMER","pin":"PRI_RET"}`. Hints need
a unique `id`, a `reason`, and `version:1`. Unsupported targets and malformed
records fail rather than being silently guessed. See `src/schemer/hints.py` and
`tests/test_hints.py` for the complete validated schema.

The sample board fixture contains the semantic intent used for its baseline.
Production code still has no special cases for its identities. Hints must not
repair coordinate bugs, accidental overlaps or unexplained doglegs. Those are
generator defects; each new hint kind needs explicit review before use.
