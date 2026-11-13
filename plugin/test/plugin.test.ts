import { afterEach, beforeEach, describe, expect, test, spyOn } from "bun:test"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { spawnSync } from "node:child_process"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "../index"
import { parseResearchCommand } from "../src/commands"
import { controlInstructions } from "../src/coordinator-boundary"
import { decodeChunk, kindChips, knowledgeRows, materialRows, overviewModel, runtimeNotes, sessionRows } from "../src/board/model"
import { associatedStatus } from "../src/format"
import { StorageClient } from "../src/storage"
import { Research } from "../src/research"
import { keepsTool } from "../src/tools"
import { SPECIALIST_STALL_MS, SPECIALIST_TIMEOUT_MS, STOP_GRACE_MS, SUMMARY_GRACE_MS, Specialists, specialistGraceForTest, specialistProgressEvent } from "../src/specialists"
import { TOOL_NAMES } from "../src/tools"
import { forgetControllerForTest } from "../src/controller-lifecycle"

type Hook = (request: any) => Promise<void> | void

/** OpenCode's event stream, fed by the test. */
class EventStream implements AsyncIterable<any> {
  private queue: any[] = []
  private waiter: ((value: IteratorResult<any>) => void) | null = null
  private sequence = new Map<string, number>()
  end() {
    if (this.waiter) {
      const waiter = this.waiter
      this.waiter = null
      waiter({ value: undefined, done: true })
    } else this.queue.push(null)
  }
  push(event: any) {
    const session = event.data?.sessionID ?? ''
    const seq = (this.sequence.get(session) ?? 0) + 1
    this.sequence.set(session, seq)
    event.durable ??= { aggregateID: session, seq, version: 1 }
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
          ? (() => { const value = this.queue.shift(); return Promise.resolve({ value, done: value === null }) })()
          : new Promise((resolve) => (this.waiter = resolve)),
    }
  }
  subscribe(signal?: AbortSignal): AsyncIterable<any> {
    const iterator = this[Symbol.asyncIterator]()
    let resolveNext: ((value: IteratorResult<any>) => void) | undefined
    signal?.addEventListener('abort', () => resolveNext?.({ value: undefined, done: true }), { once: true })
    return { [Symbol.asyncIterator]: () => ({ next: () => signal?.aborted
      ? Promise.resolve({ value: undefined, done: true })
      : new Promise(resolve => { resolveNext = resolve; void iterator.next().then(resolve) }) }) }
  }
}

/** The slice of OpenCode's plugin context the server half uses. */
const nativeSessions = new Map<string, {
  locations: Map<string, string>; transcripts: Map<string, any[]>; busy: Set<string>; waiters: Map<string, (() => void)[]>
}>()
function fakeHost(
  registryPath: string,
  directory: string,
  python?: string,
  contextPlacement?: string,
  exposeSessionBusy = true,
  busySignal: "status" | "execution" = "status",
) {
  const tools = new Map<string, any>()
  const hooks = new Map<string, Hook>()
  const synthetic: any[] = []
  const interrupted: string[] = []
  const created: string[] = []
  const models: any[] = []
  const permissions = new Map<string, any[]>()
  const events = new EventStream()
  const prompts: any[] = []
  const running = new Set<string>()
  const queuedPrompts = new Map<string, { messageID: string; text: string; files?: any[] }[]>()
  let subscriptions = 0
  // A session is busy from a resuming message until its turn ends; wait() resolves then.
  // Reloading the plugin does not reset the native host's sessions or history.
  let native = nativeSessions.get(registryPath)
  if (!native) nativeSessions.set(registryPath, native = { locations: new Map(), transcripts: new Map(), busy: new Set(), waiters: new Map() })
  const { locations, transcripts, busy, waiters } = native
  const endTurn = (sessionID: string, text: string, outcome: string) => {
    const content = text ? [{ type: "assistant", content: [{ type: "text", text }] }] : []
    transcripts.set(sessionID, [...(transcripts.get(sessionID) ?? []), ...content, { type: "idle", outcome }])
    busy.delete(sessionID)
    running.delete(sessionID)
    // Production OpenCode 2.2.5 has no session.busy. The fallback reads events.
    // Captured traces have session.execution.started and its execution end, not session.status.
    if (!exposeSessionBusy) signalIdle(sessionID, outcome)
    for (const resolve of waiters.get(sessionID) ?? []) resolve()
    waiters.delete(sessionID)
  }
  let handlers: any
  const ctx: any = {
    options: { registryPath, python, ...(contextPlacement ? { contextPlacement } : {}) },
    location: { directory },
    tool: {
      list: async () => [...tools.values()],
      hook: async (name:string,callback:Hook) => { hooks.set(`tool.${name}`,callback); return {dispose:async()=>{}} },
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
      get: async ({ sessionID }: { sessionID: string }) => ({ id: sessionID, location: { directory: locations.get(sessionID) ?? directory } }),
      create: async (input: any) => {
        const id = input.id ?? `ses_node_${created.length + 1}`
        created.push(id)
        locations.set(id, input.location.directory)
        models.push({ sessionID: id, model: input.model, via: "create" })
        if (input.permissions) permissions.set(id, input.permissions)
        return { id, title: input.title, location: input.location }
      },
      switchModel: async (input: any) => void models.push({ sessionID: input.sessionID, model: input.model, via: "switch" }),
      synthetic: async (input: any) => {
        synthetic.push(input)
        transcripts.set(input.sessionID, [...(transcripts.get(input.sessionID) ?? []), { type: "synthetic", id: input.id, text: input.text }])
        if (input.resume) {
          busy.add(input.sessionID)
          events.push({ type: 'session.inbox.delivered', data: { sessionID: input.sessionID, inboxID: input.id } })
          if (!exposeSessionBusy) signalBusy(input.sessionID)
        }
        return { id: input.id }
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
      busy: ({ sessionID }: { sessionID: string }) => busy.has(sessionID),
      context: async ({ sessionID }: { sessionID: string }) => transcripts.get(sessionID) ?? [],
    },
    event: { subscribe: (options?: { signal?: AbortSignal }) => { subscriptions++; return events.subscribe(options?.signal) } },
    rpc: {
      register: async (_definition: unknown, registered: any) => {
        handlers = registered
        return { dispose: async () => {}, events: { emit: async () => {} } }
      },
    },
  }
  if (!exposeSessionBusy) delete ctx.session.busy
  /** Busy and idle as the no-`session.busy` fallback sees them. `execution` matches captured traces. */
  function signalBusy(sessionID: string) {
    if (busySignal === "execution") {
      events.push({ type: "session.execution.started", data: { sessionID } })
      return
    }
    events.push({ type: "session.status", data: { sessionID, status: { type: "busy" } } })
  }
  function signalIdle(sessionID: string, outcome: string) {
    if (busySignal === "execution") {
      const ended = outcome === "succeeded" || outcome === "failed" || outcome === "interrupted"
      events.push(ended
        ? { type: `session.execution.${outcome}`, data: { sessionID } }
        : { type: "session.idle", data: { sessionID } })
      return
    }
    events.push({ type: "session.status", data: { sessionID, status: { type: "idle" } } })
  }
  /** The model of `sessionID` ends its turn with a final text reply. */
  const reply = (sessionID: string, text: string, outcome = "succeeded") => endTurn(sessionID, text, outcome)
  /**
   * A real user prompt: the host runs the prompt hook, then honours the final
   * delivery value. A queued prompt is held until the host's next step.
   */
  const prompt = async (sessionID: string, text: string, messageID: string, files?: any[], hold = false) => {
    const input: any = { sessionID, messageID, prompt: { text, ...(files ? { files } : {}) }, delivery: 'steer' }
    await hooks.get('prompt')?.(input)
    prompts.push({ sessionID, messageID, text, delivery: input.delivery, files: files ?? [] })
    if (hold || running.has(sessionID) && input.delivery === 'queue') {
      // A prompt sent while the model is generating (or queued behind a
      // terminal action) is held until the host's next step.
      queuedPrompts.set(sessionID, [...(queuedPrompts.get(sessionID) ?? []), { messageID, text, files }])
      return input.delivery as string
    }
    // Steering, or an idle session: the host admits the input now.
    transcripts.set(sessionID, [...(transcripts.get(sessionID) ?? []),
      { type: 'user', id: messageID, text, ...(files ? { files } : {}) }])
    events.push({ type: 'session.inbox.delivered', data: { sessionID, inboxID: messageID } })
    return input.delivery as string
  }
  /** The host delivers one held prompt at its next step boundary. */
  const deliverQueued = (sessionID: string) => {
    const next = queuedPrompts.get(sessionID)?.shift()
    if (!next) return undefined
    transcripts.set(sessionID, [...(transcripts.get(sessionID) ?? []),
      { type: 'user', id: next.messageID, text: next.text, ...(next.files ? { files: next.files } : {}) }])
    events.push({ type: 'session.inbox.delivered', data: { sessionID, inboxID: next.messageID } })
    return next
  }
  const appendTranscript = (sessionID: string, entry: any) =>
    transcripts.set(sessionID, [...(transcripts.get(sessionID) ?? []), entry])
  return { ctx, tools, hooks, synthetic, interrupted, created, models, permissions, events, reply, locations, prompt, deliverQueued,
    appendTranscript, prompts, queuedPrompts, running, subscriptions: () => subscriptions, rpc: () => handlers }
}

function contextText(request: ReturnType<typeof modelRequest>): string {
  return JSON.stringify([request.system, request.messages])
}

function modelRequest(sessionID: string) {
  return {
    sessionID,
    agent: "build",
    model: { id: "scripted", providerID: "mock" },
    system: [{ type: "text", text: "base system" }],
    messages: [{ role: "user", content: [{ type: "text", text: "hello" }] }],
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

function simulateDeadHost(): void {
  forgetControllerForTest(project, registry)
  const script = `import json,sqlite3,sys\ndb=sqlite3.connect(sys.argv[1])\nrow=db.execute("SELECT details FROM workflow_intents WHERE intent_id='controller-owner:v1'").fetchone()\nassert row\ndetails=json.loads(row[0]); details['pid']=2147483647\ndb.execute("UPDATE workflow_intents SET details=? WHERE intent_id='controller-owner:v1'", (json.dumps(details),)); db.commit()`
  const result = spawnSync('python3', ['-c', script, join(project, '.research', 'state.sqlite3')], { encoding: 'utf8' })
  if (result.status !== 0) throw new Error(result.stderr)
}

beforeEach(() => {
  nativeSessions.clear()
  // Process-local controller state belongs to one test; only a warm rebind
  // inside a test may inherit it.
  const controllers = (globalThis as any)[Symbol.for("auto-research.controller.v1")] as Map<string, any> | undefined
  controllers?.clear()
  dir = mkdtempSync(join(tmpdir(), "ari-opencode-"))
  project = join(dir, "project")
  mkdirSync(project)
  registry = join(dir, "registry", "registry.sqlite3")
})

afterEach(() => {
  specialistGraceForTest.stopMs = undefined
  specialistGraceForTest.summaryMs = undefined
  for (const cleanup of cleanups.splice(0)) cleanup()
  rmSync(dir, { recursive: true, force: true })
})

async function load(options: { python?: string; directory?: string; placement?: string; hostSetup?: (host: ReturnType<typeof fakeHost>) => void; sessionBusy?: boolean; busySignal?: "status" | "execution" } = {}) {
  const host = fakeHost(registry, options.directory ?? project, options.python, options.placement, options.sessionBusy !== false, options.busySignal ?? "status")
  options.hostSetup?.(host)
  cleanups.push((await plugin.setup(host.ctx)) as () => void)
  let calls = 0
  const call = async (tool: string, sessionID: string, input: any, signal?: AbortSignal) => {
    calls += 1
    const messageID = `msg_${calls}`
    // A tool call belongs to the assistant step that generated it. The native
    // transcript carries that step, which is what the plugin associates with.
    host.appendTranscript(sessionID, { type: 'assistant', id: messageID })
    const context = { sessionID, messageID, id: `call_${calls}`, agent: "build", signal }
    const result = await host.tools.get(tool).execute(input, context)
    return JSON.parse(result.content)
  }
  const command = (action: string, sessionID: string | null, args: any = {}) =>
    host.rpc().command({ action, sessionID: sessionID ?? undefined, directory: project, args })
  const turn = {
    start: (sessionID: string) => { host.running.add(sessionID); host.events.push({ type: "session.execution.started", data: { sessionID } }) },
    end: (sessionID: string, outcome = "succeeded", extra: any = {}) => {
      host.running.delete(sessionID)
      host.events.push({ type: `session.execution.${outcome}`, data: { sessionID, ...extra } })
    },
  }
  const summary = () => host.rpc().board({ view: "summary", directory: project })
  const page = (collection: string) => host.rpc().board({ view: "page", directory: project, fields: { collection } })
  const said = (sessionID: string) => host.synthetic.filter((item) => item.sessionID === sessionID)
  return { ...host, call, command, turn, summary, page, said }
}

test('an attachment that saw no registry discovers a later research association', async () => {
  const firstStorage = new StorageClient({ registryPath: registry })
  const secondStorage = new StorageClient({ registryPath: registry })
  try {
    const observer = new Research(firstStorage, project, () => existsSync(registry))
    const creator = new Research(secondStorage, project, () => existsSync(registry))
    await observer.confirmMembership('ses_main')
    expect(observer.isAssociated('ses_main')).toBe(false)
    await creator.init('ses_main', project, 'membership fixture')
    await observer.confirmMembership('ses_main')
    expect(observer.roleOf('ses_main')).toBe('main')
    expect(keepsTool('shell', true, observer.roleOf('ses_main'), [])).toBe(false)
    await observer.confirmMembership('ordinary')
    expect(observer.isAssociated('ordinary')).toBe(false)
  } finally {
    firstStorage.close()
    secondStorage.close()
  }
})

test('negative membership cache skips repeated misses and invalidates on registry writes', async () => {
  const first = new StorageClient({ registryPath: registry })
  const second = new StorageClient({ registryPath: registry })
  try {
    const creator = new Research(second, project, () => existsSync(registry))
    await creator.init('ses_main', project, 'cache fixture')
    const observer = new Research(first, project, () => existsSync(registry))
    await observer.ready()
    const original = first.request.bind(first)
    let lookups = 0
    first.request = ((method, fields, operation) => {
      if (method === 'session_membership') lookups++
      return original(method, fields, operation)
    }) as typeof first.request
    await observer.confirmMembership('ses_later')
    await observer.confirmMembership('ses_later')
    expect(lookups).toBe(1)
    await second.request('open', { host_id: 'opencode', session_id: 'ses_later', root: project, cwd: project, session_role: 'node_core' })
    await observer.confirmMembership('ses_later')
    expect(observer.roleOf('ses_later')).toBe('node_core')
    expect(lookups).toBe(2)
  } finally { first.close(); second.close() }
})

test('ready event health is valid JSON in status and session RPC responses', async () => {
  const host = await load()
  await host.command('init','ses_main',{goal:'status fixture'})
  const status = await host.command('status','ses_main')
  expect(status.state.event_recovery.state).toBe('ready')
  expect(Object.hasOwn(status.state.event_recovery,'message')).toBe(false)
  const session = await host.rpc().session({sessionID:'ordinary'})
  expect(session.eventRecovery).toBeNull()
})

test('a lost execution-start response retries the same token and retains inbox authorization', async () => {
  const original = StorageClient.prototype.request
  const tokens: string[] = []
  let lost = false
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(method, fields, operation) {
    const result = await original.call(this, method, fields, operation)
    if (method === 'runtime_execution_start') {
      tokens.push(String(fields?.execution_token))
      if (!lost) { lost = true; throw new Error('lost start response') }
    }
    return result
  })
  try {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'event retry' })
    await until(() => tokens.length >= 2, 'same event retried')
    expect(new Set(tokens).size).toBe(1)
    host.turn.end('ses_main')
    await until(() => host.said('ses_main').filter(item => item.resume).length === 2, 'authorized continuation')
  } finally { spy.mockRestore() }
})

test('a lost execution-end response retries persistence without losing or duplicating continuation', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'terminal retry' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  const original = StorageClient.prototype.request
  let lost = false
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(method, fields, operation) {
    const result = await original.call(this, method, fields, operation)
    if (method === 'runtime_execution_end' && !lost) { lost = true; throw new Error('lost terminal response') }
    return result
  })
  try {
    host.events.push({ id: 'end-with-lost-response', type: 'session.execution.succeeded', data: { sessionID: 'ses_main' } })
    await until(() => host.said('ses_main').filter(item => item.resume).length === 2, 'one continuation')
    host.events.push({ id: 'end-with-lost-response', type: 'session.execution.succeeded', data: { sessionID: 'ses_main' } })
    await Bun.sleep(100)
    expect(host.said('ses_main').filter(item => item.resume)).toHaveLength(2)
    await host.call('research_note', 'ses_main', { body: 'second turn is still writable' })
    host.turn.end('ses_main')
    await until(() => host.said('ses_main').filter(item => item.resume).length === 3, 'duplicate did not poison the next turn')
  } finally { spy.mockRestore() }
})

test('unrelated native telemetry cannot poison research event control', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'unrelated telemetry' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  const original = StorageClient.prototype.request
  let unrelatedLookups = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'session_membership' && fields?.session_id === 'ordinary') { unrelatedLookups++; return Promise.reject(new Error('unrelated lookup')) }
    return original.call(this, method, fields, operation)
  })
  try {
    for (let i = 0; i < 1000; i++) host.events.push({ type: 'session.step.started', data: { sessionID: 'ordinary' } })
    host.turn.end('ses_main')
    await until(() => host.said('ses_main').filter(item => item.resume).length === 2, 'research remains active')
    expect(unrelatedLookups).toBe(0)
  } finally { spy.mockRestore() }
})

test('failed event persistence still permits pause and stop', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'event failure control' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  const original = StorageClient.prototype.request
  let failures = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'runtime_execution_end') { failures++; return Promise.reject(new Error('event store down')) }
    return original.call(this, method, fields, operation)
  })
  try {
    host.turn.end('ses_main')
    await until(() => failures === 3, 'bounded retries exhausted')
    expect((await host.command('pause', 'ses_main')).error).toBeUndefined()
    expect((await host.summary()).run.state).toBe('paused')
    // The same reducing path must also reach the host when an event is held.
    host.turn.start('ses_main')
    expect((await host.command('stop', 'ses_main')).error).toBeUndefined()
    expect(host.interrupted).toContain('ses_main')
    expect((await host.summary()).run.state).toBe('stopped')
  } finally { spy.mockRestore() }
})

test('a held event still refuses research writes before execution', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'held write refusal' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  const original = StorageClient.prototype.request
  let failures = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'runtime_execution_end') { failures++; return Promise.reject(new Error('event store down')) }
    return original.call(this, method, fields, operation)
  })
  try {
    host.turn.end('ses_main')
    await until(() => failures === 3, 'held event after bounded retries')
    // The pre-execution write check is independent of the new read-only fallback.
    await expect(host.call('research_note', 'ses_main', { body: 'should be refused' }))
      .rejects.toThrow('研究事件需核对')
  } finally { spy.mockRestore() }
})

test('explicit resume replays a held terminal after storage recovers and continues once', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'retry a held event' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  const original = StorageClient.prototype.request
  let failures = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'runtime_execution_end') { failures++; return Promise.reject(new Error('temporary event store failure')) }
    return original.call(this, method, fields, operation)
  })
  try {
    host.turn.end('ses_main')
    await until(() => failures === 3, 'terminal held after three attempts')
    expect((await host.command('pause', 'ses_main')).error).toBeUndefined()
  } finally { spy.mockRestore() }
  expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
  await host.call('research_note', 'ses_main', { body: 'control recovered' })
  expect(host.said('ses_main').filter(item => item.resume)).toHaveLength(2)
})

test('a disconnected event subscription can be re-established by explicit resume', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'event stream reconnect' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  host.events.end()
  await Bun.sleep(30)
  expect((await host.command('pause', 'ses_main')).error).toBeUndefined()
  expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
  expect(host.subscriptions()).toBe(2)
  host.turn.end('ses_main')
  await until(() => host.said('ses_main').filter(item => item.resume).length === 2, 'new subscription processes terminal')
})

test('resume reconciles a coordinator terminal missed during an event-stream disconnect', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'missed coordinator terminal' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  host.events.end()
  await Bun.sleep(30)
  host.reply('ses_main', 'coordinator turn ended while disconnected')
  expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
  await until(() => host.said('ses_main').filter(item => item.resume).length === 2, 'recovered coordinator continues once')
  await Bun.sleep(50)
  expect(host.said('ses_main').filter(item => item.resume)).toHaveLength(2)
})

test('a failure before session creation leaves one queued task that can be resumed', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'pre-host failure' })
  await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  const original = StorageClient.prototype.request
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'task_context') return Promise.reject(new Error('context temporarily unavailable'))
    return original.call(this, method, fields, operation)
  })
  try {
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    expect(host.created).toHaveLength(0)
    expect((await host.page('tasks')).items[0].state).toBe('queued')
  } finally { spy.mockRestore() }
  expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
  expect(host.created).toHaveLength(1)
  expect((await host.page('tasks')).items).toHaveLength(1)
  expect((await host.page('tasks')).items[0].state).toBe('running')
})

test('workspace aliases of the same project share a single live controller', async () => {
  const first = await load({ hostSetup: host => { host.ctx.location.workspaceID = 'workspace-a' } })
  await first.command('auto', 'ses_main', { goal: 'workspace aliases' })
  const second = await load({ hostSetup: host => { host.ctx.location.workspaceID = 'workspace-b' } })
  await until(async () => (await second.command('status', 'ses_main')).state?.workflow?.controller?.phase === 'ready', 'one owner for both workspaces')
  expect((await second.summary()).run.state).toBe('running')
  expect((await first.command('pause', 'ses_main')).error).toBeUndefined()
  expect((await second.summary()).run.state).toBe('paused')
})

for (const check of [1, 2]) test(`continuation preflight check ${check} failure preserves the task and refunds only the unsent turn`, async () => {
  const first = await load()
  await first.command('auto', 'ses_main', { goal: 'unsent continuation' })
  await first.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  await first.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
  const child = first.created[0]!
  await first.call('research_note', child, { body: 'work started' })
  const original = StorageClient.prototype.request
  let checks = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'controller_check' && fields?.session_id === child && ++checks === check) return Promise.reject(new Error('transient preflight storage failure'))
    return original.call(this, method, fields, operation)
  })
  try {
    first.reply(child, 'current turn ended')
    first.turn.end(child)
    await until(async () => (await first.rpc().session({ sessionID: child })).pauseReason === 'fault', 'safe continuation pause')
    expect((await first.page('tasks')).items[0].state).toBe('running')
    expect(first.said(child).filter(item => item.resume)).toHaveLength(1)
    expect((await first.command('budget', child)).message).toContain('"turns": 1')
  } finally { spy.mockRestore() }
  cleanups.pop()!()
  const second = await load()
  await until(async () => (await second.command('status', 'ses_main')).state?.workflow?.controller?.phase === 'ready', 'warm controller ready')
  expect((await second.page('tasks')).items[0].state).toBe('running')
  expect((await second.command('resume', 'ses_main')).error).toBeUndefined()
  expect(second.said(child).filter(item => item.resume)).toHaveLength(1)
})

test('an accepted continuation with a lost host response remains unverified and charged', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'uncertain continuation' })
  await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
  const child = host.created[0]!
  await host.call('research_note', child, { body: 'work started' })
  const original = host.ctx.session.synthetic
  host.ctx.session.synthetic = async (input: any) => {
    const result = await original(input)
    if (input.sessionID === child && input.resume) throw new Error('host response lost after acceptance')
    return result
  }
  host.reply(child, 'first turn ended')
  host.turn.end(child)
  await until(async () => (await host.page('tasks')).items[0].state === 'unverified', 'uncertain continuation retained')
  expect((await host.command('budget', child)).message).toContain('"turns": 2')
})

test('a terminal with unresolved membership is retained and visible until replay', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'retain an unclassified terminal' })
  await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
  const child = host.created[0]!
  await host.call('research_finish', child, { state: 'finished', summary: 'saved result' })
  const associated = Research.prototype.isAssociated
  const original = StorageClient.prototype.request
  let failures = 0
  const membership = spyOn(Research.prototype, 'isAssociated').mockImplementation(function(sid) { return sid === child ? false : associated.call(this, sid) })
  const storage = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'session_membership' && fields?.session_id === child) { failures++; return Promise.reject(new Error('membership unavailable')) }
    return original.call(this, method, fields, operation)
  })
  try {
    host.reply(child, 'finished')
    host.turn.end(child)
    await until(() => failures >= 3, 'membership retries exhausted')
    await until(async () => (await host.rpc().session({ sessionID: 'ses_main' })).eventRecovery.held === 1, 'held event visible in sidebar state')
    expect((await host.command('status', 'ses_main')).message).toContain('/research auto')
    expect((await host.command('pause', 'ses_main')).error).toBeUndefined()
  } finally { storage.mockRestore(); membership.mockRestore() }
  expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
  await until(async () => (await host.page('tasks')).items[0].state === 'finished', 'retained terminal settled')
  expect((await host.rpc().session({ sessionID: 'ses_main' })).eventRecovery.state).toBe('ready')
})

test('unresolved ordinary membership is bounded and cannot freeze known research continuation', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'unknown ordinary event' })
  await host.call('research_query', 'ses_main', { collection: 'nodes' })
  const original = StorageClient.prototype.request
  let failures = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'session_membership' && fields?.session_id === 'ordinary') { failures++; return Promise.reject(new Error('unresolved membership')) }
    return original.call(this, method, fields, operation)
  })
  try {
    host.turn.end('ordinary')
    await until(() => failures === 3, 'unknown event held')
    await until(async () => (await host.rpc().session({ sessionID: 'ses_main' })).eventRecovery.unresolved === 1, 'unknown classification visible')
    host.turn.end('ses_main')
    await until(() => host.said('ses_main').filter(item => item.resume).length === 2, 'known research not blocked')
  } finally { spy.mockRestore() }
  expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
  expect((await host.rpc().session({ sessionID: 'ses_main' })).eventRecovery.state).toBe('ready')
})

test('three consecutive unsent launches pause scheduling instead of creating an endless retry stream', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'bounded preflight failure' })
  await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  const original = StorageClient.prototype.request
  let attempts = 0
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
    if (method === 'task_context') { attempts++; return Promise.reject(new Error('persistent invalid context')) }
    return original.call(this, method, fields, operation)
  })
  try {
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    await host.command('resume', 'ses_main')
    await host.command('resume', 'ses_main')
    expect((await host.rpc().session({ sessionID: 'ses_main' })).pauseReason).toBe('fault')
    expect((await host.page('tasks')).items[0].state).toBe('queued')
    host.turn.end('ses_main')
    await host.call('research_query', 'ses_main', { collection: 'nodes' })
    expect(attempts).toBe(3)
    expect(host.created).toHaveLength(0)
    await expect(host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })).rejects.toMatchObject({ code: 'research_control_paused', details: { allow_fallback: false } })
  } finally { spy.mockRestore() }
})

test('finish mismatch selects the active task when session history contains an older finished task', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'active finish mismatch' })
  await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
  const child = host.created[0]!
  await host.call('research_finish', child, { state: 'finished', summary: 'saved' })
  const original = StorageClient.prototype.request
  const mutations: any[] = []
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(method, fields, operation) {
    if (method === 'workflow' && fields?.action === 'task_state') mutations.push(fields.fields)
    const result = await original.call(this, method, fields, operation)
    if (method === 'node_finish_pending' && result) return { ...result, turn_token: 'mismatched-token' }
    if (method === 'status' && result.workflow?.tasks?.length) return { ...result, workflow: { ...result.workflow,
      tasks: [{ ...result.workflow.tasks[0], task_id: 'T_history', session_id: child, state: 'finished' }, ...result.workflow.tasks] } }
    return result
  })
  try {
    host.turn.end(child)
    await until(() => mutations.length > 0, 'finish mismatch recorded')
    expect(mutations[0]).toMatchObject({ task_id: task.task_id, state: 'unverified', error: 'finish execution mismatch: reconciliation required' })
    expect(mutations.some(item => item.task_id === 'T_history')).toBe(false)
  } finally { spy.mockRestore() }
})

test('wait while paused returns the structured control error with no fallback permission', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'structured wait refusal' })
  await host.command('pause', 'ses_main')
  await expect(host.call('research_wait', 'ses_main', { task_ids: ['T_unknown'] })).rejects.toMatchObject({
    code: 'research_control_paused', details: { run_state: 'paused', retryable: false, recovery: 'explicit_resume', allow_fallback: false },
  })
})

test("R2 dispatch observes local and backend rejection without recording input text", async () => {
  const host = await load()
  await host.command("auto", "ses_main", { goal: "fixture" })
  await expect(host.call("research_note", "ses_main", { body: 42 })).rejects.toThrow()
  await expect(host.call("research_memory", "ses_main", {
    action: "record", kind: "claim", statement: "private-claim-text", visibility: "project",
  })).rejects.toThrow()
  const directory = join(project, ".research", "observations")
  const text = readdirSync(directory).map(name => readFileSync(join(directory, name), "utf8")).join("")
  const rows = text.trim().split("\n").map(line => JSON.parse(line))
  expect(rows.filter(row => row.phase === "start")).toHaveLength(2)
  expect(rows.filter(row => row.phase === "end").map(row => row.outcome)).toEqual(["failed", "failed"])
  expect(rows.some(row => row.phase === "transport" && row.attempt === 1)).toBe(true)
  expect(text).not.toContain("private-claim-text")
})

test("checkpoint query returns a bound base and stale writes are never refreshed automatically", async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'checkpoint fixture' })
  const read = await host.call('research_query', 'ses_main', { collection: 'checkpoint' })
  expect(read.snapshot.head_revision).toBe(0)
  const first = await host.call('research_memory', 'ses_main', { action: 'checkpoint', base: read.base, state: { step: 1 } })
  expect(first.revision).toBe(1)
  await expect(host.call('research_memory', 'ses_main', {
    action: 'checkpoint', base: read.base, state: { step: 'stale' },
  })).rejects.toMatchObject({ code: 'checkpoint_conflict', details: { current_revision: 1, read: { collection: 'checkpoint' } } })
  await expect(host.call('research_memory', 'ses_main', {
    action: 'checkpoint', state: { step: 'unread' },
  })).rejects.toMatchObject({ code: 'checkpoint_base_required' })
  const next = await host.call('research_memory', 'ses_main', {
    action: 'checkpoint', base: first.next_base, state: { step: 2 },
  })
  expect(next.revision).toBe(2)
  expect(next.source_identity.checkpoint_base.head_revision).toBe(1)
})

test('R2 exposes one narrow family and automatically freezes the base from the delivered model request', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'R2 integrated checkpoint'})
  host.events.push({id:'step-a',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'assistant-a',model:{id:'actual',providerID:'mock',variant:'v'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items.length===1,'native step registration')
  const request=modelRequest('ses_main')
  await host.hooks.get('context')!(request)
  expect(Object.keys(request.tools)).not.toContain('research_memory')
  for (const name of ['research_record','research_revise','research_checkpoint','research_dispose','research_narrow_scope']) expect(Object.keys(request.tools)).toContain(name)
  const execute=(id:string) => host.tools.get('research_checkpoint').execute({state:{next:id}},
    {sessionID:'ses_main',messageID:'assistant-a',id,agent:'build'})
  const results=await Promise.allSettled([execute('one'),execute('parallel')])
  expect(results.filter(row=>row.status==='fulfilled')).toHaveLength(1)
  expect(results.filter(row=>row.status==='rejected')).toHaveLength(1)
  const fulfilled=results.find(row=>row.status==='fulfilled') as PromiseFulfilledResult<any>
  const value=JSON.parse(fulfilled.value.content)
  expect(value.revision).toBe(1)
  const next=modelRequest('ses_main')
  next.messages=[{role:'tool',content:[{type:'tool-result',id:results[0].status==='fulfilled'?'one':'parallel',name:'research_checkpoint',result:{type:'text',value:fulfilled.value.content}}]}] as any
  host.events.push({id:'step-b',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'assistant-b',model:{id:'actual',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items.length===2,'next native step')
  await host.hooks.get('context')!(next)
  const saved=await host.tools.get('research_checkpoint').execute({state:{next:'two'}},
    {sessionID:'ses_main',messageID:'assistant-b',id:'second',agent:'build'})
  expect(JSON.parse(saved.content).revision).toBe(2)
})

test('autonomous turns stop at the durable cap; duplicate end events and restart cannot bypass it', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'budget fixture'})
  await host.command('budget','ses_main',{limit:'turns',value:'1',reason:'test cap'})
  host.turn.start('ses_main')
  const terminal={id:'terminal-budget',type:'session.execution.succeeded',data:{sessionID:'ses_main'}}
  host.events.push(terminal); host.events.push(terminal)
  await until(async () => (await host.rpc().session({sessionID:'ses_main'})).pauseReason==='budget','budget pause')
  expect(host.said('ses_main').filter(row=>row.resume)).toHaveLength(1)
  const before=host.said('ses_main').length
  const refused=await host.command('resume','ses_main')
  expect(refused.error).toContain('预算已到限')
  expect(host.said('ses_main').filter(row=>row.resume)).toHaveLength(1)
  await host.command('budget','ses_main',{limit:'turns',value:'2',reason:'explicit increase'})
  await host.command('resume','ses_main')
  expect(host.said('ses_main').filter(row=>row.resume)).toHaveLength(2)
  expect(host.said('ses_main').length).toBeGreaterThan(before)
  cleanups.pop()!()
  simulateDeadHost()
  const restarted=await load()
  await until(async () => (await restarted.summary()).run.state==='paused','cold recovery')
  expect((await restarted.command('resume','ses_main')).error).toContain('预算已到限')
  expect(restarted.said('ses_main').filter(row=>row.resume)).toHaveLength(0)
})

test('native usage records actual variant once and external observations omit command content', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'usage fixture'})
  await host.hooks.get('model.request')!({sessionID:'ses_main',kind:'compaction',model:{id:'actual-model',providerID:'actual-provider',variant:'high'}})
  const tokens={input:4,output:2,reasoning:1,cache:{read:2,write:1}}
  const event={id:'native-aux',type:'session.usage.recorded',data:{sessionID:'ses_main',source:'compaction',tokens,cost:0.25}}
  host.events.push(event); host.events.push(event)
  host.events.push({id:'aggregate',type:'session.usage.updated',data:{sessionID:'ses_main',tokens,cost:99}})
  let page:any
  await until(async () => {
    page=await host.call('research_query','ses_main',{collection:'usage'})
    return page.items.length===1 && page.items[0].amount===10
  },'usage reconciliation')
  expect(page.items[0].provider).toBe('actual-provider')
  expect(page.items[0].model).toBe('actual-model')
  expect(page.items[0].details.variant).toBe('high')
  await host.hooks.get('tool.execute.before')!({tool:'read',sessionID:'ses_main',id:'external',input:{path:'PRIVATE_COMMAND_BODY'}})
  await host.hooks.get('tool.execute.after')!({tool:'read',sessionID:'ses_main',id:'external',status:'completed'})
  const directory=join(project,'.research','observations')
  const text=readdirSync(directory).map(name=>readFileSync(join(directory,name),'utf8')).join('')
  expect(text).not.toContain('PRIVATE_COMMAND_BODY')
  expect(text).toContain('external_execution')
  expect(text).toContain('actual-model')
  if (process.env.R2_CAPTURE_TRACE) writeFileSync(new URL('../../docs/evidence/r2-runtime/native-invocations.jsonl',import.meta.url),text)
})

test('wall deadline interrupts an active native session without waiting for a model end event',async()=>{
  const host=await load()
  await host.command('auto','ses_main',{goal:'deadline fixture'})
  await host.command('budget','ses_main',{limit:'seconds',value:'1',reason:'bounded fixture'})
  await until(async()=> (await host.summary()).run.state==='paused','wall-clock monitor pause')
  await until(()=>host.interrupted.includes('ses_main'),'wall-clock interrupt')
  expect(host.interrupted).toContain('ses_main')
  expect((await host.rpc().session({sessionID:'ses_main'})).pauseReason).toBe('budget')
  expect(host.said('ses_main').filter(row=>row.resume)).toHaveLength(1)
})

