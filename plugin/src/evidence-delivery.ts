/** Evidence receipts count only exact tool results visible in a model request. */
import { createHash } from 'node:crypto'

export type EvidencePage = { snapshot_id:string; draft_id?:string; part?:string; material_ref?:string;
  model_message_id?:string; offset:number; end:number; manifest_digest:string; chunk_digest:string }
export type EvidenceAck = { receipt_ids:string[]; rejected?:{index?:number;reason:string}[] }
type MaterialPage = { key:string; page:EvidencePage; epoch:number }
type MaterialFailure = { page:EvidencePage; reason:string }
type EvidenceContext = { epoch:number; pages:MaterialPage[]; receipts:string[]; message?:string }

export class EvidenceDelivery {
  private active = new Map<string,string>()
  private messages = new Map<string,string[]>()
  private calls = new Map<string,string[]>()
  private outputs = new Map<string,{session:string;name:string;text:string;json:string;page:EvidencePage}>()
  private epoch = new Map<string,number>()
  private contexts = new Map<string,EvidenceContext>()
  private messageContexts = new Map<string,number>()
  private materialMessages = new Map<string,MaterialPage[]>()
  private materialInFlight = new Map<string,Promise<void>>()
  private materialPending = new Map<string,Set<number>>()
  private materialFailures = new Map<string,Map<string,MaterialFailure>>()
  private materialOutcomes = new Map<string,number>()
  private generations = new Map<string,number>()

  private bind(session:string,message:string,context:EvidenceContext): void {
    const key=`${session}:${message}`
    context.message=message
    this.messageContexts.set(key,context.epoch)
    this.materialMessages.set(key,context.pages)
    this.messages.set(key,context.receipts)
  }

  stepStarted(session:string, message:string): void {
    this.active.set(session,message)
    const context=this.contexts.get(session)
    if (context && !context.message) this.bind(session,message,context)
  }

  stepFinished(session:string,message:string): void {
    if (this.active.get(session)===message) this.active.delete(session)
  }

  stepFailed(session:string,message:string): void {
    this.stepFinished(session,message)
    this.materialMessages.delete(`${session}:${message}`)
  }

  materialTimedOut(session:string,message:string): void {
    const entries=this.materialMessages.get(`${session}:${message}`) ?? []
    if (!entries.length) return
    const failures=this.materialFailures.get(session) ?? new Map<string,MaterialFailure>()
    const pending=this.materialPending.get(`${session}:${message}`)
    for (const [index,{key,page,epoch}] of entries.entries()) {
      if (pending && !pending.has(index)) continue
      if ((this.materialOutcomes.get(key) ?? 0) <= epoch) {
        failures.set(key,{page,reason:'backend_unavailable'})
        this.materialOutcomes.set(key,epoch)
      }
    }
    if (failures.size) this.materialFailures.set(session,failures)
  }

  async confirmMaterials(session:string,message:string,
    acknowledge:(pages:EvidencePage[],onBatch:(indices:number[],result:EvidenceAck)=>void)=>Promise<EvidenceAck | void>): Promise<void> {
    const key=`${session}:${message}`
    const existing=this.materialInFlight.get(key)
    if (existing) return existing
    const entries=this.materialMessages.get(key) ?? []
    if (!entries.length) return
    const generation=this.generations.get(session) ?? 0
    const pages=entries.map(({page})=>({...page,model_message_id:message}))
    const unconfirmed=new Set(entries.map((_,index)=>index))
    this.materialPending.set(key,unconfirmed)
    const pending=(async () => {
      const reasons:string[]=[]
      const collect=(indices:number[],result:EvidenceAck) => {
        if ((this.generations.get(session) ?? 0)!==generation) return
        const failures=this.materialFailures.get(session) ?? new Map<string,MaterialFailure>()
        const rejected=new Map((result.rejected ?? []).map(item=>[item.index!,item.reason]))
        for (const index of indices) {
          if (!unconfirmed.delete(index)) continue
          const entry=entries[index]
          if ((this.materialOutcomes.get(entry.key) ?? 0)>entry.epoch ||
              (this.outputs.get(entry.key)?.page!==entry.page && !failures.has(entry.key))) continue
          this.materialOutcomes.set(entry.key,entry.epoch)
          const reason=rejected.get(index)
          if (reason) {
            reasons.push(reason)
            failures.set(entry.key,{page:entry.page,reason})
            // A rejected historical result must not poison every subsequent request.
            // Transport failures remain eligible for retry; a fresh read gets a new key.
            if (reason!=='backend_unavailable' && this.outputs.get(entry.key)?.page===entry.page) this.outputs.delete(entry.key)
          } else {
            for (const [failedKey,failure] of failures) {
              const old=failure.page, current=entry.page
              if ((this.materialOutcomes.get(failedKey) ?? 0)<=entry.epoch &&
                  old.snapshot_id===current.snapshot_id && old.material_ref===current.material_ref &&
                  old.offset===current.offset && old.end===current.end && old.chunk_digest===current.chunk_digest) {
                failures.delete(failedKey)
                this.materialOutcomes.set(failedKey,entry.epoch)
              }
            }
          }
        }
        if (failures.size) this.materialFailures.set(session,failures)
        else this.materialFailures.delete(session)
      }
      try {
        const result=await acknowledge(pages,collect)
        if (result) collect([...unconfirmed],result)
      } catch {
        const indices=[...unconfirmed]
        collect(indices,{receipt_ids:[],rejected:indices.map(index=>({index,reason:'backend_unavailable'}))})
      }
      if ((this.generations.get(session) ?? 0)!==generation) return
      this.materialMessages.delete(key)
      if (reasons.length) throw new Error(`Evidence material delivery was not confirmed: ${reasons.join(', ')}`)
    })()
    this.materialInFlight.set(key,pending)
    try { await pending } finally {
      if(this.materialInFlight.get(key)===pending) {
        this.materialInFlight.delete(key)
        this.materialPending.delete(key)
      }
    }
  }

