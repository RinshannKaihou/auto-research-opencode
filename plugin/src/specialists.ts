/**
 * Node specialists: bounded read-only helpers a node executor delegates inside
 * its own turn. The ledger records each one (task, lineage, exit state and
 * report); OpenCode runs it as a session of its own that is held to read-only
 * tools twice, by the session's permission rules and by the per-request tool
 * filter. The delegating tool call waits for the specialist, so interrupting
 * or stopping the node ends the specialist too. A blind reviewer sees only its
 * assigned inputs: no workspace, no research records, none of OpenCode's prompt.
 */
import { mkdirSync, unlinkSync, writeFileSync } from "node:fs"
import { createHash, randomUUID } from "node:crypto"
import { join } from "node:path"
import { wasNotSent } from './host-submission'
import { oneLine, type ModelRef, type PermissionRule, type SessionHost } from "./engine"
import type { Research } from "./research"

export interface SpecialistTask {
  label: string
  question: string
  purpose: string
  inputs?: string[]
  tool_scope?: string[]
  node_id?: string
  context_mode?: "research" | "blind"
  deliverable: string
  completion_criteria: string
  report_requirements: string
}

const TOOLS_BY_MODE: Record<string, readonly string[]> = {
  research: ["read", "glob", "grep", "research_query", "research_read_input"],
  blind: ["research_read_input"],
}
/** The whole system prompt of a blind reviewer's requests; its research context follows. */
export const BLIND_SYSTEM =
  "You review materials independently. You see only the inputs assigned to you; judge them on their own content."
const FINAL_REPORT =
  "你的最后一条回复就是交回的报告：写明每条结论和它的依据（材料、文件路径或研究引用），以及仍不确定的地方；证据不足时直接写明「未完成」和原因。"
/** Report text handed back to the node; the full report stays in the ledger. */
const PREVIEW_CHARS = 1600
/** How long an interrupted specialist may take to stop before its exit counts as unverified. */
export const STOP_GRACE_MS = 60_000
/**
 * One tool-free follow-up after a stall or the absolute cap.
 * Long enough for a reasoning reply. The summary turn does not turn reasoning down.
 */
export const SUMMARY_GRACE_MS = 60_000
/** One host read or busy probe after the summary grace. Not a plugin option. */
const HOST_PROBE_MS = 2_000
/** Silence that stops a specialist. Any new reasoning, text, or tool activity resets it. */
export const SPECIALIST_STALL_MS = 300_000
/** Absolute backstop for a specialist that never goes quiet. */
export const SPECIALIST_TIMEOUT_MS = 3_600_000
/** Not a plugin option. Tests set this before plugin setup so they do not wait out the production grace. */
export const specialistGraceForTest: { stopMs?: number; summaryMs?: number } = {}

const PROGRESS_EVENTS = new Set([
  "session.reasoning.started",
  "session.reasoning.delta",
  "session.reasoning.ended",
  "session.text.started",
  "session.text.delta",
  "session.text.ended",
  "session.tool.input.started",
  "session.tool.input.delta",
  "session.tool.input.ended",
  "session.tool.called",
  "session.tool.progress",
  "session.tool.success",
  "session.tool.failed",
  "session.step.started",
  "session.step.streamed",
  "session.message.content.updated",
])

/** Streamed reasoning, text, or tool activity in a child session. Status heartbeats are not progress. */
export function specialistProgressEvent(type: unknown): boolean {
  return typeof type === "string" && PROGRESS_EVENTS.has(type)
}
const PARTIAL_REPORT = "达到专家时间上限，已收回部分报告"
const NO_USABLE_REPORT = "达到专家时间上限，没有可用报告"
const STALL_PARTIAL = "专家一段时间没有新的推进，已收回部分报告"
const STALL_NONE = "专家一段时间没有新的推进，没有可用报告"
type LimitKind = "stall" | "cap"
const UNVERIFIED_MARK = "【未完成·未核实】"
const SUMMARY_PROMPT = [
  "时间已到。不要使用任何工具。",
  "请只根据本会话里已经出现的文字、推理和工具结果，写一份报告：列出已看到的发现和依据，并写明仍不确定、尚未核实的地方。",
  "不要把开场的进度说明当作结论。",
].join("\n")

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function withoutPrompt(task: any): any {
  const { prompt: _prompt, ...rest } = task ?? {}
  return rest
}

