/**
 * Server half of the research plugin, built around the autonomous loop:
 * execution events drive the engine, research tools write the ledger, and
 * each model request of a research session carries its role's research
 * context. Sessions outside a project see exactly what they would without
 * the plugin: the context hook only removes this plugin's tools.
 */
import { Plugin } from "@opencode/plugin"
import { Error as ToolError } from "@opencode/plugin/promise/tool"
import { existsSync } from "node:fs"
import { randomUUID } from "node:crypto"
import { Observations } from "./src/observations"
import { NativeObservations } from './src/native-observations'
import { ResearchRpc } from "./rpc"
import { sessionTitle } from "./src/session-title"
import { StorageClient } from "./src/storage"
import { Research, findProjectRoot, guidanceNote, type CommandResult } from "./src/research"
import { Engine, type ModelRef, type SessionHost, type ConclusionInput } from "./src/engine"
import { BLIND_SYSTEM, Specialists, SPECIALIST_STALL_MS, SPECIALIST_TIMEOUT_MS, STOP_GRACE_MS, SUMMARY_GRACE_MS, specialistGraceForTest, specialistProgressEvent, type SpecialistTask } from "./src/specialists"
import { TOOLS, keepsTool, type ToolDefinition } from "./src/tools"
import { COORDINATOR_NATIVE_READS, controlInstructions } from "./src/coordinator-boundary"
import { STALE_CONTEXT_MARKER, markHistoryCacheBoundary, researchStateMessage, splitResearchContext } from "./src/context-placement"
import { parameters, validateArgs } from "./src/schema"
import { USAGE, runtimeBudgetSummary } from "./src/format"
import { SOURCE_ALGORITHM, SOURCE_LIMITS, WorkspaceProbe, type SourceSnapshot } from './src/workspace-probe'
import { attachController } from './src/controller-lifecycle'
import { preflight } from './src/host-submission'
import { sameDirectory, locationError, isSessionEntryError, isSessionMove, toolEntryError, assertResearchTools } from './src/session-location'

const STARTUP_WAIT_MS = 1500
const RESEARCH_READS = new Set(['research_query', 'research_read_input'])

async function within<T>(promise: Promise<T>, ms: number): Promise<T | void> {
  let timer:ReturnType<typeof setTimeout> | undefined
  try{return await Promise.race([promise,new Promise<void>(resolve=>{timer=setTimeout(resolve,ms)})])}
  finally{clearTimeout(timer)}
}

/** RPC handlers report failures as values; thrown errors lose their text on the way to the TUI. */
async function reported<T>(work: () => Promise<T>): Promise<T | { error: string }> {
  try {
    return await work()
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) }
  }
}

