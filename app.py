import re
import json
import os
import sqlite3
import hashlib
import urllib.parse
from datetime import datetime, timezone, timedelta
from html import unescape

import feedparser
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
import plotly.graph_objects as go

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None
try:
    from supabase import create_client
except Exception:
    create_client = None

APP_TITLE = "Global Cat Watch"
DEFAULT_REFRESH_MINUTES = 10
HISTORY_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cat_event_history.db")
REQUEST_HEADERS = {"User-Agent": "GlobalCatWatch/1.0 (monitoring dashboard)"}

FEEDS = [
    {"source": "GDACS", "name": "GDACS All Events", "url": "https://www.gdacs.org/XML/RSS.xml", "default_peril": "All"},
    {"source": "NHC", "name": "NHC Atlantic Active Cyclones", "url": "https://www.nhc.noaa.gov/index-at.xml", "default_peril": "Tropical Cyclone"},
    {"source": "NHC", "name": "NHC East Pacific Active Cyclones", "url": "https://www.nhc.noaa.gov/index-ep.xml", "default_peril": "Tropical Cyclone"},
    {"source": "NHC", "name": "NHC Central Pacific Active Cyclones", "url": "https://www.nhc.noaa.gov/index-cp.xml", "default_peril": "Tropical Cyclone"},
    {"source": "NHC", "name": "NHC Graphical Tropical Weather Outlook", "url": "https://www.nhc.noaa.gov/gtwo.xml", "default_peril": "Tropical Cyclone"},
    {"source": "NHC", "name": "NHC Atlantic Tropical Weather Outlook", "url": "https://www.nhc.noaa.gov/xml/TWOAT.xml", "default_peril": "Tropical Cyclone"},
    {"source": "NHC", "name": "NHC East Pacific Tropical Weather Outlook", "url": "https://www.nhc.noaa.gov/xml/TWOEP.xml", "default_peril": "Tropical Cyclone"},
    {"source": "NHC", "name": "NHC Central Pacific Tropical Weather Outlook", "url": "https://www.nhc.noaa.gov/xml/TWOCP.xml", "default_peril": "Tropical Cyclone"},
]

CALFIRE_URL = "https://incidents.fire.ca.gov/umbraco/api/IncidentApi/List?inactive=false"

NEWS_SOURCES = {
    "BBC": "bbc.com", "CNBC": "cnbc.com", "Reuters": "reuters.com", "AP": "apnews.com",
    "Al Jazeera": "aljazeera.com", "Guardian": "theguardian.com", "Bloomberg": "bloomberg.com",
    "Insurance Insider": "insuranceinsider.com", "Artemis": "artemis.bm", "Reinsurance News": "reinsurancene.ws",
}

PERIL_ORDER = ["Tropical Cyclone", "Earthquake", "Flood", "Wildfire", "Volcano", "Drought", "Other"]
TIER_ORDER = {"Critical": 0, "Watch": 1, "Advisory": 2, "Info": 3}
ALERT_ORDER = {"Red": 0, "Orange": 1, "Green": 2, "Unknown": 3}

TIER_HEX = {"Critical": "#DC322F", "Watch": "#F08C14", "Advisory": "#E6C828", "Info": "#8C96A0"}
TIER_SIZE = {"Critical": 20, "Watch": 16, "Advisory": 13, "Info": 10}

BASIN_MAP = {
    "nwpacific": "NW Pacific", "westpacific": "W Pacific", "nepacific": "NE Pacific",
    "eastpacific": "E Pacific", "southpacific": "South Pacific", "northatlantic": "North Atlantic",
    "southatlantic": "South Atlantic", "northindian": "North Indian", "southindian": "South Indian",
    "southwestindian": "SW Indian", "arabiansea": "Arabian Sea", "bayofbengal": "Bay of Bengal",
}
_STOP_LOC_WORDS = {"On", "From", "During", "Until", "Last", "Started", "Ongoing", "In", "At", "Center"}


def clean_html(value: str) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def format_numbers(text: str) -> str:
    if not text:
        return text

    def repl(m):
        num = m.group(0)
        val = int(num)
        if len(num) == 4 and 1900 <= val <= 2100:
            return num
        return f"{val:,}"

    return re.sub(r"(?<![\d/])\d{4,}(?![\d/])", repl, text)


def display_summary(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\s*The cyclone affects these countries:.*?vulnerability[^)]*\)\.", "", text, flags=re.IGNORECASE)
    text = text.replace("[unknown]", "n/a")
    text = format_numbers(text)
    return re.sub(r"\s+", " ", text).strip()


def to_float(x):
    try:
        return float(x)
    except Exception:
        return None


def extract_latlon(entry):
    lat = to_float(entry.get("geo_lat"))
    lon = to_float(entry.get("geo_long"))
    if lat is not None and lon is not None:
        return lat, lon
    point = entry.get("georss_point") or entry.get("point")
    if point:
        try:
            a, b = str(point).split()[:2]
            return float(a), float(b)
        except Exception:
            pass
    where = entry.get("where")
    if isinstance(where, dict):
        coords = where.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                return float(coords[1]), float(coords[0])
            except Exception:
                pass
    return None, None


