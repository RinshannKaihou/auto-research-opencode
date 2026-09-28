/**
 * Ledger access for OpenCode sessions: which sessions belong to which project
 * and in what role, project setup commands, the board, per-request research
 * memory and ledger tool calls. The autonomous loop lives in engine.ts.
 */
import { randomUUID } from "node:crypto"
import { existsSync, realpathSync } from "node:fs"
import { dirname, isAbsolute, join, resolve } from "node:path"
import type { StorageClient } from "./storage"
import { associatedStatus, unassociatedStatus } from "./format"

export const HOST_ID = "opencode"
const MEMORY_TIMEOUT_MS = 2000
const QUIET_STATES = new Set(["manual", "stopped", "complete"])

function canonical(path: string): string {
  try {
    return realpathSync(path)
  } catch {
    return resolve(path)
  }
}

/** Nearest directory at or above `directory` that holds a research ledger. */
export function findProjectRoot(directory: string): string | null {
  let current = resolve(directory)
  while (true) {
    if (existsSync(join(current, ".research", "state.sqlite3"))) return current
    const parent = dirname(current)
    if (parent === current) return null
    current = parent
  }
}

export interface CommandResult {
  message: string
  state?: unknown
  /** One line kept in the conversation, so the change stays visible after the dialog closes. */
  note?: string
  /** Session the TUI should show after the command. */
  sessionID?: string
}

/** What the session sidebar and status line show; nothing for sessions outside a project. */
export interface SessionResearch {
  associated: boolean
  goal?: string
  role?: string
  runState?: string
  pauseReason?: string | null
  nodeID?: string | null
  tasks?: { queued: number; running: number; finished: number }
  waiting?: number
  counts?: { nodes: number; publications: number; knowledge: number }
}

interface Membership {
  root: string
  role?: string
  node?: string
}

export class Research {
  private members = new Map<string, Membership>()
  private loading: Promise<void> | null = null
  private memoryCache = new Map<string, string>()

  /**
   * OpenCode loads one plugin instance per directory and every instance sees
   * every session event. An instance therefore manages only the project rooted
   * exactly at its own directory; elsewhere it offers read-only views.
   */
  readonly scope: string

  constructor(
    readonly storage: StorageClient,
    scope: string,
    private readonly registryExists: () => boolean,
    private readonly changed: (root: string) => void = () => {},
  ) {
    this.scope = canonical(scope)
  }

  inScope(root: string): boolean {
    return canonical(root) === this.scope
  }

  requireScope(root: string): void {
    if (!this.inScope(root)) {
      throw new Error(`研究项目在 ${root}；请在这个目录启动 OpenCode 后再操作（当前在 ${this.scope}）。`)
    }
  }

  /** Loads this host's sessions and their roles once; with no registry there is nothing to load. */
  ready(): Promise<void> {
    this.loading ??= this.registryExists() ? this.refresh().catch(() => {}) : Promise.resolve()
    return this.loading
  }

  private async refresh(): Promise<void> {
    const rows = await this.storage.request<{ session_id: string; root: string }[]>("host_sessions", {
      host_id: HOST_ID,
    })
    const scoped = rows.filter((item) => this.inScope(item.root))
    for (const root of new Set(scoped.map((row) => row.root))) {
      const sessions = await this.sessionRows(root).catch(() => new Map<string, any>())
      for (const row of scoped.filter((item) => item.root === root)) {
        const session = sessions.get(row.session_id)
        this.members.set(row.session_id, { root, role: session?.role, node: session?.node_id ?? undefined })
      }
    }
  }

  /**
   * A project's workflow sessions with their roles, read without a session
   * identity: blind reviewers may not read project status, yet their role must
   * survive a restart.
   */
  private async sessionRows(root: string): Promise<Map<string, any>> {
    const sessions = new Map<string, any>()
    let cursor: Record<string, unknown> | null = null
    do {
      const page: any = await this.storage.request("project_read", {
        root, view: "page", collection: "sessions", limit: 100, ...(cursor ?? {}),
      })
      for (const item of page.items ?? []) sessions.set(item.session_id, item)
      cursor = page.cursor ?? null
    } while (cursor)
    return sessions
  }

  isAssociated(sessionID: string): boolean {
    return this.members.has(sessionID)
  }

  roleOf(sessionID: string): string | undefined {
    return this.members.get(sessionID)?.role
  }

  rootOf(sessionID: string): string | undefined {
    return this.members.get(sessionID)?.root
  }

  nodeOf(sessionID: string): string | undefined {
    return this.members.get(sessionID)?.node
  }

  roots(): string[] {
    return [...new Set([...this.members.values()].map((member) => member.root))]
  }

