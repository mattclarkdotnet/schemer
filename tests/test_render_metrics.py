from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from schemer.render_metrics import (
    rendered_overview_metrics,
    require_rendered_overview_quality,
    require_rendered_text_legibility,
)
from schemer.toolchain import ToolchainError


def _synthetic_capture(path: Path, *, glyph_height: int) -> None:
    image = Image.new("RGB", (320, 240), (245, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.line((40, 40, 280, 40), fill=(0, 128, 0), width=2)
    for index in range(7):
        left = 70 + index * 18
        draw.rectangle(
            (left, 80, left + 5, 80 + glyph_height - 1),
            fill=(0, 100, 100),
        )
    # Package metadata used to be magenta in this viewer. It is deliberately
    # not the legibility sample.
    draw.rectangle((70, 120, 75, 145), fill=(132, 0, 132))
    image.save(path)


def test_render_metrics_measure_actual_annotation_pixels(tmp_path: Path) -> None:
    capture = tmp_path / "capture.png"
    _synthetic_capture(capture, glyph_height=15)

    metrics = rendered_overview_metrics(capture, crop_left=0, crop_bottom=0)

    assert metrics.typical_annotation_height_pixels == 15
    assert metrics.annotation_glyph_count == 7
    assert metrics.content_bounds is not None
    assert metrics.content_bounds.left == 40
    assert metrics.content_bounds.right == 281


def test_render_metrics_keeps_large_readable_glyphs(tmp_path: Path) -> None:
    capture = tmp_path / "capture.png"
    _synthetic_capture(capture, glyph_height=35)

    metrics = rendered_overview_metrics(capture, crop_left=0, crop_bottom=0)

    assert metrics.typical_annotation_height_pixels == 35


def test_rendered_text_gate_rejects_tiny_annotations(tmp_path: Path) -> None:
    capture = tmp_path / "capture.png"
    _synthetic_capture(capture, glyph_height=8)

    with pytest.raises(ToolchainError, match="typical component annotation is 8px"):
        require_rendered_text_legibility(capture, minimum_annotation_height_pixels=14)


def test_default_text_gate_rejects_previously_accepted_annotation_size(tmp_path: Path) -> None:
    capture = tmp_path / "capture.png"
    _synthetic_capture(capture, glyph_height=17)

    with pytest.raises(ToolchainError, match="minimum is 28px"):
        require_rendered_text_legibility(capture)

    _synthetic_capture(capture, glyph_height=28)
    require_rendered_text_legibility(capture)


def test_rendered_overview_records_unused_area_without_rejecting_layout(tmp_path: Path) -> None:
    capture = tmp_path / "capture.png"
    _synthetic_capture(capture, glyph_height=15)

    metrics = require_rendered_overview_quality(
        capture,
        minimum_annotation_height_pixels=14,
        crop_left=0,
        crop_bottom=0,
    )

    assert metrics.content_area_occupancy < 0.55
