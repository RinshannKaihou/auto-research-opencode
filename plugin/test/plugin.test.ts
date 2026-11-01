import { afterEach, beforeEach, describe, expect, test, spyOn } from "bun:test"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { spawnSync } from "node:child_process"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "../index"
import { parseResearchCommand } from "../src/commands"
import { decodeChunk, kindChips, knowledgeRows, materialRows, overviewModel, runtimeNotes, sessionRows } from "../src/board/model"
import { associatedStatus } from "../src/format"
import { StorageClient } from "../src/storage"
import { Research } from "../src/research"
import { keepsTool } from "../src/tools"
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
  transcripts: Map<string, any[]>; busy: Set<string>; waiters: Map<string, (() => void)[]>
}>()
function fakeHost(registryPath: string, directory: string, python?: string) {
  const tools = new Map<string, any>()
  const hooks = new Map<string, Hook>()
  const synthetic: any[] = []
  const interrupted: string[] = []
  const created: string[] = []
  const models: any[] = []
  const permissions = new Map<string, any[]>()
  const events = new EventStream()
  let subscriptions = 0
  // A session is busy from a resuming message until its turn ends; wait() resolves then.
  // Reloading the plugin does not reset the native host's sessions or history.
  let native = nativeSessions.get(registryPath)
  if (!native) nativeSessions.set(registryPath, native = { transcripts: new Map(), busy: new Set(), waiters: new Map() })
  const { transcripts, busy, waiters } = native
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
      get: async ({ sessionID }: { sessionID: string }) => ({ id: sessionID, location: { directory } }),
      create: async (input: any) => {
        const id = input.id ?? `ses_node_${created.length + 1}`
        created.push(id)
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
  /** The model of `sessionID` ends its turn with a final text reply. */
  const reply = (sessionID: string, text: string, outcome = "succeeded") => endTurn(sessionID, text, outcome)
  return { ctx, tools, hooks, synthetic, interrupted, created, models, permissions, events, reply, subscriptions: () => subscriptions, rpc: () => handlers }
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

function simulateDeadHost(): void {
  forgetControllerForTest(project, registry)
  const script = `import json,sqlite3,sys\ndb=sqlite3.connect(sys.argv[1])\nrow=db.execute("SELECT details FROM workflow_intents WHERE intent_id='controller-owner:v1'").fetchone()\nassert row\ndetails=json.loads(row[0]); details['pid']=2147483647\ndb.execute("UPDATE workflow_intents SET details=? WHERE intent_id='controller-owner:v1'", (json.dumps(details),)); db.commit()`
  const result = spawnSync('python3', ['-c', script, join(project, '.research', 'state.sqlite3')], { encoding: 'utf8' })
  if (result.status !== 0) throw new Error(result.stderr)
}

beforeEach(() => {
  nativeSessions.clear()
  dir = mkdtempSync(join(tmpdir(), "ari-opencode-"))
  project = join(dir, "project")
  mkdirSync(project)
  registry = join(dir, "registry", "registry.sqlite3")
})

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  rmSync(dir, { recursive: true, force: true })
})

async function load(options: { python?: string; directory?: string; hostSetup?: (host: ReturnType<typeof fakeHost>) => void } = {}) {
  const host = fakeHost(registry, options.directory ?? project, options.python)
  options.hostSetup?.(host)
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
    expect((await host.command('status', 'ses_main')).message).toContain('/research resume')
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
      expect(JSON.stringify(request.system)).toContain("X holds under A")
      expect(JSON.stringify(request.system)).toContain("## findings")
      await until(async () => {
        const view = await host.rpc().board({ view: "context_preview", directory: project })
        return view.last_recorded?.policy_version === "memory-r1-v1"
      }, "actual R1 injection record")
      const observed = await host.rpc().board({ view: "context_preview", directory: project })
      expect(observed.observations.generated).toBeGreaterThan(0)
      expect(observed.observations.recorded).toBeGreaterThan(0)
    } else expect(JSON.stringify(request.system)).not.toContain("X holds under A")

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
    expect(JSON.stringify(request.system)).toContain("Only an explicit /research resume")
    expect(JSON.stringify(request.system)).not.toContain("decide and continue")
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
    expect(JSON.stringify(request.system)).toContain("用户指导：重点验证 Y，放弃 Z")
  })

  test("auto with text is refused, not dropped, while research runs or is paused, or when too long", async () => {
    const host = await load()
    await host.command("auto", "ses_main", { goal: "Find X" })
    const running = await host.command("auto", "ses_main", { goal: "改看 Y" })
    expect(running.error).toContain("补充指令没有发送")
    await host.command("pause", "ses_main")
    const paused = await host.command("auto", "ses_main", { goal: "改看 Y" })
    expect(paused.error).toContain("补充指令没有发送")
    expect(paused.error).toContain("/research resume")
    const long = await host.command("auto", "ses_main", { goal: "长".repeat(2001) })
    expect(long.error).toContain("超过 2000 字")
    // Notes of the open work segment reach the research context; the typed one shows that, the refused text is absent.
    await host.hooks.get("prompt")!({ sessionID: "ses_main", prompt: { text: "先测小事务" } })
    const request = modelRequest("ses_main")
    await host.hooks.get("context")!(request)
    const context = JSON.stringify(request.system)
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
    expect(JSON.stringify(context.system)).toContain("用户指导：按方案 B 继续")
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
      expect(JSON.stringify(request.system)).not.toContain('PRIVATE_DEFAULT_WORKSPACE_AND_AGENTS')
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
      expect(JSON.stringify(request.system)).not.toContain('private workspace AGENTS')
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
    expect(parseResearchCommand('budget cost 0.25 small experiment')).toMatchObject({kind:'rpc',args:{limit:'cost',value:'0.25'}})
    expect(parseResearchCommand('budget turns 0.25 invalid')).toMatchObject({kind:'error'})
    expect(parseResearchCommand('budget cost 0 invalid')).toMatchObject({kind:'error'})
    expect(parseResearchCommand("work X-001")).toMatchObject({ kind: "error" })
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
    expect(text).toContain("未解决项 12 条 · 审阅限制 2 条 · 审阅材料 1 条，逐条见 /research board 概览")
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
  expect(JSON.stringify(legacy.system)).not.toContain("critical_now")
  const switched = await host.command("context-policy", "ses_main", { policy: "r1" })
  expect(switched.message).toContain("memory-r1-v1")
  expect(switched.message).not.toContain("诊断")
  expect(switched.state.last_recorded.policy_version).toBe("memory-v2")
  const current = modelRequest("ses_main")
  await host.hooks.get("context")!(current)
  expect(JSON.stringify(current.system)).toContain("critical_now")
  expect(JSON.stringify(current.system)).toContain("findings")
  const back = await host.command("context-policy", "ses_main", { policy: "legacy" })
  expect(back.state.policy_version).toBe("memory-v2")
  expect(back.state.last_recorded.policy_version).toBe("memory-r1-v1")
  const restored = modelRequest("ses_main")
  await host.hooks.get("context")!(restored)
  expect(JSON.stringify(restored.system)).not.toContain("critical_now")
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
  await host.hooks.get('context')!(context)
  expect(JSON.stringify(context.system)).toContain('exploration')
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
    await expect(first.call('research_decide','ses_main',{scope_ref:scope.ref,snapshot_id:view.snapshot_id,
      route_refs:[route.ref],action:'probe',reason:'Resolve the tail assumption',next_work:{route_refs:[route.ref],
        question:'Does tail differ?',why_now:'Choose direction',plan:'Measure',root_reason:'First probe'}}))
      .rejects.toThrow('host interrupted before materialization')
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
