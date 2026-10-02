"""Read-only diagnostic: capture the installed viewer's worker traffic."""

import argparse
import json
import math
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from schemer.analysis.visibility import (
    clean_schematic_labels,
    electrical_view,
    focus_module,
    hide_root_children,
)
from schemer.integration.toolchain import evaluate_zener, resolve_toolchain, viewer_evaluation
from schemer.integration.viewer import _viewer_server
from schemer.symbols.library import balanced_blocks, symbol_local_bounds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("entrypoint", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--module")
    parser.add_argument("--translate-x", type=float, default=0.0)
    parser.add_argument("--keep-child", action="append")
    parser.add_argument("--probe-pin-direction", nargs=3, metavar=("COMPONENT", "PIN", "ANGLE"))
    args = parser.parse_args()
    chain = resolve_toolchain()
    schematic = evaluate_zener(args.entrypoint, chain.compiler)
    focused = electrical_view(schematic)
    if args.module:
        focused = focus_module(focused, schematic["root_ref"] + "." + args.module)
    if args.keep_child:
        root = focused["instances"][focused["root_ref"]]
        focused = hide_root_children(
            focused,
            set(root["children"]) - set(args.keep_child),
            root_symbol_ids={key for key in root["symbol_positions"] if key.startswith("sym:")},
        )
    for position in focused["instances"][focused["root_ref"]]["symbol_positions"].values():
        position["x"] += args.translate_x
    if args.probe_pin_direction:
        component, number, angle = args.probe_pin_direction
        instance = focused["instances"][focused["root_ref"] + "." + component]
        old_bounds = symbol_local_bounds(instance)
        raw = instance["attributes"]["__symbol_value"]
        source = raw["String"]
        for pin in balanced_blocks(source, "pin"):
            if f'(number "{number}"' not in pin:
                continue
            at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)", pin)
            length = re.search(r"\(length\s+([-\d.]+)\)", pin)
            x, y, old_angle = map(float, at.groups())
            end_x = x + float(length.group(1)) * math.cos(math.radians(old_angle))
            end_y = y + float(length.group(1)) * math.sin(math.radians(old_angle))
            changed = pin.replace(at.group(0), f"(at {x} {y} {float(angle)})")
            changed = changed.replace(length.group(0), "(length 0)")
            # Keep the original painted lead, changing only the routing port
            # direction. Diagnostic only: never persist this as a part symbol.
            lead = (
                f"(polyline (pts (xy {x} {y}) (xy {end_x} {end_y})) "
                "(stroke (width 0.2032) (type default)) (fill (type none)))"
            )
            source = source.replace(pin, changed + "\n" + lead)
            break
        raw["String"] = source
        new_bounds = symbol_local_bounds(instance)
        position = focused["instances"][focused["root_ref"]]["symbol_positions"][
            "comp:" + component
        ]
        position["x"] += 10 * (new_bounds.min_x - old_bounds.min_x)
        position["y"] -= 10 * (new_bounds.max_y - old_bounds.max_y)
    evaluation = viewer_evaluation(clean_schematic_labels(focused))
    args.output.mkdir(parents=True, exist_ok=True)
    with _viewer_server(chain.viewer_assets, evaluation) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=str(chain.chrome),
            headless=True,
            args=["--enable-webgl", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"],
        )
        page = browser.new_page(viewport={"width": 2400, "height": 1800})
        console = []
        page.on("console", lambda message: console.append(message.text))
        page.add_init_script("""
          window.workerTrace = [];
          const OriginalWorker = window.Worker;
          function capture(direction, value) {
            const data = typeof value === 'string' ? value :
              value instanceof ArrayBuffer ? Array.from(new Uint8Array(value)) :
              ArrayBuffer.isView(value) ? Array.from(value) : value;
            window.workerTrace.push({direction, data});
          }
          window.Worker = class extends OriginalWorker {
            constructor(...args) {
              super(...args);
              this.addEventListener('message', event => capture('received', event.data));
            }
            postMessage(data, ...args) {
              capture('sent', data);
              return super.postMessage(data, ...args);
            }
          };
        """)
        page.goto(url)
        page.wait_for_function("window.__schemerReady || window.__schemerError")
        page.wait_for_timeout(3000)
        trace = page.evaluate("window.workerTrace")
        (args.output / "worker.json").write_text(json.dumps(trace, indent=2) + "\n")
        (args.output / "console.json").write_text(json.dumps(console, indent=2) + "\n")
        print("viewer_error:", page.evaluate("window.__schemerError"))
        page.screenshot(path=str(args.output / "view.png"))
        browser.close()
    for item in trace:
        data = item["data"]
        print(
            item["direction"],
            type(data).__name__,
            list(data)[:12] if isinstance(data, dict) else str(data)[:200],
        )


if __name__ == "__main__":
    main()