/** The tools a specialist gets: its mode's read-only set, or the narrower scope its task names. */
export function specialistTools(task: SpecialistTask): readonly string[] {
  const allowed = TOOLS_BY_MODE[task.context_mode ?? "research"]!
  if (!task.tool_scope?.length) return allowed
  const outside = task.tool_scope.filter((name) => !allowed.includes(name))
  if (outside.length) {
    throw new Error(`tool_scope 只能从这些工具里选：${allowed.join("、")}；不能用 ${outside.join("、")}。`)
  }
  return [...new Set(task.tool_scope)]
}

/** Session rules: deny everything, then allow the specialist's tools. Later rules win. */
export function permissionsFor(tools: readonly string[]): PermissionRule[] {
  const rules: PermissionRule[] = [{ action: "*", resource: "*", effect: "deny" }]
  for (const action of tools) rules.push({ action, resource: "*", effect: "allow" })
  // Allowing read would otherwise also open the secrets OpenCode asks about.
  if (tools.includes("read")) {
    for (const resource of ["*.env", "*.env.*"]) rules.push({ action: "read", resource, effect: "deny" })
  }
  return rules
}

/** The task as the ledger stores it and the specialist reads it. */
export function taskText(task: SpecialistTask): string {
  const inputs = task.inputs ?? []
  // A blind reviewer never learns where its inputs come from.
  const materials = !inputs.length
    ? "无"
    : task.context_mode === "blind"
      ? `${inputs.map((_, index) => `input-${index + 1}`).join("、")}（用 research_read_input 按编号分段读取）`
      : `${inputs.join("、")}（用 research_query 读取）`
  return [
    `问题：${task.question}`,
    `目的：${task.purpose}`,
    `材料：${materials}`,
    `交付物：${task.deliverable}`,
    `完成标准：${task.completion_criteria}`,
    `回报要求：${task.report_requirements}`,
    "保留不确定性；证据不足时如实写明未完成。",
  ].join("\n")
}

function kickoff(task: any, workspace: string, tools: readonly string[]): string {
  if (task.context_mode === "blind") {
    return [
      `你是一位盲评专家（任务 ${task.task_id}）：只根据分配给你的材料独立作判断，不要推测材料的作者、来源或预期结论。`,
      task.prompt,
      `可用工具：${tools.join("、")}。`,
      FINAL_REPORT,
    ].join("\n")
  }
  return [
    `你是节点 ${task.node_id} 的专家（任务 ${task.task_id}），只读：可以查看文件和研究记录，不能运行命令、修改任何东西或再委派别人。`,
    task.prompt,
    `节点工作目录：${workspace}。可用工具：${tools.join("、")}。`,
    FINAL_REPORT,
  ].join("\n")
}

function assistantText(message: any): string {
  return (message?.content ?? [])
    .filter((part: any) => part?.type === "text")
    .map((part: any) => String(part.text ?? ""))
    .join("\n")
    .trim()
}

/** How a specialist session's turn ended and the last text it wrote. */
export function sessionResult(messages: any[]): { outcome: string | null; text: string } {
  const last = messages.at(-1)
  const outcome = last?.type === "idle" && last.outcome ? String(last.outcome) : null
  for (let index = messages.length - 1; index >= 0; index--) {
    const item = messages[index]
    if (item?.type !== "assistant") continue
    const text = assistantText(item)
    if (text) return { outcome, text }
  }
  return { outcome, text: "" }
}

function messageID(item: any): string {
  return String(item?.id ?? item?.info?.id ?? "")
}

/** A host call that must return. `fallback` is used when it outlives `ms`. */
function probeWithin<T>(work: Promise<T>, fallback: T, ms = HOST_PROBE_MS): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  return Promise.race([
    work,
    new Promise<T>((resolve) => { timer = setTimeout(() => resolve(fallback), ms) }),
  ]).finally(() => clearTimeout(timer))
}

/** Text written after the summary prompt. Earlier progress lines are not part of it. */
function textSince(messages: any[], id: string): string {
  const start = messages.findIndex((item) => messageID(item) === id)
  if (start < 0) return ""
  return sessionResult(messages.slice(start + 1)).text
}

