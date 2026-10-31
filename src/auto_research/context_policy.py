"""R1 current-state read model. Callers own the SQLite read transaction."""
from __future__ import annotations

from collections import Counter
import hashlib
import json

from . import epistemic, frozen_refs, hints
from .errors import ValidationError, NotFoundError

POLICIES = ("memory-v2", "memory-r1-v1")
COLLECTIONS = ("critical_now", "findings", "completed_findings")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_model(store, db, *, node_id=None, decision_refs=None, final_ref=None):
    if decision_refs is not None and (not isinstance(decision_refs, list) or
            any(not isinstance(ref, str) or not ref.strip() for ref in decision_refs)):
        raise ValidationError("decision_refs must be a list of nonempty references")
    if final_ref is not None and not frozen_refs.parse(final_ref).get('publication_id'):
        raise ValidationError("final_ref must identify a publication or publication item")
    bound = epistemic.read_bound(db)
    records = [store._decode_knowledge(row, db) for row in db.execute(
        "SELECT e.kind,e.node_id,r.* FROM knowledge_entries e "
        "JOIN knowledge_revisions r USING(knowledge_id) ORDER BY r.rowid DESC")]
    by_ref = {row['ref']: row for row in records}
    latest = {}
    for row in records:
        latest.setdefault(row['knowledge_id'], row)
    scope = {}
    pub_scopes = {}

    def add(ref, reason):
        visited = ref in scope
        scope.setdefault(ref, set()).add(reason)
        if visited:
            return
        if ref.startswith('knowledge/'):
            if ref not in by_ref:
                raise NotFoundError(f'Unknown research reference: {ref}')
            return
        input_node = db.execute('SELECT node_id,question_ref FROM nodes WHERE node_id=?', (ref,)).fetchone()
        if input_node:
            if input_node['question_ref']:
                add(input_node['question_ref'], 'declared_node_input')
            for entry in latest.values():
                if entry['node_id'] == ref:
                    add(entry['ref'], 'declared_node_input')
            return
        parsed = frozen_refs.parse(ref)
        if frozen_refs.is_frozen(ref):
            resolution = frozen_refs.resolve(db, store.root, ref)
            if resolution['outcome'] == 'not_found':
                raise NotFoundError(resolution['message'])
            if resolution['outcome'] in ('unsupported', 'ambiguous'):
                raise ValidationError(resolution['message'])
        pid = parsed.get('publication_id')
        if pid:
            if not db.execute('SELECT 1 FROM publications WHERE publication_id=?', (pid,)).fetchone():
                raise NotFoundError(f'Unknown research reference: {ref}')
            if parsed.get('item_id'):
                item = db.execute('SELECT knowledge_refs FROM publication_items WHERE publication_id=? AND item_id=?',
                                  (pid, parsed['item_id'])).fetchone()
                if item is None:
                    raise NotFoundError(f'Unknown research reference: {ref}')
                pinned = json.loads(item['knowledge_refs'])
            else:
                pinned = [f'knowledge/{r[0]}@{r[1]}' for r in db.execute(
                    'SELECT knowledge_id,revision FROM publication_knowledge WHERE publication_id=?', (pid,))]
            pub_scopes[ref] = (pid, pinned)
            for fixed in pinned:
                add(fixed, reason)
            return
        if parsed.get('snapshot_id'):
            return
        store._require_ref(db, ref)

    for ref in decision_refs or []:
        add(ref, 'decision_input')
    if final_ref is None:
        conclusion = store.conclusion_view(db)
        final_ref = conclusion['final_ref'] if conclusion else None
    if final_ref:
        add(final_ref, 'final')
    if node_id:
        node = db.execute('SELECT inputs,question_ref FROM nodes WHERE node_id=?', (node_id,)).fetchone()
        if node is None:
            raise NotFoundError(f'Unknown node: {node_id}')
        for ref in json.loads(node['inputs']) + ([node['question_ref']] if node['question_ref'] else []):
            add(ref, 'node_input')
        for edge in db.execute('SELECT input_refs FROM node_dependencies WHERE successor_node_id=?', (node_id,)):
            for ref in json.loads(edge['input_refs']):
                add(ref, 'dependency')
        for row in latest.values():
            if row['node_id'] == node_id:
                add(row['ref'], 'node_knowledge')
    # Only the materialised typed support graph propagates scientific dependence.
    queue = list(scope)
    while queue:
        ref = queue.pop()
        for edge in db.execute('SELECT used_ref FROM knowledge_support_edges WHERE user_ref=?', (ref,)):
            if edge[0] not in scope:
                add(edge[0], 'support_ancestor')
                queue.append(edge[0])

    finished = list(db.execute("SELECT node_id,MAX(ended_at) completed_at FROM attempts "
                               "WHERE state='finished' AND node_id IS NOT NULL GROUP BY node_id "
                               "ORDER BY completed_at DESC,node_id"))
    ordinary = {}
    for row in latest.values():
        if row['kind'] in ('claim', 'observation'):
            ordinary.setdefault(row['node_id'], []).append(row)
    completed = [{'node_id': row['node_id'], 'finding_count': len(ordinary.get(row['node_id'], [])),
                  'status': 'registered' if ordinary.get(row['node_id']) else '未登记发现',
                  'selected_refs': [v['ref'] for v in ordinary.get(row['node_id'], [])[:2]],
                  'expand': {'collection': 'findings', 'node_id': row['node_id']}}
                 for row in finished]
    selected = {}

    def select(row, reason):
        if row['kind'] == 'open_question':
            return
        value = selected.setdefault(row['ref'], {**row, 'selection_reasons': [],
                                               'scope_origins': sorted(scope.get(row['ref'], []))})
        if reason not in value['selection_reasons']:
            value['selection_reasons'].append(reason)

    for ref in scope:
        if ref in by_ref:
            select(by_ref[ref], 'explicit_input')
            current = latest[by_ref[ref]['knowledge_id']]
            select(current, 'current_version')
    # Round-robin by completed node, so one prolific node cannot occupy the first page.
    for slot in range(max((len(ordinary.get(node["node_id"], [])) for node in finished), default=0)):
        for node in finished:
            rows = ordinary.get(node['node_id'], [])
            if slot < len(rows):
                select(rows[slot], 'completed_node')
    for row in db.execute('SELECT knowledge_id,revision FROM publication_knowledge ORDER BY rowid DESC'):
        select(by_ref[f'knowledge/{row[0]}@{row[1]}'], 'publication_pinned')
        select(latest[row[0]], 'current_version')
    question = db.execute('SELECT question FROM nodes WHERE node_id=?', (node_id,)).fetchone() if node_id else None
    for row in store._knowledge_query(db, query=question[0] if question else store._project(db)['goal'], limit=24)['items']:
        select(row, 'question_terms')
    for row in latest.values():
        if row['status'] == 'disputed' or row['revision'] > 1 or row['kind'] == 'lesson' or row['node_id'] is None:
            select(row, 'disputed' if row['status'] == 'disputed' else 'revised' if row['revision'] > 1 else 'background')
    if node_id:
        for row in ordinary.get(node_id, []):
            select(row, 'node_knowledge')

    risks = []

    def risk(kind, target, details, expand):
        risks.append({'kind': kind, 'target': target, 'risk': details,
                      'relevance': 'related' if target in scope else 'unknown',
                      'relevance_reason': sorted(scope[target]) if target in scope else ['decision_scope_not_declared_for_target'],
                      'expand': expand})

    for impact in epistemic.valid_impacts(db, bound):
        disposition = epistemic.current_disposition(db, impact['change_id'], impact['affected_version'], bound)
        if disposition is None or disposition['kind'] == 'unresolved':
            risk('impact', impact['affected_version'], {
                'impact_id': impact['impact_id'], 'change_id': impact['change_id'],
                'scope_unconfirmed': epistemic.effective_scope(db, impact['change_id'], bound)['mode'] == 'unknown',
                'explanation': epistemic.explain_path(db, impact['affected_version'],
                    epistemic.change_roots(db, impact['change_id'], bound)),
            }, {'ref': impact['impact_id']})
    for edge in db.execute("SELECT e.user_ref,e.used_ref FROM knowledge_support_edges e "
                           "JOIN knowledge_revisions r ON ('knowledge/'||r.knowledge_id||'@'||r.revision)=e.used_ref "
                           "JOIN knowledge_revisions u ON ('knowledge/'||u.knowledge_id||'@'||u.revision)=e.user_ref "
                           "WHERE r.status='retracted' AND u.status<>'retracted' ORDER BY e.user_ref,e.used_ref"):
        risk('retracted_basis', edge[0], {'retracted_ref': edge[1]}, {'ref': edge[0]})
    # Field failures on obsolete, unreferenced revisions remain in history.
    # Explicit inputs, pins and their support ancestors remain current concerns.
    check_refs = epistemic.anchor_set(db) | set(scope)
    queue = list(check_refs)
    while queue:
        for edge in db.execute('SELECT used_ref FROM knowledge_support_edges WHERE user_ref=?', (queue.pop(),)):
            if edge[0] not in check_refs:
                check_refs.add(edge[0])
                queue.append(edge[0])
    for row in records:
        if row['ref'] in scope:
            reasons = epistemic.publication_reference_risk(db, row['ref'], bound)
            if reasons:
                risk('input_reference', row['ref'], {'reasons': reasons}, {'ref': row['ref']})
            uncovered = epistemic.uncovered_refs(db, row['ref'])
            if uncovered:
                risk('coverage', row['ref'], {'uncovered_refs': uncovered}, {'ref': row['ref']})
        residual = [value for value in epistemic.residual_use_risk(db, row['ref'], bound)
                    if value['reason'] == 'disposed_old_ref']
        if residual:
            risk('residual_use', row['ref'], {'reasons': residual}, {'ref': row['ref']})
        for check in row['field_checks'] if row['ref'] in check_refs else []:
            if check['result'] != 'consistent':
                risk('field_check', row['ref'], {key: check[key] for key in ('check_index','result','reason','input_ref')}, {'ref': row['ref']})
    publications = [(f"pub/{r[0]}", r[0], None) for r in db.execute("SELECT publication_id FROM publications WHERE status='complete' ORDER BY publication_id")
                    if f'pub/{r[0]}' not in pub_scopes and not any(v[0] == r[0] for v in pub_scopes.values())]
    publications += [(ref, pid, pinned) for ref, (pid, pinned) in pub_scopes.items()]
    for ref, pid, pinned in publications:
        check = epistemic.publication_check(db, pid, bound, declared_refs=pinned)
        status = check['result']['status']
        if status != 'no impact found' or not check['targets_complete']:
            risk('publication', ref, {'status': status, 'versions': check['result']['versions'],
                                     'targets_complete': check['targets_complete']}, {'ref': ref})
    for row in selected.values():
        row['current_ref'] = latest[row['knowledge_id']]['ref']
        row['version_notices'] = epistemic.version_notices(db, row['ref'], bound)
        if row['current_ref'] != row['ref']:
            row['version_notices'].append({'notice':'newer_revision_exists','source':row['ref'],'current_ref':row['current_ref']})
        row['risk'] = [r['kind'] for r in risks if r['target'] == row['ref']]
    risks.sort(key=lambda v: (v['relevance'] != 'related', v['kind'], v['target'], encoded(v['risk'])))
    return {'critical_now': risks, 'findings': list(selected.values()),
            'completed_findings': completed, 'source_sequence': bound,
            'decision_scope': {ref: sorted(reasons) for ref, reasons in scope.items()}}