test('a failed deadline refresh keeps the existing stop timer armed',async()=>{
  const host=await load()
  await host.command('auto','ses_main',{goal:'preserve deadline'})
  const original=StorageClient.prototype.request
  let fail=false,reads=0
  const spy=spyOn(StorageClient.prototype,'request').mockImplementation(function(this:StorageClient,method:string,fields?:any,id?:string):any{
    if(method==='runtime_deadline'){
      reads++
      if(fail)return Promise.reject(new Error('transient deadline read failure'))
    }
    return original.call(this,method,fields,id)
  })
  try{
    await host.command('budget','ses_main',{limit:'seconds',value:'1',reason:'bounded fixture'})
    await until(()=>reads>0,'initial deadline query')
    await Bun.sleep(20)
    fail=true
    await host.command('budget','ses_main',{limit:'turns',value:'5',reason:'trigger failed refresh'})
    await until(async()=> (await host.summary()).run.state==='paused','original deadline still stops')
    await until(()=>host.interrupted.includes('ses_main'),'original deadline interrupt')
    expect(host.interrupted).toContain('ses_main')
  }finally{spy.mockRestore()}
})

test('slow workspace metadata leaves backend queries responsive and remains optional for kickoff',async()=>{
  const host=await load()
  await host.command('auto','ses_main',{goal:'bounded source probe'})
  const node=await host.call('research_propose','ses_main',{question:'Q',why_now:'now',plan:'measure',root_reason:'first'})
  const original=StorageClient.prototype.request
  let entered=false,release!:(value:any)=>void
  const pending=new Promise(resolve=>{release=resolve})
  const spy=spyOn(StorageClient.prototype,'request').mockImplementation(function(this:StorageClient,method:string,fields?:any,id?:string):any{
    if(method==='runtime_source_owner'){entered=true;return pending}
    return original.call(this,method,fields,id)
  })
  try{
    const kickoff=host.call('research_dispatch','ses_main',{node_id:node.node_id})
    await until(()=>entered,'source metadata request')
    const page=await host.call('research_query','ses_main',{collection:'nodes'})
    expect(page.items.some((value:any)=>value.node_id===node.node_id)).toBe(true)
    await kickoff
    expect(host.created.length).toBeGreaterThan(0)
  }finally{release(undefined);spy.mockRestore()}
})

test('wall deadline during node launch keeps its execution unverified', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'deadline during launch' })
  const node = await host.call('research_propose', 'ses_main', { question: 'Q', why_now: 'now', plan: 'measure', root_reason: 'first' })
  const original = StorageClient.prototype.request
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(this: StorageClient, method: string, fields?: any, id?: string): any {
    if (method === 'runtime_reserve' && fields?.launch_intent_id) {
      const code = "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute(\"UPDATE runtime_budgets SET deadline_at='2000-01-01T00:00:00+00:00'\"); db.commit()"
      const result = spawnSync('python3', ['-c', code, join(project, '.research', 'state.sqlite3')], { encoding: 'utf8' })
      if (result.status !== 0) throw new Error(result.stderr)
    }
    return original.call(this, method, fields, id)
  })
  try {
    const result = await host.call('research_dispatch', 'ses_main', { node_id: node.node_id })
    expect(result).toMatchObject({ accepted: true, state: 'unverified', reason: 'delivery_unverified' })
    expect((await host.summary()).run.state).toBe('paused')
    expect((await host.page('tasks')).items[0].state).toBe('unverified')
    expect(host.said(host.created[0]!).filter(row => row.resume)).toHaveLength(0)
  } finally { spy.mockRestore() }
})

test('real node source edits prevent idle pauses while the turn budget still stops continuation',async()=>{
  const host=await load()
  await host.command('auto','ses_main',{goal:'source activity and independent budget'})
  await host.command('budget','ses_main',{limit:'turns',value:'5',reason:'bounded node fixture'})
  const node=await host.call('research_propose','ses_main',{question:'Q',why_now:'now',plan:'measure',root_reason:'first'})
  await host.call('research_dispatch','ses_main',{node_id:node.node_id})
  const session=host.created[0]!
  const workspace=/工作目录：(.+?)。/.exec(host.said(session).at(-1).text)![1]!
  const resumed=()=>host.said(session).filter(row=>row.resume).length
  for(let turn=0;turn<4;turn++){
    const before=resumed()
    writeFileSync(join(workspace,'experiment.py'),`print(${turn})\n`)
    host.turn.end(session)
    await until(()=>resumed()>before,'source edit continues node')
  }
  expect((await host.rpc().session({sessionID:session})).pauseReason).toBeNull()
  expect(resumed()).toBe(5)
  writeFileSync(join(workspace,'experiment.py'),'print(100)\n')
  host.turn.end(session)
  await until(async()=>(await host.rpc().session({sessionID:session})).pauseReason==='budget','budget stops despite source activity')
  expect(resumed()).toBe(5)
})

