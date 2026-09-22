# CatWatch — Deployment Handoff

**What this is:** A Streamlit web app (Python) that monitors tropical cyclones
(NHC/JTWC), GDACS global peril alerts, CAL FIRE wildfires, and a custom
keyword news feed. Currently running on Streamlit Community Cloud, which is
not suitable for company-wide use (12-hour idle sleep, one private app limit,
capped resources). This doc covers moving it to **Azure App Service**.

**Owner / ongoing changes:** Chris Doherty owns the application code and will
continue to iterate on it independently after initial setup. IT's role is
limited to the one-time hosting setup below; no code review or ongoing IT
involvement is required for future app updates (see "Ongoing iteration" at
the bottom).

---

## Why Azure App Service

- Already inside the company's Azure/Microsoft tenant — no new vendor
  relationship or security review for a third-party host.
- Supports "Always On" (kills the sleep/timeout problem the current
  Community Cloud deployment has).
- Access can be restricted to company staff only via Entra ID, with no
  application code changes required.
- Runs any Python web app via a Linux container; no changes to app
  architecture needed.

## What's in this package

| File | Purpose |
|---|---|
| `app.py` | The application itself. No further code changes needed to deploy. |
| `requirements.txt` | Python dependencies for `pip install`. |
| `.streamlit/config.toml` | Server config (port, headless mode) — used automatically by Streamlit on any host. |
| `.streamlit/secrets.toml.example` | Template only — shows which optional AI API keys the app supports. Do not deploy this file as-is; see "Application Settings" below. |
| `.gitignore` | Prevents a real `secrets.toml` from ever being committed to source control. |

---

## One-time setup steps for IT

### 1. Create the resource
- Resource type: **App Service (Linux)**
- Runtime stack: **Python 3.11**
- Recommended plan: **Basic B2** or higher (2 vCPU / ~3.5 GB RAM). The app
  does background threading and occasional matplotlib rendering for storm
  forecast cones — B1 (1 core) will work but B2 gives headroom for multiple
  simultaneous company users.

### 2. Deploy the code
Two supported options, either is fine:
- **GitHub Deployment Center (recommended):** point Azure's Deployment
  Center at the CatWatch GitHub repo. This is what makes ongoing iteration
  work without IT involvement (see below) — pushes to the main branch
  auto-deploy.
- **Zip deploy:** zip the project folder and upload via `az webapp deploy`
  or the Azure Portal's zip deploy blade. Fine for the initial go-live, but
  means IT re-uploads for every future change, which defeats the point.

### 3. Startup command
Azure's Python auto-detection usually finds Streamlit automatically because
of `.streamlit/config.toml`, but if a custom startup command is needed,
set it under **Configuration → General Settings → Startup Command**:

```
python -m streamlit run app.py
```

### 4. Application Settings (environment variables)
Under **Configuration → Application settings**, add any of the following.
**All are optional** — the app runs fully without them, they only enable
the "Ask CatWatch" AI chat feature. The app already reads secrets from
`st.secrets` first and falls back to plain environment variables, so no
`secrets.toml` file should be deployed to the server at all.

| Name | Required? | Notes |
|---|---|---|
| `GEMINI_API_KEY` | Optional | Tried first if present |
| `GROQ_API_KEY` | Optional | Tried second |
| `GROQ_MODEL` | Optional | Defaults to `openai/gpt-oss-120b` if unset |
| `LLAMA_API_KEY` | Optional | Tried third; requires `LLAMA_MODEL` too |
| `LLAMA_MODEL` | Optional | Required alongside `LLAMA_API_KEY` |
| `OPENROUTER_API_KEY` | Optional | Tried last |

### 5. Enable Web Sockets
**Configuration → General Settings → Web sockets: On.**
Required — Streamlit uses WebSockets for live UI updates (including the
background JTWC data refresh and the AI chat interface).

### 6. Enable Always On
**Configuration → General Settings → Always On: On.**
Requires Basic tier or higher. This is what fixes the current
sleep-on-idle problem from Community Cloud.

### 7. Restrict access to company staff (Entra ID)
**Authentication → Add identity provider → Microsoft.**
Scope it to the company's own Entra ID tenant so only employees can sign
in. No code changes are needed for this — App Service handles the login
redirect in front of the app automatically ("Easy Auth").

### 8. Verify
Visit the app's `*.azurewebsites.net` URL (or custom domain, if one is
assigned), confirm sign-in redirects correctly, and confirm the Mission
Control map and Hurricanes tab load with live data.

---

## Ongoing iteration (no IT involvement needed after setup)

Once Deployment Center is linked to the GitHub repo (step 2 above), Chris
can continue making changes on his own timeline:

1. Edit `app.py` locally, test with `streamlit run app.py`.
2. Commit and push to the connected branch.
3. Azure automatically rebuilds and redeploys — typically within a couple
   of minutes.

No Azure Portal access, approvals, or IT tickets are required for routine
app changes (new tabs, peril logic, UI tweaks, etc.) — only for
infrastructure changes like scaling the plan up, changing secrets, or
adjusting auth rules.