export default Plugin.define({
  id: "auto-research",
  async setup(ctx) {
    const options = ctx.options as {
      python?: string
      registryPath?: string
      concurrency?: number
      specialistFanout?: number
      /** Absolute backstop for one specialist. Omitted means `SPECIALIST_TIMEOUT_MS`. */
      specialistTimeoutMs?: number
      /** Silence that stops one specialist. Omitted means `SPECIALIST_STALL_MS`. */
      specialistStallMs?: number
      contextPlacement?: string
    }
    // Omitted placement uses tail. Explicit system and unknown-value rollback
    // keep the full-system path; an unknown value warns once at setup.
    const contextPlacement = options.contextPlacement === undefined || options.contextPlacement === 'tail' ? 'tail' : 'system'
    if (options.contextPlacement !== undefined && options.contextPlacement !== 'system' && options.contextPlacement !== 'tail') {
      process.stderr.write(`Unknown contextPlacement ${JSON.stringify(options.contextPlacement)}; using "system".\n`)
    }
    const observations = new Observations()
    const workspaceProbe = new WorkspaceProbe()
    const storage = new StorageClient({ python: options.python, registryPath: options.registryPath,
      observeRequest: (operation_id,method,fields) => observations.transportObserver(operation_id,method,fields),
    })
    const lifecycle = attachController(ctx.location.directory, storage.registryPath, ctx.location.workspaceID)
    // Subscribe before asynchronous setup so relevant events are not dropped
    // while membership and controller ownership are being loaded.
    let listening = true
    let eventGap = false
    let gapVersion = 0
    let eventHandler: ((event: any) => Promise<void>) | undefined
    let telemetryHandler: ((event: any) => void) | undefined
    const earlyEvents: any[] = []
    const eventChains = new Map<string, Promise<void>>()
    const failedEvents = new Map<string, any[]>()
    const shellCalls = lifecycle.state.shellCalls ??= new Map()
    const shellSeen = lifecycle.state.shellSeen ??= new Set()
    const terminalCalls: Map<string, Set<{ execution?: string; step?: string }>> =
      lifecycle.state.terminalCalls ??= new Map()
    const queuedGuidance: Map<string, { id: string; text: string; execution?: string; delivered?: boolean }[]> =
      lifecycle.state.queuedGuidance ??= new Map()
    let queuedEvents = 0
    const relevantEvents = new Set(['session.execution.started', 'session.execution.succeeded', 'session.execution.failed',
      'session.inbox.enqueued', 'session.inbox.delivered', 'session.execution.interrupted', 'session.tool.failed'])
    let notifyEventState = () => {}
    const markGap = () => { eventGap = true; gapVersion++; notifyEventState() }
    const waitEvents = (sessionID: string) => eventChains.get(sessionID) ?? Promise.resolve()
    /** Drop a finishing marker only for its own execution (or for cleanup). */
    const clearTurnFinish = (sessionID: string, execution?: string) => {
      const markers = lifecycle.state.waitFinishes
      const marker = markers?.get(sessionID)
      if (marker && (execution === undefined || marker.execution === execution)) markers!.delete(sessionID)
    }
    const GUIDANCE_QUEUE_LIMIT = 16
    /**
     * The newest real user input that arrived in the native transcript after the
     * assistant step that generated the terminal call. The transcript is the
     * authoritative conversation order: `user` entries are real prompts and
     * `synthetic` entries are reports/wakes, so no ID whitelist or wall-clock
     * guess is involved. `null` means no new input; `undefined` means the
     * transcript cannot prove it either way.
     */
    const inputAfterStep = async (sessionID: string, step: string | undefined) => {
      if (!step) return undefined
      const transcript = await within(host.messages(sessionID).catch(() => [] as any[]), 2000) as any[] | void
      if (!transcript?.length) return undefined
      const at = transcript.findIndex((item: any) => item?.id === step)
      if (at < 0) return undefined
      for (let index = transcript.length - 1; index > at; index--) {
        const item: any = transcript[index]
        if (item?.type === 'user' && typeof item.id === 'string') return item.id
      }
      return null
    }
    /**
     * Register one terminal call before it awaits the service. The token keeps
     * the assistant step that generated the call, so the finishing step can ask
     * the transcript what arrived after that step, not what the session looked
     * like when the RPC returned.
     */
    const beginTerminalCall = (sessionID: string, execution: string | undefined, call?: any) => {
      const token: { execution?: string; step?: string } = {
        execution,
        step: typeof call?.messageID === 'string' ? call.messageID : undefined,
      }
      const calls = terminalCalls.get(sessionID) ?? new Set<{ execution?: string; step?: string }>()
      calls.add(token)
      terminalCalls.set(sessionID, calls)
      return token
    }
    const endTerminalCall = (sessionID: string, token: { execution?: string; step?: string }) => {
      const calls = terminalCalls.get(sessionID)
      if (!calls) return
      calls.delete(token)
      if (!calls.size) terminalCalls.delete(sessionID)
    }
    /** An in-flight terminal call that belongs to this execution. */
    const terminalCallHere = (sessionID: string, execution: string | undefined) =>
      [...(terminalCalls.get(sessionID) ?? [])].some(token => token.execution === execution)
    /**
     * A terminal action covers this execution only: an accepted marker in it, or
     * a call still awaiting the service that was registered for it. A late call
     * from an earlier execution keeps its own registration but no longer routes
     * input in a later one.
     */
    const terminalWindow = (sessionID: string) => {
      const execution = engine.executionToken(sessionID)
      const calls = terminalCalls.get(sessionID)
      if (calls) for (const token of calls) if (token.execution === execution) return true
      const marker = lifecycle.state.waitFinishes?.get(sessionID)
      return !!marker && execution === marker.execution
    }
    const rememberQueuedGuidance = (sessionID: string, id: string, text: string, execution: string | undefined) => {
      if (!id) return
      const records = queuedGuidance.get(sessionID) ?? []
      if (records.some(item => item.id === id)) return
      records.push({ id, text, execution })
      queuedGuidance.set(sessionID, records.slice(-GUIDANCE_QUEUE_LIMIT))
    }
    const markQueuedGuidanceDelivered = (sessionID: string, id: string) => {
      for (const item of queuedGuidance.get(sessionID) ?? []) if (item.id === id) item.delivered = true
    }
    /**
     * The host may still deliver a queued prompt as the next step of the same
     * execution. The note therefore waits for a different execution, so the old
     * turn's wire body never carries the pending instruction through memory.
     */
    const flushQueuedGuidance = async (sessionID: string) => {
      const records = queuedGuidance.get(sessionID)
      if (!records?.length) return
      const execution = engine.executionToken(sessionID)
      const ready = records.filter(item => item.delivered && item.execution !== execution)
      if (!ready.length) return
      queuedGuidance.set(sessionID, records.filter(item => !ready.includes(item)))
      for (const item of ready) {
        try { await research.call('note', sessionID, guidanceNote(item.text), `guidance:${sessionID}:${item.id}`) }
        catch { /* A finished work segment has no writable note target; the native message is still persisted. */ }
      }
    }
    let observeSpecialist: ((event: any) => void) | undefined
    const queueEvent = (event: any) => {
      observeSpecialist?.(event)
      if (event?.type === 'session.tool.failed' && !shellCalls.has(`${event.data?.sessionID}:${event.data?.id}`)) {
        telemetryHandler?.(event); return
      }
      if (event?.type === 'session.inbox.enqueued' && event.data?.item?.payload?.metadata?.source === 'shell' &&
          typeof event.data?.inboxID === 'string') {
        shellSeen.add(`${event.data.sessionID}:${event.data.inboxID}`)
        if (shellSeen.size > 1024) shellSeen.delete(shellSeen.values().next().value!)
      }
      if (event?.type === 'session.inbox.enqueued' && event.data?.item?.payload?.metadata?.source !== 'shell') {
        telemetryHandler?.(event); return
      }
      if (!relevantEvents.has(event?.type)) { telemetryHandler?.(event); return }
      const sessionID = event.data?.sessionID
      if (!sessionID) return
      if (!eventHandler) {
        if (earlyEvents.length >= 256) markGap()
        else earlyEvents.push(event)
        return
      }
      if (queuedEvents >= 256) { markGap(); return }
      queuedEvents++
      const chain = (eventChains.get(sessionID) ?? Promise.resolve()).then(async () => {
        if (failedEvents.has(sessionID)) {
          const pending = failedEvents.get(sessionID)!
          if (pending.length < 256) pending.push(event)
          else markGap()
          return
        }
        for (let attempt = 0; attempt < 3 && listening && lifecycle.current(); attempt++) {
          try { await eventHandler!(event); return }
          catch {
            if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 25 * (attempt + 1)))
            else {
              if (failedEvents.size < 128) failedEvents.set(sessionID, [event])
              else markGap()
              notifyEventState()
            }
          }
        }
      }).finally(() => {
        queuedEvents--
        if (eventChains.get(sessionID) === chain) eventChains.delete(sessionID)
      })
      eventChains.set(sessionID, chain)
    }
    let subscription: AbortController | undefined
    const subscribe = () => {
      subscription?.abort()
      const current = new AbortController()
      subscription = current
      void (async () => {
        try {
          for await (const event of ctx.event.subscribe({ signal: current.signal })) {
            if (!listening || current.signal.aborted) break
            queueEvent(event)
          }
          if (listening && !current.signal.aborted) markGap()
        } catch { if (listening && !current.signal.aborted) markGap() }
      })()
    }
    subscribe()
    const requireCurrent = () => {
      if (!lifecycle.current()) throw new Error("Research controller attachment has been replaced")
    }
    let emitChanged: (root: string) => void = () => {}
    const research = new Research(
      storage,
      ctx.location.directory,
      () => existsSync(storage.registryPath),
      (root) => emitChanged(root),
      observations.instanceID,
      session=>{
        nativeObservations.forget(session)
        lifecycle.state.waitFinishes?.delete(session)
        terminalCalls.delete(session)
        queuedGuidance.delete(session)
        for(const key of externalStarts.keys())if(key.startsWith(session+':'))externalStarts.delete(key)
      },
    )
    const eventHealth = (root?: string) => {
      if (!root || !research.inScope(root)) return undefined
      let held = 0, unresolved = 0
      for (const [sid, events] of failedEvents) {
        if (research.rootOf(sid) === root) held += events.length
        else if (!research.isAssociated(sid)) unresolved++
      }
      const state = eventGap ? 'gap' : held ? 'held' : unresolved ? 'unresolved' : 'ready'
      const message = state === 'ready' ? undefined : state === 'unresolved'
        ? `有 ${unresolved} 个事件会话归属待核对；已知会话继续按原授权控制。用 /research auto 重试核对。`
        : `研究事件需核对（${eventGap ? '事件流中断或溢出；' : ''}暂存 ${held} 条）；暂停和停止仍可用，用 /research auto 重新订阅并核对。`
      return { state, held, unresolved, ...(message ? {message} : {}) }
    }
    notifyEventState = () => { for (const root of research.roots()) if (research.inScope(root)) emitChanged(root) }
    research.setExecutionResolver(sessionID => lifecycle.state.turns.get(sessionID))
    let ensureController: (root: string, main: string) => Promise<void> = async () => { throw new Error('Controller not initialized') }
    let bootstrap: () => Promise<void> = async () => { throw new Error('Controller not initialized') }
    const verifyController = async (sessionID: string, reducingOrReading = false) => {
      requireCurrent()
      if (!reducingOrReading && (eventGap || failedEvents.has(sessionID))) throw Object.assign(new Error('研究事件需核对；停止和暂停仍可用，核对后可 /research auto'), { code: 'research_events_need_check' })
      const root = research.rootOf(sessionID) ?? research.scope
      const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
      if (!main || !research.inScope(root)) throw new Error('Research controller scope is unavailable')
      await ensureController(root, main)
      await research.call('controller_check', sessionID)
      requireCurrent()
    }
    const assertLocation = async (sessionID: string) => {
      const root = research.registeredRootOf(sessionID)
      if (!root) return
      let directory: string
      try { directory = await sessionDirectory(sessionID) }
      catch { throw locationError(root, 'unavailable (host session lookup failed)', research.scope) }
      if (!research.inScope(root) || !sameDirectory(root, directory)) throw locationError(root, directory, research.scope)
    }
    const assertReady = async (sessionID: string) => {
      await assertLocation(sessionID)
      const root = research.rootOf(sessionID)
      if (!root) return
      let tools
      try { tools = await ctx.tool.list() }
      catch { throw toolEntryError(root, ['host tool catalog unavailable']) }
      assertResearchTools(root, research.roleOf(sessionID), new Set(tools.map(tool => tool.name)))
    }
    const host: SessionHost = {
      assertReady,
      source: async sessionID => {
        requireCurrent()
        if (research.roleOf(sessionID)!=='node_core') return undefined
        try {
          const deadline=performance.now()+SOURCE_LIMITS.timeoutMs
          const owner=await within(research.call('runtime_source_owner',sessionID),SOURCE_LIMITS.timeoutMs)
          if (!owner?.cwd) return undefined
          const sample=await workspaceProbe.sample(owner.cwd,deadline-performance.now())
          if(sample.reason) observations.append(research.rootOf(sessionID)!,{
            phase:'coverage_gap',source:'workspace_probe',reason:sample.reason,
          })
          return { ...sample, algorithm:SOURCE_ALGORITHM, owner } satisfies SourceSnapshot
        } catch {
          observations.append(research.rootOf(sessionID)!,{phase:'coverage_gap',source:'workspace_probe',reason:'owner_unavailable'})
          return undefined
        }
      },
      create: async (title, directory, model, permissions, sessionID) => {
        await preflight('create', async () => {
          requireCurrent()
          const main = research.sessionsIn(directory).find(sid => research.roleOf(sid) === 'main')
          if (!main) throw new Error('Research controller owner is unavailable')
          await verifyController(main)
        })
        return (await ctx.session.create({
            title: sessionTitle(title),
            ...(sessionID ? { id: sessionID } : {}),
            location: { directory: ctx.location.directory },
            ...(model ? { model } : {}),
            ...(permissions ? { permissions } : {}),
          })).id
      },
      switchModel: async (sessionID, model) => {
        requireCurrent()
        await verifyController(sessionID)
        await ctx.session.switchModel({ sessionID, model })
      },
      synthetic: async (sessionID, text, label, resume, messageID, wakeIntentID, launchIntentID, specialistLaunchID) => {
        const outboundID = messageID ?? (resume ? `msg_research_${randomUUID().replaceAll('-', '')}` : undefined)
        let reservation:any
        await preflight('synthetic', async () => {
          requireCurrent()
          await verifyController(sessionID, !resume)
          if (resume) await assertReady(sessionID)
          if (resume && research.isAssociated(sessionID)) {
            const source_snapshot=await host.source?.(sessionID)
            reservation = await research.call('runtime_reserve', sessionID,{source_snapshot, message_id: outboundID, ...(wakeIntentID ? { wake_intent_id: wakeIntentID } : {}),
              ...(launchIntentID ? { launch_intent_id: launchIntentID } : {}),
              ...(specialistLaunchID ? { specialist_launch_id: specialistLaunchID } : {})})
            if (!reservation.allowed) {
              if (reservation.reason==='wall_clock') await enforceDeadline(sessionID)
              research.notifyChanged(research.rootOf(sessionID)!)
              throw Object.assign(new Error(`自主调度预算已到限：${reservation.reason}；先用 /research budget 显式调整额度，再恢复。`), { code: 'runtime_budget_exhausted' })
            }
          }
          requireCurrent()
          await verifyController(sessionID, !resume)
        }).catch(async error => {
          if (resume && outboundID && !wakeIntentID && !launchIntentID && !specialistLaunchID) {
            Object.assign(error, { unsent_message_id: outboundID })
            await research.call('runtime_turn_not_sent', sessionID, { message_id: outboundID }).catch(() => {})
          }
          throw error
        })
        const sent = await ctx.session.synthetic({ sessionID, text, description: label, resume, ...(outboundID ? { id: outboundID } : {}) })
        if (outboundID && sent.id !== outboundID) throw new Error("Host did not confirm the requested research message ID")
        requireCurrent()
        if (reservation?.allowed) await research.call('runtime_submitted',sessionID,
          {reservation_id:reservation.reservation_id},`submitted:${reservation.reservation_id}`).catch(error => {
            observations.append(research.rootOf(sessionID)!,{phase:'coverage_gap',source:'budget_submission',
              session_id:sessionID,error_code:error.code ?? 'storage_unavailable'})
          })
      },
      interrupt: async (sessionID) => {
        requireCurrent()
        await verifyController(sessionID, true)
        await ctx.session.interrupt({ sessionID })
      },
      wait: async (sessionID) => {
        requireCurrent()
        await verifyController(sessionID, true)
        await ctx.session.wait({ sessionID })
      },
      messages: async (sessionID) => {
        requireCurrent()
        await verifyController(sessionID, true)
        return (await ctx.session.context({ sessionID })) as any[]
      },
      busy: async (sessionID) => {
        const probe = (ctx.session as { busy?: (input: { sessionID: string }) => boolean | Promise<boolean> }).busy
        if (typeof probe !== "function") return specialists.appearsBusy(sessionID)
        try {
          const result = await within(Promise.resolve(probe({ sessionID })), 2_000)
          // A probe that never returns is not proof the session went idle.
          return result === undefined ? true : !!result
        } catch {
          return true
        }
      },
      eventsPending: (sessionID) => eventGap || eventChains.has(sessionID) || failedEvents.has(sessionID),
      recoveryMessages: async (sessionID) => {
        requireCurrent()
        const messages = await ctx.session.context({ sessionID })
        requireCurrent()
        return messages as any[]
      },
    }
    const nativeObservations = new NativeObservations(research,observations)
    await ctx.session.hook('model.request',request => { if (lifecycle.current()) nativeObservations.modelRequest(request) })
    await ctx.session.hook('retry',request => {
      const root = research.rootOf(request.sessionID)
      if (root && research.inScope(root)) observations.append(root,{phase:'provider_retry',session_id:request.sessionID,
        attempt:request.attempt,model:request.model,will_retry:request.decision.retry})
    })
    const externalStarts = new Map<string,number>()
    // The host may enqueue a very fast shell's terminal notice while the
    // execute.after hook is still persisting its shell ID.
    let specialists!: Specialists
    const beforeTool = async (request: any) => {
      if (!lifecycle.current() && !RESEARCH_READS.has(request.tool)) {
        if (lifecycle.successorReady()) return lifecycle.forwardBefore(request)
        if (findProjectRoot(research.scope) === research.scope && !COORDINATOR_NATIVE_READS.has(request.tool) && !RESEARCH_READS.has(request.tool)) {
          throw new ToolError({ message: '研究控制器正在交接；请先等待身份核对。',
            metadata: { code: 'research_control_recovery_pending', retryable: false, allow_fallback: false } })
        }
        return
      }
      try {
        const ready = await within(research.confirmMembership(request.sessionID).then(() => true), STARTUP_WAIT_MS)
        if (!ready) throw new Error("Research membership is not ready")
      } catch {
        const registered = research.registeredRootOf(request.sessionID)
        if (registered && !research.inScope(registered)) throw locationError(registered, 'unavailable (membership lookup failed)', research.scope)
        if (!lifecycle.current() && lifecycle.successorReady()) return lifecycle.forwardBefore(request)
        if (findProjectRoot(research.scope) === research.scope && !COORDINATOR_NATIVE_READS.has(request.tool) && !RESEARCH_READS.has(request.tool)) {
          throw new ToolError({ message: "研究会话身份尚未核实；请先核对状态，不能执行此工具。",
            metadata: { code: "research_control_unavailable", retryable: false, allow_fallback: false } })
        }
        return
      }
      if (!lifecycle.current() && !RESEARCH_READS.has(request.tool)) {
        if (lifecycle.successorReady()) return lifecycle.forwardBefore(request)
        throw new ToolError({ message: '研究控制器正在交接；请先等待身份核对。',
          metadata: { code: 'research_control_recovery_pending', retryable: false, allow_fallback: false } })
      }
      await assertLocation(request.sessionID)
      const root = research.rootOf(request.sessionID)
      if (!root) return
      if (isSessionMove(request.tool)) throw new ToolError({
        message: `research_session_move_unsupported: 研究会话固定在研究根目录 ${root}；文件操作请使用节点 workspace 的明确路径。`,
        metadata: {code: 'research_session_move_unsupported', retryable: false, allow_fallback: false},
      })
      try {
        await bootstrap()
        const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
        if (!main) throw new Error('Research controller owner is unavailable')
        await ensureController(root, main)
      } catch {
        if (!COORDINATOR_NATIVE_READS.has(request.tool) && !RESEARCH_READS.has(request.tool)) {
          throw new ToolError({ message: '研究控制器尚未完成核对；只能读取现有材料。',
            metadata: { code: 'research_control_recovery_pending', retryable: false, allow_fallback: false } })
        }
      }
      const role = research.roleOf(request.sessionID)
      if (!keepsTool(request.tool, true, role, specialists?.toolsOf(request.sessionID) ?? [])) {
        const message = role === 'main' ? '主协调不能执行此工具；节点工作必须交给节点执行会话。'
          : role === 'specialist' ? '专家只能使用本任务明确授予的只读工具。'
          : role === 'node_core' ? '节点执行会话不能使用此工具。'
          : '研究角色尚未登记或无法识别；只能读取已有材料，请先核对会话身份。'
        throw new ToolError({ message,
          metadata: { code: 'research_role_violation', role: role ?? 'unknown', retryable: false, allow_fallback: false } })
      }
      if (role === 'node_core') await waitEvents(request.sessionID)
      if (role === 'node_core' && !COORDINATOR_NATIVE_READS.has(request.tool) && request.tool !== 'research_query' &&
          (eventGap || failedEvents.has(request.sessionID) || !await engine.nodeTurnAllowed(request.sessionID))) {
        throw new ToolError({ message: '当前节点执行轮没有暂停期间的收尾授权；请先核对状态。',
          metadata: { code: 'research_control_paused', retryable: false, allow_fallback: false } })
      }
      if (role === 'node_core' && request.tool === 'shell') {
        const key = `${request.sessionID}:${request.id}`
        let resolve!: () => void
        const settled = new Promise<void>(done => { resolve = done })
        shellCalls.set(key, {sessionID:request.sessionID, background:request.input?.background === true, settled, resolve})
        try {
          await research.call('runtime_shell_begin', request.sessionID,
            { call_id: request.id, execution_token: engine.executionToken(request.sessionID) },
            `shell-begin:${request.sessionID}:${request.id}`)
        } catch (error) {
          resolve(); shellCalls.delete(key)
          throw new ToolError({message:error instanceof Error ? error.message : String(error),
            metadata:{code:(error as any)?.code ?? 'research_storage_unavailable',retryable:false,allow_fallback:false}})
        }
      }
      if (TOOLS.some(tool => tool.name === request.tool)) return
      externalStarts.set(`${request.sessionID}:${request.id}`,performance.now())
      observations.append(root,{phase:'external_execution',session_id:request.sessionID,id:request.id,tool:request.tool,
        status:'started',execution_ref:`session:${request.sessionID}`,resource_usage:'unknown'})
    }
    await ctx.tool.hook('execute.before',beforeTool)
    await ctx.tool.hook('execute.after',async request => {
      const key = `${request.sessionID}:${request.id}`
      const started = externalStarts.get(key)
      if (started===undefined) return
      externalStarts.delete(key)
      const root = research.rootOf(request.sessionID)!
      if (request.tool === 'shell' && research.roleOf(request.sessionID) === 'node_core') {
        const metadata = request.status === 'completed' ? request.result?.metadata : undefined
        const running = metadata?.status === 'running'
        const shellID = typeof metadata?.shellID === 'string' ? metadata.shellID : null
        const state = running && !shellID || request.status !== 'completed' && shellCalls.get(key)?.background
          ? 'unverified' : running ? 'running' : 'finished'
        const pending = research.call('runtime_shell_result', request.sessionID,
          { call_id: request.id, state, shell_id: state === 'running' ? shellID : null },
          `shell-result:${request.sessionID}:${request.id}`).then(() => {}).catch(error => {
          observations.append(root,{phase:'coverage_gap',source:'shell_result',session_id:request.sessionID,
            error_code:error.code ?? 'storage_unavailable'})
        })
        try { await pending }
        finally { shellCalls.get(key)?.resolve(); shellCalls.delete(key) }
      }
      observations.append(root,{phase:'external_execution',session_id:request.sessionID,id:request.id,tool:request.tool,
        status:request.status,background:request.status === 'completed' && request.result?.metadata?.status === 'running',duration_ms:performance.now()-started,resource_usage:'unknown'})
      if (request.status === 'completed' && request.result?.metadata?.status !== 'running' && research.roleOf(request.sessionID) === 'node_core' &&
          ['bash','shell','python','terminal'].includes(request.tool) && research.conclusionContractOf(request.sessionID)==='evidence-v1') {
        const observed = await within(research.call('runtime_tool_observed', request.sessionID,
          {tool:request.tool,call_id:request.id}, `tool-observed:${request.sessionID}:${request.id}`).catch(error => {
            observations.append(root,{phase:'coverage_gap',source:'execution_observation',session_id:request.sessionID,
              error_code:error.code ?? 'storage_unavailable'})
          }), 2000)
        if (!observed) observations.append(root,{phase:'coverage_gap',source:'execution_observation',session_id:request.sessionID,
          error_code:'not_confirmed'})
      }
    })
    const count = (value: unknown, fallback: number) => Math.max(1, Math.floor(Number(value ?? fallback)) || fallback)
    const engine = new Engine(research, host, count(options.concurrency, 1), lifecycle.state, lifecycle.current,
      (root, main) => ensureController(root, main), (root) => specialists.interruptUnverified(root))
    specialists = new Specialists(
      research,
      host,
      (root) => engine.modelFor(root),
      count(options.specialistFanout, 2),
      count(options.specialistTimeoutMs, SPECIALIST_TIMEOUT_MS),
      count(options.specialistStallMs, SPECIALIST_STALL_MS),
      lifecycle.state,
      lifecycle.current,
      specialistGraceForTest.stopMs ?? STOP_GRACE_MS,
      specialistGraceForTest.summaryMs ?? SUMMARY_GRACE_MS,
    )
    observeSpecialist = (event) => {
      const sessionID = event?.data?.sessionID
      if (typeof sessionID !== "string" || !sessionID) return
      if (specialistProgressEvent(event.type)) specialists.noteProgress(sessionID)
      const type = event.type
      if (type === "session.deleted") specialists.noteBusy(sessionID, "deleted")
      else if (type === "session.status") specialists.noteBusy(sessionID, event.data?.status?.type)
      else if (type === "session.idle" || type === "session.execution.succeeded" || type === "session.execution.failed" || type === "session.execution.interrupted") specialists.noteBusy(sessionID, "idle")
      else if (type === "session.execution.started") specialists.noteBusy(sessionID, "busy")
    }
    const controllers = new Map<string, Promise<void>>()
    ensureController = (root, main) => {
      let existing = controllers.get(root)
      if (!existing) {
        existing = (async () => {
          requireCurrent()
          const owner = await research.call<any>('controller_attach', main,
            { epoch: lifecycle.epoch, binding: lifecycle.binding, pid: lifecycle.pid })
          requireCurrent()
          research.setController(root, { epoch: owner.epoch, binding: owner.binding, fence: owner.fence })
          if (owner.phase === 'recovery_required') throw new Error('研究控制器身份不明，需先核对原有执行；不能自动恢复。')
          if (owner.phase === 'reconciling') {
            if (owner.mode === 'cold') {
              await engine.coldRecover()
              await specialists.recover()
            } else {
              await engine.recoverWarm()
              await specialists.recoverWarm()
            }
            await research.call('controller_ready', main)
          }
        })()
        controllers.set(root, existing)
        void existing.catch(() => { if (controllers.get(root) === existing) controllers.delete(root) })
      }
      return existing
    }
    let boot: Promise<void> | null = null
    bootstrap = () => {
      boot ??= (async () => {
        await research.ready()
        requireCurrent()
        for (const root of research.roots().filter(item => research.inScope(item))) {
          const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
          if (main) await ensureController(root, main)
        }
      })().catch(error => { boot = null; throw error })
      return boot
    }
    let repairing: Promise<void> | null = null
    const repairEvents = () => {
      repairing ??= (async () => {
        await bootstrap()
        await Promise.all([...eventChains.values()])
        const version = gapVersion
        // Replay terminal facts while scheduling is paused. Only the explicit
        // resume below may start new work after the event gap is reconciled.
        for (const root of research.roots().filter(root => research.inScope(root))) {
          const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
          if (main && (await research.call('status', main)).workflow?.run?.state === 'running') {
            await research.call('workflow', main, { action: 'run', fields: { state: 'paused' } })
          }
        }
        if (eventGap) subscribe()
        let unknownChecks = 0
        for (const [session, events] of failedEvents) {
          if (!research.isAssociated(session)) {
            if (unknownChecks++ >= 8) continue
            try {
              const ready = await within(research.confirmMembership(session).then(() => true), 200)
              if (!ready) throw new Error('Membership is still pending')
            } catch {
              failedEvents.delete(session); failedEvents.set(session, events)
              continue
            }
            if (!research.isAssociated(session)) { failedEvents.delete(session); continue }
          }
          for (const event of events) await eventHandler!(event)
          failedEvents.delete(session)
        }
        if (eventGap) {
          await engine.recoverWarm()
          await specialists.recoverWarm()
        }
        if (gapVersion !== version) throw new Error('事件流仍不可用；暂停或停止后检查宿主连接。')
        eventGap = false
        notifyEventState()
      })().finally(() => { repairing = null })
      return repairing
    }
    void bootstrap().catch(error => {
      if (listening && lifecycle.current()) process.stderr.write(`Research controller initialization: ${error instanceof Error ? error.message : String(error)}\n`)
    })

    async function runTool(tool: ToolDefinition, call: any, args: Record<string, any>): Promise<unknown> {
      if (!lifecycle.current() && !RESEARCH_READS.has(tool.name)) {
        if (lifecycle.successorReady()) return lifecycle.forwardTool(tool, call, args)
        throw new Error('研究控制器正在交接；请先等待身份核对。')
      }
      const currentTool = TOOLS.find(candidate => candidate.name === tool.name)
      if (!currentTool) throw new Error('当前控制器不认识这个研究工具。')
      tool = currentTool
      args = validateArgs(args, currentTool.parameters)
      if (tool.writes) await bootstrap()
      // A started event may already be buffered while setup is completing.
      // Consume it before a node asks to finish that execution.
      await waitEvents(call.sessionID)
      if (tool.writes && (eventGap || failedEvents.has(call.sessionID))) throw new Error('研究事件需核对；停止和暂停仍可用，核对后可 /research auto')
      if (tool.writes) requireCurrent()
      const sessionID = call.sessionID as string
      await research.confirmMembership(sessionID)
      await assertLocation(sessionID)
      if (!research.isAssociated(sessionID)) {
        throw new Error("本会话没有关联研究项目。用 /research auto <目标> 开始，或 /research recover takeover 接管。")
      }
      const root = research.rootOf(sessionID)!
      const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
      if (!main) throw new Error('Research controller owner is unavailable')
      if (tool.writes) await ensureController(root, main)
      const role = research.roleOf(sessionID)
      if (!tool.roles.includes(role as any)) throw new Error(`${tool.name} 不属于本会话的角色（${role ?? "未登记"}）`)
      if (role === 'main') {
        const confirmation=research.confirmMaterialDelivery(sessionID,call.messageID).catch(() => {})
        const needsCurrentEvidence = tool.name === 'research_prepare_conclusion' || tool.name === 'research_conclude' ||
          (tool.name === 'research_query' && args.collection === 'evidence')
        if (needsCurrentEvidence) {
          let timer:ReturnType<typeof setTimeout> | undefined
          try {
            const outcome=await Promise.race([confirmation.then(()=>'confirmed'),
              new Promise<'timeout'>(resolve=>{timer=setTimeout(()=>resolve('timeout'),2000)})])
            if (outcome==='timeout') research.evidenceDelivery.materialTimedOut(sessionID,call.messageID)
          } finally { clearTimeout(timer) }
        }
      }
      if (role === "main" && (tool.name === "research_propose" || tool.name === "research_conclude")) {
        const current = await research.call<any>("status", sessionID)
        const run = current.workflow?.run?.state
        const pause = current.workflow?.session?.pause_reason
        if ((run !== "running" && run !== "manual") || (pause && pause !== "wait")) {
          throw Object.assign(new Error("研究已暂停；请由用户显式恢复后再开始新工作，不能自行代替节点执行。"),
            { code: "research_control_paused", details: { run_state: run, pause_reason: pause, retryable: false, recovery: "explicit_resume", allow_fallback: false } })
        }
      }
      const operationID = `${sessionID}:${call.messageID}:${call.id}:${tool.name}`
      switch (tool.engine) {
        case "dispatch":
          return engine.dispatch(sessionID, String(args.node_id), operationID)
        case "wait": {
          const taskIDs = (args.task_ids ?? []).map(String)
          const execution = engine.executionToken(sessionID)
          // Register before awaiting the service so user input that arrives in
          // the acceptance window is queued instead of steering this call.
          const terminal = beginTerminalCall(sessionID, execution, call)
          try {
            const result: any = await engine.wait(sessionID, taskIDs)
            // Tail mode finishes the accepted wait on the next primary request.
            // A failed wait throws above and a newer execution never reuses the
            // late result of an older one.
            if (contextPlacement === 'tail' && execution !== undefined
                && engine.executionToken(sessionID) === execution) {
              const waiting = Array.isArray(result?.waiting) ? result.waiting.map(String) : []
              if (waiting.length) {
                const markers = lifecycle.state.waitFinishes ??= new Map()
                markers.set(sessionID, { execution, waiting, step: terminal.step })
              }
            }
            return result
          } finally { endTerminalCall(sessionID, terminal) }
        }
        case "finish": {
          const execution = engine.executionToken(sessionID)
          const terminal = beginTerminalCall(sessionID, execution, call)
          try {
            const result = await engine.finish(sessionID, { state: String(args.state), summary: String(args.summary) }, operationID)
            if (contextPlacement === 'tail' && execution !== undefined
                && engine.executionToken(sessionID) === execution) {
              const markers = lifecycle.state.waitFinishes ??= new Map()
              markers.set(sessionID, { execution, waiting: [], kind: 'finish', step: terminal.step })
            }
            return result
          } finally { endTerminalCall(sessionID, terminal) }
        }
        case "conclude":
          return engine.conclude(sessionID, args as unknown as ConclusionInput, operationID,
            research.evidenceDelivery.forCall(sessionID,call.messageID,call.id))
        case "exploration_decide":
          return engine.explorationDecide(sessionID, args, operationID)
        case "delegate":
          return specialists.delegate(sessionID, args as unknown as SpecialistTask, call.signal, operationID)
        case "delegate_batch":
          return specialists.delegateBatch(sessionID, args.tasks as SpecialistTask[], call.signal, operationID)
        default:
          return research.tool(
            { sessionID, messageID: call.messageID, callID: call.id, name: tool.name },
            tool.method!,
            tool.fields(args),
            tool.writes,
          )
      }
    }

    await ctx.tool.transform((editor) => {
      for (const tool of TOOLS) {
        editor.add({
          name: tool.name,
          description: tool.description,
          input: parameters(tool.parameters),
          options: { codemode: false },
          async execute(input, call) {
            if (tool.name==='research_checkpoint' || tool.name==='research_memory' && (input as any)?.action==='checkpoint') {
              research.checkpoints.forCall(call.sessionID,call.messageID,call.id)
            }
            const invocation_id = randomUUID()
            const root = research.rootOf(call.sessionID)
            const execute = async () => {
              const args = validateArgs(input, tool.parameters)
              await research.ensureContract()
              const value = await runTool(tool, call, args)
              const applied = typeof value==='object' && value!==null && 'applied' in value ? value.applied as Record<string,any> : undefined
              if (root && research.inScope(root)) observations.append(root,{phase:'expansion',invocation_id,
                applied_fields:applied && ['editorial','correct_previous'].includes(applied.preset)
                  ? ['change_kind','affected_scope_mode','affected_scope'].filter(key=>key in applied).length : 0})
              if (tool.writes) engine.wroteLedger(call.sessionID)
              return { content: JSON.stringify(value, null, 2) }
            }
            if (!root || !research.inScope(root)) return execute()
            const operation_id = `${call.sessionID}:${call.messageID}:${call.id}:${tool.name}`
              return await observations.invoke(root, { invocation_id, operation_id, session_id: call.sessionID,
                tool: tool.name, contract: "r2-v1", input_fields: Object.keys(input ?? {}).length,
                input_chars: Array.from(JSON.stringify(input) ?? "").length }, execute)
          },
        })
      }
    })

    const injectContext = async (request: any) => {
      const protectBlind = () => {
        const granted = specialists.toolsOf(request.sessionID)
        if ((research.roleOf(request.sessionID) === 'specialist' || granted.length > 0) && specialists.isBlind(request.sessionID)) {
          request.system.splice(0, request.system.length, { type: 'text', text: BLIND_SYSTEM })
          for (const name of Object.keys(request.tools)) if (!granted.includes(name)) delete request.tools[name]
          return true
        }
        return false
      }
      const protectUnknown = () => {
        if (!protectBlind() && !research.roleOf(request.sessionID)) {
          request.system.splice(0, request.system.length, { type: 'text', text: 'Research identity is unavailable. Read existing information only until membership is verified.' })
        }
      }
      protectBlind()
      if (!lifecycle.current()) {
        if (lifecycle.successorReady()) return lifecycle.forwardContext(request)
        if (findProjectRoot(research.scope) === research.scope) {
          protectUnknown()
          for (const name of Object.keys(request.tools)) if (!COORDINATOR_NATIVE_READS.has(name) && !RESEARCH_READS.has(name)) delete request.tools[name]
          request.system.push({ type: 'text', text: 'Research controller handoff is not ready; read existing information only.' })
        }
        return
      }
      try {
        const ready = await within(research.confirmMembership(request.sessionID).then(() => true), STARTUP_WAIT_MS)
        if (!ready) throw new Error("Research membership is not ready")
        await assertLocation(request.sessionID)
        const associated = research.isAssociated(request.sessionID)
        const role = research.roleOf(request.sessionID)
        if (!lifecycle.current()) {
          if (lifecycle.successorReady()) return lifecycle.forwardContext(request)
          throw new Error('Research controller handoff is not ready')
        }
        const granted = specialists.toolsOf(request.sessionID)
        for (const name of Object.keys(request.tools)) {
          if (!keepsTool(name, associated, role, granted)) delete request.tools[name]
        }
        if (!associated) return
        protectBlind()
        await within(bootstrap().then(() => true), STARTUP_WAIT_MS).then(ready => {
          if (!ready) throw new Error('Research controller is not ready')
        })
        const root = research.rootOf(request.sessionID)!
        const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
        if (!main) throw new Error('Research controller owner is unavailable')
        await ensureController(root, main)
        let finishing: 'wait' | 'finish' | undefined
        let readOnly = false
        let fullSystem = false
        // Both terminal actions share the existing execution-scoped lifetime.
        // Switching to system mode discards either kind across a warm rebind.
        const markers = lifecycle.state.waitFinishes
        if (contextPlacement !== 'tail') markers?.delete(request.sessionID)
        // Consume events that already arrived before recording the boundary this
        // preparation belongs to. Anything appended later invalidates it.
        await waitEvents(request.sessionID)
        // A queued guidance note is filed only for the execution after the one
        // it was queued behind, so the old turn never sees it through memory.
        await flushQueuedGuidance(request.sessionID)
        const gapAtStart = gapVersion
        const executionAtStart = engine.executionToken(request.sessionID)
        // The marker is compared before and after preparation in every case,
        // including "none at start, installed during an await".
        const markerAtStart = contextPlacement === 'tail' ? lifecycle.state.waitFinishes?.get(request.sessionID) : undefined
        // The new read-only fallback belongs to tail terminal preparation only:
        // system mode and markerless tail requests keep their baseline tools.
        const terminalAtStart = contextPlacement === 'tail'
          && (markerAtStart !== undefined || terminalCallHere(request.sessionID, executionAtStart))
        const candidate = markerAtStart
        let markerUsed: typeof candidate
        let markerCleared = false
        let needsResearchTools = role === 'node_core'
        if (role === "main") {
          const state = await research.call<any>("status", request.sessionID)
          const run = state.workflow?.run?.state
          const pause = state.workflow?.session?.pause_reason
          needsResearchTools = ['running', 'manual'].includes(run) && !pause
          if (state.project?.config?.exploration_policy !== 'exploration-v1') {
            for (const name of ['research_scope', 'research_route']) delete request.tools[name]
            if (!state.exploration_closing_allowed) delete request.tools.research_decide
            else if (request.tools.research_decide) {
              const spec=TOOLS.find(tool=>tool.name==='research_decide')!.parameters
              const closing=Object.fromEntries(Object.entries(spec).filter(([key])=>key!=='next_work'&&key!=='work_batch')
                .map(([key,value])=>[key,key==='action' ? {...value,enum:['defer','propose_stop']} : value]))
              request.tools.research_decide.input=parameters(closing) as typeof request.tools.research_decide.input
              request.tools.research_decide.description='Exploration is off. Consider existing results and close or defer routes; do not create new work.'
            }
          }
          const evidenceContract = state.project?.config?.conclusion_contract === 'evidence-v1'
          if (!evidenceContract) delete request.tools.research_prepare_conclusion
          const conclude = request.tools.research_conclude
          if (conclude) {
            const spec = TOOLS.find(tool => tool.name === 'research_conclude')!.parameters
            const keys = evidenceContract ? ['summary', 'final_ref', 'draft_id'] : ['summary', 'final_ref', 'outcome', 'gaps', 'review']
            conclude.input = parameters(Object.fromEntries(keys.map(key => [key, {...spec[key], required:true}]))) as typeof conclude.input
            conclude.description = evidenceContract
              ? 'Conclude using the prepared draft and its published final report after the complete draft was delivered in this model request.'
              : 'Conclude with a complete final report, the outcome, remaining gaps and an explicitly declared review.'
          }
          if (controlInstructions(run, pause)) {
            for (const name of ["research_propose", "research_dispatch", "research_conclude", 'research_scope', 'research_route', 'research_decide', 'research_prepare_conclusion']) delete request.tools[name]
          }
          // An accepted wait finishes on the next primary request of the same
          // execution. This marker is process-local and never reconstructs
          // control from old tool results. A warm rebind to system mode must
          // discard a marker left by a tail attachment.
          if (candidate) {
            const execution = executionAtStart
            const waiting = Array.isArray(state.workflow?.session?.waiting)
              ? state.workflow.session.waiting.map(String) : []
            const matches = candidate.kind !== 'finish' && execution !== undefined && execution === candidate.execution
              && run === 'running' && pause === 'wait'
              && waiting.length === candidate.waiting.length
              && waiting.every((id: string) => candidate.waiting.includes(id))
            if (matches) {
              finishing = 'wait'
              markerUsed = candidate
            }
            // Only the marker this request inspected may be dropped.
            else if (markers?.get(request.sessionID) === candidate) {
              markers.delete(request.sessionID)
              markerCleared = true
            }
          }
        }
        if (role === 'node_core') {
          if (!await engine.nodeTurnAllowed(request.sessionID)) {
            needsResearchTools = false
            let shellWake = engine.isShellWake(request.sessionID)
            if (!shellWake) {
              const transcript = await within(host.recoveryMessages!(request.sessionID).catch(() => undefined), 1000)
              const lastInput = transcript?.slice(-64).findLast((item: any) => item?.type === 'user' || item?.type === 'synthetic')
              shellWake = lastInput?.type === 'synthetic' && lastInput.metadata?.source === 'shell'
            }
            // A transient authorization failure is not proof that an accepted
            // finish expired. Keep the marker for a stable retry, stay read-only
            // and never fall back to a working tail request.
            readOnly = true
            fullSystem = true
            if (shellWake) for (const name of Object.keys(request.tools)) delete request.tools[name]
            else for (const name of Object.keys(request.tools)) {
              if (!COORDINATOR_NATIVE_READS.has(name) && name !== 'research_query') delete request.tools[name]
            }
            request.system.push({ type: 'text', text: shellWake
              ? 'A background completion arrived while execution is paused or unverified. Stop this response and report that explicit recovery is required.'
              : 'This node turn has no active drain authorization. Read existing material only until the user resumes or execution is reconciled.' })
          } else if (candidate) {
            const execution = engine.executionToken(request.sessionID)
            // The service remains authoritative: never infer a finish from
            // transcript text or commit it before the real execution ends.
            const pending = candidate.kind === 'finish' && execution === candidate.execution
              ? await research.call<any>('node_finish_pending', request.sessionID) : undefined
            if (execution !== undefined && pending?.turn_token === execution
                && engine.executionToken(request.sessionID) === execution
                && markers?.get(request.sessionID) === candidate) {
              finishing = 'finish'
              markerUsed = candidate
            } else if (markers?.get(request.sessionID) === candidate) {
              markers.delete(request.sessionID)
              markerCleared = true
            }
          }
        }
        // Keep every sibling result. Provider acceptance of this tool-free
        // request with tool history is a required, route-specific native gate.
        const deadline=performance.now()+2000
        // Delivery checks read the persistent history and the original text;
        // the temporary tail is not part of either.
        const history = request.messages
        const evidence = role==='main' || role==='node_core'
          ? research.observeEvidence(request.sessionID,history,Math.max(0,deadline-performance.now()))
          : Promise.resolve([])
        const text = await research.memory(request.sessionID)
        if (text && (role==='main' || role==='node_core')) {
          await research.delivered(request.sessionID,text,history,deadline-performance.now())
        }
        const issues = await evidence
        // Ask the native transcript what arrived after the assistant step that
        // generated the terminal call. This is a generation-step association, not
        // a session-level snapshot: a newer preparation cannot change it.
        const stepInput = finishing ? await inputAfterStep(request.sessionID, markerUsed?.step) : null
        // Wait for events that were already queued (including this message's own
        // delivery). The wait only covers the chain that existed at call time, so
        // the synchronous check below still tests whether another one was appended.
        await within(waitEvents(request.sessionID).then(() => true), 1000)
        // Nothing below awaits. A finishing decision, a tool set or an old
        // authorization only applies to the boundary it was prepared from:
        // attachment, execution, marker identity and the session event chain.
        // The marker is read again from the shared map, so a terminal action
        // that installed the very first marker during an await (including
        // "none at start") also invalidates this preparation.
        const markerNow = contextPlacement === 'tail' ? lifecycle.state.waitFinishes?.get(request.sessionID) : undefined
        const markerStable = markerCleared ? markerNow === undefined : markerNow === markerAtStart
        const boundaryStable = lifecycle.current() && !eventGap && !failedEvents.has(request.sessionID)
          && gapVersion === gapAtStart && !eventChains.has(request.sessionID)
          && engine.executionToken(request.sessionID) === executionAtStart
          && markerStable
          && (finishing === undefined || markerNow === markerUsed)
        const terminalContext = contextPlacement === 'tail'
          && (terminalAtStart || markerNow !== undefined || terminalCallHere(request.sessionID, executionAtStart))
        let degraded = false
        if (!boundaryStable && terminalContext && (role === 'main' || role === 'node_core')) {
          // Only a tail terminal preparation falls back. An ordinary request
          // keeps its tools for this unrelated event boundary change.
          finishing = undefined
          readOnly = true
          fullSystem = true
          degraded = true
        }
        if (finishing) for (const name of Object.keys(request.tools)) delete request.tools[name]
        else if (readOnly) for (const name of Object.keys(request.tools)) {
          if (!COORDINATOR_NATIVE_READS.has(name) && !RESEARCH_READS.has(name)) delete request.tools[name]
        }
        if (needsResearchTools && !finishing && !readOnly && boundaryStable) assertResearchTools(root, role, new Set(Object.keys(request.tools)))
        // In tail mode the stable blocks stay in the system prompt and the
        // volatile ones travel in one temporary message after the history.
        // The stale notice, the blockless control fallbacks and an unstable
        // boundary keep the old system placement: they change the safety text.
        let tailMode = !finishing && !fullSystem && contextPlacement === 'tail' && !!text
          && text.startsWith('Research context:\n') && !text.includes(STALE_CONTEXT_MARKER)
        let stable = text ?? ''
        const tail: string[] = []
        if (text && tailMode) {
          const split = splitResearchContext(text)
          const messages = markHistoryCacheBoundary(request)
          if (messages) {
            request.messages = messages
            stable = split.stable
            if (split.volatile) tail.push(split.volatile)
          } else {
            // No legal breakpoint before the tail: keep today's placement for
            // this request instead of caching a message that disappears.
            tailMode = false
          }
        }
        if (text && stable) request.system.push({ type: "text", text: stable })
        if (issues.length) {
          const warning = `Evidence delivery was not fully confirmed (${issues.join(', ')}). Re-read the affected pages and retry.`
          if (tailMode) tail.push(warning)
          else request.system.push({type:'text',text:warning})
        }
        if (degraded) {
          request.system.push({ type: 'text', text:
            'Research control or the accepted terminal action changed while this request was being prepared. ' +
            'This response may read existing material only; do not claim the terminal action succeeded or start new work. ' +
            'The next stable request re-checks the authoritative state.' })
        }
        if (finishing) {
          // `stepInput` is the newest real user input after the generating step.
          // An uninterpretable boundary (pre-9424fb0 warm marker, missing
          // transcript, or an evicted step) must not assert "no new input": keep
          // the tool-free barrier and answer in text when a user message exists.
          const newInput = stepInput === undefined
            ? request.messages.some((message: any) => message?.role === 'user')
            : stepInput !== null
          request.system.push({ type: 'text', text: newInput
            ? 'A user message arrived while this terminal action was still pending. This execution is finishing and no tools are available: answer that message in text without starting new work. The engine commits the pending result after this response ends.'
            : finishing === 'finish'
            ? 'A research finish was accepted in this execution. End this response without further work; ' +
              'the engine will commit the node result and notify the coordinator after this execution ends. Do not call tools.'
            : 'A research wait was accepted in this execution. Acknowledge that the coordinator is waiting and end this response; ' +
            'the engine will deliver the result and wake the coordinator. Do not call tools.' })
        }
        // Allocate a new array: the boundary helper returns the host's array
        // when the history already carries a hint, and the tail must never be
        // written into the host's own message list.
        if (tail.length) request.messages = [...request.messages, researchStateMessage(tail) as any]
      } catch (error) {
        const registered = research.registeredRootOf(request.sessionID)
        if (registered && !research.inScope(registered) && !isSessionEntryError(error)) {
          error = locationError(registered, 'unavailable (membership lookup failed)', research.scope)
        }
        if (isSessionEntryError(error)) {
          for (const name of Object.keys(request.tools)) delete request.tools[name]
          request.system.splice(0, request.system.length, {type: 'text', text: error.message})
          // Keep the request closed even if persisting its fault fails.
          if (error.code === 'research_tool_entry_unavailable') await engine.blockTools(request.sessionID, error)
          // An explicit diagnostic response gets no tools. Keep the host loop
          // able to process user-requested location changes at its boundary;
          // the owning controller separately prevents autonomous continuation.
          return
        }
        // Unknown control is not permission to use an old research context or execute tools.
        if (!lifecycle.current() && lifecycle.successorReady()) return lifecycle.forwardContext(request)
        if (research.isAssociated(request.sessionID) || findProjectRoot(research.scope) === research.scope) {
          protectUnknown()
          for (const name of Object.keys(request.tools)) {
            if (!COORDINATOR_NATIVE_READS.has(name) && !RESEARCH_READS.has(name)) delete request.tools[name]
          }
          request.system.push({ type: "text", text: "Research control is unavailable. Read existing information only; do not start research or assume it is running." })
        }
      }
    }
    await ctx.session.hook("context", injectContext)

    // A message the user types into a research session is guidance: record it
    // and let it reach the model; the loop keeps going afterwards. While an
    // accepted terminal action, or one still awaiting the service, owns this
    // execution, the host queues the input instead of steering the finishing
    // request. The guidance note then waits for delivery and for the execution
    // after it, so the old turn's context never carries the pending text.
    await ctx.session.hook("prompt", async (input) => {
      try {
        if (!lifecycle.current()) return
        if (!research.isAssociated(input.sessionID)) return
        const prompt = (input.prompt ?? {}) as { text?: unknown; files?: unknown }
        const text = String(prompt.text ?? "").trim()
        // A slash command keeps its own entry and priority.
        if (text.startsWith("/")) return
        const attachments = Array.isArray(prompt.files) ? prompt.files.length : 0
        const role = research.roleOf(input.sessionID)
        const messageID = String(input.messageID ?? "")
        // Decide before the first await: the host gets the final delivery
        // value in the same synchronous turn this hook returns. A message with
        // only an attachment is still a real user input and must be held too.
        if (messageID && contextPlacement === 'tail' && (role === 'main' || role === 'node_core')
            && (text.length > 0 || attachments > 0) && terminalWindow(input.sessionID)) {
          input.delivery = 'queue'
          // Only text becomes a note; an attachment-only input still reaches the
          // next request through the host's own history.
          if (text) rememberQueuedGuidance(input.sessionID, messageID, text, engine.executionToken(input.sessionID))
          return
        }
        if (!text) return
        await research.call("note", input.sessionID, guidanceNote(text))
      } catch {
        // Without an open work segment there is nowhere to file the note; the message still goes through.
      }
    })

    async function enforceDeadline(sessionID:string): Promise<void> {
      const stopped = await research.call('runtime_budget_stop', sessionID)
      if (stopped.stopped) {
        const root = research.rootOf(sessionID)!
        for (const sid of research.sessionsIn(root)) engine.expectInterrupt(sid)
        await Promise.all(research.sessionsIn(root).map(sessionID => host.interrupt(sessionID).catch(() => {})))
        const main = research.sessionsIn(root).find(sid=>research.roleOf(sid)==='main')
        if (main) await host.synthetic(main, '【Research】自主调度已到时限，排队任务已取消，正在停止会话。需显式提额后恢复。', '自主调度时限已到', false)
        research.notifyChanged(root)
      }
    }
    const budgetTimers=new Map<string,ReturnType<typeof setTimeout>>()
    const refreshingBudgets=new Set<string>()
    const dirtyBudgets=new Set<string>()
    async function refreshBudget(root:string):Promise<void>{
      if(refreshingBudgets.has(root)){dirtyBudgets.add(root);return}
      refreshingBudgets.add(root)
      try{
        const main=research.sessionsIn(root).find(sid=>research.roleOf(sid)==='main')
        if(!main || !listening){clearTimeout(budgetTimers.get(root));budgetTimers.delete(root);return}
        const deadline=await research.call('runtime_deadline',main)
        if(!listening)return
        clearTimeout(budgetTimers.get(root));budgetTimers.delete(root)
        if(listening && deadline.state==='running' && deadline.deadline_at){
          const delay=Math.max(0,Date.parse(deadline.deadline_at)-Date.now())
          budgetTimers.set(root,setTimeout(()=>{
            budgetTimers.delete(root)
            void (delay>2147483647 ? refreshBudget(root) : enforceDeadline(main)).catch(error=>{
              observations.append(root,{phase:'coverage_gap',source:'deadline_monitor',error_code:error.code ?? 'storage_unavailable'})
            })
          },Math.min(delay,2147483647)))
        }
      }catch(error){
        observations.append(root,{phase:'coverage_gap',source:'deadline_monitor',error_code:(error as any).code ?? 'storage_unavailable'})
      }finally{
        refreshingBudgets.delete(root)
        if(dirtyBudgets.delete(root) && listening)void refreshBudget(root)
      }
    }
    const shellMonitor = setInterval(() => {
      if (listening && lifecycle.current()) void engine.pollShells().catch(error => {
        process.stderr.write(`Research shell monitor deferred: ${String(error)}\n`)
      })
    }, 15_000)
    shellMonitor.unref?.()
    eventHandler = async event => {
      if (!listening || !lifecycle.current()) return
      const { type, data } = event as { type: string; data?: { sessionID?: string } }
      const sessionID = data?.sessionID
      if (sessionID && !research.isAssociated(sessionID)) await research.confirmMembership(sessionID)
      if (!sessionID || !research.isAssociated(sessionID)) return
      await bootstrap()
      if (!lifecycle.current()) return
      const root = research.rootOf(sessionID)!
      const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
      if (main && research.inScope(root)) await ensureController(root, main)
      const sequence = typeof event.durable?.seq === 'number' ? event.durable.seq : undefined
      if (type === "session.execution.started") {
        await engine.started(sessionID, event.id, sequence)
        const current = engine.executionToken(sessionID)
        const marker = lifecycle.state.waitFinishes?.get(sessionID)
        if (marker && marker.execution !== current) clearTurnFinish(sessionID, marker.execution)
      }
      else if (type === 'session.inbox.enqueued') {
        const metadata = (data as any)?.item?.payload?.metadata
        // Register in execute.before: the notification can precede execute.after itself.
        if (metadata?.source === 'shell') await within(Promise.all([...shellCalls.values()]
          .filter(call => call.sessionID === sessionID).map(call => call.settled)).then(() => {}), 2000)
        await engine.shellNotice(sessionID, String((data as any)?.inboxID ?? ''), metadata)
      }
      else if (type === 'session.tool.failed') {
        const callID = (data as any)?.id
        const key = `${sessionID}:${callID}`
        const call = shellCalls.get(key)
        if (call) {
          // OpenCode 2.2.5 omits execute.after for foreground failure/abort.
          // Its terminal tool event arrives after the foreground shell is killed.
          try {
            await research.call('runtime_shell_result', sessionID, {call_id:callID,
              state:call.background ? 'unverified' : 'finished',shell_id:null}, `shell-result:${sessionID}:${callID}`)
          } catch (error) {
            observations.append(root,{phase:'coverage_gap',source:'shell_failure_result',session_id:sessionID,
              error_code:(error as any)?.code ?? 'storage_unavailable'})
          } finally {
            call.resolve(); shellCalls.delete(key); externalStarts.delete(key)
          }
        }
      }
      else if (type === 'session.inbox.delivered') {
        const inboxID = String((data as any)?.inboxID ?? '')
        await engine.inboxDelivered(sessionID, inboxID, sequence)
        markQueuedGuidanceDelivered(sessionID, inboxID)
      }
      else if (['session.execution.succeeded', 'session.execution.failed', 'session.execution.interrupted'].includes(type)) {
        const outcome = type.slice('session.execution.'.length) as 'succeeded' | 'failed' | 'interrupted'
        const ending = engine.executionToken(sessionID)
        const handled = await engine.ended(sessionID, outcome, event.id, sequence)
        // Only a terminal event this handler actually processed may confirm
        // that the marked execution ended. Replayed duplicates and no-op ends
        // leave a newer execution's marker untouched.
        if (handled && engine.executionToken(sessionID) === ending) clearTurnFinish(sessionID, ending)
      }
      nativeObservations.event(event)
    }
    telemetryHandler = event => {
      if (listening && lifecycle.current() && research.isAssociated(event.data?.sessionID)) nativeObservations.event(event)
    }
    for (const event of earlyEvents.splice(0)) queueEvent(event)

    async function sessionDirectory(sessionID: string): Promise<string> {
      const session = await ctx.session.get({ sessionID })
      return session.location.directory
    }

    function modelArg(value: any): ModelRef | undefined {
      if (!value || typeof value.id !== "string" || typeof value.providerID !== "string") return undefined
      return { id: value.id, providerID: value.providerID, ...(typeof value.variant === "string" ? { variant: value.variant } : {}) }
    }

    const USER_COMMAND_LABELS: Record<string, string> = {
      budget: "/research budget",
      exploration: "/research exploration",
      recover: "/research recover",
      init: "/research auto --pause",
      takeover: "/research recover takeover",
      retry: "/research recover retry",
      resume: "/research auto",
    }

    function needsSession(sessionID: string | null, action: string): string {
      if (!sessionID) {
        const label = USER_COMMAND_LABELS[action]
        throw new Error(label ? `请先进入一个会话，再使用 ${label}。` : "请先进入一个会话再执行该操作。")
      }
      return sessionID
    }

    function runCommand(
      action: string,
      sessionID: string | null,
      directory: string,
      args: Record<string, any>,
    ): Promise<CommandResult> {
      switch (action) {
        case "status":
          return research.status(sessionID, directory)
        case 'budget': {
          return (async () => {
            const sid = needsSession(sessionID,action)
            if (args.limit) {
              const names:Record<string,string> = {turns:'max_session_turns',nodes:'max_total_nodes',seconds:'max_wall_seconds',tokens:'max_tokens',cost:'max_cost',work:'shared_work_budget',resource:'resource_capacities'}
              if (!names[args.limit]) throw new Error('未知预算字段')
              let value:any = args.value==='off' ? null : Number(args.value)
              if (args.limit==='resource') {
                const current = await research.call<any>('runtime_budget_status',sid)
                value = {...(current.limits?.resource_capacities ?? {})}
                if (args.value==='off') delete value[String(args.resource)]
                else value[String(args.resource)] = Number(args.value)
              }
              await research.call('runtime_budget_set',sid,{limits:{[names[args.limit]!]:value},reason:args.reason})
              research.notifyChanged(research.rootOf(sid)!)
            }
            const budget = await research.call('runtime_budget_status',sid)
            return {message:`自主调度预算（work 为计划投入单位；token/cost 为已观测停发阈值，外部进程未计量）；逐项分配与租约：research_query collection=exploration_budget\n${JSON.stringify(runtimeBudgetSummary(budget),null,2)}`}
          })()
        }
        case "auto":
          return engine.auto(sessionID, directory, String(args.goal ?? ""), modelArg(args.model))
        case "pause":
          return engine.pause(sessionID, directory)
        case "resume":
          return engine.resume(sessionID, directory, modelArg(args.model))
        case "recover":
          return (async () => {
            const sid = needsSession(sessionID, action)
            const root = research.requireAssociated(sid)
            if (research.roleOf(sid) !== 'main') throw new Error('只能由项目主会话执行恢复操作。')
            if (args.kind === 'controller') {
              try { await ensureController(root, sid) } catch { /* adoption validates the persisted binding */ }
              const owner = await research.call<any>('controller_adopt', sid, { reason: String(args.reason ?? '') })
              research.setController(root, { epoch: owner.epoch, binding: owner.binding, fence: owner.fence })
              controllers.set(root, Promise.resolve())
              boot = null
              research.notifyChanged(root)
              return { message: '控制器已接管；项目保持暂停，未知执行仍需逐项核对。' +
                (eventGap ? ' 当前事件流需核对，可用 /research auto 重新订阅并核对。' : '核对后可显式 /research auto。') }
            }
            await bootstrap()
            if (args.kind === 'resource') return engine.recoverResource(sid, String(args.task_id ?? ''), String(args.reason ?? ''))
            if (args.kind === 'task') return engine.recoverTask(sid, String(args.task_id ?? ''), String(args.reason ?? ''))
            if (args.kind === 'specialist') {
              await research.call('recovery_confirm_specialist', sid, { task_id: String(args.task_id ?? ''), reason: String(args.reason ?? '') })
              research.notifyChanged(root)
              return { message: `已记录专家 ${args.task_id} 的人工退出核对，已有报告保留。` }
            }
            if (args.kind === 'delivery') return engine.recoverDelivery(sid, String(args.notification_id ?? ''),
              String(args.delivery_kind ?? ''), String(args.disposition ?? ''), String(args.reason ?? ''))
            throw new Error('未知恢复对象。')
          })()
        case "retry":
          return engine.retryTask(needsSession(sessionID, action), String(args.task_id ?? ''), String(args.reason ?? ''))
        case "stop":
          return engine.stop(sessionID, directory)
        case "init":
          return (async () => {
            const sid = needsSession(sessionID, action)
            const result = await research.init(sid, directory, String(args.goal ?? ""))
            await ensureController(research.requireAssociated(sid), sid)
            return result
          })()
        case "takeover":
          return research.takeover(needsSession(sessionID, action), directory)
        case "exploration":
          return engine.explorationPolicy(needsSession(sessionID, action), args.mode)
        // Internal-only RPC actions (no user slash entry): retained alongside `status` and
        // `resume` for host probe scripts (`scripts/probe_*.py`) and isolated R1 / evidence-v1 /
        // session-boundary test harnesses.
        case "detach":
          return research.detach(needsSession(sessionID, action))
        case "context-policy":
          return research.contextPolicy(needsSession(sessionID, action), args.policy)
        case "conclusion-contract":
          return research.conclusionContract(needsSession(sessionID, action), args.contract)
        default:
          throw new Error(`未知的 /research 子命令：${action}\n\n${USAGE}`)
      }
    }

    const commandHandler = (input: any) => reported(async () => {
          const action = String(input.action ?? "help")
          if (action === "help") return { message: USAGE }
          if ((action === 'resume' || (action === 'auto' && !input.args?.goal)) && (eventGap || failedEvents.size)) await repairEvents()
          if (!['status', 'recover'].includes(action)) await bootstrap()
          if (['auto','resume','init','takeover','budget','recover','retry'].includes(action)) await research.ensureContract()
          await within(research.ready(), STARTUP_WAIT_MS)
          const sessionID = input.sessionID ? String(input.sessionID) : null
          if (sessionID) {
            await research.confirmMembership(sessionID)
            const registered = research.registeredRootOf(sessionID)
            // Management commands at the owning root do not require model tools.
            // No foreign instance may initialize or take over this ledger.
            if (registered && !research.inScope(registered)) await assertLocation(sessionID)
          }
          const directory = sessionID ? await sessionDirectory(sessionID) : String(input.directory ?? ctx.location.directory)
          const result = await runCommand(action, sessionID, directory, (input.args ?? {}) as Record<string, any>)
          if (action === 'status') {
            if (sessionID) {
              try { await assertLocation(sessionID) }
              catch (error) { if (!isSessionEntryError(error)) throw error; result.message += '\n' + error.message }
            }
            const health = eventHealth(sessionID ? research.rootOf(sessionID) : findProjectRoot(directory) ?? undefined)
            if (health?.message) result.message += '\n' + health.message
            if (health && result.state && typeof result.state === 'object') result.state = { ...result.state, event_recovery: health }
          }
          const target = result.sessionID ?? sessionID
          if (result.note && target) {
            // resume:false records the line without starting a model turn.
            void host.synthetic(target, result.note, result.note, false).catch(() => {})
          }
          return result
        })
    const sessionHandler = (input: any) => reported(async () => {
          const sessionID = String(input.sessionID)
          await within(research.confirmMembership(sessionID), STARTUP_WAIT_MS)
          const registered = research.registeredRootOf(sessionID)
          if (registered && !research.inScope(registered)) await assertLocation(sessionID)
          const state = await research.sessionState(sessionID)
          const root = research.rootOf(sessionID)
          return { ...state, eventRecovery: eventHealth(root) ?? null, observationCoverage: root && research.inScope(root) ? observations.coverage(root) : null }
        })
    const boardHandler = (input: any) => reported(async () => {
          const sessionID = input.sessionID ? String(input.sessionID) : null
          const directory =
            input.directory ?? (sessionID ? await sessionDirectory(sessionID) : ctx.location.directory)
          const view = String(input.view)
          const data = await research.board(directory, view, input.fields ?? {})
          if (view !== 'summary' || !data?.project_root) return data
          let membershipReady = true
          await within(research.ready(), STARTUP_WAIT_MS).catch(() => { membershipReady = false })
          if (membershipReady && sessionID) {
            await within(research.confirmMembership(sessionID), STARTUP_WAIT_MS).catch(() => { membershipReady = false })
          }
          const root = String(data.project_root)
          const hasHostMain = membershipReady
            ? research.sessionsIn(root).some((sid) => research.roleOf(sid) === 'main')
            : undefined
          const health = eventHealth(root)
          let locationWarning: string | undefined
          if (sessionID) {
            try { await assertLocation(sessionID) }
            catch (error) { if (isSessionEntryError(error)) locationWarning = error.message }
          }
          return {
            ...data,
            ...(hasHostMain !== undefined ? { has_host_main: hasHostMain } : {}),
            ...(health ? { event_recovery: health } : {}),
            ...(locationWarning ? { location_warning: locationWarning } : {}),
          }
        })
    const routeRpc = (kind: 'command' | 'session' | 'board', input: any) => {
      if (lifecycle.current()) return { command: commandHandler, session: sessionHandler, board: boardHandler }[kind](input)
      if (lifecycle.successorReady()) return lifecycle.forwardRpc(kind, input)
      return Promise.resolve({ error: '研究控制器正在交接；请稍后再试。' })
    }
    const registration = await ctx.rpc.register(ResearchRpc, {
      command: (input: any) => routeRpc('command', input),
      session: (input: any) => routeRpc('session', input),
      board: (input: any) => routeRpc('board', input),
    })
    emitChanged = (root) => {
      if (!lifecycle.current()) return
      void registration.events.emit("changed", { root }).catch(() => {})
      void refreshBudget(root).catch(error=>observations.append(root,{phase:'coverage_gap',source:'deadline_monitor',error_code:error.code ?? 'storage_unavailable'}))
    }
    lifecycle.setHandlers({ before: beforeTool, context: injectContext, tool: runTool,
      command: commandHandler, session: sessionHandler, board: boardHandler })
    void research.ready().then(()=>Promise.all(research.roots().map(root=>refreshBudget(root)))).catch(() => {})
    void bootstrap().then(() => lifecycle.markReady()).catch(() => {})

    return () => {
      clearInterval(shellMonitor)
      lifecycle.detach()
      listening = false
      subscription?.abort()
      for(const timer of budgetTimers.values())clearTimeout(timer)
      storage.close()
      workspaceProbe.close()
    }
  },
})
