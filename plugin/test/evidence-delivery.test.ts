import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { EvidenceDelivery } from '../src/evidence-delivery'
import { Research } from '../src/research'

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

test('a late previous step cannot confirm a later request or a failed step', async () => {
  const delivery=new EvidenceDelivery()
  const first={snapshot_id:'material-1',part:'material',material_ref:'pub/P-1#data',offset:0,end:3,
    chunk:'one',total_bytes:6,cursor:null,manifest_digest:'digest'}
  const second={...first,offset:3,end:6,chunk:'two'}
  delivery.result('main','call','research_query',first)
  delivery.stepStarted('main','old')
  await delivery.request('main',result(first),async()=>({receipt_ids:[]}),100)
  delivery.result('main','call','research_query',second)
  await delivery.request('main',result(second),async()=>({receipt_ids:[]}),100)
  let observed:any[]=[]
  await delivery.confirmMaterials('main','old',async pages=>{observed=pages;return {receipt_ids:['ok']}})
  expect(observed.map(page=>page.offset)).toEqual([0])
  delivery.stepFinished('main','old')
  delivery.stepStarted('main','new')
  delivery.stepFailed('main','new')
  observed=[]
  await delivery.confirmMaterials('main','new',async pages=>{observed=pages;return {receipt_ids:['ok']}})
  expect(observed).toEqual([])
})

test('a failed material confirmation is visible in the next request', async () => {
  const delivery=new EvidenceDelivery()
  const material={snapshot_id:'material-1',part:'material',material_ref:'pub/P-1#data',offset:0,end:3,
    chunk:'one',total_bytes:3,cursor:null,manifest_digest:'digest'}
  delivery.result('main','call','research_query',material)
  await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)
  delivery.stepStarted('main','message')
  await expect(delivery.confirmMaterials('main','message',async()=>({receipt_ids:[],rejected:[{index:0,reason:'expired'}]}))).rejects.toThrow()
  delivery.stepFinished('main','message')
  const issues=await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)
  expect(issues).toContain('material_delivery_unconfirmed: expired pub/P-1#data [0,3)')
})

test('one rejected material page does not erase valid pages or poison later requests', async () => {
  const delivery=new EvidenceDelivery()
  const good={snapshot_id:'good',part:'material',material_ref:'pub/P-1#good',offset:0,end:3,
    chunk:'yes',manifest_digest:'good-digest'}
  const bad={...good,snapshot_id:'bad',material_ref:'pub/P-1#bad',chunk:'bad',manifest_digest:'bad-digest'}
  const parts=[good,bad].map((value,index)=>({type:'tool-result',id:`call-${index}`,name:'research_query',result:{type:'json',value}}))
  for (const [index,value] of [good,bad].entries()) delivery.result('main',`call-${index}`,'research_query',value)
  delivery.stepStarted('main','first')
  await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  await expect(delivery.confirmMaterials('main','first',async pages=>{
    expect(pages.map(page=>page.material_ref)).toEqual([good.material_ref,bad.material_ref])
    return {receipt_ids:['good-receipt'],rejected:[{index:1,reason:'material_unavailable'}]}
  })).rejects.toThrow('material_unavailable')
  delivery.stepFinished('main','first')
  delivery.stepStarted('main','second')
  const issues=await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  expect(issues).toEqual(['material_delivery_unconfirmed: material_unavailable pub/P-1#bad [0,3)'])
  await delivery.confirmMaterials('main','second',async pages=>{
    expect(pages.map(page=>page.material_ref)).toEqual([good.material_ref])
    return {receipt_ids:['good-receipt']}
  })
  // A new read of the restored material is eligible even while the old output is visible.
  delivery.result('main','fresh','research_query',bad)
  delivery.stepFinished('main','second'); delivery.stepStarted('main','third')
  const fresh={type:'tool-result',id:'fresh',name:'research_query',result:{type:'json',value:bad}}
  await delivery.request('main',[{content:[...parts,fresh]}],async()=>({receipt_ids:[]}),100)
  await delivery.confirmMaterials('main','third',async pages=>{
    expect(pages.map(page=>page.material_ref)).toEqual([good.material_ref,bad.material_ref])
    return {receipt_ids:['restored']}
  })
  delivery.stepFinished('main','third'); delivery.stepStarted('main','fourth')
  expect(await delivery.request('main',[{content:[...parts,fresh]}],async()=>({receipt_ids:[]}),100)).toEqual([])
})

