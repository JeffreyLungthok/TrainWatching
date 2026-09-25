#!/usr/bin/env python3
"""Convert the swiss-maps TopoJSON (npm package `swiss-maps`, data (c) swisstopo) into the small
GeoJSON background used by the web viewer (web/basemap.json).

    npm pack swiss-maps && tar xzf swiss-maps-*.tgz
    python make_basemap.py package/2026/ch-combined.json ../../web/basemap.json

Only needs to be re-run if you want newer boundaries; the result is committed.
"""
import json
import sys

import numpy as np


def rdp(pts, eps):
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = pts[j] - pts[i]
        L = np.hypot(*seg)
        rel = pts[i + 1:j] - pts[i]
        d = np.hypot(rel[:, 0], rel[:, 1]) if L == 0 else np.abs(rel[:, 0] * seg[1] - rel[:, 1] * seg[0]) / L
        k = int(np.argmax(d))
        if d[k] > eps:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return pts[keep]


def main(src, dst, eps=0.0006):
    topo = json.load(open(src))
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        a = np.cumsum(np.asarray(arc, float), axis=0)
        arcs.append(np.c_[a[:, 0] * sx + tx, a[:, 1] * sy + ty])
    arcs = [rdp(a, eps) for a in arcs]

    def ring(idx):
        out = []
        for i in idx:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(a.tolist() if not out else a[1:].tolist())
        return [[round(x, 4), round(y, 4)] for x, y in out]

    def features(name):
        feats = []
        for g in topo["objects"][name]["geometries"]:
            if g["type"] == "Polygon":
                polys = [g["arcs"]]
            elif g["type"] == "MultiPolygon":
                polys = g["arcs"]
            else:
                continue
            coords = [[ring(r) for r in poly] for poly in polys]
            coords = [p for p in coords if len(p[0]) >= 4]
            if coords:
                feats.append({"type": "Feature", "properties": {"layer": name},
                              "geometry": {"type": "MultiPolygon", "coordinates": coords}})
        return feats

    out = {"type": "FeatureCollection",
           "features": features("country") + features("lakes") + features("cantons")}
    with open(dst, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {dst}: {len(out['features'])} features")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
