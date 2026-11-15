import re
import time
from datetime import datetime, timezone

import pytest

from radar_sixel import api, cli, term

TIMES = [f"2026-11-15T00:0{i}:00.000Z" for i in range(3)]
ENDS = datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)
ALERTS = [
    api.Alert("Tornado Warning", "KFWS", "W", "u1", ENDS),
    api.Alert("Wind Advisory", "KFWS", "Y", "u2", None),
    api.Alert("Wind Advisory", "KOUN", "Y", "u3", ENDS),
]
TEXTS = {
    url: api.AlertText(event, f"{event} headline", "NWS", "Tarrant, TX", "BODY " + url, "")
    for event, url in (("Tornado Warning", "u1"), ("Wind Advisory", "u2"), ("Wind Advisory", "u3"))
}


class FakeTerm:
    """Delivers scripted keys once every frame has been fetched."""

    def __init__(self, keys, show):
        self.keys = list(keys)
        self.show = show

        self.screens = []

    def write(self, data):
        self.screens.append(data)

    def size(self):
        return (40, 120, 0, 0)

    def read_key(self, timeout):
        if any(f is None for f in self.show.frames):
            time.sleep(0.001)
            return None
        key = self.keys.pop(0) if self.keys else "quit"
        if key is None:
            time.sleep(0.002)
        return key


@pytest.fixture
def make_show(monkeypatch):
    monkeypatch.setattr(api, "fetch_legend", lambda layer: b"")
    monkeypatch.setattr(api, "fetch_frame", lambda layer, ts, w, h, bg, overlays=(): ts.encode())
    monkeypatch.setattr(cli.render, "load_legend", lambda data: None)
    monkeypatch.setattr(
        cli.render, "compose", lambda png, legend, theme, markers=None: png + (markers or b"")
    )
    monkeypatch.setattr(cli.render, "marker_layer", lambda *a: b"+pins")
    monkeypatch.setattr(cli.sixel, "encode", lambda image: image)
    monkeypatch.setattr(api, "overlay_times", lambda: {})
    monkeypatch.setattr(api, "fetch_alerts", lambda overlays, bbox, ts, snapshots: ALERTS)
    monkeypatch.setattr(api, "fetch_alert_text", lambda alert: TEXTS[alert.url])

    def make(keys, delay, loop=False, overlays=(), markers=None, station_count=1):
        layer = api.Layer("kabx", "sr_bref", "", "", (0, 0, 1, 1), tuple(TIMES))
        show = cli.Slideshow(
            None,
            layer,
            TIMES,
            (10, 10),
            "dark",
            delay,
            loop,
            legend=True,
            overlays=overlays,
            markers=markers,
            station_count=station_count,
        )
        show.term = FakeTerm(keys, show)
        shown = []
        original = show._show
        show._show = lambda i: (shown.append(i), original(i))
        return show, shown

    return make


def test_plays_once_then_stops(make_show):
    show, shown = make_show([None] * 15, delay=0.001)
    show.run()
    assert shown == [0, 1, 2]


def test_space_at_end_replays(make_show):
    show, shown = make_show([None] * 15 + ["space"] + [None] * 15, delay=0.001)
    show.run()
    assert shown == [0, 1, 2, 0, 1, 2]


def test_loop(make_show):
    show, shown = make_show([None] * 20, delay=0.001, loop=True)
    show.run()
    assert shown[:5] == [0, 1, 2, 0, 1]


def test_arrows_step_and_pause(make_show):
    keys = [None, "right", "right", "right", "left", "space", None, "space", "left", "left"]
    show, shown = make_show(keys, delay=100)
    show.run()
    # Right clamps at the last frame; left clamps at the first.
    assert shown == [0, 1, 2, 1, 0]


def test_space_pauses_playback(make_show):
    show, shown = make_show([None, "space"] + [None] * 15, delay=0.001)
    show.run()
    assert shown == [0, 1]


def test_failed_frame_is_shown_as_error(make_show, monkeypatch):
    def fetch(layer, ts, w, h, bg, overlays=()):
        if ts == TIMES[1]:
            raise api.ApiError("boom")
        return ts.encode()

    monkeypatch.setattr(api, "fetch_frame", fetch)
    show, shown = make_show([None] * 15, delay=0.001)
    show.run()
    assert shown == [0, 1, 2]
    assert isinstance(show.frames[1], api.ApiError)


def test_alert_summary_groups_by_event(make_show):
    show, shown = make_show([None] * 15, delay=0.001, overlays=("hazards",))
    show.run()
    lines = show._alert_lines(0, 120)
    assert "Tornado Warning  KFWS  until Sun 01:00 UTC" in lines[0]
    assert lines[0].startswith(f"{cli.CSI}31m")
    assert "Wind Advisory \u00d72  KFWS, KOUN  until Sun 01:00 UTC" in lines[1]
    assert "m: messages" in show._status_text(0, 0, False)


def test_alert_summary_truncates(make_show, monkeypatch):
    many = [api.Alert(f"Event {i}", "KFWS", "", f"u{i}", None) for i in range(6)]
    monkeypatch.setattr(api, "fetch_alerts", lambda *a: many)
    show, shown = make_show([None] * 15, delay=0.001, overlays=("warnings",))
    show.run()
    lines = show._alert_lines(0, 120)
    assert len(lines) == cli.ALERT_LINES
    assert lines[-1] == "... and 3 more (m: read messages)"


