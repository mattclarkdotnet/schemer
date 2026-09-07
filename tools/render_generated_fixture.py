"""Render a procedural fixture directly, without unrelated sheet refinement."""

import argparse
from pathlib import Path

from schemer.block_generation import generate_functional_ic_blocks
from schemer.layout import LayoutPlan, ModuleLayout
from schemer.toolchain import evaluate_zener, resolve_toolchain
from schemer.viewer import render_schematic

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
toolchain = resolve_toolchain()
schematic = evaluate_zener(args.source, toolchain.compiler)
plan = LayoutPlan((ModuleLayout(schematic["root_ref"], args.source, {}),))
result = generate_functional_ic_blocks(schematic, plan)
assert result.module_blocks, "fixture was not handled by procedural blocks"
render_schematic(result.plan.apply_to_schematic(schematic), toolchain, args.output,
                 width=4000, height=3000)
print(args.output)
