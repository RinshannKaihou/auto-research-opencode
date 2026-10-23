/**
 * Research-process view: node titles, a git-style rail over the dependency
 * graph, and three separate state channels. Execution (did the work run),
 * delivery (what was published) and knowledge (what was learned) are derived
 * independently and never merged into a single "done".
 */
import { KNOWLEDGE_KINDS, RELATION_TYPES } from "./model"

export type Execution = "pending" | "queued" | "running" | "paused" | "finished" | "failed" | "stopped" | "unknown"

export const EXECUTION: Record<Execution, { glyph: string; label: string }> = {
  pending: { glyph: "○", label: "未开始" },
  queued: { glyph: "○", label: "排队中" },
  running: { glyph: "◐", label: "运行中" },
  paused: { glyph: "‖", label: "已暂停" },
  finished: { glyph: "●", label: "已结束" },
  failed: { glyph: "✕", label: "失败" },
  stopped: { glyph: "■", label: "已停止" },
  unknown: { glyph: "?", label: "待核实" },
}

export interface Edge {
  from: string
  to: string
  type: string
  rationale: string
  inputs: number
}

export interface NodeView {
  id: string
  title: string
  node: any
  execution: Execution
  /** Wall time of the latest attempt; running attempts count up to `now`. */
  duration?: number
  /** Latest publication of the node, if any. */
  delivery?: { id: string; status: string; items: number }
  knowledge: any[]
  upstream: Edge[]
  downstream: Edge[]
  attempts: any[]
}

export interface RailLine {
  kind: "node" | "meta" | "link"
  /** Two cells per lane: the lane glyph and the cell to its right. */
  rail: string
  /** Index into `ProcessView.nodes` for node and meta lines. */
  node?: number
  /** Cell of the node glyph within `rail`, for node lines. */
  column?: number
}

export interface ProcessView {
  nodes: NodeView[]
  rail: RailLine[]
  /** Rail width in cells. */
  width: number
}

const QUIET_PAUSES = new Set(["wait", "segment_complete", "finished", "complete"])

const WIDE = /[ᄀ-ᅟ⺀-〾ぁ-㏿㐀-䶿一-鿿ꀀ-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]|[\u{1f300}-\u{1faff}\u{20000}-\u{3fffd}]/u

export function displayWidth(text: string): number {
  let width = 0
  for (const char of text) width += WIDE.test(char) ? 2 : 1
  return width
}

/** Cuts `text` to `width` terminal cells, ending with "…" when shortened. */
export function fit(text: string, width: number): string {
  if (width <= 0) return ""
  if (displayWidth(text) <= width) return text
  let out = ""
  let used = 0
  for (const char of text) {
    const cells = WIDE.test(char) ? 2 : 1
    if (used + cells > width - 1) break
    out += char
    used += cells
  }
  return `${out}…`
}

export function formatDuration(ms: number): string {
  const minutes = Math.max(1, Math.round(ms / 60_000))
  if (minutes < 60) return `${minutes} 分钟`
  const hours = ms / 3_600_000
  return hours < 48 ? `${hours.toFixed(1)} 小时` : `${(hours / 24).toFixed(1)} 天`
}

export function relationName(type: string): string {
  return RELATION_TYPES[type] ?? type
}

export function kindName(kind: string): string {
  return KNOWLEDGE_KINDS[kind] ?? kind
}

/**
 * Nodes have no title of their own yet. A published display title names the
 * work best; otherwise the question's first clause stands in.
 */
export function nodeTitle(node: any, publication?: any): string {
  const display = typeof publication?.display?.title === "string" ? publication.display.title.trim() : ""
  if (display) {
    const stripped = display.replace(new RegExp(`^${node.node_id}\\s*[：:·\\-]?\\s*`), "").trim()
    return stripped || display
  }
  const question = String(node.question ?? "").replace(/\s+/g, " ").trim()
  const cut = question.search(/[：？。?]/)
  return cut >= 8 ? question.slice(0, cut) : question
}

