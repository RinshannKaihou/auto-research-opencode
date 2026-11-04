/**
 * Where the research context goes inside a model request.
 *
 * The backend renders the context as `Research context:` followed by
 * `## <block>` sections separated by blank lines. Providers cache a request
 * prefix, so blocks that the model rewrites during a turn belong after the
 * conversation, where a change cannot invalidate the cached history. Blocks
 * that only change at role or run-state transitions stay in the system prompt.
 *
 * Everything outside the allowlist is volatile, including blocks added later:
 * a volatile block left in the system prompt silently brings the bug back,
 * while a stable block sent as volatile only costs its own size.
 */

/** Blocks that change only at role or run-state transitions. */
export const STABLE_CONTEXT_BLOCKS = [
  'role_instructions',
  'control_instructions',
  'identity',
  'project',
  'task',
  'specialist_task',
  'frozen_context',
  'method_guidance',
  'index',
] as const

const STABLE = new Set<string>(STABLE_CONTEXT_BLOCKS)
const HEADER = 'Research context:'

/**
 * Marker the fallback in `Research.memory` adds when the ledger is slow. Its
 * text replaces the real instructions, so it keeps the old system placement.
 */
export const STALE_CONTEXT_MARKER = '[Research memory is stale.'

export interface ResearchContextParts {
  stable: string
  volatile: string
}

/**
 * Split rendered context without parsing or rewriting block values. The
 * backend joins blocks with a blank line and keeps each value on one line, so
 * the same separators are enough to find every block exactly once.
 */
export function splitResearchContext(text: string): ResearchContextParts {
  if (!text.startsWith(HEADER)) return { stable: text, volatile: '' }
  const header = text.startsWith(`${HEADER}\n`) ? `${HEADER}\n` : HEADER
  const body = text.slice(header.length)
  const chunks = body.split('\n\n')
  if (!chunks.some(chunk => /^## [^\n]+\n/.test(chunk))) return { stable: text, volatile: '' }
  const preamble: string[] = []
  const stable: string[] = []
  const volatile: string[] = []
  for (const chunk of chunks) {
    const match = /^## ([^\n]+)\n/.exec(chunk)
    if (!match) {
      // Text before the first block is not a block; the trailing stale notice
      // and any future non-block text travel with the volatile part.
      if (!stable.length && !volatile.length) preamble.push(chunk)
      else if (chunk.trim()) volatile.push(chunk)
      continue
    }
    if (STABLE.has(match[1]!)) stable.push(chunk)
    else volatile.push(chunk)
  }
  const stableText = stable.length || preamble.length
    ? `${header}${[...preamble, ...stable].join('\n\n')}`
    : ''
  return { stable: stableText, volatile: volatile.join('\n\n') }
}

/**
 * One user message carrying the volatile part. It is appended after the tool
 * results, only for this request, and says that it is injected state so the
 * model does not treat it as new instructions.
 */
export function researchStateMessage(parts: string[]): {
  role: 'user'
  content: { type: 'text'; text: string }[]
} {
  const body = parts.filter(part => part.trim()).join('\n\n')
  return {
    role: 'user',
    content: [{
      type: 'text',
      text: [
        '<research_state>',
        'Injected research state for this request, not user input. It reports ledger state and can change between requests.',
        '',
        body,
        '</research_state>',
      ].join('\n'),
    }],
  }
}

interface CacheablePart {
  type?: string
  cache?: unknown
  [key: string]: unknown
}

/**
 * Part types whose cache hint survives request lowering. The installed
 * Anthropic adapter keeps `cache_control` on text and tool results only: it
 * rebuilds thinking and tool-use blocks without the hint, and the
 * OpenAI-compatible chat adapter does the same. Media is not included because
 * that adapter drops its hint too. Anything else is schema-legal but not a
 * usable boundary.
 */
const LOWERED_CACHE_PARTS = new Set(['text', 'tool-result'])
/**
 * Hints the SDK counts before lowering. This is deliberately wider than
 * LOWERED_CACHE_PARTS: the budget is spent by schema-legal hints even when a
 * protocol later drops them, so the count must match the SDK's own.
 */
const COUNTED_CACHE_PARTS = new Set(['text', 'media', 'tool-call', 'tool-result', 'reasoning'])
/** `@opencode/ai` counts manual hints against this cap before placing its own. */
const CACHE_BREAKPOINT_LIMIT = 4

function isLoweredCachePart(part: unknown): part is CacheablePart {
  return typeof part === 'object' && part !== null
    && LOWERED_CACHE_PARTS.has(String((part as CacheablePart).type))
}

function isCountedCachePart(part: unknown): part is CacheablePart {
  return typeof part === 'object' && part !== null
    && COUNTED_CACHE_PARTS.has(String((part as CacheablePart).type))
}

function hintCount(request: { system?: any[]; messages?: any[]; tools?: Record<string, any> }): number {
  let count = 0
  for (const tool of Object.values(request.tools ?? {})) if (tool?.cache !== undefined) count++
  for (const part of request.system ?? []) if (part?.cache !== undefined) count++
  for (const message of request.messages ?? []) {
    for (const part of Array.isArray(message?.content) ? message.content : []) {
      if (isCountedCachePart(part) && part.cache !== undefined) count++
    }
  }
  return count
}

/**
 * Put the SDK's cache hint on the last content part of the persistent history
 * whose hint survives lowering, so the breakpoint before an injected tail is
 * reusable. Returns the messages to use, or undefined when the request must
 * fall back to the system placement: no lowered boundary exists, or the SDK
 * marker budget is already spent.
 */
export function markHistoryCacheBoundary(request: {
  system?: any[]
  messages?: any[]
  tools?: Record<string, any>
}): any[] | undefined {
  const messages = request.messages
  if (!Array.isArray(messages) || !messages.length) return undefined
  const messageIndex = messages.findLastIndex(message =>
    Array.isArray(message?.content) && message.content.some(isLoweredCachePart))
  if (messageIndex < 0) return undefined
  const message = messages[messageIndex]
  const partIndex = message.content.findLastIndex(isLoweredCachePart)
  const part = message.content[partIndex] as CacheablePart
  // An explicit hint here is already the reusable boundary; keep it as is.
  if (part.cache !== undefined) return messages
  if (hintCount(request) >= CACHE_BREAKPOINT_LIMIT) return undefined
  const content = message.content.map((item: CacheablePart, index: number) =>
    index === partIndex ? { ...item, cache: { type: 'ephemeral' } } : item)
  const next = messages.slice()
  next[messageIndex] = { ...message, content }
  return next
}
