"""Provision an already verified local Codex installation for the companion."""
import argparse
import os
from pathlib import Path
import subprocess

from connectors.codex_bridge.codex_runtime import pin_bundle
from connectors.codex_bridge.rpc import executable, TESTED_VERSIONS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    args = parser.parse_args()
    binary = args.binary or Path(executable())
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run([str(binary), '--version'], capture_output=True, text=True,
                            timeout=15, check=True, creationflags=flags)
    version = result.stdout.strip().removeprefix('codex-cli ')
    if version not in TESTED_VERSIONS:
        raise SystemExit('Codex version is not verified: ' + version + '. Update the paper companion first.')
    pin_bundle(binary, version, TESTED_VERSIONS)
    print('Verified paper companion runtime ready: Codex ' + version)


if __name__ == '__main__':
    main()
