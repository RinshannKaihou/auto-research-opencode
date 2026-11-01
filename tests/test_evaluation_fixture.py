"""Evaluation projects contain only their task inputs and reproducible ledger state."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

from auto_research.native_store import NativeStore, SCHEMA_VERSION
from auto_research.review_lifecycle import unresolved_challenges


spec = importlib.util.spec_from_file_location('evaluation_fixture',
    Path(__file__).parents[1] / 'scripts/e1e2_evaluation_fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@pytest.mark.parametrize('task_id', ['E2-01-stale', 'E2-02-self-reference', 'E2-03-qualified'])
def test_e2_evaluation_projects_reproduce_the_requested_ledger_state(tmp_path, task_id):
    root = fixture.prepare(task_id, tmp_path / task_id)
    assert (root / 'TASK.md').is_file()
    assert not list(root.rglob('scoring.md'))
    with NativeStore(root, readonly=True)._read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        if task_id == 'E2-01-stale':
            assert unresolved_challenges(db)
            assert db.execute("SELECT status FROM knowledge_revisions ORDER BY rowid DESC LIMIT 1").fetchone()[0] == 'retracted'
            assert db.execute('SELECT COUNT(*) FROM evidence_read_receipts').fetchone()[0] > 0
        elif task_id == 'E2-02-self-reference':
            objects = [row[0] for row in db.execute("SELECT object_version FROM publication_items WHERE item_id IN ('data','report') ORDER BY item_id")]
            assert len(objects) == 2 and objects[0] == objects[1] and objects[0] is not None
        else:
            assert db.execute('SELECT COUNT(*) FROM conclusion_drafts').fetchone()[0] == 0


def test_direct_delivery_task_has_a_real_failing_then_fixable_test(tmp_path):
    root = fixture.prepare('E1-03-direct-delivery', tmp_path / 'delivery')
    def run():
        return subprocess.run([sys.executable, '-B', '-m', 'unittest', '-q'], cwd=root, capture_output=True).returncode
    assert run() != 0
    path = root / 'delivery.py'
    path.write_text(path.read_text().replace('recieve', 'receive'))
    assert run() == 0
    assert not list(root.rglob('scoring.md'))
