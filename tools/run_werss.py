"""Start the locally installed WeRSS, listening on loopback only."""
import os
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1] / ".local/werss-source"
    if not (root / "config.yaml").is_file() or not (root / ".env").is_file():
        raise SystemExit("Install WeRSS and prepare its private local configuration first")
    sys.path.insert(0, str(root.parents[1]))
    from tools.werss_compat import prepare
    prepare(root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    # Collection is triggered by the daily sync task; no unsolicited messaging
    # jobs or automatic QR requests are enabled on startup.
    sys.argv = ["werss", "-job", "False", "-init", "False"]
    from core.db import DB
    DB.ensure_tables_exist()
    marker = root / "data/.daily-papers-initialized"
    if not marker.exists():
        from init_sys import init_user
        init_user(DB)
        marker.write_text("initialized", encoding="utf-8")
    import uvicorn
    uvicorn.run("web:app", host="127.0.0.1", port=8001, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
