from datetime import datetime, timezone

import pytest

from radar_sixel import api, render

CAPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Name>WMS</Name>
      <Layer queryable="1" opaque="0">
        <Name>kabx_sr_bref</Name>
        <Abstract>Super Resolution Base Reflectivity</Abstract>
        <EX_GeographicBoundingBox>
          <westBoundLongitude>-111.824</westBoundLongitude>
          <eastBoundLongitude>-101.82455</eastBoundLongitude>
          <southBoundLatitude>30.15055</southBoundLatitude>
          <northBoundLatitude>40.15</northBoundLatitude>
        </EX_GeographicBoundingBox>
        <Dimension name="time" default="2026-11-15T02:32:26Z" units="ISO8601">2026-11-15T00:43:17.000Z,2026-11-15T00:38:26.000Z,2026-11-15T02:32:26.000Z</Dimension>
        <Style><Name>radar_reflectivity</Name></Style>
      </Layer>
      <Layer queryable="1" opaque="0">
        <Name>kabx_sr_bvel</Name>
        <EX_GeographicBoundingBox>
          <westBoundLongitude>-111.824</westBoundLongitude>
          <eastBoundLongitude>-101.82455</eastBoundLongitude>
          <southBoundLatitude>30.15055</southBoundLatitude>
          <northBoundLatitude>40.15</northBoundLatitude>
        </EX_GeographicBoundingBox>
        <Style><Name>radar_velocity</Name></Style>
      </Layer>
      <Layer><Name>radar_reflectivity</Name></Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""


def test_parse_capabilities():
    layers = api.parse_capabilities("kabx", CAPS)
    assert sorted(layers) == ["sr_bref", "sr_bvel"]
    bref = layers["sr_bref"]
    assert bref.qualified_name == "kabx:kabx_sr_bref"
    assert bref.bbox == (-111.824, 30.15055, -101.82455, 40.15)
    # Sorted oldest first regardless of server order.
    assert bref.times == (
        "2026-11-15T00:38:26.000Z",
        "2026-11-15T00:43:17.000Z",
        "2026-11-15T02:32:26.000Z",
    )
    assert layers["sr_bvel"].times == ()


def test_default_product():
    layers = api.parse_capabilities("kabx", CAPS)
    assert api.default_product(layers) == "sr_bref"
    tdwr = {
        "bvel": api.Layer("tatl", "bvel", "", "radar_velocity", (0, 0, 1, 1), ()),
        "bref1": api.Layer("tatl", "bref1", "", "radar_reflectivity", (0, 0, 1, 1), ()),
    }
    assert api.default_product(tdwr) == "bref1"


def test_parse_time():
    expected = datetime(2026, 11, 15, 2, 32, 26, tzinfo=timezone.utc)
    assert api.parse_time("2026-11-15T02:32:26.000Z") == expected
    assert api.parse_time("2026-11-15T02:32:26Z") == expected
    with pytest.raises(api.ApiError):
        api.parse_time("yesterday")


def test_map_size_keeps_mercator_aspect():
    bbox = (-111.824, 30.15055, -101.82455, 40.15)
    width, height = render.map_size(bbox, 800)
    assert height == 800 and width < 800
    x0, y0, x1, y1 = api.mercator_bbox(bbox)
    assert width / height == pytest.approx((x1 - x0) / (y1 - y0), rel=0.01)
    assert render.map_size(bbox, 800, max_height=400)[1] == 400


def test_default_product_for_region():
    layers = {
        p: api.Layer("conus", p, "", "radar_reflectivity", (0, 0, 1, 1), ())
        for p in ("cref_qcd", "bref_qcd", "neet_v18")
    }
    assert api.default_product(layers) == "bref_qcd"


