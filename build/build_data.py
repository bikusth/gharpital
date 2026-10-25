#!/usr/bin/env python3
"""
Gharpital data build.

Inputs (all CSV, editable on github.com):
  data/raw/nmc_doctors.csv   - written by the NMC scraper (do not hand-edit)
  data/hospitals.csv         - hospitals with province, district and coordinates
  data/doctor_hospital.csv   - which doctor (NMC no.) works at which hospital
  data/corrections.csv       - hand fixes; always applied last, always win

Outputs (read by the app; regenerated on every build, never hand-edit):
  data/build/meta.json                 - counts, provinces -> districts, specialties
  data/build/hospitals.json            - all hospitals
  data/build/province/<Province>.json  - doctors working in that province
  data/build/doctors-index.json        - every doctor, compact, for name/NMC search
  data/export/gharpital.xlsx           - Excel copy for browsing

Run:  python build/build_data.py
"""
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "build"
EXPORT = DATA / "export"

PROVINCES = {
    "Koshi": ["Bhojpur", "Dhankuta", "Ilam", "Jhapa", "Khotang", "Morang", "Okhaldhunga",
              "Panchthar", "Sankhuwasabha", "Solukhumbu", "Sunsari", "Taplejung", "Terhathum", "Udayapur"],
    "Madhesh": ["Bara", "Dhanusha", "Mahottari", "Parsa", "Rautahat", "Saptari", "Sarlahi", "Siraha"],
    "Bagmati": ["Bhaktapur", "Chitwan", "Dhading", "Dolakha", "Kathmandu", "Kavrepalanchok", "Lalitpur",
                "Makwanpur", "Nuwakot", "Ramechhap", "Rasuwa", "Sindhuli", "Sindhupalchok"],
    "Gandaki": ["Baglung", "Gorkha", "Kaski", "Lamjung", "Manang", "Mustang", "Myagdi", "Nawalpur",
                "Parbat", "Syangja", "Tanahun"],
    "Lumbini": ["Arghakhanchi", "Banke", "Bardiya", "Dang", "Gulmi", "Kapilvastu", "Palpa", "Parasi",
                "Pyuthan", "Rolpa", "Rukum East", "Rupandehi"],
    "Karnali": ["Dailekh", "Dolpa", "Humla", "Jajarkot", "Jumla", "Kalikot", "Mugu", "Rukum West",
                "Salyan", "Surkhet"],
    "Sudurpashchim": ["Achham", "Baitadi", "Bajhang", "Bajura", "Dadeldhura", "Darchula", "Doti",
                      "Kailali", "Kanchanpur"],
}
DISTRICT_TO_PROVINCE = {d: p for p, ds in PROVINCES.items() for d in ds}
# Common alternative spellings -> canonical
DISTRICT_ALIASES = {
    "chitawan": "Chitwan", "kabhrepalanchok": "Kavrepalanchok", "kavre": "Kavrepalanchok",
    "kapilbastu": "Kapilvastu", "makawanpur": "Makwanpur", "tanahu": "Tanahun",
    "nawalparasi east": "Nawalpur", "nawalparasi west": "Parasi", "sindhupalchowk": "Sindhupalchok",
    "tehrathum": "Terhathum", "dhanusa": "Dhanusha", "rukum (east)": "Rukum East",
    "rukum (west)": "Rukum West", "eastern rukum": "Rukum East", "western rukum": "Rukum West",
}

