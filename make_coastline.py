#!/usr/bin/env python3
"""Build coastline.json for the radar from Natural Earth's 10m coastline.

Keeps only the coast within --radius-km of the location in config.json, thins
out points that are very close together, and writes compact [lat, lon] lines.
Re-run this if you change your location.
"""

import argparse
import json
import math
import os
import urllib.request

from flight_near_me import HERE, KM_PER_DEG_LAT, SSL_CONTEXT, haversine_km, load_config

SOURCE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
              "master/geojson/ne_10m_coastline.geojson")


def load_geojson(path):
    if path:
        with open(path) as f:
            return json.load(f)
    print(f"Downloading {SOURCE_URL} ...")
    with urllib.request.urlopen(SOURCE_URL, timeout=120, context=SSL_CONTEXT) as r:
        return json.load(r)


def iter_lines(geojson):
    for feat in geojson["features"]:
        geom = feat.get("geometry") or {}
        if geom.get("type") == "LineString":
            yield geom["coordinates"]
        elif geom.get("type") == "MultiLineString":
            yield from geom["coordinates"]


def clip_and_thin(lines, lat0, lon0, radius_km, min_step_km):
    dlat = radius_km / KM_PER_DEG_LAT
    dlon = radius_km / (KM_PER_DEG_LAT * math.cos(math.radians(lat0)))
    inside = lambda lat, lon: abs(lat - lat0) <= dlat and abs(lon - lon0) <= dlon  # noqa: E731

    out = []
    for coords in lines:
        run = []
        prev = None  # last point outside the box, so lines reach the edge
        for lon, lat in coords:
            if inside(lat, lon):
                if not run and prev:
                    run.append(prev)
                if not run or haversine_km(run[-1][0], run[-1][1], lat, lon) >= min_step_km:
                    run.append((lat, lon))
                prev = None
            else:
                if run:
                    run.append((lat, lon))
                    out.append(run)
                    run = []
                prev = (lat, lon)
        if len(run) > 1:
            out.append(run)
    return [[[round(lat, 4), round(lon, 4)] for lat, lon in run] for run in out if len(run) > 1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--input", help="local ne_10m_coastline.geojson (otherwise downloaded)")
    ap.add_argument("--radius-km", type=float, default=450)
    ap.add_argument("--min-step-km", type=float, default=0.25)
    ap.add_argument("--output", default=os.path.join(HERE, "coastline.json"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    lines = clip_and_thin(iter_lines(load_geojson(args.input)), cfg["latitude"],
                          cfg["longitude"], args.radius_km, args.min_step_km)
    with open(args.output, "w") as f:
        json.dump({"source": "Natural Earth 10m coastline (public domain)",
                   "center": [cfg["latitude"], cfg["longitude"]],
                   "lines": lines}, f, separators=(",", ":"))
    print(f"Wrote {args.output}: {len(lines)} lines, {sum(map(len, lines))} points")


if __name__ == "__main__":
    main()
