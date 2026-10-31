import { appendFileSync, mkdirSync } from "node:fs"
import { join } from "node:path"
import { randomUUID } from "node:crypto"
import { AsyncLocalStorage } from "node:async_hooks"

export interface Invocation {
  invocation_id: string
  operation_id: string
  session_id: string
  tool: string
  contract: string
  input_fields: number
  input_chars: number
  context?: Record<string, unknown>
}

/** Local metadata only. A failed ledger transaction cannot roll this file back. */
export class Observations {
  readonly instanceID = randomUUID()
  private failures = new Map<string, string>()
  private active = new AsyncLocalStorage<{root:string;invocation_id:string}>()

  transportObserver(operation_id: string, method: string,fields:Record<string,unknown>): {attempt(n:number):void;response(value:any):void} | undefined {
    const call = this.active.getStore()
    if (!call) return undefined
    return {
      attempt: attempt => this.append(call.root, {phase:'transport',operation_id,method,attempt,invocation_id:call.invocation_id,
        wire_fields:Object.keys(fields).length+3,
        host_fields:['host_id','session_id','plugin_instance_id','model_call','checkpoint_base','contract_version'].filter(key=>key in fields).length}),
      response: value => {
        if(value.invocation_context) this.append(call.root,{phase:'binding',invocation_id:call.invocation_id,context:value.invocation_context})
        this.append(call.root, {phase:'dispatch_result',operation_id,method,invocation_id:call.invocation_id,
          replayed:value.replayed ?? null,deduplicated:value.value?.deduplicated ?? null})
      },
    }
  }

  coverage(root: string): { complete: boolean; error?: string } {
    const error = this.failures.get(root)
    return error ? { complete: false, error } : { complete: true }
  }

  append(root: string, fact: Record<string, unknown>): void {
    const directory = join(root, ".research", "observations")
    try {
      mkdirSync(directory, { recursive: true })
      appendFileSync(join(directory, `${this.instanceID}.jsonl`), JSON.stringify({
        ...fact, version: "invocation/1", plugin_instance_id: this.instanceID, at: new Date().toISOString(),
      }) + "\n", { encoding: "utf8", mode: 0o600 })
    } catch (error) {
      // Observation I/O must not turn an already committed mutation into a failed call.
      if (!(error instanceof Error) || !("code" in error)) throw error
      const detail = `Research observation write failed (${String(error.code)})`
      if (!this.failures.has(root)) process.stderr.write(`${detail}\n`)
      this.failures.set(root, detail)
    }
  }

  async invoke<T>(root: string, invocation: Invocation, work: () => Promise<T>): Promise<T> {
    const started = performance.now()
    this.append(root, { ...invocation, phase: "start" })
    try {
      const result = await this.active.run({root,invocation_id:invocation.invocation_id},work)
      this.append(root, { invocation_id: invocation.invocation_id, phase: "end", outcome: "succeeded",
        duration_ms: performance.now() - started })
      return result
    } catch (error) {
      this.append(root, { invocation_id: invocation.invocation_id, phase: "end", outcome: "failed",
        error_code: error instanceof Error && "code" in error && typeof error.code === "string"
          ? error.code : error instanceof Error ? error.name : "unknown",
        duration_ms: performance.now() - started })
      throw error
    }
  }
}