def parse_dt(value):
    if not value:
        return None
    try:
        dt = dtparser.parse(value)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_entry_dt(entry):
    for key in ["published", "updated", "created"]:
        dt = parse_dt(entry.get(key))
        if dt:
            return dt
    for key in ["published_parsed", "updated_parsed"]:
        if entry.get(key):
            try:
                return datetime(*entry[key][:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def age_label(dt):
    if not dt:
        return "Unknown"
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h ago"
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


def is_outlook(raw_title, summary):
    text = f"{raw_title} {summary}".lower()
    return ("tropical weather outlook" in text) or raw_title.strip().lower() == "tropical cyclone center"


def outlook_basin(summary):
    low = summary.lower()
    if "atlantic" in low or "caribbean" in low:
        return "Atlantic"
    if "eastern" in low and "pacific" in low:
        return "East Pacific"
    if "central" in low and "pacific" in low:
        return "Central Pacific"
    if "pacific" in low:
        return "Pacific"
    return ""


def parse_outlook_systems(summary):
    parts = list(re.finditer(r"\(([A-Z]{2}9\d)\)", summary))
    systems = []
    for i, m in enumerate(parts):
        code = m.group(1).upper()
        start = m.end()
        end = parts[i + 1].start() if i + 1 < len(parts) else len(summary)
        block = summary[start:end]
        pcts = [int(x) for x in re.findall(r"(\d{1,3})\s*percent", block, flags=re.IGNORECASE)]
        pcts = [p for p in pcts if 0 <= p <= 100]
        systems.append((code, max(pcts) if pcts else None))
    return systems


def extract_formation_chances(summary):
    """Return each NHC disturbance code and its stated maximum formation chance."""
    return {code: chance for code, chance in parse_outlook_systems(summary)}


def clean_tc_status_text(text):
    """Remove Hurricane Center boilerplate before classifying a storm."""
    text = text or ""
    for item in [
        "National Hurricane Center", "NWS National Hurricane Center",
        "Central Pacific Hurricane Center", "NWS Central Pacific Hurricane Center",
        "Hurricane Center Honolulu", "Hurricane Center Miami",
    ]:
        text = re.sub(re.escape(item), "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def title_case_storm_token(token):
    token = (token or "").strip(" .,-")
    if not token:
        return ""
    out = []
    for part in token.split("-"):
        if part.isdigit():
            out.append(part)
        elif len(part) == 1:
            out.append(part.upper())
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return "-".join(out)


def extract_tc_identity(raw_title, summary=""):
    """Return a readable storm/invest identifier such as One-C, Cristobal, AL032026, or EP91."""
    text = f"{raw_title or ''} {summary or ''}"
    patterns = [
        r"\b(?:potential tropical cyclone|post-tropical cyclone|tropical cyclone|tropical storm|tropical depression|major hurricane|hurricane|typhoon|cyclone|storm)\s+([A-Z][A-Za-z]+(?:-[A-Z0-9]+)?|[A-Z]{2}\d{2,4})\b",
        r"\b([A-Z][A-Z]+)-\d{2}\b",
        r"\b([A-Z]{2}9\d)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            ident = m.group(1).strip()
            if ident.lower() not in {"warning", "watch", "outlook", "advisory", "public", "discussion", "center"}:
                return title_case_storm_token(ident)
    return ""


def extract_nhc_product_type(raw_title, summary="", link=""):
    text = f"{raw_title or ''} {summary or ''} {link or ''}".lower()
    checks = [
        ("Tropical Weather Outlook", ["tropical weather outlook", "twoat", "twoep", "twocp", "gtwo"]),
        ("Discussion", ["discussion"]),
        ("Public Advisory", ["public advisory"]),
        ("Forecast/Advisory", ["forecast/advisory", "forecast advisory"]),
        ("Wind Probabilities", ["wind speed probabilities", "wind probabilities"]),
        ("Advisory", ["advisory"]),
    ]
    for label, terms in checks:
        if any(term in text for term in terms):
            return label
    return "NHC Product"


def build_tc_title(raw_title, summary="", status="", wind=None):
    ident = extract_tc_identity(raw_title, summary)
    basin = extract_basin(raw_title, summary) or outlook_basin(summary)
    status = status or extract_storm_status(f"{raw_title or ''} {summary or ''}") or "Tropical Cyclone"
    if ident:
        head = f"Tropical Cyclone {ident}" if status in {"", "Invest"} else f"{status} {ident}"
    else:
        head = status or "Tropical Cyclone"
    wind_txt = f"{wind:g} km/h" if wind else ""
    tail = " - ".join([p for p in [wind_txt, basin] if p])
    return f"{head} - {tail}" if tail else head


def clean_optional_text(value):
    """Return blank for missing values, including pandas NaN/NaT."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def extract_nhc_storm_name(raw_title, summary=""):
    """Extract a named NHC storm without treating forecast prose as its name."""
    raw_title = clean_optional_text(raw_title)
    summary = clean_optional_text(summary)
    status_terms = (
        "potential tropical cyclone|post-tropical cyclone|tropical storm|"
        "tropical depression|major hurricane|hurricane"
    )
    stop_words = {
        "a", "an", "as", "at", "for", "in", "is", "it", "near", "of", "on",
        "the", "to", "warning", "watch", "outlook", "advisory", "discussion",
        "forecast", "probabilities", "center", "product",
    }

    # Product titles are the safest source: "Tropical Storm Lala ..."
    match = re.search(
        rf"\b(?:{status_terms})\s+([A-Z][A-Za-z-]+)\b",
        raw_title,
        flags=re.IGNORECASE,
    )
    if match and match.group(1).casefold() not in stop_words:
        return title_case_storm_token(match.group(1))

    # NHC bulletin headlines often use "LALA FORECAST/ADVISORY...".
    match = re.search(
        r"(?:^|[. ]{2,})([A-Z][A-Z-]{2,})\s+"
        r"(?:FORECAST|ADVISORY|DISCUSSION|WIND|PUBLIC)\b",
        summary,
    )
    if match and match.group(1).casefold() not in stop_words:
        return title_case_storm_token(match.group(1))

    # Explicit status + name in the summary is acceptable only when it is not
    # preceded by forecast language such as "become a hurricane as...".
    for match in re.finditer(
        rf"\b(?:{status_terms})\s+([A-Z][A-Za-z-]+)\b",
        summary,
        flags=re.IGNORECASE,
    ):
        candidate = match.group(1)
        prefix = summary[max(0, match.start() - 35):match.start()].casefold()
        if candidate.casefold() in stop_words:
            continue
        if re.search(r"(?:forecast|expected|potential|could|may|likely)\s+(?:to\s+)?(?:become|strengthen|intensify)?\s*$", prefix):
            continue
        return title_case_storm_token(candidate)
    return ""


def extract_current_storm_status(raw_title, summary=""):
    """Extract the storm's stated current status, excluding forecast wording."""
    raw_title = clean_optional_text(raw_title)
    summary = clean_optional_text(summary)
    ordered = [
        ("Post-Tropical Cyclone", r"post-tropical cyclone"),
        ("Major Hurricane", r"major hurricane"),
        ("Hurricane", r"hurricane"),
        ("Tropical Storm", r"tropical storm"),
        ("Tropical Depression", r"tropical depression"),
        ("Potential Tropical Cyclone", r"potential tropical cyclone"),
    ]
    # The product title states what the system currently is.
    for label, pattern in ordered:
        if re.search(rf"\b{pattern}\b", raw_title, flags=re.IGNORECASE):
            return label
    # Advisory headers can also state the current classification. Ignore any
    # match immediately associated with forecast/expected language.
    for label, pattern in ordered:
        for match in re.finditer(rf"\b{pattern}\b", summary, flags=re.IGNORECASE):
            prefix = summary[max(0, match.start() - 40):match.start()].casefold()
            if re.search(r"(?:forecast|expected|could|may|likely)\s+(?:to\s+)?(?:become|strengthen|intensify)?\s*$", prefix):
                continue
            suffix = summary[match.end():match.end() + 45]
            if re.match(r"\s+[A-Z][A-Za-z-]+\b", suffix):
                return label
    return ""


def extract_forecast_status(text):
    """Return a forecast classification separately from the current status."""
    text = clean_optional_text(text)
    patterns = [
        ("Major Hurricane", r"(?:forecast|expected|likely)\s+to\s+(?:become|strengthen|intensify)(?:\s+into)?\s+(?:a\s+)?major hurricane"),
        ("Hurricane", r"(?:forecast|expected|likely)\s+to\s+(?:become|strengthen|intensify)(?:\s+into)?\s+(?:a\s+)?hurricane"),
        ("Tropical Storm", r"(?:forecast|expected|likely)\s+to\s+(?:become|strengthen|intensify)(?:\s+into)?\s+(?:a\s+)?tropical storm"),
    ]
    for label, pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return ""


def nhc_active_storm_key(row):
    if row.get("source") != "NHC" or row.get("peril") != "Tropical Cyclone":
        return ""
    raw_title = clean_optional_text(row.get("raw_title"))
    summary = clean_optional_text(row.get("summary"))
    if is_outlook(raw_title, summary):
        return ""

    storm_name = extract_nhc_storm_name(raw_title, summary)
    if storm_name:
        return f"NHC|NAME|{storm_name.casefold()}"

    atcf_id = clean_optional_text(row.get("atcf_id"))
    if atcf_id:
        return f"ATCF|{atcf_id.upper()}"
    return ""

def extract_atcf_id(text):
    """Extract an ATCF identifier such as AL032026 from text or a URL."""
    m = re.search(r"\b([A-Z]{2}\d{2}\d{4})\b", text or "", flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"/(?:al|ep|cp)(\d{2})(\d{4})", text or "", flags=re.IGNORECASE)
    if m:
        prefix = re.search(r"/(al|ep|cp)", text, flags=re.IGNORECASE).group(1).upper()
        return f"{prefix}{m.group(1)}{m.group(2)}"
    return ""


def extract_storm_status(text):
    low = clean_tc_status_text(text).lower()
    ordered = [
        ("Post-Tropical Cyclone", ["post-tropical cyclone"]),
        ("Major Hurricane", ["major hurricane", "category 3", "category 4", "category 5"]),
        ("Hurricane", ["hurricane"]),
        ("Typhoon", ["typhoon"]),
        ("Tropical Storm", ["tropical storm"]),
        ("Tropical Depression", ["tropical depression"]),
        ("Potential Tropical Cyclone", ["potential tropical cyclone"]),
        ("Invest", ["invest", "disturbance"]),
    ]
    for status, terms in ordered:
        if any(term in low for term in terms):
            return status
    return ""


def extract_pressure_mb(text):
    m = re.search(r"(?:minimum central pressure|pressure)\D{0,15}(\d{3,4})\s*(?:mb|hpa)", text or "", flags=re.IGNORECASE)
    return float(m.group(1)) if m else None


def extract_advisory_number(text):
    m = re.search(r"advisory\s+(?:number\s+)?(\d+[A-Z]?)", text or "", flags=re.IGNORECASE)
    return m.group(1).upper() if m else ""


def extract_movement(text):
    m = re.search(r"(?:moving|movement)\s*:?\s*([A-Z-]+)(?:\s+or\s+\d+\s+degrees)?\s+(?:at|near)\s+(\d+(?:\.\d+)?)\s*(mph|km/h|kt|knots)", text or "", flags=re.IGNORECASE)
    if not m:
        return "", None
    direction, speed, unit = m.group(1).upper(), float(m.group(2)), m.group(3).lower()
    if unit == "mph":
        speed *= 1.60934
    elif unit in ("kt", "knots"):
        speed *= 1.852
    return direction, speed


def extract_wind_kmh_any(text):
    value = extract_wind_kmh(text)
    if value is not None:
        return value
    m = re.search(r"(?:maximum sustained winds?|max sustained)\D{0,20}(\d+(?:\.\d+)?)\s*(mph|kt|knots)", text or "", flags=re.IGNORECASE)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    return value * (1.60934 if unit == "mph" else 1.852)


def infer_peril(title, summary, default_peril):
    text = f"{title} {summary}".lower()
    if any(x in text for x in ["tropical cyclone", "hurricane", "typhoon", "cyclone", "tropical storm", "tropical depression", "disturbance"]):
        return "Tropical Cyclone"
    if any(x in text for x in ["earthquake", "magnitude", "seismic"]):
        return "Earthquake"
    if "flood" in text:
        return "Flood"
    if any(x in text for x in ["wildfire", "forest fire", "bushfire", "wild fire"]):
        return "Wildfire"
    if any(x in text for x in ["volcano", "eruption", "volcanic"]):
        return "Volcano"
    if "drought" in text:
        return "Drought"
    if default_peril != "All":
        return default_peril
    return "Other"


def infer_alert_level(title, summary):
    text = f"{title} {summary}".lower()
    if re.search(r"\bred\b", text):
        return "Red"
    if re.search(r"\borange\b", text):
        return "Orange"
    if re.search(r"\bgreen\b", text):
        return "Green"
    return "Unknown"


def extract_magnitude(text):
    for pattern in [r"magnitude\s*([0-9]+(?:\.[0-9]+)?)", r"\bM\s*([0-9]+(?:\.[0-9]+)?)\b"]:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                return None
    return None


def extract_wind_kmh(text):
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*km/h", text, flags=re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def _clean_location(loc):
    loc = loc.strip(" .,-")
    kept = []
    for w in re.split(r"\s+", loc):
        if w in _STOP_LOC_WORDS and kept:
            break
        kept.append(w)
    return " ".join(kept).strip(" .,-")


def extract_location(raw_title, summary=""):
    for text in [raw_title, summary]:
        if not text:
            continue
        m = re.search(r"\bin\s+([A-Z][A-Za-z .'\-]+?)(?:\s+\d{1,2}[/-]\d|\s*,|\s*\(|\.|$)", text)
        if m:
            loc = _clean_location(m.group(1))
            if 2 <= len(loc) <= 40:
                return loc
    return ""


def extract_basin(raw_title, summary=""):
    text = f"{raw_title} {summary}"
    m = re.search(r"active in\s+([A-Za-z]+)", text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"\bin\s+([A-Za-z]*(?:Pacific|Atlantic|Indian|Bengal|Sea))\b", text, flags=re.IGNORECASE)
    if m:
        token = m.group(1)
        key = token.lower()
        if key in BASIN_MAP:
            return BASIN_MAP[key]
        pretty = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token)
        if any(b in pretty for b in ["Pacific", "Atlantic", "Indian", "Bengal", "Sea"]):
            return pretty
    return ""


def extract_storm_name(raw_title, summary):
    return extract_tc_identity(raw_title, summary)

def build_display_title(peril, raw_title, summary="", magnitude=None, wind=None):
    raw_title = (raw_title or "").strip()

    if peril == "Earthquake":
        loc = extract_location(raw_title, summary)
        mag_txt = f"M{magnitude:g}" if magnitude else ""
        if mag_txt and loc:
            return f"Earthquake {mag_txt} \u2014 {loc}"
        if mag_txt:
            return f"Earthquake {mag_txt}"
        return f"Earthquake \u2014 {loc}" if loc else (raw_title or "Earthquake")

    if peril == "Tropical Cyclone":
        if is_outlook(raw_title, summary):
            basin = outlook_basin(summary)
            head = f"Tropical Weather Outlook - {basin}" if basin else "Tropical Weather Outlook"
            systems = parse_outlook_systems(summary)
            if systems:
                bits = ", ".join(f"{c} {o}%" if o is not None else c for c, o in systems)
                return f"{head} ({bits})"
            return head
        status = extract_storm_status(f"{raw_title} {summary}")
        return build_tc_title(raw_title, summary, status=status, wind=wind)

    if peril in ("Flood", "Volcano", "Drought", "Wildfire"):
        loc = extract_location(raw_title, summary)
        return f"{peril} \u2014 {loc}" if loc else (raw_title or peril)

    loc = extract_location(raw_title, summary)
    if loc:
        return f"{peril} \u2014 {loc}"
    if peril.lower().split()[0] in raw_title.lower():
        return raw_title
    return f"{peril}: {raw_title}" if raw_title else peril


def infer_tier(row):
    text = f"{row.get('title','')} {row.get('summary','')}".lower()
    text = text.replace("national hurricane center", "").replace("hurricane center", "")
    peril = row.get("peril", "Other")
    alert = row.get("alert_level", "Unknown")
    mag = row.get("magnitude")
    wind = row.get("wind_kmh")

    if alert == "Red":
        return "Critical"
    if alert == "Orange":
        return "Watch"
    if peril == "Earthquake":
        if mag and mag >= 7.0:
            return "Critical"
        if mag and mag >= 6.0:
            return "Watch"
        if mag and mag >= 5.5:
            return "Advisory"
    if peril == "Tropical Cyclone":
        if "tropical weather outlook" in text:
            return "Advisory"
        status = clean_optional_text(row.get("status"))
        if status == "Major Hurricane" or (wind and wind >= 178):
            return "Critical"
        if status in {"Hurricane", "Typhoon"} or (wind and wind >= 119):
            return "Watch"
        if status in {"Tropical Storm", "Tropical Depression", "Potential Tropical Cyclone", "Invest"}:
            return "Advisory"
    if peril in ["Flood", "Volcano", "Wildfire"] and alert in ["Unknown", "Green"]:
        if any(x in text for x in ["evacuation", "displaced", "fatal", "deaths", "emergency"]):
            return "Watch"
    return "Info"


@st.cache_data(ttl=300, show_spinner=False)
def fetch_feed(feed):
    parsed = feedparser.parse(feed["url"], request_headers=REQUEST_HEADERS)
    rows = []
    for entry in parsed.entries:
        raw_title = clean_html(entry.get("title", ""))
        summary = clean_html(entry.get("summary", entry.get("description", "")))
        dt = parse_entry_dt(entry)
        peril = infer_peril(raw_title, summary, feed["default_peril"])
        combo = f"{raw_title} {summary}"
        magnitude = extract_magnitude(combo)
        wind_kmh = extract_wind_kmh_any(combo)
        lat, lon = extract_latlon(entry)
        link = entry.get("link", feed["url"])
        atcf_id = extract_atcf_id(f"{combo} {link}")
        status = extract_current_storm_status(raw_title, summary) if peril == "Tropical Cyclone" else ""
        forecast_status = extract_forecast_status(combo) if peril == "Tropical Cyclone" else ""
        pressure_mb = extract_pressure_mb(combo)
        advisory_number = extract_advisory_number(combo)
        move_direction, move_kmh = extract_movement(combo)
        product_type = extract_nhc_product_type(raw_title, summary, link) if feed["source"] == "NHC" else ""
        row = {
            "source": feed["source"], "feed": feed["name"], "url": feed["url"],
            "raw_title": raw_title or "Untitled",
            "title": build_display_title(peril, raw_title or "Untitled item", summary, magnitude, wind_kmh),
            "summary": summary, "link": link,
            "published_utc": dt,
            "published": dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "Unknown",
            "age": age_label(dt), "peril": peril,
            "alert_level": infer_alert_level(raw_title, summary),
            "magnitude": magnitude, "wind_kmh": wind_kmh,
            "status": status, "forecast_status": forecast_status,
            "atcf_id": atcf_id, "pressure_mb": pressure_mb,
            "advisory_number": advisory_number, "move_direction": move_direction,
            "move_kmh": move_kmh, "formation_chance": None,
            "disturbance_code": "", "acres": None, "contained_pct": None,
            "lat": lat, "lon": lon, "product_type": product_type,
            "source_product_count": 1, "source_products": product_type,
        }
        row["tier"] = infer_tier(row)
        systems = extract_formation_chances(summary) if is_outlook(raw_title, summary) else {}
        if systems:
            for code, chance in systems.items():
                system_row = dict(row)
                system_row["disturbance_code"] = code
                system_row["formation_chance"] = chance
                system_row["status"] = "Invest"
                system_row["raw_title"] = f"{code} Tropical Disturbance"
                system_row["title"] = f"Tropical Disturbance {code}" + (f" - {chance}% formation chance" if chance is not None else "")
                system_row["link"] = link
                rows.append(system_row)
        else:
            rows.append(row)
    return rows


@st.cache_data(ttl=300, show_spinner=False)
def fetch_calfire():
    rows = []
    try:
        resp = requests.get(CALFIRE_URL, headers=REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
        incidents = resp.json()
    except Exception as exc:
        return [{
            "source": "CAL FIRE", "feed": "CAL FIRE Incidents", "url": CALFIRE_URL,
            "raw_title": "CAL FIRE feed unavailable", "title": "Wildfire: CAL FIRE feed unavailable",
            "summary": str(exc), "link": "https://www.fire.ca.gov/incidents",
            "published_utc": None, "published": "Unknown", "age": "Unknown",
            "peril": "Wildfire", "alert_level": "Unknown", "magnitude": None,
            "wind_kmh": None, "lat": None, "lon": None, "tier": "Info",
        }]

    for inc in incidents:
        name = inc.get("Name", "Unnamed incident")
        county = inc.get("County", "")
        location = inc.get("Location", "")
        acres = inc.get("AcresBurned")
        contained = inc.get("PercentContained")
        updated = parse_dt(inc.get("Updated")) or parse_dt(inc.get("Started"))
        url = inc.get("Url") or "https://www.fire.ca.gov/incidents"
        lat = to_float(inc.get("Latitude"))
        lon = to_float(inc.get("Longitude"))
        if lat == 0 and lon == 0:
            lat = lon = None

        acres_txt = f"{int(acres):,} acres" if isinstance(acres, (int, float)) else "acres n/a"
        cont_txt = f"{int(contained)}% contained" if isinstance(contained, (int, float)) else "containment n/a"
        county_txt = f"{county} County" if county else location

        title = f"Wildfire: {name} (CA, {county_txt}) - {acres_txt}, {cont_txt}"
        summary = f"California, USA. Location: {location or county_txt}. {acres_txt}. {cont_txt}."

        if isinstance(acres, (int, float)) and acres >= 10000 and (not isinstance(contained, (int, float)) or contained < 50):
            tier = "Critical"
        elif isinstance(acres, (int, float)) and acres >= 1000:
            tier = "Watch"
        elif isinstance(acres, (int, float)) and acres >= 100:
            tier = "Advisory"
        else:
            tier = "Info"

        rows.append({
            "source": "CAL FIRE", "feed": "CAL FIRE Incidents", "url": CALFIRE_URL,
            "raw_title": name, "title": title, "summary": summary, "link": url,
            "published_utc": updated,
            "published": updated.strftime("%Y-%m-%d %H:%M UTC") if updated else "Unknown",
            "age": age_label(updated), "peril": "Wildfire", "alert_level": "Unknown",
            "magnitude": None, "wind_kmh": None, "status": "", "atcf_id": "",
            "pressure_mb": None, "advisory_number": "", "move_direction": "", "move_kmh": None,
            "formation_chance": None, "disturbance_code": "",
            "acres": acres if isinstance(acres, (int, float)) else None,
            "contained_pct": contained if isinstance(contained, (int, float)) else None,
            "lat": lat, "lon": lon, "tier": tier,
        })
    return rows


def tracking_key(row):
    nhc_key = nhc_active_storm_key(row)
    if nhc_key:
        return nhc_key
    if row.get("atcf_id"):
        return f"ATCF|{row['atcf_id']}"
    if row.get("disturbance_code"):
        return f"INVEST|{row['disturbance_code']}"
    if row.get("source") == "CAL FIRE":
        return f"CALFIRE|{str(row.get('raw_title', '')).strip().lower()}"
    name = extract_storm_name(str(row.get("raw_title", "")), str(row.get("summary", "")))
    if row.get("peril") == "Tropical Cyclone" and name:
        return f"CYCLONE|{name.lower()}"
    return f"{row.get('source','')}|{str(row.get('raw_title','')).strip().lower()}"


@st.cache_resource(show_spinner=False)
def supabase_client():
    """Create the server-side Supabase client from private Streamlit secrets."""
    if create_client is None:
        return None
    try:
        config = st.secrets["supabase"]
        url = str(config["url"]).strip()
        key = str(config["key"]).strip()
    except (KeyError, TypeError, AttributeError):
        return None
    if not url or not key:
        return None
    return create_client(url, key)


def supabase_health_check():
    """Verify that CatWatch can read the history schema without writing data."""
    if create_client is None:
        return False, "Supabase package is not installed."
    try:
        config = st.secrets["supabase"]
        if not str(config["url"]).strip() or not str(config["key"]).strip():
            return False, "Supabase secrets are incomplete."
    except (KeyError, TypeError, AttributeError):
        return False, "Supabase secrets are not configured."
    try:
        client = supabase_client()
        if client is None:
            return False, "Supabase client could not be created."
        client.table("events").select("event_key").limit(1).execute()
        return True, "Connected to persistent event history."
    except Exception as exc:
        # Do not expose credentials or a full backend exception in the UI.
        message = str(exc).replace("\n", " ").strip()
        if len(message) > 180:
            message = message[:177] + "..."
        return False, f"Connection failed: {message}"


HISTORY_FIELDS = [
    "status", "forecast_status", "wind_kmh", "pressure_mb",
    "formation_chance", "move_direction", "move_kmh", "lat", "lon",
    "magnitude", "acres", "contained_pct", "alert_level", "tier",
    "advisory_number", "raw_title", "summary",
]

HISTORY_LABELS = {
    "status": "Status", "forecast_status": "Forecast", "wind_kmh": "Wind",
    "pressure_mb": "Pressure", "formation_chance": "Formation chance",
    "move_direction": "Movement direction", "move_kmh": "Movement speed",
    "lat": "Latitude", "lon": "Longitude", "magnitude": "Magnitude",
    "acres": "Area", "contained_pct": "Containment", "alert_level": "Alert",
    "tier": "Tier", "advisory_number": "Advisory", "raw_title": "Source title",
    "summary": "Source text",
}


def history_value(value):
    """Convert pandas/numpy values into stable JSON-safe Python values."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float):
        return round(value, 4)
    return value


def iso_utc(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    parsed = parse_dt(str(value))
    return parsed.isoformat() if parsed else None


def observation_state(row):
    return {field: history_value(row.get(field)) for field in HISTORY_FIELDS}


def observation_hash(state):
    payload = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_history_value(field, value):
    if value is None or value == "":
        return "not stated"
    try:
        number = float(value)
        if field == "wind_kmh":
            return f"{number:.0f} km/h"
        if field == "pressure_mb":
            return f"{number:.0f} mb"
        if field in {"formation_chance", "contained_pct"}:
            return f"{number:.0f}%"
        if field == "move_kmh":
            return f"{number:.0f} km/h"
        if field == "magnitude":
            return f"M{number:g}"
        if field == "acres":
            return f"{number:,.0f} acres"
        if field in {"lat", "lon"}:
            return f"{number:.3f}"
    except (TypeError, ValueError):
        pass
    if field == "forecast_status" and value:
        return f"Expected to become {value}"
    return str(value)


def initial_state_text(current):
    """Build a concise baseline summary while full values remain in observations."""
    parts = []
    for field in ("status", "magnitude", "wind_kmh", "pressure_mb", "formation_chance", "acres", "contained_pct"):
        value = current.get(field)
        if value not in (None, ""):
            parts.append(format_history_value(field, value))
    alert = current.get("alert_level")
    if alert not in (None, "", "Unknown"):
        parts.append(f"{alert} alert")
    tier = current.get("tier")
    if tier not in (None, ""):
        parts.append(str(tier))
    return " · ".join(parts[:6])


def build_change_records(previous, current):
    if previous is None:
        baseline = initial_state_text(current)
        changes = [("first_observed", None, None, "First observed by CatWatch")]
        if baseline:
            changes.append(("initial_state", None, None, f"Initial state: {baseline}"))
        return changes

    changes = []
    for field in HISTORY_FIELDS:
        old = previous.get(field)
        new = current.get(field)
        if old == new:
            continue
        # Full source text and exact coordinates remain stored for audit. Source
        # wording alone is a genuine observation update, but it is not a user
        # notification unless another tracked event field also changed.
        if field in {"raw_title", "summary"}:
            continue
        # Small coordinate movements create noise and are retained only in the
        # underlying observation record.
        if field in {"lat", "lon"}:
            continue
        label = HISTORY_LABELS[field]
        changes.append((
            field, old, new,
            f"{label}: {format_history_value(field, old)} → {format_history_value(field, new)}",
        ))
    raw_changed = any(previous.get(field) != current.get(field) for field in ("raw_title", "summary"))
    if raw_changed and not changes:
        changes.append(("source_text", None, None, "Source text updated"))
    return changes

def persist_event_history(df):
    """Write only genuinely changed event states to Supabase."""
    client = supabase_client()
    if client is None or df.empty:
        return df, "Persistent history unavailable."

    out = df.copy()
    row_changes = []
    failures = []
    detected_at = datetime.now(timezone.utc).isoformat()

    for _, row in out.iterrows():
        event_key = str(row.get("event_id") or tracking_key(row))
        source_updated_at = iso_utc(row.get("published_utc"))
        state = observation_state(row)
        state_hash = observation_hash(state)
        current_change_texts = []
        try:
            event_payload = {
                "event_key": event_key,
                "peril": clean_optional_text(row.get("peril")) or "Other",
                "source": clean_optional_text(row.get("source")) or "Unknown",
                "display_name": clean_optional_text(row.get("title")) or event_key,
                "last_seen_at": detected_at,
                "active": True,
                "latest_source_url": clean_optional_text(row.get("link")) or None,
                "updated_at": detected_at,
            }
            client.table("events").upsert(event_payload, on_conflict="event_key").execute()

            prior_response = (
                client.table("observations")
                .select("*")
                .eq("event_key", event_key)
                .order("detected_at", desc=True)
                .limit(1)
                .execute()
            )
            prior = prior_response.data[0] if prior_response.data else None
            if prior and prior.get("observation_hash") == state_hash:
                row_changes.append([])
                continue

            observation_payload = {
                "event_key": event_key,
                "detected_at": detected_at,
                "source_updated_at": source_updated_at,
                "status": state.get("status"),
                "forecast_status": state.get("forecast_status"),
                "wind_kmh": state.get("wind_kmh"),
                "pressure_mb": state.get("pressure_mb"),
                "formation_chance": state.get("formation_chance"),
                "movement_direction": state.get("move_direction"),
                "movement_kmh": state.get("move_kmh"),
                "latitude": state.get("lat"),
                "longitude": state.get("lon"),
                "magnitude": state.get("magnitude"),
                "acres": state.get("acres"),
                "contained_pct": state.get("contained_pct"),
                "alert_level": state.get("alert_level"),
                "tier": state.get("tier"),
                "advisory_number": state.get("advisory_number"),
                "raw_title": state.get("raw_title"),
                "raw_summary": state.get("summary"),
                "source_url": clean_optional_text(row.get("link")) or None,
                "observation_hash": state_hash,
            }
            try:
                inserted = client.table("observations").insert(observation_payload).execute()
            except Exception:
                existing = (
                    client.table("observations")
                    .select("observation_id")
                    .eq("event_key", event_key)
                    .eq("observation_hash", state_hash)
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    row_changes.append([])
                    continue
                raise
            if not inserted.data:
                existing = (
                    client.table("observations")
                    .select("observation_id")
                    .eq("event_key", event_key)
                    .eq("observation_hash", state_hash)
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    row_changes.append([])
                    continue
                raise RuntimeError("Observation insert returned no record")
            observation_id = inserted.data[0]["observation_id"]

            previous_state = None
            if prior:
                previous_state = {
                    "status": prior.get("status"), "forecast_status": prior.get("forecast_status"),
                    "wind_kmh": prior.get("wind_kmh"), "pressure_mb": prior.get("pressure_mb"),
                    "formation_chance": prior.get("formation_chance"),
                    "move_direction": prior.get("movement_direction"), "move_kmh": prior.get("movement_kmh"),
                    "lat": prior.get("latitude"), "lon": prior.get("longitude"),
                    "magnitude": prior.get("magnitude"), "acres": prior.get("acres"),
                    "contained_pct": prior.get("contained_pct"), "alert_level": prior.get("alert_level"),
                    "tier": prior.get("tier"), "advisory_number": prior.get("advisory_number"),
                    "raw_title": prior.get("raw_title"), "summary": prior.get("raw_summary"),
                }
            changes = build_change_records(previous_state, state)

            change_payloads = []
            for field, old, new, text in changes:
                change_payloads.append({
                    "event_key": event_key,
                    "observation_id": observation_id,
                    "detected_at": detected_at,
                    "source_updated_at": source_updated_at,
                    "field_name": field,
                    "previous_value": None if old is None else str(old),
                    "current_value": None if new is None else str(new),
                    "change_text": text,
                })
            if change_payloads:
                client.table("changes").insert(change_payloads).execute()
            current_change_texts = [item[3] for item in changes if item[0] != "source_text"]
            row_changes.append(current_change_texts)
        except Exception as exc:
            failures.append(f"{event_key}: {str(exc)[:120]}")
            row_changes.append([])

    out["history_changes"] = row_changes
    if failures:
        sample = " | ".join(failures[:3])
        return out, f"{len(failures)} history write error(s). {sample}"
    changed_count = sum(bool(items) for items in row_changes)
    return out, f"History current. {changed_count} genuine event update(s) recorded."


def get_event_timeline(event_key, limit=25):
    client = supabase_client()
    if client is None:
        return []
    try:
        response = (
            client.table("changes")
            .select("change_id,observation_id,detected_at,source_updated_at,change_text,field_name")
            .eq("event_key", event_key)
            .order("source_updated_at", desc=True)
            .order("detected_at", desc=True)
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
        return []


NON_CHANGE_FIELDS = {"first_observed", "initial_state", "source_text"}


def genuine_timeline_changes(changes):
    """Exclude the entire baseline observation, including legacy baseline fields."""
    baseline_observation_ids = {
        item.get("observation_id")
        for item in changes
        if item.get("field_name") in {"first_observed", "initial_state"}
        and item.get("observation_id") is not None
    }
    return [
        item for item in changes
        if item.get("field_name") not in NON_CHANGE_FIELDS
        and item.get("observation_id") not in baseline_observation_ids
    ]


def timeline_update_count(changes):
    genuine = genuine_timeline_changes(changes)
    return len({item.get("observation_id") for item in genuine if item.get("observation_id") is not None})


def get_history_events():
    client = supabase_client()
    if client is None:
        return []
    try:
        response = (
            client.table("events")
            .select("event_key,display_name,peril,source,first_seen_at,last_seen_at,active")
            .order("last_seen_at", desc=True)
            .limit(500)
            .execute()
        )
        return response.data or []
    except Exception:
        return []


def get_event_observations(event_key, limit=100):
    client = supabase_client()
    if client is None:
        return []
    try:
        response = (
            client.table("observations")
            .select("*")
            .eq("event_key", event_key)
            .order("source_updated_at", desc=True)
            .order("detected_at", desc=True)
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
        return []


def timeline_timestamp(item):
    value = item.get("source_updated_at") or item.get("detected_at")
    parsed = parse_dt(value)
    return parsed.strftime("%d %b %Y · %H:%M UTC") if parsed else "Time unavailable"


def render_event_timeline(event_key, compact=True):
    all_changes = get_event_timeline(event_key, limit=100)
    changes = genuine_timeline_changes(all_changes)
    if not changes:
        st.caption("No changes recorded since first observed.")
        return
    grouped = {}
    for item in changes:
        observation_id = item.get("observation_id")
        grouped.setdefault(observation_id, []).append(item)
    for items in grouped.values():
        st.markdown(f"**{timeline_timestamp(items[0])}**")
        for item in items:
            st.markdown(f"- {item.get('change_text', 'Update recorded')}")
        detected = parse_dt(items[0].get("detected_at"))
        source_time = parse_dt(items[0].get("source_updated_at"))
        if detected and source_time and abs((detected - source_time).total_seconds()) >= 60:
            st.caption(f"Detected by CatWatch: {detected.strftime('%d %b %Y · %H:%M UTC')}")

def render_history_tab():
    st.subheader("Event History")
    st.caption("Persistent timelines containing actual source changes, not dashboard refreshes.")
    events = get_history_events()
    if not events:
        st.info("No persistent event history has been recorded yet.")
        return
    events_df = pd.DataFrame(events)
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        peril_options = ["All"] + sorted(events_df["peril"].dropna().unique().tolist())
        peril_filter = st.selectbox("Peril", peril_options, key="history_peril")
    with c2:
        source_options = ["All"] + sorted(events_df["source"].dropna().unique().tolist())
        source_filter = st.selectbox("Source", source_options, key="history_source")
    with c3:
        search = st.text_input("Find event", placeholder="Storm, fire, earthquake...", key="history_search")
    view = events_df.copy()
    if peril_filter != "All":
        view = view[view["peril"] == peril_filter]
    if source_filter != "All":
        view = view[view["source"] == source_filter]
    if search:
        view = view[view["display_name"].str.contains(search, case=False, na=False)]
    if view.empty:
        st.info("No events match the selected history filters.")
        return
    labels = {
        row["event_key"]: f"{row['display_name']} · {row['source']}"
        for _, row in view.iterrows()
    }
    event_key = st.selectbox("Event", list(labels), format_func=lambda key: labels[key], key="history_event")
    selected = view[view["event_key"] == event_key].iloc[0]
    st.markdown(f"### {selected['display_name']}")
    st.caption(f"{selected['peril']} · {selected['source']} · Event key: {event_key}")
    observations = get_event_observations(event_key)
    if observations:
        latest = observations[0]
        metrics = []
        for label, field in [("Status", "status"), ("Forecast", "forecast_status"), ("Wind", "wind_kmh"), ("Pressure", "pressure_mb"), ("Area", "acres"), ("Containment", "contained_pct")]:
            value = latest.get(field)
            if value not in (None, ""):
                metrics.append((label, format_history_value(field, value)))
        if metrics:
            columns = st.columns(min(len(metrics), 4))
            for index, (label, value) in enumerate(metrics):
                columns[index % len(columns)].metric(label, value)
    st.markdown("#### Timeline")
    render_event_timeline(event_key, compact=False)
    if observations:
        with st.expander("Original source updates"):
            for observation in observations:
                stamp = observation.get("source_updated_at") or observation.get("detected_at")
                st.markdown(f"**{timeline_timestamp({'source_updated_at': stamp})}**")
                st.write(observation.get("raw_title") or "Untitled source update")
                if observation.get("raw_summary"):
                    st.caption(observation["raw_summary"])
                if observation.get("source_url"):
                    st.markdown(f"[Open source]({observation['source_url']})")
                st.divider()


def history_connection():
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS observations (
        tracking_key TEXT NOT NULL, observed_at TEXT NOT NULL, published TEXT,
        title TEXT, peril TEXT, tier TEXT, status TEXT, wind_kmh REAL,
        pressure_mb REAL, formation_chance REAL, acres REAL, contained_pct REAL,
        advisory_number TEXT, move_direction TEXT, move_kmh REAL,
        PRIMARY KEY (tracking_key, observed_at)
    )""")
    return conn


def material_changes(df, refresh_token):
    """Compare current observations with SQLite history and classify meaningful updates."""
    out = df.copy()
    conn = history_connection()
    changes_col, material_col, update_col = [], [], []
    now = datetime.now(timezone.utc).isoformat()
    numeric = ["wind_kmh", "pressure_mb", "formation_chance", "acres", "contained_pct", "move_kmh"]
    status_rank = {"Invest": 0, "Potential Tropical Cyclone": 1, "Tropical Depression": 2,
                   "Tropical Storm": 3, "Hurricane": 4, "Typhoon": 4, "Major Hurricane": 5,
                   "Post-Tropical Cyclone": -1}

    for _, row in out.iterrows():
        key = tracking_key(row)
        prior_row = conn.execute("SELECT status, wind_kmh, pressure_mb, formation_chance, acres, contained_pct, advisory_number, move_direction, move_kmh FROM observations WHERE tracking_key=? ORDER BY observed_at DESC LIMIT 1", (key,)).fetchone()
        prior = dict(zip(["status", "wind_kmh", "pressure_mb", "formation_chance", "acres", "contained_pct", "advisory_number", "move_direction", "move_kmh"], prior_row)) if prior_row else {}
        changes, important, kind = [], False, "Routine update"
        status = row.get("status") or ""

        if not prior and row.get("peril") == "Tropical Cyclone" and status in ("Tropical Depression", "Tropical Storm", "Hurricane", "Typhoon", "Major Hurricane", "Potential Tropical Cyclone"):
            changes.append(f"New {status}")
            important, kind = True, "New cyclone"
        elif prior.get("status") and status and prior["status"] != status:
            changes.append(f"Status: {prior['status']} -> {status}")
            if status_rank.get(status, 0) > status_rank.get(prior["status"], 0):
                important, kind = True, "Cyclone upgrade"

        def delta(field):
            current, previous = row.get(field), prior.get(field)
            if current is None or pd.isna(current) or previous is None:
                return None
            return float(current) - float(previous)

        d = delta("wind_kmh")
        if d is not None and abs(d) >= 1:
            changes.append(f"Wind {d:+.0f} km/h")
            if d >= 15:
                important, kind = True, "Rapid strengthening"
        d = delta("pressure_mb")
        if d is not None and abs(d) >= 1:
            changes.append(f"Pressure {d:+.0f} mb")
            if d <= -5:
                important, kind = True, "Pressure falling"
        d = delta("formation_chance")
        if d is not None and abs(d) >= 1:
            changes.append(f"Formation chance {d:+.0f} pts")
            cur = float(row.get("formation_chance"))
            prev = float(prior.get("formation_chance"))
            if d >= 20 or any(prev < x <= cur for x in (40, 70, 90)):
                important, kind = True, "Formation chance increased"
        d = delta("acres")
        if d is not None and abs(d) >= 1:
            changes.append(f"Area {d:+,.0f} acres")
            if d >= 500:
                important, kind = True, "Wildfire expanded"
        d = delta("contained_pct")
        if d is not None and abs(d) >= 1:
            changes.append(f"Containment {d:+.0f} pts")
            if d <= -5:
                important, kind = True, "Containment deteriorated"
        if prior.get("advisory_number") and row.get("advisory_number") and prior["advisory_number"] != row.get("advisory_number"):
            changes.append(f"Advisory {prior['advisory_number']} -> {row.get('advisory_number')}")

        changes_col.append(changes)
        material_col.append(important)
        update_col.append(kind if important else ("Updated" if changes else "No material change"))
        values = [key, now, row.get("published"), row.get("title"), row.get("peril"), row.get("tier"), status]
        for field in ["wind_kmh", "pressure_mb", "formation_chance", "acres", "contained_pct"]:
            value = row.get(field)
            values.append(None if value is None or pd.isna(value) else float(value))
        values += [row.get("advisory_number") or "", row.get("move_direction") or ""]
        mv = row.get("move_kmh")
        values.append(None if mv is None or pd.isna(mv) else float(mv))

        # Insert by explicit column name so the app remains compatible with both
        # the original 15-column database and the newer 16-column database that
        # includes source_stamp.
        observation_columns = [
            "tracking_key", "observed_at", "published", "title", "peril", "tier",
            "status", "wind_kmh", "pressure_mb", "formation_chance", "acres",
            "contained_pct", "advisory_number", "move_direction", "move_kmh",
        ]
        database_columns = {
            column[1] for column in conn.execute("PRAGMA table_info(observations)").fetchall()
        }
        if "source_stamp" in database_columns:
            source_stamp = "|".join([
                str(row.get("published") or ""),
                str(row.get("advisory_number") or ""),
                str(status),
                str(row.get("wind_kmh") or ""),
                str(row.get("pressure_mb") or ""),
                str(row.get("formation_chance") or ""),
                str(row.get("acres") or ""),
                str(row.get("contained_pct") or ""),
            ])
            observation_columns.insert(1, "source_stamp")
            values.insert(1, source_stamp)

        placeholders = ", ".join("?" for _ in observation_columns)
        column_names = ", ".join(observation_columns)
        conn.execute(
            f"INSERT OR REPLACE INTO observations ({column_names}) VALUES ({placeholders})",
            values,
        )

    conn.commit()
    conn.close()
    out["changes"], out["is_material_update"], out["update_type"] = changes_col, material_col, update_col
    return out


def render_headline(filtered, new_visible):
    if filtered.empty:
        return

    critical = int((filtered["tier"] == "Critical").sum())
    watch = int((filtered["tier"] == "Watch").sum())

    newest = filtered.sort_values(
        "published_sort",
        ascending=False,
        na_position="last"
    ).head(1)

    newest_text = "No events"
    if not newest.empty:
        newest_text = (
            f"{newest.iloc[0]['title']} "
            f"• {newest.iloc[0]['age']}"
        )

    latest_change = "No material changes detected"

    changed = filtered[
        filtered["changes"].apply(
            lambda x: isinstance(x, list) and len(x) > 0
        )
    ]

    if not changed.empty:
        changed = changed.sort_values(
            "published_sort",
            ascending=False,
            na_position="last"
        )

        r = changed.iloc[0]

        latest_change = (
            f"{r['title']} • "
            + " | ".join(r["changes"])
        )

    highest_risk = "No Critical events"

    risk_df = filtered[
        filtered["tier"].isin(["Critical", "Watch"])
    ]

    if not risk_df.empty:
        r = risk_df.iloc[0]

        risk_bits = []

        if pd.notna(r.get("wind_kmh")):
            risk_bits.append(f"{r['wind_kmh']:.0f} km/h")

        if pd.notna(r.get("acres")):
            risk_bits.append(f"{r['acres']:,.0f} acres")

        highest_risk = (
            r["title"]
            + (" • " + " • ".join(risk_bits) if risk_bits else "")
        )

    with st.container(border=True):

        c1, c2, c3 = st.columns(3)

        with c1:
            st.markdown(
                f"""
### 🔴 Critical: {critical}

### 🟠 Watch: {watch}

### 🆕 New: {new_visible}
"""
            )

        with c2:
            st.markdown("#### 🚨 HIGHEST RISK")
            st.write(highest_risk)

        with c3:
            st.markdown("#### 🆕 NEWEST EVENT")
            st.write(newest_text)

        st.divider()

        st.markdown(
            f"#### 📈 LATEST CHANGE\n{latest_change}"
        )

def render_material_updates(df):
    updates = df[df["is_material_update"]].copy() if "is_material_update" in df else df.iloc[0:0]
    st.subheader("Latest Material Updates")
    if updates.empty:
        st.caption("No material changes detected on this refresh.")
        return
    updates = updates.sort_values("published_sort", ascending=False, na_position="last")
    for _, row in updates.head(8).iterrows():
        icon = peril_icon(row.get("peril", "Other"))
        change_text = " | ".join(row.get("changes") or [])
        st.markdown(f"**{icon} {row.get('update_type')}: {row.get('title')}**  ")
        st.caption(f"{change_text} | {row.get('source')} | {row.get('age')}")


def stable_event_id(row):
    nhc_key = nhc_active_storm_key(row)
    if nhc_key:
        return nhc_key
    if row.get("disturbance_code"):
        return f"INVEST|{row.get('disturbance_code')}"
    if row.get("source") == "CAL FIRE":
        return f"CALFIRE|{str(row.get('raw_title', '')).strip().lower()}"
    return "|".join([str(row.get("source", "")), str(row.get("raw_title", "")).strip(), str(row.get("link", "")).strip()])


def collapse_nhc_storm_products(df):
    """Collapse multiple NHC products for the same active storm into one storm card."""
    if df.empty:
        return df
    work = df.copy()
    work["published_sort"] = pd.to_datetime(work.get("published_utc"), errors="coerce", utc=True)
    work["_nhc_storm_key"] = work.apply(nhc_active_storm_key, axis=1)
    mask = work["_nhc_storm_key"].astype(str).ne("")
    if not mask.any():
        return work.drop(columns=["_nhc_storm_key"], errors="ignore")
    priority = {"Advisory": 90, "Public Advisory": 85, "Forecast/Advisory": 80, "Wind Probabilities": 55, "Discussion": 35, "NHC Product": 25}
    collapsed = []
    used = set()
    for key, group in work[mask].groupby("_nhc_storm_key", sort=False):
        g = group.copy()
        g["_product_priority"] = g.get("product_type").map(priority).fillna(10)
        g["_metric_score"] = 0
        for col in ["wind_kmh", "pressure_mb", "advisory_number", "move_kmh", "lat", "lon"]:
            if col in g.columns:
                g["_metric_score"] += g[col].notna().astype(int)
        g = g.sort_values(["_metric_score", "_product_priority", "published_sort"], ascending=[False, False, False], na_position="last")
        best = g.iloc[0].copy()
        latest = g["published_sort"].max()
        if pd.notna(latest):
            latest_py = latest.to_pydatetime() if hasattr(latest, "to_pydatetime") else latest
            best["published_utc"] = latest_py
            best["published"] = latest_py.strftime("%Y-%m-%d %H:%M UTC")
            best["age"] = age_label(latest_py)
            best["published_sort"] = pd.Timestamp(latest_py)
        status_rank = {
            "Potential Tropical Cyclone": 0, "Tropical Depression": 1,
            "Tropical Storm": 2, "Hurricane": 3, "Major Hurricane": 4,
            "Post-Tropical Cyclone": 5,
        }
        current_statuses = [
            clean_optional_text(value)
            for value in g.get("status", pd.Series(dtype=str)).tolist()
            if clean_optional_text(value)
        ]
        if current_statuses:
            best["status"] = max(current_statuses, key=lambda value: status_rank.get(value, -1))
        forecast_statuses = [
            clean_optional_text(value)
            for value in g.get("forecast_status", pd.Series(dtype=str)).tolist()
            if clean_optional_text(value)
        ]
        best["forecast_status"] = (
            max(forecast_statuses, key=lambda value: status_rank.get(value, -1))
            if forecast_statuses else ""
        )
        products = [p for p in g.get("product_type", pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if p]
        best["source_product_count"] = int(len(g))
        best["source_products"] = ", ".join(products)
        best["event_id"] = str(key)
        best["dedup_key"] = str(key)
        wind_val = best.get("wind_kmh") if pd.notna(best.get("wind_kmh")) else None
        storm_name = extract_nhc_storm_name(best.get("raw_title", ""), best.get("summary", ""))
        title_source = f"{best.get('status', '')} {storm_name}".strip() if storm_name else best.get("raw_title", "")
        best["title"] = build_tc_title(title_source, "", status=best.get("status", ""), wind=wind_val)
        best["tier"] = infer_tier(best)
        collapsed.append(best.drop(labels=[c for c in ["_metric_score", "_product_priority"] if c in best.index]))
        used.update(g.index.tolist())
    untouched = work.drop(index=list(used))
    out = pd.concat([untouched, pd.DataFrame(collapsed)], ignore_index=True, sort=False)
    return out.drop(columns=["_nhc_storm_key"], errors="ignore")

@st.cache_data(ttl=300, show_spinner=True)
def load_events():
    all_rows = []
    for feed in FEEDS:
        try:
            all_rows.extend(fetch_feed(feed))
        except Exception as exc:
            all_rows.append({
                "source": feed["source"], "feed": feed["name"], "url": feed["url"],
                "raw_title": "Feed error", "title": f"{feed['name']} unavailable",
                "summary": str(exc), "link": feed["url"], "published_utc": None,
                "published": "Unknown", "age": "Unknown",
                "peril": feed["default_peril"] if feed["default_peril"] != "All" else "Other",
                "alert_level": "Unknown", "magnitude": None, "wind_kmh": None,
                "lat": None, "lon": None, "tier": "Info",
            })
    all_rows.extend(fetch_calfire())

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df
    df["published_sort"] = pd.to_datetime(df["published_utc"], errors="coerce", utc=True)
    df = collapse_nhc_storm_products(df)
    df["event_id"] = df.apply(stable_event_id, axis=1)
    df["tier_rank"] = df["tier"].map(TIER_ORDER).fillna(9)
    df["alert_rank"] = df["alert_level"].map(ALERT_ORDER).fillna(9)
    df["published_sort"] = pd.to_datetime(df["published_utc"], errors="coerce", utc=True)
    df = df.drop_duplicates(subset=["event_id"], keep="first")

    def dedup_key(r):
        nhc_key = nhc_active_storm_key(r)
        if nhc_key:
            return nhc_key
        if is_outlook(r["raw_title"], r["summary"]):
            codes = sorted({c for c, _ in parse_outlook_systems(r["summary"])})
            return "OUTLOOK|" + ("-".join(codes) if codes else outlook_basin(r["summary"]))
        return r["event_id"]

    df["dedup_key"] = df.apply(dedup_key, axis=1)
    df = df.sort_values("published_sort", ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["dedup_key"], keep="first")
    df = df.sort_values(["tier_rank", "alert_rank", "published_sort"], ascending=[True, True, False], na_position="last")
    return df.reset_index(drop=True)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_news(query, source_domains):
    q = query.strip()
    if "when:" not in q.lower():
        q = f"{q} when:30d"
    if source_domains:
        site_filter = " OR ".join(f"site:{d}" for d in source_domains)
        q = f"{q} ({site_filter})"
    encoded = urllib.parse.quote(q)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    parsed = feedparser.parse(url, request_headers=REQUEST_HEADERS)
    items = []
    for entry in parsed.entries[:15]:
        dt = parse_entry_dt(entry)
        src = ""
        if entry.get("source"):
            try:
                src = clean_html(entry["source"].get("title", ""))
            except Exception:
                src = ""
        items.append({
            "title": clean_html(entry.get("title", "")),
            "link": entry.get("link", ""),
            "source": src,
            "age": age_label(dt),
        })
    return items


def news_query_from_row(row):
    peril = row.get("peril", "Other")
    raw = str(row.get("raw_title", ""))
    title = str(row.get("title", ""))
    summary = str(row.get("summary", ""))
    year = datetime.now(timezone.utc).year

    if is_outlook(raw, summary):
        code = row.get("disturbance_code") or ""
        return f'"{code}" tropical disturbance {outlook_basin(summary)} {year}'.strip()
    if peril == "Tropical Cyclone":
        name = extract_tc_identity(raw, summary)
        status = row.get("status") or "tropical cyclone"
        return f'"{status} {name}" {year}' if name else f'"{raw}" tropical cyclone {year}'
    if peril == "Wildfire":
        fire_name = re.sub(r"^Wildfire:\s*", "", raw, flags=re.IGNORECASE).strip()
        geography = "California" if row.get("source") == "CAL FIRE" else extract_location(title, summary)
        return f'"{fire_name}" {geography} wildfire {year}'.strip()
    if peril == "Flood":
        location = extract_location(title, summary) or raw
        return f'"{location}" flood {year}'.strip()
    base_q = re.sub(r"^(?:red|orange|green) notification for\s+", "", raw, flags=re.IGNORECASE)
    return f'"{base_q.strip()}" {peril} {year}'.strip()


def maps_url(row):
    lat, lon = row.get("lat"), row.get("lon")
    if pd.notna(lat) and pd.notna(lon):
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    q = extract_location(row.get("raw_title", ""), row.get("summary", "")) or row.get("raw_title", "")
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(q)}"


def filter_df(df, min_tier, perils, sources, hours):
    if df.empty:
        return df
    out = df[df["tier_rank"] <= TIER_ORDER[min_tier]].copy()
    if perils:
        out = out[out["peril"].isin(perils)]
    if sources:
        out = out[out["source"].isin(sources)]
    if hours:
        cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(hours=hours))
        out = out[(out["published_sort"].isna()) | (out["published_sort"] >= cutoff)]
    return out


def tier_badge(tier):
    return {"Critical": "\U0001F534 Critical", "Watch": "\U0001F7E0 Watch", "Advisory": "\U0001F7E1 Advisory", "Info": "\u26AA Info"}.get(tier, "\u26AA Info")


def peril_icon(peril):
    return {"Tropical Cyclone": "\U0001F300", "Earthquake": "\U0001F30E", "Flood": "\U0001F30A", "Wildfire": "\U0001F525",
            "Volcano": "\U0001F30B", "Drought": "\u2600\uFE0F", "Other": "\U0001F4CC"}.get(peril, "\U0001F4CC")


def inject_app_theme():
    """Apply a restrained catastrophe-operations visual system."""
    st.markdown("""
    <style>
    :root {
      --gcw-bg: #07111f;
      --gcw-panel: #0d1a2a;
      --gcw-panel-2: #112238;
      --gcw-border: rgba(148, 163, 184, 0.20);
      --gcw-text: #eef4fb;
      --gcw-muted: #91a4ba;
      --gcw-cyan: #38bdf8;
    }
    .stApp {
      background:
        radial-gradient(circle at 82% -8%, rgba(14,165,233,.10), transparent 30rem),
        linear-gradient(180deg, #07111f 0%, #08131f 100%);
      color: var(--gcw-text);
    }
    [data-testid="stHeader"] { background: rgba(7,17,31,.78); backdrop-filter: blur(12px); }
    [data-testid="stSidebar"] { background: linear-gradient(180deg, #0a1626, #07111f); border-right: 1px solid var(--gcw-border); }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { letter-spacing: .01em; }
    .block-container { max-width: 1680px; padding-top: 1.15rem; padding-bottom: 3rem; }
    .gcw-hero {
      display:flex; justify-content:space-between; align-items:flex-end; gap:1.25rem;
      padding:1.25rem 1.45rem; margin:0 0 1rem 0;
      border:1px solid rgba(56,189,248,.18); border-radius:18px;
      background:linear-gradient(120deg, rgba(17,34,56,.96), rgba(8,22,38,.96));
      box-shadow:0 18px 45px rgba(0,0,0,.24), inset 0 1px 0 rgba(255,255,255,.035);
    }
    .gcw-kicker { color:#38bdf8; font-size:.72rem; font-weight:800; letter-spacing:.16em; text-transform:uppercase; margin-bottom:.3rem; }
    .gcw-title { color:#f8fbff; font-size:2rem; line-height:1.05; font-weight:800; letter-spacing:-.035em; }
    .gcw-subtitle { color:#91a4ba; font-size:.9rem; margin-top:.45rem; }
    .gcw-stats { display:flex; gap:.55rem; flex-wrap:wrap; justify-content:flex-end; }
    .gcw-stat { min-width:92px; padding:.58rem .72rem; border:1px solid var(--gcw-border); border-radius:11px; background:rgba(5,15,27,.46); }
    .gcw-stat-label { color:#8296ae; font-size:.65rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
    .gcw-stat-value { color:#f5f9ff; font-size:1.05rem; font-weight:750; margin-top:.08rem; }
    .gcw-map-legend {
      display:flex; align-items:center; flex-wrap:wrap; gap:.6rem 1rem;
      margin:.15rem 0 .65rem 0; padding:.72rem .9rem;
      border:1px solid var(--gcw-border); border-radius:12px; background:rgba(13,26,42,.72);
      color:#b9c8d8; font-size:.78rem;
    }
    .gcw-map-legend strong { color:#edf5ff; font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; }
    .gcw-dot { width:9px; height:9px; border-radius:999px; display:inline-block; margin-right:.28rem; box-shadow:0 0 8px currentColor; }
    div[data-testid="stVerticalBlockBorderWrapper"] {
      border-color:var(--gcw-border) !important; border-radius:15px !important;
      background:linear-gradient(145deg, rgba(13,26,42,.88), rgba(8,20,34,.88));
      box-shadow:0 10px 28px rgba(0,0,0,.16);
    }
    div[data-testid="stMetric"] { padding:.15rem .15rem .35rem .15rem; }
    div[data-testid="stMetricLabel"] { color:#8fa3ba; }
    div[data-testid="stMetricValue"] { letter-spacing:-.025em; }
    button[data-baseweb="tab"] { font-weight:650; color:#8fa3ba; padding-top:.72rem; padding-bottom:.72rem; }
    button[data-baseweb="tab"][aria-selected="true"] { color:#eaf6ff; }
    [data-testid="stExpander"] { border-color:rgba(148,163,184,.18) !important; background:rgba(5,15,27,.30); border-radius:10px; }
    [data-testid="stLinkButton"] a, .stButton button { border-radius:10px; }
    hr { border-color:rgba(148,163,184,.16) !important; }
    @media (max-width: 900px) {
      .gcw-hero { align-items:flex-start; flex-direction:column; }
      .gcw-stats { justify-content:flex-start; }
    }
    </style>
    """, unsafe_allow_html=True)


def render_app_header(filtered=None, new_visible=0):
    filtered = filtered if filtered is not None else pd.DataFrame()
    critical = int((filtered.get("tier") == "Critical").sum()) if not filtered.empty else 0
    watch = int((filtered.get("tier") == "Watch").sum()) if not filtered.empty else 0
    refresh_time = datetime.now(timezone.utc).strftime("%H:%M UTC")
    st.markdown(f"""
    <div class="gcw-hero">
      <div>
        <div class="gcw-kicker">Live catastrophe intelligence</div>
        <div class="gcw-title">Global Cat Watch</div>
        <div class="gcw-subtitle">GDACS, NHC and CAL FIRE monitoring with persistent event histories</div>
      </div>
      <div class="gcw-stats">
        <div class="gcw-stat"><div class="gcw-stat-label">Critical</div><div class="gcw-stat-value">{critical}</div></div>
        <div class="gcw-stat"><div class="gcw-stat-label">Watch</div><div class="gcw-stat-value">{watch}</div></div>
        <div class="gcw-stat"><div class="gcw-stat-label">New</div><div class="gcw-stat-value">{new_visible}</div></div>
        <div class="gcw-stat"><div class="gcw-stat-label">Refreshed</div><div class="gcw-stat-value">{refresh_time}</div></div>
      </div>
    </div>
    """, unsafe_allow_html=True)


def render_map_legend(perils):
    peril_bits = "".join(
        f'<span>{peril_icon(peril)} {peril}</span>' for peril in PERIL_ORDER if peril in perils
    )
    st.markdown(f"""
    <div class="gcw-map-legend">
      <strong>Severity</strong>
      <span><i class="gcw-dot" style="background:#DC322F;color:#DC322F"></i>Critical</span>
      <span><i class="gcw-dot" style="background:#F08C14;color:#F08C14"></i>Watch</span>
      <span><i class="gcw-dot" style="background:#E6C828;color:#E6C828"></i>Advisory</span>
      <span><i class="gcw-dot" style="background:#8C96A0;color:#8C96A0"></i>Info</span>
      <strong>Peril</strong>{peril_bits}
    </div>
    """, unsafe_allow_html=True)


def _base_geo(fig, height, center=None, show_legend=False):
    fig.update_geos(
        projection_type="natural earth",
        showland=True, landcolor="#3D5C39",
        showocean=True, oceancolor="#1E3A5F",
        showcountries=True, countrycolor="#4A6E3F",
        showcoastlines=True, coastlinecolor="#6B8E5C",
        lakecolor="#2A4A7C", bgcolor="rgba(0,0,0,0)",
        center=center,
    )
    fig.update_layout(
        height=height, margin=dict(l=0, r=0, t=30 if show_legend else 0, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        showlegend=show_legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0,
                    font=dict(color="#E6EAF1", size=12), bgcolor="rgba(0,0,0,0)",
                    itemclick="toggle", itemdoubleclick="toggleothers"),
    )


def render_map(df, key, height=460, show_peril_toggle=True):
    m = df.copy()
    m["lat"] = pd.to_numeric(m["lat"], errors="coerce")
    m["lon"] = pd.to_numeric(m["lon"], errors="coerce")
    m = m.dropna(subset=["lat", "lon"])
    if m.empty:
        st.info("No mappable events for this view (these feeds didn't include coordinates).")
        return

    if show_peril_toggle:
        present = [p for p in PERIL_ORDER if p in set(m["peril"])]
        if present and len(present) > 1:
            if hasattr(st, "segmented_control"):
                sel = st.segmented_control("Filter perils", present, selection_mode="multi",
                                           default=present, key=f"{key}_perils")
            else:
                sel = st.multiselect("Filter perils", present, default=present, key=f"{key}_perils")
            if sel:
                m = m[m["peril"].isin(sel)]
    if m.empty:
        st.info("No events for the selected perils.")
        return

    m["emoji"] = m["peril"].map(peril_icon)
    def map_hover(row):
        bits = [str(row.get("title", "")), f"{row.get('tier', '')} · {row.get('peril', '')} · {row.get('source', '')}"]
        metrics = []
        if pd.notna(row.get("wind_kmh")):
            metrics.append(f"Wind {row['wind_kmh']:.0f} km/h")
        if pd.notna(row.get("pressure_mb")):
            metrics.append(f"Pressure {row['pressure_mb']:.0f} mb")
        if pd.notna(row.get("magnitude")):
            metrics.append(f"Magnitude M{row['magnitude']:g}")
        if pd.notna(row.get("acres")):
            metrics.append(f"{row['acres']:,.0f} acres")
        if metrics:
            bits.append(" · ".join(metrics))
        bits.append(f"Updated {row.get('age', 'Unknown')}")
        return "<br>".join(bits)
    m["hover"] = m.apply(map_hover, axis=1)
    m["peril_lower"] = m["peril"].str.lower().str.replace(" ", "_")

    fig = go.Figure()
    # One trace per tier -> clickable legend filters tiers. Colour halo = tier, emoji = peril.
    for tier in ["Info", "Advisory", "Watch", "Critical"]:  # critical drawn last (on top)
        sub = m[m["tier"] == tier]
        if sub.empty:
            continue
        fig.add_trace(go.Scattergeo(
            lat=sub["lat"], lon=sub["lon"], mode="markers+text",
            name=f"{tier} ({len(sub)})", legendrank=TIER_ORDER[tier],
            customdata=sub[["peril", "event_id"]].values,
            marker=dict(size=TIER_SIZE.get(tier, 10) + 14, color=TIER_HEX.get(tier, "#8C96A0"),
                        opacity=0.32, line=dict(width=1.6, color=TIER_HEX.get(tier, "#8C96A0"))),
            text=sub["emoji"], textposition="middle center",
            textfont=dict(size=TIER_SIZE.get(tier, 10) + 3),
            hovertext=sub["hover"], hoverinfo="text",
        ))
    render_map_legend(set(m["peril"]))
    _base_geo(fig, height, show_legend=False)
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        geo=dict(framecolor="rgba(148,163,184,.18)", framewidth=1),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"{key}_map")
    st.caption(f"{len(m)} events mapped · Marker colour = severity · Symbol = peril")

def render_locator(row, key, height=240):
    lat, lon = to_float(row.get("lat")), to_float(row.get("lon"))
    if lat is None or lon is None:
        return False
    fig = go.Figure(go.Scattergeo(
        lat=[lat], lon=[lon], mode="markers+text",
        marker=dict(size=26, color=TIER_HEX.get(row.get("tier"), "#F08C14"),
                    opacity=0.30, line=dict(width=1.4, color=TIER_HEX.get(row.get("tier"), "#F08C14"))),
        text=[peril_icon(row.get("peril"))], textposition="middle center", textfont=dict(size=16),
        hovertext=[row.get("title", "")], hoverinfo="text",
    ))
    _base_geo(fig, height, center=dict(lat=lat, lon=lon))
    fig.update_geos(projection_scale=6, center=dict(lat=lat, lon=lon))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"loc_{key}")
    return True


