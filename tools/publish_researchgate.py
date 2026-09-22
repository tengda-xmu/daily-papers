"""Publish only validated ResearchGate paper metadata to connector-data.

The current branch and working files are never switched. Cookies, profiles
and arbitrary fields from the input are never written to the branch.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def public_records(payload):
    rows = payload.get("records", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Expected a list of publication records")
    clean = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        url = urlsplit(row.get("landing_url") or row.get("url") or "")
        if url.scheme not in ("http", "https") or url.hostname not in ("www.researchgate.net", "researchgate.net") or not url.path.startswith("/publication/"):
            continue
        item = {key: row[key] for key in ("title", "authors", "published_at", "venue", "doi") if key in row}
        if any(not isinstance(item[key], str) for key in item if key != "authors"):
            continue
        if "authors" in item and (not isinstance(item["authors"], list) or any(not isinstance(a, str) for a in item["authors"])):
            continue
        item["source"] = "ResearchGate"
        item["landing_url"] = urlunsplit(("https", url.hostname, url.path, "", ""))
        clean.append(item)
    if not clean:
        raise ValueError("No valid ResearchGate publication records; nothing will be published")
    return clean


def git(*args, data=None, env=None, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, input=data, encoding="utf-8",
                            capture_output=True, env=env)
    if check and result.returncode:
        raise RuntimeError("Git operation failed: " + args[0] + ". Check authentication and network access.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="data/inbox/researchgate.json")
    args = parser.parse_args()
    encoded = json.dumps(public_records(json.loads(Path(args.path).read_text(encoding="utf-8-sig"))), ensure_ascii=False, indent=2)
    refs = git("ls-remote", "--heads", "origin", "connector-data").stdout.strip()
    parent = None
    if refs:
        git("fetch", "origin", "connector-data")
        parent = git("rev-parse", "FETCH_HEAD").stdout.strip()
    base = (ROOT / ".local").resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Temporary directory must be inside this project")
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rg-publish-", dir=base) as temp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}
        git("read-tree", parent or "--empty", env=env)
        blob = git("hash-object", "-w", "--stdin", data=encoded, env=env).stdout.strip()
        git("update-index", "--add", "--cacheinfo", "100644", blob, "data/inbox/researchgate.json", env=env)
        tree = git("write-tree", env=env).stdout.strip()
        # Preserve the existing branch history and reject races via normal
        # fast-forward push semantics; never force-push connector data.
        args = ["commit-tree", tree, "-m", "chore: synchronize ResearchGate publication metadata"]
        if parent:
            args += ["-p", parent]
        commit = git(*args, env=env).stdout.strip()
        git("push", "origin", f"{commit}:refs/heads/connector-data")
    print("Published validated publication metadata to connector-data.")


if __name__ == "__main__":
    main()
