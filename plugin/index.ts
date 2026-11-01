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
import { StorageClient } from "./src/storage"
import { Research, findProjectRoot, guidanceNote, type CommandResult } from "./src/research"
import { Engine, type ModelRef, type SessionHost, type ConclusionInput } from "./src/engine"
import { BLIND_SYSTEM, Specialists, type SpecialistTask } from "./src/specialists"
import { TOOLS, keepsTool, type ToolDefinition } from "./src/tools"
import { COORDINATOR_NATIVE_READS, controlInstructions } from "./src/coordinator-boundary"
import { parameters, validateArgs } from "./src/schema"
import { USAGE } from "./src/format"
import { SOURCE_ALGORITHM, SOURCE_LIMITS, WorkspaceProbe, type SourceSnapshot } from './src/workspace-probe'
import { attachController } from './src/controller-lifecycle'
import { preflight } from './src/host-submission'

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
      specialistTimeoutMs?: number
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
    let queuedEvents = 0
    const relevantEvents = new Set(['session.execution.started', 'session.execution.succeeded', 'session.execution.failed',
      'session.inbox.delivered', 'session.execution.interrupted'])
    let notifyEventState = () => {}
    const markGap = () => { eventGap = true; gapVersion++; notifyEventState() }
    const waitEvents = (sessionID: string) => eventChains.get(sessionID) ?? Promise.resolve()
    const queueEvent = (event: any) => {
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
        ? `有 ${unresolved} 个事件会话归属待核对；已知会话继续按原授权控制。用 /research resume 重试核对。`
        : `研究事件需核对（${eventGap ? '事件流中断或溢出；' : ''}暂存 ${held} 条）；暂停和停止仍可用，用 /research resume 重新订阅并核对。`
      return { state, held, unresolved, ...(message ? {message} : {}) }
    }
    notifyEventState = () => { for (const root of research.roots()) if (research.inScope(root)) emitChanged(root) }
    research.setExecutionResolver(sessionID => lifecycle.state.turns.get(sessionID))
    let ensureController: (root: string, main: string) => Promise<void> = async () => { throw new Error('Controller not initialized') }
    let bootstrap: () => Promise<void> = async () => { throw new Error('Controller not initialized') }
    const verifyController = async (sessionID: string, reducingOrReading = false) => {
      requireCurrent()
      if (!reducingOrReading && (eventGap || failedEvents.has(sessionID))) throw new Error('研究事件需核对；停止和暂停仍可用，核对后可 /research resume')
      const root = research.rootOf(sessionID) ?? research.scope
      const main = research.sessionsIn(root).find(sid => research.roleOf(sid) === 'main')
      if (!main || !research.inScope(root)) throw new Error('Research controller scope is unavailable')
      await ensureController(root, main)
      await research.call('controller_check', sessionID)
      requireCurrent()
    }
    const host: SessionHost = {
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
            title,
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
    }
    const nativeObservations = new NativeObservations(research,observations)
    await ctx.session.hook('model.request',request => { if (lifecycle.current()) nativeObservations.modelRequest(request) })
    await ctx.session.hook('retry',request => {
      const root = research.rootOf(request.sessionID)
      if (root && research.inScope(root)) observations.append(root,{phase:'provider_retry',session_id:request.sessionID,
        attempt:request.attempt,model:request.model,will_retry:request.decision.retry})
    })
    const externalStarts = new Map<string,number>()
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
      const root = research.rootOf(request.sessionID)
      if (!root) return
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
      observations.append(root,{phase:'external_execution',session_id:request.sessionID,id:request.id,tool:request.tool,
        status:request.status,duration_ms:performance.now()-started,resource_usage:'unknown'})
      if (request.status === 'completed' && research.roleOf(request.sessionID) === 'node_core' &&
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
      count(options.specialistTimeoutMs, 600_000),
      lifecycle.state,
      lifecycle.current,
    )
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
      if (tool.writes && (eventGap || failedEvents.has(call.sessionID))) throw new Error('研究事件需核对；停止和暂停仍可用，核对后可 /research resume')
      if (tool.writes) requireCurrent()
      const sessionID = call.sessionID as string
      await research.confirmMembership(sessionID)
      if (!research.isAssociated(sessionID)) {
        throw new Error("本会话没有关联研究项目。用 /research auto <目标> 开始，或 /research takeover 接管。")
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
        case "wait":
          return engine.wait(sessionID, (args.task_ids ?? []).map(String))
        case "finish":
          return engine.finish(sessionID, { state: String(args.state), summary: String(args.summary) }, operationID)
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
        if (role === "main") {
          const state = await research.call<any>("status", request.sessionID)
          const run = state.workflow?.run?.state
          const pause = state.workflow?.session?.pause_reason
          if (state.project?.config?.exploration_policy !== 'exploration-v1') {
            for (const name of ['research_scope', 'research_route']) delete request.tools[name]
            if (!state.exploration_closing_allowed) delete request.tools.research_decide
            else if (request.tools.research_decide) {
              const spec=TOOLS.find(tool=>tool.name==='research_decide')!.parameters
              const closing=Object.fromEntries(Object.entries(spec).filter(([key])=>key!=='next_work')
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
        }
        if (role === 'node_core') {
          await waitEvents(request.sessionID)
          if (!await engine.nodeTurnAllowed(request.sessionID)) {
            for (const name of Object.keys(request.tools)) {
              if (!COORDINATOR_NATIVE_READS.has(name) && name !== 'research_query') delete request.tools[name]
            }
            request.system.push({ type: 'text', text: 'This node turn has no active drain authorization. Read existing material only until the user resumes or execution is reconciled.' })
          }
        }
        const deadline=performance.now()+2000
        const evidence = role==='main' || role==='node_core'
          ? research.observeEvidence(request.sessionID,request.messages,Math.max(0,deadline-performance.now()))
          : Promise.resolve([])
        const text = await research.memory(request.sessionID)
        if (text) {
          request.system.push({ type: "text", text })
          if (role==='main' || role==='node_core') await research.delivered(request.sessionID,text,request.messages,deadline-performance.now())
        }
        const issues = await evidence
        if (issues.length) request.system.push({type:'text',text:`Evidence delivery was not fully confirmed (${issues.join(', ')}). Re-read the affected pages and retry.`})
      } catch {
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
    // and let it reach the model; the loop keeps going afterwards.
    await ctx.session.hook("prompt", async (input) => {
      try {
        if (!lifecycle.current()) return
        if (!research.isAssociated(input.sessionID)) return
        const text = String((input.prompt as any)?.text ?? "").trim()
        if (!text || text.startsWith("/")) return
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
      if (type === "session.execution.started") await engine.started(sessionID, event.id, sequence)
      else if (type === 'session.inbox.delivered') await engine.inboxDelivered(sessionID, String((data as any)?.inboxID ?? ''), sequence)
      else if (['session.execution.succeeded', 'session.execution.failed', 'session.execution.interrupted'].includes(type)) {
        const outcome = type.slice('session.execution.'.length) as 'succeeded' | 'failed' | 'interrupted'
        await engine.ended(sessionID, outcome, event.id, sequence)
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

    function needsSession(sessionID: string | null, action: string): string {
      if (!sessionID) throw new Error(`请先进入一个会话，再使用 /research ${action}。`)
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
              const names:Record<string,string> = {turns:'max_session_turns',nodes:'max_total_nodes',seconds:'max_wall_seconds',tokens:'max_tokens',cost:'max_cost'}
              if (!names[args.limit]) throw new Error('未知预算字段')
              await research.call('runtime_budget_set',sid,{limits:{[names[args.limit]!]:args.value==='off'?null:Number(args.value)},reason:args.reason})
              research.notifyChanged(research.rootOf(sid)!)
            }
            const budget = await research.call('runtime_budget_status',sid)
            return {message:`自主调度预算（token/cost 为已观测停发阈值，外部进程未计量）\n${JSON.stringify(budget,null,2)}`}
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
                (eventGap ? ' 当前事件流需核对，可用 /research resume 重新订阅并核对。' : '核对后可显式 /research resume。') }
            }
            await bootstrap()
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
        case "detach":
          return research.detach(needsSession(sessionID, action))
        case "context-policy":
          return research.contextPolicy(needsSession(sessionID, action), args.policy)
        case "exploration":
          return engine.explorationPolicy(needsSession(sessionID, action), args.mode)
        case "conclusion-contract":
          return research.conclusionContract(needsSession(sessionID, action), args.contract)
        case "guidance":
          return research.guidance(needsSession(sessionID, action), directory, args.path, args.version)
        default:
          throw new Error(`未知的 /research 子命令：${action}\n\n${USAGE}`)
      }
    }

    const commandHandler = (input: any) => reported(async () => {
          const action = String(input.action ?? "help")
          if (action === "help") return { message: USAGE }
          if (action === 'resume' && (eventGap || failedEvents.size)) await repairEvents()
          if (!['status', 'recover'].includes(action)) await bootstrap()
          if (['auto','resume','init','takeover','budget','recover','retry'].includes(action)) await research.ensureContract()
          await within(research.ready(), STARTUP_WAIT_MS)
          const sessionID = input.sessionID ? String(input.sessionID) : null
          if (sessionID && action !== 'takeover') await research.confirmMembership(sessionID)
          const directory = sessionID ? await sessionDirectory(sessionID) : String(input.directory ?? ctx.location.directory)
          const result = await runCommand(action, sessionID, directory, (input.args ?? {}) as Record<string, any>)
          if (action === 'status') {
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
          const state = await research.sessionState(sessionID)
          const root = research.rootOf(sessionID)
          return { ...state, eventRecovery: eventHealth(root) ?? null, observationCoverage: root && research.inScope(root) ? observations.coverage(root) : null }
        })
    const boardHandler = (input: any) => reported(async () => {
          const directory =
            input.directory ?? (input.sessionID ? await sessionDirectory(String(input.sessionID)) : ctx.location.directory)
          return research.board(directory, String(input.view), input.fields ?? {})
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
      lifecycle.detach()
      listening = false
      subscription?.abort()
      for(const timer of budgetTimers.values())clearTimeout(timer)
      storage.close()
      workspaceProbe.close()
    }
  },
})
