#!/usr/bin/env python3
"""
Swiss train map - data builder
==============================

Downloads the official Swiss GTFS timetable (opentransportdata.swiss) and the
OpenStreetMap rail network, routes every scheduled train along the real OSM
tracks and writes two compact JSON files for the web viewer:

    web/data/network.json    stops + track geometry of every stop-to-stop leg
    web/data/timetable.json  patterns, timings, service calendars and trips

Usage
-----
    python build_data.py                         # download everything, 60 days from today
    python build_data.py --days 14               # smaller output
    python build_data.py --gtfs GTFS_FP2026_20260923.zip --osm-pbf switzerland-latest.osm.pbf

Everything that is downloaded is cached in pipeline/cache/, so re-runs are fast.
Delete the cache folder to force fresh downloads.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import json
import math
import re
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "web" / "data"
DEFAULT_CACHE = HERE / "cache"

# Switzerland plus a margin so that border stations (Konstanz, Singen, Mulhouse,
# Domodossola, Como, Feldkirch ...) still get real track geometry.
DEFAULT_BBOX = (45.70, 5.80, 48.00, 10.70)  # south, west, north, east

# Local equirectangular projection (metres). Accurate to well below 1 % inside Switzerland,
# which is plenty for snapping and routing.
LAT0, LON0 = 46.8, 8.2
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))

RAIL_VALUES = ("rail", "narrow_gauge", "light_rail", "tram")
EXCLUDED_SERVICE = ("yard",)

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
CKAN_PACKAGE = "https://data.opentransportdata.swiss/api/3/action/package_show?id=timetable-{year}-gtfs2020"
DATASET_PAGES = ("https://data.opentransportdata.swiss/en/dataset/timetable-{year}-gtfs2020",
                 "https://data.opentransportdata.swiss/dataset/timetable-{year}-gtfs2020")
PERMALINK = "https://data.opentransportdata.swiss/dataset/timetable-{year}-gtfs2020/permalink"
# Daily copy of the official feed (trains only) published by geOps - used if opentransportdata.swiss
# refuses the request, which happens from some cloud machines such as GitHub's.
GEOPS_TRAINS = "https://gtfs.geops.ch/dl/gtfs_train.zip"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; swiss-train-map/1.1; timetable visualisation)",
                "Accept": "*/*"}

# Routing parameters
SNAP_RADIUS = 300.0        # m, candidate tracks around a stop
SNAP_FAR = 2000.0          # m, fallback radius when nothing is within SNAP_RADIUS
SNAP_PENALTY = 1.5         # cost per metre of snapping distance
MAX_TURN_DEG = 90.0        # trains cannot turn sharper than this between two track pieces
SIMPLIFY_M = 4.0           # Douglas-Peucker tolerance for the output geometry
TRAM_COST = 3.0            # tram tracks count 3x their length: used only where no railway fits


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def project(lon, lat):
    return (np.asarray(lon, float) - LON0) * M_PER_DEG_LON, (np.asarray(lat, float) - LAT0) * M_PER_DEG_LAT


def unproject(x, y):
    return np.asarray(x, float) / M_PER_DEG_LON + LON0, np.asarray(y, float) / M_PER_DEG_LAT + LAT0


def today_in_switzerland() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Zurich")).date()
    except Exception:  # pragma: no cover - missing tz database
        return (dt.datetime.utcnow() + dt.timedelta(hours=1)).date()


# --------------------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------------------

def download(url: str, dest: Path, what: str) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        log(f"Using cached {what}: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    log(f"Downloading {what} from {url}")
    with requests.get(url, stream=True, timeout=180, headers=HTTP_HEADERS, allow_redirects=True) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} for {r.url}")
        total = int(r.headers.get("content-length") or 0)
        done, last = 0, 0.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 5:
                    pct = f" ({done / total:.0%})" if total else ""
                    log(f"  {done / 1e6:,.0f} MB{pct}")
                    last = time.time()
    if dest.suffix == ".zip" and not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the server did not return a zip file")
    tmp.replace(dest)
    log(f"  done: {dest.stat().st_size / 1e6:,.0f} MB")
    return dest


def timetable_year(day: dt.date) -> int:
    """Swiss timetable years start on the second Sunday of December."""
    dec1 = dt.date(day.year, 12, 1)
    first_sunday = dec1 + dt.timedelta(days=(6 - dec1.weekday()) % 7)
    change = first_sunday + dt.timedelta(days=7)
    return day.year + 1 if day >= change else day.year


def _date_key(text: str) -> str:
    """YYYYMMDD found in a file name like GTFS_FP2026_20260923.zip or GTFS_FP2026_2025-07-03.zip."""
    digits = re.findall(r"(20\d{2})-?(\d{2})-?(\d{2})", text or "")
    return "".join(digits[-1]) if digits else ""


def _official_via_api(year: int):
    r = requests.get(CKAN_PACKAGE.format(year=year), timeout=60, headers=HTTP_HEADERS)
    r.raise_for_status()
    zips = [x for x in r.json()["result"]["resources"]
            if str(x.get("url", "")).lower().split("?")[0].endswith(".zip") or str(x.get("format", "")).lower() == "zip"]
    if not zips:
        return None
    zips.sort(key=lambda x: _date_key(str(x.get("name") or x.get("url"))) or str(x.get("created") or ""), reverse=True)
    return zips[0]["url"]


def _official_via_page(year: int):
    """Read the download links from the dataset's web page (the way a person would)."""
    from urllib.parse import urljoin
    for page in DATASET_PAGES:
        url = page.format(year=year)
        r = requests.get(url, timeout=60, headers=HTTP_HEADERS)
        r.raise_for_status()
        links = {urljoin(url, h) for h in re.findall(r'href="([^"]+/download/[^"]+\.zip)"', r.text, flags=re.I)}
        links = [u for u in links if _date_key(Path(u).name)]
        if links:
            return max(links, key=lambda u: _date_key(Path(u).name))
    return None


def gtfs_sources(day: dt.date):
    """Yields (url, cache file name, description), best source first."""
    year = timetable_year(day)
    stamp = day.strftime("%Y%m%d")
    for finder, how in ((_official_via_api, "catalogue API"), (_official_via_page, "dataset page")):
        for y in (year, year - 1):
            try:
                url = finder(y)
            except Exception as exc:
                log(f"  timetable {y} via {how}: {exc}")
                continue
            if url:
                yield url, Path(url.split("?")[0]).name, f"official Swiss GTFS (timetable {y}, found via {how})"
                break
    yield PERMALINK.format(year=year), f"GTFS_FP{year}_permalink_{stamp}.zip", f"official Swiss GTFS (timetable {year}, permalink)"
    yield GEOPS_TRAINS, f"geops_gtfs_train_{stamp}.zip", "geOps mirror of the official feed (trains only)"


