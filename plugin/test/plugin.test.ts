import { afterEach, beforeEach, describe, expect, test, spyOn } from "bun:test"
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "../index"
import { parseResearchCommand } from "../src/commands"
import { decodeChunk, kindChips, knowledgeRows, materialRows, overviewModel, runtimeNotes, sessionRows } from "../src/board/model"
import { associatedStatus } from "../src/format"
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
  await host.hooks.get('tool.execute.before')!({tool:'shell',sessionID:'ses_main',id:'external',input:{command:'PRIVATE_COMMAND_BODY'}})
  await host.hooks.get('tool.execute.after')!({tool:'shell',sessionID:'ses_main',id:'external',status:'completed'})
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

test('real node source edits prevent idle pauses while the turn budget still stops continuation',async()=>{
  const host=await load()
  await host.command('auto','ses_main',{goal:'source activity and independent budget'})
  await host.command('budget','ses_main',{limit:'turns',value:'5',reason:'bounded node fixture'})
  const node=await host.call('research_propose','ses_main',{question:'Q',why_now:'now',plan:'measure',root_reason:'first'})
  await host.call('research_dispatch','ses_main',{node_id:node.node_id})
  const session='ses_node_1'
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
    const seen = await host.call("research_query", "ses_node_1", { collection: "checkpoint" })
    const checkpoint = await host.call("research_memory", "ses_node_1", { action: "checkpoint", base: seen.base, state: { next: "finish" } })
    expect(checkpoint.node_id).toBe("X-001")
    await host.call("research_finish", "ses_node_1", { state: "finished", summary: "X holds under A" })
    host.turn.end("ses_node_1")
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("节点 X-001 已完成")), "coordinator wake-up")
    const wake = host.said("ses_main").at(-1)
    expect(wake.text).toContain(`pub/${pub.publication_id}`)
    const request = modelRequest("ses_main")
    await host.hooks.get("context")!(request)
    if (policy === "r1") {
      expect(wake.text).toContain("发现：knowledge/")
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
    host.turn.start("ses_node_2")
    let tentative: any
    if (policy === "r1") tentative = await host.call("research_memory", "ses_node_2", {
      action: "record", kind: "hypothesis", statement: "Tentative explanation only",
    })
    await host.call("research_publish", "ses_node_2", {
      status: "complete", summary: "node 2 result", items: [{ item_id: "note", kind: "text", content: { text: "Q2 answered" } }],
    })
    await host.call("research_finish", "ses_node_2", { state: "finished", summary: "Q2 answered" })
    host.turn.end("ses_node_2")
    await until(() => host.said("ses_main").some((item) => item.resume && item.text.includes("节点 X-002 已完成")), "second wake-up")

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
    await host.command("context-policy", "ses_main", { policy: "r1" })
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
    expect(parseResearchCommand('budget cost 0.25 small experiment')).toMatchObject({kind:'rpc',args:{limit:'cost',value:'0.25'}})
    expect(parseResearchCommand('budget turns 0.25 invalid')).toMatchObject({kind:'error'})
    expect(parseResearchCommand('budget cost 0 invalid')).toMatchObject({kind:'error'})
    expect(parseResearchCommand("work X-001")).toMatchObject({ kind: "error" })
  })
})

describe("board text", () => {
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
