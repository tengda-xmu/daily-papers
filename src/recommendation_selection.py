"""CNS first, then publication date; preserve evidence-backed history limits."""
from datetime import datetime, timezone
import re
import unicodedata

from src.auto_reading import eligible
from src.conferences import info as conference_info
from src.models import RawRecord, parse_date
from src.recommendation_heat import repeat_evidence
from src.research_directions import active_directions
from src.research_focus import focus_priority
from src.venues import venue_priority


def matching_directions(paper, profile, tier=None):
    return [d for d in active_directions(profile, tier) if d['id'] in paper.get('topic_tags', [])]


def ranking_key(paper, profile=None, tier=None):
    """One ordering for the pipeline and final selection; no direction quotas."""
    record = RawRecord.from_mapping(paper)
    weights = [d['weight'] for d in matching_directions(paper, profile, tier)] if profile else []
    priority = max(venue_priority(record.venue), 1 if conference_info(paper) else 0)
    published = parse_date(record.published_at)
    date_value = (published - datetime.min.replace(tzinfo=timezone.utc)).total_seconds() if published else 0
    if priority == 3:
        ordering = (date_value, focus_priority(record), max(weights, default=0), bool(record.abstract))
    else:
        # The new preference changes CNS selection, not ordinary-journal order.
        ordering = (focus_priority(record), len(record.topic_tags), bool(record.abstract), date_value)
    title = re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', unicodedata.normalize('NFKC', record.title).replace('°', 'deg').casefold())
    return (priority, *ordering, record.source_score, record.citation_count or 0, title, paper.get('id', record.source_id))


def _with_direction(paper, profile, tier):
    directions = matching_directions(paper, profile, tier)
    if not directions:
        return None
    # max is stable: equal weights retain the user's configured direction order.
    return {**paper, 'recommended_direction': max(directions, key=lambda d: d['weight'])['id']}


def select(papers, history, analyses, heat, profile, now, policy):
    new, core_candidates = [], []
    for paper in papers:
        state = history.find(paper) if history else None
        if not state:
            new.append(paper)
            core_candidates.append(paper)
            continue
        paper = {**paper, 'id': state['id']}
        assessment = analyses.get(state['id'], {})
        if not eligible(assessment, paper):
            continue
        previous = state.get('last_core') or state['latest']
        evidence = repeat_evidence(state, heat.get('papers', {}).get(state['id'], {}), now, policy) if state.get('last_core') else None
        if state.get('last_core') and not evidence:
            continue
        reason = assessment['evaluation']['reason']
        decision = {'kind': 'repeat' if evidence else 'promotion', 'previous': previous,
                    'reason': reason, 'heat': evidence}
        core_candidates.append({**paper, 'recommendation_decision': decision})
    core, extended, used, old_count = [], [], set(), 0
    for paper in sorted(core_candidates, key=lambda p: ranking_key(p, profile, 'core'), reverse=True):
        if len(core) >= profile['core_count']:
            break
        selected = _with_direction(paper, profile, 'core')
        old = bool(paper.get('recommendation_decision'))
        if selected is None or paper['id'] in used or (old and old_count >= int(policy['historical_slots'])):
            continue
        core.append(selected)
        used.add(paper['id'])
        old_count += old
    for paper in sorted(new, key=lambda p: ranking_key(p, profile, 'extended'), reverse=True):
        if len(extended) >= profile['extended_count']:
            break
        selected = _with_direction(paper, profile, 'extended')
        if selected is not None and paper['id'] not in used:
            extended.append(selected)
            used.add(paper['id'])
    return core, extended
