/**
 * Pure projections from ledger views to board rows and detail text. Execution,
 * delivery and scientific state stay separate: an ended work segment, a
 * complete publication and a knowledge status are never merged into "done".
 */
import { pauseReason, roleName, runState } from "../format"

export type Tab = "overview" | "process" | "knowledge" | "materials" | "runtime"

export const TABS: readonly { id: Tab; key: string; title: string }[] = [
  { id: "overview", key: "1", title: "概览" },
  { id: "process", key: "2", title: "研究过程" },
  { id: "knowledge", key: "3", title: "成果与知识" },
  { id: "materials", key: "4", title: "材料" },
  { id: "runtime", key: "5", title: "运行" },
]

export interface Row {
  id: string
  label: string
  /** Frozen reference the reader can open, when the row is a file. */
  ref?: string
  data?: any
}

const PUBLICATION_STATUS: Record<string, string> = {
  partial: "阶段材料（partial）",
  complete: "交付完成（complete，不等于科学验证）",
}

function oneLine(value: unknown, limit = 90): string {
  const text = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : ""
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

function displayBlock(display: any): string[] {
  if (!display || typeof display !== "object") return []
  const lines = [display.title ? `「${display.title}」` : "", display.overview ?? ""]
  for (const section of display.sections ?? []) {
    lines.push(`${section.heading}：`)
    for (const item of section.items ?? []) lines.push(`  · ${item}`)
  }
  return lines.filter(Boolean)
}

export function overviewText(summary: any, presentation: any): string {
  if (!summary?.project) return "当前目录没有研究项目。用 /research init <研究目标> 新建。"
  const counts = summary.counts ?? {}
  const latest = summary.final_publication
  const lines = [
    `目标：${summary.project.goal ?? "未记录"}`,
    `项目目录：${summary.project_root ?? ""}`,
    `运行状态：${runState(summary.run?.state)}（这是执行状态，不表示研究结论）`,
    `节点 ${counts.nodes ?? 0} · 发布 ${counts.publications ?? 0} · 知识 ${counts.knowledge ?? 0} · 检查点 ${counts.checkpoints ?? 0} · 待整理 ${summary.review_queue?.pending_total ?? 0}`,
    "",
  ]
  if (presentation?.status === "ready") {
    const value = presentation.value ?? {}
    lines.push("项目展示摘要" + (value.stale ? "（绑定的发布已过期，请核对新材料）" : "") + "：")
    lines.push(value.title ?? "", value.summary ?? "")
    const shown = (side: any) => (side?.value ?? side?.value === 0 ? String(side.value) : "（冻结值不可用）")
    for (const metric of (value.metrics ?? []).slice(0, 4)) {
      const unit = metric.unit ? ` ${metric.unit}` : ""
      const split = metric.split ? `（${metric.split}）` : ""
      lines.push(`  · ${metric.label ?? metric.id}${split}：${shown(metric.baseline)} → ${shown(metric.current)}${unit}`)
      if (metric.comparison_warning) lines.push(`    ${metric.comparison_warning}`)
    }
    for (const deliverable of value.deliverables ?? []) lines.push(`  交付：${deliverable.label} ${deliverable.ref}`)
    lines.push("")
  } else if (presentation?.status === "invalid") {
    lines.push(`项目展示摘要不可用：${presentation.message ?? ""}`, "")
  }
  if (latest) {
    lines.push(`最新阶段成果 ${latest.publication_id}（最新，不代表最终结论）`)
    lines.push(`交付状态：${PUBLICATION_STATUS[latest.status] ?? latest.status} · 节点 ${latest.node_id ?? "未挂节点"}`)
    const display = displayBlock(latest.display)
    lines.push(...(display.length ? display : [oneLine(latest.summary, 400)]))
  } else {
    lines.push("尚无已登记的阶段成果。")
  }
  return lines.filter((line) => line !== undefined).join("\n")
}

export function nodeRows(page: any): Row[] {
  return (page?.items ?? []).map((node: any) => ({
    id: node.node_id,
    label: `${node.node_id} · ${node.status} · ${oneLine(node.question, 70)}`,
    data: node,
  }))
}

export function nodeDetail(node: any, dependencies: any[], attempts: any[]): string {
  if (!node) return ""
  const incoming = dependencies.filter((edge) => edge.successor_node_id === node.node_id)
  const outgoing = dependencies.filter((edge) => edge.predecessor_node_id === node.node_id)
  const work = attempts.filter((attempt) => attempt.node_id === node.node_id)
  return [
    `${node.node_id}（${node.status}）`,
    `问题：${node.question}`,
    `为什么现在做：${node.why_now ?? ""}`,
    `计划：${node.plan ?? ""}`,
    node.root_reason ? `独立根理由：${node.root_reason}` : "",
    `问题版本：${node.question_ref ?? "未记录"}`,
    `固定输入：${(node.inputs ?? []).length ? node.inputs.join(", ") : "无"}`,
    `前驱：${incoming.length ? incoming.map((e) => `${e.predecessor_node_id}（${e.relation_type}）`).join(", ") : "无"}`,
    `后继：${outgoing.length ? outgoing.map((e) => `${e.successor_node_id}（${e.relation_type}）`).join(", ") : "无"}`,
    `工作段：${work.length ? work.map((a) => `${a.attempt_id} ${a.state}${a.ended_at ? "" : "（进行中）"}`).join(", ") : "无"}`,
  ]
    .filter(Boolean)
    .join("\n")
}

export function knowledgeRows(page: any): Row[] {
  return (page?.items ?? []).map((item: any) => ({
    id: item.ref,
    label: `${item.ref} · ${item.kind} · ${item.status} · ${oneLine(item.statement, 60)}`,
    data: item,
  }))
}

export function knowledgeDetail(item: any): string {
  if (!item) return ""
  return [
    `${item.ref}（${item.kind}，状态 ${item.status}；状态不表示科学验证）`,
    item.statement ?? "",
    `节点：${item.node_id ?? "项目级"}`,
    `证据：${(item.evidence_refs ?? []).join(", ") || "无"}`,
    item.conditions && Object.keys(item.conditions).length ? `条件：${JSON.stringify(item.conditions)}` : "",
    item.scope && Object.keys(item.scope).length ? `范围：${JSON.stringify(item.scope)}` : "",
    (item.supersedes ?? []).length ? `取代：${item.supersedes.join(", ")}` : "",
  ]
    .filter(Boolean)
    .join("\n")
}

/** One row per publication item, so Enter can open the frozen file. */
export function materialRows(page: any): Row[] {
  const rows: Row[] = []
  for (const publication of page?.items ?? []) {
    const items = (publication.items ?? []).filter((item: any) => item.item_id !== "__research_display")
    const title = publication.display?.title ?? oneLine(publication.summary, 40)
    if (!items.length) {
      rows.push({ id: publication.publication_id, label: `${publication.publication_id} · ${publication.status} · ${title}`, data: publication })
    }
    for (const item of items) {
      rows.push({
        id: item.ref ?? `pub/${publication.publication_id}#${item.item_id}`,
        label: `${publication.publication_id}#${item.item_id} · ${item.source_path ?? item.kind ?? ""} · ${title}`,
        ref: item.object_kind === "file" || item.source_path ? item.ref ?? `pub/${publication.publication_id}#${item.item_id}` : undefined,
        data: publication,
      })
    }
  }
  return rows
}

export function publicationDetail(publication: any): string {
  if (!publication) return ""
  const display = displayBlock(publication.display)
  return [
    `${publication.publication_id} · ${PUBLICATION_STATUS[publication.status] ?? publication.status}`,
    `节点：${publication.node_id ?? "未挂节点"} · ${publication.created_at ?? ""}`,
    ...(display.length ? display : []),
    "",
    `说明：${publication.summary ?? ""}`,
    (publication.gaps ?? []).length ? `缺口：${publication.gaps.join("；")}` : "",
    "",
    "Enter 打开选中的文件",
  ]
    .filter((line) => line !== "")
    .join("\n")
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
function sessionActivity(session: any): string {
  if (session.task_id) return `${session.label} · ${SPECIALIST_STATES[session.specialist_state] ?? session.specialist_state}`
  return pauseReason(session.pause_reason) || "活动"
}

export function sessionRows(page: any): Row[] {
  return (page?.items ?? []).map((session: any) => ({
    id: session.session_id,
    label: `${roleName(session.role)}${session.node_id ? ` ${session.node_id}` : ""}${session.detached ? "（已移出）" : ""} · ${sessionActivity(session)} · ${session.session_id}`,
    data: session,
  }))
}

const TASK_STATES: Record<string, string> = {
  queued: "排队",
  starting: "启动中",
  running: "运行中",
  finished: "已结束",
  failed: "失败",
  cancelled: "已取消",
  unverified: "待核实",
}

export function runtimeText(summary: any, session: any, tasks: any[] = []): string {
  const run = summary?.run ?? {}
  const lines = [
    `项目运行状态：${runState(run.state)}`,
    `研究主会话：${run.main_session_id ?? "无"}`,
    "",
    `节点任务（${tasks.length}）：`,
    ...(tasks.length
      ? tasks.map((task) => `  ${task.task_id} · ${task.node_id} · ${TASK_STATES[task.state] ?? task.state}${task.error ? ` · ${task.error}` : ""}`)
      : ["  无"]),
  ]
  if (session) {
    lines.push(
      "",
      `会话：${session.session_id}`,
      `角色：${roleName(session.role)}${session.detached ? "（已移出项目）" : ""}`,
      `节点：${session.node_id ?? "无"}`,
      ...(session.task_id ? [`专家任务：${session.task_id}`] : []),
      `状态：${sessionActivity(session)}`,
      `工作段收尾：${session.close_state ?? "无"}`,
    )
  }
  if (session) lines.push("", "Enter 打开这个会话")
  return lines.join("\n")
}

export function decodeChunk(content: any): string {
  if (!content) return ""
  if (content.kind === "directory") return "（这是一个目录）"
  if (content.kind === "binary") return `（二进制文件，${content.total_bytes ?? "?"} 字节，终端里不显示）`
  if (content.kind !== "text") return content.resolution?.message ?? "（无法读取）"
  return Buffer.from(content.chunk ?? "", "base64").toString("utf8")
}
