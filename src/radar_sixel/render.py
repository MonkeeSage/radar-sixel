"""Frame sizing and composition (map + legend)."""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

from .api import mercator_bbox

THEMES = {
    "dark": (0x1C, 0x1C, 0x1C),
    "light": (0xE8, 0xE8, 0xE8),
}
LEGEND_PAD = 8
# --marker pins: (fill, outline) per theme, default size (radius and height)
# in pixels, and default opacity.
PIN_COLORS = {
    "dark": ((0xFF, 0xFF, 0xFF), (0x00, 0x00, 0x00)),
    "light": ((0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF)),
}
PIN_RADIUS = 7
PIN_HEIGHT = 22
PIN_SCALE = 4
MARKER_OPACITY = 0.3
LABEL_SIZE = 13


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


def marker_position(
    bbox: tuple[float, float, float, float], size: tuple[int, int], lat: float, lon: float
) -> tuple[float, float] | None:
    """Pixel position of a point on the map, or None if it is outside."""
    if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
        return None
    x0, y0, x1, y1 = mercator_bbox(bbox)
    px, py = mercator_bbox((lon, lat, lon, lat))[:2]
    return (px - x0) / (x1 - x0) * size[0], (y1 - py) / (y1 - y0) * size[1]


def _draw_pin(
    draw: ImageDraw.ImageDraw, x: float, y: float, height: int, fill: tuple, edge: tuple
) -> None:
    """A map pin of the given height whose tip is at (x, y)."""
    h = height * PIN_SCALE
    r = h * PIN_RADIUS / PIN_HEIGHT
    cx, cy = x * PIN_SCALE, y * PIN_SCALE - h + r
    # Tangent points of the lines from the tip to the circle.
    d = h - r
    s = math.asin(r / d)
    tx, ty = r * math.cos(s), r * math.sin(s)
    outline = h * 2 / PIN_HEIGHT
    draw.polygon([(cx - tx, cy + ty), (cx + tx, cy + ty), (x * PIN_SCALE, y * PIN_SCALE)], fill=edge)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=edge)
    inner = r - outline
    draw.polygon(
        [
            (cx - tx * inner / r, cy + ty * inner / r),
            (cx + tx * inner / r, cy + ty * inner / r),
            (x * PIN_SCALE, y * PIN_SCALE - outline * d / r),
        ],
        fill=fill,
    )
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=fill)
    dot = inner * 0.4
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=edge)


def contrast(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever stands out more against color."""
    luma = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    return (0, 0, 0) if luma >= 128 else (0xFF, 0xFF, 0xFF)


def marker_layer(
    size: tuple[int, int],
    markers: list[tuple[float, float, str]],
    theme: str,
    color: tuple[int, int, int] | None = None,
    opacity: float = MARKER_OPACITY,
    pin_height: int = PIN_HEIGHT,
) -> Image.Image:
    """Transparent map-sized layer with a pin at each (x, y) and its label.

    Pins are pin_height pixels tall, drawn at the given opacity in color (or
    the theme's default);
    labels stay opaque so they are readable.
    """
    fill, edge = (color, contrast(color)) if color else PIN_COLORS[theme]
    # Pins are drawn supersampled, then scaled down for smooth edges.
    pins = Image.new("RGBA", (size[0] * PIN_SCALE, size[1] * PIN_SCALE))
    draw = ImageDraw.Draw(pins)
    for x, y, _ in markers:
        _draw_pin(draw, x, y, pin_height, fill + (255,), edge + (255,))
    pins = pins.resize(size, Image.Resampling.LANCZOS)
    pins.putalpha(pins.getchannel("A").point(lambda a: round(a * opacity)))

    layer = Image.new("RGBA", size)
    layer.alpha_composite(pins)
    draw = ImageDraw.Draw(layer)
    font = ImageFont.load_default(LABEL_SIZE)
    for x, y, label in markers:
        if not label:
            continue
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font, stroke_width=2)
        width, height = right - left, bottom - top
        lx = min(max(x - width / 2, 0), max(size[0] - width, 0)) - left
        ly = max(y - pin_height - 2 - height, 0) - top
        draw.text((lx, ly), label, font=font, fill=fill, stroke_width=2, stroke_fill=edge)
    return layer


def compose(
    map_png: bytes,
    legend: Image.Image | None,
    theme: str,
    markers: Image.Image | None = None,
) -> Image.Image:
    """Stack the map (with any marker layer) and the legend centered below it."""
    radar = Image.open(io.BytesIO(map_png)).convert("RGB")
    if markers is not None:
        if markers.size != radar.size:
            markers = markers.resize(radar.size, Image.Resampling.LANCZOS)
        radar = Image.alpha_composite(radar.convert("RGBA"), markers).convert("RGB")
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