test('slow native telemetry cannot block event binding or introduce an invocation metadata dependency',async()=>{
  const host=await load()
  await host.command('auto','ses_main',{goal:'telemetry isolation'})
  const original=StorageClient.prototype.request
  let release!:(value:any)=>void
  const pending=new Promise(resolve=>{release=resolve})
  let metadataCalls=0
  const spy=spyOn(StorageClient.prototype,'request').mockImplementation(function(this:StorageClient,method:string,fields?:any,id?:string):any{
    if(method==='runtime_native_event')return pending
    if(method==='invocation_context'){metadataCalls++;return Promise.reject(new Error('metadata unavailable'))}
    return original.call(this,method,fields,id)
  })
  try{
    host.events.push({id:'slow-step',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'slow-assistant',model:{id:'actual',providerID:'mock'}}})
    await host.hooks.get('context')!(modelRequest('ses_main'))
    const result=await host.tools.get('research_checkpoint').execute({state:{next:'work'}},
      {sessionID:'ses_main',messageID:'slow-assistant',id:'write',agent:'build'})
    expect(JSON.parse(result.content).revision).toBe(1)
    expect(metadataCalls).toBe(0)
    const scope=await host.call('research_review_scope','ses_main',{refs:[],reason:'no current adoption'})
    expect(scope.refs).toEqual([])
  }finally{release({});spy.mockRestore()}
})

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
  for (const state of ['manual', 'paused', 'stopped', 'complete']) test(`coordinator native limits persist in ${state} and reject stale tool calls`, async () => {
    const host = await load()
    if (state === 'manual') await host.command('init', 'ses_main', { goal: 'persistent coordinator role' })
    else {
      await host.command('auto', 'ses_main', { goal: 'persistent coordinator role' })
      if (state === 'paused') await host.command('pause', 'ses_main')
      if (state === 'stopped') await host.command('stop', 'ses_main')
      if (state === 'complete') {
        const publication = await host.call('research_publish', 'ses_main', { status: 'complete', summary: 'report',
          items: [{ item_id: 'report', kind: 'text', content: { text: 'unresolved' } }] })
        await host.call('research_conclude', 'ses_main', { summary: 'done', final_ref: `pub/${publication.publication_id}#report`,
          outcome: 'unresolved', gaps: ['unknown'], review: { status: 'unreviewed', refs: [], limitations: ['not reviewed'] } })
      }
    }
    const blocked = ['shell', 'write', 'edit', 'websearch', 'webfetch', 'todowrite', 'mcp__unknown__action', 'research_unknown_plugin']
    const request = modelRequest('ses_main')
    for (const name of ['glob', 'grep', ...blocked]) request.tools[name] = { description: name, input: {} }
    await host.hooks.get('context')!(request)
    expect((await host.summary()).run.state).toBe(state)
    for (const name of ['read', 'glob', 'grep']) {
      expect(Object.keys(request.tools)).toContain(name)
      await host.hooks.get('tool.execute.before')!({ tool: name, sessionID: 'ses_main', id: `read-${name}`, input: {} })
    }
    for (const name of blocked) {
      expect(Object.keys(request.tools)).not.toContain(name)
      await expect(host.hooks.get('tool.execute.before')!({ tool: name, sessionID: 'ses_main', id: `stale-${name}`, input: {} }))
        .rejects.toMatchObject({ metadata: { code: 'research_role_violation', role: 'main', allow_fallback: false } })
    }
  })

  test('detaching a quiet main restores ordinary tools without changing other sessions', async () => {
    const host = await load()
    await host.command('init', 'ses_main', { goal: 'detach boundary' })
    const ordinary = modelRequest('ordinary')
    ordinary.tools.webfetch = { description: 'webfetch', input: {} }
    await host.hooks.get('context')!(ordinary)
    expect(Object.keys(ordinary.tools)).toContain('shell')
    expect(Object.keys(ordinary.tools)).toContain('webfetch')
    await host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: 'ordinary', id: 'ordinary-call', input: {} })
    expect((await host.command('detach', 'ses_main')).error).toBeUndefined()
    const detached = modelRequest('ses_main')
    await host.hooks.get('context')!(detached)
    expect(Object.keys(detached.tools)).toContain('shell')
    expect(Object.keys(detached.tools)).not.toContain('research_dispatch')
    await host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: 'ses_main', id: 'detached-call', input: {} })
  })

  test('missing and unrecognized associated roles cannot inherit node execution rights', async () => {
    for (const role of [undefined, 'unrecognized', 'exploration']) {
      for (const name of ['read', 'glob', 'grep']) expect(keepsTool(name, true, role)).toBe(true)
      for (const name of ['shell', 'write', 'webfetch', 'research_finish']) expect(keepsTool(name, true, role)).toBe(false)
    }
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'unknown role' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(method, fields, operation) {
      const result = await original.call(this, method, fields, operation)
      return method === 'session_membership' && fields?.session_id === child ? { ...result, session: { ...result.session, role: null } } : result
    })
    try {
      await expect(host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: child, id: 'unknown-call', input: {} }))
        .rejects.toMatchObject({ message: expect.stringContaining('研究角色尚未登记'), metadata: { role: 'unknown', allow_fallback: false } })
    } finally { spy.mockRestore() }
  })

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
    const overview = overviewModel(summary, null)!
    expect(overview.conclusion?.outcomeLabel).toBe("未解决")
    expect(overview.conclusion?.reviewNote).toBe("不表示结论通过")
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

  test.each(["legacy", "r1"])("runs goal → nodes → node sessions → synthesis → conclusion (%s)", async (policy) => {
    const host = await load()
    const started = await host.command("auto", "ses_main", { goal: "Find X" })
    await host.command("context-policy", "ses_main", { policy })
    expect(started.message).toContain("自主研究已开始")
    expect(host.said("ses_main").at(-1)).toMatchObject({ resume: true })
    expect(host.said("ses_main").at(-1).text).toContain("开始自主研究")

    // Coordinator turn: plan two nodes, dispatch the first, wait for it.
    host.turn.start("ses_main")
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "start", plan: "p1", root_reason: "first" })
    await host.call("research_propose", "ses_main", { question: "Q2?", why_now: "next", plan: "p2", root_reason: "second" })
    const task1 = await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(host.created).toHaveLength(1)
    const kickoff = host.said(host.created[0]!).at(-1)
    expect(kickoff).toMatchObject({ resume: true })
    expect(kickoff.text).toContain("你是节点 X-001 的执行会话")
    expect(kickoff.text).toContain("research_finish.summary 原文复制本次执行最后一份完整发布的 summary")
    expect(kickoff.text).toContain("新增证据、限制和建议先发布")
    await host.call("research_wait", "ses_main", { task_ids: [task1.task_id] })
    const mainBefore = host.said("ses_main").length
    host.turn.end("ses_main")
    await Bun.sleep(100)
    expect(host.said("ses_main").length).toBe(mainBefore)

    // Node turn: publish a file from its workspace, record a claim, finish.
    const workspace = /工作目录：(.+?)。/.exec(kickoff.text)![1]!
    mkdirSync(join(workspace, "output"), { recursive: true })
    writeFileSync(join(workspace, "output", "report.md"), "# X-001\nresult\n")
    host.turn.start(host.created[0]!)
    const pub = await host.call("research_publish", host.created[0]!, {
      status: "complete", summary: "node 1 result", items: [{ item_id: "report", kind: "report", source_path: "output/report.md" }],
    })
    await host.call("research_memory", host.created[0]!, {
      action: "record", kind: "observation", statement: "X holds under A", evidence_refs: [`pub/${pub.publication_id}#report`],
    })
    const seen = await host.call("research_query", host.created[0]!, { collection: "checkpoint" })
    const checkpoint = await host.call("research_memory", host.created[0]!, { action: "checkpoint", base: seen.base, state: { next: "finish" } })
    expect(checkpoint.node_id).toBe("X-001")
    await host.call("research_finish", host.created[0]!, { state: "finished", summary: "X holds under A" })
    host.turn.end(host.created[0]!)
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("等待的节点有了结果")), "coordinator wake-up")
    const report = host.said("ses_main").find((item) => item.text.includes("节点 X-001 已完成"))
    expect(report.text).toContain(`pub/${pub.publication_id}`)
    const request = modelRequest("ses_main")
    await host.hooks.get("context")!(request)
    if (policy === "r1") {
      expect(report.text).toContain("发现：knowledge/")
      expect(contextText(request)).toContain("X holds under A")
      expect(contextText(request)).toContain("## findings")
      await until(async () => {
        const view = await host.rpc().board({ view: "context_preview", directory: project })
        return view.last_recorded?.policy_version === "memory-r1-v1"
      }, "actual R1 injection record")
      const observed = await host.rpc().board({ view: "context_preview", directory: project })
      expect(observed.observations.generated).toBeGreaterThan(0)
      expect(observed.observations.recorded).toBeGreaterThan(0)
    } else expect(contextText(request)).not.toContain("X holds under A")

    // Coordinator dispatches the second node; node 2 finishes with a content-only publication.
    host.turn.start("ses_main")
    const task2 = await host.call("research_dispatch", "ses_main", { node_id: "X-002" })
    await host.call("research_wait", "ses_main", { task_ids: [task2.task_id] })
    host.turn.end("ses_main")
    await until(() => host.created.length === 2, "second node session")
    host.turn.start(host.created[1]!)
    let tentative: any
    if (policy === "r1") tentative = await host.call("research_memory", host.created[1]!, {
      action: "record", kind: "hypothesis", statement: "Tentative explanation only",
    })
    await host.call("research_publish", host.created[1]!, {
      status: "complete", summary: "node 2 result", items: [{ item_id: "note", kind: "text", content: { text: "Q2 answered" } }],
    })
    await host.call("research_finish", host.created[1]!, { state: "finished", summary: "Q2 answered" })
    host.turn.end(host.created[1]!)
    await until(() => host.said("ses_main").some((item) => item.text.includes("节点 X-002 已完成")), "second wake-up")

    if (policy === "r1") {
      expect(host.said("ses_main").some((value) => value.text.includes("未登记发现"))).toBe(true)
      expect(host.said("ses_main").some((value) => value.text.includes(`发现：${tentative.ref}`))).toBe(false)
    }
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
    host.turn.start(host.created[0]!)
    await host.call("research_publish", host.created[0]!, {
      status: "partial", summary: "r", items: [{ item_id: "r", kind: "text", content: { text: "r" } }],
    })
    await host.call("research_finish", host.created[0]!, { state: "finished", summary: "done" })
    host.turn.end(host.created[0]!)
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("等待的节点有了结果")), "wake-up")
    expect(host.said("ses_main").some((item) => item.text.includes("没有写入"))).toBe(false)
  })

  test('queued dispatch distinguishes capacity from a pause after acceptance', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'queue semantics' })
    await host.call('research_propose', 'ses_main', { question: 'Q1?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_propose', 'ses_main', { question: 'Q2?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const queued = await host.call('research_dispatch', 'ses_main', { node_id: 'X-002' })
    expect(queued).toMatchObject({ accepted: true, state: 'queued', reason: 'capacity', recovery: { action: 'wait' } })
  })

  test('a pause after task acceptance leaves it queued without launching', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'acceptance race' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(this: StorageClient, method: string, fields?: any, id?: string): Promise<any> {
      const value = await original.call(this, method, fields, id)
      if (method === 'workflow' && fields?.action === 'task') {
        await original.call(this, 'workflow', { ...fields, action: 'run', fields: { state: 'paused' } }, 'test-pause-after-acceptance')
      }
      return value
    })
    try {
      const result = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
      expect(result).toMatchObject({ accepted: true, state: 'queued', reason: 'control_paused', recovery: { action: 'explicit_resume' } })
      expect(host.created).toHaveLength(0)
    } finally { spy.mockRestore() }
  })

  test("autonomous turns use the model selected in the TUI, for the coordinator and every node", async () => {
    const host = await load()
    const model = { id: "deepseek-v4.1-flash", providerID: "opencode-go" }
    await host.command("auto", "ses_main", { goal: "Find X", model })
    expect(host.models).toContainEqual({ sessionID: "ses_main", model, via: "switch" })
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(host.models).toContainEqual({ sessionID: host.created[0]!, model, via: "create" })
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
    const node = modelRequest(host.created[0]!)
    await host.hooks.get("context")!(node)
    expect(Object.keys(node.tools)).toContain("research_finish")
    expect(Object.keys(node.tools)).not.toContain("research_dispatch")
    await expect(host.call("research_dispatch", host.created[0]!, { node_id: "X-001" })).rejects.toThrow("不属于本会话的角色")
  })

  test("research sessions never get the built-in subagent and question tools", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.call("research_propose", "ses_main", { question: "Q1?", why_now: "w", plan: "p", root_reason: "r" })
    await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    for (const sessionID of ["ses_main", host.created[0]!]) {
      const request = modelRequest(sessionID)
      await host.hooks.get("context")!(request)
      expect(Object.keys(request.tools)).not.toContain("subagent")
      expect(Object.keys(request.tools)).not.toContain("question")
      if (sessionID === "ses_main") expect(Object.keys(request.tools)).not.toContain("shell")
      else expect(Object.keys(request.tools)).toContain("shell")
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
    await until(() => host.said("ses_main").some((item) => item.text.includes("连续 3 轮没有研究推进")), "no-progress pause")
    const continued = host.said("ses_main").filter((item) => item.resume && item.text.includes("继续推进研究"))
    expect(continued).toHaveLength(2)
    const state = await host.rpc().session({ sessionID: "ses_main" })
    expect(state.pauseReason).toBe("no_progress")
    await host.command("resume", "ses_main")
    expect((await host.rpc().session({ sessionID: "ses_main" })).pauseReason).toBeNull()
  })

  test('a background shell waits without idle nudges and its native completion wakes only once', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'background work' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'now', plan: 'measure', root_reason: 'first' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    await until(() => host.said(child).some(item => item.resume), 'node launched')
    await host.hooks.get('tool.execute.before')!({tool:'shell', sessionID:child, id:'call_background', input:{background:true}})
    await host.hooks.get('tool.execute.after')!({tool:'shell', sessionID:child, id:'call_background', status:'completed',
      result:{metadata:{status:'running',shellID:'sh_background'}}})
    const sent = host.said(child).filter(item => item.resume).length
    host.turn.end(child)
    await until(async () => (await host.rpc().session({sessionID:child})).externalRunning === 1, 'background waiting state')
    await Bun.sleep(150)
    expect(host.said(child).filter(item => item.resume)).toHaveLength(sent)
    expect((await host.rpc().session({sessionID:child})).pauseReason).toBeNull()
    const event = {id:'job-enqueued', type:'session.inbox.enqueued', durable:{seq:20}, data:{sessionID:child,
      inboxID:'msg_job_done', item:{payload:{metadata:{source:'shell',shellID:'sh_background',state:'completed',exit:0}}}}}
    host.events.push(event)
    host.events.push(event)
    host.events.push({id:'job-started',type:'session.execution.started',durable:{seq:21},data:{sessionID:child}})
    host.events.push({id:'job-delivered',type:'session.inbox.delivered',durable:{seq:22},data:{sessionID:child,inboxID:'msg_job_done'}})
    await until(async () => (await host.rpc().session({sessionID:child})).externalRunning === 0, 'background terminal')
    const request = modelRequest(child)
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('shell')
    expect(host.said(child).filter(item => item.resume)).toHaveLength(sent)
  })

  test('a completed background shell cannot resume a paused node or trigger a read loop', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'pause background work' })
    await host.call('research_propose', 'ses_main', { question:'Q?', why_now:'now', plan:'measure', root_reason:'first' })
    await host.call('research_dispatch', 'ses_main', { node_id:'X-001' })
    const child = host.created[0]!
    await until(() => host.said(child).some(item => item.resume), 'node launched')
    await host.hooks.get('tool.execute.before')!({tool:'shell', sessionID:child, id:'call_paused', input:{background:true}})
    await host.hooks.get('tool.execute.after')!({tool:'shell', sessionID:child, id:'call_paused', status:'completed',
      result:{metadata:{status:'running',shellID:'sh_paused'}}})
    host.turn.end(child)
    await until(async () => (await host.rpc().session({sessionID:child})).externalRunning === 1, 'background waiting state')
    await host.command('pause', 'ses_main')
    host.events.push({id:'paused-job',type:'session.inbox.enqueued',durable:{seq:30},data:{sessionID:child,
      inboxID:'msg_paused_done',item:{payload:{metadata:{source:'shell',shellID:'sh_paused',state:'completed',exit:0}}}}})
    host.events.push({id:'paused-start',type:'session.execution.started',durable:{seq:31},data:{sessionID:child}})
    host.events.push({id:'paused-delivery',type:'session.inbox.delivered',durable:{seq:32},data:{sessionID:child,inboxID:'msg_paused_done'}})
    await until(async () => (await host.rpc().session({sessionID:child})).externalRunning === 0, 'paused terminal')
    const request = modelRequest(child)
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(request.system.some(item => item.text.includes('background completion arrived'))).toBe(true)
    expect((await host.rpc().session({sessionID:child})).runState).toBe('paused')
    host.turn.end(child)
    await until(() => {
      const query = spawnSync('python3', ['-c', "import sqlite3,sys,json; d=sqlite3.connect(sys.argv[1]); r=d.execute(\"SELECT details FROM workflow_intents WHERE session_id=? AND kind='execution_start' ORDER BY rowid DESC LIMIT 1\",(sys.argv[2],)).fetchone(); t=json.loads(r[0])['execution_token']; print(d.execute(\"SELECT COUNT(*) FROM workflow_intents WHERE session_id=? AND kind='execution_terminal' AND json_extract(details,'$.execution_token')=?\",(sys.argv[2],t)).fetchone()[0])", join(project,'.research','state.sqlite3'), child], {encoding:'utf8'})
      return query.stdout.trim() === '1'
    }, 'paused wake terminal persisted')
    cleanups.pop()!()
    const reloaded = await load()
    await until(async () => (await reloaded.command('status','ses_main')).state?.workflow?.controller?.phase === 'ready', 'paused reload ready')
    expect((await reloaded.page('tasks')).items[0].state).toBe('running')
    expect((await reloaded.summary()).run.state).toBe('paused')
    expect(reloaded.said(child).filter(item => item.resume)).toHaveLength(0)
  })

  test('a shell completion delivered inside an active turn does not reserve another turn', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal:'inline background completion' })
    await host.call('research_propose', 'ses_main', { question:'Q?', why_now:'now', plan:'measure', root_reason:'first' })
    await host.call('research_dispatch', 'ses_main', { node_id:'X-001' })
    const child = host.created[0]!
    await until(() => host.said(child).some(item => item.resume), 'node launched')
    await host.hooks.get('tool.execute.before')!({tool:'shell', sessionID:child, id:'call_inline', input:{background:true}})
    await host.hooks.get('tool.execute.after')!({tool:'shell', sessionID:child, id:'call_inline', status:'completed',
      result:{metadata:{status:'running',shellID:'sh_inline'}}})
    const before = shellTurns(child)
    host.events.push({id:'inline-job',type:'session.inbox.enqueued',durable:{seq:30},data:{sessionID:child,
      inboxID:'msg_inline_done',item:{payload:{metadata:{source:'shell',shellID:'sh_inline',state:'completed',exit:0}}}}})
    host.events.push({id:'inline-delivery',type:'session.inbox.delivered',durable:{seq:31},data:{sessionID:child,inboxID:'msg_inline_done'}})
    await until(async () => (await host.rpc().session({sessionID:child})).externalRunning === 0, 'inline completion')
    const request = modelRequest(child)
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('shell')
    expect(shellTurns(child)).toBe(before)
  })

  test('a fast shell completion waits for its shell ID to be persisted', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal:'fast background completion' })
    await host.call('research_propose', 'ses_main', { question:'Q?', why_now:'now', plan:'measure', root_reason:'first' })
    await host.call('research_dispatch', 'ses_main', { node_id:'X-001' })
    const child = host.created[0]!
    await until(() => host.said(child).some(item => item.resume), 'node launched')
    await host.hooks.get('tool.execute.before')!({tool:'shell', sessionID:child, id:'call_fast', input:{background:true}})
    const original = StorageClient.prototype.request
    let release: (() => void) | undefined
    let notices = 0
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(method, fields, operation) {
      if (method === 'runtime_shell_result') await new Promise<void>(resolve => { release = resolve })
      if (method === 'runtime_shell_notice') notices++
      return original.call(this, method, fields, operation)
    })
    try {
      const after = host.hooks.get('tool.execute.after')!({tool:'shell', sessionID:child, id:'call_fast', status:'completed',
        result:{metadata:{status:'running',shellID:'sh_fast'}}})
      await until(() => !!release, 'shell result is being persisted')
      host.events.push({id:'fast-job',type:'session.inbox.enqueued',durable:{seq:40},data:{sessionID:child,
        inboxID:'msg_fast_done',item:{payload:{metadata:{source:'shell',shellID:'sh_fast',state:'completed',exit:0}}}}})
      await Bun.sleep(80)
      expect(notices).toBe(0)
      release!()
      await after
      host.events.push({id:'fast-delivery',type:'session.inbox.delivered',durable:{seq:41},data:{sessionID:child,inboxID:'msg_fast_done'}})
      await until(() => notices === 1, 'terminal notice after registration')
      const request = modelRequest(child)
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toContain('shell')
      expect((await host.rpc().session({sessionID:child})).externalUnverified).toBe(0)
    } finally { release?.(); spy.mockRestore() }
  })

  test('same-process warm reload preserves a running background shell', async () => {
    const first = await load()
    await first.command('auto', 'ses_main', { goal:'background recovery' })
    await first.call('research_propose', 'ses_main', { question:'Q?', why_now:'now', plan:'measure', root_reason:'first' })
    await first.call('research_dispatch', 'ses_main', { node_id:'X-001' })
    const child = first.created[0]!
    await until(() => first.said(child).some(item => item.resume), 'node launched')
    await first.hooks.get('tool.execute.before')!({tool:'shell', sessionID:child, id:'call_reload', input:{background:true}})
    await first.hooks.get('tool.execute.after')!({tool:'shell', sessionID:child, id:'call_reload', status:'completed',
      result:{metadata:{status:'running',shellID:'sh_reload'}}})
    first.turn.end(child)
    await until(async () => (await first.rpc().session({sessionID:child})).externalRunning === 1, 'background waiting state')
    await until(() => {
      const query = spawnSync('python3', ['-c', "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('SELECT COUNT(*) FROM runtime_progress WHERE session_id=? AND last_finished_turn IS NOT NULL',(sys.argv[2],)).fetchone()[0])", join(project,'.research','state.sqlite3'), child], {encoding:'utf8'})
      return query.stdout.trim() === '1'
    }, 'waiting turn persisted')
    cleanups.pop()!()
    const second = await load()
    await until(async () => (await second.command('status','ses_main')).state?.workflow?.controller?.phase === 'ready', 'warm controller ready')
    expect((await second.page('tasks')).items[0].state).toBe('running')
    expect((await second.rpc().session({sessionID:child})).externalUnverified).toBe(0)
    await second.command('resume','ses_main')
    expect(second.said(child).filter(item => item.resume)).toHaveLength(0)
    expect(second.created).toHaveLength(0)
  })

  async function shellNode(goal: string) {
    const host = await load()
    await host.command('auto', 'ses_main', {goal})
    await host.call('research_propose','ses_main',{question:'Q?',why_now:'now',plan:'measure',root_reason:'first'})
    await host.call('research_dispatch','ses_main',{node_id:'X-001'})
    const child = host.created[0]!
    await until(() => host.said(child).some(item => item.resume),'node launched')
    return {host,child}
  }
  function shellTurns(child: string): number {
    const result=spawnSync('python3',['-c',"import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute(\"SELECT COUNT(*) FROM runtime_budget_reservations WHERE session_id=? AND kind='turn' AND state!='not_sent'\",(sys.argv[2],)).fetchone()[0])",join(project,'.research','state.sqlite3'),child],{encoding:'utf8'})
    if (result.status !== 0) throw new Error(result.stderr)
    return Number(result.stdout.trim())
  }
  function shellEvent(child:string, id:string, shellID:string, messageID:string, state:any='completed') {
    return {id,type:'session.inbox.enqueued',data:{sessionID:child,inboxID:messageID,
      item:{payload:{metadata:{source:'shell',shellID,state,exit:0}}}}}
  }

  test('a draining paused turn may start a shell with its execution token', async () => {
    const {host,child}=await shellNode('drain shell')
    await host.command('pause','ses_main')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'drain',input:{}})
    expect((await host.rpc().session({sessionID:child})).externalStarting).toBe(1)
    expect((await host.command('resume','ses_main')).message).not.toContain('execution_unverified')
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'drain',status:'completed',result:{metadata:{}}})
    host.turn.end(child)
    await Bun.sleep(80)
    expect((await host.page('tasks')).items[0].state).toBe('running')
  })

  test('foreground tool errors leave no background recovery obligation', async () => {
    const {host,child}=await shellNode('foreground failure')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'fg_error',input:{background:false}})
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'fg_error',status:'error'})
    expect((await host.rpc().session({sessionID:child})).externalUnverified).toBe(0)
    host.turn.end(child,'interrupted')
    await until(async()=>(await host.rpc().session({sessionID:child})).pauseReason==='native_stop','native pause')
    const result=await host.command('resume','ses_main')
    expect(result.message).not.toContain('execution_unverified')
  })

  test('pause notice delivery failures do not change the task execution state', async () => {
    const {host,child}=await shellNode('pause notice failure')
    const original=host.ctx.session.synthetic
    host.ctx.session.synthetic=async(input:any)=>{
      if (!input.resume && input.sessionID===child) throw new Error('notice unavailable')
      return original(input)
    }
    host.turn.end(child,'interrupted')
    await until(async()=>(await host.rpc().session({sessionID:child})).pauseReason==='native_stop','pause saved')
    expect((await host.page('tasks')).items[0].state).toBe('running')
  })

  test('native foreground tool failure settles even when execute.after is absent', async () => {
    const {host,child}=await shellNode('native foreground abort')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'native_abort',input:{}})
    host.events.push({id:'native_tool_failed',type:'session.tool.failed',data:{sessionID:child,id:'native_abort',error:{type:'aborted'}}})
    host.turn.end(child,'interrupted')
    await until(async()=>(await host.rpc().session({sessionID:child})).pauseReason==='native_stop','foreground abort settled')
    expect((await host.page('tasks')).items[0].state).toBe('running')
    expect((await host.rpc().session({sessionID:child})).externalStarting).toBe(0)
  })

  test('storage failure while settling a native failure cannot block its terminal event', async () => {
    const {host,child}=await shellNode('failed event storage gap')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'storage_abort',input:{}})
    const original=StorageClient.prototype.request
    let attempts=0
    const spy=spyOn(StorageClient.prototype,'request').mockImplementation(async function(method,fields,operation){
      if(method==='runtime_shell_result'){attempts++;throw new Error('temporary storage failure')}
      return original.call(this,method,fields,operation)
    })
    try {
      host.events.push({id:'storage_tool_failed',type:'session.tool.failed',data:{sessionID:child,id:'storage_abort',error:{type:'aborted'}}})
      host.turn.end(child,'interrupted')
      await until(async()=>(await host.page('tasks')).items[0].state==='unverified','terminal reconciliation after result gap')
      expect(attempts).toBe(1)
      expect((await host.command('status','ses_main')).state.event_recovery.state).toBe('ready')
      expect((await host.command('resume','ses_main')).message).toContain('/research recover task')
    } finally {spy.mockRestore()}
  })

  test('shell begin preserves the storage error code instead of claiming the project is paused', async () => {
    const {host,child}=await shellNode('begin error classification')
    const original=StorageClient.prototype.request
    const spy=spyOn(StorageClient.prototype,'request').mockImplementation(async function(method,fields,operation){
      if(method==='runtime_shell_begin')throw Object.assign(new Error('storage offline'),{code:'storage_offline'})
      return original.call(this,method,fields,operation)
    })
    try {
      await expect(host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'begin_error',input:{}}))
        .rejects.toMatchObject({metadata:{code:'storage_offline'}})
      expect((await host.page('tasks')).items[0].state).toBe('running')
    } finally {spy.mockRestore()}
  })

  test('untracked tool failures do not consume the research event queue', async () => {
    const {host,child}=await shellNode('unrelated failure flood')
    for(let i=0;i<300;i++)host.events.push({id:`untracked_${i}`,type:'session.tool.failed',data:{sessionID:child,id:`other_${i}`,error:{type:'tool.execution'}}})
    host.turn.end(child,'interrupted')
    await until(async()=>(await host.rpc().session({sessionID:child})).pauseReason==='native_stop','terminal after unrelated events')
    expect((await host.command('status','ses_main')).state.event_recovery.state).toBe('ready')
    expect((await host.page('tasks')).items[0].state).toBe('running')
  })

  test('an interrupted call without an after hook has an immediate operator recovery path', async () => {
    const {host,child}=await shellNode('orphaned shell')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'missing_after',input:{}})
    host.turn.end(child,'interrupted')
    await until(async()=>(await host.page('tasks')).items[0].state==='unverified','task follows shell uncertainty')
    const result=await host.command('resume','ses_main')
    expect(result.message).toContain('/research recover task')
    const task=(await host.page('tasks')).items[0]
    await host.command('recover','ses_main',{kind:'task',task_id:task.task_id,reason:'verified process exit'})
    expect((await host.page('tasks')).items[0].state).toBe('failed')
    expect((await host.rpc().session({sessionID:child})).externalUnverified).toBe(0)
  })

  test('a notice arriving before execute.after waits for its matching shell result', async () => {
    const {host,child}=await shellNode('early terminal notice')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'before_after',input:{background:true}})
    host.events.push(shellEvent(child,'early','sh_early','msg_early'))
    await Bun.sleep(60)
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'before_after',status:'completed',result:{metadata:{status:'running',shellID:'sh_early'}}})
    host.events.push({id:'early_delivered',type:'session.inbox.delivered',data:{sessionID:child,inboxID:'msg_early'}})
    await until(async()=>(await host.rpc().session({sessionID:child})).externalRunning===0,'terminal matched')
    const request=modelRequest(child);await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('shell')
    expect((await host.page('tasks')).items[0].state).toBe('running')
  })

  test('enqueue in one turn and delivery in the next reserves the native wake exactly once', async () => {
    const {host,child}=await shellNode('delivery boundary')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'boundary',input:{background:true}})
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'boundary',status:'completed',result:{metadata:{status:'running',shellID:'sh_boundary'}}})
    const before=shellTurns(child)
    host.events.push(shellEvent(child,'boundary_notice','sh_boundary','msg_boundary'))
    await until(async()=>(await host.rpc().session({sessionID:child})).externalRunning===0,'notice enqueued')
    const sent=host.said(child).filter(item=>item.resume).length
    host.turn.end(child)
    await Bun.sleep(100)
    expect(host.said(child).filter(item=>item.resume)).toHaveLength(sent)
    host.turn.start(child)
    host.events.push({id:'boundary_delivery',type:'session.inbox.delivered',data:{sessionID:child,inboxID:'msg_boundary'}})
    await until(()=>shellTurns(child)===before+1,'new wake reserved')
    const request=modelRequest(child);await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('shell')
  })

  test('a transcript witness binds a new native wake against the real backend before delivered arrives', async () => {
    const {host,child}=await shellNode('real transcript binding')
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'transcript_job',input:{background:true}})
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'transcript_job',status:'completed',result:{metadata:{status:'running',shellID:'sh_transcript_job'}}})
    host.turn.end(child)
    await until(()=>{
      const result=spawnSync('python3',['-c',"import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('SELECT COUNT(*) FROM runtime_progress WHERE session_id=? AND last_finished_turn IS NOT NULL',(sys.argv[2],)).fetchone()[0])",join(project,'.research','state.sqlite3'),child],{encoding:'utf8'})
      return result.stdout.trim()==='1'
    },'waiting turn ended')
    const before=shellTurns(child)
    host.events.push(shellEvent(child,'transcript_enqueued','sh_transcript_job','msg_transcript_job'))
    host.turn.start(child)
    const original=host.ctx.session.context
    host.ctx.session.context=async(args:any)=>args.sessionID===child ? [
      ...(await original(args)),{type:'synthetic',id:'msg_transcript_job',metadata:{source:'shell',shellID:'sh_transcript_job',state:'completed',exit:0}}
    ] : original(args)
    const request=modelRequest(child)
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('shell')
    expect((await host.rpc().session({sessionID:child})).pauseReason).toBeNull()
    expect((await host.page('tasks')).items[0].state).toBe('running')
    expect(shellTurns(child)).toBe(before+1)
    const bound=spawnSync('python3',['-c',"import sqlite3,json,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute(\"SELECT details FROM workflow_intents WHERE kind='execution_start' AND session_id=? ORDER BY rowid DESC LIMIT 1\",(sys.argv[2],)).fetchone()[0])",join(project,'.research','state.sqlite3'),child],{encoding:'utf8'})
    expect(JSON.parse(bound.stdout)).toMatchObject({binding_source:'transcript',message_id:'msg_transcript_job'})
    expect(typeof JSON.parse(bound.stdout).event_seq).toBe('number')
    const again=modelRequest(child);await host.hooks.get('context')!(again)
    expect(Object.keys(again.tools)).toContain('shell')
    expect(shellTurns(child)).toBe(before+1)
    host.events.push({id:'transcript_delivered_late',type:'session.inbox.delivered',data:{sessionID:child,inboxID:'msg_transcript_job'}})
    await host.hooks.get('context')!(modelRequest(child))
    expect(shellTurns(child)).toBe(before+1)
    expect((await host.rpc().session({sessionID:child})).pauseReason).toBeNull()
  })

  test('unknown and stale shell notices do not poison subsequent session events', async () => {
    const {host,child}=await shellNode('stale notice')
    host.events.push(shellEvent(child,'no_id','sh_missing',''))
    host.events.push(shellEvent(child,'stale','sh_missing','msg_stale'))
    host.events.push({id:'stale_delivered',type:'session.inbox.delivered',data:{sessionID:child,inboxID:'msg_stale'}})
    await Bun.sleep(100)
    const request=modelRequest(child);await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('shell') // Already-active legitimate turn remains authorized.
    expect((await host.page('tasks')).items[0].state).toBe('running')
    expect((await host.command('status','ses_main')).state.event_recovery.state).toBe('ready')
  })

  test('a recovered paused shell notification cannot start a read loop, while later user questions can read', async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal:'paused recovery context' })
    await host.call('research_propose', 'ses_main', { question:'Q?', why_now:'now', plan:'measure', root_reason:'first' })
    await host.call('research_dispatch', 'ses_main', { node_id:'X-001' })
    const child = host.created[0]!
    host.turn.end(child, 'interrupted')
    await until(async () => (await host.rpc().session({sessionID:child})).pauseReason === 'native_stop', 'node paused')
    const original = host.ctx.session.context
    host.ctx.session.context = async ({sessionID}: any) => sessionID === child
      ? [{type:'synthetic',id:'msg_recovered',metadata:{source:'shell',shellID:'sh_recovered',state:'completed'}}]
      : original({sessionID})
    const recovered = modelRequest(child)
    await host.hooks.get('context')!(recovered)
    expect(Object.keys(recovered.tools)).toHaveLength(0)
    host.ctx.session.context = async ({sessionID}: any) => sessionID === child
      ? [{type:'synthetic',metadata:{source:'shell'}},{type:'user',id:'msg_question'}] : original({sessionID})
    const question = modelRequest(child)
    await host.hooks.get('context')!(question)
    expect(Object.keys(question.tools)).toContain('read')
    expect(Object.keys(question.tools)).not.toContain('shell')
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

  test("a paused node finishes its current turn without waking the coordinator", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "pause fixture" })
    await host.call("research_propose", "ses_main", { question: "Q?", why_now: "w", plan: "p", root_reason: "r" })
    const task = await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    await host.call("research_wait", "ses_main", { task_ids: [task.task_id] })
    host.turn.start(host.created[0]!)
    await host.command("pause", "ses_main")
    await host.call("research_finish", host.created[0]!, { state: "finished", summary: "existing work finished" })
    const before = host.said("ses_main").filter(item => item.resume).length
    host.turn.end(host.created[0]!)
    await until(async () => (await host.page("tasks")).items[0].state === "finished", "paused node finish")
    expect((await host.summary()).run.state).toBe("paused")
    expect(host.said("ses_main").filter(item => item.resume)).toHaveLength(before)
  })

  test("a first-turn inbox event can authorize a paused node finish without a visible started event", async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'first turn event' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    await host.command('pause', 'ses_main')
    await host.call('research_finish', child, { state: 'finished', summary: 'first turn drained' })
    host.turn.end(child)
    await until(async () => (await host.page('tasks')).items[0].state === 'finished', 'first turn finish')
  })

  test("a new node turn after pause cannot inherit the previous drain authorization", async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'drain boundary' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    host.turn.start(child)
    await host.command('pause', 'ses_main')
    await host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: child, id: 'old-turn', input: {} })
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'old-turn',status:'completed',result:{metadata:{}}})
    host.turn.end(child)
    host.turn.start(child)
    await expect(host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: child, id: 'new-turn', input: {} }))
      .rejects.toMatchObject({ metadata: { code: 'research_control_paused', allow_fallback: false } })
    await expect(host.call('research_finish', child, { state: 'finished', summary: 'not authorized' })).rejects.toThrow('不能开始新工作')
  })

  test("a new user message during pause revokes the existing turn's drain authorization", async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'steered pause boundary' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    host.turn.start(child)
    await host.command('pause', 'ses_main')
    await host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: child, id: 'before-steer', input: {} })
    host.events.push({ type: 'session.inbox.delivered', data: { sessionID: child, inboxID: 'msg_unapproved_user' } })
    await expect(host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: child, id: 'after-steer', input: {} }))
      .rejects.toMatchObject({ metadata: { code: 'research_control_paused' } })
  })

  test("coordinator cannot execute native or unknown tools through an old request", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "role fixture" })
    for (const tool of ["shell", "edit", "write", "research_shell"]) {
      await expect(host.hooks.get("tool.execute.before")!({ tool, sessionID: "ses_main", id: `blocked-${tool}`, input: {} }))
        .rejects.toMatchObject({ metadata: { code: "research_role_violation", allow_fallback: false } })
    }
    await host.hooks.get("tool.execute.before")!({ tool: "read", sessionID: "ses_main", id: "read", input: {} })
  })

  test("paused context replaces continue instructions and cannot propose new work", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "control fixture" })
    await host.command("pause", "ses_main")
    await host.hooks.get("prompt")!({ sessionID: "ses_main", prompt: { text: "为何停止了？" } })
    const request = modelRequest("ses_main")
    await host.hooks.get("context")!(request)
    expect(Object.keys(request.tools)).not.toContain("research_dispatch")
    expect(Object.keys(request.tools)).not.toContain("research_propose")
    expect(contextText(request)).toContain("Only an explicit /research auto")
    expect(contextText(request)).not.toContain("/research resume")
    expect(contextText(request)).not.toContain("decide and continue")
    expect(controlInstructions("paused", null)).toContain("an explicit /research auto is required")
    expect(controlInstructions("paused", null)).not.toContain("/research resume")
    expect(controlInstructions("running", "finished")).toContain("/research auto does not reopen it")
    expect(controlInstructions("running", "finished")).not.toContain("/research resume")
    await expect(host.call("research_propose", "ses_main", { question: "new?", why_now: "w", plan: "p", root_reason: "r" }))
      .rejects.toThrow("研究已暂停")
    expect((await host.summary()).run.state).toBe("paused")
  })

  test("a restart leaves a running project paused until the user resumes it", async () => {
    const first = await load()
    await first.command("auto", "ses_main", { goal: "Find X" })
    cleanups.pop()!()
    simulateDeadHost()
    const second = await load()
    await until(async () => (await second.summary()).run.state === "paused", "cold pause")
    expect((await second.rpc().session({ sessionID: "ses_main" })).pauseReason).toBe("cold")
    await second.command("resume", "ses_main")
    expect((await second.summary()).run.state).toBe("running")
  })

  for (const outcome of ['failed', 'interrupted']) test(`a ${outcome} finish turn can finish in the next authorized turn`, async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'retry a finish turn' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    await host.call('research_finish', child, { state: 'finished', summary: 'first result' })
    const turns = host.said(child).filter(item => item.resume).length
    host.turn.end(child, outcome)
    if (outcome === 'interrupted') {
      await until(async () => (await host.rpc().session({ sessionID: child })).pauseReason === 'native_stop', 'interrupted finish pause')
      await host.command('resume', 'ses_main')
    }
    await until(() => host.said(child).filter(item => item.resume).length > turns, 'next node turn')
    await host.call('research_finish', child, { state: 'finished', summary: 'rechecked result' })
    host.turn.end(child)
    await until(async () => (await host.page('tasks')).items[0].state === 'finished', 'finish after retry')
    expect(host.created).toHaveLength(1)
  })

  test('warm reload correlates the second node turn instead of the original kickoff', async () => {
    const first = await load()
    await first.command('auto', 'ses_main', { goal: 'multiple turns across reload' })
    await first.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await first.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = first.created[0]!
    const kickoff = first.said(child).find(item => item.resume).id
    first.turn.end(child)
    await until(() => first.said(child).filter(item => item.resume).length === 2, 'second turn nudge')
    const currentMessage = first.said(child).filter(item => item.resume).at(-1).id
    await first.call('research_finish', child, { state: 'finished', summary: 'second turn result' })
    cleanups.pop()!()
    const second = await load({ hostSetup: host => {
      const nativeWait = host.ctx.session.wait
      const nativeContext = host.ctx.session.context
      host.ctx.session.wait = async (input: any) => input.sessionID === child ? undefined : nativeWait(input)
      host.ctx.session.context = async ({ sessionID }: any) => sessionID === child
        ? [{ type: 'synthetic', id: kickoff }, { type: 'idle', outcome: 'succeeded' },
           { type: 'synthetic', id: currentMessage }, { type: 'assistant', content: [{ type: 'text', text: 'done' }] },
           { type: 'idle', outcome: 'succeeded' }] : nativeContext({ sessionID })
    } })
    await until(async () => (await second.page('tasks')).items[0].state === 'finished', 'second turn recovered')
    expect((await second.summary()).run.state).toBe('running')
    expect(second.said(child).filter(item => item.resume)).toHaveLength(0)
  })

  test('warm reload preserves a node paused after a verified terminal event', async () => {
    const first = await load()
    await first.command('auto', 'ses_main', { goal: 'idle paused node reload' })
    await first.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await first.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = first.created[0]!
    first.turn.end(child, 'interrupted')
    await until(async () => (await first.rpc().session({ sessionID: child })).pauseReason === 'native_stop', 'node pause')
    cleanups.pop()!()
    const second = await load()
    await until(async () => (await second.command('status', 'ses_main')).state?.workflow?.controller?.phase === 'ready', 'warm ready')
    expect((await second.page('tasks')).items[0].state).toBe('running')
    expect(second.said(child).filter(item => item.resume)).toHaveLength(0)
    await second.command('resume', 'ses_main')
    expect(second.said(child).filter(item => item.resume)).toHaveLength(1)
  })

  test('warm reload settles a saved finish when its terminal was persisted before handoff', async () => {
    const first = await load()
    await first.command('auto', 'ses_main', { goal: 'handoff after terminal persistence' })
    await first.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await first.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = first.created[0]!
    await first.call('research_finish', child, { state: 'finished', summary: 'saved before handoff' })
    const original = StorageClient.prototype.request
    let release: (() => void) | undefined
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
      if (method === 'node_finish_commit' && !release) return new Promise((_resolve, reject) => {
        release = () => reject(new Error('old attachment closed'))
      })
      return original.call(this, method, fields, operation)
    })
    try {
      first.turn.end(child)
      await until(() => !!release, 'terminal saved before finish settlement')
      cleanups.pop()!()
      const second = await load()
      release!()
      await until(async () => (await second.page('tasks')).items[0].state === 'finished', 'durable finish settled by successor')
      expect(second.said(child).filter(item => item.resume)).toHaveLength(0)
      expect((await second.page('tasks')).items).toHaveLength(1)
      expect(second.created).toHaveLength(0)
    } finally { release?.(); spy.mockRestore() }
  })

  test("same-process plugin reload preserves running research and a pending node finish", async () => {
    const first = await load()
    await first.command("auto", "ses_main", { goal: "reload fixture" })
    await first.call("research_propose", "ses_main", { question: "Q?", why_now: "w", plan: "p", root_reason: "r" })
    await first.call("research_dispatch", "ses_main", { node_id: "X-001" })
    first.turn.start(first.created[0]!)
    await first.call("research_finish", first.created[0]!, { state: "finished", summary: "completed before reload" })
    cleanups.pop()!()
    let releaseWait!: () => void
    const waitForTerminal = new Promise<void>(resolve => { releaseWait = resolve })
    const second = await load({ hostSetup: host => {
      const nativeWait = host.ctx.session.wait
      host.ctx.session.wait = async ({ sessionID }: { sessionID: string }) => {
        if (sessionID === first.created[0]) await waitForTerminal
        else await nativeWait({ sessionID })
      }
    } })
    expect((await second.summary()).run.state).toBe("running")
    second.turn.end(first.created[0]!)
    await until(async () => (await second.page("tasks")).items[0].state === "finished", "finish after reload")
    releaseWait()
    expect((await second.summary()).run.state).toBe("running")
  })

  test("a still-live prior attachment forwards role checks and research tools to the new controller", async () => {
    const first = await load()
    await first.command('auto', 'ses_main', { goal: 'parallel attachment fixture' })
    await first.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await first.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = first.created[0]!
    let release!: () => void
    const idle = new Promise<void>(resolve => { release = resolve })
    const second = await load({ hostSetup: host => {
      const nativeWait = host.ctx.session.wait
      host.ctx.session.wait = async ({ sessionID }: { sessionID: string }) => { if (sessionID === child) await idle; else await nativeWait({ sessionID }) }
    } })
    await until(async () => (await second.command('status', 'ses_main')).state?.workflow?.controller?.phase === 'ready', 'new controller')
    const request = modelRequest(child)
    await first.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_finish')
    expect(Object.keys(request.tools)).not.toContain('research_dispatch')
    await expect(first.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: 'ses_main', id: 'old-main', input: {} }))
      .rejects.toMatchObject({ metadata: { code: 'research_role_violation' } })
    const nodes = await first.call('research_query', child, { collection: 'nodes' })
    expect(nodes.items.some((item: any) => item.node_id === 'X-001')).toBe(true)
    release()
  })

  test("a missed terminal event is reconciled once from the exact kickoff", async () => {
    const first = await load()
    await first.command("auto", "ses_main", { goal: "event gap fixture" })
    await first.call("research_propose", "ses_main", { question: "Q?", why_now: "w", plan: "p", root_reason: "r" })
    await first.call("research_dispatch", "ses_main", { node_id: "X-001" })
    const child = first.created[0]!
    const kickoff = first.said(child).find(item => item.resume).id
    first.turn.start(child)
    await first.call("research_finish", child, { state: "finished", summary: "finished in the gap" })
    cleanups.pop()!()
    const second = await load({ hostSetup: host => {
      const nativeWait = host.ctx.session.wait
      const nativeContext = host.ctx.session.context
      host.ctx.session.wait = async (input: any) => input.sessionID === child ? undefined : nativeWait(input)
      host.ctx.session.context = async ({ sessionID }: { sessionID: string }) => sessionID === child
        ? [{ type: 'synthetic', id: kickoff }, { type: 'assistant', content: [{ type: 'text', text: 'done' }] },
           { type: 'idle', outcome: 'succeeded' }] : nativeContext({ sessionID })
    } })
    await until(async () => (await second.page('tasks')).items[0].state === 'finished', 'recovered node terminal')
    const sentBefore = second.said(child).filter(item => item.resume).length
    second.events.push({ id: 'late-terminal', type: 'session.execution.succeeded', data: { sessionID: child } })
    await Bun.sleep(100)
    expect(second.said(child).filter(item => item.resume)).toHaveLength(sentBefore)
    expect((await second.page('tasks')).items[0].state).toBe('finished')
  })

  test("a lost kickoff response leaves one unverified launch without a second session", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "uncertain delivery" })
    await host.call("research_propose", "ses_main", { question: "Q?", why_now: "w", plan: "p", root_reason: "r" })
    const original = host.ctx.session.synthetic
    host.ctx.session.synthetic = async (input: any) => {
      const value = await original(input)
      if (input.sessionID !== "ses_main" && input.resume) throw new Error("host response lost")
      return value
    }
    const first = await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(first.accepted).toBe(true)
    expect(first.state).toBe("unverified")
    expect(host.created).toHaveLength(1)
    expect(host.said(host.created[0]!).filter(item => item.resume)).toHaveLength(1)
    const retry = await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
    expect(retry.state).toBe("unverified")
    expect(host.created).toHaveLength(1)
    expect(host.said(host.created[0]!).filter(item => item.resume)).toHaveLength(1)
    host.turn.end(host.created[0]!)
    await Bun.sleep(100)
    expect(host.said(host.created[0]!).filter(item => item.resume)).toHaveLength(1)
    const originalMessage = host.said(host.created[0]!).find(item => item.resume).id
    host.ctx.session.synthetic = original
    const checked = await host.command("recover", "ses_main", { kind: "task", task_id: first.task_id, reason: "host exit inspected" })
    expect(checked.message).toContain("人工退出核对")
    const restarted = await host.command("retry", "ses_main", { task_id: first.task_id, reason: "repeat after check" })
    expect(restarted.message).toContain("重新受理")
    const messages = host.said(host.created[0]!).filter(item => item.resume)
    expect(messages).toHaveLength(2)
    expect(messages[1].id).not.toBe(originalMessage)
  })

  test("a restarted host does not start a second turn for an unverified node", async () => {
    const first = await load()
    await first.command("auto", "ses_main", { goal: "restart fixture" })
    await first.call("research_propose", "ses_main", { question: "Q?", why_now: "w", plan: "p", root_reason: "r" })
    await first.call("research_dispatch", "ses_main", { node_id: "X-001" })
    const child = first.created[0]!
    cleanups.pop()!()
    simulateDeadHost()
    const second = await load()
    await until(async () => (await second.page("tasks")).items[0].state === "unverified", "unverified node")
    await second.command("resume", "ses_main")
    expect((await second.page("tasks")).items[0].state).toBe("unverified")
    expect(second.said(child).filter(item => item.resume)).toHaveLength(0)
  })

  test("a real restart also reconciles in-flight work when the project was already paused", async () => {
    const first = await load()
    await first.command('auto', 'ses_main', { goal: 'paused restart fixture' })
    await first.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await first.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    await first.command('pause', 'ses_main')
    cleanups.pop()!()
    simulateDeadHost()
    const second = await load()
    await until(async () => (await second.page('tasks')).items[0].state === 'unverified', 'paused restart reconciliation')
    expect((await second.summary()).run.state).toBe('paused')
  })

  test("a user message in a research session is kept as guidance", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    await host.hooks.get("prompt")!({ sessionID: "ses_main", prompt: { text: "先测小事务" } })
    const notes = await host.rpc().board({ view: "reference_get", directory: project, fields: { ref: "N-001" } })
    expect(JSON.stringify(notes)).toContain("用户指导：先测小事务")
  })

  test("auto with text on an existing project keeps the goal and records the text as guidance", async () => {
    const host = await load()
    const first = await host.command("auto", "ses_main", { goal: "Find X" })
    expect(first.message).not.toContain("补充指令")
    const pub = await host.call("research_publish", "ses_main", {
      status: "complete", summary: "final", items: [{ item_id: "report", kind: "text", content: { text: "partial" } }],
    })
    await host.call("research_conclude", "ses_main", {
      summary: "ended", final_ref: `pub/${pub.publication_id}#report`, outcome: "partial", gaps: ["Y untested"],
      review: { status: "unreviewed", refs: [], limitations: ["Not reviewed"] },
    })
    host.turn.end("ses_main")
    await host.call("research_query", "ses_main", { collection: "nodes" })
    const restarted = await host.command("auto", "ses_main", { goal: "重点验证 Y，放弃 Z" })
    expect(restarted.message).toContain("补充指令已记为用户指导")
    const summary = await host.summary()
    expect(summary.project.goal).toBe("Find X")
    expect(summary.run.state).toBe("running")
    const kickoff = host.said("ses_main").at(-1)
    expect(kickoff).toMatchObject({ resume: true })
    expect(kickoff.text).toContain("目标：Find X")
    expect(kickoff.text).toContain("重点验证 Y，放弃 Z")
    // Filed in the new work segment, so later requests carry it, not only the kickoff message.
    const request = modelRequest("ses_main")
    await host.hooks.get("context")!(request)
    expect(contextText(request)).toContain("用户指导：重点验证 Y，放弃 Z")
  })

  test("auto with text is refused, not dropped, while research runs or is paused, or when too long", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    const running = await host.command("auto", "ses_main", { goal: "改看 Y" })
    expect(running.error).toContain("补充指令没有发送")
    await host.command("pause", "ses_main")
    const paused = await host.command("auto", "ses_main", { goal: "改看 Y" })
    expect(paused.error).toContain("补充指令没有发送")
    expect(paused.error).toContain("/research auto")
    const long = await host.command("auto", "ses_main", { goal: "长".repeat(2001) })
    expect(long.error).toContain("超过 2000 字")
    // Notes of the open work segment reach the research context; the typed one shows that, the refused text is absent.
    await host.hooks.get("prompt")!({ sessionID: "ses_main", prompt: { text: "先测小事务" } })
    const request = modelRequest("ses_main")
    await host.hooks.get("context")!(request)
    const context = contextText(request)
    expect(context).toContain("用户指导：先测小事务")
    expect(context).not.toContain("改看 Y")
    expect(context).not.toContain("长长长")
  })

  test("auto with text waits until the coordinator's current turn ends", async () => {
    const host = await load()
    await host.command("init", "ses_main", { goal: "Find X" })
    host.turn.start("ses_main")
    await Bun.sleep(30)
    const busy = await host.command("auto", "ses_main", { goal: "按方案 B 继续" })
    expect(busy.error).toContain("这一轮还没结束")
    expect((await host.summary()).run.state).toBe("manual")
    host.turn.end("ses_main")
    await Bun.sleep(30)
    const started = await host.command("auto", "ses_main", { goal: "按方案 B 继续" })
    expect(started.message).toContain("补充指令已记为用户指导")
    expect(host.said("ses_main").at(-1).text).toContain("按方案 B 继续")
  })

  test("a guidance note that cannot be filed still starts the run and says so", async () => {
    const host = await load()
    await host.command("init", "ses_main", { goal: "Find X" })
    const request = StorageClient.prototype.request
    StorageClient.prototype.request = function (this: StorageClient, method: string, ...rest: any[]) {
      return method === "note" ? Promise.reject(new Error("ledger unavailable")) : (request as any).call(this, method, ...rest)
    } as typeof request
    cleanups.push(() => (StorageClient.prototype.request = request))
    const started = await host.command("auto", "ses_main", { goal: "按方案 B 继续" })
    expect(started.message).toContain("没能记入研究记录")
    expect(started.message).toContain("ledger unavailable")
    expect((await host.summary()).run.state).toBe("running")
    expect(host.said("ses_main").at(-1).text).toContain("按方案 B 继续")
  })

  test("a turn that starts while auto sets up the run gets no kickoff, and the result says so", async () => {
    const host = await load()
    await host.command("init", "ses_main", { goal: "Find X" })
    const request = StorageClient.prototype.request
    StorageClient.prototype.request = async function (this: StorageClient, method: string, ...rest: any[]) {
      if (method === "note") {
        // The user presses Enter in the main session after the busy check has passed.
        host.turn.start("ses_main")
        await Bun.sleep(30)
      }
      return (request as any).call(this, method, ...rest)
    } as typeof request
    cleanups.push(() => (StorageClient.prototype.request = request))
    const started = await host.command("auto", "ses_main", { goal: "按方案 B 继续" })
    expect(started.message).toContain("启动消息没有发出")
    expect(host.said("ses_main").some((item) => item.text.includes("按方案 B 继续"))).toBe(false)
    const context = modelRequest("ses_main")
    await host.hooks.get("context")!(context)
    expect(contextText(context)).toContain("用户指导：按方案 B 继续")
  })

  test("an instance opened in a subdirectory never drives the project", async () => {
    const root = await load()
    await root.command("auto", "ses_main", { goal: "Find X" })
    const sub = join(project, "sub")
    mkdirSync(sub)
    const other = await load({ directory: sub })
    const request = modelRequest("ses_main")
    await other.hooks.get("context")!(request)
    expect(JSON.stringify(request.system)).toContain("research_session_location")
    expect(Object.keys(request.tools)).toHaveLength(0)
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

/** What a default specialist is told about the stall window and the absolute cap. */
function specialistLimitSentence(): string {
  return `连续 ${SPECIALIST_STALL_MS / 60_000} 分钟没有新的推进会被停下；即使一直有推进，${SPECIALIST_TIMEOUT_MS / 60_000} 分钟后也会停下。请边做边把发现写进回复，这样停下时仍有可用文字。`
}

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
    return /工作目录：(.+?)。/.exec(host.said(host.created[0]!).at(-1).text)![1]!
  }

  /** The n-th session created after the node, once it has received its task. */
  async function specialist(host: Awaited<ReturnType<typeof load>>, index: number): Promise<string> {
    await until(() => host.created.length > index && host.said(host.created[index]!).length > 0, `specialist ${index}`)
    return host.created[index]!
  }

  for (const mode of ['membership-error', 'membership-timeout', 'handoff']) test(`blind system stays private during ${mode}`, async () => {
    const first = await load()
    const workspace = await nodeAtWork(first)
    mkdirSync(join(workspace, 'output'), { recursive: true })
    writeFileSync(join(workspace, 'output', 'blind.md'), 'assigned evidence')
    const publication = await first.call('research_publish', first.created[0]!, { status: 'complete', summary: 'report',
      items: [{ item_id: 'report', kind: 'report', source_path: 'output/blind.md' }] })
    const pending = first.call('research_delegate', first.created[0]!, { ...TASK, context_mode: 'blind', inputs: [`pub/${publication.publication_id}#report`] })
    const child = await specialist(first, 1)
    const original = StorageClient.prototype.request
    let release: (() => void) | undefined
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
      if (mode === 'handoff' && method === 'controller_attach' || mode !== 'handoff' && method === 'session_membership' && fields?.session_id === child) {
        if (mode === 'membership-error') return Promise.reject(new Error('membership unavailable'))
        return new Promise((_resolve, reject) => { release = () => reject(new Error('injected unavailable control')) })
      }
      return original.call(this, method, fields, operation)
    })
    try {
      if (mode === 'handoff') await load()
      const request = modelRequest(child)
      request.system.push({ type: 'text', text: 'PRIVATE_DEFAULT_WORKSPACE_AND_AGENTS' })
      await first.hooks.get('context')!(request)
      expect(contextText(request)).not.toContain('PRIVATE_DEFAULT_WORKSPACE_AND_AGENTS')
      expect(Object.keys(request.tools)).not.toContain('shell')
    } finally {
      release?.(); spy.mockRestore()
      first.reply(child, 'partial report')
      await pending
    }
  })

  test("a node delegates a read-only specialist and gets its report back", async () => {
    const host = await load()
    const workspace = await nodeAtWork(host)
    host.turn.start(host.created[0]!)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
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
    expect(kickoff.text).toContain(specialistLimitSentence())
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
    const pending = host.call("research_delegate", host.created[0]!, TASK, controller.signal)
    const child = await specialist(host, 1)
    controller.abort()
    const result = await pending
    expect(result.state).toBe("cancelled")
    expect(host.interrupted).toContain(child)
    const again = host.call("research_delegate", host.created[0]!, TASK)
    host.reply(await specialist(host, 2), "第二次复核的报告")
    expect((await again).state).toBe("completed")
  })

  test('an unexpected host error after specialist submission still interrupts the live child', async () => {
    const host = await load()
    await nodeAtWork(host)
    const original = host.ctx.session.synthetic
    host.ctx.session.synthetic = async (input: any) => {
      const result = await original(input)
      if (input.sessionID !== host.created[0] && input.resume) throw new Error('lost specialist submission response')
      return result
    }
    const result = await host.call('research_delegate', host.created[0]!, TASK)
    expect(host.interrupted).toContain(host.created[1]!)
    expect(result).toMatchObject({ state: 'incomplete', exit_verified: true })
  })

  test('specialist pre-create failure is incomplete with no unknown executor or occupied slot', async () => {
    const host = await load()
    await nodeAtWork(host)
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
      if (method === 'status' && fields?.session_id === host.created[0]) return Promise.reject(new Error('parent context unavailable'))
      return original.call(this, method, fields, operation)
    })
    try {
      const result = await host.call('research_delegate', host.created[0]!, TASK)
      expect(result).toMatchObject({ state: 'incomplete', exit_verified: true })
      expect(host.created).toHaveLength(1)
    } finally { spy.mockRestore() }
  })

  test('a blind specialist can read only its assigned input while controller recovery is pending', async () => {
    const first = await load()
    const workspace = await nodeAtWork(first)
    mkdirSync(join(workspace, 'output'), { recursive: true })
    writeFileSync(join(workspace, 'output', 'report.md'), 'assigned evidence for review')
    const publication = await first.call('research_publish', first.created[0]!, { status: 'complete', summary: 'report',
      items: [{ item_id: 'report', kind: 'report', source_path: 'output/report.md' }] })
    const pending = first.call('research_delegate', first.created[0]!, { ...TASK, context_mode: 'blind', inputs: [`pub/${publication.publication_id}#report`] })
    const child = await specialist(first, 1)
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation) {
      if (method === 'controller_attach') return Promise.reject(new Error('operator recovery required'))
      return original.call(this, method, fields, operation)
    })
    try {
      const second = await load()
      await second.hooks.get('tool.execute.before')!({ tool: 'research_read_input', sessionID: child, id: 'safe-input', input: { input_id: 'input-1' } })
      const input = await second.call('research_read_input', child, { input_id: 'input-1' })
      expect(JSON.stringify(input)).toContain('assigned evidence for review')
      await expect(second.call('research_read_input', child, { input_id: 'input-other' })).rejects.toThrow()
      await expect(second.call('research_read_input', 'ordinary', { input_id: 'input-1' })).rejects.toThrow('没有关联')
      const request = modelRequest(child)
      request.system.push({ type: 'text', text: 'private workspace AGENTS and project goal' })
      await second.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toContain('research_read_input')
      expect(Object.keys(request.tools)).not.toContain('shell')
      expect(contextText(request)).not.toContain('private workspace AGENTS')
    } finally {
      spy.mockRestore()
      first.reply(child, 'review interrupted by controller handoff')
      await pending
    }
  })

  test("stopping research also requests interruption of a running specialist", async () => {
    const host = await load()
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK).catch(() => null)
    const child = await specialist(host, 1)
    const result = await host.command('stop', 'ses_main')
    expect(result.error).toBeUndefined()
    expect(host.interrupted).toContain(child)
    expect((await host.summary()).run.state).toBe('stopped')
    await pending
  })

  test("a batch runs specialists side by side up to the node's limit; beyond it nothing starts", async () => {
    const host = await load()
    await nodeAtWork(host)
    await expect(host.call("research_delegate_batch", host.created[0]!, { tasks: [TASK, TASK, TASK] })).rejects.toThrow("1 到 2")
    expect(host.created).toHaveLength(1)
    const pending = host.call("research_delegate_batch", host.created[0]!, { tasks: [TASK, { ...TASK, label: "另一视角" }] })
    const [one, two] = [await specialist(host, 1), await specialist(host, 2)]
    await expect(host.call("research_delegate", host.created[0]!, TASK)).rejects.toThrow("fan-out limit")
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
    await host.command("context-policy", "ses_main", { policy: "r1" })
    mkdirSync(join(workspace, "output"), { recursive: true })
    writeFileSync(join(workspace, "output", "report.md"), "# 结果\nWAL 快 3 倍\n")
    const pub = await host.call("research_publish", host.created[0]!, {
      status: "complete", summary: "r", items: [{ item_id: "report", kind: "report", source_path: "output/report.md" }],
    })
    const input = `pub/${pub.publication_id}#report`
    const blind = { ...TASK, label: "盲评结果", context_mode: "blind" }
    await expect(host.call("research_delegate", host.created[0]!, { ...blind, inputs: [] })).rejects.toThrow("Blind review requires")
    await expect(host.call("research_delegate", host.created[0]!, { ...blind, inputs: [input], tool_scope: ["read"] })).rejects.toThrow("tool_scope")
    expect(host.created).toHaveLength(1)

    const pending = host.call("research_delegate", host.created[0]!, { ...blind, inputs: [input] })
    const child = await specialist(host, 1)
    const rules = host.permissions.get(child)!
    expect(rules.filter((rule) => rule.effect === "allow").map((rule) => rule.action)).toEqual(["research_read_input"])
    const kickoff = host.said(child).at(-1).text
    expect(kickoff).toContain("盲评专家")
    expect(kickoff).toContain(specialistLimitSentence())
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
    await until(async () => {
      const preview = await host.rpc().board({ view: "context_preview", directory: project, fields: { preview_session_id: child } })
      const row = preview.last_recorded
      return row?.policy_version === "memory-r1-v1" && row.selection.context_mode === "blind"
    }, "saved R1 blind context mode")

    expect((await host.call("research_read_input", child, { input_id: "input-1" })).text).toContain("WAL 快 3 倍")
    await expect(host.call("research_query", child, { collection: "nodes" })).rejects.toThrow("Blind reviewers")
    host.reply(child, "盲评：数据支持结论。")
    expect((await pending).state).toBe("completed")
  })

  test("a restart keeps an active specialist unverified until its exit is checked", async () => {
    const first = await load()
    await nodeAtWork(first)
    void first.call("research_delegate", first.created[0]!, TASK).catch(() => {})
    const child = await specialist(first, 1)
    cleanups.pop()!()
    simulateDeadHost()
    const second = await load()
    await until(async () => (await second.page("specialists")).items[0]?.state === "unverified", "unverified after restart")
    expect(second.interrupted).not.toContain(child)
    expect((await second.rpc().session({ sessionID: child })).role).toBe("specialist")
  })

  test("warm reload reattaches a specialist with its original scope and deadline", async () => {
    const first = await load()
    await nodeAtWork(first)
    const pending = first.call("research_delegate", first.created[0]!, { ...TASK, tool_scope: ['read'] }).catch(() => {})
    const child = await specialist(first, 1)
    const kickoffID = first.said(child).find(item => item.resume).id
    const launchState = () => {
      const code = `import json,sqlite3,sys\ndb=sqlite3.connect(sys.argv[1])\nr=db.execute("SELECT state,details FROM workflow_intents WHERE kind='specialist_launch' AND json_extract(details,'$.session_id')=?", (sys.argv[2],)).fetchone()\nprint(json.dumps({'state':r[0], **json.loads(r[1])}) if r else '{}')`
      const result = spawnSync('python3', ['-c', code, join(project, '.research', 'state.sqlite3'), child], { encoding: 'utf8' })
      if (result.status !== 0) throw new Error(result.stderr)
      return JSON.parse(result.stdout)
    }
    await until(() => launchState().state === 'submitted', 'specialist kickoff receipt')
    const originalLaunch = launchState()
    let watched = false
    let complete!: () => void
    const waiting = new Promise<void>(resolve => { complete = resolve })
    cleanups.pop()!()
    const second = await load({ hostSetup: host => {
      const nativeWait = host.ctx.session.wait
      const nativeContext = host.ctx.session.context
      host.ctx.session.wait = async ({ sessionID }: { sessionID: string }) => {
        if (sessionID === child) { watched = true; await waiting }
        else await nativeWait({ sessionID })
      }
      host.ctx.session.context = async ({ sessionID }: { sessionID: string }) => sessionID === child
        ? [{ type: 'synthetic', id: kickoffID }, { type: 'assistant', content: [{ type: 'text', text: '恢复后的专家报告' }] },
           { type: 'idle', outcome: 'succeeded' }] : nativeContext({ sessionID })
    } })
    await until(() => watched, 'reattached specialist watcher')
    expect(launchState().deadline_at).toBe(originalLaunch.deadline_at)
    expect(launchState().tools).toEqual(['read'])
    const request = modelRequest(child)
    await second.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toEqual(['read'])
    complete()
    await until(async () => (await second.page('specialists')).items[0]?.state === 'completed', 'reattached specialist result')
    expect(second.interrupted).not.toContain(child)
    first.reply(child, 'original waiter unwinds')
    await pending
  })

  const OPENING_LINE = "I'll start by exploring the workspace structure and then reading the key files."
  const AUDIT_REASONING = "The actual audit findings are here..."

  /** Issue #27's transcript tail: an opening progress line, later reasoning, then the interrupt. */
  function issue27Tail(idle: any = { type: "idle", outcome: "interrupted" }) {
    return [
      { type: "assistant", content: [{ type: "text", text: OPENING_LINE }] },
      { type: "assistant", content: [{ type: "reasoning", text: AUDIT_REASONING }] },
      idle,
    ]
  }

  /** Issue #27's transcript: an opening line, later reasoning, then the timeout interrupt. */
  function timeoutTranscript(host: Awaited<ReturnType<typeof load>>, timeoutMs = 200) {
    host.ctx.options.specialistTimeoutMs = timeoutMs
    const native = host.ctx.session.context
    const shaped = new Set<string>()
    host.ctx.session.context = async (input: { sessionID: string }) => {
      const messages = await native(input)
      const idle = messages.at(-1)
      if (!shaped.has(input.sessionID) && idle?.type === "idle" && idle.outcome === "interrupted") {
        shaped.add(input.sessionID)
        messages.pop()
        messages.push(...issue27Tail(idle))
      }
      return messages
    }
  }

  /** Production keeps `SUMMARY_GRACE_MS` / `STOP_GRACE_MS`. These tests must not wait them out. */
  function shortGrace(_host: Awaited<ReturnType<typeof load>>, summaryMs: number, stopMs = summaryMs) {
    specialistGraceForTest.summaryMs = summaryMs
    specialistGraceForTest.stopMs = stopMs
  }

  test("a timed-out specialist is asked for one tool-free summary, archived as unverified", async () => {
    const host = await load({ hostSetup: timeoutTranscript })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "timeout summary", 4000)
    const prompt = host.said(child).at(-1)
    expect(prompt.resume).toBe(true)
    expect(prompt.text).toContain("不要使用任何工具")
    expect(prompt.text).toContain("推理")
    expect(prompt.text).toContain("工具结果")
    const request = modelRequest(child)
    await host.hooks.get("context")!(request)
    expect(Object.keys(request.tools)).toEqual([])
    host.reply(child, "已看到 8 个事件里有 3 个与原始计数不符。")
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，已收回部分报告",
      child_session_id: child,
    })
    expect(result.result.session_id).toBe(child)
    expect(result.result.incomplete).toBe(true)
    expect(result.result.unverified).toBe(true)
    expect(result.result.preview).toContain("【未完成·未核实】")
    expect(result.result.preview).toContain("原始计数不符")
    expect(result.result.preview).not.toContain("I'll start")
    const archived = JSON.parse(readFileSync(result.result.report_path, "utf8"))
    expect(archived).toMatchObject({ stop_reason: "interrupted", incomplete: true, unverified: true })
    expect(archived.output[0].text).toContain("【未完成·未核实】")
    expect(archived.output[0].text).toContain("原始计数不符")
    expect(archived.output[0].text).not.toContain("I'll start")
  })

  test("a timed-out specialist with an empty summary records no usable report and keeps the session", async () => {
    const host = await load({ hostSetup: timeoutTranscript })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "timeout summary", 4000)
    host.reply(child, "")
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告",
      child_session_id: child,
    })
    expect(result.error).not.toBe("达到专家时间上限，已收回部分报告")
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain("I'll start")
    expect(result.result.report_path).toBeUndefined()
  })

  test("a completed specialist keeps its report when the last message is reasoning only", async () => {
    const host = await load({
      hostSetup: (raw) => {
        const native = raw.ctx.session.context
        raw.ctx.session.context = async (input: { sessionID: string }) => {
          const messages = await native(input)
          const idle = messages.at(-1)
          if (raw.created.includes(input.sessionID) && input.sessionID !== raw.created[0]
            && idle?.type === "idle" && idle.outcome === "succeeded") {
            const report = messages.find((item: any) => item?.type === "assistant"
              && (item.content ?? []).some((part: any) => part?.type === "text" && String(part.text ?? "").trim()))
            return [
              ...messages.filter((item: any) => item?.type !== "assistant" && item !== idle),
              report ?? { type: "assistant", content: [{ type: "text", text: "FINAL REPORT" }] },
              { type: "assistant", content: [{ type: "reasoning", text: "still thinking" }] },
              idle,
            ]
          }
          return messages
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    host.reply(child, "FINAL REPORT")
    const result = await pending
    expect(result).toMatchObject({ state: "completed", exit_verified: true, child_session_id: child })
    expect(result.result.preview).toBe("FINAL REPORT")
    expect(result.error).toBeFalsy()
  })

  /** Issue #27's opening line, then reasoning only, kept in front of the turn's idle marker. */
  function openingBeforeIdle(host: Awaited<ReturnType<typeof load>>, extra: any[] = []) {
    const native = host.ctx.session.context
    host.ctx.session.context = async (input: { sessionID: string }) => {
      const messages = [...await native(input)]
      const idle = messages.at(-1)
      const node = host.created[0]
      if (node && input.sessionID !== node && host.created.includes(input.sessionID) && idle?.type === "idle") {
        messages.pop()
        messages.push(
          { type: "assistant", content: [{ type: "text", text: OPENING_LINE }, ...extra] },
          { type: "assistant", content: [{ type: "reasoning", text: "real findings only in reasoning" }] },
          idle,
        )
      }
      return messages
    }
  }

  test("a failed turn's opening line is archived unverified, not as a clean report", async () => {
    const host = await load({ hostSetup: openingBeforeIdle })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    host.reply(child, "", "failed")
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "专家会话以「failed」结束",
      child_session_id: child,
    })
    expect(result.state).not.toBe("completed")
    expect(result.result.incomplete).toBe(true)
    expect(result.result.unverified).toBe(true)
    expect(result.result.preview).toContain("【未完成·未核实】")
    expect(result.result.preview).toContain(OPENING_LINE)
    expect(String(result.result.preview).startsWith("【未完成·未核实】")).toBe(true)
    const archived = JSON.parse(readFileSync(result.result.report_path, "utf8"))
    expect(archived).toMatchObject({ stop_reason: "failed", incomplete: true, unverified: true })
    expect(archived.output[0].text.startsWith("【未完成·未核实】")).toBe(true)
    expect(archived.output[0].text).toContain(OPENING_LINE)
  })

  test("a failed specialist turn archives its findings marked unverified", async () => {
    const finding = "失败前写下的发现：有三处与原始计数不符。"
    const host = await load()
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    host.reply(child, finding, "failed")
    const result = await pending
    expect(result.state).not.toBe("completed")
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "专家会话以「failed」结束",
      child_session_id: child,
    })
    expect(result.result.incomplete).toBe(true)
    expect(result.result.unverified).toBe(true)
    expect(result.result.preview).toContain("【未完成·未核实】")
    expect(result.result.preview).toContain(finding)
    const archived = JSON.parse(readFileSync(result.result.report_path, "utf8"))
    expect(archived.unverified).toBe(true)
    expect(archived.incomplete).toBe(true)
    expect(archived.output[0].text.startsWith("【未完成·未核实】")).toBe(true)
    expect(archived.output[0].text).toContain(finding)
  })

  test("a parent cancel does not archive the opening progress line", async () => {
    const host = await load({ hostSetup: openingBeforeIdle })
    await nodeAtWork(host)
    const controller = new AbortController()
    const pending = host.call("research_delegate", host.created[0]!, TASK, controller.signal)
    await specialist(host, 1)
    controller.abort()
    const result = await pending
    expect(result).toMatchObject({
      state: "cancelled",
      error: "专家这一轮被中断",
    })
    expect(result.result.preview ?? "").toBe("")
    expect(result.result.report_path).toBeUndefined()
    expect(JSON.stringify(result.result)).not.toContain(OPENING_LINE)
  })

  test("a succeeded turn whose only text precedes a tool call is not a clean report", async () => {
    const host = await load({
      hostSetup: (raw) => openingBeforeIdle(raw, [{
        type: "tool",
        id: "call_read",
        name: "read",
        state: { status: "completed", input: { path: "notes.md" }, content: [{ type: "text", text: "file body" }] },
      }]),
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    host.reply(child, "", "succeeded")
    const result = await pending
    expect(result.state).not.toBe("completed")
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      child_session_id: child,
    })
    expect(result.error).toContain("进度")
    expect(result.result.preview).toContain("【可能只是进度说明】")
    expect(result.result.preview).toContain(OPENING_LINE)
    expect(result.result.incomplete).toBe(true)
    expect(result.result.unverified).toBeUndefined()
    const archived = JSON.parse(readFileSync(result.result.report_path, "utf8"))
    expect(archived.output[0].text).toContain("【可能只是进度说明】")
    expect(archived.output[0].text).toContain(OPENING_LINE)
    expect(archived.incomplete).toBe(true)
    expect(archived.unverified).toBeUndefined()
  })

  test("a progress-only succeeded turn keeps assistant text written after the kickoff", async () => {
    const finding = "沿途发现：八个事件里有三处与原始计数不符。"
    const beforeKickoff = "启动前的旧文字不应进入报告。"
    const host = await load({
      hostSetup: (raw) => {
        const native = raw.ctx.session.context
        raw.ctx.session.context = async (input: { sessionID: string }) => {
          const messages = [...await native(input)]
          const idle = messages.at(-1)
          const node = raw.created[0]
          if (node && input.sessionID !== node && raw.created.includes(input.sessionID) && idle?.type === "idle") {
            messages.pop()
            messages.unshift({ type: "assistant", content: [{ type: "text", text: beforeKickoff }] })
            messages.push(
              { type: "assistant", content: [{ type: "text", text: finding }] },
              {
                type: "assistant",
                content: [
                  { type: "text", text: OPENING_LINE },
                  {
                    type: "tool",
                    id: "call_read",
                    name: "read",
                    state: { status: "completed", input: { path: "notes.md" }, content: [{ type: "text", text: "file body" }] },
                  },
                ],
              },
              idle,
            )
          }
          return messages
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    host.reply(child, "", "succeeded")
    const result = await pending
    expect(result.state).not.toBe("completed")
    expect(result).toMatchObject({ state: "incomplete", exit_verified: true, child_session_id: child })
    expect(result.result.incomplete).toBe(true)
    expect(result.result.unverified).toBeUndefined()
    const preview = String(result.result.preview)
    expect(preview.startsWith("【可能只是进度说明】")).toBe(true)
    expect(preview.indexOf(finding)).toBeGreaterThan(0)
    expect(preview.indexOf(OPENING_LINE)).toBeGreaterThan(preview.indexOf(finding))
    expect(preview).not.toContain(beforeKickoff)
    const archived = JSON.parse(readFileSync(result.result.report_path, "utf8"))
    const body = String(archived.output[0].text)
    expect(archived.incomplete).toBe(true)
    expect(archived.unverified).toBeUndefined()
    expect(body.indexOf(finding)).toBeGreaterThan(0)
    expect(body.indexOf(OPENING_LINE)).toBeGreaterThan(body.indexOf(finding))
    expect(body).not.toContain(beforeKickoff)
  })

  function refuseSummary(host: Awaited<ReturnType<typeof load>>, error: Error) {
    const original = host.ctx.session.synthetic
    let resumes = 0
    host.ctx.session.synthetic = async (input: any) => {
      const node = host.created[0]
      const summary = node && input.sessionID !== node && input.resume && ++resumes >= 2
      if (summary) throw error
      return original(input)
    }
  }

  test("a refused cap summary keeps the cap sentence with the refusal", async () => {
    const host = await load({ hostSetup: timeoutTranscript })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    host.events.end()
    await Bun.sleep(30)
    const result = await pending
    expect(result.error).toContain("达到专家时间上限，没有可用报告")
    expect(result.error).toContain("事件流中断或需核对，超时补问没有发出；先 /research auto。已有进度句不是报告。")
    expect(String(result.error).indexOf("达到专家时间上限，没有可用报告"))
      .toBeLessThan(String(result.error).indexOf("事件流中断或需核对"))
  })

  test("a refused stall summary keeps the stall sentence with the refusal", async () => {
    const host = await load({
      hostSetup: (raw) => {
        raw.ctx.options.specialistStallMs = 200
        raw.ctx.options.specialistTimeoutMs = 60_000
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    host.events.end()
    const result = await pending
    expect(result.error).toContain("专家一段时间没有新的推进，没有可用报告")
    expect(result.error).toContain("事件流中断或需核对，超时补问没有发出；先 /research auto。已有进度句不是报告。")
    expect(String(result.error).indexOf("专家一段时间没有新的推进，没有可用报告"))
      .toBeLessThan(String(result.error).indexOf("事件流中断或需核对"))
    expect(result.error).not.toContain("达到专家时间上限")
  })

  test("a summary refusal matches research_events_need_check instead of the Chinese sentence", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        refuseSummary(raw, Object.assign(new Error("controller check refused the send"), { code: "research_events_need_check" }))
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    const result = await pending
    expect(result.error).toContain("达到专家时间上限，没有可用报告")
    expect(result.error).toContain("事件流中断或需核对，超时补问没有发出；先 /research auto。已有进度句不是报告。")
  })

  test("the Chinese event-gap sentence without that code is not a summary refusal", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        refuseSummary(raw, new Error("研究事件需核对；停止和暂停仍可用，核对后可 /research auto"))
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    const result = await pending
    expect(result.error).toBe("达到专家时间上限，没有可用报告")
    expect(result.error).not.toContain("事件流中断或需核对")
  })

  test("a summary refused by an event-stream gap names the gap", async () => {
    const host = await load({ hostSetup: timeoutTranscript })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    host.events.end()
    await Bun.sleep(30)
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告\n事件流中断或需核对，超时补问没有发出；先 /research auto。已有进度句不是报告。",
    })
    expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
    expect(result.result.report_path).toBeUndefined()
  })

  test("a summary refused because the project is paused names the pause", async () => {
    const host = await load({ hostSetup: (raw) => timeoutTranscript(raw, 800) })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    expect((await host.command("pause", "ses_main")).error).toBeUndefined()
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告\n研究已暂停，超时补问没有发出；恢复前已有进度句不是报告。",
    })
    expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
  })

  test("a summary refused because the budget is exhausted names the budget", async () => {
    const host = await load({ hostSetup: (raw) => timeoutTranscript(raw, 800) })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    await specialist(host, 1)
    expect((await host.command("budget", "ses_main", { limit: "turns", value: "1", reason: "block the timeout summary" })).error).toBeUndefined()
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告\n自主调度预算已到限，超时补问没有发出；先用 /research budget 调整额度。已有进度句不是报告。",
    })
    expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
  })

  /** Later interrupt calls are recorded but do not end the summary turn. */
  function ignoreSummaryStop(host: Awaited<ReturnType<typeof load>>) {
    const native = host.ctx.session.interrupt
    host.ctx.session.interrupt = async (input: { sessionID: string }) => {
      if (host.interrupted.includes(input.sessionID)) {
        host.interrupted.push(input.sessionID)
        return { interrupted: true }
      }
      return native(input)
    }
  }

  test("a summary turn that ignores the interrupt stays unverified", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        shortGrace(raw, 1_000, 40)
        ignoreSummaryStop(raw)
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "timeout summary", 4000)
    const result = await pending
    expect(host.interrupted.filter((id) => id === child).length).toBeGreaterThan(1)
    expect(result).toMatchObject({
      state: "unverified",
      exit_verified: false,
      error: "专家会话被中断后没有按时停下，退出未核实",
      child_session_id: child,
    })
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain("I'll start")
    expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
    expect((await host.page("specialists")).items[0]?.state).toBe("unverified")
  }, 15_000)

  test("a summary send that throws after the turn starts stays unverified when stop fails", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        shortGrace(raw, 1_000, 40)
        ignoreSummaryStop(raw)
        const original = raw.ctx.session.synthetic
        let resumes = 0
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          const summary = node && input.sessionID !== node && input.resume && ++resumes >= 2
          const result = await original(input)
          if (summary) throw new Error("summary accepted then host failed")
          return result
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    const result = await pending
    expect(host.interrupted.filter((id) => id === child).length).toBeGreaterThan(1)
    expect(result).toMatchObject({
      state: "unverified",
      exit_verified: false,
      error: "专家会话被中断后没有按时停下，退出未核实",
      child_session_id: child,
    })
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain("I'll start")
    expect(result.result.report_path).toBeUndefined()
  }, 15_000)

  test("a summary send that never resolves is unverified and keeps the session", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        shortGrace(raw, 40)
        const original = raw.ctx.session.synthetic
        let resumes = 0
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          if (node && input.sessionID !== node && input.resume && ++resumes >= 2) return new Promise(() => {})
          return original(input)
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    const result = await pending
    expect(result).toMatchObject({
      state: "unverified",
      exit_verified: false,
      error: "专家会话被中断后没有按时停下，退出未核实",
      child_session_id: child,
    })
    expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
    expect(result.result.report_path).toBeUndefined()
    expect((await host.page("specialists")).items[0]?.state).toBe("unverified")
  }, 15_000)

  test("a summary send that lands after the grace is unverified and its turn is stopped", async () => {
    let releaseSend = () => {}
    let opened = false
    const gate = new Promise<void>((resolve) => {
      releaseSend = () => {
        if (opened) return
        opened = true
        resolve()
      }
    })
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        shortGrace(raw, 40)
        const original = raw.ctx.session.synthetic
        let resumes = 0
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          if (node && input.sessionID !== node && input.resume && ++resumes >= 2) await gate
          return original(input)
        }
      },
    })
    try {
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      const result = await pending
      expect(opened).toBe(false)
      expect(host.said(child)).toHaveLength(1)
      expect(result).toMatchObject({
        state: "unverified",
        exit_verified: false,
        error: "专家会话被中断后没有按时停下，退出未核实",
        child_session_id: child,
      })
      expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
      expect(result.result.session_id).toBe(child)
      expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
      expect(result.result.report_path).toBeUndefined()
      const before = host.interrupted.filter((id) => id === child).length
      releaseSend()
      await until(
        () => host.said(child).length >= 2 && host.interrupted.filter((id) => id === child).length > before,
        "late summary turn stopped",
      )
      expect(host.said(child).at(-1).text).toContain("不要使用任何工具")
      expect((await host.page("specialists")).items[0]?.state).toBe("unverified")
      expect(JSON.stringify((await host.page("specialists")).items[0])).not.toContain(OPENING_LINE)
    } finally {
      releaseSend()
    }
  }, 15_000)

  test("a summary that uses up its grace records no usable report", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        shortGrace(raw, 1_000, 40)
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "timeout summary", 4000)
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告",
      child_session_id: child,
    })
    expect(result.error).not.toBe("达到专家时间上限，已收回部分报告")
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain("I'll start")
    expect(result.result.report_path).toBeUndefined()
    expect(host.interrupted).toContain(child)
  }, 15_000)

  test("a summary interrupted at grace with partial text archives a truncated summary", async () => {
    const partial = "已核对两处计数，第三处还没看完。"
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        shortGrace(raw, 200, 40)
        const native = raw.ctx.session.context
        raw.ctx.session.context = async (input: { sessionID: string }) => {
          const messages = await native(input)
          const node = raw.created[0]
          if (!node || input.sessionID === node) return messages
          const summaryAt = messages.findIndex((item: any) =>
            item?.type === "synthetic" && String(item.text ?? "").includes("不要使用任何工具"))
          if (summaryAt < 0 || messages.some((item: any) => String(item?.content?.[0]?.text ?? "").includes(partial))) return messages
          const next = messages.slice()
          next.splice(summaryAt, 0, { type: "assistant", content: [{ type: "text", text: OPENING_LINE }] })
          next.splice(summaryAt + 2, 0, { type: "assistant", content: [{ type: "text", text: partial }] })
          return next
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，已收回部分报告",
      child_session_id: child,
    })
    expect(result.result.incomplete).toBe(true)
    expect(result.result.unverified).toBe(true)
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview).toContain("【未完成·未核实】")
    expect(result.result.preview).toContain(partial)
    expect(result.result.preview).not.toContain(OPENING_LINE)
    expect(result.result.preview).not.toContain(AUDIT_REASONING)
    const archived = JSON.parse(readFileSync(result.result.report_path, "utf8"))
    expect(archived).toMatchObject({ stop_reason: "interrupted", incomplete: true, unverified: true })
    expect(archived.output[0].text).toContain("【未完成·未核实】")
    expect(archived.output[0].text).toContain(partial)
    expect(archived.output[0].text).not.toContain(OPENING_LINE)
    expect(host.interrupted).toContain(child)
  }, 15_000)

  test("a parent abort during the summary records no usable report", async () => {
    const host = await load({ hostSetup: timeoutTranscript })
    await nodeAtWork(host)
    const controller = new AbortController()
    const pending = host.call("research_delegate", host.created[0]!, TASK, controller.signal)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "timeout summary", 4000)
    expect(host.said(child).at(-1).text).toContain("不要使用任何工具")
    controller.abort()
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告",
      child_session_id: child,
    })
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain("I'll start")
    expect(result.result.report_path).toBeUndefined()
    expect(host.interrupted).toContain(child)
  }, 8_000)

  test("a failed timeout summary records no usable report instead of the opening line", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw)
        const original = raw.ctx.session.synthetic
        let resumes = 0
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          if (node && input.sessionID !== node && input.resume && ++resumes >= 2) throw new Error("summary model unavailable")
          return original(input)
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，没有可用报告",
      child_session_id: child,
    })
    expect(result.error).not.toBe("达到专家时间上限，已收回部分报告")
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain("I'll start")
    expect(result.result.report_path).toBeUndefined()
  })

  test("a specialist that keeps making progress is not stopped at the stall window", async () => {
    let stopFeed = () => {}
    const host = await load({
      hostSetup: (raw) => {
        raw.ctx.options.specialistStallMs = 800
        raw.ctx.options.specialistTimeoutMs = 8_000
        shortGrace(raw, 200, 40)
        const original = raw.ctx.session.synthetic
        let feeding = false
        raw.ctx.session.synthetic = async (input: any) => {
          const result = await original(input)
          const node = raw.created[0]
          if (!feeding && input.resume && node && input.sessionID !== node) {
            feeding = true
            const feed = () => {
              raw.events.push({
                type: "session.reasoning.delta",
                data: { sessionID: input.sessionID, delta: "仍在阅读" },
              })
              raw.events.push({
                type: "session.tool.progress",
                data: { sessionID: input.sessionID, id: "read-1" },
              })
            }
            feed()
            const timer = setInterval(feed, 40)
            stopFeed = () => clearInterval(timer)
          }
          return result
        }
      },
    })
    try {
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      await Bun.sleep(1_200)
      expect(host.interrupted).not.toContain(child)
      expect(host.said(child).some((item) => String(item.text).includes("不要使用任何工具"))).toBe(false)
      host.reply(child, "持续阅读后的报告")
      const result = await pending
      expect(result).toMatchObject({ state: "completed", exit_verified: true, child_session_id: child })
      expect(result.result.preview).toBe("持续阅读后的报告")
      expect(result.error).toBeFalsy()
    } finally {
      stopFeed()
    }
  })

  test("a silent specialist is stopped after the stall window and the summary is archived", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw, 8_000)
        raw.ctx.options.specialistStallMs = 200
        shortGrace(raw, 1_000, 40)
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "stall summary", 4_000)
    expect(host.said(child).at(-1).text).toContain("不要使用任何工具")
    host.reply(child, "停滞前看到两处计数不一致。")
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "专家一段时间没有新的推进，已收回部分报告",
      child_session_id: child,
    })
    expect(result.error).not.toBe("达到专家时间上限，已收回部分报告")
    expect(result.result.preview).toContain("计数不一致")
    expect(result.result.preview).not.toContain(OPENING_LINE)
    expect(result.result.session_id).toBe(child)
  })

  test("progress does not extend past the absolute specialist cap", async () => {
    let stopFeed = () => {}
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw, 250)
        raw.ctx.options.specialistStallMs = 10_000
        shortGrace(raw, 1_000, 40)
        const original = raw.ctx.session.synthetic
        raw.ctx.session.synthetic = async (input: any) => {
          const result = await original(input)
          const node = raw.created[0]
          if (input.resume && node && input.sessionID !== node && String(input.text).includes("你是节点")) {
            const feed = () => {
              raw.events.push({
                type: "session.reasoning.delta",
                data: { sessionID: input.sessionID, delta: "仍在推理" },
              })
            }
            feed()
            const timer = setInterval(feed, 20)
            stopFeed = () => clearInterval(timer)
          }
          return result
        }
      },
    })
    try {
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      await until(() => host.said(child).length >= 2, "absolute cap summary", 4_000)
      host.reply(child, "到绝对上限前的发现。")
      const result = await pending
      expect(result).toMatchObject({
        state: "incomplete",
        exit_verified: true,
        error: "达到专家时间上限，已收回部分报告",
        child_session_id: child,
      })
      expect(result.error).not.toContain("没有新的推进")
      expect(result.result.preview).toContain("绝对上限")
      expect(result.result.preview).not.toContain(OPENING_LINE)
    } finally {
      stopFeed()
    }
  })

  test("a specialist finishes normally when the host has no session.busy", async () => {
    const host = await load({ sessionBusy: false })
    expect(host.ctx.session.busy).toBeUndefined()
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    expect(host.said(child).at(-1).text).toContain(specialistLimitSentence())
    host.reply(child, "没有 session.busy 时的报告")
    const result = await pending
    expect(result).toMatchObject({ state: "completed", exit_verified: true, child_session_id: child })
    expect(result.result.preview).toContain("没有 session.busy 时的报告")
    expect(result.error).toBeFalsy()
  })

  test("a silent specialist still stops at the stall window when the host has no session.busy", async () => {
    const host = await load({
      sessionBusy: false,
      hostSetup: (raw) => {
        timeoutTranscript(raw, 8_000)
        raw.ctx.options.specialistStallMs = 200
        shortGrace(raw, 1_000, 40)
      },
    })
    expect(host.ctx.session.busy).toBeUndefined()
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "stall summary without session.busy", 4_000)
    expect(host.said(child).at(-1).text).toContain("不要使用任何工具")
    host.reply(child, "停滞前看到两处计数不一致。")
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "专家一段时间没有新的推进，已收回部分报告",
      child_session_id: child,
    })
    expect(result.result.preview).toContain("计数不一致")
    expect(result.result.preview).not.toContain(OPENING_LINE)
  })

  test("progress still stops at the absolute cap when the host has no session.busy", async () => {
    let stopFeed = () => {}
    const host = await load({
      sessionBusy: false,
      hostSetup: (raw) => {
        timeoutTranscript(raw, 250)
        raw.ctx.options.specialistStallMs = 10_000
        shortGrace(raw, 1_000, 40)
        const original = raw.ctx.session.synthetic
        raw.ctx.session.synthetic = async (input: any) => {
          const result = await original(input)
          const node = raw.created[0]
          if (input.resume && node && input.sessionID !== node && String(input.text).includes("你是节点")) {
            const feed = () => {
              raw.events.push({
                type: "session.reasoning.delta",
                data: { sessionID: input.sessionID, delta: "仍在推理" },
              })
            }
            feed()
            const timer = setInterval(feed, 20)
            stopFeed = () => clearInterval(timer)
          }
          return result
        }
      },
    })
    try {
      expect(host.ctx.session.busy).toBeUndefined()
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      await until(() => host.said(child).length >= 2, "absolute cap summary without session.busy", 4_000)
      host.reply(child, "到绝对上限前的发现。")
      const result = await pending
      expect(result).toMatchObject({
        state: "incomplete",
        exit_verified: true,
        error: "达到专家时间上限，已收回部分报告",
        child_session_id: child,
      })
      expect(result.error).not.toContain("没有新的推进")
      expect(result.result.preview).toContain("绝对上限")
      expect(result.result.preview).not.toContain(OPENING_LINE)
    } finally {
      stopFeed()
    }
  })

  test("a late summary stop without session.busy stays unverified from status events", async () => {
    let releaseSend = () => {}
    const gate = new Promise<void>((resolve) => {
      releaseSend = () => resolve()
    })
    const busyProbe = spyOn(Specialists.prototype, "appearsBusy")
    const host = await load({
      sessionBusy: false,
      hostSetup: (raw) => {
        timeoutTranscript(raw, 40)
        shortGrace(raw, 40, 40)
        const original = raw.ctx.session.synthetic
        const nativeWait = raw.ctx.session.wait
        const nativeInterrupt = raw.ctx.session.interrupt
        let summaryStarted = false
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          const summary = node && input.sessionID !== node && input.resume && String(input.text).includes("不要使用任何工具")
          if (!summary) return original(input)
          summaryStarted = true
          await original(input)
          await gate
          return { id: input.id }
        }
        raw.ctx.session.wait = async (input: { sessionID: string }) => {
          if (summaryStarted && input.sessionID !== raw.created[0]) return
          return nativeWait(input)
        }
        raw.ctx.session.interrupt = async (input: { sessionID: string }) => {
          if (summaryStarted) {
            raw.interrupted.push(input.sessionID)
            return { interrupted: true }
          }
          return nativeInterrupt(input)
        }
      },
    })
    try {
      expect(host.ctx.session.busy).toBeUndefined()
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      const result = await pending
      expect(busyProbe).toHaveBeenCalled()
      expect(busyProbe.mock.results.some((item) => item.value === true)).toBe(true)
      expect(host.said(child).some((item) => String(item.text).includes("不要使用任何工具"))).toBe(true)
      expect(result).toMatchObject({
        state: "unverified",
        exit_verified: false,
        error: "专家会话被中断后没有按时停下，退出未核实",
        child_session_id: child,
      })
      expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
      expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
      expect(result.result.report_path).toBeUndefined()
    } finally {
      releaseSend()
      busyProbe.mockRestore()
    }
  })

  /** Event types the no-`session.busy` host emitted. `session.status` is absent from captured traces. */
  function watchEventTypes(raw: { events: { push: (event: any) => void } }): string[] {
    const seen: string[] = []
    const push = raw.events.push.bind(raw.events)
    raw.events.push = (event: any) => {
      seen.push(event?.type)
      push(event)
    }
    return seen
  }

  test("a late summary stop without session.busy stays unverified when only session.execution.started says busy", async () => {
    let releaseSend = () => {}
    const gate = new Promise<void>((resolve) => {
      releaseSend = () => resolve()
    })
    const busyProbe = spyOn(Specialists.prototype, "appearsBusy")
    let seen: string[] = []
    const host = await load({
      sessionBusy: false,
      busySignal: "execution",
      hostSetup: (raw) => {
        seen = watchEventTypes(raw)
        timeoutTranscript(raw, 40)
        shortGrace(raw, 40, 40)
        const original = raw.ctx.session.synthetic
        const nativeWait = raw.ctx.session.wait
        const nativeInterrupt = raw.ctx.session.interrupt
        let summaryStarted = false
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          const summary = node && input.sessionID !== node && input.resume && String(input.text).includes("不要使用任何工具")
          if (!summary) return original(input)
          summaryStarted = true
          await original(input)
          await gate
          return { id: input.id }
        }
        raw.ctx.session.wait = async (input: { sessionID: string }) => {
          if (summaryStarted && input.sessionID !== raw.created[0]) return
          return nativeWait(input)
        }
        raw.ctx.session.interrupt = async (input: { sessionID: string }) => {
          if (summaryStarted) {
            raw.interrupted.push(input.sessionID)
            return { interrupted: true }
          }
          return nativeInterrupt(input)
        }
      },
    })
    try {
      expect(host.ctx.session.busy).toBeUndefined()
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      const result = await pending
      const answers = busyProbe.mock.calls.flatMap((call, index) => call[0] === child ? [busyProbe.mock.results[index]?.value] : [])
      expect(seen).toContain("session.execution.started")
      expect(seen).toContain("session.execution.interrupted")
      expect(seen).not.toContain("session.status")
      expect(answers).toContain(true)
      expect(host.said(child).some((item) => String(item.text).includes("不要使用任何工具"))).toBe(true)
      expect(result).toMatchObject({
        state: "unverified",
        exit_verified: false,
        error: "专家会话被中断后没有按时停下，退出未核实",
        child_session_id: child,
      })
      expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
      expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
      expect(result.result.report_path).toBeUndefined()
    } finally {
      releaseSend()
      busyProbe.mockRestore()
    }
  })

  test("an execution end after session.execution.started makes the no-busy fallback report not busy", async () => {
    let releaseSend = () => {}
    const gate = new Promise<void>((resolve) => {
      releaseSend = () => resolve()
    })
    let summaryChild = ""
    let sawSummaryBusy = false
    let clearedAfterBusy = false
    const originalNoteBusy = Specialists.prototype.noteBusy
    const noteBusy = spyOn(Specialists.prototype, "noteBusy").mockImplementation(function (this: Specialists, sessionID: string, status: unknown) {
      if (sessionID === summaryChild && (status === "busy" || status === "retry")) sawSummaryBusy = true
      if (sessionID === summaryChild && sawSummaryBusy && (status === "idle" || status === "deleted")) clearedAfterBusy = true
      return originalNoteBusy.call(this, sessionID, status)
    })
    const busyProbe = spyOn(Specialists.prototype, "appearsBusy")
    let seen: string[] = []
    const host = await load({
      sessionBusy: false,
      busySignal: "execution",
      hostSetup: (raw) => {
        seen = watchEventTypes(raw)
        timeoutTranscript(raw, 40)
        shortGrace(raw, 500, 40)
        const original = raw.ctx.session.synthetic
        const nativeWait = raw.ctx.session.wait
        const nativeInterrupt = raw.ctx.session.interrupt
        let summaryStarted = false
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          const summary = node && input.sessionID !== node && input.resume && String(input.text).includes("不要使用任何工具")
          if (!summary) return original(input)
          summaryStarted = true
          summaryChild = input.sessionID
          await original(input)
          raw.events.push({ type: "session.execution.succeeded", data: { sessionID: input.sessionID } })
          const deadline = Date.now() + 300
          while (!clearedAfterBusy && Date.now() < deadline) await Bun.sleep(10)
          await gate
          return { id: input.id }
        }
        raw.ctx.session.wait = async (input: { sessionID: string }) => {
          if (summaryStarted && input.sessionID !== raw.created[0]) return
          return nativeWait(input)
        }
        raw.ctx.session.interrupt = async (input: { sessionID: string }) => {
          if (summaryStarted) {
            raw.interrupted.push(input.sessionID)
            return { interrupted: true }
          }
          return nativeInterrupt(input)
        }
      },
    })
    try {
      expect(host.ctx.session.busy).toBeUndefined()
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      const result = await pending
      const answers = busyProbe.mock.calls.flatMap((call, index) => call[0] === child ? [busyProbe.mock.results[index]?.value] : [])
      expect(seen).toContain("session.execution.started")
      expect(seen).toContain("session.execution.succeeded")
      expect(seen).not.toContain("session.status")
      expect(sawSummaryBusy).toBe(true)
      expect(clearedAfterBusy).toBe(true)
      expect(answers).toEqual([false])
      expect(result).toMatchObject({
        state: "incomplete",
        exit_verified: true,
        error: "达到专家时间上限，没有可用报告",
        child_session_id: child,
      })
      expect(result.error).not.toBe("专家会话被中断后没有按时停下，退出未核实")
      expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
      expect(result.result.report_path).toBeUndefined()
    } finally {
      releaseSend()
      noteBusy.mockRestore()
      busyProbe.mockRestore()
    }
  })

  test("user options cannot set the stop or summary grace", async () => {
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw, 40)
        raw.ctx.options.specialistSummaryGraceMs = 1
        raw.ctx.options.specialistStopGraceMs = 1
        shortGrace(raw, 2_000, 40)
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    await until(() => host.said(child).length >= 2, "summary despite user grace options", 4_000)
    host.reply(child, "补问里的发现。")
    const result = await pending
    expect(result).toMatchObject({
      state: "incomplete",
      exit_verified: true,
      error: "达到专家时间上限，已收回部分报告",
      child_session_id: child,
    })
    expect(result.result.preview).toContain("补问里的发现")
  })

  test("a late summary stop stays unverified when the child is still busy", async () => {
    let releaseSend = () => {}
    let sawBusy = false
    const gate = new Promise<void>((resolve) => {
      releaseSend = () => resolve()
    })
    const host = await load({
      hostSetup: (raw) => {
        timeoutTranscript(raw, 40)
        shortGrace(raw, 40, 40)
        const original = raw.ctx.session.synthetic
        const nativeWait = raw.ctx.session.wait
        const nativeInterrupt = raw.ctx.session.interrupt
        let summaryStarted = false
        raw.ctx.session.synthetic = async (input: any) => {
          const node = raw.created[0]
          const summary = node && input.sessionID !== node && input.resume && String(input.text).includes("不要使用任何工具")
          if (!summary) return original(input)
          summaryStarted = true
          await original(input)
          await gate
          return { id: input.id }
        }
        raw.ctx.session.wait = async (input: { sessionID: string }) => {
          if (summaryStarted && input.sessionID !== raw.created[0]) return
          return nativeWait(input)
        }
        raw.ctx.session.interrupt = async (input: { sessionID: string }) => {
          if (summaryStarted) {
            raw.interrupted.push(input.sessionID)
            return { interrupted: true }
          }
          return nativeInterrupt(input)
        }
        const nativeBusy = raw.ctx.session.busy
        raw.ctx.session.busy = (input: { sessionID: string }) => {
          const value = nativeBusy(input)
          if (summaryStarted && input.sessionID !== raw.created[0] && value) sawBusy = true
          return value
        }
      },
    })
    try {
      await nodeAtWork(host)
      const pending = host.call("research_delegate", host.created[0]!, TASK)
      const child = await specialist(host, 1)
      const result = await pending
      // The summary message has landed and the child is still busy. Unverified comes from that, not from a missing send.
      expect(sawBusy).toBe(true)
      expect(host.said(child).some((item) => String(item.text).includes("不要使用任何工具"))).toBe(true)
      expect(result).toMatchObject({
        state: "unverified",
        exit_verified: false,
        error: "专家会话被中断后没有按时停下，退出未核实",
        child_session_id: child,
      })
      expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
      expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
      expect(result.result.report_path).toBeUndefined()
    } finally {
      releaseSend()
    }
  })

  test("production summary grace is 60s and the stall and absolute cap keep their defaults", () => {
    expect(SUMMARY_GRACE_MS).toBe(60_000)
    expect(STOP_GRACE_MS).toBe(60_000)
    expect(SPECIALIST_STALL_MS).toBe(300_000)
    expect(SPECIALIST_TIMEOUT_MS).toBe(3_600_000)
    expect(specialistProgressEvent("session.reasoning.delta")).toBe(true)
    expect(specialistProgressEvent("session.text.delta")).toBe(true)
    expect(specialistProgressEvent("session.tool.called")).toBe(true)
    expect(specialistProgressEvent("session.tool.progress")).toBe(true)
    expect(specialistProgressEvent("session.tool.success")).toBe(true)
    expect(specialistProgressEvent("session.status")).toBe(false)
    expect(specialistProgressEvent("session.idle")).toBe(false)
  })

  test("a timed-out specialist that cannot be stopped does not archive the opening line", async () => {
    const host = await load({
      hostSetup: (raw) => {
        raw.ctx.options.specialistTimeoutMs = 200
        shortGrace(raw, 40)
        const native = raw.ctx.session.context
        raw.ctx.session.context = async (input: { sessionID: string }) => {
          const messages = await native(input)
          const node = raw.created[0]
          if (!node || input.sessionID === node || !raw.created.includes(input.sessionID)) return messages
          const kept = messages.filter((item: any) => item?.type !== "assistant" && item?.type !== "idle")
          return [...kept, ...issue27Tail()]
        }
        raw.ctx.session.interrupt = async (input: { sessionID: string }) => {
          raw.interrupted.push(input.sessionID)
          return { interrupted: true }
        }
      },
    })
    await nodeAtWork(host)
    const pending = host.call("research_delegate", host.created[0]!, TASK)
    const child = await specialist(host, 1)
    const result = await pending
    expect(result).toMatchObject({
      state: "unverified",
      exit_verified: false,
      error: "专家会话被中断后没有按时停下，退出未核实",
      child_session_id: child,
    })
    expect(result.error).not.toBe("达到专家时间上限，没有可用报告")
    expect(result.error).not.toBe("达到专家时间上限，已收回部分报告")
    expect(result.result.session_id).toBe(child)
    expect(result.result.preview ?? "").not.toContain(OPENING_LINE)
    expect(result.result.preview ?? "").not.toContain(AUDIT_REASONING)
    expect(result.result.report_path).toBeUndefined()
    expect(JSON.stringify(result.result)).not.toContain(OPENING_LINE)
    expect(host.said(child).some((item) => String(item.text).includes("不要使用任何工具"))).toBe(false)
    expect((await host.page("specialists")).items[0]?.state).toBe("unverified")
    expect(JSON.stringify((await host.page("specialists")).items[0])).not.toContain(OPENING_LINE)
  }, 15_000)
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
    expect(parseResearchCommand("")).toEqual({ kind: "board" })
    expect(parseResearchCommand("help")).toEqual({ kind: "rpc", action: "help", args: {} })
    expect(parseResearchCommand("auto  Find the cause of X ")).toEqual({
      kind: "rpc", action: "auto", args: { goal: "Find the cause of X" },
    })
    expect(parseResearchCommand("auto --pause  Find the cause of X ")).toEqual({
      kind: "rpc", action: "init", args: { goal: "Find the cause of X" },
    })
    expect(parseResearchCommand("auto --pause")).toMatchObject({ kind: "error" })
    expect(parseResearchCommand("auto")).toEqual({ kind: "rpc", action: "auto", args: { goal: null } })
    expect(parseResearchCommand("pause")).toEqual({ kind: "rpc", action: "pause", args: {} })
    expect((parseResearchCommand("stop") as any).confirm).toContain("停止自主研究")
    expect(parseResearchCommand('budget cost 0.25 small experiment')).toMatchObject({kind:'rpc',action:'budget',args:{limit:'cost',value:'0.25'}})
    expect(parseResearchCommand('budget turns 0.25 invalid')).toMatchObject({kind:'error'})
    expect(parseResearchCommand('budget cost 0 invalid')).toMatchObject({kind:'error'})
    expect(parseResearchCommand("recover retry T-001 retry after fix")).toMatchObject({
      kind: "rpc", action: "retry", args: { task_id: "T-001", reason: "retry after fix" },
    })
    expect((parseResearchCommand("recover retry T-001 retry after fix") as any).confirm).toContain("重新执行该节点")
    expect(parseResearchCommand("recover takeover")).toMatchObject({ kind: "rpc", action: "takeover", args: {} })
    expect((parseResearchCommand("recover takeover") as any).confirm).toContain("接管")
    expect(parseResearchCommand("exploration")).toEqual({ kind: "rpc", action: "exploration", args: { mode: null } })
    expect(parseResearchCommand("exploration auto")).toEqual({ kind: "rpc", action: "exploration", args: { mode: "auto" } })
    expect(parseResearchCommand("exploration on")).toEqual({ kind: "rpc", action: "exploration", args: { mode: "on" } })
    expect(parseResearchCommand("exploration off")).toEqual({ kind: "rpc", action: "exploration", args: { mode: "off" } })
    expect(parseResearchCommand("exploration invalid")).toEqual({
      kind: "error",
      message: "用法：/research exploration [auto|on|off]",
    })
    for (const removed of [
      "board",
      "board extra",
      "resume",
      "resume extra",
      "init Find X",
      "status",
      "context-policy r1",
      "conclusion-contract evidence-v1",
      "guidance read docs",
      "detach",
      "retry T-001 原因",
      "takeover",
      "work X-001",
    ]) {
      expect(parseResearchCommand(removed)).toMatchObject({ kind: "error" })
    }
  })
})

