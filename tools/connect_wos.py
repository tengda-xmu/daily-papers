"""Validate a local WOS key, configure local/GitHub credentials, then update the site.

python -m tools.connect_wos --check
python -m tools.connect_wos --activate
Only --activate saves credentials and dispatches the daily workflow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import getproxies

from src.settings import ROOT, read_env
from src.sources.public_literature import WebOfScienceAdapter

REPO = "tengda-xmu/daily-papers"


def read_key(path: Path) -> str:
    if path.is_file():
        value = path.read_text(encoding="utf-8-sig").strip()
        if value.startswith("WOS_API_KEY="):
            value = value.split("=", 1)[1].strip()
        value = value.strip("\"'")
    else:
        value = os.getenv("WOS_API_KEY", "").strip() or read_env().get("WOS_API_KEY", "")
    if not value:
        raise ValueError("缺少密钥：请在项目根目录保存 WOS_API_KEY.txt，或配置本机 WOS_API_KEY。")
    if any(char.isspace() for char in value) or not value.isascii():
        raise ValueError("密钥文件应只包含 API Key，不应包含说明文字或换行。")
    return value


def verify_key(value: str) -> dict:
    adapter = WebOfScienceAdapter(api_key=value, queries=['"large language model"'], limit=1)
    now = datetime.now(timezone.utc)
    rows = adapter.fetch(now - timedelta(days=30), now)
    status = adapter.status
    return {"authorized": status.status in ("ok", "no_data"), "status": status.status,
            "count": len(rows), "message": status.message}


def github_cli() -> str:
    command = shutil.which("gh")
    if not command and os.name == "nt":
        candidate = Path(os.getenv("ProgramFiles", "C:/Program Files")) / "GitHub CLI/gh.exe"
        if candidate.is_file():
            command = str(candidate)
    if not command:
        raise RuntimeError("找不到 GitHub CLI；请先安装并登录 gh。")
    return command


def save_local_key(value: str, path: Path) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() != "WOS_API_KEY"]
    lines.append("WOS_API_KEY=" + value)
    # Write only to the ignored .env; no key appears in process arguments.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def activate(value: str) -> None:
    gh = github_cli()
    env = os.environ.copy()
    proxies = getproxies()
    if proxies.get("https") or proxies.get("http"):
        env.setdefault("HTTPS_PROXY", proxies.get("https") or proxies["http"])
        env.setdefault("HTTP_PROXY", proxies.get("http") or proxies["https"])
    result = subprocess.run([gh, "secret", "set", "WOS_API_KEY", "--repo", REPO],
                            input=value, encoding="utf-8", capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError("GitHub 密钥保存失败；请检查 gh 登录及网络。密钥未输出。")
    save_local_key(value, ROOT / ".env")
    result = subprocess.run([gh, "workflow", "run", "daily.yml", "--repo", REPO, "--ref", "main"],
                            capture_output=True, encoding="utf-8", env=env)
    if result.returncode:
        raise RuntimeError("本机与 GitHub 密钥已保存，但日报未启动；请在 GitHub Actions 手动运行 Daily papers。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, default=ROOT / "WOS_API_KEY.txt")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="Validate with one API request; save nothing")
    actions.add_argument("--activate", action="store_true", help="Validate, save credentials locally and to GitHub, and run Daily papers")
    args = parser.parse_args()
    try:
        value = read_key(args.key_file)
        result = verify_key(value)
        print(result["message"])
        if not result["authorized"]:
            raise SystemExit("Web of Science 未通过授权验证；未保存或上传密钥。")
        if args.activate:
            activate(value)
            print("Web of Science 已验证，本机和 GitHub 密钥已保存，Daily papers 已启动。")
        else:
            print("Web of Science API 授权验证通过；尚未写入配置。")
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    except OSError:
        raise SystemExit("本机文件或命令访问失败；请检查路径、权限和网络。密钥未输出。") from None


if __name__ == "__main__":
    main()