def render_desktop_alerts(new_major_df, enabled):
    """Browser desktop notifications for new Critical/Watch events."""
    payload = []
    if not new_major_df.empty:
        for _, r in new_major_df.iterrows():
            payload.append({
                "id": "|".join([
                    str(r.get("event_id", "")), str(r.get("update_type", "")),
                    ";".join(str(x) for x in (r.get("changes") or [])), str(r.get("published", "")),
                ]),
                "title": str(r.get("title", "")),
                "sub": " \u00B7 ".join(x for x in [str(r.get("update_type", "")),
                    "; ".join(str(x) for x in (r.get("changes") or [])),
                    str(r.get("tier", "")), str(r.get("source", ""))] if x),
            })
    data = json.dumps(payload)
    enabled_js = "true" if enabled else "false"
    html = f"""
    <div style="font-family:sans-serif;color:#E6EAF1;font-size:13px;display:flex;align-items:center;gap:10px">
      <button id="gcwEnable" style="background:#F97316;color:#0E1117;border:none;padding:6px 12px;
        border-radius:6px;cursor:pointer;font-weight:600">\U0001F514 Enable desktop alerts</button>
      <span id="gcwStatus" style="color:#8A94A6"></span>
    </div>
    <script>
      const events = {data};
      const autoEnabled = {enabled_js};
      const statusEl = document.getElementById('gcwStatus');
      function setStatus(){{
        if(!('Notification' in window)){{ statusEl.innerText='Not supported in this browser'; return; }}
        statusEl.innerText = 'Permission: ' + Notification.permission;
      }}
      function fire(){{
        if(!('Notification' in window) || Notification.permission!=='granted') return;
        let seen = [];
        try {{ seen = JSON.parse(localStorage.getItem('gcw_seen')||'[]'); }} catch(e) {{ seen=[]; }}
        events.forEach(function(e){{
          if(e.id && seen.indexOf(e.id)===-1){{
            try {{ new Notification('\U0001F310 '+e.title, {{ body: e.sub, tag: e.id }}); }} catch(err) {{}}
            seen.push(e.id);
          }}
        }});
        localStorage.setItem('gcw_seen', JSON.stringify(seen.slice(-500)));
      }}
      document.getElementById('gcwEnable').onclick = function(){{
        if(!('Notification' in window)) return;
        Notification.requestPermission().then(function(p){{ setStatus(); if(p==='granted') fire(); }});
      }};
      setStatus();
      if(autoEnabled) fire();
    </script>
    """
    components.html(html, height=48)