describe("board text", () => {
  test("status identifies unverified execution and its recovery command", () => {
    const text = associatedStatus({ workflow: { run: { state: 'paused' }, session: { role: 'main' },
      unverified: { tasks: [{ task_id: 'T-001', node_id: 'X-001' }], tasks_total: 1,
        specialists: [{ task_id: 'S-001', node_id: 'X-001' }], specialists_total: 1 } } })
    expect(text).toContain('T-001（X-001）')
    expect(text).toContain('/research recover task')
    expect(text).toContain('S-001（X-001）')
    expect(text).toContain('/research recover specialist')
  })
  test("historical completion reports a missing contract, not an inferred review", () => {
    const overview = overviewModel({ project: { goal: "old" }, counts: {}, run: { state: "complete" } }, null)!
    expect(overview.notices).toEqual(["结项记录：未记录；审阅情况未知。"])
    expect(overview.conclusion).toBeUndefined()
  })
  test("status counts the conclusion lists and leaves the entries to the board", () => {
    const gaps = Array.from({ length: 12 }, (_, index) => `gap ${index}`)
    const conclusion = { outcome: "partial", final_ref: "pub/P-2#r", gaps, review: { status: "partial", refs: ["pub/P-3#review"], limitations: ["l1", "l2"] } }
    const text = associatedStatus({ project: { goal: "G" }, workflow: { run: { state: "complete" }, conclusion } })
    expect(text).toContain("未解决项 12 条 · 审阅限制 2 条 · 审阅材料 1 条，逐条见 /research 概览")
    const quietUnrecorded = overviewModel(
      { project: { goal: "New or DSH" }, counts: {}, run: { state: "manual" }, controller: { phase: "unrecorded" } },
      null,
    )!
    expect(quietUnrecorded.notices).toEqual([])
    const runningUnrecorded = overviewModel(
      { project: { goal: "Upgraded running" }, counts: {}, run: { state: "running" }, controller: { phase: "unrecorded" } },
      null,
    )!
    expect(runningUnrecorded.notices.some((line) => line.includes("控制器：unrecorded；需先核对执行状态"))).toBe(true)
    expect(text).not.toContain("gap 0")
    const clean = associatedStatus({ workflow: { run: { state: "complete" }, conclusion: { ...conclusion, gaps: [], review: { status: "reviewed", refs: [], limitations: [] } } } })
    expect(clean).not.toContain("逐条见")
  })
  test("labels the newest publication as latest, not final, and hides display items", () => {
    const overview = overviewModel(
      {
        project: { goal: "G" }, project_root: "/p", run: { state: "running" }, counts: {}, review_queue: {},
        final_publication: { publication_id: "P-003", status: "complete", summary: "s" },
      },
      null,
    )!
    // Without a conclusion the newest publication is shown as latest, never as final.
    expect(overview.final).toBeUndefined()
    expect(overview.latest?.id).toBe("P-003")
    const rows = materialRows({
      items: [{ publication_id: "P-001", status: "partial", summary: "s", items: [
        { item_id: "__research_display", ref: "pub/P-001#__research_display" },
        { item_id: "report", ref: "pub/P-001#report", source_path: "r.md", object_kind: "file" },
      ] }],
    })
    expect(rows.map((row) => row.ref)).toEqual(["pub/P-001#report"])
  })
  test("rows lead with a short code and the content, not the full reference", () => {
    const rows = knowledgeRows({
      items: [
        { ref: "knowledge/K-007@1", kind: "observation", status: "proposed", statement: "GPU 7 idle" },
        { ref: "knowledge/K-031@2", kind: "claim", status: "proposed", statement: "F5 dominates" },
      ],
    })
    expect(rows.map((row) => row.code)).toEqual(["K-007", "K-031@2"])
    expect(rows[0]!.title).toBe("[观察] GPU 7 idle")
    const sessions = sessionRows({ items: [{ session_id: "ses_opaque123", role: "node_core", node_id: "X-002" }] })
    expect(sessions[0]!.code).toBe("X-002")
    expect(sessions[0]!.title).not.toContain("ses_opaque123")
  })
  test("runtime detail does not repeat the conclusion record", () => {
    const conclusion = { outcome: "partial", final_ref: "pub/P-1#r", gaps: ["gap"], review: { status: "reviewed", refs: [], limitations: ["lim"] } }
    expect(runtimeNotes({ run: { state: "complete" }, conclusion })).toEqual(["结项记录：已登记，见概览"])
    expect(runtimeNotes({ run: { state: "complete" }, conclusion: null })).toEqual(["结项记录：未记录；审阅情况未知。"])
  })
  test("overview shows the final report once and a newer publication as latest", () => {
    const conclusion = { outcome: "partial", final_ref: "pub/P-2#r", gaps: ["g1", "g2"], review: { status: "reviewed", refs: ["pub/P-3#review"], limitations: ["l1"] } }
    const final = { publication_id: "P-2", status: "complete", summary: "final", display: { title: "Final", overview: "o", sections: [{ heading: "h", items: ["i"] }] } }
    const base = { project: { goal: "G" }, project_root: "/p", run: { state: "complete" }, counts: { nodes: 7 }, review_queue: { pending_total: 3 }, conclusion, final_publication: final }
    const same = overviewModel({ ...base, latest_publication: final }, null)!
    expect(same.final?.title).toBe("Final")
    expect(same.latest).toBeUndefined()
    expect(same.conclusion?.gaps).toEqual(["g1", "g2"])
    expect(same.counts.find((count) => count.label === "待整理")?.value).toBe(3)
    const newer = overviewModel({ ...base, latest_publication: { publication_id: "P-4", status: "partial", summary: "later" } }, null)!
    expect(newer.latest?.id).toBe("P-4")
  })
  test("kind chips come from server totals, not from the loaded page", () => {
    // The first page holds 100 open questions; the one hypothesis is entry 101.
    const chips = kindChips([
      { kind: undefined, total: 101 },
      { kind: "claim", total: 0 },
      { kind: "hypothesis", total: 1 },
      { kind: "open_question", total: 100 },
    ])
    expect(chips.total).toBe(101)
    expect(chips.kinds).toEqual([
      { kind: "open_question", count: 100 },
      { kind: "hypothesis", count: 1 },
    ])
  })
  test("unreadable files read as plain language", () => {
    const missing = decodeChunk({ ref: "pub/P-1#a", resolution: { outcome: "unavailable", reason: "object_missing", message: "pub/P-1#a: object_missing" } })
    expect(missing).toContain("不在本机")
    expect(missing).toContain("object_missing")
  })
})

