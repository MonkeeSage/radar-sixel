"""Command-line interface and slideshow loop."""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__, api, render, sixel
from .term import CSI, ESC, Terminal

PROG = "radar-sixel"
STATUS_LINES = 2
# Alert summary lines under the status line when hazards/warnings are shown.
ALERT_LINES = 4
SIG_COLORS = {"W": "31", "A": "33", "Y": "36"}
SCROLL_KEYS = {"up", "down", "pageup", "pagedown", "home", "end"}
SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def fill_line(text: str, cols: int) -> str:
    """Pad a line to the full width with concealed spaces.

    Some terminals (e.g. Konsole) only repaint rows whose cells changed, so
    rows left blank over a removed sixel image keep showing its pixels.
    Concealed spaces look blank but differ from empty cells, forcing every
    row to be repainted, both now and when the image is drawn again.
    """
    pad = cols - len(SGR_RE.sub("", text))
    return f"{text}{CSI}0;8m{' ' * pad}{CSI}0m" if pad > 0 else text


@dataclass
class Messages:
    """State of the full-text alert view for one frame."""

    index: int
    alerts: list[api.Alert]
    offset: int = 0
    drawn: str = ""
    # Identity token: the text fetcher stops once the view is closed.
    token: object = field(default_factory=object)


class Slideshow:
    """Fetches frames serially in the background while the UI plays them."""

    def __init__(
        self,
        term: Terminal,
        layer: api.Layer,
        times: list[str],
        size: tuple[int, int],
        theme: str,
        delay: float,
        loop: bool,
        legend: bool,
        overlays: tuple[str, ...] = (),
    ) -> None:
        self.term = term
        self.layer = layer
        self.times = times
        self.size = size
        self.theme = theme
        self.delay = delay
        self.loop = loop
        self.legend = legend
        self.overlays = overlays
        self.timed = tuple(o for o in overlays if api.OVERLAYS[o].timed)
        # Each slot holds encoded sixel bytes, the fetch error, or None (pending).
        self.frames: list[bytes | Exception | None] = [None] * len(times)
        # Alerts in view per frame, filled in before the frame itself.
        self.alerts: list[list[api.Alert] | Exception | None] = [None] * len(times)
        # Full alert text by URL, shared between frames.
        self.texts: dict[str, api.AlertText | Exception] = {}
        self.messages: Messages | None = None
        self._status = ""

    def _fetch_all(self) -> None:
        legend = None
        if self.legend:
            try:
                legend = render.load_legend(api.fetch_legend(self.layer))
            except Exception:
                pass  # The legend is optional; frames still render without it.
        snapshots: dict[str, tuple[str, ...]] | Exception = {}
        if self.timed:
            try:
                snapshots = api.overlay_times()
            except Exception as e:
                snapshots = e
        for i, ts in enumerate(self.times):
            if self.timed:
                try:
                    if isinstance(snapshots, Exception):
                        raise snapshots
                    self.alerts[i] = api.fetch_alerts(self.timed, self.layer.bbox, ts, snapshots)
                except Exception as e:
                    self.alerts[i] = e
            try:
                png = api.fetch_frame(
                    self.layer, ts, *self.size, render.bgcolor(self.theme), self.overlays
                )
                self.frames[i] = sixel.encode(render.compose(png, legend, self.theme))
            except Exception as e:
                self.frames[i] = e

    def _show(self, index: int) -> None:
        frame = self.frames[index]
        if isinstance(frame, Exception):
            body = f"Could not load capture {index + 1}: {frame}\r\n".encode()
        else:
            assert frame is not None
            body = frame + b"\r\n"
        # Clear, draw, then save the cursor so the status lines can be redrawn
        # below the image without touching it.
        self.term.write(f"{CSI}H{CSI}2J".encode() + body + f"{ESC}7".encode())
        self._status = ""

    def _draw_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.term.write(f"{ESC}8{CSI}J{text}")

    def _status_text(self, current: int | None, target: int, playing: bool) -> str:
        n = len(self.times)
        cols = self.term.size()[1] or 80
        loaded = sum(f is not None for f in self.frames)
        parts = [f"{self.layer.workspace.upper()} {self.layer.product}"]
        if self.overlays:
            parts[0] += f" +{','.join(self.overlays)}"
        if current is not None:
            when = api.parse_time(self.times[current])
            age = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
            parts.append(f"{current + 1}/{n}")
            parts.append(f"{when:%Y-%m-%d %H:%M} UTC ({age} min ago)")
        if target != current:
            parts.append(f"loading {target + 1}/{n}...")
        elif playing:
            parts.append("playing")
        elif current == n - 1 and not self.loop:
            parts.append("end")
        else:
            parts.append("paused")
        if loaded < n:
            parts.append(f"[fetched {loaded}/{n}]")
        lines = ["  ".join(parts)[:cols]]
        if self.timed:
            lines += self._alert_lines(current, cols)
        help_text = "space: play/pause  ←/→: step  "
        if self.timed:
            help_text += "m: messages  "
        lines.append((help_text + "q/esc: quit")[:cols])
        return "\r\n".join(lines)

    def _alert_lines(self, index: int | None, cols: int) -> list[str]:
        """Summary of a frame's alerts, one line per event type."""
        alerts = None if index is None else self.alerts[index]
        if alerts is None:
            return ["alerts: loading..."]
        if isinstance(alerts, Exception):
            return [f"alerts: {alerts}"[:cols]]
        if not alerts:
            return ["no active alerts in view"]
        groups: dict[str, list[api.Alert]] = {}
        for alert in alerts:
            groups.setdefault(alert.event, []).append(alert)
        lines = []
        for event, group in groups.items():
            text = event if len(group) == 1 else f"{event} \u00d7{len(group)}"
            offices = sorted({a.wfo for a in group if a.wfo})
            if offices:
                text += f"  {', '.join(offices)}"
            ends = [a.ends for a in group if a.ends]
            if ends:
                text += f"  until {max(ends):%a %H:%M} UTC"
            lines.append((group[0].sig, text))
        if len(lines) > ALERT_LINES:
            more = len(lines) - ALERT_LINES + 1
            lines = lines[: ALERT_LINES - 1] + [("", f"... and {more} more (m: read messages)")]
        out = []
        for sig, text in lines:
            color = SIG_COLORS.get(sig)
            text = text[:cols]
            out.append(f"{CSI}{color}m{text}{CSI}0m" if color else text)
        return out

    def _open_messages(self, index: int) -> None:
        alerts = self.alerts[index]
        self.messages = Messages(index, alerts if isinstance(alerts, list) else [])
        threading.Thread(
            target=self._fetch_texts, args=(self.messages,), daemon=True
        ).start()

    def _fetch_texts(self, messages: Messages) -> None:
        for alert in messages.alerts:
            if self.messages is not messages:
                return
            if alert.url not in self.texts:
                try:
                    self.texts[alert.url] = api.fetch_alert_text(alert)
                except Exception as e:
                    self.texts[alert.url] = e

    def _message_lines(self, alerts: list[api.Alert], width: int) -> list[str]:
        def wrap(text: str, indent: str = "") -> list[str]:
            out = []
            for line in text.splitlines():
                out += textwrap.wrap(
                    line, width, initial_indent=indent, subsequent_indent=indent
                ) or [""]
            return out

        if not alerts:
            return ["No active alerts in view for this frame."]
        lines: list[str] = []
        for i, alert in enumerate(alerts, 1):
            color = SIG_COLORS.get(alert.sig)
            title = f"{i}/{len(alerts)}  {alert.event}  {alert.wfo}"[:width]
            lines.append(f"{CSI}1;{color}m{title}{CSI}0m" if color else f"{CSI}1m{title}{CSI}0m")
            text = self.texts.get(alert.url)
            if text is None:
                lines.append("loading...")
            elif isinstance(text, Exception):
                lines += wrap(f"could not load message: {text}")
            else:
                if text.headline:
                    lines += wrap(text.headline)
                if text.area:
                    lines += wrap(f"Areas: {text.area}")
                for body in (text.description, text.instruction):
                    if body:
                        lines.append("")
                        lines += wrap(body)
            lines += ["\u2500" * min(width, 72), ""]
        return lines

    def _draw_messages(self) -> None:
        messages = self.messages
        assert messages is not None
        rows, cols = self.term.size()[:2]
        rows, cols = rows or 24, cols or 80
        lines = self._message_lines(messages.alerts, max(cols - 1, 20))
        page = max(rows - 1, 1)
        messages.offset = min(max(messages.offset, 0), max(len(lines) - page, 0))
        shown = lines[messages.offset : messages.offset + page]
        body = shown + [""] * (page - len(shown))
        loaded = sum(a.url in self.texts for a in messages.alerts)
        when = api.parse_time(self.times[messages.index])
        footer = (
            f"alerts at {when:%H:%M} UTC  lines {messages.offset + 1}-"
            f"{messages.offset + len(shown)}/{len(lines)}"
        )
        if loaded < len(messages.alerts):
            footer += f"  loading {loaded}/{len(messages.alerts)}..."
        footer += "  \u2191/\u2193 PgUp/PgDn: scroll  m/esc: back  q: quit"
        screen = (
            f"{CSI}H{CSI}2J"
            + "".join(f"{CSI}{y};1H{fill_line(line, cols)}" for y, line in enumerate(body, 1))
            + f"{CSI}{rows};1H{fill_line(f'{CSI}7m{footer[:cols]}', cols)}"
        )
        if screen != messages.drawn:
            messages.drawn = screen
            self.term.write(screen)

    def _scroll_messages(self, key: str) -> None:
        messages = self.messages
        assert messages is not None
        page = max((self.term.size()[0] or 24) - 2, 1)
        delta = {"up": -1, "down": 1, "pageup": -page, "pagedown": page}
        if key == "home":
            messages.offset = 0
        elif key == "end":
            messages.offset = sys.maxsize  # Clamped when drawn.
        else:
            messages.offset += delta[key]

    def run(self) -> None:
        n = len(self.times)
        threading.Thread(target=self._fetch_all, daemon=True).start()

        self.term.write(f"{CSI}H{CSI}2J{ESC}7")
        current: int | None = None
        target = 0
        playing = True
        next_due = 0.0

        while True:
            if self.messages is not None:
                self._draw_messages()
                key = self.term.read_key(0.1)
                if key == "quit":
                    return
                if key in ("esc", "messages"):
                    self.messages = None
                    assert current is not None
                    self._show(current)
                elif key in SCROLL_KEYS:
                    self._scroll_messages(key)
                continue

            now = time.monotonic()
            if playing and current is not None and target == current and now >= next_due:
                if current < n - 1:
                    target = current + 1
                elif self.loop and n > 1:
                    target = 0
                else:
                    playing = False

            if target != current and self.frames[target] is not None:
                self._show(target)
                current = target
                next_due = now + self.delay

            self._draw_status(self._status_text(current, target, playing))

            key = self.term.read_key(0.1)
            if key in ("quit", "esc"):
                return
            if key == "messages" and self.timed and current is not None:
                playing = False
                target = current
                self._open_messages(current)
            elif key == "space":
                if playing:
                    playing = False
                    if current is not None:
                        target = current
                else:
                    playing = True
                    if current == n - 1 and target == current and not self.loop:
                        target = 0  # Replay from the start.
                    next_due = time.monotonic() + self.delay
            elif key in ("left", "right"):
                playing = False
                base = target if current is None else current
                step = -1 if key == "left" else 1
                target = min(max(base + step, 0), n - 1)


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _positive_float(value: str) -> float:
    n = float(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Show the most recent NOAA radar captures for a station or region "
            "as a sixel slideshow."
        ),
        epilog="overlays: "
        + "; ".join(f"{k} = {o.description}" for k, o in api.OVERLAYS.items())
        + ". keys: space play/pause, left/right step, m read alert messages "
        "(with hazards/warnings), q or esc quit",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-l",
        "--list",
        nargs="?",
        const="",
        metavar="FILTER",
        help="list stations, optionally filtered by ID, name or WFO",
    )
    source.add_argument("-s", "--station", help="station ID, e.g. KABX")
    source.add_argument(
        "-r",
        "--region",
        type=str.lower,
        choices=api.REGIONS,
        help="show a regional MRMS mosaic instead of a single station",
    )
    parser.add_argument(
        "--products",
        action="store_true",
        help="list the products for the selected station or region, marking the "
        "one selected and the captures that would be shown",
    )
    parser.add_argument(
        "-p",
        "--product",
        type=str.lower,
        help="product to show (default: base reflectivity, i.e. sr_bref for "
        "NEXRAD stations, bref1 for TDWR, bref_qcd for regions)",
    )
    parser.add_argument(
        "-o",
        "--overlay",
        action="append",
        default=[],
        type=str.lower,
        choices=list(api.OVERLAYS),
        help="add a map overlay; repeat for more (see below)",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=_positive_int,
        default=10,
        help="number of most recent captures to show (default: %(default)s)",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=_positive_float,
        default=1.5,
        help="seconds between frames (default: %(default)s)",
    )
    parser.add_argument(
        "--loop", action="store_true", help="loop the slideshow instead of stopping"
    )
    parser.add_argument(
        "--size",
        type=_positive_int,
        default=800,
        help="maximum image width/height in pixels (default: %(default)s)",
    )
    parser.add_argument(
        "--theme",
        choices=sorted(render.THEMES),
        default="dark",
        help="map background (default: %(default)s)",
    )
    parser.add_argument(
        "--no-legend", action="store_true", help="do not draw the color legend"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the terminal sixel support check",
    )
    parser.add_argument("-V", "--version", action="version", version=__version__)
    return parser