function markUnverified(text: string): string {
  return `${UNVERIFIED_MARK}\n${text.trim()}`
}

function limitCopy(limit: LimitKind, usable: boolean): string {
  if (limit === "stall") return usable ? STALL_PARTIAL : STALL_NONE
  return usable ? PARTIAL_REPORT : NO_USABLE_REPORT
}

function verdict(exited: boolean, outcome: string | null, text: string, limit: LimitKind | null) {
  if (!exited) return { state: "unverified", error: "专家会话被中断后没有按时停下，退出未核实" }
  if (outcome === "succeeded") {
    return text ? { state: "completed", error: null } : { state: "incomplete", error: "专家没有给出文字报告" }
  }
  if (outcome === "interrupted") {
    return limit
      ? { state: "incomplete", error: limitCopy(limit, true) }
      : { state: "cancelled", error: "专家这一轮被中断" }
  }
  return { state: "incomplete", error: `专家会话以「${outcome ?? "未知"}」结束` }
}

export class Specialists {
  /** Tools of each specialist session this process started, for the per-request filter. */
  private tools: Map<string, readonly string[]>
  private researchMode: Set<string>
  /** Last streamed activity for a specialist this process is watching. Missing means it is not watched. */
  private progressAt = new Map<string, number>()
  /** Specialist children currently busy. Idle and deleted sessions are removed. */
  private sessionBusy = new Map<string, boolean>()

  constructor(
    private readonly research: Research,
    private readonly host: SessionHost,
    private readonly modelFor: (root: string) => ModelRef | undefined,
    private readonly fanout = 2,
    private readonly timeoutMs = SPECIALIST_TIMEOUT_MS,
    private readonly stallMs = SPECIALIST_STALL_MS,
    runtime?: { specialistTools: Map<string, readonly string[]>; specialistResearch: Set<string> },
    private readonly current: () => boolean = () => true,
    /** Tests pass a shorter wait. Production keeps `STOP_GRACE_MS`. */
    private readonly stopGraceMs = STOP_GRACE_MS,
    /** Tests pass a shorter wait. Production keeps `SUMMARY_GRACE_MS`. */
    private readonly summaryGraceMs = SUMMARY_GRACE_MS,
  ) {
    this.tools = runtime?.specialistTools ?? new Map()
    this.researchMode = runtime?.specialistResearch ?? new Set()
  }

  /** Resets the stall window. Events for a session that is not being watched are ignored. */
  noteProgress(sessionID: string): void {
    if (sessionID && this.progressAt.has(sessionID)) this.progressAt.set(sessionID, Date.now())
  }

  /** Remembers a busy specialist so a late stop can refuse a verified exit. */
  noteBusy(sessionID: string, status: unknown): void {
    if (!sessionID) return
    if (status === "idle" || status === "deleted") {
      this.sessionBusy.delete(sessionID)
      return
    }
    if ((status === "busy" || status === "retry") && (this.tools.has(sessionID) || this.progressAt.has(sessionID))) {
      this.sessionBusy.set(sessionID, true)
    }
  }

  /** False when this process has not seen a busy status for the session. */
  appearsBusy(sessionID: string): boolean {
    return this.sessionBusy.get(sessionID) === true
  }

  /** Prefer the host's own busy bit. A missing, failed, or slow probe is not proof the child went idle. */
  private childBusy(child: string): Promise<boolean> {
    const probe = (async (): Promise<boolean> => {
      try {
        if (this.host.busy) return (await this.host.busy(child)) === true
        return this.appearsBusy(child)
      } catch {
        return true
      }
    })()
    return probeWithin(probe, true)
  }

  /** None for a specialist from before a restart; its session's permission rules still hold. */
  toolsOf(sessionID: string): readonly string[] {
    return this.tools.get(sessionID) ?? []
  }

  /** Whether a specialist must not see the project; one from before a restart counts as blind, to be safe. */
  isBlind(sessionID: string): boolean {
    return !this.researchMode.has(sessionID)
  }

  async delegate(parent: string, task: SpecialistTask, signal: AbortSignal | undefined, operationID: string): Promise<unknown> {
    const root = this.research.requireAssociated(parent)
    const reserved = await this.reserve(parent, task, operationID)
    return this.run(root, parent, task, reserved, signal, operationID)
  }

