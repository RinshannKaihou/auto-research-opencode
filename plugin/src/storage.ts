/**
 * Private stdio JSONL channel to `python -m auto_research.service`, adapted from
 * the DSH plugin's ipc.js. The process starts on first use, never at OpenCode
 * startup, and restarts after an unexpected exit unless it keeps failing.
 */
import { execFile, spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { randomUUID } from "node:crypto"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const PLUGIN_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..")
const MAX_REQUEST = 64 * 1024
const MAX_RESPONSE = 4 * 1024 * 1024
const STDERR_TAIL = 8 * 1024
const MAX_PENDING = 4
const MAX_QUEUE = 4096
const RESTART_WINDOW_MS = 60_000
const MAX_RESTARTS = 3

export function pythonModulePath(): string {
  const bundled = join(PLUGIN_ROOT, "python")
  if (existsSync(join(bundled, "auto_research", "service.py"))) return bundled
  return join(PLUGIN_ROOT, "..", "src")
}

export function defaultRegistryPath(): string {
  const data = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share")
  return join(data, "auto-research-opencode", "registry.sqlite3")
}

export interface StorageOptions {
  python?: string
  pythonModulePath?: string
  registryPath?: string
  timeoutMs?: number
}

interface Entry {
  transportId: string
  line: string
  resolve: (value: any) => void
  reject: (error: Error) => void
  retries: number
  timer?: ReturnType<typeof setTimeout>
}

export class StorageClient {
  readonly python: string
  readonly modulePath: string
  readonly registryPath: string
  readonly timeoutMs: number
  private child: ChildProcessWithoutNullStreams | null = null
  private starting: Promise<void> | null = null
  private pending = new Map<string, Entry>()
  private queue: Entry[] = []
  private buffer = Buffer.alloc(0)
  private stderr = ""
  private exits: number[] = []
  private unavailable: Error | null = null
  private closed = false

  constructor(options: StorageOptions = {}) {
    this.python = options.python ?? "python3"
    this.modulePath = options.pythonModulePath ?? pythonModulePath()
    this.registryPath = options.registryPath ?? defaultRegistryPath()
    this.timeoutMs = options.timeoutMs ?? 15_000
  }

  /** Why the service cannot run, if a start already failed for good. */
  get failure(): Error | null {
    return this.unavailable
  }

  request<T = any>(method: string, fields: Record<string, unknown> = {}, operationId: string = randomUUID()): Promise<T> {
    if (this.closed) return Promise.reject(new Error("Research storage is closed"))
    if (this.unavailable) return Promise.reject(this.unavailable)
    if (this.queue.length >= MAX_QUEUE) {
      return Promise.reject(new Error("Research storage queue is full; retry the same operation"))
    }
    const transportId = randomUUID()
    const line = JSON.stringify({ ...fields, transport_id: transportId, operation_id: operationId, method }) + "\n"
    if (Buffer.byteLength(line) > MAX_REQUEST) return Promise.reject(new Error("Research request is too large"))
    return new Promise<T>((resolve, reject) => {
      this.queue.push({ transportId, line, resolve, reject, retries: 0 })
      this.ensureStarted().then(() => this.pump(), (error) => this.failAll(error))
    })
  }

  close(): void {
    this.closed = true
    this.failAll(new Error("Research storage is closed"))
    this.child?.stdin.destroy()
    this.child?.kill("SIGTERM")
    this.child = null
  }

  private ensureStarted(): Promise<void> {
    if (this.child) return Promise.resolve()
    this.starting ??= this.start().finally(() => {
      this.starting = null
    })
    return this.starting
  }

  private async start(): Promise<void> {
    const version = await new Promise<string>((resolve) => {
      execFile(
        this.python,
        ["-c", 'import sys; print("%d.%d" % sys.version_info[:2])'],
        { timeout: 5000, encoding: "utf8" },
        (error, stdout) => resolve(error ? "" : String(stdout).trim()),
      )
    })
    if (!/^3\.(?:1[1-9]|[2-9]\d)$/.test(version)) {
      this.unavailable = new Error(
        `Auto Research needs local Python 3.11+ (${this.python} is ${version ? `version ${version}` : "unavailable"})`,
      )
      throw this.unavailable
    }
    if (this.closed) throw new Error("Research storage is closed")
    const child = spawn(this.python, ["-m", "auto_research.service", "--registry", this.registryPath], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { PATH: process.env.PATH ?? "", PYTHONIOENCODING: "utf-8", PYTHONPATH: this.modulePath },
    })
    this.child = child
    this.buffer = Buffer.alloc(0)
    child.stdout.on("data", (data: Buffer) => this.receive(data))
    child.stderr.on("data", (data: Buffer) => {
      this.stderr = (this.stderr + data.toString("utf8")).slice(-STDERR_TAIL)
    })
    child.stdin.on("error", () => this.lost(child, "Research storage channel closed"))
    child.on("error", () => this.lost(child, "Research storage could not start"))
    child.on("exit", () => this.lost(child, "Research storage stopped"))
  }

  private lost(child: ChildProcessWithoutNullStreams, reason: string): void {
    if (this.child !== child) return
    this.child = null
    const now = Date.now()
    this.exits = [...this.exits.filter((at) => now - at < RESTART_WINDOW_MS), now]
    const detail = this.stderr.trim().split("\n").slice(-3).join(" | ")
    const error = new Error(detail ? `${reason}: ${detail}` : reason)
    if (this.exits.length >= MAX_RESTARTS) this.unavailable = error
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer)
      entry.reject(error)
    }
    this.pending.clear()
    if (this.unavailable) this.failAll(this.unavailable)
    else if (this.queue.length && !this.closed) {
      this.ensureStarted().then(() => this.pump(), (failure) => this.failAll(failure))
    }
  }

  private receive(data: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, data])
    while (true) {
      const end = this.buffer.indexOf(10)
      if (end < 0) break
      const raw = this.buffer.subarray(0, end)
      this.buffer = this.buffer.subarray(end + 1)
      if (raw.length > MAX_RESPONSE) continue
      let response: any
      try {
        response = JSON.parse(raw.toString("utf8"))
      } catch {
        continue
      }
      const entry = this.pending.get(response.request_id)
      if (!entry) continue
      this.pending.delete(response.request_id)
      clearTimeout(entry.timer)
      if (response.ok) entry.resolve(response.value)
      else entry.reject(new Error(response.error?.message ?? "Research storage request failed"))
    }
    if (this.buffer.length > MAX_RESPONSE) this.buffer = Buffer.alloc(0)
    this.pump()
  }

  private pump(): void {
    const child = this.child
    while (child && !this.closed && this.pending.size < MAX_PENDING && this.queue.length) {
      const entry = this.queue.shift()!
      entry.timer = setTimeout(() => {
        this.pending.delete(entry.transportId)
        if (++entry.retries <= 2) this.queue.unshift(entry)
        else entry.reject(new Error("Research storage timed out"))
        this.pump()
      }, this.timeoutMs)
      this.pending.set(entry.transportId, entry)
      child.stdin.write(entry.line)
    }
  }

  private failAll(error: Error): void {
    for (const entry of [...this.pending.values(), ...this.queue.splice(0)]) {
      clearTimeout(entry.timer)
      entry.reject(error)
    }
    this.pending.clear()
  }
}
