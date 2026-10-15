"""Apply current display counts without changing a published edition."""
from copy import deepcopy

from src.research_directions import clean_profile, profile_revision


def current_recommendations(payload, profile):
    """Project the original current edition; never use this for historical pages.

    Each call starts from the unmodified source payload, so reducing a tier and
    restoring its count does not lose members. Counts never borrow papers from
    another tier, the candidate pool, or earlier editions. The edition identity,
    original research-profile snapshot, and all other source data stay intact.
    """
    if not isinstance(payload, dict):
        raise ValueError('当前推荐数据无效。')
    profile = clean_profile(profile)
    result = deepcopy(payload)
    available, requested = {}, {}
    for tier in ('core', 'extended'):
        members = payload.get(tier, [])
        if not isinstance(members, list):
            raise ValueError('当前推荐列表无效。')
        key = tier + '_count'
        available[key] = len(members)
        requested[key] = profile[key]
        result[tier] = deepcopy(members[:profile[key]])
    result['display_settings'] = {
        'scope': 'current_edition', 'revision': profile_revision(profile),
        'core_count': len(result['core']), 'extended_count': len(result['extended']),
        'requested': requested, 'available': available,
    }
    return result
