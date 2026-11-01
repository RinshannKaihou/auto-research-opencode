/** Evidence receipts count only exact tool results visible in a model request. */
import { createHash } from 'node:crypto'

export type EvidencePage = { snapshot_id:string; draft_id?:string; part?:string; material_ref?:string;
  model_message_id?:string; offset:number; end:number; manifest_digest:string; chunk_digest:string }
export type EvidenceAck = { receipt_ids:string[]; rejected?:{reason:string}[] }
type EvidenceContext = { epoch:number; pages:EvidencePage[]; receipts:string[]; message?:string }

export class EvidenceDelivery {
  private active = new Map<string,string>()
  private messages = new Map<string,string[]>()
  private calls = new Map<string,string[]>()
  private outputs = new Map<string,{session:string;name:string;text:string;json:string;page:EvidencePage}>()
  private epoch = new Map<string,number>()
  private contexts = new Map<string,EvidenceContext>()
  private messageContexts = new Map<string,number>()
  private materialMessages = new Map<string,EvidencePage[]>()
  private materialInFlight = new Map<string,Promise<void>>()
  private materialFailure = new Map<string,string>()

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

  async confirmMaterials(session:string,message:string, acknowledge:(pages:EvidencePage[])=>Promise<EvidenceAck>): Promise<void> {
    const key=`${session}:${message}`
    const existing=this.materialInFlight.get(key)
    if (existing) return existing
    const pages=(this.materialMessages.get(key) ?? []).map(page=>({...page,model_message_id:message}))
    if (!pages.length) return
    const pending=(async () => {
      const result=await acknowledge(pages)
      if (result.rejected?.length) throw new Error(`Evidence material delivery was not confirmed: ${result.rejected.map(item=>item.reason).join(', ')}`)
      this.materialMessages.delete(key)
      this.materialFailure.delete(session)
    })()
    this.materialInFlight.set(key,pending)
    try { await pending } catch (error) {
      this.materialFailure.set(session,'material_delivery_unconfirmed')
      throw error
    } finally { if(this.materialInFlight.get(key)===pending)this.materialInFlight.delete(key) }
  }

  forget(session:string): void {
    this.epoch.set(session,(this.epoch.get(session) ?? 0)+1)
    this.active.delete(session); this.contexts.delete(session); this.materialFailure.delete(session)
    for (const key of this.messageContexts.keys()) if (key.startsWith(session+':')) this.messageContexts.delete(key)
    for (const key of this.messages.keys()) if (key.startsWith(session+':')) this.messages.delete(key)
    for (const key of this.calls.keys()) if (key.startsWith(session+':')) this.calls.delete(key)
    for (const key of this.materialMessages.keys()) if (key.startsWith(session+':')) this.materialMessages.delete(key)
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
    const pages:EvidencePage[]=[]
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
      if (matched) pages.push(output.page)
    }
    for (const [key,value] of this.outputs) if (value.session===session && !visible.has(key)) this.outputs.delete(key)
    const materialPages=pages.filter(page=>page.part==='material')
    const decisionPages=pages.filter(page=>page.part!=='material')
    const context:EvidenceContext={epoch,pages:materialPages,receipts:[]}
    this.contexts.set(session,context)
    const current=this.active.get(session)
    if (current && !this.messageContexts.has(`${session}:${current}`)) this.bind(session,current,context)
    const receipts=new Set<string>()
    const issues=new Set<string>()
    const failure=this.materialFailure.get(session)
    if (failure) issues.add(failure)
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
