# Global Cat Watch

A simple Streamlit dashboard that monitors global catastrophe RSS feeds from GDACS and NHC.

## Files

Add these files to your existing `reins_dashboard` folder:

- `app.py`
- `requirements.txt`
- optional: `.streamlit/config.toml`

## Install / update environment

From your `reins_dashboard` folder:

```bash
uv pip install -r requirements.txt
```

If you are not using uv:

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

## What it does

The app pulls RSS feeds from:

- GDACS all-events RSS
- NHC Atlantic active cyclone feed
- NHC East Pacific active cyclone feed
- NHC Central Pacific active cyclone feed
- NHC tropical weather outlook feeds

It then normalizes the items into a single event board and assigns each event a deterministic tier:

- Critical
- Watch
- Advisory
- Info

## Notes

This first version intentionally avoids AI dependency. The dashboard should work reliably from RSS feeds alone. An AI triage layer can be added later once the basic feed board is behaving well.
