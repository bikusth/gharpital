"""
Nepal's provinces and districts, common spellings, and a parser that turns an
NMC registration address into (place, district) without keeping anything finer.
Shared by the scraper and the data build.
"""
import re

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

# lower-cased spelling -> canonical district. None = ambiguous (old district later split).
DISTRICT_ALIASES = {
    "chitawan": "Chitwan", "kabhrepalanchok": "Kavrepalanchok", "kavre": "Kavrepalanchok",
    "kabhre": "Kavrepalanchok", "kavrepalanchowk": "Kavrepalanchok", "kapilbastu": "Kapilvastu",
    "makawanpur": "Makwanpur", "tanahu": "Tanahun", "nawalparasi east": "Nawalpur",
    "nawalparasi west": "Parasi", "nawalparasi (bardaghat susta east)": "Nawalpur",
    "nawalparasi (bardaghat susta west)": "Parasi", "nawalparasi": None, "rukum": None,
    "sindhupalchowk": "Sindhupalchok", "sindupalchok": "Sindhupalchok", "tehrathum": "Terhathum",
    "dhanusa": "Dhanusha", "dang deukhuri": "Dang", "deukhuri": "Dang", "rukum (east)": "Rukum East",
    "rukum (west)": "Rukum West", "eastern rukum": "Rukum East", "western rukum": "Rukum West",
    "ktm": "Kathmandu", "patan": None, "illam": "Ilam", "taplejung": "Taplejung", "sankhuwasava": "Sankhuwasabha",
    "solukhumbhu": "Solukhumbu", "okhaldunga": "Okhaldhunga", "kanchanpur": "Kanchanpur",
    "bardia": "Bardiya", "dadeldura": "Dadeldhura", "achaam": "Achham", "mahotari": "Mahottari",
    "sarlahee": "Sarlahi", "rauthat": "Rautahat", "udaypur": "Udayapur", "sunsary": "Sunsari",
}


def _key(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z() ]", " ", (s or "").lower())).strip()


_CANON = {_key(d): d for d in DISTRICT_TO_PROVINCE}


def match_district(token):
    """Return the canonical district for a token, None if not a district (or ambiguous)."""
    k = _key(token)
    if not k:
        return None
    if k in _CANON:
        return _CANON[k]
    if k in DISTRICT_ALIASES:
        return DISTRICT_ALIASES[k]
    # "Dang Deukhuri", "Kathmandu Metropolitan", "Lalitpur District"...
    for suffix in (" district", " jilla", " metropolitan city", " metropolitan", " sub metropolitan city",
                   " sub metropolitan", " municipality", " city"):
        if k.endswith(suffix) and k[: -len(suffix)] in _CANON:
            return _CANON[k[: -len(suffix)]]
    first = k.split(" ")[0]
    if first in _CANON and len(k.split(" ")) <= 2:
        return _CANON[first]
    return None


def parse_address(text):
    """NMC address -> (place, district).

    Keeps only the locality next to the district (usually the municipality or VDC) and the
    district itself - never the tole/street part. Examples:
      "Lagankhel, Patan , Lalitpur"  -> ("Patan", "Lalitpur")
      "Khaireni Tar , Tanahu,"       -> ("Khaireni Tar", "Tanahun")
      "Saptari, Bishahariya"         -> ("Bishahariya", "Saptari")
      "Majuwa, Chandrakot"           -> ("Chandrakot", "")
      "Gorkha"                       -> ("", "Gorkha")
    """
    parts = [re.sub(r"\s+", " ", p).strip(" .-") for p in re.split(r"[,;/]", text or "")]
    parts = [p for p in parts if p and not re.fullmatch(r"(ward|wada)?\s*(no\.?)?\s*-?\s*\d+", p, re.I)
             and p.lower() not in ("nepal",)]
    if not parts:
        return "", ""
    idx = [i for i, p in enumerate(parts) if match_district(p)]
    if idx:
        i = idx[-1]
        district = match_district(parts[i])
        others = [p for j, p in enumerate(parts) if j != i and not match_district(p)]
        if i > 0 and not match_district(parts[i - 1]):
            place = parts[i - 1]
        elif i + 1 < len(parts):
            place = parts[i + 1]
        else:
            place = others[-1] if others else ""
        return clean_place(place), district
    # no district named: the last part is usually the municipality / VDC
    return clean_place(parts[-1]), ""


def clean_place(p):
    p = re.sub(r"\b(ward|wada)\s*(no\.?)?\s*-?\s*\d+\b", "", p or "", flags=re.I)
    p = re.sub(r"\b\d+\b", "", p)
    p = re.sub(r"\b(vdc|v\.d\.c\.?|gabisa|municipality|nagarpalika|n\.?p\.?|metropolitan city|sub-?metropolitan city|rural municipality|gaunpalika)\b",
               "", p, flags=re.I)
    return re.sub(r"\s+", " ", p).strip(" .-")


if __name__ == "__main__":
    for t in ["Lagankhel, Patan , Lalitpur", "Khaireni Tar , Tanahu,", "Saptari, Bishahariya",
              "Majuwa, Chandrakot", "Gorkha", "Tulsipur , Dang Deukhuri,", "Deodiha, Janakpur , Dhanusa",
              "Manakamana , Gorkha,", "Dhankuta,", "Khaluwalahalaura, Manpur", "Ward No. 5, Bharatpur, Chitwan",
              "Kathmandu Metropolitan City-10, Kathmandu", "Butwal Sub-Metropolitan City, Rupandehi", ""]:
        print(repr(t), "->", parse_address(t))
