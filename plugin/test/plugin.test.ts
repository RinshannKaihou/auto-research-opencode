import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "../index"
import { parseResearchCommand } from "../src/commands"
import { materialRows, overviewText } from "../src/board/model"
import { StorageClient } from "../src/storage"
import { TOOL_NAMES } from "../src/tools"

type Hook = (request: any) => Promise<void> | void

/** OpenCode's event stream, fed by the test. */
class EventStream implements AsyncIterable<any> {
  private queue: any[] = []
  private waiter: ((value: IteratorResult<any>) => void) | null = null
  push(event: any) {
    if (this.waiter) {
      const waiter = this.waiter
      this.waiter = null
      waiter({ value: event, done: false })
    } else this.queue.push(event)
  }
  [Symbol.asyncIterator](): AsyncIterator<any> {
    return {
      next: () =>
        this.queue.length
          ? Promise.resolve({ value: this.queue.shift(), done: false })
          : new Promise((resolve) => (this.waiter = resolve)),
    }
  }
}

/** The slice of OpenCode's plugin context the server half uses. */
function fakeHost(registryPath: string, directory: string, python?: string) {
  const tools = new Map<string, any>()
  const hooks = new Map<string, Hook>()
  const synthetic: any[] = []
  const interrupted: string[] = []
  const created: string[] = []
  const models: any[] = []
  const permissions = new Map<string, any[]>()
  const events = new EventStream()
  // A session is busy from a resuming message until its turn ends; wait() resolves then.
  const transcripts = new Map<string, any[]>()
  const busy = new Set<string>()
  const waiters = new Map<string, (() => void)[]>()
  const endTurn = (sessionID: string, text: string, outcome: string) => {
    const content = text ? [{ type: "assistant", content: [{ type: "text", text }] }] : []
    transcripts.set(sessionID, [...(transcripts.get(sessionID) ?? []), ...content, { type: "idle", outcome }])
    busy.delete(sessionID)
    for (const resolve of waiters.get(sessionID) ?? []) resolve()
    waiters.delete(sessionID)
  }
  let handlers: any
  const ctx: any = {
    options: { registryPath, python },
    location: { directory },
    tool: {
      transform: async (callback: (editor: any) => void) => {
        callback({ add: (tool: any) => tools.set(tool.name, tool) })
        return { dispose: async () => {} }
      },
    },
    session: {
      hook: async (name: string, callback: Hook) => {
        hooks.set(name, callback)
        return { dispose: async () => {} }
      },
      get: async ({ sessionID }: { sessionID: string }) => ({ id: sessionID, location: { directory } }),
      create: async (input: any) => {
        const id = `ses_node_${created.length + 1}`
        created.push(id)
        models.push({ sessionID: id, model: input.model, via: "create" })
        if (input.permissions) permissions.set(id, input.permissions)
        return { id, title: input.title, location: input.location }
      },
      switchModel: async (input: any) => void models.push({ sessionID: input.sessionID, model: input.model, via: "switch" }),
      synthetic: async (input: any) => {
        synthetic.push(input)
        transcripts.set(input.sessionID, [...(transcripts.get(input.sessionID) ?? []), { type: "synthetic", text: input.text }])
        if (input.resume) busy.add(input.sessionID)
      },
      interrupt: async ({ sessionID }: { sessionID: string }) => {
        interrupted.push(sessionID)
        if (busy.has(sessionID)) endTurn(sessionID, "", "interrupted")
        return { interrupted: true }
      },
      wait: ({ sessionID }: { sessionID: string }) =>
        busy.has(sessionID)
          ? new Promise<void>((resolve) => waiters.set(sessionID, [...(waiters.get(sessionID) ?? []), resolve]))
          : Promise.resolve(),
      context: async ({ sessionID }: { sessionID: string }) => transcripts.get(sessionID) ?? [],
    },
    event: { subscribe: () => events },
    rpc: {
      register: async (_definition: unknown, registered: any) => {
        handlers = registered
        return { dispose: async () => {}, events: { emit: async () => {} } }
      },
    },
  }
  /** The model of `sessionID` ends its turn with a final text reply. */
  const reply = (sessionID: string, text: string, outcome = "succeeded") => endTurn(sessionID, text, outcome)
  return { ctx, tools, hooks, synthetic, interrupted, created, models, permissions, events, reply, rpc: () => handlers }
}

