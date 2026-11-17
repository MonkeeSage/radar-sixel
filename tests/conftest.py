import pytest

from radar_sixel import api
from radar_sixel.cache import Cache


@pytest.fixture(autouse=True)
def no_cache(monkeypatch, tmp_path):
    """Keep tests away from the user's cache, which cli.main() points at."""
    monkeypatch.setattr(api, "cache", Cache(None))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