def fetch_gtfs(cache: Path, day: dt.date) -> Path:
    for url, name, what in gtfs_sources(day):
        try:
            return download(url, cache / name, what)
        except Exception as exc:
            log(f"  {what}: {exc}")
    raise SystemExit("Could not download the Swiss GTFS timetable from any source. Download a GTFS zip yourself "
                     "and pass --gtfs path/or/url (in GitHub Actions: set the repository variable GTFS_URL).")


# --------------------------------------------------------------------------------------
# GTFS
# --------------------------------------------------------------------------------------

class Gtfs:
    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        self.names = {Path(n).name: n for n in self.zip.namelist() if not n.endswith("/")}

    def has(self, name: str) -> bool:
        return name in self.names

    def read(self, name: str, columns=None, chunksize=None):
        wanted = None if columns is None else set(columns)
        f = self.zip.open(self.names[name])
        reader = pd.read_csv(
            f, dtype=str, keep_default_na=False, encoding="utf-8-sig", chunksize=chunksize,
            usecols=(lambda c: c.strip() in wanted) if wanted else None,
        )
        if chunksize:
            return reader
        reader.columns = [c.strip() for c in reader.columns]
        return reader


def hms_to_seconds(s: pd.Series) -> np.ndarray:
    s = s.astype(str).str.strip()
    out = np.full(len(s), np.nan)
    has = (s.str.len() > 0).to_numpy()
    if has.any():
        parts = s[has].str.split(":", expand=True).astype(float)
        out[has] = parts[0].to_numpy() * 3600 + parts[1].to_numpy() * 60 + parts[2].to_numpy()
    return out


def parse_route_types(spec: str) -> set[int]:
    types: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            types.update(range(int(a), int(b) + 1))
        elif part:
            types.add(int(part))
    return types


def service_calendar(g: Gtfs, service_ids: set[str], start: dt.date, ndays: int) -> dict[str, np.ndarray]:
    """service_id -> boolean array (one entry per day of the output window)."""
    days = [start + dt.timedelta(days=i) for i in range(ndays)]
    weekday = np.array([d.weekday() for d in days])
    ymd = np.array([int(d.strftime("%Y%m%d")) for d in days])
    active: dict[str, np.ndarray] = {}
    if g.has("calendar.txt"):
        cal = g.read("calendar.txt")
        cal = cal[cal["service_id"].isin(service_ids)]
        wd_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        flags = np.stack([(cal[c].str.strip() == "1").to_numpy() for c in wd_cols], axis=1)
        s_dates = pd.to_numeric(cal["start_date"].str.strip(), errors="coerce").fillna(0).to_numpy()
        e_dates = pd.to_numeric(cal["end_date"].str.strip(), errors="coerce").fillna(0).to_numpy()
        for sid, fl, sd, ed in zip(cal["service_id"].to_numpy(), flags, s_dates, e_dates):
            active[sid] = fl[weekday] & (ymd >= sd) & (ymd <= ed)
    if g.has("calendar_dates.txt"):
        for cd in g.read("calendar_dates.txt", chunksize=1_000_000):
            cd.columns = [c.strip() for c in cd.columns]
            cd = cd[cd["service_id"].isin(service_ids)]
            if cd.empty:
                continue
            d = pd.to_datetime(cd["date"].str.strip(), format="%Y%m%d", errors="coerce")
            idx = (d - pd.Timestamp(start)).dt.days
            ok = idx.notna() & (idx >= 0) & (idx < ndays)
            for sid, i, et in zip(cd["service_id"][ok].to_numpy(), idx[ok].astype(int).to_numpy(),
                                  cd["exception_type"][ok].str.strip().to_numpy()):
                arr = active.get(sid)
                if arr is None:
                    arr = active[sid] = np.zeros(ndays, bool)
                arr[i] = et == "1"
    return active


def feed_date_range(g: Gtfs) -> tuple[dt.date | None, dt.date | None]:
    def to_date(v):
        try:
            return dt.datetime.strptime(str(v).strip(), "%Y%m%d").date()
        except ValueError:
            return None
    if g.has("feed_info.txt"):
        fi = g.read("feed_info.txt")
        if len(fi) and "feed_start_date" in fi and "feed_end_date" in fi:
            a, b = to_date(fi["feed_start_date"].iloc[0]), to_date(fi["feed_end_date"].iloc[0])
            if a and b:
                return a, b
    if g.has("calendar.txt"):
        cal = g.read("calendar.txt", columns=["start_date", "end_date"])
        if len(cal):
            return to_date(cal["start_date"].min()), to_date(cal["end_date"].max())
    return None, None


def category_for(route_desc: str, route_type: int, short_name: str = "") -> str:
    d = route_desc.strip().upper()
    if d:
        return d
    m = re.match(r"\s*([A-Za-z]{1,4})(?=[\s\d]|$)", short_name or "")
    if m:  # "IC 5", "S3", "IR15", "RE" -> category from the line name
        return m.group(1).upper()
    return {101: "HS", 102: "IC", 103: "IR", 105: "EN", 106: "R", 107: "PE", 109: "S", 116: "R"}.get(route_type, "R")


