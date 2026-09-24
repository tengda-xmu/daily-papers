"""Idempotent morning updates; the workflow gate uses only the standard library."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import uuid

BEIJING = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {'queued', 'in_progress', 'waiting', 'pending', 'requested'}


def update_needed(payload, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(BEIJING)
    start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < start:
        return False, 'before_morning_update'
    try:
        generated = datetime.fromisoformat(payload['generated_at'].replace('Z', '+00:00'))
        if generated.tzinfo is None:
            raise ValueError('Missing timezone')
        valid = (isinstance(payload.get('core'), list) and bool(payload['core'])
                 and isinstance(payload.get('extended'), list))
        if valid and start <= generated <= now:
            return False, 'already_updated_today'
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return True, 'morning_update_missing'


def workflow_gate(payload, event, scheduled_check=False, now=None):
    # Explicit manual updates remain available even after today's morning run.
    if event != 'schedule' and not scheduled_check:
        return True, 'manual_update'
    return update_needed(payload, now)


def dispatch_if_needed(remote, now=None):
    # Importing the bridge adapter here does not construct the app or open its DB.
    from connectors.codex_bridge.daily_update import API, REPO

    now = now or datetime.now(timezone.utc)
    if now.astimezone(BEIJING).hour < 6:
        return {'state': 'before_morning_update'}
    # The daily workflow commits data only after deployment, so a current record
    # also confirms that a completed update has passed the publication step.
    content = remote('GET', f'repos/{REPO}/contents/data/daily.json?ref=main')
    # The contents API omits the body for files over 1 MB. Recommendation
    # metadata includes candidate records and can exceed that threshold.
    if content.get('encoding') == 'none':
        sha = content.get('sha', '')
        if not re.fullmatch(r'[a-f0-9]{40,64}', sha):
            raise ValueError('Invalid data blob identifier')
        content = remote('GET', f'repos/{REPO}/git/blobs/{sha}')
    payload = json.loads(base64.b64decode(content['content']))
    needed, reason = update_needed(payload, now)
    if not needed:
        return {'state': reason}
    runs = remote('GET', API + '/workflows/daily.yml/runs?branch=main&per_page=30')
    if any(run.get('status') in ACTIVE for run in runs.get('workflow_runs', [])):
        return {'state': 'update_already_running'}
    day = now.astimezone(BEIJING).date().isoformat()
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{REPO}/morning/{day}'))
    response = remote('POST', API + '/workflows/daily.yml/dispatches', {
        'ref': 'main', 'return_run_details': True,
        'inputs': {'request_id': request_id, 'scheduled_check': True, 'send_digest': False}})
    # Never retry an ambiguous POST here. The next scheduled check first reads
    # current runs; the workflow itself checks freshness again under its lock.
    return {'state': 'dispatched', 'run_id': response.get('workflow_run_id')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dispatch', action='store_true', help='Check GitHub and dispatch a missing update')
    args = parser.parse_args()
    if args.dispatch:
        from connectors.codex_bridge.daily_update import github
        log = ROOT / '.local/daily-schedule.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        code = 0
        try:
            result = dispatch_if_needed(github)
        except Exception:
            # gh output, proxy addresses and credentials must not reach logs.
            result = {'state': 'check_failed', 'message': 'Check GitHub login and network; next check will retry.'}
            code = 1
        result['checked_at'] = datetime.now(BEIJING).isoformat()
        text = json.dumps(result, ensure_ascii=False)
        with log.open('a', encoding='utf-8') as output:
            output.write(text + '\n')
        print(text)
        return code
    try:
        payload = json.loads((ROOT / 'data/daily.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        payload = {}
    needed, reason = workflow_gate(payload, os.environ.get('GITHUB_EVENT_NAME', ''),
                                  os.environ.get('SCHEDULED_CHECK', '').lower() == 'true')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'needed={str(needed).lower()}\n')
    print(json.dumps({'needed': needed, 'reason': reason}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
