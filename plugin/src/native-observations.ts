import { randomUUID } from 'node:crypto'
import type { Research } from './research'
import type { Observations } from './observations'

export class NativeObservations {
  private auxiliary = new Map<string,string[]>()
  private seen = new Map<string,Set<string>>()
  constructor(private research:Research, private log:Observations) {}

  forget(session:string): void {
    this.seen.delete(session)
    for(const key of this.auxiliary.keys()) if(key.startsWith(session+':'))this.auxiliary.delete(key)
  }

  private async save(session:string,fields:Record<string,unknown>,id:string): Promise<void> {
    const root = this.research.rootOf(session)!
    this.log.append(root,{...fields,phase:'native_usage',usage_phase:fields.phase,session_id:session,event_id:id})
    await this.research.call('runtime_native_event',session,{fields},`native:${id}`).catch(error => {
      this.log.append(root,{phase:'coverage_gap',source:'native_usage',session_id:session,event_id:id,
        error_code:error.code ?? 'storage_unavailable'})
    })
  }

  modelRequest(request:any): void {
    const session = request.sessionID
    const root = this.research.rootOf(session)
    if (!root || !this.research.inScope(root)) return
    this.log.append(root,{phase:'model_request',session_id:session,kind:request.kind,model:request.model})
    if (request.kind==='primary') return
    const key = `aux:${session}:${request.kind}:${randomUUID()}`
    const queueKey = `${session}:${request.kind}`
    this.auxiliary.set(queueKey,[...(this.auxiliary.get(queueKey) ?? []),key])
    void this.save(session,{phase:'started',source_key:key,kind:request.kind,model:request.model},key)
  }

  event(event:any): void {
    const {type,data} = event
    const session = data?.sessionID
    if (!session || !this.research.isAssociated(session)) return
    const root = this.research.rootOf(session)!
    if (type==='session.step.started') {
      this.research.checkpoints.stepStarted(session,data.assistantMessageID)
      this.research.evidenceDelivery.stepStarted(session,data.assistantMessageID)
    }
    if (type==='session.step.ended') {
      void this.research.confirmMaterialDelivery(session,data.assistantMessageID).catch(error => {
        this.log.append(root,{phase:'coverage_gap',source:'material_delivery',session_id:session,
          error_code:error.code ?? 'delivery_unconfirmed'})
      })
      this.research.evidenceDelivery.stepFinished(session,data.assistantMessageID)
    }
    if (type==='session.step.failed') this.research.evidenceDelivery.stepFinished(session,data.assistantMessageID)
    if (!event.id) return
    if (type==='session.step.started') {
      void this.save(session,{phase:'started',source_key:`step:${session}:${data.assistantMessageID}`,model:data.model},event.id)
    } else if (type==='session.step.ended' || type==='session.step.failed') {
      void this.save(session,{phase:type.endsWith('failed')?'failed':'completed',
        source_key:`step:${session}:${data.assistantMessageID}`,tokens:data.tokens ?? null,cost:data.cost ?? null},event.id)
    } else if (type==='session.usage.recorded') {
      const seen=this.seen.get(session) ?? new Set<string>()
      if(seen.has(event.id))return
      seen.add(event.id);this.seen.set(session,seen)
      const request_key = this.auxiliary.get(`${session}:${data.source}`)?.shift()
      if(!this.auxiliary.get(`${session}:${data.source}`)?.length)this.auxiliary.delete(`${session}:${data.source}`)
      void this.save(session,{phase:'completed',source_key:`aux-event:${event.id}`,kind:data.source,
        ...(request_key?{request_key}:{}),tokens:data.tokens,cost:data.cost},event.id)
    } else if (type==='session.usage.updated') {
      this.log.append(root,{phase:'native_aggregate',session_id:session,event_id:event.id,tokens:data.tokens,cost:data.cost})
    }
  }
}