  async delegateBatch(
    parent: string,
    tasks: SpecialistTask[],
    signal: AbortSignal | undefined,
    operationID: string,
  ): Promise<unknown> {
    const root = this.research.requireAssociated(parent)
    if (!tasks.length || tasks.length > this.fanout) {
      throw new Error(`一批专家必须是 1 到 ${this.fanout} 个；没有启动任何专家。`)
    }
    tasks.forEach(specialistTools)
    // Reserve the whole batch before starting anyone, so a refused batch starts nothing.
    const reserved: any[] = []
    try {
      for (const [index, task] of tasks.entries()) reserved.push(await this.reserve(parent, task, `${operationID}:${index}`))
    } catch (error) {
      for (const item of reserved) {
        // A replayed reservation may already have acquired its launch claim.
        // Only a transaction that sees `prepared` may declare it unstarted.
        await this.research.call('specialist_cancel_unstarted', parent, { task_id: item.task_id },
          `${operationID}:${item.task_id}:cancel`).catch(() => {})
      }
      throw error
    }
    const results = await Promise.allSettled(
      reserved.map((item, index) => this.run(root, parent, tasks[index]!, item, signal, `${operationID}:${index}`)),
    )
    return {
      tasks: results.map((result, index) =>
        result.status === "fulfilled" ? result.value : { task_id: reserved[index].task_id, error: message(result.reason) },
      ),
    }
  }

  /** The ledger checks the caller's role, its live node attempt, the inputs and the node's specialist limit. */
  private reserve(parent: string, task: SpecialistTask, operationID: string): Promise<any> {
    const tools = specialistTools(task)
    const digest = createHash('sha256').update(operationID).digest('hex').slice(0, 32)
    return this.research.call(
      "specialist_create",
      parent,
      {
        fields: {
          purpose: "domain",
          label: task.label,
          prompt: taskText(task),
          inputs: task.inputs ?? [],
          ...(task.node_id ? { node_id: task.node_id } : {}),
          context_mode: task.context_mode ?? "research",
          fanout_limit: this.fanout,
          launch: { tools: [...tools], deadline_at: Date.now() + this.timeoutMs,
            session_id: `ses_research_${digest}`, message_id: `msg_research_${digest}` },
        },
        model_call: true,
      },
      `${operationID}:create`,
    )
  }