def render_event_card(row, news_sources, new_ids, ns="", compact=False):
    is_new = row.get("event_id") in new_ids
    with st.container(border=True):
        c1, c2 = st.columns([0.78, 0.22])
        with c1:
            head = f"**{tier_badge(row['tier'])} \u00B7 {peril_icon(row['peril'])} {row['peril']} \u00B7 {row['source']}**"
            if is_new:
                head += "  \u00B7  :green[\U0001F195 NEW]"
            st.markdown(head)
            st.markdown(f"#### {row['title']}")
            if row.get("changes"):
                st.markdown("  ".join(f":blue[**{change}**]" for change in row["changes"]))
            if row.get("summary"):
                text = display_summary(row["summary"])
                if compact:
                    st.caption(text[:240] + ("..." if len(text) > 240 else ""))
                else:
                    st.write(text[:800] + ("..." if len(text) > 800 else ""))

            has_coords = pd.notna(row.get("lat")) and pd.notna(row.get("lon"))
            event_key = str(row.get("event_id") or tracking_key(row))
            timeline = get_event_timeline(event_key, limit=100)
            update_count = timeline_update_count(timeline)
            if update_count:
                update_label = "change" if update_count == 1 else "changes"
                with st.expander(f"Change history ({update_count} {update_label})"):
                    render_event_timeline(event_key, compact=True)
            else:
                st.caption("No changes recorded since first observed.")
            ncol, mcol = st.columns(2)
            with ncol:
                with st.expander("\U0001F4F0 Related news"):
                    query = news_query_from_row(row)
                    st.caption(f"Searching news for: **{query}**")
                    items = fetch_news(query, tuple(news_sources))
                    if not items:
                        st.info("No related news found for this event yet.")
                    for it in items:
                        meta = " \u00B7 ".join(x for x in [it.get("source", ""), it.get("age", "")] if x)
                        st.markdown(
                            f"- [{it['title']}]({it['link']})  \n"
                            f"<span style='color:#8A94A6;font-size:0.8em'>{meta}</span>",
                            unsafe_allow_html=True,
                        )
            with mcol:
                with st.expander("\U0001F4CD Locate"):
                    if has_coords:
                        render_locator(row, key=f"{ns}_{row.get('event_id', row.get('title',''))}")
                    else:
                        st.caption("No exact coordinates in feed; use the map link below.")
                    st.link_button("Open in Google Maps", maps_url(row), use_container_width=True)
        with c2:
            st.metric("Updated", row["age"])
            st.caption(f"Published: {row['published']}")
            if row["alert_level"] != "Unknown":
                st.caption(f"Alert: {row['alert_level']}")
            if pd.notna(row.get("magnitude")):
                st.caption(f"Magnitude: {row['magnitude']}")
            if clean_optional_text(row.get("status")):
                st.caption(f"Status: {clean_optional_text(row.get('status'))}")
            if clean_optional_text(row.get("forecast_status")):
                st.caption(f"Forecast: Expected to become {clean_optional_text(row.get('forecast_status'))}")
            if pd.notna(row.get("wind_kmh")):
                st.caption(f"Wind: {row['wind_kmh']:.0f} km/h")
            if pd.notna(row.get("pressure_mb")):
                st.caption(f"Pressure: {row['pressure_mb']:.0f} mb")
            if pd.notna(row.get("formation_chance")):
                st.caption(f"Formation chance: {row['formation_chance']:.0f}%")
            if pd.notna(row.get("acres")):
                st.caption(f"Area: {row['acres']:,.0f} acres")
            if pd.notna(row.get("contained_pct")):
                st.caption(f"Containment: {row['contained_pct']:.0f}%")
            if row.get("link"):
                st.link_button("Open source", row["link"], use_container_width=True)


