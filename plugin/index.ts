/**
 * Server half of the research plugin, built around the autonomous loop:
 * execution events drive the engine, research tools write the ledger, and
 * each model request of a research session carries its role's research
 * context. Sessions outside a project see exactly what they would without
 * the plugin: the context hook only removes this plugin's tools.
 */
import { Plugin } from "@opencode/plugin"
import { existsSync } from "node:fs"
import { randomUUID } from "node:crypto"
import { Observations } from "./src/observations"
import { NativeObservations } from './src/native-observations'
import { ResearchRpc } from "./rpc"
import { StorageClient } from "./src/storage"
import { Research, guidanceNote, type CommandResult } from "./src/research"
import { Engine, type ModelRef, type SessionHost, type ConclusionInput } from "./src/engine"
import { BLIND_SYSTEM, Specialists, type SpecialistTask } from "./src/specialists"
import { TOOLS, keepsTool, type ToolDefinition } from "./src/tools"
import { parameters, validateArgs } from "./src/schema"
import { USAGE } from "./src/format"
import { SOURCE_ALGORITHM, SOURCE_LIMITS, WorkspaceProbe, type SourceSnapshot } from './src/workspace-probe'

const STARTUP_WAIT_MS = 1500

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
    const host: SessionHost = {
      source: async sessionID => {
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
      create: async (title, directory, model, permissions) =>
        (
          await ctx.session.create({
            title,
            location: { directory },
            ...(model ? { model } : {}),
            ...(permissions ? { permissions } : {}),
          })
        ).id,
      switchModel: async (sessionID, model) => {
        await ctx.session.switchModel({ sessionID, model })
      },
      synthetic: async (sessionID, text, label, resume) => {
        let reservation:any
        if (resume && research.isAssociated(sessionID)) {
          const source_snapshot=await host.source?.(sessionID)
          reservation = await research.call('runtime_reserve', sessionID,{source_snapshot})
          if (!reservation.allowed) {
            if (reservation.reason==='wall_clock') await enforceDeadline(sessionID)
            research.notifyChanged(research.rootOf(sessionID)!)
            throw Object.assign(new Error(`自主调度预算已到限：${reservation.reason}；先用 /research budget 显式调整额度，再恢复。`), { code: 'runtime_budget_exhausted' })
          }
        }
        await ctx.session.synthetic({ sessionID, text, description: label, resume })
        if (reservation?.allowed) await research.call('runtime_submitted',sessionID,
          {reservation_id:reservation.reservation_id},`submitted:${reservation.reservation_id}`).catch(error => {
            observations.append(research.rootOf(sessionID)!,{phase:'coverage_gap',source:'budget_submission',
              session_id:sessionID,error_code:error.code ?? 'storage_unavailable'})
          })
      },
      interrupt: async (sessionID) => {
        await ctx.session.interrupt({ sessionID })
      },
      wait: async (sessionID) => {
        await ctx.session.wait({ sessionID })
      },
      messages: async (sessionID) => (await ctx.session.context({ sessionID })) as any[],
    }
    const nativeObservations = new NativeObservations(research,observations)
    await ctx.session.hook('model.request',request => nativeObservations.modelRequest(request))
    await ctx.session.hook('retry',request => {
      const root = research.rootOf(request.sessionID)
      if (root && research.inScope(root)) observations.append(root,{phase:'provider_retry',session_id:request.sessionID,
        attempt:request.attempt,model:request.model,will_retry:request.decision.retry})
    })
    const externalStarts = new Map<string,number>()
    await ctx.tool.hook('execute.before',request => {
      const root = research.rootOf(request.sessionID)
      if (!root || request.tool.startsWith('research_')) return
      externalStarts.set(`${request.sessionID}:${request.id}`,performance.now())
      observations.append(root,{phase:'external_execution',session_id:request.sessionID,id:request.id,tool:request.tool,
        status:'started',execution_ref:`session:${request.sessionID}`,resource_usage:'unknown'})
    })
    await ctx.tool.hook('execute.after',request => {
      const key = `${request.sessionID}:${request.id}`
      const started = externalStarts.get(key)
      if (started===undefined) return
      externalStarts.delete(key)
      const root = research.rootOf(request.sessionID)!
      observations.append(root,{phase:'external_execution',session_id:request.sessionID,id:request.id,tool:request.tool,
        status:request.status,duration_ms:performance.now()-started,resource_usage:'unknown'})
    })
    const count = (value: unknown, fallback: number) => Math.max(1, Math.floor(Number(value ?? fallback)) || fallback)
    const engine = new Engine(research, host, count(options.concurrency, 1))
    const specialists = new Specialists(
      research,
      host,
      (root) => engine.modelFor(root),
      count(options.specialistFanout, 2),
      count(options.specialistTimeoutMs, 600_000),
    )
    void engine.coldRecover()
    void specialists.recover()

    async function runTool(tool: ToolDefinition, call: any, args: Record<string, any>): Promise<unknown> {
      const sessionID = call.sessionID as string
      if (!research.isAssociated(sessionID)) {
        throw new Error("本会话没有关联研究项目。用 /research auto <目标> 开始，或 /research takeover 接管。")
      }
      const role = research.roleOf(sessionID)
      if (!tool.roles.includes(role as any)) throw new Error(`${tool.name} 不属于本会话的角色（${role ?? "未登记"}）`)
      const operationID = `${sessionID}:${call.messageID}:${call.id}:${tool.name}`
      switch (tool.engine) {
        case "dispatch":
          return engine.dispatch(sessionID, String(args.node_id), operationID)
        case "wait":
          return engine.wait(sessionID, (args.task_ids ?? []).map(String))
        case "finish":
          return engine.finish(sessionID, { state: String(args.state), summary: String(args.summary) })
        case "conclude":
          return engine.conclude(sessionID, args as unknown as ConclusionInput, operationID)
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

    await ctx.session.hook("context", async (request) => {
      try {
        await within(research.ready(), STARTUP_WAIT_MS)
        const associated = research.isAssociated(request.sessionID)
        const role = research.roleOf(request.sessionID)
        const granted = specialists.toolsOf(request.sessionID)
        for (const name of Object.keys(request.tools)) {
          if (!keepsTool(name, associated, role, granted)) delete request.tools[name]
        }
        if (!associated) return
        if (role === "specialist" && specialists.isBlind(request.sessionID)) {
          // OpenCode's prompt names the working directory and carries AGENTS.md; a blind reviewer gets neither.
          request.system.splice(0, request.system.length, { type: "text", text: BLIND_SYSTEM })
        }
        const deadline=performance.now()+2000
        const text = await research.memory(request.sessionID)
        if (text) {
          request.system.push({ type: "text", text })
          if (role==='main' || role==='node_core') await research.delivered(request.sessionID,text,request.messages,deadline-performance.now())
        }
      } catch {
        // Research state must never break a model request.
      }
    })

    // A message the user types into a research session is guidance: record it
    // and let it reach the model; the loop keeps going afterwards.
    await ctx.session.hook("prompt", async (input) => {
      try {
        if (!research.isAssociated(input.sessionID)) return
        const text = String((input.prompt as any)?.text ?? "").trim()
        if (!text || text.startsWith("/")) return
        await research.call("note", input.sessionID, guidanceNote(text))
      } catch {
        // Without an open work segment there is nowhere to file the note; the message still goes through.
      }
    })

    let listening = true
    async function enforceDeadline(sessionID:string): Promise<void> {
      const stopped = await research.call('runtime_budget_stop', sessionID)
      if (stopped.stopped) {
        const root = research.rootOf(sessionID)!
        for (const sid of research.sessionsIn(root)) engine.expectInterrupt(sid)
        await Promise.all(research.sessionsIn(root).map(sessionID => ctx.session.interrupt({sessionID})))
        const main = research.sessionsIn(root).find(sid=>research.roleOf(sid)==='main')
        if (main) await ctx.session.synthetic({sessionID:main,text:'【Research】自主调度已到时限，排队任务已取消，正在停止会话。需显式提额后恢复。',description:'自主调度时限已到',resume:false})
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
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe()) {
          if (!listening) break
          const { type, data } = event as { type: string; data?: { sessionID?: string } }
          const sessionID = data?.sessionID
          if (!sessionID || !research.isAssociated(sessionID)) continue
          if (type === "session.execution.started") engine.started(sessionID)
          else if (type === "session.execution.succeeded") void engine.ended(sessionID, "succeeded",event.id)
          else if (type === "session.execution.failed") void engine.ended(sessionID, "failed",event.id)
          else if (type === "session.execution.interrupted") void engine.ended(sessionID, "interrupted",event.id)
          nativeObservations.event(event)
        }
      } catch {
        // The stream ends when the plugin unloads.
      }
    })()

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
        case "stop":
          return engine.stop(sessionID, directory)
        case "init":
          return research.init(needsSession(sessionID, action), directory, String(args.goal ?? ""))
        case "takeover":
          return research.takeover(needsSession(sessionID, action), directory)
        case "detach":
          return research.detach(needsSession(sessionID, action))
        case "context-policy":
          return research.contextPolicy(needsSession(sessionID, action), args.policy)
        case "guidance":
          return research.guidance(needsSession(sessionID, action), directory, args.path, args.version)
        default:
          throw new Error(`未知的 /research 子命令：${action}\n\n${USAGE}`)
      }
    }

    const registration = await ctx.rpc.register(ResearchRpc, {
      command: (input: any) =>
        reported(async () => {
          const action = String(input.action ?? "help")
          if (action === "help") return { message: USAGE }
          if (['auto','resume','init','takeover','budget'].includes(action)) await research.ensureContract()
          await within(research.ready(), STARTUP_WAIT_MS)
          const sessionID = input.sessionID ? String(input.sessionID) : null
          const directory = sessionID ? await sessionDirectory(sessionID) : String(input.directory ?? ctx.location.directory)
          const result = await runCommand(action, sessionID, directory, (input.args ?? {}) as Record<string, any>)
          const target = result.sessionID ?? sessionID
          if (result.note && target) {
            // resume:false records the line without starting a model turn.
            void ctx.session.synthetic({ sessionID: target, text: result.note, description: result.note, resume: false }).catch(() => {})
          }
          return result
        }),
      session: (input: any) =>
        reported(async () => {
          await within(research.ready(), STARTUP_WAIT_MS)
          const sessionID = String(input.sessionID)
          const state = await research.sessionState(sessionID)
          const root = research.rootOf(sessionID)
          return { ...state, observationCoverage: root && research.inScope(root) ? observations.coverage(root) : null }
        }),
      board: (input: any) =>
        reported(async () => {
          const directory =
            input.directory ?? (input.sessionID ? await sessionDirectory(String(input.sessionID)) : ctx.location.directory)
          return research.board(directory, String(input.view), input.fields ?? {})
        }),
    })
    emitChanged = (root) => {
      void registration.events.emit("changed", { root }).catch(() => {})
      void refreshBudget(root).catch(error=>observations.append(root,{phase:'coverage_gap',source:'deadline_monitor',error_code:error.code ?? 'storage_unavailable'}))
    }
    void research.ready().then(()=>Promise.all(research.roots().map(root=>refreshBudget(root))))

    return () => {
      listening = false
      for(const timer of budgetTimers.values())clearTimeout(timer)
      storage.close()
      workspaceProbe.close()
    }
  },
})
