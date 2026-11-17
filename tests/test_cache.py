import math
import os
import time

import pytest

from radar_sixel import api, cache
from radar_sixel.cache import Cache


def test_round_trip(tmp_path):
    c = Cache(tmp_path)
    assert c.get("k") is None
    c.put("k", b"data", 123.0, etag='"e"', last_modified="Mon")
    entry = c.get("k")
    assert entry == cache.Entry(b"data", 123.0, '"e"', "Mon")
    c.put("forever", b"x")
    assert c.get("forever").fresh_until == math.inf


def test_disabled():
    for c in (Cache(None), Cache(None, 0)):
        c.put("k", b"data")
        assert c.get("k") is None


def test_zero_limit_writes_nothing(tmp_path):
    Cache(tmp_path, 0).put("k", b"data")
    assert list(tmp_path.iterdir()) == []


def test_corrupt_entry_is_a_miss(tmp_path):
    c = Cache(tmp_path)
    c.put("k", b"data")
    path = c._path("k")
    path.write_bytes(path.read_bytes()[:-1])
    assert c.get("k") is None
    path.write_bytes(b"not json\n")
    assert c.get("k") is None


def entry_size(key, data):
    header = {"format": cache.FORMAT, "key": key, "size": len(data), "fresh_until": None,
              "etag": "", "last_modified": ""}
    return len(cache.json.dumps(header)) + 1 + len(data)


def test_oldest_written_are_evicted_first(tmp_path):
    size = entry_size("k0", b"x" * 100)
    c = Cache(tmp_path, 3 * size)
    for i in range(3):
        c.put(f"k{i}", b"x" * 100)
    # Reading doesn't renew an entry, but writing it again does.
    c.get("k0")
    c.put("k1", b"x" * 100)
    c.put("k3", b"x" * 100)
    assert [k for k in ("k0", "k1", "k2", "k3") if c.get(k)] == ["k1", "k2", "k3"]
    c.put("k4", b"x" * 100)
    assert [k for k in ("k1", "k2", "k3", "k4") if c.get(k)] == ["k1", "k3", "k4"]


def test_existing_entries_are_counted_oldest_first(tmp_path):
    size = entry_size("k0", b"x" * 100)
    old = Cache(tmp_path)
    for i in range(3):
        old.put(f"k{i}", b"x" * 100)
        # Written in order k2, k1, k0 as far as the next run can tell.
        os.utime(old._path(f"k{i}"), (time.time() - i * 10,) * 2)
    c = Cache(tmp_path, 3 * size)
    c.put("k3", b"x" * 100)
    assert [k for k in ("k0", "k1", "k2", "k3") if c.get(k)] == ["k0", "k1", "k3"]


def test_only_entry_files_are_ever_deleted(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"x" * 1000)
    (tmp_path / "ab").mkdir()
    (tmp_path / "ab" / "photo.jpg").write_bytes(b"x" * 1000)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / ("a" * 64)).write_bytes(b"x" * 1000)
    c = Cache(tmp_path, 1)
    c.put("k", b"x" * 100)
    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / "ab" / "photo.jpg").exists()
    assert (tmp_path / "docs" / ("a" * 64)).exists()
    # Too big for the limit on its own, so it goes too.
    assert c.get("k") is None


def test_leftover_temp_files_are_removed(tmp_path):
    c = Cache(tmp_path)
    c.put("k", b"data")
    shard = c._path("k").parent
    temp = shard / f".{'b' * 64}.1.2.tmp"
    temp.write_bytes(b"partial")
    fresh = shard / f".{'c' * 64}.1.2.tmp"
    fresh.write_bytes(b"partial")
    os.utime(temp, (time.time() - 2 * cache.STALE_TEMP,) * 2)
    Cache(tmp_path).put("k2", b"data")
    assert not temp.exists()
    assert fresh.exists()


