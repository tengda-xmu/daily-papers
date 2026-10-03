import json
from pathlib import Path

import pytest

from connectors.codex_bridge import codex_runtime, rpc


def bundle(tmp_path):
    source = tmp_path / 'extension'
    source.mkdir()
    (source / 'codex.exe').write_bytes(b'verified binary')
    (source / 'codex-resources').mkdir()
    (source / 'codex-resources/data').write_bytes(b'verified resource')
    return source / 'codex.exe'


def test_pinned_runtime_survives_extension_upgrade_and_path_changes(tmp_path, monkeypatch):
    binary = bundle(tmp_path)
    root = tmp_path / 'private-runtime'
    pinned = codex_runtime.pin_bundle(binary, '0.159.2', rpc.TESTED_VERSIONS, root)
    monkeypatch.setattr(codex_runtime, 'RUNTIME', root)
    monkeypatch.delenv('PAPER_CODEX_EXE', raising=False)
    monkeypatch.setattr(rpc.shutil, 'which', lambda _: str(binary))
    binary.write_bytes(b'new unverified editor version')
    assert Path(rpc.executable()) == pinned
    binary.unlink()
    assert Path(rpc.executable()) == pinned
    assert codex_runtime.pin_bundle(pinned, '0.159.2', rpc.TESTED_VERSIONS, root) == pinned
    assert len(list(root.glob('0.159.2-*'))) == 1


def test_explicit_override_remains_authoritative(tmp_path, monkeypatch):
    binary = bundle(tmp_path)
    monkeypatch.setenv('PAPER_CODEX_EXE', str(binary))
    assert rpc.executable() == str(binary.resolve())
    binary.unlink()
    with pytest.raises(rpc.CodexError, match='PAPER_CODEX_EXE'):
        rpc.executable()


@pytest.mark.parametrize('name', ['codex.exe', 'codex-resources/data'])
def test_modified_bundle_is_not_selected(tmp_path, name):
    root = tmp_path / 'runtime'
    pinned = codex_runtime.pin_bundle(bundle(tmp_path), '0.159.2', rpc.TESTED_VERSIONS, root)
    (pinned.parent / name).write_bytes(b'modified')
    assert codex_runtime.pinned_executable(rpc.TESTED_VERSIONS, root) is None


def test_unknown_versions_and_paths_cannot_replace_selection(tmp_path):
    root = tmp_path / 'runtime'
    binary = bundle(tmp_path)
    pinned = codex_runtime.pin_bundle(binary, '0.159.2', rpc.TESTED_VERSIONS, root)
    with pytest.raises(ValueError):
        codex_runtime.pin_bundle(binary, 'unverified', rpc.TESTED_VERSIONS, root)
    assert codex_runtime.pinned_executable(rpc.TESTED_VERSIONS, root) == pinned
    value = json.loads((root / 'current.json').read_text())
    value['directory'] = '../extension'
    (root / 'current.json').write_text(json.dumps(value))
    assert codex_runtime.pinned_executable(rpc.TESTED_VERSIONS, root) is None