test('a transport failure leaves the material page eligible for retry', async () => {
  const delivery=new EvidenceDelivery()
  const material={snapshot_id:'s',part:'material',material_ref:'pub/P-1#data',offset:0,end:3,chunk:'one',manifest_digest:'d'}
  delivery.result('main','call','research_query',material)
  delivery.stepStarted('main','first')
  await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)
  await expect(delivery.confirmMaterials('main','first',async()=>{throw new Error('offline')})).rejects.toThrow('backend_unavailable')
  delivery.stepFinished('main','first'); delivery.stepStarted('main','retry')
  const issues=await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)
  expect(issues[0]).toContain('backend_unavailable')
  await delivery.confirmMaterials('main','retry',async pages=>{
    expect(pages).toHaveLength(1)
    return {receipt_ids:['ok']}
  })
  delivery.stepFinished('main','retry'); delivery.stepStarted('main','after')
  expect(await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)).toEqual([])
})

test('material rejection indices are rebased across batches', async () => {
  const delivery=new EvidenceDelivery()
  const parts=[]
  for (let index=0;index<102;index++) {
    const value={snapshot_id:`s-${index}`,part:'material',material_ref:`pub/P-1#${index}`,
      offset:0,end:1,chunk:'x',manifest_digest:'d'}
    delivery.result('main',`call-${index}`,'research_query',value)
    parts.push({type:'tool-result',id:`call-${index}`,name:'research_query',result:{type:'json',value}})
  }
  delivery.stepStarted('main','first')
  await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  const fake={roleOf:()=> 'main',evidenceDelivery:delivery,
    call:async (_method:string,_session:string,fields:any)=>({receipt_ids:['ok'],
      rejected:fields.pages.length===100 ? [{index:3,reason:'digest_mismatch'}] : [{index:0,reason:'material_unavailable'}]})}
  await expect(Research.prototype.confirmMaterialDelivery.call(fake as any,'main','first')).rejects.toThrow()
  delivery.stepFinished('main','first'); delivery.stepStarted('main','second')
  const issues=await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  expect(issues).toContain('material_delivery_unconfirmed: digest_mismatch pub/P-1#3 [0,1)')
  expect(issues).toContain('material_delivery_unconfirmed: material_unavailable pub/P-1#100 [0,1)')
  await delivery.confirmMaterials('main','second',async pages=>{
    expect(pages).toHaveLength(100)
    expect(pages.some(page=>page.material_ref==='pub/P-1#3' || page.material_ref==='pub/P-1#100')).toBe(false)
    return {receipt_ids:['ok']}
  })
})

test('a late rejection cannot undo a newer successful material confirmation', async () => {
  const delivery=new EvidenceDelivery()
  const material={snapshot_id:'s',part:'material',material_ref:'pub/P-1#data',offset:0,end:3,chunk:'one',manifest_digest:'d'}
  delivery.result('main','call','research_query',material)
  delivery.stepStarted('main','older')
  await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)
  let rejectOld!:(value:any)=>void
  const old=new Promise<any>(resolve=>{rejectOld=resolve})
  const pending=delivery.confirmMaterials('main','older',()=>old).catch(()=>{})
  delivery.stepFinished('main','older'); delivery.stepStarted('main','newer')
  await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)
  await delivery.confirmMaterials('main','newer',async()=>({receipt_ids:['confirmed']}))
  rejectOld({receipt_ids:[],rejected:[{index:0,reason:'material_unavailable'}]})
  await pending
  delivery.stepFinished('main','newer'); delivery.stepStarted('main','after')
  expect(await delivery.request('main',result(material),async()=>({receipt_ids:[]}),100)).toEqual([])
  await delivery.confirmMaterials('main','after',async pages=>{
    expect(pages).toHaveLength(1)
    return {receipt_ids:['still-confirmed']}
  })
})

