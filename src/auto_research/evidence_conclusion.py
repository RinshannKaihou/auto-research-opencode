"""Prepare a version-bound conclusion without equating delivery with review."""

import hashlib
import json
import copy

from . import frozen_refs
from .controller import require_controller
from .errors import ConflictError, EvidenceBasisChanged, EvidenceDeliveryIncomplete, ValidationError
from .evidence_read import basis_changes, build_evidence_view
from .exploration_store import _main, contract_versions
from .runtime_budget import require_new_work
from .workflow_store import encoded, now


REVIEW_TYPES = {'citation_scope', 'semantic_inference', 'recompute', 'reimplementation', 'adversarial_test'}
STOP_REASONS = {'goal_met', 'sufficient_for_current_choice', 'low_expected_value', 'resource_limited', 'user_requested'}


def decision_package(view, result):
    """Select the facts needed for the current decision; retain the full view separately."""
    core = set(result['core_claim_refs'])
    reachable = set(core)
    while True:
        following = reachable | {edge['used_ref'] for edge in view['edges'] if edge['user_ref'] in reachable}
        if following == reachable:
            break
        reachable = following
    records = []
    for record in view['records']:
        if record['ref'] not in core:
            continue
        pinned, current = record['pinned'], record['current']
        records.append({'ref':record['ref'], 'statement':pinned['statement'], 'status':pinned['status'],
                        'scope':pinned['scope'], 'conditions':pinned['conditions'],
                        'evidence_refs':pinned['evidence_refs'], 'dependencies':pinned['dependencies'],
                        'current_ref':current['ref']})
    materials = []
    for item in view['materials']:
        selected = {key:item[key] for key in ('ref','outcome','reason','object','subpath','inline_digest',
                    'material_identity','material_read','read_error') if key in item}
        content = (item.get('item') or {}).get('content')
        if content and not item.get('object'):
            selected['inline_content'] = content
        materials.append(selected)
    routes = [{key:route.get(key) for key in ('ref','title','disposition','disposition_reason',
               'reactivation_condition','evidence_refs')} for route in view['routes']]
    pending = [{key:item.get(key) for key in ('result_id','kind','source_ref','handled_by_decision')}
               for item in view['pending_results']]
    return {'roots':view['roots'], 'scope':view['scope'], 'conclusion_scope':view['conclusion_scope'],
            'records':records, 'edges':[edge for edge in view['edges'] if edge['user_ref'] in reachable],
            'materials':materials, 'risks':view['risks'], 'limitations':view['limitations'],
            'reviews':result['review_coverage']['coverage'], 'routes':routes, 'pending_results':pending,
            'graph_complete':view['graph_complete'], 'raw_material_read':view['raw_material_read'],
            'conclusion_decision':result,
            'history':{'collection':'review_history', 'note':'Full structural view and historical review events remain in the draft.'}}


