"""On-disk cache of server responses and rendered frames, evicted oldest first."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMIT_MB = 250
# Bump when the entry format changes; old entries are then ignored and
# evicted like any others.
FORMAT = "radar-sixel-cache-1"
# Temporary files older than this are left over from a crash.
STALE_TEMP = 3600
# Only files named like entries are ever counted or deleted, so pointing the
# cache at a directory with other files in it does no harm.
_SHARD_RE = re.compile(r"[0-9a-f]{2}")
_ENTRY_RE = re.compile(r"[0-9a-f]{64}")
_TEMP_RE = re.compile(r"\.[0-9a-f]{64}\.\d+\.\d+\.tmp")


def default_dir() -> Path:
    """$XDG_CACHE_HOME/radar-sixel, or ~/.cache/radar-sixel."""
    base = os.environ.get("XDG_CACHE_HOME", "")
    # The XDG spec says relative paths are to be ignored.
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "radar-sixel"


@dataclass(frozen=True)
class Entry:
    data: bytes
    # time.time() until which the entry can be used without asking the
    # server; math.inf for content that never changes.
    fresh_until: float
    # Validators for a conditional request once the entry is stale.
    etag: str = ""
    last_modified: str = ""


class Cache:
    """Entries keyed by an arbitrary string, one file each.

    When the files add up to more than the limit, the oldest written are
    deleted first (first in, first out). Reading an entry does not renew it.
    All errors are ignored: the cache only ever saves requests.
    """

    def __init__(self, root: Path | None, limit: int = DEFAULT_LIMIT_MB * 1024 * 1024) -> None:
        # A root of None (or a limit of 0) turns the cache off.
        self.root = root if limit > 0 else None
        self.limit = limit
        self._lock = threading.Lock()
        # Entry paths, oldest first, with their sizes; read from disk on the
        # first write.
        self._files: OrderedDict[Path, int] | None = None
        self._total = 0

    def _path(self, key: str) -> Path:
        assert self.root is not None
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / digest[:2] / digest

    def get(self, key: str) -> Entry | None:
        if self.root is None:
            return None
        try:
            with open(self._path(key), "rb") as f:
                header = json.loads(f.readline())
                if header.get("format") != FORMAT or header.get("key") != key:
                    return None
                data = f.read()
        except (OSError, ValueError):
            return None
        if len(data) != header.get("size"):
            return None
        fresh_until = header.get("fresh_until")
        return Entry(
            data=data,
            fresh_until=math.inf if fresh_until is None else float(fresh_until),
            etag=header.get("etag", ""),
            last_modified=header.get("last_modified", ""),
        )

    def put(
        self,
        key: str,
        data: bytes,
        fresh_until: float = math.inf,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        if self.root is None:
            return
        header = {
            "format": FORMAT,
            "key": key,
            "size": len(data),
            # JSON has no infinity.
            "fresh_until": None if math.isinf(fresh_until) else fresh_until,
            "etag": etag,
            "last_modified": last_modified,
        }
        path = self._path(key)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "wb") as f:
                f.write(json.dumps(header).encode() + b"\n")
                f.write(data)
            # Readers see the old entry or the new one, never half of one.
            os.replace(temp, path)
            size = path.stat().st_size
        except OSError:
            temp.unlink(missing_ok=True)
            return
        self._added(path, size)

    def _added(self, path: Path, size: int) -> None:
        with self._lock:
            if self._files is None:
                self._scan()
                assert self._files is not None
            self._total -= self._files.pop(path, 0)
            self._files[path] = size
            self._total += size
            while self._total > self.limit and self._files:
                old, old_size = self._files.popitem(last=False)
                self._total -= old_size
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass

    def _scan(self) -> None:
        """Index the entries on disk, oldest first."""
        assert self.root is not None
        found = []
        now = time.time()
        try:
            shards = [
                d
                for d in os.scandir(self.root)
                if _SHARD_RE.fullmatch(d.name) and d.is_dir(follow_symlinks=False)
            ]
        except OSError:
            shards = []
        for shard in shards:
            try:
                entries = list(os.scandir(shard.path))
            except OSError:
                continue
            for entry in entries:
                temp = _TEMP_RE.fullmatch(entry.name)
                if not (temp or _ENTRY_RE.fullmatch(entry.name)):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    if temp:
                        if now - st.st_mtime > STALE_TEMP:
                            os.unlink(entry.path)
                        continue
                except OSError:
                    continue
                found.append((st.st_mtime, Path(entry.path), st.st_size))
        found.sort(key=lambda f: f[0])
        self._files = OrderedDict((path, size) for _, path, size in found)
        self._total = sum(self._files.values())
