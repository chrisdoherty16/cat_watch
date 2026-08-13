"""data_loader.py — free market data via yfinance, plus analytics helpers."""
from __future__ import annotations
import numpy as np
import pandas as pd
try:
    import yfinance as yf
except ImportError:
    yf = None
try:
    import streamlit as st
    cache_data = st.cache_data
except ImportError:
    def cache_data(**_kwargs):
        def _wrap(fn):
            return fn
        return _wrap
import config


def _safe(d: dict, *keys, default=np.nan):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _pct(x) -> float:
    try:
        return float(x) * 100.0
    except (TypeError, ValueError):
        return np.nan


@cache_data(ttl=config.CACHE_TTL_QUOTES, show_spinner=False)
def get_quotes(tickers: tuple[str, ...]) -> pd.DataFrame:
    rows = []
    for t in tickers:
        price = prev = np.nan
        try:
            tk = yf.Ticker(t)
            fi = getattr(tk, "fast_info", {}) or {}
            price = _safe(dict(fi), "last_price", "lastPrice")
            prev = _safe(dict(fi), "previous_close", "previousClose")
            if pd.isna(price) or pd.isna(prev):
                hist = tk.history(period="2d")
                if not hist.empty:
                    price = hist["Close"].iloc[-1]
                    prev = hist["Close"].iloc[0]
        except Exception:
            pass
        change = price - prev if not (pd.isna(price) or pd.isna(prev)) else np.nan
        change_pct = (change / prev * 100.0) if not pd.isna(change) and prev else np.nan
        rows.append(dict(ticker=t, name=config.TICKER_TO_NAME.get(t, t),
                         bucket=config.TICKER_TO_BUCKET.get(t, "Peer"),
                         market=config.TICKER_TO_MARKET.get(t, ""),
                         price=price, prev_close=prev, change=change,
                         change_pct=change_pct))
    return pd.DataFrame(rows).set_index("ticker")


@cache_data(ttl=config.CACHE_TTL_FUNDAMENTALS, show_spinner=False)
def get_fundamentals(tickers: tuple[str, ...]) -> pd.DataFrame:
    rows = []
    for t in tickers:
        info: dict = {}
        try:
            info = yf.Ticker(t).info or {}
        except Exception:
            info = {}
        price = _safe(info, "currentPrice", "regularMarketPrice",
                      "regularMarketPreviousClose")
        bvps = _safe(info, "bookValue")
        pb = _safe(info, "priceToBook")
        if pd.isna(pb) and not pd.isna(price) and not pd.isna(bvps) and bvps:
            pb = price / bvps
        rows.append(dict(ticker=t, name=config.TICKER_TO_NAME.get(t, t),
                         bucket=config.TICKER_TO_BUCKET.get(t, "Peer"),
                         market=config.TICKER_TO_MARKET.get(t, ""),
                         price=price, market_cap=_safe(info, "marketCap"),
                         pb=pb, ptbv=_safe(info, "priceToTangibleBook", default=pb),
                         pe=_safe(info, "trailingPE", "forwardPE"),
                         roe_pct=_pct(_safe(info, "returnOnEquity")), bvps=bvps))
    return pd.DataFrame(rows).set_index("ticker")


@cache_data(ttl=config.CACHE_TTL_HISTORY, show_spinner=False)
def get_price_history(tickers: tuple[str, ...], period: str = "5y") -> pd.DataFrame:
    try:
        raw = yf.download(list(tickers), period=period, auto_adjust=True,
                          progress=False, group_by="column")
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    return close.dropna(how="all")


def rebase_to_100(prices: pd.DataFrame, start=None) -> pd.DataFrame:
    if prices.empty:
        return prices
    df = prices.copy()
    if start is not None:
        df = df.loc[df.index >= pd.to_datetime(start)]
    if df.empty:
        return df
    base = df.apply(lambda col: col.loc[col.first_valid_index()]
                    if col.first_valid_index() is not None else np.nan)
    return df.divide(base).multiply(100.0)


def _trailing_return(series: pd.Series, days: int | None, ytd: bool = False) -> float:
    """% return over the trailing window. days=None+ytd=True -> year-to-date."""
    s = series.dropna()
    if s.empty:
        return np.nan
    last = s.iloc[-1]
    if ytd:
        yr = s.index[-1].year
        prior = s[s.index < pd.Timestamp(yr, 1, 1)]
        base = prior.iloc[-1] if not prior.empty else s.iloc[0]
    else:
        cutoff = s.index[-1] - pd.Timedelta(days=days)
        window = s[s.index <= cutoff]
        base = window.iloc[-1] if not window.empty else s.iloc[0]
    return (last / base - 1.0) * 100.0 if base else np.nan


