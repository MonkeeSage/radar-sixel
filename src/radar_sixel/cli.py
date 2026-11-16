"""Command-line interface and slideshow loop."""

from __future__ import annotations

import argparse
import math
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
# Status and help lines under the map.
STATUS_LINES = 2
# Smallest side panel worth showing, and its gap from the map, in columns.
PANEL_MIN_WIDTH = 30
PANEL_GAP = 2
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


@dataclass(frozen=True)
class Marker:
    lat: float
    lon: float
    label: str = ""

    def __str__(self) -> str:
        where = f"{self.lat:g},{self.lon:g}"
        return f"{self.label!r} ({where})" if self.label else where


def parse_marker(value: str) -> Marker:
    """argparse type for --marker LAT,LON[,LABEL]."""
    parts = value.split(",", 2)
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(f"expected LAT,LON[,LABEL], got {value!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid coordinates in {value!r}") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise argparse.ArgumentTypeError(f"coordinates out of range in {value!r}")
    return Marker(lat, lon, parts[2].strip() if len(parts) > 2 else "")


def side_panel(
    term_size: tuple[int, int, int, int], map_size: tuple[int, int]
) -> tuple[int, int, int] | None:
    """First column, width and rows of the alert panel right of the map.

    None if the terminal does not report its pixel size or the panel would
    be too narrow; the alert summary then goes under the status line.
    """
    rows, cols, xpix, ypix = term_size
    if not (rows and cols and xpix and ypix):
        return None
    first = math.ceil(map_size[0] * cols / xpix) + PANEL_GAP + 1
    # The last column is left alone so no line can wrap.
    width = cols - first
    height = int(map_size[1] * rows / ypix)
    if width < PANEL_MIN_WIDTH or height < 1:
        return None
    return first, width, height


