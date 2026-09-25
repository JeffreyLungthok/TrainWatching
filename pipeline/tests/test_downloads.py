#!/usr/bin/env python3
"""Offline checks of the download logic (CKAN lookup, Overpass tiling/retries) with mocked HTTP."""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_data as bd  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_timetable_year():
    print("Timetable year")
    check(bd.timetable_year(dt.date(2026, 9, 25)) == 2026, "25 Sep 2026 -> 2026")
    check(bd.timetable_year(dt.date(2026, 12, 12)) == 2026, "12 Dec 2026 (Saturday before the change) -> 2026")
    check(bd.timetable_year(dt.date(2026, 12, 13)) == 2027, "13 Dec 2026 (second Sunday of December) -> 2027")
    check(bd.timetable_year(dt.date(2025, 12, 14)) == 2026, "14 Dec 2025 -> 2026")


def test_ckan():
    print("CKAN lookup")
    resources = [
        {"name": "GTFS_FP2026_2025-07-03.zip", "url": "https://x/download/GTFS_FP2026_2025-07-03.zip", "created": "2025-07-03T04:00"},
        {"name": "GTFS_FP2026_20260923.zip", "url": "https://x/download/GTFS_FP2026_20260923.zip", "created": "2026-09-23T04:00"},
        {"name": "GTFS_FP2026_20260919.zip", "url": "https://x/download/GTFS_FP2026_20260919.zip", "created": "2026-09-19T04:00"},
        {"name": "Readme", "url": "https://x/readme.pdf", "format": "PDF"},
    ]
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "timetable-2026" in url:
            return FakeResponse(200, {"result": {"resources": resources}})
        return FakeResponse(404, None)
    with mock.patch.object(bd.requests, "get", fake_get):
        url, name = bd.latest_gtfs_url(dt.date(2026, 9, 25))
    check(name == "GTFS_FP2026_20260923.zip", f"newest zip is picked ({name})")
    check("timetable-2026-gtfs2020" in calls[0], "asks for the current timetable year first")


def test_overpass_tiles():
    print("Overpass tiling, retries and merging")
    data = json.load(open(HERE / "out" / "osm_synthetic.json"))
    nodes = {e["id"]: e for e in data["elements"] if e["type"] == "node"}
    ways = [e for e in data["elements"] if e["type"] == "way"]
    calls = {"n": 0}

    def fake_post(url, data=None, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            return FakeResponse(504)  # first two attempts fail
        q = data["data"]
        bbox = q.split("(")[-1].split(")")[0]
        s, w, n, e = map(float, bbox.split(","))
        sel = [wy for wy in ways if any(s <= nodes[i]["lat"] <= n and w <= nodes[i]["lon"] <= e for i in wy["nodes"])]
        sel = [wy for wy in sel if bd.keep_way(wy["tags"])]  # the query filters server side
        used = {i for wy in sel for i in wy["nodes"]}
        return FakeResponse(200, {"elements": sel + [nodes[i] for i in sorted(used)]})

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(bd.requests, "post", fake_post), mock.patch.object(bd.time, "sleep", lambda s: None):
        args = SimpleNamespace(osm_pbf=None, osm_json=None, bbox=bd.DEFAULT_BBOX)
        ids, lon, lat, wys, _ = bd.load_osm(args, Path(tmp))
        n_calls = calls["n"]
        cached = sorted(p.name for p in (Path(tmp) / "osm").iterdir())
        ids2, _, _, wys2, _ = bd.load_osm(args, Path(tmp))  # second run: served from cache
    ref_ids, _, _, ref_ways, _ = bd.ways_from_overpass([data])
    check(n_calls == 8, f"6 tiles, 2 retried after HTTP 504 ({n_calls} requests)")
    check(len(cached) == 6, "each tile is cached")
    check(calls["n"] == n_calls, "second run uses the cache only")
    check(len(wys) == len(ref_ways), f"merged ways match the single-file input ({len(wys)} vs {len(ref_ways)})")
    used_ref = np.unique(np.concatenate(ref_ways))
    check(np.array_equal(np.intersect1d(ids, used_ref), used_ref), "all nodes of all ways present after merging")


if __name__ == "__main__":
    test_timetable_year()
    test_ckan()
    test_overpass_tiles()
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)