  private async run(
    root: string,
    parent: string,
    task: SpecialistTask,
    reserved: any,
    signal: AbortSignal | undefined,
    operationID: string,
  ): Promise<unknown> {
    // A replayed call returns the recorded task instead of starting a second specialist.
    if (reserved.state !== "starting") return withoutPrompt(reserved)
    const launch = reserved.launch
    if (!launch) return { task_id: reserved.task_id, state: 'unverified', error: '专家启动身份未记录，需先核对。' }
    const claimed = await this.research.call('specialist_launch_phase', parent, { intent_id: launch.intent_id, phase: 'creating' })
    if (!claimed.claimed) return withoutPrompt(await this.research.call('specialist_get', parent, { task_id: reserved.task_id }))
    const tools: readonly string[] = launch.tools
    const controller = new AbortController()
    let limit: LimitKind | null = null
    const disarm = this.armLimits(String(launch.session_id), launch.deadline_at, controller, (kind) => {
      if (controller.signal.aborted || limit) return
      limit = kind
    })
    const cancel = () => controller.abort()
    signal?.addEventListener("abort", cancel, { once: true })
    if (signal?.aborted) controller.abort()
    let child: string | undefined
    let createAttempted = false
    let kickoffAttempted = false
    try {
      const parentState = await this.research.call("status", parent)
      const workspace: string = parentState.workflow?.session?.cwd ?? root
      const kind = reserved.context_mode === "blind" ? "盲评" : "专家"
      createAttempted = true
      const created = await this.host.create(
        `${reserved.node_id ?? "项目"} · ${kind} · ${oneLine(reserved.label, 40)}`,
        root,
        this.modelFor(root),
        permissionsFor(tools),
        launch.session_id,
      )
      if (created !== launch.session_id) throw new Error('宿主没有确认预定的专家会话 ID')
      child = created
      await this.research.call('specialist_launch_phase', parent, { intent_id: launch.intent_id, phase: 'created' }, `${operationID}:created`)
      await this.research.call(
        "specialist_bind_child",
        parent,
        { task_id: reserved.task_id, child_session_id: child, node_id: reserved.node_id, cwd: workspace },
        `${operationID}:bind`,
      )
      this.research.register(child, root, "specialist", reserved.node_id ?? undefined)
      this.tools.set(child, tools)
      if (reserved.context_mode !== "blind") this.researchMode.add(child)
      await this.research.call('specialist_launch_phase', parent, { intent_id: launch.intent_id, phase: 'bound' }, `${operationID}:bound`)
      this.research.notifyChanged(root)
      kickoffAttempted = true
      await this.host.synthetic(child, kickoff(reserved, workspace, tools), `专家任务 ${reserved.task_id}`, true,
        launch.message_id, undefined, undefined, launch.intent_id)
      await this.research.call('specialist_launch_phase', parent, { intent_id: launch.intent_id, phase: 'submitted' }, `${operationID}:submitted`)
      const exited = await this.finished(child, controller.signal)
      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified', error: '控制器已重绑定，等待新实例核对。' }
      const reason = limit
      disarm()
      return await this.finishChild(root, parent, reserved.task_id, child, exited, reason, operationID, signal)
    } catch (error) {
      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified', error: '控制器已重绑定，等待新实例核对。' }
      const stage = !child && (!createAttempted || wasNotSent(error, 'create')) ? 'create'
        : child && (!kickoffAttempted || wasNotSent(error, 'synthetic')) ? 'synthetic' : undefined
      if (stage) {
        const undone = await this.research.call('runtime_outbound_not_sent', parent, {
          intent_id: launch.intent_id, stage, reason: message(error),
        }).catch(() => null)
        if (undone?.rolled_back) return withoutPrompt(await this.research.call('specialist_get', parent, { task_id: reserved.task_id }))
      }
      // An RPC/archive/bind failure can leave a live child too. The current
      // owner must request its exit even when no timeout or abort occurred.
      const exited = child ? await this.stop(child).catch(() => false) : false
      return this.settle(
        parent,
        reserved.task_id,
        {
          state: exited ? "incomplete" : "unverified",
          result: { session_id: child ?? null },
          error: message(error).slice(0, 1800),
          exit_verified: exited,
        },
        `${operationID}:error`,
      )
    } finally {
      disarm()
      signal?.removeEventListener("abort", cancel)
    }
  }

  /**
   * Stops the specialist after `stallMs` without streamed activity, or at `deadlineAt`
   * even when activity keeps arriving. The callback runs before the abort.
   */
  private armLimits(
    sessionID: string,
    deadlineAt: number,
    controller: AbortController,
    mark: (kind: LimitKind) => void,
  ): () => void {
    this.progressAt.set(sessionID, Date.now())
    let armed = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = () => {
      if (!armed || controller.signal.aborted) return
      const now = Date.now()
      const quiet = now - (this.progressAt.get(sessionID) ?? now)
      if (!armed) return
      if (now >= deadlineAt) {
        mark("cap")
        controller.abort()
        return
      }
      if (quiet >= this.stallMs) {
        mark("stall")
        controller.abort()
        return
      }
      timer = setTimeout(tick, Math.max(0, Math.min(deadlineAt - now, this.stallMs - quiet)))
    }
    timer = setTimeout(tick, Math.max(0, Math.min(deadlineAt - Date.now(), this.stallMs)))
    return () => {
      armed = false
      clearTimeout(timer)
      this.progressAt.delete(sessionID)
    }
  }

  /** Waits for the specialist's turn to end; on abort it is interrupted first. False if it would not stop. */
  private async finished(child: string, signal: AbortSignal): Promise<boolean> {
    const idle = this.host.wait(child)
    if (!signal.aborted) {
      let aborted!: () => void
      const abort = new Promise<"abort">((resolve) => (aborted = () => resolve("abort")))
      signal.addEventListener("abort", aborted, { once: true })
      const first = await Promise.race([idle.then(() => "idle" as const), abort])
      signal.removeEventListener("abort", aborted)
      if (first === "idle") return true
    }
    return this.stop(child, idle)
  }

