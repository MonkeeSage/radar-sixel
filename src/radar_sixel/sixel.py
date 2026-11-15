"""Minimal sixel encoder built on Pillow and numpy."""

from __future__ import annotations

import re

import numpy as np
from PIL import Image

MAX_COLORS = 255
_WEIGHTS = (1 << np.arange(6, dtype=np.uint8))[:, None]
_RUN_RE = re.compile(rb"([?-~])\1{3,}")


def _rle(data: bytes) -> bytes:
    return _RUN_RE.sub(lambda m: b"!%d%c" % (len(m.group(0)), m.group(1)[0]), data)


def encode(image: Image.Image) -> bytes:
    """Encode an image as a sixel DCS sequence."""
    quantized = image.convert("RGB").quantize(
        colors=MAX_COLORS,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    width, height = quantized.size
    pixels = np.asarray(quantized, dtype=np.int16)
    palette = quantized.getpalette() or []
    ncolors = int(pixels.max()) + 1

    # Pad to whole six-pixel bands; -1 never matches a palette index.
    pad = -height % 6
    if pad:
        pixels = np.vstack([pixels, np.full((pad, width), -1, dtype=np.int16)])

    out = [b'\x1bPq"1;1;%d;%d' % (width, height)]
    for i in range(ncolors):
        r, g, b = palette[i * 3 : i * 3 + 3]
        out.append(b"#%d;2;%d;%d;%d" % (i, r * 100 // 255, g * 100 // 255, b * 100 // 255))

    for top in range(0, pixels.shape[0], 6):
        band = pixels[top : top + 6]
        rows = []
        for color in np.unique(band):
            if color < 0:
                continue
            bits = ((band == color) * _WEIGHTS).sum(axis=0, dtype=np.uint8)
            data = (bits + 63).tobytes().rstrip(b"?")
            rows.append(b"#%d%s" % (color, _rle(data)))
        out.append(b"$".join(rows) + b"-")

    out.append(b"\x1b\\")
    return b"".join(out)
