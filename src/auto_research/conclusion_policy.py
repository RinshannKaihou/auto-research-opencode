"""Resolve the conclusion contract for the current research generation."""

import json


def current_contract(db, config=None):
    if config is None:
        row = db.execute('SELECT config FROM project LIMIT 1').fetchone()
        config = json.loads(row[0]) if row else {}
    if db.execute('PRAGMA user_version').fetchone()[0] < 15:
        return config.get('conclusion_contract', 'legacy-v1')
    row = db.execute('SELECT c.contract FROM generation_conclusion_contracts c '
                     'JOIN workflow_project p ON p.generation=c.generation WHERE p.id=1').fetchone()
    return row[0] if row else 'legacy-v1'


def set_contract(db, generation, contract, origin):
    db.execute('INSERT INTO generation_conclusion_contracts(generation,contract,origin) VALUES(?,?,?) '
               'ON CONFLICT(generation) DO UPDATE SET contract=excluded.contract,origin=excluded.origin',
               (generation, contract, origin))
