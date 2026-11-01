"""Errors which callers may report without an internal traceback."""

import json


class ResearchError(Exception):
    """A rejected operation or unavailable research record."""


class ValidationError(ResearchError, ValueError):
    pass


class NotFoundError(ResearchError, LookupError):
    pass


class ConflictError(ResearchError):
    pass


class EvidenceDeliveryIncomplete(ConflictError):
    code = 'evidence_delivery_incomplete'

    def __init__(self, draft_id, covered_bytes, total_bytes):
        self.details = {'draft_id':draft_id, 'part':'decision', 'covered_prefix_bytes':covered_bytes,
                        'total_bytes':total_bytes, 'read':{'collection':'evidence','draft_id':draft_id}}
        super().__init__('Complete evidence must be delivered to the current model request; '
                         f'read every decision page of draft {draft_id} again '
                         f'(confirmed prefix {covered_bytes}/{total_bytes} bytes)')


class EvidenceBasisChanged(ConflictError):
    def __init__(self, context, differences):
        self.code = context + '_basis_changed'
        self.details = {**differences, 'read': {'collection': 'evidence'}}
        super().__init__(f'{self.code}: refresh evidence and pending results; changes={json.dumps(differences, ensure_ascii=False)}')


class ResearchControlError(ConflictError):
    """A control state prevents a new autonomous operation."""

    def __init__(self, run_state, pause_reason=None, control_revision=None, controller_phase=None):
        paused = run_state in {"running", "paused", "cold"}
        self.code = "research_control_paused" if paused else "research_control_stopped"
        recovery = "explicit_resume" if paused else "explicit_auto"
        self.details = {
            "run_state": run_state,
            "pause_reason": pause_reason,
            "controller_phase": controller_phase,
            "control_revision": control_revision,
            "retryable": False,
            "recovery": recovery,
            "allow_fallback": False,
        }
        instruction = "请由用户执行 /research resume 后再开始新工作" if paused else "请由用户明确启动自主研究后再开始新工作"
        super().__init__(f"研究当前不能开始新工作（状态 {run_state}，原因 {pause_reason or '无'}）；{instruction}。不要重复请求，也不要自行执行节点工作。")


class BudgetError(ConflictError):
    pass


class ResearchExecutionUnverified(ConflictError):
    code = 'research_control_recovery_pending'

    def __init__(self, run_state, control_revision, task_id, pause_reason=None, controller_phase=None):
        self.details = {'run_state': run_state, 'pause_reason': pause_reason,
                        'controller_phase': controller_phase, 'control_revision': control_revision,
                        'retryable': False, 'recovery': 'inspect_execution',
                        'task_id': task_id, 'allow_fallback': False}
        super().__init__(f'节点任务 {task_id} 的执行状态未核实；先由用户核对原执行，不能自动继续或重新派发。')


class CheckpointBaseRequired(ValidationError):
    code = "checkpoint_base_required"
    details = {"read": {"collection": "checkpoint"}}


class CheckpointBaseMismatch(ValidationError):
    code = "checkpoint_base_mismatch"
    details = {"read": {"collection": "checkpoint"}}


class CheckpointConflict(ConflictError):
    code = "checkpoint_conflict"

    def __init__(self, message, *, current_revision):
        super().__init__(message)
        self.details = {"current_revision": current_revision, "read": {"collection": "checkpoint"}}