def page(store, db, collection, *, cursor=None, limit=50, node_id=None, kind=None,
         query=None, decision_refs=None, final_ref=None):
    if collection not in COLLECTIONS:
        raise ValidationError(f'Unsupported derived collection: {collection}')
    model = read_model(store, db, node_id=node_id, decision_refs=decision_refs, final_ref=final_ref)
    items = model[collection]
    if node_id and collection != 'critical_now':
        items = [v for v in items if v['node_id'] == node_id]
        if collection == 'findings':
            items.sort(key=lambda v: v['kind'] not in ('claim', 'observation'))
    kinds = sorted({v['kind'] for v in items if 'kind' in v})
    if kind:
        items = [v for v in items if v.get('kind') == kind]
    if query is not None and collection != 'findings':
        raise ValidationError('query is only supported by the findings collection')
    if query:
        items = [v for v in items if query.casefold() in v['statement'].casefold()]
    filters = dict(node_id=node_id, kind=kind, query=query, decision_refs=decision_refs, final_ref=final_ref)
    signature = digest({'collection': collection, 'project': store._project(db)['project_id'],
                        'filters': filters, 'items': items})
    after = 0
    if cursor is not None:
        if not isinstance(cursor, dict) or not isinstance(cursor.get('fingerprint'), str) or not isinstance(cursor.get('after'), int):
            raise ValidationError('Invalid derived collection cursor')
        if cursor['fingerprint'] != signature:
            raise ValidationError('read_view_changed: refresh the collection')
        after = cursor['after']
        if not isinstance(after, int) or after < 0:
            raise ValidationError('Invalid derived cursor position')
    limit = max(1, min(int(limit), 200))
    values = items[after:after + limit]
    end = after + len(values)
    return {'collection': collection, 'items': values, 'total': len(items), 'kinds': kinds,
            'has_more': end < len(items), 'cursor': {'fingerprint': signature, 'after': end} if end < len(items) else None,
            'source_sequence': model['source_sequence']}