def render_appendix(draft_id, basis_digest, data):
    """Human-readable appendix from the same immutable draft as the report."""
    view, result = data['view'], data['evaluation']
    lines = ['# 证据附录', '', f'草案：{draft_id}', f'依据版本：{basis_digest}',
             '', '## 回答与证据状态', f"- 回答：{result['outcome']}",
             f"- 证据闭合：{result['evidence_closure']['status']}",
             f"- 审阅覆盖：{result['review_coverage']['status']}"]
    for gap in result['gaps']:
        lines.append(f'- 未解决：{gap}')
    lines.extend(['', '## 核心主张与支撑'])
    if not view['records']:
        lines.append('- 未登记结构化核心依据。')
    for record in view['records']:
        pinned, current = record['pinned'], record['current']
        lines.append(f"- {record['ref']}：{pinned['statement']}")
        if current['ref'] != record['ref']:
            lines.append(f"  - 当前版本：{current['ref']}；固定引用未自动替换。")
        if pinned.get('conditions'):
            conditions = pinned['conditions']
            lines.append('  - 条件：' + ('; '.join(f'{key}: {value}' for key, value in conditions.items())
                                      if isinstance(conditions, dict) else str(conditions)))
        for ref in pinned.get('evidence_refs', []):
            lines.append(f'  - 依据：{ref}')
    lines.extend(['', '## 材料与读取范围'])
    for material in view['materials']:
        included = ('原文已在当前决策包中' if (material.get('item') or {}).get('content') and not material.get('object')
                    else '原文曾完整送达主会话' if (material.get('material_read') or {}).get('complete') else '正文未完整送达')
        lines.append(f"- {material['ref']}：{material['outcome']}；{included}。")
    if not view['materials']:
        lines.append('- 未登记固定材料。')
    lines.extend(['', '## 当前风险与限制'])
    for risk in view['risks']:
        lines.append(f"- {risk['risk_id']}：{risk['kind']}，涉及 {risk['target']}。")
    for limit in view['limitations']:
        lines.append(f"- {limit['risk_id']}：{limit['reason']}，涉及 {limit['ref']}。")
    for judgment in result['evidence_closure']['risk_judgments']:
        lines.append(f"- {judgment['risk_id']}：{judgment['reason']}；{judgment['disposition']}。")
    if not view['risks'] and not view['limitations']:
        lines.append('- 当前声明范围内没有列出风险；这不证明结论为真。')
    lines.extend(['', '## 审阅'])
    for item in result['review_coverage']['coverage']:
        lines.append(f"- {item['target_ref']}：{item['review_type']}，{'有覆盖' if item['covered'] else '未覆盖'}。")
        for review in item['reviews']:
            identity = review['identity']
            model = identity.get('model')
            model_name = f"{model['provider']}/{model['model']}" if model else '未知模型'
            lines.append(f"  - 报告：{review['source_ref']}；模型：{model_name}；"
                         f"上下文：{identity.get('context_mode') or '未记录'}；结果：{review['result']}。")
            for limit in review['limitations']:
                lines.append(f'  - 限制：{limit}')
    if not result['review_coverage']['coverage']:
        lines.append('- 没有声明需要的审阅类型；不据此推导已审阅。')
    lines.extend(['', '## 探索与停止', f"- 停止理由：{result['exploration_stop']['reason']}"])
    for route in view['routes']:
        lines.append(f"- {route['title']}（{route['ref']}）：{route['disposition']}；{route.get('disposition_reason') or '未记录处置理由'}。")
    for item in result['exploration_stop'].get('route_judgments', []):
        lines.append(f"- {', '.join(item['route_refs'])}：{item['reason']}；恢复条件：{item['reactivation_condition']}。")
    for item in result['exploration_stop']['pending_results']:
        lines.append(f'- 未处理结果：{item}')
    lines.extend(['', '完整结构、固定引用与读取范围保存在同一结项草案中；交付材料不证明模型已理解或验证其内容。', ''])
    return '\n'.join(lines)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f'{name} must be nonempty text')
    return value.strip()


def review_coverage(view, requirements, core):
    if not isinstance(requirements, list):
        raise ValidationError('review_requirements must be a list')
    entries, unknown, seen_targets = [], False, set()
    for requirement in requirements:
        if not isinstance(requirement, dict) or not isinstance(requirement.get('target_ref'), str):
            raise ValidationError('Each review requirement needs a target_ref')
        if requirement['target_ref'] in seen_targets:
            raise ValidationError('Review requirements must name each target once')
        seen_targets.add(requirement['target_ref'])
        types = requirement.get('types', [])
        if not isinstance(types, list) or any(item not in REVIEW_TYPES for item in types):
            raise ValidationError('Review requirement contains an unknown type')
        if not types:
            _text(requirement.get('reason'), 'review requirement reason')
        for kind in types:
            matches = [item for item in view['reviews'] if item['target_ref'] == requirement['target_ref']
                       and item['review_type'] == kind and item['action'] == 'completed']
            eligible = []
            for item in matches:
                if not item['effective_in_generation']:
                    unknown = True
                    continue
                identity = item['identity']
                if not identity.get('model'):
                    unknown = True
                    continue
                if kind in {'recompute', 'reimplementation', 'adversarial_test'} and not (
                        identity.get('role') == 'node_core' and identity.get('attempt_id') and identity.get('observed_execution_tools')):
                    unknown = True
                    continue
                if requirement.get('independent') and not (
                        identity.get('context_mode') == 'blind' and identity.get('same_session_as_author') is False):
                    unknown |= identity.get('same_session_as_author') is None
                    continue
                if requirement.get('independent'):
                    reachable = {requirement['target_ref']}
                    while True:
                        expanded = reachable | {edge['used_ref'] for edge in view['edges'] if edge['user_ref'] in reachable}
                        if expanded == reachable:
                            break
                        reachable = expanded
                    if not reachable.intersection(identity.get('assigned_inputs') or []):
                        unknown = True
                        continue
                eligible.append(item)
            entries.append({'target_ref': requirement['target_ref'], 'review_type': kind,
                            'independent': bool(requirement.get('independent')),
                            'covered': bool(eligible), 'review_events': [item['event_id'] for item in eligible],
                            'reviews': [{'event_id': item['event_id'], 'source_ref': item['source_ref'],
                                         'result': item['scope_snapshot'].get('result'), 'identity': item['identity'],
                                         'limitations': item['limitations']} for item in eligible]})
    historical = any(item['action'] in {'completed', 'report_partial'} for item in view['review_history'])
    unassessed = sorted(set(core) - seen_targets) if requirements else []
    if entries and not unassessed and all(item['covered'] for item in entries):
        status = 'reviewed'
    elif any(item['covered'] for item in entries):
        status = 'partial'
    elif unknown or historical or unassessed:
        status = 'unknown'
    else:
        status = 'unreviewed'
    limits = sorted({limit for entry in entries for item in entry['reviews'] for limit in item['limitations']})
    if status == 'unknown':
        limits.append('Historical or incompletely attributed reviews do not establish typed coverage')
    return {'status': status, 'requirements': requirements, 'coverage': entries, 'unassessed_refs': unassessed,
            'limitations': limits}


