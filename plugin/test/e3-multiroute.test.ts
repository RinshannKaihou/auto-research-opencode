import {expect,test} from 'bun:test'
import {Engine,fairTaskOrder} from '../src/engine'
import {attachController} from '../src/controller-lifecycle'
import {parseResearchCommand} from '../src/commands'
import {TOOLS} from '../src/tools'
import {parameters,validateArgs} from '../src/schema'
import {Research} from '../src/research'
import {overviewModel} from '../src/board/model'
import {runtimeBudgetSummary} from '../src/format'

function engineFixture(running = true, fail?: string) {
  const calls:any[]=[]
  let scheduled=0
  const research:any={requireAssociated:()=>'/tmp/e3-tool-fixture',notifyChanged:()=>{},call:async(method:string,_sid:string,fields:any)=>{
    calls.push({method,...fields})
    if(method==='exploration_decide')return {intent_id:'first',intent_ids:['first','second']}
    if(method==='status') {
      if (fail==='status') throw new Error('temporary status failure')
      return {workflow:{run:{state:running?'running':'manual'}}}
    }
    if(method==='exploration_materialize') {
      if (fields.intent_id===fail) throw new Error('temporary materialization failure')
      return {intent_id:fields.intent_id,state:'materialized',task_id:`task-${fields.intent_id}`}
    }
    throw new Error(`unexpected ${method}`)
  }}
  const runtime=attachController('/tmp/e3-tool-fixture',`/tmp/e3-${crypto.randomUUID()}`).state
  const engine=new Engine(research,{} as any,2,runtime,()=>true,async()=>{},async()=>{})
  ;(engine as any).schedule=async()=>{scheduled++}
  ;(engine as any).deliver=async()=>{}
  return {engine,calls,scheduled:()=>scheduled}
}

test('a batch materializes every persisted intent once before ordinary scheduling',async()=>{
  const fixture=engineFixture()
  const result:any=await fixture.engine.explorationDecide('main',{work_batch:[]},'decision-id')
  expect(fixture.calls.filter(call=>call.method==='exploration_materialize').map(call=>call.intent_id)).toEqual(['first','second'])
  expect(result.work).toEqual(result.works[0])
  expect(result.works.map((work:any)=>work.task_id)).toEqual(['task-first','task-second'])
  expect(fixture.scheduled()).toBe(1)
})

test('a manual batch stays durable and unmaterialized until explicit auto',async()=>{
  const fixture=engineFixture(false)
  const result:any=await fixture.engine.explorationDecide('main',{},'decision-id')
  expect(result.works).toHaveLength(2)
  expect(result.work).toMatchObject({intent_id:'first',state:'pending',reason:'awaiting_auto'})
  expect(fixture.calls.some(call=>call.method==='exploration_materialize')).toBe(false)
  expect(fixture.scheduled()).toBe(0)
})

test('a recorded batch reports partial materialization without asking for a duplicate decision',async()=>{
  const fixture=engineFixture(true,'first')
  const result:any=await fixture.engine.explorationDecide('main',{},'decision-id')
  expect(result.decision_recorded).toBe(true)
  expect(result.works).toEqual([{intent_id:'first',state:'unconfirmed'},
    {intent_id:'second',state:'materialized',task_id:'task-second'}])
  expect(result.errors[0]).toMatchObject({phase:'materialize',intent_id:'first'})
  expect(result.recovery).toContain('Do not repeat the decision')
  expect(fixture.calls.filter(call=>call.method==='exploration_decide')).toHaveLength(1)
  expect(fixture.scheduled()).toBe(1)
})

test('status and follow-up scheduling failures preserve the committed decision result',async()=>{
  const failed=engineFixture(true,'status')
  const result:any=await failed.engine.explorationDecide('main',{},'decision-id')
  expect(result.decision_recorded).toBe(true)
  expect(result.errors[0].phase).toBe('status')
  const fixture=engineFixture()
  ;(fixture.engine as any).schedule=async()=>{throw new Error('schedule unavailable')}
  const scheduled:any=await fixture.engine.explorationDecide('main',{},'decision-id')
  expect(scheduled.decision_recorded).toBe(true)
  expect(scheduled.works.every((item:any)=>item.state==='materialized')).toBe(true)
  expect(scheduled.errors[0].phase).toBe('schedule')
})

test('resource blocking delivers a durable operator notice instead of silently skipping',async()=>{
  const fixture=engineFixture()
  const engine:any=fixture.engine
  let delivered=0
  engine.mainIn=()=> 'main'
  engine.allTasks=async()=>[{task_id:'waiting',node_id:'node',state:'queued',cwd:'/tmp/work'}]
  engine.call=async(method:string)=> {
    if (method==='status') return {workflow:{run:{state:'running'}}}
    if (method==='node_launch_claim') return {claimed:false,reason:'waiting_resource',notification_id:'persisted-notice'}
    throw new Error(method)
  }
  engine.deliver=async()=>{delivered++}
  await (Engine.prototype as any).schedule.call(engine,'/tmp/e3-tool-fixture')
  expect(delivered).toBe(1)
  const text=await engine.noticeText('/tmp/e3-tool-fixture',{kind:'resource_wait',payload:{task_id:'waiting',
    resources:{gpu:{capacity:1,held:1}},blockers:[{task_id:'finished',state:'uncertain',task_state:'finished'}],
    recovery_commands:['/research recover resource finished <reason>']}})
  expect(text).toContain('finished（finished / uncertain）')
  expect(text).toContain('/research recover resource finished')
  expect(text).toContain('独立核实')
})

