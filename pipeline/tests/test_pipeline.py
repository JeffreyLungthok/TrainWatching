#!/usr/bin/env python3
"""Checks for build_data.py. Run from the pipeline folder:

    python tests/make_synthetic.py
    python build_data.py --gtfs tests/out/gtfs_synthetic.zip --osm-json tests/out/osm_synthetic.json \
        --start 2026-09-24 --days 7 --out tests/out/data
    python tests/test_pipeline.py
"""
import base64
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_data as bd  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def decode_leg(a):
    q = np.cumsum(np.asarray(a, float).reshape(-1, 2), axis=0) / 1e5
    return q  # lon, lat


def max_turn_deg(lonlat):
    x, y = bd.project(lonlat[:, 0], lonlat[:, 1])
    v = np.diff(np.c_[x, y], axis=0)
    L = np.hypot(v[:, 0], v[:, 1])
    v = v[L > 0.5] / L[L > 0.5][:, None]
    if len(v) < 2:
        return 0.0
    cos = np.clip((v[:-1] * v[1:]).sum(axis=1), -1, 1)
    return float(np.degrees(np.arccos(cos)).max())


def length_m(lonlat):
    x, y = bd.project(lonlat[:, 0], lonlat[:, 1])
    return float(np.hypot(np.diff(x), np.diff(y)).sum())


def synthetic_output():
    print("Synthetic end-to-end output")
    net = json.load(open(HERE / "out" / "data" / "network.json"))
    tt = json.load(open(HERE / "out" / "data" / "timetable.json"))
    stops = net["stops"]
    idx = {}
    for i, (name, lon, lat) in enumerate(stops):
        idx.setdefault(name, []).append(i)
    legs = [decode_leg(a) for a in net["legs"]]

    def leg_between(na, nb, which=0):
        a, b = idx[na][which], idx[nb][0]
        for p_stops, refs in tt["patterns"]:
            for (s1, s2), r in zip(zip(p_stops[:-1], p_stops[1:]), refs):
                if (s1, s2) == (a, b):
                    g = legs[abs(r) - 1]
                    return g if r > 0 else g[::-1]
        return None

    check(all(r[2] != 700 for r in tt["routes"]), "bus route is filtered out")
    check(len(tt["routes"]) == 10, f"10 rail routes kept (got {len(tt['routes'])})")

    turns = [max_turn_deg(g) for g in legs if len(g) > 2]
    check(max(turns) < 90, f"no leg contains a turn sharper than 90 deg (max {max(turns):.1f})")

    g = leg_between("Basel SBB", "Olten")
    liestal = np.array(stops[idx["Liestal"][0]][1:])
    dmin = min(math.dist(liestal, p) for p in g) * 111_000 if g is not None else 1e9
    check(g is not None and dmin < 400, f"Basel->Olten follows the tracks via Liestal, ignoring the yard shortcut ({dmin:.0f} m)")

    # the Zentralbahn platform in Luzern is closer to the standard-gauge tracks; it must still
    # be routed on the separate metre-gauge network
    luz = [i for i in idx["Luzern"]]
    zb_leg = None
    for p_stops, refs in tt["patterns"]:
        if stops[p_stops[1]][0] == "Sarnen" and p_stops[0] in luz:
            r = refs[0]
            zb_leg = legs[abs(r) - 1] if r > 0 else legs[abs(r) - 1][::-1]
    crow = math.dist(stops[luz[-1]][1:], stops[idx["Sarnen"][0]][1:]) * 80_000
    check(zb_leg is not None and len(zb_leg) > 5, "Luzern (zb platform) -> Sarnen is routed on the metre-gauge track")
    if zb_leg is not None:
        zb_node = bd.project(8.3114, 47.0500)
        start = bd.project(*zb_leg[0])
        check(math.dist(zb_node, start) < 30, f"  ... and starts at the zb tracks ({math.dist(zb_node, start):.0f} m from zb station)")

    for a, b in (("Langenthal", "Burgdorf"), ("Burgdorf", "Bern")):
        g = leg_between(a, b)
        check(g is not None and len(g) > 5, f"{a} -> {b} routed on tracks (halt between the tracks)")

    g = leg_between("Zürich HB", "Milano Centrale")
    check(g is not None and len(g) == 2, "Zürich -> Milano (no tracks in data) falls back to a straight line")

    g = leg_between("Zürich HB", "Aarau")
    check(g is not None and len(g) > 5 and max_turn_deg(g) < 90, "Zürich HB is not captured by the nearby tram ring")

    # calendar
    services = [np.unpackbits(np.frombuffer(base64.b64decode(s), np.uint8), bitorder="little") for s in tt["services"]]
    routes = tt["routes"]
    trips = tt["trips"]

    def running(label, day):
        n = 0
        for t_start, svc, route in zip(trips["start"], trips["service"], trips["route"]):
            if routes[route][0] == label and services[svc][day]:
                n += 1
        return n
    check(tt["meta"]["windowStart"] == "2026-09-24", "window starts on 2026-09-24")
    check(running("S23", 0) > 0 and running("S23", 1) == 0, "calendar_dates removal: S23 runs 24 Sep, not 25 Sep")
    check(running("RE", 1) > 0 and running("RE", 2) > 0 and running("RE", 3) == 0, "calendar_dates-only service: RE runs 25+26 Sep only")
    check(running("IC5", 1) > 0 and running("IC5", 2) == 1, "weekday service: IC5 on Friday, only the daily night train on Saturday")
    brb = [s for s, r in zip(trips["start"], trips["route"]) if routes[r][1] == "PE"]
    check(len(brb) == 18 and min(brb) == 8.5 * 3600, f"frequencies.txt expanded to 18 rack-railway trips (got {len(brb)})")
    late = [(s, t) for s, t, r in zip(trips["start"], trips["timing"], trips["route"]) if s == 23 * 3600 + 2400]
    ok = False
    if late:
        rel = np.cumsum(tt["timings"][late[0][1]][1:])
        ok = late[0][0] + rel[-1] > 24 * 3600
    check(ok, "trip departing 23:40 ends after 24:00 (service-day time kept)")