def test_no_alerts_without_timed_overlays(make_show, monkeypatch):
    monkeypatch.setattr(api, "fetch_alerts", lambda *a: pytest.fail("not expected"))
    show, shown = make_show([None] * 15, delay=0.001, overlays=("counties",))
    show.run()
    assert show.alerts == [None] * 3
    assert "m: messages" not in show._status_text(0, 0, False)


def test_messages_view_pauses_and_returns(make_show):
    keys = ["messages"] + [None] * 10 + ["end", "esc"] + [None] * 5 + ["space"] + [None] * 15
    show, shown = make_show(keys, delay=0.001, overlays=("hazards",))
    show.run()
    # Paused on frame 0 while reading, redrawn on return, then resumed.
    assert shown[:2] == [0, 0] and shown[-1] == 2
    assert all(url in show.texts for url in ("u1", "u2", "u3"))
    view = next(s for s in show.term.screens if isinstance(s, str) and "BODY u1" in s)
    assert "Tornado Warning headline" in view and "Areas: Tarrant, TX" in view
    # Every cell is written, so no rows are left over the removed image.
    rows = re.split(r"\x1b\[\d+;1H", view)[1:]
    assert len(rows) == 40
    assert all(len(cli.SGR_RE.sub("", r)) == 120 for r in rows)


def test_l_toggles_looping(make_show):
    show, shown = make_show(["loop"] + [None] * 15, delay=0.001)
    show.run()
    assert shown[:5] == [0, 1, 2, 0, 1]
    assert "l: stop looping" in show._status_text(0, 0, False)


def test_l_stops_looping(make_show):
    show, shown = make_show(["loop"] + [None] * 15, delay=0.001, loop=True)
    show.run()
    assert shown == [0, 1, 2]
    assert "l: loop" in show._status_text(2, 2, False)


def test_t_toggles_markers(make_show):
    keys = [None, "space", "markers", "right", "markers"]
    show, shown = make_show(keys, delay=100, markers=[(5, 5, "Home")])
    show.run()
    assert shown == [0, 0, 1, 1]
    images = [s for s in show.term.screens if isinstance(s, bytes) and b"2026" in s]
    assert [b"+pins" in s for s in images] == [True, False, False, True]
    assert "t: hide markers" in show._status_text(1, 1, False)


def test_t_ignored_without_markers(make_show):
    show, shown = make_show([None, "space", "markers"], delay=100)
    show.run()
    assert shown == [0]
    assert show.plain == [None] * 3
    assert "t:" not in show._status_text(0, 0, False)


def test_s_switches_station_and_resumes(make_show):
    show, shown = make_show([None, "space", "right", "station"], delay=100, station_count=2)
    assert show.run() == "station"
    assert shown == [0, 1]
    assert "s: next station (1/2)" in show._status_text(1, 1, False)
    fetcher = show._fetcher
    show.term.keys = [None, "right"]
    assert show.run() == "quit"
    # Back on this station: redrawn where it was, still paused, not refetched.
    assert shown == [0, 1, 1, 2]
    assert show._fetcher is fetcher


def test_s_ignored_with_one_station(make_show):
    show, shown = make_show([None, "space", "station"], delay=100)
    assert show.run() == "quit"
    assert shown == [0]
    assert "s:" not in show._status_text(0, 0, False)


def test_cycle_stations_wraps_and_carries_toggles():
    class Show:
        def __init__(self, name, results):
            self.name, self.results = name, results
            self.loop = self.show_markers = False

        def run(self):
            order.append((self.name, self.loop, self.show_markers))
            self.loop = self.show_markers = True
            return self.results.pop(0)

    order = []
    a, b = Show("a", ["station", "quit"]), Show("b", ["station"])
    cli.cycle_stations([a, b])
    assert order == [("a", False, False), ("b", True, True), ("a", True, True)]


def test_fill_line():
    assert cli.fill_line("ab", 4) == f"ab{cli.CSI}0;8m  {cli.CSI}0m"
    assert cli.fill_line(f"{cli.CSI}1mab{cli.CSI}0m", 2) == f"{cli.CSI}1mab{cli.CSI}0m"


def test_exit_blanks_every_cell(monkeypatch):
    t = term.Terminal.__new__(term.Terminal)
    t._alt_screen = True
    out = []
    monkeypatch.setattr(t, "write", out.append)
    monkeypatch.setattr(t, "size", lambda: (3, 5, 0, 0))
    monkeypatch.setattr(term.time, "sleep", lambda s: out.append(s))
    t.leave_alt_screen()
    blank = f"{cli.CSI}0;8m     {cli.CSI}0m"
    # The blank screen is written and left up before switching back.
    assert out == [
        "".join(f"{cli.CSI}{y};1H{blank}" for y in (1, 2, 3)),
        term.BLANK_DELAY,
        f"{cli.CSI}?25h{cli.CSI}?1049l",
    ]
    t.leave_alt_screen()
    assert len(out) == 3
