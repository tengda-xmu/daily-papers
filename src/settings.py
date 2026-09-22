"""Load the ignored local .env without overriding GitHub Actions secrets."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or ROOT / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.replace("_", "").isalnum():
            values[key] = value
    return values


def load_env(path: Path | None = None) -> None:
    for key, value in read_env(path).items():
        os.environ.setdefault(key, value)
