#!/usr/bin/env python3
"""Generate a synthetic test dataset in the exact formats the real sources use:

* gtfs_synthetic.zip   - Swiss GTFS layout (SLOID stop ids, Parent stations, extended route
                         types, calendar + calendar_dates exceptions, frequencies.txt,
                         times beyond 24:00, a bus route that must be ignored)
* osm_synthetic.json   - Overpass JSON (ways with node refs + nodes), double track with
                         crossovers, a separate metre-gauge network, a rack railway, a tram ring
                         that must not capture rail stops, and a yard shortcut that must be ignored

Station coordinates are real; the tracks between them are invented curves. It only exists to
exercise the pipeline and the viewer end-to-end without internet access.
"""
import csv
import io
import json
import math
import sys
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_data import project, unproject  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"

STATIONS = {  # key: (name, lon, lat, uic)
    "BAS": ("Basel SBB", 7.5897, 47.5476, 8500010),
    "LIE": ("Liestal", 7.7347, 47.4843, 8500023),
    "OLT": ("Olten", 7.9077, 47.3519, 8500218),
    "AAR": ("Aarau", 8.0512, 47.3913, 8502113),
    "ZUE": ("Zürich HB", 8.5403, 47.3779, 8503000),
    "ZOF": ("Zofingen", 7.9453, 47.2878, 8502105),
    "SUR": ("Sursee", 8.0977, 47.1713, 8502007),
    "LUZ": ("Luzern", 8.3102, 47.0502, 8505000),
    "LAN": ("Langenthal", 7.7852, 47.2172, 8508008),
    "BER": ("Bern", 7.4391, 46.9490, 8507000),
    # metre gauge (Zentralbahn) - separate network, own station nodes
    "LUZZB": ("Luzern", 8.3114, 47.0500, 8505000),
    "SAR": ("Sarnen", 8.2449, 46.8958, 8505203),
    "MEI": ("Meiringen", 8.1838, 46.7289, 8505213),
    "BRZ": ("Brienz", 8.0357, 46.7546, 8507483),
    "INT": ("Interlaken Ost", 7.8691, 46.6905, 8507492),
    # rack railway (Brienz Rothorn Bahn)
    "BRB": ("Brienz BRB", 8.0372, 46.7556, 8507484),
    "ROT": ("Brienzer Rothorn", 8.0470, 46.7870, 8507485),
}
HALTS = {"BUR": ("Burgdorf", 8508005)}  # intermediate halt on the Langenthal-Bern corridor

CORRIDORS = [  # from, to, bend (fraction of length), tracks, railway tag
    ("BAS", "LIE", 0.10, 2, "rail"),
    ("LIE", "OLT", -0.12, 2, "rail"),
    ("OLT", "AAR", 0.08, 2, "rail"),
    ("AAR", "ZUE", -0.07, 2, "rail"),
    ("OLT", "ZOF", 0.06, 2, "rail"),
    ("ZOF", "SUR", -0.08, 2, "rail"),
    ("SUR", "LUZ", 0.07, 2, "rail"),
    ("OLT", "LAN", -0.06, 2, "rail"),
    ("LAN", "BER", 0.08, 2, "rail"),
    ("LUZZB", "SAR", 0.10, 1, "narrow_gauge"),
    ("SAR", "MEI", -0.14, 1, "narrow_gauge"),
    ("MEI", "BRZ", 0.12, 1, "narrow_gauge"),
    ("BRZ", "INT", -0.06, 1, "narrow_gauge"),
    ("BRB", "ROT", 0.25, 1, "narrow_gauge"),
]


class Osm:
    def __init__(self):
        self.nodes, self.ways, self.next_node, self.next_way = {}, [], 1, 1
        self.station_node = {}

    def node(self, x, y):
        nid = self.next_node
        self.next_node += 1
        self.nodes[nid] = (x, y)
        return nid

    def way(self, nodes, **tags):
        self.ways.append({"type": "way", "id": self.next_way, "nodes": list(nodes), "tags": tags})
        self.next_way += 1

    def to_overpass(self):
        els = list(self.ways)
        ids = sorted(self.nodes)
        xs = np.array([self.nodes[i][0] for i in ids]); ys = np.array([self.nodes[i][1] for i in ids])
        lon, lat = unproject(xs, ys)
        for i, a, b in zip(ids, lon, lat):
            els.append({"type": "node", "id": int(i), "lat": round(float(b), 7), "lon": round(float(a), 7)})
        return {"version": 0.6, "generator": "synthetic", "elements": els}