function modelRequest(sessionID: string) {
  return {
    sessionID,
    agent: "build",
    model: { id: "scripted", providerID: "mock" },
    system: [{ type: "text", text: "base system" }],
    messages: [{ role: "user", content: "hello" }],
    options: {},
    tools: Object.fromEntries(
      ["read", "shell", "subagent", "question", ...TOOL_NAMES, "research_other_plugin"].map((name) => [
        name,
        { description: name, input: {} },
      ]),
    ),
  }
}

async function until(check: () => boolean | Promise<boolean>, label: string, ms = 3000) {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    if (await check()) return
    await Bun.sleep(10)
  }
  throw new Error(`timed out waiting for ${label}`)
}

let dir: string
let project: string
let registry: string
const cleanups: (() => void)[] = []

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "ari-opencode-"))
  project = join(dir, "project")
  mkdirSync(project)
  registry = join(dir, "registry", "registry.sqlite3")
})

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  rmSync(dir, { recursive: true, force: true })
})

async function load(options: { python?: string; directory?: string } = {}) {
  const host = fakeHost(registry, options.directory ?? project, options.python)
  cleanups.push((await plugin.setup(host.ctx)) as () => void)
  let calls = 0
  const call = async (tool: string, sessionID: string, input: any, signal?: AbortSignal) => {
    calls += 1
    const context = { sessionID, messageID: `msg_${calls}`, id: `call_${calls}`, agent: "build", signal }
    const result = await host.tools.get(tool).execute(input, context)
    return JSON.parse(result.content)
  }
  const command = (action: string, sessionID: string | null, args: any = {}) =>
    host.rpc().command({ action, sessionID: sessionID ?? undefined, directory: project, args })
  const turn = {
    start: (sessionID: string) => host.events.push({ type: "session.execution.started", data: { sessionID } }),
    end: (sessionID: string, outcome = "succeeded", extra: any = {}) =>
      host.events.push({ type: `session.execution.${outcome}`, data: { sessionID, ...extra } }),
  }
  const summary = () => host.rpc().board({ view: "summary", directory: project })
  const page = (collection: string) => host.rpc().board({ view: "page", directory: project, fields: { collection } })
  const said = (sessionID: string) => host.synthetic.filter((item) => item.sessionID === sessionID)
  return { ...host, call, command, turn, summary, page, said }
}

describe("sessions outside a project", () => {
  test("see an unchanged request apart from this plugin's tools", async () => {
    const host = await load()
    const request = modelRequest("ses_plain")
    const expected = structuredClone(request)
    for (const name of TOOL_NAMES) delete (expected.tools as any)[name]
    await host.hooks.get("context")!(request)
    expect(request).toEqual(expected)
  })

  test("never start Python when no registry exists", async () => {
    const host = await load({ python: "/nonexistent/python3" })
    const request = modelRequest("ses_plain")
    await host.hooks.get("context")!(request)
    expect(Object.keys(request.tools)).toEqual(["read", "shell", "subagent", "question", "research_other_plugin"])
  })
})