  private async stop(child: string, idle: Promise<void> = this.host.wait(child)): Promise<boolean> {
    const exited = idle.then(() => true, () => false)
    await this.host.interrupt(child).catch(() => {})
    let timer: ReturnType<typeof setTimeout> | undefined
    const late = new Promise<boolean>((resolve) => (timer = setTimeout(() => resolve(false), this.stopGraceMs)))
    try {
      return await Promise.race([exited, late])
    } finally {
      clearTimeout(timer)
    }
  }

  /**
   * One tool-free follow-up after a timeout. Host calls share the summary grace.
   * `exited` is false when the child cannot be confirmed stopped. A summary send
   * that has not been accepted yet is in that group: it can still start a turn.
   */
  private async summarize(child: string, signal?: AbortSignal): Promise<{ text: string; exited: boolean }> {
    if (signal?.aborted || !this.current()) return { text: "", exited: true }
    this.tools.set(child, [])
    const summaryMessageID = `msg_research_${randomUUID().replaceAll("-", "")}`
    const controller = new AbortController()
    let resolveDeadline!: (value: "deadline") => void
    const deadline = new Promise<"deadline">((resolve) => { resolveDeadline = resolve })
    let fromAbort = false
    const onAbort = () => {
      controller.abort()
      resolveDeadline("deadline")
    }
    const onSignal = () => {
      fromAbort = true
      onAbort()
    }
    signal?.addEventListener("abort", onSignal, { once: true })
    const timer = setTimeout(onAbort, this.summaryGraceMs)
    // A host call that never settles must not keep the delegating tool waiting past the grace.
    const bounded = async <T>(work: Promise<T>): Promise<T | "deadline"> => {
      const settled = work.then(
        (value) => ({ ok: true as const, value }),
        (error: unknown) => ({ ok: false as const, error }),
      )
      const winner = await Promise.race([settled, deadline])
      if (winner === "deadline") return "deadline"
      if (!winner.ok) throw winner.error
      return winner.value
    }
    const stopped = () => this.stop(child).catch(() => false)
    // Once the delegating call has moved on, a late send must not leave a turn running.
    const stopLateSend = (send: Promise<unknown>) => {
      void send.then(() => this.stop(child).catch(() => {}), () => this.stop(child).catch(() => {}))
    }
    try {
      const send = this.host.synthetic(child, SUMMARY_PROMPT, "专家超时后的未核实摘要", true, summaryMessageID)
      const sent = await bounded(send)
      if (sent === "deadline") {
        // `send` has not settled. A parent abort can still stop a turn that already
        // accepted the summary; the grace timer cannot spend another grace waiting
        // to discover that. An idle child is not a verified exit: the send can
        // still start a turn, and that turn is stopped if the send later lands.
        const aborted = fromAbort
        const reading = this.host.messages(child).catch(() => [] as any[])
        const exited = await stopped()
        const messages = await Promise.race([
          reading,
          new Promise<any[]>((resolve) => {
            const probe = setTimeout(() => resolve([]), aborted ? this.summaryGraceMs : 0)
            void reading.then(() => clearTimeout(probe), () => clearTimeout(probe))
          }),
        ])
        if (messages.some((item) => messageID(item) === summaryMessageID)) {
          if (exited && await this.childBusy(child)) return { text: "", exited: false }
          return { text: "", exited }
        }
        stopLateSend(send)
        return { text: "", exited: false }
      }
      const exited = await this.finished(child, controller.signal)
      if (!exited) return { text: "", exited: false }
      if (controller.signal.aborted) {
        if (fromAbort) return { text: "", exited: true }
        // The grace already fired, so the shared deadline cannot bound this read.
        const messages = await probeWithin(this.host.messages(child).catch(() => [] as any[]), [] as any[])
        return { text: textSince(messages, summaryMessageID), exited: true }
      }
      const messages = await bounded(this.host.messages(child).catch(() => []))
      if (messages === "deadline" || controller.signal.aborted) return { text: "", exited: true }
      return { text: textSince(messages, summaryMessageID), exited: true }
    } catch {
      return { text: "", exited: await stopped() }
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener("abort", onSignal)
    }
  }

