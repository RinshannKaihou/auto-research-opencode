/** A base follows a model request and its assistant message, never the latest cache. */
export interface CheckpointSeen { base:string; snapshot:Record<string,any> }

export class CheckpointDelivery {
  private active = new Map<string,string>()
  private delivered = new Map<string,CheckpointSeen>()
  private messages = new Map<string,CheckpointSeen>()
  private calls = new Map<string,CheckpointSeen | undefined>()
  private outputs = new Map<string,{session:string;id:string;name:string;text:string;json:string;seen:CheckpointSeen}>()

  stepStarted(session:string, message:string): void {
    this.active.set(session,message)
    const seen = this.delivered.get(session)
    if (seen) this.messages.set(`${session}:${message}`,seen)
    else this.messages.delete(`${session}:${message}`)
  }

  clear(session:string): void {
    this.delivered.delete(session)
    const message = this.active.get(session)
    if (message) this.messages.delete(`${session}:${message}`)
  }

  result(session:string,id:string,name:string,value:any): void {
    const base = value.next_base ?? value.base
    const snapshot = value.next_snapshot ?? value.snapshot
    if (typeof base === 'string' && snapshot) this.outputs.set(`${session}:${id}`, {
      session,id,name,text:JSON.stringify(value,null,2),json:JSON.stringify(value),seen:{base,snapshot},
    })
  }

  request(session:string, messages:any[], owner:Record<string,any> | undefined, context?:CheckpointSeen): void {
    let chosen = context
    for (const message of messages) for (const part of message.content ?? []) {
      if (part.type !== 'tool-result') continue
      const output = this.outputs.get(`${session}:${part.id}`)
      if (!output || output.name !== part.name) continue
      const result = part.result
      const matched = result?.type === 'text' && result.value === output.text
        || result?.type === 'json' && JSON.stringify(result.value) === output.json
        || result?.type === 'content' && Array.isArray(result.value)
           && result.value.length === 1 && result.value[0].type === 'text' && result.value[0].text === output.text
      if (!matched) continue
      const snapshot = output.seen.snapshot
      if (owner && ['host_id','session_id','association_id','attempt_id','node_id','generation'].some(key => owner[key] !== snapshot[key])) continue
      if (!chosen || snapshot.head_revision >= chosen.snapshot.head_revision) chosen = output.seen
    }
    this.clear(session)
    if (chosen) {
      this.delivered.set(session,chosen)
      const active = this.active.get(session)
      if (active) this.messages.set(`${session}:${active}`,chosen)
    }
  }

  forCall(session:string,message:string,id:string): string | undefined {
    const key = `${session}:${message}:${id}`
    if (!this.calls.has(key)) this.calls.set(key,this.messages.get(`${session}:${message}`))
    return this.calls.get(key)?.base
  }
}
