"""
CatWatch — morning catastrophe monitoring board.

Primary tropical cyclone layer: NHC + JTWC via Tropycal.
Other global perils layer: GDACS RSS, excluding tropical cyclones to avoid double-counting.
Optional wildfire supplement: CAL FIRE active incidents.

Run:
    uv run streamlit run app.py

Likely dependencies beyond your current hurricane app:
    uv add feedparser requests beautifulsoup4 python-dateutil pandas pydeck tropycal streamlit numpy google-genai
"""

import math
import re
import time
import threading
from datetime import datetime, timedelta, timezone
from html import unescape

import numpy as np
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from tropycal import realtime

try:
    import feedparser
except Exception:
    feedparser = None

try:
    from zoneinfo import ZoneInfo
    BERMUDA_TZ = ZoneInfo("Atlantic/Bermuda")
except Exception:
    BERMUDA_TZ = None

# Optional AI layer. Absent library or key -> app runs unchanged.
try:
    from google import genai
except Exception:
    genai = None

# ---------------------------------------------------------------------------
# Page config + constants
# ---------------------------------------------------------------------------

APP_TITLE = "CatWatch"
REQUEST_HEADERS = {"User-Agent": "CatWatch/2.0 global catastrophe monitoring dashboard"}
GDACS_RSS_URL = "https://www.gdacs.org/XML/RSS.xml"
CALFIRE_URL = "https://incidents.fire.ca.gov/umbraco/api/IncidentApi/List?inactive=false"