test('a slow batch reports only its own pages as unconfirmed', async () => {
  const delivery=new EvidenceDelivery()
  const values=['fast','slow'].map(name=>({snapshot_id:name,part:'material',material_ref:`pub/P-1#${name}`,
    offset:0,end:3,chunk:'one',manifest_digest:'d'}))
  const parts=values.map((value,index)=>({type:'tool-result',id:`call-${index}`,name:'research_query',result:{type:'json',value}}))
  for (const [index,value] of values.entries()) delivery.result('main',`call-${index}`,'research_query',value)
  delivery.stepStarted('main','first')
  await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  let completeSlow!:()=>void
  const gate=new Promise<void>(resolve=>{completeSlow=resolve})
  const pending=delivery.confirmMaterials('main','first',async (_pages,onBatch)=>{
    onBatch([0],{receipt_ids:['confirmed']})
    await gate
    onBatch([1],{receipt_ids:[],rejected:[{index:1,reason:'material_unavailable'}]})
  }).catch(()=>{})
  delivery.materialTimedOut('main','first')
  delivery.stepFinished('main','first'); delivery.stepStarted('main','next')
  const issues=await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  expect(issues).toEqual(['material_delivery_unconfirmed: backend_unavailable pub/P-1#slow [0,3)'])
  completeSlow()
  await pending
  delivery.stepFinished('main','next'); delivery.stepStarted('main','after')
  const after=await delivery.request('main',[{content:parts}],async()=>({receipt_ids:[]}),100)
  expect(after).toEqual(['material_delivery_unconfirmed: material_unavailable pub/P-1#slow [0,3)'])
  await delivery.confirmMaterials('main','after',async pages=>{
    expect(pages.map(page=>page.material_ref)).toEqual(['pub/P-1#fast'])
    return {receipt_ids:['confirmed']}
  })
})

test('a late success for an older result cannot clear a newer failure', async () => {
  const delivery=new EvidenceDelivery()
  const material={snapshot_id:'s',part:'material',material_ref:'pub/P-1#data',offset:0,end:3,chunk:'one',manifest_digest:'d'}
  delivery.result('main','old','research_query',material)
  const part=(id:string)=>({type:'tool-result',id,name:'research_query',result:{type:'json',value:material}})
  delivery.stepStarted('main','older')
  await delivery.request('main',[{content:[part('old')]}],async()=>({receipt_ids:[]}),100)
  let releaseOld!:(value:any)=>void
  const old=new Promise<any>(resolve=>{releaseOld=resolve})
  const pending=delivery.confirmMaterials('main','older',()=>old)
  delivery.result('main','new','research_query',material)
  delivery.stepFinished('main','older'); delivery.stepStarted('main','newer')
  await delivery.request('main',[{content:[part('old'),part('new')]}],async()=>({receipt_ids:[]}),100)
  await expect(delivery.confirmMaterials('main','newer',async()=>({receipt_ids:['old'],
    rejected:[{index:1,reason:'material_unavailable'}]}))).rejects.toThrow()
  releaseOld({receipt_ids:['late']})
  await pending
  delivery.stepFinished('main','newer'); delivery.stepStarted('main','after')
  const issues=await delivery.request('main',[{content:[part('old'),part('new')]}],async()=>({receipt_ids:[]}),100)
  expect(issues).toContain('material_delivery_unconfirmed: material_unavailable pub/P-1#data [0,3)')
})
