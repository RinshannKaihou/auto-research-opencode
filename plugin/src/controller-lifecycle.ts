import { randomUUID } from "node:crypto"
import { realpathSync } from "node:fs"
import { dirname, relative, resolve } from "node:path"
import type { ModelRef } from "./engine"

/** State shared by successive plugin attachments within one server process. */
export interface RuntimeState {
  busy: Set<string>
  ours: Set<string>
  wrote: Map<string, boolean>
  idle: Map<string, number>
  turns: Map<string, string>
  startedAt: Map<string, number>
  startedEvents: Map<string, string>
  pendingInbox: Map<string, { id: string; seq?: number }[]>
  inferredStart: Set<string>
  pendingStarts: Map<string, { execution: string; eventID?: string; eventSeq?: number; ambiguous: boolean }>
  pendingEnds: Map<string, { execution?: string; turn: string; uncertain: boolean; error?: unknown }>
  endedEvents: Map<string, string[]>
  retried: Set<string>
  queues: Map<string, Promise<unknown>>
  lastScheduledScope?: Map<string,string>
  models: Map<string, ModelRef>
  specialistTools: Map<string, readonly string[]>
  specialistResearch: Set<string>
  shellInboxes?: Map<string, { sessionID: string; execution?: string; matched: boolean; authorized?: boolean }>
  shellWake?: Map<string, { matched: boolean }>
  shellCalls?: Map<string, { sessionID: string; background: boolean; settled: Promise<void>; resolve: () => void }>
}

interface Entry {
  binding: string
  ready: boolean
  state: RuntimeState
  handlers?: {
    before: (request: any) => Promise<void>
    context: (request: any) => Promise<void>
    tool: (tool: any, call: any, args: Record<string, any>) => Promise<unknown>
    command: (input: any) => Promise<unknown>
    session: (input: any) => Promise<unknown>
    board: (input: any) => Promise<unknown>
  }
}

const REGISTRY = Symbol.for("auto-research.controller.v1")
const EPOCH = Symbol.for("auto-research.process-epoch.v1")

function registry(): Map<string, Entry> {
  const global = globalThis as any
  return global[REGISTRY] ??= new Map<string, Entry>()
}

function canonical(path: string): string {
  const target = resolve(path)
  let parent = target
  for (;;) {
    try { return resolve(realpathSync(parent), relative(parent, target)) } catch {
      const next = dirname(parent)
      if (next === parent) return target
      parent = next
    }
  }
}

function key(scope: string, registryPath: string, workspaceID?: string): string {
  // There is one persisted owner per ledger. Workspace aliases must share it.
  return JSON.stringify([canonical(scope), canonical(registryPath)])
}

export function attachController(scope: string, registryPath: string, workspaceID?: string) {
  const global = globalThis as any
  const epoch: string = global[EPOCH] ??= randomUUID()
  const identity = key(scope, registryPath, workspaceID)
  let entry = registry().get(identity)
  const warm = !!entry
  const binding = randomUUID()
  if (!entry) {
    entry = { binding, ready: false, state: {
      busy: new Set(), ours: new Set(), wrote: new Map(), idle: new Map(), turns: new Map(),
      startedAt: new Map(), startedEvents: new Map(), pendingInbox: new Map(), inferredStart: new Set(), pendingStarts: new Map(), pendingEnds: new Map(), endedEvents: new Map(), retried: new Set(), queues: new Map(), models: new Map(),
      specialistTools: new Map(), specialistResearch: new Set(),
    } }
    registry().set(identity, entry)
  } else {
    entry.binding = binding
    entry.ready = false
    entry.state.startedEvents ??= new Map()
    entry.state.pendingInbox ??= new Map()
    entry.state.inferredStart ??= new Set()
    entry.state.pendingStarts ??= new Map()
    entry.state.pendingEnds ??= new Map()
    entry.state.endedEvents ??= new Map()
  }
  let closed = false
  return {
    state: entry.state,
    warm,
    epoch,
    binding,
    pid: process.pid,
    current: () => !closed && entry!.binding === binding,
    successorReady: () => entry!.binding !== binding && entry!.ready,
    markReady: () => { if (!closed && entry!.binding === binding) entry!.ready = true },
    setHandlers: (handlers: NonNullable<Entry['handlers']>) => { if (!closed && entry!.binding === binding) entry!.handlers = handlers },
    forwardBefore: async (request: any) => {
      if (!entry!.ready || !entry!.handlers) throw new Error('Research controller handoff is not ready')
      return entry!.handlers.before(request)
    },
    forwardContext: async (request: any) => {
      if (!entry!.ready || !entry!.handlers) throw new Error('Research controller handoff is not ready')
      return entry!.handlers.context(request)
    },
    forwardTool: async (tool: any, call: any, args: Record<string, any>) => {
      if (!entry!.ready || !entry!.handlers) throw new Error('Research controller handoff is not ready')
      return entry!.handlers.tool(tool, call, args)
    },
    forwardRpc: async (kind: 'command' | 'session' | 'board', input: any) => {
      if (!entry!.ready || !entry!.handlers) throw new Error('Research controller handoff is not ready')
      return entry!.handlers[kind](input)
    },
    detach: () => { closed = true; if (entry!.binding === binding) entry!.ready = false },
  }
}

/** @internal Tests simulate a new host process rather than a plugin reload. */
export function forgetControllerForTest(scope: string, registryPath: string, workspaceID?: string): void {
  registry().delete(key(scope, registryPath, workspaceID))
}
