/**
 * Ledger access for OpenCode sessions: which sessions belong to which project
 * and in what role, project setup commands, the board, per-request research
 * memory and ledger tool calls. The autonomous loop lives in engine.ts.
 */
import { randomUUID, createHash } from "node:crypto"
import { existsSync, realpathSync, statSync } from "node:fs"
import { dirname, isAbsolute, join, resolve } from "node:path"
import type { StorageClient } from "./storage"
import { CheckpointDelivery } from './checkpoint-delivery'
import { EvidenceDelivery } from './evidence-delivery'
import { associatedStatus, unassociatedStatus } from "./format"
import packageInfo from '../package.json'

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

/** Longest user guidance the ledger keeps, in characters. */
export const GUIDANCE_LIMIT = 2000

/** The note that files a user's words to a research session as guidance in the open work segment. */
export function guidanceNote(text: string): { body: string; kind: string } {
  return { body: `用户指导：${text.slice(0, GUIDANCE_LIMIT)}`, kind: "correction" }
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
  externalRunning?: number
  externalUnverified?: number
  eventRecovery?: { state: string; held: number; unresolved: number; message?: string }
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
  readonly checkpoints = new CheckpointDelivery()
  readonly evidenceDelivery = new EvidenceDelivery()
  private members = new Map<string, Membership>()
  private nonmembers = new Map<string, { stamp: string; at: number }>()
  private controllers = new Map<string, { epoch: string; binding: string; fence: number }>()
  private conclusionContracts = new Map<string,string>()
  private executionFor: (sessionID: string) => string | undefined = () => undefined
  private readonly memoryObservations = { since: new Date().toISOString(), requested: 0, generated: 0, recorded: 0, recordFailed: 0, cached: 0 }
  private memoryGeneration = 0
  private loading: Promise<void> | null = null
  private contract: Promise<void> | null = null
  private receipts = new Map<string,{key:string;value:Promise<{base:string}>}>()
  private memoryCache = new Map<string, { text: string; policy_version: string; source_sequence: number;
    checkpoint_snapshot?:Record<string,any>;checkpoint_owner?:Record<string,any> }>()

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
    readonly instanceID = randomUUID(),
    private readonly forgotten: (session:string)=>void = ()=>{},
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
    if (!this.registryExists()) return Promise.resolve()
    this.loading ??= this.refresh().catch(error => {
      this.loading = null
      throw error
    })
    return this.loading
  }

  /** Resolve this session afresh; another plugin attachment may have registered it. */
  async confirmMembership(sessionID: string): Promise<void> {
    await this.ready()
    if (!this.registryExists()) {
      if (this.members.has(sessionID) || findProjectRoot(this.scope) === this.scope) {
        throw new Error('Research membership registry is unavailable')
      }
      return
    }
    const stamp = [this.storage.registryPath, this.storage.registryPath + '-wal'].map(path => {
      try { const info = statSync(path); return `${info.mtimeMs}:${info.ctimeMs}:${info.size}` } catch { return '-' }
    }).join('|')
    const negative = this.nonmembers.get(sessionID)
    if (!this.members.has(sessionID) && negative?.stamp === stamp && Date.now() - negative.at < 1000) return
    const value = await this.storage.request<any>('session_membership', { host_id: HOST_ID, session_id: sessionID })
    if (!value.associated || !this.inScope(value.root)) {
      if (this.members.has(sessionID)) this.forget(sessionID)
      if (this.nonmembers.size >= 2048) this.nonmembers.delete(this.nonmembers.keys().next().value!)
      this.nonmembers.set(sessionID, { stamp, at: Date.now() })
      return
    }
    this.nonmembers.delete(sessionID)
    const row = value.session
    this.members.set(sessionID, { root: value.root, role: row?.detached ? undefined : row?.role, node: row?.node_id ?? undefined })
    const main = value.main
    if (main) this.members.set(main.session_id, { root: value.root, role: main.role, node: main.node_id ?? undefined })
  }

  ensureContract(): Promise<void> {
    this.contract ??= this.storage.request('capabilities').then(value => {
      if (value.memory_write_contract!=='r2-v1' || value.runtime_control_contract !== 'controller-launch-v1' ||
          value.exploration_contract !== 'exploration-v1' || value.multiroute_contract !== 'multiroute-v1' || value.evidence_contract !== 'evidence-v1' ||
          value.evidence_read !== 'snapshot-delivery-v2') throw Object.assign(
        new Error('Research backend contract mismatch: install the matching plugin and bundled Python package.'),
        {code:'research_contract_mismatch'})
    }).catch(error=>{this.contract=null;throw error})
    return this.contract
  }

  private async refresh(): Promise<void> {
    const rows = await this.storage.request<{ session_id: string; root: string }[]>("host_sessions", {
      host_id: HOST_ID,
    })
    const scoped = rows.filter((item) => this.inScope(item.root))
    for (const root of new Set(scoped.map((row) => row.root))) {
      const sessions = await this.sessionRows(root)
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

  conclusionContractOf(sessionID:string): string | undefined {
    const root=this.rootOf(sessionID)
    return root ? this.conclusionContracts.get(root) : undefined
  }

  roots(): string[] {
    return [...new Set([...this.members.values()].map((member) => member.root))]
  }

  sessionsIn(root: string): string[] {
    return [...this.members].filter(([, member]) => member.root === root).map(([sessionID]) => sessionID)
  }

  register(sessionID: string, root: string, role: string, node?: string): void {
    this.nonmembers.delete(sessionID)
    this.members.set(sessionID, { root, role, node })
  }

  forget(sessionID: string): void {
    this.nonmembers.delete(sessionID)
    this.members.delete(sessionID)
    this.memoryCache.delete(sessionID)
    this.checkpoints.forget(sessionID)
    this.evidenceDelivery.forget(sessionID)
    this.receipts.delete(sessionID)
    this.forgotten(sessionID)
  }

  notifyChanged(root: string): void {
    this.changed(root)
  }

  setController(root: string, owner: { epoch: string; binding: string; fence: number }): void {
    this.controllers.set(canonical(root), owner)
  }

  setExecutionResolver(resolve: (sessionID: string) => string | undefined): void {
    this.executionFor = resolve
  }

  call<T = any>(method: string, sessionID: string, fields: Record<string, unknown> = {}, operationId?: string) {
    const root = this.rootOf(sessionID) ?? (typeof fields.root === 'string' ? fields.root : undefined)
    const owner = root ? this.controllers.get(canonical(root)) : undefined
    const execution = this.executionFor(sessionID)
    return this.storage.request<T>(
      method,
      { host_id: HOST_ID, session_id: sessionID, ...(execution ? { execution_token: execution } : {}), ...fields,
        ...(owner ? { control_owner: owner } : {}), plugin_instance_id: this.instanceID },
      (operationId ?? `${sessionID}:${method}:${randomUUID()}`).slice(0, 256),
    ).then(value => {
      if (root) {
        const result=value as any
        const contract=method==='status' ? result?.project?.config?.conclusion_contract
          : ['memory_context','exploration_policy','conclusion_contract'].includes(method) ? result?.conclusion_contract : undefined
        if (typeof contract==='string') this.conclusionContracts.set(root,contract)
        if (method==='workflow' && fields.action==='run' && (fields.fields as any)?.new_generation) this.conclusionContracts.delete(root)
      }
      return value
    })
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

  async contextPolicy(sessionID: string, policy?: string | null): Promise<CommandResult> {
    await this.ready()
    const root = this.requireAssociated(sessionID)
    this.requireScope(root)
    if (policy && policy !== "legacy" && policy !== "r1") throw new Error("用法：/research context-policy legacy|r1")
    const state = await this.call("context_policy", sessionID,
      policy ? { policy: policy === "legacy" ? "memory-v2" : "memory-r1-v1" } : {})
    if (policy) {
      this.memoryGeneration++
      for (const session of this.sessionsIn(root)) this.memoryCache.delete(session)
      this.changed(root)
    }
    const labels: Record<string, string> = { "memory-v2": "Legacy (memory-v2)", "memory-r1-v1": "R1 (memory-r1-v1)" }
    const last = state.last_recorded
    const recorded = last ? `${labels[last.policy_version] ?? last.policy_version} · ${last.created_at}` : "暂无保存记录"
    return { message: `当前上下文策略：${labels[state.policy_version]}\n最近一次记录：${recorded}\n后续模型请求生效；不会重算旧回答或重开已结束的项目。`, state: { ...state, observations: { ...this.memoryObservations } } }
  }

  async explorationPolicy(sessionID: string, mode?: string | null): Promise<CommandResult> {
    await this.ready()
    const root = this.requireAssociated(sessionID)
    this.requireScope(root)
    const state = await this.call<any>("exploration_policy", sessionID, { mode: mode ?? null })
    if (mode) {
      this.memoryGeneration++
      for (const session of this.sessionsIn(root)) this.memoryCache.delete(session)
      this.changed(root)
    }
    return { message: `探索策略：${state.exploration_policy}${state.effective_for==='next_generation' ? '（从下一研究代生效）' : ''}\n上下文策略：${state.context_policy}\n结项契约：${state.conclusion_contract}（研究代 ${state.generation}）`, state }
  }

  async conclusionContract(sessionID: string, contract?: string | null): Promise<CommandResult> {
    await this.ready()
    const root = this.requireAssociated(sessionID)
    this.requireScope(root)
    const state = await this.call<any>('conclusion_contract',sessionID,{contract:contract ?? null})
    if (contract) this.changed(root)
    return {message:`研究代 ${state.generation} 的结项契约：${state.conclusion_contract}`,state}
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
    const result = await this.storage.request("project_read", { ...fields, root, view })
    return view === "context_preview" ? { ...result, observations: this.inScope(root) ? { ...this.memoryObservations } : null } : result
  }

  /** One ledger tool call; the operation ID makes a retried call replay its first result. */
  async tool(
    call: { sessionID: string; messageID: string; callID: string; name: string },
    method: string,
    fields: Record<string, unknown>,
    writes: boolean,
  ): Promise<unknown> {
    const checkpoint_base = method==='memory_write' && fields.action==='checkpoint'
      ? this.checkpoints.forCall(call.sessionID,call.messageID,call.callID) : undefined
    await this.ready()
    const root = this.requireAssociated(call.sessionID)
    let value = await this.call(
      method,
      call.sessionID,
      { ...fields, model_call: true, ...(checkpoint_base ? {checkpoint_base} : {}) },
      `${call.sessionID}:${call.messageID}:${call.callID}:${call.name}`,
    )
    if (method === 'query' && fields.collection === 'checkpoint') {
      const receipt = await this.call('checkpoint_observed', call.sessionID, {
        snapshot: value.snapshot, origin: 'query',
      },`observed:${createHash('sha256').update(JSON.stringify([this.instanceID,call,value.snapshot])).digest('hex')}`)
      value = { ...value, ...receipt }
    }
    if (value && typeof value === 'object') this.checkpoints.result(call.sessionID,call.callID,call.name,value)
    if (value && typeof value === 'object') this.evidenceDelivery.result(call.sessionID,call.callID,call.name,value)
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
    const external = state.workflow?.session?.role === 'node_core'
      ? await this.call<any>('runtime_shell_status', sessionID) : null
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
      externalRunning: external?.running ?? 0,
      externalUnverified: external?.unverified ?? 0,
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
    const generation = this.memoryGeneration
    this.memoryObservations.requested++
    try {
      const view = await Promise.race([
        this.call("memory_context", sessionID, { max_chars: 12000, profile: "opencode-auto" }),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error("research memory timeout")), MEMORY_TIMEOUT_MS)
        }),
      ])
      if (generation !== this.memoryGeneration) throw new Error("Research context policy changed during read")
      const text = `Research context:\n${view.text}`
      this.memoryObservations.generated++
      this.memoryCache.set(sessionID, { text, policy_version: view.policy_version, source_sequence: view.source_sequence,
        checkpoint_snapshot:view.checkpoint_snapshot,checkpoint_owner:view.checkpoint_owner })
      void this.call("context_record", sessionID, {
        fields: {
          body: view.text,
          source_sequence: view.source_sequence,
          purpose: "primary",
          policy_version: view.policy_version,
          dependencies: view.dependencies ?? [],
          selection: { source_digest: view.source_digest, context_mode: view.context_mode, omitted: view.omitted ?? [],
            exploration_policy: view.exploration_policy, conclusion_contract: view.conclusion_contract,
            exploration_context: view.exploration_context, plugin_version:packageInfo.version,
            selected: view.selection, coverage: view.coverage, block_chars: view.block_chars, truncation: view.truncation },
        },
      }).then(() => { this.memoryObservations.recorded++ }, () => { this.memoryObservations.recordFailed++ })
      return text
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (/not associated/i.test(message)) {
        this.forget(sessionID)
        return null
      }
      const cached = this.memoryCache.get(sessionID)
      if (cached) this.memoryObservations.cached++
      if (cached) {
        // Cached scientific context is useful, but its old control instructions cannot authorize work.
        const facts = cached.text.replace(/^## (?:role_instructions|control_instructions)\n[^\n]*(?:\n|$)/gm, "")
        return `${facts}\n\n[Research memory is stale. Execution control is unavailable; do not start research or assume it is running.]`
      }
      return "Research context and execution control are unavailable. Do not start research or assume it is running."
    } finally {
      clearTimeout(timer)
    }
  }

  async delivered(sessionID:string,text:string,messages:any[],timeoutMs=MEMORY_TIMEOUT_MS): Promise<void> {
    const deadline=performance.now()+Math.max(0,timeoutMs)
    this.checkpoints.clear(sessionID)
    const cached = this.memoryCache.get(sessionID)
    const matches = cached && (text===cached.text || text===`${cached.text}\n\n[Research memory is stale: storage did not answer in time.]`)
    let context
    if (matches && cached.checkpoint_snapshot) {
      const snapshot=cached.checkpoint_snapshot
      const {source_sequence,...identity}=snapshot
      const key=createHash('sha256').update(JSON.stringify([this.instanceID,identity])).digest('hex')
      let receipt=this.receipts.get(sessionID)
      if(receipt?.key!==key){
        const operation=createHash('sha256').update(JSON.stringify([this.instanceID,snapshot])).digest('hex')
        receipt={key,value:this.call('checkpoint_observed',sessionID,{snapshot,origin:'context'},`observed:${operation}`)}
        this.receipts.set(sessionID,receipt)
        receipt.value.catch(()=>{if(this.receipts.get(sessionID)===receipt)this.receipts.delete(sessionID)})
      }
      let timer:ReturnType<typeof setTimeout> | undefined
      try{
        const read=await Promise.race([receipt.value,new Promise<undefined>(resolve=>{timer=setTimeout(()=>resolve(undefined),Math.max(0,timeoutMs))})])
        if(read) context={base:read.base,snapshot}
      } catch {
        // No acknowledged base is delivered; the next write must explicitly read.
      } finally {clearTimeout(timer)}
    }
    this.checkpoints.request(sessionID,messages,matches ? cached.checkpoint_owner : undefined,context)
  }

  async observeEvidence(sessionID:string,messages:any[],timeoutMs=MEMORY_TIMEOUT_MS): Promise<string[]> {
    return this.evidenceDelivery.request(sessionID,messages,async (pages,onBatch) => {
      const batches = Array.from({length:Math.ceil(pages.length/100)},(_,i)=>pages.slice(i*100,(i+1)*100))
      const requestID=randomUUID()
      await Promise.all(batches.map(async (batch,index) => {
        try {
          const result=await this.call<{receipt_ids:string[];rejected?:{reason:string}[]}>('evidence_observed',sessionID,
            {pages:batch},`evidence-observed:${requestID}:${index}`)
          onBatch(result)
        } catch {
          onBatch({receipt_ids:[],rejected:[{reason:'backend_unavailable'}]})
        }
      }))
    },Math.max(0,timeoutMs))
  }

  async confirmMaterialDelivery(sessionID:string,messageID:string): Promise<void> {
    if (this.roleOf(sessionID)!=='main') return
    await this.evidenceDelivery.confirmMaterials(sessionID,messageID,async (pages,onBatch) => {
      const requestID=randomUUID()
      const batches=Array.from({length:Math.ceil(pages.length/100)},(_,i)=>pages.slice(i*100,(i+1)*100))
      await Promise.all(batches.map(async (batch,index) => {
        let result:{receipt_ids:string[];rejected:{index:number;reason:string}[]}
        try {
          const response=await this.call<{receipt_ids:string[];rejected?:{index:number;reason:string}[]}>(
            'evidence_observed',sessionID,{pages:batch},`material-observed:${requestID}:${index}`)
          result={receipt_ids:response.receipt_ids,
            rejected:(response.rejected ?? []).map(item=>({...item,index:item.index+index*100}))}
        } catch {
          result={receipt_ids:[],rejected:batch.map((_,offset)=>({index:index*100+offset,reason:'backend_unavailable'}))}
        }
        onBatch(batch.map((_,offset)=>index*100+offset),result)
      }))
    })
  }
}
