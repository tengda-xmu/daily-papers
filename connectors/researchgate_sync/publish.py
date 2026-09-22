"""Publish a ResearchGate metadata export to the connector-data branch.

Run this from a machine where the repository's Git remote is authenticated.
Only the normalized JSON file is copied; the Playwright profile is never
included.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/inbox/researchgate.json")
    parser.add_argument("--branch", default="connector-data")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = (root / args.input).resolve()
    if not source.exists():
        raise SystemExit(f"Export file does not exist: {source}")
    remote = run("git", "config", "--get", "remote.origin.url", cwd=root).stdout.strip()
    if not remote:
        raise SystemExit("No authenticated origin remote is configured")
    with tempfile.TemporaryDirectory(prefix="daily-papers-rg-") as temp:
        checkout = Path(temp)
        cloned = run("git", "clone", "--branch", args.branch, "--single-branch", remote, str(checkout), check=False)
        if cloned.returncode != 0:
            shutil.rmtree(checkout)
            checkout.mkdir()
            run("git", "clone", remote, str(checkout))
            run("git", "switch", "--create", args.branch, cwd=checkout)
        target = checkout / "data" / "inbox" / "researchgate.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        run("git", "config", "user.name", "researchgate-connector", cwd=checkout)
        run("git", "config", "user.email", "researchgate-connector@users.noreply.github.com", cwd=checkout)
        run("git", "add", "-f", str(target.relative_to(checkout)), cwd=checkout)
        changed = run("git", "diff", "--cached", "--quiet", cwd=checkout, check=False)
        if changed.returncode == 0:
            return
        run("git", "commit", "-m", "chore: update ResearchGate metadata", cwd=checkout)
        run("git", "push", "origin", args.branch, cwd=checkout)


if __name__ == "__main__":
    main()
