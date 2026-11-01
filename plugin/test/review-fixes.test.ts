import {expect,test} from 'bun:test'
import {Research} from '../src/research'
import {CheckpointDelivery} from '../src/checkpoint-delivery'

test('a transient capability failure is retried on the next call',async()=>{
  let calls=0
  const storage:any={request:async()=>{if(++calls===1)throw new Error('startup timeout');return {memory_write_contract:'r2-v1',runtime_control_contract:'controller-launch-v1',exploration_contract:'exploration-v1',evidence_contract:'evidence-v1',evidence_read:'snapshot-delivery-v1'}}}
  const research=new Research(storage,'/tmp',()=>false)
  await expect(research.ensureContract()).rejects.toThrow('startup timeout')
  await research.ensureContract();await research.ensureContract()
  expect(calls).toBe(2)
})

test('a late receipt never upgrades the timed-out model request, and identical snapshots reuse it',async()=>{
  const snapshot={host_id:'opencode',session_id:'s',association_id:'a',attempt_id:'t',node_id:null,generation:1,
    head_revision:0,selected_checkpoint_id:null,selected_revision:null,state_digest:'digest',source_sequence:1}
  let release!:(value:{base:string})=>void
  const pending=new Promise<{base:string}>(resolve=>{release=resolve})
  let receipts=0
  const storage:any={request:async(method:string)=>{
    if(method==='memory_context')return {text:'complete context',checkpoint_snapshot:snapshot,checkpoint_owner:snapshot}
    if(method==='checkpoint_observed'){receipts++;return pending}
    return {}
  }}
  const research=new Research(storage,'/tmp',()=>false)
  research.register('s','/tmp','main')
  const text=(await research.memory('s'))!
  await research.delivered('s',text,[],20)
  research.checkpoints.stepStarted('s','old')
  expect(research.checkpoints.forCall('s','old','write')).toBeUndefined()
  release({base:'b0'});await pending
  expect(research.checkpoints.forCall('s','old','write')).toBeUndefined()
  snapshot.source_sequence=2
  await research.delivered('s',(await research.memory('s'))!,[],20)
  research.checkpoints.stepStarted('s','new')
  expect(research.checkpoints.forCall('s','new','write')).toBe('b0')
  expect(receipts).toBe(1)
})

test('detaching drops delivered bases and compacted outputs cannot reappear as trusted results',()=>{
  const delivery=new CheckpointDelivery()
  const snapshot={head_revision:0}
  const value={base:'base',snapshot}
  delivery.result('s','read','research_query',value)
  delivery.request('s',[],snapshot)
  delivery.request('s',[{content:[{type:'tool-result',id:'read',name:'research_query',result:{type:'json',value}}]}],snapshot)
  delivery.stepStarted('s','new')
  expect(delivery.forCall('s','new','write')).toBeUndefined()
  delivery.request('s',[],snapshot,{base:'base',snapshot})
  delivery.stepStarted('s','active')
  delivery.forget('s')
  expect(delivery.forCall('s','active','write')).toBeUndefined()
})