def evaluate(store, db, view, fields):
    core = fields.get('core_claim_refs', [])
    if not isinstance(core, list) or any(not isinstance(ref, str) for ref in core) or len(core) != len(set(core)):
        raise ValidationError('core_claim_refs must be a list of distinct exact references')
    outcome = fields.get('outcome')
    if outcome not in {'answered', 'partial', 'unresolved'}:
        raise ValidationError('outcome must be answered, partial or unresolved')
    if outcome == 'answered' and any(item['reason'] == 'evidence_traversal_limit' for item in view['limitations']):
        raise ConflictError('Evidence traversal was truncated; choose partial/unresolved or narrow the scope')
    gaps = fields.get('gaps', [])
    if not isinstance(gaps, list) or any(not isinstance(gap, str) or not gap.strip() for gap in gaps):
        raise ValidationError('gaps must be a list of text')
    if outcome != 'answered' and not gaps:
        raise ValidationError('partial/unresolved outcomes require gaps')
    records = {item['ref']: item['pinned'] for item in view['records']}
    missing_core = [ref for ref in core if ref not in records or records[ref]['kind'] != 'claim']
    materials = {item['ref']: item for item in view['materials']}
    def has_fixed_support(ref, seen):
        if ref in seen:
            return False
        seen = seen | {ref}
        material = materials.get(ref)
        if material:
            if material['outcome'] != 'resolved':
                return False
            if (material.get('material_read') or {}).get('complete'):
                return True
            if not material.get('object') and (material.get('item') or {}).get('content'):
                return True
        return any(has_fixed_support(edge['used_ref'], seen) for edge in view['edges'] if edge['user_ref'] == ref)
    unsupported_core = [ref for ref in core if ref in records and (
        records[ref]['status'] == 'retracted' or not has_fixed_support(ref, set()))]
    core_known = bool(core) and not missing_core and not unsupported_core

    risks = [item for item in view['risks'] if item['kind'] != 'version_notice' and not (
        item['kind'] == 'impact' and item.get('disposition') and item['disposition']['kind'] != 'unresolved')]
    risks.extend(view['limitations'])
    by_id = {item['risk_id']: item for item in risks}
    requested_judgments = fields.get('risk_judgments', [])
    if not isinstance(requested_judgments, list):
        raise ValidationError('risk_judgments must be a list')
    judgments = []
    for item in requested_judgments:
        if isinstance(item, dict) and 'risk_ids' in item:
            ids = item['risk_ids']
            if 'risk_id' in item or not isinstance(ids, list) or not ids:
                raise ValidationError('risk_ids must be a nonempty list without risk_id')
            judgments.extend({**{key: value for key, value in item.items() if key != 'risk_ids'}, 'risk_id': ident} for ident in ids)
        else:
            judgments.append(item)
    seen, unresolved = set(), []
    for item in judgments:
        if not isinstance(item, dict) or item.get('risk_id') not in by_id or item['risk_id'] in seen:
            raise ValidationError('Risk judgments must refer to distinct risks in the evidence snapshot')
        seen.add(item['risk_id'])
        if item.get('core_impact') not in {'yes', 'no', 'unknown'} or item.get('disposition') not in {'handled', 'unresolved'}:
            raise ValidationError('Risk judgment requires core_impact and disposition')
        _text(item.get('reason'), 'risk judgment reason')
        refs = item.get('evidence_refs', [])
        if not isinstance(refs, list):
            raise ValidationError('Risk judgment evidence_refs must be a list')
        for ref in refs:
            store._require_ref(db, ref)
        if item['disposition'] == 'handled' and (not refs or by_id[item['risk_id']].get('kind') == 'review_challenge'):
            raise ValidationError('Handling a risk requires evidence; challenges need their exact disposition')
        if item['core_impact'] == 'no' and not refs:
            raise ValidationError('A non-core risk judgment requires evidence_refs')
        if item['disposition'] == 'unresolved':
            _text(item.get('gap'), 'unresolved risk gap')
            unresolved.append(item)
    unclassified = [item['risk_id'] for item in risks if item['risk_id'] not in seen]
    blocking = [item for item in unresolved if item['core_impact'] != 'no']
    noncore = [item for item in unresolved if item['core_impact'] == 'no']
    if not core_known:
        closure = 'unknown'
    elif unclassified or blocking:
        closure = 'unresolved'
    elif noncore:
        closure = 'qualified'
    else:
        closure = 'resolved'
    if outcome == 'answered' and closure not in {'resolved', 'qualified'}:
        raise ConflictError('Core evidence or its risks are unresolved; choose partial/unresolved or update the evidence')

    stop = fields.get('exploration_stop')
    if not isinstance(stop, dict) or stop.get('reason_kind') not in STOP_REASONS:
        raise ValidationError('exploration_stop requires a recognized reason_kind')
    _text(stop.get('reason'), 'exploration_stop.reason')
    pending = [item['result_id'] for item in view['pending_results']]
    accounted = stop.get('unprocessed_results', [])
    if pending and (not isinstance(accounted, list) or set(pending) - set(accounted)):
        raise ConflictError('Current pending results must be considered or explicitly recorded as unprocessed')
    if pending and outcome == 'answered':
        raise ConflictError('Pending results must be considered before an answered conclusion')
    route_judgments = stop.get('route_judgments', [])
    if not isinstance(route_judgments, list):
        raise ValidationError('route_judgments must be a list')
    current_routes = {route['ref']: route for route in view['routes']}
    accounted_routes = set()
    for judgment in route_judgments:
        if not isinstance(judgment, dict) or not isinstance(judgment.get('route_refs'), list):
            raise ValidationError('A route judgment needs route_refs')
        _text(judgment.get('reason'), 'route judgment reason')
        _text(judgment.get('reactivation_condition'), 'route reactivation condition')
        for ref in judgment['route_refs']:
            if ref not in current_routes or ref in accounted_routes:
                raise ValidationError('Route judgments must identify distinct current routes')
            accounted_routes.add(ref)
    unaccounted_routes = [ref for ref, route in current_routes.items()
                          if route['disposition'] in {'untried', 'investigating'} and ref not in accounted_routes]
    if unaccounted_routes and outcome == 'answered':
        raise ConflictError('Current routes need a stopping judgment before an answered conclusion')
    requirements = fields.get('review_requirements', [])
    if any(item.get('target_ref') not in core for item in requirements if isinstance(item, dict)):
        raise ValidationError('Review requirements must target an exact core claim')
    coverage = review_coverage(view, requirements, core)
    return {'outcome': outcome, 'gaps': gaps, 'core_claim_refs': core,
            'evidence_closure': {'status': closure, 'missing_core': missing_core, 'unsupported_core': unsupported_core,
                                 'unclassified_risks': unclassified, 'risk_judgments': judgments},
            'review_coverage': coverage, 'exploration_stop': {**stop, 'pending_results': pending,
                                                               'unaccounted_routes': unaccounted_routes,
                                                               'considered_route_refs': list(current_routes)}}


