import { expect, test } from "bun:test"
import { spawnSync } from "node:child_process"
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const root = fileURLToPath(new URL("../..", import.meta.url))

test("release versions agree across Python and plugin metadata", () => {
  const manifest = JSON.parse(readFileSync(join(root, "plugin/package.json"), "utf8"))
  const python = readFileSync(join(root, "src/auto_research/__init__.py"), "utf8")
  const project = readFileSync(join(root, "pyproject.toml"), "utf8")
  expect(python.match(/__version__ = "([^"]+)"/)?.[1]).toBe(manifest.version)
  expect(project.match(/^version = "([^"]+)"/m)?.[1]).toBe(manifest.version)
})

test("packed backend starts, concludes and restarts without repository sources", () => {
  const temporary = mkdtempSync(join(tmpdir(), "research-package-"))
  try {
    const staging = join(temporary, "build")
    const plugin = join(staging, "plugin")
    const release = join(temporary, "release")
    const archive = join(temporary, "release.tgz")
    mkdirSync(plugin, { recursive: true })
    mkdirSync(release)
    cpSync(join(root, "src"), join(staging, "src"), { recursive: true })
    for (const file of ["index.ts", "tui.tsx", "rpc.ts", "src", "scripts", "package.json", "README.md"]) {
      cpSync(join(root, "plugin", file), join(plugin, file), { recursive: true })
    }
    // Exercise the actual pack lifecycle, including prepack and package.json files.
    const packed = spawnSync(process.execPath, ["pm", "pack", "--filename", archive, "--quiet"], {
      cwd: plugin, encoding: "utf8", timeout: 30_000,
      env: { ...process.env, PATH: `${dirname(process.execPath)}:${process.env.PATH ?? ""}` },
    })
    expect({ status: packed.status, error: packed.stderr }).toMatchObject({ status: 0 })
    const extracted = spawnSync("tar", ["-xzf", archive, "-C", release], { encoding: "utf8", timeout: 10_000 })
    expect(extracted.status).toBe(0)
    rmSync(staging, { recursive: true, force: true })

    const python = process.env.RESEARCH_TEST_PYTHON ?? "python3"
    const modulePath = join(release, "package/python")
    const env = { ...process.env, PYTHONPATH: modulePath, PYTHONDONTWRITEBYTECODE: "1" }
    // -S excludes site packages, editable installs, and .pth fallbacks to src/.
    const origin = spawnSync(python, ["-S", "-c", "import auto_research.service; print(auto_research.service.__file__)"], {
      cwd: release, env, encoding: "utf8", timeout: 10_000,
    })
    expect({ status: origin.status, error: origin.stderr }).toMatchObject({ status: 0 })
    expect(origin.stdout.trim()).toBe(join(modulePath, "auto_research/service.py"))

    const project = join(temporary, "project")
    mkdirSync(project)
    writeFileSync(join(project, "report.md"), "# Result\nThe research question remains unresolved.\n")
    writeFileSync(join(project,'experiment.py'),'print(1)\n')
    const workerProbe=spawnSync(process.execPath,['-e',`
import {WorkspaceProbe} from ${JSON.stringify(pathToFileURL(join(release,'package/src/workspace-probe.ts')).href)};
const probe=new WorkspaceProbe();
try {const result=await probe.sample(${JSON.stringify(project)});console.log(JSON.stringify(result));}
finally {probe.close();}
`],{cwd:release,encoding:'utf8',timeout:10_000})
    expect({status:workerProbe.status,error:workerProbe.stderr}).toMatchObject({status:0})
    const sourceResult=JSON.parse(workerProbe.stdout)
    expect(sourceResult.digest).toBeString()
    expect(sourceResult.files).toBe(1)
    const registry = join(temporary, "registry.sqlite3")
    const request = (id: string, method: string, fields: Record<string, unknown> = {}) => ({
      transport_id: id, operation_id: id, host_id: "opencode", session_id: "main", method, ...fields,
    })
    const run = (requests: object[]) => {
      const process = spawnSync(python, ["-S", "-m", "auto_research.service", "--registry", registry], {
        cwd: release, env, encoding: "utf8", timeout: 15_000,
        input: requests.map((value) => JSON.stringify(value)).join("\n") + "\n",
      })
      expect({ status: process.status, error: process.stderr }).toMatchObject({ status: 0 })
      return process.stdout.trim().split("\n").map((line) => JSON.parse(line))
    }
    const contract = {
      summary: "Investigation ended", final_ref: "pub/P-001#report", outcome: "unresolved",
      gaps: ["No decisive evidence"], review: { status: "unreviewed", refs: [], limitations: ["Not independently reviewed"] },
    }
    const responses = run([
      request("capabilities", "capabilities"),
      request("open", "open", { root: project, cwd: project, goal: "Packaged research" }),
      request("checkpoint", "memory_write", { model_call: true, action: "checkpoint", fields: { state: { phase: "synthesis" }, expected_revision: 0 } }),
      request("publish", "publish", { model_call: true, status: "complete", summary: "Report", gaps: [], knowledge_refs: [],
        items: [{ item_id: "report", kind: "report", source_path: "report.md" }] }),
      request("bad-conclusion", "conclude", { fields: { ...contract, final_ref: "pub/P-001#missing" } }),
      request("conclusion", "conclude", { fields: contract }),
      request("status", "status"),
    ])
    expect(responses.map((response) => response.ok)).toEqual([true, true, true, true, false, true, true])
    expect(responses[0].value.schema_version).toBe(16)
    expect(responses[0].value.multiroute_contract).toBe('multiroute-v1')
    expect(responses[0].value.memory_write_contract).toBe('r2-v1')
    expect(responses[0].value.checkpoint_base).toBe('receipt-cas-v1')
    expect(responses[4].error.message).toContain("item_missing")
    expect(responses[6].value.workflow.run.state).toBe("complete")
    const restarted = run([request("restart-status", "status"), request("conclusion", "conclude", { fields: contract }),
      request("r1-context", "memory_context", { policy: "memory-r1-v1", profile: "opencode-auto" }),
      request("r1-preview", "project_read", { root: project, view: "context_preview" })])
    expect(restarted[0].value.workflow.conclusion).toEqual(responses[5].value)
    expect(restarted[1].value).toEqual(responses[5].value)
    expect(restarted[2].ok).toBe(true)
    expect(restarted[2].value.policy_version).toBe("memory-r1-v1")
    expect(restarted[2].value.text).toContain("critical_now")
    expect(restarted[3].ok).toBe(true)
    expect(restarted[3].value.available).toBe(true)
    expect(restarted[3].value.views["memory-r1-v1"].text).toBe(restarted[2].value.text)
    expect(restarted[3].value.views["memory-v2"].source_sequence).toBe(restarted[2].value.source_sequence)

    // Exercise the migration from the extracted package, with no source-tree imports.
    const downgraded = spawnSync(python,['-S','-c',`
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    for table in ('review_candidates','review_obligations','review_lifecycle_events','checkpoint_read_receipts','runtime_budgets','runtime_budget_reservations','runtime_progress','generation_conclusion_contracts'):
        db.execute('DROP TABLE '+table)
    db.execute('PRAGMA user_version=10')
`,join(project,'.research/state.sqlite3')],{cwd:release,env,encoding:'utf8',timeout:10_000})
    expect({status:downgraded.status,error:downgraded.stderr}).toMatchObject({status:0})
    const old=run([request('old-capabilities','project_read',{root:project,view:'capabilities'})])
    expect(old[0].value.schema_version).toBe(10)
    expect(old[0].value.memory_write_contract).toBe('requires_migration')
    const migrated=run([request('migrated-status','status'),
      request('new-capabilities','project_read',{root:project,view:'capabilities'}),
      request('checkpoint','memory_write',{model_call:true,action:'checkpoint',fields:{state:{phase:'synthesis'},expected_revision:0}})])
    expect(migrated.every(row=>row.ok)).toBe(true)
    expect(migrated[1].value.schema_version).toBe(16)
    expect(migrated[2].value).toEqual(responses[2].value)
    expect(readFileSync(join(project,'.research/schema-10-backup.sqlite3')).length).toBeGreaterThan(0)

    // The extracted backend also exercises the new durable choice and the
    // evidence-v1 report/receipt contract, including a service restart.
    const scenario = spawnSync(python, ['-S','-c', `
import hashlib, json, pathlib, sys
from auto_research.service import NativeService
base=pathlib.Path(sys.argv[1]); registry=base/'e1e2-registry.sqlite3'; count=0
service=NativeService(registry)
def call(method, **fields):
    global count
    count+=1
    req={'transport_id':str(count),'operation_id':str(count),'host_id':'opencode','session_id':'main','method':method,**fields}
    result=service.handle(req)
    assert result['ok'], result
    return result['value']
e1=base/'e1-project'; e1.mkdir()
call('open',root=str(e1),cwd=str(e1),goal='Choose a probe')
call('workflow',action='run',fields={'state':'running'})
call('exploration_policy',mode='on')
scope=call('exploration_scope',model_call=True,fields={'changes':{'objective':'Choose a probe','task_kind':'selection'}})
route=call('exploration_route',model_call=True,fields={'scope_ref':scope['ref'],'changes':{
    'title':'Measure','rationale':'Resolve uncertainty','critical_assumption':'Data is representative'}})
view=call('query',collection='evidence',refs=[])
decision=call('exploration_decide',model_call=True,fields={'scope_ref':scope['ref'],'snapshot_id':view['snapshot_id'],
    'route_refs':[route['ref']],'action':'probe','reason':'Measure first','next_work':{'route_refs':[route['ref']],
    'question':'Is it representative?','why_now':'Choose','plan':'Measure','root_reason':'First probe'}})
service=NativeService(registry)
pending=call('exploration_pending')
assert pending==[decision['intent_id']]
work=call('exploration_materialize',intent_id=pending[0])
assert work['state']=='materialized'
e2=base/'e2-project'; e2.mkdir(); service=NativeService(base/'e2-registry.sqlite3')
call('open',root=str(e2),cwd=str(e2),goal='Answer a bounded question')
call('conclusion_contract',contract='evidence-v1')
source=call('publish',model_call=True,status='complete',summary='data',gaps=[],knowledge_refs=[],
    items=[{'item_id':'data','kind':'text','content':{'value':7}}])
claim=call('memory_write',model_call=True,action='record',fields={'kind':'claim','statement':'Value is 7',
    'visibility':'project','evidence_refs':['pub/'+source['publication_id']+'#data']})
view=call('query',collection='evidence',refs=[claim['ref']])
draft=call('conclusion_prepare',model_call=True,fields={'snapshot_id':view['snapshot_id'],
    'core_claim_refs':[claim['ref']],'outcome':'answered','gaps':[],'risk_judgments':[],
    'review_requirements':[],'exploration_stop':{'reason_kind':'goal_met','reason':'Measured target'}})
pub=call('publish',model_call=True,status='complete',summary='report',gaps=[],knowledge_refs=[claim['ref']],
    items=[{'item_id':'report','kind':'text','content':{'text':'The measured value is 7.'}}],
    draft_id=draft['draft_id'],report_item_id='report')
page=call('query',collection='evidence',draft_id=draft['draft_id'],limit=500)
pages=[]
while True:
    end=page['end']
    pages.append({'snapshot_id':page['snapshot_id'],'draft_id':page['draft_id'],'part':page['part'],'offset':page['offset'],'end':end,
        'manifest_digest':page['manifest_digest'],'chunk_digest':hashlib.sha256(page['chunk'].encode()).hexdigest()})
    if not page['cursor']: break
    page=call('query',collection='evidence',cursor=page['cursor'],limit=500)
receipts=call('evidence_observed',pages=pages,plugin_instance_id='pack-test')['receipt_ids']
result=call('conclude',fields={'draft_id':draft['draft_id'],'final_ref':'pub/'+pub['publication_id']+'#report','summary':'Bounded answer'},
    delivery_receipts=receipts,plugin_instance_id='pack-test')
assert result['evidence']['evidence_closure']['status']=='resolved'
service=NativeService(base/'e2-registry.sqlite3')
assert call('status')['workflow']['conclusion']['conclusion_id']==result['conclusion_id']
print(json.dumps({'pending_recovered':True,'evidence_concluded':True}))
`, temporary], {cwd:release, env, encoding:'utf8', timeout:15_000})
    expect({status:scenario.status,error:scenario.stderr}).toMatchObject({status:0})
    expect(JSON.parse(scenario.stdout)).toEqual({pending_recovered:true,evidence_concluded:true})
  } finally {
    rmSync(temporary, { recursive: true, force: true })
  }
}, 60_000)