  /** Records the specialist's report. A stall or the absolute cap is summarized instead of replaying an earlier progress line. */
  private async finishChild(
    root: string,
    parent: string,
    taskID: string,
    child: string,
    exited: boolean,
    limit: LimitKind | null,
    operationID: string,
    signal?: AbortSignal,
  ): Promise<unknown> {
    if (!this.current()) return { task_id: taskID, state: "unverified", error: "控制器已重绑定，等待新实例核对。" }
    const found = sessionResult(await this.host.messages(child).catch(() => []))
    if (limit && exited && found.outcome !== "succeeded") {
      const summary = signal?.aborted ? { text: "", exited: true } : await this.summarize(child, signal)
      if (!this.current()) return { task_id: taskID, state: "unverified", error: "控制器已重绑定，等待新实例核对。" }
      const stopReason = found.outcome ?? "interrupted"
      if (!summary.exited) {
        const failed = verdict(false, stopReason, "", null)
        return this.settle(parent, taskID, {
          state: failed.state,
          error: failed.error,
          exit_verified: false,
          result: { stop_reason: stopReason, session_id: child, preview: "" },
        }, `${operationID}:finish`)
      }
      if (summary.text) {
        const text = markUnverified(summary.text)
        let archiveError: string | undefined
        const artifact = await this.archive(root, parent, taskID, stopReason, text, operationID, true).catch((failure) => {
          archiveError = message(failure)
          return null
        })
        return this.settle(parent, taskID, {
          state: "incomplete",
          error: limitCopy(limit, true),
          exit_verified: true,
          result: {
            stop_reason: stopReason,
            session_id: child,
            preview: text.slice(0, PREVIEW_CHARS),
            incomplete: true,
            unverified: true,
            ...(artifact ? { artifact, report_path: join(root, artifact.path) } : {}),
            ...(archiveError ? { archive_error: archiveError } : {}),
          },
        }, `${operationID}:finish`)
      }
      return this.settle(parent, taskID, {
        state: "incomplete",
        error: limitCopy(limit, false),
        exit_verified: true,
        result: { stop_reason: stopReason, session_id: child, preview: "" },
      }, `${operationID}:finish`)
    }
    const { state, error } = verdict(exited, found.outcome, found.text, limit)
    // An unverified exit must not freeze an earlier progress line as the report.
    const report = exited ? found.text : ""
    let archiveError: string | undefined
    const artifact = report
      ? await this.archive(root, parent, taskID, found.outcome, report, operationID).catch((failure) => {
          archiveError = message(failure)
          return null
        })
      : null
    return this.settle(parent, taskID, {
      state,
      error,
      exit_verified: exited,
      result: {
        stop_reason: found.outcome,
        session_id: child,
        preview: report.slice(0, PREVIEW_CHARS),
        ...(artifact ? { artifact, report_path: join(root, artifact.path) } : {}),
        ...(archiveError ? { archive_error: archiveError } : {}),
      },
    }, `${operationID}:finish`)
  }

  /** Freezes the report into the ledger's objects, the same file format as the DSH plugin. */
  private async archive(
    root: string,
    parent: string,
    taskID: string,
    outcome: string | null,
    text: string,
    operationID: string,
    partial = false,
  ): Promise<{ path: string; version: string; kind: string }> {
    const directory = join(root, ".research", "specialist-results")
    mkdirSync(directory, { recursive: true })
    const path = join(directory, `${taskID}.json`)
    const body = {
      stop_reason: outcome,
      ...(partial ? { incomplete: true, unverified: true } : {}),
      output: [{ type: "text", text }],
    }
    writeFileSync(path, JSON.stringify(body), { mode: 0o600 })
    try {
      return await this.research.call("specialist_result_import", parent, { task_id: taskID }, `${operationID}:result`)
    } finally {
      try {
        unlinkSync(path)
      } catch {}
    }
  }

  private async settle(parent: string, taskID: string, fields: Record<string, unknown>, operationID: string): Promise<unknown> {
    const value = await this.research.call("specialist_finish", parent, { fields: { task_id: taskID, ...fields } }, operationID)
    const root = this.research.rootOf(parent)
    if (root) this.research.notifyChanged(root)
    return withoutPrompt(value)
  }

  private async activeTasks(root: string, includeUnverified = false): Promise<any[]> {
    const tasks: any[] = []
    let cursor: Record<string, unknown> | null = null
    do {
      if (tasks.length >= 10_000) throw new Error('专家任务过多，需先缩小恢复范围。')
      const page: any = await this.research.storage.request('project_read',
        { root, view: 'page', collection: 'specialists', limit: 100, ...(cursor ?? {}) })
      tasks.push(...(page.items ?? []).filter((item: any) =>
        ['starting', 'running', ...(includeUnverified ? ['unverified'] : [])].includes(item.state)))
      cursor = page.cursor ?? null
    } while (cursor)
    return tasks
  }

