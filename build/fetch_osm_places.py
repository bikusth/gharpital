#!/usr/bin/env python3
"""
Import healthcare places and a gazetteer of towns/villages for Nepal from OpenStreetMap
(via the Overpass API).

Writes:
  data/sources/osm_places.csv      hospitals, clinics, dentists, optometry, specialized
                                   hospitals, pharmacies and Ayurveda centres, each with a
                                   category (and level / specialty for hospitals)
  data/sources/osm_localities.csv  cities, towns, villages, municipalities... with their
                                   district, used to place doctors by registered address

Province and district come from each point's coordinates (data/geo/districts.geojson).
The build merges osm_places.csv with data/hospitals.csv - your rows there always win.

OpenStreetMap data (c) OpenStreetMap contributors, ODbL.

Run:  python build/fetch_osm_places.py               (needs internet; used by GitHub Actions)
      python build/fetch_osm_places.py --from-json places.json --localities-json loc.json  (offline)
"""
import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
from fetch_osm_hospitals import ENDPOINTS, load_districts, locate  # noqa: E402  (shared helpers)

OUT_PLACES = ROOT / "data" / "sources" / "osm_places.csv"
OUT_LOCALITIES = ROOT / "data" / "sources" / "osm_localities.csv"

PLACES_QUERY = """[out:json][timeout:300];
area["ISO3166-1"="NP"][admin_level=2]->.np;
(
  nwr["amenity"~"^(hospital|clinic|doctors|dentist|pharmacy)$"](area.np);
  nwr["healthcare"~"^(hospital|clinic|doctor|dentist|pharmacy|optometrist|alternative|centre|rehabilitation)$"](area.np);
  nwr["shop"~"^(optician|chemist)$"](area.np);
  nwr["name"~"[Aa]yurved|आयुर्वेद|औषधालय"](area.np);
);
out center tags;"""

LOCALITIES_QUERY = """[out:json][timeout:300];
area["ISO3166-1"="NP"][admin_level=2]->.np;
(
  node["place"~"^(city|town|village|hamlet|suburb|quarter|neighbourhood|locality)$"](area.np);
  rel["boundary"="administrative"]["admin_level"~"^(6|7|8)$"](area.np);
);
out center tags;"""

PLACE_FIELDS = ["hospital_id", "name", "name_ne", "category", "level", "specialty", "ownership", "province",
                "district", "address", "lat", "lng", "phone", "website", "opening_hours", "emergency", "source"]
LOCALITY_FIELDS = ["name", "name_en", "name_ne", "kind", "district", "province", "lat", "lng"]

# ---------------- classification ----------------
SPECIALTIES = [  # (specialty, keywords in name or healthcare:speciality)
    ("Cancer", r"cancer|oncolog|tumou?r|क्यान्सर"),
    ("Kidney", r"kidney|nephro|dialysis|urolog|मृगौला|मिर्गौला"),
    ("Heart", r"heart|cardi[ao]|gangalal|मुटु|हृदय"),
    ("Orthopaedic", r"ortho|bone|spine|हाड|अर्थो"),
    ("Children's", r"child|kanti|p(a)?ediatr|बाल अस्पताल|बाल रोग|शिशु"),
    ("Maternity", r"maternit|prasuti|प्रसूति|women'?s hospital|birthing|matri"),
]
MAJOR = (r"teaching|medical college|institute of medicine|academy of health|health sciences|"
         r"central hospital|bir hospital|tribhuvan university|tuth|b\.? ?p\.? koirala|bpkihs|patan hospital|"
         r"civil service hospital|police hospital|army hospital|birendra (military|sainik)|शिक्षण अस्पताल|"
         r"केन्द्रीय अस्पताल|मेडिकल कलेज|प्रतिष्ठान")
PROVINCIAL = r"provincial hospital|pradesh|प्रदेश|regional hospital|zonal hospital|sub-?regional|क्षेत्रीय|अञ्चल"
DISTRICT = r"district hospital|jilla|जिल्ला अस्पताल|primary hospital|community hospital|प्राथमिक अस्पताल|सामुदायिक अस्पताल"
PRIMARY_CARE = r"health ?post|health cent(er|re)|primary health|phc\b|स्वास्थ्य चौकी|स्वास्थ्य केन्द्र|हेल्थ पोष्ट"


NOT_CARE = r"basic science|hostel|quarters?\b|staff housing|nursing college|college of nursing|campus\b|office of|warehouse|store ?room"