def place_markers(
    markers: list[Marker],
    bbox: tuple[float, float, float, float],
    size: tuple[int, int],
    where: str = "",
) -> list[tuple[float, float, str]]:
    """Pixel positions and labels of the markers, warning about any off the map."""
    area = f"the {where} map area" if where else "the map area"
    placed = []
    for marker in markers:
        pos = render.marker_position(bbox, size, marker.lat, marker.lon)
        if pos is None:
            print(f"{PROG}: marker {marker} is outside {area}; skipping", file=sys.stderr)
        else:
            placed.append((*pos, marker.label))
    return placed


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
        markers: list[tuple[float, float, str]] | None = None,
        marker_color: tuple[int, int, int] | None = None,
        marker_opacity: float = render.MARKER_OPACITY,
        marker_size: int = render.PIN_HEIGHT,
        station_index: int = 0,
        station_count: int = 1,
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
        # Marker pixel positions and labels, already limited to the map.
        self.markers = markers or []
        self.marker_color = marker_color
        self.marker_opacity = marker_opacity
        self.marker_size = marker_size
        # Position among the -s stations the s key cycles through.
        self.station_index = station_index
        self.station_count = station_count
        # Frames, frames without markers, and alerts for each set of timed
        # overlays shown so far, keyed by that set; the h and w keys switch
        # between them.
        self._cache: dict[tuple[str, ...], tuple[list, list, list]] = {}
        self._select()
        self.show_markers = True
        # Full alert text by URL, shared between frames.
        self.texts: dict[str, api.AlertText | Exception] = {}
        self.messages: Messages | None = None
        self._status = ""
        # Side panel lines on screen.
        self._panel: list[str] = []
        self._fetcher: threading.Thread | None = None
        # Wakes the fetcher when an overlay toggle gives it more to fetch.
        self._wake = threading.Condition()
        # Frame the fetcher starts from: the one on screen when toggled.
        self._fetch_from = 0
        # The timed overlays of the frame on screen.
        self._shown_timed: tuple[str, ...] | None = None
        # (current, target, playing), kept so switching back to this station
        # resumes where it left off.
        self._position: tuple[int | None, int, bool] = (None, 0, True)

    def _select(self) -> None:
        """Point frames, plain and alerts at the cache for the current overlays."""
        self.timed = tuple(o for o in api.OVERLAYS if o in self.overlays and api.OVERLAYS[o].timed)
        n = len(self.times)
        # Each frame slot holds encoded sixel bytes, the fetch error, or None
        # (pending). plain holds the same frames without markers, filled in
        # just before frames; alerts holds the alerts in view per frame,
        # filled in before the frame itself.
        self.frames, self.plain, self.alerts = self._cache.setdefault(
            self.timed, ([None] * n, [None] * n, [None] * n)
        )

    def _toggle_overlay(self, name: str, current: int | None) -> None:
        with self._wake:
            if name in self.overlays:
                self.overlays = tuple(o for o in self.overlays if o != name)
            else:
                self.overlays += (name,)
            self._select()
            self._fetch_from = current or 0
            self._wake.notify()

    def _next_fetch(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple, int] | None:
        """The overlays, timed overlays, cache entry and frame to fetch next."""
        n = len(self.times)
        for k in range(n):
            i = (self._fetch_from + k) % n
            if self.frames[i] is None:
                return self.overlays, self.timed, (self.frames, self.plain, self.alerts), i
        return None

    def _fetch_all(self) -> None:
        legend = None
        if self.legend:
            try:
                legend = render.load_legend(api.fetch_legend(self.layer))
            except Exception:
                pass  # The legend is optional; frames still render without it.
        markers = None
        if self.markers:
            markers = render.marker_layer(
                self.size,
                self.markers,
                self.theme,
                self.marker_color,
                self.marker_opacity,
                self.marker_size,
            )
        # Fetched the first time a timed overlay is shown.
        snapshots: dict[str, tuple[str, ...]] | Exception | None = None
        while True:
            with self._wake:
                while (work := self._next_fetch()) is None:
                    self._wake.wait()
            overlays, timed, (frames, plain, alerts), i = work
            ts = self.times[i]
            if timed:
                if snapshots is None:
                    try:
                        snapshots = api.overlay_times()
                    except Exception as e:
                        snapshots = e
                try:
                    if isinstance(snapshots, Exception):
                        raise snapshots
                    alerts[i] = api.fetch_alerts(timed, self.layer.bbox, ts, snapshots)
                except Exception as e:
                    alerts[i] = e
            try:
                png = api.fetch_frame(
                    self.layer, ts, *self.size, render.bgcolor(self.theme), overlays
                )
                if markers is not None:
                    plain[i] = sixel.encode(render.compose(png, legend, self.theme))
                frames[i] = sixel.encode(render.compose(png, legend, self.theme, markers))
            except Exception as e:
                plain[i] = frames[i] = e

    def _show(self, index: int) -> None:
        hidden = self.markers and not self.show_markers
        frame = (self.plain if hidden else self.frames)[index]
        if isinstance(frame, Exception):
            body = f"Could not load capture {index + 1}: {frame}\r\n".encode()
        else:
            assert frame is not None
            body = frame + b"\r\n"
        # Clear, draw, then save the cursor so the status lines can be redrawn
        # below the image without touching it.
        self.term.write(f"{CSI}H{CSI}2J".encode() + body + f"{ESC}7".encode())
        self._status = ""
        self._panel = []
        self._shown_timed = self.timed

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
        if side_panel(self.term.size(), self.size) is None:
            lines.append(self._alert_line(current, cols))
        help_text = "space: play/pause  ←/→: step  "
        if self.timed:
            help_text += "m: messages  "
        help_text += f"l: {'stop looping' if self.loop else 'loop'}  "
        for key, name in (("h", "hazards"), ("w", "warnings")):
            help_text += f"{key}: {'hide' if name in self.overlays else 'show'} {name}  "
        if self.markers:
            help_text += f"t: {'hide' if self.show_markers else 'show'} markers  "
        if self.station_count > 1:
            help_text += f"s: next station ({self.station_index + 1}/{self.station_count})  "
        lines.append((help_text + "q/esc: quit")[:cols])
        return "\r\n".join(lines)

    def _alert_line(self, index: int | None, cols: int) -> str:
        """The frame's most severe alert, with a count of any others."""
        if not self.timed:
            return "No warning or alert overlays."[:cols]
        alerts = None if index is None else self.alerts[index]
        if alerts is None:
            return "alerts: loading..."
        if isinstance(alerts, Exception):
            return f"alerts: {alerts}"[:cols]
        if not alerts:
            return "no active alerts in view"
        # Alerts come most severe first; the first event type is summarized.
        event = alerts[0].event
        group = [a for a in alerts if a.event == event]
        text = event if len(group) == 1 else f"{event} \u00d7{len(group)}"
        offices = sorted({a.wfo for a in group if a.wfo})
        if offices:
            text += f"  {', '.join(offices)}"
        ends = [a.ends for a in group if a.ends]
        if ends:
            text += f"  until {max(ends):%a %H:%M} UTC"
        others = len({a.event for a in alerts}) - 1
        more = f"  (+{others} more, m: messages)" if others else ""
        # Keep the count visible by shortening the summary instead.
        text = text[: max(cols - len(more), 0)]
        color = SIG_COLORS.get(alerts[0].sig)
        return (f"{CSI}{color}m{text}{CSI}0m" if color else text) + more[: cols - len(text)]

    def _panel_lines(self, index: int | None, width: int, height: int) -> list[str]:
        """The frame's alerts by event type, most severe first, to fit the panel."""
        alerts = None if index is None else self.alerts[index]
        if alerts is None:
            return ["alerts: loading..."]
        if isinstance(alerts, Exception):
            return textwrap.wrap(f"alerts: {alerts}", width)[:height]
        when = api.parse_time(self.times[index])
        header = f"{CSI}1m{f'Alerts in view at {when:%H:%M} UTC'[:width]}{CSI}0m"
        if not alerts:
            return [header, "none"][:height]
        groups: dict[str, list[api.Alert]] = {}
        for alert in alerts:
            groups.setdefault(alert.event, []).append(alert)
        blocks = []
        for event, group in groups.items():
            title = (event if len(group) == 1 else f"{event} \u00d7{len(group)}")[:width]
            color = SIG_COLORS.get(group[0].sig)
            block = [f"{CSI}1;{color}m{title}{CSI}0m" if color else title]
            offices = sorted({a.wfo for a in group if a.wfo})
            if offices:
                block += textwrap.wrap(
                    ", ".join(offices), width, initial_indent="  ", subsequent_indent="  "
                )
            ends = [a.ends for a in group if a.ends]
            if ends:
                block.append(f"  until {max(ends):%a %H:%M} UTC"[:width])
            blocks.append(block)
        lines = [header]
        for i, block in enumerate(blocks):
            # Keep a line for the count of event types left out, if any.
            room = height - len(lines) - (i < len(blocks) - 1)
            if len(block) > room:
                more = len(blocks) - i
                lines.append(f"... and {more} more (m: messages)"[:width])
                break
            lines += block
        return lines[:height]

    def _draw_panel(self, index: int | None) -> None:
        """Write the alert panel right of the map, if there is room for one.

        Old lines are overwritten with concealed spaces rather than erased:
        tmux drops a sixel image when an erase touches any of its rows.
        """
        panel = side_panel(self.term.size(), self.size)
        if panel is None:
            return
        first, width, height = panel
        lines = self._panel_lines(index, width, height) if self.timed else []
        if lines == self._panel:
            return
        rows = len(lines) + max(len(self._panel) - len(lines), 0)
        padded = lines + [""] * (rows - len(lines))
        self._panel = lines
        self.term.write(
            "".join(f"{CSI}{y};{first}H{fill_line(line, width)}" for y, line in enumerate(padded, 1))
        )

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

    def run(self) -> str:
        """Play until quit; return "quit", or "station" to switch stations."""
        n = len(self.times)
        if self._fetcher is None:
            self._fetcher = threading.Thread(target=self._fetch_all, daemon=True)
            self._fetcher.start()

        current, target, playing = self._position
        if current is None or self.frames[current] is None:
            # A frame not loaded yet is drawn by the loop once it is.
            self.term.write(f"{CSI}H{CSI}2J{ESC}7")
            self._status = ""
            self._panel = []
            self._shown_timed = None
        else:
            self._show(current)
        next_due = time.monotonic() + self.delay

        while True:
            if self.messages is not None:
                self._draw_messages()
                key = self.term.read_key(0.1)
                if key == "quit":
                    return "quit"
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

            # After an h or w toggle, redraw once the current frame is loaded.
            if (
                current is not None
                and target == current
                and self._shown_timed != self.timed
                and self.frames[current] is not None
            ):
                self._show(current)

            if target != current and self.frames[target] is not None:
                self._show(target)
                current = target
                next_due = now + self.delay

            self._draw_status(self._status_text(current, target, playing))
            self._draw_panel(current)

            key = self.term.read_key(0.1)
            if key in ("quit", "esc"):
                return "quit"
            if key == "station" and self.station_count > 1:
                self._position = (current, target, playing)
                return "station"
            if key == "messages" and self.timed and current is not None:
                playing = False
                target = current
                self._open_messages(current)
            elif key == "loop":
                self.loop = not self.loop
            elif key in ("hazards", "warnings"):
                self._toggle_overlay(key, current)
            elif key == "markers" and self.markers and current is not None:
                self.show_markers = not self.show_markers
                if self.frames[current] is not None:
                    self._show(current)
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