def render_cards(df, news_sources, new_ids, ns="", limit=50, compact=False):
    if df.empty:
        st.info("No events match the selected filters.")
        return

    display_df = df.copy()
    # Apply the intended operational order in every event tab:
    # Critical, Watch, Advisory, Info; newest source update first within tier.
    display_df["_display_tier_rank"] = display_df["tier"].map(TIER_ORDER).fillna(9)
    display_df["_display_updated"] = pd.to_datetime(
        display_df["published_utc"], errors="coerce", utc=True
    )
    display_df = display_df.sort_values(
        ["_display_tier_rank", "_display_updated"],
        ascending=[True, False],
        na_position="last",
        kind="stable",
    )

    for _, row in display_df.head(limit).iterrows():
        render_event_card(row, news_sources, new_ids, ns=ns, compact=compact)


def render_summary(df, new_count):
    st.subheader("Global Event Summary")
    if df.empty:
        st.info("No events currently monitored under the selected filters.")
        return
    pivot = pd.pivot_table(df, index="peril", columns="tier", values="title", aggfunc="count", fill_value=0)
    for col in ["Critical", "Watch", "Advisory", "Info"]:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot = pivot[["Critical", "Watch", "Advisory", "Info"]]
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.reindex([p for p in PERIL_ORDER if p in pivot.index])
    total_row = pivot.sum(axis=0).to_frame().T
    total_row.index = ["ALL PERILS"]
    display = pd.concat([pivot, total_row])
    st.dataframe(
        display, use_container_width=True,
        column_config={
            "Critical": st.column_config.NumberColumn("\U0001F534 Critical"),
            "Watch": st.column_config.NumberColumn("\U0001F7E0 Watch"),
            "Advisory": st.column_config.NumberColumn("\U0001F7E1 Advisory"),
            "Info": st.column_config.NumberColumn("\u26AA Info"),
            "Total": st.column_config.NumberColumn("\u03A3 Total"),
        },
    )
    caption = f"Monitoring {int(display.loc['ALL PERILS','Total'])} events across {len(pivot)} peril types."
    if new_count:
        caption += f"  \u00B7  \U0001F195 {new_count} new since last refresh."
    st.caption(caption)