describe("autonomous research", () => {
  test("conclusion rejects missing contracts and bad final refs without ending work", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    const pub = await host.call("research_publish", "ses_main", {
      status: "complete", summary: "final", items: [{ item_id: "report", kind: "text", content: { text: "unknown" } }],
    })
    const base = { summary: "done", final_ref: `pub/${pub.publication_id}#report` }
    await expect(host.call("research_conclude", "ses_main", base)).rejects.toThrow("outcome is required")
    const contract = { ...base, outcome: "unresolved", gaps: ["No decisive evidence"],
      review: { status: "unreviewed", refs: [], limitations: ["No review yet"] } }
    await expect(host.call("research_conclude", "ses_main", { ...contract, final_ref: `pub/${pub.publication_id}#missing` })).rejects.toThrow("item_missing")
    expect((await host.summary()).run.state).toBe("running")
    expect((await host.summary()).conclusion).toBeNull()
    const partial = await host.call("research_publish", "ses_main", {
      status: "partial", summary: "draft", items: [{ item_id: "report", kind: "text", content: { text: "draft" } }],
    })
    await expect(host.call("research_conclude", "ses_main", { ...contract, final_ref: `pub/${partial.publication_id}#report` })).rejects.toThrow("must be complete")
    await host.call("research_conclude", "ses_main", contract)
    const summary = await host.summary()
    expect(summary.run.state).toBe("complete")
    expect(summary.final_publication.publication_id).toBe(pub.publication_id)
    expect(summary.latest_publication.publication_id).toBe(partial.publication_id)
    expect(overviewText(summary, null)).toContain("结项结果：未解决")
    expect(overviewText(summary, null)).toContain("不表示结论通过")
  })

  test("notification failure preserves the committed conclusion across restart", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    const pub = await host.call("research_publish", "ses_main", {
      status: "complete", summary: "final", items: [{ item_id: "report", kind: "text", content: { text: "unknown" } }],
    })
    host.ctx.session.synthetic = async () => { throw new Error("notification unavailable") }
    const result = await host.call("research_conclude", "ses_main", {
      summary: "ended", final_ref: `pub/${pub.publication_id}#report`, outcome: "partial", gaps: ["Remaining work"],
      review: { status: "unreviewed", refs: [], limitations: ["Not reviewed"] },
    })
    expect(result.notification_warning).toContain("结项已保存")
    const restarted = await load()
    expect((await restarted.summary()).conclusion.conclusion_id).toBe(result.conclusion_id)
    expect((await restarted.summary()).run.state).toBe("complete")
    expect(restarted.said("ses_main").some((item) => item.resume)).toBe(false)
  })

  test("runs goal → nodes → node sessions → synthesis → conclusion without human messages", async () => {
    const host = await load()
    const started = await host.command("auto", "ses_main", { goal: "Find X" })
    expect(started.message).toContain("自主研究已开始")
    expect(host.said("ses_main").at(-1)).toMatchObject({ resume: true })
    expect(host.said("ses_main").at(-1).text).toContain("开始自主研究")

    // Coordinator turn: plan two nodes, dispatch the first, wait for it.
    host.turn.start("ses_main")
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "start", plan: "p1", root_reason: "first" })
    await host.call("research_propose", "ses_main", { question: "Q2?", why_now: "next", plan: "p2", root_reason: "second" })
    const task1 = await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(host.created).toEqual(["ses_node_1"])
    const kickoff = host.said("ses_node_1").at(-1)
    expect(kickoff).toMatchObject({ resume: true })
    expect(kickoff.text).toContain("你是节点 X-001 的执行会话")
    await host.call("research_wait", "ses_main", { task_ids: [task1.task_id] })
    const mainBefore = host.said("ses_main").length
    host.turn.end("ses_main")
    await Bun.sleep(100)
    expect(host.said("ses_main").length).toBe(mainBefore)

    // Node turn: publish a file from its workspace, record a claim, finish.
    const workspace = /工作目录：(.+?)。/.exec(kickoff.text)![1]!
    mkdirSync(join(workspace, "output"), { recursive: true })
    writeFileSync(join(workspace, "output", "report.md"), "# X-001\nresult\n")
    host.turn.start("ses_node_1")
    const pub = await host.call("research_publish", "ses_node_1", {
      status: "complete", summary: "node 1 result", items: [{ item_id: "report", kind: "report", source_path: "output/report.md" }],
    })
    await host.call("research_memory", "ses_node_1", {
      action: "record", kind: "observation", statement: "X holds under A", evidence_refs: [`pub/${pub.publication_id}#report`],
    })
    const checkpoint = await host.call("research_memory", "ses_node_1", { action: "checkpoint", state: { next: "finish" } })
    expect(checkpoint.node_id).toBe("X-001")
    await host.call("research_finish", "ses_node_1", { state: "finished", summary: "X holds under A" })
    host.turn.end("ses_node_1")
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("节点 X-001 已完成")), "coordinator wake-up")
    const wake = host.said("ses_main").at(-1)
    expect(wake.text).toContain(`pub/${pub.publication_id}`)

    // Coordinator dispatches the second node; node 2 finishes with a content-only publication.
    host.turn.start("ses_main")
    const task2 = await host.call("research_dispatch", "ses_main", { node_id: "X-002" })
    await host.call("research_wait", "ses_main", { task_ids: [task2.task_id] })
    host.turn.end("ses_main")
    await until(() => host.created.length === 2, "second node session")
    host.turn.start("ses_node_2")
    await host.call("research_publish", "ses_node_2", {
      status: "complete", summary: "node 2 result", items: [{ item_id: "note", kind: "text", content: { text: "Q2 answered" } }],
    })
    await host.call("research_finish", "ses_node_2", { state: "finished", summary: "Q2 answered" })
    host.turn.end("ses_node_2")
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("节点 X-002 已完成")), "second wake-up")

    // Synthesis and conclusion stop the loop.
    host.turn.start("ses_main")
    const final = await host.call("research_publish", "ses_main", {
      status: "complete", summary: "final report", items: [{ item_id: "final", kind: "text", content: { text: "X holds under A; Q2 answered" } }],
    })
    await host.call("research_conclude", "ses_main", { summary: "done", final_ref: `pub/${final.publication_id}#final`, outcome: "answered", gaps: [], review: { status: "unreviewed", refs: [], limitations: ["Scripted test, no scientific review"] } })
    const afterConclude = host.said("ses_main").length
    host.turn.end("ses_main")
    await Bun.sleep(100)
    expect(host.said("ses_main").length).toBe(afterConclude)

    const state = await host.summary()
    expect(state.run.state).toBe("complete")
    expect(state.counts.publications).toBe(3)
    const tasks = await host.page("tasks")
    expect(tasks.items.map((task: any) => task.state)).toEqual(["finished", "finished"])
  })

  test("a coordinator that forgets to wait is put into waiting, not paused as idle", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    host.turn.start("ses_main")
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(host.said("ses_main").some((item) => item.text.includes("已启动节点"))).toBe(false)
    host.turn.end("ses_main")
    await until(() => host.said("ses_main").filter((item) => item.text.includes("继续推进研究")).length === 1, "one nudge")
    host.turn.start("ses_main")
    host.turn.end("ses_main")
    await until(async () => (await host.rpc().session({ sessionID: "ses_main" })).pauseReason === "wait", "implicit wait")
    host.turn.start("ses_node_1")
    await host.call("research_publish", "ses_node_1", {
      status: "partial", summary: "r", items: [{ item_id: "r", kind: "text", content: { text: "r" } }],
    })
    await host.call("research_finish", "ses_node_1", { state: "finished", summary: "done" })
    host.turn.end("ses_node_1")
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("节点 X-001 已完成")), "wake-up")
    expect(host.said("ses_main").some((item) => item.text.includes("没有写入"))).toBe(false)
  })

  test("autonomous turns use the model selected in the TUI, for the coordinator and every node", async () => {
    const host = await load()
    const model = { id: "deepseek-v4.1-flash", providerID: "opencode-go" }
    await host.command("auto", "ses_main", { goal: "Find X", model })
    expect(host.models).toContainEqual({ sessionID: "ses_main", model, via: "switch" })
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(host.models).toContainEqual({ sessionID: "ses_node_1", model, via: "create" })
  })

  test("roles see only their own tools and their own research context", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    const main = modelRequest("ses_main")
    await host.hooks.get("context")!(main)
    expect(Object.keys(main.tools)).toContain("research_dispatch")
    expect(Object.keys(main.tools)).not.toContain("research_finish")
    expect(main.system.at(-1)!.text).toContain("research_conclude")
    const node = modelRequest("ses_node_1")
    await host.hooks.get("context")!(node)
    expect(Object.keys(node.tools)).toContain("research_finish")
    expect(Object.keys(node.tools)).not.toContain("research_dispatch")
    await expect(host.call("research_dispatch", "ses_node_1", { node_id: "X-001" })).rejects.toThrow("不属于本会话的角色")
  })

  test("research sessions never get the built-in subagent and question tools", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    for (const sessionID of ["ses_main", "ses_node_1"]) {
      const request = modelRequest(sessionID)
      await host.hooks.get("context")!(request)
      expect(Object.keys(request.tools)).not.toContain("subagent")
      expect(Object.keys(request.tools)).not.toContain("question")
      expect(Object.keys(request.tools)).toContain("shell")
    }
    const plain = modelRequest("ses_plain")
    await host.hooks.get("context")!(plain)
    expect(Object.keys(plain.tools)).toContain("subagent")
    expect(Object.keys(plain.tools)).toContain("question")
  })

  test("a session that stops writing the ledger is paused after three turns", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    for (let index = 0; index < 3; index++) {
      host.turn.start("ses_main")
      host.turn.end("ses_main")
      await Bun.sleep(60)
    }
    await until(() => host.said("ses_main").some((item) => item.text.includes("连续 3 轮没有写入")), "no-progress pause")
    const continued = host.said("ses_main").filter((item) => item.resume && item.text.includes("继续推进研究"))
    expect(continued).toHaveLength(2)
    const state = await host.rpc().session({ sessionID: "ses_main" })
    expect(state.pauseReason).toBe("no_progress")
    await host.command("resume", "ses_main")
    expect((await host.rpc().session({ sessionID: "ses_main" })).pauseReason).toBeNull()
  })

  test("a failed turn is retried once, then the session pauses", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    host.turn.end("ses_main", "failed")
    await until(() => host.said("ses_main").some((item) => item.text.includes("重试一次")), "retry")
    host.turn.end("ses_main", "failed")
    await until(() => host.said("ses_main").some((item) => item.text.includes("连续出错")), "fault pause")
  })

  test("user interrupts pause the session; the engine's own interrupts do not", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    host.turn.start("ses_main")
    host.turn.end("ses_main", "interrupted", { reason: "user" })
    await until(() => host.said("ses_main").some((item) => item.text.includes("你中断了这一轮")), "native stop")
    await host.command("resume", "ses_main")
    host.turn.start("ses_main")
    await Bun.sleep(30)
    const stopped = await host.command("stop", "ses_main")
    expect(stopped.message).toContain("已停止")
    expect(host.interrupted).toEqual(["ses_main"])
    const before = host.said("ses_main").length
    host.turn.end("ses_main", "interrupted", { reason: "user" })
    await Bun.sleep(100)
    expect(host.said("ses_main").length).toBe(before)
    expect((await host.summary()).run.state).toBe("stopped")
  })

  test("pause stops continuation after the current turn; resume continues", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.command("pause", "ses_main")
    const before = host.said("ses_main").length
    host.turn.start("ses_main")
    host.turn.end("ses_main")
    await Bun.sleep(100)
    expect(host.said("ses_main").length).toBe(before + 0)
    await host.command("resume", "ses_main")
    expect(host.said("ses_main").at(-1)).toMatchObject({ resume: true })
  })

  test("a restart leaves a running project paused until the user resumes it", async () => {
    const first = await load()
    await first.command("auto", "ses_main", { goal: "Find X" })
    cleanups.pop()!()
    const second = await load()
    await until(async () => (await second.summary()).run.state === "paused", "cold pause")
    expect((await second.rpc().session({ sessionID: "ses_main" })).pauseReason).toBe("cold")
    await second.command("resume", "ses_main")
    expect((await second.summary()).run.state).toBe("running")
  })

  test("a user message in a research session is kept as guidance", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.hooks.get("prompt")!({ sessionID: "ses_main", prompt: { text: "先测小事务" } })
    const notes = await host.rpc().board({ view: "reference_get", directory: project, fields: { ref: "N-001" } })
    expect(JSON.stringify(notes)).toContain("用户指导：先测小事务")
  })

  test("an instance opened in a subdirectory never drives the project", async () => {
    const root = await load()
    await root.command("auto", "ses_main", { goal: "Find X" })
    const sub = join(project, "sub")
    mkdirSync(sub)
    const other = await load({ directory: sub })
    const request = modelRequest("ses_main")
    await other.hooks.get("context")!(request)
    expect(Object.keys(request.tools)).not.toContain("research_dispatch")
    const refused = await other.rpc().command({ action: "pause", directory: sub, args: {} })
    expect(refused.error).toContain("请在这个目录启动 OpenCode")
  })

  test("status works on the home screen, before any session exists", async () => {
    const host = await load()
    const empty = await host.rpc().command({ action: "status", directory: project })
    expect(empty.message).toStartWith("当前不在会话里")
    const needsGoal = await host.rpc().command({ action: "auto", directory: project, args: {} })
    expect(needsGoal.error).toBeDefined()
  })
})

