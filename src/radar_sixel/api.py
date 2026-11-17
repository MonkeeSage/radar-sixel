"""Client for the NOAA/NCEP opengeo GeoServer (WMS/WFS) using only the stdlib."""

from __future__ import annotations

import http.client
import json
import math
import re
import threading
import time as _time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import __version__
from .cache import Cache

BASE_URL = "https://opengeo.ncep.noaa.gov/geoserver"
INDEX_URL = f"{BASE_URL}/www/index.html"
USER_AGENT = f"radar-sixel/{__version__}"
TIMEOUT = 30
# How long the station list and legends are reused before asking again; the
# server sends no caching headers for them.
DAY = 24 * 3600

# Base reflectivity: NEXRAD stations, then MRMS regional mosaics.
DEFAULT_PRODUCTS = ("sr_bref", "bref_qcd")
REGIONS = ("conus", "alaska", "hawaii", "carib", "guam")

_WMS_NS = {"wms": "http://www.opengis.net/wms"}
# Station workspaces are four-character names, e.g. ".../geoserver/kabx/ows?...".
_WORKSPACE_RE = re.compile(r"geoserver/([a-z0-9]{4})/ows")


class ApiError(Exception):
    pass


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float | None
    lon: float | None
    wfo: str


@dataclass(frozen=True)
class Layer:
    # Station ID or region name; also the GeoServer workspace.
    workspace: str
    product: str
    title: str
    style: str
    # (min lon, min lat, max lon, max lat)
    bbox: tuple[float, float, float, float]
    # ISO 8601 timestamps as advertised by the server, oldest first.
    times: tuple[str, ...]

    @property
    def name(self) -> str:
        return f"{self.workspace}_{self.product}"

    @property
    def qualified_name(self) -> str:
        return f"{self.workspace}:{self.name}"


# Draw order, bottom to top. Overlays use the BELOW/ABOVE levels.
BELOW_RADAR, _RADAR, ABOVE_RADAR, _BORDERS, ABOVE_BORDERS = range(5)


@dataclass(frozen=True)
class Overlay:
    layer: str
    style: str
    z: int
    # Time-enabled layers are matched to each frame's capture time.
    timed: bool
    description: str


OVERLAYS = {
    "counties": Overlay("nws:us_counties", "boundary_gray", ABOVE_RADAR, False, "US county lines"),
    "provinces": Overlay("nws:ca_provinces", "boundary_src", ABOVE_RADAR, False, "Canadian provinces"),
    "cwa": Overlay("nws:cwa_boundary", "boundary", ABOVE_RADAR, False, "NWS forecast office areas"),
    "rfc": Overlay("nws:rfc_boundary", "boundary", ABOVE_RADAR, False, "River Forecast Center basins"),
    "artcc": Overlay("nws:artcc_boundary", "boundary", ABOVE_RADAR, False, "FAA air route traffic control centers"),
    "hazards": Overlay("wwa:hazards", "wwa_hazards", BELOW_RADAR, True, "advisories, watches and warnings (filled)"),
    "warnings": Overlay("wwa:warnings", "wwa_warnings", ABOVE_BORDERS, True, "short-fuse warnings (outlined)"),
}


@dataclass(frozen=True)
class Alert:
    """An active watch, warning, advisory or statement from the WWA layers."""

    event: str
    wfo: str
    # VTEC significance: W(arning), A (watch), Y (advisory), S(tatement).
    sig: str
    # Full alert on api.weather.gov.
    url: str
    ends: datetime | None


@dataclass(frozen=True)
class AlertText:
    event: str
    headline: str
    sender: str
    area: str
    description: str
    instruction: str


ALERT_URL_PREFIX = "https://api.weather.gov/alerts/"
_SIG_ORDER = {"W": 0, "A": 1, "Y": 2, "S": 3}
# Short-fuse warnings (the wwa:warnings layer) are listed before the rest.
_SHORT_FUSE = (
    "Tornado Warning",
    "Extreme Wind Warning",
    "Severe Thunderstorm Warning",
    "Flash Flood Warning",
    "Snow Squall Warning",
    "Dust Storm Warning",
    "Special Marine Warning",
)
# The VTEC end time, e.g. "/O.EXT.KCRP.FL.W.0029.261115T0458Z-261117T0624Z/".
_VTEC_END_RE = re.compile(r"-(\d{6}T\d{4})Z/")


