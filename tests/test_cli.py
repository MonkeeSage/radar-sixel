import pytest

from radar_sixel import api, cli

TIMES = tuple(f"2026-11-15T0{i}:00:00.000Z" for i in range(5))
LAYERS = {
    p: api.Layer("conus", p, f"{p} title", "radar_reflectivity", (0, 0, 1, 1), TIMES)
    for p in ("bref_qcd", "cref_qcd")
}


@pytest.fixture(autouse=True)
def fake_layers(monkeypatch):
    requested = []

    def workspace_layers(ws):
        requested.append(ws)
        return LAYERS

    monkeypatch.setattr(api, "workspace_layers", workspace_layers)
    return requested


def test_products_follows_region_product_count_and_overlays(capsys, fake_layers):
    code = cli.main(["-r", "CONUS", "--products", "-p", "cref_qcd", "-n", "2", "-o", "warnings"])
    out = capsys.readouterr().out
    assert code == 0
    assert fake_layers == ["conus"]
    assert "CONUS products" in out and "newest 2 captures" in out
    lines = out.splitlines()
    cref = next(line for line in lines if "cref_qcd" in line)
    bref = next(line for line in lines if "bref_qcd" in line)
    assert cref.startswith("*") and bref.startswith(" ")
    assert "2/5" in cref and "2026-11-15 03:00–04:00 UTC" in cref
    assert "warnings" in out and "matched to each frame's time" in out


def test_products_marks_default(capsys):
    assert cli.main(["-s", "kabx", "--products"]) == 0
    out = capsys.readouterr().out
    assert next(line for line in out.splitlines() if "bref_qcd" in line).startswith("*")
    assert "Overlays" not in out


def test_unknown_product_errors(capsys):
    with pytest.raises(SystemExit):
        cli.main(["-r", "conus", "--products", "-p", "sr_bvel"])
    assert "available: bref_qcd, cref_qcd" in capsys.readouterr().err


def test_station_and_region_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["-s", "kabx", "-r", "conus"])
    assert "not allowed with" in capsys.readouterr().err


def test_bad_overlay_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["-s", "kabx", "-o", "roads"])
    assert "invalid choice" in capsys.readouterr().err


def test_parse_marker():
    assert cli.parse_marker("32.75,-97.33") == cli.Marker(32.75, -97.33)
    assert cli.parse_marker("32.75,-97.33, Fort Worth, TX") == cli.Marker(
        32.75, -97.33, "Fort Worth, TX"
    )
    for bad in ("32.75", "north,west", "91,0", "0,181"):
        with pytest.raises(Exception):
            cli.parse_marker(bad)


def test_bad_marker_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["-s", "kabx", "--marker", "abc,1"])
    assert "invalid coordinates" in capsys.readouterr().err


def test_markers_outside_map_are_skipped(capsys):
    markers = [cli.Marker(0.5, 0.5, "in"), cli.Marker(5, 5, "out"), cli.Marker(-1, 0.5)]
    placed = cli.place_markers(markers, (0, 0, 1, 1), (100, 100))
    assert [label for *_, label in placed] == ["in"]
    err = capsys.readouterr().err
    assert "marker 'out' (5,5) is outside the map area" in err
    assert "marker -1,0.5 is outside" in err


def test_parse_color():
    assert cli.parse_color("#FF8800") == (255, 136, 0)
    assert cli.parse_color("ff8800") == (255, 136, 0)
    assert cli.parse_color("#f80") == (255, 136, 0)
    for bad in ("#ff88", "orange", "#gg0000"):
        with pytest.raises(Exception):
            cli.parse_color(bad)


def test_marker_style_options(capsys):
    args = cli.build_parser().parse_args(["-s", "kabx"])
    assert args.marker_opacity == 30 and args.marker_color is None
    assert args.marker_size == 22
    assert cli.build_parser().parse_args(["-s", "kabx", "--marker-size", "40"]).marker_size == 40
    args = cli.build_parser().parse_args(
        ["-s", "kabx", "--marker-opacity", "0", "--marker-color", "#00ff00"]
    )
    assert args.marker_opacity == 0 and args.marker_color == (0, 255, 0)
    with pytest.raises(SystemExit):
        cli.main(["-s", "kabx", "--marker-opacity", "101"])
    assert "must be from 0 to 100" in capsys.readouterr().err