def st_xy(key):
    _, lon, lat, _ = STATIONS[key]
    x, y = project(lon, lat)
    return float(x), float(y)


def centerline(p0, p1, bend, spacing=40.0):
    p0, p1 = np.array(p0), np.array(p1)
    d = p1 - p0
    L = np.hypot(*d)
    nrm = np.array([-d[1], d[0]]) / L
    ctrl = (p0 + p1) / 2 + nrm * bend * L
    t = np.linspace(0, 1, 2000)[:, None]
    pts = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * ctrl + t ** 2 * p1
    # add a gentle wiggle so the track is visibly not a straight line
    wig = np.sin(t[:, 0] * math.pi * 5) * min(400.0, 0.02 * L)
    tang = np.gradient(pts, axis=0)
    tang /= np.hypot(tang[:, 0], tang[:, 1])[:, None]
    pts = pts + np.c_[-tang[:, 1], tang[:, 0]] * wig[:, None] * np.sin(t[:, 0] * math.pi)[:, None]
    seg = np.hypot(*np.diff(pts, axis=0).T)
    cum = np.r_[0, np.cumsum(seg)]
    s = np.arange(0, cum[-1], spacing)
    s = np.r_[s, cum[-1]] if cum[-1] - s[-1] > spacing * 0.3 else np.r_[s[:-1], cum[-1]]
    res = np.c_[np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])]
    tang = np.gradient(res, axis=0)
    tang /= np.hypot(tang[:, 0], tang[:, 1])[:, None]
    return res, np.c_[-tang[:, 1], tang[:, 0]]


