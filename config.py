"""config.py — settings + peer list."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Peer:
    ticker: str
    name: str
    bucket: str
    market: str
    note: str = ""


PEERS: list[Peer] = [
    # ---- Bermuda cat-focused (re)insurers -------------------------------- #
    Peer("RNR",  "RenaissanceRe",       "Bermuda Cat",       "US", "Property cat benchmark"),
    Peer("HG",   "Hamilton Insurance",  "Bermuda Cat",       "US", "Bermuda peer"),
    Peer("SPNT", "SiriusPoint",         "Bermuda Cat",       "US", "Bermuda (re)insurer"),
    Peer("GLRE", "Greenlight Re",       "Bermuda Cat",       "US", "Cayman P&C reinsurer"),

    # ---- Dynamic capital allocators -------------------------------------- #
    Peer("ACGL", "Arch Capital",        "Dynamic Allocator", "US", "Agile capital allocator"),
    Peer("MKL",  "Markel Group",        "Dynamic Allocator", "US", "Owns Nephila ILS"),
    Peer("WTM",  "White Mountains",     "Dynamic Allocator", "US", "Bermuda holding; owns Ark/WM re"),

    # ---- Global re/insurance majors -------------------------------------- #
    Peer("EG",   "Everest Group",       "Global Re/Insurer", "US", "Global re/insurance major"),
    Peer("CB",   "Chubb Ltd",           "Global Re/Insurer", "US", "Global P&C major"),
    Peer("MUV2.DE","Munich Re",         "Global Re/Insurer", "Europe", "Largest global reinsurer (XETRA)"),
    Peer("HNR1.DE","Hannover Re",       "Global Re/Insurer", "Europe", "Global reinsurer (XETRA)"),
    Peer("SREN.SW","Swiss Re",          "Global Re/Insurer", "Europe", "Global reinsurer (SIX)"),
    Peer("SCR.PA", "SCOR SE",           "Global Re/Insurer", "Europe", "European reinsurer (Paris)"),
    Peer("TLX.DE", "Talanx",            "Global Re/Insurer", "Europe", "Hannover Re parent (XETRA)"),
    Peer("ALV.DE", "Allianz",           "Global Re/Insurer", "Europe", "Global insurer (XETRA)"),
    Peer("ZURN.SW","Zurich Insurance",  "Global Re/Insurer", "Europe", "Global insurer (SIX)"),
    Peer("G.MI",   "Generali",          "Global Re/Insurer", "Europe", "Italian insurer (Milan)"),
    Peer("SAMPO.HE","Sampo",            "Global Re/Insurer", "Europe", "Nordic P&C (Helsinki)"),

    # ---- Property-cat specialists (coastal / quake / primary cat) -------- #
    Peer("PLMR", "Palomar Holdings",    "Property Cat Specialist", "US", "Earthquake/cat specialty"),
    Peer("UVE",  "Universal Insurance", "Property Cat Specialist", "US", "Florida property cat"),
    Peer("HRTG", "Heritage Insurance",  "Property Cat Specialist", "US", "FL/coastal property cat"),
    Peer("ACIC", "American Coastal",    "Property Cat Specialist", "US", "FL commercial property cat"),

    # ---- Large-cap P&C (cat-exposed primary) ----------------------------- #
    Peer("TRV",  "Travelers",           "Large-Cap P&C",     "US", "US P&C major, cat-exposed"),
    Peer("ALL",  "Allstate",            "Large-Cap P&C",     "US", "US personal lines, cat-exposed"),
    Peer("CINF", "Cincinnati Financial","Large-Cap P&C",     "US", "US P&C, cat-exposed"),
    Peer("AIZ",  "Assurant",            "Large-Cap P&C",     "US", "Housing/lender-placed, cat-exposed"),
    Peer("THG",  "Hanover Insurance",   "Large-Cap P&C",     "US", "US P&C"),
    Peer("MCY",  "Mercury General",     "Large-Cap P&C",     "US", "California P&C, cat-exposed"),

    # ---- Specialty writers ----------------------------------------------- #
    Peer("AXS",  "Axis Capital",        "Specialty",         "US", "Specialty writer"),
    Peer("WRB",  "W. R. Berkley",       "Specialty",         "US", "US specialty P&C"),
    Peer("KNSL", "Kinsale Capital",     "Specialty",         "US", "E&S specialty"),
    Peer("RLI",  "RLI Corp",            "Specialty",         "US", "US specialty"),
    Peer("SKWD", "Skyward Specialty",   "Specialty",         "US", "US specialty"),
    Peer("BEZ.L","Beazley",             "Specialty",         "London", "Lloyd's specialty"),
    Peer("HSX.L","Hiscox",              "Specialty",         "London", "Bermuda/London specialty"),

    # ---- Pure-play catastrophe reinsurers (London) ----------------------- #
    Peer("CRE.L","Conduit Holdings",    "Pure-Play Cat",     "London", "Pure-play cat reinsurer"),
    Peer("LRE.L","Lancashire Holdings", "Pure-Play Cat",     "London", "Specialty cat"),

    # ---- Japanese majors (huge global reinsurance buyers) ---------------- #
    Peer("8766.T","Tokio Marine",       "Global Re/Insurer", "Japan", "Largest Japanese P&C (Tokyo)"),
    Peer("8725.T","MS&AD Insurance",    "Global Re/Insurer", "Japan", "Japanese P&C major (Tokyo)"),
    Peer("8630.T","Sompo Holdings",     "Global Re/Insurer", "Japan", "Japanese P&C; owns Sompo Intl (Tokyo)"),
]

BENCHMARK: Peer = Peer("KBWP", "Invesco KBW P&C ETF", "Benchmark", "US",
                       "US sector benchmark ETF")

PEER_TICKERS: list[str] = [p.ticker for p in PEERS]
ALL_TICKERS: list[str] = PEER_TICKERS + [BENCHMARK.ticker]
TICKER_TO_NAME: dict[str, str] = {p.ticker: p.name for p in PEERS + [BENCHMARK]}
TICKER_TO_BUCKET: dict[str, str] = {p.ticker: p.bucket for p in PEERS + [BENCHMARK]}
TICKER_TO_MARKET: dict[str, str] = {p.ticker: p.market for p in PEERS + [BENCHMARK]}
MARKETS: list[str] = ["US", "London", "Europe", "Japan"]

BUCKET_EMOJI: dict[str, str] = {
    "Bermuda Cat": "🔵", "Pure-Play Cat": "🔷", "Dynamic Allocator": "🟢",
    "Global Re/Insurer": "🟠", "Property Cat Specialist": "🔴",
    "Large-Cap P&C": "🟡", "Specialty": "🟣", "Benchmark": "⚪",
}
BUCKET_COLORS: dict[str, str] = {
    "Bermuda Cat": "#2f81f7", "Pure-Play Cat": "#17becf",
    "Dynamic Allocator": "#2ca02c", "Global Re/Insurer": "#ff7f0e",
    "Property Cat Specialist": "#e15759", "Large-Cap P&C": "#bcbd22",
    "Specialty": "#a371f7", "Benchmark": "#8b949e",
}


@dataclass(frozen=True)
class IBKRSettings:
    host: str = "127.0.0.1"
    port: int = 7496
    client_id: int = 17
    timeout: float = 4.0
    readonly: bool = True


IBKR = IBKRSettings()

PAGE_TITLE = "Capital Markets Dashboard"
PAGE_ICON = "💸"
CACHE_TTL_QUOTES = 60
CACHE_TTL_FUNDAMENTALS = 60 * 60
CACHE_TTL_HISTORY = 60 * 15
