import {expect,test} from 'bun:test'
import {Engine} from '../src/engine'
import {attachController} from '../src/controller-lifecycle'

function fixture() {
  const root='/tmp/shell-reconcile-fixture'
  const runtime=attachController(root,`/tmp/shell-reconcile-${crypto.randomUUID()}`).state
  const notice={type:'synthetic',id:'msg_done',metadata:{source:'shell',shellID:'sh_job',state:'completed',exit:0}}
  const f={job:'running',task:'running',run:'running',pending:false,reads:0,reservations:0,allowed:true,bindFailures:0,bindMatched:true,
    binds:[] as any[],pauses:[] as string[],messages:[notice] as any[],read:undefined as undefined|(()=>Promise<any[]>),
    beforeUnknown:undefined as undefined|(()=>void),execution:{state:'open',execution_token:'exec_wake',message_id:'msg_done'}}
  const task=()=>({task_id:'T-job',session_id:'node',parent_session_id:'main',node_id:'X-001',state:f.task})
  const research:any={roots:()=>[root],inScope:()=>true,sessionsIn:()=>['main','node'],rootOf:()=>root,
    requireScope:()=>{},requireAssociated:()=>root,isAssociated:()=>true,roleOf:(sid:string)=>sid==='main'?'main':'node_core',
    notifyChanged:()=>{},conclusionContractOf:()=>undefined,
    call:async(method:string,sid:string,fields:any={})=>{
      if(method==='runtime_shell_status')return {running:f.job==='running'?1:0,jobs:f.job==='running'?[{state:'running',call_id:'call_job',shell_id:'sh_job'}]:[]}
      if(method==='runtime_shell_notice'){const first=f.job==='running';f.job='completed';return {matched:true,first,call_id:'call_job'}}
      if(method==='runtime_shell_unverified'){
        f.beforeUnknown?.()
        const changed=f.job==='running'
        if(changed){f.job='unverified';f.task='unverified'}
        return {changed,state:f.job}
      }
      if(method==='status')return {workflow:{run:{state:f.run},session:{role:sid==='main'?'main':'node_core',pause_reason:null},tasks:[task()]}}
      if(method==='runtime_reserve'){f.reservations++;return {allowed:f.allowed,reservation_id:'BR-wake'}}
      if(method==='runtime_execution_bind' && f.bindFailures-- > 0)throw new Error('lost bind response')
      if(method==='runtime_execution_bind'){f.binds.push(fields);return {matched:f.bindMatched && (fields.event_seq !== undefined || fields.transcript_witness === true)}}
      if(['runtime_submitted','runtime_execution_start','runtime_drain_check'].includes(method))return {matched:true}
      if(method==='runtime_execution_end')return {claimed:true}
      if(method==='runtime_execution_view')return sid==='main'?{state:'ended'}:f.execution
      if(method==='node_launch_get')return {state:'submitted'}
      if(method==='workflow' && fields.action==='run'){f.run=fields.fields.state;return {}}
      throw new Error(`unexpected ${method}`)
    }}
  const host:any={eventsPending:()=>f.pending,source:async()=>undefined,synthetic:async()=>{},
    recoveryMessages:async()=>{f.reads++;return f.read?await f.read():f.messages}}
  const engine=new Engine(research,host,1,runtime,()=>true,async()=>{},async()=>{})
  ;(engine as any).pauseSession=async(_sid:string,_reason:string,note:string)=>{f.pauses.push(note)}
  ;(engine as any).watchRecoveredNode=async()=>{}
  return {engine,runtime,f,notice,root}
}

test('a normal completion handled during a slow transcript read cannot fail the task',async()=>{
  const {engine,f,notice}=fixture()
  await engine.pollShells(0)
  let release!:(messages:any[])=>void
  f.read=()=>new Promise(resolve=>{release=resolve})
  const scan=engine.pollShells(15_000)
  while(!release)await Bun.sleep(1)
  await engine.shellNotice('node',notice.id,notice.metadata)
  release([notice]);await scan
  expect(f.job).toBe('completed');expect(f.task).toBe('running');expect(f.pauses).toHaveLength(0)
})

test('queued normal events exclude a session from lost-notice reconciliation',async()=>{
  const {engine,f}=fixture()
  await engine.pollShells(0)
  f.pending=true
  await engine.pollShells(15_000)
  expect(f.reads).toBe(1);expect(f.task).toBe('running');expect(f.pauses).toHaveLength(0)
})

