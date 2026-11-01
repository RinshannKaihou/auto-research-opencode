/**
 * The autonomous research loop. OpenCode ends every model turn with an
 * execution event; the engine then reads the ledger and decides whether the
 * session continues, waits, reports to its coordinator, starts the next node
 * or stops. It never decides research content: models do, and the ledger
 * records what they did.
 *
 * All research sessions live in the project root, so one plugin instance sees
 * every event. Decisions for one project run one at a time; helpers called
 * from inside a decision must not queue another decision for the same root.
 */
import type { CommandResult, Research } from "./research"
import { findProjectRoot, GUIDANCE_LIMIT, guidanceNote } from "./research"
import { randomUUID } from "node:crypto"
import type { SourceSnapshot } from './workspace-probe'
import type { RuntimeState } from './controller-lifecycle'
import { wasNotSent } from './host-submission'

/** The model a research run uses; autonomous turns name no model of their own. */
export interface ModelRef {
  id: string
  providerID: string
  variant?: string
}

/** An OpenCode permission rule; for one request the last matching rule wins. */
export interface PermissionRule {
  action: string
  resource: string
  effect: "allow" | "deny" | "ask"
}

export interface SessionHost {
  source?(sessionID:string):Promise<SourceSnapshot | undefined>
  create(title: string, directory: string, model?: ModelRef, permissions?: PermissionRule[], sessionID?: string): Promise<string>
  switchModel(sessionID: string, model: ModelRef): Promise<void>
  /** Adds a message the model reads as user text; `label` is what the TUI shows. */
  synthetic(sessionID: string, text: string, label: string, resume: boolean, messageID?: string, wakeIntentID?: string, launchIntentID?: string, specialistLaunchID?: string): Promise<void>
  interrupt(sessionID: string): Promise<void>
  /** Resolves once the session is idle again, however its turn ended. */
  wait(sessionID: string): Promise<void>
  /** The session's messages; the record of a finished turn ends with `{ type: "idle", outcome }`. */
  messages(sessionID: string): Promise<any[]>
  /** Read-only context during controller reconciliation, before it becomes ready. */
  recoveryMessages?(sessionID: string): Promise<any[]>
}

export interface ConclusionInput {
  summary: string
  final_ref: string
  draft_id?: string
  outcome: "answered" | "partial" | "unresolved"
  gaps: string[]
  review: { status: "unreviewed" | "partial" | "reviewed"; refs: string[]; limitations: string[] }
}

export type Outcome = "succeeded" | "failed" | "interrupted"

export const PREFIX = "【Research 自动推进】"
const IDLE_LIMIT = 3
const ACTIVE_TASK = new Set(["starting", "running", "stopping", "unverified"])
const LIVE_TASK = new Set(["queued", ...ACTIVE_TASK, "waiting"])
/** Node reports that end a wait; a publication notice is delivered without waking. */
const WAKING = new Set(["finished", "stopped", "failed", "unknown", "exploration_result"])
const NOTICE_OUTCOME: Record<string, string> = {
  finished: "已完成",
  stopped: "受阻停止",
  failed: "失败",
  unknown: "结束（状态未核实）",
  published: "发布了新材料",
  paused: "已暂停，等待处理",
}
/** Pauses that /research resume clears; wait and closed segments resolve themselves. */
const RESUMABLE = new Set(["project", "project_wait", "cold", "native_stop", "fault", "no_progress",'budget'])

