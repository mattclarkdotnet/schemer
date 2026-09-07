"""Headless host for the schematic viewer shipped by the Zener extension."""

from __future__ import annotations

import json
import math
import mimetypes
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from schemer.layout import Position
from schemer.toolchain import Toolchain, ToolchainError, viewer_evaluation
from schemer.view_policy import clean_schematic_labels

_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Schemer viewer harness</title>
  <style>
    * { box-sizing: border-box; }
    html, body, canvas { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body { background: #1e1e1e; }
    canvas { display: block; }
    #error {
      display: none;
      position: fixed;
      inset: 0;
      padding: 2rem;
      color: #f48771;
      background: #1e1e1e;
      white-space: pre-wrap;
      font: 14px ui-monospace, monospace;
    }
  </style>
</head>
<body>
  <canvas id="canvas"></canvas>
  <pre id="error"></pre>
  <script type="module">
    window.__schemerReady = false;
    window.__schemerError = null;
    window.__schemerHostMessages = [];

    function fail(error) {
      const message = error instanceof Error
        ? `${error.message}\n${error.stack ?? ""}`
        : String(error);
      window.__schemerError = message;
      const output = document.getElementById("error");
      output.textContent = message;
      output.style.display = "block";
      console.error(error);
    }

    try {
      const [evaluationResponse, configResponse, workerResponse, workerWasmResponse] =
        await Promise.all([
        fetch("/evaluation.json"),
        fetch("/config.json"),
        fetch("/wasm/worker.js"),
        fetch("/wasm/worker_bg.wasm"),
      ]);
      if (
        !evaluationResponse.ok ||
        !configResponse.ok ||
        !workerResponse.ok ||
        !workerWasmResponse.ok
      ) {
        throw new Error("Failed to load Schemer viewer inputs");
      }

      const evaluation = await evaluationResponse.json();
      const config = await configResponse.json();
      window.__can_edit_layout = config.can_edit_layout;
      window.__can_edit_parameters = config.can_edit_parameters;
      window.__auto_place_on_load = config.auto_place_on_load;
      window.__default_sidebar_collapsed = config.default_sidebar_collapsed;
      window.__show_chrome = config.show_chrome;
      window.__pan_zoom_mode = config.pan_zoom_mode;
      const workerSource = await workerResponse.text();
      const workerWasmUrl = URL.createObjectURL(await workerWasmResponse.blob());
      window.__workerUrl = URL.createObjectURL(new Blob([
        workerSource,
        `\nwasm_bindgen(${JSON.stringify(workerWasmUrl)});`,
      ], { type: "text/javascript" }));

      const viewer = await import("/wasm/schematic_viewer.js");
      const viewerWasmResponse = await fetch("/wasm/schematic_viewer_bg.wasm");
      if (!viewerWasmResponse.ok) {
        throw new Error(`Failed to load viewer WASM: ${viewerWasmResponse.status}`);
      }
      const viewerWasm = await WebAssembly.compileStreaming(viewerWasmResponse);
      await viewer.default(viewerWasm);
      viewer.set_host_callback((message) => window.__schemerHostMessages.push(message));
      await viewer.start("canvas", JSON.stringify(evaluation), "dark");
      viewer.send_message({
        type: "set_capabilities",
        ...config,
      });
      window.__schemerReady = true;
    } catch (error) {
      fail(error);
    }
  </script>
</body>
</html>
"""


class _ViewerServer(ThreadingHTTPServer):
    viewer_assets: Path
    evaluation: bytes
    config: bytes


class _ViewerHandler(BaseHTTPRequestHandler):
    server: _ViewerServer

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        request_path = unquote(urlparse(self.path).path)
        if request_path == "/":
            self._send_bytes("text/html; charset=utf-8", _VIEWER_HTML.encode())
            return
        if request_path == "/evaluation.json":
            self._send_bytes("application/json", self.server.evaluation)
            return
        if request_path == "/config.json":
            self._send_bytes("application/json", self.server.config)
            return
        if request_path.startswith("/wasm/"):
            asset_name = request_path.removeprefix("/wasm/")
            if "/" in asset_name or asset_name in {"", ".", ".."}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            asset = self.server.viewer_assets / asset_name
            if asset.parent != self.server.viewer_assets or not asset.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            if asset.suffix == ".wasm":
                content_type = "application/wasm"
            self._send_bytes(content_type, asset.read_bytes())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_bytes(self, content_type: str, content: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _viewer_server(
    viewer_assets: Path,
    evaluation: dict[str, Any],
    *,
    auto_place_on_load: bool = False,
) -> Iterator[str]:
    server = _ViewerServer(("127.0.0.1", 0), _ViewerHandler)
    server.viewer_assets = viewer_assets.resolve()
    server.evaluation = json.dumps(evaluation, separators=(",", ":")).encode()
    server.config = json.dumps(
        {
            "can_edit_layout": True,
            "can_edit_parameters": False,
            "auto_place_on_load": auto_place_on_load,
            "default_sidebar_collapsed": True,
            "show_chrome": False,
            "pan_zoom_mode": "cad",
        },
        separators=(",", ":"),
    ).encode()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def positions_from_host_messages(messages: object) -> dict[str, Position]:
    """Parse the latest complete placement committed by the viewer."""

    if not isinstance(messages, list):
        raise ToolchainError("Zener viewer host messages were not an array")
    commits = [
        message
        for message in messages
        if isinstance(message, dict)
        and message.get("type") == "positions_committed"
        and isinstance(message.get("positions"), list)
    ]
    if not commits:
        raise ToolchainError("Zener viewer did not commit an auto-placement")

    positions: dict[str, Position] = {}
    for raw in commits[-1]["positions"]:
        if not isinstance(raw, dict):
            raise ToolchainError("Zener viewer returned an invalid position record")
        symbol_id = raw.get("symbol_id")
        x = raw.get("x")
        y = raw.get("y")
        rotation = raw.get("rotation", 0)
        mirror = raw.get("mirror")
        if not isinstance(symbol_id, str) or not symbol_id.startswith(("comp:", "sym:")):
            raise ToolchainError("Zener viewer returned an invalid symbol ID")
        if symbol_id in positions:
            raise ToolchainError(f"Zener viewer returned duplicate symbol ID: {symbol_id}")
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in (x, y, rotation)
        ):
            raise ToolchainError(f"Zener viewer returned invalid coordinates for {symbol_id}")
        if mirror is not None and not isinstance(mirror, str):
            raise ToolchainError(f"Zener viewer returned an invalid mirror for {symbol_id}")
        positions[symbol_id] = Position(float(x), float(y), float(rotation), mirror)
    if not positions:
        raise ToolchainError("Zener viewer committed an empty auto-placement")
    return positions


def auto_place_schematic(
    schematic: dict[str, Any],
    toolchain: Toolchain,
    *,
    timeout_seconds: float = 45.0,
) -> dict[str, Position]:
    """Run the extension's built-in auto-placement and return its coordinates."""

    console_messages: list[str] = []
    with _viewer_server(
        toolchain.viewer_assets,
        viewer_evaluation(schematic),
        auto_place_on_load=True,
    ) as viewer_url:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(toolchain.chrome),
                    headless=True,
                    args=[
                        "--enable-webgl",
                        "--ignore-gpu-blocklist",
                        "--enable-unsafe-swiftshader",
                    ],
                )
                try:
                    page = browser.new_page(viewport={"width": 2400, "height": 1600})
                    page.on(
                        "console",
                        lambda message: console_messages.append(
                            f"console {message.type}: {message.text}"
                        ),
                    )
                    page.on(
                        "pageerror",
                        lambda error: console_messages.append(f"page error: {error}"),
                    )
                    page.goto(viewer_url, wait_until="domcontentloaded")
                    page.wait_for_function(
                        "window.__schemerReady || window.__schemerError",
                        timeout=timeout_seconds * 1000,
                    )
                    viewer_error = page.evaluate("window.__schemerError")
                    if viewer_error:
                        raise ToolchainError(f"Zener viewer failed:\n{viewer_error}")
                    page.wait_for_function(
                        "window.__schemerHostMessages.some("
                        "message => message.type === 'positions_committed') || "
                        "window.__schemerError",
                        timeout=timeout_seconds * 1000,
                    )
                    viewer_error = page.evaluate("window.__schemerError")
                    if viewer_error:
                        raise ToolchainError(f"Zener viewer failed:\n{viewer_error}")
                    return positions_from_host_messages(
                        page.evaluate("window.__schemerHostMessages")
                    )
                finally:
                    browser.close()
        except PlaywrightTimeoutError as error:
            detail = "\n".join(console_messages[-20:])
            raise ToolchainError(
                f"Timed out waiting for Zener auto-placement.\n{detail}"
            ) from error
        except PlaywrightError as error:
            detail = "\n".join(console_messages[-20:])
            raise ToolchainError(f"Headless browser failed: {error}\n{detail}") from error