test('a terminal committed after the fresh read cannot cause a lost-notice pause',async()=>{
  const {engine,f}=fixture()
  await engine.pollShells(0)
  f.beforeUnknown=()=>{f.job='completed'}
  await engine.pollShells(15_000)
  expect(f.task).toBe('running');expect(f.pauses).toHaveLength(0)
})

test('a truly lost notice needs a second observation and pauses only once',async()=>{
  const {engine,f}=fixture()
  await engine.pollShells(0)
  expect(f.task).toBe('running')
  await engine.pollShells(14_999)
  expect(f.task).toBe('running')
  await engine.pollShells(15_000)
  await engine.pollShells(30_000)
  expect(f.task).toBe('unverified');expect(f.job).toBe('completed');expect(f.pauses).toHaveLength(1)
})

test('polling does not add a fault pause to a project already paused',async()=>{
  const {engine,f}=fixture();f.run='paused'
  await engine.pollShells(0);await engine.pollShells(15_000)
  expect(f.task).toBe('unverified');expect(f.pauses).toHaveLength(0)
})

test('slow transcript I/O cannot hold the project command queue',async()=>{
  const {engine,f,root}=fixture()
  let release!:(messages:any[])=>void
  f.read=()=>new Promise(resolve=>{release=resolve})
  const scan=engine.pollShells(0)
  while(!release)await Bun.sleep(1)
  try {
    const result=await Promise.race([engine.pause('main',root),Bun.sleep(100).then(()=>null)])
    expect(result).not.toBeNull();expect(f.run).toBe('paused')
  } finally {release([]);await scan}
})

test('long-running jobs back off full context reads up to a minute',async()=>{
  const {engine,f}=fixture();f.messages=[]
  for(const now of [0,1_000,15_000,30_000,60_000,89_999])await engine.pollShells(now)
  expect(f.reads).toBe(2)
  await engine.pollShells(90_000)
  expect(f.reads).toBe(3)
})

test('an undelivered inbox expires into a visible pause instead of a permanent hidden gate',async()=>{
  const {engine,runtime,f}=fixture();f.job='completed';f.messages=[]
  runtime.shellInboxes!.set('msg_missing',{sessionID:'node',execution:'old',matched:true,createdAt:0})
  await engine.pollShells(30_000)
  expect(f.pauses).toHaveLength(0)
  await engine.pollShells(60_000)
  expect(runtime.shellInboxes!.get('msg_missing')).toMatchObject({matched:false,refused:true})
  expect(f.task).toBe('running');expect(f.pauses).toHaveLength(1)
})

test('a refused native wake is not reserved or announced again at delivery',async()=>{
  const {engine,f,notice}=fixture();f.allowed=false
  await engine.shellNotice('node',notice.id,notice.metadata)
  await engine.inboxDelivered('node',notice.id,2)
  expect(f.reservations).toBe(1);expect(f.pauses).toHaveLength(1)
})

test('same-turn pending notices do not cause full transcript reads on tool checks',async()=>{
  const {engine,runtime,f,notice}=fixture()
  runtime.busy.add('node');runtime.turns.set('node','original')
  await engine.shellNotice('node',notice.id,notice.metadata)
  for(let i=0;i<5;i++)expect(await engine.nodeTurnAllowed('node')).toBe(true)
  expect(f.reads).toBe(0);expect(f.reservations).toBe(0)
  await engine.inboxDelivered('node',notice.id,2)
  expect(runtime.shellInboxes!.has(notice.id)).toBe(false)
})

test('a transcript witness requests the dedicated bind path without forging a delivery sequence',async()=>{
  const {engine,runtime,f,notice}=fixture()
  runtime.busy.add('node');runtime.turns.set('node','original')
  await engine.shellNotice('node',notice.id,notice.metadata)
  runtime.turns.set('node','next')
  expect(await engine.nodeTurnAllowed('node')).toBe(true)
  expect(f.binds.at(-1)).toMatchObject({transcript_witness:true})
  expect(f.binds.at(-1).event_seq).toBeUndefined()
  expect(runtime.shellInboxes!.has(notice.id)).toBe(false);expect(f.reservations).toBe(1)
})

test('warm transcript recovery neither registers a live inbox nor reserves another wake',async()=>{
  const {engine,runtime,f}=fixture()
  await engine.recoverWarm()
  expect(f.job).toBe('completed');expect(f.task).toBe('running')
  expect(runtime.shellInboxes!.size).toBe(0);expect(f.reservations).toBe(0)
})


