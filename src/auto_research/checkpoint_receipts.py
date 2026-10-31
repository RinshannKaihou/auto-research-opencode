"""Checkpoint read receipts; callers supply a snapshot, never a write-time latest base."""
import hashlib
import json
from uuid import uuid4

from .errors import ValidationError, CheckpointBaseMismatch


def digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def identity(store, db, host_id, session_id):
    association = store._association(db, host_id, session_id)
    session = db.execute("SELECT * FROM workflow_sessions WHERE host_id=? AND session_id=? AND detached=0",
                         (host_id, session_id)).fetchone()
    if not session or session['role'] not in {'main', 'node_core', 'exploration'}:
        raise ValidationError('checkpoint requires a main or node session')
    attempt = db.execute("SELECT * FROM attempts WHERE association_id=? AND ended_at IS NULL",
                         (association['association_id'],)).fetchone()
    owner = attempt['node_id'] if attempt and attempt['node_id'] else session['node_id']
    if session['role'] != 'main' and not owner:
        raise ValidationError('Node checkpoint has no associated node')
    if session['node_id'] and owner != session['node_id']:
        raise ValidationError('checkpoint attempt and session node disagree')
    generation = db.execute("SELECT generation FROM workflow_project WHERE id=1").fetchone()
    return dict(host_id=host_id, session_id=session_id, association_id=association['association_id'],
                attempt_id=attempt['attempt_id'] if attempt else None, node_id=owner,
                generation=generation[0] if generation else 0)


def snapshot(store, db, caller):
    selected, recovery = store.checkpoint_view(db, caller['node_id'])
    checkpoint = dict(selected) if selected else None
    if checkpoint:
        checkpoint['state'] = json.loads(checkpoint['state'])
        checkpoint['source_identity'] = json.loads(checkpoint['source_identity'])
    descriptor = {**caller, 'head_revision': recovery['head_revision'],
                  'selected_checkpoint_id': selected['checkpoint_id'] if selected else None,
                  'selected_revision': selected['revision'] if selected else None,
                  'state_digest': digest(checkpoint['state'] if checkpoint else None),
                  'source_sequence': db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]}
    return dict(checkpoint=checkpoint, recovery=recovery, snapshot=descriptor)


def issue(db, descriptor, instance_id, origin, created_at):
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValidationError('plugin_instance_id is required')
    if origin not in {'context', 'query', 'write_result'}:
        raise ValidationError('Invalid checkpoint receipt origin')
    base = 'CB-' + uuid4().hex
    db.execute('''INSERT INTO checkpoint_read_receipts VALUES
                  (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (base, descriptor['host_id'], descriptor['session_id'], instance_id,
                descriptor['association_id'], descriptor['attempt_id'], descriptor['generation'],
                descriptor['node_id'], descriptor['head_revision'], descriptor['selected_checkpoint_id'],
                descriptor['selected_revision'], descriptor['state_digest'], descriptor['source_sequence'],
                origin, created_at))
    return base


def expected(db, caller, base_id, instance_id):
    if not isinstance(base_id, str) or not isinstance(instance_id, str):
        raise CheckpointBaseMismatch('checkpoint base and plugin instance must be supplied')
    row = db.execute('SELECT * FROM checkpoint_read_receipts WHERE base_id=?', (base_id,)).fetchone()
    if (not row or row['plugin_instance_id'] != instance_id
            or any(row[key] != value for key, value in caller.items())):
        raise CheckpointBaseMismatch('checkpoint base must match this session, instance and current work owner')
    return row['head_revision']


def validate_observation(store, db, caller, descriptor):
    if not isinstance(descriptor, dict) or any(descriptor.get(k) != v for k, v in caller.items()):
        raise CheckpointBaseMismatch('checkpoint snapshot owner changed; read the current checkpoint')
    head = descriptor.get('head_revision')
    if type(head) is not int or head < 0:
        raise ValidationError('checkpoint head_revision must be a nonnegative integer')
    current = db.execute('SELECT COALESCE(MAX(revision),0) FROM node_checkpoints WHERE node_id IS ?',
                         (caller['node_id'],)).fetchone()[0]
    if head > current:
        raise CheckpointBaseMismatch('checkpoint snapshot head does not exist')
    selected = descriptor.get('selected_checkpoint_id')
    row = db.execute('SELECT * FROM node_checkpoints WHERE checkpoint_id=?', (selected,)).fetchone() if selected else None
    if selected and (not row or row['node_id'] != caller['node_id'] or row['revision'] > head
                     or row['revision'] != descriptor.get('selected_revision')):
        raise CheckpointBaseMismatch('checkpoint snapshot does not match immutable history')
    if not selected and descriptor.get('selected_revision') is not None:
        raise CheckpointBaseMismatch('checkpoint snapshot has no selected row')
    if descriptor.get('state_digest') != digest(json.loads(row['state']) if row else None):
        raise CheckpointBaseMismatch('checkpoint snapshot state digest differs')
    sequence = descriptor.get('source_sequence')
    upper = db.execute('SELECT COALESCE(MAX(event_id),0) FROM events').fetchone()[0]
    if type(sequence) is not int or not 0 <= sequence <= upper:
        raise ValidationError('Invalid checkpoint snapshot source_sequence')