  sessionsIn(root: string): string[] {
    return [...this.members].filter(([, member]) => member.root === root).map(([sessionID]) => sessionID)
  }

  register(sessionID: string, root: string, role: string, node?: string): void {
    this.members.set(sessionID, { root, role, node })
  }

  forget(sessionID: string): void {
    this.members.delete(sessionID)
    this.memoryCache.delete(sessionID)
  }

  notifyChanged(root: string): void {
    this.changed(root)
  }

  call<T = any>(method: string, sessionID: string, fields: Record<string, unknown> = {}, operationId?: string) {
    return this.storage.request<T>(
      method,
      { host_id: HOST_ID, session_id: sessionID, ...fields },
      (operationId ?? `${sessionID}:${method}:${randomUUID()}`).slice(0, 256),
    )
  }

  requireAssociated(sessionID: string): string {
    const root = this.rootOf(sessionID)
    if (!root) throw new Error("本会话没有关联研究项目。用 /research auto <目标> 开始，或 /research takeover 接管。")
    return root
  }

  async status(sessionID: string | null, directory: string): Promise<CommandResult> {
    await this.ready()
    if (sessionID && this.roleOf(sessionID) === "specialist") {
      const node = this.nodeOf(sessionID)
      return { message: `本会话是${node ? `节点 ${node} 的` : ""}专家会话，只读；它的报告由委派它的节点执行会话收回。` }
    }
    if (sessionID && this.isAssociated(sessionID)) {
      const state = await this.call("status", sessionID)
      return { message: associatedStatus(state), state }
    }
    const root = findProjectRoot(directory)
    const summary = root ? await this.board(directory, "summary") : null
    return { message: unassociatedStatus(root, summary, sessionID !== null) }
  }

  /** Creates the project with this session as its main session; nothing starts yet. */
  async init(sessionID: string, directory: string, goal: string): Promise<CommandResult> {
    await this.ready()
    if (!goal.trim()) throw new Error("用法：/research init <研究目标>")
    if (this.isAssociated(sessionID)) throw new Error("本会话已经关联了研究项目；用 /research status 查看。")
    const existing = findProjectRoot(directory)
    if (existing) {
      throw new Error(`${existing} 已有研究项目。用 /research auto 继续，或 /research takeover 接管。`)
    }
    this.requireScope(directory)
    const state = await this.call("open", sessionID, { root: directory, cwd: directory, goal: goal.trim() })
    this.register(sessionID, state.project_root, "main")
    this.changed(state.project_root)
    return {
      message: `已建立研究项目，本会话是研究主会话。\n用 /research auto 开始自主研究。\n\n${associatedStatus(state)}`,
      state,
      note: `【Research】已建立研究项目「${goal.trim()}」，本会话是研究主会话。`,
    }
  }

  async takeover(sessionID: string, directory: string): Promise<CommandResult> {
    await this.ready()
    const root = findProjectRoot(directory)
    if (!root) throw new Error("当前目录及上级目录里没有研究项目。")
    this.requireScope(root)
    const summary = await this.board(directory, "summary")
    const run = summary?.run
    if (run?.main_session_id === sessionID && this.isAssociated(sessionID)) {
      return { message: "本会话已经是这个项目的研究主会话。" }
    }
    if (run && !QUIET_STATES.has(run.state)) {
      throw new Error(`项目运行状态是「${run.state}」，请先停止项目（/research stop）再接管。`)
    }
    const state = await this.call("takeover_main", sessionID, { root, cwd: directory })
    for (const session of this.sessionsIn(root)) {
      if (session !== sessionID && this.roleOf(session) === "main") this.forget(session)
    }
    this.register(sessionID, state.project_root, "main")
    this.changed(state.project_root)
    return {
      message: `已接管，本会话现在是研究主会话。\n\n${associatedStatus(state)}`,
      state,
      note: `【Research】本会话已接管研究项目「${state.project?.goal ?? ""}」，成为研究主会话。`,
    }
  }

  async detach(sessionID: string): Promise<CommandResult> {
    await this.ready()
    const root = this.requireAssociated(sessionID)
    const current = await this.call("status", sessionID)
    if (this.roleOf(sessionID) === "main" && !QUIET_STATES.has(current.workflow?.run?.state)) {
      throw new Error("研究还在进行；先用 /research stop 停止项目，再移出主会话。")
    }
    if (current.attempt) await this.call("finish", sessionID, { state: "stopped", details: { reason: "detach" } })
    await this.call("workflow", sessionID, { action: "session", fields: { detached: 1, pause_reason: "detached" } })
    await this.call("detach", sessionID)
    this.forget(sessionID)
    this.changed(root)
    return {
      message: "已把本会话移出研究项目；对话和已发布的材料都保留。",
      note: "【Research】本会话已移出研究项目。",
    }
  }