test('a repeated enqueue after delivery cannot recreate a continuation gate',async()=>{
  const {engine,runtime,f,notice}=fixture()
  await engine.shellNotice('node',notice.id,notice.metadata)
  await engine.inboxDelivered('node',notice.id,2)
  await engine.shellNotice('node',notice.id,notice.metadata)
  expect(runtime.shellInboxes!.size).toBe(0);expect(f.reservations).toBe(1)
})


test('a user-authorized resume can receive a previously refused notice inline',async()=>{
  const {engine,runtime,f,notice}=fixture();f.allowed=false
  await engine.shellNotice('node',notice.id,notice.metadata)
  runtime.busy.add('node');runtime.turns.set('node','exec_resume')
  f.execution={state:'open',execution_token:'exec_resume',message_id:'msg_user_resume',reservation_id:'BR-user'} as any
  await engine.inboxDelivered('node',notice.id,2)
  expect(await engine.nodeTurnAllowed('node')).toBe(true)
  expect(f.reservations).toBe(1);expect(f.pauses).toHaveLength(1)
})

test('failed delivery binding can be retried without another reservation or a stale gate',async()=>{
  const {engine,runtime,f,notice}=fixture()
  await engine.shellNotice('node',notice.id,notice.metadata)
  f.bindFailures=1
  await expect(engine.inboxDelivered('node',notice.id,2)).rejects.toThrow('lost bind response')
  expect(runtime.shellInboxes!.has(notice.id)).toBe(true)
  await engine.inboxDelivered('node',notice.id,2)
  expect(runtime.shellInboxes!.has(notice.id)).toBe(false);expect(f.reservations).toBe(1)
})


test('an unbound new turn cannot gain tools when the delivery witness is unavailable',async()=>{
  const {engine,runtime,f,notice}=fixture()
  runtime.busy.add('node');runtime.turns.set('node','old')
  await engine.shellNotice('node',notice.id,notice.metadata)
  runtime.turns.set('node','unbound_next');f.messages=[]
  expect(await engine.nodeTurnAllowed('node')).toBe(false)
  expect(f.reservations).toBe(0)
})

test('a rejected execution binding does not grant native wake tools',async()=>{
  const {engine,f,notice}=fixture();f.bindMatched=false
  await engine.shellNotice('node',notice.id,notice.metadata)
  await engine.inboxDelivered('node',notice.id,2)
  expect(await engine.nodeTurnAllowed('node')).toBe(false)
  expect(f.pauses).toHaveLength(1);expect(f.task).toBe('running')
})


test('a pending transcript bind keeps the inbox and read-only gate without pausing a healthy node',async()=>{
  const {engine,runtime,f,notice}=fixture()
  runtime.busy.add('node');runtime.turns.set('node','original')
  await engine.shellNotice('node',notice.id,notice.metadata)
  runtime.turns.set('node','next');f.bindMatched=false
  expect(await engine.nodeTurnAllowed('node')).toBe(false)
  expect(runtime.shellInboxes!.has(notice.id)).toBe(true);expect(f.pauses).toHaveLength(0)
  f.bindMatched=true
  await engine.inboxDelivered('node',notice.id,2)
  expect(await engine.nodeTurnAllowed('node')).toBe(true)
  expect(f.reservations).toBe(1)
})

test('a notification queued during a long turn gets a full idle delivery grace period',async()=>{
  const {engine,runtime,f}=fixture();f.job='completed';f.messages=[]
  runtime.shellInboxes!.set('msg_long',{sessionID:'node',execution:'long',matched:true,createdAt:0})
  runtime.busy.add('node')
  await engine.pollShells(100_000)
  runtime.busy.delete('node')
  await engine.pollShells(100_001)
  expect(f.pauses).toHaveLength(0)
  await engine.pollShells(130_000)
  expect(f.pauses).toHaveLength(0)
  await engine.pollShells(130_001)
  expect(f.pauses).toHaveLength(1)
})


test('native start and terminal events reset delivery expiry to the ensuing idle period',async()=>{
  const {engine,runtime}=fixture()
  runtime.shellInboxes!.set('msg_long',{sessionID:'node',execution:'old',matched:true,createdAt:0,eligibleAt:0})
  await engine.started('node','start_long',1)
  expect(runtime.shellInboxes!.get('msg_long')!.eligibleAt).toBeUndefined()
  ;(engine as any).afterTurn=async()=>{}
  const before=Date.now()
  await engine.ended('node','succeeded','end_long',2)
  expect(runtime.shellInboxes!.get('msg_long')!.eligibleAt).toBeGreaterThanOrEqual(before)
  expect(runtime.shellInboxes!.get('msg_long')!.eligibleAt).toBeLessThanOrEqual(Date.now())
})