def test_fetch_frame_draw_order(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_get_image", lambda url, params: calls.append(params) or b"")
    layer = api.Layer("kabx", "sr_bref", "", "", (-111.8, 30.1, -101.8, 40.1), ())
    api.fetch_frame(layer, "T", 10, 10, "0x000000", ("warnings", "counties", "hazards"))

    params = calls[0]
    assert params["layers"].split(",") == [
        "wwa:hazards",
        "kabx:kabx_sr_bref",
        "nws:us_counties",
        "geopolitical",
        "wwa:warnings",
    ]
    assert params["styles"].split(",") == ["wwa_hazards", "", "boundary_gray", "", "wwa_warnings"]
    assert params["time"] == "T"
    assert params["crs"] == "EPSG:3857"


def test_fetch_frame_without_overlays(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_get_image", lambda url, params: calls.append(params) or b"")
    layer = api.Layer("conus", "cref_qcd", "", "", (-130, 20, -60, 55), ())
    api.fetch_frame(layer, "T", 10, 10, "0x000000")
    assert calls[0]["layers"] == "conus:conus_cref_qcd,geopolitical"
    assert calls[0]["styles"] == ","


def test_invalid_station_rejected_without_request(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda *a, **k: pytest.fail("no request expected"))
    with pytest.raises(api.ApiError, match="invalid station"):
        api.workspace_layers("../etc")


def _feature(event, url, sig="W", wfo="KFWS", vtec=None):
    return {"properties": {"prod_type": event, "url": url, "sig": sig, "wfo": wfo, "vtec": vtec}}


def test_fetch_alerts_dedupes_and_sorts(monkeypatch):
    responses = {
        "wwa:hazards": [
            _feature("Wind Advisory", "u1", sig="Y"),
            _feature("Flood Watch", "u2", sig="A"),
            _feature("Flood Warning", "u4"),
            _feature("Severe Thunderstorm Warning", "u5"),
            _feature("Wind Advisory", "u1", sig="Y"),  # second zone, same alert
            _feature(None, ""),  # empty-snapshot placeholder
        ],
        "wwa:warnings": [
            _feature(
                "Tornado Warning", "u3", vtec="/O.NEW.KFWS.TO.W.0001.261115T0300Z-261115T0345Z/"
            ),
        ],
    }
    calls = []

    def get(url, params):
        calls.append(params)
        return api.json.dumps({"features": responses[params["typeName"]]}).encode()

    monkeypatch.setattr(api, "_get", get)
    snapshots = {
        "hazards": ("2026-11-15T02:58:00.000Z", "2026-11-15T03:02:00.000Z"),
        "warnings": ("2026-11-15T03:01:00.000Z",),
    }
    alerts = api.fetch_alerts(
        ("counties", "hazards", "warnings"), (-98, 32, -96, 34), "2026-11-15T03:01:30Z", snapshots
    )
    assert [a.event for a in alerts] == [
        "Tornado Warning",
        "Severe Thunderstorm Warning",
        "Flood Warning",
        "Flood Watch",
        "Wind Advisory",
    ]
    assert alerts[0].ends == datetime(2026, 11, 15, 3, 45, tzinfo=timezone.utc)
    assert alerts[3].ends is None
    # Only timed overlays are queried, each at the snapshot the map shows.
    assert [c["typeName"] for c in calls] == ["wwa:hazards", "wwa:warnings"]
    assert "IDP_FileDate = '2026-11-15T03:02:00.000Z'" in calls[0]["CQL_FILTER"]
    assert "'EPSG:3857')" in calls[0]["CQL_FILTER"]


def test_nearest_time():
    times = ("2026-11-15T03:00:00Z", "2026-11-15T03:02:00Z")
    assert api.nearest_time(times, "2026-11-15T03:01:30Z") == times[1]
    assert api.nearest_time(times, "2026-11-15T01:00:00Z") == times[0]
    assert api.nearest_time((), "2026-11-15T01:00:00Z") is None


def test_alert_text_only_from_weather_gov(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda *a, **k: pytest.fail("no request expected"))
    alert = api.Alert("Flood Warning", "KCRP", "W", "https://example.com/x", None)
    with pytest.raises(api.ApiError, match="unexpected alert URL"):
        api.fetch_alert_text(alert)