def _percent(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError("must be from 0 to 100")
    return n


HEX_COLOR_RE = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


def parse_color(value: str) -> tuple[int, int, int]:
    """argparse type for a hex color: #RRGGBB or #RGB, the # optional."""
    match = HEX_COLOR_RE.fullmatch(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"expected a hex color like #FF8800, got {value!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


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
        "(with hazards/warnings), h/w show/hide hazards/warnings, l loop on/off, "
        "t show/hide markers, s next station "
        "(with several -s), q or esc quit",
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
    source.add_argument(
        "-s",
        "--station",
        action="append",
        help="station ID, e.g. KABX; repeat to cycle between stations with the s key",
    )
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
        "--marker",
        action="append",
        default=[],
        type=parse_marker,
        metavar="LAT,LON[,LABEL]",
        help="mark a location on the map, with an optional label; repeat for more",
    )
    parser.add_argument(
        "--marker-opacity",
        type=_percent,
        default=round(render.MARKER_OPACITY * 100),
        metavar="PERCENT",
        help="marker pin opacity from 0 to 100 (default: %(default)s)",
    )
    parser.add_argument(
        "--marker-size",
        type=_positive_int,
        default=render.PIN_HEIGHT,
        metavar="PX",
        help="marker pin height in pixels (default: %(default)s)",
    )
    parser.add_argument(
        "--marker-color",
        type=parse_color,
        metavar="HEX",
        help="marker pin and label color, e.g. '#FF8800' (default: white on the "
        "dark theme, black on light)",
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


def fit_size(term: Terminal, layer: api.Layer, size: int, legend: bool) -> tuple[int, int]:
    """Map size from --size, shrunk if the terminal reports a smaller window."""
    term_size = term.size()
    rows, _, xpix, ypix = term_size
    box = min(size, xpix) if xpix else size

    def fit(lines: int) -> tuple[int, int]:
        max_height = None
        if ypix and rows:
            cell_height = ypix / rows
            # Leave room for the legend, the lines below and sixel band rounding.
            reserved = (lines + 1) * cell_height + 6
            if legend:
                reserved += 30 + 2 * render.LEGEND_PAD
            max_height = max(int(ypix - reserved), 32)
        return render.map_size(layer.bbox, box, max_height)

    map_size = fit(STATUS_LINES)
    if side_panel(term_size, map_size) is None:
        # No room beside the map: the alert summary goes under the status line.
        map_size = fit(STATUS_LINES + 1)
    return map_size


def cycle_stations(shows: list[Slideshow]) -> None:
    """Run each station's slideshow in turn as the s key is pressed."""
    i = 0
    while shows[i].run() == "station":
        nxt = shows[(i + 1) % len(shows)]
        # The l, t, h and w toggles carry over to the next station.
        nxt.loop, nxt.show_markers = shows[i].loop, shows[i].show_markers
        for name in ("hazards", "warnings"):
            if (name in shows[i].overlays) != (name in nxt.overlays):
                nxt._toggle_overlay(name, nxt._position[0])
        i = (i + 1) % len(shows)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.list is not None:
            print_stations(args.list)
            return 0
        # Keep the order given, dropping repeats.
        workspaces = list(dict.fromkeys(s.lower() for s in args.station or ()))
        if args.region:
            workspaces = [args.region]
        if not workspaces:
            parser.error("one of --list, --station or --region is required")
        overlays = tuple(dict.fromkeys(args.overlay))

        # Check every station before showing anything.
        selected: list[tuple[dict[str, api.Layer], str]] = []
        for workspace in workspaces:
            layers = api.workspace_layers(workspace)
            product = args.product or api.default_product(layers)
            if product not in layers:
                parser.error(
                    f"{workspace.upper()} has no product {product!r} "
                    f"(available: {', '.join(sorted(layers))})"
                )
            selected.append((layers, product))
        if args.products:
            for i, (layers, product) in enumerate(selected):
                if i:
                    print()
                print_products(layers, product, args.count, list(overlays))
            return 0

        shown: list[tuple[api.Layer, list[str]]] = []
        for layers, product in selected:
            layer = layers[product]
            times = list(layer.times[-args.count :])
            if not times:
                raise api.ApiError(f"no captures available for {layer.name}")
            shown.append((layer, times))

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.error("the slideshow needs an interactive terminal")

        with Terminal() as term:
            if not args.force and term.supports_sixel() is False:
                raise api.ApiError(
                    "this terminal does not report sixel support (use --force to try anyway)"
                )
            shows = []
            for i, (layer, times) in enumerate(shown):
                size = fit_size(term, layer, args.size, not args.no_legend)
                # Warnings go to the main screen, which is restored on exit.
                markers = place_markers(
                    list(dict.fromkeys(args.marker)),
                    layer.bbox,
                    size,
                    layer.workspace.upper() if len(shown) > 1 else "",
                )
                shows.append(
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
                        markers=markers,
                        marker_color=args.marker_color,
                        marker_opacity=args.marker_opacity / 100,
                        marker_size=args.marker_size,
                        station_index=i,
                        station_count=len(shown),
                    )
                )
            term.enter_alt_screen()
            cycle_stations(shows)
    except api.ApiError as e:
        print(f"{PROG}: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