def compact_finding(item):
    value = {key: item[key] for key in ('ref','current_ref','node_id','kind','status','risk','version_notices','selection_reasons','scope_origins')}
    value['expand'] = {'ref': item['ref']}
    value['truncated_fields'] = []
    for key, budget in (('statement', 200), ('scope', 180), ('conditions', 180)):
        source = item[key]
        text = source if isinstance(source, str) else encoded(source)
        if len(text) > budget:
            value[key] = {'preview': text[:budget], 'notice': '未显示完整，必须展开'}
            value['truncated_fields'].append(key)
        else:
            value[key] = {'status': '未记录'} if source is None or source == '' or source == {} or source == [] else source
    return value


def compact_risk(item):
    value = {**item, 'risk': {}, 'truncated_fields': []}
    for key, field in item['risk'].items():
        if len(encoded(field)) > 400:
            value['truncated_fields'].append(key)
        else:
            value['risk'][key] = field
    return value


def render_context(store, db, blocks, node_id, max_chars, *, decision_refs=None, final_ref=None):
    max_chars = max(500, min(int(max_chars), 12000))
    model = read_model(store, db, node_id=node_id, decision_refs=decision_refs, final_ref=final_ref)
    block_map = dict(blocks)
    project = dict(block_map['project'])
    if len(project['goal']) > 1000:
        project['goal'] = project['goal'][:1000]
        project['goal_truncated'] = True
        project['expand'] = 'research_query without filters returns the project goal'
    block_map['project'] = project
    expand_scope = {key: value for key, value in (
        ('node_id', node_id), ('decision_refs', decision_refs), ('final_ref', final_ref)
    ) if value is not None}
    views = {'identity': block_map['identity']}
    for name in ('critical_now', 'findings'):
        views[name] = {'total': len(model[name]), 'items': [], 'index_complete': False,
                       'details_complete': False, 'expand_required': True,
                       'expand': {'collection': name, **expand_scope}}
    views['critical_now']['risk_index'] = []
    completed = model['completed_findings']
    views['findings']['completed_nodes'] = {
        'total': len(completed), 'items': completed[:8], 'shown': min(8, len(completed)),
        'has_more': len(completed) > 8, 'expand': {'collection': 'completed_findings'}}
    counts = dict(db.execute("SELECT trigger_kind,COUNT(*) FROM review_todos WHERE state='pending' "
                             "AND (? IS NULL OR node_id=?) GROUP BY trigger_kind", (node_id, node_id)))
    clerical = Counter(v['class'] for v in hints.structure_hints(
        db, limit=None, node_id=node_id, klass='clerical')['items'])
    views['review_backlog'] = {'total': sum(counts.values()), 'by_trigger_kind': {},
                               'categories_omitted': len(counts),
                               'expand': {'collection': 'review_todos', 'state': 'pending',
                                          **({'node_id': node_id} if node_id else {})}}
    for kind, count in sorted(counts.items()):
        views['review_backlog']['by_trigger_kind'][kind] = count
        if len('## review_backlog\n' + encoded(views['review_backlog'])) > 600:
            del views['review_backlog']['by_trigger_kind'][kind]
            break
        views['review_backlog']['categories_omitted'] -= 1
    views['clerical_index'] = {'by_class': dict(clerical),
                               'expand': {'collection': 'hints', 'kind': 'clerical',
                                          **({'node_id': node_id} if node_id else {})}}

    def body():
        return '\n\n'.join(f'## {key}\n{encoded(value)}' for key, value in views.items())

    # Shrink optional indexes first; never trim a fixed reference in place.
    if len(body()) > max_chars:
        views['findings']['completed_nodes']['items'] = []
        views['findings']['completed_nodes']['shown'] = 0
        views['findings']['completed_nodes']['has_more'] = bool(completed)
    if len(body()) > max_chars:
        del views['review_backlog'], views['clerical_index']
    if len(body()) > max_chars:
        for name in ('critical_now', 'findings'):
            views[name] = {'total': len(model[name]), 'details_complete': False,
                           'expand_required': True, 'expand': {'collection': name,
                            **expand_scope}}
        views['findings']['completed_nodes'] = {'total': len(completed),
                                               'expand': {'collection': 'completed_findings'}}
    if len(body()) > max_chars:
        del views['identity']
    if len(body()) > max_chars:
        raise ValidationError(f'Context budget too small for decision scope; requires at least {len(body())} characters')
    # Role/control and current task are preserved before filling optional prose.
    for key in ('role_instructions', 'control_instructions', 'attempt', 'task', 'project'):
        if block_map[key] is not None:
            views[key] = block_map[key]
            if len(body()) > max_chars:
                del views[key]
    remaining = max_chars - len(body())
    finding_budget = min(4000, remaining * 5 // 8) if model['critical_now'] else min(4000, remaining)
    risk_budget = min(2400, remaining - finding_budget) if model['findings'] else min(2400, remaining)
    # List stable risk identities before spending the remaining risk budget on details.
    if 'risk_index' in views['critical_now']:
        for item in model['critical_now']:
            index = {'kind': item['kind'], 'target': item['target'], 'ref': item['expand']['ref'],
                     'risk': {key: item['risk'][key] for key in
                              ('status', 'result', 'check_index', 'retracted_ref', 'scope_unconfirmed')
                              if key in item['risk']}}
            entries = views['critical_now']['risk_index']
            entries.append(index)
            if len(encoded(entries)) > risk_budget or len(body()) > max_chars:
                entries.pop()
                break
        risk_budget = max(0, risk_budget - len(encoded(views['critical_now']['risk_index'])))
    findings = model['findings']
    groups = [
        [v for v in findings if 'explicit_input' in v['selection_reasons']],
        [v for v in findings if 'completed_node' in v['selection_reasons']],
        [v for v in findings if 'disputed' in v['selection_reasons']][:2],
        [v for v in findings if 'revised' in v['selection_reasons']][:2],
    ]
    # Interleave independent channels; history volume cannot replace ordinary findings.
    ordered, used_refs = [], set()
    for slot in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if slot < len(group) and group[slot]['ref'] not in used_refs:
                ordered.append(group[slot]); used_refs.add(group[slot]['ref'])
    ordered.extend(v for v in findings if v['ref'] not in used_refs)
    for name, candidates, budget in (
        ('critical_now', model['critical_now'], risk_budget), ('findings', ordered, finding_budget)
    ):
        if 'items' not in views[name]:
            continue
        for item in candidates:
            value = compact_finding(item) if name == 'findings' else compact_risk(item)
            if len(encoded(views[name]['items'] + [value])) > budget:
                if name == 'findings' and any(reason in item['selection_reasons'] for reason in ('explicit_input', 'completed_node')):
                    # Keep the first deferred priority ref; do not replace it with cheaper background.
                    views[name]['deferred_refs'] = [item['ref']]
                    if len(body()) > max_chars:
                        del views[name]['deferred_refs']
                    break
                continue
            views[name]['items'].append(value)
            if len(body()) > max_chars:
                views[name]['items'].pop()
                break
        views[name]['index_complete'] = len(views[name].get('risk_index', views[name]['items'])) == len(model[name])
        views[name]['details_complete'] = len(views[name]['items']) == len(model[name]) and not any(
            value['truncated_fields'] for value in views[name]['items'])
        views[name]['expand_required'] = not views[name]['details_complete']
    for key in ('questions', 'checkpoint_recovery', 'checkpoint', 'conclusion', 'frozen_context', 'specialist_task', 'method_guidance', 'recent_node_notes'):
        if block_map[key] is not None:
            views[key] = block_map[key]
            if len(body()) > max_chars:
                del views[key]
    text = body()
    shown = views['findings'].get('items', [])
    risk_shown = views['critical_now'].get('items', [])
    risk_index = views['critical_now'].get('risk_index', [])
    dependencies = {value['ref'] for value in shown}
    dependencies.update(views['findings'].get('deferred_refs', []))
    dependencies.update(value['target'] for value in risk_shown + risk_index)
    dependencies.update(value['risk']['retracted_ref'] for value in risk_shown if 'retracted_ref' in value['risk'])
    if 'checkpoint' in views:
        value = views['checkpoint']
        dependencies.add(f"checkpoint/{value['checkpoint_id']}@{value['revision']}")
    if 'method_guidance' in views:
        value = views['method_guidance']
        dependencies.add(f"guidance/{value['guidance_id']}@{value['content_hash']}")
    omitted = [name for name, value in blocks if value is not None and value != [] and value != {} and name not in views and name not in
               ('knowledge', 'pending_review', 'affected_knowledge', 'structure_hints', 'retracted_refs', 'index')]
    coverage = {name: {'source_complete': True, 'total': len(model[name]),
                      'shown': len(views[name].get('items', [])),
                      'index_shown': len(views[name].get('risk_index', views[name].get('items', []))),
                      'index_complete': views[name].get('index_complete', False),
                      'details_complete': views[name]['details_complete']}
                for name in ('critical_now', 'findings')}
    coverage['completed_findings'] = {'total': len(completed),
        'shown': views['findings']['completed_nodes']['shown'] if 'shown' in views['findings']['completed_nodes'] else 0,
        'source_complete': True, 'expand': {'collection': 'completed_findings'}}
    return {'text': text, 'policy_version': 'memory-r1-v1', 'context_mode': block_map['specialist_task']['context_mode'] if block_map['specialist_task'] else None,
            'source_digest': hashlib.sha256(text.encode()).hexdigest(),
            'source_sequence': model['source_sequence'], 'dependencies': sorted(dependencies),
            'selection': {value['ref']: {'reasons': value['selection_reasons'], 'scope_origins': value['scope_origins']} for value in shown},
            'truncation': {'fields': {value['ref']: value['truncated_fields'] for value in shown if value['truncated_fields']},
                           'scope_params_omitted': False, 'project_goal': bool(project.get('goal_truncated')),
                           'unshown': {name: len(model[name]) - len(views[name].get('items', [])) for name in ('critical_now', 'findings')}},
            'coverage': coverage,
            'block_chars': {name: len(f'## {name}\n{encoded(value)}') for name, value in views.items()},
            'truncated': any(not coverage[name]['details_complete'] for name in ('critical_now', 'findings')) or bool(omitted) or bool(project.get('goal_truncated')),
            'omitted': omitted}


def context_metrics(view):
    """Count rendered text, separating clerical hints from publication risk."""
    import re
    matches = list(re.finditer(r'(?m)^## ([^\n]+)\n', view['text']))
    blocks = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(view['text'])
        text = view['text'][match.end():end].rstrip()
        blocks[match[1]] = json.loads(text)
    chars = {match[1]: len(view['text'][match.start():matches[index + 1].start() if index + 1 < len(matches) else len(view['text'])].rstrip()) for index, match in enumerate(matches)}
    if view['policy_version'] == 'memory-r1-v1':
        findings = blocks.get('findings', {}).get('items', [])
        critical = blocks.get('critical_now', {})
        routine = chars.get('review_backlog', 0) + chars.get('clerical_index', 0)
    else:
        knowledge = blocks.get('knowledge', {})
        groups = knowledge.values() if isinstance(knowledge, dict) and 'items' not in knowledge else [knowledge]
        findings = [row for group in groups if isinstance(group, dict) for row in group.get('items', [])]
        critical = {}
        clerical = [item for item in blocks.get('structure_hints', {}).get('items', [])
                    if item.get('class') in hints.CLASSES[:3]]
        routine = chars.get('pending_review', 0)
        if clerical:
            routine += len('## clerical_index\n' + encoded({'items': clerical}))
    return {'characters': len(view['text']), 'routine_characters': routine,
            'visible_refs': sorted({row['ref'] for row in findings if isinstance(row.get('ref'), str) and re.fullmatch(r'knowledge/[^@\s]+@\d+', row['ref'])}),
            'critical_total': critical.get('total'), 'critical_shown': len(critical.get('risk_index', critical.get('items', []))),
            'critical_details_shown': len(critical.get('items', [])),
            'critical_details_complete': critical.get('details_complete'), 'block_chars': chars}
