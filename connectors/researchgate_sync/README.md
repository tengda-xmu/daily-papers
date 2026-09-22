# ResearchGate connector

This is an optional local/VPS collector. Run it interactively once to create a
browser profile, then schedule low-frequency runs:

```powershell
pip install -r connectors/researchgate_sync/requirements.txt
playwright install chromium
python connectors/researchgate_sync/export.py --url https://www.researchgate.net/profile/YOUR_PROFILE
```

Set `RESEARCHGATE_SESSION_DIR` outside the repository when possible. The
profile contains login cookies and must never be committed. Copy only the
generated `data/inbox/researchgate.json` into the pipeline input or publish it
through a private connector-data branch.