  /** Stop requests are best-effort; a host interrupt is not proof of process exit. */
  async interruptUnverified(root: string): Promise<void> {
    const tasks = await this.activeTasks(root, true)
    let failed = false
    for (const session of new Set<string>(tasks.map(task => task.child_session_id).filter(Boolean))) {
      try { await this.host.interrupt(session) } catch { failed = true }
    }
    if (failed) throw new Error('Some specialist interrupt requests failed')
  }

  /** Recreate watchers from persisted identities, scope and the original deadline. */
  async recoverWarm(): Promise<void> {
    await this.research.ready()
    for (const root of this.research.roots().filter(item => this.research.inScope(item))) {
      const main = this.research.sessionsIn(root).find(sid => this.research.roleOf(sid) === 'main')
      if (!main) continue
      for (const task of await this.activeTasks(root)) {
        const launch = await this.research.call('specialist_launch_get', task.parent_session_id, { task_id: task.task_id })
        if (!launch || launch.state !== 'submitted' || !task.child_session_id || task.child_session_id !== launch.session_id) {
          await this.settle(task.parent_session_id, task.task_id, { state: 'unverified', result: {}, preserve_result: true,
            exit_verified: false, error: '重绑定时专家启动或退出状态未核实。' }, `${task.task_id}:warm-unverified`).catch(() => {})
          continue
        }
        this.tools.set(launch.session_id, launch.tools)
        if (launch.context_mode === 'blind') this.researchMode.delete(launch.session_id)
        else this.researchMode.add(launch.session_id)
        void this.watchRecovered(root, task, launch)
      }
    }
  }

  private async watchRecovered(root: string, task: any, launch: any): Promise<void> {
    const controller = new AbortController()
    let limit: LimitKind | null = null
    const disarm = this.armLimits(String(launch.session_id), launch.deadline_at, controller, (kind) => {
      if (controller.signal.aborted || limit) return
      limit = kind
    })
    try {
      const exited = await this.finished(launch.session_id, controller.signal)
      if (!this.current()) return
      const messages = await this.host.messages(launch.session_id)
      const start = messages.findIndex(item => item?.id === launch.message_id || item?.info?.id === launch.message_id)
      const subsequent = start >= 0 ? messages.slice(start + 1) : []
      const correlated = start >= 0 && !subsequent.some(item => item?.type === 'user' || item?.type === 'synthetic')
      const { outcome } = sessionResult(messages)
      if (!correlated || !outcome) {
        await this.settle(task.parent_session_id, task.task_id, { state: 'unverified', result: {}, preserve_result: true,
          exit_verified: false, error: '无法把专家终态与原启动消息对应；需人工核对。' }, `${task.task_id}:warm-unknown`)
        return
      }
      const reason = limit
      disarm()
      await this.finishChild(root, task.parent_session_id, task.task_id, launch.session_id, exited, reason,
        `${task.task_id}:warm-result`)
    } catch {
      if (this.current()) await this.settle(task.parent_session_id, task.task_id, { state: 'unverified', result: {}, preserve_result: true,
        exit_verified: false, error: '专家恢复核对失败；需人工核对。' }, `${task.task_id}:warm-error`).catch(() => {})
    } finally {
      disarm()
    }
  }

  /** A restarted host cannot prove that a previous specialist or its work exited. */
  async recover(): Promise<void> {
    await this.research.ready()
    for (const root of this.research.roots().filter((item) => this.research.inScope(item))) {
      const main = this.research.sessionsIn(root).find((sessionID) => this.research.roleOf(sessionID) === "main")
      if (!main) continue
      for (const task of await this.activeTasks(root)) {
        await this.settle(
          task.parent_session_id,
          task.task_id,
          {
            state: "unverified",
            result: {},
            preserve_result: true,
            exit_verified: false,
            error: "OpenCode 重启后专家退出状态未核实；请先核对，不能自动重新委派。",
          },
          `${task.task_id}:restart`,
        ).catch(() => {})
      }
    }
  }
}