def test_default_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cache.default_dir() == tmp_path / "radar-sixel"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for value in ("", "relative/path"):
        monkeypatch.setenv("XDG_CACHE_HOME", value)
        assert cache.default_dir() == tmp_path / "home" / ".cache" / "radar-sixel"


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Scripted responses for api._request; records the request headers."""
    monkeypatch.setattr(api, "cache", Cache(tmp_path))
    requests = []
    responses = []

    def request(url, headers):
        requests.append((url, headers))
        return responses.pop(0)

    monkeypatch.setattr(api, "_request", request)
    return requests, responses


def test_uncached_without_ttl(server):
    requests, responses = server
    responses += [(FakeResponse(200), b"a"), (FakeResponse(200), b"b")]
    assert api._get("https://h/x") == b"a"
    assert api._get("https://h/x") == b"b"
    assert len(requests) == 2


def test_server_max_age_wins_over_ttl(server, monkeypatch):
    requests, responses = server
    now = [1000.0]
    monkeypatch.setattr(api._time, "time", lambda: now[0])
    headers = {"Cache-Control": "must-revalidate, max-age=15"}
    responses += [(FakeResponse(200, headers), b"a"), (FakeResponse(200, headers), b"b")]
    assert api._get("https://h/caps", ttl=0) == b"a"
    now[0] += 10
    assert api._get("https://h/caps", ttl=0) == b"a"
    now[0] += 10
    assert api._get("https://h/caps", ttl=0) == b"b"
    assert len(requests) == 2


def test_stale_entry_is_revalidated(server, monkeypatch):
    requests, responses = server
    now = [1000.0]
    monkeypatch.setattr(api._time, "time", lambda: now[0])
    lm = "Wed, 19 Aug 2026 19:31:46 GMT"
    responses += [
        (FakeResponse(200, {"Last-Modified": lm, "ETag": '"v1"'}), b"page"),
        (FakeResponse(304), b""),
        (FakeResponse(200, {"ETag": '"v2"'}), b"new page"),
    ]
    assert api._get("https://h/index.html", ttl=api.DAY) == b"page"
    now[0] += api.DAY + 1
    assert api._get("https://h/index.html", ttl=api.DAY) == b"page"
    assert requests[1][1]["If-None-Match"] == '"v1"'
    assert requests[1][1]["If-Modified-Since"] == lm
    # The 304 made it fresh for another day.
    now[0] += api.DAY - 1
    assert api._get("https://h/index.html", ttl=api.DAY) == b"page"
    now[0] += 2
    assert api._get("https://h/index.html", ttl=api.DAY) == b"new page"
    assert requests[2][1]["If-None-Match"] == '"v1"'


def test_immutable_entries_ignore_max_age(server, monkeypatch):
    requests, responses = server
    monkeypatch.setattr(api._time, "time", lambda: 1000.0)
    responses.append((FakeResponse(200, {"Cache-Control": "max-age=0"}), b"\x89PNG frame"))
    for _ in range(2):
        assert api._get_image("https://h/map", {"time": "T"}, ttl=math.inf) == b"\x89PNG frame"
    assert len(requests) == 1


def test_errors_are_not_cached(server):
    requests, responses = server
    responses += [
        (FakeResponse(200), b"<ServiceExceptionReport>"),
        (FakeResponse(500), b"oops"),
        (FakeResponse(200), b"\x89PNG ok"),
    ]
    with pytest.raises(api.ApiError, match="server returned an error"):
        api._get_image("https://h/map", {}, ttl=math.inf)
    with pytest.raises(api.ApiError, match="HTTP 500"):
        api._get_image("https://h/map", {}, ttl=math.inf)
    assert api._get_image("https://h/map", {}, ttl=math.inf) == b"\x89PNG ok"
    assert api._get_image("https://h/map", {}, ttl=math.inf) == b"\x89PNG ok"
    assert len(requests) == 3


def test_tag_is_part_of_the_key(server):
    requests, responses = server
    responses += [(FakeResponse(200), b"a"), (FakeResponse(200), b"b")]
    assert api._get("https://h/x", ttl=math.inf, tag="warnings=1") == b"a"
    assert api._get("https://h/x", ttl=math.inf, tag="warnings=2") == b"b"
    assert api._get("https://h/x", ttl=math.inf, tag="warnings=1") == b"a"
    assert len(requests) == 2


def test_snapshot_tag():
    snaps = {"hazards": ("2026-11-15T00:00:00Z", "2026-11-15T00:10:00Z"),
             "warnings": ("2026-11-15T00:04:00Z",)}
    t = "2026-11-15T00:03:00.000Z"
    assert api.snapshot_tag(("counties",), t, None) == ""
    assert api.snapshot_tag(("counties", "hazards"), t, None) is None
    assert api.snapshot_tag(("hazards", "warnings"), t, snaps) == (
        "hazards=2026-11-15T00:00:00Z warnings=2026-11-15T00:04:00Z"
    )
    # Newer than every warnings snapshot: a nearer one may still come.
    assert api.snapshot_tag(("warnings",), "2026-11-15T00:05:00.000Z", snaps) is None
    assert api.snapshot_tag(("hazards",), t, {"hazards": ()}) is None


def test_frames_with_unsettled_snapshots_are_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_get_image", lambda url, params, **k: calls.append(k) or b"")
    layer = api.Layer("kabx", "sr_bref", "", "", (-111.8, 30.1, -101.8, 40.1), ())
    t = "2026-11-15T00:03:00.000Z"
    api.fetch_frame(layer, t, 10, 10, "0x000000", ("counties",))
    api.fetch_frame(layer, t, 10, 10, "0x000000", ("warnings",))
    api.fetch_frame(layer, t, 10, 10, "0x000000", ("warnings",),
                    snapshots={"warnings": ("2026-11-15T00:04:00Z",)})
    assert calls == [
        {"ttl": math.inf, "tag": ""},
        {"ttl": None, "tag": ""},
        {"ttl": math.inf, "tag": "warnings=2026-11-15T00:04:00Z"},
    ]