def render_schematic(
    schematic: dict[str, Any],
    toolchain: Toolchain,
    output: Path,
    *,
    width: int = 2400,
    height: int = 1600,
    timeout_seconds: float = 45.0,
    settle_seconds: float = 2.0,
    auto_place_on_load: bool = False,
    messages_output: Path | None = None,
    zoom_factor: float = 1.0,
    zoom_center: tuple[float, float] | None = None,
    clip: tuple[int, int, int, int] | None = None,
) -> Path:
    """Render schematic JSON through the installed viewer and save a PNG."""

    if width < 320 or height < 240:
        raise ToolchainError("render dimensions must be at least 320x240")
    if not math.isfinite(zoom_factor) or not 1.0 <= zoom_factor <= 8.0:
        raise ToolchainError("render zoom factor must be between 1 and 8")
    if zoom_center is not None:
        zoom_x, zoom_y = zoom_center
        if not 0 <= zoom_x <= width or not 0 <= zoom_y <= height:
            raise ToolchainError("render zoom center must be inside the viewport")
    if clip is not None:
        clip_x, clip_y, clip_width, clip_height = clip
        if (
            clip_x < 0
            or clip_y < 0
            or clip_width < 1
            or clip_height < 1
            or clip_x + clip_width > width
            or clip_y + clip_height > height
        ):
            raise ToolchainError("render clip must be a positive rectangle inside the viewport")

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    console_messages: list[str] = []

    with _viewer_server(
        toolchain.viewer_assets,
        viewer_evaluation(clean_schematic_labels(schematic)),
        auto_place_on_load=auto_place_on_load,
    ) as viewer_url:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(toolchain.chrome),
                    headless=True,
                    args=[
                        "--enable-webgl",
                        "--ignore-gpu-blocklist",
                        "--enable-unsafe-swiftshader",
                    ],
                )
                try:
                    page = browser.new_page(
                        viewport={"width": width, "height": height},
                        device_scale_factor=1,
                    )
                    page.on(
                        "console",
                        lambda message: console_messages.append(
                            f"console {message.type}: {message.text}"
                        ),
                    )
                    page.on(
                        "pageerror",
                        lambda error: console_messages.append(f"page error: {error}"),
                    )
                    page.goto(viewer_url, wait_until="domcontentloaded")
                    page.wait_for_function(
                        "window.__schemerReady || window.__schemerError",
                        timeout=timeout_seconds * 1000,
                    )
                    viewer_error = page.evaluate("window.__schemerError")
                    if viewer_error:
                        raise ToolchainError(f"Zener viewer failed:\n{viewer_error}")
                    page.wait_for_timeout(settle_seconds * 1000)
                    if zoom_factor > 1.0:
                        # The extension's CAD input mode zooms one notch per wheel
                        # event. Round upward so review details meet, rather than
                        # undershoot, the requested nominal scale.
                        # In extension 2.1.41, a -100 pixel CAD-wheel event is
                        # approximately a 1.5x step. Use two steps for a requested
                        # 2x review, yielding roughly 2.25x rather than undershooting.
                        zoom_steps = math.ceil(math.log(zoom_factor, 1.5))
                        zoom_x, zoom_y = zoom_center or (width / 2, height / 2)
                        page.mouse.move(zoom_x, zoom_y)
                        for _ in range(zoom_steps):
                            page.mouse.wheel(0, -100)
                        page.wait_for_timeout(250)
                    screenshot_options: dict[str, Any] = {"path": str(output)}
                    if clip is not None:
                        clip_x, clip_y, clip_width, clip_height = clip
                        screenshot_options["clip"] = {
                            "x": clip_x,
                            "y": clip_y,
                            "width": clip_width,
                            "height": clip_height,
                        }
                    page.screenshot(**screenshot_options)
                    if messages_output is not None:
                        messages_output = messages_output.expanduser().resolve()
                        messages_output.parent.mkdir(parents=True, exist_ok=True)
                        messages = page.evaluate("window.__schemerHostMessages")
                        messages_output.write_text(
                            json.dumps(messages, indent=2, sort_keys=True) + "\n"
                        )
                finally:
                    browser.close()
        except PlaywrightTimeoutError as error:
            detail = "\n".join(console_messages[-20:])
            raise ToolchainError(f"Timed out waiting for the Zener viewer.\n{detail}") from error
        except PlaywrightError as error:
            detail = "\n".join(console_messages[-20:])
            raise ToolchainError(f"Headless browser failed: {error}\n{detail}") from error

    return output
