/**
 * Pure projections from ledger views to board rows and view models. Execution,
 * delivery and scientific state stay separate: an ended work segment, a
 * complete publication and a knowledge status are never merged into "done".
 */
import { OUTCOMES, REVIEWS, conclusionLines, pauseReason, roleName, runState } from "../format"

export type Tab = "overview" | "process" | "knowledge" | "materials" | "runtime" | "review"

export const TABS: readonly { id: Tab; key: string; title: string; views: readonly { id: Tab; title: string }[] }[] = [
  { id: "overview", key: "1", title: "概览", views: [{ id: "overview", title: "概览" }] },
  { id: "process", key: "2", title: "研究", views: [
    { id: "process", title: "研究过程" }, { id: "runtime", title: "运行会话" },
  ] },
  { id: "knowledge", key: "3", title: "成果", views: [
    { id: "knowledge", title: "研究发现" }, { id: "materials", title: "发布材料" }, { id: "review", title: "依据检查" },
  ] },
]

export interface Row {
  id: string
  /** Short ledger identifier, shown dimmed in front of the title. */
  code: string
  title: string
  /** Frozen reference the reader can open, when the row is a file. */
  ref?: string
  data?: any
}

export const DELIVERY: Record<string, string> = { partial: "阶段材料", complete: "交付完成" }
/** Said once per page: delivery is about the report, not about the science. */
export const DELIVERY_NOTE = "complete 只表示交付完成，不等于科学验证"
export const LATEST_NOTE = "最新，不代表最终结论"
export const RUN_NOTE = "执行状态，不代表研究结论"
export const REVIEW_NOTE = "不表示结论通过"
export const STATUS_NOTE = "状态不表示科学验证"

export const RELATION_TYPES: Record<string, string> = {
  depends_on: "依赖",
  branches_from: "衍生自",
  revises: "修订",
}

export const KNOWLEDGE_KINDS: Record<string, string> = {
  claim: "主张",
  observation: "观察",
  hypothesis: "假设",
  decision: "决定",
  lesson: "经验",
  open_question: "未决问题",
}

export const KNOWLEDGE_STATUSES: Record<string, string> = {
  proposed: "已提出",
  working: "进行中",
  disputed: "有争议",
  superseded: "已被取代",
  retracted: "已撤回",
}

/** `knowledge/K-007@1` → `K-007`; later revisions keep their number. */
export function knowledgeCode(ref: string): string {
  const match = /^knowledge\/([^@]+)(?:@(\d+))?$/.exec(ref ?? "")
  if (!match) return ref ?? ""
  return match[2] && match[2] !== "1" ? `${match[1]}@${match[2]}` : match[1]!
}

function oneLine(value: unknown, limit = 90): string {
  const text = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : ""
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

export interface PublicationCard {
  id: string
  status: string
  node?: string
  title?: string
  overview?: string
  sections: { heading: string; items: string[] }[]
  summary: string
  gaps: string[]
}

export function publicationCard(publication: any): PublicationCard | undefined {
  if (!publication?.publication_id) return undefined
  const display = publication.display && typeof publication.display === "object" ? publication.display : {}
  return {
    id: publication.publication_id,
    status: publication.status,
    node: publication.node_id ?? undefined,
    title: display.title || undefined,
    overview: display.overview || undefined,
    sections: (display.sections ?? []).map((section: any) => ({ heading: section.heading, items: section.items ?? [] })),
    summary: String(publication.summary ?? ""),
    gaps: publication.gaps ?? [],
  }
}

export interface OverviewModel {
  goal: string
  root: string
  runState?: string
  runLabel: string
  runNote: string
  /** Verbatim notices, such as a historical completion without a conclusion record. */
  notices: string[]
  conclusion?: {
    outcome: string
    outcomeLabel: string
    finalRef: string
    reviewLabel: string
    reviewNote: string
    gaps: string[]
    limitations: string[]
    refs: string[]
  }
  counts: { label: string; value: number }[]
  /** The publication the conclusion names. */
  final?: PublicationCard
  /** The newest publication, when it is not the final one. */
  latest?: PublicationCard
  presentation?: {
    title: string
    summary: string
    stale: boolean
    metrics: { label: string; values: string; warning?: string }[]
    deliverables: { label: string; ref: string }[]
  }
  presentationError?: string
}

export function overviewModel(summary: any, presentation: any): OverviewModel | null {
  if (!summary?.project) return null
  const counts = summary.counts ?? {}
  const conclusion = summary.conclusion
  const final = conclusion ? publicationCard(summary.final_publication) : undefined
  const newest = publicationCard(summary.latest_publication ?? (conclusion ? null : summary.final_publication))
  const shown = (side: any) => (side?.value ?? side?.value === 0 ? String(side.value) : "（固定版本中的数值不可用）")
  const value = presentation?.status === "ready" ? presentation.value ?? {} : null
  return {
    goal: summary.project.goal ?? "未记录",
    root: summary.project_root ?? "",
    runState: summary.run?.state,
    runLabel: runState(summary.run?.state),
    runNote: RUN_NOTE,
    notices: conclusion ? [] : conclusionLines(null, summary.run?.state),
    conclusion: conclusion
      ? {
          outcome: conclusion.outcome,
          outcomeLabel: OUTCOMES[conclusion.outcome] ?? conclusion.outcome,
          finalRef: conclusion.final_ref,
          reviewLabel: REVIEWS[conclusion.review?.status] ?? conclusion.review?.status,
          reviewNote: REVIEW_NOTE,
          gaps: conclusion.gaps ?? [],
          limitations: conclusion.review?.limitations ?? [],
          refs: conclusion.review?.refs ?? [],
        }
      : undefined,
    counts: [
      { label: "节点", value: counts.nodes ?? 0 },
      { label: "发布", value: counts.publications ?? 0 },
      { label: "知识", value: counts.knowledge ?? 0 },
      { label: "检查点", value: counts.checkpoints ?? 0 },
      { label: "待整理", value: summary.review_queue?.pending_total ?? 0 },
    ],
    final,
    latest: newest && newest.id !== final?.id ? newest : undefined,
    presentation: value
      ? {
          title: value.title ?? "",
          summary: value.summary ?? "",
          stale: Boolean(value.stale),
          metrics: (value.metrics ?? []).slice(0, 4).map((metric: any) => ({
            label: `${metric.label ?? metric.id}${metric.split ? `（${metric.split}）` : ""}`,
            values: `${shown(metric.baseline)} → ${shown(metric.current)}${metric.unit ? ` ${metric.unit}` : ""}`,
            warning: metric.comparison_warning || undefined,
          })),
          deliverables: (value.deliverables ?? []).map((item: any) => ({ label: item.label, ref: item.ref })),
        }
      : undefined,
    presentationError: presentation?.status === "invalid" ? presentation.message ?? "" : undefined,
  }
}

export function knowledgeRows(page: any): Row[] {
  return (page?.items ?? []).map((item: any) => ({
    id: item.ref,
    code: knowledgeCode(item.ref),
    title: `[${KNOWLEDGE_KINDS[item.kind] ?? item.kind}] ${oneLine(item.statement, 60)}`,
    data: item,
  }))
}

/**
 * Kind chips from server-side totals (one count query per kind), so a kind
 * that first appears after the loaded page is still listed and selectable.
 * The entry without a kind carries the total for all kinds.
 */
export function kindChips(totals: { kind?: string; total: number }[]): { total: number; kinds: { kind: string; count: number }[] } {
  return {
    total: totals.find((entry) => !entry.kind)?.total ?? 0,
    kinds: totals
      .filter((entry) => entry.kind && entry.total > 0)
      .map((entry) => ({ kind: entry.kind!, count: entry.total }))
      .sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind)),
  }
}