test("R1 context policy switches through the host and remains opt-in", async () => {
  const host = await load()
  await host.command("init", "ses_main", { goal: "R1 policy fixture" })
  const legacy = modelRequest("ses_main")
  await host.hooks.get("context")!(legacy)
  expect(contextText(legacy)).not.toContain("critical_now")
  const switched = await host.command("context-policy", "ses_main", { policy: "r1" })
  expect(switched.message).toContain("memory-r1-v1")
  expect(switched.message).not.toContain("诊断")
  expect(switched.state.last_recorded.policy_version).toBe("memory-v2")
  const current = modelRequest("ses_main")
  await host.hooks.get("context")!(current)
  expect(contextText(current)).toContain("critical_now")
  expect(contextText(current)).toContain("findings")
  const back = await host.command("context-policy", "ses_main", { policy: "legacy" })
  expect(back.state.policy_version).toBe("memory-v2")
  expect(back.state.last_recorded.policy_version).toBe("memory-r1-v1")
  const restored = modelRequest("ses_main")
  await host.hooks.get("context")!(restored)
  expect(contextText(restored)).not.toContain("critical_now")
})

test("switching context policy clears cached and in-flight legacy context", async () => {
  const { Research } = await import("../src/research")
  let resolveRead!: (value: any) => void
  const storage = {
    request: async (method: string) => {
      if (method === "context_policy") return { policy_version: "memory-r1-v1" }
      if (method === "memory_context") return new Promise((resolve) => { resolveRead = resolve })
      return {}
    },
  } as unknown as StorageClient
  const research = new Research(storage, project, () => false)
  research.register("main", project, "main")
  const pending = research.memory("main")
  await research.contextPolicy("main", "r1")
  resolveRead({ text: "LEGACY_IN_FLIGHT", policy_version: "memory-v2", source_sequence: 1 })
  expect(await pending).not.toContain("LEGACY_IN_FLIGHT")
})

test('exploration chooses one durable probe through the ordinary node scheduler', async () => {
  const host = await load()
  await host.command('auto','ses_main',{goal:'Choose an approach'})
  const policy = await host.command('exploration','ses_main',{mode:'on'})
  expect(policy.state.exploration_policy).toBe('exploration-v1')
  expect(policy.state.conclusion_contract).toBe('evidence-v1')
  const scope = await host.call('research_scope','ses_main',{changes:{objective:'Choose an approach',task_kind:'selection'}})
  const route = await host.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'Measure tail',
    rationale:'The average may not explain the tail',critical_assumption:'The distributions differ'}})
  const alternative = await host.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'Check service replay',
    rationale:'A direct replay can measure the actual target',critical_assumption:'The workload is representative'}})
  const evidence = await host.call('research_query','ses_main',{collection:'evidence',refs:[]})
  const decision = await host.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:evidence.snapshot_id,
    route_refs:[route.ref],action:'probe',reason:'Resolve the assumption',next_work:{route_refs:[route.ref],
      question:'Does the tail differ?',why_now:'Choose a direction',plan:'Measure the tail',root_reason:'First probe',work_kind:'probe'}})
  expect(decision.intent_id).toBeTruthy()
  expect(decision.work.task_id).toBeTruthy()
  expect(host.created).toHaveLength(1)
  expect(host.said(host.created[0]!).some(item=>item.text.includes('信息不足'))).toBe(true)
  const context = modelRequest('ses_main')
  host.events.push({id:'exploration-preview-step',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'exploration-preview',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
    .some((item:any)=>item.source_key==='step:ses_main:exploration-preview'),'exploration context step')
  await host.hooks.get('context')!(context)
  expect(contextText(context)).toContain('exploration')
  expect(Object.keys(context.tools)).toContain('research_decide')
  expect(Object.keys(context.tools)).toContain('research_prepare_conclusion')
  expect(Object.keys((context.tools.research_conclude.input as any).properties).sort()).toEqual(['draft_id','final_ref','summary'])
  expect((context.tools.research_conclude.input as any).required).toContain('draft_id')
  await host.call('research_wait','ses_main',{task_ids:[decision.work.task_id]})
  host.turn.end('ses_main')
  host.turn.start(host.created[0]!)
  await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:host.created[0]!,id:'probe-exec',input:{command:'private'}})
  await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:host.created[0]!,id:'probe-exec',status:'completed'})
  const usage = await host.call('research_query','ses_main',{collection:'usage'})
  const toolObservation = usage.items.find((item:any) => item.purpose === 'tool_execution')
  expect(toolObservation.details.tool).toBe('shell')
  expect(JSON.stringify(toolObservation)).not.toContain('private')
  const negative = await host.call('research_publish',host.created[0]!,{status:'partial',summary:'No clear tail benefit',
    items:[{item_id:'result',kind:'text',content:{outcome:'insufficient'}}]})
  await host.call('research_finish',host.created[0]!,{state:'finished',summary:'The probe is inconclusive'})
  host.turn.end(host.created[0]!)
  await until(async () => (await host.call('research_query','ses_main',{collection:'exploration_results'})).total === 2,
    'publication and finish available for a decision')
  const results = await host.call('research_query','ses_main',{collection:'exploration_results'})
  const updated = await host.call('research_query','ses_main',{collection:'evidence',refs:[`pub/${negative.publication_id}#result`]})
  const redirect = await host.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:updated.snapshot_id,
    route_refs:[decision.route_refs[0],alternative.ref],result_ids:results.items.map((item:any)=>item.result_id),
    action:'redirect',reason:'The first probe did not answer the tail question',
    route_changes:[{route_ref:decision.route_refs[0],disposition:'deferred',reason:'The probe was inconclusive',
      reactivation_condition:'New workload data is available',evidence_refs:[`pub/${negative.publication_id}#result`]}],
    next_work:{route_refs:[alternative.ref],question:'Does the replay meet the target?',why_now:'Use the stronger comparison',
      plan:'Measure a service replay',predecessors:[{node_id:decision.work.node_id,relation_type:'branches_from',
        rationale:'The prior probe was inconclusive',input_refs:[`pub/${negative.publication_id}#result`]}],work_kind:'probe'}})
  expect(redirect.work.task_id).toBeTruthy()
  expect(host.created).toHaveLength(2)
  const routes = await host.call('research_query','ses_main',{collection:'exploration_routes'})
  expect(routes.items.find((item:any)=>item.route_id===route.route_id).disposition).toBe('deferred')
  await host.call('research_wait','ses_main',{task_ids:[redirect.work.task_id]})
  host.turn.end('ses_main')
  const second = host.created[1]!
  host.turn.start(second)
  const data = await host.call('research_publish',second,{status:'complete',summary:'The replay measured the target',
    items:[{item_id:'data',kind:'text',content:{measured:7}}]})
  const claim = await host.call('research_record',second,{kind:'claim',statement:'The measured target is 7',
    evidence_refs:[`pub/${data.publication_id}#data`],visibility:'project'})
  await host.call('research_finish',second,{state:'finished',summary:'Measured the target'})
  host.turn.end(second)
  await until(async () => (await host.call('research_query','ses_main',{collection:'exploration_results'})).items
    .filter((item:any)=>!item.handled_by_decision).length === 2, 'second probe results')
  const arrived = await host.call('research_query','ses_main',{collection:'exploration_results'})
  const beforeStop = await host.call('research_query','ses_main',{collection:'evidence',refs:[claim.ref]})
  const selected = routes.items.find((item:any)=>item.route_id===alternative.route_id)
  await host.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:beforeStop.snapshot_id,
    route_refs:routes.items.map((item:any)=>item.ref),result_ids:arrived.items.filter((item:any)=>!item.handled_by_decision)
      .map((item:any)=>item.result_id),action:'propose_stop',reason:'The replay answers the bounded question',
    route_changes:[{route_ref:selected.ref,disposition:'adopted',reason:'The replay provides the target measurement',
      evidence_refs:[claim.ref]}]})
  const firstPage = await host.call('research_query','ses_main',{collection:'evidence',refs:[claim.ref,'knowledge/K-404@1']})
  let body=firstPage.chunk, cursor=firstPage.cursor
  while (cursor) {
    const page=await host.call('research_query','ses_main',{collection:'evidence',cursor})
    body+=page.chunk; cursor=page.cursor
  }
  const view=JSON.parse(body)
  const missing=view.limitations.find((item:any)=>item.reason==='knowledge_missing')
  const draft = await host.call('research_prepare_conclusion','ses_main',{snapshot_id:firstPage.snapshot_id,
    core_claim_refs:[claim.ref],outcome:'answered',gaps:[],review_requirements:[],risk_judgments:[{
      risk_id:missing.risk_id,core_impact:'no',disposition:'unresolved',reason:'The missing mechanism is outside the measured target',
      evidence_refs:[claim.ref],gap:'The mechanism remains unverified'}],
    exploration_stop:{reason_kind:'goal_met',reason:'The target is measured under the stated workload'}})
  expect(draft.evidence_closure.status).toBe('qualified')
  const report = await host.call('research_publish','ses_main',{status:'complete',summary:'Final report',gaps:[],
    items:[{item_id:'report',kind:'text',content:{text:'The target was 7 under the measured workload.'}}],
    knowledge_refs:[claim.ref],draft_id:draft.draft_id,report_item_id:'report'})
  const parts:any[]=[]; cursor=null
  do {
    const id=`integrated-page-${parts.length}`
    const raw=await host.tools.get('research_query').execute({collection:'evidence',limit:500,
      ...(cursor?{cursor}:{draft_id:draft.draft_id})},{sessionID:'ses_main',messageID:`integrated-read-${parts.length}`,id,agent:'build'})
    const page=JSON.parse(raw.content)
    parts.push({type:'tool-result',id,name:'research_query',result:{type:'text',value:raw.content}})
    cursor=page.cursor
  } while (cursor)
  const finalRequest=modelRequest('ses_main')
  finalRequest.messages=[{role:'tool',content:parts}] as any
  host.events.push({id:'integrated-step',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'integrated-final',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
    .some((item:any)=>item.source_key==='step:ses_main:integrated-final'), 'integrated conclusion step')
  await host.hooks.get('context')!(finalRequest)
  const concluded = JSON.parse((await host.tools.get('research_conclude').execute({draft_id:draft.draft_id,
    final_ref:`pub/${report.publication_id}#report`,summary:'Answered within the measured scope'},
    {sessionID:'ses_main',messageID:'integrated-final',id:'integrated-final-call',agent:'build'})).content)
  expect(concluded.evidence.evidence_closure.status).toBe('qualified')
  expect(concluded.evidence.appendix_ref).toContain('#evidence-appendix')
})

test('a stale queued exploration wakes the coordinator after another node finishes', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'Choose an approach'})
  await host.command('exploration','ses_main',{mode:'on'})
  host.turn.start('ses_main')
  const ordinary=await host.call('research_propose','ses_main',{question:'Existing work',why_now:'now',plan:'measure',root_reason:'first'})
  await host.call('research_dispatch','ses_main',{node_id:ordinary.node_id})
  const scope=await host.call('research_scope','ses_main',{changes:{objective:'Choose',task_kind:'selection'}})
  const route=await host.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'Probe',rationale:'Compare',critical_assumption:'Tail differs'}})
  const view=await host.call('research_query','ses_main',{collection:'evidence',refs:[]})
  const decision=await host.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:view.snapshot_id,
    route_refs:[route.ref],action:'probe',reason:'Measure',next_work:{route_refs:[route.ref],
      question:'Tail?',why_now:'now',plan:'measure',root_reason:'first'}})
  await host.call('research_route','ses_main',{scope_ref:scope.ref,route_ref:decision.route_refs[0],
    reason:'Changed premise',changes:{critical_assumption:'New workload'}})
  await host.call('research_wait','ses_main',{task_ids:[decision.work.task_id]})
  host.turn.end('ses_main')
  const child=host.created[0]!
  host.turn.start(child)
  await host.call('research_finish',child,{state:'finished',summary:'Existing work completed'})
  host.turn.end(child)
  await until(()=>host.said('ses_main').some(item=>item.text.includes('探索工作未启动')),'stale exploration notice')
  await until(()=>host.said('ses_main').some(item=>item.resume && item.text.includes('等待的节点有了结果')),'stale exploration wake')
  const results=await host.call('research_query','ses_main',{collection:'exploration_results'})
  expect(results.items.some((item:any)=>item.kind==='stale')).toBe(true)
  expect(host.created).toHaveLength(1)
})

test('turning exploration off wakes the waiting coordinator and retains closing decisions', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'Choose an approach'})
  await host.command('exploration','ses_main',{mode:'on'})
  host.turn.start('ses_main')
  const ordinary=await host.call('research_propose','ses_main',{question:'Existing work',why_now:'now',plan:'measure',root_reason:'first'})
  await host.call('research_dispatch','ses_main',{node_id:ordinary.node_id})
  const scope=await host.call('research_scope','ses_main',{changes:{objective:'Choose',task_kind:'selection'}})
  const route=await host.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'Probe',rationale:'Compare',critical_assumption:'Tail differs'}})
  const view=await host.call('research_query','ses_main',{collection:'evidence',refs:[]})
  const decision=await host.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:view.snapshot_id,
    route_refs:[route.ref],action:'probe',reason:'Measure',next_work:{route_refs:[route.ref],
      question:'Tail?',why_now:'now',plan:'measure',root_reason:'first'}})
  await host.call('research_wait','ses_main',{task_ids:[decision.work.task_id]})
  host.turn.end('ses_main')
  await host.command('exploration','ses_main',{mode:'off'})
  await until(()=>host.said('ses_main').some(item=>item.resume && item.text.includes('等待的节点有了结果')),'disabled exploration wake')
  const context=modelRequest('ses_main')
  await host.hooks.get('context')!(context)
  const spec=(context.tools.research_decide.input as any).properties
  expect(spec.next_work).toBeUndefined()
  expect(spec.work_batch).toBeUndefined()
  expect(spec.action.enum).toEqual(['defer','propose_stop'])
  expect(context.tools.research_scope).toBeUndefined()
})

test('a durable exploration decision resumes exactly once after host restart', async () => {
  const first = await load()
  await first.command('auto','ses_main',{goal:'Choose an approach'})
  await first.command('exploration','ses_main',{mode:'on'})
  const scope = await first.call('research_scope','ses_main',{changes:{objective:'Choose an approach',task_kind:'selection'}})
  const route = await first.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'Probe tail',
    rationale:'Tail may differ',critical_assumption:'The distribution differs'}})
  const view = await first.call('research_query','ses_main',{collection:'evidence',refs:[]})
  const original = StorageClient.prototype.request
  const spy = spyOn(StorageClient.prototype,'request').mockImplementation(function(method, fields, operation) {
    if (method === 'exploration_materialize') return Promise.reject(new Error('host interrupted before materialization'))
    return original.call(this,method,fields,operation)
  })
  try {
    const recorded=await first.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:view.snapshot_id,
      route_refs:[route.ref],action:'probe',reason:'Resolve the tail assumption',next_work:{route_refs:[route.ref],
        question:'Does tail differ?',why_now:'Choose direction',plan:'Measure',root_reason:'First probe'}})
    expect(recorded.decision_recorded).toBe(true)
    expect(recorded.works[0]).toMatchObject({intent_id:recorded.intent_id,state:'unconfirmed'})
    expect(recorded.errors[0]).toMatchObject({phase:'materialize',intent_id:recorded.intent_id,
      reason:'Error: host interrupted before materialization'})
  } finally { spy.mockRestore() }
  expect((await first.page('nodes')).items).toHaveLength(0)
  cleanups.pop()!()
  simulateDeadHost()
  const second = await load()
  await until(async () => (await second.summary()).run.state === 'paused','cold pause')
  expect((await second.page('nodes')).items).toHaveLength(0)
  await second.command('resume','ses_main')
  await until(async () => (await second.page('tasks')).items.length === 1,'recovered exploration task')
  expect((await second.page('nodes')).items).toHaveLength(1)
  expect(second.created).toHaveLength(1)
  expect((await second.page('tasks')).items).toHaveLength(1)
})

test('evidence conclusion requires the actual draft pages in the model request', async () => {
  const host = await load()
  await host.command('init','ses_main',{goal:'Answer a bounded question'})
  const legacy = modelRequest('ses_main')
  host.events.push({id:'legacy-preview-step',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'legacy-preview',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
    .some((item:any)=>item.source_key==='step:ses_main:legacy-preview'),'legacy context step')
  await host.hooks.get('context')!(legacy)
  expect(Object.keys((legacy.tools.research_conclude.input as any).properties).sort()).toEqual(['final_ref','gaps','outcome','review','summary'])
  expect((legacy.tools.research_conclude.input as any).required).toContain('review')
  await host.command('conclusion-contract','ses_main',{contract:'evidence-v1'})
  const source = await host.call('research_publish','ses_main',{status:'complete',summary:'measurement',gaps:[],
    items:[{item_id:'data',kind:'text',content:{measured:7}}],knowledge_refs:[]})
  const claim = await host.call('research_record','ses_main',{kind:'claim',statement:'Measured value is 7',
    evidence_refs:[`pub/${source.publication_id}#data`],visibility:'project'})
  const view = await host.call('research_query','ses_main',{collection:'evidence',refs:[claim.ref]})
  const draft = await host.call('research_prepare_conclusion','ses_main',{snapshot_id:view.snapshot_id,
    core_claim_refs:[claim.ref],outcome:'answered',gaps:[],risk_judgments:[],review_requirements:[],
    exploration_stop:{reason_kind:'goal_met',reason:'Measured under the stated conditions'}})
  const publication = await host.call('research_publish','ses_main',{status:'complete',summary:'report',gaps:[],
    items:[{item_id:'report',kind:'text',content:{text:'The measured value is 7.'}}],knowledge_refs:[claim.ref],
    draft_id:draft.draft_id,report_item_id:'report'})
  const parts:any[]=[]
  let cursor:any = null
  do {
    const id=`evidence-page-${parts.length}`
    const raw = await host.tools.get('research_query').execute({collection:'evidence',limit:500,
      ...(cursor ? {cursor} : {draft_id:draft.draft_id})},{sessionID:'ses_main',messageID:`read-${parts.length}`,id,agent:'build'})
    const page=JSON.parse(raw.content)
    parts.push({type:'tool-result',id,name:'research_query',result:{type:'text',value:raw.content}})
    cursor=page.cursor
  } while (cursor)
  const request=modelRequest('ses_main')
  request.messages=[{role:'tool',content:parts}] as any
  host.events.push({id:'evidence-step',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'final-message',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items.length>0,'evidence step')
  await host.hooks.get('context')!(request)
  const result = await host.tools.get('research_conclude').execute({draft_id:draft.draft_id,
    final_ref:`pub/${publication.publication_id}#report`,summary:'Answered under the measured conditions'},
    {sessionID:'ses_main',messageID:'final-message',id:'final-call',agent:'build'})
  const conclusion=JSON.parse(result.content)
  expect(conclusion.outcome).toBe('answered')
  expect(conclusion.evidence.evidence_closure.status).toBe('resolved')
  expect(conclusion.evidence.review_coverage.status).toBe('unreviewed')
  expect(conclusion.evidence.appendix_ref).toContain('#evidence-appendix')
})

test('material pages are confirmed when the coordinator actually responds', async () => {
  const host = await load()
  await host.command('init','ses_main',{goal:'Answer from a frozen file'})
  await host.command('conclusion-contract','ses_main',{contract:'evidence-v1'})
  writeFileSync(join(project,'measurement.txt'),'温度😀\r\nMeasured value: 7\r\n')
  const source=await host.call('research_publish','ses_main',{status:'complete',summary:'measurement',gaps:[],
    items:[{item_id:'data',kind:'text',source_path:'measurement.txt'}],knowledge_refs:[]})
  const ref=`pub/${source.publication_id}#data`
  const claim=await host.call('research_record','ses_main',{kind:'claim',statement:'Measured value is 7',
    evidence_refs:[ref],visibility:'project'})
  const material=await host.tools.get('research_query').execute({collection:'evidence',material_ref:ref},
    {sessionID:'ses_main',messageID:'read-material',id:'material-page',agent:'build'})
  const request=modelRequest('ses_main')
  request.messages=[{role:'tool',content:[{type:'tool-result',id:'material-page',name:'research_query',
    result:{type:'text',value:material.content}}]}] as any
  host.events.push({id:'material-step',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'material-response',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items.length>0,'material step')
  await host.hooks.get('context')!(request)
  const response=await host.tools.get('research_query').execute({collection:'evidence',refs:[claim.ref],limit:16384},
    {sessionID:'ses_main',messageID:'material-response',id:'after-material',agent:'build'})
  const view=JSON.parse(JSON.parse(response.content).chunk)
  expect(view.materials.find((item:any)=>item.ref===ref).material_read.complete).toBe(true)
})

test('a missing frozen material does not block the coordinator or credit its page', async () => {
  const host=await load()
  await host.command('init','ses_main',{goal:'Recover after a frozen material becomes unavailable'})
  await host.command('conclusion-contract','ses_main',{contract:'evidence-v1'})
  writeFileSync(join(project,'good.txt'),'usable source')
  writeFileSync(join(project,'missing.txt'),'source later unavailable')
  const source=await host.call('research_publish','ses_main',{status:'complete',summary:'data',gaps:[],
    items:[{item_id:'good',kind:'text',source_path:'good.txt'},
      {item_id:'missing',kind:'text',source_path:'missing.txt'}],knowledge_refs:[]})
  const good=`pub/${source.publication_id}#good`, missing=`pub/${source.publication_id}#missing`
  const parts=[]
  for (const [index,ref] of [good,missing].entries()) {
    const id=`material-${index}`
    const raw=await host.tools.get('research_query').execute({collection:'evidence',material_ref:ref},
      {sessionID:'ses_main',messageID:`read-${index}`,id,agent:'build'})
    parts.push({type:'tool-result',id,name:'research_query',result:{type:'text',value:raw.content}})
  }
  const sql=`import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("SELECT object_version FROM publication_items WHERE item_id='missing'").fetchone()[0])`
  const object=spawnSync('python3',['-c',sql,join(project,'.research','state.sqlite3')],{encoding:'utf8'})
  expect(object.status).toBe(0)
  const objectPath=join(project,'.research','objects',object.stdout.trim())
  const original=readFileSync(objectPath)
  rmSync(objectPath)
  const request=modelRequest('ses_main')
  request.messages=[{role:'tool',content:parts}] as any
  const step=async (message:string) => {
    host.events.push({id:message,type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:message,
      model:{id:'scripted',providerID:'mock'}}})
    await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
      .some((item:any)=>item.source_key===`step:ses_main:${message}`),message)
    const current=modelRequest('ses_main'); current.messages=request.messages
    await host.hooks.get('context')!(current)
    return current
  }
  await step('first-response')
  const first=await host.tools.get('research_query').execute({collection:'evidence',refs:[good,missing],limit:16384},
    {sessionID:'ses_main',messageID:'first-response',id:'view-first',agent:'build'})
  const view=JSON.parse(JSON.parse(first.content).chunk)
  expect(view.materials.find((item:any)=>item.ref===good).material_read.complete).toBe(true)
  expect(view.materials.find((item:any)=>item.ref===missing).material_read?.complete).not.toBe(true)
  const next=await step('second-response')
  expect(contextText(next)).toContain(`material_unavailable ${missing}`)
  const second=await host.tools.get('research_query').execute({collection:'evidence',refs:[good,missing]},
    {sessionID:'ses_main',messageID:'second-response',id:'view-second',agent:'build'})
  expect(JSON.parse(second.content).snapshot_id).toBeTruthy()
  writeFileSync(objectPath,original)
  const fresh=await host.tools.get('research_query').execute({collection:'evidence',material_ref:missing},
    {sessionID:'ses_main',messageID:'second-response',id:'fresh-material',agent:'build'})
  request.messages=[{role:'tool',content:[...parts,{type:'tool-result',id:'fresh-material',name:'research_query',
    result:{type:'text',value:fresh.content}}]}] as any
  await step('third-response')
  const recovered=await host.tools.get('research_query').execute({collection:'evidence',refs:[missing],limit:16384},
    {sessionID:'ses_main',messageID:'third-response',id:'view-recovered',agent:'build'})
  const restored=JSON.parse(JSON.parse(recovered.content).chunk).materials.find((item:any)=>item.ref===missing)
  expect(restored.material_read.complete).toBe(true)
})

test('unrelated coordinator tools do not wait for a stalled material confirmation', async () => {
  const host=await load()
  await host.command('init','ses_main',{goal:'Keep unrelated tools usable'})
  writeFileSync(join(project,'material.txt'),'a synthetic source')
  const source=await host.call('research_publish','ses_main',{status:'complete',summary:'data',gaps:[],
    items:[{item_id:'data',kind:'text',source_path:'material.txt'}],knowledge_refs:[]})
  const ref=`pub/${source.publication_id}#data`
  const raw=await host.tools.get('research_query').execute({collection:'evidence',material_ref:ref},
    {sessionID:'ses_main',messageID:'read',id:'material',agent:'build'})
  const request=modelRequest('ses_main')
  request.messages=[{role:'tool',content:[{type:'tool-result',id:'material',name:'research_query',
    result:{type:'text',value:raw.content}}]}] as any
  host.events.push({id:'slow-material',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'slow-material',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
    .some((item:any)=>item.source_key==='step:ses_main:slow-material'),'slow material step')
  await host.hooks.get('context')!(request)
  const original=StorageClient.prototype.request
  let release!:(value:any)=>void
  const gate=new Promise(resolve=>{release=resolve})
  const spy=spyOn(StorageClient.prototype,'request').mockImplementation(function(method,fields,operation) {
    return method==='evidence_observed' ? gate : original.call(this,method,fields,operation)
  })
  try {
    const pending=host.tools.get('research_query').execute({collection:'nodes'},
      {sessionID:'ses_main',messageID:'slow-material',id:'unrelated',agent:'build'})
    const value=await Promise.race([pending.then(()=>true),Bun.sleep(2000).then(()=>false)])
    expect(value).toBe(true)
    const evidence=host.tools.get('research_query').execute({collection:'evidence',refs:[ref],limit:16384},
      {sessionID:'ses_main',messageID:'slow-material',id:'evidence-after-timeout',agent:'build'})
    const output=await Promise.race([evidence,Bun.sleep(4000).then(()=>null)])
    expect(output).not.toBeNull()
    const view=JSON.parse(JSON.parse(output.content).chunk)
    expect(view.materials.find((item:any)=>item.ref===ref).material_read.complete).toBe(false)
    host.events.push({id:'after-timeout',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'after-timeout',
      model:{id:'scripted',providerID:'mock'}}})
    await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
      .some((item:any)=>item.source_key==='step:ses_main:after-timeout'),'post-timeout step')
    const next=modelRequest('ses_main'); next.messages=request.messages
    await host.hooks.get('context')!(next)
    expect(contextText(next)).toContain(`backend_unavailable ${ref}`)
  } finally { release({receipt_ids:[],rejected:[]}); spy.mockRestore() }
},10000)