  async guidance(sessionID: string, directory: string, path?: string, version?: string): Promise<CommandResult> {
    await this.ready()
    this.requireAssociated(sessionID)
    if (!path) {
      const state = await this.call("guidance_status", sessionID)
      return { message: JSON.stringify(state, null, 2), state }
    }
    const absolute = isAbsolute(path) ? path : resolve(directory, path)
    const state = await this.call("guidance_register", sessionID, { path: absolute, ...(version ? { version } : {}) })
    return { message: `已登记科研指导：${absolute}`, state }
  }

  /** Read-only project views located from a directory; no session or association needed. */
  async board(directory: string, view: string, fields: Record<string, unknown> = {}): Promise<any> {
    const root = findProjectRoot(directory)
    if (!root) return { project: null, project_root: null }
    return this.storage.request("project_read", { ...fields, root, view })
  }

  /** One ledger tool call; the operation ID makes a retried call replay its first result. */
  async tool(
    call: { sessionID: string; messageID: string; callID: string; name: string },
    method: string,
    fields: Record<string, unknown>,
    writes: boolean,
  ): Promise<unknown> {
    await this.ready()
    const root = this.requireAssociated(call.sessionID)
    const value = await this.call(
      method,
      call.sessionID,
      { ...fields, model_call: true },
      `${call.sessionID}:${call.messageID}:${call.callID}:${call.name}`,
    )
    if (writes) this.changed(root)
    return value
  }

  async sessionState(sessionID: string): Promise<SessionResearch> {
    if (!this.isAssociated(sessionID)) return { associated: false }
    if (this.roleOf(sessionID) === "specialist") {
      // Read by root: a blind reviewer may not read project status itself.
      const summary = await this.board(this.rootOf(sessionID)!, "summary")
      return {
        associated: true,
        goal: summary.project?.goal,
        role: "specialist",
        runState: summary.run?.state,
        pauseReason: null,
        nodeID: this.nodeOf(sessionID) ?? null,
        counts: {
          nodes: summary.counts?.nodes ?? 0,
          publications: summary.counts?.publications ?? 0,
          knowledge: summary.counts?.knowledge ?? 0,
        },
      }
    }
    const state = await this.call("status", sessionID)
    const counts = state.counts ?? {}
    // Ended tasks leave the control view; count the project's task list instead.
    const root = this.rootOf(sessionID)!
    const page = await this.storage
      .request<{ items: { state: string }[] }>("project_read", { root, view: "page", collection: "tasks", limit: 100 })
      .catch(() => ({ items: [] }))
    const mine = state.workflow?.session?.role === "main" ? page.items : []
    return {
      associated: true,
      goal: state.project?.goal,
      role: state.workflow?.session?.role,
      runState: state.workflow?.run?.state,
      pauseReason: state.workflow?.session?.pause_reason ?? null,
      nodeID: state.attempt?.node_id ?? state.workflow?.session?.node_id ?? null,
      tasks: {
        queued: mine.filter((task) => task.state === "queued").length,
        running: mine.filter((task) => ["starting", "running"].includes(task.state)).length,
        finished: mine.filter((task) => task.state === "finished").length,
      },
      waiting: (state.workflow?.session?.waiting ?? []).length,
      counts: { nodes: counts.nodes ?? 0, publications: counts.publications ?? 0, knowledge: counts.knowledge ?? 0 },
    }
  }

  /**
   * The research context for one model request, or null for sessions outside
   * a project. A slow ledger falls back to the last context, marked stale.
   */
  async memory(sessionID: string): Promise<string | null> {
    if (!this.isAssociated(sessionID)) return null
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const view = await Promise.race([
        this.call("memory_context", sessionID, { max_chars: 12000, profile: "opencode-auto" }),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error("research memory timeout")), MEMORY_TIMEOUT_MS)
        }),
      ])
      const text = `Research context:\n${view.text}`
      this.memoryCache.set(sessionID, text)
      void this.call("context_record", sessionID, {
        fields: {
          body: view.text,
          source_sequence: view.source_sequence,
          purpose: "primary",
          policy_version: "memory-v2",
          dependencies: view.dependencies ?? [],
          selection: { source_digest: view.source_digest, omitted: view.omitted ?? [] },
        },
      }).catch(() => {})
      return text
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (/not associated/i.test(message)) {
        this.forget(sessionID)
        return null
      }
      const cached = this.memoryCache.get(sessionID)
      if (cached) return `${cached}\n\n[Research memory is stale: storage did not answer in time.]`
      return "Research context is unavailable for this session right now. Continue carefully; use research tools only when needed."
    } finally {
      clearTimeout(timer)
    }
  }
}