/** One row per publication item, so Enter can open the frozen file. */
export function materialRows(page: any): Row[] {
  const rows: Row[] = []
  for (const publication of page?.items ?? []) {
    const items = (publication.items ?? []).filter((item: any) => item.item_id !== "__research_display")
    const title = publication.display?.title ?? oneLine(publication.summary, 40)
    if (!items.length) {
      rows.push({ id: publication.publication_id, code: publication.publication_id, title, data: publication })
    }
    for (const item of items) {
      rows.push({
        id: item.ref ?? `pub/${publication.publication_id}#${item.item_id}`,
        code: publication.publication_id,
        title: `${item.item_id}  ${item.source_path ?? item.kind ?? ""}`,
        ref: item.object_kind === "file" || item.source_path ? item.ref ?? `pub/${publication.publication_id}#${item.item_id}` : undefined,
        data: { ...publication, item },
      })
    }
  }
  return rows
}

export function isMarkdown(path: string | undefined): boolean {
  return /\.(md|markdown)$/i.test(path ?? "")
}

const SPECIALIST_STATES: Record<string, string> = {
  starting: "启动中",
  running: "运行中",
  completed: "已交回报告",
  incomplete: "未完成",
  cancelled: "已取消",
  unverified: "退出未核实",
}

/** A specialist session shows its task; other sessions their pause state. */
export function sessionActivity(session: any): string {
  if (session.task_id) return `${session.label} · ${SPECIALIST_STATES[session.specialist_state] ?? session.specialist_state}`
  return pauseReason(session.pause_reason) || "活动"
}

/** Session IDs are opaque; the row names the role and node, the detail keeps the ID. */
export function sessionRows(page: any): Row[] {
  return (page?.items ?? []).map((session: any) => ({
    id: session.session_id,
    code: session.node_id ?? "项目",
    title: `${roleName(session.role)}${session.detached ? "（已移出）" : ""} · ${sessionActivity(session)}`,
    data: session,
  }))
}

export const TASK_STATES: Record<string, string> = {
  queued: "排队",
  starting: "启动中",
  running: "运行中",
  finished: "已结束",
  failed: "失败",
  cancelled: "已取消",
  unverified: "待核实",
}

/** Execution only: the conclusion record lives on the overview and is not repeated here. */
export function runtimeNotes(summary: any): string[] {
  return summary?.conclusion ? ["结项记录：已登记，见概览"] : conclusionLines(null, summary?.run?.state)
}

const UNAVAILABLE: Record<string, string> = {
  object_missing: "这个文件的固定版本不在本机：.research/objects 里没有它，所以打不开。",
}

export function decodeChunk(content: any): string {
  if (!content) return ""
  if (content.kind === "directory") return "（这是一个目录）"
  if (content.kind === "binary") return `（二进制文件，${content.total_bytes ?? "?"} 字节，终端里不显示）`
  if (content.kind !== "text") {
    const reason = content.resolution?.reason
    const message = content.resolution?.message ?? "（无法读取）"
    return UNAVAILABLE[reason] ? `${UNAVAILABLE[reason]}（${reason}）` : message
  }
  return Buffer.from(content.chunk ?? "", "base64").toString("utf8")
}