def build_osm():
    osm = Osm()
    for key in STATIONS:
        osm.station_node[key] = osm.node(*st_xy(key))
    halt_pos = {}
    for a, b, bend, ntracks, tag in CORRIDORS:
        pa, pb = np.array(st_xy(a)), np.array(st_xy(b))
        d = (pb - pa) / np.hypot(*(pb - pa))
        c, nrm = centerline(pa + d * 250, pb - d * 250, bend)
        offsets = [-2.25, 2.25] if ntracks == 2 else [0.0]
        tracks = []
        for off in offsets:
            ids = [osm.node(*(c[i] + nrm[i] * off)) for i in range(len(c))]
            tracks.append(ids)
            extra = {"gauge": "1000", "rack": "abt"} if (a, b) == ("BRB", "ROT") else ({"gauge": "1000"} if tag == "narrow_gauge" else {})
            osm.way([osm.station_node[a]] + ids + [osm.station_node[b]], railway=tag, usage="main", **extra)
        if ntracks == 2:
            n = len(c)
            for k, i in enumerate(range(20, n - 30, 60)):
                if (a, b) == ("LAN", "BER") and abs(i - int(0.55 * n)) < 80:
                    # only one crossover orientation around the Burgdorf halt
                    osm.way([tracks[0][i], tracks[1][i + 3]], railway="rail", service="crossover")
                    continue
                if k % 2 == 0:
                    osm.way([tracks[0][i], tracks[1][i + 3]], railway="rail", service="crossover")
                else:
                    osm.way([tracks[1][i], tracks[0][i + 3]], railway="rail", service="crossover")
            # station siding (platform track) near both ends
            for i0 in (5, n - 25):
                j = i0 + 18
                sid = [osm.node(*(c[i] + nrm[i] * 8.5)) for i in range(i0 + 3, j - 2)]
                osm.way([tracks[1][i0]] + sid + [tracks[1][j]], railway="rail", service="siding")
        if (a, b) == ("LAN", "BER"):
            i = int(0.55 * len(c))
            halt_pos["BUR"] = c[i] + nrm[i] * 4.0  # platform next to the right-hand track
    # tram ring near Zürich HB (must not capture the rail stop)
    zx, zy = st_xy("ZUE")
    ring = [osm.node(zx + 160 * math.cos(a), zy + 90 + 110 * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 40)[:-1]]
    osm.way(ring + [ring[0]], railway="tram")
    # a yard connecting Basel directly with Olten - would be a huge shortcut if not filtered
    bx, by = st_xy("BAS"); ox, oy = st_xy("OLT")
    yard = [osm.node(bx + (ox - bx) * t, by + (oy - by) * t) for t in np.linspace(0.02, 0.98, 30)]
    osm.way([osm.station_node["BAS"]] + yard + [osm.station_node["OLT"]], railway="rail", service="yard")
    return osm, halt_pos


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def build_gtfs(halt_pos, path):
    # --- stops -------------------------------------------------------------------------
    stops = []
    plat = {}

    def add_stop(key, name, lon, lat, uic, track="1"):
        parent = f"Parentch:1:sloid:{uic % 100000}"
        if parent not in {s[0] for s in stops}:
            stops.append((parent, name, lat, lon, "1", ""))
        sid = f"ch:1:sloid:{uic % 100000}:0:{track}"
        stops.append((sid, name, lat, lon, "", parent))
        plat[key] = sid

    for key, (name, lon, lat, uic) in STATIONS.items():
        if key == "LUZZB":
            # Zentralbahn platform: coordinate closer to the SBB tracks than to the metre-gauge ones
            add_stop(key, name, 8.31055, 47.05012, uic, track="13")
        else:
            add_stop(key, name, lon, lat, uic)
    hx, hy = halt_pos["BUR"]
    hlon, hlat = unproject(hx, hy)
    add_stop("BUR", "Burgdorf", round(float(hlon), 6), round(float(hlat), 6), HALTS["BUR"][1])
    add_stop("MIL", "Milano Centrale", 9.2046, 45.4868, 8301700)
    stops.append(("ch:1:sloid:99999:0:A", "Bus stop", 47.30, 8.30, "", ""))

    # --- routes --------------------------------------------------------------------------
    routes = [  # route_id, agency, short, long, desc, type
        ("r_ic1", "11", "IC1", "", "IC", "102"),
        ("r_ic5", "11", "IC5", "", "IC", "102"),
        ("r_ir", "11", "IR27", "", "IR", "103"),
        ("r_s3", "11", "S3", "", "S", "109"),
        ("r_s29", "11", "S29", "", "S", "109"),
        ("r_s23", "11", "S23", "", "S", "109"),
        ("r_re", "11", "RE", "", "RE", "106"),
        ("r_zb", "54", "IR70", "", "IR", "103"),
        ("r_brb", "88", "", "", "PE", "116"),
        ("r_ec", "11", "EC", "", "EC", "102"),
        ("r_bus", "801", "12", "", "B", "700"),
    ]
    # --- calendar -------------------------------------------------------------------------
    calendar = [
        ("DAILY", 1, 1, 1, 1, 1, 1, 1, "20251214", "20261212"),
        ("WKDAY", 1, 1, 1, 1, 1, 0, 0, "20251214", "20261212"),
        ("WKEND", 0, 0, 0, 0, 0, 1, 1, "20251214", "20261212"),
        ("EXC", 1, 1, 1, 1, 1, 1, 1, "20251214", "20261212"),
    ]
    calendar_dates = [("EXC", "20260925", "2"), ("CDONLY", "20260925", "1"), ("CDONLY", "20260926", "1")]

    trips, stop_times, freqs = [], [], []
    counter = [0]

    def add_trip(route, service, seq, dep, runs, dwell=60, number="", headsign="", freq=None):
        """seq: station keys; runs: minutes between consecutive stops."""
        counter[0] += 1
        tid = f"{counter[0]}.TA.91-{route}"
        names = {**{k: v[0] for k, v in STATIONS.items()}, "BUR": "Burgdorf", "MIL": "Milano Centrale"}
        trips.append((route, service, tid, headsign or names[seq[-1]], number))
        t = dep
        for i, key in enumerate(seq):
            arr = t
            d = t if i in (0, len(seq) - 1) else t + dwell
            stop_times.append((tid, hms(arr), hms(d), plat[key], str(i + 1)))
            if i < len(seq) - 1:
                t = d + runs[i] * 60
        if freq:
            freqs.append((tid,) + freq)

    def both(route, service, seq, runs, first, last, every, **kw):
        t = first
        while t <= last:
            add_trip(route, service, seq, t, runs, **kw)
            add_trip(route, service, seq[::-1], t + 7 * 60, runs[::-1], **kw)
            t += every

    H = 3600
    both("r_ic1", "DAILY", ["BAS", "OLT", "BER"], [24, 27], 6 * H, 22.5 * H, 30 * 60, number="9")
    both("r_ic5", "WKDAY", ["ZUE", "AAR", "OLT", "BER"], [25, 9, 27], 6 * H + 120, 22 * H, 30 * 60, number="15")
    both("r_ir", "DAILY", ["BAS", "LIE", "OLT", "ZOF", "SUR", "LUZ"], [9, 13, 6, 10, 13], 5.5 * H, 23 * H, 60 * 60)
    both("r_s3", "DAILY", ["OLT", "LAN", "BUR", "BER"], [12, 11, 12], 5 * H + 900, 23.5 * H, 30 * 60)
    both("r_s29", "WKEND", ["OLT", "ZOF", "SUR", "LUZ"], [8, 12, 15], 6 * H, 22 * H, 60 * 60)
    both("r_s23", "EXC", ["BAS", "LIE"], [11], 6 * H, 22 * H, 30 * 60)       # must NOT run on 2026-09-25
    both("r_re", "CDONLY", ["LUZ", "SUR", "ZOF", "OLT"], [12, 9, 7], 6 * H + 1800, 21 * H, 60 * 60)
    both("r_zb", "DAILY", ["LUZZB", "SAR", "MEI", "BRZ", "INT"], [22, 31, 12, 17], 6 * H, 21 * H, 60 * 60)
    add_trip("r_brb", "DAILY", ["BRB", "ROT"], 0, [55], freq=("08:30:00", "16:31:00", "3600", "0"))
    add_trip("r_brb", "DAILY", ["ROT", "BRB"], 0, [60], freq=("09:40:00", "17:41:00", "3600", "0"))
    for h in (7.55, 11.55, 15.55, 19.55):
        add_trip("r_ec", "DAILY", ["ZUE", "MIL"], h * H, [198], number="317", headsign="Milano Centrale")
    add_trip("r_ic5", "DAILY", ["ZUE", "AAR", "OLT", "BER"], 23 * H + 2400, [25, 9, 27], number="1099")  # past midnight
    # a bus trip (route_type 700) that must be ignored
    counter[0] += 1
    trips.append(("r_bus", "DAILY", f"{counter[0]}.bus", "Nowhere", ""))
    stop_times.append((f"{counter[0]}.bus", "08:00:00", "08:00:00", "ch:1:sloid:99999:0:A", "1"))
    stop_times.append((f"{counter[0]}.bus", "08:10:00", "08:10:00", plat["BAS"], "2"))

    def table(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        return "﻿" + buf.getvalue()  # the real feed has a BOM

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("agency.txt", table(["agency_id", "agency_name", "agency_url", "agency_timezone"],
                                       [("11", "SBB", "https://www.sbb.ch", "Europe/Berlin"),
                                        ("54", "zb", "https://www.zentralbahn.ch", "Europe/Berlin"),
                                        ("88", "BRB", "https://brienz-rothorn-bahn.ch", "Europe/Berlin"),
                                        ("801", "PAG", "https://www.postauto.ch", "Europe/Berlin")]))
        z.writestr("stops.txt", table(["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"], stops))
        z.writestr("routes.txt", table(["route_id", "agency_id", "route_short_name", "route_long_name", "route_desc", "route_type"], routes))
        z.writestr("trips.txt", table(["route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name"], trips))
        z.writestr("stop_times.txt", table(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], stop_times))
        z.writestr("calendar.txt", table(["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                          "sunday", "start_date", "end_date"], calendar))
        z.writestr("calendar_dates.txt", table(["service_id", "date", "exception_type"], calendar_dates))
        z.writestr("frequencies.txt", table(["trip_id", "start_time", "end_time", "headway_secs", "exact_times"], freqs))
        z.writestr("feed_info.txt", table(["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date",
                                           "feed_end_date", "feed_version"],
                                          [("SKI+", "https://opentransportdata.swiss", "DE", "20251214", "20261212", "synthetic")]))
    return len(trips)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    osm, halt_pos = build_osm()
    with open(OUT / "osm_synthetic.json", "w") as f:
        json.dump(osm.to_overpass(), f)
    n = build_gtfs(halt_pos, OUT / "gtfs_synthetic.zip")
    print(f"synthetic OSM: {len(osm.ways)} ways, {len(osm.nodes)} nodes; GTFS: {n} trips -> {OUT}")


if __name__ == "__main__":
    main()