def print_stations(filter_text: str) -> None:
    needle = filter_text.lower()
    stations = [
        s
        for s in api.list_stations()
        if needle in s.id or needle in s.name.lower() or needle in s.wfo.lower()
    ]
    if not stations:
        print(f"no stations match {filter_text!r}", file=sys.stderr)
        return
    width = max(len("NAME"), *(len(s.name) for s in stations)) + 2
    print(f"{'ID':<6}{'NAME':<{width}}{'WFO':<6}{'LAT':>9}{'LON':>11}")
    for s in stations:
        lat = f"{s.lat:9.4f}" if s.lat is not None else " " * 9
        lon = f"{s.lon:11.4f}" if s.lon is not None else " " * 11
        print(f"{s.id.upper():<6}{s.name:<{width}}{s.wfo:<6}{lat}{lon}")


def _time_range(times: list[str]) -> str:
    if not times:
        return "no captures"
    first, last = api.parse_time(times[0]), api.parse_time(times[-1])
    if first == last:
        return f"{first:%Y-%m-%d %H:%M} UTC"
    return f"{first:%Y-%m-%d %H:%M}\u2013{last:%H:%M} UTC"


def print_products(
    layers: dict[str, api.Layer], selected: str, count: int, overlays: list[str]
) -> None:
    """List products, marking the selection and the captures -n would show."""
    workspace = next(iter(layers.values())).workspace.upper()
    plural = "capture" if count == 1 else "captures"
    print(f"{workspace} products (* = selected; showing newest {count} {plural}):")
    width = max(len(p) for p in layers) + 2
    for product, layer in sorted(layers.items()):
        mark = "*" if product == selected else " "
        shown = list(layer.times[-count:])
        print(
            f"{mark} {product:<{width}}{len(shown):>3}/{len(layer.times):<3} "
            f"{_time_range(shown):<29} {layer.title}"
        )
    if overlays:
        print("\nOverlays:")
        for name in overlays:
            o = api.OVERLAYS[name]
            timing = "matched to each frame's time" if o.timed else "static"
            print(f"  {name:<{width}}{o.layer:<20} {timing:<29} {o.description}")