  forget(session:string): void {
    this.epoch.set(session,(this.epoch.get(session) ?? 0)+1)
    this.generations.set(session,(this.generations.get(session) ?? 0)+1)
    this.active.delete(session); this.contexts.delete(session); this.materialFailures.delete(session)
    for (const key of this.messageContexts.keys()) if (key.startsWith(session+':')) this.messageContexts.delete(key)
    for (const key of this.messages.keys()) if (key.startsWith(session+':')) this.messages.delete(key)
    for (const key of this.calls.keys()) if (key.startsWith(session+':')) this.calls.delete(key)
    for (const key of this.materialMessages.keys()) if (key.startsWith(session+':')) this.materialMessages.delete(key)
    for (const key of this.materialInFlight.keys()) if (key.startsWith(session+':')) this.materialInFlight.delete(key)
    for (const key of this.materialPending.keys()) if (key.startsWith(session+':')) this.materialPending.delete(key)
    for (const key of this.materialOutcomes.keys()) if (key.startsWith(session+':')) this.materialOutcomes.delete(key)
    for (const [key,value] of this.outputs) if (value.session===session) this.outputs.delete(key)
  }

  result(session:string, id:string, name:string, value:any): void {
    if (name !== 'research_query' || typeof value?.snapshot_id !== 'string' || typeof value?.chunk !== 'string') return
    const end = value.end ?? value.cursor?.offset ?? value.total_chars
    if (typeof value.offset !== 'number' || typeof end !== 'number' || end <= value.offset || typeof value.manifest_digest !== 'string') return
    this.outputs.set(`${session}:${id}`, { session, name, text:JSON.stringify(value,null,2), json:JSON.stringify(value),
      page:{snapshot_id:value.snapshot_id,...(value.draft_id ? {draft_id:value.draft_id} : {}),
        ...(value.part ? {part:value.part} : {}),...(value.material_ref ? {material_ref:value.material_ref} : {}),
        offset:value.offset,end,manifest_digest:value.manifest_digest,
        chunk_digest:createHash('sha256').update(value.chunk,'utf8').digest('hex')} })
  }

  async request(session:string, messages:any[], acknowledge:(pages:EvidencePage[],onBatch:(result:EvidenceAck)=>void)=>Promise<EvidenceAck | void>, timeoutMs:number): Promise<string[]> {
    const epoch=(this.epoch.get(session) ?? 0)+1
    this.epoch.set(session,epoch)
    const pages:MaterialPage[]=[]
    const visible=new Set<string>()
    for (const message of messages) for (const part of message.content ?? []) {
      if (part.type !== 'tool-result') continue
      const key=`${session}:${part.id}`; visible.add(key)
      const output=this.outputs.get(key)
      if (!output || output.name !== part.name) continue
      const result=part.result
      const matched=result?.type==='text' && result.value===output.text
        || result?.type==='json' && JSON.stringify(result.value)===output.json
        || result?.type==='content' && Array.isArray(result.value) && result.value.length===1
           && result.value[0].type==='text' && result.value[0].text===output.text
      if (matched) pages.push({key,page:output.page,epoch})
    }
    for (const [key,value] of this.outputs) if (value.session===session && !visible.has(key)) this.outputs.delete(key)
    for (const key of this.materialOutcomes.keys()) if (key.startsWith(session+':') && !visible.has(key)) this.materialOutcomes.delete(key)
    const materialPages=pages.filter(({page})=>page.part==='material')
    const decisionPages=pages.filter(({page})=>page.part!=='material').map(({page})=>page)
    const context:EvidenceContext={epoch,pages:materialPages,receipts:[]}
    this.contexts.set(session,context)
    const current=this.active.get(session)
    if (current && !this.messageContexts.has(`${session}:${current}`)) this.bind(session,current,context)
    const receipts=new Set<string>()
    const issues=new Set<string>()
    const failures=this.materialFailures.get(session)
    if (failures) {
      for (const [key,{page,reason}] of failures) {
        if (!visible.has(key)) { failures.delete(key); continue }
        issues.add(`material_delivery_unconfirmed: ${reason} ${page.material_ref} [${page.offset},${page.end})`)
      }
      if (!failures.size) this.materialFailures.delete(session)
    }
    let accepting=true
    const collect=(result:EvidenceAck) => {
      if (!accepting || this.epoch.get(session)!==epoch) return
      for (const id of result.receipt_ids) receipts.add(id)
      for (const item of result.rejected ?? []) issues.add(item.reason)
    }
    if (decisionPages.length && timeoutMs > 0) {
      let timer:ReturnType<typeof setTimeout> | undefined
      try {
        const result=await Promise.race([acknowledge(decisionPages,collect),new Promise<'timeout'>(resolve=>{timer=setTimeout(()=>resolve('timeout'),timeoutMs)})])
        if (result==='timeout') issues.add('timeout')
        else if (result) collect(result)
      } catch { issues.add('backend_unavailable') }
      finally { clearTimeout(timer) }
    } else if (decisionPages.length) {
      issues.add('timeout')
    }
    accepting=false
    if (this.epoch.get(session)!==epoch) return []
    const accepted=[...receipts]
    context.receipts=accepted
    if (context.message) this.messages.set(`${session}:${context.message}`,accepted)
    return [...issues]
  }

  forCall(session:string,message:string,id:string): string[] {
    const key=`${session}:${message}:${id}`
    if (!this.calls.has(key)) this.calls.set(key,this.messages.get(`${session}:${message}`) ?? [])
    return this.calls.get(key)!
  }
}