export function oneLine(value: unknown, limit = 60): string {
  const text = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : ""
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

/** Rotate independent scope queues without letting a blocked resource hold up other scopes. */
export function fairTaskOrder(tasks: any[], afterScope?: string): any[] {
  const groups = new Map<string,any[]>()
  for (const task of tasks) {
    const scope = task.exploration_scope_id ?? '__ordinary__'
    if (!groups.has(scope)) groups.set(scope,[])
    groups.get(scope)!.push(task)
  }
  const scopes = [...groups.keys()]
  const after = afterScope ? scopes.indexOf(afterScope) : -1
  const order = after < 0 ? scopes : [...scopes.slice(after+1),...scopes.slice(0,after+1)]
  const result = []
  while (groups.size) for (const scope of order) {
    const queue = groups.get(scope)
    if (!queue) continue
    result.push(queue.shift())
    if (!queue.length) groups.delete(scope)
  }
  return result
}

export class Engine {
  /** In-memory rotation hint; durable queue order remains the restart fallback. */
  private lastScheduledScope: Map<string,string>
  private busy: Set<string>
  /** Interrupts the engine issued itself; OpenCode reports every interrupt as "user". */
  private ours: Set<string>
  private wrote: Map<string, boolean>
  private idle: Map<string, number>
  private turns: Map<string, string>
  private startedAt: Map<string, number>
  private startedEvents: Map<string, string>
  private pendingInbox: Map<string, { id: string; seq?: number }[]>
  private inferredStart: Set<string>
  private pendingStarts: RuntimeState['pendingStarts']
  private retried: Set<string>
  private queues: Map<string, Promise<unknown>>
  /** Per project: the model the user chose in the TUI when starting or resuming. */
  private models: Map<string, ModelRef>
  private shellInboxes: NonNullable<RuntimeState['shellInboxes']>
  private shellWake: NonNullable<RuntimeState['shellWake']>

  constructor(
    private readonly research: Research,
    private readonly host: SessionHost,
    private readonly concurrency = 1,
    private readonly runtime: RuntimeState,
    private readonly current: () => boolean,
    private readonly ensureController: (root: string, main: string) => Promise<void>,
    private readonly interruptSpecialists: (root: string) => Promise<void>,
  ) {
    this.busy = runtime.busy; this.ours = runtime.ours; this.wrote = runtime.wrote
    this.idle = runtime.idle; this.turns = runtime.turns; this.startedAt = runtime.startedAt; this.startedEvents = runtime.startedEvents
    this.lastScheduledScope = runtime.lastScheduledScope ??= new Map()
    this.pendingInbox = runtime.pendingInbox
    this.inferredStart = runtime.inferredStart
    this.pendingStarts = runtime.pendingStarts
    this.retried = runtime.retried; this.queues = runtime.queues; this.models = runtime.models
    this.shellInboxes = runtime.shellInboxes ??= new Map()
    this.shellWake = runtime.shellWake ??= new Map()
  }

  private serial<T>(root: string, work: () => Promise<T>): Promise<T> {
    const run = (this.queues.get(root) ?? Promise.resolve()).catch(() => {}).then(work)
    this.queues.set(root, run)
    return run
  }

  private call<T = any>(method: string, sessionID: string, fields: Record<string, unknown> = {}, operationId?: string) {
    if (!this.current()) return Promise.reject(new Error("Research controller attachment has been replaced"))
    return this.research.call<T>(method, sessionID, fields, operationId)
  }

  private state(sessionID: string): Promise<any> {
    return this.call("status", sessionID)
  }

  private async allTasks(sessionID: string, workflow: any): Promise<any[]> {
    const tasks = [...(workflow?.tasks ?? [])]
    let cursor = workflow?.cursors?.tasks
    while (cursor != null) {
      if (tasks.length >= 10_000) throw new Error('活动任务过多，需先缩小恢复范围。')
      const page = await this.call('workflow_page', sessionID, { cursors: { tasks: cursor } })
      tasks.push(...(page.tasks ?? []))
      cursor = page.cursors?.tasks
    }
    return tasks
  }

  private async allNotifications(sessionID: string, workflow: any): Promise<any[]> {
    const notices = [...(workflow?.notifications ?? [])]
    let cursor = workflow?.cursors?.notifications
    while (cursor != null) {
      if (notices.length >= 10_000) throw new Error('待处理通知过多，需先核对。')
      const page = await this.call('workflow_page', sessionID, { cursors: { notifications: cursor } })
      notices.push(...(page.notifications ?? []))
      cursor = page.cursors?.notifications
    }
    return notices
  }

  private mainIn(root: string): string | undefined {
    return this.research.sessionsIn(root).find((sessionID) => this.research.roleOf(sessionID) === "main")
  }

  private async say(sessionID: string, text: string, resume = false, label = text, messageID?: string, wakeIntentID?: string, launchIntentID?: string): Promise<void> {
    if (!this.current()) throw new Error("Research controller attachment has been replaced")
    await this.host.synthetic(sessionID, `${PREFIX}${text}`, `${PREFIX}${label}`, resume, messageID, wakeIntentID, launchIntentID)
  }

  /** A specialist's single turn belongs to the tool call that delegated it, not to the loop. */
  private drives(sessionID: string): boolean {
    return this.research.isAssociated(sessionID) && this.research.roleOf(sessionID) !== "specialist"
  }

  modelFor(root: string): ModelRef | undefined {
    return this.models.get(root)
  }

  // ── OpenCode execution events ────────────────────────────────────────────

  async started(sessionID: string, eventID?: string, eventSeq?: number, inferred = false): Promise<void> {
    if (!this.current() || !this.drives(sessionID)) return
    const pending = this.pendingStarts.get(sessionID)
    if (eventID && this.startedEvents.get(sessionID) === eventID && !pending) return
    if (pending && pending.eventID === eventID) {
      await this.call('runtime_execution_start', sessionID, { execution_token: pending.execution,
        event_seq: pending.eventSeq, ambiguous: pending.ambiguous }, `start:${sessionID}:${pending.execution}`)
      this.pendingStarts.delete(sessionID)
      for (const item of this.pendingInbox.get(sessionID) ?? []) {
        await this.call('runtime_execution_bind', sessionID, { execution_token: pending.execution, message_id: item.id, event_seq: item.seq })
      }
      this.pendingInbox.delete(sessionID)
      return
    }
    if (!inferred && this.busy.has(sessionID) && this.inferredStart.has(sessionID)) {
      this.inferredStart.delete(sessionID)
      if (eventID) this.startedEvents.set(sessionID, eventID)
      return
    }
    if (eventID) this.startedEvents.set(sessionID, eventID)
    const ambiguous = this.busy.has(sessionID)
    this.busy.add(sessionID)
    if (inferred) this.inferredStart.add(sessionID)
    else this.inferredStart.delete(sessionID)
    const execution = randomUUID()
    this.turns.set(sessionID, execution)
    this.startedAt.set(sessionID, Date.now())
    this.wrote.set(sessionID, false)
    this.pendingStarts.set(sessionID, { execution, eventID, eventSeq, ambiguous })
    await this.call('runtime_execution_start', sessionID, { execution_token: execution, event_seq: eventSeq, ambiguous }, `start:${sessionID}:${execution}`)
    this.pendingStarts.delete(sessionID)
    for (const item of this.pendingInbox.get(sessionID) ?? []) {
      await this.call('runtime_execution_bind', sessionID, { execution_token: execution, message_id: item.id, event_seq: item.seq })
    }
    this.pendingInbox.delete(sessionID)
  }

  async inboxDelivered(sessionID: string, messageID: string, eventSeq?: number): Promise<void> {
    if (!this.current() || !this.drives(sessionID) || !messageID) return
    const shell = this.shellInboxes.get(messageID)
    if (shell?.sessionID === sessionID) {
      this.shellInboxes.delete(messageID)
      this.shellWake.set(sessionID, { matched: shell.matched })
      if (shell.inline) return
    }
    const pending = this.pendingStarts.get(sessionID)
    if (pending) await this.started(sessionID, pending.eventID, pending.eventSeq)
    if (!this.busy.has(sessionID)) await this.started(sessionID, `inbox:${messageID}`, eventSeq, true)
    const execution = this.busy.has(sessionID) ? this.turns.get(sessionID) : undefined
    if (execution) await this.call('runtime_execution_bind', sessionID, { execution_token: execution, message_id: messageID, event_seq: eventSeq })
    else this.pendingInbox.set(sessionID, [...(this.pendingInbox.get(sessionID) ?? []), { id: messageID, seq: eventSeq }].slice(-32))
  }

  async nodeTurnAllowed(sessionID: string): Promise<boolean> {
    if (this.shellWake.get(sessionID)?.matched === false) return false
    try { await this.call('runtime_drain_check', sessionID, { execution_token: this.turns.get(sessionID) }); return true }
    catch { return false }
  }

  isShellWake(sessionID: string): boolean {
    return this.shellWake.has(sessionID)
  }

  /** A host-generated shell completion wakes the model itself; never send a second wake. */
  async shellNotice(sessionID: string, messageID: string, metadata: any): Promise<void> {
    if (!this.current() || !this.drives(sessionID) || this.research.roleOf(sessionID) !== 'node_core') return
    if (typeof metadata?.shellID !== 'string' || typeof messageID !== 'string') return
    const inline = this.busy.has(sessionID)
    const result = await this.call('runtime_shell_notice', sessionID, {
      shell_id: metadata.shellID, message_id: messageID, outcome: metadata.state,
      exit_code: metadata.exit ?? null,
    }, `shell-notice:${sessionID}:${messageID}`)
    this.shellInboxes.set(messageID, { sessionID, inline, matched: !!result.matched })
    if (!result.matched || inline) return
    let authorized = false
    try {
      const source_snapshot = await this.host.source?.(sessionID)
      const reservation = await this.call('runtime_reserve', sessionID, { message_id: messageID, source_snapshot },
        `shell-wake:${sessionID}:${messageID}`)
      if (reservation.allowed) {
        await this.call('runtime_submitted', sessionID,
          { reservation_id: reservation.reservation_id }, `shell-wake-submitted:${sessionID}:${messageID}`)
        authorized = true
      }
    } catch {
      // A pause, changed controller or missing budget must never authorize a wake.
    }
    this.shellInboxes.set(messageID, { sessionID, inline, matched: authorized })
    const root = this.research.rootOf(sessionID)
    if (root) this.research.notifyChanged(root)
  }

  wroteLedger(sessionID: string): void {
    this.wrote.set(sessionID, true)
  }

  isBusy(sessionID: string): boolean {
    return this.busy.has(sessionID)
  }

  expectInterrupt(sessionID:string): void {
    if (this.busy.has(sessionID)) this.ours.add(sessionID)
  }

  ended(sessionID: string, outcome: Outcome, eventID?:string, eventSeq?: number): Promise<void> {
    if (!this.current()) return Promise.resolve()
    if (eventID && this.runtime.endedEvents.get(sessionID)?.includes(eventID)) return Promise.resolve()
    const root = this.research.rootOf(sessionID)
    if (!root || !this.drives(sessionID)) return Promise.resolve()
    const pending = this.runtime.pendingEnds.get(sessionID) ?? {
      execution: this.turns.get(sessionID), turn: eventID ?? this.turns.get(sessionID) ?? randomUUID(), uncertain: false,
    }
    this.runtime.pendingEnds.set(sessionID, pending)
    const { execution, turn } = pending
    return this.serial(root, async () => {
      this.busy.delete(sessionID)
      this.inferredStart.delete(sessionID)
      if (!pending.error) {
        let claim: any
        try {
          claim = await this.call('runtime_execution_end',sessionID,{turn,outcome,event_seq:eventSeq,...(execution ? { execution_token: execution } : {})},`end:${sessionID}:${turn}`)
        } catch (error) {
          pending.uncertain = true
          throw error // The event queue retries the same token and operation.
        }
        try {
          if (claim.claimed || pending.uncertain) await this.afterTurn(sessionID, root, outcome,turn,execution)
        } catch (error) {
          if (!this.current()) return
          if ((error as any)?.code === 'runtime_budget_exhausted') {
            await this.say(sessionID, (error as Error).message).catch(() => {})
          } else pending.error = error
        }
      }
      if (pending.error) {
        const unsent = wasNotSent(pending.error, 'synthetic')
        if (unsent && (pending.error as any).unsent_message_id) await this.call('runtime_turn_not_sent', sessionID,
          { message_id: (pending.error as any).unsent_message_id })
        // Uncertain API results require reconciliation; pre-API failures only
        // pause continuation and preserve the existing task's verified state.
        const state = await this.state(sessionID)
        const task = (await this.allTasks(sessionID, state.workflow)).find((item: any) => item.session_id === sessionID && ACTIVE_TASK.has(item.state))
        const main = this.mainIn(root)
        if (!unsent && task && main) await this.call('workflow', main, { action: 'task_state', fields: {
          task_id: task.task_id, state: 'unverified', error: 'terminal follow-up failed: execution needs reconciliation' } })
        await this.call('workflow', sessionID, { action: 'session', fields: { pause_reason: 'fault' } })
        process.stderr.write(`Research execution follow-up paused: ${pending.error instanceof Error ? pending.error.message : String(pending.error)}\n`)
      }
      this.runtime.pendingEnds.delete(sessionID)
      this.shellWake.delete(sessionID)
      if (eventID) this.runtime.endedEvents.set(sessionID, [...(this.runtime.endedEvents.get(sessionID) ?? []), eventID].slice(-32))
    })
  }

  private async afterTurn(sessionID: string, root: string, outcome: Outcome, turn:string, execution?:string): Promise<void> {
    if (outcome === "interrupted") {
      if (this.ours.delete(sessionID)) return
      await this.pauseSession(sessionID, "native_stop", "你中断了这一轮，本会话的自动推进已暂停；用 /research resume 继续。")
      return
    }
    const state = await this.state(sessionID)
    const row = state.workflow?.session
    if (row?.role === 'node_core' && this.shellWake.get(sessionID)?.matched === false) {
      if (state.workflow?.run?.state === 'running' && !row.pause_reason) {
        const task = (await this.allTasks(sessionID, state.workflow)).find((item: any) => item.session_id === sessionID && ACTIVE_TASK.has(item.state))
        const main = this.mainIn(root)
        if (task && main) await this.call('workflow', main, { action: 'task_state', fields: {
          task_id: task.task_id, state: 'unverified', error: 'background wake could not be authorized' } })
        await this.pauseSession(sessionID, 'fault', '后台任务完成通知无法与当前执行核对；请检查任务状态后恢复。')
      }
      return
    }
    if (row?.role === 'node_core' && (await this.allTasks(sessionID, state.workflow))
      .some((task: any) => task.session_id === sessionID && task.state === 'unverified')) return
    if (outcome === "succeeded" && row?.role === "node_core") {
      const finish = await this.call("node_finish_pending", sessionID)
      if (finish) {
        if (!execution || finish.turn_token !== execution) {
          const task = (await this.allTasks(sessionID, state.workflow)).find((item: any) => item.session_id === sessionID && ACTIVE_TASK.has(item.state))
          const main = this.mainIn(root)
          if (task && main) await this.call('workflow', main, { action: 'task_state', fields: {
            task_id: task.task_id, state: 'unverified', error: 'finish execution mismatch: reconciliation required' } })
          await this.say(sessionID, "节点结束请求已保存，但当前轮的结束事件无法对应；请先核对执行状态。")
          return
        }
        await this.completeNode(sessionID, root, finish, turn, execution)
        return
      }
    }
    if (state.workflow?.run?.state !== "running" || !row) return
    if (outcome === "failed") {
      if (!this.retried.has(sessionID)) {
        this.retried.add(sessionID)
        await this.continueSession(sessionID, state, "上一轮出错，重试一次。")
      } else {
        this.retried.delete(sessionID)
        await this.pauseSession(sessionID, "fault", "连续出错，本会话的自动推进已暂停；用 /research resume 继续。")
      }
      return
    }
    this.retried.delete(sessionID)
    const source_snapshot=row.role==='node_core' ? await this.host.source?.(sessionID) : undefined
    const progress = await this.call('runtime_turn_finished', sessionID, { turn, source_snapshot }, `turn:${sessionID}:${turn}`)
    const idle = progress.idle_turns
    this.idle.set(sessionID, idle)
    if (row.role === "main" && row.pause_reason === "wait") {
      await this.deliver(root)
      return
    }
    if (row.pause_reason) return
    if (progress.external_unverified) {
      const shells = await this.call('runtime_shell_status', sessionID)
      for (const job of shells.jobs.filter((item: any) => item.state !== 'running'))
        await this.call('runtime_shell_unverified', sessionID, { call_id: job.call_id }, `shell-unknown:${sessionID}:${job.call_id}`)
      await this.pauseSession(sessionID, 'fault', '后台执行状态未核实；请先核对任务及外部进程。')
      return
    }
    if (progress.waiting_external) {
      this.research.notifyChanged(root)
      return
    }
    // A coordinator with nothing to do while its nodes run waits for them rather
    // than being nudged, or paused as idle, turn after turn.
    const running = row.role === "main" ? this.runningTasks({ workflow: { tasks: await this.allTasks(sessionID, state.workflow) } }, sessionID) : []
    if (row.role === "main" && idle > 0 && running.length) {
      await this.call("workflow", sessionID, { action: "session", fields: { waiting: running, pause_reason: "wait" } })
      this.idle.delete(sessionID)
      this.research.notifyChanged(root)
      await this.deliver(root)
      return
    }
    if (idle >= IDLE_LIMIT) {
      await this.pauseSession(
        sessionID,
        "no_progress",
        `连续 ${IDLE_LIMIT} 轮没有研究推进，本会话的自动推进已暂停；用 /research resume 继续。`,
      )
      return
    }
    await this.continueSession(sessionID, state)
  }

  private runningTasks(state: any, sessionID: string): string[] {
    return (state.workflow?.tasks ?? [])
      .filter((task: any) => task.parent_session_id === sessionID && (task.state === "queued" || ACTIVE_TASK.has(task.state)))
      .map((task: any) => task.task_id)
  }

  private progressLine(state: any): string {
    const tasks = (state.workflow?.tasks ?? []) as { state: string }[]
    const count = (states: string[]) => tasks.filter((task) => states.includes(task.state)).length
    return `当前：节点 ${state.counts?.nodes ?? 0} 个，发布 ${state.counts?.publications ?? 0} 份，知识 ${state.counts?.knowledge ?? 0} 条；任务排队 ${count(["queued"])}、运行 ${count(["starting", "running"])}。`
  }

  private async continueSession(sessionID: string, state: any, reason = ""): Promise<void> {
    const row = state.workflow?.session
    const text =
      row?.role === "node_core"
        ? `${reason}继续完成节点 ${row.node_id}。完成或受阻时调用 research_finish。`
        : `${reason}继续推进研究。${this.progressLine(state)}按角色说明决定下一步；有节点在运行而你没有别的工作时调用 research_wait；目标已经回答时发布最终报告并调用 research_conclude。`
    await this.say(sessionID, text, true, "继续")
  }

  private async pauseSession(sessionID: string, reason: string, note: string): Promise<void> {
    await this.call("workflow", sessionID, { action: "session", fields: { pause_reason: reason } })
    await this.say(sessionID, note)
    const root = this.research.rootOf(sessionID)
    if (root) {
      if (this.research.roleOf(sessionID) === 'node_core') {
        await this.call('workflow', sessionID, { action: 'notify', fields: {
          kind: 'paused', key: `paused:${reason}:${this.turns.get(sessionID) ?? 'unknown'}`, summary: note,
        } })
        await this.deliver(root)
      }
      this.research.notifyChanged(root)
    }
  }

  // ── Node lifecycle ───────────────────────────────────────────────────────

  private async completeNode(
    sessionID: string,
    root: string,
    finish: { intent_id: string; state: string; summary: string },
    terminal: string,
    execution: string,
  ): Promise<void> {
    // Attempt, task, notification and intent settle in one ledger transaction.
    await this.call("node_finish_commit", sessionID,
      { intent_id: finish.intent_id, turn_token: execution, terminal }, `commit:${finish.intent_id}`)
    this.idle.delete(sessionID)
    await this.say(sessionID, `节点工作已结束（${finish.state === "finished" ? "完成" : "受阻停止"}），结果已保存，主协调消息正在交付或核对。`).catch(() => {})
    this.research.notifyChanged(root)
    await this.deliver(root)
    await this.schedule(root)
  }

  private async noticeText(root: string, notice: any): Promise<string> {
    const payload = notice.payload ?? {}
    if (notice.kind === 'exploration_result') {
      return `探索工作未启动：${payload.reason}。待处理结果 ${payload.result_id}；请读取探索结果并决定下一步。`
    }
    if (notice.kind === 'resource_wait') {
      const blockers = (payload.blockers ?? []).map((item: any) => `${item.task_id}（${item.task_state} / ${item.state}）`).join('、')
      const commands: string[] = payload.recovery_commands ?? []
      return [`任务 ${payload.task_id} 等待资源：${JSON.stringify(payload.resources)}。`,
        blockers ? `持有任务：${blockers}。` : '请核对已配置的资源容量。',
        commands.length ? `正常节点结束仍需用户独立核实外部进程退出，再执行：\n${commands.join('\n')}` : '运行中或退出未核实的任务继续占用资源；不会自动释放。',
      ].join('\n')
    }
    const pubs = await this.research.storage
      .request("project_read", { root, view: "page", collection: "publications", node_id: payload.node_id, limit: 5 })
      .catch(() => null)
    const refs = ((pubs as any)?.items ?? [])
      .map((pub: any) => `pub/${pub.publication_id}（${oneLine(pub.display?.title ?? pub.summary, 40)}）`)
      .join("、")
    const policy = await this.call("context_policy", this.mainIn(root)!)
    let finding = ""
    if (policy.policy_version === "memory-r1-v1" && payload.kind === "finished") {
      const page: any = await this.research.storage.request("project_read", {
        root, view: "page", collection: "completed_findings", node_id: payload.node_id, limit: 1,
      })
      const ref = page.items[0].selected_refs[0]
      if (ref) {
        const record: any = await this.research.storage.request("project_read", { root, view: "reference_get", ref })
        finding = `发现：${ref} ${oneLine(record.value.statement, 120)}；scope/conditions 用 research_query ref 展开。`
      } else finding = "发现：未登记发现（claim/observation）"
    }
    const outcome = NOTICE_OUTCOME[payload.kind] ?? payload.kind
    return [
      `节点 ${payload.node_id} ${outcome}（任务 ${payload.task_id}）。`,
      `总结：${payload.summary || "（未提供）"}`,
      `发布：${refs || "无"}`,
      ...(finding ? [finding] : []),
    ].join("\n")
  }

  /** Hands pending node reports to the coordinator and wakes it if it was waiting for them. */
  private async deliver(root: string): Promise<void> {
    const main = this.mainIn(root)
    if (!main) return
    const state = await this.state(main)
    const row = state.workflow?.session
    const waiting: string[] = row?.waiting ?? []
    const notices = (await this.allNotifications(main, state.workflow)).filter((notice: any) => notice.recipient === main)
    const delivered: any[] = []
    for (const notice of notices.filter((item: any) => item.state === "pending")) {
      const text = await this.noticeText(root, notice)
      const delivery = await this.call("notification_begin", main, { notification_id: notice.notification_id, text })
      if (!delivery.send) continue // prior submission may have reached the host; do not blindly resend
      try {
        await this.say(main, delivery.text, false, "收到节点消息", delivery.message_id)
        await this.call("notification_confirm", main, { intent_id: delivery.intent_id, message_id: delivery.message_id },
          `notification-confirm:${notice.notification_id}`)
        delivered.push(notice)
      } catch (error) {
        if (wasNotSent(error, 'synthetic')) await this.call('runtime_outbound_not_sent', main, {
          intent_id: delivery.intent_id, stage: 'synthetic', reason: String(error),
        }).catch(() => {})
        // Keep the report queryable and the intent unconfirmed when delivery is uncertain.
      }
    }
    // A waited task may have ended before the coordinator started waiting.
    const wakeNotice = [...delivered, ...notices.filter((item: any) => item.state === "delivered")]
      .find((item: any) => WAKING.has(item.kind) && (waiting.includes(item.task_id) || item.kind === 'exploration_result' && !item.task_id))
    const canWake =
      wakeNotice && row?.pause_reason === "wait" && state.workflow?.run?.state === "running" && !this.busy.has(main)
    if (canWake) {
      const intent = await this.call("notification_wake_begin", main, { notification_id: wakeNotice.notification_id })
      if (intent.send) {
        try {
          await this.say(main, `等待的节点有了结果。继续推进研究。${this.progressLine(state)}`, true,
            delivered.length ? `收到 ${delivered.length} 条节点消息，继续` : "等待结束，继续", intent.message_id, intent.intent_id)
          await this.call("notification_wake_confirm", main, { intent_id: intent.intent_id, message_id: intent.message_id },
            `notification-wake-confirm:${wakeNotice.notification_id}`)
          this.idle.delete(main)
        } catch (error) {
          if (wasNotSent(error, 'synthetic')) await this.call('runtime_outbound_not_sent', main, {
            intent_id: intent.intent_id, stage: 'synthetic', reason: String(error),
          }).catch(() => {})
          throw error
        }
      }
    }
  }

  private async schedule(root: string): Promise<void> {
    const main = this.mainIn(root)
    if (!main) return
    let state = await this.state(main)
    if (state.workflow?.run?.state !== "running") return
    let settled = false
    if (state.project?.config?.exploration_policy === 'exploration-v1') {
      const intents: string[] = await this.call('exploration_pending', main)
      for (const intent_id of intents) {
        const result: any = await this.call('exploration_materialize', main, { intent_id })
        if (['stale','failed','cancelled'].includes(result.state)) settled = true
        if (result.state === 'waiting_budget' && result.notify_waiting) await this.say(main, `探索工作等待预算：${result.reason}`).catch(() => {})
      }
      if (intents.length) state = await this.state(main)
    }
    const tasks = await this.allTasks(main, state.workflow)
    let active = tasks.filter((task) => ACTIVE_TASK.has(task.state)).length
    for (const task of fairTaskOrder(tasks.filter((item) => item.state === "queued"),this.lastScheduledScope.get(root))) {
      if (active >= this.concurrency) break
      try {
        const result = await this.startTask(root, main, task)
        if (result === 'started') {
          active++
          this.lastScheduledScope.set(root,task.exploration_scope_id ?? '__ordinary__')
        }
        if (result === 'settled' || result === 'notify') settled = true
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        if ((error as any)?.unsent_rolled_back) {
          const paused = (error as any).unsent_paused
          await this.say(main, paused ? `节点 ${task.node_id} 连续三次未能提交，自动调度已暂停；任务保留在队列。排障后用 /research resume。`
            : `节点 ${task.node_id} 尚未提交宿主，任务已保留在队列。${message}`).catch(() => {})
          if (paused) break
          continue
        }
        if (error instanceof Error && 'code' in error && error.code==='runtime_budget_exhausted') {
          const fresh=await this.state(main)
          // A wall-clock stop has already marked in-flight work unverified.
          // Never overwrite that durable decision with a guessed cancellation.
          if (fresh.workflow?.run?.state==='running') await this.call('workflow',main,{action:'task_state',fields:{
            task_id:task.task_id,state:'queued',error:message}})
          await this.say(main,`节点 ${task.node_id} 预算到限，工作已保留。${message}`).catch(() => {})
          if (fresh.workflow?.run?.state!=='running') break
          continue
        }
        // Once startup has entered "starting", a host request may have been
        // accepted even if its response was lost. Never turn that uncertainty
        // into a retryable failure or create another executor blindly.
        const fresh = await this.state(main).catch(() => null)
        const current = fresh ? (await this.allTasks(main, fresh.workflow)).find((item: any) => item.task_id === task.task_id) : null
        const uncertain = current && current.state !== "queued"
        if (!uncertain && error instanceof Error && 'code' in error &&
            (error.code === 'research_control_paused' || error.code === 'research_control_stopped')) return
        await this.call("workflow", main, {
          action: "task_state",
          fields: { task_id: task.task_id, state: uncertain ? "unverified" : "failed", error: message },
        }).catch(() => {})
        await this.say(main, uncertain
          ? `节点 ${task.node_id} 的启动结果未核实，请先核对会话与执行状态；不会自动重复启动。原因：${message}`
          : `节点 ${task.node_id} 启动失败：${message}`).catch(() => {})
      }
    }
    if (settled) await this.deliver(root)
  }

  private async startTask(root: string, main: string, task: any): Promise<'started' | 'settled' | 'notify' | 'skipped'> {
    let workspace: string | null = task.cwd
    if (!workspace) {
      const prepared = await this.call("prepare_branch", main, { node_id: task.node_id }, `${task.task_id}:prepare`)
      workspace = prepared.workspace as string
    }
    const launch = await this.call("node_launch_claim", main, { task_id: task.task_id, concurrency: this.concurrency })
    if (!launch.claimed) {
      if (launch.reason === 'waiting_resource' && launch.notification_id) return 'notify'
      return ['exploration_basis_changed','exploration_basis_format_upgraded','exploration_disabled'].includes(launch.reason) ? 'settled' : 'skipped'
    }
    let createAttempted = false
    let createConfirmed = !!launch.reuse
    let kickoffAttempted = false
    try {
      const context = await this.call("task_context", main, { task_id: task.task_id })
      const sessionID: string = launch.session_id
      if (launch.reuse && this.research.isAssociated(sessionID)) {
        await this.call("workflow", sessionID, { action: "session", fields: { pause_reason: null } })
        await this.useModel(root, sessionID)
        await this.call("node_launch_phase", main, { intent_id: launch.intent_id, phase: "created" }, `${launch.intent_id}:created`)
      } else {
        if (launch.reuse) throw Object.assign(new Error("已绑定的节点会话尚未关联，无法安全恢复"), { code: 'research_execution_unverified' })
        await this.call("node_launch_phase", main, { intent_id: launch.intent_id, phase: "creating" }, `${launch.intent_id}:creating`)
        createAttempted = true
        const created = await this.host.create(`${task.node_id} · ${oneLine(context.question, 50)}`, root, this.models.get(root), undefined, sessionID)
        if (created !== sessionID) throw new Error("宿主没有确认预定的节点会话 ID")
        createConfirmed = true
        await this.call("node_launch_phase", main, { intent_id: launch.intent_id, phase: "created" }, `${launch.intent_id}:created`)
        await this.call(
          "open",
          sessionID,
          { root, cwd: workspace, session_role: "node_core", node_id: task.node_id, context },
          `${launch.intent_id}:open`,
        )
        this.research.register(sessionID, root, "node_core")
      }
      await this.call("focus", sessionID, { node_id: task.node_id, role: "core", mode: "auto" }, `${launch.intent_id}:focus`)
      await this.call("node_launch_phase", main, { intent_id: launch.intent_id, phase: "ready" }, `${launch.intent_id}:ready`)
      this.idle.delete(sessionID)
      const inputs = (context.inputs ?? []) as string[]
      kickoffAttempted = true
      await this.say(
        sessionID,
        [
          `你是节点 ${task.node_id} 的执行会话（任务 ${task.task_id}）。`,
          `问题：${context.question}`,
          `计划：${context.plan}`,
          `固定输入：${inputs.length ? `${inputs.join("、")}（已放在 ${workspace}/inputs/，只读）` : "无"}`,
          `工作目录：${workspace}。先 cd 到这里；在 scratch/ 里做实验，把报告和数据放进 output/。`,
          "发布时 source_path 写相对这个工作目录的路径，例如 output/report.md。",
          ['scout','probe'].includes(context.purpose)
            ? "只回答这个会影响选择的问题。可返回支持、反例、混合或信息不足；发布最小可读结果及固定依据，说明条件、未知和建议。受阻不等于反证；完成或受阻时调用 research_finish。"
            : "完成条件：回答节点问题；用 research_publish 发布报告和数据，用 research_record 记录引用发布材料的结论，用 research_checkpoint 更新检查点；完成或受阻时调用 research_finish。",
        ].join("\n"),
        true,
        `开始节点 ${task.node_id}`,
        launch.message_id,
        undefined,
        launch.intent_id,
      )
      await this.call("node_launch_submitted", main,
        { intent_id: launch.intent_id, message_id: launch.message_id, cwd: workspace }, `${launch.intent_id}:submitted`)
      // Inside the coordinator's own dispatch call the tool result already says this.
      if (!this.busy.has(main)) await this.say(main, `已启动节点 ${task.node_id} 的执行会话（任务 ${task.task_id}）。`).catch(() => {})
      this.research.notifyChanged(root)
      return 'started'
    } catch (error) {
      const stage = (error as any)?.code === 'research_execution_unverified' ? undefined
        : !createConfirmed && (!createAttempted || wasNotSent(error, 'create')) ? 'create'
        : createConfirmed && (!kickoffAttempted || wasNotSent(error, 'synthetic')) ? 'synthetic' : undefined
      if (stage && this.current()) {
        const undone = await this.call('runtime_outbound_not_sent', main, {
          intent_id: launch.intent_id, stage, reason: String(error),
        }).catch(() => null)
        if (undone?.rolled_back) Object.assign(error instanceof Error ? error : {}, { unsent_rolled_back: true, unsent_paused: undone.paused })
      }
      throw error
    }
  }

  // ── Engine tools called by models ────────────────────────────────────────

  explorationDecide(sessionID: string, fields: Record<string, unknown>, operationID: string): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      const decision = await this.call<any>('exploration_decide', sessionID, { fields, model_call: true }, operationID)
      this.research.notifyChanged(root)
      const intents: string[] = decision.intent_ids ?? (decision.intent_id ? [decision.intent_id] : [])
      if (!intents.length) return decision
      let state: any
      try { state = await this.state(sessionID) }
      catch (error) {
        return {...decision, decision_recorded:true, works:intents.map(intent_id=>({intent_id,state:'unconfirmed'})),
          errors:[{phase:'status',reason:String(error)}], recovery:'Decision is recorded; query or retry the same intent IDs. Do not repeat the decision.'}
      }
      if (state.workflow?.run?.state !== 'running') {
        const works = intents.map(intent_id => ({intent_id,state:'pending',reason:'awaiting_auto'}))
        return {...decision,decision_recorded:true,work:works[0],works}
      }
      const works: any[] = [], errors: {phase:string;intent_id?:string;reason:string}[] = []
      for (const intent_id of intents) {
        try { works.push(await this.call<any>('exploration_materialize', sessionID, {intent_id})) }
        catch (error) {
          works.push({intent_id,state:'unconfirmed'})
          errors.push({phase:'materialize',intent_id,reason:String(error)})
        }
      }
      if (works.some(work => work.state === 'materialized')) {
        try { await this.schedule(root) } catch (error) { errors.push({phase:'schedule',reason:String(error)}) }
      }
      if (works.some(work => ['stale','failed','cancelled'].includes(work.state))) {
        try { await this.deliver(root) } catch (error) { errors.push({phase:'deliver',reason:String(error)}) }
      }
      return {...decision,decision_recorded:true,work:works[0],works,errors,
        ...(errors.length ? {recovery:'Decision is recorded; query or retry the same intent IDs. Do not repeat the decision.'} : {})}
    })
  }

  explorationPolicy(sessionID:string,mode?:string | null): Promise<CommandResult> {
    const root=this.research.requireAssociated(sessionID)
    return this.serial(root,async()=>{
      const result=await this.research.explorationPolicy(sessionID,mode)
      if (mode==='off') await this.deliver(root)
      return result
    })
  }

  dispatch(sessionID: string, nodeID: string, operationID: string): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      await this.call("validate_branch_inputs", sessionID, { node_id: nodeID })
      const task = await this.call("workflow", sessionID, { action: "task", fields: { node_id: nodeID } }, operationID)
      this.research.notifyChanged(root)
      await this.schedule(root)
      const lookup = await this.call("query", sessionID, { ref: task.task_id }).catch(() => null)
      const accepted = lookup?.value ?? task
      if (accepted.state === 'unverified') return { ...accepted, accepted: true, reason: 'delivery_unverified',
        recovery: { action: 'inspect_execution', task_ids: [task.task_id] } }
      if (accepted.state === 'failed') return { ...accepted, accepted: true, reason: 'launch_failed',
        recovery: { action: 'inspect_execution', task_ids: [task.task_id] } }
      if (accepted.state === 'queued') {
        const fresh = await this.state(sessionID)
        const running = fresh.workflow?.run?.state === 'running' && [null, undefined, 'wait'].includes(fresh.workflow?.session?.pause_reason)
        return { ...accepted, accepted: true, reason: running ? 'capacity' : 'control_paused',
          recovery: running ? { action: 'wait', task_ids: [task.task_id] } : { action: 'explicit_resume' } }
      }
      return { ...accepted, accepted: true }
    })
  }

  wait(sessionID: string, taskIDs: string[]): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      const state = await this.state(sessionID)
      const run = state.workflow?.run
      const pause = state.workflow?.session?.pause_reason
      if (run?.state !== 'running' || pause && pause !== 'wait') {
        const resumable = ['running', 'paused', 'cold'].includes(run?.state)
        throw Object.assign(new Error('研究当前不能等待；请先按控制状态显式恢复。'), {
          code: resumable ? 'research_control_paused' : 'research_control_stopped',
          details: { run_state: run?.state, pause_reason: pause, control_revision: run?.control_revision,
            controller_phase: state.workflow?.controller?.phase, retryable: false,
            recovery: resumable ? 'explicit_resume' : 'explicit_auto', allow_fallback: false },
        })
      }
      const mine = new Set((await this.allTasks(sessionID, state.workflow))
        .filter((task: any) => task.parent_session_id === sessionID).map((task: any) => task.task_id))
      // Finished tasks leave the control view; look them up so waiting on a task
      // that already reported still wakes the coordinator.
      for (const taskID of taskIDs.filter((id) => !mine.has(id))) {
        const found = await this.call("query", sessionID, { ref: taskID }).catch(() => null)
        if (found?.value?.parent_session_id === sessionID) mine.add(taskID)
      }
      const unknown = taskIDs.filter((taskID) => !mine.has(taskID))
      if (!taskIDs.length || unknown.length) throw new Error(`只能等待自己派发的任务：${unknown.join(", ") || "未提供任务"}`)
      await this.call("workflow", sessionID, { action: "session", fields: { waiting: taskIDs, pause_reason: "wait" } })
      this.research.notifyChanged(root)
      return { waiting: taskIDs, message: "本轮结束后进入等待；任务有结果时会以【Research 自动推进】消息通知你。" }
    })
  }

  recoverTask(sessionID: string, taskID: string, reason: string): Promise<CommandResult> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      await this.call('recovery_confirm_exit', sessionID, { task_id: taskID, reason })
      this.research.notifyChanged(root)
      await this.deliver(root).catch(() => {})
      return { message: `已记录任务 ${taskID} 的人工退出核对；如需重新执行，使用 /research retry ${taskID} <原因>。` }
    })
  }

  recoverResource(sessionID: string, taskID: string, reason: string): Promise<CommandResult> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      await this.call('runtime_resources_release', sessionID, {task_id:taskID,reason})
      this.research.notifyChanged(root)
      await this.schedule(root)
      return {message:`已记录任务 ${taskID} 的外部进程退出核对并释放声明资源；未知用量仍保留。`}
    })
  }

  retryTask(sessionID: string, taskID: string, reason: string): Promise<CommandResult> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      await this.call('recovery_retry_task', sessionID, { task_id: taskID, reason })
      await this.schedule(root)
      this.research.notifyChanged(root)
      return { message: `任务 ${taskID} 已重新受理；启动情况见研究状态。` }
    })
  }

  recoverDelivery(sessionID: string, notificationID: string, kind: string, disposition: string, reason: string): Promise<CommandResult> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      await this.call('recovery_resolve_delivery', sessionID,
        { notification_id: notificationID, kind, disposition, reason })
      await this.deliver(root).catch(() => {})
      this.research.notifyChanged(root)
      return { message: `已记录通知 ${notificationID} 的人工核对结果。` }
    })
  }

  async finish(sessionID: string, finish: { state: string; summary: string }, operationID: string): Promise<unknown> {
    if (this.research.roleOf(sessionID) !== "node_core") throw new Error("只有节点执行会话可以调用 research_finish")
    const turn_token = this.turns.get(sessionID)
    if (!turn_token) throw new Error("当前节点执行轮尚未核实；请先核对执行状态。")
    await this.call("node_finish_request", sessionID, { ...finish, turn_token }, operationID)
    return { message: "本轮结束后，节点本次执行结束，总结会交给主协调。", state: finish.state }
  }

  conclude(sessionID: string, fields: ConclusionInput, operationID: string, deliveryReceipts: string[] = []): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    if (this.research.roleOf(sessionID) !== "main") throw new Error("只有主协调可以结束研究")
    return this.serial(root, async () => {
      const conclusion = await this.call("conclude", sessionID, { fields, model_call: true,
        ...(fields.draft_id ? { delivery_receipts: deliveryReceipts } : {}) }, operationID)
      let notification_warning: string | undefined
      try {
        this.research.notifyChanged(root)
        await this.say(sessionID, `研究执行已结束，结果 ${conclusion.outcome}，最终报告 ${conclusion.final_ref}。审阅状态 ${conclusion.evidence ? conclusion.evidence.review_coverage.status + ' 为派生覆盖' : conclusion.review.status + ' 为调用者声明'}，不表示结论通过。`)
      } catch {
        notification_warning = "结项已保存，但会话通知发送失败；可通过状态或项目概览查看。"
      }
      return { message: "研究执行已结束，结项记录已保存，自动推进停止。", ...conclusion,
        ...(notification_warning ? { notification_warning } : {}) }
    })
  }

  // ── User commands ────────────────────────────────────────────────────────

  /** Reconcile persisted launches before allowing a reattached controller to schedule. */
  async recoverWarm(): Promise<void> {
    for (const root of this.research.roots().filter(item => this.research.inScope(item))) {
      const main = this.mainIn(root)
      if (!main) continue
      const state = await this.state(main)
      const coordinator = await this.call('runtime_execution_view', main, { execution_token: this.turns.get(main) })
      if (coordinator?.state === 'open' && coordinator.message_id) {
        this.turns.set(main, coordinator.execution_token)
        this.busy.add(main)
        setTimeout(() => { if (this.current()) void this.watchRecoveredCoordinator(root, main, coordinator.message_id, coordinator.execution_token) }, 0)
      }
      const tasks = await this.allTasks(main, state.workflow)
      for (const task of tasks.filter((item: any) => ['starting', 'running'].includes(item.state))) {
        const launch = await this.call('node_launch_get', main, { task_id: task.task_id })
        if (task.state !== 'running' || launch?.state !== 'submitted' || !this.research.isAssociated(task.session_id)) {
          await this.call('workflow', main, { action: 'task_state', fields: { task_id: task.task_id,
            state: 'unverified', error: 'reload: launch state not verified' } })
          continue
        }
        const current = await this.call('runtime_execution_view', task.session_id,
          { execution_token: this.turns.get(task.session_id) })
        const shells = await this.call('runtime_shell_status', task.session_id)
        if (shells.jobs.length) {
          const messages = await (this.host.recoveryMessages?.(task.session_id) ?? Promise.resolve([])).catch(() => [])
          let uncertain = false
          for (const job of shells.jobs) {
            const notice = messages.find((item: any) => item?.type === 'synthetic' &&
              item.metadata?.source === 'shell' && item.metadata?.shellID === job.shell_id)
            if (notice?.id) {
              // The shell is known to have exited, but a host wake delivered
              // while detached may have run without a tracked authorization.
              // Preserve that uncertainty unless the currently open execution
              // is explicitly bound to this exact notice.
              if (current?.state !== 'open' || current?.message_id !== notice.id) {
                await this.call('runtime_shell_unverified', task.session_id, { call_id: job.call_id },
                  `shell-unknown:${task.session_id}:${job.call_id}`)
                uncertain = true
              }
              await this.call('runtime_shell_notice', task.session_id, { shell_id: job.shell_id,
                message_id: notice.id, outcome: notice.metadata.state, exit_code: notice.metadata.exit ?? null },
                `shell-notice:${task.session_id}:${notice.id}`)
            } else {
              await this.call('runtime_shell_unverified', task.session_id, { call_id: job.call_id },
                `shell-unknown:${task.session_id}:${job.call_id}`)
              uncertain = true
            }
          }
          if (uncertain) continue
        }
        const execution = current?.execution_token
        // A disallowed wake can still reach a native terminal while the
        // project is paused. Its revoked authorization stays revoked; the
        // terminal is enough to preserve the paused task without replaying it.
        if (state.workflow?.run?.state === 'paused' && current?.state === 'revoked' &&
            current.terminal && !current.has_unbound_turn && !this.busy.has(task.session_id)) continue
        if (current?.state === 'ended' && !current.has_unbound_turn && !this.busy.has(task.session_id)) {
          this.runtime.pendingEnds.delete(task.session_id)
          const finish = await this.call('node_finish_pending', task.session_id)
          if (finish) {
            if (finish.turn_token !== execution || current.terminal?.outcome !== 'succeeded') {
              await this.call('workflow', main, { action: 'task_state', fields: { task_id: task.task_id,
                state: 'unverified', error: 'reload: pending finish does not match terminal execution' } })
            } else setTimeout(() => {
              if (this.current()) void this.ensureController(root, main)
                .then(() => this.serial(root, () => this.completeNode(task.session_id, root, finish, current.terminal.turn, execution)))
                .catch(async () => {
                  const fresh = await this.state(main)
                  if ((await this.allTasks(main, fresh.workflow)).some((item: any) => item.task_id === task.task_id && ACTIVE_TASK.has(item.state))) {
                    await this.call('workflow', main, { action: 'task_state', fields: {
                      task_id: task.task_id, state: 'unverified', error: 'reload: pending finish settlement failed' } })
                  }
                }).catch(() => {})
            }, 0)
          }
          continue
        }
        if (!execution || !current?.message_id || current.state !== 'open') {
          await this.call('workflow', main, { action: 'task_state', fields: { task_id: task.task_id,
            state: 'unverified', error: 'reload: launch or execution state not verified' } })
          continue
        }
        this.turns.set(task.session_id, execution)
        this.busy.add(task.session_id)
        setTimeout(() => { if (this.current()) void this.watchRecoveredNode(root, main, task, current.message_id, execution) }, 0)
      }
    }
  }

  private async watchRecoveredCoordinator(root: string, main: string, messageID: string, execution: string): Promise<void> {
    try {
      await this.host.wait(main)
      if (!this.current() || !this.busy.has(main) || this.turns.get(main) !== execution) return
      const messages = await this.host.messages(main)
      const start = messages.findIndex(item => item?.id === messageID || item?.info?.id === messageID)
      const later = start >= 0 ? messages.slice(start + 1) : []
      const last = later.at(-1)
      if (start >= 0 && !later.some(item => item?.type === 'user' || item?.type === 'synthetic') &&
          last?.type === 'idle' && ['succeeded', 'failed', 'interrupted'].includes(last.outcome)) {
        await this.ended(main, last.outcome, `recovered:${messageID}:${execution}`)
        return
      }
    } catch { if (!this.current()) return }
    if (!this.current() || this.turns.get(main) !== execution) return
    this.busy.delete(main)
    await this.call('workflow', main, { action: 'session', fields: { pause_reason: 'fault' } }).catch(() => {})
    const state = await this.state(main).catch(() => null)
    if (state?.workflow?.run?.state === 'running') {
      await this.call('workflow', main, { action: 'run', fields: { state: 'paused' } }).catch(() => {})
    }
    this.research.notifyChanged(root)
  }

  private async watchRecoveredNode(root: string, main: string, task: any, messageID: string, execution: string): Promise<void> {
    try {
      await this.host.wait(task.session_id)
      if (!this.current() || !this.busy.has(task.session_id) || this.turns.get(task.session_id) !== execution) return
      const messages = await this.host.messages(task.session_id)
      const start = messages.findIndex(item => item?.id === messageID || item?.info?.id === messageID)
      const later = start >= 0 ? messages.slice(start + 1) : []
      const last = later.at(-1)
      const correlated = start >= 0 && !later.some(item => item?.type === 'user' || item?.type === 'synthetic') &&
        last?.type === 'idle' && ['succeeded', 'failed', 'interrupted'].includes(last.outcome)
      if (correlated) {
        await this.ended(task.session_id, last.outcome, `recovered:${messageID}:${execution}`)
      } else {
        await this.call('workflow', main, { action: 'task_state', fields: { task_id: task.task_id,
          state: 'unverified', error: 'reload: native terminal event could not be correlated' } })
        this.research.notifyChanged(root)
      }
    } catch {
      if (this.current()) await this.call('workflow', main, { action: 'task_state', fields: { task_id: task.task_id,
        state: 'unverified', error: 'reload: execution reconciliation failed' } }).catch(() => {})
    }
  }

  private async useModel(root: string, sessionID: string): Promise<void> {
    const model = this.models.get(root)
    if (model) await this.host.switchModel(sessionID, model)
  }

  private rootFor(sessionID: string | null, directory: string): string {
    const root = (sessionID && this.research.rootOf(sessionID)) || findProjectRoot(directory)
    if (!root) throw new Error("当前目录及上级目录里没有研究项目。用 /research auto <研究目标> 开始。")
    this.research.requireScope(root)
    return root
  }

  /**
   * Starts or restarts autonomous research. Without a project, `text` is the
   * goal of a new one; with a project the goal stays and `text` is the user's
   * guidance for the new run.
   */
  async auto(sessionID: string | null, directory: string, text: string, model?: ModelRef): Promise<CommandResult> {
    await this.research.ready()
    let created: CommandResult | null = null
    if (!findProjectRoot(directory)) {
      if (!sessionID) throw new Error("请在会话里执行，或给出研究目标。")
      if (!text.trim()) throw new Error("当前目录还没有研究项目。用法：/research auto <研究目标>")
      created = await this.research.init(sessionID, directory, text)
    }
    const guidance = created ? "" : text.trim()
    if (guidance.length > GUIDANCE_LIMIT) {
      throw new Error(`补充指令超过 ${GUIDANCE_LIMIT} 字，没有发送；请缩短。长期有效的规则可以写成文档，用 /research guidance 登记。`)
    }
    const root = this.rootFor(sessionID, directory)
    const main = this.mainIn(root)
    if (!main) throw new Error("这个项目的主会话不在 OpenCode 里。先用 /research takeover 接管。")
    await this.ensureController(root, main)
    if (model) this.models.set(root, model)
    return this.serial(root, async () => {
      await this.useModel(root, main)
      const state = await this.state(main)
      const run = state.workflow?.run?.state
      if (run === "running") {
        if (guidance) throw new Error("自主研究已经在运行，补充指令没有发送；请直接在主会话里发消息，会记为用户指导。")
        return { message: "自主研究已经在运行。", sessionID: main }
      }
      if (run === "paused" || run === "cold") {
        throw new Error(
          guidance
            ? "项目处于暂停状态，补充指令没有发送；先在主会话里发消息说明，再用 /research resume 继续。"
            : "项目处于暂停状态；用 /research resume 继续。",
        )
      }
      const unknown = await this.call('runtime_unverified', main)
      if (unknown.tasks || unknown.specialists) {
        throw new Error('仍有执行退出状态未核实；请先核对，不能开始新一代研究。')
      }
      // A busy session gets no kickoff, so the guidance would reach the model only through the note.
      if (guidance && this.busy.has(main)) {
        throw new Error("主会话这一轮还没结束，补充指令没有发送；等这一轮结束后再执行 /research auto。")
      }
      await this.call("workflow", main, { action: "run", fields: { state: "running", new_generation: true } })
      if (state.attempt && state.attempt.mode !== "auto") {
        await this.call("finish", main, { state: "finished", details: { reason: "switch-to-autonomous" } })
      }
      if (!state.attempt || state.attempt.mode !== "auto") {
        await this.call("focus", main, { node_id: null, role: "planner", mode: "auto" })
      }
      // Only now is a work segment open to file the note in; it carries the guidance into later requests.
      // The run has already switched, so a failed note is reported, not thrown: the kickoff still carries the text.
      let noteError = ""
      if (guidance) {
        await this.call("note", main, guidanceNote(guidance)).catch((error) => {
          noteError = error instanceof Error ? error.message : String(error)
        })
      }
      await this.call("workflow", main, { action: "session", fields: { pause_reason: null, waiting: [] } })
      this.idle.delete(main)
      this.research.notifyChanged(root)
      // The busy check above came before several awaits; a turn that started since means no kickoff goes out.
      const kickoffSent = !this.busy.has(main)
      if (kickoffSent) {
        const kickoff = [`开始自主研究。目标：${state.project?.goal}`]
        if (guidance) kickoff.push(`本轮附带用户指导（项目目标不变），请遵循：\n${guidance}`)
        kickoff.push("先盘点已有材料和记录，然后规划节点、派发执行；结果回来后综合，目标回答后发布最终报告并调用 research_conclude。")
        await this.say(main, kickoff.join("\n"), true, "开始自主研究")
      }
      await this.schedule(root)
      let filed = ""
      if (guidance && kickoffSent) {
        filed = noteError
          ? `项目目标不变；补充指令写进了启动消息，但没能记入研究记录（${noteError}）。`
          : "项目目标不变，补充指令已记为用户指导。"
      } else if (guidance) {
        filed = noteError
          ? `项目目标不变；但补充指令没能记入研究记录（${noteError}），主会话又刚开始了一轮、启动消息没有发出，请在主会话里重新发送这段指令。`
          : "项目目标不变，补充指令已记为用户指导；但主会话刚开始了一轮，启动消息没有发出，模型只能从研究上下文里读到这段指令。"
      }
      return {
        message: `${created ? "已建立研究项目，" : ""}自主研究已开始。${filed}进展会显示在侧栏、状态条和 /research board 里。`,
        sessionID: main,
      }
    })
  }

  pause(sessionID: string | null, directory: string): Promise<CommandResult> {
    const root = this.rootFor(sessionID, directory)
    const main = this.mainIn(root)
    if (!main) throw new Error("这个项目的主会话不在 OpenCode 里。")
    return this.serial(root, async () => {
      const state = await this.state(main)
      if (state.workflow?.run?.state !== "running") return { message: "自主研究没有在运行。" }
      await this.call("workflow", main, { action: "run", fields: { state: "paused" } })
      this.research.notifyChanged(root)
      await this.say(main, "自主研究已暂停；正在进行的这一轮会做完。用 /research resume 继续。")
      return { message: "已暂停。正在进行的这一轮会做完，之后不再继续；用 /research resume 恢复。" }
    })
  }

  resume(sessionID: string | null, directory: string, model?: ModelRef): Promise<CommandResult> {
    const root = this.rootFor(sessionID, directory)
    const main = this.mainIn(root)
    if (!main) throw new Error("这个项目的主会话不在 OpenCode 里。")
    if (model) this.models.set(root, model)
    return this.serial(root, async () => {
      const state = await this.state(main)
      const run = state.workflow?.run?.state
      if (!["paused", "cold", "running"].includes(run)) {
        throw new Error(`项目状态是「${run}」；用 /research auto 开始。`)
      }
      if (run !== "running") await this.call("workflow", main, { action: "run", fields: { state: "running" } })
      const tasks = await this.allTasks(main, state.workflow)
      const resumed: string[] = []
      for (const member of this.research.sessionsIn(root).filter((sessionID) => this.drives(sessionID))) {
        const current = await this.state(member)
        const row = current.workflow?.session
        if (!row || row.detached || !["main", "node_core"].includes(row.role)) continue
        if (row.pause_reason && !RESUMABLE.has(row.pause_reason)) continue
        const task = tasks.find((item: any) => item.session_id === member && (
          ACTIVE_TASK.has(item.state) || item.state==='waiting' && item.error?.startsWith('wall_clock_budget:')))
        if (row.role === "node_core" && !task) continue
        if (task?.state==='unverified') continue
        if (task?.state==='waiting') await this.call('workflow',main,{action:'task_state',fields:{task_id:task.task_id,state:'running'}})
        let waitingExternal = false
        if (row.pause_reason) {
          const fresh = await this.state(member)
          const result = await this.call('runtime_resume_session', main, {
            target_session: member, expected_revision: fresh.workflow.run.control_revision,
          })
          if (!result.resumed) continue
          waitingExternal = !!result.waiting_external
        } else if (row.role === 'node_core') {
          const shells = await this.call('runtime_shell_status', member)
          if (shells.unverified) continue
          waitingExternal = !!shells.running
        }
        await this.useModel(root, member)
        this.retried.delete(member)
        this.idle.delete(member)
        if (!waitingExternal && !this.busy.has(member)) await this.continueSession(member, await this.state(member), "自主研究已恢复。")
        resumed.push(member)
      }
      await this.deliver(root)
      await this.schedule(root)
      this.research.notifyChanged(root)
      return { message: `已恢复自主研究（${resumed.length} 个会话继续）。` }
    })
  }

  stop(sessionID: string | null, directory: string): Promise<CommandResult> {
    const root = this.rootFor(sessionID, directory)
    const main = this.mainIn(root)
    if (!main) throw new Error("这个项目的主会话不在 OpenCode 里。")
    return this.serial(root, async () => {
      await this.call("workflow", main, { action: "run", fields: { state: "stopping" } })
      const stoppingState = await this.state(main)
      const stoppingTasks = await this.allTasks(main, stoppingState.workflow)
      const interruptFailures: string[] = []
      // Request interruption, but do not claim that an external execution exited
      // merely because an interrupt RPC returned or an old idle record exists.
      for (const member of this.research.sessionsIn(root).filter((sessionID) => this.drives(sessionID))) {
        const current = await this.state(member).catch(() => null)
        const active = stoppingTasks.some((item: any) => item.session_id === member && item.state === 'unverified')
        if (this.busy.has(member) || active || this.runtime.pendingEnds.has(member) || this.pendingStarts.has(member)) {
          this.ours.add(member)
          await this.host.interrupt(member).catch(() => { this.ours.delete(member); interruptFailures.push(member) })
        }
      }
      let specialistWarning = ''
      try { await this.interruptSpecialists(root) }
      catch { specialistWarning = ' 专家中断请求未全部完成，请核对专家会话。' }
      const state = await this.state(main)
      for (const task of (await this.allTasks(main, state.workflow)).filter((item: any) => item.state === "queued")) {
        await this.call("workflow", main, { action: "task_state", fields: { task_id: task.task_id, state: "cancelled" } })
      }
      await this.call("workflow", main, { action: "session", fields: { pause_reason: null, waiting: [] } })
      await this.call("workflow", main, { action: "run", fields: { state: "stopped" } })
      this.research.notifyChanged(root)
      const warning = interruptFailures.length ? ` 中断请求失败：${interruptFailures.join('、')}；请检查宿主连接并核对执行。` : ''
      await this.say(main, `自主研究已停止；排队任务已取消，已尝试中断进行中的执行，退出状态需核实。${warning}`).catch(() => {})
      return { message: `已停止。排队任务已取消；已尝试中断进行中的执行，退出状态需核实。记录都保留。${warning}${specialistWarning}` }
    })
  }

  /** A host restart pauses the project; prior execution exit must still be verified. */
  async coldRecover(): Promise<void> {
    await this.research.ready()
    for (const root of this.research.roots().filter((item) => this.research.inScope(item))) {
      const main = this.mainIn(root)
      if (!main) continue
      await this.serial(root, async () => {
        const state = await this.state(main)
        const tasks = await this.allTasks(main, state.workflow)
        if (state.workflow?.run?.state === "running") {
          await this.call("workflow", main, { action: "run", fields: { state: "paused" } })
          if (!state.workflow?.session?.pause_reason || state.workflow.session.pause_reason === 'wait') {
            await this.call("workflow", main, { action: "session", fields: { pause_reason: "cold" } })
          }
        }
        for (const task of tasks.filter((item: any) => ["starting", "running", "waiting", "stopping"].includes(item.state))) {
          await this.call("workflow", main, { action: "task_state", fields: { task_id: task.task_id,
            state: "unverified", error: "host_restart: execution exit has not been verified" } })
        }
        this.research.notifyChanged(root)
      })
    }
  }
}
