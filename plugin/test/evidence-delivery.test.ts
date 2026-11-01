import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { EvidenceDelivery } from '../src/evidence-delivery'

const page = { snapshot_id:'evidence-1', draft_id:'CD-001', offset:0, chunk:'证据', total_chars:2,
  cursor:null, manifest_digest:'digest' }
const result = (value:any) => [{content:[{type:'tool-result',id:'call',name:'research_query',result:{type:'json',value}}]}]

test('only an exact visible evidence page is bound to the next assistant message', async () => {
  const delivery=new EvidenceDelivery()
  delivery.result('main','call','research_query',page)
  delivery.stepStarted('main','message-1')
  await delivery.request('main',result(page),async pages => {
    expect(pages).toEqual([{snapshot_id:'evidence-1',draft_id:'CD-001',offset:0,end:2,manifest_digest:'digest',
      chunk_digest:createHash('sha256').update('证据').digest('hex')}])
    return {receipt_ids:['receipt']}
  },100)
  expect(delivery.forCall('main','message-1','finish')).toEqual(['receipt'])
  delivery.stepStarted('main','message-2')
  await delivery.request('main',result({...page,chunk:'truncated'}),async()=>({receipt_ids:['wrong']}),100)
  expect(delivery.forCall('main','message-2','finish')).toEqual([])
})

test('a late receipt cannot authorize the timed-out model request', async () => {
  const delivery=new EvidenceDelivery()
  delivery.result('main','call','research_query',page)
  delivery.stepStarted('main','message-1')
  let settle!:(value:{receipt_ids:string[]})=>void
  const late=new Promise<{receipt_ids:string[]}>(resolve=>{settle=resolve})
  await delivery.request('main',result(page),()=>late,1)
  settle({receipt_ids:['late']})
  await Promise.resolve()
  expect(delivery.forCall('main','message-1','finish')).toEqual([])
  delivery.stepStarted('main','message-2')
  await delivery.request('main',result(page),async()=>({receipt_ids:['confirmed']}),100)
  expect(delivery.forCall('main','message-2','finish')).toEqual(['confirmed'])
})

test('completed batches survive another batch timing out', async () => {
  const delivery=new EvidenceDelivery()
  delivery.result('main','call','research_query',page)
  delivery.stepStarted('main','message')
  const issues=await delivery.request('main',result(page),async (_pages,onBatch) => {
    onBatch({receipt_ids:['valid'],rejected:[{reason:'evidence_snapshot_expired'}]})
    await new Promise<void>(()=>{})
  },1)
  expect(delivery.forCall('main','message','finish')).toEqual(['valid'])
  expect(issues.sort()).toEqual(['evidence_snapshot_expired','timeout'])
})

test('material pages become durable only after their model step produces a response', async () => {
  const delivery=new EvidenceDelivery()
  const material={snapshot_id:'material-1',part:'material',material_ref:'pub/P-1#data',offset:0,end:6,
    chunk:'中文',total_bytes:6,cursor:null,manifest_digest:'material-digest'}
  delivery.result('main','call','research_query',material)
  delivery.stepStarted('main','model-message')
  let early=0
  await delivery.request('main',result(material),async()=>{early++;return {receipt_ids:[]}},100)
  expect(early).toBe(0)
  let seen:any[]=[]
  await delivery.confirmMaterials('main','model-message',async pages=>{seen=pages;return {receipt_ids:['accepted']}})
  expect(seen[0].model_message_id).toBe('model-message')
  expect(seen[0].material_ref).toBe('pub/P-1#data')
})
