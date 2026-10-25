# Gharpital

**Healthcare registry access from ghar.** A free directory of Nepal's registered doctors and hospitals, by province and district.

- **Live app:** https://bikusth.github.io/gharpital/
- **Editor (for you):** https://bikusth.github.io/gharpital/admin.html

## How it works

```
NMC register ──scraper──► data/raw/nmc_doctors.csv ─┐
OpenStreetMap ──import──► data/sources/osm_hospitals.csv ─┤
                     you ──► data/hospitals.csv ─────┼──build──► data/build/*.json ──► index.html (the app)
                     you ──► data/doctor_hospital.csv ┤                └──► data/export/gharpital.xlsx
          you / editor ──► data/corrections.csv ────┘   (corrections always win)
```

Everything runs on GitHub for free. GitHub Pages hosts the app; GitHub Actions keeps the data fresh.

| What | When | Workflow |
|---|---|---|
| Rebuild the app data | Every time a CSV in `data/` changes | `Build data` |
| Refresh the NMC register | Every Saturday (or run it by hand) | `Scrape NMC register` |
| Refresh hospitals from OpenStreetMap | 1st of every month (or by hand) | `Refresh hospitals (OpenStreetMap)` |

## First-time setup

1. **Upload these files** to `bikusth/gharpital` (main branch).
2. **Turn on Pages:** Settings → Pages → Source: *Deploy from a branch* → `main` / `(root)`.
3. **Let Actions push:** Settings → Actions → General → Workflow permissions → *Read and write permissions*.
4. **Fill the data:** Actions tab → run **Refresh hospitals (OpenStreetMap)**, then **Scrape NMC register**.
   The register has about 38,300 numbers. Each scrape run works for up to 5½ hours, saves its place, and the next run continues, so the first full pass takes two or three runs (start the next one by hand, or wait for Saturday).
5. **Set up the editor:** create a fine-grained token (GitHub → Settings → Developer settings → Fine-grained tokens) with *Repository access: only gharpital* and *Permissions → Contents: Read and write*. Open `admin.html` and paste it. It stays in that browser only.

### If NMC blocks GitHub's servers
Run the scraper on your own computer instead, then upload `data/raw/nmc_doctors.csv` and `data/raw/scrape_state.json`:

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium
python scraper/nmc_scraper.py --workers 2        # stop any time with Ctrl+C; it resumes where it left off
```

## Changing the data

**Easiest:** use the editor (`admin.html`). Look up a doctor, change what you need, *Save changes*.

**By hand on github.com:** open `data/corrections.csv` → pencil icon → add a line → *Commit changes*.

```
nmc_no,field,new_value,note
12345,specialty,Cardiology,confirmed on hospital site
12345,hospital,osm-n943246318,"hospital_id from hospitals, or the exact hospital name"
12345,opd_days,Sun–Thu,applies to the hospital on the line above
12345,photo_url,https://hospital.example/dr-photo.jpg,from the hospital's own profile
23456,hide,yes,doctor asked to be removed
NEW-1,name,Dr. Example Sharma,doctor not on the register yet
NEW-1,specialty,Pediatrics,
```

Fields: `name`, `gender`, `degree`, `specialty`, `hospital`, `remove_hospital`, `department`, `opd_days`, `opd_time`, `photo_url`, `bio`, `hide` (`yes`/`no`). To undo a correction, delete its line.

**Hospitals:** `data/hospitals.csv` adds hospitals or overrides OpenStreetMap ones (use the same `hospital_id`, e.g. `osm-n943246318`). Set `type` to `hide` to remove one.

**Bulk links:** `data/doctor_hospital.csv` is for many doctor-to-hospital links at once, for example a whole hospital's doctor list: `nmc_no,hospital_id,department,opd_days,opd_time,source,last_checked`.

## What's stored, and what isn't

From the NMC register: NMC number, name, gender, degree, speciality and remarks. Doctors whose remarks say they are deregistered (for example after death) are kept out of the app. The register's **address field is deliberately not collected**, because it is a home address, not a workplace. Photos come only from hospitals' own published profiles.

Everything in this repository is public, so keep personal phone numbers and home addresses out of the CSVs.

## Files

| Path | What it is |
|---|---|
| `index.html` | The app |
| `admin.html` | The editor |
| `scraper/nmc_scraper.py` | Reads the NMC register one number at a time, politely |
| `build/fetch_osm_hospitals.py` | Imports hospitals from OpenStreetMap and assigns each to its district |
| `build/build_data.py` | Merges everything into the app's data files and the Excel export |
| `data/geo/` | District boundaries (for "Use my location") and the province map |
| `data/build/` | Generated: never edit by hand |
| `data/export/gharpital.xlsx` | Generated Excel copy for browsing |

Doctor data: Nepal Medical Council public register. Hospital locations and district boundaries: © OpenStreetMap contributors (ODbL); boundaries via [mesaugat/geoJSON-Nepal](https://github.com/mesaugat/geoJSON-Nepal). Fonts: Mukta and Baloo 2 (SIL OFL). Map: Leaflet (BSD-2).
