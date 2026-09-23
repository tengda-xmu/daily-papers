"""Publish only validated WeRSS article metadata to connector-data."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from src.wechat_metadata import public_export, public_health
from tools.publish_researchgate import ROOT, git


def publish(path: Path, status_only: bool = False):
    outputs = {}
    if not status_only:
        outputs["data/inbox/wechat.json"] = public_export(json.loads(path.read_text(encoding="utf-8-sig")))
    health_path = path.with_name("wechat-status.json")
    if health_path.is_file():
        outputs["data/inbox/wechat-status.json"] = public_health(json.loads(health_path.read_text(encoding="utf-8-sig")))
    if not outputs:
        raise ValueError("No validated WeRSS data or status to publish")
    refs = git("ls-remote", "--heads", "origin", "connector-data").stdout.strip()
    parent = None
    if refs:
        git("fetch", "origin", "connector-data")
        parent = git("rev-parse", "FETCH_HEAD").stdout.strip()
    base = (ROOT / ".local").resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Temporary directory must remain inside the project")
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wechat-publish-", dir=base) as temp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}
        git("read-tree", parent or "--empty", env=env)
        for target, payload in outputs.items():
            encoded = json.dumps(payload, ensure_ascii=False, indent=2)
            blob = git("hash-object", "-w", "--stdin", data=encoded, env=env).stdout.strip()
            git("update-index", "--add", "--cacheinfo", "100644", blob, target, env=env)
        tree = git("write-tree", env=env).stdout.strip()
        args = ["commit-tree", tree, "-m", "chore: synchronize WeChat article metadata"]
        if parent:
            args += ["-p", parent]
        commit = git(*args, env=env).stdout.strip()
        git("push", "origin", f"{commit}:refs/heads/connector-data")
    print("Published public WeChat metadata to connector-data; existing connector files preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=Path("data/inbox/wechat.json"))
    parser.add_argument("--status-only", action="store_true", help="Publish connection health while preserving the last good article export")
    args = parser.parse_args()
    publish(args.path, args.status_only)


if __name__ == "__main__":
    main()
