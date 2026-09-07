"""Pixel-level acceptance metrics for Zener viewer captures."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from schemer.toolchain import ToolchainError

_SCHEMATIC_ANNOTATION_RGB = (0, 100, 100)
DEFAULT_MINIMUM_ANNOTATION_HEIGHT_PIXELS = 28


@dataclass(frozen=True)
class PixelBounds:
    """One image-space bounding box, with an exclusive right/bottom edge."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def as_dict(self) -> dict[str, int]:
        return {
            "bottom": self.bottom,
            "height": self.height,
            "left": self.left,
            "right": self.right,
            "top": self.top,
            "width": self.width,
        }


@dataclass(frozen=True)
class RenderedOverviewMetrics:
    """Measurements taken from the pixels the user actually sees."""

    image_width: int
    image_height: int
    analysis_bounds: PixelBounds
    content_bounds: PixelBounds | None
    typical_annotation_height_pixels: int | None
    annotation_glyph_count: int

    @property
    def horizontal_content_occupancy(self) -> float:
        if self.content_bounds is None:
            return 0.0
        return self.content_bounds.width / self.analysis_bounds.width

    @property
    def vertical_content_occupancy(self) -> float:
        if self.content_bounds is None:
            return 0.0
        return self.content_bounds.height / self.analysis_bounds.height

    @property
    def content_area_occupancy(self) -> float:
        return self.horizontal_content_occupancy * self.vertical_content_occupancy

    def passes_annotation_height(
        self,
        minimum_pixels: int = DEFAULT_MINIMUM_ANNOTATION_HEIGHT_PIXELS,
    ) -> bool:
        return (
            self.typical_annotation_height_pixels is not None
            and self.typical_annotation_height_pixels >= minimum_pixels
        )

    def as_dict(
        self,
        *,
        minimum_annotation_height_pixels: int = (DEFAULT_MINIMUM_ANNOTATION_HEIGHT_PIXELS),
    ) -> dict[str, object]:
        return {
            "analysis_bounds": self.analysis_bounds.as_dict(),
            "annotation_glyph_count": self.annotation_glyph_count,
            "content_bounds": (
                self.content_bounds.as_dict() if self.content_bounds is not None else None
            ),
            "content_area_occupancy": round(self.content_area_occupancy, 4),
            "horizontal_content_occupancy": round(self.horizontal_content_occupancy, 4),
            "image_height": self.image_height,
            "image_width": self.image_width,
            "minimum_annotation_height_pixels": minimum_annotation_height_pixels,
            "passes_annotation_height": self.passes_annotation_height(
                minimum_annotation_height_pixels
            ),
            "typical_annotation_height_pixels": self.typical_annotation_height_pixels,
            "vertical_content_occupancy": round(self.vertical_content_occupancy, 4),
        }


def _near_colour_mask(
    image: Image.Image,
    target: tuple[int, int, int],
    tolerance: int,
) -> Image.Image:
    channels = ImageChops.difference(image, Image.new("RGB", image.size, target)).split()
    masks = [channel.point(lambda value: 255 if value <= tolerance else 0) for channel in channels]
    return ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2])


