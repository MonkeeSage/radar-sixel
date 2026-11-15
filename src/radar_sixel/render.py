"""Frame sizing and composition (map + legend)."""

from __future__ import annotations

import io

from PIL import Image

from .api import mercator_bbox

THEMES = {
    "dark": (0x1C, 0x1C, 0x1C),
    "light": (0xE8, 0xE8, 0xE8),
}
LEGEND_PAD = 8


def bgcolor(theme: str) -> str:
    """WMS bgcolor parameter for a theme, e.g. "0x1C1C1C"."""
    return "0x%02X%02X%02X" % THEMES[theme]


def map_size(
    bbox: tuple[float, float, float, float], size: int, max_height: int | None = None
) -> tuple[int, int]:
    """Fit the map's Web Mercator aspect ratio in a size x size box."""
    x0, y0, x1, y1 = mercator_bbox(bbox)
    aspect = (x1 - x0) / (y1 - y0)
    if aspect >= 1:
        width, height = size, round(size / aspect)
    else:
        width, height = round(size * aspect), size
    if max_height is not None and height > max_height:
        width, height = round(width * max_height / height), max_height
    return max(width, 1), max(height, 1)


def legend_height(legend: Image.Image | None, width: int) -> int:
    if legend is None:
        return 0
    scale = min(1.0, width / legend.width)
    return round(legend.height * scale) + 2 * LEGEND_PAD


def load_legend(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


def compose(map_png: bytes, legend: Image.Image | None, theme: str) -> Image.Image:
    """Stack the map and (optionally) the legend centered below it."""
    radar = Image.open(io.BytesIO(map_png)).convert("RGB")
    if legend is None:
        return radar

    if legend.width > radar.width:
        height = round(legend.height * radar.width / legend.width)
        legend = legend.resize((radar.width, max(height, 1)), Image.Resampling.LANCZOS)

    canvas = Image.new(
        "RGB", (radar.width, radar.height + legend.height + 2 * LEGEND_PAD), THEMES[theme]
    )
    canvas.paste(radar, (0, 0))
    canvas.paste(
        legend,
        ((radar.width - legend.width) // 2, radar.height + LEGEND_PAD),
        legend,
    )
    return canvas
