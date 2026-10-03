"""Keep a verified private Codex bundle independent of editor/PATH upgrades."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

RUNTIME = Path(__file__).resolve().parents[2] / '.local/codex-bridge/codex-runtime'
BUNDLE_FILES = ('codex.exe', 'codex-command-runner.exe', 'codex-code-mode-host.exe',
                'codex-windows-sandbox-setup.exe', 'rg.exe', 'codex-package.json',
                'codex-resources', 'codex-path')


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def pinned_executable(versions, runtime=None):
    root = Path(runtime or RUNTIME).resolve()
    try:
        value = json.loads((root / 'current.json').read_text(encoding='utf-8'))
        if value['version'] not in versions or not value['files']:
            return None
        folder = (root / value['directory']).resolve()
        if not folder.is_relative_to(root) or folder == root:
            return None
        for name, expected in value['files'].items():
            path = (folder / name).resolve()
            if not path.is_relative_to(folder) or digest(path) != expected:
                return None
        binary = folder / 'codex.exe'
        return binary if 'codex.exe' in value['files'] and binary.is_file() else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def pin_bundle(binary, version, versions, runtime=None):
    if version not in versions or not re.fullmatch(r'[0-9A-Za-z.-]+', version):
        raise ValueError('Only verified Codex versions may be pinned')
    root = Path(runtime or RUNTIME).resolve()
    binary = Path(binary).resolve()
    if binary.name.lower() != 'codex.exe' or not binary.is_file():
        raise ValueError('Expected a Windows Codex executable')
    existing = pinned_executable(versions, root)
    if existing and existing == binary:
        return existing
    # Never overwrite an executable in use. Publish the selection atomically
    # only after every bundled file has been copied and verified.
    folder = root / (version + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    for name in BUNDLE_FILES:
        source = binary if name == 'codex.exe' else binary.parent / name
        if source.is_dir():
            shutil.copytree(source, folder / name)
        elif source.is_file():
            shutil.copy2(source, folder / name)
    files = {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob('*') if p.is_file()}
    if files.get('codex.exe') != digest(binary):
        raise ValueError('Codex changed while copying; existing selection was retained')
    value = {'version': version, 'directory': folder.name, 'files': files}
    temporary = root / ('selection-' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(root / 'current.json')
    return folder / 'codex.exe'