describe("node specialists", () => {
  const TASK = {
    label: "复核结论",
    question: "结论成立吗？",
    purpose: "独立复核",
    deliverable: "判断与依据",
    completion_criteria: "给出判断",
    report_requirements: "列出证据",
  }

  /** A running project whose first node executor ses_node_1 is at work; returns its workspace. */
  async function nodeAtWork(host: Awaited<ReturnType<typeof load>>): Promise<string> {
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    return /工作目录：(.+?)。/.exec(host.said("ses_node_1").at(-1).text)![1]!
  }

  /** The n-th session created after the node, once it has received its task. */
  async function specialist(host: Awaited<ReturnType<typeof load>>, index: number): Promise<string> {
    await until(() => host.created.length > index && host.said(host.created[index]!).length > 0, `specialist ${index}`)
    return host.created[index]!
  }

  test("a node delegates a read-only specialist and gets its report back", async () => {
    const host = await load()
    const workspace = await nodeAtWork(host)
    host.turn.start("ses_node_1")
    const pending = host.call("research_delegate", "ses_node_1", TASK)
    const child = await specialist(host, 1)

    // Read-only twice: the session's own rules, and the filter on every request.
    const rules = host.permissions.get(child)!
    expect(rules[0]).toEqual({ action: "*", resource: "*", effect: "deny" })
    expect(rules.filter((rule) => rule.effect === "allow").map((rule) => rule.action)).toEqual([
      "read", "glob", "grep", "research_query", "research_read_input",
    ])
    expect(rules).toContainEqual({ action: "read", resource: "*.env", effect: "deny" })
    const kickoff = host.said(child).at(-1)
    expect(kickoff).toMatchObject({ resume: true })
    expect(kickoff.text).toContain("你是节点 X-001 的专家")
    expect(kickoff.text).toContain(`节点工作目录：${workspace}`)
    const request = modelRequest(child)
    await host.hooks.get("context")!(request)
    expect(Object.keys(request.tools).sort()).toEqual(["read", "research_query", "research_read_input"])
    expect(request.system.at(-1)!.text).toContain("read-only specialist")

    // It reads the records but cannot write them, and the loop leaves its turn alone.
    host.turn.start(child)
    await host.call("research_query", child, { collection: "nodes" })
    await expect(host.call("research_note", child, { body: "x" })).rejects.toThrow("不属于本会话的角色")
    host.reply(child, "结论成立：依据 output/report.md 第 2 行。")
    host.turn.end(child)
    const result = await pending
    expect(result).toMatchObject({ state: "completed", exit_verified: true, child_session_id: child })
    expect(result.result.preview).toContain("结论成立")
    expect(readFileSync(result.result.report_path, "utf8")).toContain("结论成立")
    await Bun.sleep(60)
    expect(host.said(child)).toHaveLength(1)
    expect((await host.page("specialists")).items.map((item: any) => item.state)).toEqual(["completed"])
    expect((await host.rpc().session({ sessionID: child })).role).toBe("specialist")
  })

  test("interrupting the delegating call cancels the specialist and frees its slot", async () => {
    const host = await load()
    await nodeAtWork(host)
    const controller = new AbortController()
    const pending = host.call("research_delegate", "ses_node_1", TASK, controller.signal)
    const child = await specialist(host, 1)
    controller.abort()
    const result = await pending
    expect(result.state).toBe("cancelled")
    expect(host.interrupted).toContain(child)
    const again = host.call("research_delegate", "ses_node_1", TASK)
    host.reply(await specialist(host, 2), "第二次复核的报告")
    expect((await again).state).toBe("completed")
  })

  test("a batch runs specialists side by side up to the node's limit; beyond it nothing starts", async () => {
    const host = await load()
    await nodeAtWork(host)
    await expect(host.call("research_delegate_batch", "ses_node_1", { tasks: [TASK, TASK, TASK] })).rejects.toThrow("1 到 2")
    expect(host.created).toHaveLength(1)
    const pending = host.call("research_delegate_batch", "ses_node_1", { tasks: [TASK, { ...TASK, label: "另一视角" }] })
    const [one, two] = [await specialist(host, 1), await specialist(host, 2)]
    await expect(host.call("research_delegate", "ses_node_1", TASK)).rejects.toThrow("fan-out limit")
    host.reply(two, "报告二")
    host.reply(one, "报告一")
    const result = await pending
    expect(result.tasks.map((task: any) => [task.state, task.result.preview])).toEqual([
      ["completed", "报告一"],
      ["completed", "报告二"],
    ])
  })

  test("a blind reviewer sees only its assigned inputs, without the project or OpenCode's prompt", async () => {
    const host = await load()
    const workspace = await nodeAtWork(host)
    mkdirSync(join(workspace, "output"), { recursive: true })
    writeFileSync(join(workspace, "output", "report.md"), "# 结果\nWAL 快 3 倍\n")
    const pub = await host.call("research_publish", "ses_node_1", {
      status: "complete", summary: "r", items: [{ item_id: "report", kind: "report", source_path: "output/report.md" }],
    })
    const input = `pub/${pub.publication_id}#report`
    const blind = { ...TASK, label: "盲评结果", context_mode: "blind" }
    await expect(host.call("research_delegate", "ses_node_1", { ...blind, inputs: [] })).rejects.toThrow("Blind review requires")
    await expect(host.call("research_delegate", "ses_node_1", { ...blind, inputs: [input], tool_scope: ["read"] })).rejects.toThrow("tool_scope")
    expect(host.created).toHaveLength(1)

    const pending = host.call("research_delegate", "ses_node_1", { ...blind, inputs: [input] })
    const child = await specialist(host, 1)
    const rules = host.permissions.get(child)!
    expect(rules.filter((rule) => rule.effect === "allow").map((rule) => rule.action)).toEqual(["research_read_input"])
    const kickoff = host.said(child).at(-1).text
    expect(kickoff).toContain("盲评专家")
    expect(kickoff).toContain("input-1")
    expect(kickoff).not.toContain(pub.publication_id)
    expect(kickoff).not.toContain(workspace)
    const request = modelRequest(child)
    await host.hooks.get("context")!(request)
    expect(Object.keys(request.tools)).toEqual(["research_read_input"])
    const system = request.system.map((part) => part.text).join("\n")
    expect(system).not.toContain("base system")
    expect(system).toContain("blind reviewer")
    expect(system).not.toContain("Find X")

    expect((await host.call("research_read_input", child, { input_id: "input-1" })).text).toContain("WAL 快 3 倍")
    await expect(host.call("research_query", child, { collection: "nodes" })).rejects.toThrow("Blind reviewers")
    host.reply(child, "盲评：数据支持结论。")
    expect((await pending).state).toBe("completed")
  })

  test("a restart stops and settles specialists that were still working", async () => {
    const first = await load()
    await nodeAtWork(first)
    void first.call("research_delegate", "ses_node_1", TASK).catch(() => {})
    const child = await specialist(first, 1)
    cleanups.pop()!()
    const second = await load()
    await until(async () => (await second.page("specialists")).items[0]?.state === "incomplete", "settled after restart")
    expect(second.interrupted).toContain(child)
    expect((await second.rpc().session({ sessionID: child })).role).toBe("specialist")
  })
})

