import re

import numpy as np
from PIL import Image

from radar_sixel import sixel


def decode(data: bytes) -> tuple[np.ndarray, dict[int, tuple[int, int, int]]]:
    """Tiny sixel decoder covering what the encoder emits."""
    assert data.startswith(b"\x1bPq") and data.endswith(b"\x1b\\")
    body = data[3:-2].decode()
    m = re.match(r'"1;1;(\d+);(\d+)', body)
    width, height = int(m.group(1)), int(m.group(2))
    body = body[m.end():]

    palette = {}
    for i, r, g, b in re.findall(r"#(\d+);2;(\d+);(\d+);(\d+)", body):
        palette[int(i)] = (int(r), int(g), int(b))
    body = re.sub(r"#\d+;2;\d+;\d+;\d+", "", body)

    pixels = np.full((height + 6, width), -1)
    band = x = color = 0
    for tok in re.findall(r"#\d+|!\d+[?-~]|[?-~]|\$|-", body):
        if tok[0] == "#":
            color, x = int(tok[1:]), 0
        elif tok == "$":
            x = 0
        elif tok == "-":
            band, x = band + 1, 0
        else:
            count, ch = (int(tok[1:-1]), tok[-1]) if tok[0] == "!" else (1, tok)
            bits = ord(ch) - 63
            for _ in range(count):
                for row in range(6):
                    if bits & (1 << row):
                        pixels[band * 6 + row, x] = color
                x += 1
    return pixels[:height], palette


def test_roundtrip_preserves_pixels():
    rng = np.random.default_rng(0)
    colors = np.array([[0, 0, 0], [255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]])
    # Height not a multiple of 6, with runs long enough to be RLE-compressed.
    idx = rng.integers(0, len(colors), size=(13, 40))
    idx[:, 10:30] = 1
    image = Image.fromarray(colors[idx].astype(np.uint8), "RGB")

    pixels, palette = decode(sixel.encode(image))

    assert pixels.shape == (13, 40)
    assert (pixels >= 0).all()
    decoded = np.array([[palette[c] for c in row] for row in pixels])
    expected = colors[idx] * 100 // 255
    assert (decoded == expected).all()


def test_rle_used_for_runs():
    image = Image.new("RGB", (100, 6), (10, 20, 30))
    data = sixel.encode(image)
    assert b"!100~" in data