# Specialty categories shown in the app filter. First matching pattern wins.
SPECIALTY_RULES = [
    ("Cardiology", r"cardi"),
    ("Neurology", r"neurolog(?!.*surg)"),
    ("Neurosurgery", r"neuro.?surg"),
    ("Orthopedics", r"ortho"),
    ("Obstetrics & Gynecology", r"gyn|obstet|\bobg|ob/?gyn"),
    ("Pediatrics", r"p(a)?ediatric|child health"),
    ("Dermatology", r"dermat|venereo|skin"),
    ("Ophthalmology", r"ophthal|\beye\b"),
    ("ENT", r"\bent\b|otorhino|oto-rhino|head and neck"),
    ("Psychiatry", r"psychiat"),
    ("Radiology", r"radio(logy|diag)|imaging"),
    ("Anesthesiology", r"an(a)?esth"),
    ("Pathology", r"patholog"),
    ("Oncology", r"oncolog|cancer"),
    ("Nephrology", r"nephro"),
    ("Urology", r"urolog"),
    ("Gastroenterology", r"gastro|hepato"),
    ("Pulmonology", r"pulmon|chest|respirat"),
    ("Endocrinology", r"endocrin|diabet"),
    ("General Surgery", r"surg"),
    ("Internal Medicine", r"internal medicine|general medicine|\bmd\b.*medicine"),
    ("Emergency Medicine", r"emergency"),
    ("Family Medicine", r"family|general practice|\bgp\b|mdgp"),
    ("Community Medicine", r"community|public health|preventive"),
    ("Microbiology", r"microbio"),
    ("Biochemistry", r"biochem"),
    ("Pharmacology", r"pharmacol"),
    ("Forensic Medicine", r"forensic"),
    ("Physiology", r"physiolog"),
    ("Anatomy", r"anatom"),
    ("Dentistry", r"dent|\bbds\b|\bmds\b|oral|maxillofac|orthodon|periodon|prosthodon|endodon"),
]


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def norm_district(name):
    if not name:
        return ""
    key = name.strip().lower()
    if key in DISTRICT_ALIASES:
        return DISTRICT_ALIASES[key]
    for d in DISTRICT_TO_PROVINCE:
        if d.lower() == key:
            return d
    return name.strip()


def norm_province(name, district=""):
    if district and district in DISTRICT_TO_PROVINCE:
        return DISTRICT_TO_PROVINCE[district]
    key = (name or "").strip().lower()
    for p in PROVINCES:
        if p.lower() == key or key.startswith(p.lower()[:4]):
            return p
    return name.strip() if name else ""


def categorize(speciality, degree):
    """Map NMC's free-text speciality/degree to an app filter category."""
    text = f"{speciality} {degree}".lower()
    if speciality:
        for cat, pat in SPECIALTY_RULES:
            if re.search(pat, speciality.lower()):
                return cat
    for cat, pat in SPECIALTY_RULES:
        if re.search(pat, text):
            return cat
    if re.search(r"mbbs|equivalent to mbbs|\bmd\b", text):
        return "General Physician"
    return "Not listed"


def clean_name(name):
    name = re.sub(r"\s+", " ", name or "").strip()
    return name