class EvidenceConclusionStore:
    def evidence_observed(self, host, session, instance_id, pages, operation):
        if not isinstance(instance_id, str) or not instance_id:
            raise ValidationError('A plugin instance identity is required')
        payload = {'host': host, 'session': session, 'instance_id': instance_id, 'pages': pages}

        def work(db):
            self._association(db, host, session)
            generation = db.execute('SELECT generation FROM workflow_project WHERE id=1').fetchone()[0]
            receipts = []
            for page in pages:
                if page['generation'] != generation:
                    raise ConflictError('Evidence was delivered in another generation')
                part = page.get('part', 'manifest')
                if part == 'material':
                    _main(db, host, session)
                identity = [generation, host, session, instance_id, page['snapshot_id'], page['draft_id'],
                            part, page['offset'], page['end'], page['digest']]
                receipt = 'evr-' + hashlib.sha256(encoded(identity).encode()).hexdigest()
                columns = ('receipt_id,generation,host_id,session_id,plugin_instance_id,snapshot_id,draft_id,part,start_offset,end_offset,digest,created_at')
                values = (receipt, generation, host, session, instance_id, page['snapshot_id'], page['draft_id'],
                          part, page['offset'], page['end'], page['digest'], now())
                if db.execute('PRAGMA user_version').fetchone()[0] >= 15:
                    columns += ',model_message_id'
                    values += (page.get('model_message_id'),)
                db.execute(f"INSERT OR IGNORE INTO evidence_read_receipts ({columns}) VALUES({','.join('?' for _ in values)})", values)
                receipts.append(receipt)
            return {'receipt_ids': receipts}
        return self._mutate('evidence.observed', payload, operation, work)

    def conclusion_contract(self, host, session, contract, operation):
        if contract is None:
            with self._read() as db:
                run = _main(db, host, session)
                return {'generation': run['generation'], 'conclusion_contract': self._project(db)['config'].get('conclusion_contract', 'legacy-v1')}
        if contract not in {'legacy-v1', 'evidence-v1'}:
            raise ValidationError('Unknown conclusion contract')
        payload = {'host': host, 'session': session, 'contract': contract}

        def work(db):
            require_controller(db)
            run = _main(db, host, session)
            if run['state'] == 'complete':
                raise ConflictError('Start a new generation before changing the conclusion contract')
            config = self._project(db)['config']
            if contract == 'legacy-v1' and config.get('conclusion_contract', 'legacy-v1') == 'evidence-v1':
                raise ConflictError('The conclusion contract cannot be downgraded in the current generation')
            if config.get('conclusion_contract', 'legacy-v1') != contract:
                if db.execute('PRAGMA user_version').fetchone()[0] >= 15:
                    from .conclusion_policy import set_contract
                    set_contract(db, run['generation'], contract, 'explicit')
                else:
                    config['conclusion_contract'] = contract
                    self._set_project_config(db, config)
                self._event(db, 'conclusion.contract_adopted', {'generation': run['generation'], 'contract': contract})
            return {'generation': run['generation'], 'conclusion_contract': contract}
        return self._mutate('conclusion.contract', payload, operation, work)

    def prepare_conclusion(self, host, session, fields, snapshot, operation):
        payload = {'host': host, 'session': session, 'fields': fields}
        if snapshot:
            snapshot = {**snapshot, 'view': copy.deepcopy(snapshot['view'])}

        def work(db):
            nonlocal snapshot
            require_controller(db)
            run = _main(db, host, session)
            require_new_work(db, host, session, manual=True, waiting=True, role='main')
            if self._project(db)['config'].get('conclusion_contract', 'legacy-v1') != 'evidence-v1':
                raise ConflictError('Explicitly adopt evidence-v1 before preparing a conclusion')
            if not snapshot or snapshot['generation'] != run['generation']:
                raise ConflictError('evidence_snapshot_expired: read the evidence again')
            if snapshot.get('host_id') != host or snapshot.get('session_id') != session:
                raise ConflictError('evidence_snapshot_session_changed: read the evidence again in the current main session')
            current = build_evidence_view(self, db, snapshot['view']['roots'])
            if current['basis_digest'] != snapshot['basis_digest']:
                raise EvidenceBasisChanged('conclusion', basis_changes(snapshot['view'], current['view']))
            result = evaluate(self, db, snapshot['view'], fields)
            project = self._project(db)
            config = project['config']
            before = config.get('conclusion_review_scope')
            if before and before['generation'] != run['generation']:
                before = None
            after = {'generation': run['generation'], 'refs': sorted(result['core_claim_refs'])}
            if before != after:
                removed = sorted(set((before or {}).get('refs', [])) - set(after['refs']))
                scope_change = fields.get('scope_change') or {}
                if removed:
                    _text(scope_change.get('reason'), 'scope_change.reason')
                    _text(scope_change.get('excluded_impact'), 'scope_change.excluded_impact')
                config['conclusion_review_scope'] = after
                self._set_project_config(db, config)
                self._event(db, 'conclusion.scope_changed', {'before': before, 'after': after,
                    'removed': removed, 'reason': scope_change.get('reason'),
                    'excluded_impact': scope_change.get('excluded_impact'), 'actor': {'host_id': host, 'session_id': session}})
                from .review_lifecycle import reconcile
                reconcile(self, db, operation)
                refreshed = build_evidence_view(self, db, snapshot['view']['roots'])
                view = refreshed['view']
                old_materials = {item['ref']: item for item in snapshot['view']['materials']}
                for item in view['materials']:
                    old = old_materials.get(item['ref'], {})
                    for key in ('material_identity', 'material_read', 'read_error'):
                        if key in old:
                            item[key] = old[key]
                    if (old.get('material_read') or {}).get('complete'):
                        view['limitations'] = [limit for limit in view['limitations'] if not (
                            limit['ref'] == item['ref'] and limit['reason'] == 'material_content_not_delivered')]
                snapshot = {**snapshot, 'view': view, 'basis_digest': refreshed['basis_digest'],
                            'source_sequence': refreshed['source_sequence']}
                result = evaluate(self, db, view, fields)
            package = decision_package(snapshot['view'], result)
            body = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
            if len(body) > 32000:
                counts = {key:len(package[key]) for key in ('records','edges','materials','risks','limitations')}
                raise ValidationError(f'decision_package_too_large: {len(body)} bytes (limit 32000); '
                                      f'counts={encoded(counts)}; narrow the declared scope explicitly')
            snapshot['manifest_digest'] = hashlib.sha256(body).hexdigest()
            draft_id = self._next(db, 'conclusion_draft', 'CD')
            data = {'format_version': 2, 'view': snapshot['view'], 'decision_package':package,
                    'evaluation': result, 'fields': fields, 'snapshot_id': snapshot['snapshot_id'],
                    'versions': contract_versions(self, db, host, session),
                    'material_content_in_snapshot': snapshot['view'].get('material_content_in_snapshot', [])}
            db.execute('INSERT INTO conclusion_drafts VALUES(?,?,?,?,?,?,?,?,?)',
                       (draft_id, run['generation'], host, session, snapshot['basis_digest'], snapshot['manifest_digest'],
                        snapshot['source_sequence'], encoded(data), now()))
            self._event(db, 'conclusion.prepared', {'draft_id': draft_id, 'basis_digest': snapshot['basis_digest']})
            return {'draft_id': draft_id, 'generation': run['generation'], 'basis_digest': snapshot['basis_digest'],
                    'manifest_digest': snapshot['manifest_digest'], **result}
        return self._mutate('conclusion.prepare', payload, operation, work)

    def evidence_conclusion_in_tx(self, db, host, session, fields, receipt_ids, instance_id):
        if set(fields) - {'draft_id', 'final_ref', 'summary'}:
            raise ValidationError('Evidence conclusion accepts only draft_id, final_ref and summary')
        draft_id = fields.get('draft_id')
        run = _main(db, host, session)
        draft = db.execute('SELECT * FROM conclusion_drafts WHERE draft_id=? AND generation=?',
                           (draft_id, run['generation'])).fetchone()
        if not draft:
            raise ValidationError('A conclusion draft for the current generation is required')
        data = json.loads(draft['data'])
        if data.get('format_version') != 2 or 'decision_package' not in data:
            raise ConflictError('conclusion_draft_format_changed: prepare the conclusion again')
        current = build_evidence_view(self, db, data['view']['roots'])
        if current['basis_digest'] != draft['basis_digest']:
            raise EvidenceBasisChanged('conclusion', basis_changes(data['view'], current['view']))
        final_ref = _text(fields.get('final_ref'), 'final_ref')
        parsed = frozen_refs.parse(final_ref)
        if parsed.get('form') not in {'publication', 'publication_item'} or parsed.get('subpath'):
            raise ValidationError('final_ref must identify the final publication or report item')
        publication = db.execute('SELECT * FROM conclusion_publications WHERE publication_id=? AND draft_id=?',
                                 (parsed['publication_id'], draft_id)).fetchone()
        if not publication or (parsed.get('item_id') and parsed['item_id'] != publication['report_item_id']):
            raise ValidationError('The final report and appendix must be published from this draft')
        for key, expected in (('report_item_id', publication['report_digest']), ('appendix_item_id', publication['appendix_digest'])):
            item = db.execute('SELECT content,object_version FROM publication_items WHERE publication_id=? AND item_id=?',
                              (publication['publication_id'], publication[key])).fetchone()
            digest = item['object_version'] or hashlib.sha256(encoded(json.loads(item['content'])).encode()).hexdigest() if item else None
            if digest != expected:
                raise ConflictError('Final report or evidence appendix changed')
        if publication['basis_digest'] != draft['basis_digest']:
            raise ConflictError('Final report does not match the conclusion evidence')
        body = json.dumps(data['decision_package'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        total_bytes = len(body.encode('utf-8'))
        if not isinstance(instance_id, str) or not instance_id or not isinstance(receipt_ids, list) or not receipt_ids:
            raise EvidenceDeliveryIncomplete(draft_id, 0, total_bytes)
        ranges = []
        for receipt_id in set(receipt_ids):
            receipt = db.execute('SELECT * FROM evidence_read_receipts WHERE receipt_id=? AND draft_id=? '
                                 "AND generation=? AND host_id=? AND session_id=? AND plugin_instance_id=? AND digest=? AND part='decision'",
                                 (receipt_id, draft_id, run['generation'], host, session, instance_id, draft['manifest_digest'])).fetchone()
            if receipt:
                ranges.append((receipt['start_offset'], receipt['end_offset']))
        end = 0
        for start, stop in sorted(ranges):
            if start > end:
                break
            end = max(end, stop)
        if end < total_bytes:
            raise EvidenceDeliveryIncomplete(draft_id, end, total_bytes)
        result = evaluate(self, db, data['view'], data['fields'])

        reachable = set(result['core_claim_refs'])
        changed = True
        while changed:
            before = len(reachable)
            reachable.update(edge['used_ref'] for edge in data['view']['edges'] if edge['user_ref'] in reachable)
            changed = len(reachable) != before
        report = db.execute('SELECT object_version,content FROM publication_items WHERE publication_id=? AND item_id=?',
                            (publication['publication_id'], publication['report_item_id'])).fetchone()
        inline_digest = (hashlib.sha256(encoded(json.loads(report['content'])).encode('utf-8')).hexdigest()
                         if not report['object_version'] else None)
        self_source = []
        for material in data['view']['materials']:
            if material['ref'] not in reachable:
                continue
            source = frozen_refs.parse(material['ref'])
            same_publication = source.get('publication_id') == publication['publication_id']
            same_object = report['object_version'] and (material.get('object') or {}).get('version') == report['object_version']
            same_file = report['object_version'] and (material.get('material_identity') or {}).get('digest') == report['object_version']
            same_inline = not report['object_version'] and not material.get('object') and (
                (material.get('material_identity') or {}).get('digest') == inline_digest or
                material.get('item') and material['item']['content'] == json.loads(report['content']))
            if same_publication or same_object or same_file or same_inline:
                self_source.append(material['ref'])
        if self_source:
            if result['outcome'] == 'answered':
                raise ConflictError('A final report cannot be treated as evidence for its own core claims')
            result['evidence_closure']['status'] = 'unknown'
            result['evidence_closure']['self_references'] = self_source
        coverage = result['review_coverage']
        legacy_status = coverage['status'] if coverage['status'] != 'unknown' else 'unreviewed'
        covered = {event_id for item in coverage['coverage'] for event_id in item['review_events']}
        review = {'status': legacy_status,
                  'refs': sorted({item['source_ref'] for item in data['view']['reviews'] if item['event_id'] in covered}),
                  'limitations': coverage['limitations']}
        return {'summary': _text(fields.get('summary'), 'summary'), 'final_ref': final_ref,
                'outcome': result['outcome'], 'gaps': result['gaps'], 'review': review,
                'evidence': {**result, 'draft_id': draft_id, 'basis_digest': draft['basis_digest'],
                             'versions':data['versions'],
                             'manifest_digest': draft['manifest_digest'], 'source_sequence': draft['source_sequence'],
                             'report_ref': f"pub/{publication['publication_id']}#{publication['report_item_id']}",
                             'appendix_ref': f"pub/{publication['publication_id']}#{publication['appendix_item_id']}"}}
