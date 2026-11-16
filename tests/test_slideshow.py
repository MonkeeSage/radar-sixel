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


def test_alert_summary_shows_most_severe(make_show):
    show, _ = make_show([None] * 15, delay=0.001, overlays=("hazards",))
    show.run()
    line = show._alert_line(0, 120)
    assert line == (
        f"{cli.CSI}31mTornado Warning  KFWS  until Sun 01:00 UTC{cli.CSI}0m"
        "  (+1 more, m: messages)"
    )
    assert "m: messages" in show._status_text(0, 0, False)


def test_alert_summary_groups_first_event(make_show, monkeypatch):
    winds = [a for a in ALERTS if a.event == "Wind Advisory"]
    monkeypatch.setattr(api, "fetch_alerts", lambda *a: winds)
    show, _ = make_show([None] * 15, delay=0.001, overlays=("warnings",))
    show.run()
    line = show._alert_line(0, 120)
    assert "Wind Advisory \u00d72  KFWS, KOUN  until Sun 01:00 UTC" in line
    assert "more" not in line


def test_alert_summary_keeps_count_when_narrow(make_show):
    show, _ = make_show([None] * 15, delay=0.001, overlays=("hazards",))
    show.run()
    plain = cli.SGR_RE.sub("", show._alert_line(0, 30))
    assert plain == "Tornad  (+1 more, m: messages)"


def test_no_alerts_without_timed_overlays(make_show, monkeypatch):
    monkeypatch.setattr(api, "fetch_alerts", lambda *a: pytest.fail("not expected"))
    show, shown = make_show([None] * 15, delay=0.001, overlays=("counties",))
    show.run()
    assert show.alerts == [None] * 3
    status = show._status_text(0, 0, False)
    assert "m: messages" not in status
    assert status.split("\r\n")[1] == "No warning or alert overlays."


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
            self.overlays = ("counties",)
            self._position = (1, 1, False)

        def run(self):
            order.append((self.name, self.loop, self.show_markers, self.overlays))
            self.loop = self.show_markers = True
            self.overlays = ("counties", "warnings")
            return self.results.pop(0)

        def _toggle_overlay(self, name, current):
            assert current == 1
            self.overlays += (name,)

    order = []
    a, b = Show("a", ["station", "quit"]), Show("b", ["station"])
    cli.cycle_stations([a, b])
    shown = ("counties", "warnings")
    assert order == [
        ("a", False, False, ("counties",)),
        ("b", True, True, shown),
        ("a", True, True, shown),
    ]


def test_h_and_w_toggle_overlays(make_show, monkeypatch):
    fetched = []

    def fetch(layer, ts, w, h, bg, overlays=()):
        fetched.append((TIMES.index(ts), overlays))
        return ts.encode() + b"".join(b"+" + o.encode() for o in overlays)

    monkeypatch.setattr(api, "fetch_frame", fetch)
    alerts_for = []
    monkeypatch.setattr(
        api, "fetch_alerts", lambda overlays, *a: alerts_for.append(overlays) or ALERTS
    )
    keys = [None, "space", "right", "hazards", None, "warnings", None, "hazards", None]
    show, shown = make_show(keys, delay=100, overlays=("counties",))
    assert "h: show hazards  w: show warnings" in show._status_text(0, 0, False)
    show.run()
    assert shown == [0, 1, 1, 1, 1]
    # Each new combination is fetched starting from the frame on screen.
    assert fetched == [
        (0, ("counties",)),
        (1, ("counties",)),
        (2, ("counties",)),
        (1, ("counties", "hazards")),
        (2, ("counties", "hazards")),
        (0, ("counties", "hazards")),
        (1, ("counties", "hazards", "warnings")),
        (2, ("counties", "hazards", "warnings")),
        (0, ("counties", "hazards", "warnings")),
        (1, ("counties", "warnings")),
        (2, ("counties", "warnings")),
        (0, ("counties", "warnings")),
    ]
    assert alerts_for == [("hazards",)] * 3 + [("hazards", "warnings")] * 3 + [("warnings",)] * 3
    images = [s for s in show.term.screens if isinstance(s, bytes) and b"2026" in s]
    assert [i.count(b"+") for i in images] == [1, 1, 2, 3, 2]
    assert "h: show hazards  w: hide warnings" in show._status_text(1, 1, False)
    assert "Tornado Warning" in show._status_text(1, 1, False)