def render_table(df, new_ids):
    if df.empty:
        st.info("No events match the selected filters.")
        return
    view = df.copy()
    view["new"] = view["event_id"].isin(new_ids).map({True: "\U0001F195", False: ""})
    cols = ["new", "tier", "peril", "source", "title", "age", "published", "link"]
    st.dataframe(
        view[cols], use_container_width=True, hide_index=True,
        column_config={
            "link": st.column_config.LinkColumn("Link"),
            "new": "New", "tier": "Tier", "peril": "Peril", "source": "Source",
            "title": "Event", "age": "Age", "published": "Published",
        },
    )


def render_digest(filtered, new_ids):
    st.subheader("Session Digest")
    st.caption("A running summary of what has changed since you opened the dashboard. "
               "Useful as a quick written brief; a true daily AI write-up can be added later.")
    if filtered.empty:
        st.info("Nothing to summarise yet.")
        return

    new_df = filtered[filtered["event_id"].isin(new_ids)]
    crit = filtered[filtered["tier"] == "Critical"]
    watch = filtered[filtered["tier"] == "Watch"]

    c1, c2, c3 = st.columns(3)
    c1.metric("New since last refresh", len(new_df))
    c2.metric("Critical now", len(crit))
    c3.metric("Watch now", len(watch))

    lines = []
    lines.append(f"As of {datetime.now().strftime('%Y-%m-%d %H:%M')}, monitoring {len(filtered)} events "
                 f"({len(crit)} critical, {len(watch)} watch).")
    by_peril = filtered.groupby("peril")["title"].count().sort_values(ascending=False)
    if not by_peril.empty:
        parts = [f"{peril_icon(p)} {n} {p.lower()}" for p, n in by_peril.items()]
        lines.append("Active mix: " + ", ".join(parts) + ".")
    if not new_df.empty:
        lines.append("")
        lines.append("New / updated this refresh:")
        for _, r in new_df.head(15).iterrows():
            lines.append(f"  \u2022 [{r['tier']}] {r['title']} ({r['source']}, {r['age']})")
    else:
        lines.append("No new events since the last refresh.")

    digest_text = "\n".join(lines)
    st.text_area("Copyable brief", digest_text, height=260)
    st.download_button("Download digest (.txt)", digest_text,
                       file_name=f"cat_watch_digest_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                       use_container_width=False)


def update_new_ids(current_ids, refresh_token):
    ss = st.session_state
    if "seen_ids" not in ss:
        ss.seen_ids = set(current_ids)
        ss.new_ids = set()
        ss.last_token = refresh_token
    elif refresh_token != ss.last_token:
        ss.new_ids = set(current_ids) - ss.seen_ids
        ss.seen_ids |= set(current_ids)
        ss.last_token = refresh_token
    return ss.new_ids


def app():
    st.set_page_config(page_title=APP_TITLE, page_icon="\U0001F310", layout="wide")
    st.title("\U0001F310 Global Cat Watch")
    st.caption("GDACS + NHC + CAL FIRE catastrophe monitor with maps, news lookup and desktop alerts.")
    

    with st.sidebar:
        st.markdown("## CatWatch Controls")
        st.caption("Live monitoring and display settings")
        st.markdown("### Refresh")
        auto_refresh = st.toggle("Auto-refresh", value=True)
        refresh_minutes = st.number_input("Refresh interval, minutes", 5, 60, DEFAULT_REFRESH_MINUTES, 5)
        auto_count = 0
        if auto_refresh and st_autorefresh:
            auto_count = st_autorefresh(interval=int(refresh_minutes * 60 * 1000), key="cat_watch_refresh")
        elif auto_refresh:
            st.caption("Install streamlit-autorefresh to enable auto refresh.")
        if st.button("Refresh now", use_container_width=True):
            st.session_state.refresh_clicks = st.session_state.get("refresh_clicks", 0) + 1
            st.cache_data.clear()
            st.rerun()

        st.divider()
        st.markdown("### Notifications")
        desktop_alerts = st.toggle("Desktop alerts", value=False)
        alert_material = st.checkbox("Material status and metric changes", value=True, disabled=not desktop_alerts)
        alert_major = st.checkbox("New Critical/Watch events", value=True, disabled=not desktop_alerts)
        st.divider()
        st.markdown("### View filters")
        time_window = st.selectbox("Time window", ["24 hours", "7 days", "30 days", "All available"], index=1)
        hours_lookup = {"24 hours": 24, "7 days": 168, "30 days": 720, "All available": None}
        min_tier = st.selectbox("Minimum tier", ["Critical", "Watch", "Advisory", "Info"], index=2)
        perils = st.multiselect("Perils", PERIL_ORDER, default=PERIL_ORDER)
        sources = st.multiselect("Sources", ["GDACS", "NHC", "CAL FIRE"], default=["GDACS", "NHC", "CAL FIRE"])

        st.divider()
        st.subheader("News filter")
        news_labels = st.multiselect("Preferred news sources (blank = all)", list(NEWS_SOURCES.keys()), default=[])
        news_sources = [NEWS_SOURCES[l] for l in news_labels]
        st.divider()
        with st.expander("System status"):
            supabase_ok, supabase_message = supabase_health_check()
            if supabase_ok:
                st.success(supabase_message)
            else:
                st.warning(supabase_message)
            st.caption("Persistent history records only changed event states.")
            prior_history_status = st.session_state.get("history_write_status", "")
            if prior_history_status:
                if "error" in prior_history_status.lower():
                    st.warning(prior_history_status)
                else:
                    st.caption(prior_history_status)

    refresh_token = f"{auto_count}:{st.session_state.get('refresh_clicks', 0)}"

    df = load_events()
    history_write_status = ""
    if not df.empty:
        df = material_changes(df, refresh_token)
        if supabase_health_check()[0]:
            df, history_write_status = persist_event_history(df)

    if df.empty:
        st.error("No feed items loaded. Check connectivity or source availability.")
        return
    st.session_state.history_write_status = history_write_status

    new_ids = update_new_ids(set(df["event_id"]), refresh_token)
    filtered = filter_df(df, min_tier, perils, sources, hours_lookup[time_window])
    new_visible = len(set(filtered["event_id"]) & new_ids) if not filtered.empty else 0

    render_app_header(filtered, new_visible)

    major = filtered[filtered["tier"].isin(["Critical", "Watch"])].copy()
    major["_major_tier_rank"] = major["tier"].map({"Critical": 0, "Watch": 1}).fillna(9)
    major["_major_updated"] = pd.to_datetime(major["published_utc"], errors="coerce", utc=True)
    major = major.sort_values(
        ["_major_tier_rank", "_major_updated"],
        ascending=[True, False],
        na_position="last",
        kind="stable",
    )

    if desktop_alerts:
        alert_frames = []
        if alert_major:
            alert_frames.append(major[major["event_id"].isin(new_ids)])
        if alert_material:
            alert_frames.append(filtered[filtered["is_material_update"]])
        alert_df = pd.concat(alert_frames, ignore_index=True).drop_duplicates(subset=["event_id"]) if alert_frames else filtered.iloc[0:0]
        with st.sidebar:
            st.caption("Browser notification permission")
            render_desktop_alerts(alert_df, enabled=True)

    # Map as hero header
    st.subheader("Global Event Map")
    render_map(filtered, key="major", height=560)
    
    # Single tabbed alert view. This replaces the duplicated quick-filter
    # buttons and the separate Critical & Watch alert section.
    st.divider()
    tabs = st.tabs([
        "All Perils",
        "Tropical Cyclone",
        "Wildfire",
        "Earthquake",
        "Flood",
        "Other",
        "Event History",
    ])

    tab_views = [
        ("All Perils", filtered, "all", False),
        ("Tropical Cyclone", filtered[filtered["peril"] == "Tropical Cyclone"], "tc", False),
        ("Wildfire", filtered[filtered["peril"] == "Wildfire"], "wf", False),
        ("Earthquake", filtered[filtered["peril"] == "Earthquake"], "eq", False),
        ("Flood", filtered[filtered["peril"] == "Flood"], "fl", False),
        (
            "Other",
            filtered[~filtered["peril"].isin([
                "Tropical Cyclone", "Wildfire", "Earthquake", "Flood"
            ])],
            "other",
            True,
        ),
        ("Event History", None, "history", False),
    ]

    for tab, (label, view_df, namespace, compact) in zip(tabs, tab_views):
        with tab:
            if namespace == "history":
                render_history_tab()
            else:
                st.subheader(label)
                if view_df.empty:
                    st.caption(f"No {label.lower()} events currently.")
                else:
                    render_cards(
                        view_df,
                        news_sources,
                        new_ids,
                        ns=namespace,
                        compact=compact,
                    )
if __name__ == "__main__":
    app()
