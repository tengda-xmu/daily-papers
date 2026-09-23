"""Compatibility entry point for the metadata-only publisher."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.publish_researchgate import main as publish


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/inbox/researchgate.json")
    parser.add_argument("--branch", choices=["connector-data"], default="connector-data")
    args = parser.parse_args()
    sys.argv = [sys.argv[0], str(ROOT / args.input)]
    publish()


if __name__ == "__main__":
    main()