function executionOf(attempt: any, task: any, session: any): Execution {
  const paused = session?.pause_reason && !QUIET_PAUSES.has(session.pause_reason)
  if (task?.state === "queued") return "queued"
  if (["starting", "running"].includes(task?.state) || attempt?.state === "open") return paused ? "paused" : "running"
  if (task?.state === "failed") return "failed"
  if (attempt?.state === "finished" || task?.state === "finished") return "finished"
  if (attempt?.state === "stopped" || task?.state === "cancelled") return "stopped"
  if (attempt?.state === "unknown" || task?.state === "unverified") return "unknown"
  return "pending"
}

/** Kahn's order with ledger order as the tie-break; a cycle falls back to ledger order. */
function order(ids: string[], edges: Edge[]): string[] {
  const indegree = new Map(ids.map((id) => [id, 0]))
  for (const edge of edges) if (indegree.has(edge.to) && indegree.has(edge.from)) indegree.set(edge.to, indegree.get(edge.to)! + 1)
  const result: string[] = []
  const ready = ids.filter((id) => indegree.get(id) === 0)
  while (ready.length) {
    const id = ready.shift()!
    result.push(id)
    for (const edge of edges.filter((e) => e.from === id && indegree.has(e.to))) {
      indegree.set(edge.to, indegree.get(edge.to)! - 1)
      if (indegree.get(edge.to) === 0) {
        ready.push(edge.to)
        ready.sort((a, b) => ids.indexOf(a) - ids.indexOf(b))
      }
    }
  }
  return result.length === ids.length ? result : ids
}

/** Drops edges implied by a longer path, so the rail shows the main lineage only. */
function reduce(ids: string[], edges: Edge[]): Map<string, string[]> {
  const next = new Map(ids.map((id) => [id, [] as string[]]))
  for (const edge of edges) if (next.has(edge.from) && next.has(edge.to) && !next.get(edge.from)!.includes(edge.to)) next.get(edge.from)!.push(edge.to)
  const reachable = (start: string, target: string, skip: string): boolean => {
    const stack = next.get(start)!.filter((id) => id !== skip)
    const seen = new Set<string>()
    while (stack.length) {
      const id = stack.pop()!
      if (id === target) return true
      if (seen.has(id)) continue
      seen.add(id)
      stack.push(...next.get(id)!)
    }
    return false
  }
  const reduced = new Map<string, string[]>()
  for (const [from, targets] of next) reduced.set(from, targets.filter((to) => !reachable(from, to, to)))
  return reduced
}

/** Lanes run top to bottom like `git log --graph`: a branch opens lanes, a merge closes them. */
export function railLayout(ids: string[], successors: Map<string, string[]>): { rail: RailLine[]; width: number } {
  const lanes: (string | null)[] = []
  const rail: RailLine[] = []
  const idle = (): string[] => lanes.map((lane) => (lane ? "│ " : "  "))
  // One row joining `column` to the lanes in `joined`, all of which lie to its right.
  const join = (column: number, joined: number[], end: string, middle: string) => {
    const last = joined[joined.length - 1]!
    const cells = idle()
    for (let lane = column; lane <= last; lane++) {
      if (lane === column) cells[lane] = "├─"
      else if (joined.includes(lane)) cells[lane] = lane === last ? `${end} ` : `${middle}─`
      else cells[lane] = lanes[lane] ? "┼─" : "──"
    }
    rail.push({ kind: "link", rail: cells.join("") })
  }
  ids.forEach((id, index) => {
    const waiting = lanes.flatMap((lane, at) => (lane === id ? [at] : []))
    let column = waiting[0] ?? lanes.indexOf(null)
    if (column < 0) column = lanes.push(null) - 1
    if (waiting.length > 1) {
      join(column, waiting.slice(1), "╯", "┴")
      for (const lane of waiting.slice(1)) lanes[lane] = null
    }
    const cells = idle()
    cells[column] = "● "
    rail.push({ kind: "node", rail: cells.join(""), node: index, column: column * 2 })
    const targets = successors.get(id) ?? []
    lanes[column] = targets[0] ?? null
    rail.push({ kind: "meta", rail: idle().join(""), node: index })
    const opened = targets.slice(1).map((target) => {
      let lane = lanes.findIndex((value, at) => value === null && at > column)
      if (lane < 0) lane = lanes.push(null) - 1
      lanes[lane] = target
      return lane
    })
    if (opened.length) join(column, opened.sort((a, b) => a - b), "╮", "┬")
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop()
  })
  const width = Math.max(0, ...rail.map((line) => line.rail.trimEnd().length))
  return { rail: rail.map((line) => ({ ...line, rail: line.rail.slice(0, width).padEnd(width) })), width }
}

