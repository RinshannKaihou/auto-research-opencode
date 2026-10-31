"""Explicit revision conveniences. No semantic classification or inferred execution."""
from .errors import ValidationError


def expand(payload, current):
    preset = payload.get('preset')
    if not isinstance(preset, str) or preset not in {'editorial', 'correct_previous', 'custom'}:
        raise ValidationError('preset must be editorial, correct_previous or custom')
    if preset == 'custom':
        if set(payload['preset_scope_input']) != {'change_kind','affected_scope_mode','affected_scope'}:
            raise ValidationError('custom requires explicit change_kind, affected_scope_mode and affected_scope')
        return {key: payload[key] for key in ('change_kind', 'affected_scope_mode', 'affected_scope')}
    if preset == 'editorial':
        if set(payload['changes']) - {'statement', 'author'} or payload['relations']:
            raise ValidationError('preset_conflict: editorial only changes wording or author')
        expanded = dict(change_kind='reword', affected_scope_mode='none', affected_scope=[])
    else:
        expanded = dict(change_kind='correct', affected_scope_mode='versions', affected_scope=[current['ref']])
    for key, value in payload['preset_scope_input'].items():
        if value != expanded[key]:
            raise ValidationError(f'preset_conflict: {key}')
    return expanded


def sources(payload, current, value):
    preserve = payload.get('preserve_sources', False)
    no_new = payload.get('no_new_evidence', False)
    if type(preserve) is not bool or type(no_new) is not bool:
        raise ValidationError('preserve_sources and no_new_evidence must be booleans')
    if preserve != no_new:
        raise ValidationError('source_preservation_conflict: preserve_sources requires no_new_evidence')
    if not preserve:
        return False
    if 'execution_refs' in payload:
        raise ValidationError('source_preservation_conflict: explicit execution_refs supplied')
    for key in ('evidence_refs', 'dependencies', 'motivated_by', 'checks'):
        if value[key] != current[key]:
            raise ValidationError(f'source_preservation_conflict: {key} changed')
    return True