def load_timetable(gtfs_path: Path, start: dt.date | None, days: int, route_types: set[int]):
    g = Gtfs(gtfs_path)
    feed_start, feed_end = feed_date_range(g)
    log(f"Feed validity: {feed_start} .. {feed_end}")

    # Output window: yesterday (for trips running past midnight) + `days` days.
    if start is None:
        start = today_in_switzerland() - dt.timedelta(days=1)
    if feed_start and start < feed_start:
        start = feed_start
    end = start + dt.timedelta(days=days)
    if feed_end and end > feed_end:
        end = feed_end
    ndays = (end - start).days + 1
    if ndays <= 0:
        raise SystemExit(f"The feed ({feed_start}..{feed_end}) does not cover the requested dates.")
    log(f"Output window: {start} .. {end} ({ndays} days)")

    routes = g.read("routes.txt")
    for col in ("route_short_name", "route_long_name", "route_desc", "agency_id"):
        if col not in routes:
            routes[col] = ""
    rtype = pd.to_numeric(routes["route_type"].str.strip(), errors="coerce").fillna(-1).astype(int).to_numpy()
    rail = np.isin(rtype, sorted(route_types))
    routes = routes[rail].reset_index(drop=True)
    routes["rtype"] = rtype[rail]
    log(f"Rail routes: {len(routes):,}")

    trips = g.read("trips.txt", columns=["route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name"])
    for col in ("trip_headsign", "trip_short_name"):
        if col not in trips:
            trips[col] = ""
    trips = trips[trips["route_id"].isin(routes["route_id"])]

    active = service_calendar(g, set(trips["service_id"]), start, ndays)
    active_ids = {sid for sid, arr in active.items() if arr.any()}
    trips = trips[trips["service_id"].isin(active_ids)].reset_index(drop=True)
    log(f"Rail trips running in the window: {len(trips):,}")
    if trips.empty:
        raise SystemExit("No rail trips found in the output window.")

    # ---- stop_times (the big one), streamed in chunks ----------------------------------
    trip_index = pd.Index(trips["trip_id"])
    parts, rows = [], 0
    for chunk in g.read("stop_times.txt", chunksize=2_000_000,
                        columns=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]):
        chunk.columns = [c.strip() for c in chunk.columns]
        rows += len(chunk)
        parts.append(chunk[chunk["trip_id"].isin(trip_index)])
        log(f"  stop_times: {rows:,} rows scanned")
    st = pd.concat(parts, ignore_index=True)
    del parts
    log(f"Rail stop_times: {len(st):,}")

    # ---- stops ------------------------------------------------------------------------------
    stops = g.read("stops.txt")
    for col in ("parent_station",):
        if col not in stops:
            stops[col] = ""
    stops["lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
    by_id = stops.set_index("stop_id")
    # fill missing coordinates / names from the parent station
    missing = stops["lat"].isna() | stops["lon"].isna() | ((stops["lat"].abs() < 1e-6) & (stops["lon"].abs() < 1e-6))
    for i in np.flatnonzero(missing.to_numpy()):
        parent = stops.at[i, "parent_station"]
        if parent in by_id.index:
            stops.at[i, "lat"] = by_id.at[parent, "lat"]
            stops.at[i, "lon"] = by_id.at[parent, "lon"]
    empty_name = stops["stop_name"].str.strip() == ""
    for i in np.flatnonzero(empty_name.to_numpy()):
        parent = stops.at[i, "parent_station"]
        if parent in by_id.index:
            stops.at[i, "stop_name"] = by_id.at[parent, "stop_name"]
    valid = stops["lat"].between(-90, 90) & stops["lon"].between(-180, 180) & \
        ~((stops["lat"].abs() < 1e-6) & (stops["lon"].abs() < 1e-6))
    stops = stops[valid]
    stops = stops[stops["stop_id"].isin(set(st["stop_id"]))].reset_index(drop=True)

    # Stops are identified by their (rounded) coordinate: platform-level stops that share the
    # station coordinate collapse into one, while stops with precise platform coordinates stay apart.
    qlon = np.round(stops["lon"].to_numpy() * 1e5).astype(np.int64)
    qlat = np.round(stops["lat"].to_numpy() * 1e5).astype(np.int64)
    keys = qlon * 100_000_000 + qlat
    ukeys, skey_of_stop = np.unique(keys, return_inverse=True)
    stop_lon = np.zeros(len(ukeys)); stop_lat = np.zeros(len(ukeys)); stop_name = [""] * len(ukeys)
    for i, k in enumerate(skey_of_stop):
        if not stop_name[k]:
            stop_name[k] = stops.at[i, "stop_name"].strip()
            stop_lon[k] = qlon[i] / 1e5
            stop_lat[k] = qlat[i] / 1e5
    skey_map = dict(zip(stops["stop_id"], skey_of_stop))
    log(f"Rail stops: {len(stops):,} stop ids -> {len(ukeys):,} distinct locations")

    st["skey"] = st["stop_id"].map(skey_map)
    st = st[st["skey"].notna()]
    st["seq"] = pd.to_numeric(st["stop_sequence"], errors="coerce")
    st = st.sort_values(["trip_id", "seq"], kind="stable").reset_index(drop=True)
    arr_s = hms_to_seconds(st["arrival_time"])
    dep_s = hms_to_seconds(st["departure_time"])
    arr_s = np.where(np.isnan(arr_s), dep_s, arr_s)
    dep_s = np.where(np.isnan(dep_s), arr_s, dep_s)
    skeys = st["skey"].to_numpy().astype(np.int64)
    tid = st["trip_id"].to_numpy()
    bounds = np.flatnonzero(tid[1:] != tid[:-1]) + 1
    starts = np.r_[0, bounds]
    ends = np.r_[bounds, len(st)]

    # ---- frequencies (template trips repeated every n seconds) ---------------------------
    freq: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    if g.has("frequencies.txt"):
        fr = g.read("frequencies.txt")
        fr = fr[fr["trip_id"].isin(trip_index)]
        fs, fe = hms_to_seconds(fr["start_time"]), hms_to_seconds(fr["end_time"])
        hw = pd.to_numeric(fr["headway_secs"], errors="coerce").to_numpy()
        for t, a, b, h in zip(fr["trip_id"].to_numpy(), fs, fe, hw):
            if np.isfinite(a) and np.isfinite(b) and np.isfinite(h) and h > 0:
                freq[t].append((int(a), int(b), int(h)))
        log(f"Frequency-based rail trips: {len(freq):,}")

    # ---- routes, headsigns ---------------------------------------------------------------
    route_idx = {rid: i for i, rid in enumerate(routes["route_id"])}
    route_out = []
    for r in routes.itertuples(index=False):
        short = str(r.route_short_name).strip()
        desc = str(r.route_desc).strip()
        label = short or desc or str(r.route_long_name).strip() or "Train"
        if short and desc and short.isdigit():
            label = f"{desc}{short}"
        route_out.append([label, category_for(desc, int(r.rtype), short), int(r.rtype)])
    trip_info = dict(zip(trips["trip_id"], zip(trips["route_id"], trips["service_id"],
                                               trips["trip_headsign"], trips["trip_short_name"])))

    # ---- service bitsets ----------------------------------------------------------------
    service_idx: dict[bytes, int] = {}
    service_out: list[str] = []
    sid_to_idx: dict[str, int] = {}

    def service_index(sid: str) -> int:
        if sid in sid_to_idx:
            return sid_to_idx[sid]
        bits = np.packbits(active[sid].astype(np.uint8), bitorder="little").tobytes()
        if bits not in service_idx:
            service_idx[bits] = len(service_out)
            service_out.append(base64.b64encode(bits).decode("ascii"))
        sid_to_idx[sid] = service_idx[bits]
        return sid_to_idx[sid]

    # ---- patterns, timings, trips --------------------------------------------------------
    pattern_idx: dict[tuple, int] = {}
    patterns: list[tuple] = []
    timing_idx: dict[tuple, int] = {}
    timings: list[list[int]] = []
    headsign_idx: dict[str, int] = {}
    headsigns: list[str] = []
    out_trips = []  # (start, timing, service, route, headsign, number)
    skipped = 0
    for a, b in zip(starts, ends):
        trip_id = tid[a]
        k = skeys[a:b]
        ar = arr_s[a:b].copy()
        de = dep_s[a:b].copy()
        # interpolate completely missing times
        bad = np.isnan(ar)
        if bad.all():
            skipped += 1
            continue
        if bad.any():
            idx = np.arange(len(ar))
            ar[bad] = np.interp(idx[bad], idx[~bad], ar[~bad])
            de[bad] = ar[bad]
        # merge consecutive visits of the same location
        keep = np.r_[True, k[1:] != k[:-1]]
        if not keep.all():
            last_of_grp = np.r_[np.flatnonzero(keep)[1:] - 1, len(k) - 1]
            de_m = de[last_of_grp]
            k, ar, de = k[keep], ar[keep], de_m
        if len(k) < 2:
            skipped += 1
            continue
        times = np.empty(2 * len(k))
        times[0::2], times[1::2] = ar, de
        times = np.maximum.accumulate(times)
        t0 = times[1]
        rel = np.round(times - t0).astype(np.int64)

        pkey = tuple(k.tolist())
        p = pattern_idx.get(pkey)
        if p is None:
            p = pattern_idx[pkey] = len(patterns)
            patterns.append(pkey)
        tkey = (p, tuple(rel.tolist()))
        ti = timing_idx.get(tkey)
        if ti is None:
            ti = timing_idx[tkey] = len(timings)
            deltas = np.diff(np.r_[0, rel]).tolist()  # first entry = arrival offset at the first stop
            timings.append([p] + [int(x) for x in deltas])

        route_id, service_id, headsign, number = trip_info[trip_id]
        si = service_index(service_id)
        ri = route_idx[route_id]
        hs = str(headsign).strip() or stop_name[k[-1]]
        hi = headsign_idx.get(hs)
        if hi is None:
            hi = headsign_idx[hs] = len(headsigns)
            headsigns.append(hs)
        num = str(number).strip()
        if trip_id in freq:
            for fs_, fe_, h in freq[trip_id]:
                for t in range(fs_, fe_, h):
                    out_trips.append((t, ti, si, ri, hi, num))
        else:
            out_trips.append((int(round(t0)), ti, si, ri, hi, num))
    if skipped:
        log(f"Skipped {skipped:,} trips without usable stop times")
    out_trips.sort()
    log(f"Patterns: {len(patterns):,}  timings: {len(timings):,}  trips: {len(out_trips):,}  "
        f"service calendars: {len(service_out):,}")

    return {
        "window_start": start, "ndays": ndays, "feed": gtfs_path.name,
        "stop_lon": stop_lon, "stop_lat": stop_lat, "stop_name": stop_name,
        "patterns": patterns, "timings": timings, "trips": out_trips,
        "services": service_out, "routes": route_out, "headsigns": headsigns,
    }


# --------------------------------------------------------------------------------------
# OpenStreetMap rail network
# --------------------------------------------------------------------------------------

def keep_way(tags: dict) -> bool:
    return (tags.get("railway") in RAIL_VALUES
            and tags.get("service") not in EXCLUDED_SERVICE
            and tags.get("area") != "yes")


def overpass_query(bbox) -> str:
    s, w, n, e = bbox
    rail = "|".join(RAIL_VALUES)
    return (f'[out:json][timeout:600][maxsize:1073741824];\n'
            f'way["railway"~"^({rail})$"]["service"!~"^(yard)$"]({s},{w},{n},{e});\n'
            f'out body qt;\n>;\nout skel qt;')


def fetch_overpass_tile(bbox, dest: Path, depth: int = 0) -> list[dict]:
    """Returns a list of Overpass JSON documents covering bbox (tiles are split on failure)."""
    if dest.exists():
        with gzip.open(dest, "rt", encoding="utf-8") as f:
            return [json.load(f)]
    query = overpass_query(bbox)
    last_error = None
    for attempt in range(len(OVERPASS_ENDPOINTS) * 2):
        url = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            log(f"  Overpass {url.split('/')[2]} bbox={bbox}")
            r = requests.post(url, data={"data": query}, timeout=1000, headers=HTTP_HEADERS)
            if r.status_code in (429, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            remark = str(data.get("remark", ""))
            if "error" in remark.lower():
                raise RuntimeError(remark[:200])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(dest, "wt", encoding="utf-8") as f:
                json.dump(data, f)
            return [data]
        except Exception as exc:
            last_error = exc
            log(f"    failed: {exc}")
            time.sleep(min(60, 10 * (attempt + 1)))
    if depth < 2:
        log("  splitting tile into four smaller ones")
        s, w, n, e = bbox
        ms, mw = (s + n) / 2, (w + e) / 2
        docs = []
        for i, sub in enumerate([(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]):
            docs += fetch_overpass_tile(sub, dest.with_name(dest.name.replace(".json.gz", f"_{i}.json.gz")), depth + 1)
        return docs
    raise SystemExit(f"Overpass download failed ({last_error}). Try again later or use --osm-pbf "
                     "with a Geofabrik extract (see README).")


def way_cost(tags: dict) -> float:
    return TRAM_COST if tags.get("railway") == "tram" else 1.0


def ways_from_overpass(docs):
    """-> node ids (sorted), lon, lat, list of node-id arrays, list of cost factors"""
    node_ids, node_lat, node_lon = [], [], []
    ways: dict[int, tuple[np.ndarray, float]] = {}
    for data in docs:
        ids, lats, lons = [], [], []
        for el in data.get("elements", []):
            t = el.get("type")
            if t == "node":
                ids.append(el["id"]); lats.append(el["lat"]); lons.append(el["lon"])
            elif t == "way" and keep_way(el.get("tags", {})):
                ways[el["id"]] = (np.asarray(el["nodes"], dtype=np.int64), way_cost(el.get("tags", {})))
        node_ids.append(np.asarray(ids, np.int64)); node_lat.append(np.asarray(lats)); node_lon.append(np.asarray(lons))
    ids = np.concatenate(node_ids) if node_ids else np.zeros(0, np.int64)
    lat = np.concatenate(node_lat) if node_lat else np.zeros(0)
    lon = np.concatenate(node_lon) if node_lon else np.zeros(0)
    ids, first = np.unique(ids, return_index=True)
    return ids, lon[first], lat[first], [w for w, _ in ways.values()], [c for _, c in ways.values()]


def ways_from_pbf(path: Path, bbox):
    try:
        import osmium
    except ImportError:
        raise SystemExit("Reading .osm.pbf files needs pyosmium:  pip install osmium")
    s, w, n, e = bbox

    class Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways, self.costs, self.coords = [], [], {}

        def way(self, way):
            tags = {t.k: t.v for t in way.tags}
            if not keep_way(tags):
                return
            refs, inside = [], False
            for nd in way.nodes:
                if not nd.location.valid():
                    continue
                lon, lat = nd.location.lon, nd.location.lat
                refs.append(nd.ref)
                self.coords[nd.ref] = (lon, lat)
                inside = inside or (s <= lat <= n and w <= lon <= e)
            if inside and len(refs) > 1:
                self.ways.append(np.asarray(refs, np.int64))
                self.costs.append(way_cost(tags))

    h = Handler()
    log(f"Reading {path} (this takes a minute)")
    h.apply_file(str(path), locations=True)
    ids = np.fromiter(h.coords.keys(), np.int64, len(h.coords))
    ll = np.array(list(h.coords.values()), float).reshape(-1, 2)
    order = np.argsort(ids)
    return ids[order], ll[order, 0], ll[order, 1], h.ways, h.costs


def load_osm(args, cache: Path):
    if args.osm_pbf:
        return ways_from_pbf(Path(args.osm_pbf), args.bbox)
    if args.osm_json:
        p = Path(args.osm_json)
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as f:
            return ways_from_overpass([json.load(f)])
    log("Downloading the OSM rail network from the Overpass API")
    s, w, n, e = args.bbox
    docs = []
    lat_edges = np.linspace(s, n, 3)
    lon_edges = np.linspace(w, e, 4)
    tile = 0
    for i in range(2):
        for j in range(3):
            bb = tuple(round(float(v), 4) for v in (lat_edges[i], lon_edges[j], lat_edges[i + 1], lon_edges[j + 1]))
            docs += fetch_overpass_tile(bb, cache / "osm" / f"rail_tile{tile}.json.gz")
            tile += 1
    return ways_from_overpass(docs)


# --------------------------------------------------------------------------------------
# Rail graph, snapping and turn-restricted routing
# --------------------------------------------------------------------------------------

def rdp(pts: np.ndarray, eps: float) -> np.ndarray:
    n = len(pts)
    if n < 3:
        return pts
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = pts[j] - pts[i]
        L = math.hypot(seg[0], seg[1])
        rel = pts[i + 1:j] - pts[i]
        if L < 1e-9:
            d = np.hypot(rel[:, 0], rel[:, 1])
        else:
            d = np.abs(rel[:, 0] * seg[1] - rel[:, 1] * seg[0]) / L
        k = int(np.argmax(d))
        if d[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return pts[keep]


class RailNetwork:
    def __init__(self, node_ids, lon, lat, ways, costs=None):
        self.x, self.y = project(lon, lat)
        if costs is None:
            costs = [1.0] * len(ways)
        segs, segc = [], []
        n = len(node_ids)
        for nodes, cost in zip(ways, costs):
            if len(nodes) < 2 or n == 0:
                continue
            idx = np.clip(np.searchsorted(node_ids, nodes), 0, n - 1)
            ok = node_ids[idx] == nodes
            pairs = np.stack([idx[:-1], idx[1:]], axis=1)
            if not ok.all():
                pairs = pairs[ok[:-1] & ok[1:]]
            segs.append(pairs)
            segc.append(np.full(len(pairs), cost))
        segs = np.concatenate(segs) if segs else np.zeros((0, 2), np.int64)
        segc = np.concatenate(segc) if segc else np.zeros(0)
        keep = segs[:, 0] != segs[:, 1]
        segs, segc = np.sort(segs[keep], axis=1), segc[keep]
        segs, inv = np.unique(segs, axis=0, return_inverse=True)
        cost = np.full(len(segs), np.inf)
        np.minimum.at(cost, inv.ravel(), segc)  # a track that is both rail and tram counts as rail
        self.segs = segs.astype(np.int64)
        self.seg_cost = cost
        total_km = np.hypot(self.x[segs[:, 1]] - self.x[segs[:, 0]], self.y[segs[:, 1]] - self.y[segs[:, 0]]).sum() / 1000
        log(f"OSM rail network: {len(ways):,} ways, {len(self.segs):,} segments, {total_km:,.0f} km of track")

    # -- snapping ------------------------------------------------------------------------
    def snap(self, sx, sy, radius=SNAP_RADIUS, far=SNAP_FAR, per_component=3, max_components=3):
        """For every stop, pick candidate points on the track network.

        Candidates come from up to `max_components` separate networks (standard gauge, metre gauge,
        tram ...) and up to `per_component` parallel tracks each, so the router can choose the one
        that actually connects to the previous/next stop.
        Returns, per stop, a list of (graph node, snapping distance).
        """
        segs = self.segs
        m = len(segs)
        if m == 0:
            return [[] for _ in sx]
        ax, ay = self.x[segs[:, 0]], self.y[segs[:, 0]]
        dx, dy = self.x[segs[:, 1]] - ax, self.y[segs[:, 1]] - ay
        L2 = dx * dx + dy * dy
        L = np.sqrt(L2)
        n = len(self.x)
        adj = csr_matrix((np.ones(m), (segs[:, 0], segs[:, 1])), shape=(n, n))
        _, comp = connected_components(adj, directed=False)
        seg_comp = comp[segs[:, 0]]

        step = 15.0
        cnt = np.maximum(2, np.ceil(L / step).astype(np.int64) + 1)
        rep = np.repeat(np.arange(m), cnt)
        off = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        t = off / np.repeat(cnt - 1, cnt)
        tree = cKDTree(np.c_[ax[rep] + dx[rep] * t, ay[rep] + dy[rep] * t])
        del t, off

        near = tree.query_ball_point(np.c_[sx, sy], r=radius + step)
        picks = []
        for i, lst in enumerate(near):
            px, py = sx[i], sy[i]
            if lst:
                segids = np.unique(rep[lst])
                limit = radius
            else:
                d0, j = tree.query([px, py], k=1, distance_upper_bound=far)
                if not np.isfinite(d0):
                    picks.append([])
                    continue
                segids = np.array([rep[j]])
                limit = far
            tt = np.clip(((px - ax[segids]) * dx[segids] + (py - ay[segids]) * dy[segids]) / np.maximum(L2[segids], 1e-9), 0, 1)
            qx, qy = ax[segids] + dx[segids] * tt, ay[segids] + dy[segids] * tt
            d = np.hypot(px - qx, py - qy)
            chosen, per_comp = [], {}
            for k in np.argsort(d):
                if d[k] > limit:
                    break
                c = seg_comp[segids[k]]
                pts = per_comp.get(c)
                if pts is None:
                    if len(per_comp) >= max_components:
                        continue
                    pts = per_comp[c] = []
                if len(pts) >= per_component or any(math.hypot(qx[k] - a, qy[k] - b) < 3.0 for a, b in pts):
                    continue
                pts.append((qx[k], qy[k]))
                chosen.append((int(segids[k]), float(tt[k]), float(qx[k]), float(qy[k]), float(d[k])))
            picks.append(chosen)
        return self._insert_snap_nodes(picks, L)

    def _insert_snap_nodes(self, picks, seg_len):
        """Split track segments at the snapped points so that every candidate is a graph node."""
        by_seg = defaultdict(list)
        for si, lst in enumerate(picks):
            for ci, (seg, t, qx, qy, d) in enumerate(lst):
                by_seg[seg].append((t, si, ci, qx, qy))
        out = [[None] * len(lst) for lst in picks]
        next_id = len(self.x)
        new_x, new_y, removed, added, added_cost = [], [], [], [], []
        for seg, items in by_seg.items():
            a, b = int(self.segs[seg, 0]), int(self.segs[seg, 1])
            L = float(seg_len[seg])
            items.sort()
            chain, prev_t = [a], None
            for t, si, ci, qx, qy in items:
                if t * L < 0.5:
                    node = a
                elif (1 - t) * L < 0.5:
                    node = b
                elif prev_t is not None and (t - prev_t) * L < 0.5:
                    node = chain[-1]
                else:
                    node = next_id
                    next_id += 1
                    new_x.append(qx); new_y.append(qy)
                    chain.append(node)
                    prev_t = t
                out[si][ci] = node
            if len(chain) > 1:
                chain.append(b)
                removed.append(seg)
                added.extend(zip(chain[:-1], chain[1:]))
                added_cost.extend([self.seg_cost[seg]] * (len(chain) - 1))
        if new_x:
            self.x = np.r_[self.x, new_x]
            self.y = np.r_[self.y, new_y]
        if removed:
            keep = np.ones(len(self.segs), bool)
            keep[removed] = False
            self.segs = np.r_[self.segs[keep], np.asarray(added, np.int64).reshape(-1, 2)]
            self.seg_cost = np.r_[self.seg_cost[keep], np.asarray(added_cost, float)]
        cands = []
        for si, lst in enumerate(picks):
            best: dict[int, float] = {}
            for ci, (_, _, _, _, d) in enumerate(lst):
                node = out[si][ci]
                if node not in best or d < best[node]:
                    best[node] = d
            cands.append(sorted(best.items(), key=lambda kv: kv[1]))
        return cands

    # -- graph ---------------------------------------------------------------------------
    def build(self, keep_nodes, max_turn_deg=MAX_TURN_DEG):
        """Contract chains of degree-2 nodes and build the turn-restricted routing graph."""
        n, segs = len(self.x), self.segs
        m = len(segs)
        rows = np.r_[segs[:, 0], segs[:, 1]]
        cols = np.r_[segs[:, 1], segs[:, 0]]
        sid = np.r_[np.arange(m), np.arange(m)]
        order = np.argsort(rows, kind="stable")
        rows, cols, sid = rows[order], cols[order], sid[order]
        indptr = np.searchsorted(rows, np.arange(n + 1))
        deg = np.diff(indptr)
        junction = deg != 2
        if len(keep_nodes):
            junction[np.asarray(sorted(keep_nodes), np.int64)] = True
        cols_l, sid_l, ip, junc = cols.tolist(), sid.tolist(), indptr.tolist(), junction.tolist()
        visited = bytearray(m)
        polys: list[list[int]] = []
        poly_sids: list[list[int]] = []

        def walk_from(j):
            for p in range(ip[j], ip[j + 1]):
                s = sid_l[p]
                if visited[s]:
                    continue
                visited[s] = 1
                path, sids, prev_s, cur = [j], [s], s, cols_l[p]
                while not junc[cur]:
                    path.append(cur)
                    q = ip[cur] if sid_l[ip[cur]] != prev_s else ip[cur] + 1
                    prev_s = sid_l[q]
                    sids.append(prev_s)
                    cur = cols_l[q]
                    if visited[prev_s]:
                        break
                    visited[prev_s] = 1
                path.append(cur)
                polys.append(path)
                poly_sids.append(sids)

        for j in np.flatnonzero(junction & (deg > 0)).tolist():
            walk_from(j)
        # isolated loops without any junction
        for s in range(m):
            if not visited[s]:
                j = int(segs[s, 0])
                junc[j] = True
                walk_from(j)

        E = len(polys)
        flat = np.fromiter((v for p in polys for v in p), np.int64)
        lens = np.fromiter((len(p) for p in polys), np.int64, E)
        offs = np.r_[0, np.cumsum(lens)]
        px, py = self.x[flat], self.y[flat]
        seg_d = np.hypot(np.diff(px), np.diff(py))
        seg_d[offs[1:-1] - 1] = 0.0  # no distance across polyline boundaries
        cum = np.r_[0, np.cumsum(seg_d)]
        edge_len = cum[offs[1:] - 1] - cum[offs[:-1]]
        # routing cost: length weighted by track type (tram tracks are penalised)
        seg_w = np.hypot(self.x[segs[:, 1]] - self.x[segs[:, 0]], self.y[segs[:, 1]] - self.y[segs[:, 0]]) * self.seg_cost
        flat_sids = np.fromiter((v for ss in poly_sids for v in ss), np.int64)
        sid_offs = np.r_[0, np.cumsum([len(ss) for ss in poly_sids])[:-1]]
        edge_cost = np.add.reduceat(seg_w[flat_sids], sid_offs) if E else np.zeros(0)

        # direction when leaving the start node / arriving at the end node (measured over ~20 m)
        start_vec = np.zeros((E, 2))
        end_vec = np.zeros((E, 2))
        for e in range(E):
            a, b = offs[e], offs[e + 1]
            c = cum[a:b] - cum[a]
            L = c[-1]
            probe = min(20.0, L / 2)
            k = min(max(int(np.searchsorted(c, probe)), 1), b - a - 1)
            start_vec[e] = (px[a + k] - px[a], py[a + k] - py[a])
            k2 = max(min(int(np.searchsorted(c, L - probe, side="right")) - 1, b - a - 2), 0)
            end_vec[e] = (px[b - 1] - px[a + k2], py[b - 1] - py[a + k2])
        for v in (start_vec, end_vec):
            nrm = np.hypot(v[:, 0], v[:, 1])
            nrm[nrm == 0] = 1
            v /= nrm[:, None]

        U = flat[offs[:-1]]
        V = flat[offs[1:] - 1]
        nd = 2 * E
        st_node = np.empty(nd, np.int64); en_node = np.empty(nd, np.int64)
        st_node[0::2], st_node[1::2] = U, V
        en_node[0::2], en_node[1::2] = V, U
        sv = np.empty((nd, 2)); sv[0::2], sv[1::2] = start_vec, -end_vec
        dlen = np.repeat(edge_cost, 2) + 1e-3

        # transitions: arrive via (a ^ 1), leave via b, for all out-edges a != b at a node
        order = np.argsort(st_node, kind="stable")
        sn = st_node[order]
        grp_start = np.r_[0, np.flatnonzero(np.diff(sn)) + 1]
        grp_cnt = np.diff(np.r_[grp_start, len(sn)])
        g_of = np.repeat(np.arange(len(grp_start)), grp_cnt)
        c_of = grp_cnt[g_of]
        rep_i = np.repeat(np.arange(len(sn)), c_of)
        offs2 = np.arange(len(rep_i)) - np.repeat(np.cumsum(c_of) - c_of, c_of)
        rep_j = grp_start[g_of][rep_i] + offs2
        a_st, b_st = order[rep_i], order[rep_j]
        ok = a_st != b_st
        a_st, b_st = a_st[ok], b_st[ok]
        cos_max = math.cos(math.radians(max_turn_deg))
        arrive = a_st ^ 1
        # direction of travel when arriving via `arrive` equals -(start direction of a_st)
        turn_ok = (-sv[a_st] * sv[b_st]).sum(axis=1) >= cos_max - 1e-9
        self.G_turn = csr_matrix((dlen[arrive[turn_ok]], (arrive[turn_ok], b_st[turn_ok])), shape=(nd, nd))
        self.G_free = csr_matrix((dlen[arrive], (arrive, b_st)), shape=(nd, nd))

        self.polys, self.edge_len, self.dlen = polys, edge_len, dlen
        self.st_node, self.en_node = st_node, en_node
        o = np.argsort(st_node, kind="stable")
        self._out_order, self._out_keys = o, st_node[o]
        o = np.argsort(en_node, kind="stable")
        self._in_order, self._in_keys = o, en_node[o]
        log(f"Routing graph: {E:,} track edges, {int(turn_ok.sum()):,} allowed transitions")

    def out_states(self, node):
        a = np.searchsorted(self._out_keys, node); b = np.searchsorted(self._out_keys, node, side="right")
        return self._out_order[a:b]

    def in_states(self, node):
        a = np.searchsorted(self._in_keys, node); b = np.searchsorted(self._in_keys, node, side="right")
        return self._in_order[a:b]

    def state_path(self, pred, last_state) -> list[int]:
        chain = [int(last_state)]
        while pred[chain[-1]] >= 0:
            chain.append(int(pred[chain[-1]]))
        chain.reverse()
        nodes: list[int] = []
        for d in chain:
            poly = self.polys[d >> 1]
            if d & 1:
                poly = poly[::-1]
            nodes.extend(poly if not nodes else poly[1:])
        return nodes

    def route_from(self, src_cands, targets, restricted=True):
        """Route from one stop to several others.

        src_cands: [(node, snap_dist)], targets: {key: (cands, crow_distance)}
        Returns {key: (node path, cost)} for the targets that could be reached.
        """
        G = self.G_turn if restricted else self.G_free
        best: dict = {}
        max_limit = max(max(3 * crow, crow + 8000) for _, crow in targets.values()) + 2 * SNAP_RADIUS
        for na, da in src_cands:
            srcs = self.out_states(na)
            if len(srcs) == 0:
                continue
            dist, pred, _ = dijkstra(G, directed=True, indices=srcs, return_predecessors=True,
                                     limit=max_limit, min_only=True)
            for key, (cands, crow) in targets.items():
                limit = max(3 * crow, crow + 8000) + 2 * SNAP_RADIUS
                for nb, db in cands:
                    if nb == na:
                        cost, state = 0.0, None
                    else:
                        ins = self.in_states(nb)
                        if len(ins) == 0:
                            continue
                        c = dist[ins] + self.dlen[ins]
                        k = int(np.argmin(c))
                        cost, state = float(c[k]), int(ins[k])
                    if not np.isfinite(cost) or cost > limit:
                        continue
                    total = cost + SNAP_PENALTY * (da + db)
                    if key not in best or total < best[key][1]:
                        path = [na] if state is None else self.state_path(pred, state)
                        best[key] = (path, total)
        return best


def route_legs(net: RailNetwork, tt, restricted_only=False):
    """Compute track geometry for every stop pair used by any pattern."""
    stop_x, stop_y = project(tt["stop_lon"], tt["stop_lat"])
    pairs = set()
    for p in tt["patterns"]:
        for a, b in zip(p[:-1], p[1:]):
            pairs.add((a, b) if a <= b else (b, a))
    used = sorted({s for pair in pairs for s in pair})
    log(f"Stop pairs to route: {len(pairs):,} between {len(used):,} stops")

    t0 = time.time()
    cand_list = net.snap(stop_x[used], stop_y[used])
    cands = dict(zip(used, cand_list))
    unsnapped = [s for s in used if not cands[s]]
    log(f"Snapped stops to tracks in {time.time() - t0:.0f}s ({len(unsnapped):,} stops without track nearby)")
    if unsnapped:
        names = sorted({tt["stop_name"][s] for s in unsnapped})
        log("  e.g. " + ", ".join(names[:12]) + (" ..." if len(names) > 12 else ""))

    keep = {node for lst in cand_list for node, _ in lst}
    net.build(keep)

    by_src = defaultdict(dict)
    for a, b in pairs:
        crow = float(math.hypot(stop_x[a] - stop_x[b], stop_y[a] - stop_y[b]))
        by_src[a][b] = crow

    legs: dict[tuple[int, int], np.ndarray] = {}
    mode_count = {"track": 0, "track (no turn limits)": 0, "straight line": 0}
    detours = []
    t0, done = time.time(), 0
    for a, dests in by_src.items():
        done += 1
        if done % 250 == 0:
            log(f"  routed from {done:,}/{len(by_src):,} stops ({time.time() - t0:.0f}s)")
        targets = {b: (cands[b], crow) for b, crow in dests.items() if cands[b]}
        found = net.route_from(cands[a], targets, restricted=True) if (cands[a] and targets) else {}
        missing = {b: v for b, v in targets.items() if b not in found}
        if missing and not restricted_only:
            for b, res in net.route_from(cands[a], missing, restricted=False).items():
                found[b] = res
                mode_count["track (no turn limits)"] += 1
        for b, crow in dests.items():
            if b in found:
                if b not in missing:
                    mode_count["track"] += 1
                path = found[b][0]
                pts = np.c_[net.x[path], net.y[path]]
                length = float(np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1])).sum())
                if crow > 500:
                    detours.append(length / crow)
            else:
                mode_count["straight line"] += 1
                pts = np.array([[stop_x[a], stop_y[a]], [stop_x[b], stop_y[b]]])
            legs[(a, b)] = rdp(pts, SIMPLIFY_M)
    log(f"Routing finished in {time.time() - t0:.0f}s: " + ", ".join(f"{k}: {v:,}" for k, v in mode_count.items()))
    if detours:
        d = np.array(detours)
        log(f"  track length / straight distance: median {np.median(d):.2f}, 99th percentile {np.percentile(d, 99):.2f}")
    return legs, mode_count


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

def encode_line(pts_xy: np.ndarray) -> list[int]:
    lon, lat = unproject(pts_xy[:, 0], pts_xy[:, 1])
    q = np.c_[np.round(lon * 1e5), np.round(lat * 1e5)].astype(np.int64)
    keep = np.r_[True, np.any(np.diff(q, axis=0) != 0, axis=1)]
    q = q[keep]
    if len(q) == 1:
        q = np.r_[q, q]
    d = np.r_[q[:1], np.diff(q, axis=0)]
    return d.ravel().tolist()


def write_output(tt, legs, stats, out_dir: Path, bbox):
    out_dir.mkdir(parents=True, exist_ok=True)
    leg_index = {}
    leg_list = []
    for key in sorted(legs):
        leg_index[key] = len(leg_list)
        leg_list.append(encode_line(legs[key]))
    patterns_out = []
    for p in tt["patterns"]:
        refs = []
        for a, b in zip(p[:-1], p[1:]):
            if a <= b:
                refs.append(leg_index[(a, b)] + 1)
            else:
                refs.append(-(leg_index[(b, a)] + 1))
        patterns_out.append([list(p), refs])

    stops_out = [[tt["stop_name"][i], round(float(tt["stop_lon"][i]), 5), round(float(tt["stop_lat"][i]), 5)]
                 for i in range(len(tt["stop_name"]))]

    network = {
        "stops": stops_out,
        "legs": leg_list,
        "bbox": bbox,
    }
    trips = tt["trips"]
    timetable = {
        "meta": {
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "feed": tt["feed"],
            "windowStart": tt["window_start"].isoformat(),
            "days": tt["ndays"],
            "timezone": "Europe/Zurich",
            "routing": stats,
            "attribution": [
                "Timetable: opentransportdata.swiss (Swiss GTFS)",
                "Tracks: © OpenStreetMap contributors (ODbL)",
            ],
        },
        "routes": tt["routes"],
        "headsigns": tt["headsigns"],
        "services": tt["services"],
        "patterns": patterns_out,
        "timings": tt["timings"],
        "trips": {
            "start": [t[0] for t in trips],
            "timing": [t[1] for t in trips],
            "service": [t[2] for t in trips],
            "route": [t[3] for t in trips],
            "headsign": [t[4] for t in trips],
            "number": [t[5] for t in trips],
        },
    }
    for name, obj in (("network.json", network), ("timetable.json", timetable)):
        path = out_dir / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        log(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")


# --------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs", help="GTFS zip file or URL (default: newest Swiss feed from opentransportdata.swiss)")
    ap.add_argument("--osm-json", help="Overpass JSON file (optionally .gz) with the rail ways and their nodes")
    ap.add_argument("--osm-pbf", help="OpenStreetMap .osm.pbf extract, e.g. Geofabrik's switzerland-latest.osm.pbf")
    ap.add_argument("--days", type=int, default=60, help="number of days to include (default 60)")
    ap.add_argument("--start", help="first service day YYYY-MM-DD (default: yesterday, Swiss time)")
    ap.add_argument("--route-types", default="2,100-199", help="GTFS route_type values treated as trains")
    ap.add_argument("--bbox", default=",".join(map(str, DEFAULT_BBOX)), help="south,west,north,east for OSM data")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default ../web/data)")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="download cache folder")
    args = ap.parse_args(argv)
    args.bbox = tuple(float(v) for v in args.bbox.split(","))
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    start = dt.date.fromisoformat(args.start) if args.start else None
    t_start = time.time()

    # 1. timetable
    if args.gtfs and not re.match(r"https?://", args.gtfs):
        gtfs_path = Path(args.gtfs)
    else:
        if args.gtfs:
            name = Path(args.gtfs.split("?")[0]).name or "gtfs.zip"
            gtfs_path = download(args.gtfs, cache / name, "GTFS timetable")
        else:
            gtfs_path = fetch_gtfs(cache, today_in_switzerland())
    tt = load_timetable(gtfs_path, start, args.days, parse_route_types(args.route_types))

    # 2. tracks
    node_ids, lon, lat, ways, costs = load_osm(args, cache)
    net = RailNetwork(node_ids, lon, lat, ways, costs)
    del node_ids, lon, lat, ways, costs

    # 3. map-match every stop-to-stop leg onto the tracks
    legs, stats = route_legs(net, tt)

    # 4. write
    write_output(tt, legs, stats, Path(args.out), args.bbox)
    log(f"All done in {(time.time() - t_start) / 60:.1f} min")


if __name__ == "__main__":
    main()
