import json
import tempfile
import unittest
from pathlib import Path
from tools.round_journal import RoundJournal


class JournalTests(unittest.TestCase):
    def test_result_is_retained_once_across_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RoundJournal(directory)
            result = dict(scene='s_balloon_1_5', seq=1, score=96, passed=True)
            for _ in range(3):
                journal.observe(dict(inRound=False, lastResult=result))
            journal.observe(dict(inRound=True, scene='s_balloon_1_5', round={'seq':2}))
            saved = [json.loads(line) for line in (journal.directory/'results.jsonl').read_text().splitlines()]
            self.assertEqual(saved, [result])