test('material delivery remains complete after earlier pages leave the model context', async () => {
  const host = await load()
  await host.command('init','ses_main',{goal:'Answer from a paged frozen file'})
  await host.command('conclusion-contract','ses_main',{contract:'evidence-v1'})
  writeFileSync(join(project,'paged.txt'),'0123456789')
  const source=await host.call('research_publish','ses_main',{status:'complete',summary:'data',
    items:[{item_id:'data',kind:'text',source_path:'paged.txt'}],knowledge_refs:[]})
  const ref=`pub/${source.publication_id}#data`
  const first=await host.tools.get('research_query').execute({collection:'evidence',material_ref:ref,limit:5},
    {sessionID:'ses_main',messageID:'read-first',id:'page-one',agent:'build'})
  const request1=modelRequest('ses_main')
  request1.messages=[{role:'tool',content:[{type:'tool-result',id:'page-one',name:'research_query',
    result:{type:'text',value:first.content}}]}] as any
  host.events.push({id:'page-step-1',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'page-response-1',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
    .some((item:any)=>item.source_key==='step:ses_main:page-response-1'),'first material step')
  await host.hooks.get('context')!(request1)
  const second=await host.tools.get('research_query').execute({collection:'evidence',cursor:JSON.parse(first.content).cursor,limit:5},
    {sessionID:'ses_main',messageID:'page-response-1',id:'page-two',agent:'build'})
  const request2=modelRequest('ses_main')
  request2.messages=[{role:'tool',content:[{type:'tool-result',id:'page-two',name:'research_query',
    result:{type:'text',value:second.content}}]}] as any
  host.events.push({id:'page-step-2',type:'session.step.started',data:{sessionID:'ses_main',assistantMessageID:'page-response-2',
    model:{id:'scripted',providerID:'mock'}}})
  await until(async () => (await host.call('research_query','ses_main',{collection:'usage'})).items
    .some((item:any)=>item.source_key==='step:ses_main:page-response-2'),'second material step')
  await host.hooks.get('context')!(request2)
  const view=await host.tools.get('research_query').execute({collection:'evidence',refs:[ref],limit:16384},
    {sessionID:'ses_main',messageID:'page-response-2',id:'read-view',agent:'build'})
  const material=JSON.parse(JSON.parse(view.content).chunk).materials.find((item:any)=>item.ref===ref)
  expect(material.material_read.complete).toBe(true)
  expect(material.material_read.covered_bytes).toBe(10)
})

test('node execution observation waits only after evidence conclusion is adopted', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'Compare execution observation'})
  host.turn.start('ses_main')
  const node=await host.call('research_propose','ses_main',{question:'Measure',why_now:'now',plan:'measure',root_reason:'first'})
  await host.call('research_dispatch','ses_main',{node_id:node.node_id})
  const child=host.created[0]!
  host.turn.start(child)
  const original=StorageClient.prototype.request
  const spy=spyOn(StorageClient.prototype,'request').mockImplementation(function(method,fields,operation) {
    return original.call(this,method,fields,operation)
  })
  try {
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'legacy-shell'})
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'legacy-shell',status:'completed'})
    expect(spy.mock.calls.filter(([method])=>method==='runtime_tool_observed')).toHaveLength(0)
    await host.command('conclusion-contract','ses_main',{contract:'evidence-v1'})
    await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'evidence-shell'})
    await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'evidence-shell',status:'completed'})
    expect(spy.mock.calls.filter(([method])=>method==='runtime_tool_observed')).toHaveLength(1)
  } finally { spy.mockRestore() }
})

test('warm recovery loads the evidence contract before allowing executable work', async () => {
  const first=await load()
  await first.command('auto','ses_main',{goal:'Recover execution observation'})
  await first.command('exploration','ses_main',{mode:'on'})
  first.turn.start('ses_main')
  const node=await first.call('research_propose','ses_main',{question:'Measure',why_now:'now',plan:'measure',root_reason:'first'})
  await first.call('research_dispatch','ses_main',{node_id:node.node_id})
  const child=first.created[0]!
  first.turn.start(child)
  await first.call('research_query',child,{collection:'nodes'})
  const original=StorageClient.prototype.request
  let unblock!:()=>void
  const gate=new Promise<void>(resolve=>{unblock=resolve})
  let delayed=false, observed=0
  const spy=spyOn(StorageClient.prototype,'request').mockImplementation(async function(method,fields,operation){
    if(method==='status' && !delayed){ delayed=true; await gate }
    if(method==='runtime_tool_observed') observed++
    return original.call(this,method,fields,operation)
  })
  try {
    const second=await load()
    await until(()=>delayed,'delayed status')
    let allowed=false
    const before=second.hooks.get('tool.execute.before')!({tool:'shell',sessionID:child,id:'after-warm'}).then(()=>{allowed=true})
    await Bun.sleep(100)
    expect(allowed).toBe(false)
    unblock()
    await before
    await second.hooks.get('tool.execute.after')!({tool:'shell',sessionID:child,id:'after-warm',status:'completed'})
    expect(observed).toBe(1)
  } finally {unblock();spy.mockRestore()}
},15000)

test('E3 tools materialize a shared probe and another route with one common allocation ledger', async () => {
  const host=await load()
  await host.command('auto','ses_main',{goal:'Multi-route engineering fixture'})
  await host.command('exploration','ses_main',{mode:'on'})
  await host.command('budget','ses_main',{limit:'work',value:'3',reason:'Bound the shared fixture'})
  await host.command('budget','ses_main',{limit:'resource',resource:'gpu:fixture',value:'1',reason:'Controlled measurement'})
  const scope=await host.call('research_scope','ses_main',{changes:{objective:'Choose a measured route',task_kind:'selection'}})
  const a=await host.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'A',rationale:'First route',critical_assumption:'Shared probe is representative'}})
  const b=await host.call('research_route','ses_main',{scope_ref:scope.ref,changes:{title:'B',rationale:'Second route',critical_assumption:'Shared probe is representative',relations:[{kind:'competes_with',route_ref:a.ref}]}})
  const other=await host.call('research_scope','ses_main',{changes:{objective:'Independent direction',task_kind:'discovery'}})
  const evidence=await host.call('research_query','ses_main',{collection:'evidence',scope_id:scope.scope_id,refs:[]})
  const decision=await host.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:evidence.snapshot_id,
    route_refs:[a.ref,b.ref],action:'probe',reason:'One shared measurement, then a route-specific probe',work_batch:[
      {key:'shared',budget_amount:1,resources:{'gpu:fixture':1},route_refs:[a.ref,b.ref],question:'Shared conditions?',why_now:'Both candidates need it',plan:'Measure once',root_reason:'Shared calibration'},
      {key:'specific',budget_amount:2,route_refs:[b.ref],question:'B target?',why_now:'B differs',plan:'Measure B',root_reason:'Independent probe'},
    ]})
  expect(decision.intent_ids).toHaveLength(2)
  expect(decision.works).toHaveLength(2)
  expect(host.created).toHaveLength(1)
  const budget=JSON.parse((await host.command('budget','ses_main')).message.split('\n').slice(1).join('\n'))
  expect(budget.exploration.allocated).toBe(3)
  expect(budget.exploration.allocations_total).toBe(2)
  const allocationPage=await host.call('research_query','ses_main',{collection:'exploration_budget'})
  expect(allocationPage.items.filter((item:any)=>item.kind==='allocation')).toHaveLength(2)
  expect(budget.exploration.actual_usage_status).toBe('unknown')
  const summary=await host.summary()
  expect(summary.exploration).toMatchObject({scopes_total:2,active_scopes_total:2,routes_total:2})
  expect(summary.exploration.scopes.map((item:any)=>item.scope_id)).toContain(other.scope_id)
  const context=modelRequest('ses_main')
  await host.hooks.get('context')!(context)
  expect(contextText(context)).toContain('exploration_scopes')
  expect(contextText(context)).toContain('scope_id')
})

describe('context placement', () => {
  function systemText(request: any): string {
    return request.system.map((part: any) => part.text ?? '').join('\n')
  }

  function lastMessageText(request: any): string {
    const last = request.messages.at(-1)
    return Array.isArray(last?.content) ? last.content.map((part: any) => part.text ?? '').join('\n') : ''
  }

  function cacheHints(request: any): any[] {
    const hints: any[] = []
    for (const part of request.system) if (part.cache) hints.push(part.cache)
    for (const message of request.messages) {
      for (const part of Array.isArray(message.content) ? message.content : []) if (part.cache) hints.push(part.cache)
    }
    return hints
  }

  function tailMessages(request: any): any[] {
    return request.messages.filter((message: any) =>
      Array.isArray(message?.content) && message.content.some((part: any) =>
        typeof part?.text === 'string' && part.text.startsWith('<research_state>\n')))
  }

  async function nodeContext(host: Awaited<ReturnType<typeof load>>, child: string, messages?: any[]) {
    const request = modelRequest(child)
    if (messages) request.messages = messages
    await host.hooks.get('context')!(request)
    return request
  }

  test('tail placement keeps a node system prefix stable across ledger writes', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'cache placement fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    host.events.push({ id: 'node-step', type: 'session.step.started',
      data: { sessionID: child, assistantMessageID: 'node-message', model: { id: 'actual', providerID: 'mock', variant: 'v' } } })
    await until(async () => (await host.call('research_query', child, { collection: 'usage' })).items.length === 1, 'node step registration')
    const before = await nodeContext(host, child)
    const systemBefore = JSON.stringify(before.system)
    const toolsBefore = JSON.stringify(before.tools)
    const tailBefore = lastMessageText(before)
    expect(systemBefore).toContain('## role_instructions')
    expect(systemBefore).not.toContain('## recent_node_notes')
    expect(tailBefore.startsWith('<research_state>\n')).toBe(true)
    expect(tailBefore).toContain('## recent_node_notes')
    expect(cacheHints(before).length).toBe(1)

    await host.call('research_note', child, { body: 'A measured note', kind: 'progress' })
    await host.call('research_record', child, { kind: 'hypothesis', statement: 'Observed behaviour',
      evidence_refs: [], visibility: 'node' })
    const checkpoint = await host.tools.get('research_memory').execute({ action: 'checkpoint', state: { step: 'one' } },
      { sessionID: child, messageID: 'node-message', id: 'node-checkpoint', agent: 'build' })
    expect(JSON.parse(checkpoint.content).revision).toBe(1)
    const after = await nodeContext(host, child)
    expect(JSON.stringify(after.system)).toBe(systemBefore)
    expect(JSON.stringify(after.tools)).toBe(toolsBefore)
    expect(lastMessageText(after)).not.toBe(tailBefore)
    expect(lastMessageText(after)).toContain('A measured note')
    expect(lastMessageText(after)).toContain('## checkpoint')
    expect(JSON.stringify(after.system)).not.toContain('## checkpoint')
    expect(JSON.stringify(after.messages)).not.toContain('A measured note\n\n## role_instructions')
  })

  test('tail placement keeps the coordinator prefix stable across proposal and dispatch', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'coordinator cache fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    const before = await nodeContext(host, 'ses_main')
    const systemBefore = JSON.stringify(before.system)
    const toolsBefore = JSON.stringify(before.tools)
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const after = await nodeContext(host, 'ses_main')
    expect(JSON.stringify(after.system)).toBe(systemBefore)
    expect(JSON.stringify(after.tools)).toBe(toolsBefore)
    expect(lastMessageText(after)).not.toBe(lastMessageText(before))
    expect(lastMessageText(after)).toContain('questions')
  })

  test('an existing history hint leaves the host array untouched and the tail is not replayed', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'history ownership fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    const history = [{
      role: 'assistant',
      content: [{ type: 'text', text: 'earlier result', cache: { type: 'ephemeral' } }],
    }]
    const snapshot = JSON.stringify(history)
    const request = modelRequest('ses_main')
    request.messages = history as any

    await host.hooks.get('context')!(request)

    expect(request.messages).not.toBe(history)
    expect(JSON.stringify(history)).toBe(snapshot)
    expect(tailMessages(request)).toHaveLength(1)
    expect(request.messages.at(-1)).toBe(tailMessages(request)[0]!)
    expect((request.messages[0] as any).content[0].cache).toEqual({ type: 'ephemeral' })

    // Reusing the host's original history must not replay the previous tail.
    const second = modelRequest('ses_main')
    second.messages = history as any
    await host.hooks.get('context')!(second)
    expect(JSON.stringify(history)).toBe(snapshot)
    expect(tailMessages(second)).toHaveLength(1)
    expect(second.messages.at(-1)).toBe(tailMessages(second)[0]!)
  })

  test('a newly added history hint does not mutate the host array', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'new hint ownership fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    const history = [{ role: 'assistant', content: [{ type: 'text', text: 'earlier result' }] }]
    const snapshot = JSON.stringify(history)
    const request = modelRequest('ses_main')
    request.messages = history as any

    await host.hooks.get('context')!(request)

    expect(request.messages).not.toBe(history)
    expect(JSON.stringify(history)).toBe(snapshot)
    expect((history[0]!.content[0] as any).cache).toBeUndefined()
    expect((request.messages[0] as any).content[0].cache).toEqual({ type: 'ephemeral' })
    expect((request.messages[0] as any).content[0].text).toBe('earlier result')
    expect(tailMessages(request)).toHaveLength(1)
    expect(request.messages.at(-1)).toBe(tailMessages(request)[0]!)
  })

  test('tail placement still accepts a checkpoint base delivered in context', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'checkpoint under tail placement' })
    host.events.push({ id: 'step-a', type: 'session.step.started',
      data: { sessionID: 'ses_main', assistantMessageID: 'assistant-a', model: { id: 'actual', providerID: 'mock', variant: 'v' } } })
    await until(async () => (await host.call('research_query', 'ses_main', { collection: 'usage' })).items.length === 1, 'native step registration')
    const request = modelRequest('ses_main')
    await host.hooks.get('context')!(request)
    const execute = (id: string) => host.tools.get('research_checkpoint').execute({ state: { next: id } },
      { sessionID: 'ses_main', messageID: 'assistant-a', id, agent: 'build' })
    const results = await Promise.allSettled([execute('one'), execute('parallel')])
    const fulfilled = results.find((row) => row.status === 'fulfilled') as PromiseFulfilledResult<any>
    expect(JSON.parse(fulfilled.value.content).revision).toBe(1)
    const next = modelRequest('ses_main')
    next.messages = [{ role: 'tool', content: [{ type: 'tool-result', id: results[0]!.status === 'fulfilled' ? 'one' : 'parallel',
      name: 'research_checkpoint', result: { type: 'text', value: fulfilled.value.content } }] }] as any
    host.events.push({ id: 'step-b', type: 'session.step.started',
      data: { sessionID: 'ses_main', assistantMessageID: 'assistant-b', model: { id: 'actual', providerID: 'mock' } } })
    await until(async () => (await host.call('research_query', 'ses_main', { collection: 'usage' })).items.length === 2, 'next native step')
    const delivered = (next.messages[0] as any).content[0]
    await host.hooks.get('context')!(next)
    const after = ((next.messages as any[]).find((message: any) =>
      message.content?.some?.((part: any) => part.type === 'tool-result'))).content[0]
    expect(after.result).toEqual(delivered.result)
    expect(after.id).toBe(delivered.id)
    expect(after.name).toBe(delivered.name)
    const saved = await host.tools.get('research_checkpoint').execute({ state: { next: 'two' } },
      { sessionID: 'ses_main', messageID: 'assistant-b', id: 'second', agent: 'build' })
    expect(JSON.parse(saved.content).revision).toBe(2)
  })

  test('an evidence warning travels in the tail under tail placement', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('init', 'ses_main', { goal: 'Warning placement fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    writeFileSync(join(project, 'source.txt'), 'measured material')
    const source = await host.call('research_publish', 'ses_main', { status: 'complete', summary: 'data', gaps: [],
      items: [{ item_id: 'data', kind: 'text', source_path: 'source.txt' }], knowledge_refs: [] })
    const ref = `pub/${source.publication_id}#data`
    await host.call('research_record', 'ses_main', { kind: 'claim', statement: 'Material supports the claim',
      evidence_refs: [ref], visibility: 'project' })
    const page = await host.tools.get('research_query').execute({ collection: 'evidence', refs: [ref], limit: 16384 },
      { sessionID: 'ses_main', messageID: 'read-decision', id: 'decision-page', agent: 'build' })
    host.events.push({ id: 'warning-step', type: 'session.step.started',
      data: { sessionID: 'ses_main', assistantMessageID: 'warning-response', model: { id: 'scripted', providerID: 'mock' } } })
    await until(async () => (await host.call('research_query', 'ses_main', { collection: 'usage' })).items.length > 0, 'warning step')
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function (method, fields, operation) {
      if (method === 'evidence_observed') return Promise.reject(new Error('injected evidence outage'))
      return original.call(this, method, fields, operation)
    })
    try {
      const request = modelRequest('ses_main')
      request.messages = [{ role: 'tool', content: [{ type: 'tool-result', id: 'decision-page', name: 'research_query',
        result: { type: 'text', value: page.content } }] }] as any
      await host.hooks.get('context')!(request)
      const tail = lastMessageText(request)
      expect(tail).toContain('Evidence delivery was not fully confirmed (backend_unavailable).')
      expect(systemText(request)).not.toContain('Evidence delivery was not fully confirmed')
    } finally { spy.mockRestore() }
  })

  for (const policy of ['legacy', 'r1'] as const) test(`omitted placement uses tail with ${policy} context`, async () => {
    const host = await load()
    await host.command('auto', 'ses_main', { goal: 'default tail placement fixture' })
    if (policy === 'r1') await host.command('context-policy', 'ses_main', { policy })
    const request = modelRequest('ses_main')
    await host.hooks.get('context')!(request)
    expect(systemText(request)).toContain('## role_instructions')
    expect(systemText(request)).not.toContain('## recent_node_notes')
    expect(tailMessages(request)).toHaveLength(1)
    expect(request.messages.at(-1)).toBe(tailMessages(request)[0]!)
    expect(lastMessageText(request)).toContain(policy === 'r1' ? '## recent_node_notes' : '## knowledge')
    const current = await host.command('context-policy', 'ses_main')
    expect(current.state.policy_version).toBe(policy === 'r1' ? 'memory-r1-v1' : 'memory-v2')
  })

  test('explicit system placement keeps the whole context in the system prompt', async () => {
    const host = await load({ placement: 'system' })
    await host.command('auto', 'ses_main', { goal: 'default placement fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    const request = modelRequest('ses_main')
    await host.hooks.get('context')!(request)
    expect(systemText(request)).toContain('## recent_node_notes')
    expect(systemText(request)).toContain('## role_instructions')
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
  })

  test('tail placement falls back to system when no legal history boundary exists', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'fallback fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    const request = modelRequest('ses_main')
    request.messages = [{ role: 'user', content: 'plain string content' }] as any
    await host.hooks.get('context')!(request)
    expect(systemText(request)).toContain('## recent_node_notes')
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
  })

  test('stale context keeps the original system placement and warning', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'stale placement fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    await host.hooks.get('context')!(modelRequest('ses_main'))
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function (method, fields, operation) {
      if (method === 'memory_context') return Promise.reject(new Error('injected ledger outage'))
      return original.call(this, method, fields, operation)
    })
    try {
      const request = modelRequest('ses_main')
      await host.hooks.get('context')!(request)
      expect(systemText(request)).toContain('[Research memory is stale.')
      expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    } finally { spy.mockRestore() }
  })

  test('a blind specialist receives no project tail under tail placement', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'Find X' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const node = host.created[0]!
    await until(() => host.said(node).length > 0, 'node kickoff')
    const workspace = /工作目录：(.+?)。/.exec(host.said(node).at(-1).text)![1]!
    mkdirSync(join(workspace, 'output'), { recursive: true })
    writeFileSync(join(workspace, 'output', 'report.md'), '# 结果\nblind evidence\n')
    const publication = await host.call('research_publish', node, { status: 'complete', summary: 'r',
      items: [{ item_id: 'report', kind: 'report', source_path: 'output/report.md' }] })
    const pending = host.call('research_delegate', node, { label: '盲评', question: '结论成立吗？',
      purpose: '独立复核', deliverable: '判断与依据', completion_criteria: '给出判断', report_requirements: '列出证据',
      context_mode: 'blind', inputs: [`pub/${publication.publication_id}#report`] })
    await until(() => host.created.length > 1 && host.said(host.created[1]!).length > 0, 'blind specialist')
    const child = host.created[1]!
    const request = modelRequest(child)
    await host.hooks.get('context')!(request)
    const system = systemText(request)
    expect(system).toContain('blind reviewer')
    expect(system).not.toContain('base system')
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    expect(JSON.stringify(request.messages)).not.toContain(publication.publication_id)
    expect(JSON.stringify(request.messages)).not.toContain('Find X')
    host.reply(child, '盲评：材料可见，结论成立。')
    expect((await pending).state).toBe('completed')
  })

  test('sessions outside a project are untouched under tail placement', async () => {
    const host = await load({ placement: 'tail' })
    const request = modelRequest('ses_plain')
    await host.hooks.get('context')!(request)
    expect(systemText(request)).toBe('base system')
    expect(request.messages).toHaveLength(1)
  })

  test('an unknown placement value warns once and falls back to system mode', async () => {
    const writes: string[] = []
    const spy = spyOn(process.stderr, 'write').mockImplementation(((chunk: any) => {
      writes.push(String(chunk))
      return true
    }) as any)
    try {
      const host = await load({ placement: 'turn' })
      await host.command('auto', 'ses_main', { goal: 'unknown placement fixture' })
      await host.command('context-policy', 'ses_main', { policy: 'r1' })
      for (let index = 0; index < 3; index++) {
        const request = modelRequest('ses_main')
        await host.hooks.get('context')!(request)
        expect(systemText(request)).toContain('## recent_node_notes')
        expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
      }
      const warnings = writes.filter((line) =>
        line.includes('Unknown contextPlacement') && line.includes('"turn"') && line.includes('"system"'))
      expect(warnings).toHaveLength(1)
    } finally { spy.mockRestore() }
  })

  test('a node turn without drain authorization keeps its refusal in the full system prompt', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'drain boundary fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const child = host.created[0]!
    host.turn.start(child)
    await host.command('pause', 'ses_main')
    await host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: child, id: 'old-turn', input: {} })
    await host.hooks.get('tool.execute.after')!({ tool: 'shell', sessionID: child, id: 'old-turn', status: 'completed', result: { metadata: {} } })
    host.turn.end(child)
    host.turn.start(child)
    const request = modelRequest(child)
    await host.hooks.get('context')!(request)
    expect(systemText(request)).toContain('no active drain authorization')
    // An unverifiable authorization keeps the safe full-system context: no
    // temporary tail and no placement-only hint.
    expect(systemText(request)).toContain('## recent_node_notes')
    expect(Object.keys(request.tools)).not.toContain('shell')
    expect(lastMessageText(request).startsWith('<research_state>')).toBe(false)
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
  })

  test('pausing keeps control instructions in the system prompt under tail placement', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'pause placement fixture' })
    await host.command('pause', 'ses_main')
    const request = modelRequest('ses_main')
    await host.hooks.get('context')!(request)
    expect(systemText(request)).toContain('## control_instructions')
    expect(systemText(request)).toContain('Research execution is paused')
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(true)
  })
})

