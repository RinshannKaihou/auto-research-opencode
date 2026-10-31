import { Worker } from 'node:worker_threads'

export const SOURCE_ALGORITHM = 'source-worker-v1'
export const SOURCE_LIMITS = { entries: 10000, files: 2000, bytes: 16 * 1024 * 1024, timeoutMs: 500 }
export interface SourceResult {
  digest: string | null
  reason?: string
  entries: number
  files: number
  filesHashed: number
  bytesRead: number
}
export interface SourceSnapshot extends SourceResult {
  algorithm: typeof SOURCE_ALGORITHM
  owner: { cwd: string; node_id: string; attempt_id: string | null; generation: number }
}
export const unknownSource = (reason: string): SourceResult => ({
  digest: null, reason, entries: 0, files: 0, filesHashed: 0, bytesRead: 0,
})

/** One bounded worker; overlapping or unavailable probes stay unknown, never queue behind I/O. */
export class WorkspaceProbe {
  private worker?: Worker
  private pending?: { resolve(value: SourceResult): void; timer: ReturnType<typeof setTimeout> }
  private stopping?: Promise<number>
  private closed = false

  constructor(
    private readonly limits = SOURCE_LIMITS,
    private readonly workerURL = new URL('./workspace-probe-worker.ts', import.meta.url),
  ) {}

  sample(cwd: string, timeoutMs = this.limits.timeoutMs): Promise<SourceResult> {
    if (this.closed) return Promise.resolve(unknownSource('closed'))
    if (this.pending || this.stopping) return Promise.resolve(unknownSource('busy'))
    if (timeoutMs<=0) return Promise.resolve(unknownSource('time_limit'))
    return new Promise(resolve => {
      this.pending = { resolve, timer: setTimeout(() => {
        this.finish(unknownSource('time_limit'))
        this.stopWorker()
      }, timeoutMs) }
      try {
        if (!this.worker) {
          const worker = new Worker(this.workerURL)
          this.worker = worker
          worker.unref()
          worker.on('message', (result: SourceResult) => {
            if (this.worker === worker) this.finish(result)
          })
          worker.on('error', () => {
            if (this.worker === worker) {
              this.finish(unknownSource('worker_exit'))
              this.stopWorker()
            }
          })
          worker.on('exit', () => {
            if (this.worker === worker) {
              this.worker = undefined
              this.finish(unknownSource('worker_exit'))
            }
          })
        }
        this.worker.postMessage({ cwd, limits: {...this.limits,timeoutMs} })
      } catch {
        this.finish(unknownSource('worker_exit'))
        this.stopWorker()
      }
    })
  }

  private finish(result: SourceResult): void {
    const pending = this.pending
    if (!pending) return
    this.pending = undefined
    clearTimeout(pending.timer)
    pending.resolve(result)
  }

  private stopWorker(): void {
    const worker = this.worker
    this.worker = undefined
    if (worker) {
      this.stopping = worker.terminate()
      void this.stopping.finally(() => { this.stopping = undefined })
    }
  }

  close(): void {
    this.closed = true
    this.finish(unknownSource('closed'))
    this.stopWorker()
  }
}
