/** Human explanations of existing ledger states; no scientific judgements. */
import type { Row } from "./model"

export const REVIEW_COLLECTIONS = [
  { id: "critical_now", title: "当前风险", note: "与当前范围相关或相关性尚不明确的检查结果。" },
  { id: 'review_obligations', title:'当前审阅义务',note:'精确到版本和变更。任务完成不表示科学风险解决。' },
  { id: 'review_candidates', title:'审阅候选',note:'普通事件形成的候选，按需展开；不会自动阻断研究。' },
  { id: "review_todos", title: "历史审阅队列", note: "历史审阅记录，不是继续研究或结项的统一门槛。" },
  { id: "hints", title: "文书提示", note: "可按需整理的引用和关系提示，不代表科学结论错误。" },
] as const

export interface ReviewLink { ref: string; label: string; file: boolean; path?: string }
export interface ReviewCard {
  title: string
  summary: string
  relevance: string
  sections: { title: string; lines: string[] }[]
  links: ReviewLink[]
}

const TRIGGERS: Record<string, string> = {
  publication: "发布材料", "attempt-finished": "工作段结束",
  "knowledge-revision": "知识修订", "correction-note": "纠正说明",
}
const STATES: Record<string, string> = { pending: "待审阅", running: "审阅中", proposal_ready: "审阅材料已准备", completed: "任务已完成",out_of_scope:'已离开范围（风险保留）',waived:'显式例外（未证明通过）' }
export function reviewFilterLabel(value: string): string {
  const labels: Record<string, string> = { ...TRIGGERS, ...STATES,
    impact: "知识影响", retracted_basis: "撤回依据", input_reference: "输入版本风险", residual_use: "旧依据使用",
    field_check: "字段检查", coverage: "检查覆盖", claim: "主张", observation: "观察", hypothesis: "假设", lesson: "经验", decision: "决策",
    prose_mention_without_relation: "正文提及未关联", whole_snapshot_evidence: "快照未定位条目", lineage_mention_without_predecessor: "节点提及未关联前驱" }
  return labels[value] ?? value
}

const REASONS: Record<string, string> = {
  final: "已登记的最终报告", decision_input: "本次明确指定的综合材料", node_input: "节点固定输入",
  dependency: "节点声明的依赖", declared_node_input: "明确引用的输入节点及其登记发现", node_knowledge: "当前节点登记的知识", support_ancestor: "沿知识支撑边关联",
  decision_scope_not_declared_for_target: "尚无足够的范围声明判断其是否影响当前决策",
  retracted_ref: "仍引用已撤回的版本", disposed_old_ref: "仍引用已被修订或撤回处置的旧依据",
  in_change_scope: "位于尚待处置的变更范围", value_mismatch: "声明值与冻结材料不一致",
  missing_value: "冻结材料中缺少声明字段", type_mismatch: "字段类型不一致",
  unsupported_format: "材料格式不支持字段检查", not_a_scalar: "字段不是单个值",
  invalid_number: "字段不是有效数值", object_missing: "冻结文件缺失", object_corrupted: "冻结文件无法校验",
}

function text(value: unknown): string { return typeof value === "string" ? value : JSON.stringify(value) }
export function recordFields(value: unknown): [string, string][] {
  if (value === null || value === undefined || value === "") return []
  if (typeof value !== "object") return [["", text(value)]]
  return Object.entries(value).map(([key, entry]) => [key, text(entry)])
}
function fields(value: unknown): string[] { return recordFields(value).map(([key, value]) => key ? `${key}：${value}` : value) }