@cache_data(ttl=config.CACHE_TTL_HISTORY, show_spinner=False)
def get_returns_table(tickers: tuple[str, ...]) -> pd.DataFrame:
    """Trailing returns (%) over standard windows, per ticker. Terminal-style."""
    hist = get_price_history(tickers, period="2y")
    rows = []
    for t in tickers:
        if hist.empty or t not in hist.columns:
            s = pd.Series(dtype=float)
        else:
            s = hist[t]
        rows.append(dict(
            ticker=t,
            r_1w=_trailing_return(s, 7),
            r_1m=_trailing_return(s, 30),
            r_3m=_trailing_return(s, 91),
            r_ytd=_trailing_return(s, None, ytd=True),
            r_1y=_trailing_return(s, 365),
            # Drop empty days FIRST so thinly-traded ADRs (HVRRY, SSREY)
            # yield a clean list of real prices instead of a NaN-riddled one
            # (a list with NaNs can't render as a line and shows as text).
            spark=(s.dropna().tail(90).tolist() if not s.empty else []),
        ))
    return pd.DataFrame(rows).set_index("ticker")


def intraday_normalized(tickers: tuple[str, ...]) -> pd.DataFrame:
    try:
        raw = yf.download(list(tickers), period="1d", interval="1m",
                          auto_adjust=True, progress=False, group_by="column")
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    return rebase_to_100(close.dropna(how="all"))


def intraday_actual(tickers: tuple[str, ...]) -> pd.DataFrame:
    """Today's 1-minute ACTUAL prices (NOT rebased) — used by the drill-down."""
    try:
        raw = yf.download(list(tickers), period="1d", interval="1m",
                          auto_adjust=True, progress=False, group_by="column")
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    return close.dropna(how="all")


# --------------------------------------------------------------------------- #
#  Macro / rates data (Treasury yields + spreads) for the Macro tab
# --------------------------------------------------------------------------- #
# yfinance yield tickers (quoted directly in %):
MACRO_YIELDS = {
    "^IRX": "3-Month T-Bill",
    "^FVX": "5-Year Treasury",
    "^TNX": "10-Year Treasury",
    "^TYX": "30-Year Treasury",
}


@cache_data(ttl=config.CACHE_TTL_HISTORY, show_spinner=False)
def get_macro_history(period: str = "5y") -> pd.DataFrame:
    """Treasury yields (%) over time. Columns = friendly names."""
    tickers = tuple(MACRO_YIELDS.keys())
    try:
        raw = yf.download(list(tickers), period=period, progress=False,
                          group_by="column")
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    close = close.rename(columns=MACRO_YIELDS).dropna(how="all")
    return close


def get_macro_snapshot(period: str = "5y") -> dict:
    """Latest yields + key spreads, with the full history for charting."""
    hist = get_macro_history(period)
    out = {"hist": hist, "latest": {}, "spreads": pd.DataFrame()}
    if hist.empty:
        return out
    latest = hist.ffill().iloc[-1]
    prev = hist.ffill().iloc[-2] if len(hist) > 1 else latest
    out["latest"] = {c: (latest[c], latest[c] - prev[c]) for c in hist.columns}

    sp = pd.DataFrame(index=hist.index)
    if {"10-Year Treasury", "3-Month T-Bill"}.issubset(hist.columns):
        sp["10Y - 3M"] = hist["10-Year Treasury"] - hist["3-Month T-Bill"]
    if {"10-Year Treasury", "5-Year Treasury"}.issubset(hist.columns):
        sp["10Y - 5Y"] = hist["10-Year Treasury"] - hist["5-Year Treasury"]
    if {"30-Year Treasury", "10-Year Treasury"}.issubset(hist.columns):
        sp["30Y - 10Y"] = hist["30-Year Treasury"] - hist["10-Year Treasury"]
    out["spreads"] = sp.dropna(how="all")
    return out


# --------------------------------------------------------------------------- #
#  Credit & currency data for the Macro tab
# --------------------------------------------------------------------------- #
# REAL credit spreads: ICE BofA Option-Adjusted Spreads (OAS) from FRED.
# These are THE actual market credit-spread series (in percentage points),
# not a proxy. Pulled from FRED's public CSV endpoint — no API key needed.
FRED_CREDIT = {
    "BAMLC0A0CM":  "IG Corporate OAS",     # investment-grade
    "BAMLC0A4CBBB": "BBB Corporate OAS",    # BBB slice (nearest to reinsurers)
    "BAMLH0A0HYM2": "High-Yield OAS",       # high-yield
}
# Dollar / risk gauges.
FX_TICKERS = {
    "DX-Y.NYB": "US Dollar Index (DXY)",
    "^VIX": "VIX (equity vol)",
}