def classify(t, name):
    """Return (category, level, specialty) for an OSM element's tags."""
    # healthcare:speciality counts only when a single speciality is listed: general hospitals
    # often list many ("general;paediatrics;gynaecology") and are not specialized hospitals
    spec_tag = t.get("healthcare:speciality", "")
    spec_tag = spec_tag if spec_tag and ";" not in spec_tag else ""
    n = f"{name} {t.get('name:en', '')} {t.get('name:ne', '')} {spec_tag}".lower()
    if re.search(NOT_CARE, f"{name} {t.get('name:en', '')}".lower()):
        return "", "", ""
    amenity, hc, shop = t.get("amenity", ""), t.get("healthcare", ""), t.get("shop", "")
    alt = (t.get("healthcare:alternative", "") + " " + spec_tag).lower()

    if "ayurved" in alt or re.search(r"ayurved|आयुर्वेद|औषधालय", n):
        return "ayurveda", "", ""
    if amenity == "pharmacy" or hc == "pharmacy" or shop == "chemist":
        return "pharmacy", "", ""
    if hc == "alternative":
        return "", "", ""  # other alternative medicine: not listed for now
    if amenity == "dentist" or hc == "dentist" or re.search(r"dental|dentist|दन्त|दाँत", n):
        return "dentist", "", ""
    if hc == "optometrist" or shop == "optician" or re.search(
            r"\beye\b|netra|नेत्र|aankha|आँखा|optical|optic|vision|lasik|ophthalm|optometr", n):
        return "optometry", "", ""
    is_hospital = amenity == "hospital" or hc == "hospital" or re.search(r"hospital|अस्पताल|हस्पिटल", n)
    for spec, pat in SPECIALTIES:
        if re.search(pat, n) and (is_hospital or amenity in ("clinic", "doctors") or hc in ("clinic", "doctor", "centre")):
            return "specialized", "", spec
    if is_hospital and not re.search(PRIMARY_CARE, n):
        if re.search(MAJOR, n):
            level = "Major"
        elif re.search(PROVINCIAL, n):
            level = "Provincial"
        elif re.search(DISTRICT, n):
            level = "District"
        else:
            level = ""
        return "hospital", level, ""
    if amenity in ("clinic", "doctors") or hc in ("clinic", "doctor", "centre", "rehabilitation") or re.search(PRIMARY_CARE, n):
        return "clinic", "Health post" if re.search(PRIMARY_CARE, n) else "", ""
    return "", "", ""


def ownership(tags):
    v = (tags.get("operator:type") or tags.get("ownership") or "").lower()
    if v in ("government", "public", "state"):
        return "Government"
    if v in ("community", "ngo", "religious"):
        return {"ngo": "NGO", "community": "Community", "religious": "Religious"}[v]
    if v.startswith("private"):
        return "Private"
    return ""