describe("storage", () => {
  test("reports a missing Python instead of hanging", async () => {
    const storage = new StorageClient({ python: "/nonexistent/python3", registryPath: registry })
    await expect(storage.request("host_sessions", { host_id: "opencode" })).rejects.toThrow("Python 3.11+")
    storage.close()
  })
})

describe("command parsing", () => {
  test("maps /research input to actions", () => {
    expect(parseResearchCommand("")).toEqual({ kind: "rpc", action: "help", args: {} })
    expect(parseResearchCommand("auto  Find the cause of X ")).toEqual({
      kind: "rpc", action: "auto", args: { goal: "Find the cause of X" },
    })
    expect(parseResearchCommand("auto")).toEqual({ kind: "rpc", action: "auto", args: { goal: null } })
    expect(parseResearchCommand("pause")).toEqual({ kind: "rpc", action: "pause", args: {} })
    expect((parseResearchCommand("stop") as any).confirm).toContain("停止自主研究")
    expect(parseResearchCommand("board")).toEqual({ kind: "board" })
    expect(parseResearchCommand("work X-001")).toMatchObject({ kind: "error" })
  })
})

describe("board text", () => {
  test("historical completion reports a missing contract, not an inferred review", () => {
    const text = overviewText({ project: { goal: "old" }, counts: {}, run: { state: "complete" } }, null)
    expect(text).toContain("结项契约：未记录")
    expect(text).toContain("审阅情况未知")
  })
  test("labels the newest publication as latest, not final, and hides display items", () => {
    const text = overviewText(
      {
        project: { goal: "G" }, project_root: "/p", run: { state: "running" }, counts: {}, review_queue: {},
        final_publication: { publication_id: "P-003", status: "complete", summary: "s" },
      },
      null,
    )
    expect(text).toContain("最新阶段成果 P-003")
    const rows = materialRows({
      items: [{ publication_id: "P-001", status: "partial", summary: "s", items: [
        { item_id: "__research_display", ref: "pub/P-001#__research_display" },
        { item_id: "report", ref: "pub/P-001#report", source_path: "r.md", object_kind: "file" },
      ] }],
    })
    expect(rows.map((row) => row.ref)).toEqual(["pub/P-001#report"])
  })
})
