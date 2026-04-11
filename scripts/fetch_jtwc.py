#!/usr/bin/env python3
"""
JTWC Typhoon Data Fetcher
Pulls ATCF advisory data from the Joint Typhoon Warning Center,
parses it, and writes structured JSON for the interactive viewer.

Data is fetched server-side (GitHub Actions) to bypass CORS restrictions.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
JTWC_BASE = "https://www.metoc.navy.mil"
JTWC_MAIN = f"{JTWC_BASE}/jtwc/jtwc.html"
JTWC_PRODUCTS = f"{JTWC_BASE}/jtwc/products"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "storms.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JTWCForecastViewer/2.0; "
        "+https://github.com/gtg0116/JTWCTyphoonData)"
    ),
    "Accept": "text/html,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 30  # seconds

# ATCF basin→human name mapping
BASIN_NAMES = {
    "WP": "Western North Pacific",
    "IO": "North Indian Ocean",
    "SH": "Southern Hemisphere",
    "EP": "Eastern North Pacific",
    "CP": "Central North Pacific",
    "AL": "North Atlantic",
}

# Knot thresholds for JTWC intensity scale with SSHS-equivalent colours
INTENSITY_SCALE = [
    (34,  "#7ec8e3", "Tropical Depression",       "TD"),
    (64,  "#00d4ff", "Tropical Storm",            "TS"),
    (83,  "#aaff00", "Typhoon (Category 1)",       "TY1"),
    (96,  "#ffd700", "Typhoon (Category 2)",       "TY2"),
    (113, "#ff8c00", "Typhoon (Category 3)",       "TY3"),
    (130, "#ff3a3a", "Typhoon (Category 4)",       "TY4"),
    (999, "#c800ff", "Super Typhoon (Category 5)", "ST"),
]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def kt_to_mph(kt: float) -> int:
    return round(kt * 1.15078)


def kt_to_kmh(kt: float) -> int:
    return round(kt * 1.852)


def intensity_info(wind_kt: int) -> dict:
    """Return colour hex, label, and short code for a given wind speed."""
    for threshold, color, label, code in INTENSITY_SCALE:
        if wind_kt < threshold:
            return {"color": color, "label": label, "code": code}
    return {"color": "#c800ff", "label": "Super Typhoon (Category 5)", "code": "ST"}


def parse_lat(s: str) -> float | None:
    """Convert ATCF lat string '152N' or text '15.2N' to decimal degrees."""
    s = s.strip()
    m = re.match(r"^(\d+\.?\d*)([NS])$", s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    # ATCF stores tenths without a decimal point (152N = 15.2N)
    if "." not in s and val > 900:
        val /= 10.0
    elif "." not in s and val > 90:
        val /= 10.0
    return val if m.group(2).upper() == "N" else -val


def parse_lon(s: str) -> float | None:
    """Convert ATCF lon string '1438E' or text '143.8E' to decimal degrees."""
    s = s.strip()
    m = re.match(r"^(\d+\.?\d*)([EW])$", s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    if "." not in s and val > 1800:
        val /= 10.0
    elif "." not in s and val > 180:
        val /= 10.0
    return val if m.group(2).upper() == "E" else -val


def parse_atcf_dtg(s: str) -> datetime | None:
    """Parse YYYYMMDDHH string to UTC datetime."""
    try:
        return datetime.strptime(s.strip(), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_position(lat, lon, wind_kt, pressure_mb, storm_type, dt, tau) -> dict:
    """Build a standard position dict with all derived fields."""
    info = intensity_info(wind_kt)
    return {
        "tau": tau,
        "lat": round(lat, 1),
        "lon": round(lon, 1),
        "wind_kt": wind_kt,
        "wind_mph": kt_to_mph(wind_kt),
        "wind_kmh": kt_to_kmh(wind_kt),
        "pressure_mb": pressure_mb,
        "classification": storm_type,
        "classification_label": info["label"],
        "intensity_code": info["code"],
        "intensity_color": info["color"],
        "datetime": dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None,
    }


# ---------------------------------------------------------------------------
# Fetching helpers
# ---------------------------------------------------------------------------

def fetch(url: str, binary: bool = False):
    """Fetch a URL; return content (str or bytes) or None on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.content if binary else r.text
    except Exception as exc:
        print(f"  [WARN] Could not fetch {url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# ATCF a-deck parser
# ---------------------------------------------------------------------------

def parse_atcf(content: str, storm_id: str) -> dict | None:
    """
    Parse an ATCF a-deck (forecast deck) file.
    Returns a storm dict with current position + forecast array, or None.
    """
    # Group records by advisory datetime (DTG), keep only OFCL/JTWC technique
    advisories: dict[str, list[dict]] = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue

        try:
            tech = parts[4].strip().upper()
            if tech not in ("OFCL", "JTWC", "CARQ"):
                continue

            dtg = parts[2].strip()
            tau = int(parts[5])
            lat = parse_lat(parts[6])
            lon = parse_lon(parts[7])
            wind_kt = int(parts[8]) if parts[8].strip() else 0
            pressure_mb = int(parts[9]) if parts[9].strip() else 0
            storm_type = parts[10].strip() or "XX"
        except (ValueError, IndexError):
            continue

        if lat is None or lon is None:
            continue

        base_dt = parse_atcf_dtg(dtg)
        if not base_dt:
            continue
        forecast_dt = base_dt + timedelta(hours=tau)

        if dtg not in advisories:
            advisories[dtg] = []

        advisories[dtg].append(
            build_position(lat, lon, wind_kt, pressure_mb, storm_type, forecast_dt, tau)
        )

    if not advisories:
        return None

    # Use the most recent advisory
    latest_dtg = sorted(advisories.keys())[-1]
    positions = sorted(advisories[latest_dtg], key=lambda p: p["tau"])

    # Extract current (tau=0 or first entry)
    current = next((p for p in positions if p["tau"] == 0), positions[0])

    # Try to extract storm name from line 28+ of ATCF records
    name = None
    for line in content.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 28:
            candidate = parts[27].strip()
            if candidate and re.match(r"^[A-Z]{2,}$", candidate):
                name = candidate.title()
                break

    basin = storm_id[:2].upper()
    return _build_storm(storm_id, basin, name, latest_dtg, current, positions)


# ---------------------------------------------------------------------------
# TCW (Tropical Cyclone Warning text) parser
# ---------------------------------------------------------------------------

def parse_tcw(text: str, storm_id: str) -> dict | None:
    """
    Parse a JTWC TCW-format text advisory.
    Handles both Western Pacific and Indian/Southern Hemisphere advisories.
    """
    # ---- Storm name --------------------------------------------------------
    name = None
    name_match = re.search(
        r"(?:TROPICAL|SUPER)\s+(?:CYCLONE|STORM|DEPRESSION|TYPHOON)\s+\d+\w*\s+\(([A-Z]+)\)",
        text,
    )
    if name_match:
        name = name_match.group(1).title()

    # ---- Advisory time -----------------------------------------------------
    # "AT 11/0000Z" or "AT 11/0000Z..."
    adv_dt = None
    time_match = re.search(r"AT\s+(\d{1,2})/(\d{4})Z", text)
    if time_match:
        day = int(time_match.group(1))
        hhmm = time_match.group(2)
        now = datetime.now(timezone.utc)
        adv_month = now.month
        adv_year = now.year
        # Roll back month if day is in the future (e.g., near month end)
        if day > now.day + 1:
            adv_month -= 1
            if adv_month < 1:
                adv_month = 12
                adv_year -= 1
        try:
            adv_dt = datetime(
                adv_year, adv_month, day,
                int(hhmm[:2]), int(hhmm[2:]),
                tzinfo=timezone.utc,
            )
        except ValueError:
            adv_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    else:
        adv_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # ---- Current position --------------------------------------------------
    pos_match = re.search(
        r"(?:POSITION|LOCATED\s+NEAR)[:\s]+(\d+\.?\d*)\s*([NS])[,\s]+(\d+\.?\d*)\s*([EW])",
        text, re.I,
    )
    if not pos_match:
        print(f"  [WARN] Could not find position in TCW for {storm_id}")
        return None

    lat = float(pos_match.group(1)) * (1 if pos_match.group(2).upper() == "N" else -1)
    lon = float(pos_match.group(3)) * (1 if pos_match.group(4).upper() == "E" else -1)

    wind_match = re.search(r"MAX\s+SUSTAINED\s+WINDS?\s*[-–]\s*(\d+)\s*KT", text, re.I)
    pres_match = re.search(r"(?:MINIMUM|MIN)\s+SEA\s+LEVEL\s+PRESSURE\s*[-–]\s*(\d+)\s*MB", text, re.I)

    wind_kt = int(wind_match.group(1)) if wind_match else 0
    pressure_mb = int(pres_match.group(1)) if pres_match else 0

    # Determine storm type from wind speed and text
    storm_type = _infer_type(wind_kt, text)
    current = build_position(lat, lon, wind_kt, pressure_mb, storm_type, adv_dt, 0)
    positions = [current]

    # ---- Forecast positions ------------------------------------------------
    # "FORECAST VALID 12/0000Z 16.5N  142.0E"
    forecast_lines = list(
        re.finditer(
            r"FORECAST\s+VALID\s+(\d{1,2})/(\d{4})Z\s+(\d+\.?\d*)\s*([NS])\s+(\d+\.?\d*)\s*([EW])",
            text, re.I,
        )
    )
    # Build a lookup from position in text → max wind
    wind_after = []
    for m in re.finditer(r"MAX\s+WIND\s+(\d+)\s*KT", text, re.I):
        wind_after.append((m.start(), int(m.group(1))))

    for idx, fm in enumerate(forecast_lines):
        day = int(fm.group(1))
        hhmm = fm.group(2)
        f_lat = float(fm.group(3)) * (1 if fm.group(4).upper() == "N" else -1)
        f_lon = float(fm.group(5)) * (1 if fm.group(6).upper() == "E" else -1)

        # Find MAX WIND that comes right after this forecast line
        f_wind = 0
        for wpos, wval in wind_after:
            if wpos > fm.end():
                f_wind = wval
                break

        # Compute forecast datetime
        try:
            base_month = adv_dt.month
            base_year = adv_dt.year
            if day < adv_dt.day:
                base_month += 1
                if base_month > 12:
                    base_month = 1
                    base_year += 1
            f_dt = datetime(
                base_year, base_month, day,
                int(hhmm[:2]), int(hhmm[2:]),
                tzinfo=timezone.utc,
            )
        except ValueError:
            f_dt = adv_dt + timedelta(hours=(idx + 1) * 24)

        tau = int((f_dt - adv_dt).total_seconds() / 3600)
        if tau < 0:
            tau = (idx + 1) * 24

        f_type = _infer_type(f_wind, "")
        positions.append(build_position(f_lat, f_lon, f_wind, 0, f_type, f_dt, tau))

    basin = storm_id[:2].upper()
    dtg_str = adv_dt.strftime("%Y%m%d%H")
    return _build_storm(storm_id, basin, name, dtg_str, current, positions)


def _infer_type(wind_kt: int, text: str) -> str:
    """Infer ATCF storm type from wind speed and text context."""
    text_up = text.upper()
    if "SUPER TYPHOON" in text_up or wind_kt >= 130:
        return "ST"
    if "TYPHOON" in text_up or wind_kt >= 64:
        return "TY"
    if "TROPICAL STORM" in text_up or wind_kt >= 34:
        return "TS"
    return "TD"


def _build_storm(
    storm_id: str, basin: str, name: str | None,
    dtg: str, current: dict, positions: list[dict]
) -> dict:
    """Assemble a complete storm dict."""
    cy_match = re.search(r"(\d+)", storm_id)
    cy = int(cy_match.group(1)) if cy_match else 0
    year = int(dtg[:4]) if len(dtg) >= 4 else datetime.now(timezone.utc).year

    # Advisory time as ISO string
    adv_dt = parse_atcf_dtg(dtg)
    adv_str = adv_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if adv_dt else None

    return {
        "id": storm_id.upper(),
        "basin": basin,
        "basin_name": BASIN_NAMES.get(basin, basin),
        "number": cy,
        "year": year,
        "name": name,
        "advisory_time": adv_str,
        "current": current,
        "forecast": positions,
    }


# ---------------------------------------------------------------------------
# JTWC page scraper — discovers active storms
# ---------------------------------------------------------------------------

def get_active_storm_urls() -> list[tuple[str, str]]:
    """
    Scrape the JTWC main page and return a list of (storm_id, url) tuples
    pointing to advisory product files (.TCW or .dat).
    """
    print(f"Fetching JTWC main page: {JTWC_MAIN}")
    html = fetch(JTWC_MAIN)
    if not html:
        print("[ERROR] Could not fetch JTWC main page.")
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Find all links that look like JTWC product files
    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Normalise to absolute URL
        if href.startswith("/"):
            href = JTWC_BASE + href
        elif not href.startswith("http"):
            href = JTWC_BASE + "/jtwc/" + href

        # Match product file patterns:
        #   /jtwc/products/wp012025/wp012025.TCW
        #   /jtwc/products/01W.2025.TCW
        #   /jtwc/products/wp012025/wp012025prog.txt
        m = re.search(
            r"(?:/|^)((?:wp|io|sh|ep|cp|al)\d{2,4}(?:\d{4})?)"
            r"(?:\.tcw|\.dat|prog\.txt|web\.txt)?",
            href, re.I,
        )
        if m:
            sid = m.group(1).upper()
            # Derive basin from 2-char prefix
            basin = sid[:2].upper()
            if basin not in BASIN_NAMES:
                continue
            if sid in seen:
                continue
            seen.add(sid)
            results.append((sid, href))
            print(f"  Found storm: {sid} → {href}")

    # If nothing found from direct links, try pattern-guessing from
    # text in the page (e.g. "2 ACTIVE TROPICAL CYCLONES")
    if not results:
        results = _guess_from_text(html)

    return results


def _guess_from_text(html: str) -> list[tuple[str, str]]:
    """
    Fallback: look for 'NNW', 'NNA', etc. patterns in the page text
    and construct candidate TCW URLs.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text()
    results = []
    seen: set[str] = set()
    year = datetime.now(timezone.utc).year

    # Match patterns like "TYPHOON 01W", "TROPICAL CYCLONE 02A", "STORM 03S"
    for m in re.finditer(r"\b(\d{2})([WEASBCP])\b", text):
        cy = m.group(1)
        suffix = m.group(2).upper()
        basin_map = {
            "W": "WP", "E": "EP", "C": "CP",
            "A": "IO", "B": "IO", "S": "SH", "P": "SH",
        }
        basin = basin_map.get(suffix, "")
        if not basin:
            continue
        sid = f"{basin}{cy}{year}"
        if sid in seen:
            continue
        seen.add(sid)
        # Construct plausible TCW URL
        url = f"{JTWC_PRODUCTS}/{sid.lower()}/{sid.lower()}.TCW"
        results.append((sid, url))
        print(f"  Guessed storm: {sid} → {url}")

    return results


# ---------------------------------------------------------------------------
# Per-storm data fetching
# ---------------------------------------------------------------------------

def fetch_storm(storm_id: str, hint_url: str) -> dict | None:
    """
    Try several URL patterns to find and parse advisory data for a storm.
    Returns a storm dict or None.
    """
    sid_lower = storm_id.lower()
    year = storm_id[-4:] if len(storm_id) >= 6 else str(datetime.now(timezone.utc).year)
    basin_lower = storm_id[:2].lower()
    cy = storm_id[2:4]

    # Build a list of URLs to try (ATCF preferred, TCW as fallback)
    candidate_urls = [
        hint_url,
        # ATCF a-deck (official forecast deck)
        f"{JTWC_PRODUCTS}/{sid_lower}/{sid_lower}.dat",
        f"{JTWC_PRODUCTS}/{sid_lower}/{sid_lower}a.dat",
        f"{JTWC_PRODUCTS}/{sid_lower}/{sid_lower}.fdat",
        # Text warning files
        f"{JTWC_PRODUCTS}/{sid_lower}/{sid_lower}.TCW",
        f"{JTWC_PRODUCTS}/{sid_lower}/{sid_lower}web.txt",
        f"{JTWC_PRODUCTS}/{sid_lower}/{sid_lower}prog.txt",
        # Alternative capitalisation / structure
        f"{JTWC_PRODUCTS}/{cy}{storm_id[4]}.{year}.TCW",
    ]

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_urls = []
    for u in candidate_urls:
        if u and u not in seen:
            seen.add(u)
            unique_urls.append(u)

    for url in unique_urls:
        print(f"  Trying {url} …")
        content = fetch(url)
        if not content or len(content.strip()) < 50:
            continue

        # Detect format: ATCF lines start with basin code + comma
        first_line = content.strip().splitlines()[0]
        if re.match(r"^[A-Z]{2},\s*\d+,", first_line):
            print(f"  → Detected ATCF a-deck format")
            result = parse_atcf(content, storm_id)
        else:
            print(f"  → Detected TCW text format")
            result = parse_tcw(content, storm_id)

        if result:
            print(f"  ✓ Parsed {storm_id}: {result.get('name')} "
                  f"@ {result['current']['wind_kt']} kt")
            return result

    print(f"  ✗ Could not retrieve data for {storm_id}")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print("JTWC Typhoon Data Fetcher")
    print(f"Run time: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * 60)

    storm_urls = get_active_storm_urls()

    if not storm_urls:
        print("\n[INFO] No active storms found on JTWC page.")

    storms = []
    for storm_id, url in storm_urls:
        print(f"\nProcessing {storm_id} …")
        storm = fetch_storm(storm_id, url)
        if storm:
            storms.append(storm)

    # Sort by basin then number
    storms.sort(key=lambda s: (s["basin"], s["number"]))

    output = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "JTWC - Joint Typhoon Warning Center",
        "source_url": JTWC_MAIN,
        "api_version": "2.0",
        "storm_count": len(storms),
        "storms": storms,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    print(f"Written {len(storms)} storm(s) to {OUTPUT_FILE}")
    print("=" * 60)

    return 0 if storms is not None else 1


if __name__ == "__main__":
    sys.exit(main())