describe('tail wait completion', () => {
  function systemText(request: any): string {
    return request.system.map((part: any) => part.text ?? '').join('\n')
  }

  function lastMessageText(request: any): string {
    const last = request.messages.at(-1)
    return Array.isArray(last?.content) ? last.content.map((part: any) => part.text ?? '').join('\n') : ''
  }

  function cacheHints(request: any): any[] {
    const hints: any[] = []
    for (const part of request.system) if (part.cache) hints.push(part.cache)
    for (const message of request.messages) {
      for (const part of Array.isArray(message.content) ? message.content : []) if (part.cache) hints.push(part.cache)
    }
    return hints
  }

  function waitResult(taskID: string) {
    return { role: 'tool', content: [{ type: 'tool-result', id: 'wait-call', name: 'research_wait',
      result: { type: 'text', value: JSON.stringify({ waiting: [taskID], message: '本轮结束后进入等待' }) } }] }
  }

  /** The plugin's process-local state, read through the shared controller registry. */
  function lifecycleState(sessionID: string): any {
    const registry = (globalThis as any)[Symbol.for('auto-research.controller.v1')] as Map<string, any> | undefined
    // The newest attachment for this session owns the live state; older test
    // attachments stay in the process-wide registry.
    for (const entry of [...(registry?.values() ?? [])].reverse()) {
      if (entry?.state?.turns?.has(sessionID)) return entry.state
    }
    return undefined
  }

  function currentMarker(sessionID: string): any {
    return lifecycleState(sessionID)?.waitFinishes?.get(sessionID)
  }

  /**
   * One generated model step with a real user input visible to it. The prompt
   * hook records the native input ID and the context request fixes the boundary
   * that a terminal call generated by this step will carry.
   */
  async function prepareStep(host: Awaited<ReturnType<typeof load>>, sessionID: string, inputID: string) {
    await host.prompt(sessionID, `input for ${inputID}`, inputID)
    const request = modelRequest(sessionID)
    request.messages = [{ role: 'user', id: inputID, content: [{ type: 'text', text: `input for ${inputID}` }] }] as any
    await host.hooks.get('context')!(request)
  }

  async function acceptedWait(host: Awaited<ReturnType<typeof load>>) {
    await host.command('auto', 'ses_main', { goal: 'wait completion fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await prepareStep(host, 'ses_main', 'msg_wait_step_input')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })
    return task
  }

  async function nodeAtFinish(host: Awaited<ReturnType<typeof load>>, accept = true) {
    await host.command('auto', 'ses_main', { goal: 'node finish completion fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await prepareStep(host, 'ses_main', 'msg_finish_step_input')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const node = host.created[0]!
    host.turn.start(node)
    await prepareStep(host, node, 'msg_node_step_input')
    await host.call('research_publish', node, { status: 'complete', summary: 'node result',
      items: [{ item_id: 'r', kind: 'text', content: { text: 'delivered result' } }] })
    if (accept) await host.call('research_finish', node, { state: 'finished', summary: 'node result' })
    return node
  }

  function finishHistory() {
    return [{ role: 'assistant', content: [
      { type: 'tool-call', id: 'query-call', name: 'research_query', arguments: { collection: 'nodes' } },
      { type: 'tool-call', id: 'finish-call', name: 'research_finish', arguments: { state: 'finished', summary: 'node result' } },
    ] }, { role: 'tool', content: [
      { type: 'tool-result', id: 'query-call', name: 'research_query', result: { type: 'text', value: 'nodes' } },
      { type: 'tool-result', id: 'finish-call', name: 'research_finish', result: { type: 'text', value: 'accepted' } },
    ] }]
  }

  test('an accepted node finish keeps sibling results and only commits after execution end', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    const history = finishHistory()
    const snapshot = JSON.stringify(history)
    for (let preparation = 0; preparation < 2; preparation++) {
      const request = modelRequest(node)
      request.messages = history as any
      await host.hooks.get('context')!(request)
      expect(request.messages).toBe(history)
      expect(JSON.stringify(history)).toBe(snapshot)
      expect(Object.keys(request.tools)).toHaveLength(0)
      expect(systemText(request)).toContain('A research finish was accepted')
      expect(systemText(request)).toContain('after this execution ends')
      expect(systemText(request)).toContain('## recent_node_notes')
      expect(JSON.stringify(request.messages)).not.toContain('<research_state>')
      expect(cacheHints(request)).toHaveLength(0)
    }
    const before = await host.command('status', 'ses_main')
    expect(before.state.workflow.tasks.find((task: any) => task.session_id === node).state).toBe('running')
    expect(host.said('ses_main').some((item) => item.text.includes('节点 X-001'))).toBe(false)
    host.turn.end(node)
    await until(() => host.said('ses_main').some((item) => !item.resume && item.text.includes('节点 X-001')),
      'node finish committed after execution end')
    const after = await host.page('tasks')
    expect(after.items).toHaveLength(1)
    expect(after.items[0].state).toBe('finished')
    const request = modelRequest(node)
    await host.hooks.get('context')!(request)
    expect(systemText(request)).not.toContain('A research finish was accepted')
  })

  test('a rejected node finish and transcript alone cannot latch finishing', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host, false)
    await expect(host.call('research_finish', node, { state: 'invalid', summary: 'node result' })).rejects.toThrow()
    const request = modelRequest(node)
    request.messages = finishHistory() as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_query')
    expect(lastMessageText(request).startsWith('<research_state>')).toBe(true)
    expect(systemText(request)).not.toContain('A research finish was accepted')
  })

  test('a mismatched authoritative node finish invalidates the marker', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    const call = Research.prototype.call
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      const result = await (call as any).call(this, method, ...args)
      return method === 'node_finish_pending' && result ? { ...result, turn_token: 'another-execution' } : result
    })
    try {
      const request = modelRequest(node)
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toContain('research_query')
      expect(systemText(request)).not.toContain('A research finish was accepted')
    } finally { spy.mockRestore() }
    const request = modelRequest(node)
    await host.hooks.get('context')!(request)
    expect(systemText(request)).not.toContain('A research finish was accepted')
  })

  test('a warm rebind retains an accepted node finish for the same execution', async () => {
    const first = await load({ placement: 'tail' })
    const node = await nodeAtFinish(first)
    const second = await load({ placement: 'tail' })
    const request = modelRequest(node)
    await second.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research finish was accepted')
  })

  test('system mode discards a node finish marker and tail rebind cannot resurrect it', async () => {
    const first = await load({ placement: 'tail' })
    const node = await nodeAtFinish(first)
    const system = await load({ placement: 'system' })
    const request = modelRequest(node)
    await system.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_query')
    expect(systemText(request)).not.toContain('A research finish was accepted')
    expect(JSON.stringify(request.messages)).not.toContain('<research_state>')
    const tail = await load({ placement: 'tail' })
    const next = modelRequest(node)
    await tail.hooks.get('context')!(next)
    expect(Object.keys(next.tools)).toContain('research_query')
    expect(lastMessageText(next).startsWith('<research_state>')).toBe(true)
    expect(systemText(next)).not.toContain('A research finish was accepted')
  })

  test('system mode does not latch an accepted node finish', async () => {
    const host = await load({ placement: 'system' })
    const node = await nodeAtFinish(host)
    const request = modelRequest(node)
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_query')
    expect(systemText(request)).not.toContain('A research finish was accepted')
  })

  test('an accepted wait keeps finishing until the execution ends', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const history = [waitResult(task.task_id)]
    const snapshot = JSON.stringify(history)
    const request = modelRequest('ses_main')
    request.messages = history as any

    await host.hooks.get('context')!(request)

    expect(request.messages).toBe(history)
    expect(JSON.stringify(history)).toBe(snapshot)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(lastMessageText(request)).toBe('')
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    expect(systemText(request)).toContain('A research wait was accepted')
    expect(systemText(request)).toContain('Do not call tools')
    expect(systemText(request)).toContain('## recent_node_notes')
    expect(systemText(request)).toContain('## checkpoint_recovery')
    expect(cacheHints(request)).toHaveLength(0)

    // A repeated preparation for the same active execution is still finishing.
    const repeated = modelRequest('ses_main')
    repeated.messages = [waitResult(task.task_id)] as any
    await host.hooks.get('context')!(repeated)
    expect(Object.keys(repeated.tools)).toHaveLength(0)
    expect(systemText(repeated)).toContain('A research wait was accepted')

    // A verified terminal transition clears the marker even before a new execution.
    host.turn.end('ses_main')
    await Bun.sleep(100)
    const afterEnd = modelRequest('ses_main')
    await host.hooks.get('context')!(afterEnd)
    expect(Object.keys(afterEnd.tools)).toContain('research_query')
    expect(systemText(afterEnd)).not.toContain('A research wait was accepted')

    // A later execution gets the normal placement too.
    host.turn.start('ses_main')
    await Bun.sleep(50)
    const next = modelRequest('ses_main')
    await host.hooks.get('context')!(next)
    expect(Object.keys(next.tools)).toContain('research_query')
    expect(lastMessageText(next).startsWith('<research_state>')).toBe(true)
    expect(systemText(next)).not.toContain('A research wait was accepted')
  })

  test('a failed wait does not latch a finishing request', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'failed wait fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    await expect(host.call('research_wait', 'ses_main', { task_ids: ['T_unknown'] }))
      .rejects.toThrow('只能等待自己派发的任务')

    const request = modelRequest('ses_main')
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_query')
    expect(lastMessageText(request).startsWith('<research_state>')).toBe(true)
    expect(systemText(request)).not.toContain('A research wait was accepted')
  })

  test('a mixed wait and query group keeps both results before finishing', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const query = await host.call('research_query', 'ses_main', { collection: 'nodes' })
    const history = [
      { role: 'tool', content: [{ type: 'tool-result', id: 'query-call', name: 'research_query',
        result: { type: 'text', value: JSON.stringify(query) } }] },
      waitResult(task.task_id),
    ]
    const snapshot = JSON.stringify(history)
    const request = modelRequest('ses_main')
    request.messages = history as any

    await host.hooks.get('context')!(request)

    expect(JSON.stringify(history)).toBe(snapshot)
    expect(JSON.stringify(request.messages)).toContain('nodes')
    expect(JSON.stringify(request.messages)).toContain('waiting')
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research wait was accepted')
  })

  test('a late wait result from an older execution cannot finish a newer execution', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    host.turn.end('ses_main')
    host.turn.start('ses_main')
    await Bun.sleep(50)

    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id)] as any
    await host.hooks.get('context')!(request)

    expect(Object.keys(request.tools)).toContain('research_query')
    expect(lastMessageText(request).startsWith('<research_state>')).toBe(true)
    expect(systemText(request)).not.toContain('A research wait was accepted')
  })

  test('a replayed older terminal event does not clear a newer wait marker', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'terminal replay fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    host.events.push({ id: 'older-terminal', type: 'session.execution.succeeded',
      data: { sessionID: 'ses_main' } })
    await Bun.sleep(100)

    // Execution B accepts a wait; then execution A's terminal event is replayed.
    host.turn.start('ses_main')
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })
    host.events.push({ id: 'older-terminal', type: 'session.execution.succeeded',
      data: { sessionID: 'ses_main' } })
    await Bun.sleep(100)

    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id)] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research wait was accepted')
    expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
  })

  test('a warm rebind keeps the accepted wait for the same execution', async () => {
    const first = await load({ placement: 'tail' })
    const task = await acceptedWait(first)
    const second = await load({ placement: 'tail' })
    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id)] as any

    await second.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research wait was accepted')

    second.turn.end('ses_main')
    second.turn.start('ses_main')
    await Bun.sleep(50)
    const next = modelRequest('ses_main')
    await second.hooks.get('context')!(next)
    expect(Object.keys(next.tools)).toContain('research_query')
    expect(lastMessageText(next).startsWith('<research_state>')).toBe(true)
  })

  test('a warm rebind to system mode discards a tail wait marker', async () => {
    const first = await load({ placement: 'tail' })
    const task = await acceptedWait(first)
    const system = await load({ placement: 'system' })
    const history = [waitResult(task.task_id)]
    const request = modelRequest('ses_main')
    request.messages = history as any

    await system.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_query')
    expect(systemText(request)).toContain('## recent_node_notes')
    expect(systemText(request)).not.toContain('A research wait was accepted')
    expect(request.messages).toBe(history)
    expect(JSON.stringify(request.messages)).not.toContain('<research_state>')

    // Switching back to tail must not resurrect the discarded marker.
    const tail = await load({ placement: 'tail' })
    const next = modelRequest('ses_main')
    next.messages = [waitResult(task.task_id)] as any
    await tail.hooks.get('context')!(next)
    expect(Object.keys(next.tools)).toContain('research_query')
    expect(lastMessageText(next).startsWith('<research_state>')).toBe(true)
    expect(systemText(next)).not.toContain('A research wait was accepted')
  })

  test('a report notification does not clear the marker and the wake runs once', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const node = host.created[0]!
    host.turn.start(node)
    await host.call('research_publish', node, { status: 'complete', summary: 'node result',
      items: [{ item_id: 'r', kind: 'text', content: { text: 'delivered result' } }] })
    await host.call('research_finish', node, { state: 'finished', summary: 'node result' })
    host.turn.end(node)
    await until(() => host.said('ses_main').some((item) => !item.resume && item.text.includes('节点 X-001')),
      'report notice without resume')

    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id)] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research wait was accepted')

    host.turn.end('ses_main')
    await until(() => host.said('ses_main').filter((item) => item.resume && item.text.includes('等待的节点有了结果')).length === 1,
      'single wake')
    const after = modelRequest('ses_main')
    await host.hooks.get('context')!(after)
    expect(Object.keys(after.tools)).toContain('research_query')
    expect(lastMessageText(after).startsWith('<research_state>')).toBe(true)
    expect(lastMessageText(after)).toContain('pub/P-001')
  })

  test('the finishing request keeps an evidence warning in the system prompt', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const spy = spyOn(Research.prototype, 'observeEvidence').mockResolvedValue(['timeout'])
    try {
      const request = modelRequest('ses_main')
      request.messages = [waitResult(task.task_id)] as any
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toHaveLength(0)
      expect(systemText(request)).toContain('Evidence delivery was not fully confirmed (timeout).')
      expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    } finally { spy.mockRestore() }
  })

  test('system mode still ends its turn normally after a wait', async () => {
    const host = await load({ placement: 'system' })
    await host.command('auto', 'ses_main', { goal: 'system wait fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })

    const request = modelRequest('ses_main')
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toContain('research_query')
    expect(systemText(request)).toContain('## recent_node_notes')
    expect(systemText(request)).not.toContain('A research wait was accepted')
    expect(lastMessageText(request)).not.toContain('<research_state>')
  })

  test('a transient drain error keeps the accepted node finish and stays read-only', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    const call = Research.prototype.call
    let failing = true
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (failing && method === 'runtime_drain_check') throw new Error('injected drain outage')
      return (call as any).call(this, method, ...args)
    })
    try {
      // A repeated transient failure must not dissolve the accepted barrier.
      for (let attempt = 0; attempt < 2; attempt++) {
        const request = modelRequest(node)
        request.messages = finishHistory() as any
        await host.hooks.get('context')!(request)
        expect(Object.keys(request.tools).sort()).toEqual(['read', 'research_query'])
        expect(systemText(request)).toContain('no active drain authorization')
        expect(systemText(request)).toContain('## recent_node_notes')
        expect(systemText(request)).not.toContain('A research finish was accepted')
        expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
        expect(currentMarker(node)).toMatchObject({ kind: 'finish' })
      }
      // The original accepted finish is still the one that resumes.
      failing = false
      const request = modelRequest(node)
      request.messages = finishHistory() as any
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toHaveLength(0)
      expect(systemText(request)).toContain('A research finish was accepted')
      expect(systemText(request)).toContain('after this execution ends')
      const status = await host.command('status', 'ses_main')
      expect(status.state.workflow.tasks.find((task: any) => task.session_id === node).state).toBe('running')
      expect(host.said('ses_main').some((item) => item.text.includes('节点 X-001'))).toBe(false)
    } finally { spy.mockRestore() }
  })

  test('a verified non-matching authoritative state only removes the marker it inspected', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    const call = Research.prototype.call
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      const result = await (call as any).call(this, method, ...args)
      return method === 'node_finish_pending' && result ? { ...result, turn_token: 'another-execution' } : result
    })
    try {
      const request = modelRequest(node)
      await host.hooks.get('context')!(request)
      expect(systemText(request)).not.toContain('A research finish was accepted')
      expect(currentMarker(node)).toBeUndefined()
    } finally { spy.mockRestore() }
  })

  test('a new execution during preparation never commits the old finishing decision', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const call = Research.prototype.call
    let started!: () => void
    const startedSignal = new Promise<void>(resolve => { started = resolve })
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'runtime_execution_start') started()
      return (call as any).call(this, method, ...args)
    })
    const memory = Research.prototype.memory
    const memorySpy = spyOn(Research.prototype, 'memory').mockImplementation(async function(this: Research, ...args: any[]) {
      host.turn.start('ses_main')
      await startedSignal
      return (memory as any).call(this, ...args)
    })
    try {
      const request = modelRequest('ses_main')
      request.messages = [waitResult(task.task_id)] as any
      await host.hooks.get('context')!(request)
      expect(systemText(request)).not.toContain('A research wait was accepted')
      expect(Object.keys(request.tools).sort()).toEqual(['read', 'research_query'])
      expect(systemText(request)).toContain('## recent_node_notes')
      expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    } finally {
      memorySpy.mockRestore()
      spy.mockRestore()
    }
  })

  test('an appended session event during preparation keeps the safe context', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const call = Research.prototype.call
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    let entered!: () => void
    const handlerEntered = new Promise<void>(resolve => { entered = resolve })
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'runtime_execution_bind') { entered(); await gate }
      return (call as any).call(this, method, ...args)
    })
    const memory = Research.prototype.memory
    const memorySpy = spyOn(Research.prototype, 'memory').mockImplementation(async function(this: Research, ...args: any[]) {
      host.events.push({ type: 'session.inbox.delivered', data: { sessionID: 'ses_main', inboxID: 'msg_late_delivery' } })
      await handlerEntered
      return (memory as any).call(this, ...args)
    })
    try {
      const request = modelRequest('ses_main')
      request.messages = [waitResult(task.task_id)] as any
      await host.hooks.get('context')!(request)
      expect(systemText(request)).not.toContain('A research wait was accepted')
      expect(Object.keys(request.tools).sort()).toEqual(['read', 'research_query'])
      expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    } finally {
      release()
      memorySpy.mockRestore()
      spy.mockRestore()
    }
  })

  /** Append a relevant event whose handler is still blocked when preparation ends. */
  function pendingEventDuringMemory(host: Awaited<ReturnType<typeof load>>, sessionID: string, inboxID: string) {
    const call = Research.prototype.call
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    let entered!: () => void
    const handlerEntered = new Promise<void>(resolve => { entered = resolve })
    const callSpy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'runtime_execution_bind') { entered(); await gate }
      return (call as any).call(this, method, ...args)
    })
    const memory = Research.prototype.memory
    const memorySpy = spyOn(Research.prototype, 'memory').mockImplementation(async function(this: Research, ...args: any[]) {
      host.events.push({ type: 'session.inbox.delivered', data: { sessionID, inboxID } })
      await handlerEntered
      return (memory as any).call(this, ...args)
    })
    return () => { release(); memorySpy.mockRestore(); callSpy.mockRestore() }
  }

  test('system mode keeps its baseline tools when an unrelated event is pending', async () => {
    const host = await load({ placement: 'system' })
    await host.command('auto', 'ses_main', { goal: 'system boundary fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    const baseline = modelRequest('ses_main')
    await host.hooks.get('context')!(baseline)
    const restore = pendingEventDuringMemory(host, 'ses_main', 'msg_unrelated_system')
    try {
      const request = modelRequest('ses_main')
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools).sort()).toEqual(Object.keys(baseline.tools).sort())
      expect(systemText(request)).toContain('## recent_node_notes')
      expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
    } finally { restore() }
  })

  test('markerless tail keeps its tools and tail when an unrelated event is pending', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'markerless boundary fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    const baseline = modelRequest('ses_main')
    await host.hooks.get('context')!(baseline)
    const restore = pendingEventDuringMemory(host, 'ses_main', 'msg_unrelated_tail')
    try {
      const request = modelRequest('ses_main')
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools).sort()).toEqual(Object.keys(baseline.tools).sort())
      expect(lastMessageText(request).startsWith('<research_state>')).toBe(true)
      expect(systemText(request)).not.toContain('Research control or the accepted terminal action changed')
    } finally { restore() }
  })

  test('a user prompt during an accepted wait is queued, preserved and never enters the old request', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const notes: any[] = []
    const call = Research.prototype.call
    const noteSpy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'note') notes.push(args)
      return (call as any).call(this, method, ...args)
    })
    try {
      expect(await host.prompt('ses_main', 'new user instruction', 'msg_user_wait')).toBe('queue')
      expect(host.prompts.at(-1)).toMatchObject({ messageID: 'msg_user_wait', delivery: 'queue' })
      expect(host.queuedPrompts.get('ses_main')).toEqual([{ messageID: 'msg_user_wait', text: 'new user instruction' }])
      // Nothing was filed while the prompt was still queued.
      expect(notes).toHaveLength(0)

      const request = modelRequest('ses_main')
      request.messages = [waitResult(task.task_id)] as any
      await host.hooks.get('context')!(request)
      expect(systemText(request)).toContain('A research wait was accepted')
      expect(Object.keys(request.tools)).toHaveLength(0)
      expect(JSON.stringify(request.system)).not.toContain('new user instruction')
      expect(JSON.stringify(request.messages)).not.toContain('new user instruction')
      expect(notes).toHaveLength(0)

      // The host delivers the held prompt at its next step.
      expect(host.deliverQueued('ses_main')).toMatchObject({ messageID: 'msg_user_wait', text: 'new user instruction' })
      await until(() => (lifecycleState('ses_main').queuedGuidance.get('ses_main') ?? []).every((item: any) => item.delivered),
        'queued guidance delivery mark')
      // Still queued behind the same execution: no note yet.
      expect(notes).toHaveLength(0)

      host.turn.end('ses_main')
      host.turn.start('ses_main')
      const next = modelRequest('ses_main')
      await host.hooks.get('context')!(next)
      expect(systemText(next)).not.toContain('A research wait was accepted')
      expect(JSON.stringify(next.messages)).toContain('new user instruction')
      expect(notes).toHaveLength(1)
      expect(notes[0]![1].body).toContain('new user instruction')
      expect(notes[0]![2]).toBe('guidance:ses_main:msg_user_wait')
      // Filing once is the contract: a repeated event must not add a note.
      const repeated = modelRequest('ses_main')
      await host.hooks.get('context')!(repeated)
      expect(notes).toHaveLength(1)
    } finally { noteSpy.mockRestore() }
  })

  test('a prompt in the finish acceptance window is queued and a failure leaves no routing', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host, false)
    const call = Research.prototype.call
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    let entered!: () => void
    const requestEntered = new Promise<void>(resolve => { entered = resolve })
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'node_finish_request') { entered(); await gate }
      return (call as any).call(this, method, ...args)
    })
    try {
      const accepted = host.call('research_finish', node, { state: 'finished', summary: 'node result' })
      await requestEntered
      expect(await host.prompt(node, 'arrived in the window', 'msg_finish_window')).toBe('queue')
      release()
      await accepted
      expect(currentMarker(node)).toMatchObject({ kind: 'finish' })
      // The queued input is still held, not merged into the finishing request.
      const request = modelRequest(node)
      request.messages = finishHistory() as any
      await host.hooks.get('context')!(request)
      expect(systemText(request)).toContain('A research finish was accepted')
      expect(JSON.stringify(request.messages)).not.toContain('arrived in the window')
    } finally { spy.mockRestore() }
  })

  test('a failed finish clears its in-flight registration and leaves no marker', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host, false)
    const call = Research.prototype.call
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'node_finish_request') throw new Error('injected finish outage')
      return (call as any).call(this, method, ...args)
    })
    try {
      await expect(host.call('research_finish', node, { state: 'finished', summary: 'node result' })).rejects.toThrow()
      expect(lifecycleState(node).terminalCalls.get(node)).toBeUndefined()
      expect(currentMarker(node)).toBeUndefined()
      expect(await host.prompt(node, 'after the failure', 'msg_after_failure')).toBe('steer')
      const request = modelRequest(node)
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toContain('research_query')
      expect(systemText(request)).not.toContain('A research finish was accepted')
      expect(lastMessageText(request).startsWith('<research_state>')).toBe(true)
    } finally { spy.mockRestore() }
  })

  test('a failed sibling terminal call does not clear an accepted wait', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'sibling terminal fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const results = await Promise.allSettled([
      host.call('research_wait', 'ses_main', { task_ids: [task.task_id] }),
      host.call('research_wait', 'ses_main', { task_ids: ['T_unknown'] }),
    ])
    expect(results.map(row => row.status).sort()).toEqual(['fulfilled', 'rejected'])
    expect(lifecycleState('ses_main').terminalCalls.get('ses_main')).toBeUndefined()
    const execution = lifecycleState('ses_main').turns.get('ses_main')
    expect(execution).toBeDefined()
    const marker = currentMarker('ses_main')
    expect(marker).toBeDefined()
    expect(marker.execution).toBe(execution)
    expect(marker.waiting).toEqual([task.task_id])
    expect(marker.kind).toBeUndefined()

    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id)] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research wait was accepted')
    // The successful sibling's marker survives the request and its sibling's failure.
    expect(currentMarker('ses_main')).toBe(marker)
    expect(marker.waiting).toEqual([task.task_id])
  })

  test('system mode queues nothing and keeps its guidance timing', async () => {
    const host = await load({ placement: 'system' })
    await host.command('auto', 'ses_main', { goal: 'system handoff fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })
    const notes: any[] = []
    const call = Research.prototype.call
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'note') notes.push(args)
      return (call as any).call(this, method, ...args)
    })
    try {
      expect(await host.prompt('ses_main', 'system guidance', 'msg_system')).toBe('steer')
      expect(notes).toHaveLength(1)
      expect(notes[0]![1].body).toContain('system guidance')
      expect(host.queuedPrompts.get('ses_main')).toBeUndefined()
    } finally { spy.mockRestore() }
  })

  test('a session outside research keeps its prompt untouched', async () => {
    const host = await load({ placement: 'tail' })
    expect(await host.prompt('ses_plain', 'ordinary note', 'msg_plain')).toBe('steer')
    expect(host.queuedPrompts.get('ses_plain')).toBeUndefined()
  })

  test('an older preparation never removes a marker installed while it awaited', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    const replacement = { execution: 'newer-execution', waiting: [], kind: 'finish' as const }
    const call = Research.prototype.call
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      const result = await (call as any).call(this, method, ...args)
      if (method === 'node_finish_pending') lifecycleState(node).waitFinishes.set(node, replacement)
      return result
    })
    try {
      const request = modelRequest(node)
      request.messages = finishHistory() as any
      await host.hooks.get('context')!(request)
      expect(systemText(request)).not.toContain('A research finish was accepted')
      expect(currentMarker(node)).toBe(replacement)
    } finally { spy.mockRestore() }
  })

  test('a finished node cannot regain shell access through a new user execution', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    host.turn.end(node)
    await until(() => host.said('ses_main').some((item) => !item.resume && item.text.includes('节点 X-001')),
      'node finish committed')
    host.turn.start(node)
    const request = modelRequest(node)
    await host.hooks.get('context')!(request)
    // The closed work segment stays authoritative for new work.
    await expect(host.hooks.get('tool.execute.before')!({ tool: 'shell', sessionID: node, id: 'after-finish', input: {} }))
      .rejects.toBeDefined()
  })

  test('a marker installed during preparation invalidates the old decision', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'late marker fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const memory = Research.prototype.memory
    let installed: any
    const memorySpy = spyOn(Research.prototype, 'memory').mockImplementation(async function(this: Research, ...args: any[]) {
      // A wait is accepted while this preparation is still awaiting memory.
      await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })
      installed = lifecycleState('ses_main').waitFinishes.get('ses_main')
      return (memory as any).call(this, ...args)
    })
    try {
      expect(currentMarker('ses_main')).toBeUndefined()
      const request = modelRequest('ses_main')
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools).sort()).toEqual(['read', 'research_query'])
      expect(systemText(request)).toContain('## recent_node_notes')
      expect(systemText(request)).not.toContain('A research wait was accepted')
      expect(request.messages.some((message: any) => JSON.stringify(message).includes('<research_state>'))).toBe(false)
      // The marker installed during the await is kept as the same object.
      expect(installed).toBeDefined()
      expect(installed.execution).toBe(lifecycleState('ses_main').turns.get('ses_main'))
      expect(installed.waiting).toEqual([task.task_id])
      expect(currentMarker('ses_main')).toBe(installed)
    } finally { memorySpy.mockRestore() }
  })

  test('an attachment-only user prompt is queued during an accepted wait', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const notes: any[] = []
    const call = Research.prototype.call
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'note') notes.push(args)
      return (call as any).call(this, method, ...args)
    })
    try {
      const files = [{ uri: 'file:///tmp/note.txt', name: 'note.txt', mime: 'text/plain' }]
      expect(await host.prompt('ses_main', '', 'msg_attachment', files)).toBe('queue')
      expect(host.queuedPrompts.get('ses_main')).toEqual([{ messageID: 'msg_attachment', text: '', files }])
      expect(notes).toHaveLength(0)

      const request = modelRequest('ses_main')
      request.messages = [waitResult(task.task_id)] as any
      await host.hooks.get('context')!(request)
      expect(systemText(request)).toContain('A research wait was accepted')
      expect(JSON.stringify(request.messages)).not.toContain('note.txt')
    } finally { spy.mockRestore() }
  })

  test('an in-flight terminal call from an earlier execution does not queue a new execution', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host, false)
    const tokenA = lifecycleState(node).turns.get(node)
    const call = Research.prototype.call
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    let entered!: () => void
    const requestEntered = new Promise<void>(resolve => { entered = resolve })
    const spy = spyOn(Research.prototype, 'call').mockImplementation(async function(this: Research, method: string, ...args: any[]) {
      if (method === 'node_finish_request') { entered(); await gate }
      return (call as any).call(this, method, ...args)
    })
    try {
      const accepted = host.call('research_finish', node, { state: 'finished', summary: 'node result' })
      await requestEntered
      host.turn.end(node)
      host.turn.start(node)
      await until(() => lifecycleState(node).turns.get(node) !== tokenA, 'the next execution')
      expect(await host.prompt(node, 'input for the new execution', 'msg_new_execution')).toBe('steer')
      expect(host.queuedPrompts.get(node)).toBeUndefined()
      release()
      await accepted.catch(() => {})
      // The late result cannot latch the new execution.
      expect(currentMarker(node)).toBeUndefined()
    } finally {
      release()
      spy.mockRestore()
    }
  })

  test('an input before the node finish registration is answered by the post-terminal step', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'node generation window fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await prepareStep(host, 'ses_main', 'msg_main_step_input')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    const node = host.created[0]!
    host.turn.start(node)
    await prepareStep(host, node, 'msg_node_base')
    await host.call('research_publish', node, { status: 'complete', summary: 'node result',
      items: [{ item_id: 'r', kind: 'text', content: { text: 'delivered result' } }] })
    // The input arrives after the generating step and before the finish is registered.
    expect(await host.prompt(node, 'node input in the window', 'msg_node_window', undefined, true)).toBe('steer')
    await host.call('research_finish', node, { state: 'finished', summary: 'node result' })
    const marker = currentMarker(node)
    expect(marker).toBeDefined()
    expect(host.deliverQueued(node)).toMatchObject({ messageID: 'msg_node_window' })

    const request = modelRequest(node)
    request.messages = [
      { role: 'user', id: 'msg_node_base', content: [{ type: 'text', text: 'input for msg_node_base' }] },
      ...finishHistory(),
      { role: 'user', id: 'msg_node_window', content: [{ type: 'text', text: 'node input in the window' }] },
    ] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A user message arrived while this terminal action was still pending')
    expect(systemText(request)).not.toContain('A research finish was accepted')
    expect(currentMarker(node)).toBe(marker)
  })

  test('a newer preparation cannot replace an older step input boundary', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'step boundary fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await prepareStep(host, 'ses_main', 'msg_step_base')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    // A newer preparation sees a held input that the generating step never saw.
    expect(await host.prompt('ses_main', 'not yet delivered', 'msg_prepared_later', undefined, true)).toBe('steer')
    const newer = modelRequest('ses_main')
    newer.messages = [
      { role: 'user', id: 'msg_step_base', content: [{ type: 'text', text: 'input for msg_step_base' }] },
      { role: 'user', id: 'msg_prepared_later', content: [{ type: 'text', text: 'not yet delivered' }] },
    ] as any
    await host.hooks.get('context')!(newer)
    // The wait call is still generated by the older step; its boundary comes
    // from the transcript, not from the newest preparation.
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })
    // The held input is delivered by the next step; the wait was generated by the
    // older step, so this is a new input for the finishing decision.
    expect(host.deliverQueued('ses_main')).toMatchObject({ messageID: 'msg_prepared_later' })
    const finishing = modelRequest('ses_main')
    finishing.messages = [
      waitResult(task.task_id),
      { role: 'user', id: 'msg_step_base', content: [{ type: 'text', text: 'input for msg_step_base' }] },
      { role: 'user', id: 'msg_prepared_later', content: [{ type: 'text', text: 'not yet delivered' }] },
    ] as any
    await host.hooks.get('context')!(finishing)
    expect(systemText(finishing)).toContain('A user message arrived while this terminal action was still pending')
    expect(systemText(finishing)).not.toContain('A research wait was accepted')
  })

  test('a marker without an input boundary is handled conservatively', async () => {
    const host = await load({ placement: 'tail' })
    const node = await nodeAtFinish(host)
    // Simulate the 9424fb0 warm-state shape: a marker stored before the input
    // boundary existed.
    const legacy = { ...currentMarker(node) }
    delete (legacy as any).step
    lifecycleState(node).waitFinishes.set(node, legacy)
    expect(await host.prompt(node, 'message after the upgrade', 'msg_legacy_new')).toBe('queue')

    const withInput = modelRequest(node)
    withInput.messages = [
      ...finishHistory(),
      { role: 'user', id: 'msg_legacy_new', content: [{ type: 'text', text: 'message after the upgrade' }] },
    ] as any
    await host.hooks.get('context')!(withInput)
    expect(Object.keys(withInput.tools)).toHaveLength(0)
    expect(systemText(withInput)).toContain('A user message arrived while this terminal action was still pending')
    expect(systemText(withInput)).not.toContain('A research finish was accepted')

    // Without any real user input there is nothing to answer: the normal
    // finishing instruction remains correct.
    const withoutInput = modelRequest(node)
    withoutInput.messages = finishHistory() as any
    await host.hooks.get('context')!(withoutInput)
    expect(systemText(withoutInput)).toContain('A research finish was accepted')
    expect(currentMarker(node)).toBe(legacy)
  })

  test('a long queue does not forget an undelivered input', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    for (let index = 0; index < 40; index++) {
      expect(await host.prompt('ses_main', `queued instruction ${index}`, `msg_queue_${index}`)).toBe('queue')
    }
    expect(host.deliverQueued('ses_main')).toMatchObject({ messageID: 'msg_queue_0' })
    const request = modelRequest('ses_main')
    request.messages = [
      waitResult(task.task_id),
      { role: 'user', id: 'msg_wait_step_input', content: [{ type: 'text', text: 'input for msg_wait_step_input' }] },
      { role: 'user', id: 'msg_queue_0', content: [{ type: 'text', text: 'queued instruction 0' }] },
    ] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A user message arrived while this terminal action was still pending')
    expect(systemText(request)).not.toContain('A research wait was accepted')
  })

  test('a synthetic wake is not mistaken for a new user input', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id),
      { role: 'user', id: 'msg_research_wake', content: [{ type: 'text', text: '【Research 自动推进】等待的节点有了结果' }] }] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A research wait was accepted')
    expect(systemText(request)).not.toContain('A user message arrived while this terminal action was still pending')
  })

  test('a terminal call generated after the input was visible does not answer it again', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    expect(await host.prompt('ses_main', 'late instruction', 'msg_late_step')).toBe('queue')
    host.deliverQueued('ses_main')
    // The post-terminal step ends; the next step already sees the input.
    const step = modelRequest('ses_main')
    step.messages = [
      { role: 'user', id: 'msg_wait_step_input', content: [{ type: 'text', text: 'input for msg_wait_step_input' }] },
      { role: 'user', id: 'msg_late_step', content: [{ type: 'text', text: 'late instruction' }] },
    ] as any
    await host.hooks.get('context')!(step)
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })

    const finishing = modelRequest('ses_main')
    finishing.messages = step.messages
    await host.hooks.get('context')!(finishing)
    expect(Object.keys(finishing.tools)).toHaveLength(0)
    expect(systemText(finishing)).toContain('A research wait was accepted')
    expect(systemText(finishing)).not.toContain('A user message arrived while this terminal action was still pending')
  })

  test('a delivered attachment-only prompt also replaces the finishing instruction', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const files = [{ uri: 'file:///tmp/note.txt', name: 'note.txt', mime: 'text/plain' }]
    expect(await host.prompt('ses_main', '', 'msg_attachment_step', files)).toBe('queue')
    expect(host.deliverQueued('ses_main')).toMatchObject({ messageID: 'msg_attachment_step', text: '' })
    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id),
      { role: 'user', id: 'msg_attachment_step', content: [{ type: 'attachment', name: 'note.txt' }] }] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A user message arrived while this terminal action was still pending')
    expect(systemText(request)).not.toContain('A research wait was accepted')
    expect((lifecycleState('ses_main').queuedGuidance.get('ses_main') ?? [])).toHaveLength(0)
  })

  test('a delivered queued prompt replaces the finishing instruction on its step', async () => {
    const host = await load({ placement: 'tail' })
    const task = await acceptedWait(host)
    const marker = currentMarker('ses_main')
    expect(marker).toBeDefined()
    expect(await host.prompt('ses_main', 'late instruction', 'msg_late_step')).toBe('queue')
    host.deliverQueued('ses_main')
    await until(() => (lifecycleState('ses_main').queuedGuidance.get('ses_main') ?? []).every((item: any) => item.delivered),
      'queued guidance delivery mark')
    const request = modelRequest('ses_main')
    request.messages = [waitResult(task.task_id),
      { role: 'user', id: 'msg_late_step', content: [{ type: 'text', text: 'late instruction' }] }] as any
    await host.hooks.get('context')!(request)
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect(systemText(request)).toContain('A user message arrived while this terminal action was still pending')
    expect(systemText(request)).not.toContain('A research wait was accepted')
    // The barrier is unchanged: the accepted wait keeps its identity and content.
    expect(currentMarker('ses_main')).toBe(marker)
    expect(marker.execution).toBe(lifecycleState('ses_main').turns.get('ses_main'))
    expect(marker.waiting).toEqual([task.task_id])
  })

  test('an input that arrived during model generation is answered by the post-terminal step', async () => {
    const host = await load({ placement: 'tail' })
    await host.command('auto', 'ses_main', { goal: 'generation window fixture' })
    await host.command('context-policy', 'ses_main', { policy: 'r1' })
    host.turn.start('ses_main')
    await prepareStep(host, 'ses_main', 'msg_generation_base')
    await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
    const task = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
    // The input arrives after the generating step, before the wait is registered:
    // no terminal window exists yet, so the host keeps its normal delivery.
    expect(await host.prompt('ses_main', 'generation window input', 'msg_generation_window', undefined, true)).toBe('steer')
    await host.call('research_wait', 'ses_main', { task_ids: [task.task_id] })
    const marker = currentMarker('ses_main')
    expect(marker).toBeDefined()
    // The host delivers the held input at the next step.
    expect(host.deliverQueued('ses_main')).toMatchObject({ messageID: 'msg_generation_window' })

    for (let preparation = 0; preparation < 2; preparation++) {
      const request = modelRequest('ses_main')
      request.messages = [
        waitResult(task.task_id),
        { role: 'user', id: 'msg_generation_base', content: [{ type: 'text', text: 'input for msg_generation_base' }] },
        { role: 'user', id: 'msg_generation_window', content: [{ type: 'text', text: 'generation window input' }] },
      ] as any
      await host.hooks.get('context')!(request)
      expect(Object.keys(request.tools)).toHaveLength(0)
      expect(systemText(request)).toContain('A user message arrived while this terminal action was still pending')
      expect(systemText(request)).not.toContain('A research wait was accepted')
      // A repeated preparation does not consume the new input.
      expect(currentMarker('ses_main')).toBe(marker)
    }
  })
})

for (const mode of ["init-auto", "auto"]) {
  test(`multiline research goal survives ${mode} and starts generation one`, async () => {
    const host = await load()
    const goal = "研究目标：FA 微小误差\n\n级联传播机制\n" + "完整实验约束与交付要求。".repeat(30)
    if (mode === "init-auto") {
      const initialized = await host.command("init", "ses_main", { goal })
      expect(initialized.error).toBeUndefined()
      expect((await host.summary()).run.state).toBe("manual")
      await host.command("auto", "ses_main")
    } else await host.command("auto", "ses_main", { goal })
    const summary = await host.summary()
    expect(summary.project.goal).toBe(goal)
    expect(summary.run.state).toBe("running")
    expect(summary.run.generation).toBe(1)
    const kickoffs = host.said("ses_main").filter((item: any) => item.resume)
    expect(kickoffs).toHaveLength(1)
    expect(kickoffs[0].text).toBe(`【Research 自动推进】开始自主研究。目标：${goal}\n先盘点已有材料和记录，然后规划节点、派发执行；结果回来后综合，目标回答后发布最终报告并调用 research_conclude。`)
  })
}

test("node session creation sanitizes controls in research-derived titles", async () => {
  const titles: string[] = []
  const host = await load({ hostSetup: host => {
    const create = host.ctx.session.create
    host.ctx.session.create = async (input: any) => {
      titles.push(input.title)
      if (/[\u0000-\u001f\u007f-\u009f]/u.test(input.title)) throw new Error("native terminal rejected title")
      return create(input)
    }
  } })
  await host.command("auto", "ses_main", { goal: "Find X" })
  await host.call("research_propose", "ses_main", { question: "FA\0gate\x1b down\n投影？", why_now: "w", plan: "p", root_reason: "r" })
  await host.call("research_dispatch", "ses_main", { node_id: "X-001" })
  await until(() => host.created.length === 1, "safe node title")
  expect(titles).toHaveLength(1)
  expect(titles[0]).not.toMatch(/[\u0000-\u001f\u007f-\u009f]/u)
  expect(titles[0]).toContain("投影")
})

