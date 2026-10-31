import { test,expect } from 'bun:test'
import { CheckpointDelivery } from '../src/checkpoint-delivery'

const snapshot = (revision:number,node_id:string|null=null) => ({host_id:'opencode',session_id:'s',association_id:'a',
  attempt_id:'t',node_id,generation:0,head_revision:revision})

test('a base is request-bound and frozen per call, including parallel calls and retry', () => {
  const delivery=new CheckpointDelivery()
  delivery.request('s',[],snapshot(0),{base:'b0',snapshot:snapshot(0)})
  expect(delivery.forCall('s','unknown','unbound')).toBeUndefined()
  delivery.stepStarted('s','m1')
  expect(delivery.forCall('s','m1','c1')).toBe('b0')
  expect(delivery.forCall('s','m1','parallel')).toBe('b0')
  delivery.request('s',[],snapshot(1),{base:'b1',snapshot:snapshot(1)})
  delivery.stepStarted('s','m2')
  expect(delivery.forCall('s','m1','c1')).toBe('b0')
  expect(delivery.forCall('s','m1','parallel')).toBe('b0')
  expect(delivery.forCall('s','m2','c2')).toBe('b1')
})

test('successful result does not upgrade a base until its exact output enters a new model request', () => {
  const delivery=new CheckpointDelivery()
  const value={revision:1,state:{done:true},next_base:'b1',next_snapshot:snapshot(1)}
  delivery.request('s',[],snapshot(0),{base:'b0',snapshot:snapshot(0)})
  delivery.stepStarted('s','m1')
  delivery.result('s','c1','research_checkpoint',value)
  expect(delivery.forCall('s','m1','parallel')).toBe('b0')
  delivery.request('s',[{role:'tool',content:[{type:'tool-result',id:'c1',name:'research_checkpoint',
    result:{type:'text',value:'truncated output'}}]}],snapshot(1))
  delivery.stepStarted('s','m2')
  expect(delivery.forCall('s','m2','c2')).toBeUndefined()
  delivery.request('s',[{role:'tool',content:[{type:'tool-result',id:'c1',name:'research_checkpoint',
    result:{type:'text',value:JSON.stringify(value,null,2)}}]}],snapshot(1))
  delivery.stepStarted('s','m3')
  expect(delivery.forCall('s','m3','c3')).toBe('b1')
})

test('foreign-owner outputs and restart cannot silently supply a base', () => {
  const delivery=new CheckpointDelivery()
  const value={base:'old',snapshot:snapshot(10,'node-other')}
  delivery.result('s','read','research_query',value)
  const messages=[{role:'tool',content:[{type:'tool-result',id:'read',name:'research_query',result:{type:'json',value}}]}]
  delivery.request('s',messages,snapshot(0))
  delivery.stepStarted('s','m')
  expect(delivery.forCall('s','m','write')).toBeUndefined()
  const restarted=new CheckpointDelivery()
  restarted.request('s',messages,snapshot(0))
  restarted.stepStarted('s','new')
  expect(restarted.forCall('s','new','write')).toBeUndefined()
})
