"""Usage: python tools/analyze_session.py runtime/sessions/<session>."""
import collections
import json
import sys
from pathlib import Path


def analyze(directory):
    directory = Path(directory)
    rounds = {}
    previous = {}
    for line in (directory/'events.jsonl').read_text(encoding='utf-8').splitlines():
        event = json.loads(line)
        if event['event'] == 'round_result':
            result = event['result']
            rounds.setdefault(result['seq'], {})['result'] = result
        if event['event'] != 'snapshot':
            continue
        seq = event['round'].get('seq')
        summary = rounds.setdefault(seq, {})
        summary.setdefault('started', event['time'])
        summary['latest'] = event['time']
        summary['latest_round'] = event['round']
        pots = event.get('cooking', [])
        old = previous.get(seq, {})
        timeline = summary.setdefault('timeline', [])
        for field in ('delivered', 'failed'):
            value = event['round'].get(field, 0)
            if value > old.get('round', {}).get(field, 0):
                timeline.append(dict(time=event['time'], event=field, total=value,
                                     score=event['round'].get('score'), time_left=event['round'].get('timeLeft')))
        old_chefs = {c.get('player'): c for c in old.get('chefs', [])}
        for chef in event.get('chefs', []):
            if chef.get('respawning') and not old_chefs.get(chef.get('player'), {}).get('respawning'):
                summary['observed_respawns'] = summary.get('observed_respawns', 0) + 1
                timeline.append(dict(time=event['time'], event='respawn', player=chef.get('player'),
                                     before=old_chefs.get(chef.get('player'))))
        for label, current_names, old_names in (
            ('fire', {f.get('name') for f in event.get('fires', [])}, {f.get('name') for f in old.get('fires', [])}),
            ('burnt', {p['name'] for p in pots if p.get('state') == 'Burnt' and p.get('in')},
             {p['name'] for p in old.get('cooking', []) if p.get('state') == 'Burnt' and p.get('in')})):
            for name in sorted(current_names-old_names):
                timeline.append(dict(time=event['time'], event=label+'_observed', name=name))
            for name in sorted(old_names-current_names):
                timeline.append(dict(time=event['time'], event=label+'_no_longer_observed', name=name))
        previous[seq] = event
        for label, observed in (
            ('fire', bool(event.get('fires'))),
            ('burnt', any(p.get('state') == 'Burnt' and p.get('in') for p in pots))):
            if observed:
                summary.setdefault('first_'+label, event['time'])
                summary[label+'_snapshots'] = summary.get(label+'_snapshots', 0) + 1
    log_path = directory/'cooking.log'
    counts = collections.Counter()
    if log_path.exists():
        for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines():
            for marker in ('[灭火]', '[清理糊锅]', '[救锅]', 'placeCanHandle=False', '撞到地图', '异常:', '已交付'):
                if marker in line: counts[marker] += 1
    report = {'rounds': rounds, 'log_markers': dict(counts),
              'note': 'snapshot counts are observations, not distinct incidents; a live score is not a final pass.'}
    (directory/'analysis.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    print(json.dumps(analyze(sys.argv[1]), ensure_ascii=False, indent=2))
