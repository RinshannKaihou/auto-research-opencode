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
    expect(pages).toEqual([{snapshot_id:'evidence-1',offset:0,end:2,manifest_digest:'digest',
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
