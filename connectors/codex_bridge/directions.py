"""Apply validated research interests to one fixed repository config file."""
import base64
import json
from pathlib import Path
import threading

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.research_directions import CONFIG_PATH, MAX_DIRECTIONS, clean_profile, load_profile, profile_revision
from src.recommendation_view import current_recommendations
from .daily_update import github, REPO

ENDPOINT = f'repos/{REPO}/contents/{CONFIG_PATH}'


class DirectionChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    profile: dict

    @field_validator('profile')
    @classmethod
    def validate_profile(cls, value):
        return clean_profile(value)


class DirectionManager:
    def __init__(self, root, remote=github, *, runtime=None):
        self.root = Path(root)
        self.runtime = Path(runtime) if runtime is not None else self.root / '.local/codex-bridge'
        self.path = self.root / '.local/research-directions/applied.json'
        self.remote = remote
        self.lock = threading.RLock()

    def _remote(self):
        data = self.remote('GET', ENDPOINT + '?ref=main')
        profile = clean_profile(json.loads(base64.b64decode(data['content'])))
        return data['sha'], profile

    def _write(self, profile):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def _application(self, profile):
        # Read exactly the current local recommendation source; saving counts
        # never rewrites an edition or claims that the public site deployed.
        path = self.runtime / 'recommendations.json'
        if not path.exists():
            path = self.root / 'data/daily.json'
        payload, available = {}, False
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            available = (isinstance(payload, dict) and isinstance(payload.get('core'), list)
                         and isinstance(payload.get('extended'), list))
        except (OSError, ValueError):
            pass
        display = current_recommendations(payload if available else {}, profile)['display_settings']
        return {'local_applied': available, **display}

    def _snapshot(self, profile, verified, **extra):
        return {'profile': profile, 'revision': profile_revision(profile), 'max_directions': MAX_DIRECTIONS,
                'verified': verified, 'application': self._application(profile), **extra}

    def snapshot(self):
        with self.lock:
            try:
                _, profile = self._remote()
                self._write(profile)
                return self._snapshot(profile, True)
            except HTTPException:
                return self._snapshot(load_profile(self.root), False,
                    message='当前显示本机保存的配置，尚未核对线上版本；保存时会重新检查。')

    def save(self, change):
        with self.lock:
            sha, current = self._remote()
            if profile_revision(current) != change.revision:
                raise HTTPException(409, '研究方向已在其他页面修改。请刷新配置后重新编辑，避免覆盖。')
            profile = clean_profile(change.profile)
            commit_url = ''
            if profile != current:
                body = json.dumps(profile, ensure_ascii=False, indent=2) + '\n'
                result = self.remote('PUT', ENDPOINT, {'branch': 'main', 'sha': sha,
                    'message': 'chore: update research directions',
                    'content': base64.b64encode(body.encode()).decode()})
                commit_url = result.get('commit', {}).get('html_url', '')
            self._write(profile)
            return self._snapshot(profile, True, commit_url=commit_url)
