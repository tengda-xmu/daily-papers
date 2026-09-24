# ResearchGate connector

The daily pipeline also supports a public-index fallback through the existing
`SERPAPI_API_KEY`. It runs one Google Scholar query limited to ResearchGate,
reuses the year-scoped 24-hour Scholar cache and labels its metadata as
`public_index`. Indexed PDF URLs are converted to publication landing pages;
this route never fetches ResearchGate pages or downloads PDFs. It is not an
account login and is not limited to the author profiles configured below.
Set `RESEARCHGATE_PUBLIC_INDEX=0` to use local exports only. A fresh successful
local export takes precedence; absent, stale or empty current-period exports
fall back to the public index. Missing dates remain unknown and excerpts are
labelled search snippets, not verified abstracts.

The local connector uses a separate browser profile. Logging into an ordinary
Chrome window does not authenticate the connector's Edge/Chromium profile.

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

The exporter loads the root `.env`. On Windows, set
`RESEARCHGATE_BROWSER_CHANNEL=msedge` to use installed Microsoft Edge without
downloading another browser. A local session directory outside the repository,
such as `%LOCALAPPDATA%/daily-papers/researchgate-profile`, is recommended.
`RESEARCHGATE_AUTHOR_URLS` is a comma-separated list of public profile or
publication URLs. Login pages, private feeds and messages are not collected.

The JSON envelope records `exported_at`, `failed_pages` and `records`.
Month-only dates retain their precision, so a May paper is not treated as a
September update. The import status distinguishes imported records from records
inside the current date window and flags exports older than three days.
Collection failures preserve the previous export and stop without retries.

On Windows, after one successful login, export and publish:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/sync_researchgate.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/schedule_researchgate.ps1 -Enable
```

The task `DailyPapers-ResearchGate` runs daily at 05:30 local time, before the
06:00 GitHub digest. The PC must be powered on and the Windows user signed in.
Missed starts can run once the PC becomes available. Running the scheduling
script without `-Enable` prepares a disabled task while login is unresolved.
Logs stay in `.local/researchgate-sync.log`.

If ResearchGate displays **Access restricted**, stop automated requests and
use the site's **Log in** verification entry. If that is also restricted,
wait for ResearchGate to restore access or contact its Help Center. Keep the
task disabled until an ordinary login and one collection succeed; do not
rotate proxies, use stealth patches or repeatedly retry a blocked page.