describe('research session location boundary', () => {
  async function nodeFixture() {
    const host = await load()
    await host.command('auto', 'ses_main', {goal: 'location regression'})
    await host.call('research_propose', 'ses_main', {question: 'Q', why_now: 'test', plan: 'test', root_reason: 'test'})
    await host.call('research_dispatch', 'ses_main', {node_id: 'X-001'})
    const child = host.created[0]!
    host.turn.start(child)
    const sub = join(project, 'workspaces', 'drift')
    mkdirSync(sub, {recursive: true})
    return {host, child, sub}
  }

  test('blocks direct and nested host move at the inner hook; ordinary execute remains usable', async () => {
    const {host, child, sub} = await nodeFixture()
    const before = host.hooks.get('tool.execute.before')!
    await before({tool: 'execute', sessionID: child, id: 'outer', input: {code: 'dynamic code'}})
    for (const tool of ['opencode_session_move']) {
      await expect(before({tool, sessionID: child, id: tool, input: {directory: sub}})).rejects.toThrow('research_session_move_unsupported')
    }
    await before({tool: 'opencode_session_move', sessionID: 'ordinary', id: 'ordinary', input: {directory: sub}})
    expect(host.locations.get(child)).toBe(project)
  })

  test('foreign registration diagnoses drift without granting identity or driving events', async () => {
    const {host, child, sub} = await nodeFixture()
    host.locations.set(child, sub)
    const workspace = await load({directory: sub})
    const request = modelRequest(child)
    await workspace.hooks.get('context')!(request)
    expect(JSON.stringify(request.system)).toContain('research_session_location')
    expect(Object.keys(request.tools)).toHaveLength(0)
    await expect(workspace.hooks.get('tool.execute.before')!({tool: 'shell', sessionID: child, id: 'drift'})).rejects.toThrow('research_session_location')
    await expect(workspace.call('research_finish', child, {state: 'finished', summary: 'forbidden'})).rejects.toThrow('research_session_location')
    workspace.turn.end(child)
    await Bun.sleep(60)
    expect(workspace.synthetic).toHaveLength(0)
    expect((await host.page('tasks')).items[0].state).toBe('running')
    const ordinary = modelRequest('ordinary')
    await workspace.hooks.get('context')!(ordinary)
    expect(Object.keys(ordinary.tools)).not.toContain('research_finish')
    expect(Object.keys(ordinary.tools)).toContain('shell')
    const unrelatedDirectory = join(dir, 'unrelated-project')
    mkdirSync(unrelatedDirectory)
    const unrelated = await load({directory: unrelatedDirectory})
    const foreign = modelRequest(child)
    await unrelated.hooks.get('context')!(foreign)
    expect(Object.keys(foreign.tools)).toHaveLength(0)
    expect(JSON.stringify(foreign.system)).toContain('research_session_location')
    await expect(unrelated.call('research_finish', child, {state: 'finished', summary: 'foreign project'})).rejects.toThrow('research_session_location')
    const stranger = modelRequest('unregistered-session')
    await unrelated.hooks.get('context')!(stranger)
    expect(Object.keys(stranger.tools)).not.toContain('research_finish')
    expect(Object.keys(stranger.tools)).toContain('shell')
  })

  test('first terminal after drift pauses with a diagnosis once; resume is refused before any new reservation', async () => {
    const {host, child, sub} = await nodeFixture()
    host.locations.set(child, sub)
    host.reply(child, 'no research tools', 'succeeded')
    const terminal = {id: 'location-terminal', type: 'session.execution.succeeded', data: {sessionID: child}}
    host.events.push(terminal); host.events.push(terminal)
    await until(async () => (await host.rpc().session({sessionID: child})).pauseReason === 'fault', 'location pause')
    expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
    expect(host.said(child).filter(row => row.text.includes('research_session_location'))).toHaveLength(1)
    expect((await host.page('tasks')).items[0].state).toBe('running')
    const refused = await host.command('resume', 'ses_main')
    expect(refused.error).toContain('research_session_location')
    expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
    host.locations.set(child, project)
    expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
    await host.call('research_publish', child, {status: 'complete', summary: 'recovered', gaps: [], claims: [], artifacts: [], review: {status: 'unreviewed', refs: [], limitations: ['test']}})
    const context = modelRequest(child)
    await host.hooks.get('context')!(context)
    const seen = await host.call('research_query', child, {collection: 'checkpoint'})
    await host.call('research_checkpoint', child, {base: seen.base, state: {next: 'finish'}})
    await host.call('research_finish', child, {state: 'finished', summary: 'recovered'})
    host.turn.end(child)
    await until(async () => (await host.page('tasks')).items[0].state === 'finished', 'recovered finish')
    expect((await host.page('publications')).items).toHaveLength(1)
  })

  test('a stale root context and cached research tool cannot execute after a host-side move', async () => {
    const {host, child, sub} = await nodeFixture()
    host.locations.set(child, sub)
    const request = modelRequest(child)
    await host.hooks.get('context')!(request)
    expect(JSON.stringify(request.system)).toContain('research_session_location')
    expect(Object.keys(request.tools)).toHaveLength(0)
    await expect(host.call('research_finish', child, {state: 'finished', summary: 'stale'})).rejects.toThrow('research_session_location')
    host.locations.set(child, project)
    const recovered = modelRequest(child)
    await host.hooks.get('context')!(recovered)
    expect(Object.keys(recovered.tools)).toContain('research_finish')
  })
  test('a missing registered tool entry stops the first continuation', async () => {
    const {host, child} = await nodeFixture()
    host.tools.delete('research_finish')
    host.reply(child, 'cannot finish', 'succeeded')
    host.turn.end(child)
    await until(async () => (await host.rpc().session({sessionID: child})).pauseReason === 'fault', 'tool entry pause')
    expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
    expect(host.said(child).some(row => row.text.includes('research_tool_entry_unavailable'))).toBe(true)
    expect((await host.command('resume', 'ses_main')).error).toContain('research_tool_entry_unavailable')
  })

  test('root and workspace attachments handle a duplicate terminal through one controller only', async () => {
    const {host, child, sub} = await nodeFixture()
    const workspace = await load({directory: sub})
    await host.call('research_finish', child, {state: 'finished', summary: 'one result'})
    host.reply(child, 'done', 'succeeded')
    const event = {id: 'shared-terminal', type: 'session.execution.succeeded', data: {sessionID: child}}
    host.events.push(event); workspace.events.push(event); host.events.push(event)
    await until(async () => (await host.page('tasks')).items[0].state === 'finished', 'one finish')
    await until(() => host.said('ses_main').some(row => row.text.includes('节点 X-001 已完成')), 'one notification')
    expect(host.said('ses_main').filter(row => row.text.includes('节点 X-001 已完成'))).toHaveLength(1)
    expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
    expect(workspace.synthetic).toHaveLength(0)
    expect((await host.page('tasks')).items).toHaveLength(1)
  })

  test('unrelated missing tools do not block budget queries, budget changes or existing reads', async () => {
    const {host, child} = await nodeFixture()
    host.tools.delete('research_dispatch')
    expect((await host.command('budget', 'ses_main')).error).toBeUndefined()
    expect((await host.command('budget', 'ses_main', {limit: 'turns', value: '10', reason: 'repair configuration'})).error).toBeUndefined()
    host.tools.delete('research_finish')
    await host.hooks.get('tool.execute.before')!({tool: 'read', sessionID: child, id: 'existing-read'})
    expect((await host.call('research_query', child, {collection: 'nodes'})).items).toHaveLength(1)
    expect((await host.command('pause', 'ses_main')).error).toBeUndefined()
    expect((await host.command('stop', 'ses_main')).error).toBeUndefined()
  })

  test('a gap in the actual model catalog pauses immediately without another reservation', async () => {
    const {host, child} = await nodeFixture()
    const request = modelRequest(child)
    delete request.tools.research_finish
    await host.hooks.get('context')!(request)
    expect(JSON.stringify(request.system)).toContain('research_tool_entry_unavailable')
    expect(Object.keys(request.tools)).toHaveLength(0)
    expect((await host.rpc().session({sessionID: child})).pauseReason).toBe('fault')
    host.reply(child, 'diagnosis only', 'succeeded')
    host.turn.end(child)
    await until(() => host.said('ses_main').some(row => row.text.includes('research_tool_entry_unavailable')), 'tool gap notification')
    expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
    expect((await host.page('tasks')).items[0].state).toBe('running')
    expect((await host.command('resume', 'ses_main')).error).toBeUndefined()
    const recovered = modelRequest(child)
    await host.hooks.get('context')!(recovered)
    expect(Object.keys(recovered.tools)).toContain('research_finish')
  })

  test('one continuation checks host location and registered catalog only at final preflight', async () => {
    const {host, child} = await nodeFixture()
    await host.call('research_query', child, {collection: 'nodes'})
    const get = spyOn(host.ctx.session, 'get')
    const list = spyOn(host.ctx.tool, 'list')
    try {
      host.reply(child, 'continue once', 'succeeded')
      host.turn.end(child)
      await until(() => host.said(child).filter(row => row.resume).length === 2, 'one continuation')
      expect(get.mock.calls.filter((args: any[]) => args[0].sessionID === child)).toHaveLength(1)
      expect(list).toHaveBeenCalledTimes(1)
    } finally { get.mockRestore(); list.mockRestore() }
  })

  test('resume does not require relocating an unverified node that it cannot restart', async () => {
    const {host, child, sub} = await nodeFixture()
    await host.call('research_query', child, {collection: 'nodes'})
    host.locations.set(child, sub)
    cleanups.pop()!()
    simulateDeadHost()
    const restarted = await load()
    await until(async () => (await restarted.summary()).run.state === 'paused', 'cold recovery')
    expect((await restarted.command('resume', 'ses_main')).error).toBeUndefined()
    expect((await restarted.page('tasks')).items[0].state).toBe('unverified')
    expect(restarted.said(child).filter(row => row.resume)).toHaveLength(0)
    expect(restarted.locations.get(child)).toBe(sub)
  })

  test('takeover rejects a subdirectory before changing research membership', async () => {
    const host = await load()
    await host.command('init', 'ses_main', {goal: 'takeover location fixture'})
    const sub = join(project, 'subdirectory'); mkdirSync(sub)
    host.locations.set('ses_replacement', sub)
    const result = await host.command('takeover', 'ses_replacement')
    expect(result.error).toContain('研究根目录')
    expect(result.error).toContain('接管')
    expect((await host.rpc().session({sessionID: 'ses_replacement'})).associated).toBe(false)
    expect((await host.summary()).run.main_session_id).toBe('ses_main')
    host.locations.set('ses_replacement', project)
    expect((await host.command('takeover', 'ses_replacement')).error).toBeUndefined()
    expect((await host.summary()).run.main_session_id).toBe('ses_replacement')
  })

  test('entry diagnostics stay tool-free if persisting the fault fails', async () => {
    const {host, child} = await nodeFixture()
    const request = modelRequest(child); delete request.tools.research_finish
    const original = StorageClient.prototype.request
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function(method, fields, operation): any {
      if (method === 'workflow' && fields?.action === 'session' && (fields?.fields as any)?.pause_reason === 'fault') return Promise.reject(new Error('pause persistence failed'))
      return original.call(this, method, fields, operation)
    })
    try {
      await expect(host.hooks.get('context')!(request)).rejects.toThrow('pause persistence failed')
      expect(Object.keys(request.tools)).toHaveLength(0)
      expect(JSON.stringify(request.system)).toContain('research_tool_entry_unavailable')
    } finally { spy.mockRestore() }
    expect((await host.rpc().session({sessionID: child})).pauseReason).not.toBe('fault')
    expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
  })

  test('a continuation fault preserves a budget pause written during its preflight', async () => {
    const {host, child} = await nodeFixture()
    await host.call('research_query', child, {collection: 'nodes'})
    const original = StorageClient.prototype.request
    let injected = false
    const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(async function(method, fields, operation): Promise<any> {
      if (method === 'runtime_reserve' && fields?.session_id === child && !injected) {
        injected = true
        await original.call(this, 'workflow', {...fields, action: 'session', fields: {pause_reason: 'budget'}}, 'concurrent-budget-pause')
        throw Object.assign(new Error('research_tool_entry_unavailable: concurrent catalog gap'), {code: 'research_tool_entry_unavailable'})
      }
      return original.call(this, method, fields, operation)
    })
    try {
      host.reply(child, 'done for this turn', 'succeeded'); host.turn.end(child)
      await until(() => injected, 'budget pause during submission')
      await host.call('research_query', child, {collection: 'nodes'})
      expect((await host.rpc().session({sessionID: child})).pauseReason).toBe('budget')
      expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
      expect(host.said(child).some(row => row.text.includes('concurrent catalog gap'))).toBe(false)
    } finally { spy.mockRestore() }
  })

  for (const failure of ['location', 'catalog']) test(`${failure} lookup failure blocks the first continuation`, async () => {
    const {host, child} = await nodeFixture()
    const target = failure === 'location' ? host.ctx.session : host.ctx.tool
    const spy = spyOn(target, failure === 'location' ? 'get' : 'list').mockRejectedValue(new Error('host lookup failed'))
    try {
      if (failure === 'location') {
        const request = modelRequest(child); await host.hooks.get('context')!(request)
        expect(Object.keys(request.tools)).toHaveLength(0)
        expect(JSON.stringify(request.system)).toContain('research_session_location')
      }
      host.reply(child, 'no further work', 'succeeded'); host.turn.end(child)
      await until(async () => (await host.rpc().session({sessionID: child})).pauseReason === 'fault', 'lookup failure pause')
      expect(host.said(child).filter(row => row.resume)).toHaveLength(1)
      expect(host.said(child).some(row => row.text.includes(failure === 'location' ? 'host session lookup failed' : 'host tool catalog unavailable'))).toBe(true)
    } finally { spy.mockRestore() }
  })

})


test('foreign membership has a bounded diagnostic cache with expiry and registry invalidation', async () => {
  const first = new StorageClient({registryPath: registry}), second = new StorageClient({registryPath: registry})
  try {
    const creator = new Research(second, project, () => existsSync(registry))
    await creator.init('ses_main', project, 'foreign cache fixture')
    const sub = join(project, 'subdirectory'); mkdirSync(sub)
    const observer = new Research(first, sub, () => existsSync(registry)); await observer.ready()
    const original = first.request.bind(first)
    let lookups = 0
    first.request = ((method, fields, operation) => {
      if (method === 'session_membership') lookups++
      return original(method, fields, operation)
    }) as typeof first.request
    await observer.confirmMembership('ses_main'); await observer.confirmMembership('ses_main')
    expect(lookups).toBe(1)
    expect(observer.registeredRootOf('ses_main')).toBe(creator.scope)
    expect(observer.isAssociated('ses_main')).toBe(false)
    expect(observer.roots()).toHaveLength(0)
    await Bun.sleep(1050); await observer.confirmMembership('ses_main')
    expect(lookups).toBe(2)
    await creator.detach('ses_main'); await observer.confirmMembership('ses_main')
    expect(lookups).toBe(3)
    expect(observer.registeredRootOf('ses_main')).toBeUndefined()
  } finally { first.close(); second.close() }
})

test('auto without guidance resumes a paused generation without resetting budget or opening a new generation', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'Resume via auto' })
  await until(() => host.said('ses_main').some((row) => row.resume), 'initial kickoff')
  host.reply('ses_main', 'turn 1')
  host.turn.end('ses_main')
  const beforePause = await host.summary()
  const genBefore = beforePause.run.generation
  const turnsBefore = beforePause.runtime_budget.turns
  expect(turnsBefore).toBeGreaterThanOrEqual(1)
  expect((await host.command('pause', 'ses_main')).error).toBeUndefined()
  expect((await host.summary()).run.state).toBe('paused')
  const resumed = await host.command('auto', 'ses_main', { goal: null })
  expect(resumed.error).toBeUndefined()
  const afterResume = await host.summary()
  expect(afterResume.run.state).toBe('running')
  expect(afterResume.run.generation).toBe(genBefore)
  expect(afterResume.runtime_budget.turns).toBeGreaterThanOrEqual(turnsBefore)
})

test('read-only board summary exposes unverified coordinator deliveries through the real Python path', async () => {
  let dropped = false
  const host = await load({
    hostSetup: (raw) => {
      const original = raw.ctx.session.synthetic
      raw.ctx.session.synthetic = async (input: any) => {
        if (!dropped && String(input.description ?? '').includes('收到节点消息')) {
          dropped = true
          throw new Error('simulated transport drop during report delivery')
        }
        return original(input)
      }
    },
  })
  await host.command('auto', 'ses_main', { goal: 'Board delivery visibility' })
  await host.call('research_propose', 'ses_main', { question: 'Q?', why_now: 'w', plan: 'p', root_reason: 'r' })
  await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
  const child = host.created[0]!
  await until(() => host.said(child).length > 0, 'node kickoff')
  const workspace = /工作目录：(.+?)。/.exec(host.said(child).at(-1).text)![1]!
  mkdirSync(join(workspace, 'output'), { recursive: true })
  writeFileSync(join(workspace, 'output', 'report.md'), '# X-001\nresult\n')
  host.turn.start(child)
  const pub = await host.call('research_publish', child, {
    status: 'complete',
    summary: 'node 1 result',
    items: [{ item_id: 'report', kind: 'report', source_path: 'output/report.md' }],
  })
  await host.call('research_memory', child, {
    action: 'record',
    kind: 'observation',
    statement: 'X holds under A',
    evidence_refs: [`pub/${pub.publication_id}#report`],
  })
  const seen = await host.call('research_query', child, { collection: 'checkpoint' })
  await host.call('research_memory', child, { action: 'checkpoint', base: seen.base, state: { next: 'finish' } })
  await host.call('research_finish', child, { state: 'finished', summary: 'node 1 result' })
  host.turn.end(child)

  let summary: any
  await until(async () => {
    if (!dropped) return false
    summary = await host.rpc().board({ view: 'summary', directory: project })
    return Array.isArray(summary?.deliveries) && summary.deliveries.length === 1
  }, 'unverified delivery visible on board summary')
  expect(summary.deliveries[0]).toMatchObject({ kind: 'report', state: 'unverified' })
  const overview = overviewModel(summary, null)!
  expect(overview.notices.some((line) => line.includes('/research recover delivery'))).toBe(true)

  const resolved = await host.command('recover', 'ses_main', {
    kind: 'delivery',
    delivery_kind: 'report',
    notification_id: summary.deliveries[0].notification_id,
    disposition: 'delivered',
    reason: 'checked host messages',
  })
  expect(resolved.error).toBeUndefined()
  const cleared = await host.rpc().board({ view: 'summary', directory: project })
  expect(cleared.deliveries).toEqual([])
})

test('auto without guidance resumes an interrupted session while the project run is still running, and enforces budget caps before resuming', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'Running-project session recovery' })
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 1, 'initial kickoff')
  host.turn.start('ses_main')
  host.events.push({ id: 'esc-1', type: 'session.execution.interrupted', data: { sessionID: 'ses_main' } })
  await until(async () => (await host.rpc().session({ sessionID: 'ses_main' })).pauseReason === 'native_stop', 'native_stop pause')
  expect((await host.summary()).run.state).toBe('running')

  const resumedFromEsc = await host.command('auto', 'ses_main', { goal: null })
  expect(resumedFromEsc.error).toBeUndefined()
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 2, 'resumed after native_stop')

  await host.command('budget', 'ses_main', { limit: 'turns', value: '2', reason: 'cap at current turns' })
  host.turn.start('ses_main')
  host.events.push({ id: 'turn-2-done', type: 'session.execution.succeeded', data: { sessionID: 'ses_main' } })
  await until(async () => (await host.rpc().session({ sessionID: 'ses_main' })).pauseReason === 'budget', 'budget pause')
  expect((await host.summary()).run.state).toBe('running')

  const refusedRunning = await host.command('auto', 'ses_main', { goal: null })
  expect(refusedRunning.error).toContain('预算已到限')
  expect((await host.rpc().session({ sessionID: 'ses_main' })).pauseReason).toBe('budget')

  await host.command('pause', 'ses_main')
  expect((await host.summary()).run.state).toBe('paused')
  const refusedPaused = await host.command('auto', 'ses_main', { goal: null })
  expect(refusedPaused.error).toContain('预算已到限')
  expect((await host.summary()).run.state).toBe('paused')

  await host.command('budget', 'ses_main', { limit: 'turns', value: '3', reason: 'explicitly raise cap' })
  const resumedAfterRaise = await host.command('auto', 'ses_main', { goal: null })
  expect(resumedAfterRaise.error).toBeUndefined()
  expect((await host.summary()).run.state).toBe('running')
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 3, 'resumed after raising budget')
})

test('board summary omits has_host_main when membership loading fails or times out so takeover is not falsely offered', async () => {
  const host = await load()
  await host.command('init', 'ses_main', { goal: 'Membership failure safety' })
  const normal = await host.rpc().board({ view: 'summary', directory: project })
  expect(normal.has_host_main).toBe(true)

  const original = StorageClient.prototype.request
  let mode: 'reject' | 'timeout_ready' | 'timeout_session' | 'normal' = 'reject'
  let releaseReady!: () => void
  let releaseSession!: () => void
  let readyPending = false
  let sessionPending = false
  const spy = spyOn(StorageClient.prototype, 'request').mockImplementation(function (method, fields, operation) {
    if (method === 'host_sessions' && mode === 'reject') {
      return Promise.reject(new Error('registry unavailable'))
    }
    if (method === 'host_sessions' && mode === 'timeout_ready') {
      readyPending = true
      return new Promise<void>((resolve) => { releaseReady = resolve }).then(() => {
        readyPending = false
        return original.call(this, method, fields, operation)
      })
    }
    if (method === 'session_membership' && mode === 'timeout_session') {
      sessionPending = true
      return new Promise<void>((resolve) => { releaseSession = resolve }).then(() => {
        sessionPending = false
        return original.call(this, method, fields, operation)
      })
    }
    return original.call(this, method, fields, operation)
  })
  try {
    const rejected = await load()
    const rejectedSummary = await rejected.rpc().board({ view: 'summary', directory: project })
    expect(rejectedSummary.project?.goal).toBe('Membership failure safety')
    expect(rejectedSummary.has_host_main).toBeUndefined()

    mode = 'timeout_ready'
    const slowReady = await load()
    const timedOutReady = await slowReady.rpc().board({ view: 'summary', directory: project })
    expect(readyPending).toBe(true)
    expect(timedOutReady.project?.goal).toBe('Membership failure safety')
    expect(timedOutReady.has_host_main).toBeUndefined()
    mode = 'normal'
    releaseReady()
    await until(() => !readyPending, 'delayed host_sessions settled')
    const afterReady = await slowReady.rpc().board({ view: 'summary', directory: project })
    expect(afterReady.has_host_main).toBe(true)

    mode = 'timeout_session'
    const timedOutSession = await slowReady.rpc().board({
      view: 'summary',
      sessionID: 'ses_unseen',
      directory: project,
    })
    expect(sessionPending).toBe(true)
    expect(timedOutSession.has_host_main).toBeUndefined()
    mode = 'normal'
    releaseSession()
    await until(() => !sessionPending, 'delayed session_membership settled')
  } finally {
    spy.mockRestore()
  }
})

test('session-required RPC errors never point to removed slash commands', async () => {
  const host = await load()
  for (const [action, expected] of [
    ['init', '/research auto --pause'],
    ['takeover', '/research recover takeover'],
    ['retry', '/research recover retry'],
    ['budget', '/research budget'],
    ['detach', '请先进入一个会话再执行该操作。'],
  ] as const) {
    const res = await host.rpc().command({ action, directory: project, args: {} })
    expect(res.error).toContain(expected)
    if (action !== 'budget') expect(res.error).not.toContain(`/research ${action}`)
  }
})

test('orchestrator autonomously enables exploration in auto mode and respects user off override', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'Compare two indexing architectures' })
  const view0 = await host.command('exploration', 'ses_main', { mode: null })
  expect(view0.state).toMatchObject({
    exploration_mode: 'auto',
    exploration_policy: 'off',
    conclusion_contract: 'legacy-v1',
  })
  expect(view0.message).toContain('探索控制：auto')

  const initial = modelRequest('ses_main')
  await host.hooks.get('context')!(initial)
  expect(Object.keys(initial.tools)).toContain('research_exploration')
  expect(Object.keys(initial.tools)).not.toContain('research_scope')
  expect(Object.keys(initial.tools)).not.toContain('research_decide')
  expect(Object.keys(initial.tools)).not.toContain('research_prepare_conclusion')

  // User override off hides research_exploration and rejects stale calls.
  const disabled = await host.command('exploration', 'ses_main', { mode: 'off' })
  expect(disabled.state).toMatchObject({ exploration_mode: 'off', exploration_policy: 'off' })
  const offReq = modelRequest('ses_main')
  await host.hooks.get('context')!(offReq)
  expect(Object.keys(offReq.tools)).not.toContain('research_exploration')
  await expect(
    host.call('research_exploration', 'ses_main', { action: 'enable', reason: 'Try while off' }),
  ).rejects.toThrow(/disabled by user policy/i)

  // Restore auto mode, then let the orchestrator autonomously enable exploration with an initial scope.
  const backAuto = await host.command('exploration', 'ses_main', { mode: 'auto' })
  expect(backAuto.state).toMatchObject({ exploration_mode: 'auto', exploration_policy: 'off' })
  const enabled = await host.call('research_exploration', 'ses_main', {
    action: 'enable',
    reason: 'Two competing candidate architectures require bounded comparison',
    scope: { objective: 'Select indexing architecture', task_kind: 'selection' },
  })
  expect(enabled).toMatchObject({
    action: 'enable',
    exploration_mode: 'auto',
    exploration_policy: 'exploration-v1',
    context_policy: 'memory-r1-v1',
    conclusion_contract: 'evidence-v1',
  })
  expect(enabled.scope.scope_id).toBe('ES-001')

  const activeReq = modelRequest('ses_main')
  await host.hooks.get('context')!(activeReq)
  expect(Object.keys(activeReq.tools)).toContain('research_scope')
  expect(Object.keys(activeReq.tools)).toContain('research_route')
  expect(Object.keys(activeReq.tools)).toContain('research_decide')
  expect(Object.keys(activeReq.tools)).toContain('research_prepare_conclusion')
  expect(Object.keys(activeReq.tools)).toContain('research_exploration')
  expect((activeReq.tools.research_exploration.input as any).properties.action.enum).toEqual(['ask'])
  expect(Object.keys((activeReq.tools.research_conclude.input as any).properties).sort()).toEqual([
    'draft_id',
    'final_ref',
    'summary',
  ])
})

test('orchestrator proactive ask pauses at awaiting_user and user chat reply automatically resumes continuation', async () => {
  const host = await load()
  await host.command('auto', 'ses_main', { goal: 'Evaluate high-cost benchmark vs synthetic probe' })
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 1, 'initial kickoff')

  host.turn.start('ses_main')
  const asked = await host.call('research_exploration', 'ses_main', {
    action: 'ask',
    reason: 'Full benchmark costs 8 GPU-hours while a synthetic probe is cheap',
    question: '先跑合成探针还是直接跑完整基准？',
    options: ['先跑合成探针', '直接跑完整基准'],
  })
  expect(asked).toMatchObject({ action: 'ask', state: 'awaiting_user', paused: true })

  // Post-ask request in the same turn is read-only and surfaces pending_question.
  const postAsk = modelRequest('ses_main')
  await host.hooks.get('context')!(postAsk)
  expect(Object.keys(postAsk.tools)).toContain('read')
  expect(Object.keys(postAsk.tools)).toContain('research_query')
  expect(Object.keys(postAsk.tools)).not.toContain('research_propose')
  expect(Object.keys(postAsk.tools)).not.toContain('research_dispatch')
  expect(Object.keys(postAsk.tools)).not.toContain('research_exploration')
  expect(Object.keys(postAsk.tools)).not.toContain('research_record')
  expect(contextText(postAsk)).toContain('先跑合成探针还是直接跑完整基准？')

  host.turn.end('ses_main')
  await Bun.sleep(50)
  const pausedSession = await host.rpc().session({ sessionID: 'ses_main' })
  expect(pausedSession.pauseReason).toBe('awaiting_user')
  expect(host.said('ses_main').filter((row) => row.resume)).toHaveLength(1)
  expect(
    associatedStatus({
      project: { goal: 'G' },
      workflow: { run: { state: 'running' }, session: { role: 'main', pause_reason: 'awaiting_user' } },
    }),
  ).toContain('等待用户回复')

  // If the user explicitly runs /research pause, an ordinary chat message must NOT auto-resume.
  await host.command('pause', 'ses_main')
  expect((await host.summary()).run.state).toBe('paused')
  await host.prompt('ses_main', '暂停期间的备注', 'msg_while_paused')
  host.turn.end('ses_main')
  await Bun.sleep(50)
  expect((await host.summary()).run.state).toBe('paused')
  expect(host.said('ses_main').filter((row) => row.resume)).toHaveLength(1)

  // Resume with /research auto, then ask again while running and reply directly in chat.
  const resumedExplicit = await host.command('auto', 'ses_main', { goal: null })
  expect(resumedExplicit.error).toBeUndefined()
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 2, 'explicit resume kickoff')

  host.turn.start('ses_main')
  await host.call('research_exploration', 'ses_main', {
    action: 'ask',
    reason: 'Confirm metric priority before committing probes',
    question: '优先优化 p99 延迟还是吞吐？',
    options: ['p99 延迟', '吞吐'],
  })
  host.turn.end('ses_main')
  await Bun.sleep(50)
  expect((await host.rpc().session({ sessionID: 'ses_main' })).pauseReason).toBe('awaiting_user')
  expect(host.said('ses_main').filter((row) => row.resume)).toHaveLength(2)

  // Direct user chat reply clears awaiting_user, records guidance, restores tools, and resumes loop after turn!
  await host.prompt('ses_main', '优先优化 p99 延迟，先用小探针验证。', 'msg_user_answer_1')
  expect((await host.rpc().session({ sessionID: 'ses_main' })).pauseReason).toBeNull()

  const resumedReq = modelRequest('ses_main')
  await host.hooks.get('context')!(resumedReq)
  expect(Object.keys(resumedReq.tools)).toContain('research_propose')
  expect(Object.keys(resumedReq.tools)).toContain('research_dispatch')
  expect(Object.keys(resumedReq.tools)).toContain('research_exploration')
  expect(contextText(resumedReq)).not.toContain('pending_question')

  host.turn.start('ses_main')
  await host.call('research_propose', 'ses_main', {
    question: 'Does p99 tail drop under synthetic load?',
    why_now: 'User confirmed p99 priority',
    plan: 'Run bounded probe',
    root_reason: 'Initial probe',
  })
  host.turn.end('ses_main')
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 3, 'auto-continued after user reply')
})

test('exploration review regressions: legacy off migration, waiting tool gate, manual ask, and /research auto guidance resume', async () => {
  // 1) Upgraded legacy DB with explicit exploration_policy='off' (and no exploration_mode) stays off & hides tool.
  const host = await load({ placement: 'system' })
  await host.command('init', 'ses_main', { goal: 'Legacy migration and waiting gate check' })
  const dbPath = join(project, '.research', 'state.sqlite3')
  const { Database } = await import('bun:sqlite')
  const db = new Database(dbPath)
  db.run('UPDATE project SET config = ?', [JSON.stringify({ exploration_policy: 'off' })])
  db.close()

  const migrated = await host.command('exploration', 'ses_main', { mode: null })
  expect(migrated.state).toMatchObject({ exploration_mode: 'off', exploration_policy: 'off' })
  const legacyReq = modelRequest('ses_main')
  await host.hooks.get('context')!(legacyReq)
  expect(Object.keys(legacyReq.tools)).not.toContain('research_exploration')
  await expect(
    host.call('research_exploration', 'ses_main', { action: 'enable', reason: 'Should fail on legacy off' }),
  ).rejects.toThrow(/disabled by user policy/i)

  // Opt in to auto on this project, verify manual ask does NOT leave a sticky pending_question after user reply.
  await host.command('exploration', 'ses_main', { mode: 'auto' })
  const manualAsked = await host.call('research_exploration', 'ses_main', {
    action: 'ask',
    reason: 'Manual clarification',
    question: '手动模式下用哪套数据集？',
    options: ['Dataset A', 'Dataset B'],
  })
  expect(manualAsked).toMatchObject({ paused: false, state: 'manual' })
  await host.prompt('ses_main', '用 Dataset A', 'msg_manual_reply')
  const manualAfterReq = modelRequest('ses_main')
  await host.hooks.get('context')!(manualAfterReq)
  expect(contextText(manualAfterReq)).not.toContain('pending_question')
  host.turn.end('ses_main')

  // 2) Start autonomous run, enter `wait` on a dispatched node: `research_exploration` is hidden and direct calls
  //    reject with a clear waiting message (not misleading `/research auto`).
  await host.command('auto', 'ses_main', { goal: '' })
  await until(() => host.said('ses_main').filter((row) => row.resume).length === 1, 'kickoff')
  host.turn.start('ses_main')
  await host.call('research_propose', 'ses_main', {
    question: 'Q-wait?',
    why_now: 'Initial work',
    plan: 'Run node',
    root_reason: 'Root',
  })
  const dispatched = await host.call('research_dispatch', 'ses_main', { node_id: 'X-001' })
  await host.call('research_wait', 'ses_main', { task_ids: [dispatched.task_id] })

  const waitReq = modelRequest('ses_main')
  await host.hooks.get('context')!(waitReq)
  expect(Object.keys(waitReq.tools)).not.toContain('research_exploration')
  let waitErr = ''
  try {
    await host.call('research_exploration', 'ses_main', { action: 'ask', reason: 'During wait', question: 'Q?' })
  } catch (error: any) {
    waitErr = error?.message ?? String(error)
  }
  expect(waitErr).toContain('waiting on dispatched node tasks')
  expect(waitErr).not.toContain('/research auto')
  host.turn.end('ses_main')

  // Finish node X-001 so main wakes cleanly.
  const nodeSid = host.created[0]!
  await host.call('research_publish', nodeSid, {
    status: 'complete',
    summary: 'done',
    items: [{ item_id: 'note', kind: 'text', content: { text: 'ok' } }],
  })
  await host.call('research_finish', nodeSid, { state: 'finished', summary: 'done' })
  host.turn.end(nodeSid)
  await until(() => host.said('ses_main').some((item) => item.text.includes('节点 X-001 已完成')), 'wake after wait')
  await until(async () => (await host.rpc().session({ sessionID: 'ses_main' })).pauseReason == null, 'wait cleared')

  // 3) Pause at `awaiting_user` and resume via `/research auto <guidance>` (both normal and when `note` fails).
  host.turn.start('ses_main')
  await host.call('research_exploration', 'ses_main', {
    action: 'ask',
    reason: 'Need route choice',
    question: '选方案 A 还是方案 B？',
    options: ['方案 A', '方案 B'],
  })
  host.turn.end('ses_main')
  await until(
    async () => (await host.rpc().session({ sessionID: 'ses_main' })).pauseReason === 'awaiting_user',
    'awaiting_user set 1',
  )

  // Inject failure on `runtime_resume_awaiting_user` during an ordinary chat reply: failure notice + `/research auto`
  // manual recovery instruction must be surfaced rather than silently swallowed, while `awaiting_user` remains intact.
  const baseReq = StorageClient.prototype.request
  StorageClient.prototype.request = function (this: StorageClient, method: string, ...rest: any[]) {
    return method === 'runtime_resume_awaiting_user'
      ? Promise.reject(new Error('resume rpc unavailable'))
      : (baseReq as any).call(this, method, ...rest)
  } as typeof baseReq
  try {
    await host.prompt('ses_main', '尝试直接回复但恢复失败', 'msg_resume_fail')
    host.turn.end('ses_main')
    const failNotice = host.said('ses_main').at(-1)
    expect(failNotice?.resume).toBe(false)
    expect(failNotice?.text).toContain('自动恢复研究失败（resume rpc unavailable）')
    expect(failNotice?.text).toContain('/research auto')
    expect((await host.rpc().session({ sessionID: 'ses_main' })).pauseReason).toBe('awaiting_user')
  } finally {
    StorageClient.prototype.request = baseReq
  }

  const resumedWithGuidance = await host.command('auto', 'ses_main', { goal: '按方案 B 推进' })
  expect(resumedWithGuidance.message).toContain('补充指令已记为用户指导')
  expect(host.said('ses_main').at(-1)?.text).toContain('按方案 B 推进')

  // Ask again, then simulate `note` failure during `/research auto <guidance>`: failure must be surfaced, not swallowed.
  host.turn.start('ses_main')
  await host.call('research_exploration', 'ses_main', {
    action: 'ask',
    reason: 'Need threshold confirmation',
    question: '阈值设 1ms 还是 5ms？',
    options: ['1ms', '5ms'],
  })
  host.turn.end('ses_main')
  await until(
    async () => (await host.rpc().session({ sessionID: 'ses_main' })).pauseReason === 'awaiting_user',
    'awaiting_user set 2',
  )

  const originalRequest = StorageClient.prototype.request
  StorageClient.prototype.request = function (this: StorageClient, method: string, ...rest: any[]) {
    return method === 'note'
      ? Promise.reject(new Error('ledger unavailable'))
      : (originalRequest as any).call(this, method, ...rest)
  } as typeof originalRequest
  cleanups.push(() => {
    StorageClient.prototype.request = originalRequest
  })

  const resumedNoteFailed = await host.command('auto', 'ses_main', { goal: '阈值用 1ms' })
  expect(resumedNoteFailed.message).toContain('没能记入研究记录（ledger unavailable）')
  expect(host.said('ses_main').at(-1)?.text).toContain('阈值用 1ms')
})
