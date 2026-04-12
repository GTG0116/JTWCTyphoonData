#!/usr/bin/env python3
"""
JTWC Typhoon Data Fetcher — v3 (RSS + JMV 3.0 TCW parser)

Discovery:  JTWC RSS feed  → gives direct product file URLs per active storm
Primary:    .tcw files (JMV 3.0 format) → structured forecast + wind radii
Secondary:  web.txt files → MSLP / fallback position/forecast parsing
Output:     data/storms.json  — CORS-open JSON on GitHub Pages
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────

JTWC_RSS  = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"
JTWC_BASE = "https://www.metoc.navy.mil"

OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "storms.json")

# Use a real browser UA — the JTWC site returns 403 to Python's default UA
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xml,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 30

BASIN_NAMES = {
    "WP": "Western North Pacific",
    "IO": "North Indian Ocean",
    "SH": "Southern Hemisphere",
    "EP": "Eastern North Pacific",
    "CP": "Central North Pacific",
    "AL": "North Atlantic",
}

# (upper wind threshold kt, colour hex, long label, short code)
INTENSITY_SCALE = [
    (34,  "#7ec8e3", "Tropical Depression",       "TD"),
    (64,  "#00d4ff", "Tropical Storm",            "TS"),
    (83,  "#aaff00", "Typhoon (Category 1)",       "TY1"),
    (96,  "#ffd700", "Typhoon (Category 2)",       "TY2"),
    (113, "#ff8c00", "Typhoon (Category 3)",       "TY3"),
    (130, "#ff3a3a", "Typhoon (Category 4)",       "TY4"),
    (999, "#c800ff", "Super Typhoon (Category 5)", "ST"),
]


# ─── Utility helpers ──────────────────────────────────────────────────────────

def kt_to_mph(kt):  return round(kt * 1.15078)
def kt_to_kmh(kt):  return round(kt * 1.852)

def intensity_info(wind_kt):
    for thr, color, label, code in INTENSITY_SCALE:
        if wind_kt < thr:
            return {"color": color, "label": label, "code": code}
    return {"color": "#c800ff", "label": "Super Typhoon (Category 5)", "code": "ST"}

def parse_dtg(s):
    """YYYYMMDDHH → UTC datetime"""
    try:
        return datetime.strptime(s.strip(), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def build_position(lat, lon, wind_kt, pressure_mb, storm_type, dt, tau,
                   wind_radii_nm=None):
    info = intensity_info(wind_kt)
    pos = {
        "tau":                  tau,
        "lat":                  round(lat, 1),
        "lon":                  round(lon, 1),
        "wind_kt":              wind_kt,
        "wind_mph":             kt_to_mph(wind_kt),
        "wind_kmh":             kt_to_kmh(wind_kt),
        "pressure_mb":          pressure_mb,
        "classification":       storm_type,
        "classification_label": info["label"],
        "intensity_code":       info["code"],
        "intensity_color":      info["color"],
        "datetime":             dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None,
    }
    if wind_radii_nm:
        pos["wind_radii_nm"] = wind_radii_nm
    return pos

def build_storm(product_id, basin, name, dtg, current, positions, is_final=False):
    # product_id format: wp0426 → basin=wp, cy=04, year_short=26
    cy   = int(product_id[2:4]) if len(product_id) >= 4 else 0
    year = int(dtg[:4]) if len(dtg) >= 4 else datetime.now(timezone.utc).year
    adv_dt = parse_dtg(dtg)
    return {
        "id":           product_id.upper(),
        "basin":        basin,
        "basin_name":   BASIN_NAMES.get(basin, basin),
        "number":       cy,
        "year":         year,
        "name":         name,
        "advisory_time": adv_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if adv_dt else None,
        "is_final_warning": is_final,
        "current":      current,
        "forecast":     positions,
    }


# ─── HTTP helper ─────────────────────────────────────────────────────────────

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        print(f"  [WARN] {url}: {exc}")
        return None


# ─── RSS discovery ────────────────────────────────────────────────────────────

def get_storms_from_rss():
    """
    Fetch the JTWC RSS feed and extract every active storm's product file URLs.
    Returns a list of dicts: {product_id, tcw_url, web_url, is_final}
    """
    print(f"Fetching RSS: {JTWC_RSS}")
    content = fetch(JTWC_RSS)
    if not content:
        print("[ERROR] Could not fetch JTWC RSS feed.")
        return []

    # Regex over the raw RSS — handles CDATA cleanly without XML parser quirks
    # Find every .tcw href
    tcw_matches  = re.findall(r"href='(https://[^']+\.tcw)'", content, re.I)
    web_matches  = re.findall(r"href='(https://[^']+web\.txt)'", content, re.I)

    # Build a lookup: product_id → web_url
    web_by_id = {}
    for wu in web_matches:
        m = re.search(r"/([a-z]{2}\d{4})web\.txt$", wu, re.I)
        if m:
            web_by_id[m.group(1).lower()] = wu

    storms = []
    seen = set()
    for tcw_url in tcw_matches:
        m = re.search(r"/([a-z]{2}\d{4})\.tcw$", tcw_url, re.I)
        if not m:
            continue
        pid = m.group(1).lower()   # e.g. "wp0426", "sh3026"
        if pid in seen:
            continue
        seen.add(pid)

        # Check if this is a final warning (look for it near this URL in the RSS)
        start = content.rfind("<item>", 0, content.find(tcw_url))
        end   = content.find("</item>", content.find(tcw_url))
        item_text = content[start:end] if start >= 0 and end >= 0 else ""
        is_final = bool(re.search(r"Final\s+Warning", item_text, re.I))

        storms.append({
            "product_id": pid,
            "tcw_url":    tcw_url,
            "web_url":    web_by_id.get(pid),
            "is_final":   is_final,
        })
        print(f"  Found: {pid.upper()} tcw={tcw_url.split('/')[-1]}"
              f"{' [FINAL]' if is_final else ''}")

    return storms


# ─── JMV 3.0 TCW parser ──────────────────────────────────────────────────────

def parse_tcw(content, product_id):
    """
    Parse a JTWC JMV 3.0 .tcw file.

    Header line (one per file):
      YYYYMMDDHH  STORM_ID  NAME  WARNING#  ACTIVE_COUNT  DIR  SPD  ...
      e.g.: 2026041112 04W SINLAKU    011  01 305 04 SATL XTRP 030

    Forecast lines:
      T{tau:03d}  {lat10}N/S  {lon10}E/W  {wind_kt}  [R064 NE SE SW NW QD] ...
      e.g.: T000 090N 1511E 085 R064 045 NE QD 040 SE QD 040 SW QD 030 NW QD ...
    """
    header = None
    raw_positions = []

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Header: 10-digit DTG followed by WMO storm ID (digits + letter)
        if re.match(r"^\d{10}\s+\d{2}[A-Z]", line) and header is None:
            header = line
            continue

        # Forecast position line
        m = re.match(r"^T(\d{3})\s+(\d+)([NS])\s+(\d+)([EW])\s+(\d+)(.*)", line)
        if m:
            tau      = int(m.group(1))
            lat_raw  = float(m.group(2)) / 10.0
            lat      = lat_raw if m.group(3) == "N" else -lat_raw
            lon_raw  = float(m.group(4)) / 10.0
            lon      = lon_raw if m.group(5) == "E" else -lon_raw
            wind_kt  = int(m.group(6))
            rest     = m.group(7)

            radii = _parse_tcw_radii(rest)
            raw_positions.append(dict(tau=tau, lat=lat, lon=lon,
                                      wind_kt=wind_kt, radii=radii))

    if not raw_positions:
        return None

    # Parse header for DTG, name
    dtg  = ""
    name = None
    if header:
        parts = header.split()
        dtg  = parts[0]           # YYYYMMDDHH
        # parts[1] = storm WMO ID (04W), parts[2] = name
        if len(parts) > 2 and re.match(r"^[A-Z]{2,}$", parts[2]):
            name = parts[2].title()

    base_dt = parse_dtg(dtg)
    basin   = product_id[:2].upper()

    positions = []
    for rp in raw_positions:
        dt = (base_dt + timedelta(hours=rp["tau"])) if base_dt else None
        storm_type = _infer_type(rp["wind_kt"])
        pos = build_position(rp["lat"], rp["lon"], rp["wind_kt"], 0,
                             storm_type, dt, rp["tau"], rp["radii"] or None)
        positions.append(pos)

    current = positions[0]
    return build_storm(product_id, basin, name, dtg, current, positions)


def _parse_tcw_radii(text):
    """
    Parse wind radii from the tail of a TCW T-line.
    'R064 045 NE QD 040 SE QD 040 SW QD 030 NW QD R050 ...'
    Returns {str(threshold): {NE, SE, SW, NW}} or empty dict.
    """
    radii = {}
    for m in re.finditer(
        r"R(\d{3})\s+(\d+)\s+NE\s+QD\s+(\d+)\s+SE\s+QD\s+(\d+)\s+SW\s+QD\s+(\d+)\s+NW\s+QD",
        text,
    ):
        radii[m.group(1)] = {
            "NE": int(m.group(2)),
            "SE": int(m.group(3)),
            "SW": int(m.group(4)),
            "NW": int(m.group(5)),
        }
    return radii


# ─── web.txt parser (MSLP + fallback) ────────────────────────────────────────

def extract_mslp(web_text):
    """Extract current MSLP from REMARKS section of web.txt."""
    # "MINIMUM CENTRAL PRESSURE AT 111200Z IS 965 MB."
    m = re.search(r"MINIMUM CENTRAL PRESSURE[^I\n]*IS\s+(\d+)\s*MB", web_text, re.I)
    if m:
        return int(m.group(1))
    # Older TCW format
    m = re.search(r"MINIMUM SEA LEVEL PRESSURE\s*[-–]\s*(\d+)\s*MB", web_text, re.I)
    return int(m.group(1)) if m else 0


def parse_web_txt(content, product_id):
    """
    Full fallback: parse a JTWC web.txt advisory when TCW is unavailable.
    Extracts current position + all forecast positions.
    """
    # --- Advisory time + current position ---
    # "111200Z --- NEAR 9.0N 151.1E"
    cur_m = re.search(
        r"(\d{2})(\d{4})Z\s*---\s*NEAR\s+(\d+\.?\d*)\s*([NS])\s+(\d+\.?\d*)\s*([EW])",
        content,
    )
    if not cur_m:
        return None

    day   = int(cur_m.group(1))
    hhmm  = cur_m.group(2)
    lat   = float(cur_m.group(3)) * (1 if cur_m.group(4) == "N" else -1)
    lon   = float(cur_m.group(5)) * (1 if cur_m.group(6) == "E" else -1)

    # Current winds
    wm = re.search(r"MAX SUSTAINED WINDS\s*-\s*(\d+)\s*KT", content, re.I)
    wind_kt = int(wm.group(1)) if wm else 0

    # MSLP
    pressure_mb = extract_mslp(content)

    # Derive advisory datetime
    now = datetime.now(timezone.utc)
    month, year = now.month, now.year
    if day > now.day + 1:          # date rolled past month boundary
        month -= 1
        if month < 1:
            month, year = 12, year - 1
    try:
        base_dt = datetime(year, month, day, int(hhmm[:2]), int(hhmm[2:]),
                           tzinfo=timezone.utc)
    except ValueError:
        base_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)

    dtg = base_dt.strftime("%Y%m%d%H")

    # Current position wind radii (from "RADIUS OF NNN KT WINDS -" blocks)
    cur_radii = _parse_webtxt_radii_block(
        content[cur_m.start():content.find("FORECASTS:", cur_m.start()) or len(content)]
    )

    storm_type = _infer_type(wind_kt)
    cur_pos = build_position(lat, lon, wind_kt, pressure_mb,
                             storm_type, base_dt, 0, cur_radii or None)
    positions = [cur_pos]

    # --- Forecast positions ---
    # Each block starts with "NN HRS, VALID AT:"
    # followed by "DDHHMMZ --- LAT LON"
    # followed by "MAX SUSTAINED WINDS - NNN KT"
    fc_blocks = list(re.finditer(
        r"(\d+)\s+HRS?,\s+VALID\s+AT:\s+(\d{2})(\d{4})Z\s*---\s*(\d+\.?\d*)\s*([NS])\s+(\d+\.?\d*)\s*([EW])",
        content,
    ))
    for i, fm in enumerate(fc_blocks):
        tau_h   = int(fm.group(1))
        f_day   = int(fm.group(2))
        f_hhmm  = fm.group(3)
        f_lat   = float(fm.group(4)) * (1 if fm.group(5) == "N" else -1)
        f_lon   = float(fm.group(6)) * (1 if fm.group(7) == "E" else -1)

        # Compute forecast datetime
        try:
            f_month, f_year = month, year
            if f_day < day:
                f_month += 1
                if f_month > 12:
                    f_month, f_year = 1, f_year + 1
            f_dt = datetime(f_year, f_month, f_day,
                            int(f_hhmm[:2]), int(f_hhmm[2:]), tzinfo=timezone.utc)
        except ValueError:
            f_dt = base_dt + timedelta(hours=tau_h)

        # Slice the text for this forecast block
        block_start = fm.end()
        block_end   = fc_blocks[i + 1].start() if i + 1 < len(fc_blocks) else len(content)
        block_text  = content[block_start:block_end]

        # Max wind in this block
        wm2 = re.search(r"MAX SUSTAINED WINDS\s*-\s*(\d+)\s*KT", block_text, re.I)
        f_wind = int(wm2.group(1)) if wm2 else 0

        f_radii = _parse_webtxt_radii_block(block_text)
        f_type  = _infer_type(f_wind)
        f_pos   = build_position(f_lat, f_lon, f_wind, 0,
                                 f_type, f_dt, tau_h, f_radii or None)
        positions.append(f_pos)

    basin = product_id[:2].upper()
    name = _extract_name_from_web(content)
    return build_storm(product_id, basin, name, dtg, cur_pos, positions)


def _parse_webtxt_radii_block(text):
    """
    Parse wind radii from a web.txt section:
    'RADIUS OF 064 KT WINDS - 045 NM NORTHEAST QUADRANT
                              040 NM SOUTHEAST QUADRANT ...'
    Returns {str(threshold): {NE, SE, SW, NW}} or {}.
    """
    radii = {}
    for m in re.finditer(r"RADIUS OF (\d+) KT WINDS", text, re.I):
        thr = m.group(1)
        # Grab the 4 quadrant values that follow in order: NE, SE, SW, NW
        tail = text[m.end():]
        vals = re.findall(r"(\d+)\s*NM\s+(?:NORTHEAST|SOUTHEAST|SOUTHWEST|NORTHWEST)",
                          tail[:400], re.I)
        if len(vals) == 4:
            radii[thr] = {
                "NE": int(vals[0]), "SE": int(vals[1]),
                "SW": int(vals[2]), "NW": int(vals[3]),
            }
    return radii


def _extract_name_from_web(text):
    """Extract storm name from web.txt SUBJ or body line."""
    m = re.search(r"SUBJ/(?:TYPHOON|TROPICAL CYCLONE|STORM|DEPRESSION)\s+\S+\s+\(([A-Z]+)\)",
                  text, re.I)
    if not m:
        m = re.search(r"(?:TYPHOON|TROPICAL CYCLONE|STORM|DEPRESSION)\s+\S+\s+\(([A-Z]+)\)",
                      text, re.I)
    return m.group(1).title() if m else None


def _infer_type(wind_kt):
    if wind_kt >= 130: return "ST"
    if wind_kt >= 64:  return "TY"
    if wind_kt >= 34:  return "TS"
    return "TD"


# ─── Per-storm orchestrator ───────────────────────────────────────────────────

def fetch_storm(entry):
    """
    Fetch and parse one storm using TCW-first, web.txt-fallback strategy.
    Returns a storm dict or None.
    """
    pid     = entry["product_id"]
    tcw_url = entry["tcw_url"]
    web_url = entry["web_url"]

    storm = None

    # ── Try TCW (JMV 3.0) first
    if tcw_url:
        print(f"  Fetching TCW: {tcw_url.split('/')[-1]}")
        tcw_text = fetch(tcw_url)
        if tcw_text and len(tcw_text.strip()) > 50:
            storm = parse_tcw(tcw_text, pid)
            if storm:
                print(f"  ✓ TCW parsed OK — {storm.get('name')} @ {storm['current']['wind_kt']} kt")

    # ── Fall back to web.txt
    if not storm and web_url:
        print(f"  Fallback to web.txt: {web_url.split('/')[-1]}")
        web_text = fetch(web_url)
        if web_text and len(web_text.strip()) > 50:
            storm = parse_web_txt(web_text, pid)
            if storm:
                print(f"  ✓ web.txt parsed OK — {storm.get('name')} @ {storm['current']['wind_kt']} kt")

    if not storm:
        print(f"  ✗ Could not parse {pid.upper()}")
        return None

    # ── Enrich with MSLP from web.txt (TCW format has no pressure data)
    if storm["current"]["pressure_mb"] == 0 and web_url:
        print(f"  Fetching web.txt for MSLP: {web_url.split('/')[-1]}")
        web_text = fetch(web_url)
        if web_text:
            mslp = extract_mslp(web_text)
            if mslp:
                storm["current"]["pressure_mb"] = mslp
                if storm["forecast"]:
                    storm["forecast"][0]["pressure_mb"] = mslp
                print(f"  ✓ MSLP = {mslp} mb")

    storm["is_final_warning"] = entry["is_final"]
    return storm


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 60)
    print("JTWC Typhoon Data Fetcher  v3")
    print(f"Run time: {now_str}")
    print("=" * 60)

    entries = get_storms_from_rss()
    if not entries:
        print("\n[INFO] No active storm products found in RSS.")

    storms = []
    for entry in entries:
        print(f"\nProcessing {entry['product_id'].upper()} …")
        storm = fetch_storm(entry)
        if storm:
            storms.append(storm)

    storms.sort(key=lambda s: (s["basin"], s["number"]))

    payload = {
        "generated":   now_str,
        "source":      "JTWC — Joint Typhoon Warning Center",
        "source_url":  JTWC_RSS,
        "api_version": "3.0",
        "storm_count": len(storms),
        "storms":      storms,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    print(f"Wrote {len(storms)} storm(s) → {OUTPUT_FILE}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
