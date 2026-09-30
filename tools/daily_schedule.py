"""Idempotent scheduled updates; the workflow gate uses only the standard library."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from src.update_cycle import BEIJING, cycle_start
ROOT = Path(__file__).resolve().parents[1]


def update_needed(payload, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(BEIJING)
    start = cycle_start(now)
    check = (payload or {}).get('latest_update') or {}
    try:
        checked_at = datetime.fromisoformat(check.get('checked_at', '').replace('Z', '+00:00'))
        if check.get('outcome') == 'no_new' and checked_at.tzinfo and start <= checked_at <= now:
            return False, 'already_checked_today'
    except (ValueError, TypeError):
        pass
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
    return True, 'scheduled_update_missing'


def workflow_gate(payload, event, scheduled_check=False, now=None):
    # Explicit manual updates remain available even after today's scheduled run.
    if event != 'schedule' and not scheduled_check:
        return True, 'manual_update'
    return update_needed(payload, now)


def dispatch_if_needed(remote, now=None, *, runtime=None):
    from connectors.codex_bridge.daily_update import DailyUpdater
    return DailyUpdater(runtime or ROOT / '.local/codex-bridge', remote=remote).catch_up(now=now, reconnected=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dispatch', action='store_true', help='Check GitHub and dispatch a missing update')
    args = parser.parse_args()
    if args.dispatch:
        log = ROOT / '.local/daily-schedule.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        code = 0
        try:
            from connectors.codex_bridge.daily_update import DailyUpdater
            result = DailyUpdater(ROOT / '.local/codex-bridge').catch_up(reconnected=True)
            if result.get('state') == 'failed':
                code = 1
        except Exception:
            # gh output, proxy addresses and credentials must not reach logs.
            result = {'state': 'check_failed', 'message': 'Check GitHub login and network; next check will retry.'}
            code = 1
        result['checked_at'] = datetime.now(BEIJING).isoformat()
        result = {k: result[k] for k in ('state', 'message', 'run_id', 'cycle', 'checked_at') if k in result}
        text = json.dumps(result, ensure_ascii=False)
        with log.open('a', encoding='utf-8') as output:
            output.write(text + '\n')
        print(text)
        return code
    try:
        payload = json.loads((ROOT / 'data/daily.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        payload = {}
    try:
        payload['latest_update'] = json.loads((ROOT / 'data/update-status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    needed, reason = workflow_gate(payload, os.environ.get('GITHUB_EVENT_NAME', ''),
                                  os.environ.get('SCHEDULED_CHECK', '').lower() == 'true')
    papers_needed = needed
    from tools.public_updates import needed as public_needed
    needed = needed or public_needed(ROOT)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'needed={str(needed).lower()}\n')
            output.write(f'papers_needed={str(papers_needed).lower()}\n')
    print(json.dumps({'needed': needed, 'reason': reason}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