test('scope queues alternate while preserving order within each scope',()=>{
  const tasks=[['a1','A'],['a2','A'],['b1','B'],['b2','B'],['c1','C']].map(([task_id,exploration_scope_id])=>({task_id,exploration_scope_id}))
  expect(fairTaskOrder(tasks).map(task=>task.task_id)).toEqual(['a1','b1','c1','a2','b2'])
  expect(fairTaskOrder(tasks,'A').map(task=>task.task_id)).toEqual(['b1','c1','a1','b2','a2'])
})

test('E3 schemas expose exact scope, batches, comparison conditions and paged delivery',()=>{
  const spec=(name:string)=>TOOLS.find(tool=>tool.name===name)!.parameters
  const decide=parameters(spec('research_decide')) as any
  expect(decide.properties.work_batch.items.properties.work_kind.enum).toContain('combine')
  expect(decide.properties.work_batch.items.properties.resources.additionalProperties).toBe(true)
  expect(decide.properties.expected_results.items.type).toBe('string')
  expect(decide.properties.comparisons.items.properties.conditions.items.required).toContain('baseline_ref')
  expect((parameters(spec('research_prepare_conclusion')) as any).properties.exploration_stop.properties.scope_judgments).toBeDefined()
  expect(spec('research_query').scope_id).toBeDefined()
  expect(()=>validateArgs({scope_ref:'scope',snapshot_id:'snapshot',route_refs:[],action:'probe',reason:'bounded',
    work_batch:[{key:'shared',budget_amount:1,resources:{'gpu:0':1},route_refs:['A','B'],question:'q',why_now:'r',plan:'p'}]},spec('research_decide'))).not.toThrow()
  expect(()=>validateArgs({scope_ref:'scope',snapshot_id:'snapshot',route_refs:[],action:'probe',reason:'r',
    work_batch:[{budget_amount:Infinity}]},spec('research_decide'))).toThrow()
})

test('work budget and resource recovery preserve explicit host command boundaries',()=>{
  expect(parseResearchCommand('budget work 0.5 bounded probe')).toMatchObject({kind:'rpc',args:{limit:'work',value:'0.5'}})
  expect(parseResearchCommand('budget work off no cap')).toMatchObject({kind:'rpc',args:{limit:'work',value:'off'}})
  expect(parseResearchCommand('budget resource gpu:0 1 serialize measurements')).toMatchObject({kind:'rpc',args:{limit:'resource',resource:'gpu:0',value:'1'}})
  expect(parseResearchCommand('budget resource gpu:0 off retire device')).toMatchObject({kind:'rpc',args:{limit:'resource',resource:'gpu:0',value:'off'}})
  expect(parseResearchCommand('budget resource gpu:0 -1 invalid')).toMatchObject({kind:'error'})
  expect(parseResearchCommand('budget resource gpu:0 0 invalid')).toMatchObject({kind:'error'})
  expect(parseResearchCommand('budget resource gpu:0 0.5 invalid')).toMatchObject({kind:'error'})
  const recover=parseResearchCommand('recover resource T-001 inspected external process')
  expect(recover).toMatchObject({kind:'rpc',args:{kind:'resource',task_id:'T-001'}})
  expect((recover as any).confirm).toContain('独立核实')
})

test('an older backend cannot silently run E3 tools',async()=>{
  const storage:any={request:async()=>({memory_write_contract:'r2-v1',runtime_control_contract:'controller-launch-v1',
    exploration_contract:'exploration-v1',evidence_contract:'evidence-v1',evidence_read:'snapshot-delivery-v2'})}
  await expect(new Research(storage,'/tmp',()=>false).ensureContract()).rejects.toMatchObject({code:'research_contract_mismatch'})
})

test('overview keeps multi-scope state and provisional decisions visible',()=>{
  const model=overviewModel({project:{goal:'task'},exploration:{objective:'local choice',scopes_total:3,active_scopes_total:2,
    pending_work_total:4,routes_total:7,pending_results_total:2,scopes:[{ref:'scope',objective:'other direction',state:'deferred'}],
    provisional_scopes_total:4,missing_results_total:3,unacknowledged_missing_total:1,waiting_resource_total:2,
    latest_decisions:[{scope_id:'A',decision_id:'D-1',provisional:true,expected_results_total:2,missing_results_total:1}]}},null)!
  expect(model.exploration).toMatchObject({scopesTotal:3,activeScopes:2,pendingWork:4,pendingTotal:2})
  expect(model.exploration!.scopes[0].state).toBe('deferred')
  expect(model.exploration!.decisions[0].provisional).toBe(true)
  expect(model.exploration).toMatchObject({provisionalScopes:4,missingResults:3,unacknowledgedMissing:1,waitingResources:2})
})

test('the budget command summary stays bounded as historical allocations accumulate',()=>{
  const summary=runtimeBudgetSummary({limits:{shared_work_budget:1000},exploration:{actual_usage_status:'unknown',allocated:1000,
    allocations:Array.from({length:1000},(_,i)=>({intent_id:`historical-${i}`})),resource_leases:[],by_scope:{A:{},B:{}}}})
  expect(summary.exploration).toMatchObject({allocations_total:1000,resource_leases_total:0,scopes_total:2,
    actual_usage_status:'unknown',expand:{collection:'exploration_budget'}})
  expect(JSON.stringify(summary)).not.toContain('historical-')
})
