#!/usr/bin/env python3
"""
NHC Hurricane Data Fetcher
Sources: NHC CurrentStorms.json + public advisory text (HTML/pre)
Outputs: data/nhc_atlantic.json  (Atlantic basin — AL prefix)
         data/nhc_pacific.json   (Eastern/Central Pacific — EP/CP prefix)

Run by GitHub Actions alongside fetch_jtwc.py so CORS is never an issue.
data/storms.json (JTWC) is NOT touched — this file is separate.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────

NHC_CURRENT   = "https://www.nhc.noaa.gov/CurrentStorms.json"
NHC_TEXT_TMPL = "https://www.nhc.noaa.gov/text/refresh/{product}+shtml/"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,text/plain,*/*;q=0.8",
}

TIMEOUT = 30

BASIN_NAMES = {
    "AL": "North Atlantic",
    "EP": "Eastern North Pacific",
    "CP": "Central North Pacific",
}

# NHC / SSHS intensity scale — Hurricane terminology (same thresholds as JTWC)
NHC_INTENSITY = [
    (34,  "#7ec8e3", "Tropical Depression",     "TD" ),
    (64,  "#00d4ff", "Tropical Storm",           "TS" ),
    (83,  "#aaff00", "Hurricane (Category 1)",   "HU1"),
    (96,  "#ffd700", "Hurricane (Category 2)",   "HU2"),
    (113, "#ff8c00", "Hurricane (Category 3)",   "HU3"),
    (137, "#ff3a3a", "Hurricane (Category 4)",   "HU4"),
    (999, "#c800ff", "Hurricane (Category 5)",   "HU5"),
]


# ─── Utility helpers ──────────────────────────────────────────────────────────

def kt_to_mph(kt):  return round(kt * 1.15078)
def kt_to_kmh(kt):  return round(kt * 1.852)


def nhc_intensity(wind_kt, header_text=""):
    """Return colour / label / code for NHC storms (Hurricane terminology)."""
    upper = header_text.upper()
    if re.search(r"POST.TROPICAL|REMNANT", upper):
        return {"color": "#7ec8e3", "label": "Post-Tropical Cyclone", "code": "PT"}
    if re.search(r"SUBTROPICAL DEPRESSION", upper):
        return {"color": "#7ec8e3", "label": "Subtropical Depression", "code": "SD"}
    if re.search(r"SUBTROPICAL STORM", upper):
        return {"color": "#00d4ff", "label": "Subtropical Storm",      "code": "SS"}
    for thr, color, label, code in NHC_INTENSITY:
        if wind_kt < thr:
            return {"color": color, "label": label, "code": code}
    return {"color": "#c800ff", "label": "Hurricane (Category 5)", "code": "HU5"}


def build_pos(lat, lon, wind_kt, pressure_mb, dt, tau, hdr=""):
    info = nhc_intensity(wind_kt, hdr)
    return {
        "tau":                  tau,
        "lat":                  round(lat, 1),
        "lon":                  round(lon, 1),
        "wind_kt":              wind_kt,
        "wind_mph":             kt_to_mph(wind_kt),
        "wind_kmh":             kt_to_kmh(wind_kt),
        "pressure_mb":          pressure_mb,
        "classification":       info["code"],
        "classification_label": info["label"],
        "intensity_code":       info["code"],
        "intensity_color":      info["color"],
        "datetime":             dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None,
    }


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  [WARN] {url}: {e}")
        return None


# ─── Advisory URL resolver ────────────────────────────────────────────────────

def advisory_url(storm_id: str, basic_info: dict) -> str | None:
    """
    Return the advisory text page URL.
    Prefer the URL embedded in CurrentStorms.json; fall back to construction.
    """
    pub = basic_info.get("publicAdvisory", {})
    if pub.get("url"):
        return pub["url"]

    # Construct from storm ID (e.g. al012026 → MIATCPAT1)
    basin = storm_id[:2].lower()
    num   = int(storm_id[2:4])
    if basin == "al":
        prod = f"MIATCPAT{num}"
    elif basin == "ep":
        prod = f"MIATCPEP{num}"
    elif basin == "cp":
        prod = f"HFOTCPCP{num}"
    else:
        return None
    return NHC_TEXT_TMPL.format(product=prod)


# ─── Advisory text parser ─────────────────────────────────────────────────────

def parse_nhc_advisory(text: str, storm_id: str, basic_info: dict) -> dict | None:
    """
    Parse NHC TCP (Tropical Cyclone Public Advisory) plain text.
    Returns a storm dict compatible with the JTWC storms.json schema, or None.
    """
    hdr = text[:600]   # bulletin header section for classification detection

    # ── Storm name
    name_m = re.search(
        r"(?:Hurricane|Tropical Storm|Tropical Depression|"
        r"Post-Tropical Cyclone|Subtropical Storm|Subtropical Depression)"
        r"\s+([A-Z][A-Za-z\-]+)",
        text,
    )
    name = name_m.group(1) if name_m else (basic_info.get("name") or "").title() or None

    # ── Current position — "LOCATION...47.5N 30.2W"
    loc_m = re.search(
        r"LOCATION\s*[.\s]{1,5}\s*(\d+\.?\d*)\s*([NS])[,\s]+(\d+\.?\d*)\s*([EW])",
        text, re.I,
    )
    if loc_m:
        lat = float(loc_m.group(1)) * (1 if loc_m.group(2).upper() == "N" else -1)
        lon = float(loc_m.group(3)) * (1 if loc_m.group(4).upper() == "E" else -1)
    else:
        lat = float(basic_info.get("latitudeNumeric",  0) or 0)
        lon = float(basic_info.get("longitudeNumeric", 0) or 0)
        if lat == 0 and lon == 0:
            return None

    # ── Current max wind (advisory gives MPH; convert to kt)
    wind_mph_m = re.search(r"MAXIMUM SUSTAINED WINDS\s*[.…]{1,5}\s*(\d+)\s*MPH", text, re.I)
    cur_wind_kt = 0
    if wind_mph_m:
        cur_wind_kt = round(int(wind_mph_m.group(1)) / 1.15078)
    elif basic_info.get("intensity"):
        # CurrentStorms.json intensity field is in KNOTS
        cur_wind_kt = int(basic_info["intensity"])

    # ── Current pressure
    pres_m = re.search(r"MINIMUM CENTRAL PRESSURE\s*[.…]{1,5}\s*(\d+)\s*MB", text, re.I)
    pressure_mb = int(pres_m.group(1)) if pres_m else 0

    # ── Forecast positions table:
    # "INIT  10/2100Z 47.5N  30.2W   35 KT  40 MPH"
    # "  12H  11/0900Z 49.0N  27.5W   30 KT  35 MPH"
    fc_re = re.compile(
        r"(?P<tau_s>INIT|\s*\d+H)\s+"
        r"(?P<day>\d{2})/(?P<hhmm>\d{4})Z\s+"
        r"(?P<flat>\d+\.?\d*)\s*(?P<fns>[NS])\s+"
        r"(?P<flon>\d+\.?\d*)\s*(?P<few>[EW])\s+"
        r"(?P<fkt>\d+)\s*KT",
        re.I,
    )

    now = datetime.now(timezone.utc)
    positions = []

    for m in fc_re.finditer(text):
        tau_s = m.group("tau_s").strip().upper()
        tau   = 0 if tau_s == "INIT" else int(tau_s.replace("H", ""))

        day  = int(m.group("day"))
        hhmm = m.group("hhmm")
        f_lat = float(m.group("flat")) * (1 if m.group("fns").upper() == "N" else -1)
        f_lon = float(m.group("flon")) * (1 if m.group("few").upper() == "E" else -1)
        f_kt  = int(m.group("fkt"))

        # Compute forecast UTC datetime
        month, year = now.month, now.year
        if day < now.day - 3:   # wrapped past month boundary
            month += 1
            if month > 12:
                month, year = 1, year + 1
        try:
            f_dt = datetime(year, month, day, int(hhmm[:2]), int(hhmm[2:]),
                            tzinfo=timezone.utc)
        except ValueError:
            f_dt = now + timedelta(hours=tau)

        pos = build_pos(f_lat, f_lon, f_kt, 0, f_dt, tau, hdr)
        positions.append(pos)

    if not positions:
        # No forecast table — build current-position-only entry
        pos = build_pos(lat, lon, cur_wind_kt, pressure_mb, now, 0, hdr)
        positions = [pos]

    # ── Patch τ=0 (INIT) with accurate current data from the advisory summary
    positions[0]["lat"]         = round(lat, 1)
    positions[0]["lon"]         = round(lon, 1)
    positions[0]["pressure_mb"] = pressure_mb
    if cur_wind_kt:
        positions[0]["wind_kt"]  = cur_wind_kt
        positions[0]["wind_mph"] = kt_to_mph(cur_wind_kt)
        positions[0]["wind_kmh"] = kt_to_kmh(cur_wind_kt)
    # Re-derive intensity label using patched wind
    info = nhc_intensity(positions[0]["wind_kt"], hdr)
    for k, v in info.items():
        positions[0][k if k != "label" else "classification_label"] = v
    positions[0]["intensity_code"]  = info["code"]
    positions[0]["intensity_color"] = info["color"]
    positions[0]["classification"]  = info["code"]

    current = positions[0]

    # ── Build storm dict
    basin_atcf = storm_id[:2].upper()       # AL, EP, CP
    cy         = int(storm_id[2:4])         # 01, 03 …
    year_num   = int(storm_id[4:8]) if len(storm_id) >= 8 else now.year

    return {
        "id":               storm_id.upper(),
        "basin":            basin_atcf,
        "basin_name":       BASIN_NAMES.get(basin_atcf, basin_atcf),
        "number":           cy,
        "year":             year_num,
        "name":             name,
        "source_agency":    "NHC",
        "advisory_time":    current["datetime"],
        "is_final_warning": bool(re.search(r"last.*advisory|final.*advisory", text, re.I)),
        "current":          current,
        "forecast":         positions,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 60)
    print("NHC Hurricane Data Fetcher")
    print(f"Run time: {now_str}")
    print("=" * 60)

    print(f"\nFetching: {NHC_CURRENT}")
    raw = fetch(NHC_CURRENT)
    if raw:
        active = json.loads(raw).get("activeStorms", [])
    else:
        print("[ERROR] Could not reach NHC CurrentStorms.json")
        active = []

    print(f"  Found {len(active)} active NHC storm(s)")

    atlantic: list[dict] = []
    pacific:  list[dict] = []

    for basic in active:
        sid   = basic.get("id", "").lower()
        basin = sid[:2]
        print(f"\nProcessing {sid.upper()} ({basic.get('name', '?')}) …")

        adv_url = advisory_url(sid, basic)
        if not adv_url:
            print(f"  [WARN] No advisory URL for {sid}")
            continue

        print(f"  Fetching: {adv_url}")
        html = fetch(adv_url)
        if not html:
            continue

        soup = BeautifulSoup(html, "lxml")
        pre  = soup.find("pre")
        if not pre:
            print(f"  [WARN] No <pre> content in advisory HTML")
            continue

        storm = parse_nhc_advisory(pre.get_text(), sid, basic)
        if not storm:
            print(f"  [WARN] Parse failed for {sid}")
            continue

        print(f"  ✓ {storm['name']}  {storm['current']['wind_kt']} kt  "
              f"{storm['current']['pressure_mb'] or 'N/A'} mb  "
              f"{len(storm['forecast'])} steps  "
              f"{'[FINAL]' if storm['is_final_warning'] else ''}")

        if basin == "al":
            atlantic.append(storm)
        else:
            pacific.append(storm)

    # Write outputs (always write, even if empty — keeps GitHub Pages fresh)
    for fname, storms_list, label in [
        ("nhc_atlantic.json", atlantic, "Atlantic"),
        ("nhc_pacific.json",  pacific,  "Eastern/Central Pacific"),
    ]:
        payload = {
            "generated":   now_str,
            "source":      "NHC — National Hurricane Center",
            "source_url":  NHC_CURRENT,
            "api_version": "3.0",
            "storm_count": len(storms_list),
            "storms":      storms_list,
        }
        path = os.path.join(OUTPUT_DIR, fname)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nWrote {len(storms_list)} {label} storm(s) → {fname}")

    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