def fit_size(
    term: Terminal, layer: api.Layer, size: int, legend: bool, alerts: bool = False
) -> tuple[int, int]:
    """Map size from --size, shrunk if the terminal reports a smaller window."""
    rows, _, xpix, ypix = term.size()
    box = min(size, xpix) if xpix else size
    max_height = None
    if ypix and rows:
        cell_height = ypix / rows
        # Leave room for the legend, the status lines and sixel band rounding.
        lines = STATUS_LINES + (ALERT_LINES if alerts else 0)
        reserved = (lines + 1) * cell_height + 6
        if legend:
            reserved += 30 + 2 * render.LEGEND_PAD
        max_height = max(int(ypix - reserved), 32)
    return render.map_size(layer.bbox, box, max_height)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.list is not None:
            print_stations(args.list)
            return 0
        workspace = args.station or args.region
        if not workspace:
            parser.error("one of --list, --station or --region is required")
        # Keep the order given, dropping repeats.
        overlays = tuple(dict.fromkeys(args.overlay))

        layers = api.workspace_layers(workspace)
        product = args.product or api.default_product(layers)
        if product not in layers:
            parser.error(
                f"{workspace.upper()} has no product {product!r} "
                f"(available: {', '.join(sorted(layers))})"
            )
        if args.products:
            print_products(layers, product, args.count, list(overlays))
            return 0

        layer = layers[product]
        times = list(layer.times[-args.count :])
        if not times:
            raise api.ApiError(f"no captures available for {layer.name}")

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.error("the slideshow needs an interactive terminal")

        with Terminal() as term:
            if not args.force and term.supports_sixel() is False:
                raise api.ApiError(
                    "this terminal does not report sixel support (use --force to try anyway)"
                )
            timed = any(api.OVERLAYS[o].timed for o in overlays)
            size = fit_size(term, layer, args.size, not args.no_legend, timed)
            term.enter_alt_screen()
            Slideshow(
                term,
                layer,
                times,
                size,
                args.theme,
                args.delay,
                args.loop,
                legend=not args.no_legend,
                overlays=overlays,
            ).run()
    except api.ApiError as e:
        print(f"{PROG}: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
