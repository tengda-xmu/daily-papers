"""Select new papers and at most one evidence-backed historical core paper."""
from src.auto_reading import eligible
from src.recommendation_heat import repeat_evidence
from src.research_directions import select_balanced


def select(papers, history, analyses, heat, profile, now, policy):
    new, core_candidates = [], []
    for paper in papers:
        state = history.find(paper) if history else None
        if not state:
            new.append(paper)
            core_candidates.append(paper)
            continue
        paper['id'] = state['id']
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
    # Preserve weighted direction allocation while eliminating overflow old papers.
    pool = core_candidates[:]
    while True:
        core = select_balanced(pool, profile, 'core', profile['core_count'])
        old = [p for p in core if p.get('recommendation_decision')]
        if len(old) <= int(policy['historical_slots']):
            break
        remove = {p['id'] for p in old[int(policy['historical_slots']):]}
        pool = [p for p in pool if p['id'] not in remove]
    extended = select_balanced(new, profile, 'extended', profile['extended_count'], (p['id'] for p in core))
    return core, extended