export function processView(
  data: {
    nodes: any[]
    dependencies: any[]
    attempts: any[]
    tasks: any[]
    sessions: any[]
    publications: any[]
    snapshots: any[]
    knowledge: any[]
  },
  now = Date.now(),
): ProcessView {
  const edges: Edge[] = data.dependencies.map((edge) => ({
    from: edge.predecessor_node_id,
    to: edge.successor_node_id,
    type: edge.relation_type,
    rationale: String(edge.rationale ?? "").replace(/\s+/g, " ").trim(),
    inputs: Array.isArray(edge.input_refs) ? edge.input_refs.length : 0,
  }))
  const ids = order(data.nodes.map((node) => node.node_id), edges)
  const nodeById = new Map(data.nodes.map((node) => [node.node_id, node]))
  const publicationNode = new Map(data.publications.map((publication) => [publication.publication_id, publication.node_id]))
  const attemptNode = new Map(data.attempts.map((attempt) => [attempt.attempt_id, attempt.node_id]))
  const snapshotNode = new Map(data.snapshots.map((snapshot) => [snapshot.snapshot_id, attemptNode.get(snapshot.attempt_id)]))
  // Knowledge belongs to a node directly, or through the publications and snapshots it cites.
  const knowledgeNodes = (item: any): Set<string> => {
    const owners = new Set<string>(item.node_id ? [item.node_id] : [])
    for (const ref of item.evidence_refs ?? []) {
      const publication = /^pub\/([^#/]+)/.exec(ref)?.[1]
      const snapshot = /^(?:snap\/)?(S-\d+)/.exec(ref)?.[1]
      const owner = publication ? publicationNode.get(publication) : snapshot ? snapshotNode.get(snapshot) : undefined
      if (owner) owners.add(owner)
    }
    return owners
  }
  const knowledgeByNode = new Map<string, any[]>()
  for (const item of data.knowledge) {
    for (const owner of knowledgeNodes(item)) knowledgeByNode.set(owner, [...(knowledgeByNode.get(owner) ?? []), item])
  }
  const nodes: NodeView[] = ids.map((id) => {
    const node = nodeById.get(id)
    const attempts = data.attempts
      .filter((attempt) => attempt.node_id === id)
      .sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)))
    const attempt = attempts[attempts.length - 1]
    const task = data.tasks.filter((item) => item.node_id === id).at(-1)
    const session = data.sessions.filter((item) => item.node_id === id && item.role === "node_core").at(-1)
    const publications = data.publications.filter((publication) => publication.node_id === id)
    const latest = publications.at(-1)
    const titled = [...publications].reverse().find((publication) => publication.display?.title)
    const started = attempt?.started_at ? Date.parse(attempt.started_at) : NaN
    const ended = attempt?.ended_at ? Date.parse(attempt.ended_at) : attempt?.state === "open" ? now : NaN
    return {
      id,
      title: nodeTitle(node, titled),
      node,
      execution: executionOf(attempt, task, session),
      duration: Number.isFinite(started) && Number.isFinite(ended) ? ended - started : undefined,
      delivery: latest
        ? {
            id: latest.publication_id,
            status: latest.status,
            items: (latest.items ?? []).filter((item: any) => item.item_id !== "__research_display").length,
          }
        : undefined,
      knowledge: knowledgeByNode.get(id) ?? [],
      upstream: edges.filter((edge) => edge.to === id),
      downstream: edges.filter((edge) => edge.from === id),
      attempts,
    }
  })
  const { rail, width } = railLayout(ids, reduce(ids, edges))
  return { nodes, rail, width }
}