def _component_sizes(mask: Image.Image) -> list[tuple[int, int, int]]:
    width, _ = mask.size
    points = {
        (index % width, index // width)
        for index, value in enumerate(mask.get_flattened_data())
        if value
    }
    components: list[tuple[int, int, int]] = []
    while points:
        seed = points.pop()
        stack = [seed]
        min_x = max_x = seed[0]
        min_y = max_y = seed[1]
        area = 1
        while stack:
            x, y = stack.pop()
            for neighbour_y in range(y - 1, y + 2):
                for neighbour_x in range(x - 1, x + 2):
                    neighbour = (neighbour_x, neighbour_y)
                    if neighbour not in points:
                        continue
                    points.remove(neighbour)
                    stack.append(neighbour)
                    min_x = min(min_x, neighbour_x)
                    max_x = max(max_x, neighbour_x)
                    min_y = min(min_y, neighbour_y)
                    max_y = max(max_y, neighbour_y)
                    area += 1
        components.append((max_x - min_x + 1, max_y - min_y + 1, area))
    return components


def rendered_overview_metrics(
    image_path: Path,
    *,
    crop_left: int | None = None,
    crop_bottom: int | None = None,
    colour_tolerance: int = 8,
) -> RenderedOverviewMetrics:
    """Measure fitted content and ordinary annotation glyphs in a viewer PNG.

    The current Zener light renderer uses a stable teal for references,
    electrical values, pin names, and net captions. Measuring those pixels
    catches fixed-screen-size labels that coordinate-space estimates cannot
    see. Package metadata is deliberately absent from the presentation payload
    and therefore cannot masquerade as the legibility sample. The default crop
    excludes the viewer's corner control and bottom status bar.
    """

    image_path = image_path.expanduser().resolve()
    if not image_path.is_file():
        raise ToolchainError(f"rendered overview is absent: {image_path}")
    with Image.open(image_path) as opened:
        image = opened.convert("RGB")

    width, height = image.size
    if crop_left is None:
        crop_left = round(width / 48)
    if crop_bottom is None:
        crop_bottom = round(height / 16)
    if crop_left < 0 or crop_bottom < 0 or crop_left >= width or crop_bottom >= height:
        raise ToolchainError("render-metric crop must leave a non-empty image")
    analysis = PixelBounds(crop_left, 0, width, height - crop_bottom)
    cropped = image.crop((analysis.left, analysis.top, analysis.right, analysis.bottom))

    background = cropped.getpixel((0, 0))
    difference = ImageChops.difference(cropped, Image.new("RGB", cropped.size, background))
    difference_channels = difference.split()
    visible = ImageChops.lighter(
        ImageChops.lighter(difference_channels[0], difference_channels[1]),
        difference_channels[2],
    ).point(lambda value: 255 if value > 24 else 0)
    raw_content_bounds = visible.getbbox()
    content_bounds = (
        PixelBounds(
            raw_content_bounds[0] + analysis.left,
            raw_content_bounds[1] + analysis.top,
            raw_content_bounds[2] + analysis.left,
            raw_content_bounds[3] + analysis.top,
        )
        if raw_content_bounds is not None
        else None
    )

    annotation_mask = _near_colour_mask(cropped, _SCHEMATIC_ANNOTATION_RGB, colour_tolerance)
    glyphs = [
        (component_width, component_height, area)
        for component_width, component_height, area in _component_sizes(annotation_mask)
        if 3 <= component_width <= 64 and 4 <= component_height <= 64 and area >= 4
    ]
    heights = Counter(component_height for _, component_height, _ in glyphs)
    typical_height = (
        max(heights, key=lambda candidate: (heights[candidate], candidate)) if heights else None
    )
    return RenderedOverviewMetrics(
        image_width=width,
        image_height=height,
        analysis_bounds=analysis,
        content_bounds=content_bounds,
        typical_annotation_height_pixels=typical_height,
        annotation_glyph_count=len(glyphs),
    )


def require_rendered_text_legibility(
    image_path: Path,
    *,
    minimum_annotation_height_pixels: int = DEFAULT_MINIMUM_ANNOTATION_HEIGHT_PIXELS,
) -> RenderedOverviewMetrics:
    """Reject a capture whose actual component annotations are too small."""

    metrics = rendered_overview_metrics(image_path)
    if not metrics.passes_annotation_height(minimum_annotation_height_pixels):
        actual = metrics.typical_annotation_height_pixels
        actual_text = "absent" if actual is None else f"{actual}px"
        raise ToolchainError(
            "rendered sheet text is illegible: typical component annotation is "
            f"{actual_text}, minimum is {minimum_annotation_height_pixels}px"
        )
    return metrics


def require_rendered_overview_quality(
    image_path: Path,
    *,
    minimum_annotation_height_pixels: int = DEFAULT_MINIMUM_ANNOTATION_HEIGHT_PIXELS,
    crop_left: int | None = None,
    crop_bottom: int | None = None,
) -> RenderedOverviewMetrics:
    """Reject fitted evidence whose delivered annotation text is illegible."""

    metrics = rendered_overview_metrics(
        image_path,
        crop_left=crop_left,
        crop_bottom=crop_bottom,
    )
    if not metrics.passes_annotation_height(minimum_annotation_height_pixels):
        actual = metrics.typical_annotation_height_pixels
        actual_text = "absent" if actual is None else f"{actual}px"
        raise ToolchainError(
            "rendered sheet text is illegible: typical component annotation is "
            f"{actual_text}, minimum is {minimum_annotation_height_pixels}px"
        )
    return metrics
