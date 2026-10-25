#!/usr/bin/env python3
"""
Import Nepal's hospitals from OpenStreetMap (via the Overpass API) into
data/sources/osm_hospitals.csv, with province and district worked out from each
hospital's coordinates using data/geo/districts.geojson.

The build merges this file with data/hospitals.csv. Rows in hospitals.csv win:
to fix an OSM hospital, copy its row (same hospital_id, e.g. osm-n943246318)
into hospitals.csv and edit it there; to hide one, add it there with type "hide".

OpenStreetMap data (c) OpenStreetMap contributors, ODbL - the app credits it on the map.

Run:  python build/fetch_osm_hospitals.py            (needs internet; used by GitHub Actions)
      python build/fetch_osm_hospitals.py --from-json overpass.json   (offline, from a saved response)
"""
import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEO = ROOT / "data" / "geo" / "districts.geojson"
OUT = ROOT / "data" / "sources" / "osm_hospitals.csv"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
QUERY = """[out:json][timeout:180];
area["ISO3166-1"="NP"][admin_level=2]->.np;
(nwr["amenity"="hospital"](area.np); nwr["healthcare"="hospital"](area.np););
out center tags;"""
FIELDS = ["hospital_id", "name", "name_ne", "type", "ownership", "province", "district", "address",
          "lat", "lng", "phone", "website", "source"]


# ---------- point-in-polygon (no external libraries) ----------
def load_districts():
    gj = json.loads(GEO.read_text())
    out = []
    for f in gj["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        xs = [pt[0] for poly in polys for ring in poly for pt in ring]
        ys = [pt[1] for poly in polys for ring in poly for pt in ring]
        out.append((f["properties"], polys, (min(xs), min(ys), max(xs), max(ys))))
    return out


def in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def locate(districts, lat, lng):
    best = None
    for props, polys, (x0, y0, x1, y1) in districts:
        if not (x0 <= lng <= x1 and y0 <= lat <= y1):
            continue
        for poly in polys:
            if in_ring(lng, lat, poly[0]) and not any(in_ring(lng, lat, h) for h in poly[1:]):
                return props["province"], props["district"]
        best = best or props  # bounding-box hit: fallback for points on simplified borders
    return (best["province"], best["district"]) if best else ("", "")


# ---------- fetch ----------
def fetch():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    last = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "Gharpital/1.0 (github.com/bikusth/gharpital)"})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)
        except Exception as e:  # try the next mirror
            last = e
            print(f"{url} failed: {e}", file=sys.stderr)
    raise SystemExit(f"Could not reach Overpass: {last}")


def ownership(tags):
    v = (tags.get("operator:type") or tags.get("ownership") or "").lower()
    if v in ("government", "public", "state"):
        return "Government"
    if v in ("private", "private_for_profit", "private_non_profit", "ngo", "community", "religious"):
        return {"ngo": "NGO", "community": "Community", "religious": "Religious"}.get(v, "Private")
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="read a saved Overpass response instead of fetching")
    a = ap.parse_args()
    data = json.loads(Path(a.from_json).read_text()) if a.from_json else fetch()
    districts = load_districts()

    rows = []
    for e in data.get("elements", []):
        t = e.get("tags", {})
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        name_en = t.get("name:en") or ""
        name = t.get("name") or ""
        # prefer an English name for the list; keep the Nepali one alongside
        display = name_en or name
        if not display or lat is None:
            continue
        name_ne = t.get("name:ne") or (name if name != display else "")
        province, district = locate(districts, lat, lng)
        if not province:
            continue  # outside Nepal's districts (border noise)
        addr = ", ".join(x for x in [t.get("addr:street"), t.get("addr:city") or t.get("addr:place")] if x)
        website = t.get("website") or t.get("contact:website") or ""
        if website and not website.startswith("http"):
            website = "https://" + website
        rows.append({
            "hospital_id": f"osm-{e['type'][0]}{e['id']}",
            "name": display.strip(), "name_ne": name_ne.strip(), "type": "Hospital",
            "ownership": ownership(t), "province": province, "district": district, "address": addr,
            "lat": round(lat, 6), "lng": round(lng, 6),
            "phone": t.get("phone") or t.get("contact:phone") or "", "website": website,
            "source": "OpenStreetMap",
        })

    rows.sort(key=lambda r: (r["province"], r["district"], r["name"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} hospitals to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
