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
import { createHash } from "node:crypto"
import { join } from "node:path"
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
const STOP_GRACE_MS = 60_000

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

/** How a specialist session's turn ended and the last text it wrote. */
export function sessionResult(messages: any[]): { outcome: string | null; text: string } {
  const last = messages.at(-1)
  const outcome = last?.type === "idle" && last.outcome ? String(last.outcome) : null
  for (let index = messages.length - 1; index >= 0; index--) {
    const item = messages[index]
    if (item?.type !== "assistant") continue
    const text = (item.content ?? [])
      .filter((part: any) => part?.type === "text")
      .map((part: any) => String(part.text ?? ""))
      .join("\n")
      .trim()
    if (text) return { outcome, text }
  }
  return { outcome, text: "" }
}

function verdict(exited: boolean, outcome: string | null, text: string, timedOut: boolean) {
  if (!exited) return { state: "unverified", error: "专家会话被中断后没有按时停下，退出未核实" }
  if (outcome === "succeeded") {
    return text ? { state: "completed", error: null } : { state: "incomplete", error: "专家没有给出文字报告" }
  }
  if (outcome === "interrupted") {
    return timedOut
      ? { state: "incomplete", error: "达到专家时间上限，已收回部分报告" }
      : { state: "cancelled", error: "专家这一轮被中断" }
  }
  return { state: "incomplete", error: `专家会话以「${outcome ?? "未知"}」结束` }
}

export class Specialists {
  /** Tools of each specialist session this process started, for the per-request filter. */
  private tools: Map<string, readonly string[]>
  private researchMode: Set<string>

  constructor(
    private readonly research: Research,
    private readonly host: SessionHost,
    private readonly modelFor: (root: string) => ModelRef | undefined,
    private readonly fanout = 2,
    private readonly timeoutMs = 600_000,
    runtime?: { specialistTools: Map<string, readonly string[]>; specialistResearch: Set<string> },
    private readonly current: () => boolean = () => true,
  ) {
    this.tools = runtime?.specialistTools ?? new Map()
    this.researchMode = runtime?.specialistResearch ?? new Set()
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
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, Math.max(0, launch.deadline_at - Date.now()))
    const cancel = () => controller.abort()
    signal?.addEventListener("abort", cancel, { once: true })
    if (signal?.aborted) controller.abort()
    let child: string | undefined
    try {
      const parentState = await this.research.call("status", parent)
      const workspace: string = parentState.workflow?.session?.cwd ?? root
      const kind = reserved.context_mode === "blind" ? "盲评" : "专家"
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
      await this.host.synthetic(child, kickoff(reserved, workspace, tools), `专家任务 ${reserved.task_id}`, true,
        launch.message_id, undefined, undefined, launch.intent_id)
      await this.research.call('specialist_launch_phase', parent, { intent_id: launch.intent_id, phase: 'submitted' }, `${operationID}:submitted`)
      const exited = await this.finished(child, controller.signal)
      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified', error: '控制器已重绑定，等待新实例核对。' }
      const { outcome, text } = sessionResult(await this.host.messages(child).catch(() => []))
      const { state, error } = verdict(exited, outcome, text, timedOut)
      let archiveError: string | undefined
      const artifact = text
        ? await this.archive(root, parent, reserved.task_id, outcome, text, operationID).catch((failure) => {
            archiveError = message(failure)
            return null
          })
        : null
      return await this.settle(
        parent,
        reserved.task_id,
        {
          state,
          error,
          exit_verified: exited,
          result: {
            stop_reason: outcome,
            session_id: child,
            preview: text.slice(0, PREVIEW_CHARS),
            ...(artifact ? { artifact, report_path: join(root, artifact.path) } : {}),
            ...(archiveError ? { archive_error: archiveError } : {}),
          },
        },
        `${operationID}:finish`,
      )
    } catch (error) {
      if (!this.current()) return { task_id: reserved.task_id, state: 'unverified', error: '控制器已重绑定，等待新实例核对。' }
      const shouldStop = !!child && (timedOut || !!signal?.aborted)
      const exited = shouldStop ? await this.stop(child!).catch(() => false) : false
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
      clearTimeout(timer)
      signal?.removeEventListener("abort", cancel)
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
    await this.host.interrupt(child).catch(() => {})
    let timer: ReturnType<typeof setTimeout> | undefined
    const late = new Promise<boolean>((resolve) => (timer = setTimeout(() => resolve(false), STOP_GRACE_MS)))
    try {
      return await Promise.race([idle.then(() => true, () => false), late])
    } finally {
      clearTimeout(timer)
    }
  }

  /** Freezes the report into the ledger's objects, the same file format as the DSH plugin. */
  private async archive(
    root: string,
    parent: string,
    taskID: string,
    outcome: string | null,
    text: string,
    operationID: string,
  ): Promise<{ path: string; version: string; kind: string }> {
    const directory = join(root, ".research", "specialist-results")
    mkdirSync(directory, { recursive: true })
    const path = join(directory, `${taskID}.json`)
    writeFileSync(path, JSON.stringify({ stop_reason: outcome, output: [{ type: "text", text }] }), { mode: 0o600 })
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
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, Math.max(0, launch.deadline_at - Date.now()))
    try {
      const exited = await this.finished(launch.session_id, controller.signal)
      if (!this.current()) return
      const messages = await this.host.messages(launch.session_id)
      const start = messages.findIndex(item => item?.id === launch.message_id || item?.info?.id === launch.message_id)
      const subsequent = start >= 0 ? messages.slice(start + 1) : []
      const correlated = start >= 0 && !subsequent.some(item => item?.type === 'user' || item?.type === 'synthetic')
      const { outcome, text } = sessionResult(messages)
      if (!correlated || !outcome) {
        await this.settle(task.parent_session_id, task.task_id, { state: 'unverified', result: {}, preserve_result: true,
          exit_verified: false, error: '无法把专家终态与原启动消息对应；需人工核对。' }, `${task.task_id}:warm-unknown`)
        return
      }
      const { state, error } = verdict(exited, outcome, text, timedOut)
      const operationID = `${task.task_id}:warm-result`
      const artifact = text ? await this.archive(root, task.parent_session_id, task.task_id, outcome, text, operationID).catch(() => null) : null
      await this.settle(task.parent_session_id, task.task_id, { state, error, exit_verified: exited,
        result: { stop_reason: outcome, session_id: launch.session_id, preview: text.slice(0, PREVIEW_CHARS),
          ...(artifact ? { artifact, report_path: join(root, artifact.path) } : {}) } }, `${operationID}:finish`)
    } catch {
      if (this.current()) await this.settle(task.parent_session_id, task.task_id, { state: 'unverified', result: {}, preserve_result: true,
        exit_verified: false, error: '专家恢复核对失败；需人工核对。' }, `${task.task_id}:warm-error`).catch(() => {})
    } finally {
      clearTimeout(timer)
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
