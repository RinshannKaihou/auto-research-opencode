/**
 * Server half of the research plugin, built around the autonomous loop:
 * execution events drive the engine, research tools write the ledger, and
 * each model request of a research session carries its role's research
 * context. Sessions outside a project see exactly what they would without
 * the plugin: the context hook only removes this plugin's tools.
 */
import { Plugin } from "@opencode/plugin"
import { existsSync } from "node:fs"
import { ResearchRpc } from "./rpc"
import { StorageClient } from "./src/storage"
import { Research, type CommandResult } from "./src/research"
import { Engine, type ModelRef, type SessionHost, type ConclusionInput } from "./src/engine"
import { BLIND_SYSTEM, Specialists, type SpecialistTask } from "./src/specialists"
import { TOOLS, keepsTool, type ToolDefinition } from "./src/tools"
import { parameters, validateArgs } from "./src/schema"
import { USAGE } from "./src/format"

const STARTUP_WAIT_MS = 1500

function within<T>(promise: Promise<T>, ms: number): Promise<T | void> {
  return Promise.race([promise, new Promise<void>((resolve) => setTimeout(resolve, ms))])
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
    const storage = new StorageClient({ python: options.python, registryPath: options.registryPath })
    let emitChanged: (root: string) => void = () => {}
    const research = new Research(
      storage,
      ctx.location.directory,
      () => existsSync(storage.registryPath),
      (root) => emitChanged(root),
    )
    const host: SessionHost = {
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
        await ctx.session.synthetic({ sessionID, text, description: label, resume })
      },
      interrupt: async (sessionID) => {
        await ctx.session.interrupt({ sessionID })
      },
      wait: async (sessionID) => {
        await ctx.session.wait({ sessionID })
      },
      messages: async (sessionID) => (await ctx.session.context({ sessionID })) as any[],
    }
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
            const args = validateArgs(input, tool.parameters)
            const value = await runTool(tool, call, args)
            if (tool.writes) engine.wroteLedger(call.sessionID)
            return { content: JSON.stringify(value, null, 2) }
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
        const text = await research.memory(request.sessionID)
        if (text) request.system.push({ type: "text", text })
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
        await research.call("note", input.sessionID, { body: `用户指导：${text.slice(0, 2000)}`, kind: "correction" })
      } catch {
        // Without an open work segment there is nowhere to file the note; the message still goes through.
      }
    })

    let listening = true
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe()) {
          if (!listening) break
          const { type, data } = event as { type: string; data?: { sessionID?: string } }
          const sessionID = data?.sessionID
          if (!sessionID || !research.isAssociated(sessionID)) continue
          if (type === "session.execution.started") engine.started(sessionID)
          else if (type === "session.execution.succeeded") void engine.ended(sessionID, "succeeded")
          else if (type === "session.execution.failed") void engine.ended(sessionID, "failed")
          else if (type === "session.execution.interrupted") void engine.ended(sessionID, "interrupted")
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
          return research.sessionState(String(input.sessionID))
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
    }

    return () => {
      listening = false
      storage.close()
    }
  },
})