export function reviewCard(item: any, source?: any): ReviewCard {
  const card: ReviewCard = { title: "", summary: "", relevance: "", sections: [], links: [] }
  const seen = new Set<string>()
  function link(ref: string, label: string, file = false, path?: string) {
    if (!seen.has(ref)) { card.links.push({ ref, label, file, path }); seen.add(ref) }
  }
  if (item.obligation_id) {
    card.title = STATES[item.state] ?? item.state
    card.summary = `${item.target_ref}${item.change_id ? ` · ${item.change_id}` : ''}`
    card.relevance = `${item.scope_owner} · generation ${item.generation}`
    card.sections.push({title:'任务与风险',lines:[`任务：${card.title}`,`科学审阅覆盖：尚未评估`,
      item.risk_disposition ? `风险处置：${({unresolved:'尚未解决',resolved:'已记录处置',not_assessed:'尚未评估'} as Record<string,string>)[item.risk_disposition] ?? item.risk_disposition}` : '任务完成不表示风险已解决。',
      ...(item.risk?.needs_action ? ['知识仍需处置。'] : [])]})
    link(item.target_ref,'查看精确目标')
    for (const event of item.events ?? []) {
      card.sections.push({title:event.action,lines:[event.reason,`证据 ${event.evidence_refs.length} 项`,
        ...(event.scope_snapshot.result ? [`结果：${event.scope_snapshot.result}`] : [])]})
      for (const ref of event.evidence_refs) link(ref,'查看审阅证据',ref.startsWith('pub/') || ref.startsWith('S-'))
    }
    return card
  }
  if (item.candidate_id) {
    card.title = `候选 · ${TRIGGERS[item.trigger_kind] ?? item.trigger_kind}`
    card.summary = '候选不自动成为义务，不阻断研究。'
    card.sections.push({title:'候选来源',lines:[item.trigger_ref,`目标映射：${item.mapping_status}`]})
    for (const ref of item.target_refs ?? []) link(ref,'查看候选目标')
    return card
  }
  const risk = item.risk
  if (risk) {
    const titles: Record<string, string> = {
      impact: "知识变更仍待处置", retracted_basis: "仍引用已撤回的依据", input_reference: "当前输入存在版本风险",
      residual_use: "仍在使用已处置的旧依据", coverage: "部分依据尚未覆盖检查",
      field_check: risk.result === "inconsistent" ? "声明字段与冻结材料不一致" : "声明字段无法检查",
      publication: risk.status === "attention" ? "报告引用的依据需要关注" : risk.status === "coverage partial" ? "报告的部分依据未覆盖检查" : "报告的检查覆盖未知",
      review_challenge:'审阅提出挑战（未处置）',
    }
    card.title = titles[item.kind]
    if (item.kind==='review_challenge') {
      card.summary='审阅任务已完成，但报告中的挑战仍需科学判断；完成任务不表示风险解决。'
      link(risk.review_ref,'打开挑战报告',true)
    } else if (item.kind === "publication") {
      card.summary = risk.status === "coverage unknown"
        ? "该材料未登记结构化知识引用，系统无法检查其知识依据的版本与风险。"
        : risk.status === "attention" ? "已登记的知识引用存在待处理影响或仍在使用旧依据。"
        : "已登记的引用不能全部检查，仍有未覆盖或未展开的依据。"
      card.sections.push({ title: "检查边界", lines: ["不代表报告错误，也不代表正文没有证据。检查只覆盖明确登记的引用。"] })
      for (const [ref, version] of Object.entries(risk.versions ?? {}) as [string, any][]) {
        card.sections.push({ title: ref, lines: [
          ...(version.needs_action ? ["该版本存在待处置影响"] : []),
          ...(version.residual_use_risk ?? []).map((v: any) => REASONS[v.reason] ?? v.reason),
          ...(version.uncovered_refs ?? []).map((v: unknown) => `未覆盖引用：${text(v)}`),
        ] })
        link(ref, "查看报告引用的知识")
      }
    } else if (item.kind === "field_check") {
      card.summary = `${REASONS[risk.reason] ?? risk.reason}。这是字段检查结果，不是对科学结论的判定。`
      card.sections.push({ title: "检查定位", lines: [`检查编号：${risk.check_index}`, `冻结材料：${risk.input_ref}`] })
      link(risk.input_ref, "查看冻结材料", true)
    } else if (item.kind === "impact") {
      card.summary = risk.scope_unconfirmed ? "变更影响范围尚未确定，需要结合依据判断。" : "该固定知识版本受到变更影响，尚未记录明确处置。"
      card.sections.push({ title: "变更", lines: [`变更：${risk.change_id}`, `影响记录：${risk.impact_id}`] })
      if (risk.explanation) card.sections.push({ title: "支撑路径", lines: risk.explanation.map((edge: any) => `${edge.from} → ${edge.to}（${edge.source === "dependencies" ? "依赖" : edge.source === "both" ? "依赖与证据" : "证据"}）`) })
    } else {
      card.summary = "这项风险涉及当前输入或知识的支撑依据，展开具体版本后判断其适用范围。"
      if (risk.retracted_ref) link(risk.retracted_ref, "查看被撤回的依据")
      for (const reason of risk.reasons ?? []) {
        card.sections.push({ title: "原因", lines: [REASONS[reason.reason] ?? reason.reason, ...(reason.change_id ? [`变更：${reason.change_id}`] : [])] })
        if (reason.source) link(reason.source, "查看风险来源")
      }
      if (risk.uncovered_refs) card.sections.push({ title: "未覆盖的引用", lines: risk.uncovered_refs.map(text) })
    }
    card.relevance = item.relevance === "related" ? "与当前范围相关" : "与当前范围的相关性尚不明确"
    card.sections.push({ title: "为何显示", lines: item.relevance_reason.map((v: string) => REASONS[v] ?? v) })
    card.sections.push({ title: "对象", lines: [item.target] })
    link(item.target, "查看相关知识或材料")
  } else if (item.todo_id) {
    card.title = `${STATES[item.state] ?? item.state} · ${TRIGGERS[item.trigger_kind] ?? item.trigger_kind}`
    card.summary = "这是历史审阅任务。完成审阅与科学风险已解决是不同状态，不要求清空全部历史队列。"
    card.sections.push({ title: "审阅记录", lines: [`编号：${item.todo_id}`, `来源：${item.trigger_ref}`, `节点：${item.node_id ?? "项目级"}`] })
    link(item.trigger_ref, "查看任务来源")
  } else if (item.class) {
    const labels: Record<string, string> = {
      prose_mention_without_relation: "正文提及尚未关联为结构化关系",
      whole_snapshot_evidence: "证据引用尚未定位到快照条目",
      lineage_mention_without_predecessor: "节点提及尚未登记为前驱关系",
    }
    card.title = labels[item.class] ?? item.class
    card.summary = "文书整理提示，只有明确科学关系后再登记；系统不会据此自动补关系。"
    card.sections.push({ title: "提示位置", lines: fields(item.evidence) })
    link(item.target, "查看提示对象")
  } else if (item.ref) {
    card.title = item.statement
    card.summary = "固定版本的研究发现，适用条件和科学依据以完整记录为准。"
    card.sections.push({ title: "版本", lines: [`固定引用：${item.ref}`, `状态：${item.status}`,
      ...(item.current_ref ? [`当前版本：${item.current_ref}`] : [])] })
    if (item.current_ref && item.current_ref !== item.ref) link(item.current_ref, "查看当前版本（不表示已验证）")
    link(item.ref, "查看完整发现")
  } else {
    card.title = `${item.node_id} · ${item.finding_count ? `已登记 ${item.finding_count} 项发现` : "未登记发现"}`
    card.summary = "节点执行完成不等于已有科学发现；未登记发现不会被当作验证通过。"
    link(item.node_id, "查看节点")
    for (const ref of item.selected_refs ?? []) link(ref, "查看节点发现")
  }
  if (source) {
    card.sections.push({ title: "正在查看的记录", lines: [source.ref] })
    if (source.source_path) {
      card.sections.push({ title: "材料文件", lines: [source.source_path] })
      // Upgrade an existing record link when its frozen file has been resolved.
      const old = card.links.find((v) => v.ref === source.ref)
      if (old) { old.file = true; old.path = source.source_path; old.label = "打开报告或材料" }
      else link(source.ref, "打开报告或材料", true, source.source_path)
    }
    for (const [key, title] of [["question", "节点问题"], ["plan", "节点计划"], ["body", "原说明"], ["summary", "材料说明"]]) {
      if (source[key]) card.sections.push({ title, lines: [source[key]] })
    }
    for (const ref of source.inputs ?? []) link(ref, "查看节点输入")
    if (source.manifest && Array.isArray(source.manifest)) {
      for (const entry of source.manifest) {
        if (entry.status === "fixed") link(`${source.ref}#${encodeURIComponent(entry.source_path)}`, "打开快照条目", true, entry.source_path)
      }
    }
    if (source.statement) card.sections.push({ title: "知识内容", lines: [source.statement] })
    for (const [key, title] of [["scope", "适用范围"], ["conditions", "适用条件"]]) {
      if (source.ref?.startsWith("knowledge/")) card.sections.push({ title, lines: recordFields(source[key]).length ? fields(source[key]) : ["未记录"] })
    }
    for (const ref of [...(source.knowledge_refs ?? []), ...(source.evidence_refs ?? []), ...(source.dependencies ?? [])]) link(ref, "查看依据")
    for (const file of source.items ?? []) if (file.source_path) link(file.ref, "打开材料文件", true, file.source_path)
    if (source.current_ref && source.current_ref !== source.ref) {
      card.sections.push({ title: "版本变化", lines: [`当前版本：${source.current_ref}`, "存在新版本不表示固定版本自动失效，需查看修订范围。"] })
      link(source.current_ref, "查看当前版本")
    }
    for (const ref of source.published_refs ?? []) link(ref, "查看发布中固定引用的版本")
    for (const notice of source.version_notices ?? item.version_notices ?? []) {
      card.sections.push({ title: "依据版本通知", lines: [notice.source,
        notice.notice === "superseded_ref" ? "引用版本标为已取代，需核对实际影响范围。" : "引用存在更新版本，需查看修订；不自动判为失效。"] })
      link(notice.source, "查看引用版本")
      if (notice.current_ref) link(notice.current_ref, "查看更新版本")
    }
    for (const check of source.field_checks ?? []) card.sections.push({ title: `字段检查 ${check.check_index}`, lines: [
      check.result === "consistent" ? "声明字段与冻结材料一致，不证明科学结论。" : REASONS[check.reason] ?? check.reason,
      `材料：${check.input_ref}`,
    ] })
  }
  card.links.sort((a, b) => Number(b.file) - Number(a.file))
  return card
}

export function reviewRows(items: any[]): Row[] {
  return items.map((item) => {
    const card = reviewCard(item)
    return { id: JSON.stringify([item.obligation_id ?? item.candidate_id ?? item.ref ?? item.target ?? item.todo_id ?? item.node_id,
      item.kind ?? item.class, item.risk?.change_id, item.risk?.check_index, item.risk?.retracted_ref, item.risk?.review_event,item.evidence]),
      code: item.target_ref ?? item.candidate_id ?? item.target ?? item.ref ?? item.todo_id ?? item.node_id,
      title: card.title, data: item }
  })
}