# Responses worth keeping; the CLI points this at the user's cache directory.
cache = Cache(None)

_conns: dict[str, http.client.HTTPSConnection] = {}
_host_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _connection(host: str, fresh: bool = False) -> http.client.HTTPSConnection:
    """Keep-alive connection per host, so DNS and TLS happen once per run."""
    if fresh and host in _conns:
        _conns.pop(host).close()
    if host not in _conns:
        _conns[host] = http.client.HTTPSConnection(host, timeout=TIMEOUT)
    return _conns[host]


def _host_lock(host: str) -> threading.Lock:
    with _locks_lock:
        return _host_locks.setdefault(host, threading.Lock())


def _get(
    url: str,
    params: dict[str, str] | None = None,
    *,
    ttl: float | None = None,
    tag: str = "",
    check: Callable[[bytes], None] | None = None,
) -> bytes:
    """GET a URL, through the cache when ttl is given.

    ttl is how many seconds a cached copy is used without asking the server,
    unless the server says otherwise with Cache-Control; math.inf means the
    content never changes, whatever the server says. A stale copy is
    revalidated with its ETag or Last-Modified when it has one. tag is added
    to the cache key, for what the response depends on beyond the URL. check
    raises ApiError for a response that must not be kept.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    key = f"{url} {tag}" if tag else url
    entry = cache.get(key) if ttl is not None else None
    if entry is not None and _time.time() < entry.fresh_until:
        return entry.data
    headers = {"User-Agent": USER_AGENT}
    if entry is not None:
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
    resp, data = _request(url, headers)
    if resp.status == 304 and entry is not None:
        data = entry.data
    elif resp.status != 200:
        raise ApiError(f"HTTP {resp.status} fetching {url}")
    elif check is not None:
        check(data)
    if ttl is not None:
        cache.put(
            key,
            data,
            _fresh_until(resp, ttl),
            resp.getheader("ETag") or (entry.etag if entry else ""),
            resp.getheader("Last-Modified") or (entry.last_modified if entry else ""),
        )
    return data


def _fresh_until(resp: http.client.HTTPResponse, ttl: float) -> float:
    if math.isinf(ttl):
        return ttl
    control = (resp.getheader("Cache-Control") or "").lower()
    if "no-store" in control or "no-cache" in control:
        return 0.0
    m = re.search(r"max-age=(\d+)", control)
    return _time.time() + (int(m.group(1)) if m else ttl)


def _request(url: str, headers: dict[str, str]) -> tuple[http.client.HTTPResponse, bytes]:
    parts = urllib.parse.urlsplit(url)
    path = f"{parts.path}?{parts.query}" if parts.query else parts.path
    # One request at a time per connection; other hosts are not blocked.
    with _host_lock(parts.netloc):
        for attempt in range(2):
            conn = _connection(parts.netloc, fresh=attempt > 0)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                # The server closed an idle keep-alive connection; retry once.
                if attempt:
                    _connection(parts.netloc, fresh=True)
                    raise ApiError(f"could not fetch {url}: connection closed") from None
            except (http.client.HTTPException, OSError) as e:
                _connection(parts.netloc, fresh=True)
                raise ApiError(f"could not fetch {url}: {e}") from e
    return resp, data


def _check_png(data: bytes) -> None:
    # GeoServer reports errors as XML with a 200 status.
    if not data.startswith(b"\x89PNG"):
        raise ApiError(f"server returned an error: {_service_exception(data)}")


def _check_features(data: bytes) -> None:
    try:
        json.loads(data)["features"]
    except (ValueError, KeyError, TypeError) as e:
        raise ApiError(f"server returned an error: {_service_exception(data)}") from e


def _get_image(url: str, params: dict[str, str], ttl: float | None = None, tag: str = "") -> bytes:
    return _get(url, params, ttl=ttl, tag=tag, check=_check_png)


def _service_exception(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    m = re.search(r"<(?:\w+:)?ServiceException[^>]*>(.*?)</", text, re.S)
    return (m.group(1) if m else text[:200]).strip()


def list_stations() -> list[Station]:
    """Return the radar stations that have imagery, sorted by ID."""
    index = _get(INDEX_URL, ttl=DAY).decode("utf-8", "replace")
    workspaces = set(_WORKSPACE_RE.findall(index))

    sites = json.loads(
        _get(
            f"{BASE_URL}/nws/ows",
            {
                "service": "WFS",
                "version": "1.0.0",
                "request": "GetFeature",
                "typeName": "nws:radar_sites",
                "outputFormat": "application/json",
            },
            ttl=DAY,
            check=_check_features,
        )
    )
    by_id: dict[str, Station] = {}
    for feature in sites.get("features", []):
        props = feature.get("properties", {})
        sid = str(props.get("rda_id", "")).lower()
        if sid in workspaces and sid not in by_id:
            by_id[sid] = Station(
                id=sid,
                name=" ".join(str(props.get("name") or "").split()),
                lat=props.get("lat"),
                lon=props.get("lon"),
                wfo=props.get("wfo_id") or "",
            )
    # A few workspaces have no entry in the sites layer; list them anyway.
    for sid in workspaces - by_id.keys():
        by_id[sid] = Station(id=sid, name="", lat=None, lon=None, wfo="")
    return sorted(by_id.values(), key=lambda s: s.id)


def workspace_layers(workspace: str) -> dict[str, Layer]:
    """Return a station's or region's WMS layers keyed by product (e.g. "sr_bref")."""
    workspace = workspace.lower()
    kind = "region" if workspace in REGIONS else "station"
    if kind == "station" and not re.fullmatch(r"[a-z0-9]{4}", workspace):
        raise ApiError(f"invalid station ID {workspace.upper()!r}")
    try:
        data = _get(
            f"{BASE_URL}/{workspace}/ows",
            {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
            # New captures appear every few minutes, so this is kept only as
            # long as the server says (15 seconds).
            ttl=0,
        )
    except ApiError as e:
        if "HTTP 404" in str(e):
            raise ApiError(f"unknown {kind} {workspace.upper()!r} (see --list)") from e
        raise
    layers = parse_capabilities(workspace, data)
    if not layers:
        raise ApiError(f"{kind} {workspace.upper()!r} has no radar layers")
    return layers


def parse_capabilities(workspace: str, data: bytes) -> dict[str, Layer]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ApiError(f"could not parse capabilities: {e}") from e

    prefix = f"{workspace}_"
    layers: dict[str, Layer] = {}
    for el in root.iterfind(".//wms:Layer", _WMS_NS):
        name = el.findtext("wms:Name", "", _WMS_NS)
        if not name.startswith(prefix):
            continue
        times = _parse_times(el.find("wms:Dimension[@name='time']", _WMS_NS))
        bbox_el = el.find("wms:EX_GeographicBoundingBox", _WMS_NS)
        if bbox_el is None:
            continue
        bbox = tuple(
            float(bbox_el.findtext(f"wms:{tag}", "nan", _WMS_NS))
            for tag in (
                "westBoundLongitude",
                "southBoundLatitude",
                "eastBoundLongitude",
                "northBoundLatitude",
            )
        )
        product = name[len(prefix):]
        layers[product] = Layer(
            workspace=workspace,
            product=product,
            # Abstracts end with boilerplate about MRMS; keep the first sentence.
            title=el.findtext("wms:Abstract", "", _WMS_NS).split(". ")[0].strip(),
            style=el.findtext("wms:Style/wms:Name", "", _WMS_NS),
            bbox=bbox,  # type: ignore[arg-type]
            times=times,
        )
    return layers


def _parse_times(dim: ET.Element | None) -> tuple[str, ...]:
    if dim is None or not dim.text:
        return ()
    # Only explicit lists are used here; "start/end/period" ranges are skipped.
    values = [v.strip() for v in dim.text.split(",") if v.strip() and "/" not in v]
    return tuple(sorted(values, key=parse_time))


def default_product(layers: dict[str, Layer]) -> str:
    """Pick base reflectivity: sr_bref for NEXRAD, its equivalent elsewhere."""
    for product in DEFAULT_PRODUCTS:
        if product in layers:
            return product
    for product, layer in sorted(layers.items()):
        if layer.style == "radar_reflectivity":
            return product
    return sorted(layers)[0]


def parse_time(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ApiError(f"unrecognized timestamp {value!r}")


def mercator_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, ...]:
    """Convert a lon/lat bbox to EPSG:3857 (x/y order)."""
    r = 6378137.0

    def project(lon: float, lat: float) -> tuple[float, float]:
        return (
            math.radians(lon) * r,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * r,
        )

    x0, y0 = project(bbox[0], bbox[1])
    x1, y1 = project(bbox[2], bbox[3])
    return (x0, y0, x1, y1)


def fetch_frame(
    layer: Layer,
    time: str,
    width: int,
    height: int,
    bgcolor: str,
    overlays: tuple[str, ...] = (),
    snapshots: dict[str, tuple[str, ...]] | None = None,
) -> bytes:
    """Fetch one PNG capture with borders and overlays drawn in z order.

    The single time parameter applies to every time-enabled layer, and the
    server snaps each one to its nearest snapshot. A capture never changes,
    so it is cached for good, along with the snapshots it was drawn with
    (see snapshot_tag); without snapshots, timed overlays are not cached.
    """
    tag = snapshot_tag(overlays, time, snapshots)
    stack = [(_RADAR, layer.qualified_name, ""), (_BORDERS, "geopolitical", "")]
    stack += [(OVERLAYS[o].z, OVERLAYS[o].layer, OVERLAYS[o].style) for o in overlays]
    stack.sort(key=lambda entry: entry[0])
    return _get_image(
        f"{BASE_URL}/ows",
        {
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetMap",
            "layers": ",".join(name for _, name, _ in stack),
            "styles": ",".join(style for _, _, style in stack),
            "crs": "EPSG:3857",
            "bbox": ",".join(f"{v:.3f}" for v in mercator_bbox(layer.bbox)),
            "width": str(width),
            "height": str(height),
            "format": "image/png",
            "bgcolor": bgcolor,
            "time": time,
        },
        ttl=None if tag is None else math.inf,
        tag=tag or "",
    )


def fetch_legend(layer: Layer) -> bytes:
    return _get_image(
        f"{BASE_URL}/ows",
        {
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetLegendGraphic",
            "format": "image/png",
            "width": "500",
            "height": "30",
            "layer": layer.qualified_name,
        },
        ttl=DAY,
    )


def overlay_times() -> dict[str, tuple[str, ...]]:
    """Snapshot times of the time-enabled overlays, keyed by overlay name."""
    data = _get(
        f"{BASE_URL}/wwa/ows",
        {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
        ttl=0,
    )
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ApiError(f"could not parse capabilities: {e}") from e
    by_layer = {
        el.findtext("wms:Name", "", _WMS_NS): _parse_times(
            el.find("wms:Dimension[@name='time']", _WMS_NS)
        )
        for el in root.iterfind(".//wms:Layer", _WMS_NS)
    }
    return {
        name: by_layer.get(o.layer.split(":")[1], ())
        for name, o in OVERLAYS.items()
        if o.timed
    }


def nearest_time(times: tuple[str, ...], time: str) -> str | None:
    """The snapshot the server draws for time (WMS nearestValue)."""
    if not times:
        return None
    when = parse_time(time)
    return min(times, key=lambda t: abs(parse_time(t) - when))


def snapshot_tag(
    overlays: tuple[str, ...], time: str, snapshots: dict[str, tuple[str, ...]] | None
) -> str | None:
    """The WWA snapshots a frame at time is drawn with, e.g. "warnings=...".

    "" without timed overlays. None if the snapshots are unknown, or a newer
    snapshot could still come along that is nearer to time: until there is
    one at or after time, the server's pick (and so the image) can change.
    """
    tags = []
    for name in overlays:
        if not OVERLAYS[name].timed:
            continue
        times = (snapshots or {}).get(name, ())
        if not times or parse_time(times[-1]) < parse_time(time):
            return None
        tags.append(f"{name}={nearest_time(times, time)}")
    return " ".join(tags)


def fetch_alerts(
    overlays: tuple[str, ...],
    bbox: tuple[float, float, float, float],
    time: str,
    snapshots: dict[str, tuple[str, ...]],
) -> list[Alert]:
    """Alerts drawn by the timed overlays for one frame, most severe first.

    Each WWA snapshot is a full set of polygons tagged with its IDP_FileDate,
    so this picks the snapshot the map shows and the polygons in view. An
    alert covering several zones has several polygons; it is listed once.
    """
    x0, y0, x1, y1 = mercator_bbox(bbox)
    alerts: dict[str, Alert] = {}
    for name in overlays:
        overlay = OVERLAYS[name]
        snapshot = nearest_time(snapshots.get(name, ()), time) if overlay.timed else None
        if snapshot is None:
            continue
        data = _get(
            f"{BASE_URL}/wwa/ows",
            {
                "service": "WFS",
                "version": "1.0.0",
                "request": "GetFeature",
                "typeName": overlay.layer,
                "outputFormat": "application/json",
                "propertyName": "prod_type,sig,wfo,url,cap_id,vtec",
                "CQL_FILTER": (
                    f"IDP_FileDate = '{snapshot}' AND "
                    f"BBOX(geom,{x0:.3f},{y0:.3f},{x1:.3f},{y1:.3f},'EPSG:3857')"
                ),
            },
            # A snapshot never changes once published.
            ttl=math.inf,
            check=_check_features,
        )
        features = json.loads(data)["features"]
        for feature in features:
            alert = _parse_alert(feature.get("properties") or {})
            if alert:
                alerts.setdefault(alert.url, alert)
    return sorted(alerts.values(), key=_alert_rank)


def _alert_rank(alert: Alert) -> tuple:
    short_fuse = _SHORT_FUSE.index(alert.event) if alert.event in _SHORT_FUSE else 99
    return (short_fuse, _SIG_ORDER.get(alert.sig, 9), alert.event, alert.wfo)


def _parse_alert(props: dict) -> Alert | None:
    # Snapshots with no alerts hold a single placeholder row with no product.
    event, url = props.get("prod_type"), props.get("url") or ""
    if not event or not url:
        return None
    # The feature's own time fields are local times marked as UTC, so the end
    # time comes from the VTEC string instead.
    ends = None
    m = _VTEC_END_RE.search(props.get("vtec") or "")
    if m and m.group(1) != "000000T0000":
        ends = datetime.strptime(m.group(1), "%y%m%dT%H%M").replace(tzinfo=timezone.utc)
    return Alert(
        event=event,
        wfo=props.get("wfo") or "",
        sig=props.get("sig") or "",
        url=url,
        ends=ends,
    )


def fetch_alert_text(alert: Alert) -> AlertText:
    """The full message for an alert from api.weather.gov."""
    if not alert.url.startswith(ALERT_URL_PREFIX):
        raise ApiError(f"unexpected alert URL {alert.url!r}")

    def check(data: bytes) -> None:
        try:
            json.loads(data)["properties"]
        except (ValueError, KeyError, TypeError) as e:
            raise ApiError(f"could not parse alert {alert.url}") from e

    # Updates to an alert are new messages with their own URLs.
    props = json.loads(_get(alert.url, ttl=math.inf, check=check))["properties"]
    return AlertText(
        event=props.get("event") or alert.event,
        headline=props.get("headline") or "",
        sender=props.get("senderName") or alert.wfo,
        area=props.get("areaDesc") or "",
        description=(props.get("description") or "").strip(),
        instruction=(props.get("instruction") or "").strip(),
    )