def test_toggling_back_uses_cached_frames(make_show, monkeypatch):
    fetched = []
    monkeypatch.setattr(
        api, "fetch_frame", lambda layer, ts, w, h, bg, overlays=(): fetched.append(ts) or b"x"
    )
    show, shown = make_show([None, "space", "warnings", None, "warnings", None], delay=100)
    show.run()
    assert len(fetched) == 6
    assert shown == [0, 0, 0]
    assert show.timed == ()
    assert "m: messages" not in show._status_text(0, 0, False)


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


def test_side_panel_geometry():
    # 10x20 pixel cells; an 800x600 map spans 80 columns and 30 rows.
    assert cli.side_panel((40, 200, 2000, 800), (800, 600)) == (83, 117, 30)
    assert cli.side_panel((40, 200, 0, 0), (800, 600)) is None
    assert cli.side_panel((40, 100, 1000, 800), (800, 600)) is None


def panel_writes(show):
    return [s for s in show.term.screens if isinstance(s, str) and f"{cli.CSI}1;83H" in s]


def test_alerts_go_beside_the_map(make_show):
    keys = [None, "space", "warnings", None]
    show, _ = make_show(keys, delay=100, overlays=("hazards",))
    show.size = (800, 600)
    show.term.size = lambda: (40, 200, 2000, 800)
    show.run()
    status = show._status_text(0, 0, False)
    assert "Tornado Warning" not in status and "No warning" not in status
    assert len(status.split("\r\n")) == cli.STATUS_LINES
    lines = show._panel_lines(0, 117, 30)
    assert cli.SGR_RE.sub("", lines[0]) == "Alerts in view at 00:00 UTC"
    assert [cli.SGR_RE.sub("", line) for line in lines[1:]] == [
        "Tornado Warning",
        "  KFWS",
        "  until Sun 01:00 UTC",
        "Wind Advisory ×2",
        "  KFWS, KOUN",
        "  until Sun 01:00 UTC",
    ]
    assert lines[1].startswith(f"{cli.CSI}1;31m")
    writes = panel_writes(show)
    assert "Tornado Warning" in writes[0]
    # Every panel row is overwritten to the panel width, never erased.
    for write in writes:
        assert f"{cli.CSI}K" not in write and f"{cli.CSI}J" not in write
        rows = re.split(r"\x1b\[\d+;83H", write)[1:]
        assert all(len(cli.SGR_RE.sub("", r)) == 117 for r in rows)


def test_panel_blanked_when_overlays_turned_off(make_show):
    show, _ = make_show([None], delay=100, overlays=("hazards",))
    show.size = (800, 600)
    show.term.size = lambda: (40, 200, 2000, 800)
    show.run()
    assert len(show._panel) == 7
    # Turned off while the frame on screen still shows the overlays.
    show.overlays = ()
    show._select()
    show._draw_panel(0)
    assert show._panel == []
    blank = f"{cli.CSI}0;8m{' ' * 117}{cli.CSI}0m"
    assert show.term.screens[-1] == "".join(f"{cli.CSI}{y};83H{blank}" for y in range(1, 8))


def test_panel_counts_event_types_left_out(make_show, monkeypatch):
    many = [api.Alert(f"Event {i}", "KFWS", "", f"u{i}", None) for i in range(6)]
    monkeypatch.setattr(api, "fetch_alerts", lambda *a: many)
    show, _ = make_show([None] * 15, delay=0.001, overlays=("warnings",))
    show.run()
    lines = show._panel_lines(0, 40, 6)
    assert len(lines) == 6
    assert lines[-1] == "... and 4 more (m: messages)"
    assert len(show._panel_lines(0, 40, 13)) == 13


def test_fit_size_reserves_alert_line_only_without_panel(monkeypatch):
    monkeypatch.setattr(cli.render, "map_size", lambda bbox, box, max_height: (box, max_height))
    layer = api.Layer("kabx", "sr_bref", "", "", (0, 0, 1, 1), ())

    class Term:
        def __init__(self, size):
            self.size = lambda: size

    wide = cli.fit_size(Term((40, 200, 2000, 800)), layer, 800, legend=False)
    narrow = cli.fit_size(Term((40, 100, 1000, 800)), layer, 800, legend=False)
    # 20 pixel rows: status and help (plus one spare) vs. the extra alert line.
    assert wide == (800, 800 - 3 * 20 - 6)
    assert narrow == (800, 800 - 4 * 20 - 6)