def main():
    warnings = []
    raw = read_csv(DATA / "raw" / "nmc_doctors.csv")
    # OpenStreetMap import first, then your own hospitals.csv on top (same hospital_id = yours wins)
    merged = {h["hospital_id"]: h for h in read_csv(DATA / "sources" / "osm_hospitals.csv") if h.get("hospital_id")}
    for h in read_csv(DATA / "hospitals.csv"):
        if h.get("hospital_id"):
            merged[h["hospital_id"]] = {**merged.get(h["hospital_id"], {}), **{k: v for k, v in h.items() if v}}
    hospitals_rows = [h for h in merged.values() if h.get("type", "").lower() != "hide"]
    links_rows = read_csv(DATA / "doctor_hospital.csv")
    corrections = read_csv(DATA / "corrections.csv")

    # ---- doctors from NMC ----
    doctors = {}
    for r in raw:
        key = r.get("nmc_no", "")
        if not key:
            continue
        doctors[key] = {
            "nmc_no": key,
            "name": clean_name(r.get("full_name")),
            "gender": r.get("gender", ""),
            "degree": r.get("degree", ""),
            "speciality_raw": r.get("speciality", ""),
            "specialty": "",
            "photo_url": "",
            "bio": "",
            "remarks": r.get("remarks", ""),
            # NMC marks deregistered doctors (death, cancellation...) in Remarks; keep them out of the app
            "hidden": bool(re.search(r"dereg|cancel|struck|suspend|revok|death|deceased", r.get("remarks", ""), re.I)),
            "source": "Nepal Medical Council register",
            "last_seen": r.get("last_seen", ""),
        }

    # ---- hospitals ----
    hospitals = {}
    by_name = {}
    for h in hospitals_rows:
        hid = h.get("hospital_id")
        if not hid:
            continue
        district = norm_district(h.get("district"))
        province = norm_province(h.get("province"), district)
        if province not in PROVINCES:
            warnings.append(f"hospitals.csv: {hid} has unknown province/district '{h.get('province')}/{h.get('district')}'")
        try:
            lat = float(h["lat"]) if h.get("lat") else None
            lng = float(h["lng"]) if h.get("lng") else None
        except ValueError:
            lat = lng = None
            warnings.append(f"hospitals.csv: {hid} has invalid coordinates")
        hospitals[hid] = {
            "id": hid, "name": h.get("name", ""), "name_ne": h.get("name_ne", ""), "type": h.get("type", ""),
            "ownership": h.get("ownership", ""), "province": province, "district": district,
            "address": h.get("address", ""), "lat": lat, "lng": lng,
            "phone": h.get("phone", ""), "website": h.get("website", ""),
        }
        by_name[h.get("name", "").lower()] = hid

    def resolve_hospital(value):
        if value in hospitals:
            return value
        return by_name.get(value.lower())

    # ---- doctor <-> hospital links ----
    links = defaultdict(dict)  # nmc_no -> hospital_id -> link
    for l in links_rows:
        n, hid = l.get("nmc_no"), resolve_hospital(l.get("hospital_id", ""))
        if not n or not hid:
            warnings.append(f"doctor_hospital.csv: skipped row {l}")
            continue
        links[n][hid] = {"id": hid, "department": l.get("department", ""),
                         "opd_days": l.get("opd_days", ""), "opd_time": l.get("opd_time", ""),
                         "source": l.get("source", "")}

    # ---- corrections (always last, always win) ----
    last_hospital = {}
    for c in corrections:
        n, field, val = c.get("nmc_no", ""), c.get("field", "").lower(), c.get("new_value", "")
        if not n or not field:
            continue
        if n not in doctors:
            if n.upper().startswith("NEW") or field == "name":
                doctors[n] = {"nmc_no": n, "name": "", "gender": "", "degree": "", "speciality_raw": "",
                              "specialty": "", "photo_url": "", "bio": "", "hidden": False,
                              "source": "Added manually", "last_seen": ""}
            else:
                warnings.append(f"corrections.csv: NMC {n} not in register (field {field})")
                continue
        d = doctors[n]
        if field == "hide":
            d["hidden"] = val.lower() in ("yes", "y", "true", "1")
        elif field == "hospital":
            hid = resolve_hospital(val)
            if hid:
                links[n].setdefault(hid, {"id": hid, "department": "", "opd_days": "", "opd_time": "", "source": "correction"})
                last_hospital[n] = hid
            else:
                warnings.append(f"corrections.csv: hospital '{val}' not found in hospitals.csv")
        elif field == "remove_hospital":
            hid = resolve_hospital(val)
            links[n].pop(hid, None)
        elif field in ("department", "opd_days", "opd_time"):
            # applies to the hospital named just before it; if none, to every workplace
            target = last_hospital.get(n)
            for hid, link in links[n].items():
                if target is None or hid == target:
                    link[field] = val
        elif field in ("name", "gender", "degree", "specialty", "photo_url", "bio"):
            d[field] = val
        elif field == "speciality":
            d["specialty"] = val
        else:
            warnings.append(f"corrections.csv: unknown field '{field}'")

    # ---- finalize ----
    for n, d in doctors.items():
        if not d["specialty"]:
            d["specialty"] = categorize(d["speciality_raw"], d["degree"])
        d["hospitals"] = list(links.get(n, {}).values())

    visible = [d for d in doctors.values() if not d["hidden"] and d["name"]]
    visible.sort(key=lambda d: (int(re.sub(r"\D", "", d["nmc_no"]) or 10**9), d["nmc_no"]))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "province").mkdir(exist_ok=True)
    for old in (OUT / "province").glob("*.json"):
        old.unlink()

    # per-province files: doctors with at least one hospital in that province
    per_prov = defaultdict(list)
    district_counts = defaultdict(Counter)
    for d in visible:
        provs = set()
        for link in d["hospitals"]:
            h = hospitals[link["id"]]
            if h["province"] in PROVINCES:
                provs.add(h["province"])
                district_counts[h["province"]][h["district"]] += 1
        for p in provs:
            per_prov[p].append(doctor_public(d))

    # doctors linked per hospital, for hospital pages
    docs_at = Counter(l["id"] for d in visible for l in d["hospitals"])
    hosp_by_prov = defaultdict(list)
    hosp_district_counts = defaultdict(Counter)
    for h in hospitals.values():
        if h["province"] in PROVINCES:
            hosp_by_prov[h["province"]].append({**{k: v for k, v in h.items() if v not in ("", None)},
                                                "docs": docs_at.get(h["id"], 0)})
            hosp_district_counts[h["province"]][h["district"]] += 1

    for p in PROVINCES:
        hs = sorted(hosp_by_prov.get(p, []), key=lambda h: (-h["docs"], h["name"]))
        write_json(OUT / "province" / f"{p}.json", {"doctors": per_prov.get(p, []), "hospitals": hs})

    write_json(OUT / "hospitals.json", sorted(hospitals.values(), key=lambda h: (h["province"], h["district"], h["name"])))

    # compact search index: [nmc, name, gender, degree, specialty, photo, [hospital ids]]
    index = [[d["nmc_no"], d["name"], d["gender"][:1], d["degree"], d["specialty"], d["photo_url"],
              [l["id"] for l in d["hospitals"]]] for d in visible]
    write_json(OUT / "doctors-index.json", index)

    spec_counts = Counter(d["specialty"] for d in visible)
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "doctors": len(visible),
            "doctors_with_workplace": sum(1 for d in visible if d["hospitals"]),
            "hospitals": len(hospitals),
        },
        "provinces": [
            {"name": p, "doctors": len(per_prov.get(p, [])), "hospitals": len(hosp_by_prov.get(p, [])),
             "districts": [{"name": dist, "doctors": district_counts[p].get(dist, 0),
                            "hospitals": hosp_district_counts[p].get(dist, 0)} for dist in ds]}
            for p, ds in PROVINCES.items()
        ],
        "specialties": [{"name": s, "doctors": c} for s, c in spec_counts.most_common()],
    }
    write_json(OUT / "meta.json", meta, pretty=True)

    export_excel(visible, hospitals)

    print(f"Built: {len(visible)} doctors, {meta['totals']['doctors_with_workplace']} with workplace, "
          f"{len(hospitals)} hospitals")
    for w in warnings[:50]:
        print("WARNING:", w)
    if len(warnings) > 50:
        print(f"... and {len(warnings) - 50} more warnings")


