# ResearchGate connector

This is an optional local/VPS collector. Run it interactively once to create a
browser profile, then schedule low-frequency runs:

```powershell
pip install -r connectors/researchgate_sync/requirements.txt
playwright install chromium
python connectors/researchgate_sync/export.py --login --url https://www.researchgate.net/profile/YOUR_PROFILE
python -m tools.publish_researchgate
```

Set `RESEARCHGATE_SESSION_DIR` outside the repository when possible. The
profile contains login cookies and must never be committed. Copy only the
generated `data/inbox/researchgate.json` into the pipeline input or publish it
through the connector-data branch. This repository is public, so this branch
is also public. The publisher accepts publication metadata only, strips URL
query strings and excludes sessions, cookies and arbitrary raw fields.

`--login` waits for your confirmation after you finish signing in. For later
runs omit it and use `--headless` with your saved local browser profile.
The publisher needs Git authentication and a configured Git author identity.