# ---------------- fetch ----------------
def overpass(query, label):
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for rnd in range(3):
        for url in ENDPOINTS:
            try:
                print(f"{label}: round {rnd + 1}, asking {url}", flush=True)
                req = urllib.request.Request(url, data=body, headers={
                    "User-Agent": "Gharpital/1.0 (github.com/bikusth/gharpital)",
                    "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=330) as r:
                    data = json.load(r)
                if data.get("elements"):
                    return data
                last = RuntimeError("empty response: " + str(data.get("remark", ""))[:200])
            except Exception as e:
                last = e
            print(f"  failed: {last}", flush=True)
            time.sleep(5)
        if rnd < 2:
            print("All servers busy; waiting 60 s before trying again", flush=True)
            time.sleep(60)
    raise SystemExit(f"Could not reach any Overpass server for {label}: {last}")


def coords(e):
    lat = e.get("lat") or (e.get("center") or {}).get("lat")
    lng = e.get("lon") or (e.get("center") or {}).get("lon")
    return lat, lng


def build_places(data, districts):
    rows, seen = [], set()
    for e in data.get("elements", []):
        t = e.get("tags", {})
        lat, lng = coords(e)
        display = (t.get("name:en") or t.get("name") or "").strip()
        if not display or lat is None:
            continue
        cat, level, spec = classify(t, display)
        if not cat:
            continue
        province, district = locate(districts, lat, lng)
        if not province:
            continue
        pid = f"osm-{e['type'][0]}{e['id']}"
        if pid in seen:
            continue
        seen.add(pid)
        name = t.get("name") or ""
        website = t.get("website") or t.get("contact:website") or ""
        if website and not website.startswith("http"):
            website = "https://" + website
        rows.append({
            "hospital_id": pid, "name": display, "name_ne": (t.get("name:ne") or (name if name != display else "")).strip(),
            "category": cat, "level": level, "specialty": spec, "ownership": ownership(t),
            "province": province, "district": district,
            "address": ", ".join(x for x in [t.get("addr:street"), t.get("addr:city") or t.get("addr:place")] if x),
            "lat": round(lat, 6), "lng": round(lng, 6),
            "phone": t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or "",
            "website": website, "opening_hours": t.get("opening_hours", ""),
            "emergency": "yes" if t.get("emergency") == "yes" or t.get("opening_hours") == "24/7" else "",
            "source": "OpenStreetMap",
        })
    rows = dedupe(rows)
    rows.sort(key=lambda r: (r["province"], r["district"], r["category"], r["name"]))
    return rows


def dedupe(rows):
    """The same place is often mapped twice (a point and a building outline). Keep one per
    same-name, same-category group within ~300 m, preferring the one with more details."""
    def key(r):
        return (re.sub(r"[^a-z0-9\u0900-\u097f]", "", r["name"].lower()), r["category"])
    detail = lambda r: sum(bool(r[k]) for k in ("phone", "website", "opening_hours", "address", "name_ne", "level"))
    groups, kept = {}, []
    for r in sorted(rows, key=detail, reverse=True):
        near = [k for k in groups.get(key(r), []) if abs(k["lat"] - r["lat"]) < 0.003 and abs(k["lng"] - r["lng"]) < 0.003]
        if near:
            for f in ("phone", "website", "opening_hours", "address", "name_ne", "emergency"):
                near[0][f] = near[0][f] or r[f]  # merge any extra details into the one we keep
            continue
        groups.setdefault(key(r), []).append(r)
        kept.append(r)
    return kept


KIND_RANK = {"city": 0, "town": 1, "admin": 2, "suburb": 3, "village": 4, "quarter": 5, "neighbourhood": 6,
             "hamlet": 7, "locality": 8}


def build_localities(data, districts):
    rows, seen = [], set()
    for e in data.get("elements", []):
        t = e.get("tags", {})
        lat, lng = coords(e)
        if lat is None:
            continue
        kind = t.get("place") or ("admin" if t.get("boundary") == "administrative" else "")
        names = {x.strip() for x in (t.get("name"), t.get("name:en"), t.get("int_name"), t.get("old_name"),
                                     t.get("alt_name")) if x}
        if not kind or not names:
            continue
        province, district = locate(districts, lat, lng)
        if not province:
            continue
        for nm in names:
            for part in re.split(r";", nm):
                part = re.sub(r"\s+(rural municipality|municipality|metropolitan city|sub-metropolitan city|"
                              r"gaunpalika|nagarpalika)$", "", part.strip(), flags=re.I)
                key = (part.lower(), district)
                if not part or key in seen:
                    continue
                seen.add(key)
                rows.append({"name": part, "name_en": t.get("name:en", ""), "name_ne": t.get("name:ne", ""),
                             "kind": kind, "district": district, "province": province,
                             "lat": round(lat, 4), "lng": round(lng, 4)})
    rows.sort(key=lambda r: (r["district"], KIND_RANK.get(r["kind"], 9), r["name"]))
    return rows


def write(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="saved Overpass response for places")
    ap.add_argument("--localities-json", help="saved Overpass response for localities")
    a = ap.parse_args()
    districts = load_districts()
    places = json.loads(Path(a.from_json).read_text()) if a.from_json else overpass(PLACES_QUERY, "Places")
    rows = build_places(places, districts)
    write(OUT_PLACES, PLACE_FIELDS, rows)
    from collections import Counter
    print("By category:", dict(Counter(r["category"] for r in rows)))
    print("Hospital levels:", dict(Counter(r["level"] or "unknown" for r in rows if r["category"] == "hospital")))
    if a.from_json and not a.localities_json:
        return
    loc = json.loads(Path(a.localities_json).read_text()) if a.localities_json else overpass(LOCALITIES_QUERY, "Localities")
    write(OUT_LOCALITIES, LOCALITY_FIELDS, build_localities(loc, districts))


if __name__ == "__main__":
    main()