# Preferred Flash models, best first. We pick whichever your key can access.
GEMINI_MODELS = [
    "gemini-flash-latest",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

PERIL_ORDER = [
    "Tropical Cyclone",
    "Earthquake",
    "Flood",
    "Wildfire",
    "Volcano",
    "Drought",
    "Civil Unrest",
    "Other",
]

SEVERITY_ORDER = {"Critical": 0, "Watch": 1, "Advisory": 2, "Info": 3}
ALERT_ORDER = {"Red": 0, "Orange": 1, "Green": 2, "Unknown": 3}

PERIL_META = {
    "Tropical Cyclone": {"icon": "🌪️", "color": [80, 180, 255]},
    "Earthquake": {"icon": "🌎", "color": [255, 90, 70]},
    "Flood": {"icon": "🌊", "color": [60, 150, 255]},
    "Wildfire": {"icon": "🔥", "color": [255, 120, 35]},
    "Volcano": {"icon": "🌋", "color": [210, 80, 255]},
    "Drought": {"icon": "☀️", "color": [245, 200, 70]},
    "Civil Unrest": {"icon": "⚠️", "color": [190, 170, 255]},
    "Other": {"icon": "📌", "color": [160, 170, 185]},
}

SEVERITY_COLOR = {
    "Critical": [230, 25, 75],
    "Watch": [255, 150, 35],
    "Advisory": [255, 220, 40],
    "Info": [150, 160, 175],
}

# Tropical-cyclone category palette. Single source of truth for map + legend.
TC_PALETTE = {
    "Cat 5": [230, 25, 75],
    "Cat 4": [255, 100, 40],
    "Cat 3": [255, 165, 0],
    "Cat 2": [255, 225, 25],
    "Cat 1": [150, 220, 80],
    "Trop. Storm": [40, 200, 215],
    "Depression": [90, 150, 255],
    "Invest": [150, 160, 175],
}

st.set_page_config(page_title=APP_TITLE, page_icon="🌍", layout="wide")

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.1rem; padding-bottom: 2rem; }
    .cw-card {
        background:#12151c; border:1px solid #2a2f3a; border-radius:13px;
        padding:14px 15px; margin-bottom:12px;
    }
    .cw-muted { color:#9aa4b2; font-size:0.86rem; }
    .cw-brief { color:#ff8ac4; font-weight:650; }
    .cw-chip {
        display:inline-block; padding:3px 8px; border-radius:999px; margin-right:5px;
        font-size:0.78rem; font-weight:650; background:#202532; color:#e6e9ef;
    }
    .cw-red { background:#3b171a; color:#ff7373; }
    .cw-orange { background:#3b2714; color:#ffb35c; }
    .cw-yellow { background:#393414; color:#ffe066; }
    .cw-grey { background:#232833; color:#cdd3de; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def clean_html(value: str) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def to_float(value):
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if math.isnan(out):
            return None
        return out
    except Exception:
        return None


def parse_dt(value):
    if not value:
        return None
    try:
        dt = dtparser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_entry_dt(entry):
    for key in ("published", "updated", "created"):
        dt = parse_dt(entry.get(key))
        if dt:
            return dt
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fmt_bermuda(dt):
    """Return e.g. '19 Aug 3:00 AM ADT'. Input can be UTC-aware/naive."""
    if dt is None or not hasattr(dt, "strftime"):
        return "—"
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(BERMUDA_TZ) if BERMUDA_TZ else dt
        hour12 = local.hour % 12 or 12
        ampm = "AM" if local.hour < 12 else "PM"
        tz = local.tzname() or ("AST" if BERMUDA_TZ else "UTC")
        return f"{local.day} {local.strftime('%b')} {hour12}:{local.minute:02d} {ampm} {tz}"
    except Exception:
        return dt.strftime("%d %b %H:%MZ")


def age_label(dt):
    if not dt:
        return "Unknown age"
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h ago"
    if hours:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


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


def peril_icon(peril):
    return PERIL_META.get(peril, PERIL_META["Other"])["icon"]


def severity_rank(value):
    return SEVERITY_ORDER.get(value, 9)


def display_summary(text, max_len=420):
    text = clean_html(text or "")
    text = text.replace("[unknown]", "n/a")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")

# ---------------------------------------------------------------------------
# Tropical cyclone helpers - NHC/JTWC via Tropycal
# ---------------------------------------------------------------------------


def category(vmax):
    v = vmax or 0
    if v >= 137:
        return 5
    if v >= 113:
        return 4
    if v >= 96:
        return 3
    if v >= 83:
        return 2
    if v >= 64:
        return 1
    return 0


def tc_color(vmax, invest=False):
    if invest:
        return TC_PALETTE["Invest"]
    cat = category(vmax)
    if cat >= 1:
        return TC_PALETTE[f"Cat {cat}"]
    return TC_PALETTE["Trop. Storm"] if (vmax or 0) >= 34 else TC_PALETTE["Depression"]


def classify_tc(vmax, stype, basin, invest=False):
    if invest:
        return "Invest / Area of Interest"
    v = vmax or 0
    cat = category(v)
    special = {
        "EX": "Post-Tropical Cyclone",
        "SS": "Subtropical Storm",
        "SD": "Subtropical Depression",
        "LO": "Remnant Low",
        "DB": "Disturbance",
        "WV": "Tropical Wave",
    }
    if stype in special:
        return special[stype]
    if cat >= 1:
        if basin == "west_pacific":
            return "Super Typhoon" if v >= 130 else "Typhoon"
        if basin in ("north_indian", "south_indian", "australia", "south_pacific"):
            return f"Cyclone (Cat {cat})"
        return f"Hurricane (Cat {cat})"
    if v >= 34:
        return "Tropical Storm"
    return "Tropical Depression"


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def movement(track, times):
    if len(track) < 2 or len(times) < 2:
        return "—"
    (lon1, lat1), (lon2, lat2) = track[-2], track[-1]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    comp = _COMPASS[round(brg / 22.5) % 16]
    a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2)
    dist = 3440.065 * 2 * math.asin(math.sqrt(a))
    try:
        hrs = (times[-1] - times[-2]).total_seconds() / 3600
        return f"{comp} at {dist / hrs:.0f} kt" if hrs else comp
    except Exception:
        return comp


def cone_polygon(forecast, basin):
    try:
        from tropycal import utils
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        cone = utils.generate_nhc_cone(forecast, basin, cone_days=5)
        grid = np.asarray(cone["cone"], dtype=float)
        cs = plt.contour(np.asarray(cone["lon"]), np.asarray(cone["lat"]), grid, levels=[0.5])
        best = max(cs.allsegs[0], key=len) if cs.allsegs[0] else None
        plt.close("all")
        if best is None or len(best) < 3:
            return None
        return [[float(x), float(y)] for x, y in best]
    except Exception:
        return None


def forecast_outlook(fc, cur_vmax, basin):
    try:
        fhrs = list(fc.get("fhr", []))
        vmaxs = list(fc.get("vmax", []))
        pairs = [(h, v) for h, v in zip(fhrs, vmaxs) if v is not None]
        if not pairs:
            return None
        init = fc.get("init")
        peak_v = max(v for _, v in pairs)
        peak_klass = classify_tc(peak_v, None, basin)
        by = ""
        for h, v in pairs:
            if classify_tc(v, None, basin) == peak_klass:
                if init is not None:
                    try:
                        by = fmt_bermuda(init + timedelta(hours=int(h)))
                    except Exception:
                        by = f"+{int(h)}h"
                else:
                    by = f"+{int(h)}h"
                break
        final_v = pairs[-1][1]
        if cur_vmax and peak_v >= cur_vmax + 5:
            trend = "↑ strengthening"
        elif cur_vmax and final_v <= cur_vmax - 5:
            trend = "↓ weakening"
        else:
            trend = "→ steady"
        return {"peak_klass": peak_klass, "peak_v": peak_v, "by": by, "trend": trend}
    except Exception:
        return None


def _attr(s, key):
    v = getattr(s, key, None)
    if v is None and hasattr(s, "attrs"):
        try:
            v = s.attrs.get(key)
        except Exception:
            v = None
    return v


def _formation_prob(s):
    def num(*keys):
        for k in keys:
            v = _attr(s, k)
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str) and v.strip().rstrip("%").isdigit():
                return int(v.strip().rstrip("%"))
        return None

    risk = ""
    for k in ("risk_7day", "risk_5day", "risk_2day"):
        v = _attr(s, k)
        if isinstance(v, str) and v not in ("", "N/A"):
            risk = v
            break
    return {"p2": num("prob_2day"), "p7": num("prob_7day", "prob_5day"), "risk": risk}


def _read_tropycal(rt, want_cone, source="NHC"):
    systems = []
    for sid in rt.list_active_storms():
        try:
            s = rt.get_storm(sid)
            track = [[float(lo), float(la)] for lo, la in zip(s.lon, s.lat)]
            if not track:
                continue
            times = list(getattr(s, "date", []) or getattr(s, "time", []))
            vmax = float(s.vmax[-1]) if len(getattr(s, "vmax", [])) else 0.0
            stype = s.type[-1] if getattr(s, "type", None) is not None else None
            basin = getattr(s, "basin", None)
            invest = bool(getattr(s, "invest", False))
            mslp_raw = s.mslp[-1] if getattr(s, "mslp", None) is not None else None
            mslp = float(mslp_raw) if mslp_raw and not math.isnan(float(mslp_raw)) else None

            fc_track, cone, outlook = [], None, None
            if not invest:
                try:
                    fc = s.get_forecast_realtime()
                    fc_track = [[float(lo), float(la)] for lo, la in zip(fc["lon"], fc["lat"])]
                    outlook = forecast_outlook(fc, vmax, basin)
                    if want_cone and fc_track:
                        cone = cone_polygon(fc, basin or "north_atlantic")
                except Exception:
                    pass

            discussion, nhc_url = None, None
            if source == "NHC" and not invest:
                try:
                    d = s.get_nhc_discussion(forecast=-1)
                    if isinstance(d, dict):
                        discussion = d.get("text")
                        nhc_url = d.get("url")
                except Exception:
                    discussion, nhc_url = None, None
            if source == "NHC" and not nhc_url:
                nhc_url = "https://www.nhc.noaa.gov/cyclones/"

            klass = classify_tc(vmax, stype, basin, invest)
            prob = _formation_prob(s) if invest else None
            updated = times[-1] if times else None
            systems.append({
                "id": sid,
                "event_id": f"TC|{source}|{sid}",
                "source": source,
                "peril": "Tropical Cyclone",
                "name": str(s.name).title(),
                "title": str(s.name).title(),
                "summary": "",
                "basin": basin,
                "invest": invest,
                "track": track,
                "pos": track[-1],
                "lat": track[-1][1],
                "lon": track[-1][0],
                "fc_track": ([track[-1]] + fc_track) if fc_track else [],
                "cone": cone,
                "outlook": outlook,
                "discussion": discussion,
                "url": nhc_url,
                "prob": prob,
                "vmax": vmax,
                "mslp": mslp,
                "klass": klass,
                "cat": category(vmax),
                "move": movement(track, times),
                "updated_utc": updated,
                "time": fmt_bermuda(updated) if updated else "—",
                "severity": tc_severity(vmax, invest),
                "alert_level": "Unknown",
                "color": tc_color(vmax, invest),
            })
        except Exception:
            continue
    return systems


def tc_severity(vmax, invest=False):
    if invest:
        return "Info"
    if (vmax or 0) >= 96:
        return "Critical"
    if (vmax or 0) >= 64:
        return "Watch"
    if (vmax or 0) >= 34:
        return "Advisory"
    return "Info"


@st.cache_data(ttl=600, show_spinner="Loading NHC storms...")
def get_nhc():
    try:
        return _read_tropycal(realtime.Realtime(), want_cone=True, source="NHC")
    except Exception:
        return []


@st.cache_resource(ttl=600)
def jtwc_state():
    return {"data": None, "started": False}


def get_tropical_systems():
    nhc = get_nhc()
    state = jtwc_state()
    if not state["started"]:
        state["started"] = True

        def bg():
            try:
                state["data"] = _read_tropycal(
                    realtime.Realtime(jtwc=True, jtwc_source="ucar"),
                    want_cone=True,
                    source="JTWC",
                )
            except Exception:
                state["data"] = []

        threading.Thread(target=bg, daemon=True).start()
    out = list(nhc)
    if state["data"]:
        have = {s["id"] for s in out}
        out += [s for s in state["data"] if s["id"] not in have]
    loading = state["data"] is None
    out.sort(key=lambda s: (s["invest"], -s["vmax"]))
    return out, loading

# ---------------------------------------------------------------------------
# Optional Gemini brief for tropical cyclones
# ---------------------------------------------------------------------------


def gemini_key():
    try:
        return str(st.secrets.get("GEMINI_API_KEY", "")).strip()
    except Exception:
        return ""


@st.cache_resource(show_spinner=False)
def gemini_client():
    if genai is None or not gemini_key():
        return None
    try:
        return genai.Client(api_key=gemini_key())
    except Exception:
        return None


_AI_ERROR = {"msg": ""}


@st.cache_data(ttl=3600, show_spinner=False)
def gemini_model():
    client = gemini_client()
    if client is None:
        return None
    try:
        available = [m.name.replace("models/", "") for m in client.models.list()]
    except Exception as e:
        _AI_ERROR["msg"] = f"models.list failed: {type(e).__name__}: {str(e)[:150]}"
        return "gemini-2.5-flash"
    for pref in GEMINI_MODELS:
        if pref in available:
            return pref
    flash = [n for n in available if "flash" in n and not any(x in n for x in ("image", "tts", "live", "lite"))]
    if flash:
        return sorted(flash, reverse=True)[0]
    return "gemini-2.5-flash"


def storm_facts(s):
    bits = [s["name"], s["klass"]]
    if s.get("vmax"):
        bits.append(f"{s['vmax']:.0f} kt winds")
    if s.get("mslp"):
        bits.append(f"{s['mslp']:.0f} mb")
    if s.get("move") and s["move"] != "—":
        bits.append(f"moving {s['move']}")
    fo = s.get("outlook")
    if fo:
        if fo["peak_klass"] != s["klass"] and fo["by"]:
            bits.append(f"forecast to reach {fo['peak_klass']} by {fo['by']}")
        bits.append(fo["trend"].split(" ", 1)[-1])
    return "; ".join(bits)


def fallback_brief(s):
    head = f"{s['name']} is a {s['klass'].lower()}"
    if s.get("vmax"):
        head += f" with {s['vmax']:.0f} kt winds"
    parts = [head]
    if s.get("move") and s["move"] != "—":
        parts.append(f"moving {s['move']}")
    fo = s.get("outlook")
    if fo and fo["peak_klass"] != s["klass"] and fo["by"]:
        parts.append(f"forecast to reach {fo['peak_klass'].lower()} by {fo['by']}")
    return ", ".join(parts) + "."


@st.cache_data(ttl=1800, show_spinner=False)
def ai_brief(facts, discussion=None):
    client, model = gemini_client(), gemini_model()
    if client is None or not model:
        return None
    if discussion:
        prompt = (
            "You are briefing a reinsurance underwriter. Read this official NHC forecast "
            "discussion and give ONE concise, plain sentence capturing what the storm is "
            "doing now and the key forecast threat. No preamble, no lists.\n\nDISCUSSION:\n"
            + discussion[:6000]
        )
    else:
        prompt = (
            "You are briefing a reinsurance underwriter. In ONE concise, plain sentence, "
            "state what this tropical system is doing now and its key forecast. No preamble, no lists.\nFacts: "
            + facts
        )
    try:
        resp = client.models.generate_content(model=model, contents=prompt)
        text = (resp.text or "").strip()
        return text or None
    except Exception as e:
        _AI_ERROR["msg"] = f"{type(e).__name__}: {str(e)[:200]}"
        return None

# ---------------------------------------------------------------------------
# GDACS + CAL FIRE - non-tropical-cyclone global perils
# ---------------------------------------------------------------------------


def infer_gdacs_peril(title, summary):
    text = f"{title} {summary}".lower()
    if any(x in text for x in ["tropical cyclone", "hurricane", "typhoon", "cyclone"]):
        return "Tropical Cyclone"
    if any(x in text for x in ["earthquake", "magnitude", " seismic", "m "]):
        return "Earthquake"
    if "flood" in text:
        return "Flood"
    if any(x in text for x in ["forest fire", "wildfire", "wild fire", "bushfire"]):
        return "Wildfire"
    if any(x in text for x in ["volcano", "eruption", "volcanic"]):
        return "Volcano"
    if "drought" in text:
        return "Drought"
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


def severity_from_alert(alert, peril="Other", magnitude=None):
    if alert == "Red":
        return "Critical"
    if alert == "Orange":
        return "Watch"
    if alert == "Green":
        return "Advisory"
    if peril == "Earthquake" and magnitude is not None:
        if magnitude >= 7.0:
            return "Watch"
        if magnitude >= 6.0:
            return "Advisory"
    return "Info"


def extract_magnitude(text):
    patterns = [
        r"\bM\s*([0-9]+(?:\.[0-9]+)?)\b",
        r"magnitude\s*([0-9]+(?:\.[0-9]+)?)",
        r"earthquake[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                return None
    return None


def extract_acres(item):
    for key in ("AcresBurned", "Acres", "acres", "AcresBurnedDisplay"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return float(str(value).replace(",", ""))
        except Exception:
            pass
    return None


def extract_containment(item):
    for key in ("PercentContained", "Containment", "contained_pct"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return float(str(value).replace("%", ""))
        except Exception:
            pass
    return None


@st.cache_data(ttl=600, show_spinner="Loading GDACS non-cyclone events...")
def load_gdacs_events():
    if feedparser is None:
        return []
    try:
        parsed = feedparser.parse(GDACS_RSS_URL, request_headers=REQUEST_HEADERS)
    except Exception:
        return []

    events = []
    for entry in parsed.entries:
        raw_title = clean_html(entry.get("title", ""))
        summary = clean_html(entry.get("summary", entry.get("description", "")))
        peril = infer_gdacs_peril(raw_title, summary)

        # Intentional: TCs belong only to the Tropycal/NHC/JTWC layer.
        if peril == "Tropical Cyclone":
            continue

        lat, lon = extract_latlon(entry)
        published = parse_entry_dt(entry)
        link = entry.get("link", GDACS_RSS_URL)
        alert = infer_alert_level(raw_title, summary)
        mag = extract_magnitude(f"{raw_title} {summary}")
        severity = severity_from_alert(alert, peril, mag)
        color = SEVERITY_COLOR.get(severity, SEVERITY_COLOR["Info"])

        metric_bits = []
        if mag is not None:
            metric_bits.append(f"M{mag:.1f}")
        if alert != "Unknown":
            metric_bits.append(f"{alert} alert")

        events.append({
            "event_id": f"GDACS|{link or raw_title}",
            "source": "GDACS",
            "peril": peril,
            "title": raw_title or f"{peril} event",
            "summary": summary,
            "severity": severity,
            "alert_level": alert,
            "lat": lat,
            "lon": lon,
            "published_utc": published,
            "updated_utc": published,
            "time": fmt_bermuda(published) if published else "—",
            "age": age_label(published),
            "url": link,
            "metrics": {"magnitude": mag, "alert": alert},
            "metric_text": " · ".join(metric_bits),
            "color": color,
        })
    events.sort(key=lambda e: (severity_rank(e["severity"]), ALERT_ORDER.get(e["alert_level"], 9), e.get("published_utc") or datetime.min.replace(tzinfo=timezone.utc)))
    return events


@st.cache_data(ttl=900, show_spinner="Loading CAL FIRE incidents...")
def load_calfire_events():
    try:
        resp = requests.get(CALFIRE_URL, headers=REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
        incidents = resp.json()
    except Exception:
        return []

    events = []
    for item in incidents if isinstance(incidents, list) else []:
        name = item.get("Name") or item.get("IncidentName") or "California wildfire"
        lat = to_float(item.get("Latitude") or item.get("lat"))
        lon = to_float(item.get("Longitude") or item.get("lon"))
        acres = extract_acres(item)
        contained = extract_containment(item)
        updated = parse_dt(item.get("Updated") or item.get("LastUpdated") or item.get("Started"))
        url = item.get("Url") or item.get("Link") or "https://www.fire.ca.gov/incidents"
        if url and url.startswith("/"):
            url = "https://www.fire.ca.gov" + url

        if acres and acres >= 100000:
            severity = "Critical"
        elif acres and acres >= 10000:
            severity = "Watch"
        elif acres and acres >= 1000:
            severity = "Advisory"
        else:
            severity = "Info"

        metric_bits = []
        if acres is not None:
            metric_bits.append(f"{acres:,.0f} acres")
        if contained is not None:
            metric_bits.append(f"{contained:.0f}% contained")

        location = item.get("County") or item.get("Location") or "California"
        summary = " · ".join([x for x in [str(location), " · ".join(metric_bits)] if x])

        events.append({
            "event_id": f"CALFIRE|{name}".lower(),
            "source": "CAL FIRE",
            "peril": "Wildfire",
            "title": f"{name} fire",
            "summary": summary,
            "severity": severity,
            "alert_level": "Unknown",
            "lat": lat,
            "lon": lon,
            "published_utc": updated,
            "updated_utc": updated,
            "time": fmt_bermuda(updated) if updated else "—",
            "age": age_label(updated),
            "url": url,
            "metrics": {"acres": acres, "contained_pct": contained},
            "metric_text": " · ".join(metric_bits),
            "color": SEVERITY_COLOR.get(severity, SEVERITY_COLOR["Info"]),
        })
    return events

# ---------------------------------------------------------------------------
# Normalized map records
# ---------------------------------------------------------------------------


def tc_to_map_event(s):
    return {
        "event_id": s["event_id"],
        "source": s["source"],
        "peril": "Tropical Cyclone",
        "title": s["name"],
        "summary": s["klass"],
        "severity": s["severity"],
        "alert_level": "Unknown",
        "lat": s.get("lat"),
        "lon": s.get("lon"),
        "time": s.get("time"),
        "url": s.get("url"),
        "metric_text": f"{s['vmax']:.0f} kt" if s.get("vmax") else "",
        "color": s.get("color") or tc_color(s.get("vmax"), s.get("invest")),
    }


def build_map_layers(events, tropical_systems, show_tracks=True):
    obs_paths, fc_paths, cones, dots = [], [], [], []

    if show_tracks:
        for s in tropical_systems:
            col = tc_color(s.get("vmax"), s.get("invest"))
            if len(s.get("track", [])) >= 2:
                obs_paths.append({"path": s["track"], "color": col})
            if len(s.get("fc_track", [])) >= 2:
                fc_paths.append({"path": s["fc_track"], "color": [255, 255, 255]})
            if s.get("cone"):
                cones.append({"polygon": s["cone"]})

    for e in events:
        lat, lon = to_float(e.get("lat")), to_float(e.get("lon"))
        if lat is None or lon is None:
            continue
        sev = e.get("severity", "Info")
        radius = {"Critical": 85000, "Watch": 70000, "Advisory": 56000, "Info": 44000}.get(sev, 44000)
        dots.append({
            "position": [lon, lat],
            "color": e.get("color") or PERIL_META.get(e.get("peril"), PERIL_META["Other"])["color"],
            "radius": radius,
            "title": e.get("title", "Untitled event"),
            "peril": e.get("peril", "Other"),
            "source": e.get("source", ""),
            "severity": sev,
            "metric": e.get("metric_text", ""),
            "time": e.get("time", "—"),
        })

    layers = [
        pdk.Layer(
            "PolygonLayer",
            cones,
            get_polygon="polygon",
            get_fill_color=[255, 255, 255, 30],
            get_line_color=[255, 255, 255, 110],
            stroked=True,
            filled=True,
            line_width_min_pixels=1,
        ),
        pdk.Layer("PathLayer", obs_paths, get_path="path", get_color="color", width_min_pixels=7, opacity=0.25),
        pdk.Layer("PathLayer", obs_paths, get_path="path", get_color="color", width_min_pixels=2.5),
        pdk.Layer("PathLayer", fc_paths, get_path="path", get_color="color", width_min_pixels=1.5, opacity=0.9),
        pdk.Layer(
            "ScatterplotLayer",
            dots,
            get_position="position",
            get_fill_color="color",
            get_radius="radius",
            radius_min_pixels=5,
            radius_max_pixels=22,
            stroked=True,
            get_line_color=[10, 12, 20],
            line_width_min_pixels=2,
            pickable=True,
        ),
    ]
    return layers


def render_world_map(events, tropical_systems=None, height=560, show_tracks=True, key="map"):
    tropical_systems = tropical_systems or []
    layers = build_map_layers(events, tropical_systems, show_tracks=show_tracks)
    st.pydeck_chart(
        pdk.Deck(
            map_provider="carto",
            map_style="dark",
            initial_view_state=pdk.ViewState(latitude=12, longitude=5, zoom=1.15),
            layers=layers,
            tooltip={
                "html": "<b>{title}</b><br/>{peril} · {source}<br/>{severity} {metric}<br/>{time}",
                "style": {
                    "backgroundColor": "#12151c",
                    "color": "#e6e9ef",
                    "fontSize": "12px",
                    "padding": "8px 10px",
                    "borderRadius": "6px",
                    "border": "1px solid #2a2f3a",
                },
            },
        ),
        height=height,
        use_container_width=True,
    )

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_legends(include_tc=True, include_perils=True):
    bits = []
    if include_tc:
        bits += [
            f'<span class="cw-chip" style="background:rgba({r},{g},{b},0.22);color:rgb({r},{g},{b})">{label}</span>'
            for label, (r, g, b) in TC_PALETTE.items()
        ]
    if include_perils:
        bits += [
            f'<span class="cw-chip" style="background:rgba({meta["color"][0]},{meta["color"][1]},{meta["color"][2]},0.20);color:rgb({meta["color"][0]},{meta["color"][1]},{meta["color"][2]})">{meta["icon"]} {peril}</span>'
            for peril, meta in PERIL_META.items()
            if peril != "Tropical Cyclone"
        ]
    st.markdown(" ".join(bits), unsafe_allow_html=True)


def render_event_card(event):
    sev = event.get("severity", "Info")
    chip_class = {"Critical": "cw-red", "Watch": "cw-orange", "Advisory": "cw-yellow", "Info": "cw-grey"}.get(sev, "cw-grey")
    metric = event.get("metric_text") or event.get("alert_level") or ""
    url = event.get("url") or ""
    summary = display_summary(event.get("summary", ""), max_len=360)
    link_html = f'<a href="{url}" target="_blank">Open source ↗</a>' if url else ""
    st.markdown(
        f"""
        <div class="cw-card">
            <span class="cw-chip {chip_class}">{sev}</span>
            <span class="cw-chip">{peril_icon(event.get('peril'))} {event.get('peril')}</span>
            <span class="cw-chip">{event.get('source')}</span>
            <h4 style="margin:10px 0 5px 0">{event.get('title')}</h4>
            <div class="cw-muted">{metric} · {event.get('time', '—')} · {event.get('age', '')}</div>
            <div style="margin-top:8px">{summary}</div>
            <div style="margin-top:8px">{link_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_tc_card(s, ai_on=False):
    r, g, b = tc_color(s["vmax"], s["invest"])
    fo = s.get("outlook")
    brief = None
    if ai_on and not s.get("invest"):
        brief = ai_brief(storm_facts(s), s.get("discussion"))
    if not brief:
        brief = fallback_brief(s)

    fc_badge = ""
    if fo and fo["peak_klass"] != s["klass"] and fo["by"]:
        fc_badge = f'<span class="cw-chip cw-orange">⏱ {fo["peak_klass"]} by {fo["by"]}</span>'
    trend_chip = f'<span class="cw-chip">{fo["trend"]}</span>' if fo else ""
    src_link = f'<a href="{s["url"]}" target="_blank">NHC source ↗</a>' if s.get("url") else ""

    st.markdown(
        f"""
        <div class="cw-card">
            <span class="cw-chip" style="background:rgba({r},{g},{b},0.22);color:rgb({r},{g},{b})">{s['klass']}</span>
            <span class="cw-chip">{s['source']}</span>
            {fc_badge}{trend_chip}
            <h4 style="margin:10px 0 5px 0">{s['name']}</h4>
            <div class="cw-brief">{brief}</div>
            <div class="cw-muted" style="margin-top:8px">
                Winds <b>{s['vmax']:.0f} kt</b> · Pressure <b>{f'{s['mslp']:.0f} mb' if s.get('mslp') else 'N/A'}</b> · Moving <b>{s['move']}</b><br/>
                {s['pos'][1]:.1f}°, {s['pos'][0]:.1f}° · {s['time']}
            </div>
            <div style="margin-top:8px">{src_link}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_headline(events, tropical_systems):
    combined = [tc_to_map_event(s) for s in tropical_systems if not s.get("invest")] + list(events)
    urgent = [e for e in combined if e.get("severity") in ("Critical", "Watch")]
    urgent.sort(key=lambda e: (severity_rank(e.get("severity")), e.get("peril", ""), e.get("title", "")))

    st.subheader("Morning headline")
    if not urgent:
        st.success("No Critical or Watch-level events in the current feed set.")
        return

    for e in urgent[:8]:
        metric = f" · {e.get('metric_text')}" if e.get("metric_text") else ""
        st.markdown(
            f"**{peril_icon(e.get('peril'))} {e.get('severity')} · {e.get('peril')} · {e.get('title')}**{metric}  "
            f"<span class='cw-muted'>{e.get('source', '')} · {e.get('time', '—')}</span>",
            unsafe_allow_html=True,
        )


def render_overview(tropical_systems, gdacs_events, calfire_events, jtwc_loading):
    other_events = gdacs_events + calfire_events
    map_events = [tc_to_map_event(s) for s in tropical_systems] + other_events

    active_storms = len([s for s in tropical_systems if not s.get("invest")])
    invests = len([s for s in tropical_systems if s.get("invest")])
    critical = len([e for e in map_events if e.get("severity") == "Critical"])
    watch = len([e for e in map_events if e.get("severity") == "Watch"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tropical systems", active_storms)
    c2.metric("Areas of interest", invests)
    c3.metric("Critical events", critical)
    c4.metric("Watch events", watch)

    if jtwc_loading:
        st.caption("⏳ Loading global JTWC tropical systems in the background...")

    render_legends(include_tc=True, include_perils=True)
    render_world_map(map_events, tropical_systems=tropical_systems, height=570, show_tracks=True, key="overview_map")
    render_headline(other_events, tropical_systems)


def render_hurricane_watch(tropical_systems, jtwc_loading):
    if jtwc_loading:
        st.caption("⏳ Loading global JTWC systems in the background...")
    if not tropical_systems:
        st.info("No active tropical systems anywhere right now.")
        return

    render_legends(include_tc=True, include_perils=False)
    render_world_map([tc_to_map_event(s) for s in tropical_systems], tropical_systems=tropical_systems, height=540, show_tracks=True, key="tc_map")

    storms = [s for s in tropical_systems if not s["invest"]]
    invests = [s for s in tropical_systems if s["invest"]]
    ai_on = bool(gemini_key())

    hdr = f"{len(storms)} storm{'s' if len(storms) != 1 else ''}"
    if invests:
        hdr += f" · {len(invests)} area{'s' if len(invests) != 1 else ''} of interest"
    st.subheader(f"Currently monitoring — {hdr}")
    st.caption("🧠 AI briefs on" if ai_on else "AI briefs off — add GEMINI_API_KEY to .streamlit/secrets.toml to enable.")

    cols = st.columns(3)
    for i, s in enumerate(storms):
        with cols[i % 3]:
            render_tc_card(s, ai_on=ai_on)

    st.markdown("---")
    st.subheader(f"Formation outlook — {len(invests)} area{'s' if len(invests) != 1 else ''} of interest")
    if not invests:
        st.caption("No areas of interest being monitored right now.")
    else:
        ocols = st.columns(3)
        for i, s in enumerate(invests):
            pr = s.get("prob") or {}
            p2, p7, risk = pr.get("p2"), pr.get("p7"), pr.get("risk")
            with ocols[i % 3]:
                st.markdown(
                    f"""
                    <div class="cw-card">
                        <span class="cw-chip">Invest</span>
                        <h4 style="margin:10px 0 5px 0">{s['name']}</h4>
                        <div>{risk + ' formation risk' if risk else 'Area of interest'}</div>
                        <div class="cw-muted" style="margin-top:8px">
                            48-hour <b>{f'{p2}%' if p2 is not None else '—'}</b> ·
                            7-day <b>{f'{p7}%' if p7 is not None else '—'}</b><br/>
                            {s['pos'][1]:.1f}°, {s['pos'][0]:.1f}° · {s['time']}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )


def render_other_perils(gdacs_events, calfire_events):
    all_events = gdacs_events + calfire_events
    st.caption("GDACS tropical cyclones are intentionally excluded here because hurricanes are handled by the NHC/JTWC layer.")

    if not all_events:
        st.info("No non-cyclone GDACS/CAL FIRE events available right now, or the feeds could not be loaded.")
        return

    c1, c2, c3 = st.columns([1.2, 1.2, 1])
    with c1:
        perils = [p for p in PERIL_ORDER if p != "Tropical Cyclone" and any(e.get("peril") == p for e in all_events)]
        selected_perils = st.multiselect("Perils", perils, default=perils, key="other_perils_filter")
    with c2:
        sources = sorted({e.get("source") for e in all_events if e.get("source")})
        selected_sources = st.multiselect("Sources", sources, default=sources, key="other_sources_filter")
    with c3:
        min_sev = st.selectbox("Minimum severity", ["Info", "Advisory", "Watch", "Critical"], index=0, key="other_sev_filter")

    threshold = severity_rank(min_sev)
    filtered = [
        e for e in all_events
        if e.get("peril") in selected_perils
        and e.get("source") in selected_sources
        and severity_rank(e.get("severity")) <= threshold
    ]
    filtered.sort(key=lambda e: (severity_rank(e.get("severity")), ALERT_ORDER.get(e.get("alert_level"), 9), e.get("peril", ""), e.get("title", "")))

    render_legends(include_tc=False, include_perils=True)
    render_world_map(filtered, tropical_systems=[], height=500, show_tracks=False, key="other_map")

    st.subheader(f"Current non-cyclone events — {len(filtered)}")
    cols = st.columns(2)
    for i, event in enumerate(filtered[:60]):
        with cols[i % 2]:
            render_event_card(event)


def render_data_table(tropical_systems, gdacs_events, calfire_events):
    rows = []
    for s in tropical_systems:
        rows.append(tc_to_map_event(s))
    rows.extend(gdacs_events)
    rows.extend(calfire_events)
    if not rows:
        st.info("No events loaded.")
        return
    df = pd.DataFrame(rows)
    keep = ["severity", "peril", "source", "title", "metric_text", "time", "lat", "lon", "url"]
    for col in keep:
        if col not in df.columns:
            df[col] = None
    df["severity_rank"] = df["severity"].map(SEVERITY_ORDER).fillna(9)
    df = df.sort_values(["severity_rank", "peril", "source", "title"]).drop(columns=["severity_rank"])
    st.dataframe(
        df[keep],
        use_container_width=True,
        hide_index=True,
        column_config={"url": st.column_config.LinkColumn("Source")},
    )

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def app():
    st.title("🌍 CatWatch")
    st.caption("Morning global catastrophe monitor · Hurricanes from NHC/JTWC via Tropycal · Other perils from GDACS/CAL FIRE")

    tropical_systems, jtwc_loading = get_tropical_systems()
    gdacs_events = load_gdacs_events()
    calfire_events = load_calfire_events()

    tab_overview, tab_hurricanes, tab_other, tab_data = st.tabs([
        "Overview",
        "Hurricanes",
        "Other Perils",
        "Data",
    ])

    with tab_overview:
        render_overview(tropical_systems, gdacs_events, calfire_events, jtwc_loading)

    with tab_hurricanes:
        render_hurricane_watch(tropical_systems, jtwc_loading)

    with tab_other:
        render_other_perils(gdacs_events, calfire_events)

    with tab_data:
        render_data_table(tropical_systems, gdacs_events, calfire_events)

    components.html(
        """
        <script>
        setTimeout(function(){ window.parent.location.reload(); }, 15 * 60 * 1000);
        </script>
        """,
        height=0,
    )

    if jtwc_loading:
        time.sleep(4)
        st.rerun()


if __name__ == "__main__":
    app()
