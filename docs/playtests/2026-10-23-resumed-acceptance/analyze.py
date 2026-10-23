"""Derive measurements from preserved browser records; never rewrite raw evidence."""
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROWS = [json.loads(line) for line in (ROOT / 'turns.jsonl').read_text().splitlines()]
EXCLUDED = {
    '016': 'Operator named an unseen laboratory after the door attempt failed; invalid earned-knowledge progression sample.',
    '049': 'Operator confirmed after a rejected declaration; no pending action existed.',
}
TIMING_EXCLUDED = {'006': 'Retained play tabs saturated browser connections; server received the request only after stale tabs closed.'}
EMPTY = {
    '007': 'Agrees to accompany then denies agreeing to guide; no actionable difference.',
    '012': 'No response or new information to first exterior call.',
    '014': 'Second exterior question repeats no response.',
    '036': 'Present sheep treated as unavailable to speak because held by Guz.',
    '037': 'Explicit direct speech to present sheep repeats custody-based non-answer.',
    '039': 'Direct rescue question repeats denial and gives no lead.',
    '047': 'Concrete confirmed surrender yields generic unknown-response non-outcome.',
    '051': 'Unconditional confirmed surrender yields same generic non-outcome.',
}
METRICS = ['busy_paint', 'first_token', 'first_text_paint', 'done', 'composer_ready', 'journal_settled', 'map_settled', 'failed']


def metrics(row):
    return {m['name'].rsplit(':', 1)[-1]: m['duration'] for m in row['performance']}


def percentile(values, q):
    values = sorted(values)
    i = (len(values) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return values[lo] + (values[hi] - values[lo]) * (i - lo)


def stats(rows):
    result = {'actions': len(rows)}
    for key in METRICS:
        values = [metrics(r)[key] for r in rows if key in metrics(r)]
        result[key] = {'n': len(values)}
        if values:
            result[key].update(min_ms=min(values), max_ms=max(values), p50_ms=percentile(values, .5), p90_ms=percentile(values, .9))
    return result


def category(row):
    raw = row['category'].removeprefix('ai-')
    return {'object': 'exploration', 'surrender-declaration': 'declaration'}.get(raw, raw)


eligible = [r for r in ROWS if r['id'] not in EXCLUDED]
measured = [r for r in eligible if r['id'] not in TIMING_EXCLUDED]
summary = {'raw_actions': len(ROWS), 'eligible_actions': len(eligible), 'timing_actions': len(measured),
           'excluded': EXCLUDED, 'timing_excluded': TIMING_EXCLUDED,
           'units': 'milliseconds', 'quantiles': 'linear interpolation', 'all': stats(measured),
           'empty_actions': {'n': sum(r['id'] in EMPTY for r in eligible), 'eligible': len(eligible)}, 'by_source': {}}
for label, sessions in [('wild_sheep', [36, 37]), ('retained_ai', [34])]:
    selected = [r for r in measured if r['session'] in sessions]
    summary['by_source'][label] = {'all': stats(selected), 'by_category': {c: stats([r for r in selected if category(r) == c]) for c in sorted({category(r) for r in selected})}}
baseline = [json.loads(line) for line in (ROOT.parent / '2026-10-22-preparation-runtime-replay/turns.jsonl').read_text().splitlines()]
summary['exact_input_matches'] = []
for row in measured:
    if row['input'].lower().strip() in {'yes', 'no'} or row['category'] == 'combat-round':
        continue
    prior = next((r for r in baseline if r['input'] == row['input'] and r['id'] not in {'029', '060', '061'}), None)
    if prior:
        summary['exact_input_matches'].append({'current_id': row['id'], 'baseline_id': prior['id'], 'input': row['input'], 'current_ms': metrics(row), 'baseline_ms': metrics(prior), 'caveat': 'Exact text only; context, dice, outcome and reply length differ.'})
(ROOT / 'timing-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
audit = [{'id': r['id'], 'session': r['session'], 'input': r['input'], 'category': category(r), 'excluded': r['id'] in EXCLUDED, 'timing_excluded': r['id'] in TIMING_EXCLUDED, 'useful_decision_or_progress': None if r['id'] in EXCLUDED else r['id'] not in EMPTY, 'reason': EXCLUDED.get(r['id'], EMPTY.get(r['id'], 'Movement, discovery, meaningful choice, rules feedback, safe confirmation or resolved combat.'))} for r in ROWS]
(ROOT / 'action-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
repeats = defaultdict(list)
for row in ROWS:
    repeats[row['input']].append(row['id'])
(ROOT / 'repeated-inputs.json').write_text(json.dumps([{'input': k, 'ids': v} for k, v in repeats.items() if len(v) > 1], indent=2) + '\n')
before, after = [json.loads((ROOT / name).read_text()) for name in ['resume-inventory.json', 'final-inventory.json']]
preservation = {}
for path in ['/api/campaigns', '/api/characters', '/api/adventures']:
    key = 'slug' if path.endswith('adventures') else 'id'
    b, a = [{r[key]: r for r in data[path]['body']} for data in [before, after]]
    preservation[path] = {'before': len(b), 'after': len(a), 'missing': sorted(set(b) - set(a)), 'added': sorted(set(a) - set(b)), 'changed_existing': [k for k in b if k in a and b[k] != a[k]]}
(ROOT / 'preservation-comparison.json').write_text(json.dumps(preservation, indent=2) + '\n')
for source, data in summary['by_source'].items():
    print(source)
    for c, s in data['by_category'].items():
        print(c, s['actions'], *[f"{m}={s[m].get('p50_ms',0)/1000:.2f}/{s[m].get('p90_ms',0)/1000:.2f} n={s[m]['n']}" for m in ['first_token', 'composer_ready', 'journal_settled', 'map_settled']])
print('counts', summary['raw_actions'], summary['eligible_actions'], summary['timing_actions'], summary['empty_actions'])
print('preservation', preservation)
for match in summary['exact_input_matches']:
    print('match', match['current_id'], match['baseline_id'], [(m, round(match['baseline_ms'].get(m, 0)/1000, 2), round(match['current_ms'].get(m, 0)/1000, 2)) for m in ['first_token', 'composer_ready']])
