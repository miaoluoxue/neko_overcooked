"""Append-only replay evidence, separate from the verbose planner output."""
import hashlib
import json
import time
from pathlib import Path


class RoundJournal:
    def __init__(self, root):
        self.directory = Path(root) / time.strftime('%Y%m%d-%H%M%S')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.results = set()
        self.previous = None
        self.last_snapshot = None
        engine = Path(__file__).resolve().parents[1] / 'neko' / 'engine.py'
        self.write('session', engine_sha256=hashlib.sha256(engine.read_bytes()).hexdigest())

    def write(self, event, **data):
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(time=time.time(), event=event, **data), ensure_ascii=False) + '\n')

    def observe(self, state, dynamic=None, orders=None):
        layout = state.get('layout') or {}
        round_data = state.get('round') or {}
        key = (state.get('scene'), round_data.get('seq'))
        if state.get('inRound'):
            if key != self.previous:
                self.write('round_start', scene=key[0], seq=key[1])
                self.previous = key
                self.last_snapshot = None
            previous = self.last_snapshot or {}
            current = {c.get('player', c.get('id')): c for c in layout.get('chefs', [])}
            for identity, chef in current.items():
                if chef.get('respawning') and not previous.get(identity, {}).get('respawning'):
                    self.write('chef_respawn', scene=key[0], seq=key[1], chef=chef,
                               before=previous.get(identity))
            self.last_snapshot = current
            self.write('snapshot', scene=key[0], round=round_data,
                       chefs=layout.get('chefs', []), cooking=layout.get('cooking', []),
                       stations=layout.get('stations', []), items=layout.get('items', []),
                       orders=orders if orders is not None else state.get('orders'),
                       fires=(dynamic or {}).get('fires', []))
        result = state.get('lastResult')
        if result:
            identity = (result.get('scene'), result.get('seq'))
            if identity not in self.results:
                self.write('round_result', result=result)
                self.results.add(identity)
                with (self.directory / 'results.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps(result, ensure_ascii=False) + '\n')
