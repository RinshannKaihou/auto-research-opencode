"""Atomic execution closure with an explicit, self-declared research outcome.

The receipt is not a correctness certificate. Scientific review scheduling and
claim coverage remain separate from this minimal delivery contract.
"""
import json

from . import frozen_refs
from .errors import ConflictError, ValidationError
from .workflow_store import now


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f'{name} must be nonempty text')
    return value.strip()


def texts(value, name):
    if not isinstance(value, list):
        raise ValidationError(f'{name} must be a list of nonempty strings')
    return [text(item, name) for item in value]


class ConclusionStore:
    @staticmethod
    def conclusion_history(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_conclusions'").fetchone():
            return []
        return [{**dict(row), 'gaps': json.loads(row['gaps']), 'review': json.loads(row['review'])}
                for row in db.execute('SELECT * FROM project_conclusions ORDER BY generation')]

    @staticmethod
    def conclusion_view(db):
        # Read-only browsing of schema-9 projects must not migrate their ledger.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_conclusions'").fetchone():
            return None
        row = db.execute(
            'SELECT c.* FROM project_conclusions c JOIN workflow_project w '
            'ON w.generation=c.generation WHERE w.id=1'
        ).fetchone()
        if not row:
            return None
        return {**dict(row), 'gaps': json.loads(row['gaps']), 'review': json.loads(row['review'])}

    def _conclusion_reference(self, db, ref, *, final=False):
        """Resolve full refs and verify frozen bytes, including directory owners."""
        parsed = frozen_refs.parse(ref)
        if final and parsed.get('form') not in {'publication', 'publication_item'}:
            raise ValidationError('final_ref must identify a publication or publication item')
        resolution = frozen_refs.require(db, self.root, ref)
        if final:
            publication = db.execute('SELECT status FROM publications WHERE publication_id=?',
                                     (parsed['publication_id'],)).fetchone()
            if publication['status'] != 'complete':
                raise ValidationError('Final publication must be complete (delivery, not scientific verification)')
        if parsed.get('form') == 'publication':
            items = list(db.execute('SELECT item_id FROM publication_items WHERE publication_id=? ORDER BY item_id',
                                    (parsed['publication_id'],)))
            if not items:
                raise ValidationError(f'{ref}: publication has no items')
            for item in items:
                self._conclusion_reference(db, f"{ref}#{item['item_id']}")
            return
        if resolution['object']:
            verified, path = frozen_refs.open(db, self.root, ref)
            if verified['outcome'] != 'resolved':
                raise ValidationError(verified['message'])
            if path.is_file():
                with path.open('rb') as stream:
                    stream.read(1)
            elif final:
                raise ValidationError(f'{ref}: final item must be readable content, not a directory')
        elif parsed.get('form') == 'publication_item':
            row = db.execute('SELECT content FROM publication_items WHERE publication_id=? AND item_id=?',
                             (parsed['publication_id'], parsed['item_id'])).fetchone()
            content = json.loads(row['content'])
            if content is None or content == {} or content == '' or content == []:
                raise ValidationError(f'{ref}: publication item has no readable content')
        else:
            raise ValidationError(f'{ref}: review reference must identify frozen material')

    def conclude(self, host_id, session_id, fields, request_id):
        # Preserve the caller payload for idempotent retries; validate inside work.
        payload = {'host_id': host_id, 'session_id': session_id, 'fields': fields}

        def work(db):
            association = self._association(db, host_id, session_id)
            run = db.execute('SELECT * FROM workflow_project WHERE id=1').fetchone()
            role = db.execute('SELECT * FROM workflow_sessions WHERE session_id=? AND host_id=? AND detached=0',
                              (session_id, host_id)).fetchone()
            if not run or not role or role['role'] != 'main' or run['main_session_id'] != session_id:
                raise ValidationError('Only the current project main session may conclude')
            if run['state'] == 'complete' or self.conclusion_view(db):
                raise ConflictError('This run is already concluded; start a new generation before changing its conclusion')
            if db.execute("SELECT 1 FROM exploration_tasks WHERE state IN ('queued','starting','running','waiting','stopping','unverified') LIMIT 1").fetchone():
                raise ConflictError('Live node tasks must end before conclusion')
            if db.execute("SELECT 1 FROM specialist_tasks WHERE state IN ('starting','running','unverified') LIMIT 1").fetchone():
                raise ConflictError('Live specialist tasks must end before conclusion')
            # Also cover manual work and a queued focus transition, which finish
            # would otherwise open after we had declared the project complete.
            if db.execute('SELECT 1 FROM attempts WHERE ended_at IS NULL AND '
                          '(association_id<>? OR node_id IS NOT NULL) LIMIT 1',
                          (association['association_id'],)).fetchone():
                raise ConflictError('Live node or other-session work segments must end before conclusion')
            if db.execute('SELECT 1 FROM focus_queue LIMIT 1').fetchone():
                raise ConflictError('Queued work segments must be resolved before conclusion')
            if not isinstance(fields, dict):
                raise ValidationError("conclusion fields must be an object")
            summary = text(fields.get('summary'), 'summary')
            final_ref = text(fields.get('final_ref'), 'final_ref')
            outcome = fields.get('outcome')
            if not isinstance(outcome, str) or outcome not in {'answered', 'partial', 'unresolved'}:
                raise ValidationError('outcome must be answered, partial, or unresolved')
            gaps = texts(fields.get('gaps'), 'gaps')
            if outcome != 'answered' and not gaps:
                raise ValidationError('partial/unresolved outcomes require nonempty gaps')
            review = fields.get('review')
            if not isinstance(review, dict) or set(review) != {'status', 'refs', 'limitations'}:
                raise ValidationError('review requires exactly status, refs, and limitations')
            status = review['status']
            if not isinstance(status, str) or status not in {'unreviewed', 'partial', 'reviewed'}:
                raise ValidationError('review.status must be unreviewed, partial, or reviewed')
            review = {'status': status, 'refs': texts(review['refs'], 'review.refs'),
                      'limitations': texts(review['limitations'], 'review.limitations')}
            if status != 'unreviewed' and not review['refs']:
                raise ValidationError('partial/reviewed review requires frozen review.refs')
            if status != 'reviewed' and not review['limitations']:
                raise ValidationError('unreviewed/partial review requires limitations')
            self._conclusion_reference(db, final_ref, final=True)
            for ref in review['refs']:
                self._conclusion_reference(db, ref)
            value = dict(conclusion_id=self._next(db, 'conclusion', 'C'), operation_id=request_id,
                         generation=run['generation'], host_id=host_id, session_id=session_id,
                         summary=summary, final_ref=final_ref, outcome=outcome, gaps=gaps,
                         review=review, created_at=now())
            db.execute('INSERT INTO project_conclusions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       tuple(json.dumps(value[key], ensure_ascii=False, sort_keys=True) if key in {'gaps', 'review'} else value[key]
                             for key in value))
            attempt = db.execute('SELECT * FROM attempts WHERE association_id=? AND ended_at IS NULL',
                                 (association['association_id'],)).fetchone()
            if attempt:
                self._finish_in_tx(db, host_id, session_id, 'finished',
                                   {'reason': 'project-concluded', 'conclusion_id': value['conclusion_id']})
            db.execute("UPDATE workflow_project SET state='complete',updated_at=? WHERE id=1", (value['created_at'],))
            db.execute("UPDATE project SET control='paused'")
            db.execute("UPDATE workflow_sessions SET pause_reason='complete',waiting='[]' WHERE session_id=?", (session_id,))
            self._event(db, 'project.concluded', value)
            return value

        return self._mutate('project.conclude', payload, request_id, work)