@cache_data(ttl=config.CACHE_TTL_HISTORY, show_spinner=False)
def _download_close(tickers: tuple[str, ...], period: str, rename: dict) -> pd.DataFrame:
    try:
        raw = yf.download(list(tickers), period=period, auto_adjust=True,
                          progress=False, group_by="column")
    except Exception:
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    return close.rename(columns=rename).dropna(how="all")


@cache_data(ttl=config.CACHE_TTL_HISTORY, show_spinner=False)
def _fred_series(series_id: str) -> pd.Series:
    """Pull one FRED series from the public CSV endpoint (no API key)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        df = pd.read_csv(url)
    except Exception:
        return pd.Series(dtype=float)
    if df.shape[1] < 2:
        return pd.Series(dtype=float)
    date_col = df.columns[0]            # "observation_date" or "DATE"
    val_col = df.columns[1]
    s = pd.Series(pd.to_numeric(df[val_col], errors="coerce").values,
                  index=pd.to_datetime(df[date_col], errors="coerce"))
    return s.dropna()


@cache_data(ttl=config.CACHE_TTL_HISTORY, show_spinner=False)
def get_credit_snapshot(period: str = "5y") -> dict:
    """REAL ICE BofA OAS credit spreads (pp) from FRED, plus HY-IG differential."""
    years = {"1y": 1, "2y": 2, "5y": 5, "10y": 10}.get(period, 5)
    start = pd.Timestamp.today() - pd.DateOffset(years=years)
    cols = {}
    for sid, name in FRED_CREDIT.items():
        s = _fred_series(sid)
        if not s.empty:
            cols[name] = s[s.index >= start]
    out = {"hist": pd.DataFrame(), "hyig_diff": pd.Series(dtype=float), "latest": {}}
    if not cols:
        return out
    hist = pd.DataFrame(cols).sort_index().ffill().dropna(how="all")
    out["hist"] = hist
    hy, ig = "High-Yield OAS", "IG Corporate OAS"
    if hy in hist.columns and ig in hist.columns:
        out["hyig_diff"] = (hist[hy] - hist[ig]).dropna()
    prev = hist.iloc[-2] if len(hist) > 1 else hist.iloc[-1]
    for c in hist.columns:
        out["latest"][c] = (hist[c].iloc[-1], hist[c].iloc[-1] - prev[c])
    return out


def get_fx_snapshot(period: str = "2y") -> dict:
    """US Dollar Index + VIX history and latest reads."""
    hist = _download_close(tuple(FX_TICKERS), period, FX_TICKERS)
    out = {"hist": hist, "latest": {}}
    if hist.empty:
        return out
    ff = hist.ffill()
    prev = ff.iloc[-2] if len(ff) > 1 else ff.iloc[-1]
    for c in hist.columns:
        chg_pct = (ff[c].iloc[-1] / prev[c] - 1) * 100 if prev[c] else np.nan
        out["latest"][c] = (ff[c].iloc[-1], chg_pct)
    return out


# --------------------------------------------------------------------------- #
#  Reinsurance news (free RSS feeds, parsed with the stdlib — no new deps)
# --------------------------------------------------------------------------- #
import urllib.request as _urlreq
import xml.etree.ElementTree as _ET
from html import unescape as _unescape
import re as _re

NEWS_FEEDS = {
    "Artemis": "https://www.artemis.bm/feed/",
    "Reinsurance News": "https://www.reinsurancene.ws/feed/",
}

# Keyword buckets for headline categorization (checked in order).
# Two match modes:
#   * plain substring for distinctive multi-word phrases
#   * \bword\b (word-boundary) for SHORT/ambiguous terms so we don't match
#     "q1" inside "aquisition" or "wind" inside "winding".
NEWS_CATEGORIES = {
    "Loss Events": {  # checked FIRST — most important to you
        "sub": ["hurricane", "earthquake", "wildfire", "typhoon", "cyclone",
                "tornado", "catastrophe loss", "insured loss", "loss estimate",
                "loss creep", "landfall", "severe convective", "flooding",
                "windstorm", "hailstorm", "storm surge", "bushfire",
                "nat cat", "natural catastrophe"],
        "word": ["flood", "quake", "hail", "wildfires", "storms", "storm"],
    },
    "M&A / People": {
        "sub": ["acqui", "merger", "takeover", "to buy", "buys ", "majority stake",
                "minority stake", "appoint", "steps down", "leadership",
                "joins ", "promot", "new ceo", "new cfo", "new coo",
                "chief executive", "chief financial", "chief underwriting",
                "hires", "recruit", "restructur"],
        "word": ["merge", "ceo", "cfo", "coo", "cuo", "chair", "president",
                 "names", "hire"],
    },
    "Results / Performance": {
        "sub": ["combined ratio", "loss ratio", "return on equity",
                "full-year", "full year", "half-year", "half year",
                "underwriting profit", "underwriting loss", "reports profit",
                "posts profit", "posts loss", "earnings", "guidance",
                "dividend", "share buyback", "results"],
        "word": ["profit", "quarter", "q1", "q2", "q3", "q4", "h1", "h2",
                 "roe", "result"],
    },
    "Capital / ILS": {
        "sub": ["cat bond", "catastrophe bond", "insurance-linked", "sidecar",
                "collateralized", "collateralised", "capital raise",
                "third-party capital", "third party capital", "issuance",
                "quota share", "sponsor"],
        "word": ["ils", "retro", "retrocession"],
    },
}

# Property-line detector: does this headline concern PROPERTY (re)insurance,
# especially cat? Used for the "Property only" toggle on the News tab.
PROPERTY_TERMS = {
    "sub": ["property cat", "property catastrophe", "property reinsurance",
            "property insurance", "property treaty", "property risk",
            "cat bond", "catastrophe bond", "catastrophe reinsurance",
            "homeowners", "reinsurance renewal", "1/1 renewal", "mid-year renewal",
            "hurricane", "earthquake", "wildfire", "typhoon", "cyclone",
            "tornado", "windstorm", "hailstorm", "flooding", "storm surge",
            "severe convective", "nat cat", "natural catastrophe", "landfall",
            "insured loss", "loss estimate", "aggregate cover", "per-occurrence",
            "retrocession", "sidecar"],
    "word": ["property", "cat", "flood", "quake", "hail", "wildfires"],
}


def _match(text: str, spec: dict) -> bool:
    """True if text hits any substring OR any word-boundary keyword in spec."""
    for k in spec.get("sub", []):
        if k in text:
            return True
    for w in spec.get("word", []):
        if _re.search(rf"\b{_re.escape(w)}\b", text):
            return True
    return False


def _categorize(title: str) -> str:
    t = (title or "").lower()
    for cat, spec in NEWS_CATEGORIES.items():
        if _match(t, spec):
            return cat
    return "Other"


def _is_property(title: str) -> bool:
    """Flag headlines relevant to PROPERTY (re)insurance / cat."""
    return _match((title or "").lower(), PROPERTY_TERMS)


def _clean(text: str, limit: int = 180) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace, truncate."""
    if not text:
        return ""
    text = _re.sub(r"<[^>]+>", "", text)
    text = _unescape(text)
    text = _re.sub(r"\s+", " ", text).strip()
    return (text[:limit] + "…") if len(text) > limit else text


