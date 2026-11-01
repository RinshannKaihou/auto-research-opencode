"""Atomic execution closure with an explicit, self-declared research outcome.

The receipt is not a correctness certificate. Scientific review scheduling and
claim coverage remain separate from this minimal delivery contract.
"""
import json

from . import frozen_refs
from .errors import ConflictError, EvidenceBasisChanged, ValidationError
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
    def _decode_conclusion(db, row):
        value = {**dict(row), 'gaps': json.loads(row['gaps']), 'review': json.loads(row['review'])}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='conclusion_evidence'").fetchone():
            evidence = db.execute('SELECT data FROM conclusion_evidence WHERE conclusion_id=?', (row['conclusion_id'],)).fetchone()
            if evidence:
                value['evidence'] = json.loads(evidence['data'])
        return value

    @staticmethod
    def conclusion_history(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_conclusions'").fetchone():
            return []
        return [ConclusionStore._decode_conclusion(db, row)
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
        return ConclusionStore._decode_conclusion(db, row)

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

    def conclude(self, host_id, session_id, fields, request_id, *, enforce_control=False,
                 delivery_receipts=None, plugin_instance_id=None):
        # Preserve the caller payload for idempotent retries; validate inside work.
        payload = {'host_id': host_id, 'session_id': session_id, 'fields': fields}
        if delivery_receipts is not None:
            payload.update(delivery_receipts=delivery_receipts, plugin_instance_id=plugin_instance_id)
        # Check frozen bytes outside the write transaction. Successful old
        # requests replay before needing the original file to remain present.
        if isinstance(fields, dict) and fields.get('draft_id'):
            with self._read() as db:
                previous = db.execute('SELECT 1 FROM requests WHERE request_id=?', (request_id,)).fetchone()
                if not previous:
                    self._conclusion_reference(db, text(fields.get('final_ref'), 'final_ref'), final=True)
                    draft = db.execute('SELECT data FROM conclusion_drafts WHERE draft_id=?', (fields['draft_id'],)).fetchone()
                    final = frozen_refs.parse(fields['final_ref'])
                    appendix = db.execute('SELECT appendix_item_id FROM conclusion_publications WHERE publication_id=? AND draft_id=?',
                                          (final.get('publication_id'), fields['draft_id'])).fetchone()
                    if appendix:
                        self._conclusion_reference(db, f"pub/{final['publication_id']}#{appendix['appendix_item_id']}", final=True)
                    if draft:
                        for material in json.loads(draft['data'])['view']['materials']:
                            if material.get('object') and material['outcome'] == 'resolved':
                                checked, _ = frozen_refs.open(db, self.root, material['ref'])
                                if checked['outcome'] != 'resolved':
                                    raise EvidenceBasisChanged('conclusion', {'changed_materials':[material['ref']],
                                                                              'reason':checked['reason']})

        def work(db):
            association = self._association(db, host_id, session_id)
            if enforce_control:
                from .runtime_budget import require_new_work
                require_new_work(db, host_id, session_id, manual=True, waiting=True, role='main')
            run = db.execute('SELECT * FROM workflow_project WHERE id=1').fetchone()
            role = db.execute('SELECT * FROM workflow_sessions WHERE session_id=? AND host_id=? AND detached=0',
                              (session_id, host_id)).fetchone()
            if not run or not role or role['role'] != 'main' or run['main_session_id'] != session_id:
                raise ValidationError('Only the current project main session may conclude')
            evidence_contract = self._project(db)['config'].get('conclusion_contract', 'legacy-v1') == 'evidence-v1'
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
            evidence = None
            if evidence_contract:
                if db.execute("SELECT 1 FROM workflow_intents WHERE kind='exploration_work' AND state IN ('pending','waiting_budget') "
                              "AND json_extract(details,'$.generation')=? LIMIT 1", (run['generation'],)).fetchone():
                    raise ConflictError('Pending exploration work must be explicitly superseded before conclusion')
                checked = self.evidence_conclusion_in_tx(db, host_id, session_id, fields, delivery_receipts, plugin_instance_id)
                summary, final_ref, outcome, gaps, review = (checked[key] for key in ('summary','final_ref','outcome','gaps','review'))
                evidence = checked['evidence']
            else:
                summary = text(fields.get('summary'), 'summary')
                final_ref = text(fields.get('final_ref'), 'final_ref')
                outcome = fields.get('outcome')
                if outcome is None:
                    raise ValidationError('outcome is required')
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
            if evidence:
                db.execute('INSERT INTO conclusion_evidence VALUES(?,?,?,?)',
                           (value['conclusion_id'], evidence['draft_id'], evidence['basis_digest'],
                            json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
                value['evidence'] = evidence
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