def doctor_public(d):
    return {
        "n": d["nmc_no"], "name": d["name"], "g": d["gender"], "deg": d["degree"],
        "sp": d["specialty"], "spRaw": d["speciality_raw"], "photo": d["photo_url"], "bio": d["bio"],
        "h": [{k: v for k, v in l.items() if v and k != "source"} for l in d["hospitals"]],
    }


def write_json(path, obj, pretty=False):
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def export_excel(doctors, hospitals):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        print("openpyxl not installed; skipping Excel export")
        return
    EXPORT.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2A4B58")

    def sheet(ws, headers, rows, widths):
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "Doctors"
    sheet(ws, ["NMC No", "Name", "Gender", "Degree", "Specialty", "NMC speciality (raw)", "Hospitals", "Photo URL"],
          [[d["nmc_no"], d["name"], d["gender"], d["degree"], d["specialty"], d["speciality_raw"],
            "; ".join(hospitals[l["id"]]["name"] for l in d["hospitals"]), d["photo_url"]] for d in doctors],
          [10, 32, 9, 30, 22, 26, 40, 30])
    sheet(wb.create_sheet("Hospitals"),
          ["Hospital ID", "Name", "Type", "Province", "District", "Address", "Lat", "Lng", "Phone", "Website"],
          [[h["id"], h["name"], h["type"], h["province"], h["district"], h["address"], h["lat"], h["lng"],
            h["phone"], h["website"]] for h in hospitals.values()],
          [14, 36, 18, 14, 16, 30, 10, 10, 16, 30])
    sheet(wb.create_sheet("Doctor_Hospital"),
          ["NMC No", "Doctor", "Hospital ID", "Hospital", "Department", "OPD days", "OPD time"],
          [[d["nmc_no"], d["name"], l["id"], hospitals[l["id"]]["name"], l["department"], l["opd_days"], l["opd_time"]]
           for d in doctors for l in d["hospitals"]],
          [10, 32, 14, 36, 20, 18, 14])
    wb.save(EXPORT / "gharpital.xlsx")


if __name__ == "__main__":
    sys.exit(main())
