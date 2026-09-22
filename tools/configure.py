"""Check local configuration or securely enter one GitHub repository secret.

python -m tools.configure --check
python -m tools.configure --set SERPAPI_API_KEY
"""
import argparse
from getpass import getpass
import os
from pathlib import Path
import shutil
import subprocess

from src.settings import load_env

SECRETS = (
    "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN", "SERPAPI_API_KEY", "WOS_API_KEY",
    "OPENALEX_API_KEY", "OPENALEX_MAILTO", "SEMANTIC_SCHOLAR_API_KEY", "ARXIV_CONTACT",
    "WECHAT_RSS_URLS", "WECHAT_RSS_HEADERS", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "WECHAT_WORK_WEBHOOK_URL",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="Only report local presence; never print values")
    actions.add_argument("--set", choices=SECRETS, dest="secret", help="Prompt privately and save to GitHub Actions")
    args = parser.parse_args()
    load_env()
    if args.check:
        for key in SECRETS:
            print(f"{key}: {'present' if os.getenv(key, '').strip() else 'not set locally'}")
        path = Path(os.getenv("RESEARCHGATE_IMPORT_PATH", "data/inbox/researchgate.json"))
        print(f"ResearchGate export: {'present' if path.is_file() else 'not present'}")
        print("GitHub secret values cannot be read back; this check reports local configuration only.")
        return
    gh = shutil.which("gh")
    if not gh and os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "GitHub CLI/gh.exe"
        if candidate.is_file():
            gh = str(candidate)
    if not gh:
        raise SystemExit("GitHub CLI is required. Use the repository Actions Secrets settings instead.")
    value = getpass(f"{args.secret} (input hidden; saved only to GitHub): ").strip()
    if not value:
        raise SystemExit("Empty input; nothing saved.")
    # Pass the value through stdin so it cannot appear in command history or
    # process arguments. Do not display CLI output that could echo a secret.
    result = subprocess.run([gh, "secret", "set", args.secret, "--repo", "tengda-xmu/daily-papers"],
                            input=value, encoding="utf-8", capture_output=True)
    if result.returncode:
        raise SystemExit("Save failed. Check 'gh auth status' and your network connection; no value was logged.")
    print(f"Saved {args.secret} to GitHub Actions. Run Daily papers to validate the credentials.")


if __name__ == "__main__":
    main()