def micro_turn_restriction():
    """Triangle junction with a balloon loop on its tail.

    A train from the east end of the main line to a stop on the western curve of the triangle
    cannot simply run to the western junction and back up the curve (that is a reversal). The
    only legal way is via the eastern curve, up the tail, round the balloon loop and back down.
    """
    print("Turn restriction micro test")
    pts, ways = {}, []
    nid = [0]

    def node(x, y):
        nid[0] += 1
        pts[nid[0]] = (x, y)
        return nid[0]

    def arc(cx, cy, r, a0, a1, n):
        return [node(cx + r * math.cos(a), cy + r * math.sin(a)) for a in np.linspace(a0, a1, n)]

    d = math.radians
    main = [node(x, 0) for x in np.arange(-4000, 3001, 50)]
    J1 = main[list(np.arange(-4000, 3001, 50)).index(-1000)]
    J2 = main[list(np.arange(-4000, 3001, 50)).index(1000)]
    A = node(0, 1000)
    curve1 = [J1] + arc(-1000, 1000, 1000, d(-85), d(-5), 17) + [A]      # west curve
    curve2 = [J2] + arc(1000, 1000, 1000, d(265), d(185), 17) + [A]      # east curve
    tail = [A] + [node(0, y) for y in np.arange(1050, 2001, 50)]
    Bn = tail[-1]
    # teardrop balloon loop at the end of the tail
    B = np.array(pts[Bn])
    P1 = B + 300 * np.array([math.cos(d(105)), math.sin(d(105))])
    P2 = B + 300 * np.array([math.cos(d(75)), math.sin(d(75))])
    M, r = (P1 + P2) / 2, np.hypot(*(P1 - P2)) / 2
    a1 = math.atan2(*(P1 - M)[::-1])
    loop = ([node(*(B + (P1 - B) * t)) for t in np.linspace(0.1, 0.95, 8)]
            + arc(M[0], M[1], r, a1, a1 - math.pi, 19)
            + [node(*(B + (P2 - B) * t)) for t in np.linspace(0.95, 0.1, 8)])
    ways += [main, curve1, curve2, tail, [Bn] + loop + [Bn]]
    ids = np.array(sorted(pts))
    xs = np.array([pts[i][0] for i in ids]); ys = np.array([pts[i][1] for i in ids])
    lon, lat = bd.unproject(xs, ys)
    net = bd.RailNetwork(ids, lon, lat, [np.array(w) for w in ways])
    T = (-1000 + 1000 * math.cos(d(-40)), 1000 + 1000 * math.sin(d(-40)))
    sx, sy = np.array([3000.0, T[0]]), np.array([0.0, T[1]])
    cands = net.snap(sx, sy)
    net.build({n for c in cands for n, _ in c})
    targets = {1: (cands[1], math.hypot(3000 - T[0], T[1]))}
    r_turn = net.route_from(cands[0], targets, restricted=True)
    r_free = net.route_from(cands[0], targets, restricted=False)

    def path_turn(res):
        p = res[1][0]
        return max_turn_deg(np.c_[bd.unproject(net.x[p], net.y[p])])

    def path_len(res):
        p = res[1][0]
        return float(np.hypot(np.diff(net.x[p]), np.diff(net.y[p])).sum())
    check(1 in r_free and path_turn(r_free) > 120,
          f"unrestricted routing reverses at the junction (the problem): {path_len(r_free):.0f} m")
    check(1 in r_turn and path_turn(r_turn) < 90,
          "turn-restricted routing takes the legal way round the balloon loop"
          + (f": {path_len(r_turn):.0f} m, max turn {path_turn(r_turn):.0f} deg" if 1 in r_turn else ""))


def micro_tram_penalty():
    """A tram line is a shortcut between two stations; trains must still use the railway."""
    print("Tram penalty micro test")

    def run(with_rail):
        pts, ways, costs = {}, [], []
        nid = [0]

        def node(x, y):
            nid[0] += 1
            pts[nid[0]] = (x, y)
            return nid[0]
        A, B = node(0, 0), node(4000, 0)
        tram = [A] + [node(x, 0) for x in np.arange(100, 4000, 100)] + [B]
        ways.append(tram); costs.append(bd.TRAM_COST)
        if with_rail:
            arc = [node(2000 - 2000 * math.cos(a), 1500 * math.sin(a)) for a in np.linspace(0.05, math.pi - 0.05, 60)]
            ways.append([A] + arc + [B]); costs.append(1.0)
        ids = np.array(sorted(pts))
        lon, lat = bd.unproject(np.array([pts[i][0] for i in ids]), np.array([pts[i][1] for i in ids]))
        net = bd.RailNetwork(ids, lon, lat, [np.array(w) for w in ways], costs)
        cands = net.snap(np.array([0.0, 4000.0]), np.array([-5.0, -5.0]))
        net.build({n for c in cands for n, _ in c})
        res = net.route_from(cands[0], {1: (cands[1], 4000.0)})
        p = res[1][0]
        return float(np.max(net.y[p]))
    check(run(True) > 1000, "with a railway available, the train ignores the tram shortcut")
    check(run(False) < 1, "without a railway, the tram track is used")


if __name__ == "__main__":
    synthetic_output()
    micro_turn_restriction()
    micro_tram_penalty()
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)
