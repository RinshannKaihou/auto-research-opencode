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
import { findProjectRoot } from "./research"

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
  create(title: string, directory: string, model?: ModelRef, permissions?: PermissionRule[]): Promise<string>
  switchModel(sessionID: string, model: ModelRef): Promise<void>
  /** Adds a message the model reads as user text; `label` is what the TUI shows. */
  synthetic(sessionID: string, text: string, label: string, resume: boolean): Promise<void>
  interrupt(sessionID: string): Promise<void>
  /** Resolves once the session is idle again, however its turn ended. */
  wait(sessionID: string): Promise<void>
  /** The session's messages; the record of a finished turn ends with `{ type: "idle", outcome }`. */
  messages(sessionID: string): Promise<any[]>
}

export interface ConclusionInput {
  summary: string
  final_ref: string
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
const WAKING = new Set(["finished", "stopped", "failed", "unknown"])
const NOTICE_OUTCOME: Record<string, string> = {
  finished: "已完成",
  stopped: "受阻停止",
  failed: "失败",
  unknown: "结束（状态未核实）",
  published: "发布了新材料",
}
/** Pauses that /research resume clears; wait and closed segments resolve themselves. */
const RESUMABLE = new Set(["project", "project_wait", "cold", "native_stop", "fault", "no_progress"])

export function oneLine(value: unknown, limit = 60): string {
  const text = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : ""
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

export class Engine {
  private busy = new Set<string>()
  /** Interrupts the engine issued itself; OpenCode reports every interrupt as "user". */
  private ours = new Set<string>()
  private wrote = new Map<string, boolean>()
  private idle = new Map<string, number>()
  private retried = new Set<string>()
  private finishing = new Map<string, { state: string; summary: string }>()
  private queues = new Map<string, Promise<unknown>>()
  /** Per project: the model the user chose in the TUI when starting or resuming. */
  private models = new Map<string, ModelRef>()

  constructor(
    private readonly research: Research,
    private readonly host: SessionHost,
    private readonly concurrency = 1,
  ) {}

  private serial<T>(root: string, work: () => Promise<T>): Promise<T> {
    const run = (this.queues.get(root) ?? Promise.resolve()).catch(() => {}).then(work)
    this.queues.set(root, run)
    return run
  }

  private call<T = any>(method: string, sessionID: string, fields: Record<string, unknown> = {}, operationId?: string) {
    return this.research.call<T>(method, sessionID, fields, operationId)
  }

  private state(sessionID: string): Promise<any> {
    return this.call("status", sessionID)
  }

  private mainIn(root: string): string | undefined {
    return this.research.sessionsIn(root).find((sessionID) => this.research.roleOf(sessionID) === "main")
  }

  private async say(sessionID: string, text: string, resume = false, label = text): Promise<void> {
    await this.host.synthetic(sessionID, `${PREFIX}${text}`, `${PREFIX}${label}`, resume)
  }

  /** A specialist's single turn belongs to the tool call that delegated it, not to the loop. */
  private drives(sessionID: string): boolean {
    return this.research.isAssociated(sessionID) && this.research.roleOf(sessionID) !== "specialist"
  }

  modelFor(root: string): ModelRef | undefined {
    return this.models.get(root)
  }

  // ── OpenCode execution events ────────────────────────────────────────────

  started(sessionID: string): void {
    if (!this.drives(sessionID)) return
    this.busy.add(sessionID)
    this.wrote.set(sessionID, false)
  }

  wroteLedger(sessionID: string): void {
    this.wrote.set(sessionID, true)
  }

  isBusy(sessionID: string): boolean {
    return this.busy.has(sessionID)
  }

  ended(sessionID: string, outcome: Outcome): Promise<void> {
    this.busy.delete(sessionID)
    const root = this.research.rootOf(sessionID)
    if (!root || !this.drives(sessionID)) return Promise.resolve()
    return this.serial(root, () => this.afterTurn(sessionID, root, outcome)).catch(() => {})
  }

  private async afterTurn(sessionID: string, root: string, outcome: Outcome): Promise<void> {
    if (outcome === "interrupted") {
      if (this.ours.delete(sessionID)) return
      await this.pauseSession(sessionID, "native_stop", "你中断了这一轮，本会话的自动推进已暂停；用 /research resume 继续。")
      return
    }
    const state = await this.state(sessionID)
    const row = state.workflow?.session
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
    const idle = this.wrote.get(sessionID) ? 0 : (this.idle.get(sessionID) ?? 0) + 1
    this.idle.set(sessionID, idle)
    const finish = this.finishing.get(sessionID)
    if (finish && row.role === "node_core") {
      this.finishing.delete(sessionID)
      await this.completeNode(sessionID, root, state, finish)
      return
    }
    if (row.role === "main" && row.pause_reason === "wait") {
      await this.deliver(root)
      return
    }
    if (row.pause_reason) return
    // A coordinator with nothing to do while its nodes run waits for them rather
    // than being nudged, or paused as idle, turn after turn.
    const running = row.role === "main" ? this.runningTasks(state, sessionID) : []
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
        `连续 ${IDLE_LIMIT} 轮没有写入任何研究记录，本会话的自动推进已暂停；用 /research resume 继续。`,
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
    if (root) this.research.notifyChanged(root)
  }

  // ── Node lifecycle ───────────────────────────────────────────────────────

  private async completeNode(
    sessionID: string,
    root: string,
    state: any,
    finish: { state: string; summary: string },
  ): Promise<void> {
    const task = (state.workflow?.tasks ?? []).find(
      (item: any) => item.session_id === sessionID && LIVE_TASK.has(item.state),
    )
    // Finishing the attempt also files the notice for the dispatching coordinator.
    if (state.attempt) await this.call("finish", sessionID, { state: finish.state, details: { reason: finish.summary } })
    if (task) await this.call("workflow", sessionID, { action: "task_state", fields: { task_id: task.task_id, state: "finished" } })
    await this.call("workflow", sessionID, { action: "session", fields: { pause_reason: "segment_complete" } })
    this.idle.delete(sessionID)
    await this.say(sessionID, `节点工作已结束（${finish.state === "finished" ? "完成" : "受阻停止"}），结果已交给主协调。`)
    this.research.notifyChanged(root)
    await this.deliver(root)
    await this.schedule(root)
  }

  private async noticeText(root: string, notice: any): Promise<string> {
    const payload = notice.payload ?? {}
    const pubs = await this.research.storage
      .request("project_read", { root, view: "page", collection: "publications", node_id: payload.node_id, limit: 5 })
      .catch(() => null)
    const refs = ((pubs as any)?.items ?? [])
      .map((pub: any) => `pub/${pub.publication_id}（${oneLine(pub.display?.title ?? pub.summary, 40)}）`)
      .join("、")
    const outcome = NOTICE_OUTCOME[payload.kind] ?? payload.kind
    return [
      `节点 ${payload.node_id} ${outcome}（任务 ${payload.task_id}）。`,
      `总结：${payload.summary || "（未提供）"}`,
      `发布：${refs || "无"}`,
    ].join("\n")
  }

  /** Hands pending node reports to the coordinator and wakes it if it was waiting for them. */
  private async deliver(root: string): Promise<void> {
    const main = this.mainIn(root)
    if (!main) return
    const state = await this.state(main)
    const row = state.workflow?.session
    const waiting: string[] = row?.waiting ?? []
    const notices = (state.workflow?.notifications ?? []).filter((notice: any) => notice.recipient === main)
    const texts: string[] = []
    let wake = false
    for (const notice of notices.filter((item: any) => item.state === "pending")) {
      texts.push(await this.noticeText(root, notice))
      await this.call("workflow", main, {
        action: "notification_state",
        fields: { notification_id: notice.notification_id, state: "delivered" },
      })
      if (waiting.includes(notice.task_id) && WAKING.has(notice.kind)) wake = true
    }
    // A waited task may have ended before the coordinator started waiting.
    if (notices.some((item: any) => item.state === "delivered" && waiting.includes(item.task_id) && WAKING.has(item.kind))) {
      wake = true
    }
    const canWake =
      wake && row?.pause_reason === "wait" && state.workflow?.run?.state === "running" && !this.busy.has(main)
    if (canWake) {
      await this.call("workflow", main, { action: "session", fields: { waiting: [], pause_reason: null } })
      this.idle.delete(main)
      const fresh = await this.state(main)
      const report = texts.length ? `${texts.join("\n\n")}\n\n` : ""
      await this.say(
        main,
        `${report}等待的节点有了结果。继续推进研究。${this.progressLine(fresh)}`,
        true,
        texts.length ? `收到 ${texts.length} 条节点消息，继续` : "等待结束，继续",
      )
    } else if (texts.length) {
      await this.say(main, texts.join("\n\n"), false, `收到 ${texts.length} 条节点消息`)
    }
  }

  private async schedule(root: string): Promise<void> {
    const main = this.mainIn(root)
    if (!main) return
    const state = await this.state(main)
    if (state.workflow?.run?.state !== "running") return
    const tasks = (state.workflow?.tasks ?? []) as any[]
    let active = tasks.filter((task) => ACTIVE_TASK.has(task.state)).length
    for (const task of tasks.filter((item) => item.state === "queued")) {
      if (active >= this.concurrency) break
      try {
        await this.startTask(root, main, task)
        active++
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        await this.call("workflow", main, {
          action: "task_state",
          fields: { task_id: task.task_id, state: "failed", error: message },
        }).catch(() => {})
        await this.say(main, `节点 ${task.node_id} 启动失败：${message}`)
      }
    }
  }

  private async startTask(root: string, main: string, task: any): Promise<void> {
    let workspace: string | null = task.cwd
    if (!workspace) {
      const prepared = await this.call("prepare_branch", main, { node_id: task.node_id }, `${task.task_id}:prepare`)
      workspace = prepared.workspace as string
    }
    await this.call("workflow", main, { action: "task_state", fields: { task_id: task.task_id, state: "starting" } })
    const context = await this.call("task_context", main, { task_id: task.task_id })
    let sessionID: string = task.session_id
    if (sessionID.startsWith("ses") && this.research.isAssociated(sessionID)) {
      await this.call("workflow", sessionID, { action: "session", fields: { pause_reason: null } })
      await this.useModel(root, sessionID)
    } else {
      sessionID = await this.host.create(`${task.node_id} · ${oneLine(context.question, 50)}`, root, this.models.get(root))
      await this.call("workflow", main, { action: "task_session", fields: { task_id: task.task_id, session_id: sessionID } })
      await this.call(
        "open",
        sessionID,
        { root, cwd: workspace, session_role: "node_core", node_id: task.node_id, context },
        `${task.task_id}:open:${sessionID}`,
      )
      this.research.register(sessionID, root, "node_core")
    }
    await this.call("focus", sessionID, { node_id: task.node_id, role: "core", mode: "auto" })
    await this.call("workflow", main, {
      action: "task_state",
      fields: { task_id: task.task_id, state: "running", cwd: workspace },
    })
    this.idle.delete(sessionID)
    const inputs = (context.inputs ?? []) as string[]
    await this.say(
      sessionID,
      [
        `你是节点 ${task.node_id} 的执行会话（任务 ${task.task_id}）。`,
        `问题：${context.question}`,
        `计划：${context.plan}`,
        `固定输入：${inputs.length ? `${inputs.join("、")}（已放在 ${workspace}/inputs/，只读）` : "无"}`,
        `工作目录：${workspace}。先 cd 到这里；在 scratch/ 里做实验，把报告和数据放进 output/。`,
        "发布时 source_path 写相对这个工作目录的路径，例如 output/report.md。",
        "完成条件：回答节点问题；用 research_publish 发布报告和数据，用 research_memory 记录引用发布材料的结论并更新检查点；完成或受阻时调用 research_finish。",
      ].join("\n"),
      true,
      `开始节点 ${task.node_id}`,
    )
    // Inside the coordinator's own dispatch call the tool result already says this.
    if (!this.busy.has(main)) await this.say(main, `已启动节点 ${task.node_id} 的执行会话（任务 ${task.task_id}）。`)
    this.research.notifyChanged(root)
  }

  // ── Engine tools called by models ────────────────────────────────────────

  dispatch(sessionID: string, nodeID: string, operationID: string): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      await this.call("validate_branch_inputs", sessionID, { node_id: nodeID })
      const task = await this.call("workflow", sessionID, { action: "task", fields: { node_id: nodeID } }, operationID)
      this.research.notifyChanged(root)
      await this.schedule(root)
      const lookup = await this.call("query", sessionID, { ref: task.task_id }).catch(() => null)
      return lookup?.value ?? task
    })
  }

  wait(sessionID: string, taskIDs: string[]): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    return this.serial(root, async () => {
      const state = await this.state(sessionID)
      if (state.workflow?.run?.state !== "running") throw new Error("自主研究没有在运行，不能等待")
      const mine = new Set(
        (state.workflow?.tasks ?? []).filter((task: any) => task.parent_session_id === sessionID).map((task: any) => task.task_id),
      )
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

  finish(sessionID: string, finish: { state: string; summary: string }): unknown {
    if (this.research.roleOf(sessionID) !== "node_core") throw new Error("只有节点执行会话可以调用 research_finish")
    this.finishing.set(sessionID, finish)
    return { message: "本轮结束后结束节点工作段，并把总结交给主协调。", state: finish.state }
  }

  conclude(sessionID: string, fields: ConclusionInput, operationID: string): Promise<unknown> {
    const root = this.research.requireAssociated(sessionID)
    if (this.research.roleOf(sessionID) !== "main") throw new Error("只有主协调可以结束研究")
    return this.serial(root, async () => {
      const conclusion = await this.call("conclude", sessionID, { fields }, operationID)
      let notification_warning: string | undefined
      try {
        this.research.notifyChanged(root)
        await this.say(sessionID, `研究执行已结束，结果 ${conclusion.outcome}，最终报告 ${conclusion.final_ref}。审阅状态 ${conclusion.review.status} 为调用者声明，不表示结论通过。`)
      } catch {
        notification_warning = "结项已保存，但会话通知发送失败；可通过状态或项目概览查看。"
      }
      return { message: "研究执行已结束，结项记录已保存，自动推进停止。", ...conclusion,
        ...(notification_warning ? { notification_warning } : {}) }
    })
  }

  // ── User commands ────────────────────────────────────────────────────────

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

  /** Starts or restarts autonomous research; creates the project first when a goal is given. */
  async auto(sessionID: string | null, directory: string, goal: string, model?: ModelRef): Promise<CommandResult> {
    await this.research.ready()
    let created: CommandResult | null = null
    if (!findProjectRoot(directory)) {
      if (!sessionID) throw new Error("请在会话里执行，或给出研究目标。")
      if (!goal.trim()) throw new Error("当前目录还没有研究项目。用法：/research auto <研究目标>")
      created = await this.research.init(sessionID, directory, goal)
    }
    const root = this.rootFor(sessionID, directory)
    const main = this.mainIn(root)
    if (!main) throw new Error("这个项目的主会话不在 OpenCode 里。先用 /research takeover 接管。")
    if (model) this.models.set(root, model)
    return this.serial(root, async () => {
      await this.useModel(root, main)
      const state = await this.state(main)
      const run = state.workflow?.run?.state
      if (run === "running") {
        return { message: "自主研究已经在运行。", sessionID: main }
      }
      if (run === "paused" || run === "cold") {
        throw new Error("项目处于暂停状态；用 /research resume 继续。")
      }
      await this.call("workflow", main, { action: "run", fields: { state: "running", new_generation: true } })
      if (state.attempt && state.attempt.mode !== "auto") {
        await this.call("finish", main, { state: "finished", details: { reason: "switch-to-autonomous" } })
      }
      if (!state.attempt || state.attempt.mode !== "auto") {
        await this.call("focus", main, { node_id: null, role: "planner", mode: "auto" })
      }
      await this.call("workflow", main, { action: "session", fields: { pause_reason: null, waiting: [] } })
      this.idle.delete(main)
      this.research.notifyChanged(root)
      if (!this.busy.has(main)) {
        await this.say(
          main,
          `开始自主研究。目标：${state.project?.goal}\n先盘点已有材料和记录，然后规划节点、派发执行；结果回来后综合，目标回答后发布最终报告并调用 research_conclude。`,
          true,
          "开始自主研究",
        )
      }
      await this.schedule(root)
      return {
        message: `${created ? "已建立研究项目，" : ""}自主研究已开始。进展会显示在侧栏、状态条和 /research board 里。`,
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
      const resumed: string[] = []
      for (const member of this.research.sessionsIn(root).filter((sessionID) => this.drives(sessionID))) {
        const current = await this.state(member)
        const row = current.workflow?.session
        if (!row || row.detached || !["main", "node_core"].includes(row.role)) continue
        if (row.pause_reason && !RESUMABLE.has(row.pause_reason)) continue
        const task = (current.workflow?.tasks ?? []).find((item: any) => item.session_id === member && ACTIVE_TASK.has(item.state))
        if (row.role === "node_core" && !task) continue
        if (row.pause_reason) await this.call("workflow", member, { action: "session", fields: { pause_reason: null } })
        await this.useModel(root, member)
        this.retried.delete(member)
        this.idle.delete(member)
        if (!this.busy.has(member)) await this.continueSession(member, await this.state(member), "自主研究已恢复。")
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
      // Interrupting a node also cancels the specialist its tool call is waiting for.
      for (const member of this.research.sessionsIn(root).filter((sessionID) => this.drives(sessionID))) {
        if (this.busy.has(member)) {
          this.ours.add(member)
          await this.host.interrupt(member).catch(() => this.ours.delete(member))
        }
        const current = await this.state(member).catch(() => null)
        if (!current) continue
        if (current.attempt && current.workflow?.session?.role === "node_core") {
          await this.call("finish", member, { state: "stopped", details: { reason: "project-stop" } }).catch(() => {})
        }
        for (const task of (current.workflow?.tasks ?? []).filter((item: any) => item.session_id === member && LIVE_TASK.has(item.state))) {
          await this.call("workflow", main, { action: "task_state", fields: { task_id: task.task_id, state: "cancelled" } })
        }
      }
      const state = await this.state(main)
      for (const task of (state.workflow?.tasks ?? []).filter((item: any) => item.state === "queued")) {
        await this.call("workflow", main, { action: "task_state", fields: { task_id: task.task_id, state: "cancelled" } })
      }
      await this.call("workflow", main, { action: "session", fields: { pause_reason: null, waiting: [] } })
      await this.call("workflow", main, { action: "run", fields: { state: "stopped" } })
      this.research.notifyChanged(root)
      await this.say(main, "自主研究已停止；排队和进行中的节点任务已取消。用 /research auto 重新开始。")
      return { message: "已停止。进行中的会话已中断，节点任务已取消；记录都保留。用 /research auto 重新开始。" }
    })
  }

  /** OpenCode restarted: nothing is running any more, so a running project waits for /research resume. */
  async coldRecover(): Promise<void> {
    await this.research.ready()
    for (const root of this.research.roots().filter((item) => this.research.inScope(item))) {
      const main = this.mainIn(root)
      if (!main) continue
      await this.serial(root, async () => {
        const state = await this.state(main)
        if (state.workflow?.run?.state !== "running") return
        await this.call("workflow", main, { action: "run", fields: { state: "paused" } })
        await this.call("workflow", main, { action: "session", fields: { pause_reason: "cold" } }).catch(() => {})
        this.research.notifyChanged(root)
      }).catch(() => {})
    }
  }
}
