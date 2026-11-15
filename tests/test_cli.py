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