@cache_data(ttl=600, show_spinner=False)  # 10-min cache; news moves slowly
def get_news(limit_per_feed: int = 40) -> pd.DataFrame:
    """
    Pull + categorize reinsurance headlines from free RSS feeds.
    Returns: source, category, title, link, published (datetime), summary.
    Only headlines/links/short snippets are stored (respecting copyright).
    """
    rows = []
    for source, url in NEWS_FEEDS.items():
        try:
            req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _urlreq.urlopen(req, timeout=8) as resp:
                raw = resp.read()
            root = _ET.fromstring(raw)
        except Exception:
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate") or ""
            desc = item.findtext("description") or ""
            try:
                published = pd.to_datetime(pub, errors="coerce", utc=True)
            except Exception:
                published = pd.NaT
            rows.append(dict(
                source=source, category=_categorize(title),
                property=_is_property(title), title=title,
                link=link, published=published, summary=_clean(desc, 160)))
            if len([r for r in rows if r["source"] == source]) >= limit_per_feed:
                break
    if not rows:
        return pd.DataFrame(columns=["source", "category", "property", "title",
                                     "link", "published", "summary"])
    df = pd.DataFrame(rows)
    # De-duplicate near-identical headlines across feeds (keep freshest copy).
    df = df.sort_values("published", ascending=False, na_position="last")
    df["_key"] = (df["title"].str.lower()
                  .str.replace(r"[^a-z0-9 ]", "", regex=True)
                  .str.replace(r"\s+", " ", regex=True).str.strip())
    before = len(df)
    df = df.drop_duplicates(subset="_key", keep="first").drop(columns="_key")
    df.attrs["deduped"] = before - len(df)
    return df
