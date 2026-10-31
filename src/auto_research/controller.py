"""Persist the controller binding and fence mutations from older attachments."""
import errno
import json
import os
from contextvars import ContextVar

from .errors import ConflictError, ValidationError

CURRENT_CONTROLLER = ContextVar('research_controller', default=None)
CURRENT_MODEL_CALL = ContextVar('research_model_call', default=None)
INTENT_ID = 'controller-owner:v1'


class ControllerUnavailable(ConflictError):
    code = 'research_control_recovery_pending'

    def __init__(self, reason, run=None, phase='recovery_required'):
        self.details = {'reason': reason, 'run_state': run['state'] if run else None, 'pause_reason': None,
                        'control_revision': run['control_revision'] if run else None,
                        'controller_phase': phase, 'retryable': False,
                        'recovery': 'inspect_controller', 'allow_fallback': False}
        super().__init__(f'研究控制器需要核对（{reason}）；不能派发或自行执行节点工作。')


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return False if exc.errno == errno.ESRCH else None


def controller_row(db):
    row = db.execute("SELECT * FROM workflow_intents WHERE intent_id=? AND kind='controller_owner'", (INTENT_ID,)).fetchone()
    return (row, json.loads(row['details'])) if row else (None, None)


def require_controller(db, *, reconciling=False):
    row, details = controller_row(db)
    if not row:
        return  # Legacy databases stay readable/writable until this protocol is attached.
    run = db.execute('SELECT state,control_revision FROM workflow_project WHERE id=1').fetchone()
    token = CURRENT_CONTROLLER.get()
    if not isinstance(token, dict) or any(token.get(k) != details.get(k) for k in ('epoch', 'binding', 'fence')):
        raise ControllerUnavailable('controller_fence_mismatch', run, row['state'])
    if row['state'] != 'ready' and not (reconciling and row['state'] == 'reconciling'):
        raise ControllerUnavailable(row['state'], run, row['state'])


class ControllerStore:
    def controller_attach(self, host, session, epoch, binding, pid, operation):
        if not all(isinstance(v, str) and v for v in (epoch, binding)) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValidationError('Controller epoch, binding, and process ID are required')
        payload = dict(host=host, session=session, epoch=epoch, binding=binding, pid=pid)

        def work(db):
            self._association(db, host, session)
            actor = db.execute("SELECT role,detached FROM workflow_sessions WHERE host_id=? AND session_id=?", (host, session)).fetchone()
            if not actor or actor['role'] != 'main' or actor['detached']:
                raise ValidationError('Only the current main may attach a controller')
            run = db.execute('SELECT state,control_revision FROM workflow_project WHERE id=1').fetchone()
            old, previous = controller_row(db)
            if not old:
                # An upgraded running project has no recorded host identity.
                phase = 'recovery_required' if run['state'] == 'running' else 'ready'
                mode = 'legacy_unknown' if phase != 'ready' else 'initial'
                fence = 1
            elif previous['epoch'] == epoch and previous['pid'] == pid:
                phase = 'reconciling' if old['state'] == 'ready' else old['state']
                mode = previous.get('recovery_mode', 'reattach') if old['state'] == 'reconciling' else 'reattach'
                fence = int(previous['fence']) + 1
            else:
                alive = _alive(previous['pid'])
                if alive is not False:
                    raise ControllerUnavailable('other_controller_alive_or_unknown', run)
                phase, mode, fence = 'reconciling', 'cold', int(previous['fence']) + 1
            details = {**payload, 'fence': fence, 'recovery_mode': mode if phase == 'reconciling' else None}
            db.execute("INSERT INTO workflow_intents VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(intent_id) DO UPDATE SET state=excluded.state,details=excluded.details,updated_at=excluded.updated_at",
                       (INTENT_ID, 'controller_owner', session, phase, json.dumps(details, sort_keys=True)))
            db.execute('UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=1')
            self._event(db, 'runtime.controller_attached', {'phase': phase, 'mode': mode, 'fence': fence})
            return {'phase': phase, 'mode': mode, 'epoch': epoch, 'binding': binding, 'fence': fence}
        return self._mutate('runtime.controller_attach', payload, operation, work)

    def controller_ready(self, host, session, operation):
        payload = dict(host=host, session=session)

        def work(db):
            require_controller(db, reconciling=True)
            row, details = controller_row(db)
            if not row or row['state'] != 'reconciling':
                raise ConflictError('Controller is not reconciling')
            db.execute("UPDATE workflow_intents SET state='ready',updated_at=datetime('now') WHERE intent_id=?", (INTENT_ID,))
            db.execute('UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=1')
            self._event(db, 'runtime.controller_ready', {'fence': details['fence']})
            return {'phase': 'ready', 'fence': details['fence']}
        return self._mutate('runtime.controller_ready', payload, operation, work)

    def controller_check(self, host, session):
        with self._read() as db:
            self._association(db, host, session)
            require_controller(db)
            row, details = controller_row(db)
            return {'phase': row['state'] if row else 'unrecorded', 'fence': details['fence'] if details else None}

    def controller_adopt(self, host, session, reason, operation):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValidationError('A recovery reason is required')
        payload = dict(host=host, session=session, reason=reason.strip())

        def work(db):
            self._association(db, host, session)
            row, details = controller_row(db)
            token = CURRENT_CONTROLLER.get()
            if (not row or row['state'] != 'recovery_required' or row['session_id'] != session
                    or not isinstance(token, dict) or any(token.get(k) != details.get(k) for k in ('epoch', 'binding', 'fence'))):
                raise ControllerUnavailable('controller_adoption_not_authorized')
            db.execute("UPDATE workflow_project SET state='paused',updated_at=datetime('now') WHERE state='running'")
            db.execute("UPDATE project SET control='paused' WHERE control='auto'")
            db.execute("UPDATE workflow_sessions SET pause_reason='cold' WHERE session_id=? AND detached=0", (session,))
            db.execute("UPDATE exploration_tasks SET state='unverified',error='manual recovery: exit not verified',updated_at=datetime('now') WHERE state IN ('starting','running','waiting','stopping')")
            db.execute("UPDATE specialist_tasks SET state='unverified',exit_verified=0,error='manual recovery: exit not verified',updated_at=datetime('now') WHERE state IN ('starting','running')")
            db.execute("UPDATE workflow_intents SET state='ready',updated_at=datetime('now') WHERE intent_id=?", (INTENT_ID,))
            db.execute('UPDATE workflow_project SET control_revision=control_revision+1 WHERE id=1')
            self._event(db, 'runtime.controller_adopted', {'reason': reason.strip(), 'fence': details['fence'],
                                                           'unverified_executions_preserved': True})
            return {'phase': 'ready', 'run_state': 'paused', 'unverified_executions_preserved': True}
        return self._mutate('runtime.controller_adopt', payload, operation, work)
